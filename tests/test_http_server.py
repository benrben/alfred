"""Round-trip tests for the warm-daemon HTTP server (cmd_serve).

cmd_serve exposes the one-shot CLI over localhost HTTP (the contract the
front-ends speak to). These boot a real server in a background thread on an
ephemeral high port — with the heavy Whisper warm-up and claude pre-warm stubbed
so nothing loads a model — then drive the three documented endpoints:

  - GET  /          -> health  {"ok": true}
  - GET  /contract  -> the CONTRACT JSON (schema_version present)
  - POST /  {"argv": ["doctor"]}  -> {"code": int, "out": "<captured stdout>"}

This closes the known serve round-trip gap. The server is shut down in teardown.

Run: ./.venv/bin/python -m pytest tests/test_http_server.py -q
"""

import http.client
import json
import os
import socket
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ServeRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Stub the model so cmd_serve's warm-up is instant and offline: inject a
        # fake mlx_whisper whose transcribe() does nothing. (numpy is real.)
        cls._saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake

        # Neuter the claude pre-warm so the daemon thread doesn't shell out.
        cls._saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None

        cls.port = _free_port()
        cls.args = type("NS", (), {"port": cls.port, "config": NO_CFG})()
        cls.thread = threading.Thread(target=vb.cmd_serve, args=(cls.args,),
                                      daemon=True)
        cls.thread.start()
        cls._wait_until_up(cls.port)

    @classmethod
    def tearDownClass(cls):
        # Ask the server to stop by hitting it isn't enough (serve_forever); the
        # thread is a daemon and dies with the process. We restore the patches.
        sys.modules.pop("mlx_whisper", None)
        if cls._saved_mod is not None:
            sys.modules["mlx_whisper"] = cls._saved_mod
        vb._get_warm = cls._saved_warm
        vb._DAEMON_MODE = False        # cmd_serve flips this global; reset it.

    @staticmethod
    def _wait_until_up(port, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("serve daemon did not come up in time")

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def _get(self, path):
        c = self._conn()
        c.request("GET", path)
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, json.loads(body)

    def _post(self, path, obj):
        c = self._conn()
        data = json.dumps(obj)
        c.request("POST", path, body=data,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, json.loads(body)

    def test_health_get_root(self):
        status, obj = self._get("/")
        self.assertEqual(status, 200)
        self.assertEqual(obj, {"ok": True})

    def test_contract_get(self):
        status, obj = self._get("/contract")
        self.assertEqual(status, 200)
        self.assertEqual(obj["schema_version"], 1)
        self.assertEqual(obj, vb.CONTRACT)

    def test_post_argv_runs_command_and_returns_code_out(self):
        status, obj = self._post("/", {"argv": ["doctor"]})
        self.assertEqual(status, 200)
        self.assertIn("code", obj)
        self.assertIn("out", obj)
        self.assertEqual(obj["code"], 0)            # doctor returns 0
        self.assertIn("Alfred doctor", obj["out"])  # its stdout is captured

    def test_post_contract_command_round_trips_json(self):
        # `contract` prints the CONTRACT to stdout; the daemon captures it.
        status, obj = self._post("/", {"argv": ["contract"]})
        self.assertEqual(status, 200)
        self.assertEqual(obj["code"], 0)
        self.assertEqual(json.loads(obj["out"]), vb.CONTRACT)

    def test_post_bad_argv_does_not_crash_server(self):
        # An unknown subcommand triggers argparse SystemExit, caught -> nonzero.
        status, obj = self._post("/", {"argv": ["nonsense-command"]})
        self.assertEqual(status, 200)
        self.assertIn("code", obj)
        self.assertNotEqual(obj["code"], 0)
        # Server is still alive afterwards.
        status2, health = self._get("/")
        self.assertEqual(health, {"ok": True})


if __name__ == "__main__":
    unittest.main()
