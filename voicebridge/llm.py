"""LLM backends: keyless CLI backends (claude/codex, with a warm-process
optimization) and the strictly on-device MLX local backend.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import voicebridge as _pkg

_EXTRA_BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
    os.path.expanduser("~/.cargo/bin"),
    os.path.expanduser("~/.bun/bin"),
]


# Env vars stripped before spawning each CLI so it uses the subscription login,
# never an API key — AND never a cloud gateway/router. The keyless + "nothing
# leaves the machine" promise depends on dropping the provider-routing vars too:
# a stray CLAUDE_CODE_USE_BEDROCK / ANTHROPIC_BASE_URL / OPENAI_BASE_URL in the
# user's shell would silently bill a cloud provider or route dictation text to an
# unexpected endpoint. (The keyless guarantee also assumes fast mode, which adds
# --setting-sources "" so a settings apiKeyHelper can't re-inject a key.)
_CLAUDE_KEY_VARS = [
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
]
_CODEX_KEY_VARS = ["OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"]


def find_tool(name: str) -> str | None:
    p = shutil.which(name)
    if p:
        return p
    for d in _EXTRA_BIN_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def detect_backends() -> dict:
    return {"claude": find_tool("claude"), "codex": find_tool("codex")}


def _should_prewarm_claude(cfg: dict) -> bool:
    """Whether the daemon should spawn + prompt a warm claude session at
    startup. Only 'claude' and 'auto' ever route to it (see _get_warm /
    candidate_backends) — 'local' and 'codex' must never spawn or prompt a
    claude process: local's whole point is that nothing leaves the machine,
    and codex has no use for a claude session."""
    return bool(cfg["llm"].get("warm", True)) and cfg["llm"].get("backend") in ("claude", "auto")


def candidate_backends(cfg: dict) -> list[str]:
    """Ordered list of backends to try. 'local' is the in-process MLX model (no
    binary to find — availability is checked at call time). For 'auto' we return
    both CLIs so a failure (e.g. claude not logged in) falls back to the other.
    We deliberately do NOT fall back from 'local' to a network CLI: strict-local
    must never silently make a cloud call."""
    want = cfg["llm"]["backend"]
    if want == "local":
        return ["local"]
    have = detect_backends()
    order = [want] if want in ("claude", "codex") else ["claude", "codex"]
    found = [b for b in order if have[b]]
    if not found:
        raise RuntimeError(
            "no LLM backend found. Install Claude Code (`claude`) or Codex "
            '(`codex`) and sign in once, set backend = "local" for the '
            "on-device model, or disable the translate/rewrite/optimize stages."
        )
    return found


def run_llm_fallback(backends: list[str], prompt: str, cfg: dict) -> str:
    """Try each backend in order; return the first success. Raise the last error
    only if all fail (so 'auto' survives a logged-out / broken backend)."""
    last = None
    for b in backends:
        try:
            return _pkg.run_llm(b, prompt, cfg)
        except RuntimeError as e:
            last = e
            sys.stderr.write(f"warning: backend '{b}' failed ({e}); trying next.\n")
    raise last if last else RuntimeError("no backend produced output")


def _clean_env(drop: list[str]) -> dict:
    env = os.environ.copy()
    for k in drop:
        env.pop(k, None)
    env.setdefault("NO_COLOR", "1")
    # Force UTF-8 so the CLI emits (and we read) UTF-8 even when a GUI launcher
    # gave us a bare/non-UTF-8 locale (macOS can default to mac-roman).
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env.setdefault("PYTHONUTF8", "1")
    # claude/codex read their OAuth login from the macOS Keychain, whose lookup
    # needs the user identity in the env. GUI launchers (Raycast, Hammerspoon)
    # can spawn us without USER set, in which case claude reports "Not logged in"
    # and the whole LLM step fails. Backfill it from the OS so we don't depend on
    # the launcher's environment.
    if not env.get("USER"):
        try:
            import pwd

            env["USER"] = pwd.getpwuid(os.getuid()).pw_name
        except Exception:  # noqa: BLE001
            pass
    if env.get("USER"):
        env.setdefault("LOGNAME", env["USER"])
    return env


def _run_subprocess(cmd: list[str], env: dict, timeout: int | None) -> subprocess.CompletedProcess:
    """Run the CLI, translating the two failure modes the fallback loop must
    understand (a hang, a missing/unrunnable binary) into RuntimeError."""
    try:
        return subprocess.run(
            cmd,
            env=env,
            timeout=timeout,
            cwd=tempfile.gettempdir(),  # neutral dir: don't scan user's project
            input="",  # close stdin so the CLI doesn't wait on it
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # decode as UTF-8, not locale
        )
    except subprocess.TimeoutExpired:
        # Speak the one exception type the fallback loop understands, so a hung
        # CLI in `auto` mode falls back to the next backend instead of aborting.
        # Don't embed str(e) — it includes the full command (and thus the prompt).
        raise RuntimeError(f"{os.path.basename(cmd[0])} timed out after {timeout}s") from None
    except OSError as e:
        # Binary vanished between find_tool and exec, permission denied, etc.
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed to run: {e}") from e


def _error_lines(text: str) -> list[str]:
    """Split a failed run's stderr/stdout into non-blank lines."""
    return [l for l in text.splitlines() if l.strip()]


