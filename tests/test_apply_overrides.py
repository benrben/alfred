"""Tests for _apply_overrides — the flags->config seam EVERY front-end request
crosses (--backend/--mode/--model/--translate/--paste). test_cli.py asserts the
parser side ("unset flags stay None"); this closes the loop on the apply side.

Run: ./.venv/bin/python -m pytest tests/test_apply_overrides.py -q
"""

import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


def _ns(**kw):
    defaults = dict(backend=None, model=None, language=None, mode=None,
                    translate=None, rewrite=None, optimize=None, paste=None,
                    transcribe_only=None)
    defaults.update(kw)
    return type("NS", (), defaults)()


class ApplyOverrides(unittest.TestCase):
    def _cfg(self):
        return vb.load_config(NO_CFG)

    def test_all_none_leaves_config_untouched(self):
        cfg = self._cfg()
        before = vb.json.dumps(cfg, sort_keys=True)
        vb._apply_overrides(cfg, _ns())
        self.assertEqual(vb.json.dumps(cfg, sort_keys=True), before)

    def test_translate_tristate(self):
        # None leaves config; False overrides a config True; True sets it on.
        cfg = self._cfg()
        cfg["processing"]["translate"] = True
        vb._apply_overrides(cfg, _ns(translate=None))
        self.assertTrue(cfg["processing"]["translate"])
        vb._apply_overrides(cfg, _ns(translate=False))
        self.assertFalse(cfg["processing"]["translate"])

    def test_mode_forces_rewrite_except_raw(self):
        cfg = self._cfg()
        vb._apply_overrides(cfg, _ns(mode="email"))
        self.assertEqual(cfg["processing"]["mode"], "email")
        self.assertTrue(cfg["processing"]["rewrite"])   # mode!=raw enables rewrite

        cfg2 = self._cfg()
        vb._apply_overrides(cfg2, _ns(mode="raw"))
        self.assertFalse(cfg2["processing"]["rewrite"])  # raw does NOT force it

    def test_transcribe_only_forces_every_stage_off(self):
        # --transcribe-only wins over --translate and --mode's implicit rewrite:
        # a pure transcript, no LLM stage of any kind.
        cfg = self._cfg()
        vb._apply_overrides(
            cfg, _ns(mode="email", translate=True, optimize=True,
                     transcribe_only=True))
        self.assertFalse(cfg["processing"]["translate"])
        self.assertFalse(cfg["processing"]["rewrite"])
        self.assertFalse(cfg["processing"]["optimize"])

    def test_transcribe_only_none_leaves_stages(self):
        # Unset -> no effect (config's own stage settings stand).
        cfg = self._cfg()
        cfg["processing"]["translate"] = True
        vb._apply_overrides(cfg, _ns(transcribe_only=None))
        self.assertTrue(cfg["processing"]["translate"])

    def test_paste_flips_output_mode(self):
        cfg = self._cfg()
        vb._apply_overrides(cfg, _ns(paste=True))
        self.assertEqual(cfg["output"]["mode"], "paste")
        vb._apply_overrides(cfg, _ns(paste=False))
        self.assertEqual(cfg["output"]["mode"], "copy")

    def test_model_applies_only_to_the_resolved_backend(self):
        # --model must NOT fan out to all three backends (that broke auto-fallback
        # and risked a surprise local download). It targets the chosen backend.
        cfg = self._cfg()
        vb._apply_overrides(cfg, _ns(backend="claude", model="opus"))
        self.assertEqual(cfg["llm"]["claude_model"], "opus")
        self.assertNotEqual(cfg["llm"]["codex_model"], "opus")
        self.assertNotEqual(cfg["llm"]["local_model"], "opus")

        cfg2 = self._cfg()
        vb._apply_overrides(cfg2, _ns(backend="codex", model="gpt-x"))
        self.assertEqual(cfg2["llm"]["codex_model"], "gpt-x")
        self.assertNotEqual(cfg2["llm"]["claude_model"], "gpt-x")

        cfg3 = self._cfg()   # auto -> primary (claude) only, codex keeps default
        vb._apply_overrides(cfg3, _ns(backend="auto", model="sonnet"))
        self.assertEqual(cfg3["llm"]["claude_model"], "sonnet")
        self.assertNotEqual(cfg3["llm"]["codex_model"], "sonnet")

        cfg4 = self._cfg()   # local -> only local_model, not claude/codex
        vb._apply_overrides(cfg4, _ns(backend="local", model="my-hf-model"))
        self.assertEqual(cfg4["llm"]["local_model"], "my-hf-model")
        self.assertNotEqual(cfg4["llm"]["claude_model"], "my-hf-model")
        self.assertNotEqual(cfg4["llm"]["codex_model"], "my-hf-model")

    def test_unknown_mode_warns_but_still_applies(self):
        cfg = self._cfg()
        buf = io.StringIO()
        old_stderr = sys.stderr
        sys.stderr = buf
        try:
            vb._apply_overrides(cfg, _ns(mode="not-a-real-mode"))
        finally:
            sys.stderr = old_stderr
        self.assertEqual(cfg["processing"]["mode"], "not-a-real-mode")
        self.assertIn("unknown mode", buf.getvalue())
        self.assertTrue(cfg["processing"]["rewrite"])   # still != "raw"


if __name__ == "__main__":
    unittest.main()
