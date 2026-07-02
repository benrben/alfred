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
