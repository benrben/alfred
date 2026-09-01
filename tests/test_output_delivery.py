"""Output-delivery routing tests (e2).

deliver() is pure routing over an injected sink: it decides copy-vs-file and
whether to paste; the sink performs the I/O. A FakeSink captures the calls in
memory so we assert the routing without touching the clipboard or disk:

  - short text          -> "copied", sink.copy() called, no file, no paste
  - text over threshold -> "saved",  sink.write_file() called, returns the path
  - do_paste=True       -> sink.paste() called after copy
  - empty/whitespace    -> "empty",  no side effects at all

Run: ./.venv/bin/python -m pytest tests/test_output_delivery.py -q
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


class FakeSink(vb.Sink):
    """In-memory sink: records every primitive call, no real I/O."""

    def __init__(self):
        self.copied = []
        self.written = []          # list of (text, path)
        self.pastes = 0

    def copy(self, text):
        self.copied.append(text)

    def write_file(self, text, path):
        self.written.append((text, path))
        return path

    def paste(self):
        self.pastes += 1
        return True                 # keystroke "delivered"


def _cfg(**output):
    cfg = vb.load_config(NO_CFG)
    cfg["output"].update(output)
    return cfg


class DeliverRouting(unittest.TestCase):
    def test_short_text_is_copied(self):
        sink = FakeSink()
        kind, path, paste_ok = vb.deliver("hello", _cfg(size_threshold=2000),
                                          False, sink=sink)
        self.assertEqual((kind, path, paste_ok), ("copied", None, None))
        self.assertEqual(sink.copied, ["hello"])
        self.assertEqual(sink.written, [])
        self.assertEqual(sink.pastes, 0)

    def test_over_threshold_is_saved_to_file(self):
        sink = FakeSink()
        big = "x" * 3000
        kind, path, paste_ok = vb.deliver(big, _cfg(size_threshold=2000), False,
                                          sink=sink)
        self.assertEqual(kind, "saved")
        self.assertIsNotNone(path)                  # the saved file path
        self.assertIsNone(paste_ok)                 # no paste attempted
        self.assertEqual(len(sink.written), 1)
        text, written_path = sink.written[0]
        self.assertEqual(text, big)
        self.assertEqual(written_path, path)        # deliver returns the path
        self.assertEqual(sink.copied, [])           # nothing copied
        self.assertEqual(sink.pastes, 0)

    def test_threshold_zero_never_saves(self):
        # size_threshold = 0 disables saving: even huge text is copied.
        sink = FakeSink()
        big = "y" * 5000
        kind, path, _ = vb.deliver(big, _cfg(size_threshold=0), False, sink=sink)
        self.assertEqual((kind, path), ("copied", None))
        self.assertEqual(sink.copied, [big])
        self.assertEqual(sink.written, [])

    def test_do_paste_calls_paste_after_copy(self):
        sink = FakeSink()
        kind, path, paste_ok = vb.deliver("hi", _cfg(size_threshold=2000), True,
                                          sink=sink)
        self.assertEqual((kind, path, paste_ok), ("copied", None, True))
        self.assertEqual(sink.copied, ["hi"])
        self.assertEqual(sink.pastes, 1)

    def test_paste_failure_is_reported(self):
        # A sink whose paste() returns False -> deliver reports paste_ok=False.
        class FailPaste(FakeSink):
            def paste(self):
                self.pastes += 1
                return False
        sink = FailPaste()
        kind, _, paste_ok = vb.deliver("hi", _cfg(size_threshold=2000), True,
                                       sink=sink)
        self.assertEqual(kind, "copied")
        self.assertIs(paste_ok, False)

    def test_empty_text_is_empty_no_side_effects(self):
        for txt in ("", "   ", "\n\t  "):
            sink = FakeSink()
            kind, path, paste_ok = vb.deliver(txt, _cfg(), True, sink=sink)
            self.assertEqual((kind, path, paste_ok), ("empty", None, None))
            self.assertEqual(sink.copied, [])
            self.assertEqual(sink.written, [])
            self.assertEqual(sink.pastes, 0)

    def test_restore_clipboard_snapshots_and_restores_in_paste_mode(self):
        # With restore_clipboard on, paste mode snapshots the prior clipboard and
        # puts it back after pasting (so dictation doesn't destroy it).
        class SnapSink(FakeSink):
            def __init__(self):
                super().__init__()
                self.restored = []
            def snapshot(self):
                return "PRIOR"
            def restore(self, data):
                self.restored.append(data)
        sink = SnapSink()
        vb.deliver("new text", _cfg(size_threshold=2000, restore_clipboard=True),
                   True, sink=sink)
        self.assertEqual(sink.copied, ["new text"])   # result copied for the paste
        self.assertEqual(sink.restored, ["PRIOR"])    # user's clipboard restored

    def test_restore_clipboard_failure_does_not_invalidate_a_successful_paste(self):
        # Regression: sink.restore() (putting the user's PRIOR clipboard back)
        # ran unguarded — if it raised, the exception unwound straight out of
        # deliver(), past the "copied" return that already happened, so an
        # already-successful copy+paste was reported as a crash/deliver_failed
        # instead of the success it actually was. Restoring the prior
        # clipboard is a secondary nicety, not the delivery itself, and must
        # never retroactively invalidate delivery that already succeeded.
        class RaisingRestoreSink(FakeSink):
            def snapshot(self):
                return "PRIOR"
            def restore(self, data):
                raise OSError("pbcopy busy")
        sink = RaisingRestoreSink()
        kind, path, paste_ok = vb.deliver(
            "new text", _cfg(size_threshold=2000, restore_clipboard=True),
            True, sink=sink)
        self.assertEqual((kind, path, paste_ok), ("copied", None, True))
        self.assertEqual(sink.copied, ["new text"])   # the real work still happened

    def test_restore_clipboard_is_a_declared_default_not_just_documented(self):
        # restore_clipboard is documented in config.example.toml but used to be
        # absent from DEFAULTS — every other [output] option lives there, which
        # is what makes DEFAULTS the actual config surface (settings/doctor/etc
        # can introspect it; a bare .get(..., False) elsewhere can't be told
        # apart from a typo'd key).
        self.assertIn("restore_clipboard", vb.DEFAULTS["output"])
        self.assertIs(vb.DEFAULTS["output"]["restore_clipboard"], False)

    def test_saved_path_uses_configured_format_and_dir(self):
        sink = FakeSink()
        cfg = _cfg(size_threshold=10, save_dir="/tmp/vb-test-out",
                   save_format="txt")
        _, path, _ = vb.deliver("x" * 50, cfg, False, sink=sink)
        self.assertTrue(path.startswith(os.path.expanduser("/tmp/vb-test-out")))
        self.assertTrue(path.endswith(".txt"))

    def test_saved_paths_are_unique_when_results_finish_in_same_second(self):
        # The old second-based name let concurrent large results overwrite the
        # same file. Freeze the clock so the regression is deterministic.
        class FrozenDateTime:
            @classmethod
            def now(cls):
                return datetime(2026, 8, 30, 12, 34, 56)

        cfg = _cfg(size_threshold=1, save_dir="/tmp/vb-test-out")
        with patch.object(vb._dt, "datetime", FrozenDateTime):
            first = vb._save_path(cfg)
            second = vb._save_path(cfg)
        self.assertNotEqual(first, second)


class MacosToolPath(unittest.TestCase):
    """_macos_tool resolves a stock macOS binary to its absolute path under
    /usr/bin or /bin (so delivery doesn't depend on a possibly-stripped
    $PATH, e.g. under a GUI launcher), falling back to the bare name if it
    isn't found in either."""

    def test_resolves_a_real_binary_to_an_absolute_path(self):
        # "ls" exists at /bin/ls or /usr/bin/ls on every macOS install.
        resolved = vb._macos_tool("ls")
        self.assertIn(resolved, ("/usr/bin/ls", "/bin/ls"))
        self.assertTrue(os.path.exists(resolved))

    def test_unknown_binary_falls_back_to_the_bare_name(self):
        name = "definitely-not-a-real-binary-xyz123"
        self.assertEqual(vb._macos_tool(name), name)


