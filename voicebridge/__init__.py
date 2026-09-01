"""
Alfred — local speech-to-text + LLM cleanup for macOS (Apple Silicon).

This is the *engine*. It is normally driven by the Hammerspoon front-end
(voicebridge.lua), but every command also works standalone from a terminal via
the `voicebridge.py` entry-point shim.

Pipeline:
    audio (or typed text)
      -> STT  (mlx-whisper, on-device, Hebrew-capable)
      -> [ translate -> rewrite-to-intent -> optimize ]  (optional, toggleable)
           via your installed `claude` or `codex` CLI, on its existing login
      -> clipboard  (or saved to a file if too large; optional auto-paste)

Everything stays on the Mac except the optional LLM call. No API key is ever
required, read, or stored: the LLM step shells out to the `claude` / `codex`
binary you already signed in to, with API-key env vars stripped so it uses the
subscription login rather than silently billing an API key.

Commands:
    voicebridge.py process <audio.wav>     transcribe + pipeline + deliver
    voicebridge.py stream-start <wav>       begin transcribing a growing WAV (daemon)
    voicebridge.py stream-finish <wav>      finish a streamed recording + deliver
    voicebridge.py text ["..."|-]          run pipeline on text (Type mode/tests)
    voicebridge.py history [--copy N]       list / re-copy recent results
    voicebridge.py modes                    list rewrite modes as JSON (front-end)
    voicebridge.py serve [--port N]         warm background engine (localhost HTTP)
    voicebridge.py contract                 print the IPC contract as JSON
    voicebridge.py doctor                   check the environment

Run `voicebridge.py --help` or `voicebridge.py <cmd> --help` for flags.
(set-intent, set-model, set-processing and settings exist too; they back the
front-end's menus.)

Package layout (the engine's own internal module map, distinct from the
cross-language module boundaries in .quality/quality-dependencies.json):
    contract   -- IPC contract + config schema/loading
    audio      -- WAV/PCM helpers, batch + streaming STT
    llm        -- keyless CLI backends + the on-device MLX local backend
    pipeline   -- translate/rewrite/optimize + delivery sinks
    runtime    -- history, progress, capture commands (process/stream/text)
    commands   -- config-writing + introspection commands
    daemon     -- the warm HTTP daemon + CLI argument parsing / entry point

Submodules call each other through this package's own namespace (`import
voicebridge as _pkg`, then `_pkg.name(...)`), never `from .x import y`. Several
test files monkeypatch dozens of `voicebridge.<name>` attributes (`vb.deliver =
fake`, etc.) to stub I/O; a plain cross-module import would bind its own frozen
copy of the name at import time and silently stop seeing those patches. Do not
"simplify" a submodule's cross-module calls to a direct import without checking
tests/*.py for a matching `vb.<name> =` first.
"""

from __future__ import annotations

# Re-exported so `vb.json` / `vb._dt` keep working: a couple of tests reach for
# the stdlib module through the package namespace the way the flat file always
# implicitly allowed (any name imported at module scope was a `vb.` attribute).
import datetime as _dt
import json

