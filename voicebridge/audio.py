"""Audio I/O: WAV/PCM helpers, batch + streaming speech-to-text (mlx-whisper)."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import voicebridge as _pkg


def _resample_to_16k(audio, sr: int):
    """Light linear resample of a mono float32 buffer from `sr` to 16 kHz. A
    no-op when already at 16 kHz, or when the target length rounds to zero
    (nothing sane to interpolate onto) — the caller keeps the original buffer."""
    if sr == 16000:
        return audio
    import numpy as np

    n = int(round(len(audio) * 16000 / sr))
    if n <= 0:
        return audio
    x_old = np.linspace(0.0, 1.0, num=len(audio), endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n, endpoint=False)
    return np.interp(x_new, x_old, audio).astype(np.float32)


def _load_audio_16k(path: str):
    """Load any WAV/audio file to a mono float32 numpy array at 16 kHz."""
    try:
        import numpy as np  # noqa: F401  (import validates the dependency; np unused directly)
        import soundfile as sf
    except ModuleNotFoundError as e:
        raise RuntimeError(
            f"missing dependency '{e.name}'. Install with: pip install soundfile numpy"
        ) from e

    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception as e:  # noqa: BLE001
        # A recorder killed mid-write (SIGKILL / crash) leaves an un-finalized
        # WAV whose header still declares a placeholder data length, which
        # soundfile refuses to open ("Error opening … : System error"). Fall back
        # to reading the raw int16 PCM after the data chunk using the REAL file
        # size (the streaming reader's approach) — recordings are always the
        # contract's 16 kHz mono int16, so this recovers the audio.
        audio = _pkg._read_pcm_f32(path, _wav_data_offset(path), 0, None)
        sr = 16000
        if audio.size == 0:
            raise RuntimeError(f"could not read audio (empty or unreadable WAV): {e}") from e
    if getattr(audio, "ndim", 1) > 1:  # stereo -> mono
        audio = audio.mean(axis=1)
    return _resample_to_16k(audio, sr)


def _format_ts(seconds: float) -> str:
    """Format a segment-start time as [m:ss] (or [h:mm:ss] past an hour)."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"[{h}:{m:02d}:{sec:02d}]" if h else f"[{m}:{sec:02d}]"


def _format_segments(segments) -> str:
    """One '[m:ss] text' line per Whisper segment; empty segments dropped.
    Pure and defensive: a malformed segment (no start/text) is skipped rather
    than raising — timestamped output must never be why a capture fails."""
    lines = []
    for seg in segments or []:
        try:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            lines.append(f"{_format_ts(float(seg.get('start', 0)))} {text}")
        except Exception:  # noqa: BLE001
            continue
    return "\n".join(lines)


def _whisper_task_and_lang(whisper_translate: bool, language: str | None) -> tuple[str, str | None]:
    """Map the public (language, whisper_translate) inputs to mlx-whisper's own
    (task, language) kwargs: 'auto'/empty language becomes None (let Whisper
    detect it); whisper_translate selects the 'translate' task over 'transcribe'."""
    task = "translate" if whisper_translate else "transcribe"
    lang = language if language and language != "auto" else None
    return task, lang


def _apply_stt_kwargs(
    kwargs: dict, cfg: dict, initial_prompt: str, decode_opts: dict | None
) -> None:
    """Fold the optional initial_prompt (explicit override, else the config
    default) and any streaming decode_opts into `kwargs` in place."""
    ip = initial_prompt or cfg["stt"].get("initial_prompt")
    if ip:
        kwargs["initial_prompt"] = ip
    if decode_opts:
        kwargs.update(decode_opts)


