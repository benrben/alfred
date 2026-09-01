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

-- ---- Shared module scope ---------------------------------------------------
-- voicebridge.lua stayed over the repository's 600-line File LOC gate, so the
-- HUD/engine/capture/window sections below live in voicebridge_lua/*.lua.
-- Lua has no package system that lets separate files share plain `local`
-- variables, so every name in this file AND those (VB, ICONS, PYTHON, dbg,
-- notify, showHUD, runEngine, ...) is declared WITHOUT `local` -- normally
-- that would make it a real Hammerspoon global, so instead this chunk's own
-- _ENV (the table Lua resolves every unqualified name against) is switched to
-- SHARED, a private table that falls back to the real _G (via the metatable
-- below) for genuine globals like `hs`/`os`/`string`. That makes every
-- unqualified name here and in the split files behave exactly like it did as
-- a `local` in the single flat file -- freely callable with no prefix, but
-- invisible outside this SHARED table -- while `_G.voicebridge` and the
-- `_G.voicebridgeTest`-style debug hooks below (both explicitly `_G.`-prefixed
-- already) still land on the real global table Hammerspoon and the `hs` CLI
-- expect. This MUST run before the first unqualified assignment below (HOME
-- and everything after it): anything assigned before this line would land in
-- the real _G instead of SHARED.
--
-- If you add a new module-scope name here or in a split file, decide whether
-- another file needs to see it; if so, declare it without `local` like the
-- others AND add it to `.luacheckrc`'s `globals` list (luacheck can't see this
-- _ENV trick statically, so an undeclared one reads as an accidental global).
local SHARED = setmetatable({}, { __index = _G })
_ENV = SHARED

-- ============================ USER CONFIG ============================
HOME   = os.getenv("HOME")
DIR    = HOME .. "/Claude/Projects/alfred"   -- folder holding voicebridge.py
PYTHON = DIR .. "/.venv/bin/python3"         -- created by install.sh
SCRIPT = DIR .. "/voicebridge.py"
SOX    = "/opt/homebrew/bin/sox"             -- output of `which sox`

DICTATE_HOTKEY  = { mods = { "cmd", "option" }, key = "d" }   -- dictate, no intent
INTENT_HOTKEY   = { mods = { "cmd", "option" }, key = "i" }   -- dictate + pick format
TYPE_HOTKEY     = { mods = { "cmd", "option" }, key = "t" }   -- type + pick format
WINDOW_HOTKEY   = { mods = { "cmd", "option" }, key = "v" }   -- open the app window
RAWTX_HOTKEY    = { mods = { "cmd", "option" }, key = "r" }   -- transcribe only (no LLM)
SHOW_METER      = true                         -- live mic-level bar in the HUD
DAEMON_PORT     = 8763                          -- warm background engine (localhost)
-- Hard cap on a single recording (seconds). Generous (60 min) for long
-- notes/meetings; the cap only stops a forgotten recorder from running forever
-- and filling the disk. Applied as a sox `trim 0 <secs>` effect. Mirrors the
-- Raycast front-end's MAX_RECORD_SECS.
MAX_RECORD_SECS = 3600
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

VB = {
  state = "idle", recTask = nil, wav = nil, hotkeys = {},
  hud = nil, hudTimer = nil, recStart = 0, level = 0, pulse = 0,
  captureFlags = nil,                 -- extra engine args for the next run only
  backend = nil,                      -- nil = config default; "auto"|"claude"|"codex"
  result = nil, resultTimer = nil, resultText = "",
  win = nil, winModeFlags = {}, winTranslate = true,   -- full app window state
}
_G.voicebridge = VB

ICONS = { idle = "🎙️", recording = "🔴", processing = "⏳" }

-- The selectable LLM backends (besides "Default = use config"). ONE source
-- feeding both the menubar submenu and the window's <select>; "local" is the
-- on-device MLX model — and the engine's DEFAULT — which the earlier hard-coded
-- lists both dropped. Matches voicebridge.py's `--backend` choices.
BACKENDS = { "auto", "claude", "codex", "local" }

-- Pure: the menubar glyph for a state, falling back to the idle mic.
function iconForState(s) return ICONS[s] or ICONS.idle end

-- Output formats offered by the mode picker. The catalog (Email/Commit/your
-- custom [intent] modes…) is loaded from the engine via `voicebridge.py modes`
-- (see refreshModes); this pseudo-entry is always appended after it.
-- `--mode X --rewrite` enables rewrite even for the "raw" cleanup mode.
MODE_RAWTX = { text = "Raw transcript", subText = "No LLM at all",
                     flags = { "--no-rewrite", "--no-translate", "--no-optimize" } }

-- Fallback catalog if the engine call fails; refreshModes() replaces it.
BUILTIN_CATALOG = {
  { key = "email",   label = "Email",        description = "Polished email" },
  { key = "message", label = "Message",      description = "Casual chat / DM" },
  { key = "commit",  label = "Commit",       description = "Git commit message" },
  { key = "prompt",  label = "Prompt",       description = "Prompt for an AI" },
  { key = "notes",   label = "Notes",        description = "Clean notes / bullets" },
  { key = "raw",     label = "Cleanup only", description = "Tidy wording, keep structure", default = true },
}

-- Pure: scan the catalog for the mode flagged `default`; returns its
-- key/label/prompt, all nil when none is flagged.
local function findDefaultMode(catalog)
  local defKey, defLabel, defPrompt
  for _, m in ipairs(catalog) do
    if m.default then defKey = m.key; defLabel = m.label or m.key; defPrompt = m.prompt or "" end
  end
  return defKey, defLabel, defPrompt
end

-- Pure: the Default picker entry, borrowing the flagged default mode's
-- key/prompt (if any) so editing it edits that mode too.
local function defaultModeEntry(catalog)
  local defKey, defLabel, defPrompt = findDefaultMode(catalog)
  return {
    text = defKey and ("Default · " .. defLabel) or "Default (use config)",
    subText = "Your configured mode", key = defKey, prompt = defPrompt or "",
    flags = {},   -- Default = no per-capture flags (uses config)
  }
end

-- Pure: one catalog entry -> its picker-list entry.
local function catalogModeEntry(m)
  return {
    text = m.label or m.key, subText = m.description or "",
    key = m.key, prompt = m.prompt or "",
    flags = { "--mode", m.key, "--rewrite" },
  }
end

function buildModes(catalog)
  local list = { defaultModeEntry(catalog) }
  for _, m in ipairs(catalog) do
    list[#list + 1] = catalogModeEntry(m)
  end
  list[#list + 1] = MODE_RAWTX
  return list
end

MODES = buildModes(BUILTIN_CATALOG)   -- refreshed from the engine at load

-- The mode list as JS objects {label, prompt, key} for the window picker.
function modesForJS()
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
USER_PATH = ((not os.getenv("VB_LUA_TEST") and hs.execute('echo -n "$PATH"', true)) or ""):gsub("%s+$", "")
USER_PATH = USER_PATH .. ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
  .. ":" .. HOME .. "/.local/bin:" .. HOME .. "/.cargo/bin:" .. HOME .. "/.bun/bin"
-- Force a UTF-8 locale so the engine doesn't fall back to mac-roman (which
-- mangles curly quotes / em dashes / Hebrew) when launched from the GUI.
TASK_ENV  = { PATH = USER_PATH, HOME = HOME,
                    LANG = "en_US.UTF-8", LC_ALL = "en_US.UTF-8", PYTHONUTF8 = "1" }
-- DAEMON_PORT / DAEMON_URL default to the literals above; applyContract() may
-- rewrite them from the engine's contract at load (so the port lives in one
-- place, the engine's CONTRACT).
DAEMON_URL = "http://127.0.0.1:" .. DAEMON_PORT .. "/"

-- Shared shell env prefix for every synchronous hs.execute() that shells the
-- engine (startDaemon / the contract fetch): the same PATH/HOME/UTF-8 locale as
-- TASK_ENV, so a GUI-launched engine behaves exactly like the CLI one.
ENV_PREFIX = "PATH='" .. USER_PATH .. "' HOME='" .. HOME ..
  "' LANG='en_US.UTF-8' LC_ALL='en_US.UTF-8' PYTHONUTF8=1 "
-- The engine's owner-only (0700) state dir. Our debug trace + last-capture dump
-- (AND the daemon's own stdout/stderr log, below) live here — NOT in
-- world-readable /tmp — because every one of them can contain verbatim
-- dictation (the daemon logs "transcript (…): <first 120 chars>" per capture).
-- Created on demand by ensureVBDir(); the engine also creates it.
VB_DIR        = HOME .. "/.voicebridge"
LOG_FILE      = VB_DIR .. "/voicebridge.log"
DUMP_FILE     = VB_DIR .. "/voicebridge_last.txt"
DAEMON_LOG    = VB_DIR .. "/daemon.log"

pcall(require, "hs.ipc")   -- enables the `hs` CLI for introspection

-- Forward declarations (several of these reference each other).

-- ---- Small helpers -------------------------------------------------------

function fmtTime(secs)
  return string.format("%02d:%02d", math.floor(secs / 60), secs % 60)
end

-- Ensure the owner-only ~/.voicebridge dir exists before we write into it (the
-- engine creates it 0700 too; we chmod best-effort in case we win the race).
vbDirReady = false
function ensureVBDir()
  if vbDirReady then return end
  if not hs.fs.attributes(VB_DIR) then
    hs.fs.mkdir(VB_DIR)
    pcall(function() hs.fs.chmod(VB_DIR, 448) end)   -- 0700; no-op if unsupported
  end
  vbDirReady = true
end

-- Off by default: the trace can contain dictation excerpts. Set true to write a
-- trace to ~/.voicebridge/voicebridge.log (owner-only), not world-readable /tmp.
DEBUG = false
-- Test-only seam so the VB_LUA_TEST suite can exercise dbg()'s body (normally
-- gated off) without changing the real default above. Nothing else calls this.
function setDebugForTest(v) DEBUG = v end
function dbg(msg)
  if not DEBUG then return end
  ensureVBDir()
  local fh = io.open(LOG_FILE, "a")
  if fh then fh:write(os.date("%H:%M:%S ") .. tostring(msg) .. "\n"); fh:close() end
end

-- JSON-encode a single string for embedding in JS (hs.json.encode wants a table).
function jsStr(s)
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

function tmpWav()
  local tmp = os.getenv("TMPDIR") or "/tmp/"
  return tmp .. "voicebridge_" .. os.time() .. ".wav"
end

-- Paste `text` into the frontmost app: put it on the clipboard, then (after a
-- short delay so focus settles) synthesize Cmd-V. Shared by the toast panel and
-- the app window so the sequence lives in exactly one place.
function pasteText(text, delay)
  hs.pasteboard.setContents(text or "")
  hs.timer.doAfter(delay or 0.08, function() hs.eventtap.keyStroke({ "cmd" }, "v") end)
end


-- ---- Load the split-out sections --------------------------------------------
-- Load order does not matter: nothing in these files calls another file's
-- function at load time, only later (menu bar creation, hotkey binding, an
-- actual key press) once every file below has already run.
local SELF_DIR = debug.getinfo(1, "S").source:match("@(.*/)") or "./"
for _, name in ipairs({ "hud", "engine", "capture", "window" }) do
  assert(loadfile(SELF_DIR .. "voicebridge_lua/" .. name .. ".lua", "t", SHARED))()
