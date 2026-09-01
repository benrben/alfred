"""Config-writing and introspection commands (modes, set-intent, set-model,
set-processing, set-stt, settings, contract, doctor).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import voicebridge as _pkg


def cmd_modes(args) -> int:
    """Emit the available rewrite modes (built-in + config [intent]) as JSON,
    so the front-end can populate its picker. One JSON array on stdout."""
    cfg = _pkg.load_config(args.config)
    default_mode = cfg["processing"].get("mode")
    catalog = [
        {
            "key": m["key"],
            "label": m.get("label") or m["key"],
            "description": m.get("description", ""),
            "prompt": m.get("prompt", ""),
            # Ready-made argv that realizes this mode, so front-ends consume
            # the flag grammar instead of each re-deriving it (which has
            # drifted before). "raw"/no-LLM stays a front-end pseudo-format.
            "flags": ["--mode", m["key"], "--rewrite"],
            "default": m["key"] == default_mode,
        }
        for m in _pkg.mode_catalog(cfg)
    ]
    print(json.dumps(catalog))
    return 0


def _config_target_allowed(target: Path) -> bool:
    """True unless running inside the daemon with a --config path outside the
    known search locations (an arbitrary-path write primitive otherwise: set-
    intent/-model/-processing mkdir+write to it). Front-ends never pass
    --config over the daemon, so this only ever restricts that path."""
    if not _pkg._DAEMON_MODE:
        return True
    return target.resolve() in {p.expanduser().resolve() for p in _pkg.CONFIG_SEARCH}


def _config_target(args) -> Path:
    if getattr(args, "config", None):
        target = Path(args.config).expanduser()
        if _config_target_allowed(target):
            return target
        sys.stderr.write("warning: ignoring out-of-allowlist --config over the daemon.\n")
    for p in _pkg.CONFIG_SEARCH:
        if p.expanduser().is_file():
            return p.expanduser()
    return Path.home() / ".config" / "voicebridge" / "config.toml"


def _toml_str(s: str) -> str:
    s = (
        (s or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
        .replace("\r", "")
    )
    return '"' + s + '"'


def _backup_pristine(path: Path, text: str) -> None:
    """Write a .bak holding the PRISTINE pre-edit `text`, best-effort (a
    restore must recover the old content, so this must run BEFORE the caller
    mutates anything; a failed backup must not block the edit itself)."""
    if not path.is_file():
        return
    try:
        path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _validated_intent_key(args) -> str | None:
    """The stripped intent key, or None (with an error already printed to
    stderr) if it isn't safe to use as a TOML table name."""
    key = (args.key or "").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        sys.stderr.write("error: intent key must be letters/numbers/-/_.\n")
        return None
    return key


def _set_intent_sub_table(doc, key: str, args) -> None:
    """Build/replace the [intent.<key>] sub-table `doc` will be dumped with,
    keeping any pre-existing extra keys (e.g. replace) when the value already
    is a table."""
    import tomlkit

    if "intent" not in doc:
        doc["intent"] = tomlkit.table(is_super_table=True)
    intent = doc["intent"]
    sub = intent.get(key)
    if not isinstance(sub, tomlkit.items.Table):
        sub = tomlkit.table()
    sub["prompt"] = args.prompt or ""
    if args.label:
        sub["label"] = args.label
    if args.description:
        sub["description"] = args.description
    intent[key] = sub


def cmd_set_intent(args) -> int:
    """Write/override [intent.<key>] in config.toml via tomlkit.

    Uses tomlkit (like set-model/set-processing) instead of regex surgery, so a
    prompt whose text contains a line starting with '[' can't truncate the file,
    comments/formatting are preserved, and the .bak is the PRISTINE pre-edit file
    (the old code backed up the already-mutated text, so a restore lost exactly
    the prompt it was meant to protect). Existing extra keys (e.g. replace) are
    kept."""
    import tomlkit

    key = _validated_intent_key(args)
    if key is None:
        return 2
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    # Back up the PRISTINE file BEFORE mutating, so a restore recovers the old prompt.
    _backup_pristine(path, text)
    doc = tomlkit.parse(text)
    _set_intent_sub_table(doc, key, args)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    ok = any(m["key"] == key for m in _pkg.mode_catalog(_pkg.load_config(str(path))))
    _pkg.print_status("saved" if ok else "error")
    return 0 if ok else 1


def _set_config_kv(path: Path, section: str, key: str, value_toml: str) -> None:
    """Set `key = value_toml` inside [section] in a TOML file, in place (keeps a
    .bak). Creates the section/key if missing; replaces the value if present.

    value_toml is a TOML-encoded value (e.g. '"email"', 'true'); we round-trip
    the document with tomlkit so existing comments and formatting are preserved."""
    import tomlkit

    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    doc = tomlkit.parse(text)
    # Parse the encoded value back to a typed tomlkit value (string, bool, ...).
    value = tomlkit.parse(f"_ = {value_toml}")["_"]
    table = doc.get(section)
    if table is None:
        table = tomlkit.table()
        doc[section] = table
    table[key] = value
    _backup_pristine(path, text)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def cmd_set_model(args) -> int:
    """Persist the model for a backend: [llm] claude_model / codex_model."""
    key = "claude_model" if args.backend == "claude" else "codex_model"
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_config_kv(path, "llm", key, _toml_str(args.model or ""))
    _pkg.print_status("saved")
    return 0


