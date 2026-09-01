"""Interface tests for the capture-command orchestration (cmd_process /
cmd_stream_start / cmd_stream_finish / cmd_text + _finish_capture).

These assert the *observable contract* of the capture commands at their seam,
with transcription and the LLM stubbed so no model or real audio is needed:

  - the resilient invariant: when the LLM step RAISES, the raw transcript is
    still delivered and the VB_STATUS line is suffixed `llm_failed`;
  - an empty transcript -> "empty" status, no delivery;
  - cmd_text routes stdin vs the positional arg, and routes through
    process_text (no --instruction) vs refine_text (with --instruction);
  - successful capture delivers the PROCESSED text and a clean status.

Delivery is captured via a fake `deliver` (the cmd-level call constructs the
real MacosSink, so we never let it run) — we assert the text handed to delivery
and the status line, not the clipboard.

Run: ./.venv/bin/python -m pytest tests/test_capture_commands.py -q
"""

import io
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


def _ns(**kw):
    """A minimal args namespace; unset capture flags default to None like argparse."""
    defaults = dict(config=NO_CFG, backend=None, model=None, language=None,
                    mode=None, translate=None, rewrite=None, optimize=None,
                    paste=None, stdout=False)
    defaults.update(kw)
    return type("NS", (), defaults)()


class _Capture:
    """Swap out the side-effecting collaborators so a capture command runs end
    to end in memory: deliver records (text, kind), history/print are inert, and
    progress writes nowhere."""

    def __init__(self, deliver_raises: Exception | None = None):
        # Set to make the fake deliver() raise, so tests can assert the
        # delivery-failure path (history still recorded, "deliver_failed"
        # status) without touching real clipboard/file I/O.
        self.deliver_raises = deliver_raises

    def __enter__(self):
        self.delivered = []          # list of (text, do_paste)
        self.history = []            # list of (text, source) — history_append calls
        self.statuses = []           # list of status-line part tuples
        self.results = []            # list of VB_RESULT payloads (print_result)
        self._saves = {}

        self._orig = {
            "deliver": vb.deliver,
            "history_append": vb.history_append,
            "print_status": vb.print_status,
            "print_result": vb.print_result,
            "_progress_path": vb._progress_path,
        }

        def fake_deliver(text, cfg, do_paste, sink=None):
            if self.deliver_raises is not None:
                raise self.deliver_raises
            self.delivered.append((text, do_paste))
            return "copied", None, None      # (kind, path, paste_ok)

        def fake_status(*parts):
            self.statuses.append(tuple(parts))

        # Send progress JSON to a throwaway temp path so nothing pollutes ~.
        import tempfile
        self._tmp = Path(tempfile.mkdtemp()) / "progress.json"

        vb.deliver = fake_deliver
        vb.history_append = lambda text, cfg, source: self.history.append(
            (text, source))
        vb.print_status = fake_status
        vb.print_result = lambda text: self.results.append(text)
        vb._progress_path = lambda: self._tmp
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(vb, k, v)
        return False

    @property
    def last_status(self):
        return self.statuses[-1] if self.statuses else None