def _best_error_line(lines: list[str], returncode: int) -> str:
    """Prefer a real error line over generic warning/progress noise; fall back
    to the exit code when there's no output at all."""
    meaningful = [l for l in lines if not l.lower().lstrip().startswith(("warning:", "note:"))]
    pick = meaningful or lines
    return pick[-1].strip() if pick else f"exit {returncode}"


def _run(cmd: list[str], env: dict, timeout: int | None) -> str:
    proc = _run_subprocess(cmd, env, timeout)
    if proc.returncode != 0:
        text = proc.stderr or proc.stdout or ""
        msg = _best_error_line(_error_lines(text), proc.returncode)
        raise RuntimeError(f"{cmd[0]} failed: {msg}")
    return (proc.stdout or "").strip()


# Set True by `serve` so run_llm knows it may keep a warm process alive. One-shot
# CLI runs leave this False (a warm process would never be reused).


def _claude_warm_cmd(cfg: dict) -> list[str]:
    """The claude command for a persistent stream-json session (no prompt arg —
    prompts are sent as messages over stdin)."""
    cmd = [
        find_tool("claude") or "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if cfg["llm"].get("claude_model"):
        cmd += ["--model", cfg["llm"]["claude_model"]]
    if cfg["llm"].get("claude_effort"):
        cmd += ["--effort", cfg["llm"]["claude_effort"]]
    if cfg["llm"].get("fast", True):
        cmd += ["--strict-mcp-config", "--setting-sources", ""]
    cmd += list(cfg["llm"].get("claude_extra_args") or [])
    return cmd


class WarmClaude:
    """A long-lived `claude` process fed prompts over a stream-json pipe, so the
    ~3s CLI startup is paid once instead of per call. Single-flight (serialized
    by a lock); recycles the process after N turns / idle / on any error. The
    caller falls back to a one-shot run if a turn fails."""

    def __init__(self, cmd: list[str], env: dict, max_turns: int, idle_secs: int):
        self.cmd, self.env = cmd, env
        self.max_turns, self.idle_secs = max_turns, idle_secs
        self._proc: subprocess.Popen | None = None
        self._q: queue.Queue = queue.Queue()
        self._turns = 0
        self._last = 0.0
        self._lock = threading.Lock()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _stop(self) -> None:
        p, self._proc = self._proc, None
        if not p:
            return
        for step in (lambda: p.stdin and p.stdin.close(), p.terminate, p.kill):
            try:
                step()
            except Exception:  # noqa: BLE001
                pass

    def _start(self) -> None:
        self._stop()
        self._q = queue.Queue()
        p = subprocess.Popen(
            self.cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self.env,
            cwd=tempfile.gettempdir(),
        )
        self._proc = p
        q = self._q

        def pump_out():
            try:
                for line in p.stdout:
                    q.put(line)
            except Exception:  # noqa: BLE001
                pass
            q.put(None)  # sentinel: stream closed

        def drain_err():
            try:
                for _ in p.stderr:  # keep the pipe from filling
                    pass
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=pump_out, daemon=True).start()
        threading.Thread(target=drain_err, daemon=True).start()
        self._turns = 0

    def _should_restart(self) -> bool:
        """Whether ask() must (re)start the process before sending this turn:
        not running, recycled past max_turns, or idle past idle_secs."""
        stale = bool(self._last) and time.monotonic() - self._last > self.idle_secs
        return not self._alive() or self._turns >= self.max_turns or stale

    def _drain_queue(self) -> None:
        """Drop anything left over from a prior turn before sending ours."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _write_turn(self, prompt: str) -> None:
        """Write one stream-json user turn to the process's stdin. A write
        failure kills the process (so the next ask() starts fresh) and speaks
        RuntimeError, the type the caller's fallback logic understands."""
        msg = {"type": "user", "message": {"role": "user", "content": prompt}}
        proc = self._proc
        assert proc is not None and proc.stdin is not None, (
            "_start() above always sets self._proc with stdin=PIPE"
        )
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except Exception as e:  # noqa: BLE001
            self._stop()
            raise RuntimeError(f"warm claude write failed: {e}")

    def _next_line(self, remaining: float) -> str:
        """Read the next line pumped from stdout, or raise if the process
        hung (queue.Empty) or exited (the pump thread's None sentinel) —
        stopping the process either way."""
        try:
            line = self._q.get(timeout=remaining)
        except queue.Empty:
            self._stop()
            raise RuntimeError("warm claude timed out")
        if line is None:
            self._stop()
            raise RuntimeError("warm claude exited")
        return line

    def _parse_result_line(self, raw: str) -> dict | None:
        """Parse one line of the CLI's stream-json stdout. Returns the object
        once it's a terminal 'result' message; None for a blank, non-JSON, or
        interim (non-result) line, which the caller should skip."""
        line = raw.strip()
        if not line:
            return None
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            return None
        if obj.get("type") != "result":
            return None
        return obj

    def _wait_for_result_line(self, deadline: float) -> dict:
        """Read lines until the terminal 'result' message arrives or the
        deadline passes."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._stop()
                raise RuntimeError("warm claude timed out")
            obj = self._parse_result_line(self._next_line(remaining))
            if obj is not None:
                return obj

    def _finish_turn(self, obj: dict) -> str:
        """Bookkeeping for a completed turn, then surface its result — or
        raise on an error subtype or empty output."""
        self._turns += 1
        self._last = time.monotonic()
        if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
            self._stop()
            raise RuntimeError(f"warm claude error: {obj.get('subtype')}")
        out = (obj.get("result") or "").strip()
        if not out:
            raise RuntimeError("warm claude returned empty")
        return out

    def ask(self, prompt: str, timeout: float | None) -> str:
        with self._lock:
            if self._should_restart():
                self._start()
            self._drain_queue()
            self._write_turn(prompt)
            deadline = time.monotonic() + (timeout or 120)
            obj = self._wait_for_result_line(deadline)
            return self._finish_turn(obj)


_WARM: WarmClaude | None = None
_WARM_SIG: tuple | None = None
_WARM_LOCK = threading.Lock()


def _get_warm(cfg: dict, env: dict) -> WarmClaude | None:
    """The shared warm-claude session, or None when warm mode doesn't apply
    (not the daemon, or disabled). Rebuilt if the relevant config changes."""
    global _WARM, _WARM_SIG
    if not _pkg._DAEMON_MODE or not cfg["llm"].get("warm", True):
        return None
    cmd = _claude_warm_cmd(cfg)
    sig = (
        tuple(cmd),
        int(cfg["llm"].get("warm_max_turns", 25)),
        int(cfg["llm"].get("warm_idle_secs", 600)),
    )
    with _WARM_LOCK:
        if _WARM is None or _WARM_SIG != sig:
            if _WARM is not None:
                _WARM._stop()
            _WARM = WarmClaude(cmd, env, sig[1], sig[2])
            _WARM_SIG = sig
        return _WARM


def _timeout_seconds(cfg: dict) -> int | None:
    t = int(cfg["llm"]["timeout"])
    return t if t > 0 else None  # 0 = no timeout (big prompts)


def _claude_oneshot_cmd(cfg: dict, prompt: str) -> list[str]:
    """The claude command for a single one-shot run (prompt passed as an arg,
    not over stdin) — used when there's no warm session, or it just failed."""
    cmd = [find_tool("claude") or "claude", "-p", prompt]
    if cfg["llm"].get("claude_model"):
        cmd += ["--model", cfg["llm"]["claude_model"]]
    if cfg["llm"].get("claude_effort"):
        # Low reasoning effort = faster; deep thinking isn't needed to clean
        # up dictation.
        cmd += ["--effort", cfg["llm"]["claude_effort"]]
    if cfg["llm"].get("fast", True):
        # Skip the user's MCP servers, plugins, hooks, CLAUDE.md and settings:
        # pure startup overhead for a one-shot text transform.
        cmd += ["--strict-mcp-config", "--setting-sources", ""]
    cmd += list(cfg["llm"].get("claude_extra_args") or [])
    return cmd


