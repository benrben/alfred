"""Config-writer tests (e3).

_set_config_kv now round-trips the TOML document with tomlkit instead of
hand-editing lines, so set-intent/set-model/set-processing preserve the user's
comments and formatting. These assert, against a temp config.toml that HAS
comments:

  - the value is written correctly (round-trips through the real config loader),
  - existing comments survive the edit,
  - a .bak of the pre-edit file is kept.

External behaviour (which keys get written, validation) is unchanged from the
regex version; only the in-place editing mechanism differs.

Run: ./.venv/bin/python -m pytest tests/test_config_writer.py -q
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402


def _quiet(fn, *a, **kw):
    """Run a cmd_* function, swallowing its VB_STATUS stdout line."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **kw)

_SEED = """\
# Alfred config — user comment that must survive edits.
[processing]
mode = "raw"  # default intent (inline comment)
rewrite = false

[llm]
backend = "local"  # keep transforms on-device
"""


class ConfigWriter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "config.toml"
        self.path.write_text(_SEED, encoding="utf-8")

    def _ns(self, **kw):
        kw.setdefault("config", str(self.path))
        return type("NS", (), kw)()

    def test_set_processing_writes_and_preserves_comments(self):
        # set-processing: change mode (existing) + enable rewrite/translate (one
        # existing, one new key), exactly as the front-end would call it.
        rc = _quiet(vb.cmd_set_processing,
                    self._ns(mode="email", rewrite=True, translate=None,
                             optimize=None))
        self.assertEqual(rc, 0)

        cfg = vb.load_config(str(self.path))
        self.assertEqual(cfg["processing"]["mode"], "email")
        self.assertTrue(cfg["processing"]["rewrite"])

        out = self.path.read_text(encoding="utf-8")
        self.assertIn("# Alfred config", out)                  # header comment
        self.assertIn("# default intent (inline comment)", out)  # inline comment
        self.assertIn("# keep transforms on-device", out)

    def test_set_model_writes_into_llm_section(self):
        rc = _quiet(vb.cmd_set_model, self._ns(backend="claude", model="opus"))
        self.assertEqual(rc, 0)
        cfg = vb.load_config(str(self.path))
        self.assertEqual(cfg["llm"]["claude_model"], "opus")
        # The pre-existing llm comment is untouched.
        self.assertIn("# keep transforms on-device",
                      self.path.read_text(encoding="utf-8"))

    def test_bak_is_kept(self):
        _quiet(vb.cmd_set_processing,
               self._ns(mode="notes", rewrite=None, translate=None,
                        optimize=None))
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        self.assertTrue(bak.exists(), "a .bak of the pre-edit file must be kept")
        # The .bak holds the ORIGINAL (pre-edit) content.
        self.assertEqual(bak.read_text(encoding="utf-8"), _SEED)

    def test_boolean_stage_toggle_is_real_bool(self):
        # set-processing writes TOML booleans (not the string "true").
        _quiet(vb.cmd_set_processing,
               self._ns(mode=None, rewrite=None, translate=True,
                        optimize=False))
        cfg = vb.load_config(str(self.path))
        self.assertIs(cfg["processing"]["translate"], True)
        self.assertIs(cfg["processing"]["optimize"], False)

    def test_direct_kv_replaces_value_keeping_inline_comment(self):
        # Lower-level: replacing an existing key keeps its inline comment.
        vb._set_config_kv(self.path, "processing", "mode", vb._toml_str("commit"))
        out = self.path.read_text(encoding="utf-8")
        self.assertIn('mode = "commit"', out)
        self.assertIn("# default intent (inline comment)", out)

    def test_set_stt_writes_initial_prompt(self):
        rc = _quiet(vb.cmd_set_stt,
                    self._ns(initial_prompt="Reich, Alfred, mlx", language=None))
        self.assertEqual(rc, 0)
        cfg = vb.load_config(str(self.path))
        self.assertEqual(cfg["stt"]["initial_prompt"], "Reich, Alfred, mlx")
        self.assertIn("# Alfred config",
                      self.path.read_text(encoding="utf-8"))   # comments kept


class SetIntentWriter(unittest.TestCase):
    """set-intent now uses tomlkit (not regex). The .bak must hold the PRISTINE
    pre-edit file so a restore recovers the previous prompt, a prompt containing
    a '[' line must not corrupt the file, and existing extra keys survive."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = Path(self.dir) / "config.toml"
        self.path.write_text(
            '[intent.email]\nprompt = "old prompt"\nreplace = true\n\n'
            '# keep me\n[llm]\nbackend = "local"\n', encoding="utf-8")

    def _ns(self, **kw):
        kw.setdefault("config", str(self.path))
        kw.setdefault("label", None)
        kw.setdefault("description", None)
        return type("NS", (), kw)()

    def test_overwrite_keeps_pristine_bak_and_extra_keys(self):
        rc = _quiet(vb.cmd_set_intent,
                    self._ns(key="email",
                             prompt="new line\n[not a table] still prompt"))
        self.assertEqual(rc, 0)
        cfg = vb.load_config(str(self.path))
        cat = {m["key"]: m for m in vb.mode_catalog(cfg)}
        self.assertIn("[not a table]", cat["email"]["prompt"])   # no truncation
        self.assertTrue(cat["email"].get("replace"))             # extra key kept
        self.assertEqual(cfg["llm"]["backend"], "local")         # other section intact
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        self.assertIn("old prompt", bak.read_text(encoding="utf-8"))  # pristine

    def test_add_new_intent(self):
        rc = _quiet(vb.cmd_set_intent,
                    self._ns(key="standup", prompt="Summarize as standup notes"))
        self.assertEqual(rc, 0)
        cat = {m["key"]: m for m in vb.mode_catalog(vb.load_config(str(self.path)))}
        self.assertIn("standup", cat)

    def test_invalid_key_rejected(self):
        rc = _quiet(vb.cmd_set_intent, self._ns(key="bad key!", prompt="x"))
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
