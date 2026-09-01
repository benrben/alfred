-- Alfred (Hammerspoon front-end) — recording HUD (floating canvas).
-- Split out of voicebridge.lua to stay under the repository's 600-line File
-- LOC gate. Loaded by voicebridge.lua via loadfile(path, "t", SHARED), so
-- every name declared here without `local` (module scope) is visible from
-- every other split file the same way -- see voicebridge.lua's loader comment
-- for why (no monkeypatch seam like the Python engine has; this is purely
-- about keeping cross-file calls looking exactly like ordinary Lua globals
-- without polluting Hammerspoon's real _G).

-- ---- Recording HUD (floating canvas) -------------------------------------

HUD_W = 280
HUD_H_BASE = 44                 -- dot + REC + timer; +26 when the meter shows
MET_X, MET_Y, MET_H = 16, 46, 9
MET_MAXW = HUD_W - MET_X * 2
-- element indices inside the HUD canvas: 2=dot, 4=timer, 6=meter-fill (if shown)

-- Pure: total HUD canvas height -- the base row, +26 when the meter shows.
function hudHeight(showMeter)
  return HUD_H_BASE + (showMeter and 26 or 0)
end

-- Pure: the canvas frame (position + size), centered horizontally on the
-- given screen frame, 90px down from its top.
function hudCanvasFrame(screenFrame, h)
  return {
    x = screenFrame.x + (screenFrame.w - HUD_W) / 2,
    y = screenFrame.y + 90,
    w = HUD_W,
    h = h,
  }
end

-- Pure: the HUD's always-present elements (background, dot, REC label, timer).
function hudBaseElements()
  return {
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
      frame = { x = HUD_W - 96, y = 12, w = 80, h = 24 } },
  }
end

-- Pure: the level-meter track + fill elements, appended only when SHOW_METER.
function hudMeterTrackElements()
  return {
    { type = "rectangle", action = "fill",
      roundedRectRadii = { xRadius = 3, yRadius = 3 },
      fillColor = { white = 1, alpha = 0.12 },
      frame = { x = MET_X, y = MET_Y, w = MET_MAXW, h = MET_H } },
    { type = "rectangle", action = "fill",
      roundedRectRadii = { xRadius = 3, yRadius = 3 },
      fillColor = { red = 0.22, green = 0.85, blue = 0.42, alpha = 0.95 },
      frame = { x = MET_X, y = MET_Y, w = 0, h = MET_H } },
  }
end

function showHUD()
  local h = hudHeight(SHOW_METER)
  local f = hs.screen.mainScreen():frame()
  local c = hs.canvas.new(hudCanvasFrame(f, h))
  c:level(hs.canvas.windowLevels.overlay)
  c:behavior(hs.canvas.windowBehaviors.canJoinAllSpaces)
  c:clickActivating(false)
  c:appendElements(table.unpack(hudBaseElements()))
  if SHOW_METER then
    c:appendElements(table.unpack(hudMeterTrackElements()))
  end
  c:show()
  VB.hud = c
end

-- Pure: whether the level meter's fill element should be (re)drawn -- both
-- SHOW_METER is on AND the HUD canvas actually carries that 6th element.
function hudMeterVisible(showMeter, hud)
  return showMeter and (hud[6] ~= nil)
end

-- Pure: the meter fill's frame for the given (already-decayed) level.
function hudMeterFrame(level)
  local w = math.min(1, level * 1.7) * MET_MAXW
  return { x = MET_X, y = MET_Y, w = w, h = MET_H }
end

-- Pure: the meter fill's color -- amber once the level crosses 0.55, else green.
function hudMeterColor(level)
  if level > 0.55 then
    return { red = 0.95, green = 0.55, blue = 0.20, alpha = 0.95 }
  end
  return { red = 0.22, green = 0.85, blue = 0.42, alpha = 0.95 }
end

-- Pure: the pulsing record-dot color at a given pulse phase.
function hudPulseColor(pulse)
  local a = 0.55 + 0.45 * math.abs(math.sin(pulse))
  return { red = 0.96, green = 0.26, blue = 0.21, alpha = a }
end

function updateHUD()
  pushWindowState()
  if not VB.hud then return end
  local tstr = fmtTime(os.time() - VB.recStart)
  VB.hud[4].text = tstr
  if VB.menubar then VB.menubar:setTitle("🔴 " .. tstr) end

  -- pulse the record dot
  VB.pulse = VB.pulse + 0.18
  VB.hud[2].fillColor = hudPulseColor(VB.pulse)

  -- level meter: decay toward 0, then draw the latest peak
  if hudMeterVisible(SHOW_METER, VB.hud) then
    VB.level = (VB.level or 0) * 0.72
    VB.hud[6].frame = hudMeterFrame(VB.level)
    VB.hud[6].fillColor = hudMeterColor(VB.level)
  end
end

function destroyHUD()
  if VB.hudTimer then VB.hudTimer:stop(); VB.hudTimer = nil end
  if VB.hud then VB.hud:delete(); VB.hud = nil end
  VB.level = 0; VB.pulse = 0
end

-- sox prints a progress line to stderr (with `-S`); the VU meter is the
-- bracketed segment containing a '|'. We turn its "fill" into a 0..1 level.
-- Local (used only below): fraction of `meter`'s characters that are fill
-- (non-space, non-'|'), or nil when the segment is empty.
local function meterFillFraction(meter)
  local fill, total = 0, 0
  for ch in meter:gmatch(".") do
    total = total + 1
    if ch ~= " " and ch ~= "|" then fill = fill + 1 end
  end
  if total > 0 then return fill / total end
  -- unreachable in practice (the regex that produces `meter` always captures
  -- at least the '|', so total >= 1) -- falls off the end, returning nil,
  -- same as the original single-line `if total > 0 then ... end` guard.
end

-- Local (used only below): the running peak, given a newly observed level.
local function nextPeak(peak, lvl)
  if not peak or lvl > peak then return lvl end
  return peak
end

-- Pure: scan a stderr blob and return the PEAK level (0..1) across its meter
-- segments, or nil when no meter is present.
function soxLevel(stdErr)
  local peak = nil
  for seg in (stdErr or ""):gmatch("[^\r\n]+") do
    local meter = seg:match("%[([^%[%]]-|[^%[%]]-)%]")
    if meter then
      local lvl = meterFillFraction(meter)
      if lvl then peak = nextPeak(peak, lvl) end
    end
  end
  return peak
end

function soxStream(_, _, stdErr)
  local lvl = soxLevel(stdErr)
  if lvl then VB.level = math.max(VB.level or 0, lvl) end
  return true
end

