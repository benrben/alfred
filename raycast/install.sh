#!/usr/bin/env bash
# Alfred — Raycast extension installer (0 → 100).
#
# Does EVERYTHING from scratch so Dictate just works afterwards:
#   1. the engine: Python venv beside voicebridge.py + Whisper deps
#      (mlx-whisper, soundfile, numpy) — the venv the extension auto-resolves
#   2. sox (the recorder Dictate shells out to)
#   3. a starter config.toml (if you don't have one)
#   4. the Raycast extension: npm deps, build, and import into Raycast
#
# Re-running it is safe (idempotent). It does NOT install Hammerspoon — this is
# the Raycast front-end; run ../install.sh if you also want the Hammerspoon one.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"   # engine repo root (voicebridge.py lives here)
cd "$DIR"

echo "Alfred Raycast install (engine + extension)"
echo "==========================================="
echo "Extension: $DIR"
echo "Engine:    $ROOT"
echo

# --- 1. Apple Silicon check -----------------------------------------------
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "WARNING: this Mac is not Apple Silicon (arm64). mlx-whisper (the default"
  echo "         STT engine) requires it; transcription will not run here."
  echo
fi

# --- 2. Engine: Python venv + Whisper deps --------------------------------
# The extension resolves the engine python as: the 'Python (venv)' preference,
# else a .venv beside voicebridge.py, else bare python3. We create that .venv so
# it's found automatically — no preference to set.
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "ERROR: python3 not found. Install it (e.g. 'brew install python')." >&2
  exit 1
fi
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "Using python3 $PYV at $PY"

VENV="$ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "Creating engine venv in $VENV ..."
  "$PY" -m venv "$VENV"
else
  echo "Engine venv exists: $VENV"
fi
"$VENV/bin/python" -m pip install --upgrade pip >/dev/null
echo "Installing engine deps (mlx-whisper, soundfile, numpy) — downloads MLX/torch,"
echo "may take a few minutes the first time ..."
"$VENV/bin/pip" install -r "$ROOT/requirements.txt"

# --- 3. sox (recorder) -----------------------------------------------------
if command -v sox >/dev/null 2>&1; then
  echo "sox found: $(command -v sox)"
elif command -v brew >/dev/null 2>&1; then
  echo "Installing sox (recorder) via Homebrew ..."
  brew install sox
else
  echo "ERROR: sox is not installed and Homebrew was not found." >&2
  echo "       Install Homebrew (https://brew.sh), then: brew install sox" >&2
  exit 1
fi

# --- 4. config -------------------------------------------------------------
CFG="$HOME/.config/voicebridge/config.toml"
if [[ ! -f "$CFG" ]]; then
  mkdir -p "$(dirname "$CFG")"
  cp "$ROOT/config.example.toml" "$CFG"
  echo "Wrote starter config: $CFG"
else
  echo "Existing config kept: $CFG"
fi

# --- 5. Node ---------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found. Install Node 22+ (e.g. 'brew install node')." >&2
  exit 1
fi
echo "Using node $(node -v) / npm $(npm -v)"

# --- 6. Dependencies -------------------------------------------------------
echo "Installing extension dependencies (npm install) ..."
npm install

# --- 7. Build (validates the manifest + TypeScript) ------------------------
echo "Building ..."
npm run build

# --- 8. Engine check -------------------------------------------------------
echo
echo "Running engine check (doctor) ..."
echo
"$VENV/bin/python" "$ROOT/voicebridge.py" doctor || true
echo

# --- 9. Import into Raycast ------------------------------------------------
if [[ ! -d "/Applications/Raycast.app" ]]; then
  echo
  echo "Raycast is not installed (https://raycast.com). Engine + build are ready,"
  echo "but skipping import. Install Raycast, then re-run this script."
  exit 0
fi

if ! pgrep -x Raycast >/dev/null 2>&1; then
  echo "Launching Raycast ..."
  open -a Raycast
  sleep 3
fi

echo "Importing into Raycast (via 'ray develop') ..."
LOG="$(mktemp)"
npm run dev >"$LOG" 2>&1 &
DEV_PID=$!

# Wait (up to ~60s) for the build/import to report success.
ok=""
for _ in $(seq 1 60); do
  if grep -q "built extension successfully" "$LOG" 2>/dev/null; then
    ok=1
    break
  fi
  if ! kill -0 "$DEV_PID" 2>/dev/null; then
    break
  fi
  sleep 1
done

# Let Raycast register the commands, then stop the dev watcher. The extension
# persists in Raycast after the watcher exits.
sleep 2
kill "$DEV_PID" 2>/dev/null || true
pkill -f "ray develop" 2>/dev/null || true
wait "$DEV_PID" 2>/dev/null || true

if [[ -z "$ok" ]]; then
  echo "Import did not confirm success. Build/import output:" >&2
  cat "$LOG" >&2
  rm -f "$LOG"
  exit 1
fi
rm -f "$LOG"

cat <<'EOF'

------------------------------------------------------------------
Installed (engine + extension). Open Raycast and search "Alfred":
  Dictate · Transform Text · Type & Process · Manage Intents
  History · Alfred Menu Bar · Engine Status

Next:
  1. Run "Engine Status" once to confirm the engine is reachable.
     (The daemon warms the Whisper model on first use — first capture
     may take ~30s while it downloads/loads the model.)
  2. Assign a hotkey to "Dictate" (Raycast: select it -> Cmd+K ->
     Configure Command -> Hotkey). Avoid Cmd+Opt+D/I/T/V if you also
     run the Hammerspoon front-end.
  3. First dictation prompts for Microphone access for Raycast — allow it.

The extension shows under Raycast Settings -> Extensions as a
"Development" extension and stays tied to this folder. If you edit its
code later, re-run this script (or 'npm run dev') to rebuild.
------------------------------------------------------------------
EOF
