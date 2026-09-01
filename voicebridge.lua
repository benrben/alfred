-- Alfred — Hammerspoon front-end
-- Global hotkeys + audio recording + menu-bar + typed input, driving the
-- Python engine (voicebridge.py).
--
-- Install: put this file next to voicebridge.py, then add to ~/.hammerspoon/init.lua:
--     dofile(os.getenv("HOME") .. "/Claude/Projects/alfred/voicebridge.lua")
-- and reload Hammerspoon's config.
--
-- Hotkeys (defaults): Dictate = Cmd+Option+D (press to start, press again to stop)
--                     Intent  = Cmd+Option+I (dictate, then pick a format/intent)
--                     Type    = Cmd+Option+T (typed-input box -> same pipeline)
--                     Raw     = Cmd+Option+R (transcribe only, no LLM)
-- Switch LLM backend from the menu-bar (Backend ▸). Edit/add intent formats in
-- config.toml under [intent]; "Reload intent modes" refreshes the picker.
--
-- UI: a floating recording HUD (timer + live mic-level meter), a mode picker
-- (email/message/commit/…), and a result panel with Copy/Paste/Email actions.

-- ============================ USER CONFIG ============================
local HOME   = os.getenv("HOME")
local DIR    = HOME .. "/Claude/Projects/alfred"   -- folder holding voicebridge.py
local PYTHON = DIR .. "/.venv/bin/python3"         -- created by install.sh
local SCRIPT = DIR .. "/voicebridge.py"
local SOX    = "/opt/homebrew/bin/sox"             -- output of `which sox`

local DICTATE_HOTKEY  = { mods = { "cmd", "option" }, key = "d" }   -- dictate, no intent
local INTENT_HOTKEY   = { mods = { "cmd", "option" }, key = "i" }   -- dictate + pick format
local TYPE_HOTKEY     = { mods = { "cmd", "option" }, key = "t" }   -- type + pick format
local WINDOW_HOTKEY   = { mods = { "cmd", "option" }, key = "v" }   -- open the app window
local RAWTX_HOTKEY    = { mods = { "cmd", "option" }, key = "r" }   -- transcribe only (no LLM)
local SHOW_METER      = true                         -- live mic-level bar in the HUD
local DAEMON_PORT     = 8763                          -- warm background engine (localhost)
-- Hard cap on a single recording (seconds). Generous (60 min) for long
-- notes/meetings; the cap only stops a forgotten recorder from running forever
-- and filling the disk. Applied as a sox `trim 0 <secs>` effect. Mirrors the
-- Raycast front-end's MAX_RECORD_SECS.
local MAX_RECORD_SECS = 3600
-- ====================================================================

