-- Alfred (Hammerspoon front-end) — dictation control, the mode/Type-mode
-- picker, and the result preview panel.
-- Split out of voicebridge.lua (see voicebridge.lua's loader comment for the
-- shared-module-scope convention every split file uses).

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
  VB.resultHandlers = nil
end

-- Map a button id to the injected handler the caller supplied via showResult.
RESULT_ACTIONS = { copy = "onCopy", paste = "onPaste",
                         email = "onEmail", discard = "onDiscard" }

-- Pure: route a button id to its injected handler, passing the result text.
-- Returns the handler name that fired, or nil (unknown id / no handler) — the
-- whole click→action contract of the panel, free of hs.* for testing.
function resultDispatch(id, handlers, text)
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
