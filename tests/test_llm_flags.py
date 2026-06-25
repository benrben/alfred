"""Tests for reasoning-effort flags and the feedback/refine path.

- claude/codex commands carry the low reasoning-effort flag (configurable; empty
  omits it), so deep "thinking" is off for fast text transforms.
- refine_text applies a free-text instruction in one LLM call (the feedback loop).

Run: ./.venv/bin/python -m unittest discover -s tests   (LLM calls stubbed)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


def cfg(**llm):
    c = vb.load_config(NO_CFG)
    c["llm"].update(llm)
    return c


class ReasoningEffortFlags(unittest.TestCase):
    def test_defaults_are_low(self):
        c = vb.load_config(NO_CFG)
        self.assertEqual(c["llm"]["claude_effort"], "low")
        self.assertEqual(c["llm"]["codex_reasoning_effort"], "low")

    def test_warm_claude_cmd_has_effort(self):
        cmd = vb._claude_warm_cmd(cfg(claude_effort="low"))
        self.assertIn("--effort", cmd)
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_warm_claude_cmd_omits_effort_when_blank(self):
        self.assertNotIn("--effort", vb._claude_warm_cmd(cfg(claude_effort="")))

    def test_claude_oneshot_cmd_has_effort(self):
        captured = {}
        orig = vb.run_llm_clean
        vb.run_llm_clean = lambda cmd, env, timeout: captured.setdefault("cmd", cmd) or "ok"
        try:
            vb.run_llm("claude", "hi", cfg(claude_effort="low"))
        finally:
            vb.run_llm_clean = orig
        self.assertIn("--effort", captured["cmd"])
        self.assertEqual(captured["cmd"][captured["cmd"].index("--effort") + 1], "low")

    def test_codex_cmd_has_reasoning_effort(self):
        captured = {}
        orig = vb.run_llm_clean
        vb.run_llm_clean = lambda cmd, env, timeout: captured.setdefault("cmd", cmd) or "ok"
        try:
            vb.run_llm("codex", "hi", cfg(codex_reasoning_effort="low"))
        finally:
            vb.run_llm_clean = orig
        cmd = captured["cmd"]
        self.assertIn("-c", cmd)
        self.assertIn("model_reasoning_effort=low", cmd)  # bare value, no quotes

    def test_codex_omits_effort_when_blank(self):
        captured = {}
        orig = vb.run_llm_clean
        vb.run_llm_clean = lambda cmd, env, timeout: captured.setdefault("cmd", cmd) or "ok"
        try:
            vb.run_llm("codex", "hi", cfg(codex_reasoning_effort=""))
        finally:
            vb.run_llm_clean = orig
        self.assertNotIn("-c", captured["cmd"])


class RefineFeedback(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._fb, self._cb = vb.run_llm_fallback, vb.candidate_backends
        vb.candidate_backends = lambda c: ["fake"]
        vb.run_llm_fallback = lambda backends, prompt, c: self.calls.append(prompt) or "REVISED"

    def tearDown(self):
        vb.run_llm_fallback, vb.candidate_backends = self._fb, self._cb

    def test_refine_applies_instruction_in_one_call(self):
        out = vb.refine_text("The meeting is Tuesday.", "make it formal", cfg())
        self.assertEqual(out, "REVISED")
        self.assertEqual(len(self.calls), 1)
        prompt = self.calls[0]
        self.assertIn("make it formal", prompt)            # instruction carried
        self.assertIn("The meeting is Tuesday.", prompt)   # source text carried

    def test_refine_empty_instruction_is_passthrough(self):
        out = vb.refine_text("keep me", "", cfg())
        self.assertEqual(out, "keep me")
        self.assertEqual(self.calls, [])                   # no LLM call

    def test_refine_empty_text_passthrough(self):
        self.assertEqual(vb.refine_text("", "do something", cfg()), "")
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
