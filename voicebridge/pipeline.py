"""Processing stages (translate/rewrite/optimize, mode catalog, long-input
chunking) and output delivery (clipboard/paste/file sinks).
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import voicebridge as _pkg

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
BUILTIN_MODES: list[dict[str, Any]] = [
    {
        "key": "email",
        "label": "Email",
        "description": "Polished email",
        "prompt": "Shape it as the body of a clear, courteous email. Do not invent a "
        "subject line, greeting, or signature unless they were dictated.",
    },
    {
        "key": "message",
        "label": "Message",
        "description": "Casual chat / DM",
        "prompt": "Shape it as a concise, natural chat/Slack message.",
    },
    {
        "key": "commit",
        "label": "Commit",
        "description": "Git commit message",
        "prompt": "Shape it as a git commit message: a short imperative summary line "
        "(<=72 chars), then a blank line, then bullet points if warranted.",
    },
    {
        "key": "prompt",
        "label": "Prompt Optimizer",
        "description": "Rewrite input into an optimized AI prompt",
        "prompt": _PROMPT_OPTIMIZER,
        "replace": True,
    },
    {
        "key": "notes",
        "label": "Notes",
        "description": "Clean notes / bullets",
        "prompt": "Shape it as clean, organized notes (short paragraphs or bullets).",
    },
    {
        "key": "raw",
        "label": "Cleanup only",
        "description": "Tidy wording, keep structure",
        "prompt": "",
    },
]


def _catalog_entry_for_intent(by_key: dict, key: str, spec: Any) -> dict:
    """Merge one [intent] override into a mode_catalog entry (an existing
    built-in updated in place, or a fresh entry for a custom mode)."""
    if not isinstance(spec, dict):  # shorthand: key = "prompt text"
        spec = {"prompt": str(spec)}
    entry = by_key.get(key) or {
        "key": key,
        "label": key.capitalize(),
        "description": "",
        "prompt": "",
        "replace": False,
    }
    entry.update(
        {k: spec[k] for k in ("prompt", "label", "description", "replace") if k in spec}
    )
    entry["key"] = key
    return entry


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
            is_new = key not in by_key
            by_key[key] = _catalog_entry_for_intent(by_key, key, spec)
            if is_new:
                order.append(key)
    return [by_key[k] for k in order]


def _mode_entry(cfg: dict, mode: str) -> dict | None:
    """The mode_catalog entry keyed `mode`, or None if there isn't one."""
    for m in mode_catalog(cfg):
        if m["key"] == mode:
            return m
    return None


def mode_prompt(cfg: dict, mode: str) -> str:
    entry = _mode_entry(cfg, mode)
    return entry.get("prompt", "") if entry else ""


_TRANSLATE = (
    "Translate it into fluent, natural English. If it is already "
    "English, keep it unchanged. Preserve meaning and tone."
)

_REWRITE = (
    "Clean up this raw voice transcription: remove filler words (um, uh, "
    "like), false starts, and repetitions; fix grammar, spelling, and "
    "punctuation; preserve the speaker's meaning, intent, and tone. Do "
    "not add new information and do not answer any question contained in "
    "the text."
)

_OPTIMIZE = (
    "Tighten and clarify it: remove redundancy and wordiness, improve "
    "flow and structure, while preserving meaning and tone."
)

_TAIL = (
    "Output ONLY the resulting text, with no preamble, labels, explanations, or surrounding quotes."
)


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
    return (
        bool(p["translate"]) and p.get("translate_via") == "whisper" and _whisper_can_translate(cfg)
    )


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
    entry = _mode_entry(cfg, cfg["processing"]["mode"])
    guidance = (entry or {}).get("prompt", "")
    if not guidance:
        return _REWRITE
    assert entry is not None  # guidance came from it, so it can't be None here
    if entry.get("replace"):
        return guidance
    return f"{_REWRITE} {guidance}"


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


# Chunking for long transcripts. The local MLX backend caps OUTPUT at
# local_max_tokens (claude/codex don't, but still have context limits + the
# daemon timeout). A translate/rewrite of an 18-minute dictation in ONE call
# therefore truncates at the cap — the transcript is complete but the processed
# text stops ~1024 tokens in. So we split a long input at sentence boundaries
# into chunks whose expected output stays under the cap, process each, and join.
# A short input is a single chunk -> identical to the old one-call behaviour.
_CHARS_PER_TOKEN = 3  # conservative (small) est: fewer chars/token ->
# smaller, safer chunks. Hebrew/English mixed.
_SENT_SPLIT = re.compile(r"(?<=[.!?…。！？])\s+")


def _chunk_char_budget(cfg: dict) -> int:
    """Max INPUT chars per processing chunk so a chunk's OUTPUT stays under the
    local token cap. Derived from local_max_tokens even for non-local backends —
    it's a sane reliability bound everywhere (and the cap that actually truncates
    is the local one). 0.6 leaves headroom for translation expanding the text."""
    max_tokens = int(cfg["llm"].get("local_max_tokens", 4096))
    return max(2000, int(max_tokens * 0.6 * _CHARS_PER_TOKEN))