class FinishCaptureInvariants(unittest.TestCase):
    """_finish_capture is the shared tail; assert its resilient fallback."""

    def _cfg(self, **proc):
        cfg = vb.load_config(NO_CFG)
        cfg["processing"].update(proc)
        return cfg

    def test_llm_failure_degrades_to_raw_transcript(self):
        cfg = self._cfg(rewrite=True)        # an LLM stage is active
        orig = vb.process_text
        vb.process_text = lambda text, c: (_ for _ in ()).throw(
            RuntimeError("backend exploded"))
        try:
            with _Capture() as cap:
                rc = vb._finish_capture("raw words here", cfg, _ns(), vb._Progress())
        finally:
            vb.process_text = orig

        self.assertEqual(rc, 0)
        # The RAW transcript (not a processed one) was delivered.
        self.assertEqual([t for t, _ in cap.delivered], ["raw words here"])
        # Status carries the llm_failed suffix per the CONTRACT.
        self.assertEqual(cap.last_status, ("copied", "llm_failed"))

    def test_success_delivers_processed_text_clean_status(self):
        cfg = self._cfg(rewrite=True)
        orig = vb.process_text
        vb.process_text = lambda text, c: "PROCESSED:" + text
        try:
            with _Capture() as cap:
                rc = vb._finish_capture("hello", cfg, _ns(), vb._Progress())
        finally:
            vb.process_text = orig

        self.assertEqual(rc, 0)
        self.assertEqual([t for t, _ in cap.delivered], ["PROCESSED:hello"])
        self.assertEqual(cap.last_status, ("copied",))   # no llm_failed suffix

    def test_stdout_mode_writes_final_and_skips_delivery(self):
        cfg = self._cfg(rewrite=True)
        orig = vb.process_text
        vb.process_text = lambda text, c: "FINAL"
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with _Capture() as cap:
                rc = vb._finish_capture("x", cfg, _ns(stdout=True), vb._Progress())
        finally:
            vb.process_text = orig
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "FINAL\n")
        self.assertEqual(cap.delivered, [])              # nothing copied/saved

    def test_stdout_mode_keeps_raw_fallback_on_llm_failure(self):
        # --stdout has the same contract on the failure path as on success:
        # emit the recoverable raw transcript and do not switch to clipboard /
        # file delivery just because processing failed.
        cfg = self._cfg(rewrite=True)
        orig = vb.process_text
        vb.process_text = lambda text, c: (_ for _ in ()).throw(
            RuntimeError("backend exploded"))
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with _Capture() as cap:
                rc = vb._finish_capture(
                    "raw words here", cfg, _ns(stdout=True), vb._Progress())
        finally:
            vb.process_text = orig
            sys.stdout = old

        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "raw words here\n")
        self.assertEqual(cap.delivered, [])