-- Clean up a previous load (so config reloads don't stack UI/hotkeys/timers).
if _G.voicebridge then
  local old = _G.voicebridge
  if old.menubar then old.menubar:delete() end
  for _, h in ipairs(old.hotkeys or {}) do h:delete() end
  if old.hudTimer then old.hudTimer:stop() end
  if old.hud then old.hud:delete() end
  if old.resultTimer then old.resultTimer:stop() end
  if old.result then old.result:delete() end
  if old.win then old.win:delete() end
end

local VB = {
  state = "idle", recTask = nil, wav = nil, hotkeys = {},
  hud = nil, hudTimer = nil, recStart = 0, level = 0, pulse = 0,
  captureFlags = nil,                 -- extra engine args for the next run only
  backend = nil,                      -- nil = config default; "auto"|"claude"|"codex"
  result = nil, resultTimer = nil, resultText = "",
  win = nil, winModeFlags = {}, winTranslate = true,   -- full app window state
}
_G.voicebridge = VB

local ICONS = { idle = "🎙️", recording = "🔴", processing = "⏳" }

-- The selectable LLM backends (besides "Default = use config"). ONE source
-- feeding both the menubar submenu and the window's <select>; "local" is the
-- on-device MLX model — and the engine's DEFAULT — which the earlier hard-coded
-- lists both dropped. Matches voicebridge.py's `--backend` choices.
local BACKENDS = { "auto", "claude", "codex", "local" }

-- Pure: the menubar glyph for a state, falling back to the idle mic.
local function iconForState(s) return ICONS[s] or ICONS.idle end

-- Output formats offered by the mode picker. The catalog (Email/Commit/your
-- custom [intent] modes…) is loaded from the engine via `voicebridge.py modes`
-- (see refreshModes); these two pseudo-entries always bracket it.
-- `--mode X --rewrite` enables rewrite even for the "raw" cleanup mode.
local MODE_DEFAULT = { text = "Default (use config)", subText = "Your configured mode", flags = {} }
local MODE_RAWTX   = { text = "Raw transcript", subText = "No LLM at all",
                       flags = { "--no-rewrite", "--no-translate", "--no-optimize" } }

-- Fallback catalog if the engine call fails; refreshModes() replaces it.
local BUILTIN_CATALOG = {
  { key = "email",   label = "Email",        description = "Polished email" },
  { key = "message", label = "Message",      description = "Casual chat / DM" },
  { key = "commit",  label = "Commit",       description = "Git commit message" },
  { key = "prompt",  label = "Prompt",       description = "Prompt for an AI" },
  { key = "notes",   label = "Notes",        description = "Clean notes / bullets" },
  { key = "raw",     label = "Cleanup only", description = "Tidy wording, keep structure", default = true },
}

local function buildModes(catalog)
  -- The config's current mode is flagged `default`; the Default picker entry
  -- borrows its key/prompt so it's editable too (editing it edits that mode).
  local defKey, defLabel, defPrompt
  for _, m in ipairs(catalog) do
    if m.default then defKey = m.key; defLabel = m.label or m.key; defPrompt = m.prompt or "" end
  end
  local list = { {
    text = defKey and ("Default · " .. defLabel) or "Default (use config)",
    subText = "Your configured mode", key = defKey, prompt = defPrompt or "",
    flags = {},   -- Default = no per-capture flags (uses config)
  } }
  for _, m in ipairs(catalog) do
    list[#list + 1] = {
      text = m.label or m.key, subText = m.description or "",
      key = m.key, prompt = m.prompt or "",
      flags = { "--mode", m.key, "--rewrite" },
    }
  end
  list[#list + 1] = MODE_RAWTX
  return list
end

local MODES = buildModes(BUILTIN_CATALOG)   -- refreshed from the engine at load

-- The mode list as JS objects {label, prompt, key} for the window picker.
local function modesForJS()
  local out = {}
  for _, m in ipairs(MODES) do
    out[#out + 1] = { label = m.text, prompt = m.prompt or "", key = m.key or "" }
  end
  return out
end

-- Full login PATH so the engine (and the claude/codex it spawns) is found.
-- Append the stock system dirs so pbcopy/osascript and Homebrew tools resolve
-- even if the login shell trimmed PATH.
-- The only top-level `hs.*` that runs before the test-export seam below, so it
-- is short-circuited under VB_LUA_TEST to keep the file loadable by plain lua.
local USER_PATH = ((not os.getenv("VB_LUA_TEST") and hs.execute('echo -n "$PATH"', true)) or ""):gsub("%s+$", "")
USER_PATH = USER_PATH .. ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
  .. ":" .. HOME .. "/.local/bin:" .. HOME .. "/.cargo/bin:" .. HOME .. "/.bun/bin"
-- Force a UTF-8 locale so the engine doesn't fall back to mac-roman (which
-- mangles curly quotes / em dashes / Hebrew) when launched from the GUI.
local TASK_ENV  = { PATH = USER_PATH, HOME = HOME,
                    LANG = "en_US.UTF-8", LC_ALL = "en_US.UTF-8", PYTHONUTF8 = "1" }
-- DAEMON_PORT / DAEMON_URL default to the literals above; applyContract() may
-- rewrite them from the engine's contract at load (so the port lives in one
-- place, the engine's CONTRACT).
local DAEMON_URL = "http://127.0.0.1:" .. DAEMON_PORT .. "/"

-- Shared shell env prefix for every synchronous hs.execute() that shells the
-- engine (startDaemon / the contract fetch): the same PATH/HOME/UTF-8 locale as
-- TASK_ENV, so a GUI-launched engine behaves exactly like the CLI one.
local ENV_PREFIX = "PATH='" .. USER_PATH .. "' HOME='" .. HOME ..
  "' LANG='en_US.UTF-8' LC_ALL='en_US.UTF-8' PYTHONUTF8=1 "
-- The engine's owner-only (0700) state dir. Our debug trace + last-capture dump
-- (AND the daemon's own stdout/stderr log, below) live here — NOT in
-- world-readable /tmp — because every one of them can contain verbatim
-- dictation (the daemon logs "transcript (…): <first 120 chars>" per capture).
-- Created on demand by ensureVBDir(); the engine also creates it.
local VB_DIR        = HOME .. "/.voicebridge"
local LOG_FILE      = VB_DIR .. "/voicebridge.log"
local DUMP_FILE     = VB_DIR .. "/voicebridge_last.txt"
local DAEMON_LOG    = VB_DIR .. "/daemon.log"

pcall(require, "hs.ipc")   -- enables the `hs` CLI for introspection

-- Forward declarations (several of these reference each other).
local setState, notify, tmpWav, fmtTime, dbg
local showHUD, updateHUD, destroyHUD, soxStream
local parseStatus, onResult, runEngine, refreshModes, resultPanelHandlers
local resolveConfigPath
local onRecDone, startRecording, stopRecording, toggleDictate, toggleTranscribeOnly
local pickMode, dictateWithMode, typePrompt
local closeResult, resultClick, showResult
local openWindow, closeWindow, onWebMessage, pushWindowState, updateResult
local windowToggleRecord, windowCaptureFlags, readHistory, saveIntent
local startDaemon, ensureDaemon, restartDaemon, runEngineOneShot
local refreshSettings, setModel

-- ---- Small helpers -------------------------------------------------------

function fmtTime(secs)
  secs = math.max(0, tonumber(secs) or 0)
  return string.format("%02d:%02d", math.floor(secs / 60), secs % 60)
end

-- Ensure the owner-only ~/.voicebridge dir exists before we write into it (the
-- engine creates it 0700 too; we chmod best-effort in case we win the race).
local function ensureVBDir()
  if not hs.fs.attributes(VB_DIR) then
    hs.fs.mkdir(VB_DIR)
  end
  -- Tighten an existing directory too (restores/older installs may be 0755).
  pcall(function() hs.fs.chmod(VB_DIR, 448) end)     -- 0700; no-op if unsupported
end

-- Off by default: the trace can contain dictation excerpts. Set true to write a
-- trace to ~/.voicebridge/voicebridge.log (owner-only), not world-readable /tmp.
local DEBUG = false
function dbg(msg)
  if not DEBUG then return end
  ensureVBDir()
  local fh = io.open(LOG_FILE, "a")
  if fh then fh:write(os.date("%H:%M:%S ") .. tostring(msg) .. "\n"); fh:close() end
end

-- JSON-encode a single string for embedding in JS (hs.json.encode wants a table).
local function jsStr(s)
  return '"' .. tostring(s or "")
    :gsub("\\", "\\\\"):gsub('"', '\\"')
    :gsub("\n", "\\n"):gsub("\r", "\\r"):gsub("\t", "\\t") .. '"'
end

function setState(s)
  VB.state = s
  if VB.menubar then VB.menubar:setTitle(iconForState(s)) end
  pushWindowState()
end

function notify(title, text, onClick)
  local attrs = { title = title, informativeText = text or "" }
  local n = onClick and hs.notify.new(onClick, attrs) or hs.notify.new(attrs)
  n:send()
end

local wavSeq = 0
function tmpWav()
  local tmp = os.getenv("TMPDIR") or "/tmp/"
  wavSeq = wavSeq + 1
  return tmp .. "voicebridge_" .. os.time() .. "_" .. wavSeq .. ".wav"
end

-- Paste `text` into the frontmost app: put it on the clipboard, then (after a
-- short delay so focus settles) synthesize Cmd-V. Shared by the toast panel and
-- the app window so the sequence lives in exactly one place.
local function pasteText(text, delay)
  hs.pasteboard.setContents(text or "")
  hs.timer.doAfter(delay or 0.08, function() hs.eventtap.keyStroke({ "cmd" }, "v") end)
end

-- ---- Recording HUD (floating canvas) -------------------------------------

local HUD_W = 280
local HUD_H_BASE = 44                 -- dot + REC + timer; +26 when the meter shows
local MET_X, MET_Y, MET_H = 16, 46, 9
local MET_MAXW = HUD_W - MET_X * 2
-- element indices inside the HUD canvas: 2=dot, 4=timer, 6=meter-fill (if shown)

function showHUD()
  local h = HUD_H_BASE + (SHOW_METER and 26 or 0)
  local f = hs.screen.mainScreen():frame()
  local c = hs.canvas.new({
    x = f.x + (f.w - HUD_W) / 2, y = f.y + 90, w = HUD_W, h = h,
  })
  c:level(hs.canvas.windowLevels.overlay)
  c:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces)
  c:clickActivating(false)
  c:appendElements(
    -- no frame => the background rectangle fills the whole canvas
    { type = "rectangle", action = "fill",
      roundedRectRadii = { xRadius = 14, yRadius = 14 },
      fillColor = { red = 0.07, green = 0.07, blue = 0.08, alpha = 0.9 } },
    { type = "circle", action = "fill", center = { x = 24, y = 24 }, radius = 7,
      fillColor = { red = 0.96, green = 0.26, blue = 0.21, alpha = 1 } },
    { type = "text", text = "REC", textColor = { white = 1, alpha = 0.85 },
      textSize = 13, frame = { x = 40, y = 14, w = 80, h = 20 } },
    { type = "text", text = "00:00", textColor = { white = 1, alpha = 0.95 },
      textSize = 16, textAlignment = "right",
      frame = { x = HUD_W - 96, y = 12, w = 80, h = 24 } }
  )
  if SHOW_METER then
    c:appendElements(
      { type = "rectangle", action = "fill",
        roundedRectRadii = { xRadius = 3, yRadius = 3 },
        fillColor = { white = 1, alpha = 0.12 },
        frame = { x = MET_X, y = MET_Y, w = MET_MAXW, h = MET_H } },
      { type = "rectangle", action = "fill",
        roundedRectRadii = { xRadius = 3, yRadius = 3 },
        fillColor = { red = 0.22, green = 0.85, blue = 0.42, alpha = 0.95 },
        frame = { x = MET_X, y = MET_Y, w = 0, h = MET_H } }
    )
  end
  c:show()
  VB.hud = c
end

function updateHUD()
  pushWindowState()
  if not VB.hud then return end
  local tstr = fmtTime(os.time() - VB.recStart)
  VB.hud[4].text = tstr
  if VB.menubar then VB.menubar:setTitle("🔴 " .. tstr) end

  -- pulse the record dot
  VB.pulse = VB.pulse + 0.18
  local a = 0.55 + 0.45 * math.abs(math.sin(VB.pulse))
  VB.hud[2].fillColor = { red = 0.96, green = 0.26, blue = 0.21, alpha = a }

  -- level meter: decay toward 0, then draw the latest peak
  if SHOW_METER and VB.hud[6] then
    VB.level = (VB.level or 0) * 0.72
    local w = math.min(1, VB.level * 1.7) * MET_MAXW
    VB.hud[6].frame = { x = MET_X, y = MET_Y, w = w, h = MET_H }
    VB.hud[6].fillColor = (VB.level > 0.55)
      and { red = 0.95, green = 0.55, blue = 0.20, alpha = 0.95 }
      or  { red = 0.22, green = 0.85, blue = 0.42, alpha = 0.95 }
  end
end

function destroyHUD()
  if VB.hudTimer then VB.hudTimer:stop(); VB.hudTimer = nil end
  if VB.hud then VB.hud:delete(); VB.hud = nil end
  VB.level = 0; VB.pulse = 0
end

-- sox prints a progress line to stderr (with `-S`); the VU meter is the
-- bracketed segment containing a '|'. We turn its "fill" into a 0..1 level.
-- Pure: scan a stderr blob and return the PEAK level (0..1) across its meter
-- segments, or nil when no meter is present. fill = non-space, non-'|' chars
-- over the total width of the bracketed segment.
local function soxLevel(stdErr)
  local peak = nil
  for seg in (stdErr or ""):gmatch("[^\r\n]+") do
    local meter = seg:match("%[([^%[%]]-|[^%[%]]-)%]")
    if meter then
      local fill, total = 0, 0
      for ch in meter:gmatch(".") do
        total = total + 1
        if ch ~= " " and ch ~= "|" then fill = fill + 1 end
      end
      if total > 0 then
        local lvl = fill / total
        if not peak or lvl > peak then peak = lvl end
      end
    end
  end
  return peak
end

function soxStream(_, _, stdErr)
  local lvl = soxLevel(stdErr)
  if lvl then VB.level = math.max(VB.level or 0, lvl) end
  return true
end

-- ---- Engine plumbing -----------------------------------------------------

-- The status-line grammar: sentinel, field separator, and the suffix literals.
-- Defaults mirror voicebridge.py's CONTRACT; applyContract() overrides them from
-- the live engine at load so the two can't silently drift. Read at CALL time by
-- parseStatus/classifyResult, so the load-time override takes effect everywhere.
local STATUS_SENTINEL = "VB_STATUS"
local STATUS_SEP      = "\t"
local RESULT_SENTINEL = "VB_RESULT"
local LLM_FAILED      = "llm_failed"
local PASTE_FAILED    = "paste_failed"
local DAEMON_SCHEMA_VERSION = 1

-- The health response is an identity handshake, not merely an HTTP status.
-- A different localhost service must never receive dictated text or argv.
local function isAlfredIdentity(identity)
  return type(identity) == "table"
    and identity.ok == true
    and identity.app == "alfred"
    and identity.schema_version == DAEMON_SCHEMA_VERSION
    and type(identity.pid) == "number"
    and identity.pid > 0 and identity.pid % 1 == 0
end

local function daemonPidFromInfo(content, decode)
  local ok, info = pcall(decode, content or "")
  if ok and isAlfredIdentity(info) and type(info.port) == "number" then
    return math.floor(info.pid)
  end
  return nil
end

-- Parse the engine's machine-readable status line into its tab-separated parts:
-- "VB_STATUS\tkind[\textra...]" -> { kind, extra... }; nil when none is present.
function parseStatus(out)
  for line in (out or ""):gmatch("[^\r\n]+") do
    local rest = line:match("^" .. STATUS_SENTINEL .. STATUS_SEP .. "(.+)$")
    if rest then
      local parts = {}
      for p in (rest .. STATUS_SEP):gmatch("(.-)" .. STATUS_SEP) do parts[#parts + 1] = p end
      return parts
    end
  end
  return nil
end

-- Pure: pull the JSON-encoded VB_RESULT payload (the text the engine actually
-- delivered) from stdout, still ENCODED. json.dumps escapes newlines, so it is
-- always a single physical line. Returns nil when the engine emitted no result
-- line (older engine / the --stdout path) — the caller then falls back to the
-- clipboard. The caller json-decodes (decoder injected to stay hs-free / testable).
local function resultPayload(out)
  for line in (out or ""):gmatch("[^\r\n]+") do
    local enc = line:match("^" .. RESULT_SENTINEL .. STATUS_SEP .. "(.+)$")
    if enc then return enc end
  end
  return nil
end

-- Pure: map an engine error subtype (+ optional stderr tail) to a human message.
local ERROR_MESSAGES = {
  audio_not_found = "No audio to transcribe.",
  stt_failed      = "Transcription failed.",
  llm_failed      = "The rewrite step failed.",
  -- The clipboard/paste/save-to-file step itself failed (e.g. an unwritable
  -- save_dir). The text isn't lost — the engine keeps it in history first.
  deliver_failed  = "Couldn't deliver the result (see History — it was saved there).",
  runtime         = "The engine hit an error.",
}
local function errorMessage(subtype, tail)
  local base = ERROR_MESSAGES[subtype] or "Something went wrong."
  if tail and #tail > 0 then return base .. " (" .. tail .. ")" end
  return base
end

-- Pure: the "copied" banner text, reused by the toast panel, the window's
-- notify fallback, and the plain notification (was duplicated 3× with drift).
local function resultBanner(llmFailed)
  return llmFailed and "Copied raw transcript (LLM step failed)"
                   or  "Copied to clipboard ✓"
end

-- Pure: classify a finished engine run into everything the UI needs, hs-free so
-- it is unit-testable. `decode` json-decodes the VB_RESULT payload (nil when the
-- caller has no decoder or the engine emitted no result line -> fall back to the
-- clipboard). Fields:
--   kind        — VB_STATUS kind (copied/saved/empty/error/streaming) or nil
--   path        — the saved-file path (kind == "saved")
--   subtype     — the error subtype (kind == "error"): audio_not_found/…/runtime
--   llmFailed   — LLM step failed but the raw transcript was still delivered
--   pasteFailed — auto-paste couldn't be delivered (missing Accessibility)
--   tail        — last non-blank line of stderr (the real error detail)
--   text        — the delivered text from VB_RESULT (nil when absent)
local function classifyResult(code, out, err, decode)
  local parts = parseStatus(out) or {}
  local kind = parts[1]
  local pasteFailed = false
  for _, p in ipairs(parts) do if p == PASTE_FAILED then pasteFailed = true end end
  local text
  local enc = resultPayload(out)
  if enc and decode then
    local ok, dec = pcall(decode, enc)
    if ok and type(dec) == "string" then text = dec end
  end
  return {
    kind = kind,
    path = (kind == "saved") and parts[2] or nil,
    subtype = (kind == "error") and parts[2] or nil,
    llmFailed = parts[#parts] == LLM_FAILED,
    pasteFailed = pasteFailed,
    tail = (err or ""):gsub("%s+$", ""):match("[^\r\n]+$"),
    text = text,
  }
end

-- Processing watchdog: if a run neither delivers nor connection-fails within the
-- bound (a wedged engine / a lost callback), terminate any one-shot task and
-- reset to idle so the UI can't get stuck on ⏳ forever. Tied to VB.runId so a
-- newer capture's watchdog supersedes an older one.
local function cancelWatchdog()
  if VB.watchdog then VB.watchdog:stop(); VB.watchdog = nil end
end
local function startWatchdog(runId)
  cancelWatchdog()
  VB.watchdog = hs.timer.doAfter(150, function()
    VB.watchdog = nil
    if VB.state == "processing" and VB.runId == runId then
      if VB.engineTask then pcall(function() VB.engineTask:terminate() end); VB.engineTask = nil end
      setState("idle")
      notify("Alfred", "Timed out waiting for the engine.")
    end
  end)
end

-- Thin shell over classifyResult: dispatch the finished run to the UI. `err` is
-- the daemon's captured stderr (the actual failure detail); we prefer the exact
-- delivered text from VB_RESULT and only read the clipboard as a fallback.
function onResult(code, out, err)
  cancelWatchdog()
  setState("idle")
  VB.engineTask = nil   -- release the retained task now that it has finished
  if DEBUG then         -- verbatim capture dump: owner-only + opt-in only
    ensureVBDir()
    local fh = io.open(DUMP_FILE, "w")
    if fh then
      fh:write("code=" .. tostring(code) .. "\n--- STDOUT ---\n" .. (out or "") ..
               "\n--- STDERR ---\n" .. (err or ""))
      fh:close()
    end
  end
  local r = classifyResult(code, out, err, hs.json.decode)
  dbg("onResult code=" .. tostring(code) .. " kind=" .. tostring(r.kind) ..
      " sub=" .. tostring(r.subtype) .. " err=[" ..
      ((err or ""):gsub("\n", "\\n")):sub(1, 200) .. "]")

  if r.kind == "copied" then
    -- Prefer the engine's exact delivered text (VB_RESULT); the clipboard is a
    -- fallback for older engines that don't emit the result line.
    local txt = r.text or hs.pasteboard.getContents() or ""
    local ok, e = pcall(updateResult, txt, r.llmFailed)
    if not ok then
      dbg("updateResult ERROR: " .. tostring(e))
      notify("Alfred", resultBanner(r.llmFailed))
    end
    if r.pasteFailed then
      notify("Alfred", "Auto-paste failed — granted Accessibility to Hammerspoon?")
    end
  elseif r.kind == "saved" then
    local path = r.path
    notify("Alfred", "Too long — saved to file (click to reveal)",
      function() if path then hs.execute("open -R '" .. path .. "'") end end)
  elseif r.kind == "empty" then
    notify("Alfred", "No speech detected.")
  elseif r.kind == "error" then
    notify("Alfred", errorMessage(r.subtype, r.tail))
  else
    -- No VB_STATUS line at all (a crash before the sentinel, or a foreign
    -- response): surface whatever stderr detail we have.
    notify("Alfred", "Error: " .. (r.tail or
      "see the engine log (~/.voicebridge/daemon.log)"))
  end
end

-- Pure: the detached-launch shell command for the warm daemon, redirecting its
-- stdout/stderr into `logFile`. Extracted so a test can assert that file is
-- NEVER under /tmp: the daemon logs "transcript (…): <first 120 chars>" on
-- every capture, so a world-readable /tmp log would leak verbatim dictation to
-- any other local account. logFile must be under the owner-only (0700)
-- ~/.voicebridge (ensureVBDir()) — sufficient on its own, since no other user
-- can open a path through a 0700 directory regardless of the file's own mode.
local function buildStartDaemonCmd(envPrefix, python, script, port, logFile)
  return envPrefix .. "nohup '" .. python .. "' '" .. script ..
    "' serve --port " .. port .. " >'" .. logFile .. "' 2>&1 &"
end

-- Launch the warm engine daemon, detached so it survives Hammerspoon reloads
-- (keeping the Whisper model resident). Re-launching when one already runs is
-- harmless: the new process finds the port busy and exits.
function startDaemon()
  ensureVBDir()
  hs.execute(buildStartDaemonCmd(ENV_PREFIX, PYTHON, SCRIPT, DAEMON_PORT, DAEMON_LOG))
  dbg("startDaemon: launched detached on :" .. DAEMON_PORT)
end

function ensureDaemon()
  hs.http.asyncGet(DAEMON_URL, nil, function(status, body)
    local ok, identity = pcall(hs.json.decode, body or "")
    if status == -1004 then
      startDaemon()
    elseif status == 200 and ok and isAlfredIdentity(identity) then
      return
    elseif status == 200 then
      dbg("ensureDaemon: port is occupied by a non-Alfred service")
      notify("Alfred", "Port " .. DAEMON_PORT .. " is not an Alfred engine.")
    end
  end)
end

function restartDaemon()
  local resolved = VB.contract and type(VB.contract.resolved) == "table"
                   and VB.contract.resolved.daemon_info
  local f = io.open(resolved or (VB_DIR .. "/daemon.json"), "r")
  local content = f and f:read("*a") or ""
  if f then f:close() end
  local pid = daemonPidFromInfo(content, hs.json.decode)
  if pid then
    hs.execute("/bin/kill -TERM " .. tostring(pid) .. " 2>/dev/null")
  else
    dbg("restartDaemon: no recorded Alfred daemon PID")
  end
  hs.alert.show("Restarting Alfred engine…", 1)
  hs.timer.doAfter(0.6, startDaemon)
end

-- One-shot fallback: spawn the engine as a fresh process (slow: reloads model).
function runEngineOneShot(cmd)
  local full = { SCRIPT }
  for _, a in ipairs(cmd) do full[#full + 1] = a end
  VB.engineTask = hs.task.new(PYTHON, onResult, full)   -- keep referenced (no GC kill)
  VB.engineTask:setEnvironment(TASK_ENV)
  if not VB.engineTask:start() then
    cancelWatchdog()
    setState("idle")
    notify("Alfred", "Could not launch the engine. Check PYTHON path in voicebridge.lua")
  end
end

-- Pure: assemble the engine argv from the base subcommand, the per-capture flags,
-- and the active backend. (python/script are prepended by the caller.)
local function buildEngineArgv(argv, captureFlags, backend)
  local cmd = {}
  for _, a in ipairs(argv or {}) do cmd[#cmd + 1] = a end
  for _, a in ipairs(captureFlags or {}) do cmd[#cmd + 1] = a end
  if backend then cmd[#cmd + 1] = "--backend"; cmd[#cmd + 1] = backend end
  return cmd
end

-- Pure: interpret an hs.http POST outcome to the daemon.
--   "ok"   — 200 with a body: decode + dispatch the result.
--   "down" — the daemon isn't listening (connection refused, -1004): the request
--            never reached the engine, so re-running it as a one-shot is safe.
--   "busy" — anything else, notably the ~60s asyncPost TIMEOUT while a long job
--            keeps working. Re-running would double-write history/clipboard, so
--            we must NOT: keep 'processing' and let the watchdog bound it.
-- Only a genuine connection-refused counts as "down"; a mid-flight reset/timeout
-- means the engine already owns the job.
local DAEMON_DOWN_STATUS = { [-1004] = true }
local function classifyPost(status, hasBody)
  if status == 200 and hasBody then return "ok" end
  if DAEMON_DOWN_STATUS[status] then return "down" end
  return "busy"
end

-- `argv` starts at the subcommand (e.g. {"process", wav}); no python/script.
function runEngine(argv, recordingFinished)
  if VB.state == "recording" or (VB.state == "processing" and not recordingFinished) then
    hs.alert.show("Busy…", 0.8)
    return false
  end
  setState("processing")
  local cmd = buildEngineArgv(argv, VB.captureFlags, VB.backend)
  VB.captureFlags = nil
  VB.runId = (VB.runId or 0) + 1     -- a late/duplicate result for an older run
  local myRun = VB.runId            -- is ignored (stale) by the closure below
  startWatchdog(myRun)
  dbg("runEngine[" .. myRun .. "]: " .. table.concat(cmd, " "))
  -- Prefer the warm daemon; fall back to a one-shot process ONLY when it is DOWN
  -- (see classifyPost): a >60s job would otherwise time the POST out at ~60s and
  -- get re-run, double-writing history/clipboard.
  local function postToDaemon()
    hs.http.asyncPost(DAEMON_URL, hs.json.encode({ argv = cmd }),
      { ["Content-Type"] = "application/json" },
      function(status, body)
      if myRun ~= VB.runId then
        dbg("runEngine[" .. myRun .. "]: stale result (cur=" .. tostring(VB.runId) .. ") — dropped")
        return
      end
      local outcome = classifyPost(status, body ~= nil and #body > 0)
      if outcome == "ok" then
        local ok, resp = pcall(hs.json.decode, body)
        if ok and type(resp) == "table" and isAlfredIdentity(resp.identity) then
          dbg("daemon result code=" .. tostring(resp.code))
          onResult(resp.code or 0, resp.out or "", resp.err or "")   -- thread err
          return
        end
        -- 200 but unreadable/foreign: don't re-run (the engine may have done
        -- the work, and replaying argv could duplicate delivery).
        cancelWatchdog(); setState("idle")
        dbg("daemon 200 but response identity was invalid")
        notify("Alfred", "The response was not from the Alfred engine.")
      elseif outcome == "down" then
        dbg("daemon down (status=" .. tostring(status) .. ") -> one-shot")
        runEngineOneShot(cmd)   -- watchdog stays armed to bound the one-shot
        ensureDaemon()          -- bring it up for next time
      else                      -- "busy": the daemon is still working (POST timeout)
        dbg("daemon busy (status=" .. tostring(status) .. ") -> keep processing, no re-run")
        notify("Alfred", "Still transcribing… the engine is taking a while.")
      end
      end)
  end
  -- Validate the listening service before sending argv/text. A foreign service
  -- returning HTTP 200 must not receive the dictated payload.
  hs.http.asyncGet(DAEMON_URL, nil, function(status, body)
    if myRun ~= VB.runId then return end
    local ok, identity = pcall(hs.json.decode, body or "")
    if status == 200 and ok and isAlfredIdentity(identity) then
      postToDaemon()
    elseif status == -1004 then
      runEngineOneShot(cmd)
      ensureDaemon()
    else
      cancelWatchdog(); setState("idle")
      notify("Alfred", "The configured daemon is not an Alfred engine.")
    end
  end)
  return true
end

-- The result panel's button actions, injected into showResult so the panel
-- stays a pure view. This is the only place that wires the panel back to the
-- engine (the "Email" reformat re-runs runEngine) — keeping that edge here, in
-- the engine client, instead of inside the panel itself.
function resultPanelHandlers()
  return {
    onCopy = function(text)
      hs.pasteboard.setContents(text)
      closeResult()
      hs.alert.show("Copied ✓", 0.6)
    end,
    onPaste = function(text)
      closeResult()
      pasteText(text)
    end,
    onEmail = function(text)
      if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
      closeResult()
      VB.captureFlags = { "--mode", "email" }
      runEngine({ "text", text })
    end,
    onDiscard = function() closeResult() end,
  }
end

-- Load the rewrite-mode catalog (built-in + custom [intent]) from the engine,
-- so the picker reflects config edits. Async; falls back to BUILTIN_CATALOG.
function refreshModes()
  local t = hs.task.new(PYTHON, function(code, out)
    if code == 0 and out and #out > 0 then
      local ok, parsed = pcall(hs.json.decode, out)
      if ok and type(parsed) == "table" and #parsed > 0 then
        MODES = buildModes(parsed)
        dbg("refreshModes: " .. #parsed .. " modes loaded")
        if VB.win then
          VB.win:evaluateJavaScript("window.vbModes&&vbModes(" .. hs.json.encode(modesForJS()) .. ",1)")
        end
        return
      end
    end
    dbg("refreshModes failed (code=" .. tostring(code) .. "); keeping fallback")
  end, { SCRIPT, "modes" })
  VB.modesTask = t          -- retain so GC doesn't kill it before it returns
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Fetch + decode the engine's CONTRACT once (fast: heavy libs are lazy-imported,
-- so `contract` just prints JSON). Returns the decoded table or nil. Cached in
-- VB.contract at load; applyContract() derives our port/paths/sentinels from it
-- so the front-end can't silently drift from the engine.
local function fetchContract()
  local out = hs.execute(ENV_PREFIX .. "'" .. PYTHON .. "' '" .. SCRIPT ..
    "' contract 2>/dev/null")
  if out and #out > 0 then
    local ok, c = pcall(hs.json.decode, out)
    if ok and type(c) == "table" then return c end
  end
  return nil
end

-- Derive our copies of the engine's constants from a decoded contract, KEEPING
-- the literal fallbacks where a field is missing (older engine). Touches only
-- primitives already defaulted above, so a partial contract degrades gracefully.
local function applyContract(c)
  if type(c.schema_version) == "number" then DAEMON_SCHEMA_VERSION = c.schema_version end
  local sl = type(c.status_line) == "table" and c.status_line or {}
  if type(sl.sentinel) == "string" then STATUS_SENTINEL = sl.sentinel end
  if type(sl.sep) == "string" then STATUS_SEP = sl.sep end
  if type(sl.result_sentinel) == "string" then RESULT_SENTINEL = sl.result_sentinel end
  if type(sl.llm_failed_suffix) == "string" then LLM_FAILED = sl.llm_failed_suffix end
  if type(sl.paste_failed_suffix) == "string" then PASTE_FAILED = sl.paste_failed_suffix end
  local d = type(c.daemon) == "table" and c.daemon or {}
  local host = type(d.host) == "string" and d.host or "127.0.0.1"
  if type(d.port) == "number" then DAEMON_PORT = math.floor(d.port) end
  DAEMON_URL = "http://" .. host .. ":" .. DAEMON_PORT .. "/"
end

-- Ask the engine where its config lives instead of hard-coding the path. The
-- cached contract carries `config_search` (the ordered list of paths the engine
-- consults); we open the first one (expanding a leading ~). Only if the contract
-- is unavailable do we fall back to the previous literal path.
function resolveConfigPath()
  local c = VB.contract or fetchContract()
  if c and type(c.config_search) == "table"
     and type(c.config_search[1]) == "string" and #c.config_search[1] > 0 then
    return (c.config_search[1]:gsub("^~", HOME))
  end
  dbg("resolveConfigPath: contract unavailable, using literal fallback")
  return HOME .. "/.config/voicebridge/config.toml"
end

-- Open the user's config in the default editor, falling back to the shipped
-- example when it doesn't exist yet. Shared by the window and the menubar.
local function openConfig()
  hs.execute("open -t '" .. resolveConfigPath() .. "' 2>/dev/null || open -t '"
    .. DIR .. "/config.example.toml'")
end

-- ---- Dictation -----------------------------------------------------------

function onRecDone()
  destroyHUD()
  dbg("onRecDone state=" .. tostring(VB.state) .. " wav=" .. tostring(VB.wav))
  VB.recTask = nil
  if VB.cancelRecording then
    VB.cancelRecording = nil
    if VB.wav then os.remove(VB.wav) end
    VB.wav = nil
    VB.captureFlags = nil
    setState("idle")
    return
  end
  -- Sox ended by itself at MAX_RECORD_SECS. Treat that as a normal completed
  -- recording instead of discarding the finalized WAV in a stuck UI state.
  if VB.state == "recording" then
    setState("processing")
    notify("Alfred", "Maximum recording length reached — transcribing now.")
  end
  -- sox exited (after we sent SIGINT). Process if the file has audio.
  if VB.state ~= "processing" then return end
  local f = io.open(VB.wav, "r")
  local size = 0
  if f then size = f:seek("end"); f:close() end
  dbg("onRecDone size=" .. tostring(size))
  if size and size > 1024 then
    runEngine({ "process", VB.wav }, true)
  else
    setState("idle")
    notify("Alfred", "Nothing recorded.")
  end
end

function startRecording()
  if not hs.fs.attributes(SOX) then
    notify("Alfred", "sox not found at " .. SOX .. " — run: brew install sox")
    VB.captureFlags = nil
    return
  end
  VB.wav = tmpWav()
  VB.level = 0
  -- `-S` shows the progress/VU meter on stderr so we can drive the level bar.
  -- `trim 0 MAX_RECORD_SECS` self-stops a forgotten recording (safety cap).
  VB.recTask = hs.task.new(SOX, onRecDone, soxStream,
    { "-d", "-S", "-r", "16000", "-c", "1", "-b", "16", VB.wav,
      "trim", "0", tostring(MAX_RECORD_SECS) })
  VB.recTask:setEnvironment(TASK_ENV)
  if VB.recTask:start() then
    VB.recStart = os.time()
    VB.pulse = 0
    setState("recording")
    if not VB.win then showHUD() end   -- the window shows its own record state
    VB.hudTimer = hs.timer.doEvery(0.1, updateHUD)
  else
    notify("Alfred", "Could not start the recorder (sox).")
    VB.captureFlags = nil
  end
end

function stopRecording()
  setState("processing")
  destroyHUD()
  hs.alert.closeAll()
  hs.alert.show("⏳ Transcribing…", 1.0)
  if VB.recTask and VB.recTask:isRunning() then
    hs.execute("/bin/kill -INT " .. VB.recTask:pid())  -- SIGINT -> sox finalizes WAV
  else
    onRecDone()
  end
end

function toggleDictate()
  if VB.state == "idle" then
    VB.captureFlags = nil           -- quick path uses config defaults
    startRecording()
  elseif VB.state == "recording" then
    stopRecording()
  else
    hs.alert.show("Still working…", 0.8)
  end
end

-- Transcribe-only: record and deliver the raw transcript, no LLM stage. Shares
-- the single recording state with toggleDictate, so pressing either key while
-- recording stops the same capture; only the FLAGS pinned at start differ.
function toggleTranscribeOnly()
  if VB.state == "idle" then
    VB.captureFlags = { "--transcribe-only" }   -- pure transcript, no LLM
    startRecording()
  elseif VB.state == "recording" then
    stopRecording()
  else
    hs.alert.show("Still working…", 0.8)
  end
end

-- ---- Mode picker + Type mode (hs.chooser) --------------------------------

-- Show the format list; calls onPick(flags) with the chosen entry's flags.
function pickMode(onPick)
  if VB.chooser then hs.alert.show("Chooser already open…", 0.8); return end
  local chooser
  chooser = hs.chooser.new(function(choice)
    VB.chooser = nil
    chooser = nil
    if not choice then return end     -- cancelled: do nothing
    onPick(choice.flags or {})
  end)
  VB.chooser = chooser
  chooser:placeholderText("Choose output format…")
  chooser:searchSubText(true)
  chooser:rows(#MODES)
  chooser:width(28)
  chooser:choices(MODES)
  chooser:show()
end

function dictateWithMode()
  if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
  pickMode(function(flags)
    if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
    VB.captureFlags = flags
    startRecording()
  end)
end

-- Type box: the query field is free text, the rows are formats. Setting a
-- queryChangedCallback disables hs.chooser's auto-filtering, so the format
-- rows stay visible while you type your message.
function typePrompt()
  if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
  if VB.chooser then hs.alert.show("Chooser already open…", 0.8); return end
  local chooser
  chooser = hs.chooser.new(function(choice)
    local q = (chooser and chooser:query()) or ""
    VB.chooser = nil
    chooser = nil
    if not choice then return end
    if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
    local text = q:gsub("^%s*(.-)%s*$", "%1")
    if #text == 0 then hs.alert.show("Type some text first", 1.0); return end
    VB.captureFlags = choice.flags or {}
    runEngine({ "text", text })
  end)
  VB.chooser = chooser
  chooser:placeholderText("Type text, then pick a format ↵")
  chooser:queryChangedCallback(function() end)   -- disables row filtering
  chooser:choices(MODES)
  chooser:rows(#MODES)
  chooser:width(40)
  chooser:show()
end

-- ---- Result preview panel ------------------------------------------------

function closeResult()
  if VB.resultTimer then VB.resultTimer:stop(); VB.resultTimer = nil end
  if VB.result then VB.result:delete(); VB.result = nil end
  VB.resultHandlers = nil
end

-- Map a button id to the injected handler the caller supplied via showResult.
local RESULT_ACTIONS = { copy = "onCopy", paste = "onPaste",
                         email = "onEmail", discard = "onDiscard" }

-- Pure: route a button id to its injected handler, passing the result text.
-- Returns the handler name that fired, or nil (unknown id / no handler) — the
-- whole click→action contract of the panel, free of hs.* for testing.
local function resultDispatch(id, handlers, text)
  local name = RESULT_ACTIONS[id]
  local fn = name and (handlers or {})[name]
  if fn then fn(text); return name end
  return nil
end

function resultClick(_, msg, id)
  if msg ~= "mouseUp" then return end
  resultDispatch(id, VB.resultHandlers or {}, VB.resultText or "")
end

-- showResult(text, llmFailed, handlers): the panel is now a pure view. The
-- caller injects what each button does via handlers = { onCopy, onPaste,
-- onEmail, onDiscard } (each receives the result text). The panel never reaches
-- back into the engine (no runEngine here) — the dependency is one-way.
function showResult(text, llmFailed, handlers)
  closeResult()
  VB.resultText = text or ""
  VB.resultHandlers = handlers or {}
  local W, H = 380, 184
  local preview = VB.resultText:gsub("%s+", " ")
  if #preview > 300 then preview = preview:sub(1, 300) .. "…" end
  if #preview == 0 then preview = "(empty)" end

  local f = hs.screen.mainScreen():frame()
  local c = hs.canvas.new({ x = f.x + (f.w - W) / 2, y = f.y + 90, w = W, h = H })
  c:level(hs.canvas.windowLevels.overlay)
  c:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces)
  c:clickActivating(false)
  c:mouseCallback(resultClick)

  c:appendElements(
    { type = "rectangle", action = "fill",
      roundedRectRadii = { xRadius = 14, yRadius = 14 },
      fillColor = { red = 0.07, green = 0.07, blue = 0.08, alpha = 0.94 } },
    { type = "text",
      text = resultBanner(llmFailed),
      textColor = { white = 1, alpha = 0.6 }, textSize = 12,
      frame = { x = 18, y = 12, w = W - 36, h = 18 } },
    { type = "text", text = preview,
      textColor = { white = 1, alpha = 0.95 }, textSize = 14,
      frame = { x = 18, y = 34, w = W - 36, h = 96 } }
  )

  local buttons = {
    { id = "copy", label = "Copy" }, { id = "paste", label = "Paste" },
    { id = "email", label = "Email" }, { id = "discard", label = "✕" },
  }
  local n = #buttons
  local pad, gap = 16, 8
  local bw = (W - pad * 2 - gap * (n - 1)) / n
  local by, bh = H - 46, 32
  for i, b in ipairs(buttons) do
    local bx = pad + (i - 1) * (bw + gap)
    local accent = (b.id == "discard")
      and { red = 0.50, green = 0.20, blue = 0.20, alpha = 0.55 }
      or  { red = 0.18, green = 0.34, blue = 0.62, alpha = 0.85 }
    c:appendElements(
      { type = "rectangle", action = "fill", id = b.id, trackMouseUp = true,
        roundedRectRadii = { xRadius = 8, yRadius = 8 },
        fillColor = accent, frame = { x = bx, y = by, w = bw, h = bh } },
      { type = "text", text = b.label, id = b.id, trackMouseUp = true,
        textColor = { white = 1, alpha = 0.95 }, textSize = 13,
        textAlignment = "center", frame = { x = bx, y = by + 8, w = bw, h = 20 } }
    )
  end
  c:show()
  VB.result = c
  VB.resultTimer = hs.timer.doAfter(20, closeResult)  -- auto-dismiss
end

-- ---- Full app window (hs.webview) ----------------------------------------

local WIN_W, WIN_H = 470, 650

-- The engine's resolved history.jsonl path (honours [history].dir via the
-- contract's `resolved.history`); literal fallback for older engines / no contract.
local function historyPath()
  local c = VB.contract
  local h = c and type(c.resolved) == "table" and c.resolved.history
  if type(h) == "string" and #h > 0 then return h end
  return HOME .. "/.voicebridge/history/history.jsonl"
end

-- Pure: newest-first history items from raw JSONL lines (most recent `limit`),
-- decoded by an injected `decodeFn`; skips lines that don't decode to a record
-- with text. Kept hs-free (readHistory injects hs.json.decode) for testing.
local function parseHistory(lines, limit, decodeFn)
  lines = lines or {}
  local n = #lines
  local items = {}
  for i = n, math.max(1, n - (limit or 30) + 1), -1 do
    local ok, rec = pcall(decodeFn, lines[i])
    if ok and type(rec) == "table" and rec.text then
      items[#items + 1] = { ts = rec.ts or "", chars = rec.chars or #rec.text, text = rec.text }
    end
  end
  return items
end

function readHistory(limit)
  local f = io.open(historyPath(), "r")
  if not f then return {} end
  local lines = {}
  for line in f:lines() do lines[#lines + 1] = line end
  f:close()
  return parseHistory(lines, limit, hs.json.decode)
end

-- Pure window helpers (no hs.*), extracted so the window's logic is testable:
--   buildCaptureFlags  — mode flags + the translate toggle -> engine argv flags
--   normalizeBackend   — a "setBackend" value; "" / "default" / nil all mean
--                        "use config" (nil); otherwise the backend name
--   normalizeTranslate — a "setTranslate" value coerced to a strict boolean
local function buildCaptureFlags(modeFlags, translate)
  local f = {}
  for _, a in ipairs(modeFlags or {}) do f[#f + 1] = a end
  f[#f + 1] = translate and "--translate" or "--no-translate"
  return f
end

local function normalizeBackend(value)
  if value and value ~= "" and value ~= "default" then return value end
  return nil
end

local function normalizeTranslate(value)
  return value and true or false
end

function windowCaptureFlags()
  return buildCaptureFlags(VB.winModeFlags, VB.winTranslate)
end

function windowToggleRecord()
  if VB.state == "idle" then
    VB.captureFlags = windowCaptureFlags()
    startRecording()
  elseif VB.state == "recording" then
    stopRecording()
  end
end

function pushWindowState()
  if not VB.win then return end
  local timer = (VB.state == "recording") and fmtTime(os.time() - VB.recStart) or ""
  VB.win:evaluateJavaScript(string.format(
    "window.vbState&&vbState(%q,%q,%f)", VB.state, timer, VB.level or 0))
end

-- Surface a delivered result. If the full app window is already open, refresh it
-- in place; otherwise show the lightweight Copy/Paste/Email toast panel. A quick
-- hotkey dictation must NOT summon the whole 470×650 window on every capture (the
-- toast panel is what the README documents), so we no longer force it open here.
function updateResult(text, llmFailed)
  VB.resultText = text or ""
  if VB.win then
    VB.win:evaluateJavaScript("window.vbResult&&vbResult(" .. jsStr(VB.resultText)
      .. "," .. tostring(llmFailed and true or false) .. ")")
    VB.win:evaluateJavaScript("window.vbHistory&&vbHistory(" .. hs.json.encode(readHistory(30)) .. ")")
    VB.win:show():bringToFront()
  else
    showResult(VB.resultText, llmFailed, resultPanelHandlers())
  end
end

function onWebMessage(message)
  local body = message and message.body
  if type(body) ~= "string" then return end
  local ok, d = pcall(hs.json.decode, body)
  if not ok or type(d) ~= "table" then return end
  local a = d.action
  if a == "record" then
    windowToggleRecord()
  elseif a == "setMode" then
    local m = MODES[d.index]; VB.winModeFlags = (m and m.flags) or {}
  elseif a == "setBackend" then
    VB.backend = normalizeBackend(d.value)
  elseif a == "setTranslate" then
    VB.winTranslate = normalizeTranslate(d.value)
  elseif a == "processText" then
    if type(d.text) == "string" and #d.text > 0 then
      if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
      VB.captureFlags = windowCaptureFlags()
      runEngine({ "text", d.text })
    end
  elseif a == "copy" or a == "recopy" then
    hs.pasteboard.setContents(d.text or VB.resultText or "")   -- toast shown in-window
  elseif a == "editIntents" then
    openConfig()
  elseif a == "reloadModes" then
    refreshModes()
  elseif a == "saveIntent" then
    if type(d.key) == "string" and #d.key > 0 then saveIntent(d.key, d.prompt or "") end
  elseif a == "setModel" then
    if d.backend == "claude" or d.backend == "codex" then setModel(d.backend, d.model or "") end
  elseif a == "paste" then
    if VB.win then VB.win:hide() end
    pasteText(d.text or VB.resultText or "", 0.12)   -- longer delay: window hides first
  end
end

local WIN_HEAD = [==[<!DOCTYPE html><html><head><meta charset="utf-8"><title>Alfred</title>
<style>
 :root{color-scheme:dark;
  --text:#eceef2;--muted:#888d99;--faint:#5f636e;
  --card:#15171d;--card2:#1a1d24;--border:#262932;--border2:#30343e;
  --accent:#5b7cff;--accent2:#7a5cff;--rec:#ff4d57;--ok:#37d39b;}
 *{box-sizing:border-box;}
 body{margin:0;color:var(--text);-webkit-font-smoothing:antialiased;
  font:13px/1.45 -apple-system,system-ui,"SF Pro Text",sans-serif;
  background:radial-gradient(900px 320px at 50% -12%,rgba(91,124,255,.10),transparent 70%),linear-gradient(180deg,#0f1117,#0c0d11);}
 .wrap{padding:16px 16px 18px;display:flex;flex-direction:column;gap:14px;}
 header{display:flex;align-items:center;justify-content:space-between;}
 .brand{display:flex;align-items:center;gap:9px;font-size:15px;font-weight:650;letter-spacing:.2px;}
 .brand span:first-child{font-size:18px;}
 .pill{font-size:11px;color:var(--muted);background:var(--card2);border:1px solid var(--border);border-radius:999px;padding:3px 11px;}
 .pill.rec{color:#ffd9db;background:rgba(255,77,87,.16);border-color:rgba(255,77,87,.42);}
 .pill.busy{color:#ece3bf;background:rgba(220,180,60,.14);border-color:rgba(220,180,60,.35);}
 .rec{position:relative;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;
  border:0;border-radius:14px;padding:15px;cursor:pointer;color:#fff;font-size:15px;font-weight:650;letter-spacing:.2px;
  background:linear-gradient(180deg,var(--accent),var(--accent2));
  box-shadow:0 6px 18px rgba(91,124,255,.30),inset 0 1px 0 rgba(255,255,255,.18);
  transition:transform .06s,box-shadow .2s,background .2s;}
 .rec:hover{box-shadow:0 9px 24px rgba(91,124,255,.42),inset 0 1px 0 rgba(255,255,255,.2);}
 .rec:active{transform:translateY(1px);}
 .rec .rec-dot{width:10px;height:10px;border-radius:50%;background:#fff;opacity:.95;}
 .rec .rec-time{font-variant-numeric:tabular-nums;font-weight:600;opacity:.92;min-width:40px;text-align:right;}
 .rec.recording{background:linear-gradient(180deg,var(--rec),#e23b46);box-shadow:0 6px 20px rgba(255,77,87,.42),inset 0 1px 0 rgba(255,255,255,.18);}
 .rec.recording .rec-dot{animation:pulse 1s ease-in-out infinite;}
 @keyframes pulse{0%,100%{transform:scale(1);opacity:1;}50%{transform:scale(1.55);opacity:.4;}}
 .meter{height:6px;background:var(--card2);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-top:-4px;}
 .meter-fill{height:100%;width:0;border-radius:6px;background:linear-gradient(90deg,#37d39b,#9fd84a 58%,#ffb74d 84%,#ff5d5d);transition:width .08s linear;}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
 .field{display:flex;flex-direction:column;gap:5px;}
 .flabel{display:flex;align-items:center;justify-content:space-between;font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
 select,input[type=text],textarea{width:100%;background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:10px;padding:9px 10px;font-size:13px;transition:border-color .15s,box-shadow .15s;}
 select{-webkit-appearance:none;appearance:none;cursor:pointer;padding-right:28px;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'><polygon points='0,2 10,2 5,8' fill='%23888d99'/></svg>");
  background-repeat:no-repeat;background-position:right 10px center;}
 select:focus,input:focus,textarea:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px rgba(91,124,255,.22);}
 textarea{height:116px;resize:vertical;line-height:1.5;}
 textarea.typein{height:64px;line-height:1.4;}
 textarea.intent{height:80px;font-size:12px;line-height:1.4;color:#d6d9e0;}
 .switch{display:flex;align-items:center;gap:9px;cursor:pointer;user-select:none;}
 .switch input{display:none;}
 .track{width:38px;height:22px;border-radius:999px;background:var(--card2);border:1px solid var(--border2);position:relative;transition:background .18s,border-color .18s;}
 .track::after{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#cfd2da;transition:transform .18s,background .18s;}
 .switch input:checked + .track{background:linear-gradient(180deg,var(--accent),var(--accent2));border-color:transparent;}
 .switch input:checked + .track::after{transform:translateX(16px);background:#fff;}
 .card{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow:hidden;}
 .card-head{display:flex;align-items:center;justify-content:space-between;padding:9px 12px;border-bottom:1px solid var(--border);font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;font-weight:600;}
 .card-head .tools{display:flex;gap:6px;}
 .card textarea{border:0;border-radius:0;background:transparent;}
 .card textarea:focus{box-shadow:none;}
 .chip{background:var(--card2);color:var(--text);border:1px solid var(--border2);border-radius:7px;padding:5px 11px;font-size:12px;cursor:pointer;transition:background .15s,transform .06s;}
 .chip:hover{background:#23262e;} .chip:active{transform:translateY(1px);}
 .link{background:none;border:0;color:var(--accent);font-size:11px;cursor:pointer;padding:0;text-transform:none;letter-spacing:0;}
 .link:hover{text-decoration:underline;}
 .link.muted{color:var(--muted);}
 .ibtns{display:flex;gap:12px;}
 .hist{list-style:none;margin:0;padding:0;max-height:170px;overflow:auto;}
 .hist li{padding:9px 12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background .12s;}
 .hist li:last-child{border-bottom:0;} .hist li:hover{background:var(--card2);}
 .hist .ht{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
 .hist .meta{font-size:10px;color:var(--faint);margin-top:3px;font-variant-numeric:tabular-nums;}
 .hist .empty{padding:16px 12px;color:var(--faint);text-align:center;}
 ::-webkit-scrollbar{width:9px;} ::-webkit-scrollbar-thumb{background:#2a2d36;border-radius:6px;border:2px solid transparent;background-clip:padding-box;}
 .toast{position:fixed;left:50%;bottom:18px;transform:translate(-50%,18px);background:rgba(20,22,28,.97);border:1px solid var(--border2);color:var(--text);padding:8px 16px;border-radius:999px;font-size:12px;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;box-shadow:0 10px 26px rgba(0,0,0,.55);}
 .toast.show{opacity:1;transform:translate(-50%,0);}
</style></head><body>
 <div class="wrap">
  <header>
   <div class="brand"><span>🎙️</span><span>Alfred</span></div>
   <span id="state" class="pill">Ready</span>
  </header>
  <button id="rec" class="rec"><span class="rec-dot"></span><span id="reclabel">Record</span><span id="rectime" class="rec-time"></span></button>
  <div class="meter"><div id="lvl" class="meter-fill"></div></div>
  <textarea id="typein" class="typein" placeholder="…or type here, then ⏎ to run  (⇧⏎ = new line)"></textarea>
  <div class="grid">
   <div class="field"><div class="flabel"><span>Format / intent</span><button id="editint" class="link">Edit prompt</button></div><select id="mode"></select></div>
   <div class="field"><div class="flabel"><span>LLM backend</span></div><select id="backend"></select></div>
  </div>
  <div class="grid">
   <div class="field"><div class="flabel"><span>Claude model</span></div><select id="claudemodel"></select></div>
   <div class="field"><div class="flabel"><span>Codex model</span></div><select id="codexmodel"></select></div>
  </div>
  <div class="field" id="intentbox" style="display:none"><div class="flabel"><span>Intent prompt</span><span class="ibtns"><button id="cancelint" class="link muted">Cancel</button><button id="saveint" class="link">Save &amp; close</button></span></div><textarea id="intent" class="intent" placeholder="Prompt for this format…"></textarea></div>
  <label class="switch"><input type="checkbox" id="translate"><span class="track"></span><span>Translate to English</span></label>
  <section class="card">
   <div class="card-head"><span>Result</span><div class="tools"><button id="copy" class="chip">Copy</button><button id="paste" class="chip">Paste</button></div></div>
   <textarea id="result" placeholder="Your cleaned text appears here…"></textarea>
  </section>
  <section class="card">
   <div class="card-head"><span>History</span><button id="reload" class="link">Refresh</button></div>
   <ul id="hist" class="hist"></ul>
  </section>
 </div>
 <div id="toast" class="toast"></div>
<script>window.VB_INIT=]==]

local WIN_TAIL = [==[;
 const send=(action,extra)=>window.webkit.messageHandlers.vb.postMessage(JSON.stringify(Object.assign({action},extra||{})));
 const $=id=>document.getElementById(id);
 let WIN_MODES=[];
 let toastT; const toast=msg=>{const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(toastT);toastT=setTimeout(()=>t.classList.remove('show'),1200);};
 window.vbState=function(state,timer,level){
   const st=$('state'),r=$('rec');
   if(state==='recording'){st.textContent='● Recording';st.className='pill rec';r.classList.add('recording');$('reclabel').textContent='Stop';$('rectime').textContent=timer;}
   else if(state==='processing'){st.textContent='Working…';st.className='pill busy';r.classList.remove('recording');$('reclabel').textContent='Working…';$('rectime').textContent='';}
   else{st.textContent='Ready';st.className='pill';r.classList.remove('recording');$('reclabel').textContent='Record';$('rectime').textContent='';}
   $('lvl').style.width=state==='recording'?(Math.min(1,level*1.7)*100+'%'):'0';
 };
 window.vbResult=function(text){ $('result').value=text||''; };
 window.vbHistory=function(items){
   const ul=$('hist'); ul.innerHTML='';
   if(!items||!items.length){ul.innerHTML='<li class="empty">No history yet</li>';return;}
   items.forEach(it=>{
     const li=document.createElement('li');
     const t=document.createElement('div'); t.className='ht'; t.textContent=it.text||'';
     const m=document.createElement('div'); m.className='meta'; m.textContent=((it.ts||'').replace('T',' ').slice(0,16))+'  ·  '+(it.chars||0)+'c';
     li.appendChild(t); li.appendChild(m);
     li.onclick=()=>{ $('result').value=it.text; send('recopy',{text:it.text}); toast('Copied ✓'); };
     ul.appendChild(li);
   });
 };
 window.vbModes=function(modes,index){
   WIN_MODES=modes||[];
   const s=$('mode'); s.innerHTML='';
   WIN_MODES.forEach((m,i)=>{const o=document.createElement('option');o.value=i+1;o.textContent=m.label;s.appendChild(o);});
   s.value=index||1; syncEditor();
 };
 function curMode(){ return WIN_MODES[(parseInt($('mode').value,10)||1)-1]||{}; }
 function editorOpen(){ return $('intentbox').style.display!=='none'; }
 function openEditor(){ const m=curMode(); if(!m.key){ toast('This format uses your config prompt'); return; } $('intent').value=m.prompt||''; $('intentbox').style.display=''; $('intent').focus(); }
 function closeEditor(){ $('intentbox').style.display='none'; }
 function syncEditor(){ if(editorOpen()){ const m=curMode(); if(m.key){ $('intent').value=m.prompt||''; } else { closeEditor(); } } }
 function fillModelSelect(id,list,current){
   const s=$(id); if(!s) return; s.innerHTML='';
   const opts=['']; (list||[]).forEach(m=>{ if(opts.indexOf(m)<0) opts.push(m); });
   if(current && opts.indexOf(current)<0) opts.push(current);
   opts.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=(m===''?'(default)':m);s.appendChild(o);});
   s.value=current||'';
 }
 window.vbSettings=function(s){ s=s||{}; fillModelSelect('claudemodel',s.claude_models,s.claude_model); fillModelSelect('codexmodel',s.codex_models,s.codex_model); };
 window.vbBackends=function(list,current){
   const s=$('backend'); if(!s) return; s.innerHTML='';
   const mk=(v,t)=>{const o=document.createElement('option');o.value=v;o.textContent=t;s.appendChild(o);};
   mk('default','Default (config)'); (list||[]).forEach(b=>mk(b,b)); s.value=current||'default';
 };
 const I=window.VB_INIT||{};
 vbModes(I.modes,I.modeIndex);
 vbBackends(I.backends,I.backend);
 $('translate').checked=I.translate!==false;
 vbResult(I.result||'');
 vbHistory(I.history||[]);
 vbState(I.state||'idle','',0);
 vbSettings(I.settings||{});
 $('rec').onclick=()=>send('record');
 $('mode').onchange=e=>{send('setMode',{index:parseInt(e.target.value,10)});syncEditor();};
 $('editint').onclick=openEditor;
 $('cancelint').onclick=closeEditor;
 $('saveint').onclick=()=>{const m=curMode(); if(m.key){send('saveIntent',{key:m.key,prompt:$('intent').value});toast('Saved ✓');} closeEditor();};
 $('backend').onchange=e=>send('setBackend',{value:e.target.value});
 $('claudemodel').onchange=e=>{send('setModel',{backend:'claude',model:e.target.value});toast('Saved ✓');};
 $('codexmodel').onchange=e=>{send('setModel',{backend:'codex',model:e.target.value});toast('Saved ✓');};
 $('translate').onchange=e=>send('setTranslate',{value:e.target.checked});
 $('copy').onclick=()=>{send('copy',{text:$('result').value});toast('Copied ✓');};
 $('paste').onclick=()=>send('paste',{text:$('result').value});
 $('reload').onclick=()=>{send('reloadModes');toast('Reloading…');};
 $('typein').addEventListener('keydown',e=>{ if(e.key==='Enter'&&!e.shiftKey){ e.preventDefault(); send('processText',{text:e.target.value}); e.target.value=''; }});
</script></body></html>]==]

function openWindow()
  if VB.win then VB.win:show():bringToFront(); return end
  local ctrl = hs.webview.usercontent.new("vb")
  ctrl:setCallback(onWebMessage)
  local f = hs.screen.mainScreen():frame()
  local w = hs.webview.new(
    { x = f.x + f.w - WIN_W - 40, y = f.y + 60, w = WIN_W, h = WIN_H },
    { developerExtrasEnabled = false }, ctrl)
  local m = hs.webview.windowMasks
  w:windowStyle(m.titled | m.closable | m.resizable | m.utility)
  w:allowTextEntry(true)
  w:level(hs.canvas.windowLevels.floating)
  w:closeOnEscape(true)
  w:deleteOnClose(true)
  w:windowCallback(function(action) if action == "closing" then VB.win = nil end end)
  VB.win = w
  local init = {
    modes = modesForJS(), modeIndex = 1,
    backends = BACKENDS,
    backend = VB.backend or "default",
    translate = VB.winTranslate ~= false,
    result = VB.resultText or "",
    history = readHistory(30),
    state = VB.state,
    settings = VB.settings or {},
  }
  -- A literal </script> inside dictated/history text would end the enclosing
  -- script element even though it is JSON-quoted. Escape every '<' in the
  -- serialized payload; JavaScript decodes \u003c back to the original text.
  local initJson = hs.json.encode(init):gsub("<", "\\u003c")
  w:html(WIN_HEAD .. initJson .. WIN_TAIL)
  w:show():bringToFront()
end

function closeWindow()
  if VB.win then VB.win:delete(); VB.win = nil end
end

-- Persist an intent's prompt to config (engine writes [intent.<key>]), then reload.
function saveIntent(key, prompt)
  local t = hs.task.new(PYTHON, function(code)
    dbg("saveIntent " .. tostring(key) .. " code=" .. tostring(code))
    refreshModes()
  end, { SCRIPT, "set-intent", key, "--prompt", prompt or "" })
  VB.saveTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Fetch backend/model settings + selectable model lists from the engine.
function refreshSettings()
  local t = hs.task.new(PYTHON, function(code, out)
    if code == 0 and out and #out > 0 then
      local ok, s = pcall(hs.json.decode, out)
      if ok and type(s) == "table" then
        VB.settings = s
        if VB.win then
          VB.win:evaluateJavaScript("window.vbSettings&&vbSettings(" .. hs.json.encode(s) .. ")")
        end
      end
    end
  end, { SCRIPT, "settings" })
  VB.settingsTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Persist a backend's model to config, then refresh (daemon re-reads per call).
function setModel(backend, model)
  local t = hs.task.new(PYTHON, function() refreshSettings() end,
    { SCRIPT, "set-model", backend, "--model", model or "" })
  VB.modelTask = t
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- ---- Menu bar model ------------------------------------------------------
-- Pure: build the menubar menu structure for (state, backend). `actions`
-- supplies the click callbacks by name so this stays free of hs.* — the live
-- menu passes the real handlers; tests pass stubs and assert titles, the
-- separators, and the backend radio's checked state.
local function buildMenu(state, backend, actions, backends)
  actions = actions or {}
  backends = backends or BACKENDS      -- one shared list (menubar + window)
  local function backendItem(label, value)
    return { title = label, checked = (backend == value),
             fn = function() if actions.setBackend then actions.setBackend(value) end end }
  end
  local backendMenu = { { title = "Default (config)", checked = (backend == nil),
      fn = function() if actions.setBackend then actions.setBackend(nil) end end } }
  for _, b in ipairs(backends) do backendMenu[#backendMenu + 1] = backendItem(b, b) end
  return {
    { title = "Alfred — " .. tostring(state) ..
              (backend and ("  ·  " .. backend) or ""), disabled = true },
    { title = "-" },
    { title = "Open Alfred window", fn = actions.openWindow },
    { title = "Dictate (toggle)", fn = actions.toggleDictate },
    { title = "Transcribe only", fn = actions.toggleTranscribeOnly },
    { title = "Dictate as…", fn = actions.dictateWithMode },
    { title = "Type…", fn = actions.typePrompt },
    { title = "Backend", menu = backendMenu },
    { title = "Cancel recording", fn = actions.cancel },
    { title = "-" },
    { title = "Open recordings folder", fn = actions.openRecordings },
    { title = "Edit config…", fn = actions.editConfig },
    { title = "Reload intent modes", fn = actions.reloadModes },
    { title = "Restart engine (warm)", fn = actions.restartDaemon },
    { title = "Reload Hammerspoon", fn = actions.reloadHammerspoon },
  }
end

-- ---- Test-export seam ----------------------------------------------------
-- Under VB_LUA_TEST this returns the PURE helpers (string/table/math only at
-- call time) and bails out BEFORE any Hammerspoon runtime init runs, so plain
-- `lua` can require the module and unit-test them. Zero effect when unset.
if os.getenv("VB_LUA_TEST") then
  return { parseStatus = parseStatus, buildModes = buildModes,
           modesForJS = modesForJS, fmtTime = fmtTime,
           RESULT_ACTIONS = RESULT_ACTIONS, resultDispatch = resultDispatch,
           -- hs-recording pure logic
           iconForState = iconForState, soxLevel = soxLevel,
           -- hs-app-window pure logic
           buildCaptureFlags = buildCaptureFlags,
           normalizeBackend = normalizeBackend,
           normalizeTranslate = normalizeTranslate,
           parseHistory = parseHistory,
           -- hs-hotkey-menubar pure logic
           buildMenu = buildMenu, BACKENDS = BACKENDS,
           -- engine-result pure logic
           resultPayload = resultPayload, classifyResult = classifyResult,
           errorMessage = errorMessage, resultBanner = resultBanner,
           classifyPost = classifyPost, buildEngineArgv = buildEngineArgv,
           -- daemon-launch pure logic
           buildStartDaemonCmd = buildStartDaemonCmd,
           isAlfredIdentity = isAlfredIdentity,
           daemonPidFromInfo = daemonPidFromInfo }
end

-- ---- Consume the engine contract (one fetch, cached) ---------------------
-- Ask the engine for its IPC contract and derive our port / history path /
-- status-line sentinels from it, so the front-end tracks the engine instead of
-- hard-coding. Fast (heavy libs are lazy-imported) and guarded above by the test
-- seam (plain `lua` never reaches here). Silent fallback to the literal defaults
-- when the engine or the contract call is unavailable.
VB.contract = fetchContract()
if VB.contract then
  applyContract(VB.contract)
  dbg("contract loaded: port=" .. tostring(DAEMON_PORT) .. " sentinel=" .. STATUS_SENTINEL)
else
  dbg("contract unavailable at load; using literal fallbacks")
end

-- ---- Menu bar ------------------------------------------------------------

VB.menubar = hs.menubar.new()
VB.menubar:setTitle(ICONS.idle)
VB.menubar:setMenu(function()
  return buildMenu(VB.state, VB.backend, {
    openWindow = function() openWindow() end,
    toggleDictate = toggleDictate,
    toggleTranscribeOnly = toggleTranscribeOnly,
    dictateWithMode = dictateWithMode,
    typePrompt = typePrompt,
    setBackend = function(v) VB.backend = v end,
    cancel = function()
      if VB.state == "recording" and VB.recTask and VB.recTask:isRunning() then
        -- Keep the UI non-idle until Sox's completion callback confirms the
        -- process has exited. onRecDone removes this cancelled WAV and resets.
        VB.cancelRecording = true
        setState("processing")
        destroyHUD()
        VB.recTask:terminate()
        hs.alert.show("Cancelling…", 0.8)
        return
      elseif VB.state == "processing" then
        VB.runId = (VB.runId or 0) + 1   -- invalidate any in-flight daemon result
        cancelWatchdog()
        if VB.engineTask then pcall(function() VB.engineTask:terminate() end); VB.engineTask = nil end
      end
      destroyHUD(); setState("idle"); hs.alert.show("Cancelled", 0.8)
    end,
    openRecordings = function()
      hs.execute("open ~/Documents/VoiceBridge 2>/dev/null || open ~/Documents")
    end,
    editConfig = openConfig,
    reloadModes = function() refreshModes() end,
    restartDaemon = function() restartDaemon() end,
    reloadHammerspoon = function() hs.reload() end,
  })
end)

-- ---- Bind hotkeys --------------------------------------------------------

VB.hotkeys = {
  hs.hotkey.bind(DICTATE_HOTKEY.mods, DICTATE_HOTKEY.key, toggleDictate),
  hs.hotkey.bind(INTENT_HOTKEY.mods, INTENT_HOTKEY.key, dictateWithMode),
  hs.hotkey.bind(TYPE_HOTKEY.mods, TYPE_HOTKEY.key, typePrompt),
  hs.hotkey.bind(WINDOW_HOTKEY.mods, WINDOW_HOTKEY.key, function() openWindow() end),
  hs.hotkey.bind(RAWTX_HOTKEY.mods, RAWTX_HOTKEY.key, toggleTranscribeOnly),
}

refreshModes()      -- pull the (possibly customized) mode catalog from the engine
refreshSettings()   -- pull backend/model settings + lists for the dropdowns
ensureDaemon()      -- start (or reuse) the warm background engine

-- Debug hooks (callable via the `hs` CLI):
--   voicebridgeTest()            -> render the result panel in isolation
--   voicebridgeProcess("a.wav")  -> run the full engine pipeline on a wav file
_G.voicebridgeTest = function()
  local ok, e = pcall(showResult, "TEST result panel — Copy / Paste / Email / ✕ should work.",
    false, resultPanelHandlers())
  return ok and "panel shown" or ("ERROR: " .. tostring(e))
end
_G.voicebridgeProcess = function(wav)
  VB.captureFlags = nil
  runEngine({ "process", wav })
  return "engine started on " .. tostring(wav)
end
_G.voicebridgeWindow = function()
  local ok, e = pcall(openWindow)
  return ok and (VB.win and "open" or "no-win") or ("ERROR: " .. tostring(e))
end

if not hs.fs.attributes(PYTHON) then
  notify("Alfred", "Python venv not found. Run install.sh, then edit PYTHON in voicebridge.lua")
end

hs.alert.show("Alfred loaded — ⌘⌥D dictate · ⌘⌥I intent · ⌘⌥T type", 2)
