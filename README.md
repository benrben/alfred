# Alfred

Local speech-to-text + LLM cleanup for macOS. Press a hotkey, speak in **any
language (Hebrew included)**, and clean text lands on your clipboard.

**Everything can run on your Mac** — the cleanup/translate step defaults to a
**strict on-device model** (MLX, no login, no network, $0). Prefer higher
quality? Switch the backend to the `claude` or `codex` CLI **you already signed
in to** — still keyless: no API key is ever required, read, or stored.

```
hotkey → record → transcribe (mlx-whisper) → [translate → rewrite → optimize] → clipboard / file
                                              │   via local MLX (default) ──or── keyless claude/codex   │
                                              └── optional, each toggleable ──────────────────────────┘
```

The Raycast **Dictate** view shows a live **per-step stopwatch** while it works
(Transcribing → Translating & cleaning up → Delivering), so you always see what
it's doing and how long each step takes.

This is a working **V1**: an engine plus pluggable front-ends that talk over a
tiny CLI / localhost-HTTP contract.

- **`voicebridge.py`** — the engine (STT + LLM + output). Works standalone in a terminal.
- **`voicebridge.lua`** — the Hammerspoon front-end (global hotkeys, recording, menu bar, typed input).
- **`raycast/`** — a Raycast extension front-end (dictate, transform text, manage
  intents, history, menu bar). Install with `bash raycast/install.sh`; see
  [raycast/README.md](raycast/README.md).

---

## Requirements

- Apple Silicon Mac (M1+) — required by `mlx-whisper`.
- `python3` (3.11+ recommended), Homebrew.
- `sox` for recording: `brew install sox`.
- Hammerspoon for hotkeys: `brew install --cask hammerspoon`.
- For translate/rewrite/optimize: either the default **on-device** model
  (`mlx-lm`, installed automatically — first use downloads ~2GB), or the
  `claude` and/or `codex` CLI signed in (keyless). Raw transcription needs neither.

## Install

1. From this folder, run the installer (creates `.venv`, installs deps, writes a starter config):

   ```bash
   bash install.sh
   ```

2. (Optional) Sign in to an LLM CLI once — raw transcription works without this:

   ```bash
   claude         # then type /login        — Claude Code
   codex login    # Sign in with ChatGPT    — Codex
   ```

3. Wire up the hotkeys. Open Hammerspoon once, then add to `~/.hammerspoon/init.lua`:

   ```lua
   dofile(os.getenv("HOME") .. "/Claude/Projects/alfred/voicebridge.lua")
   ```

   Choose **Reload Config** from the Hammerspoon menu. Grant **Accessibility** and
   **Microphone** when prompted (System Settings → Privacy & Security).

Check everything at once:

```bash
./.venv/bin/python voicebridge.py doctor
```

### (Optional) Raycast front-end

Prefer Raycast over Hammerspoon, or want both? Install the extension:

```bash
bash raycast/install.sh
```

It installs deps, builds, and imports the extension into Raycast (dictate,
transform text, manage intents, history, menu bar, engine status). See
[raycast/README.md](raycast/README.md) for usage and hotkeys.

## Use

