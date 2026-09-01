"""Tests for the LLM execution layer that every claude/codex/auto stage crosses
but nothing used to cover: the run_llm_fallback loop (first-success-wins,
raise-last-error, auto-survives-a-broken-backend), the _run subprocess wrapper's
error normalization (a hung or missing CLI must speak RuntimeError so the
fallback loop understands it), and the keyless env-var stripping.

No CLI is spawned: run_llm / subprocess.run are stubbed at their seams.

Run: ./.venv/bin/python -m pytest tests/test_llm_fallback.py -q
"""

import os
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


class FindTool(unittest.TestCase):
    """find_tool checks $PATH (shutil.which) first, then falls back to a
    fixed list of extra bin dirs a GUI launcher like Raycast often
    doesn't have on $PATH."""

    def setUp(self):
        self._orig_which = shutil.which
        self._orig_isfile = os.path.isfile
        self._orig_access = os.access

    def tearDown(self):
        shutil.which = self._orig_which
        os.path.isfile = self._orig_isfile
        os.access = self._orig_access

    def test_found_on_path(self):
        shutil.which = lambda name: f"/usr/bin/{name}"
        self.assertEqual(vb.find_tool("claude"), "/usr/bin/claude")

    def test_falls_back_to_extra_bin_dirs(self):
        shutil.which = lambda name: None
        target = os.path.join(vb._EXTRA_BIN_DIRS[0], "claude")

        def fake_isfile(p: str) -> bool:
            return p == target

        def fake_access(p: str, mode: int) -> bool:
            return p == target
        os.path.isfile = fake_isfile
        os.access = fake_access
        self.assertEqual(vb.find_tool("claude"), target)

    def test_not_found_anywhere_returns_none(self):
        shutil.which = lambda name: None

        def fake_isfile(p: str) -> bool:
            return False

        def fake_access(p: str, mode: int) -> bool:
            return False
        os.path.isfile = fake_isfile
        os.access = fake_access
        self.assertIsNone(vb.find_tool("nonexistent-binary-xyz"))


