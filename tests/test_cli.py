"""Interface tests for the CLI surface (build_parser / main / _bool_flag).

The parser is the contract between the front-ends and the commands. These
assert, by parsing argv and inspecting the resulting Namespace:

  - each subcommand binds the right .func (so dispatch routes correctly);
  - the --x/--no-x tristate (_bool_flag) yields True / False / None, and a flag
    left UNSET stays None — so it never overrides config;
  - common overrides (--backend/--model/--language/--mode) default to None;
  - main() dispatches to the bound func and returns its int (func stubbed).

No model or LLM is touched — we only parse argv and stub the command function.

Run: ./.venv/bin/python -m pytest tests/test_cli.py -q
"""

import argparse
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402


class FuncBinding(unittest.TestCase):
    """Each subcommand parses to a Namespace whose .func is the right handler."""

    CASES = [
        (["process", "a.wav"], vb.cmd_process),
        (["stream-start", "a.wav"], vb.cmd_stream_start),
        (["stream-finish", "a.wav"], vb.cmd_stream_finish),
        (["text", "hello"], vb.cmd_text),
        (["history"], vb.cmd_history),
        (["modes"], vb.cmd_modes),
        (["settings"], vb.cmd_settings),
        (["doctor"], vb.cmd_doctor),
        (["contract"], vb.cmd_contract),
        (["serve"], vb.cmd_serve),
        (["set-intent", "mykey"], vb.cmd_set_intent),
        (["set-model", "claude"], vb.cmd_set_model),
        (["set-processing"], vb.cmd_set_processing),
    ]

    def test_each_subcommand_binds_its_func(self):
        parser = vb.build_parser()
        for argv, func in self.CASES:
            ns = parser.parse_args(argv)
            self.assertIs(ns.func, func, f"{argv} should bind {func.__name__}")

    def test_no_subcommand_is_an_error(self):
        # subparsers are required=True -> parse with no command exits.
        parser = vb.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])


class BoolFlagTristate(unittest.TestCase):
    """_bool_flag gives a true three-state value: True / False / None."""

    def test_translate_tristate(self):
        parser = vb.build_parser()
        self.assertIsNone(parser.parse_args(["process", "a.wav"]).translate)
        self.assertIs(parser.parse_args(["process", "a.wav", "--translate"]).translate,
                      True)
        self.assertIs(parser.parse_args(["process", "a.wav", "--no-translate"]).translate,
                      False)

    def test_all_stage_flags_default_none(self):
        # Unset flags MUST stay None so _apply_overrides leaves config untouched.
        ns = vb.build_parser().parse_args(["process", "a.wav"])
        for name in ("translate", "rewrite", "optimize", "paste"):
            self.assertIsNone(getattr(ns, name), f"{name} must default None")

    def test_mutually_exclusive(self):
        parser = vb.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["process", "a.wav", "--rewrite", "--no-rewrite"])

    def test_standalone_bool_flag_helper(self):
        # Unit-test _bool_flag directly: it adds a --name/--no-name pair, dest=name.
        p = argparse.ArgumentParser()
        vb._bool_flag(p, "thing", "on help", "off help")
        self.assertIsNone(p.parse_args([]).thing)
        self.assertIs(p.parse_args(["--thing"]).thing, True)
        self.assertIs(p.parse_args(["--no-thing"]).thing, False)


class CommonOverridesDefaultNone(unittest.TestCase):
    def test_overrides_unset_are_none(self):
        ns = vb.build_parser().parse_args(["process", "a.wav"])
        for name in ("backend", "model", "language", "mode", "config"):
            self.assertIsNone(getattr(ns, name), f"{name} must default None")

    def test_overrides_parsed_when_given(self):
        ns = vb.build_parser().parse_args(
            ["process", "a.wav", "--backend", "local", "--model", "opus",
             "--language", "he", "--mode", "email"])
        self.assertEqual(ns.backend, "local")
        self.assertEqual(ns.model, "opus")
        self.assertEqual(ns.language, "he")
        self.assertEqual(ns.mode, "email")

    def test_backend_choices_enforced(self):
        with self.assertRaises(SystemExit):
            vb.build_parser().parse_args(["process", "a.wav", "--backend", "bogus"])


class MainDispatch(unittest.TestCase):
    """main() parses argv, dispatches to .func, and returns its int."""

    def test_main_returns_func_int(self):
        orig = vb.cmd_doctor
        vb.cmd_doctor = lambda args: 0
        try:
            self.assertEqual(vb.main(["doctor"]), 0)
        finally:
            vb.cmd_doctor = orig

    def test_main_passes_namespace_to_func(self):
        seen = {}
        orig = vb.cmd_settings
        def record(args):
            seen["config"] = args.config
            return 7
        vb.cmd_settings = record
        try:
            rc = vb.main(["settings", "--config", "/tmp/x.toml"])
        finally:
            vb.cmd_settings = orig
        self.assertEqual(rc, 7)
        self.assertEqual(seen["config"], "/tmp/x.toml")

    def test_main_maps_runtime_error_to_1(self):
        orig = vb.cmd_doctor
        def boom(args):
            raise RuntimeError("kaboom")
        vb.cmd_doctor = boom
        import contextlib, io
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                rc = vb.main(["doctor"])
        finally:
            vb.cmd_doctor = orig
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
