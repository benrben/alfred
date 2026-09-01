"""Lifecycle tests for StreamSession — the live streaming-STT loop, driven
WITHOUT a real model and WITHOUT the background thread's timing.

The pure chunk helpers (_wav_data_offset/_pcm_sample_count/_read_pcm_f32/
_silence_cut) are covered in test_stream_helpers.py. Here we exercise the
SESSION: _transcribe windowing, _chunk_once gating, finish()'s tail drain, the
.text join, and the stream.json sidecar — by stubbing the ONE seam
(transcribe_samples) and redirecting the stream-state path to a temp dir.

We shrink _STREAM_TARGET/_STREAM_MAX so a few-KB WAV spans several chunks and
the tests stay fast and deterministic (no sleeps, no threads except the one
start()/finish() round-trip).

Run: ./.venv/bin/python -m pytest tests/test_stream_session.py -q
"""

import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

_HAVE_NP = importlib.util.find_spec("numpy") is not None


def _wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """A minimal canonical 16-bit-mono WAV (44-byte header + PCM)."""
    byte_rate = sample_rate * 2
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def _ramp_pcm(n: int) -> bytes:
    """n int16 samples of a low-amplitude ramp (non-silent so chunks have content)."""
    return b"".join(struct.pack("<h", 200 + (i % 4000)) for i in range(n))


