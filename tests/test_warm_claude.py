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
import subprocess
import sys
import tempfile
import textwrap
import time
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

# Emits blank/invalid-JSON/non-result noise before the real result, so ask()
# must skip several lines before it finds the terminal "result" message.
NOISY_ECHO = """
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
        print("", flush=True)                                     # blank line
        print("not json {{{", flush=True)                          # invalid JSON
        print(json.dumps({"type": "system", "subtype": "init"}), flush=True)  # non-result
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "result": "ECHO:" + content}),
              flush=True)
"""

EMPTY_RESULT = """
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        print(json.dumps({"type": "result", "subtype": "success",
                          "is_error": False, "result": "   "}), flush=True)
"""

ERROR_RESULT = """
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        print(json.dumps({"type": "result", "subtype": "error_during_execution",
                          "is_error": True, "result": ""}), flush=True)
"""

# Writes to stderr before reading stdin, so drain_err's for-loop actually
# drains a real line (not just an empty/immediately-closed stream).
STDERR_NOISE_ECHO = """
    import sys, json
    print("some diagnostic noise", file=sys.stderr, flush=True)
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

    def test_recycles_after_idle_expiry(self):
        w = self._warm(ECHO, idle=1)
        try:
            w.ask("one", timeout=10)
            pid1 = w._proc.pid
            w._last = time.monotonic() - 10_000  # pretend long idle
            w.ask("two", timeout=10)             # stale -> restart
            self.assertNotEqual(w._proc.pid, pid1)
        finally:
            w._stop()

    def test_ask_skips_blank_and_non_result_lines(self):
        w = self._warm(NOISY_ECHO)
        try:
            self.assertEqual(w.ask("hi", timeout=10), "ECHO:hi")
        finally:
            w._stop()

    def test_stderr_noise_is_drained_without_blocking(self):
        w = self._warm(STDERR_NOISE_ECHO)
        try:
            self.assertEqual(w.ask("hi", timeout=10), "ECHO:hi")
        finally:
            w._stop()

    def test_ask_empty_result_raises_without_stopping(self):
        # Empty output is surfaced as an error, but (unlike a real error
        # subtype) the session is left running for the next turn.
        w = self._warm(EMPTY_RESULT)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                w.ask("hi", timeout=10)
            self.assertIn("empty", str(ctx.exception))
            self.assertTrue(w._alive())
        finally:
            w._stop()

    def test_ask_error_result_stops_and_raises(self):
        w = self._warm(ERROR_RESULT)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                w.ask("hi", timeout=10)
            self.assertIn("warm claude error", str(ctx.exception))
            self.assertFalse(w._alive())
        finally:
            w._stop()

    def test_write_failure_stops_and_raises(self):
        w = self._warm(ECHO)  # cmd is never actually run; _proc is faked below

        class BrokenStdin:
            def write(self, s):
                raise OSError("broken pipe")

            def flush(self):
                pass

            def close(self):
                pass

        class FakeAliveProc:
            def __init__(self):
                self.stdin = BrokenStdin()

            def poll(self):
                return None  # looks alive, so ask() won't try to (re)start

            def terminate(self):
                pass

            def kill(self):
                pass

        w._proc = FakeAliveProc()
        try:
            with self.assertRaises(RuntimeError) as ctx:
                w.ask("hi", timeout=10)
            self.assertIn("warm claude write failed", str(ctx.exception))
            self.assertFalse(w._alive())          # write failure stops the process
        finally:
            w._stop()

    def test_wait_for_result_line_expired_deadline_stops_and_raises(self):
        # Direct test of the extracted deadline-check branch: exercised via
        # ask() only under a real race, so drive it deterministically here.
        w = self._warm(ECHO)  # never started; _proc stays None
        with self.assertRaises(RuntimeError) as ctx:
            w._wait_for_result_line(time.monotonic() - 1)
        self.assertIn("timed out", str(ctx.exception))


class WarmClaudeStopEdgeCases(unittest.TestCase):
    """_stop must be a no-op when there's nothing to stop, and must swallow
    (not raise through) a failure from any of its cleanup steps."""

    def test_stop_when_never_started_is_a_noop(self):
        w = vb.WarmClaude(["true"], dict(os.environ), max_turns=5, idle_secs=60)
        w._stop()  # must not raise even though _proc is already None
        self.assertIsNone(w._proc)

    def test_stop_swallows_errors_from_every_step(self):
        w = vb.WarmClaude(["true"], dict(os.environ), max_turns=5, idle_secs=60)

        class BoomStdin:
            def close(self):
                raise OSError("already closed")

        class BoomProc:
            def __init__(self):
                self.stdin = BoomStdin()

            def terminate(self):
                raise ProcessLookupError("already dead")

            def kill(self):
                raise ProcessLookupError("already dead")

        w._proc = BoomProc()
        w._stop()  # none of the three failures should propagate
        self.assertIsNone(w._proc)


class WarmClaudeStartThreads(unittest.TestCase):
    """_start's pump_out/drain_err threads must survive a broken stdout/stderr
    stream (e.g. the CLI's pipe breaks mid-read) instead of crashing silently
    -- pump_out still puts the sentinel so a waiting ask() unblocks."""

    def test_pump_and_drain_swallow_stream_errors(self):
        class RaisingStream:
            def __iter__(self):
                return self

            def __next__(self):
                raise RuntimeError("pipe broke")

        class NullStdin:
            def close(self):
                pass

        class FakeProc:
            def __init__(self):
                self.stdin = NullStdin()
                self.stdout = RaisingStream()
                self.stderr = RaisingStream()

            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

        orig_popen = subprocess.Popen
        subprocess.Popen = lambda *a, **k: FakeProc()
        w = vb.WarmClaude(["true"], dict(os.environ), max_turns=5, idle_secs=60)
        try:
            w._start()
            # pump_out's except path still puts the sentinel once its stream
            # iteration raises -- draining it proves the thread survived.
            self.assertIsNone(w._q.get(timeout=2))
        finally:
            subprocess.Popen = orig_popen
            w._stop()


class ShouldPrewarmClaude(unittest.TestCase):
    """The daemon's startup pre-warm must never spawn/prompt `claude` for
    'local' (strict-local: nothing leaves the machine) or 'codex' (no use for
    a claude session) — only 'claude' and 'auto' route there. Regression for a
    bug where the gate was `backend != "codex"`, so the shipped default
    (backend = "local") still spawned and prompted claude on every daemon
    start."""

    def _cfg(self, backend, warm=True):
        return {"llm": {"backend": backend, "warm": warm}}

    def test_local_backend_never_prewarms(self):
        self.assertFalse(vb._should_prewarm_claude(self._cfg("local")))

    def test_codex_backend_never_prewarms(self):
        self.assertFalse(vb._should_prewarm_claude(self._cfg("codex")))

    def test_claude_backend_prewarms(self):
        self.assertTrue(vb._should_prewarm_claude(self._cfg("claude")))

    def test_auto_backend_prewarms(self):
        self.assertTrue(vb._should_prewarm_claude(self._cfg("auto")))

    def test_warm_disabled_never_prewarms(self):
        self.assertFalse(vb._should_prewarm_claude(self._cfg("claude", warm=False)))


class GetWarm(unittest.TestCase):
    """_get_warm builds/returns the shared warm-claude session used by the
    daemon; disabled outside the daemon or when warm=False, and rebuilt when
    the effective config (cmd/max_turns/idle_secs) changes. _WARM/_WARM_SIG
    are plain globals declared in llm.py itself (never monkeypatched by any
    test), so tests reach them through the real submodule (vb.llm), not a
    vb.* re-export."""

    def setUp(self):
        self._orig_daemon_mode = vb._DAEMON_MODE
        self._orig_warm, self._orig_sig = vb.llm._WARM, vb.llm._WARM_SIG
        vb.llm._WARM, vb.llm._WARM_SIG = None, None

    def tearDown(self):
        if vb.llm._WARM is not None:
            vb.llm._WARM._stop()
        vb._DAEMON_MODE = self._orig_daemon_mode
        vb.llm._WARM, vb.llm._WARM_SIG = self._orig_warm, self._orig_sig

    def _cfg(self, **llm):
        c = vb.load_config("/nonexistent/alfred-test-config.toml")
        c["llm"].update(llm)
        return c

    def test_not_daemon_mode_returns_none(self):
        vb._DAEMON_MODE = False
        self.assertIsNone(vb._get_warm(self._cfg(), dict(os.environ)))

    def test_daemon_mode_but_warm_disabled_returns_none(self):
        vb._DAEMON_MODE = True
        self.assertIsNone(vb._get_warm(self._cfg(warm=False), dict(os.environ)))

    def test_daemon_mode_and_warm_enabled_creates_session(self):
        vb._DAEMON_MODE = True
        warm = vb._get_warm(self._cfg(), dict(os.environ))
        self.assertIsInstance(warm, vb.WarmClaude)

    def test_same_config_reuses_the_same_session(self):
        vb._DAEMON_MODE = True
        cfg = self._cfg()
        first = vb._get_warm(cfg, dict(os.environ))
        second = vb._get_warm(cfg, dict(os.environ))
        self.assertIs(first, second)

    def test_config_change_recycles_the_session(self):
        vb._DAEMON_MODE = True
        first = vb._get_warm(self._cfg(claude_model="sonnet"), dict(os.environ))
        second = vb._get_warm(self._cfg(claude_model="opus"), dict(os.environ))
        self.assertIsNot(first, second)


if __name__ == "__main__":
    unittest.main()