class MaybeRemoveAudioBehavior(unittest.TestCase):
    """Direct tests for _maybe_remove_audio: the keep_audio escape hatch, and
    the OSError-swallowing guarantee (cleanup can never fail the command)."""

    def _cfg(self, keep_audio):
        cfg = vb.load_config(NO_CFG)
        cfg["output"]["keep_audio"] = keep_audio
        return cfg

    def test_keep_audio_true_leaves_file_in_place(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "rec.wav"
        path.write_bytes(b"\x00")
        vb._maybe_remove_audio(str(path), self._cfg(keep_audio=True))
        self.assertTrue(path.exists())

    def test_keep_audio_false_removes_file(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "rec.wav"
        path.write_bytes(b"\x00")
        vb._maybe_remove_audio(str(path), self._cfg(keep_audio=False))
        self.assertFalse(path.exists())

    def test_missing_file_oserror_is_swallowed(self):
        # No exception should escape even though the file never existed.
        vb._maybe_remove_audio("/no/such/dir/rec.wav", self._cfg(keep_audio=False))


class CmdProcessOrchestration(unittest.TestCase):
    def setUp(self):
        # Make a real (empty) file so the is_file() guard passes; the transcribe
        # stub never reads it. keep_audio defaults False, so process removes it.
        import tempfile
        self.audio = Path(tempfile.mkdtemp()) / "rec.wav"
        self.audio.write_bytes(b"\x00")
        self._orig_tx = vb.transcribe

    def tearDown(self):
        vb.transcribe = self._orig_tx

    def _ns_audio(self, **kw):
        return _ns(audio=str(self.audio), **kw)

    def test_missing_audio_reports_audio_not_found(self):
        with _Capture() as cap:
            rc = vb.cmd_process(_ns(audio="/no/such/file.wav"))
        self.assertEqual(rc, 2)
        self.assertEqual(cap.last_status, ("error", "audio_not_found"))

    def test_empty_transcript_yields_empty_status_no_delivery(self):
        vb.transcribe = lambda *a, **k: ("", None)
        with _Capture() as cap:
            rc = vb.cmd_process(self._ns_audio())
        self.assertEqual(rc, 0)
        self.assertEqual(cap.last_status, ("empty",))
        self.assertEqual(cap.delivered, [])

    def test_full_path_transcribe_then_process_then_deliver(self):
        vb.transcribe = lambda *a, **k: ("spoken text", "en")
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: "CLEAN(" + text + ")"
        try:
            with _Capture() as cap:
                rc = vb.cmd_process(self._ns_audio())
        finally:
            vb.process_text = orig_pt
        self.assertEqual(rc, 0)
        self.assertEqual([t for t, _ in cap.delivered], ["CLEAN(spoken text)"])
        self.assertEqual(cap.last_status, ("copied",))

    def test_stt_failure_reports_stt_failed(self):
        def boom(*a, **k):
            raise RuntimeError("whisper down")
        vb.transcribe = boom
        with _Capture() as cap:
            rc = vb.cmd_process(self._ns_audio())
        self.assertEqual(rc, 1)
        self.assertEqual(cap.last_status, ("error", "stt_failed"))

    def test_warns_when_translate_via_whisper_but_model_cant(self):
        # translate_via="whisper" isn't reachable through any CLI override, so
        # drive it via the loaded config (the way a config.toml would set it).
        # The default (turbo) model can't Whisper-translate, so cmd_process
        # must note it's falling back to the LLM instead of silently doing so.
        orig_load = vb.load_config

        def fake_load(path):
            cfg = orig_load(path)
            cfg["processing"]["translate"] = True
            cfg["processing"]["translate_via"] = "whisper"
            return cfg
        vb.load_config = fake_load
        vb.transcribe = lambda *a, **k: ("spoken text", "en")
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: text
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            with _Capture() as cap:
                rc = vb.cmd_process(self._ns_audio())
        finally:
            sys.stderr = old_stderr
            vb.load_config = orig_load
            vb.process_text = orig_pt
        self.assertEqual(rc, 0)
        self.assertIn("cannot Whisper-translate", buf.getvalue())
        self.assertEqual([t for t, _ in cap.delivered], ["spoken text"])


class CmdStreamFinishFallback(unittest.TestCase):
    """With no live session (daemon down), stream-finish falls back to a batch
    transcribe, then shares the same _finish_capture tail."""

    def setUp(self):
        import tempfile
        self.audio = Path(tempfile.mkdtemp()) / "rec.wav"
        self.audio.write_bytes(b"\x00")
        self._orig_tx = vb.transcribe
        vb._STREAMS.clear()

    def tearDown(self):
        vb.transcribe = self._orig_tx
        vb._STREAMS.clear()

    def test_no_session_falls_back_to_batch_transcribe(self):
        seen = {}
        def fake_tx(path, cfg, *, language, whisper_translate,
                    timestamps=False):
            seen["path"] = path
            return ("streamed words", "en")
        vb.transcribe = fake_tx
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: text.upper()
        try:
            with _Capture() as cap:
                rc = vb.cmd_stream_finish(_ns(audio=str(self.audio)))
        finally:
            vb.process_text = orig_pt
        self.assertEqual(rc, 0)
        self.assertEqual(seen["path"], str(self.audio))
        self.assertEqual([t for t, _ in cap.delivered], ["STREAMED WORDS"])

    def test_no_session_transcribe_failure_reports_stt_failed(self):
        def boom(*a, **k):
            raise RuntimeError("whisper down")
        vb.transcribe = boom
        with _Capture() as cap:
            rc = vb.cmd_stream_finish(_ns(audio=str(self.audio)))
        self.assertEqual(rc, 1)
        self.assertEqual(cap.last_status, ("error", "stt_failed"))

    def test_no_session_empty_transcript_yields_empty_status_no_delivery(self):
        vb.transcribe = lambda *a, **k: ("", None)
        with _Capture() as cap:
            rc = vb.cmd_stream_finish(_ns(audio=str(self.audio)))
        self.assertEqual(rc, 0)
        self.assertEqual(cap.last_status, ("empty",))
        self.assertEqual(cap.delivered, [])


class CmdStreamLive(unittest.TestCase):
    """The live streaming path (cmd_stream_start -> cmd_stream_finish) that every
    Raycast daemon dictation takes: session registration + finish-time override
    semantics (the format the user picks AFTER recording wins) + finish() failure."""

    def setUp(self):
        import tempfile
        self.audio = str(Path(tempfile.mkdtemp()) / "rec.wav")
        Path(self.audio).write_bytes(b"\x00")
        self._orig_ss = vb.StreamSession
        vb._STREAMS.clear()
        vb._ACTIVE_STREAM = None

    def tearDown(self):
        vb.StreamSession = self._orig_ss
        vb._STREAMS.clear()
        vb._ACTIVE_STREAM = None

    def _fake_session(self, text="streamed", lang="he", raise_on_finish=False):
        outer = self

        class FakeSession:
            def __init__(self, path, cfg, language, whisper_translate):
                self.path, self.cfg = path, cfg
                self.language, self.wt = language, whisper_translate
                self.stop = False
                self.started = 0.0
            def start(self):
                outer.started = True
            def finish(self):
                if raise_on_finish:
                    raise RuntimeError("stt exploded")
                return text, lang
        return FakeSession

    def test_start_registers_session_and_second_start_stops_the_first(self):
        vb.StreamSession = self._fake_session()
        with _Capture():
            rc = vb.cmd_stream_start(_ns(audio=self.audio))
        self.assertEqual(rc, 0)
        first = vb._STREAMS[self.audio]
        self.assertIs(vb._ACTIVE_STREAM, first)
        # A second start on the same path stops the first and replaces it.
        with _Capture():
            vb.cmd_stream_start(_ns(audio=self.audio))
        self.assertTrue(first.stop)
        self.assertIsNot(vb._STREAMS[self.audio], first)

    def test_start_reaps_a_stale_session_at_a_different_key(self):
        # The reap loop (stream-start's housekeeping for abandoned recordings)
        # must both (a) pop+stop an unrelated stale entry and (b) leave a
        # fresh, non-stale entry AT THE SAME KEY as this start alone so it can
        # still be found and stopped by the `old =` lookup right after.
        class Stub:
            def __init__(self, stop, started):
                self.stop = stop
                self.started = started

        stale = Stub(stop=True, started=0.0)          # already abandoned
        fresh_same_key = Stub(stop=False, started=time.monotonic())  # not stale
        vb._STREAMS["some/other.wav"] = stale
        vb._STREAMS[self.audio] = fresh_same_key

        vb.StreamSession = self._fake_session()
        with _Capture():
            rc = vb.cmd_stream_start(_ns(audio=self.audio))
        self.assertEqual(rc, 0)
        self.assertNotIn("some/other.wav", vb._STREAMS)   # reaped by the loop
        self.assertTrue(fresh_same_key.stop)               # stopped via `old`
        self.assertIsNot(vb._STREAMS[self.audio], fresh_same_key)  # replaced

    def test_timestamps_flag_discards_live_session_for_a_true_batch_transcribe(self):
        # Regression: --timestamps used to be silently honoured only on the
        # no-session batch path; against a LIVE session it was dropped with no
        # warning, and even if wired in, each streamed chunk's Whisper segments
        # restart their own clock at 0:00 (independent decode windows), so
        # honouring it there would emit WRONG elapsed-time markers. A live
        # session must be discarded and a real whole-file batch transcribe run
        # instead whenever --timestamps is requested — but that batch call must
        # still use the SESSION's config (base_cfg), not silently revert to
        # fresh defaults: --language he was set at record-start time only, so a
        # correct fallback still transcribes in Hebrew even though the
        # --timestamps finish call itself repeats no --language.
        vb.StreamSession = self._fake_session(text="SESSION TEXT (chunk-relative, wrong)")
        seen = {}

        def fake_tx(path, cfg, *, language, whisper_translate, timestamps=False):
            seen["timestamps"] = timestamps
            seen["path"] = path
            seen["language"] = language
            return "[0:00] batch transcript with real timestamps", "en"

        orig_tx = vb.transcribe
        vb.transcribe = fake_tx
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: text
        try:
            with _Capture() as cap:
                vb.cmd_stream_start(_ns(audio=self.audio, language="he"))
                rc = vb.cmd_stream_finish(_ns(audio=self.audio, timestamps=True))
        finally:
            vb.process_text = orig_pt
            vb.transcribe = orig_tx

        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"timestamps": True, "path": self.audio,
                                "language": "he"})
        self.assertEqual([t for t, _ in cap.delivered],
                         ["[0:00] batch transcript with real timestamps"])
        # The session was popped and stopped, not left dangling.
        self.assertNotIn(self.audio, vb._STREAMS)

    def test_timestamps_discard_finish_failure_still_falls_back_to_batch(self):
        # Regression-shaped: stopping the live session (purely to discard its
        # chunk-relative text before --timestamps forces a real batch
        # transcribe) can itself raise. That must be swallowed — logged, not
        # propagated — and the batch fallback must still run to completion.
        vb.StreamSession = self._fake_session(
            text="SESSION TEXT (chunk-relative, discarded)", raise_on_finish=True)
        seen = {}

        def fake_tx(path, cfg, *, language, whisper_translate, timestamps=False):
            seen["called"] = True
            return "batch transcript", "en"

        orig_tx = vb.transcribe
        vb.transcribe = fake_tx
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: text
        try:
            with _Capture() as cap:
                vb.cmd_stream_start(_ns(audio=self.audio))
                rc = vb.cmd_stream_finish(_ns(audio=self.audio, timestamps=True))
        finally:
            vb.process_text = orig_pt
            vb.transcribe = orig_tx

        self.assertEqual(rc, 0)
        self.assertTrue(seen.get("called"))
        self.assertEqual([t for t, _ in cap.delivered], ["batch transcript"])

    def test_finish_time_format_override_reaches_process_text(self):
        # Record with defaults, then finish with --mode email: the finish-time
        # cfg (rewrite forced on by mode) must be what process_text sees.
        vb.StreamSession = self._fake_session(text="hello there")
        seen = {}
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: seen.update(
            rewrite=c["processing"]["rewrite"], mode=c["processing"]["mode"]) or text
        try:
            with _Capture():
                vb.cmd_stream_start(_ns(audio=self.audio))
                rc = vb.cmd_stream_finish(_ns(audio=self.audio, mode="email"))
        finally:
            vb.process_text = orig_pt
        self.assertEqual(rc, 0)
        self.assertEqual(seen, {"rewrite": True, "mode": "email"})

    def test_finish_failure_reports_stt_failed(self):
        vb.StreamSession = self._fake_session(raise_on_finish=True)
        with _Capture() as cap:
            vb.cmd_stream_start(_ns(audio=self.audio))
            rc = vb.cmd_stream_finish(_ns(audio=self.audio))
        self.assertEqual(rc, 1)
        self.assertEqual(cap.last_status, ("error", "stt_failed"))