def transcribe_samples(
    audio,
    cfg: dict,
    *,
    language: str | None,
    whisper_translate: bool,
    initial_prompt: str = "",
    decode_opts: dict | None = None,
    timestamps: bool = False,
) -> tuple[str, str | None]:
    """Transcribe a mono float32 16 kHz numpy array. Return (text, lang).

    With timestamps=True the text is one '[m:ss] …' line per Whisper segment
    (for transcript files / players that seek). LLM stages would rewrite the
    markers like any other text, so callers pair it with --transcribe-only.

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

    task, lang = _whisper_task_and_lang(whisper_translate, language)
    kwargs = dict(
        path_or_hf_repo=cfg["stt"]["model"],
        task=task,
        language=lang,
        # verbose=None is fully silent: no "Detected language" print, no progress
        # bar. Critical in the threaded daemon — a global stdout redirect here
        # would race the request handler's stdout capture and the background
        # streaming thread, corrupting responses.
        verbose=None,
    )
    _apply_stt_kwargs(kwargs, cfg, initial_prompt, decode_opts)

    result = mlx_whisper.transcribe(audio, **kwargs)
    if timestamps:
        text = _format_segments(result.get("segments"))
        # Fall back to plain text if segments came back empty/malformed.
        if text:
            return text, result.get("language")
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
    except Exception:  # noqa: BLE001
        return 1.0


def transcribe(
    audio_path: str,
    cfg: dict,
    *,
    language: str | None,
    whisper_translate: bool,
    timestamps: bool = False,
) -> tuple[str, str | None]:
    """Return (text, detected_language) for a whole audio file (batch)."""
    audio = _pkg._load_audio_16k(audio_path)
    return _pkg.transcribe_samples(
        audio, cfg, language=language, whisper_translate=whisper_translate, timestamps=timestamps
    )


# --- Streaming STT: transcribe a recording WHILE it's still being recorded -----
# We read raw 16-bit-mono-16kHz PCM straight from the growing WAV (after its data
# chunk) and transcribe it in chunks cut at silences, so when the user stops only
# the last short chunk remains — turning a multi-second post-stop wait into ~1-2s.

# Chunk geometry: cut around 8s, no later than 11s. Lower than the old 12/18s so
# a MEDIUM dictation (11-18s) actually gets pre-transcribed WHILE recording
# instead of entirely at stop; 8s is still ample context for Whisper accuracy.
# Live preview: between committed chunks, re-transcribe the uncommitted tail this
# often so the HUD transcript builds every ~1.5s instead of only every ~11s.


def _secure_dir(path: Path) -> None:
    """Create a directory and tighten it to owner-only (0700). The IPC + history
    files under ~/.voicebridge hold verbatim dictation, which is personal data —
    default 0755 would let any other local `staff` account read it."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:  # noqa: S110
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
        except OSError:  # already renamed away
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
    nf = region.size // _pkg._STREAM_FRAME
    if nf <= 0:
        return hi
    r = region[: nf * _pkg._STREAM_FRAME].reshape(nf, _pkg._STREAM_FRAME)
    rms = np.sqrt((r * r).mean(axis=1) + 1e-9)
    return target + int(rms.argmin()) * _pkg._STREAM_FRAME + _pkg._STREAM_FRAME // 2


def _stream_path() -> Path:
    return _pkg.contract_paths()["stream"]