class CandidateBackends(unittest.TestCase):
    """candidate_backends picks the ordered list of backends run_llm_fallback
    tries. detect_backends() is called bare (same-module), so it's stubbed via
    the real submodule (vb.llm), not the vb.* re-export -- the re-export is a
    separate binding taken at package-import time and isn't seen by llm.py's
    own internal calls."""

    def setUp(self):
        self._orig_detect = vb.llm.detect_backends

    def tearDown(self):
        vb.llm.detect_backends = self._orig_detect

    def _cfg(self, backend):
        c = vb.load_config(NO_CFG)
        c["llm"]["backend"] = backend
        return c

    def test_local_backend_returns_local_without_detecting(self):
        vb.llm.detect_backends = lambda: (_ for _ in ()).throw(AssertionError("must not detect"))
        self.assertEqual(vb.candidate_backends(self._cfg("local")), ["local"])

    def test_claude_backend_found(self):
        vb.llm.detect_backends = lambda: {"claude": "/usr/bin/claude", "codex": None}
        self.assertEqual(vb.candidate_backends(self._cfg("claude")), ["claude"])

    def test_codex_backend_found(self):
        vb.llm.detect_backends = lambda: {"claude": None, "codex": "/usr/bin/codex"}
        self.assertEqual(vb.candidate_backends(self._cfg("codex")), ["codex"])

    def test_auto_backend_returns_both_when_found(self):
        vb.llm.detect_backends = lambda: {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"}
        self.assertEqual(vb.candidate_backends(self._cfg("auto")), ["claude", "codex"])

    def test_auto_backend_falls_back_to_whichever_is_found(self):
        vb.llm.detect_backends = lambda: {"claude": None, "codex": "/usr/bin/codex"}
        self.assertEqual(vb.candidate_backends(self._cfg("auto")), ["codex"])

    def test_none_found_raises(self):
        vb.llm.detect_backends = lambda: {"claude": None, "codex": None}
        with self.assertRaises(RuntimeError):
            vb.candidate_backends(self._cfg("auto"))

    def test_wanted_backend_not_found_raises(self):
        vb.llm.detect_backends = lambda: {"claude": None, "codex": None}
        with self.assertRaises(RuntimeError):
            vb.candidate_backends(self._cfg("claude"))


class RunLlmFallbackLoop(unittest.TestCase):
    def setUp(self):
        self.cfg = vb.load_config(NO_CFG)
        self._orig = vb.run_llm

    def tearDown(self):
        vb.run_llm = self._orig

    def test_first_success_wins_and_second_not_tried(self):
        tried = []

        def fake(backend, prompt, cfg):
            tried.append(backend)
            return f"out-from-{backend}"
        vb.run_llm = fake
        out = vb.run_llm_fallback(["claude", "codex"], "p", self.cfg)
        self.assertEqual(out, "out-from-claude")
        self.assertEqual(tried, ["claude"])          # codex never reached

    def test_first_backend_failure_falls_back_to_second(self):
        tried = []

        def fake(backend, prompt, cfg):
            tried.append(backend)
            if backend == "claude":
                raise RuntimeError("claude timed out after 120s")
            return "codex-output"
        vb.run_llm = fake
        out = vb.run_llm_fallback(["claude", "codex"], "p", self.cfg)
        self.assertEqual(out, "codex-output")
        self.assertEqual(tried, ["claude", "codex"])  # auto survived a broken backend

    def test_all_fail_raises_the_last_error(self):
        def fake(backend, prompt, cfg):
            raise RuntimeError(f"{backend} broke")
        vb.run_llm = fake
        with self.assertRaises(RuntimeError) as ctx:
            vb.run_llm_fallback(["claude", "codex"], "p", self.cfg)
        self.assertIn("codex broke", str(ctx.exception))   # the LAST error

    def test_empty_backends_raises(self):
        with self.assertRaises(RuntimeError):
            vb.run_llm_fallback([], "p", self.cfg)


class RunSubprocessErrorNormalization(unittest.TestCase):
    """_run must convert TimeoutExpired/OSError into RuntimeError (the one type
    the fallback loop catches) and must NOT leak the prompt in the message."""

    def setUp(self):
        self._orig = subprocess.run

    def tearDown(self):
        subprocess.run = self._orig

    def test_timeout_becomes_runtimeerror_without_leaking_prompt(self):
        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd=["claude", "SECRET PROMPT"],
                                            timeout=120)
        subprocess.run = boom
        with self.assertRaises(RuntimeError) as ctx:
            vb._run(["claude", "SECRET PROMPT"], {}, 120)
        msg = str(ctx.exception)
        self.assertIn("timed out", msg)
        self.assertNotIn("SECRET PROMPT", msg)       # prompt not leaked

    def test_oserror_becomes_runtimeerror(self):
        def boom(*a, **k):
            raise OSError("No such file or directory")
        subprocess.run = boom
        with self.assertRaises(RuntimeError) as ctx:
            vb._run(["codex", "x"], {}, 120)
        self.assertIn("failed to run", str(ctx.exception))

    def test_nonzero_exit_becomes_runtimeerror(self):
        def fake(*a, **k):
            return subprocess.CompletedProcess(a[0], 1, stdout="",
                                               stderr="Not logged in")
        subprocess.run = fake
        with self.assertRaises(RuntimeError) as ctx:
            vb._run(["claude", "x"], {}, 120)
        self.assertIn("Not logged in", str(ctx.exception))

    def test_nonzero_exit_with_only_noise_lines_falls_back_to_last_one(self):
        # No "real" error line among the output: _best_error_line must fall
        # back to the last noise line rather than surface nothing.
        def fake(*a, **k):
            return subprocess.CompletedProcess(a[0], 2, stdout="",
                                               stderr="Warning: minor\nNote: fyi\n")
        subprocess.run = fake
        with self.assertRaises(RuntimeError) as ctx:
            vb._run(["claude", "x"], {}, 120)
        self.assertIn("Note: fyi", str(ctx.exception))

    def test_nonzero_exit_with_no_output_reports_exit_code(self):
        def fake(*a, **k):
            return subprocess.CompletedProcess(a[0], 7, stdout="", stderr="")
        subprocess.run = fake
        with self.assertRaises(RuntimeError) as ctx:
            vb._run(["claude", "x"], {}, 120)
        self.assertIn("exit 7", str(ctx.exception))

    def test_successful_run_strips_stdout(self):
        def fake(*a, **k):
            return subprocess.CompletedProcess(a[0], 0, stdout="  hi there  \n", stderr="")
        subprocess.run = fake
        self.assertEqual(vb._run(["claude", "x"], {}, 120), "hi there")


class KeylessEnvStripping(unittest.TestCase):
    """The keyless / nothing-leaves-the-machine promise: _clean_env must drop the
    API-key AND provider-routing vars, not just the two API keys."""

    def test_claude_drops_keys_and_routing_vars(self):
        for var in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_USE_BEDROCK",
                    "ANTHROPIC_BASE_URL"):
            self.assertIn(var, vb._CLAUDE_KEY_VARS)
        env = vb._clean_env(vb._CLAUDE_KEY_VARS)
        for var in vb._CLAUDE_KEY_VARS:
            self.assertNotIn(var, env)

    def test_codex_drops_key_and_base_url(self):
        self.assertIn("OPENAI_BASE_URL", vb._CODEX_KEY_VARS)
        env = vb._clean_env(vb._CODEX_KEY_VARS)
        for var in vb._CODEX_KEY_VARS:
            self.assertNotIn(var, env)


