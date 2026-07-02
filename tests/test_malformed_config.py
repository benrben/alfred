"""A hand-edit typo in config.toml must not brick every command. load_config
falls back to built-in DEFAULTS + records _config_error; cmd_doctor surfaces it;
and cmd_history skips a corrupt JSONL line instead of crashing.

Run: ./.venv/bin/python -m pytest tests/test_malformed_config.py -q
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402


class MalformedConfig(unittest.TestCase):
    def _write(self, text):
        p = Path(tempfile.mkdtemp()) / "config.toml"
        p.write_text(text, encoding="utf-8")
        return str(p)

    def test_bad_toml_falls_back_to_defaults_with_error_recorded(self):
        bad = self._write('[stt]\nlanguage = "he\n')     # unterminated string
        cfg = vb.load_config(bad)
        # Fell back to defaults (didn't crash) and recorded the parse error.
        self.assertEqual(cfg["llm"]["backend"], vb.DEFAULTS["llm"]["backend"])
        self.assertIn("_config_error", cfg)
        self.assertNotIn("_loaded_from", cfg)

    def test_doctor_surfaces_the_parse_error(self):
        bad = self._write("this is = not = toml =\n")
        args = type("NS", (), {"config": bad})()
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = vb.cmd_doctor(args)
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)                       # doctor still runs
        self.assertIn("PARSE ERROR", buf.getvalue())

    def test_good_config_still_loads_normally(self):
        good = self._write('[llm]\nbackend = "claude"\n')
        cfg = vb.load_config(good)
        self.assertEqual(cfg["llm"]["backend"], "claude")
        self.assertNotIn("_config_error", cfg)
        self.assertIn("_loaded_from", cfg)


class HistorySkipsCorruptLines(unittest.TestCase):
    def test_cmd_history_skips_bad_jsonl_line(self):
        d = tempfile.mkdtemp()
        cfg = vb.load_config("/nonexistent/x.toml")
        cfg["history"]["dir"] = d
        # one good record, one corrupt line, one good record
        vb.history_append("first", cfg, "stt")
        p = vb.history_path(cfg)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write("{ this is not json }\n")
        vb.history_append("second", cfg, "stt")

        args = type("NS", (), {"config": None, "limit": 10, "copy": None})()
        # point history reads at our dir via a config file
        cfgfile = Path(d) / "config.toml"
        cfgfile.write_text(f'[history]\ndir = "{d}"\n', encoding="utf-8")
        args.config = str(cfgfile)

        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = vb.cmd_history(args)
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)                        # did not crash on the bad line
        out = buf.getvalue()
        self.assertIn("first", out)
        self.assertIn("second", out)


if __name__ == "__main__":
    unittest.main()
