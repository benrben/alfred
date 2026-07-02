#!/usr/bin/env python3
"""
Alfred — local speech-to-text + LLM cleanup for macOS (Apple Silicon).

This is the *engine*. It is normally driven by the Hammerspoon front-end
(voicebridge.lua), but every command also works standalone from a terminal.

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
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# Sentinel printed on stdout for the front-end to parse. Always the LAST line.
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
        "language": "auto",          # "auto", "he", "en", ...
        "initial_prompt": "",        # vocab biasing: names, jargon, brands
    },
    "processing": {
        "translate": False,          # produce English output
        "rewrite": False,            # clean up + shape to intent
        "optimize": False,           # tighten & clarify
        "mode": "raw",               # email|message|commit|prompt|notes|raw
        "translate_via": "llm",      # "llm" (better for Hebrew) or "whisper"
        "combine_stages": True,      # one LLM call (fast) vs separate calls
    },
    "llm": {
        # local = strict on-device MLX model ($0, offline, private) and the
        # default; auto|claude|codex shell out to the user's signed-in CLI
        # (keyless) as an opt-in quality boost. See adr-local-intent-default.
        "backend": "local",          # local|auto|claude|codex
        "local_model": "mlx-community/Qwen2.5-3B-Instruct-4bit",
        "local_max_tokens": 1024,    # cap on generated tokens per transform
        "local_idle_secs": 600,      # free the in-memory model after N idle secs
        "claude_model": "sonnet",    # alias tracks latest; safer than dates
        "codex_model": "",           # empty = codex default
        # Reasoning ("thinking") effort — kept LOW for fast text transforms (we
        # don't need deep reasoning to clean up dictation). "" = leave the CLI
        # default. claude: low|medium|high|xhigh|max. codex: low|medium|high
        # ("minimal" is rejected when codex's web_search/image_gen tools are on).
        "claude_effort": "low",
        "codex_reasoning_effort": "low",
        "claude_extra_args": [],
        "codex_extra_args": [],
        "timeout": 120,              # seconds per LLM call
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
        "warm_max_turns": 25,        # recycle the process after N turns (bounds
                                     # context growth / memory)
        "warm_idle_secs": 600,       # recycle after this many idle seconds
    },
    "output": {
        "mode": "copy",              # "copy" or "paste" (paste also copies)
        "size_threshold": 2000,      # chars; above -> save to file instead
        "save_dir": "~/Documents/VoiceBridge",
        "save_format": "md",         # md|txt
        "keep_audio": False,         # delete the recording after transcription
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
        "identity": {"app": "alfred", "schema_version": "int", "pid": "int",
                     "ok": "bool"},
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
        "error_subtypes": ["audio_not_found", "stt_failed", "llm_failed",
                           "runtime"],
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
            "schema": {"phase": "str", "label": "str", "ts": "int_epoch_ms",
                       "start": "int_epoch_ms",
                       "steps": [{"label": "str", "ms": "int"}]},
            "phases": ["starting", "transcribing", "processing", "delivering",
                       "done", "error", "empty"],
        },
        "stream": {
            "path": "~/.voicebridge/stream.json",
            "schema": {"transcript": "str", "recording": "bool",
                       "done": "bool", "ts": "int_epoch_ms",
                       "path": "str"},
        },
        "history": {
            "path": str(Path(DEFAULTS["history"]["dir"]) / "history.jsonl"),
            "format": "jsonl",
            "dir_config": "[history].dir",
            "schema": {"ts": "str_iso_seconds", "source": "str", "chars": "int",
                       "text": "str"},
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
    hist_dir = ((cfg or {}).get("history", {}) or {}).get("dir") \
        or DEFAULTS["history"]["dir"]
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
        "daemon_info": str(_daemon_info_path()),
        "config": str(cfg.get("_loaded_from")) if cfg and cfg.get("_loaded_from")
        else "",
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


def load_config(path: str | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    candidates = [Path(path)] if path else CONFIG_SEARCH
    for p in candidates:
        if p and p.expanduser().is_file():
            try:
                import tomllib
            except ModuleNotFoundError:  # Python < 3.11
                sys.stderr.write(
                    "warning: Python 3.11+ needed to read config.toml; "
                    "using built-in defaults.\n"
                )
                break
            try:
                with open(p.expanduser(), "rb") as fh:
                    cfg = _deep_merge(cfg, tomllib.load(fh))
            except tomllib.TOMLDecodeError as e:
                # A hand-edit typo must not brick every command (dictation, and
                # even `doctor`). Warn, remember the error for doctor to surface,
                # and fall back to built-in defaults so the tool keeps working.
                sys.stderr.write(
                    f"warning: config.toml is invalid ({p.expanduser()}): {e}; "
                    "using built-in defaults.\n"
                )
                cfg = json.loads(json.dumps(DEFAULTS))
                cfg["_config_error"] = f"{p.expanduser()}: {e}"
                break
            cfg["_loaded_from"] = str(p.expanduser())
            break
    return cfg


# ----------------------------------------------------------------------------
# Speech-to-text  (mlx-whisper)
# ----------------------------------------------------------------------------

def _load_audio_16k(path: str):
    """Load any WAV/audio file to a mono float32 numpy array at 16 kHz."""
    try:
        import numpy as np
        import soundfile as sf
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"missing dependency '{e.name}'. Install with: "
            "pip install soundfile numpy"
        ) from e

    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception as e:                   # noqa: BLE001
        # A recorder killed mid-write (SIGKILL / crash) leaves an un-finalized
        # WAV whose header still declares a placeholder data length, which
        # soundfile refuses to open ("Error opening … : System error"). Fall back
        # to reading the raw int16 PCM after the data chunk using the REAL file
        # size (the streaming reader's approach) — recordings are always the
        # contract's 16 kHz mono int16, so this recovers the audio.
        audio = _read_pcm_f32(path, _wav_data_offset(path), 0, None)
        sr = 16000
        if audio.size == 0:
            raise RuntimeError(
                f"could not read audio (empty or unreadable WAV): {e}") from e
    if getattr(audio, "ndim", 1) > 1:        # stereo -> mono
        audio = audio.mean(axis=1)
    if sr != 16000:                          # light linear resample
        n = int(round(len(audio) * 16000 / sr))
        if n > 0:
            x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
            x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
            audio = np.interp(x_new, x_old, audio).astype(np.float32)
    return audio


def transcribe_samples(audio, cfg: dict, *, language: str | None,
                       whisper_translate: bool,
                       initial_prompt: str = "",
                       decode_opts: dict | None = None) -> tuple[str, str | None]:
    """Transcribe a mono float32 16 kHz numpy array. Return (text, lang).

    decode_opts passes extra Whisper decode options (e.g. the streaming path
    disables condition_on_previous_text so a hallucinated repeat in one window
    can't seed the next)."""
    try:
        import mlx_whisper
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "mlx-whisper is not installed. Install with: pip install mlx-whisper "
            "(requires Apple Silicon)."
        ) from e

    lang = language if language and language != "auto" else None
    kwargs = dict(
        path_or_hf_repo=cfg["stt"]["model"],
        task="translate" if whisper_translate else "transcribe",
        language=lang,
        # verbose=None is fully silent: no "Detected language" print, no progress
        # bar. Critical in the threaded daemon — a global stdout redirect here
        # would race the request handler's stdout capture and the background
        # streaming thread, corrupting responses.
        verbose=None,
    )
    ip = initial_prompt or cfg["stt"].get("initial_prompt")
    if ip:
        kwargs["initial_prompt"] = ip
    if decode_opts:
        kwargs.update(decode_opts)

    result = mlx_whisper.transcribe(audio, **kwargs)
    return (result.get("text") or "").strip(), result.get("language")


# RMS below which a streamed chunk is treated as silence and NOT transcribed.
# Whisper emits confident phantom text on silence ("Thank you.", "leaf leaf
# leaf…"), and the streamer's rolling context turned that into a runaway loop.
# Well below speech level (~0.02+) but above a real mic's noise floor, so it
# drops pure pauses without clipping quiet speech.
_STREAM_SILENCE_RMS = 0.0025
# Streaming decode options: don't condition on previously-decoded text, so a
# hallucinated repeat in one window can't bleed into the next.
_STREAM_DECODE_OPTS = {"condition_on_previous_text": False}


def _rms(buf) -> float:
    """Root-mean-square level of a float32 buffer. Defensive: returns a
    non-silent value for a non-array (a stub) so it's never dropped as silence."""
    try:
        import numpy as np
        arr = np.asarray(buf, dtype=np.float32)
        if arr.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(arr * arr)))
    except Exception:                                # noqa: BLE001
        return 1.0


def transcribe(audio_path: str, cfg: dict, *, language: str | None,
               whisper_translate: bool) -> tuple[str, str | None]:
    """Return (text, detected_language) for a whole audio file (batch)."""
    audio = _load_audio_16k(audio_path)
    return transcribe_samples(audio, cfg, language=language,
                              whisper_translate=whisper_translate)


# --- Streaming STT: transcribe a recording WHILE it's still being recorded -----
# We read raw 16-bit-mono-16kHz PCM straight from the growing WAV (after its data
# chunk) and transcribe it in chunks cut at silences, so when the user stops only
# the last short chunk remains — turning a multi-second post-stop wait into ~1-2s.

# Chunk geometry: cut around 8s, no later than 11s. Lower than the old 12/18s so
# a MEDIUM dictation (11-18s) actually gets pre-transcribed WHILE recording
# instead of entirely at stop; 8s is still ample context for Whisper accuracy.
_STREAM_TARGET = 8 * 16000       # aim to cut a chunk around 8s …
_STREAM_MAX = 11 * 16000         # … but no later than 11s (Whisper likes <=30s)
_STREAM_FRAME = 800              # 50ms silence-search frame
# Live preview: between committed chunks, re-transcribe the uncommitted tail this
# often so the HUD transcript builds every ~1.5s instead of only every ~11s.
_STREAM_PREVIEW_SECS = 1.5
_STREAM_PREVIEW_MIN = int(1.2 * 16000)   # need ~1.2s of new audio to bother