end

-- ---- Menu bar model ------------------------------------------------------
-- Pure: build the menubar menu structure for (state, backend). `actions`
-- supplies the click callbacks by name so this stays free of hs.* — the live
-- menu passes the real handlers; tests pass stubs and assert titles, the
-- separators, and the backend radio's checked state.
function buildMenu(state, backend, actions, backends)
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

-- ---- Menu-bar / hotkey / debug-hook action handlers -----------------------
-- Named (rather than a fresh anonymous closure built every time the menu
-- opens or a hotkey fires) so each handler is directly callable -- and,
-- being defined above the VB_LUA_TEST seam below, directly testable --
-- without re-creating a wrapper closure around it. buildLiveMenu() itself
-- still has to be a closure (it must re-read VB.state/VB.backend fresh every
-- time the menu opens), but everything it wires in is one of these stable,
-- named handlers.

-- Pure: the live menubar model, rebuilt from current state on every open.
function buildLiveMenu()
  return buildMenu(VB.state, VB.backend, {
    openWindow = openWindow,
    toggleDictate = toggleDictate,
    toggleTranscribeOnly = toggleTranscribeOnly,
    dictateWithMode = dictateWithMode,
    typePrompt = typePrompt,
    setBackend = setBackendValue,
    cancel = cancel,
    openRecordings = openRecordings,
    editConfig = openConfig,
    reloadModes = refreshModes,
    restartDaemon = restartDaemon,
    reloadHammerspoon = reloadHammerspoon,
  })
