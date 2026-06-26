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
import sys
import unittest

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


def _cfg(**output):
    cfg = vb.load_config(NO_CFG)
    cfg["output"].update(output)
    return cfg


class DeliverRouting(unittest.TestCase):
    def test_short_text_is_copied(self):
        sink = FakeSink()
        kind, path = vb.deliver("hello", _cfg(size_threshold=2000), False,
                                sink=sink)
        self.assertEqual((kind, path), ("copied", None))
        self.assertEqual(sink.copied, ["hello"])
        self.assertEqual(sink.written, [])
        self.assertEqual(sink.pastes, 0)

    def test_over_threshold_is_saved_to_file(self):
        sink = FakeSink()
        big = "x" * 3000
        kind, path = vb.deliver(big, _cfg(size_threshold=2000), False,
                                sink=sink)
        self.assertEqual(kind, "saved")
        self.assertIsNotNone(path)                  # the saved file path
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
        kind, path = vb.deliver(big, _cfg(size_threshold=0), False, sink=sink)
        self.assertEqual((kind, path), ("copied", None))
        self.assertEqual(sink.copied, [big])
        self.assertEqual(sink.written, [])

    def test_do_paste_calls_paste_after_copy(self):
        sink = FakeSink()
        kind, path = vb.deliver("hi", _cfg(size_threshold=2000), True,
                                sink=sink)
        self.assertEqual((kind, path), ("copied", None))
        self.assertEqual(sink.copied, ["hi"])
        self.assertEqual(sink.pastes, 1)

    def test_empty_text_is_empty_no_side_effects(self):
        for txt in ("", "   ", "\n\t  "):
            sink = FakeSink()
            kind, path = vb.deliver(txt, _cfg(), True, sink=sink)
            self.assertEqual((kind, path), ("empty", None))
            self.assertEqual(sink.copied, [])
            self.assertEqual(sink.written, [])
            self.assertEqual(sink.pastes, 0)

    def test_saved_path_uses_configured_format_and_dir(self):
        sink = FakeSink()
        cfg = _cfg(size_threshold=10, save_dir="/tmp/vb-test-out",
                   save_format="txt")
        _, path = vb.deliver("x" * 50, cfg, False, sink=sink)
        self.assertTrue(path.startswith(os.path.expanduser("/tmp/vb-test-out")))
        self.assertTrue(path.endswith(".txt"))


if __name__ == "__main__":
    unittest.main()