class SinkBaseClass(unittest.TestCase):
    """Sink's I/O primitives are abstract (subclasses implement them);
    snapshot/restore have real default behavior even in the base class."""

    def test_copy_write_file_paste_are_not_implemented(self):
        sink = vb.Sink()
        with self.assertRaises(NotImplementedError):
            sink.copy("x")
        with self.assertRaises(NotImplementedError):
            sink.write_file("x", "/tmp/y")
        with self.assertRaises(NotImplementedError):
            sink.paste()

    def test_snapshot_default_is_none(self):
        self.assertIsNone(vb.Sink().snapshot())

    def test_restore_with_none_does_not_copy(self):
        sink = FakeSink()
        sink.restore(None)
        self.assertEqual(sink.copied, [])

    def test_restore_with_data_copies_it(self):
        sink = FakeSink()
        sink.restore("prior clipboard contents")
        self.assertEqual(sink.copied, ["prior clipboard contents"])


class MacosSinkSubprocessCalls(unittest.TestCase):
    """MacosSink's real primitives shell out via subprocess; stub
    subprocess.run (same convention as tests/test_llm_fallback.py) so these
    run with no actual clipboard / System Events side effects."""

    def setUp(self):
        self.sink = vb.MacosSink()
        self._orig_run = subprocess.run

    def tearDown(self):
        subprocess.run = self._orig_run

    def test_copy_shells_out_to_pbcopy_with_utf8_locale(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0)

        subprocess.run = fake_run
        self.sink.copy("שלום")

        self.assertEqual(len(calls), 1)
        cmd, kwargs = calls[0]
        self.assertTrue(cmd[0].endswith("pbcopy"))
        self.assertEqual(kwargs["input"], "שלום")
        self.assertEqual(kwargs["env"]["LANG"], "en_US.UTF-8")
        self.assertEqual(kwargs["env"]["LC_ALL"], "en_US.UTF-8")
        self.assertTrue(kwargs["check"])

    def test_write_file_writes_a_real_utf8_file_and_makes_parent_dirs(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "nested", "out.txt")
        result = self.sink.write_file("hello שלום", path)
        self.assertEqual(result, path)
        self.assertEqual(Path(path).read_text(encoding="utf-8"), "hello שלום")

    def test_paste_returns_true_on_success(self):
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 0)
        self.assertTrue(self.sink.paste())

    def test_paste_returns_false_on_nonzero_exit(self):
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 1)
        self.assertFalse(self.sink.paste())

    def test_paste_returns_false_on_oserror(self):
        def boom(*a, **k):
            raise OSError("no osascript")

        subprocess.run = boom
        self.assertFalse(self.sink.paste())

    def test_paste_returns_false_on_timeout(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["osascript"], timeout=5)

        subprocess.run = boom
        self.assertFalse(self.sink.paste())

    def test_snapshot_returns_clipboard_contents_on_success(self):
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(
            a[0], 0, stdout="clipboard text")
        self.assertEqual(self.sink.snapshot(), "clipboard text")

    def test_snapshot_returns_none_on_nonzero_exit(self):
        subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], 1, stdout="")
        self.assertIsNone(self.sink.snapshot())

    def test_snapshot_returns_none_on_oserror(self):
        def boom(*a, **k):
            raise OSError("no pbpaste")

        subprocess.run = boom
        self.assertIsNone(self.sink.snapshot())

    def test_snapshot_returns_none_on_timeout(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["pbpaste"], timeout=5)

        subprocess.run = boom
        self.assertIsNone(self.sink.snapshot())


