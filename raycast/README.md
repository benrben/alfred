# Alfred — Raycast extension

A Raycast front-end for the [Alfred](../README.md) engine (`voicebridge.py`),
alongside the Hammerspoon one. It speaks the same contract: it prefers the warm
daemon (`voicebridge.py serve`) over localhost HTTP and falls back to spawning
the CLI if the daemon is down — so dictation reuses the already-loaded Whisper
model and no API key is ever involved.

## Commands

- **Dictate (Toggle)** — run once to start recording (via `sox`), run again to
  stop and process. Bind a Raycast hotkey for push-to-talk-style capture. Result
  lands on the clipboard.
- **Transform Text** — prefilled from the current selection (or clipboard): edit,
  pick a format, run, then Copy/Paste the cleaned result back.
- **Type & Process** — type a line and run it through the pipeline.
- **History** — browse recent results (read from `~/.voicebridge/history`); copy
  or paste any of them.
- **Alfred Menu Bar** — shows recording state and gives quick access to the
  commands above.

## Setup

```bash
cd raycast
npm install
npm run dev      # loads the extension into Raycast for development
```

`npm run dev` registers the commands in Raycast; assign hotkeys to **Dictate**
(and any others) from Raycast → Extensions.

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

Raycast has no audio-capture API and no floating HUD, so there's no live
mic-level meter during dictation (the Hammerspoon front-end has that). Everything
else — formats, backends, models, translate, history — is shared through the
engine.