def _secure_dir(path: Path) -> None:
    """Create a directory and tighten it to owner-only (0700). The IPC + history
    files under ~/.voicebridge hold verbatim dictation, which is personal data —
    default 0755 would let any other local `staff` account read it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:                                   # noqa: S110
        pass


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write `text` to `path` atomically and owner-only: a temp file in the same
    dir + os.replace (atomic on APFS), created 0600. A high-frequency poller
    therefore never reads a half-written file, and a crash mid-write can't
    truncate the real file."""
    _secure_dir(path.parent)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(str(tmp), str(path))
    finally:
        try:
            os.unlink(tmp)
        except OSError:                              # already renamed away
            pass


def _atomic_write_json(path: Path, obj) -> None:
    _atomic_write_text(path, json.dumps(obj))


def _wav_data_offset(path: str) -> int:
    """Byte offset of PCM samples inside a WAV (the 'data' chunk), or 44."""
    try:
        with open(path, "rb") as f:
            head = f.read(8192)
        i = head.find(b"data")
        return i + 8 if i >= 0 else 44
    except OSError:
        return 44


def _pcm_sample_count(path: str, data_off: int) -> int:
    try:
        return max(0, (os.path.getsize(path) - data_off)) // 2
    except OSError:
        return 0


def _read_pcm_f32(path: str, data_off: int, start: int, end: int | None):
    """Read mono int16 PCM in [start, end) samples -> float32 in [-1, 1]."""
    import numpy as np
    with open(path, "rb") as f:
        f.seek(data_off + start * 2)
        raw = f.read(-1 if end is None else (end - start) * 2)
    n = len(raw) // 2
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(raw[: n * 2], dtype=np.int16).astype(np.float32) / 32768.0


def _silence_cut(buf, target: int, hard_max: int) -> int:
    """Pick a cut offset in [target, hard_max] at the quietest 50ms frame, so
    chunks break at a pause rather than mid-word."""
    import numpy as np
    if len(buf) <= target:
        return len(buf)
    hi = min(hard_max, len(buf))
    region = buf[target:hi]
    nf = region.size // _STREAM_FRAME
    if nf <= 0:
        return hi
    r = region[: nf * _STREAM_FRAME].reshape(nf, _STREAM_FRAME)
    rms = np.sqrt((r * r).mean(axis=1) + 1e-9)
    return target + int(rms.argmin()) * _STREAM_FRAME + _STREAM_FRAME // 2


def _stream_path() -> Path:
    return contract_paths()["stream"]


class StreamSession:
    """Transcribes a growing WAV in the background while it is still being
    recorded. `start` launches the chunk loop; `finish` stops it, transcribes the
    final tail, and returns the full (text, lang). Front-ends poll stream.json for
    the live partial transcript."""

    def __init__(self, path: str, cfg: dict, language, whisper_translate: bool):
        self.path = path
        self.cfg = cfg
        self.language = language
        self.wt = whisper_translate
        self.data_off = _wav_data_offset(path)
        self.cursor = 0                 # samples already transcribed (committed)
        self.parts: list[str] = []      # committed chunk texts
        self.preview = ""               # live, uncommitted tail (revised each cycle)
        self._last_preview_t = 0.0
        self.last_lang = None
        self.lock = threading.Lock()
        self.stop = False
        self.done = False
        self.started = time.monotonic()
        self.thread: threading.Thread | None = None

    @property
    def text(self) -> str:
        """Committed transcript (finalized chunks) — what finish() returns."""
        return " ".join(p for p in self.parts if p).strip()

    @property
    def display_text(self) -> str:
        """Committed text + the live tail preview — what the HUD shows."""
        p = (self.preview or "").strip()
        return (self.text + " " + p).strip() if p else self.text

    def _transcribe(self, end: int | None) -> None:
        # end=None -> final tail: take all remaining (mlx-whisper windows it
        # internally). end set -> a bounded window cut at the quietest pause.
        buf = _read_pcm_f32(self.path, self.data_off, self.cursor, end)
        if buf.size < _STREAM_FRAME:    # nothing meaningful yet
            return
        cut = len(buf) if end is None else _silence_cut(
            buf, _STREAM_TARGET, _STREAM_MAX)
        chunk = buf[:cut]
        # Silence gate: a pure-pause chunk makes Whisper hallucinate ("Thank
        # you.", "leaf leaf leaf…"); skip it (consume the samples, transcribe
        # nothing) so it can't seed a runaway repeat.
        if _rms(chunk) < _STREAM_SILENCE_RMS:
            self.cursor += cut
            self._write()
            return
        # No rolling initial_prompt: feeding the accumulated text back in
        # amplified any hallucinated repeat into a loop. Chunks are cut at
        # pauses, so cross-chunk context is marginal; the config vocab prompt
        # (names/jargon) still applies via transcribe_samples.
        txt, lang = transcribe_samples(
            chunk, self.cfg, language=self.language, whisper_translate=self.wt,
            decode_opts=_STREAM_DECODE_OPTS)
        if txt:
            self.parts.append(txt)
        if lang:
            self.last_lang = lang
        self.cursor += cut
        self.preview = ""               # committed audio absorbed the preview
        self._write()

    def _chunk_once(self) -> bool:
        if self.data_off == 44 and _wav_data_offset(self.path) != 44:
            self.data_off = _wav_data_offset(self.path)  # header now written
        avail = _pcm_sample_count(self.path, self.data_off)
        if avail - self.cursor < _STREAM_MAX:
            return False
        self._transcribe(self.cursor + _STREAM_MAX)
        return True

    def _preview(self) -> bool:
        """Transcribe the UNCOMMITTED tail as a live preview (throttled, silence-
        gated) so the transcript builds every ~1.5s instead of only when a full
        ~11s chunk commits. The preview is transient — it's revised each cycle and
        replaced by the committed chunk once the tail is long enough."""
        now = time.monotonic()
        if now - self._last_preview_t < _STREAM_PREVIEW_SECS:
            return False
        avail = _pcm_sample_count(self.path, self.data_off)
        if avail - self.cursor < _STREAM_PREVIEW_MIN:
            return False
        end = min(avail, self.cursor + _STREAM_MAX)
        buf = _read_pcm_f32(self.path, self.data_off, self.cursor, end)
        if buf.size < _STREAM_FRAME or _rms(buf) < _STREAM_SILENCE_RMS:
            return False
        txt, lang = transcribe_samples(
            buf, self.cfg, language=self.language, whisper_translate=self.wt,
            decode_opts=_STREAM_DECODE_OPTS)
        self.preview = txt or ""
        if lang:
            self.last_lang = lang
        self._last_preview_t = time.monotonic()
        self._write()
        return True

    def _run(self) -> None:
        idle = 0
        last_avail = -1
        while not self.stop:
            worked = False
            try:
                with self.lock:
                    worked = self._chunk_once()
                    if not worked:
                        self._preview()      # refresh the live tail between chunks
            except Exception as e:                       # noqa: BLE001
                sys.stderr.write(f"stream chunk error: {e}\n")
            # Abandon ONLY when the WAV stops growing (a cancelled/crashed
            # recording that never calls stream-finish) — NOT merely when a chunk
            # didn't fire, which also happens during a long pause while the file
            # is still being written. Otherwise the thread would poll a dead file
            # forever in the long-lived daemon.
            avail = _pcm_sample_count(self.path, self.data_off)
            if worked or avail != last_avail:
                idle = 0
            else:
                idle += 1
                if idle >= _STREAM_ABANDON_POLLS:
                    sys.stderr.write("stream: file not growing; abandoning session\n")
                    self.stop = True
            last_avail = avail
            # Poll fast enough that finish()'s join isn't stuck waiting out a long
            # idle sleep (was 1.0s -> up to ~1s of dead stop→result latency).
            time.sleep(0.2 if worked else 0.4)

    def start(self) -> None:
        self._write()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def finish(self) -> tuple[str, str | None]:
        self.stop = True
        if self.thread:
            self.thread.join(timeout=3)
        with self.lock:
            avail = _pcm_sample_count(self.path, self.data_off)
            while avail - self.cursor > _STREAM_FRAME:
                before = self.cursor
                if avail - self.cursor <= _STREAM_MAX:
                    self._transcribe(None)               # final tail: take all
                    break
                self._transcribe(self.cursor + _STREAM_MAX)
                if self.cursor <= before:                # safety: no progress
                    break
            self.preview = ""            # committed drain covers everything now
            self.done = True
            self._write()
        return self.text, self.last_lang

    def _write(self) -> None:
        # A superseded/abandoned session must not clobber the shared stream.json
        # that the current recording's HUD is reading. Only the active session
        # (or one still finishing, when none is active) writes.
        if _ACTIVE_STREAM is not None and _ACTIVE_STREAM is not self:
            return
        try:
            _atomic_write_json(_stream_path(), {
                "transcript": self.display_text, "recording": not self.stop,
                "done": self.done, "ts": _now_ms(), "path": self.path,
            })
        except Exception:                                # noqa: BLE001
            pass


# How many idle polls (~0.4s each) with no WAV growth before a session gives up.
_STREAM_ABANDON_POLLS = 120
# Hard TTL: a session older than this is reaped on the next stream-start.
_STREAM_TTL = 900

