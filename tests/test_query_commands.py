"""Interface tests for the read-only query commands (cmd_history / cmd_modes /
cmd_settings / cmd_doctor). They print to stdout; we capture and assert shape:

  - cmd_modes emits a JSON catalog with every built-in mode + a single default;
  - cmd_settings emits JSON exposing backend/model + a `processing` block;
  - cmd_history reads the JSONL ledger: lists newest-first, and `--copy N`
    re-copies item N (0 = most recent) — copy_clipboard is stubbed so no real
    clipboard I/O; out-of-range -> rc 2;
  - cmd_doctor prints status lines and returns 0 without crashing.

A nonexistent --config yields the shipped DEFAULTS only (hermetic).

Run: ./.venv/bin/python -m pytest tests/test_query_commands.py -q
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"

BUILTIN_KEYS = {"email", "message", "commit", "prompt", "notes", "raw"}


def _run(fn, args):
    """Run a cmd_* fn, returning (rc, captured_stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(args)
    return rc, buf.getvalue()


def _ns(**kw):
    kw.setdefault("config", NO_CFG)
    return type("NS", (), kw)()


class ModesCatalog(unittest.TestCase):
    def test_emits_json_array_with_builtins(self):
        rc, out = _run(vb.cmd_modes, _ns())
        self.assertEqual(rc, 0)
        catalog = json.loads(out)
        self.assertIsInstance(catalog, list)
        keys = {m["key"] for m in catalog}
        self.assertTrue(BUILTIN_KEYS.issubset(keys))
        # Every entry carries the picker fields the front-end relies on.
        for m in catalog:
            for field in ("key", "label", "description", "prompt", "default"):
                self.assertIn(field, m)

    def test_exactly_one_default_matches_config_mode(self):
        rc, out = _run(vb.cmd_modes, _ns())
        catalog = json.loads(out)
        defaults = [m["key"] for m in catalog if m["default"]]
        cfg_mode = vb.load_config(NO_CFG)["processing"]["mode"]
        self.assertEqual(defaults, [cfg_mode])


class SettingsJson(unittest.TestCase):
    def test_emits_backend_model_processing(self):
        rc, out = _run(vb.cmd_settings, _ns())
        self.assertEqual(rc, 0)
        s = json.loads(out)
        for key in ("backend", "claude_model", "codex_model",
                    "claude_models", "codex_models", "processing"):
            self.assertIn(key, s)
        # backend is the configured default.
        self.assertEqual(s["backend"], vb.load_config(NO_CFG)["llm"]["backend"])
        # processing block exposes the stage toggles as real booleans.
        proc = s["processing"]
        for key in ("mode", "rewrite", "translate", "optimize", "translate_via"):
            self.assertIn(key, proc)
        self.assertIsInstance(proc["rewrite"], bool)
        self.assertIn("opus", s["claude_models"])   # built-in preset list


class HistoryLedger(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = vb.load_config(NO_CFG)
        self.cfg["history"]["dir"] = self.dir
        # Seed three records the way history_append would.
        vb.history_append("first item", self.cfg, "stt")
        vb.history_append("second item", self.cfg, "text")
        vb.history_append("third item", self.cfg, "stt")
        self._orig_copy = vb.copy_clipboard
        self.copied = []
        vb.copy_clipboard = lambda text: self.copied.append(text)

    def tearDown(self):
        vb.copy_clipboard = self._orig_copy

    def _ns_hist(self, **kw):
        kw.setdefault("config", str(Path(self.dir) / "config-marker"))
        # cmd_history loads config from args.config; point it at a path that
        # resolves to our seeded dir via the [history].dir override. Simpler:
        # write a tiny config that sets the history dir.
        return type("NS", (), kw)()

    def test_missing_history_prints_placeholder(self):
        empty = vb.load_config(NO_CFG)
        empty_dir = tempfile.mkdtemp()
        empty["history"]["dir"] = empty_dir
        # Drive cmd_history against a config file pointing at the empty dir.
        cfg_path = Path(empty_dir) / "config.toml"
        cfg_path.write_text(f'[history]\ndir = "{empty_dir}"\n', encoding="utf-8")
        rc, out = _run(vb.cmd_history, _ns(config=str(cfg_path), limit=10, copy=None))
        self.assertEqual(rc, 0)
        self.assertIn("(no history yet)", out)

    def _config_pointing_here(self):
        cfg_path = Path(self.dir) / "config.toml"
        cfg_path.write_text(f'[history]\ndir = "{self.dir}"\n', encoding="utf-8")
        return str(cfg_path)

    def test_lists_newest_first(self):
        rc, out = _run(vb.cmd_history,
                       _ns(config=self._config_pointing_here(), limit=10, copy=None))
        self.assertEqual(rc, 0)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)
        # Newest first: index [0] is "third item".
        self.assertIn("third item", lines[0])
        self.assertIn("first item", lines[2])

    def test_copy_index_0_copies_most_recent(self):
        rc, out = _run(vb.cmd_history,
                       _ns(config=self._config_pointing_here(), limit=10, copy=0))
        self.assertEqual(rc, 0)
        self.assertEqual(self.copied, ["third item"])
        self.assertIn("copied item 0", out)

    def test_copy_index_1_copies_second_newest(self):
        rc, _ = _run(vb.cmd_history,
                     _ns(config=self._config_pointing_here(), limit=10, copy=1))
        self.assertEqual(rc, 0)
        self.assertEqual(self.copied, ["second item"])

    def test_copy_out_of_range_returns_2(self):
        rc, _ = _run(vb.cmd_history,
                     _ns(config=self._config_pointing_here(), limit=10, copy=99))
        self.assertEqual(rc, 2)
        self.assertEqual(self.copied, [])


class DoctorStatus(unittest.TestCase):
    def test_doctor_returns_0_and_mentions_deps(self):
        rc, out = _run(vb.cmd_doctor, _ns())
        self.assertEqual(rc, 0)
        self.assertIn("Alfred doctor", out)
        # Mentions the key dependencies / backends it checks.
        for needle in ("mlx_whisper", "Python", "LLM backend", "STT model"):
            self.assertIn(needle, out)


if __name__ == "__main__":
    unittest.main()
