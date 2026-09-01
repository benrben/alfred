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
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ContentLengthParsing(unittest.TestCase):
    def test_rejects_malformed_and_negative_values(self):
        for value in ("not-a-number", "-1", None):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "invalid Content-Length"
            ):
                vb._content_length({"Content-Length": value})

    def test_accepts_missing_and_numeric_values(self):
        self.assertEqual(vb._content_length({}), 0)
        self.assertEqual(vb._content_length({"Content-Length": "12"}), 12)


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

        # Redirect the daemon-info file to a temp dir so the test doesn't write
        # (and leave stale) ~/.voicebridge/daemon.json in the real home dir.
        import tempfile
        cls._info = Path(tempfile.mkdtemp()) / "daemon.json"
        cls._saved_info = vb._daemon_info_path
        vb._daemon_info_path = lambda: cls._info

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
        vb._daemon_info_path = cls._saved_info
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
        self.assertTrue(obj["ok"])
        self.assertEqual(obj["app"], "alfred")       # identity, not just {"ok"}
        self.assertEqual(obj["schema_version"], vb.CONTRACT["schema_version"])
        self.assertIn("pid", obj)

    def test_contract_get(self):
        status, obj = self._get("/contract")
        self.assertEqual(status, 200)
        self.assertEqual(obj["schema_version"], 1)
        # GET /contract now emits the resolved contract and the actual serving
        # port, rather than the default port baked into the static contract.
        expected = {**vb.CONTRACT,
                    "daemon": {**vb.CONTRACT["daemon"], "port": self.port,
                               "url": f"http://127.0.0.1:{self.port}/"}}
        self.assertEqual(expected, {k: obj[k] for k in vb.CONTRACT})
        self.assertIn("resolved", obj)

    def test_post_argv_runs_command_and_returns_code_out_err(self):
        status, obj = self._post("/", {"argv": ["doctor"]})
        self.assertEqual(status, 200)
        for k in ("code", "out", "err"):
            self.assertIn(k, obj)
        self.assertIn(obj["code"], (0, 1))          # missing deps are a hard check
        self.assertIn("Alfred doctor", obj["out"])  # its stdout is captured
        self.assertEqual(obj["identity"]["app"], "alfred")

    def test_post_contract_command_round_trips_json(self):
        # `contract` prints the resolved contract; the daemon captures it.
        status, obj = self._post("/", {"argv": ["contract"]})
        self.assertEqual(status, 200)
        self.assertEqual(obj["code"], 0)
        emitted = json.loads(obj["out"])
        self.assertEqual({k: emitted[k] for k in vb.CONTRACT}, vb.CONTRACT)

    def test_post_bad_argv_does_not_crash_server(self):
        # An unknown subcommand triggers argparse SystemExit, caught -> nonzero.
        status, obj = self._post("/", {"argv": ["nonsense-command"]})
        self.assertEqual(status, 200)
        self.assertIn("code", obj)
        self.assertNotEqual(obj["code"], 0)
        # Server is still alive afterwards.
        status2, health = self._get("/")
        self.assertTrue(health["ok"])

    def test_bad_json_body_does_not_crash_server(self):
        # A non-JSON body -> req={} -> argparse SystemExit(2); server stays up.
        c = self._conn()
        c.request("POST", "/", body="}{ not json",
                  headers={"Content-Type": "text/plain"})
        r = c.getresponse()
        obj = json.loads(r.read().decode())
        c.close()
        self.assertEqual(r.status, 200)
        self.assertNotEqual(obj["code"], 0)
        self.assertTrue(self._get("/")[1]["ok"])

    def test_invalid_content_length_returns_json_error(self):
        c = self._conn()
        c.putrequest("POST", "/", skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", f"127.0.0.1:{self.port}")
        c.putheader("Content-Length", "not-a-number")
        c.endheaders()
        r = c.getresponse()
        obj = json.loads(r.read().decode())
        c.close()
        self.assertEqual(r.status, 400)
        self.assertEqual(obj["error"], "invalid Content-Length")
        self.assertTrue(self._get("/")[1]["ok"])

    def test_cross_origin_post_is_refused(self):
        # A browser cross-site POST carries an Origin header -> 403 (CSRF guard).
        c = self._conn()
        c.request("POST", "/", body=json.dumps({"argv": ["doctor"]}),
                  headers={"Content-Type": "text/plain",
                           "Origin": "https://evil.example"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 403)

    def test_non_loopback_host_is_refused(self):
        # A DNS-rebinding request arrives with the attacker's hostname in Host ->
        # 403 (both GET and POST). We hand-craft the Host header.
        for method, body in (("GET", None), ("POST", json.dumps({"argv": ["doctor"]}))):
            c = self._conn()
            c.putrequest(method, "/", skip_host=True, skip_accept_encoding=True)
            c.putheader("Host", "attacker.example.com")
            if body is not None:
                c.putheader("Content-Type", "text/plain")
                c.putheader("Content-Length", str(len(body)))
            c.endheaders()
            if body is not None:
                c.send(body.encode())
            r = c.getresponse()
            r.read()
            c.close()
            self.assertEqual(r.status, 403, f"{method} with foreign Host must 403")

    def test_concurrent_posts_run_in_parallel_not_serialized(self):
        # A slow command must NOT block a concurrent one (regression for the
        # daemon-wide POST lock that serialized every request behind each
        # capture's transcribe+LLM). We patch `doctor` to sleep, fire two
        # concurrently, and assert the wall time is ~one sleep, not two.
        import concurrent.futures as cf
        orig = vb.cmd_doctor
        vb.cmd_doctor = lambda args: (time.sleep(0.6) or 0)
        try:
            t0 = time.time()
            with cf.ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(self._post, "/", {"argv": ["doctor"]})
                f2 = ex.submit(self._post, "/", {"argv": ["doctor"]})
                f1.result()
                f2.result()
            elapsed = time.time() - t0
        finally:
            vb.cmd_doctor = orig
        # Serialized would be ~1.2s; parallel ~0.6s. Allow headroom.
        self.assertLess(elapsed, 1.0,
                        f"concurrent POSTs serialized ({elapsed:.2f}s ~ 2x sleep)")

    def test_concurrent_posts_do_not_cross_output(self):
        # Two overlapping POSTs must each get their OWN captured stdout — the
        # daemon serializes the redirect so they can't cross (regression for the
        # ThreadingHTTPServer global-stdout race).
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(self._post, "/", {"argv": ["contract"]})
                    for _ in range(6)]
            results = [f.result() for f in futs]
        for status, obj in results:
            self.assertEqual(status, 200)
            self.assertEqual(obj["code"], 0)
            emitted = json.loads(obj["out"])         # each parses on its own
            self.assertEqual(emitted["schema_version"],
                             vb.CONTRACT["schema_version"])
        # A follow-up request still captures correctly (stdout not left wedged).
        self.assertIn("Alfred doctor", self._post("/", {"argv": ["doctor"]})[1]["out"])


if __name__ == "__main__":
    unittest.main()