end

function setBackendValue(v) VB.backend = v end

-- Stop any in-flight recording or processing run and snap the UI back to idle.
function cancel()
  if VB.recTask and VB.recTask:isRunning() then VB.recTask:terminate() end
  if VB.state == "processing" then
    VB.runId = (VB.runId or 0) + 1   -- invalidate any in-flight daemon result
    cancelWatchdog()
    if VB.engineTask then pcall(function() VB.engineTask:terminate() end); VB.engineTask = nil end
  end
  destroyHUD(); setState("idle"); hs.alert.show("Cancelled", 0.8)
end

function openRecordings()
  hs.execute("open ~/Documents/VoiceBridge 2>/dev/null || open ~/Documents")
end

function reloadHammerspoon() hs.reload() end

-- Debug hooks (installed onto the real _G below, callable via the `hs` CLI):
--   voicebridgeTest()            -> render the result panel in isolation
--   voicebridgeProcess("a.wav")  -> run the full engine pipeline on a wav file
--   voicebridgeWindow()          -> open the full app window
function voicebridgeTest()
  local ok, e = pcall(showResult, "TEST result panel — Copy / Paste / Email / ✕ should work.",
    false, resultPanelHandlers())
  return ok and "panel shown" or ("ERROR: " .. tostring(e))
