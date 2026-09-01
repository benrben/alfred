"""History, progress reporting, and the capture commands (process,
stream-start, stream-finish, text, history) that tie STT + pipeline + delivery
together.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import threading
import time
from pathlib import Path

import voicebridge as _pkg


def history_path(cfg: dict) -> Path:
    return _pkg.contract_paths(cfg)["history"]


_HISTORY_LOCK = threading.Lock()


def _trim_history_file(p: Path, max_items: int) -> None:
    """Rewrite the history file to its last `max_items` non-blank lines, if it
    has grown past that cap. A no-op cap (<=0) means "keep everything"."""
    if max_items <= 0:
        return
    existing = [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(existing) > max_items:
        _pkg._atomic_write_text(p, "\n".join(existing[-max_items:]) + "\n")


def history_append(text: str, cfg: dict, source: str) -> None:
    if not cfg["history"]["enabled"] or not text.strip():
        return
    p = history_path(cfg)
    rec = {
        "ts": _dt.datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "chars": len(text),
        "text": text,
    }
    line = json.dumps(rec, ensure_ascii=False)
    max_items = int(cfg["history"]["max_items"])
    # Lock so two concurrent daemon requests can't lose an entry, and append
    # (0600) so a crash can't destroy the whole file; only rewrite to trim.
    with _HISTORY_LOCK:
        _pkg._secure_dir(p.parent)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _trim_history_file(p, max_items)


# ----------------------------------------------------------------------------
# Status reporting (machine-readable last line for the front-end)
# ----------------------------------------------------------------------------


def print_status(*parts: str) -> None:
    sl = _pkg.CONTRACT["status_line"]
    print(sl["sentinel"] + sl["sep"] + sl["sep"].join(parts))


def print_result(text: str) -> None:
    """Emit the machine-readable result line (JSON-encoded so newlines survive)
    just BEFORE the final VB_STATUS. Lets a front-end read the exact delivered
    text without racing the clipboard. Best-effort: never the last line."""
    sl = _pkg.CONTRACT["status_line"]
    print(sl["result_sentinel"] + sl["sep"] + json.dumps(text, ensure_ascii=False))


def _status_parts(kind: str, path: str | None, paste_ok: bool | None, *extra: str) -> list[str]:
    """Assemble the VB_STATUS fields: kind, optional saved path, then any
    suffixes (a trailing 'paste_failed' when a requested paste didn't land,
    plus caller extras like 'llm_failed')."""
    parts = [kind] + ([path] if path else [])
    if paste_ok is False:
        parts.append(_pkg.CONTRACT["status_line"]["paste_failed_suffix"])
    parts.extend(extra)
    return parts


# ----------------------------------------------------------------------------
# Live progress (a small JSON file the front-ends poll to show a per-step
# stopwatch: what the engine is doing now + how long each step took)
# ----------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _progress_path() -> Path:
    return _pkg.contract_paths()["progress"]


class _Progress:
    """Records the pipeline's phases to a JSON file front-ends poll. `step` closes
    the previous phase (recording its duration in ms) and opens the next; `done`
    closes the last. Best-effort: never raises into the pipeline."""

    def __init__(self):
        self.start = _now_ms()
        self.steps: list[dict] = []  # completed: [{"label", "ms"}]
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
            _pkg._atomic_write_json(
                _pkg._progress_path(),
                {
                    "phase": phase,
                    "label": label,
                    "ts": ts,
                    "start": self.start,
                    "steps": self.steps,
                },
            )
        except Exception:  # noqa: BLE001
            pass


def _stage_label(cfg: dict) -> str:
    """Human label for the LLM step, naming what it does + where it runs (e.g.
    'Translating & cleaning up via claude…'). Empty if no LLM stage is active."""
    s = _pkg.active_stages(cfg)
    acts = [
        name
        for name, on in (
            ("Translating", s["translate"]),
            ("cleaning up", s["rewrite"]),
            ("polishing", s["optimize"]),
        )
        if on
    ]
    if not acts:
        return ""
    text = " & ".join(acts)
    text = text[0].upper() + text[1:]
    backend = cfg["llm"].get("backend", "auto")
    via = (
        " on-device"
        if backend == "local"
        else f" via {backend}"
        if backend in ("claude", "codex")
        else " via Claude"
    )
    return f"{text}{via}…"


# ----------------------------------------------------------------------------
# Commands
# ----------------------------------------------------------------------------


def _apply_backend_and_model_overrides(cfg: dict, args) -> None:
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
        else:  # "claude", or "auto" -> primary
            cfg["llm"]["claude_model"] = args.model


def _apply_mode_override(cfg: dict, args) -> None:
    if not args.mode:
        return
    cfg["processing"]["mode"] = args.mode
    if args.mode not in {m["key"] for m in _pkg.mode_catalog(cfg)}:
        sys.stderr.write(f"warning: unknown mode '{args.mode}'; applying generic cleanup.\n")
    if args.mode != "raw":
        cfg["processing"]["rewrite"] = True


def _apply_stage_toggles(cfg: dict, args) -> None:
    for name in ("translate", "rewrite", "optimize"):
        val = getattr(args, name)
        if val is not None:
            cfg["processing"][name] = val
    # --transcribe-only wins over every stage toggle above (and --mode's implicit
    # rewrite): a pure transcript, no LLM. Applied last so nothing re-enables it.
    if getattr(args, "transcribe_only", None):
        cfg["processing"]["translate"] = False
        cfg["processing"]["rewrite"] = False
        cfg["processing"]["optimize"] = False


def _apply_overrides(cfg: dict, args) -> dict:
    _apply_backend_and_model_overrides(cfg, args)
    if args.language:
        cfg["stt"]["language"] = args.language
    _apply_mode_override(cfg, args)
    _apply_stage_toggles(cfg, args)
    if getattr(args, "paste", None) is not None:
        cfg["output"]["mode"] = "paste" if args.paste else "copy"
    return cfg


def _deliver_and_report(
    text: str,
    cfg: dict,
    source: str,
    *extra_status: str,
    prog: "_Progress | None" = None,
    done_label: str = "Done",
) -> int:
    """Record history, then deliver, then report VB_STATUS — the shared tail
    of every capture/text-processing path (process / stream-finish / text).

    History is appended BEFORE delivery, so a delivery failure (clipboard
    write, an unwritable save_dir, ...) can never lose the result outright: it
    stays recoverable via `history --copy 0` even when the configured delivery
    itself raises. History itself is best-effort: it's a SECONDARY feature (the
    `history` command / --copy) and must never block the primary path — the
    actual delivery attempt — the way a disk-full/unwritable-history-dir error
    otherwise would, if it propagated straight out of this function. `extra_status`
    carries trailing status suffixes from the caller (e.g. "llm_failed" from the
    raw-transcript fallback); `prog` is the optional live-progress tracker
    (`text` has none)."""
    try:
        _pkg.history_append(text, cfg, source)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"warning: history append failed: {e}\n")
    try:
        kind, path, paste_ok = _pkg.deliver(text, cfg, cfg["output"]["mode"] == "paste")
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: delivery failed (kept in history): {e}\n")
        if prog:
            prog.done("error", "Delivery failed (kept in history)")
        _pkg.print_result(text)
        _pkg.print_status(*_status_parts("error", None, None, "deliver_failed", *extra_status))
        return 1
    if prog:
        prog.done("done", done_label)
    _pkg.print_result(text)
    _pkg.print_status(*_status_parts(kind, path, paste_ok, *extra_status))
    return 0


def _finish_capture(text: str, cfg: dict, args, prog: "_Progress") -> int:
    """Shared tail after transcription: LLM process -> deliver -> history ->
    VB_STATUS, with the resilient raw-transcript fallback. Used by `process`
    (batch) and `stream-finish` (streaming)."""
    llm_label = _stage_label(cfg)
    if llm_label:
        prog.step("processing", llm_label)
    try:
        final = _pkg.process_text(text, cfg)
    except Exception as e:  # noqa: BLE001
        # Resilient: still deliver the raw transcript so nothing is lost.
        sys.stderr.write(f"warning: LLM step failed, using raw transcript: {e}\n")
        if getattr(args, "stdout", False):
            prog.done()
            sys.stdout.write(text + "\n")
            return 0
        prog.step("delivering", "LLM failed — delivering raw transcript")
        return _deliver_and_report(
            text, cfg, "stt", "llm_failed", prog=prog, done_label="Done (raw transcript)"
        )

    if getattr(args, "stdout", False):
        prog.done()
        sys.stdout.write(final + "\n")
        return 0

    prog.step("delivering", "Delivering")
    return _deliver_and_report(final, cfg, "stt", prog=prog)


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


def _warn_if_whisper_translate_unavailable(cfg: dict, whisper_translate: bool) -> None:
    if (
        cfg["processing"]["translate"]
        and cfg["processing"].get("translate_via") == "whisper"
        and not whisper_translate
    ):
        sys.stderr.write(
            "note: model cannot Whisper-translate (turbo); translating via the LLM instead.\n"
        )


def _cmd_process_transcribe(
    audio: str, cfg: dict, args, prog: "_Progress", whisper_translate: bool
) -> tuple[str | None, str | None, int | None]:
    """Transcribe `audio`; on failure, clean up + report stt_failed and hand
    back an rc for the caller to return immediately. rc is None on success."""
    prog.step("transcribing", "Transcribing audio")
    try:
        text, lang = _pkg.transcribe(
            audio,
            cfg,
            language=cfg["stt"]["language"],
            whisper_translate=whisper_translate,
            timestamps=bool(getattr(args, "timestamps", None)),
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: transcription failed: {e}\n")
        _maybe_remove_audio(audio, cfg)
        prog.done("error", "Transcription failed")
        _pkg.print_status("error", "stt_failed")
        return None, None, 1
    return text, lang, None


def cmd_process(args) -> int:
    cfg = _apply_overrides(_pkg.load_config(args.config), args)
    prog = _Progress()
    audio = args.audio
    if not Path(audio).is_file():
        sys.stderr.write(f"error: audio file not found: {audio}\n")
        prog.done("error", "Audio file not found")
        _pkg.print_status("error", "audio_not_found")
        return 2

    whisper_translate = _pkg.whisper_translate_active(cfg)
    _warn_if_whisper_translate_unavailable(cfg, whisper_translate)
    text, lang, rc = _cmd_process_transcribe(audio, cfg, args, prog, whisper_translate)
    if rc is not None:
        return rc

    _maybe_remove_audio(audio, cfg)

    if not text:
        sys.stderr.write("note: no speech detected.\n")
        prog.done("empty", "No speech detected")
        _pkg.print_status("empty")
        return 0

    sys.stderr.write(f"transcript ({lang or '?'}): {text[:120]}\n")
    return _finish_capture(text, cfg, args, prog)


def cmd_stream_start(args) -> int:
    """Begin transcribing a growing WAV in the background, so most of it is done
    by the time recording stops. Only useful inside the warm daemon (the session
    lives in its process); a one-shot run would exit immediately."""
    cfg = _apply_overrides(_pkg.load_config(args.config), args)
    wt = _pkg.whisper_translate_active(cfg)
    sess = _pkg.StreamSession(args.audio, cfg, cfg["stt"]["language"], wt)
    now = time.monotonic()
    with _pkg._STREAMS_LOCK:
        # Reap abandoned sessions (recording cancelled/crashed without a
        # stream-finish) so their threads and dict entries don't leak.
        for k, s in list(_pkg._STREAMS.items()):
            if s.stop or now - s.started > _pkg._STREAM_TTL:
                s.stop = True
                _pkg._STREAMS.pop(k, None)
        old = _pkg._STREAMS.get(args.audio)
        if old:
            old.stop = True
        _pkg._STREAMS[args.audio] = sess
        _pkg._ACTIVE_STREAM = sess
    sess.start()
    _pkg.print_status("streaming")
    return 0


def _discard_live_session_for_timestamps(sess, want_timestamps: bool):
    """When --timestamps is requested mid-stream, a live session's segments
    restart their own clock per chunk and can't be honoured: stop the session
    (keeping its config for the fallback batch transcribe, discarding its
    text) and force the no-session batch path below. Returns (sess, base_cfg)
    — sess is None whenever the batch path should run."""
    if sess is None or not want_timestamps:
        return sess, None
    base_cfg = sess.cfg  # keep the session's config; discard its text
    try:
        sess.finish()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"warning: could not stop live stream session: {e}\n")
    return None, base_cfg


def _stream_finish_via_live_session(sess, args, prog: "_Progress"):
    """Finish-time format/backend wins over what was set at stream-start."""
    cfg = _apply_overrides(sess.cfg, args)
    prog.step("transcribing", "Finishing transcription")
    try:
        text, lang = sess.finish()
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: stream finish failed: {e}\n")
        _maybe_remove_audio(args.audio, cfg)
        prog.done("error", "Transcription failed")
        _pkg.print_status("error", "stt_failed")
        return None, None, cfg, 1
    return text, lang, cfg, None


def _stream_finish_via_batch(args, base_cfg: dict | None, want_timestamps: bool, prog: "_Progress"):
    cfg = _apply_overrides(base_cfg or _pkg.load_config(args.config), args)
    wt = _pkg.whisper_translate_active(cfg)
    prog.step("transcribing", "Transcribing audio")
    try:
        text, lang = _pkg.transcribe(
            args.audio,
            cfg,
            language=cfg["stt"]["language"],
            whisper_translate=wt,
            timestamps=want_timestamps,
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: transcription failed: {e}\n")
        _maybe_remove_audio(args.audio, cfg)
        prog.done("error", "Transcription failed")
        _pkg.print_status("error", "stt_failed")
        return None, None, cfg, 1
    return text, lang, cfg, None


def cmd_stream_finish(args) -> int:
    """Stop the background transcription, transcribe the final tail, then run the
    LLM pipeline and deliver — the streaming counterpart of `process`. Falls back
    to a full batch transcribe when there is no live session (daemon was down),
    or when --timestamps is requested: a streamed chunk's Whisper segments each
    restart their own clock at 0:00 (every chunk is its own independent decode
    window), so only a whole-file batch transcribe has real elapsed-time
    segments — honouring the flag mid-stream would emit WRONG timestamps rather
    than silently ignoring it."""
    prog = _Progress()
    with _pkg._STREAMS_LOCK:
        sess = _pkg._STREAMS.pop(args.audio, None)
    want_timestamps = bool(getattr(args, "timestamps", None))
    sess, base_cfg = _discard_live_session_for_timestamps(sess, want_timestamps)

    if sess is not None:
        text, lang, cfg, rc = _stream_finish_via_live_session(sess, args, prog)
    else:
        text, lang, cfg, rc = _stream_finish_via_batch(args, base_cfg, want_timestamps, prog)
    if rc is not None:
        return rc

    _maybe_remove_audio(args.audio, cfg)
    if not text:
        prog.done("empty", "No speech detected")
        _pkg.print_status("empty")
        return 0
    sys.stderr.write(f"transcript ({lang or '?'}): {text[:120]}\n")
    return _finish_capture(text, cfg, args, prog)


def cmd_text(args) -> int:
    cfg = _apply_overrides(_pkg.load_config(args.config), args)
    cfg["processing"]["translate_via"] = "llm"  # no audio to Whisper-translate
    if args.text in (None, "-"):
        text = sys.stdin.read()
    else:
        text = args.text
    instruction = getattr(args, "instruction", None)
    try:
        final = (
            _pkg.refine_text(text, instruction, cfg)
            if instruction
            else _pkg.process_text(text, cfg)
        )
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"error: processing failed: {e}\n")
        _pkg.print_status("error", "llm_failed")
        return 1
    if args.stdout:
        sys.stdout.write(final + "\n")
        return 0
    return _deliver_and_report(final, cfg, "text")


def _load_history_records(p: Path) -> list[dict]:
    recs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            recs.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue  # skip a corrupt line, keep going
    return recs


def _copy_history_item(idx: int, recs: list[dict]) -> int:
    try:
        rec = recs[-1 - idx] if idx >= 0 else recs[idx]
    except IndexError:
        sys.stderr.write("error: history index out of range\n")
        return 2
    _pkg.copy_clipboard(rec["text"])
    print(f"copied item {idx} ({rec['chars']} chars) to clipboard")
    return 0


def _print_history_list(recs: list[dict], limit: int | None) -> None:
    n = limit or 10
    for i, rec in enumerate(reversed(recs[-n:])):
        preview = rec["text"].replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:67] + "..."
        print(f"[{i}] {rec['ts']}  {rec['chars']:>5}c  {preview}")


def cmd_history(args) -> int:
    cfg = _pkg.load_config(args.config)
    p = history_path(cfg)
    if not p.exists():
        print("(no history yet)")
        return 0
    recs = _load_history_records(p)
    if args.copy is not None:
        return _copy_history_item(args.copy, recs)
    _print_history_list(recs, args.limit)
    return 0