def _paragraph_units(para: str, max_chars: int) -> list[tuple[str, bool]]:
    """Split one already-stripped, non-empty paragraph into <=max_chars units
    at sentence boundaries, hard-splitting a lone oversized sentence as a last
    resort. Only the first unit starts a new paragraph; the rest are
    same-paragraph continuations."""
    if len(para) <= max_chars:
        return [(para, True)]
    units: list[tuple[str, bool]] = []
    starts_para = True
    for sent in _SENT_SPLIT.split(para):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > max_chars:  # a lone huge sentence
            units.append((sent[:max_chars], starts_para))
            starts_para = False
            sent = sent[max_chars:]
        if sent:
            units.append((sent, starts_para))
            starts_para = False
    return units


def _split_into_units(text: str, max_chars: int) -> list[tuple[str, bool]]:
    """Break `text` into paragraph- or sentence-sized units, each <=max_chars;
    each carries whether IT is the first unit of a (new) paragraph."""
    units: list[tuple[str, bool]] = []
    for para in text.split("\n\n"):
        para = para.strip()
        if para:
            units.extend(_paragraph_units(para, max_chars))
    return units


def _sep_for_unit(cur: str, starts_para: bool) -> str:
    """Blank line to join a new-paragraph unit onto a non-empty `cur`
    (preserved WITHIN a chunk too); a plain space otherwise."""
    return "\n\n" if (cur and starts_para) else " "


def _pack_one_unit(
    chunks: list[tuple[str, bool]], cur: str, cur_starts_para: bool,
    unit: str, starts_para: bool, max_chars: int,
) -> tuple[str, bool]:
    """Fold one unit into `cur`, flushing it into `chunks` (mutated in place)
    first if it would exceed max_chars. Returns (cur, cur_starts_para)."""
    sep = _sep_for_unit(cur, starts_para)
    if cur and len(cur) + len(sep) + len(unit) > max_chars:
        chunks.append((cur, cur_starts_para))
        return unit, starts_para
    if not cur:
        cur_starts_para = starts_para
    return (f"{cur}{sep}{unit}" if cur else unit), cur_starts_para


def _pack_units(units: list[tuple[str, bool]], max_chars: int) -> list[tuple[str, bool]]:
    """Greedily pack units into <=max_chars chunks (see _pack_one_unit)."""
    chunks: list[tuple[str, bool]] = []
    cur, cur_starts_para = "", False
    for u, starts_para in units:
        cur, cur_starts_para = _pack_one_unit(chunks, cur, cur_starts_para, u, starts_para, max_chars)
    if cur:
        chunks.append((cur, cur_starts_para))
    return chunks


def _split_for_processing(text: str, max_chars: int) -> list[tuple[str, bool]]:
    """Split `text` into <=max_chars chunks, breaking at paragraph then sentence
    boundaries so a chunk never cuts mid-sentence. A single over-long sentence is
    hard-split as a last resort. Returns [text] when it already fits (the common
    case), so short captures make exactly one LLM call as before.

    Each item is (chunk_text, starts_new_paragraph): the flag tells process_text
    whether this chunk begins right after a blank-line break in the ORIGINAL
    text (so its processed output should be rejoined to the previous chunk's
    with a blank line) versus a paragraph merely having been split across chunks
    for size (rejoin with a plain space — the two chunks are one paragraph).
    Without this, every chunk boundary — including ones that landed exactly on a
    paragraph break — got flattened to a single space, destroying the structure
    of any transcript long enough to chunk."""
    if len(text) <= max_chars:
        return [(text, False)]
    units = _split_into_units(text, max_chars)
    chunks = _pack_units(units, max_chars)
    return chunks or [(text, False)]


def _process_chunk(
    text: str, cfg: dict, stages: dict, backends: list[str], rewrite_instr: str
) -> str:
    """Run the enabled stages over one chunk — one combined LLM call, or a call
    per stage when combine_stages is off. Falls back to the input on empty."""
    if cfg["processing"]["combine_stages"]:
        prompt = build_combined_prompt(stages, rewrite_instr, text)
        return _pkg.run_llm_fallback(backends, prompt, cfg) or text
    out = text
    for kind in ("translate", "rewrite", "optimize"):
        if stages[kind]:
            prompt = single_stage_prompt(kind, rewrite_instr, out)
            out = _pkg.run_llm_fallback(backends, prompt, cfg) or out
    return out


def _rejoin_chunks(
    chunks: list[tuple[str, bool]],
    cfg: dict,
    stages: dict,
    backends: list[str],
    rewrite_instr: str,
) -> str:
    """Process each chunk and rejoin outputs with the input's original
    structure: a blank line at a real paragraph break, else a plain space."""
    result = ""
    for chunk_text, starts_new_paragraph in chunks:
        out = (_process_chunk(chunk_text, cfg, stages, backends, rewrite_instr) or "").strip()
        if not out:
            continue
        if not result:
            result = out
        else:
            sep = "\n\n" if starts_new_paragraph else " "
            result = f"{result}{sep}{out}"
    return result


