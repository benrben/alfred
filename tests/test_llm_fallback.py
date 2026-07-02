"""Tests for the LLM execution layer that every claude/codex/auto stage crosses
but nothing used to cover: the run_llm_fallback loop (first-success-wins,
raise-last-error, auto-survives-a-broken-backend), the _run subprocess wrapper's
error normalization (a hung or missing CLI must speak RuntimeError so the
fallback loop understands it), and the keyless env-var stripping.

No CLI is spawned: run_llm / subprocess.run are stubbed at their seams.

Run: ./.venv/bin/python -m pytest tests/test_llm_fallback.py -q
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


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


if __name__ == "__main__":
    unittest.main()
