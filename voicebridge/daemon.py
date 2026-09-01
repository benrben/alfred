"""The warm background engine (localhost HTTP daemon) and argument parsing /
CLI entry point.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading

import voicebridge as _pkg


def _bool_flag(parser, name, help_on, help_off):
    g = parser.add_mutually_exclusive_group()
    g.add_argument(f"--{name}", dest=name, action="store_true", default=None, help=help_on)
    g.add_argument(f"--no-{name}", dest=name, action="store_false", default=None, help=help_off)


def add_common(p):
    p.add_argument("--config", help="path to config.toml")
    p.add_argument(
        "--backend",
        choices=["local", "auto", "claude", "codex"],
        help="override LLM backend (local = on-device MLX)",
    )
    p.add_argument("--model", help="override model name for the chosen backend")
    p.add_argument("--language", help="STT language code, or 'auto'")
    p.add_argument(
        "--mode",
        help="rewrite target / intent, e.g. email|message|commit|"
        "prompt|notes|raw or a custom [intent] mode (also enables "
        "--rewrite). See `voicebridge.py modes`.",
    )
    _bool_flag(p, "translate", "translate output to English", "do not translate")
    _bool_flag(p, "rewrite", "clean up & shape to intent", "do not rewrite")
    _bool_flag(p, "optimize", "tighten & clarify", "do not optimize")
    # Transcribe-only: a one-flag "pure transcript" that pins EVERY LLM stage off,
    # winning over --translate/--rewrite/--optimize/--mode. Front-ends surface it
    # as a dedicated "Transcribe Only" capture.
    p.add_argument(
        "--transcribe-only",
        dest="transcribe_only",
        action="store_true",
        default=None,
        help="pure transcription: skip all LLM stages (translate/rewrite/optimize)",
    )
    # Timestamped transcripts: '[m:ss] …' per Whisper segment. Batch paths only
    # (process / stream-finish's no-session fallback); a live streaming session
    # transcribes in windows whose segment clocks restart per chunk, so it stays
    # plain. LLM stages would rewrite the markers — pair with --transcribe-only.
    p.add_argument(
        "--timestamps",
        action="store_true",
        default=None,
        help="prefix each transcript segment with [m:ss] (batch only; use with --transcribe-only)",
    )
    _bool_flag(p, "paste", "auto-paste after copying", "copy only")
    p.add_argument(
        "--stdout", action="store_true", help="print result to stdout instead of clipboard/file"
    )


class _ThreadStream:
    """A sys.stdout/sys.stderr stand-in that routes each thread's writes to a
    per-thread buffer when one is installed, else to the real underlying stream.

    This lets the threaded daemon capture EACH request's output concurrently with
    no global lock — where a naive per-request contextlib.redirect_stdout mutates
    the process-global sys.stdout and races other requests. Installed once at
    serve start; do_POST pushes/pops a buffer around each dispatch. A capture used
    to serialize every POST (so a multi-second transcribe+LLM blocked all other
    commands); this removes that serialization while keeping outputs uncrossed."""

    def __init__(self, real):
        self._real = real
        self._tl = threading.local()

    def redirect(self, buf) -> None:
        self._tl.buf = buf

    def restore(self) -> None:
        self._tl.buf = None

    def _target(self):
        buf = getattr(self._tl, "buf", None)
        return buf if buf is not None else self._real

    def write(self, s):
        return self._target().write(s)

    def flush(self):
        try:
            self._target().flush()
        except Exception:  # noqa: BLE001
            pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


def _daemon_identity() -> dict:
    return {
        "ok": True,
        "app": "alfred",
        "schema_version": _pkg.CONTRACT["schema_version"],
        "pid": os.getpid(),
    }


def _write_daemon_info(port: int) -> None:
    """Write the discovery/identity file (0600) so a front-end — and `serve`
    itself on a busy port — can tell an Alfred daemon from a foreign server."""
    try:
        _pkg._atomic_write_json(_pkg._daemon_info_path(), {**_daemon_identity(), "port": port})
    except Exception:  # noqa: BLE001
        pass


def _probe_daemon(port: int, timeout: float = 1.0) -> dict | None:
    """GET / on a port and return the JSON identity, or None if it isn't a
    reachable HTTP server we can parse."""
    import http.client

    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        c.request("GET", "/")
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def _activate_thread_streams(out_proxy, err_proxy) -> None:
    """Ensure the per-thread router is the active stream (a test harness or
    anything else may have swapped sys.stdout since); idempotent."""
    if sys.stdout is not out_proxy:
        sys.stdout = out_proxy
    if sys.stderr is not err_proxy:
        sys.stderr = err_proxy


def _run_request(parser, argv, out_proxy, err_proxy) -> tuple[int, str, str]:
    """Parse+dispatch one daemon request's argv, capturing this thread's
    stdout/stderr into fresh per-request buffers. Returns (code, out, err)."""
    import io

    out_buf, err_buf = io.StringIO(), io.StringIO()
    _activate_thread_streams(out_proxy, err_proxy)
    # Per-thread capture (no lock): this request's prints go to its own
    # buffers; other requests run fully in parallel.
    out_proxy.redirect(out_buf)
    err_proxy.redirect(err_buf)
    code = 1
    try:
        ns = parser.parse_args(argv)
        code = ns.func(ns)
    except SystemExit as e:
        code = int(e.code or 0)
    except RuntimeError as e:
        sys.stderr.write(f"error: {e}\n")
        _pkg.print_status("error", "runtime")
        code = 1
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"alfred: request failed: {e}\n")
        _pkg.print_status("error", "runtime")
        code = 1
    finally:
        out_proxy.restore()
        err_proxy.restore()
    return code, out_buf.getvalue(), err_buf.getvalue()


def _parse_request_body(rfile, content_length: int) -> dict:
    """The request's JSON body, or {} on a missing/malformed one — a
    daemon request never 500s on a bad client, it just dispatches no argv."""
    try:
        return json.loads(rfile.read(content_length) or b"{}")
    except Exception:  # noqa: BLE001
        return {}


def _report_port_conflict(port: int, error: OSError) -> None:
    """Explain why cmd_serve couldn't bind `port`: an existing Alfred daemon
    (fine, nothing to do) or something else holding it (a real problem)."""
    who = _probe_daemon(port)
    if who and who.get("app") == "alfred":
        sys.stderr.write(
            f"alfred: port {port} already served by an Alfred "
            f"daemon (pid {who.get('pid')}) — exiting.\n"
        )
    else:
        sys.stderr.write(
            f"alfred: port {port} busy ({error}) and NOT an Alfred "
            "daemon; refusing to start. Free the port or set a "
            "different one.\n"
        )


def _loopback_host(headers) -> bool:
    """True if the request's Host header names loopback (or is absent). Rejecting
    a non-loopback Host defeats DNS-rebinding: the rebound page sends the
    attacker's hostname, not 127.0.0.1."""
    host = (headers.get("Host") or "").strip()
    if not host:
        return True
    hostname = host.rsplit(":", 1)[0].strip("[]")
    return hostname in ("127.0.0.1", "localhost", "::1")