class CleanEnvUserBackfill(unittest.TestCase):
    """USER (and derived LOGNAME) must be backfilled when a GUI launcher spawns
    us without it set, so claude/codex's macOS Keychain OAuth lookup works."""

    def test_user_already_set_is_left_alone_and_logname_backfilled(self):
        with patch.dict(os.environ, {"USER": "alice"}, clear=True):
            env = vb._clean_env([])
        self.assertEqual(env["USER"], "alice")
        self.assertEqual(env["LOGNAME"], "alice")

    def test_missing_user_is_backfilled_from_os_identity(self):
        with patch.dict(os.environ, {}, clear=True):
            env = vb._clean_env([])
        # pwd.getpwuid(os.getuid()) always resolves on a real macOS/posix box.
        self.assertTrue(env.get("USER"))
        self.assertEqual(env.get("LOGNAME"), env.get("USER"))

    def test_pwd_lookup_failure_leaves_user_and_logname_unset(self):
        with patch.dict(os.environ, {}, clear=True):
            with patch("pwd.getpwuid", side_effect=KeyError("no such uid")):
                env = vb._clean_env([])
        self.assertNotIn("USER", env)
        self.assertNotIn("LOGNAME", env)


class RunLlmClean(unittest.TestCase):
    def setUp(self):
        self._orig = subprocess.run

    def tearDown(self):
        subprocess.run = self._orig

    def test_strips_wrapping_from_successful_output(self):
        def fake(*a, **k):
            return subprocess.CompletedProcess(a[0], 0, stdout="```\nHELLO\n```\n", stderr="")
        subprocess.run = fake
        out = vb.run_llm_clean(["claude", "hi"], {}, 30)
        self.assertEqual(out, "HELLO")


class RunLlmClaudeDispatch(unittest.TestCase):
    """run_llm('claude', ...) prefers the shared warm session when available,
    and falls back to a one-shot run when there's no warm session or the warm
    turn fails."""

    def setUp(self):
        self.cfg = vb.load_config(NO_CFG)
        self._orig_get_warm = vb._get_warm
        self._orig_run_llm_clean = vb.run_llm_clean

    def tearDown(self):
        vb._get_warm = self._orig_get_warm
        vb.run_llm_clean = self._orig_run_llm_clean

    def test_uses_warm_session_when_available(self):
        class FakeWarm:
            def ask(self, prompt, timeout):
                return f"```\nWARM:{prompt}\n```"
        vb._get_warm = lambda cfg, env: FakeWarm()  # type: ignore[return-value]
        vb.run_llm_clean = lambda *a, **k: self.fail("must not fall back to one-shot")
        out = vb.run_llm("claude", "hi", self.cfg)
        self.assertEqual(out, "WARM:hi")             # fences stripped too

    def test_falls_back_to_oneshot_when_warm_fails(self):
        class FailingWarm:
            def ask(self, prompt, timeout):
                raise RuntimeError("warm claude timed out")
        vb._get_warm = lambda cfg, env: FailingWarm()  # type: ignore[return-value]
        captured = {}

        def fake_run_llm_clean(cmd, env, timeout):
            captured["cmd"] = cmd
            return "one-shot ok"
        vb.run_llm_clean = fake_run_llm_clean
        out = vb.run_llm("claude", "hi", self.cfg)
        self.assertEqual(out, "one-shot ok")
        self.assertIn("-p", captured["cmd"])         # real one-shot cmd built


class RunLlmMisc(unittest.TestCase):
    def setUp(self):
        self.cfg = vb.load_config(NO_CFG)

    def test_unknown_backend_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            vb.run_llm("bogus", "hi", self.cfg)
        self.assertIn("unknown backend", str(ctx.exception))

    def test_zero_timeout_means_no_timeout(self):
        self.cfg["llm"]["timeout"] = 0
        captured = {}
        orig = vb.run_llm_clean

        def fake_run_llm_clean(cmd, env, timeout):
            captured["timeout"] = timeout
            return "ok"
        vb.run_llm_clean = fake_run_llm_clean
        try:
            vb.run_llm("codex", "hi", self.cfg)
        finally:
            vb.run_llm_clean = orig
        self.assertIsNone(captured["timeout"])


class StripWrapping(unittest.TestCase):
    """_strip_wrapping removes accidental code fences and/or matching quotes
    around model output -- the transform every backend's output passes
    through before it reaches the clipboard."""

    def test_plain_text_is_unchanged(self):
        self.assertEqual(vb._strip_wrapping("  hello world  "), "hello world")

    def test_removes_code_fence_with_close(self):
        self.assertEqual(vb._strip_wrapping("```\nhello\nworld\n```"), "hello\nworld")

    def test_removes_unclosed_code_fence_leader_only(self):
        # No closing fence: only the opening ``` line is stripped.
        self.assertEqual(vb._strip_wrapping("```\nhello"), "hello")

    def test_removes_matching_double_quotes(self):
        self.assertEqual(vb._strip_wrapping('"hello"'), "hello")

    def test_removes_matching_single_quotes(self):
        self.assertEqual(vb._strip_wrapping("'hello'"), "hello")

    def test_mismatched_quotes_are_left_alone(self):
        self.assertEqual(vb._strip_wrapping("'hello\""), "'hello\"")

    def test_fence_and_quotes_together(self):
        self.assertEqual(vb._strip_wrapping('```\n"hello"\n```'), "hello")


if __name__ == "__main__":
    unittest.main()