class ThinWrapperDelegation(unittest.TestCase):
    """copy_clipboard/auto_paste/save_to_file are thin wrappers around the
    shared default sink (_SINK) for callers with no sink to inject (e.g.
    history re-copy). Patch _SINK's own methods directly: reassigning
    vb._SINK = fake would only rebind the PACKAGE-level name — pipeline.py's
    own module global, which these wrappers actually read, is a separate
    binding and would be untouched (see voicebridge/__init__.py's docstring
    on why submodules and monkeypatches must go through _pkg./the shared
    object rather than a bare or package-level name)."""

    def setUp(self):
        self._orig_copy = vb._SINK.copy
        self._orig_paste = vb._SINK.paste
        self._orig_write_file = vb._SINK.write_file

    def tearDown(self):
        vb._SINK.copy = self._orig_copy
        vb._SINK.paste = self._orig_paste
        vb._SINK.write_file = self._orig_write_file

    def test_copy_clipboard_delegates_to_the_shared_sink(self):
        calls = []
        vb._SINK.copy = calls.append
        vb.copy_clipboard("hello")
        self.assertEqual(calls, ["hello"])

    def test_auto_paste_delegates_to_the_shared_sink(self):
        calls = []
        vb._SINK.paste = lambda: calls.append(True)
        vb.auto_paste()
        self.assertEqual(calls, [True])

    def test_save_to_file_delegates_to_the_shared_sink(self):
        calls = []

        def fake_write(text, path):
            calls.append((text, path))
            return path

        vb._SINK.write_file = fake_write
        cfg = _cfg(save_dir="/tmp/vb-test-out")

        result = vb.save_to_file("content", cfg)

        self.assertEqual(calls, [("content", result)])


if __name__ == "__main__":
    unittest.main()