def _preview_buffer_unusable(buf) -> bool:
    """True when a preview window is too short to matter or too quiet to be
    real speech (mirrors the streaming silence gate _transcribe applies to
    committed chunks)."""
    return buf.size < _pkg._STREAM_FRAME or _rms(buf) < _STREAM_SILENCE_RMS


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
        self.cursor = 0  # samples already transcribed (committed)
        self.parts: list[str] = []  # committed chunk texts
        self.preview = ""  # live, uncommitted tail (revised each cycle)
        self._last_preview_t = 0.0
        self.last_lang: str | None = None
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
        buf = _pkg._read_pcm_f32(self.path, self.data_off, self.cursor, end)
        if buf.size < _pkg._STREAM_FRAME:  # nothing meaningful yet
            return
        cut = len(buf) if end is None else _silence_cut(buf, _pkg._STREAM_TARGET, _pkg._STREAM_MAX)
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
        txt, lang = _pkg.transcribe_samples(
            chunk,
            self.cfg,
            language=self.language,
            whisper_translate=self.wt,
            decode_opts=_STREAM_DECODE_OPTS,
        )
        if txt:
            self.parts.append(txt)
        if lang:
            self.last_lang = lang
        self.cursor += cut
        self.preview = ""  # committed audio absorbed the preview
        self._write()

    def _chunk_once(self) -> bool:
        if self.data_off == 44 and _wav_data_offset(self.path) != 44:
            self.data_off = _wav_data_offset(self.path)  # header now written
        avail = _pcm_sample_count(self.path, self.data_off)
        if avail - self.cursor < _pkg._STREAM_MAX:
            return False
        self._transcribe(self.cursor + _pkg._STREAM_MAX)
        return True

    def _preview(self) -> bool:
        """Transcribe the UNCOMMITTED tail as a live preview (throttled, silence-
        gated) so the transcript builds every ~1.5s instead of only when a full
        ~11s chunk commits. The preview is transient — it's revised each cycle and
        replaced by the committed chunk once the tail is long enough."""
        now = time.monotonic()
        if now - self._last_preview_t < _pkg._STREAM_PREVIEW_SECS:
            return False
        avail = _pcm_sample_count(self.path, self.data_off)
        if avail - self.cursor < _pkg._STREAM_PREVIEW_MIN:
            return False
        end = min(avail, self.cursor + _pkg._STREAM_MAX)
        buf = _pkg._read_pcm_f32(self.path, self.data_off, self.cursor, end)
        if _preview_buffer_unusable(buf):
            return False
        txt, lang = _pkg.transcribe_samples(
            buf,
            self.cfg,
            language=self.language,
            whisper_translate=self.wt,
            decode_opts=_STREAM_DECODE_OPTS,
        )
        self.preview = txt or ""
        if lang:
            self.last_lang = lang
        self._last_preview_t = time.monotonic()
        self._write()
        return True

    def _track_idle(self, worked: bool, avail: int, last_avail: int, idle: int) -> int:
        """Advance the idle-poll counter for _run's abandon check: reset it on
        progress (a chunk fired, or the WAV grew since the last poll); otherwise
        increment it and, once it reaches _STREAM_ABANDON_POLLS with no growth at
        all, mark the session stopped (a cancelled/crashed recording that never
        called stream-finish) so the thread doesn't poll a dead file forever."""
        if worked or avail != last_avail:
            return 0
        idle += 1
        if idle >= _STREAM_ABANDON_POLLS:
            sys.stderr.write("stream: file not growing; abandoning session\n")
            self.stop = True
        return idle

    def _run(self) -> None:
        idle = 0
        last_avail = -1
        while not self.stop:
            worked = False
            try:
                with self.lock:
                    worked = self._chunk_once()
                    if not worked:
                        self._preview()  # refresh the live tail between chunks
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"stream chunk error: {e}\n")
            # Abandon ONLY when the WAV stops growing (a cancelled/crashed
            # recording that never calls stream-finish) — NOT merely when a chunk
            # didn't fire, which also happens during a long pause while the file
            # is still being written. Otherwise the thread would poll a dead file
            # forever in the long-lived daemon.
            avail = _pcm_sample_count(self.path, self.data_off)
            idle = self._track_idle(worked, avail, last_avail, idle)
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
            while avail - self.cursor > _pkg._STREAM_FRAME:
                before = self.cursor
                if avail - self.cursor <= _pkg._STREAM_MAX:
                    self._transcribe(None)  # final tail: take all
                    break
                self._transcribe(self.cursor + _pkg._STREAM_MAX)
                if self.cursor <= before:  # safety: no progress
                    break
            self.preview = ""  # committed drain covers everything now
            self.done = True
            self._write()
        return self.text, self.last_lang

    def _write(self) -> None:
        # A superseded/abandoned session must not clobber the shared stream.json
        # that the current recording's HUD is reading. Only the active session
        # (or one still finishing, when none is active) writes.
        if _pkg._ACTIVE_STREAM is not None and _pkg._ACTIVE_STREAM is not self:
            return
        try:
            _atomic_write_json(
                _pkg._stream_path(),
                {
                    "transcript": self.display_text,
                    "recording": not self.stop,
                    "done": self.done,
                    "ts": _pkg._now_ms(),
                    "path": self.path,
                },
            )
        except Exception:  # noqa: BLE001
            pass


# How many idle polls (~0.4s each) with no WAV growth before a session gives up.
_STREAM_ABANDON_POLLS = 120
# Hard TTL: a session older than this is reaped on the next stream-start.
_STREAM_TTL = 900

_STREAMS: dict[str, _pkg.StreamSession] = {}
_STREAMS_LOCK = threading.Lock()
