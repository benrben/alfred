"""Lifecycle tests for WarmClaude — the long-lived `claude` process fed prompts
over a stream-json pipe. Previously 0% covered. We drive it against a TINY fake
CLI (a python script that speaks the same stdin/stdout JSON protocol), so no real
claude binary or login is needed:

  - ask() round-trips a prompt -> result
  - a hung backend -> RuntimeError("timed out") and the process is stopped
  - a backend that exits -> RuntimeError("exited")
  - turn recycling restarts the process after max_turns

Run: ./.venv/bin/python -m pytest tests/test_warm_claude.py -q
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402


def _fake_cli(body: str) -> list:
    """Write a python script that reads stream-json from stdin and return the
    argv to run it."""
    p = os.path.join(tempfile.mkdtemp(), "fake_claude.py")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(textwrap.dedent(body))
    return [sys.executable, p]


ECHO = """
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        content = obj.get("message", {}).get("content", "")
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "result": "ECHO:" + content}),
              flush=True)
"""

HANG = """
    import sys, time
    for line in sys.stdin:
        time.sleep(30)          # never answer
"""

DIE = """
    import sys
    sys.exit(0)                 # exit immediately, before any result
"""


class WarmClaudeLifecycle(unittest.TestCase):
    def _warm(self, body, max_turns=25, idle=600):
        return vb.WarmClaude(_fake_cli(body), dict(os.environ),
                             max_turns=max_turns, idle_secs=idle)

    def test_ask_round_trips(self):
        w = self._warm(ECHO)
        try:
            self.assertEqual(w.ask("hello", timeout=10), "ECHO:hello")
            self.assertEqual(w.ask("again", timeout=10), "ECHO:again")
            self.assertTrue(w._alive())          # process kept warm across turns
        finally:
            w._stop()

    def test_timeout_raises_and_stops(self):
        w = self._warm(HANG)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                w.ask("hi", timeout=0.5)
            self.assertIn("timed out", str(ctx.exception))
            self.assertFalse(w._alive())         # wedged process was killed
        finally:
            w._stop()

    def test_dead_process_raises(self):
        w = self._warm(DIE)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                w.ask("hi", timeout=5)
            self.assertIn("exited", str(ctx.exception))
        finally:
            w._stop()

    def test_recycles_after_max_turns(self):
        w = self._warm(ECHO, max_turns=1)
        try:
            w.ask("one", timeout=10)
            pid1 = w._proc.pid
            w.ask("two", timeout=10)             # exceeds max_turns -> restart
            self.assertNotEqual(w._proc.pid, pid1)
        finally:
            w._stop()


if __name__ == "__main__":
    unittest.main()
