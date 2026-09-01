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
import importlib.util
import io
import json
import os
import platform
import shutil
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
        # Every entry carries the picker fields the front-end relies on,
        # including the ready-made `flags` argv so front-ends don't re-derive it.
        for m in catalog:
            for field in ("key", "label", "description", "prompt", "default",
                          "flags"):
                self.assertIn(field, m)
            self.assertIsInstance(m["flags"], list)
            self.assertIn("--mode", m["flags"])

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
        # stt block exposes the vocab/initial_prompt knob for the front-end UI.
        self.assertIn("stt", s)
        for key in ("language", "initial_prompt"):
            self.assertIn(key, s["stt"])

    def test_custom_models_appended_and_deduped(self):
        # config.toml can extend the built-in preset lists; a value already in
        # the built-in list must not be duplicated, a new one must be appended.
        d = tempfile.mkdtemp()
        cfg_path = Path(d) / "config.toml"
        cfg_path.write_text(
            '[llm]\nclaude_models = ["opus", "custom-model"]\n'
            'codex_models = ["gpt-5-custom"]\n',
            encoding="utf-8",
        )
        rc, out = _run(vb.cmd_settings, _ns(config=str(cfg_path)))
        self.assertEqual(rc, 0)
        s = json.loads(out)
        self.assertIn("custom-model", s["claude_models"])
        self.assertEqual(s["claude_models"].count("opus"), 1)
        self.assertIn("gpt-5-custom", s["codex_models"])


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

    def test_blank_lines_in_history_file_are_skipped(self):
        # A stray blank line (e.g. an interrupted write) must not become a
        # phantom entry or otherwise disturb the listing.
        path = Path(self.dir) / "history.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n")
        rc, out = _run(vb.cmd_history,
                       _ns(config=self._config_pointing_here(), limit=10, copy=None))
        self.assertEqual(rc, 0)
        lines = [l for l in out.splitlines() if l.strip()]
        self.assertEqual(len(lines), 3)

    def test_long_preview_is_truncated(self):
        long_text = "x" * 100
        vb.history_append(long_text, self.cfg, "text")
        rc, out = _run(vb.cmd_history,
                       _ns(config=self._config_pointing_here(), limit=10, copy=None))
        self.assertEqual(rc, 0)
        lines = [l for l in out.splitlines() if l.strip()]
        # Newest first -> the long item just appended is first.
        self.assertIn("x" * 67 + "...", lines[0])
        self.assertNotIn("x" * 68, lines[0])


class HistoryAppendBehavior(unittest.TestCase):
    """Direct tests for history_append's guards and its max_items trim."""

    def _cfg(self, **history_kw):
        cfg = vb.load_config(NO_CFG)
        cfg["history"]["dir"] = tempfile.mkdtemp()
        cfg["history"].update(history_kw)
        return cfg

    def test_disabled_history_is_a_no_op(self):
        cfg = self._cfg(enabled=False)
        vb.history_append("hello", cfg, "text")
        self.assertFalse(vb.history_path(cfg).exists())

    def test_blank_text_is_not_appended(self):
        cfg = self._cfg()
        vb.history_append("   \n  ", cfg, "text")
        self.assertFalse(vb.history_path(cfg).exists())

    def test_max_items_trims_the_oldest(self):
        cfg = self._cfg(max_items=2)
        vb.history_append("first", cfg, "text")
        vb.history_append("second", cfg, "text")
        vb.history_append("third", cfg, "text")
        lines = [l for l in vb.history_path(cfg).read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(l)["text"] for l in lines], ["second", "third"])

    def test_max_items_zero_keeps_everything(self):
        cfg = self._cfg(max_items=0)
        for i in range(5):
            vb.history_append(f"item{i}", cfg, "text")
        lines = [l for l in vb.history_path(cfg).read_text(encoding="utf-8").splitlines()
                 if l.strip()]
        self.assertEqual(len(lines), 5)


