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

local DICTATE_HOTKEY = { mods = { "cmd", "option" }, key = "d" }   -- dictate, no intent
local INTENT_HOTKEY  = { mods = { "cmd", "option" }, key = "i" }   -- dictate + pick format
local TYPE_HOTKEY    = { mods = { "cmd", "option" }, key = "t" }   -- type + pick format
local WINDOW_HOTKEY  = { mods = { "cmd", "option" }, key = "v" }   -- open the app window
local SHOW_METER     = true                         -- live mic-level bar in the HUD
local DAEMON_PORT    = 8763                          -- warm background engine (localhost)
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
local USER_PATH = (hs.execute('echo -n "$PATH"', true) or ""):gsub("%s+$", "")
USER_PATH = USER_PATH .. ":/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"
  .. ":" .. HOME .. "/.local/bin:" .. HOME .. "/.cargo/bin:" .. HOME .. "/.bun/bin"
local TASK_ENV  = { PATH = USER_PATH, HOME = HOME }
local DAEMON_URL = "http://127.0.0.1:" .. DAEMON_PORT .. "/"

pcall(require, "hs.ipc")   -- enables the `hs` CLI for introspection

-- Forward declarations (several of these reference each other).
local setState, notify, tmpWav, fmtTime, dbg
local showHUD, updateHUD, destroyHUD, soxStream
local parseStatus, onResult, runEngine, refreshModes
local onRecDone, startRecording, stopRecording, toggleDictate
local pickMode, dictateWithMode, typePrompt
local closeResult, resultClick, showResult
local openWindow, closeWindow, onWebMessage, pushWindowState, updateResult
local windowToggleRecord, windowCaptureFlags, readHistory, saveIntent
local startDaemon, ensureDaemon, restartDaemon, runEngineOneShot
local refreshSettings, setModel

-- ---- Small helpers -------------------------------------------------------

function fmtTime(secs)
  return string.format("%02d:%02d", math.floor(secs / 60), secs % 60)
end

local DEBUG = true   -- writes a trace to /tmp/voicebridge.log; set false to silence
function dbg(msg)
  if not DEBUG then return end
  local fh = io.open("/tmp/voicebridge.log", "a")
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
  if VB.menubar then VB.menubar:setTitle(ICONS[s] or "🎙️") end
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
function soxStream(_, _, stdErr)
  for seg in (stdErr or ""):gmatch("[^\r\n]+") do
    local meter = seg:match("%[([^%[%]]-|[^%[%]]-)%]")
    if meter then
      local fill, total = 0, 0
      for ch in meter:gmatch(".") do
        total = total + 1
        if ch ~= " " and ch ~= "|" then fill = fill + 1 end
      end
      if total > 0 then VB.level = math.max(VB.level or 0, fill / total) end
    end
  end
  return true
end

-- ---- Engine plumbing -----------------------------------------------------

