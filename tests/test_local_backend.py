"""Interface tests for the warm local MLX-LM backend (s2).

Assert the *observable contract* of the `local` backend at eng-llm's run_llm
seam, with the two model-touching seams (_local_load / _local_generate) stubbed
so the suite needs neither mlx-lm nor a model download:

  - run_llm("local", ...) routes to the on-device model and strips wrapping.
  - candidate_backends("local") -> ["local"] with NO network fallback (strict).
  - the model is warm: loaded once, reused across calls; reloaded on model
    change or idle expiry.
  - a generation failure raises (so process_text falls back to raw transcript)
    and drops the wedged model.

Run: ./.venv/bin/python -m unittest discover -s tests
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402


class LocalBackend(unittest.TestCase):
    def setUp(self):
        # Reset the warm-model globals so tests are independent.
        vb._LOCAL = None
        vb._LOCAL_SIG = None
        vb._LOCAL_LAST = 0.0
        # Stub the two model seams: count loads, script generations.
        self.loads = []
        self.gens = []
        self._gen_return = "RESULT"
        self._gen_exc = None

        def fake_load(model_id):
            self.loads.append(model_id)
            return (f"model::{model_id}", "tokenizer")

        def fake_generate(model, tokenizer, prompt, max_tokens):
            self.gens.append({"prompt": prompt, "max_tokens": max_tokens})
            if self._gen_exc:
                raise self._gen_exc
            return self._gen_return

        self._orig_load, self._orig_gen = vb._local_load, vb._local_generate
        vb._local_load = fake_load
        vb._local_generate = fake_generate

    def tearDown(self):
        vb._local_load, vb._local_generate = self._orig_load, self._orig_gen
        vb._LOCAL = None
        vb._LOCAL_SIG = None
        vb._LOCAL_LAST = 0.0

    # Hermetic config: a nonexistent path yields the shipped DEFAULTS only, so
    # tests don't pick up the developer's real ~/.config/voicebridge/config.toml.
    _NO_CFG = "/nonexistent/alfred-test-config.toml"

    def _cfg(self, **llm):
        cfg = vb.load_config(self._NO_CFG)
        cfg["llm"].update(llm)
        return cfg

    def test_default_backend_is_local(self):
        self.assertEqual(vb.load_config(self._NO_CFG)["llm"]["backend"], "local")

    def test_candidate_backends_local_only_no_network_fallback(self):
        # Must return ["local"] without touching find_tool / detect_backends,
        # and must NOT silently fall back to a network CLI.
        self.assertEqual(vb.candidate_backends(self._cfg(backend="local")),
                         ["local"])

    def test_run_llm_dispatches_to_local_and_strips(self):
        self._gen_return = "```\nHELLO WORLD\n```"
        out = vb.run_llm("local", "hi", self._cfg())
        self.assertEqual(out, "HELLO WORLD")          # fences stripped
        self.assertEqual(len(self.loads), 1)
        self.assertEqual(len(self.gens), 1)
        self.assertEqual(self.gens[0]["max_tokens"], 1024)

    def test_model_is_warm_loaded_once(self):
        cfg = self._cfg()
        vb.run_local_llm("a", cfg)
        vb.run_local_llm("b", cfg)
        self.assertEqual(self.loads, [cfg["llm"]["local_model"]],
                         "model must load once and stay warm")
        self.assertEqual(len(self.gens), 2)

    def test_model_change_triggers_reload(self):
        vb.run_local_llm("a", self._cfg(local_model="m1"))
        vb.run_local_llm("b", self._cfg(local_model="m2"))
        self.assertEqual(self.loads, ["m1", "m2"])

    def test_idle_expiry_reloads(self):
        cfg = self._cfg(local_idle_secs=1)
        vb.run_local_llm("a", cfg)
        self.assertEqual(len(self.loads), 1)
        vb._LOCAL_LAST = time.monotonic() - 10_000     # pretend long idle
        vb.run_local_llm("b", cfg)
        self.assertEqual(len(self.loads), 2, "stale model must be reloaded")

    def test_generation_failure_raises_and_drops_model(self):
        cfg = self._cfg()
        self._gen_exc = ValueError("boom")
        with self.assertRaises(RuntimeError):
            vb.run_local_llm("a", cfg)
        self.assertIsNone(vb._LOCAL, "wedged model must be dropped")
        # Recovers on the next call (fresh load).
        self._gen_exc = None
        self.assertEqual(vb.run_local_llm("b", cfg), "RESULT")
        self.assertEqual(len(self.loads), 2)

    def test_missing_mlx_lm_gives_clear_error(self):
        # Restore the real loader and ensure a missing dep is a clear RuntimeError
        # (not an ImportError leaking through). Only meaningful when mlx-lm is
        # absent; if it's installed, the load path is exercised elsewhere.
        vb._local_load = self._orig_load
        import importlib.util
        if importlib.util.find_spec("mlx_lm") is not None:
            self.skipTest("mlx-lm is installed; missing-dep path not exercised")
        with self.assertRaises(RuntimeError):
            vb.run_local_llm("a", self._cfg())


if __name__ == "__main__":
    unittest.main()
