#!/usr/bin/env bash
# Alfred — Raycast extension installer.
# Installs deps, builds, and imports the extension into Raycast as a permanent
# local install (it keeps running at full speed after this script exits; the
# "Development" label is just a category). Re-running it is safe (idempotent).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "Alfred Raycast extension install"
echo "================================"
echo "Folder: $DIR"
echo

# --- 1. Node ---------------------------------------------------------------
if ! command -v node >/dev/null 2>&1; then
  echo "ERROR: node not found. Install Node 22+ (e.g. 'brew install node')." >&2
  exit 1
fi
echo "Using node $(node -v) / npm $(npm -v)"

# --- 2. Dependencies -------------------------------------------------------
echo "Installing dependencies (npm install) ..."
npm install

# --- 3. Build (validates the manifest + TypeScript) ------------------------
echo "Building ..."
npm run build

# --- 4. Import into Raycast ------------------------------------------------
if [[ ! -d "/Applications/Raycast.app" ]]; then
  echo
  echo "Raycast is not installed (https://raycast.com). Built successfully, but"
  echo "skipping import. Install Raycast, then re-run this script."
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
Installed. Open Raycast and search "Alfred". Commands:
  Dictate (Toggle) · Transform Text · Type & Process · History
  Alfred Menu Bar · Engine Status

Next:
  1. Run "Engine Status" once to confirm the engine is reachable.
  2. Assign a hotkey to "Dictate" (Raycast: select it -> Cmd+K ->
     Configure Command -> Hotkey). Avoid Cmd+Opt+D/I/T/V if you also
     run the Hammerspoon front-end.
  3. First dictation prompts for Microphone access for Raycast — allow it.

The extension shows under Raycast Settings -> Extensions as a
"Development" extension and stays tied to this folder. If you edit its
code later, re-run this script (or 'npm run dev') to rebuild.
------------------------------------------------------------------
EOF