def cmd_set_processing(args) -> int:
    """Persist the [processing] defaults a front-end can change: the default
    mode/intent and the rewrite/translate/optimize stage toggles. Only the flags
    actually passed are written, so callers can set one thing at a time."""
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.mode is not None:
        _set_config_kv(path, "processing", "mode", _toml_str(args.mode))
    for stage in ("rewrite", "translate", "optimize"):
        val = getattr(args, stage)
        if val is not None:
            _set_config_kv(path, "processing", stage, "true" if val else "false")
    _pkg.print_status("saved")
    return 0


def cmd_set_stt(args) -> int:
    """Persist [stt] settings a front-end can change — currently the vocabulary /
    initial_prompt biasing (names, jargon, brands) and the STT language. The
    highest-churn STT knob (fixing a persistently misheard name) previously had
    no front-end or CLI write path; this gives it one, mirroring set-model."""
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(args, "initial_prompt", None) is not None:
        _set_config_kv(path, "stt", "initial_prompt", _toml_str(args.initial_prompt))
    if getattr(args, "language", None):
        _set_config_kv(path, "stt", "language", _toml_str(args.language))
    _pkg.print_status("saved")
    return 0


# Selectable model presets per backend. Claude aliases track the latest model.
# Extend either list from config.toml:  [llm] claude_models / codex_models = [...]
_CLAUDE_MODELS = ["opus", "sonnet", "haiku"]
_CODEX_MODELS: list[str] = []


def cmd_settings(args) -> int:
    """Current backend/model settings, the selectable model lists, AND the
    [processing] defaults, as JSON for the front-end's dropdowns/badges."""
    cfg = _pkg.load_config(args.config)
    llm = cfg["llm"]
    proc = cfg["processing"]

    def models(defaults, key):
        out = list(defaults)
        for m in llm.get(key) or []:
            if str(m) not in out:
                out.append(str(m))
        return out

    print(
        json.dumps(
            {
                "backend": llm["backend"],
                "claude_model": llm.get("claude_model", ""),
                "codex_model": llm.get("codex_model", ""),
                "claude_models": models(_CLAUDE_MODELS, "claude_models"),
                "codex_models": models(_CODEX_MODELS, "codex_models"),
                "processing": {
                    "mode": proc.get("mode", "raw"),
                    "rewrite": bool(proc.get("rewrite")),
                    "translate": bool(proc.get("translate")),
                    "optimize": bool(proc.get("optimize")),
                    "translate_via": proc.get("translate_via", "llm"),
                },
                "stt": {
                    "language": cfg["stt"].get("language", "auto"),
                    "initial_prompt": cfg["stt"].get("initial_prompt", ""),
                },
            }
        )
    )
    return 0


def cmd_contract(args) -> int:
    """Print the IPC CONTRACT (the single source of truth the front-ends read):
    the daemon's HTTP shape, the VB_STATUS grammar, the state files, and a
    `resolved` block of absolute, [history].dir-aware paths."""
    cfg = _pkg.load_config(getattr(args, "config", None))
    print(json.dumps(_pkg.resolved_contract(cfg), indent=2))
    return 0


# Status-line markers `cmd_doctor`'s section-check helpers print with (each
# carries its own trailing space so callers just concatenate: f"{_DOCTOR_OK}...").
_DOCTOR_OK = "OK "
_DOCTOR_BAD = "XX "
_DOCTOR_WARN = "-- "


def _installed(mod: str) -> bool:
    """Whether `mod` is installed, checked via find_spec (WITHOUT importing)
    so doctor doesn't pay the multi-second MLX framework import just to say
    "present"."""
    import importlib.util as _ilu

    try:
        return _ilu.find_spec(mod) is not None
    except Exception:  # noqa: BLE001
        return False


def _doctor_python_platform() -> None:
    import platform

    pyv = sys.version_info
    print(
        f"{_DOCTOR_OK if pyv >= (3, 9) else _DOCTOR_BAD}"
        f"Python {pyv.major}.{pyv.minor}.{pyv.micro}"
    )
    mach = platform.machine()
    print(
        f"{_DOCTOR_OK if mach == 'arm64' else _DOCTOR_WARN}Architecture: {mach}"
        + ("" if mach == "arm64" else "  (mlx-whisper needs Apple Silicon)")
    )


def _doctor_python_deps() -> None:
    for mod, hint in [
        ("mlx_whisper", "pip install mlx-whisper"),
        ("soundfile", "pip install soundfile"),
        ("numpy", "pip install numpy"),
    ]:
        if _installed(mod):
            print(f"{_DOCTOR_OK}python module: {mod}")
        else:
            print(f"{_DOCTOR_BAD}python module: {mod}   -> {hint}")