@unittest.skipUnless(_HAVE_NP, "numpy not installed")
class StreamSessionLifecycle(unittest.TestCase):
    # Shrunk stream geometry so a small WAV spans multiple chunks.
    TARGET = 1200
    MAX = 1600
    FRAME = 800

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.stream_path = self.tmp / "stream.json"

        # Stub the model seam: record each call, return a per-call token + lang.
        self.calls = []

        def fake_transcribe_samples(audio, cfg, *, language, whisper_translate,
                                    initial_prompt="", decode_opts=None):
            self.calls.append({
                "n": int(getattr(audio, "size", len(audio))),
                "language": language,
                "whisper_translate": whisper_translate,
                "initial_prompt": initial_prompt,
                "decode_opts": decode_opts,
            })
            return f"c{len(self.calls)}", "he"

        # Patch the seams and shrink the chunk geometry.
        self._orig = {
            "transcribe_samples": vb.transcribe_samples,
            "_stream_path": vb._stream_path,
            "_STREAM_TARGET": vb._STREAM_TARGET,
            "_STREAM_MAX": vb._STREAM_MAX,
            "_STREAM_FRAME": vb._STREAM_FRAME,
            "_STREAM_PREVIEW_MIN": vb._STREAM_PREVIEW_MIN,
            "_STREAM_PREVIEW_SECS": vb._STREAM_PREVIEW_SECS,
        }
        vb.transcribe_samples = fake_transcribe_samples
        vb._stream_path = lambda: self.stream_path
        vb._STREAM_TARGET = self.TARGET
        vb._STREAM_MAX = self.MAX
        vb._STREAM_FRAME = self.FRAME
        vb._STREAM_PREVIEW_MIN = self.FRAME    # small: preview on any real tail
        vb._STREAM_PREVIEW_SECS = 0.0          # no throttle in tests
        # These sessions are created directly (not via cmd_stream_start); reset
        # the active-session guard so _write() isn't skipped by a prior test.
        vb._ACTIVE_STREAM = None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(vb, k, v)

    def _session(self, n_samples: int):
        path = str(self.tmp / "rec.wav")
        with open(path, "wb") as f:
            f.write(_wav_bytes(_ramp_pcm(n_samples)))
        return vb.StreamSession(path, {"stt": {"model": "x"}},
                                language="auto", whisper_translate=False)

    # ---- .text property ---------------------------------------------------
    def test_text_drops_empty_parts_joins_with_space_and_strips_ends(self):
        sess = self._session(0)
        # Empty ("") parts are dropped; the rest join with a single space and
        # the ends are stripped. (Internal whitespace is NOT collapsed.)
        sess.parts = ["", "hello", "", "world", ""]
        self.assertEqual(sess.text, "hello world")
        sess.parts = ["  spaced  ", "tail"]
        self.assertEqual(sess.text, "spaced   tail")  # ends stripped, inner kept

    # ---- _transcribe windowing -------------------------------------------
    def test_transcribe_appends_advances_no_rolling_prompt(self):
        sess = self._session(self.MAX * 2)
        sess.parts = ["earlier"]
        sess._transcribe(self.MAX)              # bounded window [0, MAX)
        self.assertEqual(sess.parts[-1], "c1")  # stub token appended
        self.assertGreater(sess.cursor, 0)      # cursor advanced past the cut
        # The rolling accumulated-text prompt was REMOVED (it amplified
        # hallucinated repeats); no per-chunk initial_prompt is passed.
        self.assertEqual(self.calls[0]["initial_prompt"], "")
        # Streaming disables cross-window conditioning to stop repeat bleed.
        self.assertEqual(self.calls[0]["decode_opts"],
                         {"condition_on_previous_text": False})
        self.assertFalse(self.calls[0]["whisper_translate"])

    def test_silent_chunk_is_skipped_not_transcribed(self):
        # A pure-silence window must NOT be sent to the model (Whisper
        # hallucinates phantom text on silence); the cursor still advances.
        import numpy as np
        sess = self._session(self.MAX * 2)
        # Force the read to return silence regardless of the on-disk ramp.
        orig = vb._read_pcm_f32
        vb._read_pcm_f32 = lambda *a, **k: np.zeros(self.MAX, dtype=np.float32)
        try:
            sess._transcribe(self.MAX)
        finally:
            vb._read_pcm_f32 = orig
        self.assertEqual(self.calls, [])        # model never called
        self.assertEqual(sess.parts, [])        # nothing appended
        self.assertGreater(sess.cursor, 0)      # silence consumed, not re-read

    def test_transcribe_noop_when_window_below_a_frame(self):
        sess = self._session(self.MAX)
        sess.cursor = vb._pcm_sample_count(sess.path, sess.data_off)  # nothing left
        sess._transcribe(sess.cursor + 10)
        self.assertEqual(self.calls, [])        # never called the model
        self.assertEqual(sess.parts, [])

    # ---- _chunk_once gating ----------------------------------------------
    def test_chunk_once_false_below_max_true_at_max(self):
        short = self._session(self.MAX - self.FRAME)   # < one MAX chunk available
        self.assertFalse(short._chunk_once())
        self.assertEqual(short.cursor, 0)

        full = self._session(self.MAX + self.FRAME)    # >= one MAX chunk available
        self.assertTrue(full._chunk_once())
        self.assertGreater(full.cursor, 0)
        self.assertEqual(full.parts, ["c1"])

    def test_chunk_once_picks_up_data_offset_once_header_is_written(self):
        # A session created while only a stub file exists (no 'data' tag yet,
        # so the offset falls back to 44) must notice once the real WAV header
        # (here with an extra chunk before 'data', so the true offset != 44)
        # has been written, rather than reading from the wrong byte forever.
        path = str(self.tmp / "rec.wav")
        with open(path, "wb") as f:
            f.write(b"RIFF" + b"\x00" * 4)          # no 'data' tag -> offset 44
        sess = vb.StreamSession(path, {"stt": {"model": "x"}},
                                language="auto", whisper_translate=False)
        self.assertEqual(sess.data_off, 44)

        pcm = _ramp_pcm(self.MAX + self.FRAME)
        extra = b"LIST" + struct.pack("<I", 4) + b"INFO"
        header = (
            b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
            + extra
            + b"data" + struct.pack("<I", len(pcm)) + pcm
        )
        with open(path, "wb") as f:
            f.write(header)
        real_off = vb._wav_data_offset(path)
        self.assertNotEqual(real_off, 44)

        self.assertTrue(sess._chunk_once())
        self.assertEqual(sess.data_off, real_off)

    # ---- finish() tail drain (no thread) ---------------------------------
    def test_finish_drains_tail_and_returns_text_and_lang(self):
        sess = self._session(self.FRAME * 3)    # one short tail, < MAX
        text, lang = sess.finish()
        self.assertEqual(text, "c1")            # single tail chunk
        self.assertEqual(lang, "he")
        self.assertTrue(sess.done)

    def test_finish_chunks_then_takes_final_tail_in_order(self):
        # ~3 MAX-sized chunks worth of audio -> several ordered parts.
        sess = self._session(self.MAX * 3 + self.FRAME)
        text, _ = sess.finish()
        self.assertGreaterEqual(len(sess.parts), 2)         # chunked, not one gulp
        self.assertEqual(sess.parts, sorted(sess.parts))    # c1,c2,... in order
        self.assertEqual(text, " ".join(sess.parts))
        # Whole recording consumed: nothing more than a frame left over.
        avail = vb._pcm_sample_count(sess.path, sess.data_off)
        self.assertLessEqual(avail - sess.cursor, self.FRAME)

    def test_finish_breaks_on_no_progress_safety_check(self):
        # If _transcribe somehow fails to advance the cursor (defensive: should
        # never happen in practice), finish()'s drain loop must not spin
        # forever — it breaks out instead.
        sess = self._session(self.MAX * 5)     # far more than one MAX chunk left
        sess._transcribe = lambda end: None    # stub: cursor never advances
        text, lang = sess.finish()
        self.assertEqual(sess.cursor, 0)       # loop broke without progress
        self.assertTrue(sess.done)
        self.assertEqual(text, "")             # nothing was ever committed
        self.assertIsNone(lang)

    # ---- live tail preview (builds the transcript between chunks) ----------
    def test_preview_shows_uncommitted_tail_before_a_chunk_commits(self):
        # Less than one full chunk of audio: nothing commits, but _preview()
        # transcribes the tail so display_text / stream.json show a live partial.
        sess = self._session(self.MAX - self.FRAME)
        self.assertFalse(sess._chunk_once())          # no full chunk yet
        self.assertTrue(sess._preview())              # preview ran
        self.assertEqual(sess.parts, [])              # still uncommitted
        self.assertTrue(sess.preview)                 # a live partial exists
        self.assertEqual(sess.display_text, sess.preview)
        live = json.loads(self.stream_path.read_text())
        self.assertEqual(live["transcript"], sess.preview)   # HUD shows the preview
        self.assertTrue(live["recording"])

    def test_preview_throttled_returns_false_within_window(self):
        sess = self._session(self.MAX * 2)
        self.assertTrue(sess._preview())           # first call succeeds (SECS=0.0)
        calls_after_first = len(self.calls)
        vb._STREAM_PREVIEW_SECS = 999999.0         # now well inside a throttle window
        self.assertFalse(sess._preview())          # throttled: no new model call
        self.assertEqual(len(self.calls), calls_after_first)

    def test_preview_returns_false_when_tail_below_preview_min(self):
        sess = self._session(self.FRAME // 2)      # less than PREVIEW_MIN of audio
        self.assertFalse(sess._preview())
        self.assertEqual(self.calls, [])
        self.assertEqual(sess.preview, "")

    def test_preview_returns_false_for_a_silent_tail(self):
        import numpy as np
        sess = self._session(self.MAX * 2)
        orig = vb._read_pcm_f32
        vb._read_pcm_f32 = lambda *a, **k: np.zeros(self.MAX, dtype=np.float32)
        try:
            self.assertFalse(sess._preview())
        finally:
            vb._read_pcm_f32 = orig
        self.assertEqual(self.calls, [])
        self.assertEqual(sess.preview, "")

    def test_preview_does_not_update_last_lang_when_lang_is_falsy(self):
        sess = self._session(self.MAX * 2)
        orig = vb.transcribe_samples
        vb.transcribe_samples = lambda *a, **k: ("partial text", None)
        try:
            self.assertTrue(sess._preview())
        finally:
            vb.transcribe_samples = orig
        self.assertEqual(sess.preview, "partial text")
        self.assertIsNone(sess.last_lang)          # unchanged: still the default

    def test_commit_absorbs_and_clears_the_preview(self):
        sess = self._session(self.MAX * 2)
        sess._preview()
        self.assertTrue(sess.preview)
        sess._transcribe(self.MAX)                    # commit a full chunk
        self.assertTrue(sess.parts)                   # committed text present
        self.assertEqual(sess.preview, "")            # preview cleared on commit
        self.assertEqual(sess.display_text, sess.text)

    def test_finish_clears_preview_and_returns_committed(self):
        sess = self._session(self.MAX * 2)
        sess._preview()
        self.assertTrue(sess.preview)
        text, _ = sess.finish()
        self.assertEqual(sess.preview, "")            # not left dangling
        self.assertEqual(text, sess.text)             # committed, not the preview

    # ---- stream.json sidecar ---------------------------------------------
    def test_write_sidecar_schema_and_done_flag(self):
        sess = self._session(self.FRAME * 3)
        sess._write()                            # mid-recording snapshot
        live = json.loads(self.stream_path.read_text())
        self.assertEqual(set(live),
                         {"transcript", "recording", "done", "ts", "path"})
        self.assertTrue(live["recording"])       # stop not yet set
        self.assertFalse(live["done"])

        sess.finish()
        final = json.loads(self.stream_path.read_text())
        self.assertFalse(final["recording"])     # stop set in finish()
        self.assertTrue(final["done"])
        self.assertEqual(final["transcript"], sess.text)

    def test_write_is_skipped_when_a_different_session_is_active(self):
        # A superseded/abandoned session must not clobber the shared
        # stream.json that the CURRENT recording's HUD is reading.
        sess = self._session(self.FRAME * 3)
        other = self._session(self.FRAME * 3)
        vb._ACTIVE_STREAM = other
        try:
            sess._write()
        finally:
            vb._ACTIVE_STREAM = None
        self.assertFalse(self.stream_path.exists())

    def test_write_swallows_errors_when_the_stream_path_is_unwritable(self):
        # _write must never let a sidecar-write failure crash the streaming
        # loop; it's swallowed.
        sess = self._session(self.FRAME * 3)
        blocker = self.tmp / "not_a_dir"
        blocker.write_text("x")                     # a FILE, not a directory
        bad_path = blocker / "nested" / "stream.json"
        orig = vb._stream_path
        vb._stream_path = lambda: bad_path
        try:
            sess._write()                            # must not raise
        finally:
            vb._stream_path = orig
        self.assertFalse(bad_path.exists())

    # ---- _track_idle (idle-poll bookkeeping extracted from _run) ---------
    def test_track_idle_resets_when_a_chunk_worked(self):
        sess = self._session(0)
        self.assertEqual(sess._track_idle(True, 100, 100, 5), 0)
        self.assertFalse(sess.stop)

    def test_track_idle_resets_when_avail_grew(self):
        sess = self._session(0)
        self.assertEqual(sess._track_idle(False, 200, 100, 5), 0)
        self.assertFalse(sess.stop)

    def test_track_idle_increments_when_stalled(self):
        sess = self._session(0)
        self.assertEqual(sess._track_idle(False, 100, 100, 5), 6)
        self.assertFalse(sess.stop)

    def test_track_idle_abandons_session_after_enough_stalled_polls(self):
        # Drive it for real to the production _STREAM_ABANDON_POLLS threshold
        # (this is a pure function — no sleeping involved — so no need to
        # shrink the real constant to keep the test fast).
        sess = self._session(0)
        idle = 0
        for _ in range(200):
            idle = sess._track_idle(False, 100, 100, idle)
            if sess.stop:
                break
        self.assertTrue(sess.stop)
        self.assertEqual(idle, vb._STREAM_ABANDON_POLLS)

    # ---- _run (the background chunk loop, driven directly/synchronously) --
    def test_run_refreshes_preview_when_no_chunk_commits(self):
        # Audio too short for a full chunk: _chunk_once() returns False every
        # time, so _run must fall through to _preview() instead of skipping it.
        sess = self._session(self.MAX - self.FRAME)
        real_chunk_once = sess._chunk_once

        def one_pass_then_stop():
            sess.stop = True          # let _run() exit after this one iteration
            return real_chunk_once()

        sess._chunk_once = one_pass_then_stop
        sess._run()                    # synchronous: exactly one iteration
        self.assertEqual(len(self.calls), 1)   # the preview call fired
        self.assertEqual(sess.preview, "c1")

    def test_run_logs_and_continues_past_a_chunk_error(self):
        sess = self._session(self.MAX - self.FRAME)

        def boom():
            sess.stop = True          # let _run() exit after this one iteration
            raise RuntimeError("boom")

        sess._chunk_once = boom
        captured = io.StringIO()
        orig_stderr = sys.stderr
        sys.stderr = captured
        try:
            sess._run()                # must not propagate the exception
        finally:
            sys.stderr = orig_stderr
        self.assertIn("stream chunk error: boom", captured.getvalue())

    # ---- start()/finish() round-trip (exercises the thread) --------------
    def test_start_then_finish_round_trip(self):
        sess = self._session(self.MAX * 2)       # fully-written WAV
        sess.start()                             # spawns the daemon thread
        text, lang = sess.finish()               # stops + joins + drains tail
        self.assertTrue(sess.done)
        self.assertTrue(text)                    # produced a transcript
        self.assertEqual(lang, "he")
        self.assertFalse(sess.thread.is_alive())  # thread joined cleanly


if __name__ == "__main__":
    unittest.main()