- **Cmd+Option+D** — dictate (uses your config's mode). Press once to start, again to stop.
- **Cmd+Option+I** — dictate *with intent*: pick a format first, then speak.
- **Cmd+Option+T** — type a line and run it through the same pipeline.
- **Cmd+Option+V** — open the **Alfred window** (see [below](#the-alfred-window)).
- The menu-bar icon shows state (🎙️ idle / 🔴 recording / ⏳ processing) and has a menu
  (Open window, Dictate, Dictate as…, Type…, Backend ▸, Restart engine, Reload intent modes).

While recording, a floating **HUD** shows a live `mm:ss` timer and a mic-level
meter so you can see it's actually hearing you. When a result is ready, a small
**panel** appears with the cleaned text and **Copy / Paste / Email / ✕** buttons
(Paste sends it straight to the app you were in; the panel auto-dismisses after
20s).

For a one-off format override, hit **Cmd+Option+I** (or **Dictate as…** / **Type…**
from the menu): a picker lets you choose Email, Message, Commit, Prompt, Notes,
Cleanup-only, your own custom modes, or a pure no-LLM transcript for that capture
— without editing your config. The picker is populated from the engine, so
custom `[intent]` modes (see [Configure](#configure)) appear automatically.

Switch the **LLM backend** live from the menu (**Backend ▸ auto / claude / codex**);
it applies to subsequent captures on top of your config default.

Change hotkeys (and toggle the level meter with `SHOW_METER`) at the top of
`voicebridge.lua`.

### The Alfred window

**Cmd+Option+V** (or **Open Alfred window** from the menu) opens a panel that
puts the whole pipeline in one place:

- a **Record** button (same toggle as the hotkey) with a live level meter;
- a **type box** — write a line and press ⏎ to run it through the pipeline;
- **Format / intent**, **LLM backend**, and per-backend **model** dropdowns;
- a **Translate to English** toggle;
- an inline **Edit prompt** editor to tweak an intent's prompt and save it; and
- the cleaned **result** (Copy / Paste) plus recent **history** (click to re-copy).

Settings you change here apply to the next capture; model and intent-prompt
edits are written back to your config.

### Speed (warm engine)

The front-end keeps a **warm engine** running in the background — a small
localhost daemon (`voicebridge.py serve`) that holds the Whisper model in memory
so each dictation skips the multi-second model load. It starts automatically and
survives Hammerspoon reloads; if it ever wedges, choose **Restart engine (warm)**
from the menu. (The very first run still downloads the model once.)

### From the terminal (no hotkey needed)

```bash
PY=./.venv/bin/python

$PY voicebridge.py text "um so like the meeting is tuesday" --rewrite --stdout
$PY voicebridge.py process recording.wav --translate --mode email
$PY voicebridge.py history            # list recent results
$PY voicebridge.py history --copy 0   # re-copy the most recent
```

Per-run flags override the config: `--translate/--no-translate`, `--rewrite`,
`--optimize`, `--mode email|message|commit|prompt|notes|raw`, `--backend
claude|codex`, `--model`, `--language he`, `--paste`, `--stdout`.

## Configure

Config lives at `~/.config/voicebridge/config.toml` (starter copied by the
installer; see `config.example.toml` for every option). Highlights:

- **Stages** (`[processing]`): `translate`, `rewrite`, `optimize` are independent
  toggles. All off = raw transcription with **no LLM call at all**. The shipped
  default turns `rewrite` on with `mode = "prompt"` (the **Prompt Optimizer**).
- **Intent** (`mode`): how `rewrite` shapes text — `prompt` (the **Prompt
  Optimizer**: rewrites your input into an optimized AI prompt), `email`,
  `message`, `commit`, `notes`, or `raw` (cleanup only). **Customizable:** override
  any built-in prompt or add your own modes in an `[intent]` section; add
  `replace = true` to use your prompt as the *whole* rewrite instruction instead
  of appending to the cleanup. See `config.example.toml`; list them with
  `voicebridge.py modes`.
- **Backend** (`[llm] backend`): `local` (default — a strict on-device MLX model,
  set by `local_model`, e.g. `Qwen2.5-3B-Instruct-4bit`; `$0`, offline, never
  leaves the Mac), `auto` (claude, else codex), or force `claude`/`codex`. `local`
  never silently falls back to a network CLI. Local is faster-to-private but lower
  quality than Claude on a small model — pick per your need.
- **Output** (`[output]`): `copy` vs `paste`; results longer than
  `size_threshold` chars are saved to `save_dir` and you get a notification with
  the path instead of a clipboard dump.

### Hebrew

`mlx-whisper` transcribes Hebrew well. Two ways to get **English** out:

- `translate_via = "llm"` (default) — the LLM (local model, or Claude/Codex)
  translates. Best quality for Hebrew, keeps tone, and combines with
  rewrite/optimize in one pass.
- `translate_via = "whisper"` — fully on-device via Whisper's translate task.
  This only works on a **full** model, so set `model =
  "mlx-community/whisper-large-v3"`. On the default `-turbo` model (which cannot
  translate) Alfred automatically routes translation through the LLM instead, so
  Hebrew→English never silently fails.

For best accuracy you can force `language = "he"` instead of `"auto"`.

## How "keyless" works (and why it won't surprise-bill you)

> With the default `backend = "local"` none of this applies — the LLM runs fully
> on-device (MLX), no CLI, no network, no key. The below is for when you switch
> to `claude`/`codex` for higher quality.

The engine spawns your own `claude`/`codex` binary and **strips the API-key
environment variables** first (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`CODEX_API_KEY`), so each call falls back to your subscription login instead of
silently billing an API key. It runs `claude -p` (not `--bare`, which would
require a key) and `codex exec --skip-git-repo-check --sandbox read-only`, both
in a temp directory so they never touch your projects. The tool never embeds or
reads a token — it just uses the CLI you already authenticated.

> Note: provider terms cover *individual* use of *your own* login on *your own*
> machine. Don't turn this into a shared multi-user service. Billing/limits for
> headless CLI use change over time — check `claude` `/status` and `/usage`.

## Troubleshooting

- **`doctor` flags a missing piece** — follow its hint (`brew install sox`,
  `pip install ...`, sign in to a CLI).
- **Hotkey does nothing** — grant Hammerspoon Accessibility; confirm the
  `dofile(...)` line and Reload Config; check the Hammerspoon console.
- **"Could not launch the engine"** — fix the `PYTHON`/`SCRIPT`/`SOX` paths at
  the top of `voicebridge.lua` (Hammerspoon needs absolute paths).
- **LLM step fails** — the engine still copies the **raw transcript** so nothing
  is lost; the notification says so. Test the CLI directly:
  `echo hi | claude -p "reply ok"`.
- **First run is slow** — the Whisper model downloads once, then it's cached.

## Files

```
voicebridge.py          engine / CLI
voicebridge.lua         Hammerspoon front-end
raycast/                Raycast extension (second front-end)
config.example.toml     all settings, documented
requirements.txt        Python deps
install.sh              setup + environment check
```

## Not yet (easy next steps)

Live partial transcripts, push-to-talk (hold) mode, audio pre-trim/VAD, and
packaging as a standalone `.app`. (A settings GUI and a warm always-on engine,
once on this list, now ship — see [The Alfred window](#the-alfred-window) and
[Speed](#speed-warm-engine).) The pipeline is structured as composable stages, so
adding the rest is incremental.