def cmd_serve(args) -> int:
    """Warm background engine: load the Whisper model once and serve requests
    over localhost HTTP, so each dictation skips the multi-second model load.
    Each request is a JSON body {"argv": [...]} = the same args the one-shot CLI
    would take; the response is {"code", "out", "err"} (out/err = captured
    stdout/stderr). GET / returns the daemon's identity; GET /contract the
    contract. Host + Origin checks block browser CSRF / DNS-rebinding."""
    import contextlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    _pkg._DAEMON_MODE = True  # allow a warm claude session

    parser = build_parser()

    # Warm the model now (mlx-whisper caches it for the life of the process).
    cfg0 = None
    try:
        import mlx_whisper
        import numpy as np

        cfg0 = _pkg.load_config(args.config)
        sys.stderr.write("alfred: warming Whisper model…\n")
        sys.stderr.flush()
        with contextlib.redirect_stdout(sys.stderr):
            mlx_whisper.transcribe(
                np.zeros(16000, dtype="float32"),
                path_or_hf_repo=cfg0["stt"]["model"],
                verbose=False,
            )
        sys.stderr.write("alfred: model ready.\n")
        sys.stderr.flush()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"alfred: warm-up skipped ({e}); loads on first request.\n")

    # Pre-warm the claude session in the background so the first capture is fast
    # too (it pays the ~3s CLI startup now, off the critical path).
    def _prewarm():
        try:
            cfg = cfg0 if cfg0 is not None else _pkg.load_config(args.config)
            if _pkg._should_prewarm_claude(cfg):
                warm = _pkg._get_warm(cfg, _pkg._clean_env(_pkg._CLAUDE_KEY_VARS))
                if warm is not None:
                    warm.ask("Reply with exactly: ok", 60)
                    sys.stderr.write("alfred: claude session warm.\n")
                    sys.stderr.flush()
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"alfred: claude pre-warm skipped ({e}).\n")

    threading.Thread(target=_prewarm, daemon=True).start()

    # Route each request thread's stdout/stderr to its own buffer (no global
    # lock, no serialization) so concurrent commands — and a long capture's
    # transcribe+LLM — never block or cross each other. do_POST installs these as
    # sys.stdout/stderr on first use (idempotent — same object, so concurrent
    # installs don't race) and never restores per-request; they stay put.
    _real_out, _real_err = sys.stdout, sys.stderr
    out_proxy, err_proxy = _ThreadStream(_real_out), _ThreadStream(_real_err)

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status, obj):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not _loopback_host(self.headers):
                self._json(403, {"error": "bad host"})
                return
            if self.path == "/contract":  # the IPC contract
                self._json(
                    200, _pkg.resolved_contract(_pkg.load_config(getattr(args, "config", None)))
                )
            else:  # health + identity
                self._json(200, _daemon_identity())

        def do_POST(self):
            # CSRF / DNS-rebinding guards: reject a non-loopback Host and any
            # cross-Origin POST. Legit callers (Node fetch to localhost,
            # Hammerspoon hs.http) send no Origin; a browser page always does.
            if not _loopback_host(self.headers):
                self._json(403, {"error": "bad host"})
                return
            if self.headers.get("Origin"):
                self._json(403, {"error": "cross-origin POST refused"})
                return
            req = _parse_request_body(
                self.rfile, int(self.headers.get("Content-Length", 0))
            )
            code, out, err = _run_request(
                parser, req.get("argv") or [], out_proxy, err_proxy
            )
            if err:  # keep it in the daemon log too
                _real_err.write(err)
            self._json(200, {"code": code, "out": out, "err": err})

        def log_message(self, *_a):
            pass

    port = int(args.port)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        _report_port_conflict(port, e)
        return 0
    _write_daemon_info(port)
    sys.stderr.write(f"alfred: serving on 127.0.0.1:{port}\n")
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            _pkg._daemon_info_path().unlink()
        except OSError:
            pass
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="voicebridge.py",
        description="Local STT + LLM cleanup for macOS (Apple Silicon).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_proc = sub.add_parser("process", help="transcribe an audio file and deliver")
    p_proc.add_argument("audio", help="path to the recorded audio file (wav)")
    add_common(p_proc)
    p_proc.set_defaults(func=_pkg.cmd_process)

    p_ss = sub.add_parser("stream-start", help="begin transcribing a growing WAV (daemon)")
    p_ss.add_argument("audio", help="path to the WAV sox is recording into")
    add_common(p_ss)
    p_ss.set_defaults(func=_pkg.cmd_stream_start)

    p_sf = sub.add_parser("stream-finish", help="finish a streamed recording: tail + LLM + deliver")
    p_sf.add_argument("audio", help="path to the recorded WAV")
    add_common(p_sf)
    p_sf.set_defaults(func=_pkg.cmd_stream_finish)

    p_text = sub.add_parser("text", help="run the pipeline on text (Type mode)")
    p_text.add_argument("text", nargs="?", help="text, or '-'/omit to read stdin")
    p_text.add_argument(
        "--instruction",
        help="apply a free-text instruction to "
        "the text (feedback refine: 'make it shorter') instead of "
        "the configured stages",
    )
    add_common(p_text)
    p_text.set_defaults(func=_pkg.cmd_text)

    p_hist = sub.add_parser("history", help="list or re-copy recent results")
    p_hist.add_argument("--config")
    p_hist.add_argument("--limit", type=int, default=10)
    p_hist.add_argument(
        "--copy", type=int, metavar="N", help="copy history item N (0 = most recent) to clipboard"
    )
    p_hist.set_defaults(func=_pkg.cmd_history)

    p_modes = sub.add_parser("modes", help="list rewrite modes (built-in + custom) as JSON")
    p_modes.add_argument("--config")
    p_modes.set_defaults(func=_pkg.cmd_modes)

    p_si = sub.add_parser("set-intent", help="save/override an intent prompt in config.toml")
    p_si.add_argument("key")
    p_si.add_argument("--prompt", default="")
    p_si.add_argument("--label")
    p_si.add_argument("--description")
    p_si.add_argument("--config")
    p_si.set_defaults(func=_pkg.cmd_set_intent)

    p_serve = sub.add_parser("serve", help="run a warm background engine (localhost HTTP)")
    p_serve.add_argument("--port", type=int, default=_pkg.DAEMON_PORT)
    p_serve.add_argument("--config")
    p_serve.set_defaults(func=cmd_serve)

    p_set = sub.add_parser("set-model", help="persist claude_model / codex_model in config")
    p_set.add_argument("backend", choices=["claude", "codex"])
    p_set.add_argument("--model", default="")
    p_set.add_argument("--config")
    p_set.set_defaults(func=_pkg.cmd_set_model)

    p_sp = sub.add_parser(
        "set-processing", help="persist [processing] defaults (mode + stage toggles)"
    )
    p_sp.add_argument("--mode", help="default rewrite mode/intent, or 'raw'")
    _bool_flag(p_sp, "rewrite", "enable rewrite by default", "disable rewrite by default")
    _bool_flag(p_sp, "translate", "translate by default", "do not translate by default")
    _bool_flag(p_sp, "optimize", "optimize by default", "do not optimize by default")
    p_sp.add_argument("--config")
    p_sp.set_defaults(func=_pkg.cmd_set_processing)

    p_st = sub.add_parser("set-stt", help="persist [stt] settings (vocab/initial_prompt, language)")
    p_st.add_argument(
        "--initial-prompt", dest="initial_prompt", help="vocabulary/name biasing for the STT model"
    )
    p_st.add_argument("--language", help="STT language code, or 'auto'")
    p_st.add_argument("--config")
    p_st.set_defaults(func=_pkg.cmd_set_stt)

    p_get = sub.add_parser("settings", help="print backend/model settings + lists as JSON")
    p_get.add_argument("--config")
    p_get.set_defaults(func=_pkg.cmd_settings)

    p_doc = sub.add_parser("doctor", help="check the environment")
    p_doc.add_argument("--config")
    p_doc.set_defaults(func=_pkg.cmd_doctor)

    p_con = sub.add_parser(
        "contract",
        help="print the IPC contract (state files + daemon API + status grammar) as JSON",
    )
    p_con.set_defaults(func=_pkg.cmd_contract)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        # Any failure (RuntimeError, a stray TOMLDecodeError/JSONDecodeError, …)
        # ends with a VB_STATUS line so the front-end never sees a bare traceback
        # with no machine-readable status. The contract promise: VB_STATUS is the
        # LAST line of every command a front-end drives — a front-end caller
        # never passes --stdout, the one path that prints no VB_STATUS at all.
        sys.stderr.write(f"error: {e}\n")
        _pkg.print_status("error", "runtime")
        return 1