_STREAMS: dict[str, StreamSession] = {}
_STREAMS_LOCK = threading.Lock()
_ACTIVE_STREAM: StreamSession | None = None      # the session that owns stream.json


# ----------------------------------------------------------------------------
# LLM backends  (keyless: shell out to the user's own claude / codex)
# ----------------------------------------------------------------------------

# A GUI launcher (Hammerspoon) often spawns us with a trimmed $PATH that omits
# user bins like ~/.local/bin, so the claude/codex CLIs aren't found by name.
# Resolve them against PATH first, then these common locations.
_EXTRA_BIN_DIRS = [
    os.path.expanduser("~/.local/bin"),
    "/opt/homebrew/bin", "/usr/local/bin",
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
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
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
            "(`codex`) and sign in once, set backend = \"local\" for the "
            "on-device model, or disable the translate/rewrite/optimize stages."
        )
    return found


def run_llm_fallback(backends: list[str], prompt: str, cfg: dict) -> str:
    """Try each backend in order; return the first success. Raise the last error
    only if all fail (so 'auto' survives a logged-out / broken backend)."""
    last = None
    for b in backends:
        try:
            return run_llm(b, prompt, cfg)
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
        except Exception:                          # noqa: BLE001
            pass
    if env.get("USER"):
        env.setdefault("LOGNAME", env["USER"])
    return env


def _run(cmd: list[str], env: dict, timeout: int) -> str:
    try:
        proc = subprocess.run(
            cmd, env=env, timeout=timeout,
            cwd=tempfile.gettempdir(),   # neutral dir: don't scan user's project
            input="",                    # close stdin so the CLI doesn't wait on it
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",  # decode as UTF-8, not locale
        )
    except subprocess.TimeoutExpired:
        # Speak the one exception type the fallback loop understands, so a hung
        # CLI in `auto` mode falls back to the next backend instead of aborting.
        # Don't embed str(e) — it includes the full command (and thus the prompt).
        raise RuntimeError(
            f"{os.path.basename(cmd[0])} timed out after {timeout}s") from None
    except OSError as e:
        # Binary vanished between find_tool and exec, permission denied, etc.
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed to run: {e}") from e
    if proc.returncode != 0:
        lines = [l for l in (proc.stderr or proc.stdout or "").splitlines() if l.strip()]
        # Prefer a real error line over generic warnings / progress noise.
        meaningful = [l for l in lines
                      if not l.lower().lstrip().startswith(("warning:", "note:"))]
        pick = meaningful or lines
        msg = pick[-1].strip() if pick else f"exit {proc.returncode}"
        raise RuntimeError(f"{cmd[0]} failed: {msg}")
    return (proc.stdout or "").strip()


# Set True by `serve` so run_llm knows it may keep a warm process alive. One-shot
# CLI runs leave this False (a warm process would never be reused).
_DAEMON_MODE = False


def _claude_warm_cmd(cfg: dict) -> list[str]:
    """The claude command for a persistent stream-json session (no prompt arg —
    prompts are sent as messages over stdin)."""
    cmd = [find_tool("claude") or "claude", "-p",
           "--input-format", "stream-json",
           "--output-format", "stream-json", "--verbose"]
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
            except Exception:                       # noqa: BLE001
                pass

    def _start(self) -> None:
        self._stop()
        self._q = queue.Queue()
        p = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1, env=self.env,
            cwd=tempfile.gettempdir(),
        )
        self._proc = p
        q = self._q

        def pump_out():
            try:
                for line in p.stdout:
                    q.put(line)
            except Exception:                       # noqa: BLE001
                pass
            q.put(None)                             # sentinel: stream closed

        def drain_err():
            try:
                for _ in p.stderr:                  # keep the pipe from filling
                    pass
            except Exception:                       # noqa: BLE001
                pass

        threading.Thread(target=pump_out, daemon=True).start()
        threading.Thread(target=drain_err, daemon=True).start()
        self._turns = 0

    def ask(self, prompt: str, timeout: float | None) -> str:
        with self._lock:
            stale = (self._last and time.monotonic() - self._last > self.idle_secs)
            if not self._alive() or self._turns >= self.max_turns or stale:
                self._start()
            # Drop anything left over from a prior turn before sending ours.
            try:
                while True:
                    self._q.get_nowait()
            except queue.Empty:
                pass
            msg = {"type": "user",
                   "message": {"role": "user", "content": prompt}}
            try:
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
            except Exception as e:                  # noqa: BLE001
                self._stop()
                raise RuntimeError(f"warm claude write failed: {e}")
            deadline = time.monotonic() + (timeout or 120)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._stop()
                    raise RuntimeError("warm claude timed out")
                try:
                    line = self._q.get(timeout=remaining)
                except queue.Empty:
                    self._stop()
                    raise RuntimeError("warm claude timed out")
                if line is None:
                    self._stop()
                    raise RuntimeError("warm claude exited")
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:                   # noqa: BLE001
                    continue
                if obj.get("type") != "result":
                    continue
                self._turns += 1
                self._last = time.monotonic()
                if obj.get("is_error") or obj.get("subtype") not in (None, "success"):
                    self._stop()
                    raise RuntimeError(f"warm claude error: {obj.get('subtype')}")
                out = (obj.get("result") or "").strip()
                if not out:
                    raise RuntimeError("warm claude returned empty")
                return out


_WARM: WarmClaude | None = None
_WARM_SIG: tuple | None = None
_WARM_LOCK = threading.Lock()


def _get_warm(cfg: dict, env: dict) -> WarmClaude | None:
    """The shared warm-claude session, or None when warm mode doesn't apply
    (not the daemon, or disabled). Rebuilt if the relevant config changes."""
    global _WARM, _WARM_SIG
    if not _DAEMON_MODE or not cfg["llm"].get("warm", True):
        return None
    cmd = _claude_warm_cmd(cfg)
    sig = (tuple(cmd), int(cfg["llm"].get("warm_max_turns", 25)),
           int(cfg["llm"].get("warm_idle_secs", 600)))
    with _WARM_LOCK:
        if _WARM is None or _WARM_SIG != sig:
            if _WARM is not None:
                _WARM._stop()
            _WARM = WarmClaude(cmd, env, sig[1], sig[2])
            _WARM_SIG = sig
        return _WARM


def run_llm(backend: str, prompt: str, cfg: dict) -> str:
    _t = int(cfg["llm"]["timeout"])
    timeout = _t if _t > 0 else None             # 0 = no timeout (big prompts)
    if backend == "local":
        return run_local_llm(prompt, cfg)
    if backend == "claude":
        # Strip API-key + provider-routing vars so claude uses the subscription
        # OAuth login and never a cloud gateway.
        env = _clean_env(_CLAUDE_KEY_VARS)
        warm = _get_warm(cfg, env)
        if warm is not None:
            try:
                return _strip_wrapping(warm.ask(prompt, timeout))
            except Exception as e:                  # noqa: BLE001
                sys.stderr.write(f"warning: warm claude failed ({e}); "
                                 "one-shot fallback.\n")
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
        return run_llm_clean(cmd, env, timeout)
    if backend == "codex":
        cmd = [find_tool("codex") or "codex", "exec", "--skip-git-repo-check",
               "--sandbox", "read-only"]
        if cfg["llm"].get("codex_reasoning_effort"):
            # Low reasoning effort = faster. ("minimal" is rejected while codex's
            # web_search/image_gen tools are enabled, so we default to "low".)
            # Bare value (no quotes) — there's no shell here to strip them.
            cmd += ["-c", f"model_reasoning_effort={cfg['llm']['codex_reasoning_effort']}"]
        if cfg["llm"].get("codex_model"):
            cmd += ["-m", cfg["llm"]["codex_model"]]
        cmd += list(cfg["llm"].get("codex_extra_args") or [])
        cmd += [prompt]
        # Strip API-key + routing vars so codex uses the ChatGPT login, not the API.
        env = _clean_env(_CODEX_KEY_VARS)
        return run_llm_clean(cmd, env, timeout)
    raise RuntimeError(f"unknown backend '{backend}'")


def run_llm_clean(cmd: list[str], env: dict, timeout: int) -> str:
    out = _run(cmd, env, timeout)
    return _strip_wrapping(out)


def _strip_wrapping(text: str) -> str:
    """Remove accidental surrounding quotes / code fences from model output."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    if len(t) >= 2 and t[0] in "\"'" and t[-1] == t[0]:
        t = t[1:-1].strip()
    return t


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

_LOCAL = None                 # (model, tokenizer) once loaded
_LOCAL_SIG: str | None = None  # the model id currently loaded
_LOCAL_LAST = 0.0             # monotonic time of the last generation
_LOCAL_LOCK = threading.Lock()


def _local_load(model_id: str):
    """Load an MLX model + tokenizer. Lazy import so the rest of the engine runs
    without mlx-lm installed; the clear error guides install when it's missing."""
    try:
        from mlx_lm import load
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "mlx-lm is not installed (needed for backend = \"local\"). Install "
            "with: pip install mlx-lm  (Apple Silicon), or set backend to "
            "\"auto\"/\"claude\"/\"codex\"."
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
            add_generation_prompt=True, tokenize=False,
        )
    return generate(model, tokenizer, prompt=text,
                    max_tokens=max_tokens, verbose=False)