class CmdTextRouting(unittest.TestCase):
    def setUp(self):
        self._orig_pt = vb.process_text
        self._orig_rt = vb.refine_text
        self.process_calls = []
        self.refine_calls = []
        vb.process_text = lambda text, c: self.process_calls.append(text) or "P:" + text
        vb.refine_text = (lambda text, instr, c:
                          self.refine_calls.append((text, instr)) or "R:" + text)

    def tearDown(self):
        vb.process_text = self._orig_pt
        vb.refine_text = self._orig_rt

    def test_arg_text_routes_through_process_text(self):
        with _Capture() as cap:
            rc = vb.cmd_text(_ns(text="hello world", instruction=None))
        self.assertEqual(rc, 0)
        self.assertEqual(self.process_calls, ["hello world"])
        self.assertEqual(self.refine_calls, [])
        self.assertEqual([t for t, _ in cap.delivered], ["P:hello world"])

    def test_instruction_routes_through_refine_text(self):
        with _Capture() as cap:
            rc = vb.cmd_text(_ns(text="draft text", instruction="make it formal"))
        self.assertEqual(rc, 0)
        self.assertEqual(self.refine_calls, [("draft text", "make it formal")])
        self.assertEqual(self.process_calls, [])
        self.assertEqual([t for t, _ in cap.delivered], ["R:draft text"])

    def test_stdout_flag_writes_final_and_skips_delivery(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            with _Capture() as cap:
                rc = vb.cmd_text(_ns(text="hello world", instruction=None, stdout=True))
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue(), "P:hello world\n")
        self.assertEqual(cap.delivered, [])

    def test_stdin_dash_reads_from_stdin(self):
        old = sys.stdin
        sys.stdin = io.StringIO("piped in")
        try:
            with _Capture():
                rc = vb.cmd_text(_ns(text="-", instruction=None))
        finally:
            sys.stdin = old
        self.assertEqual(rc, 0)
        self.assertEqual(self.process_calls, ["piped in"])

    def test_processing_failure_reports_llm_failed(self):
        vb.process_text = lambda text, c: (_ for _ in ()).throw(RuntimeError("x"))
        with _Capture() as cap:
            rc = vb.cmd_text(_ns(text="boom", instruction=None))
        self.assertEqual(rc, 1)
        self.assertEqual(cap.last_status, ("error", "llm_failed"))