class PrintResultLine(unittest.TestCase):
    """print_result emits the VB_RESULT sentinel + JSON-encoded text — mirrors
    StatusLineMatchesContract (test_contract.py) for the sibling print_status."""

    def test_emits_result_sentinel_and_json_encoded_text(self):
        s = vb.CONTRACT["status_line"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            vb.print_result("hello\nworld")
        line = buf.getvalue().rstrip("\n")
        self.assertTrue(line.startswith(s["result_sentinel"]))
        payload = line[len(s["result_sentinel"] + s["sep"]):]
        self.assertEqual(json.loads(payload), "hello\nworld")


class StatusPartsAssembly(unittest.TestCase):
    """Direct tests for _status_parts: the VB_STATUS field assembly (kind,
    optional path, the paste_failed suffix, caller extras)."""

    def test_kind_only(self):
        self.assertEqual(vb._status_parts("copied", None, None), ["copied"])

    def test_kind_and_path(self):
        self.assertEqual(vb._status_parts("saved", "/tmp/x.md", None),
                         ["saved", "/tmp/x.md"])

    def test_paste_failed_suffix_appended_when_paste_ok_is_false(self):
        suffix = vb.CONTRACT["status_line"]["paste_failed_suffix"]
        self.assertEqual(
            vb._status_parts("copied", None, False, "llm_failed"),
            ["copied", suffix, "llm_failed"])

    def test_paste_ok_true_or_none_omits_the_suffix(self):
        self.assertEqual(vb._status_parts("copied", None, True), ["copied"])
        self.assertEqual(vb._status_parts("copied", None, None), ["copied"])


class ProgressPathResolution(unittest.TestCase):
    def test_matches_contract_paths(self):
        self.assertEqual(vb._progress_path(), vb.contract_paths()["progress"])


class DoctorStatus(unittest.TestCase):
    def test_doctor_returns_0_and_mentions_deps(self):
        rc, out = _run(vb.cmd_doctor, _ns())
        self.assertEqual(rc, 0)
        self.assertIn("Alfred doctor", out)
        # Mentions the key dependencies / backends it checks.
        for needle in ("mlx_whisper", "Python", "LLM backend", "STT model"):
            self.assertIn(needle, out)


class DoctorSections(unittest.TestCase):
    """cmd_doctor's per-section checks (python deps, system tools, LLM
    backends, the local on-device model, save_dir, the macOS TCC note, and the
    warm-daemon probe), forced down their less-common branches by stubbing the
    real signal (find_spec / shutil.which / detect_backends / _probe_daemon /
    platform.system) rather than depending on this machine's ambient state."""

    def test_missing_python_module_reported(self):
        orig = importlib.util.find_spec

        def fake(name):
            if name in ("mlx_whisper", "mlx_lm"):
                return None
            return orig(name)

        importlib.util.find_spec = fake
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            importlib.util.find_spec = orig
        self.assertEqual(rc, 0)
        self.assertIn("XX python module: mlx_whisper", out)
        self.assertIn("-- python module: mlx_lm", out)

    def test_missing_system_tool_reported(self):
        orig = shutil.which

        def fake(tool):
            return None if tool == "sox" else orig(tool)

        shutil.which = fake
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            shutil.which = orig
        self.assertEqual(rc, 0)
        self.assertIn("XX command: sox", out)

    def test_llm_backends_both_missing_shows_disabled_note(self):
        orig = vb.detect_backends
        vb.detect_backends = lambda: {"claude": None, "codex": None}
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb.detect_backends = orig
        self.assertEqual(rc, 0)
        self.assertIn("-- LLM backend: claude not found", out)
        self.assertIn("-- LLM backend: codex not found", out)
        self.assertIn("LLM stages disabled", out)

    def test_llm_backend_present_with_key_env_warns(self):
        orig = vb.detect_backends
        vb.detect_backends = lambda: {"claude": "/usr/bin/claude", "codex": None}
        old_val = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb.detect_backends = orig
            if old_val is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old_val
        self.assertEqual(rc, 0)
        self.assertIn("OK LLM backend: claude", out)
        self.assertIn("WARNING: ANTHROPIC_API_KEY is set", out)

    def test_llm_backend_present_without_key_env_no_warning(self):
        orig = vb.detect_backends
        vb.detect_backends = lambda: {"claude": "/usr/bin/claude", "codex": "/usr/bin/codex"}
        old_val = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb.detect_backends = orig
            if old_val is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_val
        self.assertEqual(rc, 0)
        self.assertIn("OK LLM backend: claude", out)
        self.assertNotIn("WARNING: ANTHROPIC_API_KEY is set", out)

    def _cfg_with_local_model(self, local_model):
        d = tempfile.mkdtemp()
        cfg_path = Path(d) / "config.toml"
        cfg_path.write_text(f'[llm]\nlocal_model = "{local_model}"\n', encoding="utf-8")
        return str(cfg_path)

    def test_local_model_absent_skips_cache_line(self):
        d = tempfile.mkdtemp()
        cfg_path = Path(d) / "config.toml"
        cfg_path.write_text('[llm]\nlocal_model = ""\n', encoding="utf-8")
        rc, out = _run(vb.cmd_doctor, _ns(config=str(cfg_path)))
        self.assertEqual(rc, 0)
        self.assertNotIn("local model:", out)

    def test_local_model_not_cached(self):
        cfg = self._cfg_with_local_model("mlx-community/definitely-not-cached-alfred-test")
        rc, out = _run(vb.cmd_doctor, _ns(config=cfg))
        self.assertEqual(rc, 0)
        self.assertIn("local model: mlx-community/definitely-not-cached-alfred-test", out)
        self.assertIn("downloads on first", out)

    def test_local_model_cached(self):
        model = "mlx-community/fake-cached-alfred-test-model"
        cache_dir = (
            Path.home() / ".cache" / "huggingface" / "hub"
            / ("models--" + model.replace("/", "--"))
        )
        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            rc, out = _run(vb.cmd_doctor, _ns(config=self._cfg_with_local_model(model)))
        finally:
            with contextlib.suppress(OSError):
                cache_dir.rmdir()
        self.assertEqual(rc, 0)
        self.assertIn(f"local model: {model}", out)
        self.assertIn("(cached)", out)

    def test_save_dir_not_writable_reported(self):
        d = tempfile.mkdtemp()
        blocker = Path(d) / "blocker"
        blocker.write_text("x", encoding="utf-8")   # a FILE, not a dir
        bad_save_dir = blocker / "sub"                # mkdir(parents=True) must fail
        cfg_path = Path(d) / "config.toml"
        cfg_path.write_text(f'[output]\nsave_dir = "{bad_save_dir}"\n', encoding="utf-8")
        rc, out = _run(vb.cmd_doctor, _ns(config=str(cfg_path)))
        self.assertEqual(rc, 0)
        self.assertIn("save_dir not writable", out)

    def test_macos_permissions_note_skipped_on_non_darwin(self):
        orig = platform.system
        platform.system = lambda: "Linux"
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            platform.system = orig
        self.assertEqual(rc, 0)
        self.assertNotIn("Accessibility", out)

    def test_daemon_running_reported_ok(self):
        def fake_probe(port: int, timeout: float = 1.0) -> dict | None:
            return {"app": "alfred", "pid": 4242, "schema_version": 3}

        orig = vb._probe_daemon
        vb._probe_daemon = fake_probe
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb._probe_daemon = orig
        self.assertEqual(rc, 0)
        self.assertIn("warm daemon: running (pid 4242, schema v3)", out)

    def test_daemon_port_held_by_other_reported_bad(self):
        def fake_probe(port: int, timeout: float = 1.0) -> dict | None:
            return {"app": "something-else"}

        orig = vb._probe_daemon
        vb._probe_daemon = fake_probe
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb._probe_daemon = orig
        self.assertEqual(rc, 0)
        self.assertIn("held by a NON-Alfred server", out)

    def test_daemon_not_running_reported_warn(self):
        def fake_probe(port: int, timeout: float = 1.0) -> dict | None:
            return None

        orig = vb._probe_daemon
        vb._probe_daemon = fake_probe
        try:
            rc, out = _run(vb.cmd_doctor, _ns())
        finally:
            vb._probe_daemon = orig
        self.assertEqual(rc, 0)
        self.assertIn("warm daemon: not running", out)


if __name__ == "__main__":
    unittest.main()
