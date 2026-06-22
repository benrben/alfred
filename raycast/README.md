# Alfred — Raycast extension

A Raycast front-end for the [Alfred](../README.md) engine (`voicebridge.py`),
alongside the Hammerspoon one. It speaks the same contract: it prefers the warm
daemon (`voicebridge.py serve`) over localhost HTTP and falls back to spawning
the CLI if the daemon is down — so dictation reuses the already-loaded Whisper
model and no API key is ever involved.

## Commands

- **Dictate** — opens a live recorder (via `sox`): a running timer and a mic-level
  bar, with **⏎** to stop & transcribe, then Paste/Copy the transcript inline.
  **⌃C** cancels; **Esc** keeps recording in the background and reopening Dictate
  re-adopts that take. Bind a Raycast hotkey to it.
- **Transform Text** — prefilled from the current selection (or clipboard): edit,
  pick a format, run, then Copy/Paste the cleaned result back.
- **Type & Process** — type a line and run it through the pipeline.
- **History** — browse recent results (read from `~/.voicebridge/history`); copy
  or paste any of them.
- **Alfred Menu Bar** — shows recording state and gives quick access to the
  commands above.
- **Engine Status** — pings the warm daemon, shows the resolved engine paths, and
  runs the engine's `doctor`. Handy right after install.

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
- **LLM Backend** / **Default Format** / **Translate** — per-capture overrides on
  top of your `config.toml`.
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