def _doctor_system_tools() -> None:
    for tool, hint in [
        ("sox", "brew install sox  (needed by the recorder)"),
        ("pbcopy", "(ships with macOS)"),
    ]:
        path = shutil.which(tool)
        print(
            f"{_DOCTOR_OK if path else _DOCTOR_BAD}command: {tool}"
            + (f"  ({path})" if path else f"   -> {hint}")
        )


def _doctor_llm_backends() -> None:
    have = _pkg.detect_backends()
    for name, drop in [("claude", "ANTHROPIC_API_KEY"), ("codex", "OPENAI_API_KEY")]:
        if have[name]:
            keyset = drop in os.environ
            note = (
                (
                    f"  WARNING: {drop} is set; it will be stripped per call so "
                    "the subscription login is used"
                )
                if keyset
                else ""
            )
            print(f"{_DOCTOR_OK}LLM backend: {name}  ({have[name]}){note}")
        else:
            print(f"{_DOCTOR_WARN}LLM backend: {name} not found")
    if not any(have.values()):
        print(
            "    (LLM stages disabled until claude or codex is installed; "
            "raw transcription still works)"
        )


def _doctor_local_mlx_module() -> None:
    # Local on-device backend (MLX-LM) — strict-local, no login, no network.
    mlx_ok = _installed("mlx_lm")
    print(
        f"{_DOCTOR_OK if mlx_ok else _DOCTOR_WARN}python module: mlx_lm"
        + ("" if mlx_ok else "   -> pip install mlx-lm  (for backend = local)")
    )


def _doctor_local_model_cache(cfg) -> None:
    local_model = cfg["llm"].get("local_model", "")
    if not local_model:
        return
    cache = (
        Path.home()
        / ".cache"
        / "huggingface"
        / "hub"
        / ("models--" + local_model.replace("/", "--"))
    )
    print(
        f"{_DOCTOR_OK if cache.exists() else _DOCTOR_WARN}local model: {local_model}"
        + ("  (cached)" if cache.exists() else "  (downloads on first 'backend = local' use)")
    )


def _doctor_config_summary(cfg) -> None:
    print("-" * 40)
    if cfg.get("_config_error"):
        print(f"{_DOCTOR_BAD}config PARSE ERROR: {cfg['_config_error']}")
        print("    (using built-in defaults until fixed — check the file/line above)")
    print(f"config: {cfg.get('_loaded_from', '(built-in defaults)')}")
    print(f"STT model: {cfg['stt']['model']}   language: {cfg['stt']['language']}")
    print(
        f"stages: translate={cfg['processing']['translate']} "
        f"rewrite={cfg['processing']['rewrite']} "
        f"optimize={cfg['processing']['optimize']} "
        f"mode={cfg['processing']['mode']} "
        f"via={cfg['processing']['translate_via']}"
    )
    print(
        f"backend: {cfg['llm']['backend']}   output: {cfg['output']['mode']}   "
        f"save_dir: {cfg['output']['save_dir']}"
    )
    sd = Path(cfg["output"]["save_dir"]).expanduser()
    try:
        sd.mkdir(parents=True, exist_ok=True)
        print(f"{_DOCTOR_OK}save_dir writable: {sd}")
    except Exception as e:  # noqa: BLE001
        print(f"{_DOCTOR_BAD}save_dir not writable: {sd} ({e})")


def _doctor_macos_permissions() -> None:
    # A STATIC note, not a live probe. Actively querying Accessibility
    # (osascript "System Events" UI-elements-enabled) hangs for seconds inside
    # a headless daemon with no Automation grant, and doctor is called on every
    # Engine Status open. The runtime signal is better anyway: deliver()
    # reports a ('copied','paste_failed') status when a real paste can't be
    # delivered, so the front-end says so in context.
    import platform

    if platform.system() != "Darwin":
        return
    print("-" * 40)
    print(
        f"{_DOCTOR_WARN}auto-paste needs Accessibility granted to the app that runs "
        "Alfred (Raycast) in System Settings ▸ Privacy ▸ "
        "Accessibility; the mic needs it per-app too. (Copy mode needs "
        "neither.)"
    )


def _doctor_daemon_status() -> None:
    # Identity + owner pid, so front-ends/users can see which process owns the
    # warm engine — auto-paste attribution follows that process.
    who = _pkg._probe_daemon(_pkg.CONTRACT["daemon"]["port"])
    if who and who.get("app") == "alfred":
        print(
            f"{_DOCTOR_OK}warm daemon: running "
            f"(pid {who.get('pid')}, schema v{who.get('schema_version')})"
        )
    elif who:
        print(f"{_DOCTOR_BAD}port {_pkg.CONTRACT['daemon']['port']} held by a NON-Alfred server")
    else:
        print(f"{_DOCTOR_WARN}warm daemon: not running (starts on first capture)")


def cmd_doctor(args) -> int:
    cfg = _pkg.load_config(args.config)
    print("Alfred doctor\n" + "=" * 40)
    _doctor_python_platform()
    _doctor_python_deps()
    _doctor_system_tools()
    _doctor_llm_backends()
    _doctor_local_mlx_module()
    _doctor_local_model_cache(cfg)
    _doctor_config_summary(cfg)
    _doctor_macos_permissions()
    _doctor_daemon_status()
    return 0
