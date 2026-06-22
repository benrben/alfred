# Alfred — Raycast extension

A Raycast front-end for the [Alfred](../README.md) engine (`voicebridge.py`),
alongside the Hammerspoon one. It speaks the same contract: it prefers the warm
daemon (`voicebridge.py serve`) over localhost HTTP and falls back to spawning
the CLI if the daemon is down — so dictation reuses the already-loaded Whisper
model and no API key is ever involved.

## Formats (where Claude comes in)

Every capture runs through a **format**. **Raw transcript** does **no AI** — it's
exactly what you said. Every other format (**Cleanup only**, **Email**,
**Message**, **Commit**, **Prompt**, **Notes**, or your own) runs Claude/Codex to
clean up and reshape the text. The pickers default to your configured default,
which you set — and see — in **Manage Intents**. If output looks word-for-word,
the active format is **Raw**; switch it (or change the default).

## Commands

- **Dictate** — a live recorder (via `sox`): running timer + mic-level bar, the
  active **output format** shown (`⌘F` to change it for this take). **⏎** stops &
  transcribes, then Paste / Copy / **Reprocess as…** / Dictate Again inline.
  **⌃C** cancels; **Esc** keeps recording in the background (reopen to stop).
- **Transform Text** — prefilled from the selection (or clipboard): pick a format,
  run, then Copy / Paste / Reprocess the result.
- **Type & Process** — type a line and run it through the pipeline.
- **History** — browse recent results (from `~/.voicebridge/history`); copy/paste.
- **Manage Intents** — see which format is the **default** (starred), **Set as
  Default**, and **edit/add** the rewrite **prompt** behind each one. Saves to
  `config.toml` via the engine, so it applies to every front-end. Also a `⌘I`
  action on the Transform/Type forms.
- **Alfred Menu Bar** — recording state + quick access to every command.
- **Engine Status** — daemon health, the current **default format / stages /
  backend**, resolved paths, and the engine's `doctor`. Handy right after install.

## Setup

```bash
cd raycast
./install.sh        # installs deps, builds, and imports into Raycast
```

This leaves a **permanent local install** — it runs at full speed; the
"Development" label under Raycast → Extensions is just a category, and it stays
tied to this folder. After it finishes:

1. Run **Engine Status** once to confirm the engine is reachable.
2. Assign a hotkey to **Dictate** (Raycast → select the command → `⌘K` →
   Configure Command → Hotkey). Avoid `⌥⌘D/I/T/V` if you also run the Hammerspoon
   front-end.

To develop with hot-reload instead, run `npm install && npm run dev` and leave it
running while you edit.

### Requirements & permissions

- The Alfred engine installed and working (see the [root README](../README.md));
  run `voicebridge.py doctor` once.
- **Microphone access for Raycast** — Dictate shells out to `sox -d`, which
  records through Raycast's process. Grant it in System Settings → Privacy &
  Security → Microphone the first time.
- `sox` on `PATH` (`brew install sox`).

### Preferences

Set in Raycast → Extensions → Alfred. All optional:

- **Daemon Port** (default `8763`) — must match the engine's `serve` port.
- **LLM Backend** / **Translate** — fallback defaults when a picker is left on
  "Default (config)". (The default *format* lives in config — set it in **Manage
  Intents**, not here.)
- **Python (venv)** / **Engine Script** / **sox Path** — only used to start the
  daemon or fall back when it's down; `~` is expanded.

## How it talks to the engine

Each command sends `{"argv": [...]}` to `http://127.0.0.1:<port>/` — the same
arguments the CLI takes (e.g. `["text", "…", "--mode", "email"]`,
`["process", "/tmp/rec.wav"]`, `["modes"]`) — and reads back `{code, out}`. After
a `process`/`text` run the engine copies the result to the clipboard (or saves a
file) and prints a `VB_STATUS` line; the extension parses that to show the
outcome, mirroring the Hammerspoon front-end.

## Not handled here

Raycast can't show a *system-wide* floating HUD like the Hammerspoon front-end —
the live timer and mic-level bar live inside the Dictate window rather than
floating over every app. Everything else — formats, backends, models, translate,
history — is shared through the engine.