def _codex_cmd(cfg: dict, prompt: str) -> list[str]:
    """The codex `exec` one-shot command."""
    cmd = [
        find_tool("codex") or "codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
    ]
    if cfg["llm"].get("codex_reasoning_effort"):
        # Low reasoning effort = faster. ("minimal" is rejected while codex's
        # web_search/image_gen tools are enabled, so we default to "low".)
        # Bare value (no quotes) — there's no shell here to strip them.
        cmd += ["-c", f"model_reasoning_effort={cfg['llm']['codex_reasoning_effort']}"]
    if cfg["llm"].get("codex_model"):
        cmd += ["-m", cfg["llm"]["codex_model"]]
    cmd += list(cfg["llm"].get("codex_extra_args") or [])
    cmd += [prompt]
    return cmd


def _run_claude(prompt: str, cfg: dict, timeout: int | None) -> str:
    """Prefer the shared warm session; fall back to a one-shot run when
    there's no warm session, or the warm turn fails."""
    # Strip API-key + provider-routing vars so claude uses the subscription
    # OAuth login and never a cloud gateway.
    env = _clean_env(_CLAUDE_KEY_VARS)
    warm = _pkg._get_warm(cfg, env)
    if warm is not None:
        try:
            return _strip_wrapping(warm.ask(prompt, timeout))
        except Exception as e:  # noqa: BLE001
            sys.stderr.write(f"warning: warm claude failed ({e}); one-shot fallback.\n")
    return _pkg.run_llm_clean(_claude_oneshot_cmd(cfg, prompt), env, timeout)


