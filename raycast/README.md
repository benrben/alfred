# Alfred — Raycast extension

A Raycast front-end for the [Alfred](../README.md) engine (`voicebridge.py`),
alongside the Hammerspoon one. It speaks the same contract: it prefers the warm
daemon (`voicebridge.py serve`) over localhost HTTP and falls back to spawning
the CLI if the daemon is down — so dictation reuses the already-loaded Whisper
model and no API key is ever involved.

## Formats (where Claude comes in)

Every capture runs through a **format**. **Raw transcript** does **no AI** — it's
exactly what you said. Every other format runs Claude/Codex: **Prompt Optimizer**
(the shipped default — rewrites your input into an optimized AI prompt),
**Cleanup only**, **Email**, **Message**, **Commit**, **Notes**, or your own. The
pickers default to **Default (config)**, which uses whatever you've set in
**Manage Intents** (where the default is starred). Want the untouched words? Pick
**Raw transcript** for that capture.

## Commands

- **Dictate** — a live recorder (via `sox`): running timer + mic-level bar, the
  active **output format** shown (`⌘F` to change it for this take). **⏎** stops &
  transcribes, then Paste / Copy / **Reprocess as…** / Dictate Again inline.
  **⌃C** cancels; **Esc** keeps recording in the background (reopen to stop).
  While processing it shows a **live per-step stopwatch** — Transcribing audio →
  Translating & cleaning up (via local/claude/codex) → Delivering — with each
  finished step's time and the current step ticking, so you see exactly what it's
  doing and where the time goes.
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
./install.sh        # 0 → 100: engine venv + Whisper deps, sox, config, build & import
```

This is the **all-in-one** path — it sets up the engine (a Python `.venv` beside
`voicebridge.py` with `mlx-whisper`), installs `sox`, writes a starter
`config.toml` if you don't have one, then builds and imports the extension. It's
idempotent, so re-run it any time. (It does **not** install Hammerspoon; run the
[root `install.sh`](../install.sh) if you also want that front-end.)

It leaves a **permanent local install** — it runs at full speed; the
"Development" label under Raycast → Extensions is just a category, and it stays
tied to this folder. After it finishes:

1. Run **Engine Status** once to confirm the engine is reachable.
2. Assign a hotkey to **Dictate** (Raycast → select the command → `⌘K` →
   Configure Command → Hotkey). Avoid `⌥⌘D/I/T/V` if you also run the Hammerspoon
   front-end.

To develop with hot-reload instead, run `npm install && npm run dev` and leave it
running while you edit.

### Requirements & permissions

`install.sh` handles the engine venv, Whisper deps, `sox`, and config for you.
The two things it **can't** do, which you must grant once:

- **Apple Silicon + Python 3.11+** and **Homebrew** (for `sox`) — the script
  errors out early if these are missing.
- **Microphone access for Raycast** — Dictate shells out to `sox -d`, which
  records through Raycast's process. Grant it in System Settings → Privacy &
  Security → Microphone the first time.

### Preferences

Set in Raycast → Extensions → Alfred. All optional:

- **Daemon Port** (default `8763`) — must match the engine's `serve` port.
- **LLM Backend** / **Translate** — override per capture. **LLM Backend** offers
  **local (on-device MLX)**, `auto`, `claude`, `codex`, or **Default (config)**.
  `local` is strict on-device ($0, offline); `claude`/`codex` are keyless via your
  CLI login. (The default *format* lives in config — set it in **Manage Intents**,
  not here.)
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