from .audio import (
    _STREAM_ABANDON_POLLS,
    _STREAM_DECODE_OPTS,
    _STREAM_SILENCE_RMS,
    _STREAM_TTL,
    _STREAMS,
    _STREAMS_LOCK,
    StreamSession,
    _atomic_write_json,
    _atomic_write_text,
    _format_segments,
    _format_ts,
    _load_audio_16k,
    _pcm_sample_count,
    _read_pcm_f32,
    _rms,
    _secure_dir,
    _silence_cut,
    _stream_path,
    _wav_data_offset,
    transcribe,
    transcribe_samples,
)
from .commands import (
    _CLAUDE_MODELS,
    _CODEX_MODELS,
    _config_target,
    _set_config_kv,
    _toml_str,
    cmd_contract,
    cmd_doctor,
    cmd_modes,
    cmd_set_intent,
    cmd_set_model,
    cmd_set_processing,
    cmd_set_stt,
    cmd_settings,
)
from .contract import (
    CONFIG_SEARCH,
    CONTRACT,
    DAEMON_PORT,
    DEFAULTS,
    RESULT,
    STATUS,
    _daemon_info_path,
    _deep_merge,
    contract_paths,
    load_config,
    resolved_contract,
)
from .daemon import (
    _bool_flag,
    _daemon_identity,
    _loopback_host,
    _probe_daemon,
    _ThreadStream,
    _write_daemon_info,
    add_common,
    build_parser,
    cmd_serve,
    main,
)
from .llm import (
    _CLAUDE_KEY_VARS,
    _CODEX_KEY_VARS,
    _EXTRA_BIN_DIRS,
    _LOCAL_LOCK,
    _WARM,
    _WARM_LOCK,
    _WARM_SIG,
    WarmClaude,
    _claude_warm_cmd,
    _clean_env,
    _get_warm,
    _local_generate,
    _local_load,
    _run,
    _should_prewarm_claude,
    _strip_wrapping,
    candidate_backends,
    detect_backends,
    find_tool,
    run_llm,
    run_llm_clean,
    run_llm_fallback,
    run_local_llm,
)
from .pipeline import (
    _CHARS_PER_TOKEN,
    _OPTIMIZE,
    _PROMPT_OPTIMIZER,
    _REWRITE,
    _SENT_SPLIT,
    _SINK,
    _TAIL,
    _TRANSLATE,
    BUILTIN_MODES,
    MacosSink,
    Sink,
    _chunk_char_budget,
    _macos_tool,
    _process_chunk,
    _save_path,
    _split_for_processing,
    _whisper_can_translate,
    active_stages,
    auto_paste,
    build_combined_prompt,
    copy_clipboard,
    deliver,
    mode_catalog,
    mode_prompt,
    process_text,
    refine_text,
    rewrite_instruction,
    save_to_file,
    single_stage_prompt,
    whisper_translate_active,
)
from .runtime import (
    _HISTORY_LOCK,
    _apply_overrides,
    _deliver_and_report,
    _finish_capture,
    _maybe_remove_audio,
    _now_ms,
    _Progress,
    _progress_path,
    _stage_label,
    _status_parts,
    cmd_history,
    cmd_process,
    cmd_stream_finish,
    cmd_stream_start,
    cmd_text,
    history_append,
    history_path,
    print_result,
    print_status,
)