-- Parse the engine's machine-readable last line: "VB_STATUS\tkind[\textra...]"
function parseStatus(out)
  for line in (out or ""):gmatch("[^\r\n]+") do
    local rest = line:match("^VB_STATUS\t(.+)$")
    if rest then
      local parts = {}
      for p in (rest .. "\t"):gmatch("(.-)\t") do parts[#parts + 1] = p end
      return parts
    end
  end
  return nil
end

function onResult(code, out, err)
  setState("idle")
  VB.engineTask = nil   -- release the retained task now that it has finished
  local fh = io.open("/tmp/voicebridge_last.txt", "w")
  if fh then
    fh:write("code=" .. tostring(code) .. "\n--- STDOUT ---\n" .. (out or "") ..
             "\n--- STDERR ---\n" .. (err or ""))
    fh:close()
  end
  dbg("onResult code=" .. tostring(code) ..
      " out=[" .. ((out or ""):gsub("\n", "\\n")):sub(1, 200) .. "]" ..
      " err=[" .. ((err or ""):gsub("\n", "\\n")):sub(1, 200) .. "]")
  local parts = parseStatus(out)
  local kind = parts and parts[1] or nil
  local llmFailed = parts and parts[#parts] == "llm_failed"
  dbg("onResult kind=" .. tostring(kind))

  if kind == "copied" then
    local txt = hs.pasteboard.getContents() or ""
    local ok, e = pcall(updateResult, txt, llmFailed)
    if not ok then
      dbg("updateResult ERROR: " .. tostring(e))
      notify("Alfred", llmFailed and "Copied raw transcript (LLM step failed)"
                                        or "Copied to clipboard ✓")
    end
  elseif kind == "saved" then
    local path = parts[2]
    notify("Alfred", "Too long — saved to file (click to reveal)",
      function() if path then hs.execute("open -R '" .. path .. "'") end end)
  elseif kind == "empty" then
    notify("Alfred", "No speech detected.")
  else
    local tail = (err or ""):gsub("%s+$", ""):match("[^\r\n]+$") or "see Hammerspoon console"
    notify("Alfred", "Error: " .. tail)
  end
end

-- Launch the warm engine daemon, detached so it survives Hammerspoon reloads
-- (keeping the Whisper model resident). Re-launching when one already runs is
-- harmless: the new process finds the port busy and exits.
function startDaemon()
  hs.execute("PATH='" .. USER_PATH .. "' HOME='" .. HOME .. "' nohup '" .. PYTHON ..
    "' '" .. SCRIPT .. "' serve --port " .. DAEMON_PORT ..
    " >/tmp/alfred_daemon.log 2>&1 &")
  dbg("startDaemon: launched detached on :" .. DAEMON_PORT)
end

function ensureDaemon()
  hs.http.asyncGet(DAEMON_URL, nil, function(status)
    if status ~= 200 then startDaemon() end
  end)
end

function restartDaemon()
  hs.execute("pkill -f 'voicebridge.py serve' 2>/dev/null")
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
    setState("idle")
    notify("Alfred", "Could not launch the engine. Check PYTHON path in voicebridge.lua")
  end
end

-- `argv` starts at the subcommand (e.g. {"process", wav}); no python/script.
function runEngine(argv)
  setState("processing")
  local cmd = {}
  for _, a in ipairs(argv) do cmd[#cmd + 1] = a end
  for _, a in ipairs(VB.captureFlags or {}) do cmd[#cmd + 1] = a end
  VB.captureFlags = nil
  if VB.backend then cmd[#cmd + 1] = "--backend"; cmd[#cmd + 1] = VB.backend end
  dbg("runEngine: " .. table.concat(cmd, " "))
  -- Prefer the warm daemon; fall back to a one-shot process if it's not up.
  hs.http.asyncPost(DAEMON_URL, hs.json.encode({ argv = cmd }),
    { ["Content-Type"] = "application/json" },
    function(status, body)
      if status == 200 and body then
        local ok, resp = pcall(hs.json.decode, body)
        if ok and type(resp) == "table" then
          dbg("daemon result code=" .. tostring(resp.code))
          onResult(resp.code or 0, resp.out or "", "")
          return
        end
      end
      dbg("daemon unavailable (status=" .. tostring(status) .. ") -> one-shot")
      runEngineOneShot(cmd)
      ensureDaemon()   -- bring it up for next time
    end)
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

-- ---- Dictation -----------------------------------------------------------

function onRecDone()
  destroyHUD()
  dbg("onRecDone state=" .. tostring(VB.state) .. " wav=" .. tostring(VB.wav))
  -- sox exited (after we sent SIGINT). Process if the file has audio.
  if VB.state ~= "processing" then return end
  local f = io.open(VB.wav, "r")
  local size = 0
  if f then size = f:seek("end"); f:close() end
  dbg("onRecDone size=" .. tostring(size))
  if size and size > 1024 then
    runEngine({ "process", VB.wav })
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
  VB.recTask = hs.task.new(SOX, onRecDone, soxStream,
    { "-d", "-S", "-r", "16000", "-c", "1", "-b", "16", VB.wav })
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

-- ---- Mode picker + Type mode (hs.chooser) --------------------------------

-- Show the format list; calls onPick(flags) with the chosen entry's flags.
function pickMode(onPick)
  local chooser
  chooser = hs.chooser.new(function(choice)
    chooser = nil
    if not choice then return end     -- cancelled: do nothing
    onPick(choice.flags or {})
  end)
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
    VB.captureFlags = flags
    startRecording()
  end)
end

-- Type box: the query field is free text, the rows are formats. Setting a
-- queryChangedCallback disables hs.chooser's auto-filtering, so the format
-- rows stay visible while you type your message.
function typePrompt()
  if VB.state ~= "idle" then hs.alert.show("Busy…", 0.8); return end
  local chooser
  chooser = hs.chooser.new(function(choice)
    local q = (chooser and chooser:query()) or ""
    chooser = nil
    if not choice then return end
    local text = q:gsub("^%s*(.-)%s*$", "%1")
    if #text == 0 then hs.alert.show("Type some text first", 1.0); return end
    VB.captureFlags = choice.flags or {}
    runEngine({ "text", text })
  end)
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
end

function resultClick(_, msg, id)
  if msg ~= "mouseUp" then return end
  local text = VB.resultText or ""
  if id == "copy" then
    hs.pasteboard.setContents(text)
    closeResult()
    hs.alert.show("Copied ✓", 0.6)
  elseif id == "paste" then
    hs.pasteboard.setContents(text)
    closeResult()
    hs.timer.doAfter(0.08, function() hs.eventtap.keyStroke({ "cmd" }, "v") end)
  elseif id == "email" then
    closeResult()
    VB.captureFlags = { "--mode", "email" }
    runEngine({ "text", text })
  elseif id == "discard" then
    closeResult()
  end
end

function showResult(text, llmFailed)
  closeResult()
  VB.resultText = text or ""
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
      text = llmFailed and "Copied raw transcript (LLM step failed)" or "Copied to clipboard ✓",
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

function readHistory(limit)
  local f = io.open(HOME .. "/.voicebridge/history/history.jsonl", "r")
  if not f then return {} end
  local lines = {}
  for line in f:lines() do lines[#lines + 1] = line end
  f:close()
  local items = {}
  for i = #lines, math.max(1, #lines - (limit or 30) + 1), -1 do
    local ok, rec = pcall(hs.json.decode, lines[i])
    if ok and type(rec) == "table" and rec.text then
      items[#items + 1] = { ts = rec.ts or "", chars = rec.chars or #rec.text, text = rec.text }
    end
  end
  return items
end

function windowCaptureFlags()
  local f = {}
  for _, a in ipairs(VB.winModeFlags or {}) do f[#f + 1] = a end
  f[#f + 1] = VB.winTranslate and "--translate" or "--no-translate"
  return f
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

function updateResult(text, llmFailed)
  VB.resultText = text or ""
  if not VB.win then openWindow() end
  if not VB.win then
    notify("Alfred", llmFailed and "Copied raw transcript (LLM step failed)"
                                      or "Copied to clipboard ✓")
    return
  end
  VB.win:evaluateJavaScript("window.vbResult&&vbResult(" .. jsStr(VB.resultText)
    .. "," .. tostring(llmFailed and true or false) .. ")")
  VB.win:evaluateJavaScript("window.vbHistory&&vbHistory(" .. hs.json.encode(readHistory(30)) .. ")")
  VB.win:show():bringToFront()
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
    VB.backend = (d.value and d.value ~= "" and d.value ~= "default") and d.value or nil
  elseif a == "setTranslate" then
    VB.winTranslate = d.value and true or false
  elseif a == "processText" then
    if type(d.text) == "string" and #d.text > 0 then
      VB.captureFlags = windowCaptureFlags()
      runEngine({ "text", d.text })
    end
  elseif a == "copy" or a == "recopy" then
    hs.pasteboard.setContents(d.text or VB.resultText or "")   -- toast shown in-window
  elseif a == "editIntents" then
    hs.execute("open -t " .. HOME .. "/.config/voicebridge/config.toml 2>/dev/null || open -t '"
      .. DIR .. "/config.example.toml'")
  elseif a == "reloadModes" then
    refreshModes()
  elseif a == "saveIntent" then
    if type(d.key) == "string" and #d.key > 0 then saveIntent(d.key, d.prompt or "") end
  elseif a == "setModel" then
    if d.backend == "claude" or d.backend == "codex" then setModel(d.backend, d.model or "") end
  elseif a == "paste" then
    hs.pasteboard.setContents(d.text or VB.resultText or "")
    if VB.win then VB.win:hide() end
    hs.timer.doAfter(0.12, function() hs.eventtap.keyStroke({ "cmd" }, "v") end)
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
   <div class="field"><div class="flabel"><span>LLM backend</span></div><select id="backend">
    <option value="default">Default (config)</option><option value="auto">auto</option>
    <option value="claude">claude</option><option value="codex">codex</option>
   </select></div>
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
 const I=window.VB_INIT||{};
 vbModes(I.modes,I.modeIndex);
 $('backend').value=I.backend||'default';
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
    backend = VB.backend or "default",
    translate = VB.winTranslate ~= false,
    result = VB.resultText or "",
    history = readHistory(30),
    state = VB.state,
    settings = VB.settings or {},
  }
  w:html(WIN_HEAD .. hs.json.encode(init) .. WIN_TAIL)
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

-- ---- Menu bar ------------------------------------------------------------

VB.menubar = hs.menubar.new()
VB.menubar:setTitle(ICONS.idle)
VB.menubar:setMenu(function()
  return {
    { title = "Alfred — " .. VB.state ..
              (VB.backend and ("  ·  " .. VB.backend) or ""), disabled = true },
    { title = "-" },
    { title = "Open Alfred window", fn = function() openWindow() end },
    { title = "Dictate (toggle)", fn = toggleDictate },
    { title = "Dictate as…", fn = dictateWithMode },
    { title = "Type…", fn = typePrompt },
    { title = "Backend", menu = {
        { title = "Default (config)", checked = (VB.backend == nil),
          fn = function() VB.backend = nil end },
        { title = "auto",   checked = (VB.backend == "auto"),
          fn = function() VB.backend = "auto" end },
        { title = "claude", checked = (VB.backend == "claude"),
          fn = function() VB.backend = "claude" end },
        { title = "codex",  checked = (VB.backend == "codex"),
          fn = function() VB.backend = "codex" end },
      } },
    { title = "Cancel recording", fn = function()
        if VB.recTask and VB.recTask:isRunning() then VB.recTask:terminate() end
        destroyHUD(); setState("idle"); hs.alert.show("Cancelled", 0.8)
      end },
    { title = "-" },
    { title = "Open recordings folder", fn = function()
        hs.execute("open ~/Documents/VoiceBridge 2>/dev/null || open ~/Documents")
      end },
    { title = "Edit config…", fn = function()
        hs.execute("open -t ~/.config/voicebridge/config.toml 2>/dev/null || open -t '" .. DIR .. "/config.example.toml'")
      end },
    { title = "Reload intent modes", fn = function() refreshModes() end },
    { title = "Restart engine (warm)", fn = function() restartDaemon() end },
    { title = "Reload Hammerspoon", fn = function() hs.reload() end },
  }
end)

-- ---- Bind hotkeys --------------------------------------------------------

VB.hotkeys = {
  hs.hotkey.bind(DICTATE_HOTKEY.mods, DICTATE_HOTKEY.key, toggleDictate),
  hs.hotkey.bind(INTENT_HOTKEY.mods, INTENT_HOTKEY.key, dictateWithMode),
  hs.hotkey.bind(TYPE_HOTKEY.mods, TYPE_HOTKEY.key, typePrompt),
  hs.hotkey.bind(WINDOW_HOTKEY.mods, WINDOW_HOTKEY.key, function() openWindow() end),
}

refreshModes()      -- pull the (possibly customized) mode catalog from the engine
refreshSettings()   -- pull backend/model settings + lists for the dropdowns
ensureDaemon()      -- start (or reuse) the warm background engine

-- Debug hooks (callable via the `hs` CLI):
--   voicebridgeTest()            -> render the result panel in isolation
--   voicebridgeProcess("a.wav")  -> run the full engine pipeline on a wav file
_G.voicebridgeTest = function()
  local ok, e = pcall(showResult, "TEST result panel — Copy / Paste / Email / ✕ should work.", false)
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
