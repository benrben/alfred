"""Contract, config schema, and config-file loading.

The single source of truth the two front-ends read via `voicebridge.py contract`
/ `GET /contract`, plus the merged-over-defaults config loader every command
shares.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import voicebridge as _pkg

STATUS = "VB_STATUS"
# Sentinel for the machine-readable result text, emitted (JSON-encoded, so it
# survives newlines) on the line BEFORE the final VB_STATUS. Lets a front-end
# read the exact delivered text without depending on the clipboard.
RESULT = "VB_RESULT"

# Force UTF-8 stdio even when launched by a GUI with a non-UTF-8 locale (macOS
# can default to mac-roman, which mangles curly quotes / em dashes / Hebrew).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # pragma: no cover
        pass

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

DEFAULTS: dict = {
    "stt": {
        # Any mlx-community Whisper repo. large-v3-turbo = fast + great Hebrew
        # transcription. For Whisper-native translate, prefer whisper-large-v3.
        "model": "mlx-community/whisper-large-v3-turbo",
        "language": "auto",  # "auto", "he", "en", ...
        "initial_prompt": "",  # vocab biasing: names, jargon, brands
    },
    "processing": {
        "translate": False,  # produce English output
        "rewrite": False,  # clean up + shape to intent
        "optimize": False,  # tighten & clarify
        "mode": "raw",  # email|message|commit|prompt|notes|raw
        "translate_via": "llm",  # "llm" (better for Hebrew) or "whisper"
        "combine_stages": True,  # one LLM call (fast) vs separate calls
    },
    "llm": {
        # local = strict on-device MLX model ($0, offline, private) and the
        # default; auto|claude|codex shell out to the user's signed-in CLI
        # (keyless) as an opt-in quality boost. See adr-local-intent-default.
        "backend": "local",  # local|auto|claude|codex
        "local_model": "mlx-community/Qwen2.5-3B-Instruct-4bit",
        "local_max_tokens": 4096,  # cap on generated tokens per transform. A
        # long transcript is auto-split into chunks
        # that each stay under this (see process_text),
        # so raising it just means fewer, larger calls.
        "local_idle_secs": 600,  # free the in-memory model after N idle secs
        "claude_model": "sonnet",  # alias tracks latest; safer than dates
        "codex_model": "",  # empty = codex default
        # Reasoning ("thinking") effort — kept LOW for fast text transforms (we
        # don't need deep reasoning to clean up dictation). "" = leave the CLI
        # default. claude: low|medium|high|xhigh|max. codex: low|medium|high
        # ("minimal" is rejected when codex's web_search/image_gen tools are on).
        "claude_effort": "low",
        "codex_reasoning_effort": "low",
        "claude_extra_args": [],
        "codex_extra_args": [],
        "timeout": 120,  # seconds per LLM call
        # Speed: run claude/codex in an isolated, minimal mode for the text
        # transform — skip the user's MCP servers, plugins, hooks, CLAUDE.md and
        # settings. These are pure startup overhead here (and a correctness risk:
        # a stray CLAUDE.md/hook could alter the rewrite). Big win when the user
        # has many MCP servers. Set false to use the full environment.
        "fast": True,
        # Speed: in the daemon, keep one warm `claude` process alive and stream
        # each transform to it, so we pay the ~3s CLI startup once instead of per
        # capture (warm turns ~2s vs ~5s cold). Keyless (still the CLI login).
        # Each transform is self-contained, so turns stay independent. Only used
        # by `serve`; one-shot CLI runs always spawn fresh.
        "warm": True,
        "warm_max_turns": 25,  # recycle the process after N turns (bounds
        # context growth / memory)
        "warm_idle_secs": 600,  # recycle after this many idle seconds
    },
    "output": {
        "mode": "copy",  # "copy" or "paste" (paste also copies)
        "size_threshold": 2000,  # chars; above -> save to file instead
        "save_dir": "~/Documents/VoiceBridge",
        "save_format": "md",  # md|txt
        "keep_audio": False,  # delete the recording after transcription
        # In paste mode, snapshot the user's prior clipboard and restore it
        # after pasting, so dictation doesn't clobber whatever they had
        # copied. Off by default: older front-ends still read the delivered
        # text from the clipboard as a fallback (not VB_RESULT); enable once
        # yours reads VB_RESULT.
        "restore_clipboard": False,
    },
    "history": {
        "enabled": True,
        "dir": "~/.voicebridge/history",
        "max_items": 50,
    },
}

CONFIG_SEARCH = [
    Path.home() / ".config" / "voicebridge" / "config.toml",
    Path(__file__).resolve().parent / "config.toml",
]

# Default port for the warm `serve` daemon (also the argparse default below).
DAEMON_PORT = 8763


# ----------------------------------------------------------------------------
# CONTRACT — the single source of IPC truth
# ----------------------------------------------------------------------------
# Everything the two front-ends (Hammerspoon / Raycast) need to talk to this
# engine: the daemon's HTTP shape, the VB_STATUS status-line grammar, the state
# files it writes (their paths + JSON schema), and where config is searched.
# The writers below DERIVE their paths/format from this dict, so no path string
# or status sentinel is duplicated. `contract_paths()` expands the "~/..."
# templates (honouring [history].dir). Exposed via `voicebridge.py contract`
# and GET /contract so a front-end can read it instead of hard-coding.
# schema_version: bump ONLY on a backward-INCOMPATIBLE change; new fields are
# added additively within a version (front-ends read what they know and ignore
# the rest). Both front-ends compare their built-in fallback version against the
# engine's and warn the user when the MAJOR version differs (engine/front-end out
# of sync). See docs/COMPAT or the "compatibility" note in the README.
CONTRACT: dict = {
    "schema_version": 1,
    "daemon": {
        "host": "127.0.0.1",
        "port": DAEMON_PORT,
        "url": "http://127.0.0.1:{port}/",
        "request": {"method": "POST", "path": "/", "body": {"argv": ["<str>"]}},
        # `err` carries the request's captured stderr (error detail) so a
        # front-end can show WHAT failed. Added additively; older daemons omit it
        # and front-ends treat a missing err as "".
        "response": {"code": "int", "out": "str", "err": "str"},
        "health": {"method": "GET", "path": "/"},
        # GET / responds with this identity so a front-end (and `serve` itself on
        # a busy port) can tell an Alfred daemon from a foreign server.
        "identity": {"app": "alfred", "schema_version": "int", "pid": "int", "ok": "bool"},
        "contract": {"method": "GET", "path": "/contract"},
        # Discovery/identity file written on startup (mode 0600).
        "info_file": "~/.voicebridge/daemon.json",
    },
    "status_line": {
        "sentinel": STATUS,
        "sep": "\t",
        "kinds": {
            "copied": [],
            "saved": ["path"],
            "empty": [],
            "streaming": [],
            "error": ["subtype"],
        },
        "error_subtypes": [
            "audio_not_found",
            "stt_failed",
            "llm_failed",
            "deliver_failed",
            "runtime",
        ],
        "llm_failed_suffix": "llm_failed",
        # A 'copied' status line may carry a trailing 'paste_failed' flag when
        # auto-paste was requested but the keystroke could not be delivered
        # (usually a missing Accessibility grant for the app that owns the engine).
        "paste_failed_suffix": "paste_failed",
        # Optional machine-readable result line emitted just BEFORE the final
        # VB_STATUS: "VB_RESULT<sep><json-encoded text>". Front-ends prefer it
        # over reading the clipboard; absent on older engines / the --stdout path.
        "result_sentinel": RESULT,
    },
    # The recording format the streaming STT reader depends on (int16 mono
    # 16 kHz WAV). Front-ends build their `sox` recorder command from this so the
    # invariant lives in one place instead of being copy-pasted per front-end.
    "audio": {
        "rate": 16000,
        "channels": 1,
        "bits": 16,
        "format": "wav",
        "sox_args": ["-d", "-S", "-r", "16000", "-c", "1", "-b", "16"],
    },
    "files": {
        "progress": {
            "path": "~/.voicebridge/progress.json",
            "schema": {
                "phase": "str",
                "label": "str",
                "ts": "int_epoch_ms",
                "start": "int_epoch_ms",
                "steps": [{"label": "str", "ms": "int"}],
            },
            "phases": [
                "starting",
                "transcribing",
                "processing",
                "delivering",
                "done",
                "error",
                "empty",
            ],
        },
        "stream": {
            "path": "~/.voicebridge/stream.json",
            "schema": {
                "transcript": "str",
                "recording": "bool",
                "done": "bool",
                "ts": "int_epoch_ms",
                "path": "str",
            },
        },
        "history": {
            "path": str(Path(DEFAULTS["history"]["dir"]) / "history.jsonl"),
            "format": "jsonl",
            "dir_config": "[history].dir",
            "schema": {"ts": "str_iso_seconds", "source": "str", "chars": "int", "text": "str"},
        },
    },
    "config_search": [
        "~/.config/voicebridge/config.toml",
        "<engine_dir>/config.toml",
    ],
}


def contract_paths(cfg: dict | None = None) -> dict:
    """Expand the CONTRACT's "~/..."-style file path templates to absolute Paths.

    Single source of truth for the engine's state-file locations. The history
    path honours the [history].dir override when a config is given; progress and
    stream are fixed under ~/.voicebridge."""
    files = CONTRACT["files"]
    hist_dir = ((cfg or {}).get("history", {}) or {}).get("dir") or DEFAULTS["history"]["dir"]
    return {
        "progress": Path(files["progress"]["path"]).expanduser(),
        "stream": Path(files["stream"]["path"]).expanduser(),
        "history": Path(hist_dir).expanduser() / "history.jsonl",
    }


def _daemon_info_path() -> Path:
    return Path(CONTRACT["daemon"]["info_file"]).expanduser()


def resolved_contract(cfg: dict | None = None) -> dict:
    """The CONTRACT with a `resolved` block of ABSOLUTE, config-aware paths.

    The static CONTRACT carries "~/..." templates and the DEFAULT history path;
    front-ends that honour [history].dir need the real resolved locations. This
    merges those in without mutating CONTRACT, and is what `contract` / GET
    /contract actually emit."""
    paths = contract_paths(cfg)
    resolved = {
        "progress": str(paths["progress"]),
        "stream": str(paths["stream"]),
        "history": str(paths["history"]),
        "daemon_info": str(_pkg._daemon_info_path()),
        "config": str(cfg.get("_loaded_from")) if cfg and cfg.get("_loaded_from") else "",
    }
    return {**CONTRACT, "resolved": resolved}


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_one_config_path(p: Path) -> dict | None:
    """Load+merge config.toml at `p` over DEFAULTS, or None if `p` isn't a file
    (caller tries the next candidate). A read failure still returns a dict
    (built-in defaults, optionally with `_config_error`) rather than raising,
    so the caller's search always stops at the first *existing* path."""
    if not p or not p.expanduser().is_file():
        return None
    try:
        import tomllib
    except ModuleNotFoundError:  # Python < 3.11
        sys.stderr.write(
            "warning: Python 3.11+ needed to read config.toml; using built-in defaults.\n"
        )
        return json.loads(json.dumps(DEFAULTS))
    try:
        with open(p.expanduser(), "rb") as fh:
            cfg = _deep_merge(json.loads(json.dumps(DEFAULTS)), tomllib.load(fh))
    except tomllib.TOMLDecodeError as e:
        # A hand-edit typo must not brick every command (dictation, and even
        # `doctor`). Warn, remember the error for doctor to surface, and fall
        # back to built-in defaults so the tool keeps working.
        sys.stderr.write(
            f"warning: config.toml is invalid ({p.expanduser()}): {e}; "
            "using built-in defaults.\n"
        )
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg["_config_error"] = f"{p.expanduser()}: {e}"
        return cfg
    cfg["_loaded_from"] = str(p.expanduser())
    return cfg


def load_config(path: str | None) -> dict:
    candidates = [Path(path)] if path else CONFIG_SEARCH
    for p in candidates:
        cfg = _load_one_config_path(p)
        if cfg is not None:
            return cfg
    return json.loads(json.dumps(DEFAULTS))
