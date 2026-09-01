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

    def test_set_stt_writes_language(self):
        # The other half of the tristate matrix from test_set_stt_writes_
        # initial_prompt: initial_prompt unset, language set.
        rc = _quiet(vb.cmd_set_stt,
                    self._ns(initial_prompt=None, language="he"))
        self.assertEqual(rc, 0)
        cfg = vb.load_config(str(self.path))
        self.assertEqual(cfg["stt"]["language"], "he")

    def test_backup_failure_does_not_block_the_write(self):
        # _backup_pristine's .bak write is best-effort: if the .bak path is
        # blocked (here, occupied by a directory of the same name), the OSError
        # must be swallowed and the real edit must still go through.
        bak = self.path.with_suffix(self.path.suffix + ".bak")
        bak.mkdir()
        vb._set_config_kv(self.path, "llm", "claude_model", vb._toml_str("opus"))
        cfg = vb.load_config(str(self.path))
        self.assertEqual(cfg["llm"]["claude_model"], "opus")

    def test_set_config_kv_creates_new_file_without_bak(self):
        # _set_config_kv on a path with no pre-existing file: no PRISTINE
        # content to back up, so no .bak should appear.
        fresh_dir = tempfile.mkdtemp()
        fresh_path = Path(fresh_dir) / "brand-new.toml"
        self.assertFalse(fresh_path.exists())
        vb._set_config_kv(fresh_path, "llm", "backend", vb._toml_str("claude"))
        cfg = vb.load_config(str(fresh_path))
        self.assertEqual(cfg["llm"]["backend"], "claude")
        bak = fresh_path.with_suffix(fresh_path.suffix + ".bak")
        self.assertFalse(bak.exists())


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

    def test_empty_key_rejected(self):
        # Blank-after-strip: fails on the "not key" arm rather than the regex.
        rc = _quiet(vb.cmd_set_intent, self._ns(key="   ", prompt="x"))
        self.assertEqual(rc, 2)

    def test_label_and_description_written(self):
        rc = _quiet(vb.cmd_set_intent,
                    self._ns(key="newkey", prompt="p", label="My Label",
                             description="My desc"))
        self.assertEqual(rc, 0)
        cat = {m["key"]: m for m in vb.mode_catalog(vb.load_config(str(self.path)))}
        self.assertEqual(cat["newkey"]["label"], "My Label")
        self.assertEqual(cat["newkey"]["description"], "My desc")

    def test_creates_new_config_file_from_scratch(self):
        # No pre-existing file: text="" -> no [intent] table yet -> no .bak.
        fresh_dir = tempfile.mkdtemp()
        fresh_path = Path(fresh_dir) / "config.toml"
        self.assertFalse(fresh_path.exists())
        rc = _quiet(vb.cmd_set_intent,
                    self._ns(config=str(fresh_path), key="newone", prompt="hi"))
        self.assertEqual(rc, 0)
        self.assertTrue(fresh_path.is_file())
        bak = fresh_path.with_suffix(fresh_path.suffix + ".bak")
        self.assertFalse(bak.exists())

    def test_error_status_when_verification_fails(self):
        # If the write somehow doesn't round-trip into mode_catalog, cmd_set_
        # intent must report "error" and return 1 rather than silently "saved".
        orig = vb.mode_catalog
        vb.mode_catalog = lambda cfg: []
        try:
            rc = _quiet(vb.cmd_set_intent, self._ns(key="ghost", prompt="x"))
        finally:
            vb.mode_catalog = orig
        self.assertEqual(rc, 1)


class ConfigTargetResolution(unittest.TestCase):
    """_config_target resolves --config first, then the CONFIG_SEARCH list,
    then a built-in default path; and (only when running inside the daemon)
    guards --config against a path outside CONFIG_SEARCH."""

    def setUp(self):
        self._orig_daemon_mode = vb._DAEMON_MODE
        self._orig_search = vb.CONFIG_SEARCH
        vb._DAEMON_MODE = False

    def tearDown(self):
        vb._DAEMON_MODE = self._orig_daemon_mode
        vb.CONFIG_SEARCH = self._orig_search

    def _ns(self, **kw):
        kw.setdefault("config", None)
        return type("NS", (), kw)()

    def test_explicit_config_returned_outside_daemon(self):
        target = vb._config_target(self._ns(config="/some/explicit/path.toml"))
        self.assertEqual(target, Path("/some/explicit/path.toml").expanduser())

    def test_daemon_mode_allows_allowlisted_config(self):
        allowed = Path(tempfile.mkdtemp()) / "allowed.toml"
        vb.CONFIG_SEARCH = [allowed]
        vb._DAEMON_MODE = True
        target = vb._config_target(self._ns(config=str(allowed)))
        self.assertEqual(target, allowed)

    def test_daemon_mode_rejects_out_of_allowlist_config_and_falls_back(self):
        d = tempfile.mkdtemp()
        allowed = Path(d) / "allowed.toml"   # never created -> also not found
        vb.CONFIG_SEARCH = [allowed]
        vb._DAEMON_MODE = True
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            target = vb._config_target(self._ns(config="/tmp/not-allowed.toml"))
        self.assertIn("out-of-allowlist", stderr.getvalue())
        self.assertEqual(target, Path.home() / ".config" / "voicebridge" / "config.toml")

    def test_falls_back_to_first_existing_search_path(self):
        d = tempfile.mkdtemp()
        present = Path(d) / "present.toml"
        present.write_text("", encoding="utf-8")
        missing = Path(d) / "missing.toml"
        vb.CONFIG_SEARCH = [missing, present]
        target = vb._config_target(self._ns())
        self.assertEqual(target, present)

    def test_falls_back_to_default_when_nothing_in_search_exists(self):
        d = tempfile.mkdtemp()
        vb.CONFIG_SEARCH = [Path(d) / "nope.toml"]
        target = vb._config_target(self._ns())
        self.assertEqual(target, Path.home() / ".config" / "voicebridge" / "config.toml")


if __name__ == "__main__":
    unittest.main()