end
function voicebridgeProcess(wav)
  VB.captureFlags = nil
  runEngine({ "process", wav })
  return "engine started on " .. tostring(wav)
end
function voicebridgeWindow()
  local ok, e = pcall(openWindow)
  return ok and (VB.win and "open" or "no-win") or ("ERROR: " .. tostring(e))
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
           soxStream = soxStream,
           -- HUD pure logic + the hs.canvas wrappers themselves (tested with
           -- a faked `hs`/VB.hud where they need one -- see hud.lua's comment)
           VB = VB, SHOW_METER = SHOW_METER,
           HUD_W = HUD_W, HUD_H_BASE = HUD_H_BASE,
           MET_X = MET_X, MET_Y = MET_Y, MET_H = MET_H, MET_MAXW = MET_MAXW,
           showHUD = showHUD, updateHUD = updateHUD, destroyHUD = destroyHUD,
           hudHeight = hudHeight, hudCanvasFrame = hudCanvasFrame,
           hudBaseElements = hudBaseElements,
           hudMeterTrackElements = hudMeterTrackElements,
           hudMeterVisible = hudMeterVisible, hudMeterFrame = hudMeterFrame,
           hudMeterColor = hudMeterColor, hudPulseColor = hudPulseColor,
           -- hs-app-window pure logic
           buildCaptureFlags = buildCaptureFlags,
           normalizeBackend = normalizeBackend,
           normalizeTranslate = normalizeTranslate,
           parseHistory = parseHistory,
           historyStartIndex = historyStartIndex,
           historyRecordValid = historyRecordValid,
           historyItemFrom = historyItemFrom,
           windowCaptureFlags = windowCaptureFlags,
           windowToggleRecord = windowToggleRecord,
           pushWindowState = pushWindowState,
           updateResult = updateResult,
           saveIntent = saveIntent, refreshSettings = refreshSettings,
           taskOutputReady = taskOutputReady, setModel = setModel,
           -- hs-recording (capture.lua) -- these still take zero args in
           -- production (hotkeys, menu items, hs.task/hs.canvas callbacks);
           -- `hs` is faked as a plain global in tests (see hud.lua's showHUD
           -- test) so the real functions are exercised end-to-end.
           onRecDone = onRecDone,
           startRecording = startRecording, stopRecording = stopRecording,
           toggleDictate = toggleDictate,
           toggleTranscribeOnly = toggleTranscribeOnly,
           pickMode = pickMode, dictateWithMode = dictateWithMode,
           typePrompt = typePrompt,
           closeResult = closeResult, resultClick = resultClick,
           -- hs-hotkey-menubar pure logic
           buildMenu = buildMenu, BACKENDS = BACKENDS,
           -- engine-result pure logic
           resultPayload = resultPayload, classifyResult = classifyResult,
           errorMessage = errorMessage, resultBanner = resultBanner,
           classifyPost = classifyPost, buildEngineArgv = buildEngineArgv,
           onResult = onResult, runEngine = runEngine,
           resultPanelHandlers = resultPanelHandlers,
           startWatchdog = startWatchdog,
           -- daemon-launch pure logic
           buildStartDaemonCmd = buildStartDaemonCmd,
           startDaemon = startDaemon, ensureDaemon = ensureDaemon,
           -- engine-contract fetch/parse/apply
           fetchContract = fetchContract, applyContract = applyContract,
           resolveConfigPath = resolveConfigPath,
           -- small helpers (voicebridge.lua's own; hs.* calls faked in tests)
           ensureVBDir = ensureVBDir, vbDirReady = vbDirReady, dbg = dbg,
           setDebugForTest = setDebugForTest, jsStr = jsStr, setState = setState,
           notify = notify, tmpWav = tmpWav, pasteText = pasteText,
           -- menu-bar / hotkey / debug-hook action handlers (voicebridge.lua's
           -- own named handlers, plus the cross-file targets buildLiveMenu
           -- wires them to, so tests can assert the wiring by identity)
           buildLiveMenu = buildLiveMenu, setBackendValue = setBackendValue,
           cancel = cancel, openRecordings = openRecordings,
           reloadHammerspoon = reloadHammerspoon,
           voicebridgeTest = voicebridgeTest, voicebridgeProcess = voicebridgeProcess,
           voicebridgeWindow = voicebridgeWindow,
           openWindow = openWindow, openConfig = openConfig,
           refreshModes = refreshModes, restartDaemon = restartDaemon }
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
VB.menubar:setMenu(buildLiveMenu)

-- ---- Bind hotkeys --------------------------------------------------------

VB.hotkeys = {
  hs.hotkey.bind(DICTATE_HOTKEY.mods, DICTATE_HOTKEY.key, toggleDictate),
  hs.hotkey.bind(INTENT_HOTKEY.mods, INTENT_HOTKEY.key, dictateWithMode),
  hs.hotkey.bind(TYPE_HOTKEY.mods, TYPE_HOTKEY.key, typePrompt),
  hs.hotkey.bind(WINDOW_HOTKEY.mods, WINDOW_HOTKEY.key, openWindow),
  hs.hotkey.bind(RAWTX_HOTKEY.mods, RAWTX_HOTKEY.key, toggleTranscribeOnly),
}

refreshModes()      -- pull the (possibly customized) mode catalog from the engine
refreshSettings()   -- pull backend/model settings + lists for the dropdowns
ensureDaemon()      -- start (or reuse) the warm background engine

-- Install the debug hooks (defined above, before the test seam) onto the
-- real _G so they're reachable via the `hs` CLI (hs -c "voicebridgeTest()").
_G.voicebridgeTest = voicebridgeTest
_G.voicebridgeProcess = voicebridgeProcess
_G.voicebridgeWindow = voicebridgeWindow

if not hs.fs.attributes(PYTHON) then
  notify("Alfred", "Python venv not found. Run install.sh, then edit PYTHON in voicebridge.lua")
end

hs.alert.show("Alfred loaded — ⌘⌥D dictate · ⌘⌥I intent · ⌘⌥T type", 2)
