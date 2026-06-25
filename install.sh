#!/usr/bin/env bash
# Alfred installer — sets up the Python environment and checks prerequisites.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Alfred install"
echo "==================="
echo "Folder: $DIR"
echo

# --- 1. Apple Silicon check ------------------------------------------------
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "WARNING: this Mac is not Apple Silicon (arm64). mlx-whisper requires it."
  echo "         You can still use the pipeline with a different STT engine, but"
  echo "         the default (mlx-whisper) will not run here."
  echo
fi

# --- 2. Python -------------------------------------------------------------
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "ERROR: python3 not found. Install it (e.g. 'brew install python')." >&2
  exit 1
fi
PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
echo "Using python3 $PYV at $PY"
if [[ "$("$PY" -c 'import sys;print(1 if sys.version_info>=(3,11) else 0)')" != "1" ]]; then
  echo "NOTE: Python 3.11+ is recommended (needed to read config.toml)."
fi

# --- 3. Virtual environment + deps ----------------------------------------
echo "Creating virtual environment in .venv ..."
"$PY" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
echo "Installing dependencies (this downloads MLX; may take a minute) ..."
pip install -r requirements.txt

# --- 4. sox (recorder) -----------------------------------------------------
if command -v sox >/dev/null 2>&1; then
  echo "sox found: $(command -v sox)"
elif command -v brew >/dev/null 2>&1; then
  echo "Installing sox (recorder) via Homebrew ..."
  brew install sox
else
  echo "sox is not installed and Homebrew was not found."
  echo "Install Homebrew (https://brew.sh), then run: brew install sox"
fi

# --- 5. Hammerspoon (hotkeys) + config wiring ------------------------------
if [[ -d "/Applications/Hammerspoon.app" ]]; then
  echo "Hammerspoon found: /Applications/Hammerspoon.app"
elif command -v brew >/dev/null 2>&1; then
  echo "Installing Hammerspoon (global hotkeys + menu bar) via Homebrew ..."
  brew install --cask hammerspoon
else
  echo "Hammerspoon is not installed and Homebrew was not found."
  echo "Install Homebrew (https://brew.sh), then run: brew install --cask hammerspoon"
fi

# Load the Alfred front-end from Hammerspoon's config (idempotent).
HS_INIT="$HOME/.hammerspoon/init.lua"
LOAD_LINE="dofile(\"$DIR/voicebridge.lua\")  -- Alfred"
mkdir -p "$(dirname "$HS_INIT")"
if [[ -f "$HS_INIT" ]] && grep -qF "voicebridge.lua" "$HS_INIT"; then
  echo "Hammerspoon config already loads Alfred: $HS_INIT"
else
  printf '%s\n' "$LOAD_LINE" >> "$HS_INIT"
  echo "Added Alfred loader to $HS_INIT"
fi

# --- 6. config -------------------------------------------------------------
CFG="$HOME/.config/voicebridge/config.toml"
if [[ ! -f "$CFG" ]]; then
  mkdir -p "$(dirname "$CFG")"
  cp config.example.toml "$CFG"
  echo "Wrote starter config: $CFG"
else
  echo "Existing config kept: $CFG"
fi

# --- 7. doctor -------------------------------------------------------------
echo
echo "Running environment check ..."
echo
./.venv/bin/python voicebridge.py doctor || true

cat <<EOF

------------------------------------------------------------------
Next steps:
  1. Pick how the cleanup/translate step runs:
       • local  (default) — strict on-device MLX model, no login, no network,
         \$0. mlx-lm was just installed; the model (~2GB) downloads on first use.
       • claude / codex — higher quality, keyless via your CLI login (one-time):
            claude        # then type: /login        (Claude Code)
            codex login   # Sign in with ChatGPT     (Codex)
     Set "backend" in the config (step below). Raw transcription needs neither.

  2. Turn on the global hotkeys (Hammerspoon is installed and wired up above):
        open -a Hammerspoon
     Grant Accessibility + Microphone when macOS prompts, then choose
     "Reload Config" from the Hammerspoon menu.

  3. Test the engine directly (no hotkey needed):
        ./.venv/bin/python voicebridge.py text "hello   world" --rewrite --stdout

  Hotkeys:  Cmd+Option+D = dictate (press again to stop)   Cmd+Option+T = type
------------------------------------------------------------------
EOF
