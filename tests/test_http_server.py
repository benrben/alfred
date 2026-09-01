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
import io
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


class ServeRoundTrip(unittest.TestCase):
    _info: Path

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
        # GET /contract now emits the resolved contract (static keys + resolved).
        self.assertEqual({k: obj[k] for k in vb.CONTRACT}, vb.CONTRACT)
        self.assertIn("resolved", obj)

    def test_post_argv_runs_command_and_returns_code_out_err(self):
        status, obj = self._post("/", {"argv": ["doctor"]})
        self.assertEqual(status, 200)
        for k in ("code", "out", "err"):
            self.assertIn(k, obj)
        self.assertEqual(obj["code"], 0)            # doctor returns 0
        self.assertIn("Alfred doctor", obj["out"])  # its stdout is captured

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

    def _post_to_isolated_daemon(self, argv):
        """Start a fresh daemon on its own port and POST once. build_parser()
        binds `func=vb.cmd_doctor` (etc.) by VALUE when the daemon starts, so a
        test that monkeypatches a command must build its OWN daemon AFTER
        patching — reusing the class-level daemon (built once in setUpClass,
        before any test's monkeypatch) would silently dispatch the original,
        unpatched function."""
        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        threading.Thread(target=vb.cmd_serve, args=(args,), daemon=True).start()
        self._wait_until_up(port)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/", body=json.dumps({"argv": argv}),
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        obj = json.loads(r.read().decode())
        c.close()
        return r.status, obj

    def test_post_command_raising_runtimeerror_returns_error_status(self):
        orig = vb.cmd_doctor
        vb.cmd_doctor = lambda args: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            status, obj = self._post_to_isolated_daemon(["doctor"])
        finally:
            vb.cmd_doctor = orig
        self.assertEqual(status, 200)          # the daemon itself never crashes
        self.assertEqual(obj["code"], 1)
        self.assertIn("error: boom", obj["err"])
        self.assertTrue(self._get("/")[1]["ok"])  # the shared daemon is unaffected

    def test_post_command_raising_generic_exception_returns_error_status(self):
        orig = vb.cmd_doctor
        vb.cmd_doctor = lambda args: (_ for _ in ()).throw(ValueError("kaboom"))
        try:
            status, obj = self._post_to_isolated_daemon(["doctor"])
        finally:
            vb.cmd_doctor = orig
        self.assertEqual(status, 200)
        self.assertEqual(obj["code"], 1)
        self.assertIn("request failed", obj["err"])
        self.assertTrue(self._get("/")[1]["ok"])  # the shared daemon is unaffected

    def test_concurrent_posts_run_in_parallel_not_serialized(self):
        # A slow command must NOT block a concurrent one (regression for the
        # daemon-wide POST lock that serialized every request behind each
        # capture's transcribe+LLM). We patch `doctor` to sleep, fire two
        # concurrently, and assert the wall time is ~one sleep, not two.
        import concurrent.futures as cf

        def _slow_doctor(args):
            time.sleep(0.6)
            return 0

        orig = vb.cmd_doctor
        vb.cmd_doctor = _slow_doctor
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

    # ---- _ThreadStream.flush / .isatty / .__getattr__ -----------------------
    # Small pass-through methods; unit-tested directly, no daemon needed.

    def test_thread_stream_flush_flushes_the_real_stream(self):
        class FakeReal:
            def __init__(self):
                self.flushed = False

            def write(self, s):
                pass

            def flush(self):
                self.flushed = True

        real = FakeReal()
        ts = vb._ThreadStream(real)
        ts.flush()
        self.assertTrue(real.flushed)

    def test_thread_stream_flush_flushes_the_redirected_buffer(self):
        ts = vb._ThreadStream(sys.__stdout__)
        buf = io.StringIO()
        ts.redirect(buf)
        try:
            ts.flush()  # StringIO.flush() is a harmless no-op; must not raise
        finally:
            ts.restore()

    def test_thread_stream_flush_swallows_underlying_exception(self):
        class BoomReal:
            def write(self, s):
                pass

            def flush(self):
                raise OSError("broken pipe")

        ts = vb._ThreadStream(BoomReal())
        ts.flush()  # must not raise -- the exception is swallowed

    def test_thread_stream_isatty_is_always_false(self):
        ts = vb._ThreadStream(sys.__stdout__)
        self.assertFalse(ts.isatty())

    def test_thread_stream_getattr_delegates_to_the_real_stream(self):
        class FakeReal:
            encoding = "utf-8"

            def write(self, s):
                pass

        ts = vb._ThreadStream(FakeReal())
        self.assertEqual(ts.encoding, "utf-8")

    # ---- _write_daemon_info --------------------------------------------------

    def test_write_daemon_info_writes_identity_and_port(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp()) / "daemon.json"
        orig = vb._daemon_info_path
        vb._daemon_info_path = lambda: tmp
        try:
            vb._write_daemon_info(54321)
        finally:
            vb._daemon_info_path = orig
        data = json.loads(tmp.read_text())
        self.assertEqual(data["port"], 54321)
        self.assertEqual(data["app"], "alfred")
        self.assertTrue(data["ok"])

    def test_write_daemon_info_swallows_a_write_failure(self):
        # A parent directory that doesn't exist makes the atomic write's
        # os.open(..., O_CREAT) raise FileNotFoundError; the function must
        # swallow it -- a front-end losing discovery isn't worth crashing serve.
        orig = vb._daemon_info_path
        vb._daemon_info_path = lambda: Path("/nonexistent/alfred-test-dir/daemon.json")
        try:
            vb._write_daemon_info(54321)  # must not raise
        finally:
            vb._daemon_info_path = orig

    # ---- _probe_daemon --------------------------------------------------------

    def test_probe_daemon_returns_identity_for_a_real_alfred_daemon(self):
        who = vb._probe_daemon(self.port)
        self.assertIsNotNone(who)
        self.assertEqual(who["app"], "alfred")

    def test_probe_daemon_returns_none_for_a_closed_port(self):
        port = _free_port()  # bound then closed: nothing listens -> refused
        self.assertIsNone(vb._probe_daemon(port, timeout=0.5))

    def test_probe_daemon_returns_none_for_a_non_json_http_response(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class TextHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"not json"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), TextHandler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            self.assertIsNone(vb._probe_daemon(port, timeout=2.0))
        finally:
            srv.shutdown()
            srv.server_close()

    # ---- _loopback_host ---------------------------------------------------

    def test_loopback_host_accepts_missing_or_empty_host(self):
        self.assertTrue(vb._loopback_host({}))
        self.assertTrue(vb._loopback_host({"Host": ""}))
        self.assertTrue(vb._loopback_host({"Host": "   "}))

    def test_loopback_host_accepts_loopback_ipv4_and_localhost_with_and_without_port(self):
        for host in ("127.0.0.1", "127.0.0.1:8765", "localhost", "localhost:9999"):
            self.assertTrue(vb._loopback_host({"Host": host}), host)

    def test_loopback_host_accepts_bracketed_ipv6_loopback_with_port(self):
        # Only the bracket+port form round-trips through rsplit(":", 1)
        # correctly (the split lands on the port colon, not one inside "::1").
        self.assertTrue(vb._loopback_host({"Host": "[::1]:9999"}))

    def test_loopback_host_rejects_a_foreign_hostname(self):
        for host in ("evil.example.com", "evil.example.com:80", "10.0.0.5"):
            self.assertFalse(vb._loopback_host({"Host": host}), host)

    def test_loopback_host_rejects_bare_or_unported_bracketed_ipv6_loopback(self):
        # Documents current behavior: rsplit(":", 1) on a colon-heavy IPv6
        # literal with no port strips the wrong substring, so these are
        # (perhaps surprisingly) rejected today. Not a CSRF/rebinding
        # weakening either way -- rejecting is the fail-safe direction.
        self.assertFalse(vb._loopback_host({"Host": "::1"}))
        self.assertFalse(vb._loopback_host({"Host": "[::1]"}))

    # ---- cmd_serve: warm-up failure + port-collision branches ---------------

    def test_cmd_serve_warmup_failure_and_non_alfred_port_collision(self):
        # Occupy a port with a bare listening socket (not a real HTTP server)
        # so _probe_daemon can't parse an identity from it -> "NOT an Alfred
        # daemon". cmd_serve tries the mlx warm-up BEFORE binding the port, so
        # this one test also exercises the warm-up-failure branch.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]

        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")

        def _boom(*a, **k):
            raise RuntimeError("boom")

        fake.transcribe = _boom
        sys.modules["mlx_whisper"] = fake
        saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None

        buf = io.StringIO()
        saved_err = sys.stderr
        sys.stderr = buf
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        try:
            result = vb.cmd_serve(args)
        finally:
            sys.stderr = saved_err
            blocker.close()
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._get_warm = saved_warm
            vb._DAEMON_MODE = False

        self.assertEqual(result, 0)
        out = buf.getvalue()
        self.assertIn("warm-up skipped", out)
        self.assertIn("NOT an Alfred daemon", out)

    def test_cmd_serve_port_collision_with_real_alfred_daemon(self):
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake
        saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None
        saved_info = vb._daemon_info_path
        import tempfile
        info_path = Path(tempfile.mkdtemp()) / "daemon.json"
        vb._daemon_info_path = lambda: info_path

        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        t = threading.Thread(target=vb.cmd_serve, args=(args,), daemon=True)
        t.start()
        try:
            self._wait_until_up(port)

            args2 = type("NS", (), {"port": port, "config": NO_CFG})()
            buf = io.StringIO()
            saved_err = sys.stderr
            sys.stderr = buf
            try:
                result = vb.cmd_serve(args2)
            finally:
                sys.stderr = saved_err

            self.assertEqual(result, 0)
            self.assertIn("already served by an Alfred daemon", buf.getvalue())

            # The original daemon on `port` is unaffected -- still answers.
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/")
            r = c.getresponse()
            body = json.loads(r.read().decode())
            c.close()
            self.assertTrue(body["ok"])
        finally:
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._get_warm = saved_warm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

    def test_cmd_serve_keyboard_interrupt_runs_cleanup(self):
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake
        saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None
        saved_info = vb._daemon_info_path
        import tempfile
        info_path = Path(tempfile.mkdtemp()) / "daemon.json"
        vb._daemon_info_path = lambda: info_path

        import http.server as hs
        seen = {}

        def _raise_ki(self, poll_interval=0.5):
            # By the time serve_forever is called, _write_daemon_info already
            # ran; the finally-cleanup below unlinks it after we raise.
            seen["info_existed"] = info_path.exists()
            raise KeyboardInterrupt()

        saved_serve_forever = hs.ThreadingHTTPServer.serve_forever
        hs.ThreadingHTTPServer.serve_forever = _raise_ki

        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        try:
            result = vb.cmd_serve(args)
        finally:
            hs.ThreadingHTTPServer.serve_forever = saved_serve_forever
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._get_warm = saved_warm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

        self.assertTrue(seen.get("info_existed"))
        self.assertFalse(info_path.exists())
        self.assertEqual(result, 0)

    # ---- cmd_serve._prewarm: the warm.ask() success path ---------------------

    def test_cmd_serve_prewarm_success_logs_claude_session_warm(self):
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake

        saved_should_prewarm = vb._should_prewarm_claude
        vb._should_prewarm_claude = lambda cfg: True

        class FakeWarm:
            def ask(self, prompt, timeout):
                return "ok"

        saved_warm = vb._get_warm
        # FakeWarm duck-types WarmClaude's one used method (.ask); it isn't a
        # real WarmClaude, so tell mypy this substitution is deliberate.
        vb._get_warm = lambda cfg, env: FakeWarm()  # type: ignore[return-value]

        saved_info = vb._daemon_info_path
        import tempfile
        info_path = Path(tempfile.mkdtemp()) / "daemon.json"
        vb._daemon_info_path = lambda: info_path

        buf = io.StringIO()
        saved_err = sys.stderr
        sys.stderr = buf
        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        t = threading.Thread(target=vb.cmd_serve, args=(args,), daemon=True)
        t.start()
        try:
            self._wait_until_up(port)
            deadline = time.time() + 5.0
            while "claude session warm." not in buf.getvalue() and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn("claude session warm.", buf.getvalue())
        finally:
            sys.stderr = saved_err
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._should_prewarm_claude = saved_should_prewarm
            vb._get_warm = saved_warm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

    def test_cmd_serve_prewarm_true_but_no_warm_session_is_a_silent_noop(self):
        # _should_prewarm_claude True but _get_warm returns None (e.g. no
        # backend logged in): _prewarm must skip warm.ask() cleanly -- no
        # "claude session warm." log, no "pre-warm skipped" error either.
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake

        saved_should_prewarm = vb._should_prewarm_claude
        vb._should_prewarm_claude = lambda cfg: True
        saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None

        saved_info = vb._daemon_info_path
        import tempfile
        info_path = Path(tempfile.mkdtemp()) / "daemon.json"
        vb._daemon_info_path = lambda: info_path

        buf = io.StringIO()
        saved_err = sys.stderr
        sys.stderr = buf
        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        t = threading.Thread(target=vb.cmd_serve, args=(args,), daemon=True)
        t.start()
        try:
            self._wait_until_up(port)
            # No signal the background thread ran at all except the absence
            # of the other two outcomes; give it time, then check the daemon
            # is still healthy and neither other message ever showed up.
            time.sleep(0.3)
            self.assertTrue(self._probe_up_health(port))
            self.assertNotIn("claude session warm.", buf.getvalue())
            self.assertNotIn("pre-warm skipped", buf.getvalue())
        finally:
            sys.stderr = saved_err
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._should_prewarm_claude = saved_should_prewarm
            vb._get_warm = saved_warm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

    def test_cmd_serve_prewarm_exception_is_swallowed_and_logged(self):
        # Anything inside _prewarm's try (config load, the prewarm check, the
        # warm session itself) may raise; it must be caught and logged, never
        # crash the background thread or the daemon.
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake

        saved_should_prewarm = vb._should_prewarm_claude

        def _boom(cfg):
            raise RuntimeError("prewarm check exploded")

        vb._should_prewarm_claude = _boom

        saved_info = vb._daemon_info_path
        import tempfile
        info_path = Path(tempfile.mkdtemp()) / "daemon.json"
        vb._daemon_info_path = lambda: info_path

        buf = io.StringIO()
        saved_err = sys.stderr
        sys.stderr = buf
        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        t = threading.Thread(target=vb.cmd_serve, args=(args,), daemon=True)
        t.start()
        try:
            self._wait_until_up(port)
            deadline = time.time() + 5.0
            while "pre-warm skipped" not in buf.getvalue() and time.time() < deadline:
                time.sleep(0.05)
            self.assertIn("pre-warm skipped (prewarm check exploded)", buf.getvalue())
            self.assertTrue(self._probe_up_health(port))  # daemon survived it
        finally:
            sys.stderr = saved_err
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._should_prewarm_claude = saved_should_prewarm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

    def _probe_up_health(self, port):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        r = c.getresponse()
        ok = json.loads(r.read().decode())["ok"]
        c.close()
        return ok

    def test_cmd_serve_keyboard_interrupt_cleanup_swallows_missing_info_file(self):
        # If the daemon-info file doesn't exist at cleanup time (its own write
        # having silently failed -- see the _write_daemon_info tests above),
        # unlink() raises FileNotFoundError; the finally-cleanup must swallow
        # it too, same as a successful unlink, not let it escape cmd_serve.
        saved_mod = sys.modules.get("mlx_whisper")
        fake = types.ModuleType("mlx_whisper")
        fake.transcribe = lambda *a, **k: {"text": "", "language": None}
        sys.modules["mlx_whisper"] = fake
        saved_warm = vb._get_warm
        vb._get_warm = lambda cfg, env: None
        saved_info = vb._daemon_info_path
        missing = Path("/nonexistent/alfred-test-dir-2/daemon.json")
        vb._daemon_info_path = lambda: missing

        import http.server as hs

        def _raise_ki(self, poll_interval=0.5):
            raise KeyboardInterrupt()

        saved_serve_forever = hs.ThreadingHTTPServer.serve_forever
        hs.ThreadingHTTPServer.serve_forever = _raise_ki

        port = _free_port()
        args = type("NS", (), {"port": port, "config": NO_CFG})()
        try:
            result = vb.cmd_serve(args)
        finally:
            hs.ThreadingHTTPServer.serve_forever = saved_serve_forever
            sys.modules.pop("mlx_whisper", None)
            if saved_mod is not None:
                sys.modules["mlx_whisper"] = saved_mod
            vb._get_warm = saved_warm
            vb._daemon_info_path = saved_info
            vb._DAEMON_MODE = False

        self.assertEqual(result, 0)  # cleanup's OSError was swallowed, not raised
        self.assertFalse(missing.exists())

    # ---- main: KeyboardInterrupt -> exit 130 -------------------------------

    def test_main_returns_130_on_keyboardinterrupt(self):
        # main() builds its own parser fresh on every call (unlike the
        # class-level shared daemon above, whose parser is built once in
        # setUpClass), so patching vb.cmd_doctor right before calling main()
        # does reach dispatch here.
        orig = vb.cmd_doctor

        def _boom(args):
            raise KeyboardInterrupt()

        vb.cmd_doctor = _boom
        try:
            result = vb.main(["doctor"])
        finally:
            vb.cmd_doctor = orig
        self.assertEqual(result, 130)


if __name__ == "__main__":
    unittest.main()