__all__ = [
    "STATUS",
    "RESULT",
    "DEFAULTS",
    "CONFIG_SEARCH",
    "DAEMON_PORT",
    "CONTRACT",
    "contract_paths",
    "_daemon_info_path",
    "resolved_contract",
    "_deep_merge",
    "load_config",
    "_load_audio_16k",
    "_format_ts",
    "_format_segments",
    "transcribe_samples",
    "_STREAM_SILENCE_RMS",
    "_STREAM_DECODE_OPTS",
    "_rms",
    "transcribe",
    "_secure_dir",
    "_atomic_write_text",
    "_atomic_write_json",
    "_wav_data_offset",
    "_pcm_sample_count",
    "_read_pcm_f32",
    "_silence_cut",
    "_stream_path",
    "StreamSession",
    "_STREAM_ABANDON_POLLS",
    "_STREAM_TTL",
    "_STREAMS",
    "_STREAMS_LOCK",
    "_EXTRA_BIN_DIRS",
    "_CLAUDE_KEY_VARS",
    "_CODEX_KEY_VARS",
    "find_tool",
    "detect_backends",
    "_should_prewarm_claude",
    "candidate_backends",
    "run_llm_fallback",
    "_clean_env",
    "_run",
    "_claude_warm_cmd",
    "WarmClaude",
    "_WARM",
    "_WARM_SIG",
    "_WARM_LOCK",
    "_get_warm",
    "run_llm",
    "run_llm_clean",
    "_strip_wrapping",
    "_LOCAL_LOCK",
    "_local_load",
    "_local_generate",
    "run_local_llm",
    "_PROMPT_OPTIMIZER",
    "BUILTIN_MODES",
    "mode_catalog",
    "mode_prompt",
    "_TRANSLATE",
    "_REWRITE",
    "_OPTIMIZE",
    "_TAIL",
    "_whisper_can_translate",
    "whisper_translate_active",
    "active_stages",
    "rewrite_instruction",
    "build_combined_prompt",
    "single_stage_prompt",
    "_CHARS_PER_TOKEN",
    "_SENT_SPLIT",
    "_chunk_char_budget",
    "_split_for_processing",
    "_process_chunk",
    "process_text",
    "refine_text",
    "_macos_tool",
    "Sink",
    "MacosSink",
    "_SINK",
    "_save_path",
    "copy_clipboard",
    "auto_paste",
    "save_to_file",
    "deliver",
    "history_path",
    "_HISTORY_LOCK",
    "history_append",
    "print_status",
    "print_result",
    "_status_parts",
    "_now_ms",
    "_progress_path",
    "_Progress",
    "_stage_label",
    "_apply_overrides",
    "_deliver_and_report",
    "_finish_capture",
    "_maybe_remove_audio",
    "cmd_process",
    "cmd_stream_start",
    "cmd_stream_finish",
    "cmd_text",
    "cmd_history",
    "cmd_modes",
    "_config_target",
    "_toml_str",
    "cmd_set_intent",
    "_set_config_kv",
    "cmd_set_model",
    "cmd_set_processing",
    "cmd_set_stt",
    "_CLAUDE_MODELS",
    "_CODEX_MODELS",
    "cmd_settings",
    "cmd_contract",
    "cmd_doctor",
    "_bool_flag",
    "add_common",
    "_ThreadStream",
    "_daemon_identity",
    "_write_daemon_info",
    "_probe_daemon",
    "_loopback_host",
    "cmd_serve",
    "build_parser",
    "main",
    "json",
    "_dt",
    "_ACTIVE_STREAM",
    "_DAEMON_MODE",
    "_LOCAL",
    "_LOCAL_SIG",
    "_LOCAL_LAST",
    "_STREAM_TARGET",
    "_STREAM_MAX",
    "_STREAM_FRAME",
    "_STREAM_PREVIEW_SECS",
    "_STREAM_PREVIEW_MIN",
]

# ---- Cross-module mutable state -------------------------------------------
# These are read AND reassigned from more than one submodule (and monkeypatched
# directly by tests as `vb.<name> = ...`), so they live here -- the one place
# every submodule's `_pkg.<name>` reference and every `vb.<name>` test patch
# resolve to the same storage. A submodule must never redeclare one of these as
# its own module-level global (that would silently create a second, unsynced
# copy) or reassign it with a bare `global <name>` (same bug, one module over).

# The session that owns stream.json (StreamSession sets/reads this while
# streaming; cmd_stream_start/cmd_stream_finish in runtime.py hand it off).
_ACTIVE_STREAM: StreamSession | None = None

# Set True by `serve` so run_llm knows it may keep a warm claude process alive.
# One-shot CLI runs leave this False (a warm process would never be reused).
_DAEMON_MODE = False

# The on-device MLX model cache: (model, tokenizer) once loaded, the model id
# currently loaded, and the monotonic time of the last generation (idle-unload).
_LOCAL = None
_LOCAL_SIG: str | None = None
_LOCAL_LAST = 0.0

# Streaming STT chunk geometry (see runtime/audio.py's streaming section for
# the tuning rationale) -- tests override these to make streaming fast/deterministic.
_STREAM_TARGET = 8 * 16000
_STREAM_MAX = 11 * 16000
_STREAM_FRAME = 800
_STREAM_PREVIEW_SECS = 1.5
_STREAM_PREVIEW_MIN = int(1.2 * 16000)
