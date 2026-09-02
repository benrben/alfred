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

This is a working **V1**: an engine plus a Raycast front-end that talk over a
tiny CLI / localhost-HTTP contract.

- **`voicebridge.py`** — the engine (STT + LLM + output). Works standalone in a terminal.
- **`raycast/`** — the Raycast extension front-end (dictate, transform text, manage
  intents, history, menu bar). Install with `bash raycast/install.sh`; see
  [raycast/README.md](raycast/README.md).

---

## Requirements

- Apple Silicon Mac (M1+) — required by `mlx-whisper`.
- `python3` 3.11+ (required), Homebrew.
- `sox` for recording: `brew install sox`.
- Raycast for hotkeys and every command UI: [raycast.com](https://raycast.com).
- For translate/rewrite/optimize: either the default **on-device** model
  (`mlx-lm`, installed automatically — first use downloads ~2GB), or the
  `claude` and/or `codex` CLI signed in (keyless). Raw transcription needs neither.

## Install

1. From this folder, run the installer (creates the engine's `.venv`, installs
   Whisper deps and `sox`, writes a starter config, then builds and imports the
   extension into Raycast):

   ```bash
   bash raycast/install.sh
   ```

2. (Optional) Sign in to an LLM CLI once — raw transcription works without this:

   ```bash
   claude         # then type /login        — Claude Code
   codex login    # Sign in with ChatGPT    — Codex
   ```

3. Grant **Accessibility** and **Microphone** to Raycast when macOS prompts
   (System Settings → Privacy & Security) — auto-paste needs the former, the
   first dictation needs the latter.

Check everything at once:

```bash
./.venv/bin/python voicebridge.py doctor
```

See [raycast/README.md](raycast/README.md) for the full command reference,
hotkey setup, and preferences.

## Use

Open Raycast and search **Alfred**:

- **Dictate** — press once to start recording, `⏎` (or press again) to stop &
  transcribe. Shows a live per-step stopwatch (Transcribing → Translating &
  cleaning up → Delivering) and a transcript that builds live while you talk.
- **Transcribe Only** — the same recorder pinned to a raw transcript: no
  translate/rewrite/optimize. Fast and fully private.
- **Type & Process** — type a line and run it through the same pipeline.
- **Transform Text** — prefilled from your selection or clipboard; pick a
  format and run.
- **Manage Intents** — see the default format (starred), set a new default,
  and edit/add the rewrite prompt behind each one.
- **History** — browse and re-copy recent results.
- **Alfred Menu Bar** — recording state plus one-click access to every command.
- **Engine Status** — daemon health, current defaults, and `doctor` output.

Assign a hotkey to any command from Raycast (select it → `⌘K` → Configure
Command → Hotkey) — see [raycast/README.md](raycast/README.md) for the full
command reference and preferences.

For a one-off format override, use `⌘F` in Dictate/Transform Text: a picker
lets you choose Email, Message, Commit, Prompt, Notes, Cleanup-only, your own
custom modes, or a pure no-LLM transcript for that capture — without editing
your config. The picker is populated from the engine, so custom `[intent]`
modes (see [Configure](#configure)) appear automatically.

Switch the **LLM backend** live with `⌘B` in Dictate (`local` / `auto` /
`claude` / `codex`); it applies to that capture on top of your config default.

### Speed (warm engine)

The front-end keeps a **warm engine** running in the background — a small
localhost daemon (`voicebridge.py serve`) that holds the Whisper model in memory
so each dictation skips the multi-second model load. It starts automatically in
the background; if it ever wedges, **Engine Status** and `voicebridge.py doctor`
show the problem, and the Raycast engine starts the daemon again on the next
capture. (The very first run still downloads the model once.)

The LLM step also runs at **low reasoning effort** by default (claude `--effort
low`, codex `model_reasoning_effort=low`) — deep "thinking" isn't needed to clean
up dictation, and skipping it is noticeably faster. Tune via `claude_effort` /
`codex_reasoning_effort` in `[llm]`.

**Streaming transcription:** the warm daemon transcribes your recording **while
you're still talking** (chunked at silences), so when you stop only the last
short chunk remains — a long clip's multi-second post-stop wait drops to ~1–2s,
and the Raycast Dictate view shows the transcript building live. (A native
word-by-word streaming engine is a further optional upgrade.)

### From the terminal (no hotkey needed)

```bash
PY=./.venv/bin/python

$PY voicebridge.py text "um so like the meeting is tuesday" --rewrite --stdout
$PY voicebridge.py process recording.wav --translate --mode email
$PY voicebridge.py process recording.wav --transcribe-only   # raw, no LLM
$PY voicebridge.py history            # list recent results
$PY voicebridge.py history --copy 0   # re-copy the most recent
$PY voicebridge.py modes              # list built-in and custom intents
$PY voicebridge.py settings           # print the current backend and defaults
$PY voicebridge.py doctor             # check dependencies, permissions, and daemon
```

Per-run flags override the config: `--translate/--no-translate`, `--rewrite`,
`--optimize`, `--transcribe-only` (pure transcript — pins every LLM stage off,
winning over the others), `--mode email|message|commit|prompt|notes|raw`,
`--backend local|auto|claude|codex`, `--model`, `--language he`, `--paste`,
`--stdout`.

The `text` command accepts text as an argument or reads stdin when the argument
is omitted (or set to `-`). Use `--instruction "make it shorter"` for a
one-off refinement instead of the configured stages. For integration and
front-end debugging, `contract` prints the versioned IPC contract as JSON and
`serve --port 8763` starts the localhost daemon used by Raycast.

**Long recordings.** There's no cap on how long you can speak (the front-ends
self-stop at 60 min as a runaway guard). A long transcript is auto-split at
sentence boundaries into chunks that each stay under `local_max_tokens`, so
translate/rewrite output scales to any length instead of truncating mid-way.

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
- **Hotkey does nothing** — confirm you assigned one in Raycast (select the
  command → `⌘K` → Configure Command → Hotkey); a command with no hotkey only
  launches from the Raycast search bar.
- **"Could not launch the engine"** — check the **Python (venv)** / **Engine
  Script** / **sox Path** preferences under Raycast → Extensions → Alfred (see
  [raycast/README.md](raycast/README.md#preferences)).
- **LLM step fails** — the engine still copies the **raw transcript** so nothing
  is lost; the notification says so. Test the CLI directly:
  `echo hi | claude -p "reply ok"`.
- **First run is slow** — the Whisper model downloads once, then it's cached.

## Files

```
voicebridge.py          entry-point shim (`python3 voicebridge.py ...`)
voicebridge/            the engine / CLI package
raycast/                Raycast extension (front-end)
config.example.toml     all settings, documented
requirements.txt        Python deps (floating intent)
constraints.txt         pinned known-good versions (lockfile)
Makefile                dev tasks (make test / lint / typecheck)
```

## Development

The engine and the Raycast extension each carry their own test suite. One
entry point runs both (each is <10s):

```bash
make dev      # once: install pytest + ruff into the venv
make test     # run both suites (Python + Raycast/TS)
make lint     # ruff + eslint
make typecheck # TypeScript typecheck for the Raycast extension
```

Or individually: `make test-py`, `make test-ts`. CI
(`.github/workflows/ci.yml`) runs both on every push/PR — on plain Ubuntu,
since the suite stubs the MLX models (no Apple-Silicon wheels needed).

### Quality gate

Beyond `make test`/`make lint`, the repository carries a per-function quality
gate (coverage, complexity, CRAAP, file size, dead code, module boundaries, a
smoke start of the daemon) driven by the `code-discipline` skill. Its
configuration lives in `.quality/` (`quality-gate.json` = commands,
`quality-thresholds.json` = every numeric goal, `quality-dependencies.json` =
the intended architecture). Tools are ordinary dev dependencies
(`requirements-dev.txt`, `raycast/package.json`).

```bash
make quality        # fast pass over your local changes; read the "To fix" list
make quality-ship   # the ship report: every gate, must be green before handoff
```

### Front-end ↔ engine compatibility

The engine publishes a versioned IPC **contract** (`voicebridge.py contract` /
`GET /contract`) with a `schema_version`. Changes are **additive** within a
version — the front-end reads the fields it knows and treats missing ones as
absent, so `git pull` (which updates the engine) and a not-yet-rebuilt Raycast
bundle keep working. A backward-incompatible change bumps `schema_version`,
and the front-end warns when the engine's major version differs from the one
it was built against.

## Not yet

- **Audio pre-trim / VAD** — lower value now that the streaming transcriber
  already cuts chunks at silences.
- **Standalone `.app` packaging** — a real repackaging effort (everything
  currently assumes the repo layout + venv), not a quick step.

(Live partial transcripts already ship in the Raycast Dictate view — see
[Speed](#speed-warm-engine); a settings GUI and warm always-on engine also ship.)
The pipeline is composable stages, so adding the rest is incremental.
