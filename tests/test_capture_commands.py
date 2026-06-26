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

    def __enter__(self):
        self.delivered = []          # list of (text, do_paste)
        self.statuses = []           # list of status-line part tuples
        self._saves = {}

        self._orig = {
            "deliver": vb.deliver,
            "history_append": vb.history_append,
            "print_status": vb.print_status,
            "_progress_path": vb._progress_path,
        }

        def fake_deliver(text, cfg, do_paste, sink=None):
            self.delivered.append((text, do_paste))
            return "copied", None

        def fake_status(*parts):
            self.statuses.append(tuple(parts))

        # Send progress JSON to a throwaway temp path so nothing pollutes ~.
        import tempfile
        self._tmp = Path(tempfile.mkdtemp()) / "progress.json"

        vb.deliver = fake_deliver
        vb.history_append = lambda *a, **k: None
        vb.print_status = fake_status
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
        def fake_tx(path, cfg, *, language, whisper_translate):
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

    def test_stdin_dash_reads_from_stdin(self):
        old = sys.stdin
        sys.stdin = io.StringIO("piped in")
        try:
            with _Capture() as cap:
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