def run_local_llm(prompt: str, cfg: dict) -> str:
    """Run one transform on the warm on-device MLX model. Strict-local: no
    network, no key, nothing leaves the machine. Raises RuntimeError on failure
    so process_text falls back to the raw transcript (nothing lost)."""
    global _LOCAL, _LOCAL_SIG, _LOCAL_LAST
    model_id = cfg["llm"].get("local_model") or DEFAULTS["llm"]["local_model"]
    max_tokens = int(cfg["llm"].get("local_max_tokens", 1024))
    idle = int(cfg["llm"].get("local_idle_secs", 600))
    with _LOCAL_LOCK:                       # single-flight: one generation at a time
        stale = (_LOCAL_LAST and idle > 0
                 and time.monotonic() - _LOCAL_LAST > idle)
        if _LOCAL is None or _LOCAL_SIG != model_id or stale:
            _LOCAL = _local_load(model_id)   # (re)load; warm for next call
            _LOCAL_SIG = model_id
        model, tokenizer = _LOCAL
        try:
            out = _local_generate(model, tokenizer, prompt, max_tokens)
        except Exception as e:               # noqa: BLE001
            _LOCAL, _LOCAL_SIG = None, None  # drop a wedged model; reload next time
            raise RuntimeError(f"local MLX model failed: {e}") from e
        _LOCAL_LAST = time.monotonic()
    return _strip_wrapping(out or "")


# ----------------------------------------------------------------------------
# Processing stages  (composable, independently toggleable)
# ----------------------------------------------------------------------------

# A full, standalone rewrite instruction (used by a mode with "replace": True, so
# it stands in for the cleanup _REWRITE rather than being appended to it).
_PROMPT_OPTIMIZER = """\
You are a prompt optimizer. Given any user input, automatically rewrite it into a
clear, effective prompt. Never ask follow-up questions — infer everything from the
input alone and preserve the user's full original intent (every requirement, entity,
constraint, and nuance must survive the rewrite; never add goals they didn't imply).

INTERNAL STEPS (do not show these):
1. Deconstruct: extract the core intent, key entities, context, output requirements,
   and constraints. Map what's stated vs. merely implied.
2. Develop: silently classify the request type and apply the fitting approach —
   - Creative → multi-perspective, tone emphasis
   - Technical → constraint-based, precision focus
   - Educational → clear structure, examples
   - Complex → step-by-step reasoning, systematic framing
   Add a role/expertise framing and logical structure where it helps.
3. Auto-detect level:
   - SHORT → simple, single-step, or clear requests. Output a tight one-paragraph
     prompt with no scaffolding.
   - DETAILED → complex, professional, or multi-part requests. Output a structured
     prompt with role, context, task breakdown, and explicit output format.

OUTPUT:
Return only the rewritten prompt — no preamble, no explanation of changes, no questions."""

# Built-in rewrite "intents". A mode's prompt is appended to the cleanup rewrite
# instruction, UNLESS it sets "replace": True (then its prompt is used wholesale).
# Override any prompt/label, or add your own modes, via the [intent] section in
# config.toml (see mode_catalog / config.example.toml).
BUILTIN_MODES = [
    {"key": "email", "label": "Email", "description": "Polished email",
     "prompt": "Shape it as the body of a clear, courteous email. Do not invent a "
               "subject line, greeting, or signature unless they were dictated."},
    {"key": "message", "label": "Message", "description": "Casual chat / DM",
     "prompt": "Shape it as a concise, natural chat/Slack message."},
    {"key": "commit", "label": "Commit", "description": "Git commit message",
     "prompt": "Shape it as a git commit message: a short imperative summary line "
               "(<=72 chars), then a blank line, then bullet points if warranted."},
    {"key": "prompt", "label": "Prompt Optimizer",
     "description": "Rewrite input into an optimized AI prompt",
     "prompt": _PROMPT_OPTIMIZER, "replace": True},
    {"key": "notes", "label": "Notes", "description": "Clean notes / bullets",
     "prompt": "Shape it as clean, organized notes (short paragraphs or bullets)."},
    {"key": "raw", "label": "Cleanup only", "description": "Tidy wording, keep structure",
     "prompt": ""},
]


def mode_catalog(cfg: dict) -> list[dict]:
    """Built-in modes with config [intent] overriding prompts/labels and adding
    new modes. Returns ordered [{key, label, description, prompt}]."""
    by_key, order = {}, []
    for m in BUILTIN_MODES:
        by_key[m["key"]] = dict(m)
        order.append(m["key"])
    intent = cfg.get("intent")
    if isinstance(intent, dict):
        for key, spec in intent.items():
            if not isinstance(spec, dict):       # shorthand: key = "prompt text"
                spec = {"prompt": str(spec)}
            entry = by_key.get(key) or {"key": key, "label": key.capitalize(),
                                        "description": "", "prompt": "",
                                        "replace": False}
            entry.update({k: spec[k] for k in
                          ("prompt", "label", "description", "replace")
                          if k in spec})
            entry["key"] = key
            if key not in by_key:
                order.append(key)
            by_key[key] = entry
    return [by_key[k] for k in order]


def mode_prompt(cfg: dict, mode: str) -> str:
    for m in mode_catalog(cfg):
        if m["key"] == mode:
            return m.get("prompt", "")
    return ""

_TRANSLATE = ("Translate it into fluent, natural English. If it is already "
              "English, keep it unchanged. Preserve meaning and tone.")

_REWRITE = ("Clean up this raw voice transcription: remove filler words (um, uh, "
            "like), false starts, and repetitions; fix grammar, spelling, and "
            "punctuation; preserve the speaker's meaning, intent, and tone. Do "
            "not add new information and do not answer any question contained in "
            "the text.")

_OPTIMIZE = ("Tighten and clarify it: remove redundancy and wordiness, improve "
             "flow and structure, while preserving meaning and tone.")

_TAIL = ("Output ONLY the resulting text, with no preamble, labels, "
         "explanations, or surrounding quotes.")


def _whisper_can_translate(cfg: dict) -> bool:
    """Whether the configured Whisper model can do the translate task.

    The *-turbo distilled models were NOT trained on translation: asked to
    translate they silently emit near-source text (so a Hebrew capture comes
    back in Hebrew, not English). Only the full models (e.g. whisper-large-v3)
    translate. So `translate_via = "whisper"` is honoured only for non-turbo
    models; otherwise translation is folded into the LLM stage, which is both
    higher quality for Hebrew and the path that actually works on the default
    turbo model.
    """
    model = (cfg.get("stt", {}) or {}).get("model", "") or ""
    return "turbo" not in model.lower()


def whisper_translate_active(cfg: dict) -> bool:
    """Single source of truth: should the Whisper STT step itself translate?
    Only when translate is on, the user asked for the whisper route, AND the
    model can actually translate. Used by both `active_stages` (to avoid a
    redundant LLM translate) and the transcribe call (to pick the task)."""
    p = cfg["processing"]
    return (bool(p["translate"]) and p.get("translate_via") == "whisper"
            and _whisper_can_translate(cfg))


def active_stages(cfg: dict) -> dict:
    p = cfg["processing"]
    # Translation routes through the LLM unless Whisper both can and was asked to
    # do it; if Whisper already translated, the LLM translate stage is redundant.
    llm_translate = bool(p["translate"]) and not whisper_translate_active(cfg)
    return {
        "translate": llm_translate,
        "rewrite": bool(p["rewrite"]),
        "optimize": bool(p["optimize"]),
    }


def rewrite_instruction(cfg: dict) -> str:
    """The instruction for the rewrite stage: a mode's prompt appended to the
    cleanup _REWRITE, unless the mode is a 'replace' mode (then its prompt is
    used wholesale, e.g. the Prompt Optimizer)."""
    mode = cfg["processing"]["mode"]
    entry = next((m for m in mode_catalog(cfg) if m["key"] == mode), None)
    guidance = (entry or {}).get("prompt", "")
    if entry and entry.get("replace") and guidance:
        return guidance
    return f"{_REWRITE} {guidance}" if guidance else _REWRITE


def build_combined_prompt(stages: dict, rewrite_instr: str, text: str) -> str:
    steps = []
    if stages["translate"]:
        steps.append(_TRANSLATE)
    if stages["rewrite"]:
        steps.append(rewrite_instr)
    if stages["optimize"]:
        steps.append(_OPTIMIZE)
    numbered = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    return (
        "You are a text post-processor. Apply the following operations to the "
        "INPUT TEXT, in order:\n"
        f"{numbered}\n\n{_TAIL}\n\nINPUT TEXT:\n{text}"
    )


def single_stage_prompt(kind: str, rewrite_instr: str, text: str) -> str:
    if kind == "translate":
        instr = _TRANSLATE
    elif kind == "optimize":
        instr = _OPTIMIZE
    else:
        instr = rewrite_instr
    return f"{instr}\n\n{_TAIL}\n\nINPUT TEXT:\n{text}"


def process_text(text: str, cfg: dict) -> str:
    text = (text or "").strip()
    if not text:
        return text
    stages = active_stages(cfg)
    if not any(stages.values()):
        return text  # nothing enabled -> pass through, no LLM call

    backends = candidate_backends(cfg)
    rewrite_instr = rewrite_instruction(cfg)

    if cfg["processing"]["combine_stages"]:
        prompt = build_combined_prompt(stages, rewrite_instr, text)
        return run_llm_fallback(backends, prompt, cfg) or text

    out = text
    for kind in ("translate", "rewrite", "optimize"):
        if stages[kind]:
            prompt = single_stage_prompt(kind, rewrite_instr, out)
            out = run_llm_fallback(backends, prompt, cfg) or out
    return out


def refine_text(text: str, instruction: str, cfg: dict) -> str:
    """Apply a free-text user instruction to an existing result (the feedback
    loop: 'make it shorter', 'more formal', 'fix the date'). One LLM call that
    revises the text per the instruction; bypasses the stage pipeline. Falls back
    to the original text if the LLM returns nothing."""
    text = (text or "").strip()
    instruction = (instruction or "").strip()
    if not text or not instruction:
        return text
    backends = candidate_backends(cfg)
    prompt = (
        "Revise the INPUT TEXT according to the user's instruction. Apply only "
        "what the instruction asks; preserve everything else, including language. "
        f"Do not answer or explain.\n\nINSTRUCTION: {instruction}\n\n"
        f"{_TAIL}\n\nINPUT TEXT:\n{text}"
    )
    return run_llm_fallback(backends, prompt, cfg) or text