def process_text(text: str, cfg: dict) -> str:
    text = (text or "").strip()
    if not text:
        return text
    stages = active_stages(cfg)
    if not any(stages.values()):
        return text  # nothing enabled -> pass through, no LLM call

    backends = _pkg.candidate_backends(cfg)
    rewrite_instr = rewrite_instruction(cfg)

    chunks = _split_for_processing(text, _pkg._chunk_char_budget(cfg))
    if len(chunks) == 1:
        return _process_chunk(chunks[0][0], cfg, stages, backends, rewrite_instr)
    return _rejoin_chunks(chunks, cfg, stages, backends, rewrite_instr)


def refine_text(text: str, instruction: str, cfg: dict) -> str:
    """Apply a free-text user instruction to an existing result (the feedback
    loop: 'make it shorter', 'more formal', 'fix the date'). One LLM call that
    revises the text per the instruction; bypasses the stage pipeline. Falls back
    to the original text if the LLM returns nothing."""
    text = (text or "").strip()
    instruction = (instruction or "").strip()
    if not text or not instruction:
        return text
    backends = _pkg.candidate_backends(cfg)
    prompt = (
        "Revise the INPUT TEXT according to the user's instruction. Apply only "
        "what the instruction asks; preserve everything else, including language. "
        f"Do not answer or explain.\n\nINSTRUCTION: {instruction}\n\n"
        f"{_TAIL}\n\nINPUT TEXT:\n{text}"
    )
    return _pkg.run_llm_fallback(backends, prompt, cfg) or text


# ----------------------------------------------------------------------------
# Output / delivery
# ----------------------------------------------------------------------------


def _macos_tool(name: str) -> str:
    """Absolute path to a stock macOS binary, so we don't depend on $PATH (which
    a GUI launcher like Raycast may strip down)."""
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
        # like Raycast can spawn us with no/!UTF-8 locale, in which
        # case pbcopy reads our UTF-8 as Mac Roman and the clipboard gets
        # mojibake (Hebrew -> "◊©◊ú◊ï◊ù"). Force a UTF-8 locale for pbcopy so it
        # always matches the bytes we send.
        env = os.environ.copy()
        env["LANG"] = "en_US.UTF-8"
        env["LC_ALL"] = "en_US.UTF-8"
        subprocess.run(
            [_macos_tool("pbcopy")], input=text, text=True, encoding="utf-8", env=env, check=True
        )

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
                [
                    _macos_tool("osascript"),
                    "-e",
                    'tell application "System Events" to keystroke "v" using command down',
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return proc.returncode == 0

    def snapshot(self) -> str | None:
        try:
            proc = subprocess.run(
                [_macos_tool("pbpaste")],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=5,
            )
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
    # Results can finish concurrently in the daemon. A second-only name lets
    # the later write truncate the earlier result, so add an opaque suffix while
    # keeping the human-readable timestamp.
    return str(d / f"voicebridge_{ts}_{uuid.uuid4().hex[:8]}.{ext}")


# Thin wrappers kept for the existing call sites (e.g. history re-copy) — they
# delegate to the shared default sink, so behaviour is unchanged.
def copy_clipboard(text: str) -> None:
    _SINK.copy(text)


def auto_paste() -> None:
    _SINK.paste()


def save_to_file(text: str, cfg: dict) -> str:
    return _SINK.write_file(text, _save_path(cfg))


def _restore_prior_clipboard(sink: Sink, prior: str | None) -> None:
    """Best-effort: a failure putting the PRIOR clipboard back must never
    retroactively turn an already-successful copy+paste into an error."""
    try:
        sink.restore(prior)
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"warning: clipboard restore failed: {e}\n")


def _deliver_paste(text: str, cfg: dict, sink: Sink) -> tuple[str, str | None, bool | None]:
    """Copy then paste, snapshotting/restoring the user's prior clipboard
    first when [output].restore_clipboard is on."""
    restore = bool(cfg["output"].get("restore_clipboard", False))
    prior = sink.snapshot() if restore else None
    sink.copy(text)
    paste_ok = sink.paste() is not False  # None (fake) -> treated ok
    if restore and paste_ok:
        _restore_prior_clipboard(sink, prior)
    return "copied", None, paste_ok


def deliver(
    text: str, cfg: dict, do_paste: bool, sink: Sink | None = None
) -> tuple[str, str | None, bool | None]:
    """Pure routing over an injected sink: empty -> nothing; over the size
    threshold -> save to a file; otherwise copy (and paste if asked). Returns
    (kind, path, paste_ok): path is set only for 'saved'; paste_ok is None when
    no paste was attempted, else whether the keystroke landed.

    With [output].restore_clipboard = true, paste mode snapshots the user's
    clipboard first and restores it afterwards, so dictation doesn't destroy
    whatever they had copied (front-ends read the text from VB_RESULT, not the
    clipboard). Off by default for backward compatibility."""
    sink = sink or _SINK
    if not text.strip():
        return "empty", None, None
    threshold = int(cfg["output"]["size_threshold"])
    if threshold > 0 and len(text) > threshold:  # 0 = never save, always copy
        return "saved", sink.write_file(text, _save_path(cfg)), None
    if not do_paste:
        sink.copy(text)
        return "copied", None, None
    return _deliver_paste(text, cfg, sink)