if __name__ == "__main__":
    unittest.main()


class DeliveryFailureIsRecoverable(unittest.TestCase):
    """Regression: a delivery failure (clipboard write, unwritable save_dir,
    ...) used to lose the capture outright — deliver() raised past
    history_append(), which ran AFTER it, so nothing was ever recorded.
    _deliver_and_report now appends to history BEFORE delivery, so the text
    stays recoverable via `history --copy 0` even when delivery itself blows
    up, and the caller sees a distinct "deliver_failed" status instead of a
    bare crash."""

    def _cfg(self, **proc):
        cfg = vb.load_config(NO_CFG)
        cfg["processing"].update(proc)
        return cfg

    def test_finish_capture_success_path_keeps_history_on_delivery_failure(self):
        cfg = self._cfg(rewrite=True)
        orig = vb.process_text
        vb.process_text = lambda text, c: "PROCESSED:" + text
        try:
            with _Capture(deliver_raises=PermissionError("save_dir unwritable")) as cap:
                rc = vb._finish_capture("hello", cfg, _ns(), vb._Progress())
        finally:
            vb.process_text = orig

        self.assertEqual(rc, 1)
        self.assertEqual(cap.delivered, [])              # delivery never landed
        # ... but the processed text was NOT lost — it's in history.
        self.assertEqual(cap.history, [("PROCESSED:hello", "stt")])
        self.assertEqual(cap.results, ["PROCESSED:hello"])
        self.assertEqual(cap.last_status, ("error", "deliver_failed"))

    def test_finish_capture_raw_fallback_keeps_history_on_delivery_failure(self):
        # Both the LLM stage AND delivery fail in the same capture.
        cfg = self._cfg(rewrite=True)
        orig = vb.process_text
        vb.process_text = lambda text, c: (_ for _ in ()).throw(
            RuntimeError("backend exploded"))
        try:
            with _Capture(deliver_raises=OSError("clipboard busy")) as cap:
                rc = vb._finish_capture("raw words here", cfg, _ns(), vb._Progress())
        finally:
            vb.process_text = orig

        self.assertEqual(rc, 1)
        self.assertEqual(cap.delivered, [])
        self.assertEqual(cap.history, [("raw words here", "stt")])
        self.assertEqual(cap.results, ["raw words here"])
        # Both failure suffixes are present: the deliver failure, then llm_failed.
        self.assertEqual(cap.last_status, ("error", "deliver_failed", "llm_failed"))

    def test_cmd_text_keeps_history_on_delivery_failure(self):
        orig = vb.process_text
        vb.process_text = lambda text, c: "P:" + text
        try:
            with _Capture(deliver_raises=PermissionError("boom")) as cap:
                rc = vb.cmd_text(_ns(text="hello world", instruction=None))
        finally:
            vb.process_text = orig

        self.assertEqual(rc, 1)
        self.assertEqual(cap.delivered, [])
        self.assertEqual(cap.history, [("P:hello world", "text")])
        self.assertEqual(cap.last_status, ("error", "deliver_failed"))

    def test_history_append_failure_does_not_block_delivery(self):
        # Regression: history_append() itself failing (a disk-full or
        # unwritable ~/.voicebridge — the exact class of error the
        # deliver_failed fix above was written for) used to propagate straight
        # out of _deliver_and_report UNCAUGHT, since it ran outside any
        # try/except. That meant deliver() never even ran (no clipboard/paste/
        # file at all) and progress.json was left stuck mid-flight forever.
        # History is a secondary feature (the `history` command / --copy) and
        # must never block the primary path: the actual delivery attempt.
        cfg = self._cfg(rewrite=True)
        orig_pt = vb.process_text
        vb.process_text = lambda text, c: "PROCESSED:" + text
        try:
            with _Capture() as cap:
                vb.history_append = lambda *a, **k: (_ for _ in ()).throw(
                    OSError("disk full"))
                rc = vb._finish_capture("hello", cfg, _ns(), vb._Progress())
        finally:
            vb.process_text = orig_pt

        self.assertEqual(rc, 0)      # delivery still succeeded
        self.assertEqual([t for t, _ in cap.delivered], ["PROCESSED:hello"])
        self.assertEqual(cap.results, ["PROCESSED:hello"])
        self.assertEqual(cap.last_status, ("copied",))