# ----------------------------------------------------------------------------
# Output / delivery
# ----------------------------------------------------------------------------

def _macos_tool(name: str) -> str:
    """Absolute path to a stock macOS binary, so we don't depend on $PATH (which
    a GUI launcher like Hammerspoon may strip down)."""
    for base in ("/usr/bin/", "/bin/"):
        if os.path.exists(base + name):
            return base + name
    return name


# --- Delivery sink: the three side effects deliver() routes between ----------
# deliver() decides WHAT to do (copy vs save, paste or not); the sink does the
# actual I/O. MacosSink is the default (pbcopy / osascript / file write); tests
# inject a fake sink to assert routing without touching the clipboard or disk.

class Sink:
    """The output side effects. Subclasses implement the primitives."""

    def copy(self, text: str) -> None:
        raise NotImplementedError

    def write_file(self, text: str, path: str) -> str:
        raise NotImplementedError

    def paste(self) -> bool:
        """Send Cmd+V. Return True if the keystroke was delivered, False if it
        could not be (e.g. no Accessibility grant)."""
        raise NotImplementedError

    def snapshot(self) -> str | None:
        """Return the current clipboard contents (for save/restore), or None."""
        return None

    def restore(self, data: str | None) -> None:
        """Put previously-snapshotted clipboard contents back."""
        if data is not None:
            self.copy(data)


