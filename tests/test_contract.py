"""Contract self-consistency tests (e1).

The CONTRACT dict is the single source of IPC truth the two front-ends read.
These assert two things:

  1. CONTRACT has every key the authoritative spec requires (so the front-ends
     can rely on its shape).
  2. The engine's REAL writers actually produce what CONTRACT claims — we drive
     _Progress, history_append and print_status and validate their output
     against the schemas/paths/grammar in CONTRACT. If a writer drifts from the
     contract, one of these fails.

The model is never imported: voicebridge lazy-imports mlx_* inside the STT/LLM
seams, and these tests touch none of them.

Run: ./.venv/bin/python -m pytest tests/test_contract.py -q
"""

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


class ContractShape(unittest.TestCase):
    """CONTRACT has every key the spec mandates, exposed via cmd_contract too."""

    def test_top_level_keys(self):
        c = vb.CONTRACT
        self.assertEqual(c["schema_version"], 1)
        for key in ("daemon", "status_line", "files", "config_search"):
            self.assertIn(key, c)

    def test_daemon_shape(self):
        d = vb.CONTRACT["daemon"]
        self.assertEqual(d["host"], "127.0.0.1")
        self.assertEqual(d["port"], 8763)
        self.assertEqual(d["url"], "http://127.0.0.1:{port}/")
        self.assertEqual(d["request"],
                         {"method": "POST", "path": "/",
                          "body": {"argv": ["<str>"]}})
        self.assertEqual(d["response"], {"code": "int", "out": "str"})
        self.assertEqual(d["health"], {"method": "GET", "path": "/"})
        self.assertEqual(d["contract"], {"method": "GET", "path": "/contract"})

    def test_status_line_shape(self):
        s = vb.CONTRACT["status_line"]
        self.assertEqual(s["sentinel"], "VB_STATUS")
        self.assertEqual(s["sep"], "\t")
        self.assertEqual(set(s["kinds"]),
                         {"copied", "saved", "empty", "streaming", "error"})
        self.assertEqual(s["kinds"]["saved"], ["path"])
        self.assertEqual(s["kinds"]["error"], ["subtype"])
        self.assertEqual(s["error_subtypes"],
                         ["audio_not_found", "stt_failed", "llm_failed",
                          "runtime"])
        self.assertEqual(s["llm_failed_suffix"], "llm_failed")

    def test_files_shape(self):
        f = vb.CONTRACT["files"]
        self.assertEqual(f["progress"]["path"], "~/.voicebridge/progress.json")
        self.assertEqual(set(f["progress"]["schema"]),
                         {"phase", "label", "ts", "start", "steps"})
        self.assertEqual(f["progress"]["phases"],
                         ["starting", "transcribing", "processing",
                          "delivering", "done", "error", "empty"])
        self.assertEqual(f["stream"]["path"], "~/.voicebridge/stream.json")
        self.assertEqual(set(f["stream"]["schema"]),
                         {"transcript", "recording", "done", "ts"})
        self.assertEqual(f["history"]["path"],
                         "~/.voicebridge/history/history.jsonl")
        self.assertEqual(f["history"]["format"], "jsonl")
        self.assertEqual(f["history"]["dir_config"], "[history].dir")
        self.assertEqual(set(f["history"]["schema"]),
                         {"ts", "source", "chars", "text"})

    def test_config_search(self):
        self.assertEqual(vb.CONTRACT["config_search"],
                         ["~/.config/voicebridge/config.toml",
                          "<engine_dir>/config.toml"])

    def test_cmd_contract_prints_full_contract(self):
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            rc = vb.cmd_contract(object())
        finally:
            sys.stdout = old
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue()), vb.CONTRACT)


class ProgressWriterMatchesContract(unittest.TestCase):
    """The real _Progress writer's JSON validates against the progress schema."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "progress.json"
        self._orig = vb._progress_path
        vb._progress_path = lambda: self.tmp

    def tearDown(self):
        vb._progress_path = self._orig

    def _types_ok(self, obj, schema):
        kind = {"str": str, "int": int, "int_epoch_ms": int}
        for field, typ in schema.items():
            self.assertIn(field, obj)
            if field == "steps":
                self.assertIsInstance(obj["steps"], list)
                for step in obj["steps"]:
                    self.assertIsInstance(step["label"], str)
                    self.assertIsInstance(step["ms"], int)
            else:
                self.assertIsInstance(obj[field], kind[typ])

    def test_progress_json_matches_schema_and_phases(self):
        cspec = vb.CONTRACT["files"]["progress"]
        prog = vb._Progress()            # writes phase "starting"
        prog.step("transcribing", "Transcribing audio")
        prog.step("processing", "Cleaning up via local")
        prog.done()                      # phase "done"

        obj = json.loads(self.tmp.read_text(encoding="utf-8"))
        self._types_ok(obj, cspec["schema"])
        self.assertIn(obj["phase"], cspec["phases"])
        # The first step() opens a phase (nothing to close yet); the second
        # closes "transcribing"; done() closes "processing" -> two steps.
        self.assertEqual(len(obj["steps"]), 2)
        self.assertEqual([s["label"] for s in obj["steps"]],
                         ["Transcribing audio", "Cleaning up via local"])
        self.assertEqual(obj["phase"], "done")


class HistoryWriterMatchesContract(unittest.TestCase):
    """history_append writes a jsonl record matching the history schema, into
    the path CONTRACT describes (honouring [history].dir)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = vb.load_config(NO_CFG)
        self.cfg["history"]["dir"] = self.dir

    def test_record_matches_schema(self):
        # The writer puts the file exactly where the contract path resolves to.
        self.assertEqual(vb.history_path(self.cfg),
                         Path(self.dir) / "history.jsonl")
        vb.history_append("hello world", self.cfg, "stt")

        path = Path(self.dir) / "history.jsonl"
        self.assertTrue(path.exists())
        lines = [l for l in path.read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        schema = vb.CONTRACT["files"]["history"]["schema"]
        self.assertEqual(set(rec), set(schema))
        self.assertIsInstance(rec["ts"], str)
        self.assertIsInstance(rec["source"], str)
        self.assertIsInstance(rec["chars"], int)
        self.assertIsInstance(rec["text"], str)
        # ts is ISO with seconds precision (str_iso_seconds): T present, no micros.
        self.assertIn("T", rec["ts"])
        self.assertNotIn(".", rec["ts"])
        self.assertEqual(rec["source"], "stt")
        self.assertEqual(rec["chars"], len("hello world"))


class StatusLineMatchesContract(unittest.TestCase):
    """print_status emits the sentinel + separator CONTRACT declares."""

    def test_sentinel_and_sep(self):
        s = vb.CONTRACT["status_line"]
        buf = io.StringIO()
        old = sys.stdout
        sys.stdout = buf
        try:
            vb.print_status("saved", "/tmp/x.md")
        finally:
            sys.stdout = old
        line = buf.getvalue().rstrip("\n")
        self.assertTrue(line.startswith(s["sentinel"]))
        parts = line.split(s["sep"])
        self.assertEqual(parts, ["VB_STATUS", "saved", "/tmp/x.md"])
        # A 'saved' line carries one extra field (the path), per kinds["saved"].
        self.assertEqual(len(parts) - 2, len(s["kinds"]["saved"]))


if __name__ == "__main__":
    unittest.main()