def _run_codex(prompt: str, cfg: dict, timeout: int | None) -> str:
    # Strip API-key + routing vars so codex uses the ChatGPT login, not the API.
    env = _clean_env(_CODEX_KEY_VARS)
    return _pkg.run_llm_clean(_codex_cmd(cfg, prompt), env, timeout)


def run_llm(backend: str, prompt: str, cfg: dict) -> str:
    timeout = _timeout_seconds(cfg)
    if backend == "local":
        return run_local_llm(prompt, cfg)
    if backend == "claude":
        return _run_claude(prompt, cfg, timeout)
    if backend == "codex":
        return _run_codex(prompt, cfg, timeout)
    raise RuntimeError(f"unknown backend '{backend}'")


def run_llm_clean(cmd: list[str], env: dict, timeout: int | None) -> str:
    out = _run(cmd, env, timeout)
    return _strip_wrapping(out)


def _strip_code_fence(t: str) -> str:
    """Remove a leading/trailing ``` fence, if the text is wrapped in one."""
    if not t.startswith("```"):
        return t
    lines = t.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _strip_matching_quotes(t: str) -> str:
    """Remove one layer of surrounding matching quotes, if present."""
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        return t[1:-1].strip()
    return t


def _strip_wrapping(text: str) -> str:
    """Remove accidental surrounding quotes / code fences from model output."""
    return _strip_matching_quotes(_strip_code_fence(text.strip()))