class MacosSink(Sink):
    """Real macOS delivery: clipboard via pbcopy, Cmd+V paste via osascript,
    and a plain UTF-8 file write."""

    def copy(self, text: str) -> None:
        # We hand pbcopy UTF-8 bytes, but pbcopy decodes its stdin using the
        # locale (LANG / __CF_USER_TEXT_ENCODING). A GUI launcher
        # (Raycast/Hammerspoon) can spawn us with no/!UTF-8 locale, in which
        # case pbcopy reads our UTF-8 as Mac Roman and the clipboard gets
        # mojibake (Hebrew -> "◊©◊ú◊ï◊ù"). Force a UTF-8 locale for pbcopy so it
        # always matches the bytes we send.
        env = os.environ.copy()
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        subprocess.run([_macos_tool("pbcopy")], input=text, text=True,
                       encoding="utf-8", env=env, check=True)

    def write_file(self, text: str, path: str) -> str:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    def paste(self) -> bool:
        # osascript exits nonzero when the controlling process lacks the
        # Accessibility grant, so returncode tells us whether the paste landed.
        # timeout so a modal permission prompt / stuck System Events can't freeze
        # the stop→result path (a TimeoutExpired reads as paste_failed).
        try:
            proc = subprocess.run(
                [_macos_tool("osascript"), "-e",
                 'tell application "System Events" to keystroke "v" using '
                 'command down'],
                capture_output=True, text=True, check=False, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def snapshot(self) -> str | None:
        try:
            proc = subprocess.run([_macos_tool("pbpaste")], capture_output=True,
                                  text=True, encoding="utf-8", check=False,
                                  timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout if proc.returncode == 0 else None


# The default sink, shared by the thin module-level wrappers below.
_SINK: Sink = MacosSink()


def _save_path(cfg: dict) -> str:
    """The destination path for a saved result, derived from [output] config.
    Pure: computes the path (dir + timestamped name); the sink does the write."""
    d = Path(cfg["output"]["save_dir"]).expanduser()
    ext = "md" if cfg["output"]["save_format"] == "md" else "txt"
    ts = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return str(d / f"voicebridge_{ts}.{ext}")


# Thin wrappers kept for the existing call sites (e.g. history re-copy) — they
# delegate to the shared default sink, so behaviour is unchanged.
def copy_clipboard(text: str) -> None:
    _SINK.copy(text)


def auto_paste() -> None:
    _SINK.paste()


def save_to_file(text: str, cfg: dict) -> str:
    return _SINK.write_file(text, _save_path(cfg))


def deliver(text: str, cfg: dict, do_paste: bool,
            sink: Sink | None = None) -> tuple[str, str | None, bool | None]:
    """Pure routing over an injected sink: empty -> nothing; over the size
    threshold -> save to a file; otherwise copy (and paste if asked). Returns
    (kind, path, paste_ok): path is set only for 'saved'; paste_ok is None when
    no paste was attempted, else whether the keystroke landed.

    With [output].restore_clipboard = true, paste mode snapshots the user's
    clipboard first and restores it afterwards, so dictation doesn't destroy
    whatever they had copied (front-ends read the text from VB_RESULT, not the
    clipboard). Off by default for backward compatibility."""
    sink = sink or MacosSink()
    if not text.strip():
        return "empty", None, None
    threshold = int(cfg["output"]["size_threshold"])
    if threshold > 0 and len(text) > threshold:      # 0 = never save, always copy
        return "saved", sink.write_file(text, _save_path(cfg)), None
    if not do_paste:
        sink.copy(text)
        return "copied", None, None
    restore = bool(cfg["output"].get("restore_clipboard", False))
    prior = sink.snapshot() if restore else None
    sink.copy(text)
    paste_ok = sink.paste() is not False             # None (fake) -> treated ok
    if restore and paste_ok:
        sink.restore(prior)
    return "copied", None, paste_ok


# ----------------------------------------------------------------------------
# History
# ----------------------------------------------------------------------------

def history_path(cfg: dict) -> Path:
    return contract_paths(cfg)["history"]


_HISTORY_LOCK = threading.Lock()


def history_append(text: str, cfg: dict, source: str) -> None:
    if not cfg["history"]["enabled"] or not text.strip():
        return
    p = history_path(cfg)
    rec = {"ts": _dt.datetime.now().isoformat(timespec="seconds"),
           "source": source, "chars": len(text), "text": text}
    line = json.dumps(rec, ensure_ascii=False)
    max_items = int(cfg["history"]["max_items"])
    # Lock so two concurrent daemon requests can't lose an entry, and append
    # (0600) so a crash can't destroy the whole file; only rewrite to trim.
    with _HISTORY_LOCK:
        _secure_dir(p.parent)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if max_items > 0:
            existing = [l for l in p.read_text(encoding="utf-8").splitlines()
                        if l.strip()]
            if len(existing) > max_items:
                _atomic_write_text(p, "\n".join(existing[-max_items:]) + "\n")


# ----------------------------------------------------------------------------
# Status reporting (machine-readable last line for the front-end)
# ----------------------------------------------------------------------------

def print_status(*parts: str) -> None:
    sl = CONTRACT["status_line"]
    print(sl["sentinel"] + sl["sep"] + sl["sep"].join(parts))


def print_result(text: str) -> None:
    """Emit the machine-readable result line (JSON-encoded so newlines survive)
    just BEFORE the final VB_STATUS. Lets a front-end read the exact delivered
    text without racing the clipboard. Best-effort: never the last line."""
    sl = CONTRACT["status_line"]
    print(sl["result_sentinel"] + sl["sep"] + json.dumps(text, ensure_ascii=False))


def _status_parts(kind: str, path: str | None, paste_ok: bool | None,
                  *extra: str) -> list[str]:
    """Assemble the VB_STATUS fields: kind, optional saved path, then any
    suffixes (a trailing 'paste_failed' when a requested paste didn't land,
    plus caller extras like 'llm_failed')."""
    parts = [kind] + ([path] if path else [])
    if paste_ok is False:
        parts.append(CONTRACT["status_line"]["paste_failed_suffix"])
    parts.extend(extra)
    return parts


# ----------------------------------------------------------------------------
# Live progress (a small JSON file the front-ends poll to show a per-step
# stopwatch: what the engine is doing now + how long each step took)
# ----------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _progress_path() -> Path:
    return contract_paths()["progress"]


class _Progress:
    """Records the pipeline's phases to a JSON file front-ends poll. `step` closes
    the previous phase (recording its duration in ms) and opens the next; `done`
    closes the last. Best-effort: never raises into the pipeline."""

    def __init__(self):
        self.start = _now_ms()
        self.steps: list[dict] = []            # completed: [{"label", "ms"}]
        self._cur: tuple[str, int] | None = None
        self._write("starting", "Starting…", self.start)

    def step(self, phase: str, label: str) -> None:
        now = _now_ms()
        if self._cur:
            self.steps.append({"label": self._cur[0], "ms": now - self._cur[1]})
        self._cur = (label, now)
        self._write(phase, label, now)

    def done(self, phase: str = "done", label: str = "Done") -> None:
        now = _now_ms()
        if self._cur:
            self.steps.append({"label": self._cur[0], "ms": now - self._cur[1]})
            self._cur = None
        self._write(phase, label, now)

    def _write(self, phase: str, label: str, ts: int) -> None:
        try:
            _atomic_write_json(_progress_path(), {
                "phase": phase, "label": label, "ts": ts,
                "start": self.start, "steps": self.steps,
            })
        except Exception:                          # noqa: BLE001
            pass


def _stage_label(cfg: dict) -> str:
    """Human label for the LLM step, naming what it does + where it runs (e.g.
    'Translating & cleaning up via claude…'). Empty if no LLM stage is active."""
    s = active_stages(cfg)
    acts = [name for name, on in (("Translating", s["translate"]),
                                  ("cleaning up", s["rewrite"]),
                                  ("polishing", s["optimize"])) if on]
    if not acts:
        return ""
    text = " & ".join(acts)
    text = text[0].upper() + text[1:]
    backend = cfg["llm"].get("backend", "auto")
    via = (" on-device" if backend == "local"
           else f" via {backend}" if backend in ("claude", "codex")
           else " via Claude")
    return f"{text}{via}…"


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------

def _apply_overrides(cfg: dict, args) -> dict:
    if args.backend:
        cfg["llm"]["backend"] = args.backend
    if args.model:
        # Apply to ONLY the backend that will actually run (resolved after the
        # --backend override above). Fanning out to all three broke auto-fallback
        # — a claude alias handed to codex guarantees the fallback fails — and
        # risked overwriting local_model with a non-HF id.
        backend = cfg["llm"]["backend"]
        if backend == "local":
            cfg["llm"]["local_model"] = args.model
        elif backend == "codex":
            cfg["llm"]["codex_model"] = args.model
        else:                                    # "claude", or "auto" -> primary
            cfg["llm"]["claude_model"] = args.model
    if args.language:
        cfg["stt"]["language"] = args.language
    if args.mode:
        cfg["processing"]["mode"] = args.mode
        if args.mode not in {m["key"] for m in mode_catalog(cfg)}:
            sys.stderr.write(f"warning: unknown mode '{args.mode}'; "
                             "applying generic cleanup.\n")
        if args.mode != "raw":
            cfg["processing"]["rewrite"] = True
    for name in ("translate", "rewrite", "optimize"):
        val = getattr(args, name)
        if val is not None:
            cfg["processing"][name] = val
    if getattr(args, "paste", None) is not None:
        cfg["output"]["mode"] = "paste" if args.paste else "copy"
    return cfg


def _finish_capture(text: str, cfg: dict, args, prog: "_Progress") -> int:
    """Shared tail after transcription: LLM process -> deliver -> history ->
    VB_STATUS, with the resilient raw-transcript fallback. Used by `process`
    (batch) and `stream-finish` (streaming)."""
    llm_label = _stage_label(cfg)
    if llm_label:
        prog.step("processing", llm_label)
    try:
        final = process_text(text, cfg)
    except Exception as e:                            # noqa: BLE001
        # Resilient: still deliver the raw transcript so nothing is lost.
        sys.stderr.write(f"warning: LLM step failed, using raw transcript: {e}\n")
        prog.step("delivering", "LLM failed — delivering raw transcript")
        kind, path, paste_ok = deliver(text, cfg, cfg["output"]["mode"] == "paste")
        history_append(text, cfg, "stt")
        prog.done("done", "Done (raw transcript)")
        print_result(text)
        print_status(*_status_parts(kind, path, paste_ok, "llm_failed"))
        return 0

    if getattr(args, "stdout", False):
        prog.done()
        sys.stdout.write(final + "\n")
        return 0

    prog.step("delivering", "Delivering")
    kind, path, paste_ok = deliver(final, cfg, cfg["output"]["mode"] == "paste")
    history_append(final, cfg, "stt")
    prog.done()
    print_result(final)
    print_status(*_status_parts(kind, path, paste_ok))
    return 0


def _maybe_remove_audio(audio: str, cfg: dict) -> None:
    """Delete the recording unless [output].keep_audio is set. Called on BOTH the
    success and failure paths so a failed transcription doesn't leave the WAV
    (verbatim audio) sitting in a world-readable /tmp forever."""
    if cfg["output"]["keep_audio"]:
        return
    try:
        os.remove(audio)
    except OSError:
        pass


def cmd_process(args) -> int:
    cfg = _apply_overrides(load_config(args.config), args)
    prog = _Progress()
    audio = args.audio
    if not Path(audio).is_file():
        sys.stderr.write(f"error: audio file not found: {audio}\n")
        prog.done("error", "Audio file not found")
        print_status("error", "audio_not_found")
        return 2

    whisper_translate = whisper_translate_active(cfg)
    if (cfg["processing"]["translate"]
            and cfg["processing"].get("translate_via") == "whisper"
            and not whisper_translate):
        sys.stderr.write(
            "note: model cannot Whisper-translate (turbo); translating via the "
            "LLM instead.\n"
        )
    prog.step("transcribing", "Transcribing audio")
    try:
        text, lang = transcribe(
            audio, cfg, language=cfg["stt"]["language"],
            whisper_translate=whisper_translate,
        )
    except Exception as e:                       # noqa: BLE001
        sys.stderr.write(f"error: transcription failed: {e}\n")
        _maybe_remove_audio(audio, cfg)
        prog.done("error", "Transcription failed")
        print_status("error", "stt_failed")
        return 1

    _maybe_remove_audio(audio, cfg)

    if not text:
        sys.stderr.write("note: no speech detected.\n")
        prog.done("empty", "No speech detected")
        print_status("empty")
        return 0

    sys.stderr.write(f"transcript ({lang or '?'}): {text[:120]}\n")
    return _finish_capture(text, cfg, args, prog)


def cmd_stream_start(args) -> int:
    """Begin transcribing a growing WAV in the background, so most of it is done
    by the time recording stops. Only useful inside the warm daemon (the session
    lives in its process); a one-shot run would exit immediately."""
    global _ACTIVE_STREAM
    cfg = _apply_overrides(load_config(args.config), args)
    wt = whisper_translate_active(cfg)
    sess = StreamSession(args.audio, cfg, cfg["stt"]["language"], wt)
    now = time.monotonic()
    with _STREAMS_LOCK:
        # Reap abandoned sessions (recording cancelled/crashed without a
        # stream-finish) so their threads and dict entries don't leak.
        for k, s in list(_STREAMS.items()):
            if s.stop or now - s.started > _STREAM_TTL:
                s.stop = True
                _STREAMS.pop(k, None)
        old = _STREAMS.get(args.audio)
        if old:
            old.stop = True
        _STREAMS[args.audio] = sess
        _ACTIVE_STREAM = sess
    sess.start()
    print_status("streaming")
    return 0


def cmd_stream_finish(args) -> int:
    """Stop the background transcription, transcribe the final tail, then run the
    LLM pipeline and deliver — the streaming counterpart of `process`. Falls back
    to a full batch transcribe when there is no live session (daemon was down)."""
    prog = _Progress()
    with _STREAMS_LOCK:
        sess = _STREAMS.pop(args.audio, None)
    if sess is not None:
        cfg = _apply_overrides(sess.cfg, args)   # finish-time format/backend wins
        prog.step("transcribing", "Finishing transcription")
        try:
            text, lang = sess.finish()
        except Exception as e:                        # noqa: BLE001
            sys.stderr.write(f"error: stream finish failed: {e}\n")
            _maybe_remove_audio(args.audio, cfg)
            prog.done("error", "Transcription failed")
            print_status("error", "stt_failed")
            return 1
    else:
        cfg = _apply_overrides(load_config(args.config), args)
        wt = whisper_translate_active(cfg)
        prog.step("transcribing", "Transcribing audio")
        try:
            text, lang = transcribe(args.audio, cfg,
                                    language=cfg["stt"]["language"],
                                    whisper_translate=wt)
        except Exception as e:                        # noqa: BLE001
            sys.stderr.write(f"error: transcription failed: {e}\n")
            _maybe_remove_audio(args.audio, cfg)
            prog.done("error", "Transcription failed")
            print_status("error", "stt_failed")
            return 1
    _maybe_remove_audio(args.audio, cfg)
    if not text:
        prog.done("empty", "No speech detected")
        print_status("empty")
        return 0
    sys.stderr.write(f"transcript ({lang or '?'}): {text[:120]}\n")
    return _finish_capture(text, cfg, args, prog)


def cmd_text(args) -> int:
    cfg = _apply_overrides(load_config(args.config), args)
    cfg["processing"]["translate_via"] = "llm"   # no audio to Whisper-translate
    if args.text in (None, "-"):
        text = sys.stdin.read()
    else:
        text = args.text
    instruction = getattr(args, "instruction", None)
    try:
        final = (refine_text(text, instruction, cfg) if instruction
                 else process_text(text, cfg))
    except Exception as e:                        # noqa: BLE001
        sys.stderr.write(f"error: processing failed: {e}\n")
        print_status("error", "llm_failed")
        return 1
    if args.stdout:
        sys.stdout.write(final + "\n")
        return 0
    kind, path, paste_ok = deliver(final, cfg, cfg["output"]["mode"] == "paste")
    history_append(final, cfg, "text")
    print_result(final)
    print_status(*_status_parts(kind, path, paste_ok))
    return 0


def cmd_history(args) -> int:
    cfg = load_config(args.config)
    p = history_path(cfg)
    if not p.exists():
        print("(no history yet)")
        return 0
    recs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue                                 # skip a corrupt line, keep going
    if args.copy is not None:
        idx = args.copy
        try:
            rec = recs[-1 - idx] if idx >= 0 else recs[idx]
        except IndexError:
            sys.stderr.write("error: history index out of range\n")
            return 2
        copy_clipboard(rec["text"])
        print(f"copied item {idx} ({rec['chars']} chars) to clipboard")
        return 0
    n = args.limit or 10
    for i, rec in enumerate(reversed(recs[-n:])):
        preview = rec["text"].replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:67] + "..."
        print(f"[{i}] {rec['ts']}  {rec['chars']:>5}c  {preview}")
    return 0


def cmd_modes(args) -> int:
    """Emit the available rewrite modes (built-in + config [intent]) as JSON,
    so the front-end can populate its picker. One JSON array on stdout."""
    cfg = load_config(args.config)
    default_mode = cfg["processing"].get("mode")
    catalog = [{"key": m["key"],
                "label": m.get("label") or m["key"],
                "description": m.get("description", ""),
                "prompt": m.get("prompt", ""),
                # Ready-made argv that realizes this mode, so front-ends consume
                # the flag grammar instead of each re-deriving it (which has
                # drifted before). "raw"/no-LLM stays a front-end pseudo-format.
                "flags": ["--mode", m["key"], "--rewrite"],
                "default": m["key"] == default_mode}
               for m in mode_catalog(cfg)]
    print(json.dumps(catalog))
    return 0


def _config_target(args) -> Path:
    if getattr(args, "config", None):
        target = Path(args.config).expanduser()
        # Inside the daemon, a --config pointing outside the known search
        # locations is an arbitrary-path write primitive (set-intent/-model/
        # -processing mkdir+write to it). Front-ends never pass --config over the
        # daemon, so ignore an out-of-allowlist path there and fall back to search.
        if _DAEMON_MODE and target.resolve() not in {p.expanduser().resolve()
                                                      for p in CONFIG_SEARCH}:
            sys.stderr.write(
                "warning: ignoring out-of-allowlist --config over the daemon.\n")
        else:
            return target
    for p in CONFIG_SEARCH:
        if p.expanduser().is_file():
            return p.expanduser()
    return Path.home() / ".config" / "voicebridge" / "config.toml"


def _toml_str(s: str) -> str:
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"') \
                 .replace("\n", "\\n").replace("\t", "\\t").replace("\r", "")
    return '"' + s + '"'


def cmd_set_intent(args) -> int:
    """Write/override [intent.<key>] in config.toml via tomlkit.

    Uses tomlkit (like set-model/set-processing) instead of regex surgery, so a
    prompt whose text contains a line starting with '[' can't truncate the file,
    comments/formatting are preserved, and the .bak is the PRISTINE pre-edit file
    (the old code backed up the already-mutated text, so a restore lost exactly
    the prompt it was meant to protect). Existing extra keys (e.g. replace) are
    kept."""
    import re

    import tomlkit
    key = (args.key or "").strip()
    if not key or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        sys.stderr.write("error: intent key must be letters/numbers/-/_.\n")
        return 2
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    # Back up the PRISTINE file BEFORE mutating, so a restore recovers the old prompt.
    if path.is_file():
        try:
            path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
        except OSError:
            pass
    doc = tomlkit.parse(text)
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
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    ok = any(m["key"] == key for m in mode_catalog(load_config(str(path))))
    print_status("saved" if ok else "error")
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
    if path.is_file():
        try:
            path.with_suffix(path.suffix + ".bak").write_text(text, encoding="utf-8")
        except OSError:
            pass
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def cmd_set_model(args) -> int:
    """Persist the model for a backend: [llm] claude_model / codex_model."""
    key = "claude_model" if args.backend == "claude" else "codex_model"
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    _set_config_kv(path, "llm", key, _toml_str(args.model or ""))
    print_status("saved")
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
    print_status("saved")
    return 0


def cmd_set_stt(args) -> int:
    """Persist [stt] settings a front-end can change — currently the vocabulary /
    initial_prompt biasing (names, jargon, brands) and the STT language. The
    highest-churn STT knob (fixing a persistently misheard name) previously had
    no front-end or CLI write path; this gives it one, mirroring set-model."""
    path = _config_target(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    if getattr(args, "initial_prompt", None) is not None:
        _set_config_kv(path, "stt", "initial_prompt",
                       _toml_str(args.initial_prompt))
    if getattr(args, "language", None):
        _set_config_kv(path, "stt", "language", _toml_str(args.language))
    print_status("saved")
    return 0


# Selectable model presets per backend. Claude aliases track the latest model.
# Extend either list from config.toml:  [llm] claude_models / codex_models = [...]
_CLAUDE_MODELS = ["opus", "sonnet", "haiku"]
_CODEX_MODELS: list[str] = []


def cmd_settings(args) -> int:
    """Current backend/model settings, the selectable model lists, AND the
    [processing] defaults, as JSON for the front-end's dropdowns/badges."""
    cfg = load_config(args.config)
    llm = cfg["llm"]
    proc = cfg["processing"]

    def models(defaults, key):
        out = list(defaults)
        for m in (llm.get(key) or []):
            if str(m) not in out:
                out.append(str(m))
        return out

    print(json.dumps({
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
    }))
    return 0


def cmd_contract(args) -> int:
    """Print the IPC CONTRACT (the single source of truth the front-ends read):
    the daemon's HTTP shape, the VB_STATUS grammar, the state files, and a
    `resolved` block of absolute, [history].dir-aware paths."""
    cfg = load_config(getattr(args, "config", None))
    print(json.dumps(resolved_contract(cfg), indent=2))
    return 0


def cmd_doctor(args) -> int:
    cfg = load_config(args.config)
    ok = "OK "
    bad = "XX "
    warn = "-- "

    print("Alfred doctor\n" + "=" * 40)

    # Python / platform
    pyv = sys.version_info
    print(f"{ok if pyv >= (3, 9) else bad}Python {pyv.major}.{pyv.minor}.{pyv.micro}")
    import platform
    mach = platform.machine()
    print(f"{ok if mach == 'arm64' else warn}Architecture: {mach}"
          + ("" if mach == "arm64" else "  (mlx-whisper needs Apple Silicon)"))

    # Python deps. Use find_spec (checks installed WITHOUT importing) so doctor
    # doesn't pay the multi-second MLX framework import just to say "present".
    import importlib.util as _ilu

    def _installed(mod: str) -> bool:
        try:
            return _ilu.find_spec(mod) is not None
        except Exception:                          # noqa: BLE001
            return False

    for mod, hint in [("mlx_whisper", "pip install mlx-whisper"),
                      ("soundfile", "pip install soundfile"),
                      ("numpy", "pip install numpy")]:
        if _installed(mod):
            print(f"{ok}python module: {mod}")
        else:
            print(f"{bad}python module: {mod}   -> {hint}")

    # System tools
    for tool, hint in [("sox", "brew install sox  (needed by the recorder)"),
                       ("pbcopy", "(ships with macOS)")]:
        path = shutil.which(tool)
        print(f"{ok if path else bad}command: {tool}"
              + (f"  ({path})" if path else f"   -> {hint}"))

    # LLM backends
    have = detect_backends()
    for name, drop in [("claude", "ANTHROPIC_API_KEY"),
                       ("codex", "OPENAI_API_KEY")]:
        if have[name]:
            keyset = drop in os.environ
            note = (f"  WARNING: {drop} is set; it will be stripped per call so "
                    "the subscription login is used") if keyset else ""
            print(f"{ok}LLM backend: {name}  ({have[name]}){note}")
        else:
            print(f"{warn}LLM backend: {name} not found")
    if not any(have.values()):
        print("    (LLM stages disabled until claude or codex is installed; "
              "raw transcription still works)")

    # Local on-device backend (MLX-LM) — strict-local, no login, no network
    mlx_ok = _installed("mlx_lm")
    print(f"{ok if mlx_ok else warn}python module: mlx_lm"
          + ("" if mlx_ok else "   -> pip install mlx-lm  (for backend = local)"))
    local_model = cfg["llm"].get("local_model", "")
    if local_model:
        cache = (Path.home() / ".cache" / "huggingface" / "hub"
                 / ("models--" + local_model.replace("/", "--")))
        print(f"{ok if cache.exists() else warn}local model: {local_model}"
              + ("  (cached)" if cache.exists()
                 else "  (downloads on first 'backend = local' use)"))

    # Config + paths
    print("-" * 40)
    if cfg.get("_config_error"):
        print(f"{bad}config PARSE ERROR: {cfg['_config_error']}")
        print("    (using built-in defaults until fixed — check the file/line above)")
    print(f"config: {cfg.get('_loaded_from', '(built-in defaults)')}")
    print(f"STT model: {cfg['stt']['model']}   language: {cfg['stt']['language']}")
    print(f"stages: translate={cfg['processing']['translate']} "
          f"rewrite={cfg['processing']['rewrite']} "
          f"optimize={cfg['processing']['optimize']} "
          f"mode={cfg['processing']['mode']} "
          f"via={cfg['processing']['translate_via']}")
    print(f"backend: {cfg['llm']['backend']}   output: {cfg['output']['mode']}   "
          f"save_dir: {cfg['output']['save_dir']}")
    sd = Path(cfg["output"]["save_dir"]).expanduser()
    try:
        sd.mkdir(parents=True, exist_ok=True)
        print(f"{ok}save_dir writable: {sd}")
    except Exception as e:                          # noqa: BLE001
        print(f"{bad}save_dir not writable: {sd} ({e})")

    # macOS permissions (TCC) — a STATIC note, not a live probe. Actively
    # querying Accessibility (osascript "System Events" UI-elements-enabled) hangs
    # for seconds inside a headless daemon with no Automation grant, and doctor is
    # called on every Engine Status open. The runtime signal is better anyway:
    # deliver() reports a ('copied','paste_failed') status when a real paste
    # can't be delivered, so the front-end says so in context.
    if platform.system() == "Darwin":
        print("-" * 40)
        print(f"{warn}auto-paste needs Accessibility granted to the app that runs "
              "Alfred (Raycast/Hammerspoon) in System Settings ▸ Privacy ▸ "
              "Accessibility; the mic needs it per-app too. (Copy mode needs "
              "neither.)")

    # Running daemon (identity + owner pid), so front-ends/users can see which
    # process owns the warm engine — auto-paste attribution follows that process.
    who = _probe_daemon(CONTRACT["daemon"]["port"])
    if who and who.get("app") == "alfred":
        print(f"{ok}warm daemon: running (pid {who.get('pid')}, "
              f"schema v{who.get('schema_version')})")
    elif who:
        print(f"{bad}port {CONTRACT['daemon']['port']} held by a NON-Alfred server")
    else:
        print(f"{warn}warm daemon: not running (starts on first capture)")
    return 0


# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------

def _bool_flag(parser, name, help_on, help_off):
    g = parser.add_mutually_exclusive_group()
    g.add_argument(f"--{name}", dest=name, action="store_true", default=None,
                   help=help_on)
    g.add_argument(f"--no-{name}", dest=name, action="store_false", default=None,
                   help=help_off)


def add_common(p):
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--backend", choices=["local", "auto", "claude", "codex"],
                   help="override LLM backend (local = on-device MLX)")
    p.add_argument("--model", help="override model name for the chosen backend")
    p.add_argument("--language", help="STT language code, or 'auto'")
    p.add_argument("--mode",
                   help="rewrite target / intent, e.g. email|message|commit|"
                        "prompt|notes|raw or a custom [intent] mode (also enables "
                        "--rewrite). See `voicebridge.py modes`.")
    _bool_flag(p, "translate", "translate output to English", "do not translate")
    _bool_flag(p, "rewrite", "clean up & shape to intent", "do not rewrite")
    _bool_flag(p, "optimize", "tighten & clarify", "do not optimize")
    _bool_flag(p, "paste", "auto-paste after copying", "copy only")
    p.add_argument("--stdout", action="store_true",
                   help="print result to stdout instead of clipboard/file")


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
        except Exception:                            # noqa: BLE001
            pass

    def isatty(self):
        return False

    def __getattr__(self, name):
        return getattr(self._real, name)


def _daemon_identity() -> dict:
    return {"ok": True, "app": "alfred",
            "schema_version": CONTRACT["schema_version"], "pid": os.getpid()}


def _write_daemon_info(port: int) -> None:
    """Write the discovery/identity file (0600) so a front-end — and `serve`
    itself on a busy port — can tell an Alfred daemon from a foreign server."""
    try:
        _atomic_write_json(_daemon_info_path(),
                           {**_daemon_identity(), "port": port})
    except Exception:                                # noqa: BLE001
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
    except Exception:                                # noqa: BLE001
        return None


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
    import io
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    global _DAEMON_MODE
    _DAEMON_MODE = True                              # allow a warm claude session

    parser = build_parser()

    # Warm the model now (mlx-whisper caches it for the life of the process).
    cfg0 = None
    try:
        import mlx_whisper
        import numpy as np
        cfg0 = load_config(args.config)
        sys.stderr.write("alfred: warming Whisper model…\n")
        sys.stderr.flush()
        with contextlib.redirect_stdout(sys.stderr):
            mlx_whisper.transcribe(np.zeros(16000, dtype="float32"),
                                   path_or_hf_repo=cfg0["stt"]["model"], verbose=False)
        sys.stderr.write("alfred: model ready.\n")
        sys.stderr.flush()
    except Exception as e:                              # noqa: BLE001
        sys.stderr.write(f"alfred: warm-up skipped ({e}); loads on first request.\n")

    # Pre-warm the claude session in the background so the first capture is fast
    # too (it pays the ~3s CLI startup now, off the critical path).
    def _prewarm():
        try:
            cfg = cfg0 if cfg0 is not None else load_config(args.config)
            if cfg["llm"].get("warm", True) and cfg["llm"]["backend"] != "codex":
                warm = _get_warm(cfg, _clean_env(_CLAUDE_KEY_VARS))
                if warm is not None:
                    warm.ask("Reply with exactly: ok", 60)
                    sys.stderr.write("alfred: claude session warm.\n")
                    sys.stderr.flush()
        except Exception as e:                          # noqa: BLE001
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
            if self.path == "/contract":               # the IPC contract
                self._json(200, resolved_contract(
                    load_config(getattr(args, "config", None))))
            else:                                       # health + identity
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
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except Exception:                          # noqa: BLE001
                req = {}
            out_buf, err_buf = io.StringIO(), io.StringIO()
            code = 1
            # Ensure the per-thread router is the active stream (a test harness
            # or anything else may have swapped sys.stdout since); idempotent.
            if sys.stdout is not out_proxy:
                sys.stdout = out_proxy
            if sys.stderr is not err_proxy:
                sys.stderr = err_proxy
            # Per-thread capture (no lock): this request's prints go to its own
            # buffers; other requests run fully in parallel.
            out_proxy.redirect(out_buf)
            err_proxy.redirect(err_buf)
            try:
                ns = parser.parse_args(req.get("argv") or [])
                code = ns.func(ns)
            except SystemExit as e:
                code = int(e.code or 0)
            except RuntimeError as e:
                sys.stderr.write(f"error: {e}\n")
                print_status("error", "runtime")
                code = 1
            except Exception as e:                     # noqa: BLE001
                sys.stderr.write(f"alfred: request failed: {e}\n")
                print_status("error", "runtime")
                code = 1
            finally:
                out_proxy.restore()
                err_proxy.restore()
            err_text = err_buf.getvalue()
            if err_text:                               # keep it in the daemon log too
                _real_err.write(err_text)
            self._json(200, {"code": code, "out": out_buf.getvalue(),
                             "err": err_text})

        def log_message(self, *a):
            pass

    port = int(args.port)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        who = _probe_daemon(port)
        if who and who.get("app") == "alfred":
            sys.stderr.write(f"alfred: port {port} already served by an Alfred "
                             f"daemon (pid {who.get('pid')}) — exiting.\n")
        else:
            sys.stderr.write(f"alfred: port {port} busy ({e}) and NOT an Alfred "
                             "daemon; refusing to start. Free the port or set a "
                             "different one.\n")
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
            _daemon_info_path().unlink()
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
    p_proc.set_defaults(func=cmd_process)

    p_ss = sub.add_parser("stream-start",
                          help="begin transcribing a growing WAV (daemon)")
    p_ss.add_argument("audio", help="path to the WAV sox is recording into")
    add_common(p_ss)
    p_ss.set_defaults(func=cmd_stream_start)

    p_sf = sub.add_parser("stream-finish",
                          help="finish a streamed recording: tail + LLM + deliver")
    p_sf.add_argument("audio", help="path to the recorded WAV")
    add_common(p_sf)
    p_sf.set_defaults(func=cmd_stream_finish)

    p_text = sub.add_parser("text", help="run the pipeline on text (Type mode)")
    p_text.add_argument("text", nargs="?", help="text, or '-'/omit to read stdin")
    p_text.add_argument("--instruction", help="apply a free-text instruction to "
                        "the text (feedback refine: 'make it shorter') instead of "
                        "the configured stages")
    add_common(p_text)
    p_text.set_defaults(func=cmd_text)

    p_hist = sub.add_parser("history", help="list or re-copy recent results")
    p_hist.add_argument("--config")
    p_hist.add_argument("--limit", type=int, default=10)
    p_hist.add_argument("--copy", type=int, metavar="N",
                        help="copy history item N (0 = most recent) to clipboard")
    p_hist.set_defaults(func=cmd_history)

    p_modes = sub.add_parser("modes", help="list rewrite modes (built-in + custom) as JSON")
    p_modes.add_argument("--config")
    p_modes.set_defaults(func=cmd_modes)

    p_si = sub.add_parser("set-intent", help="save/override an intent prompt in config.toml")
    p_si.add_argument("key")
    p_si.add_argument("--prompt", default="")
    p_si.add_argument("--label")
    p_si.add_argument("--description")
    p_si.add_argument("--config")
    p_si.set_defaults(func=cmd_set_intent)

    p_serve = sub.add_parser("serve", help="run a warm background engine (localhost HTTP)")
    p_serve.add_argument("--port", type=int, default=DAEMON_PORT)
    p_serve.add_argument("--config")
    p_serve.set_defaults(func=cmd_serve)

    p_set = sub.add_parser("set-model", help="persist claude_model / codex_model in config")
    p_set.add_argument("backend", choices=["claude", "codex"])
    p_set.add_argument("--model", default="")
    p_set.add_argument("--config")
    p_set.set_defaults(func=cmd_set_model)

    p_sp = sub.add_parser("set-processing",
                          help="persist [processing] defaults (mode + stage toggles)")
    p_sp.add_argument("--mode", help="default rewrite mode/intent, or 'raw'")
    _bool_flag(p_sp, "rewrite", "enable rewrite by default", "disable rewrite by default")
    _bool_flag(p_sp, "translate", "translate by default", "do not translate by default")
    _bool_flag(p_sp, "optimize", "optimize by default", "do not optimize by default")
    p_sp.add_argument("--config")
    p_sp.set_defaults(func=cmd_set_processing)

    p_st = sub.add_parser("set-stt",
                          help="persist [stt] settings (vocab/initial_prompt, language)")
    p_st.add_argument("--initial-prompt", dest="initial_prompt",
                      help="vocabulary/name biasing for the STT model")
    p_st.add_argument("--language", help="STT language code, or 'auto'")
    p_st.add_argument("--config")
    p_st.set_defaults(func=cmd_set_stt)

    p_get = sub.add_parser("settings", help="print backend/model settings + lists as JSON")
    p_get.add_argument("--config")
    p_get.set_defaults(func=cmd_settings)

    p_doc = sub.add_parser("doctor", help="check the environment")
    p_doc.add_argument("--config")
    p_doc.set_defaults(func=cmd_doctor)

    p_con = sub.add_parser("contract",
                           help="print the IPC contract (state files + daemon "
                                "API + status grammar) as JSON")
    p_con.set_defaults(func=cmd_contract)
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:                            # noqa: BLE001
        # Any failure (RuntimeError, a stray TOMLDecodeError/JSONDecodeError, …)
        # ends with a VB_STATUS line so the front-end never sees a bare traceback
        # with no machine-readable status. The contract promise: the LAST line is
        # always VB_STATUS.
        sys.stderr.write(f"error: {e}\n")
        print_status("error", "runtime")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