# ----------------------------------------------------------------------------
# Local LLM backend  (strict on-device: MLX-LM, no network, no API key)
# ----------------------------------------------------------------------------
#
# A warm, in-process MLX model held in memory across captures so we pay the
# multi-second model load once, not per transform — the local analogue of the
# warm-claude session. Single-flight (MLX generation is not re-entrant on one
# model); the model is freed after `local_idle_secs` to reclaim RAM (it shares
# the machine with the Whisper model). The two model-touching seams (_local_load
# / _local_generate) are kept tiny and separate so tests can stub them without
# importing mlx_lm or downloading weights.

_LOCAL_LOCK = threading.Lock()


def _local_load(model_id: str):
    """Load an MLX model + tokenizer. Lazy import so the rest of the engine runs
    without mlx-lm installed; the clear error guides install when it's missing."""
    try:
        from mlx_lm import load
    except ModuleNotFoundError as e:
        raise RuntimeError(
            'mlx-lm is not installed (needed for backend = "local"). Install '
            "with: pip install mlx-lm  (Apple Silicon), or set backend to "
            '"auto"/"claude"/"codex".'
        ) from e
    return load(model_id)


def _local_generate(model, tokenizer, prompt: str, max_tokens: int) -> str:
    """One generation turn. Applies the tokenizer's chat template when present so
    instruct models behave, then generates. Isolated for stubbing in tests."""
    from mlx_lm import generate

    text = prompt
    if getattr(tokenizer, "chat_template", None):
        text = tokenizer.apply_chat_template(
            [{"role": "user", "content": prompt}],
            add_generation_prompt=True,
            tokenize=False,
        )
    return generate(model, tokenizer, prompt=text, max_tokens=max_tokens, verbose=False)


def _local_model_id(cfg: dict) -> str:
    return cfg["llm"].get("local_model") or _pkg.DEFAULTS["llm"]["local_model"]


def _local_is_stale(idle: int) -> bool:
    """Whether the warm on-device model has sat unused past `idle` seconds."""
    return bool(_pkg._LOCAL_LAST) and idle > 0 and time.monotonic() - _pkg._LOCAL_LAST > idle


def _ensure_local_model(model_id: str, idle: int) -> tuple:
    """(Re)load the warm on-device model if it's unloaded, the requested model
    changed, or it's been idle too long. Returns (model, tokenizer)."""
    if _pkg._LOCAL is None or _pkg._LOCAL_SIG != model_id or _local_is_stale(idle):
        _pkg._LOCAL = _pkg._local_load(model_id)  # (re)load; warm for next call
        _pkg._LOCAL_SIG = model_id
    return _pkg._LOCAL


def _generate_local(model, tokenizer, prompt: str, max_tokens: int) -> str:
    """Run one generation turn; drop the warm model on failure so the next
    call reloads it fresh, and stamp the idle-unload clock on success."""
    try:
        out = _pkg._local_generate(model, tokenizer, prompt, max_tokens)
    except Exception as e:  # noqa: BLE001
        _pkg._LOCAL, _pkg._LOCAL_SIG = None, None  # drop a wedged model; reload next time
        raise RuntimeError(f"local MLX model failed: {e}") from e
    _pkg._LOCAL_LAST = time.monotonic()
    return out


def run_local_llm(prompt: str, cfg: dict) -> str:
    """Run one transform on the warm on-device MLX model. Strict-local: no
    network, no key, nothing leaves the machine. Raises RuntimeError on failure
    so process_text falls back to the raw transcript (nothing lost)."""
    model_id = _local_model_id(cfg)
    max_tokens = int(cfg["llm"].get("local_max_tokens", 1024))
    idle = int(cfg["llm"].get("local_idle_secs", 600))
    with _LOCAL_LOCK:  # single-flight: one generation at a time
        model, tokenizer = _ensure_local_model(model_id, idle)
        out = _generate_local(model, tokenizer, prompt, max_tokens)
    return _strip_wrapping(out or "")
