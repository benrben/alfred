-- Plain-lua unit tests for the PURE Hammerspoon helpers in voicebridge.lua.
--
-- No busted / luarocks: built-in `assert` only. The module exposes its pure
-- helpers via a guarded test-export seam that returns BEFORE any hs.* runtime
-- init, but only when the env var VB_LUA_TEST is set. So run me as:
--
--     cd <repo root>
--     VB_LUA_TEST=1 lua tests/lua/test_helpers.lua
--
-- (cwd must be the repo root so the relative loadfile path resolves.)

assert(os.getenv("VB_LUA_TEST"),
  "VB_LUA_TEST must be set, e.g. `VB_LUA_TEST=1 lua tests/lua/test_helpers.lua`")

local H = assert(loadfile("voicebridge.lua"))()
assert(type(H) == "table", "module did not return the test-export table")

-- ---- tiny assert harness -------------------------------------------------
local passed, failed = 0, 0
local function check(cond, name)
  if cond then
    passed = passed + 1
  else
    failed = failed + 1
    io.stderr:write("  FAIL: " .. name .. "\n")
  end
end
local function eq(got, want, name)
  check(got == want, name .. " (got " .. tostring(got) .. ", want " .. tostring(want) .. ")")
end

-- =====================================================================
-- parseStatus: splits a "VB_STATUS\t..." line into its tab-separated parts,
-- returns an array of those parts, or nil when no status line is present.
-- =====================================================================
do
  local p = H.parseStatus("VB_STATUS\tcopied")
  check(type(p) == "table", "parseStatus(copied) returns a table")
  eq(#p, 1, "parseStatus(copied) has 1 part")
  eq(p[1], "copied", "parseStatus(copied)[1] == 'copied'")

  local q = H.parseStatus("VB_STATUS\tsaved\t/p.md")
  check(type(q) == "table", "parseStatus(saved) returns a table")
  eq(#q, 2, "parseStatus(saved) has 2 parts")
  eq(q[1], "saved", "parseStatus(saved)[1] == 'saved'")
  eq(q[2], "/p.md", "parseStatus(saved)[2] == '/p.md'")

  -- A non-status line yields nil.
  eq(H.parseStatus("just some text"), nil, "parseStatus(non-status) == nil")
  eq(H.parseStatus(""), nil, "parseStatus(empty) == nil")
  eq(H.parseStatus(nil), nil, "parseStatus(nil) == nil")

  -- The status line is found even when surrounded by other output lines.
  local r = H.parseStatus("hello\nVB_STATUS\tcopied\nworld")
  check(r ~= nil and r[1] == "copied", "parseStatus finds status amid other lines")
end

-- =====================================================================
-- buildModes: turns a catalog (array of {key,label,description,prompt,default})
-- into the picker list. Entry 1 is the Default (borrowing the default mode's
-- key/prompt/label), then one entry per catalog item, and a trailing Raw entry.
-- =====================================================================
do
  local catalog = {
    { key = "email",  label = "Email",  description = "Polished email" },
    { key = "commit", label = "Commit", description = "Git commit message",
      prompt = "write a commit", default = true },
  }
  local m = H.buildModes(catalog)
  check(type(m) == "table", "buildModes returns a table")
  -- 1 Default + 2 catalog entries + 1 trailing Raw = 4
  eq(#m, 4, "buildModes length = default + catalog + raw")

  -- Default entry (first) borrows the default mode's key/label/prompt.
  eq(m[1].text, "Default · Commit", "Default entry borrows default label")
  eq(m[1].key, "commit", "Default entry borrows default key")
  eq(m[1].prompt, "write a commit", "Default entry borrows default prompt")
  eq(#m[1].flags, 0, "Default entry carries no per-capture flags")

  -- Catalog entries map text<-label, subText<-description, key/prompt, and
  -- carry the --mode/--rewrite flags.
  eq(m[2].text, "Email", "catalog entry text == label")
  eq(m[2].subText, "Polished email", "catalog entry subText == description")
  eq(m[2].key, "email", "catalog entry key preserved")
  eq(m[2].flags[1], "--mode", "catalog entry flag[1] == --mode")
  eq(m[2].flags[2], "email", "catalog entry flag[2] == key")
  eq(m[2].flags[3], "--rewrite", "catalog entry flag[3] == --rewrite")

  -- The Raw transcript pseudo-entry is always LAST.
  eq(m[#m].text, "Raw transcript", "last entry is Raw transcript")
  check(#m[#m].flags >= 1, "Raw entry carries disabling flags")

  -- With no default flagged, the Default entry falls back to the generic label.
  local plain = H.buildModes({ { key = "notes", label = "Notes" } })
  eq(plain[1].text, "Default (use config)", "Default falls back when no default mode")
  eq(plain[1].key, nil, "Default key is nil when no default mode")

  -- Ordering: Default first, items in catalog order, Raw last.
  local order = H.buildModes({
    { key = "a", label = "A" }, { key = "b", label = "B" }, { key = "c", label = "C" },
  })
  eq(order[1].text, "Default (use config)", "order: Default first")
  eq(order[2].key, "a", "order: catalog item 1")
  eq(order[3].key, "b", "order: catalog item 2")
  eq(order[4].key, "c", "order: catalog item 3")
  eq(order[#order].text, "Raw transcript", "order: Raw last")
end

-- =====================================================================
-- modesForJS: projects the load-time MODES list into JS objects
-- {label, prompt, key}. MODES = buildModes(BUILTIN_CATALOG) at load.
-- =====================================================================
do
  local js = H.modesForJS()
  check(type(js) == "table" and #js >= 2, "modesForJS returns a non-trivial list")
  for i, e in ipairs(js) do
    check(e.label ~= nil, "modesForJS["..i.."].label present")
    check(e.prompt ~= nil, "modesForJS["..i.."].prompt present")
    check(e.key ~= nil, "modesForJS["..i.."].key present")
  end
  -- First is the Default entry, last is the Raw transcript entry.
  check(js[1].label:match("^Default"), "modesForJS first label starts with Default")
  eq(js[#js].label, "Raw transcript", "modesForJS last label is Raw transcript")
end

-- =====================================================================
-- fmtTime: "%02d:%02d" of minutes:seconds.
-- =====================================================================
do
  eq(H.fmtTime(0), "00:00", "fmtTime(0)")
  eq(H.fmtTime(5), "00:05", "fmtTime(5)")
  eq(H.fmtTime(65), "01:05", "fmtTime(65)")
  eq(H.fmtTime(600), "10:00", "fmtTime(600)")
  eq(H.fmtTime(3599), "59:59", "fmtTime(3599)")
end

-- =====================================================================
-- RESULT_ACTIONS: button id -> injected-handler name.
-- =====================================================================
do
  eq(H.RESULT_ACTIONS.copy, "onCopy", "RESULT_ACTIONS.copy")
  eq(H.RESULT_ACTIONS.paste, "onPaste", "RESULT_ACTIONS.paste")
  eq(H.RESULT_ACTIONS.email, "onEmail", "RESULT_ACTIONS.email")
  eq(H.RESULT_ACTIONS.discard, "onDiscard", "RESULT_ACTIONS.discard")
  -- No stray mappings.
  local n = 0
  for _ in pairs(H.RESULT_ACTIONS) do n = n + 1 end
  eq(n, 4, "RESULT_ACTIONS has exactly 4 entries")
end

-- =====================================================================
-- resultDispatch (hs-result-panel): a button id routes to its injected
-- handler, which receives the result text; returns the handler name fired.
-- =====================================================================
do
  local fired, got = nil, nil
  local handlers = {
    onCopy    = function(t) fired = "onCopy";    got = t end,
    onPaste   = function(t) fired = "onPaste";   got = t end,
    onEmail   = function(t) fired = "onEmail";   got = t end,
    onDiscard = function(t) fired = "onDiscard"; got = t end,
  }
  eq(H.resultDispatch("copy", handlers, "hello"), "onCopy", "dispatch copy -> onCopy")
  eq(fired, "onCopy", "copy fired onCopy")
  eq(got, "hello", "handler received the result text")

  eq(H.resultDispatch("paste", handlers, "x"), "onPaste", "dispatch paste -> onPaste")
  eq(H.resultDispatch("email", handlers, "x"), "onEmail", "dispatch email -> onEmail")
  eq(H.resultDispatch("discard", handlers, "x"), "onDiscard", "dispatch discard -> onDiscard")

  -- Unknown id and missing handler are both no-ops returning nil.
  eq(H.resultDispatch("nope", handlers, "x"), nil, "unknown id -> nil")
  eq(H.resultDispatch("copy", {}, "x"), nil, "missing handler -> nil")
  eq(H.resultDispatch("copy", nil, "x"), nil, "nil handlers -> nil (no crash)")
end

-- =====================================================================
-- iconForState (hs-recording): the menubar glyph per state, idle fallback.
-- =====================================================================
do
  eq(H.iconForState("idle"), "🎙️", "iconForState(idle)")
  eq(H.iconForState("recording"), "🔴", "iconForState(recording)")
  eq(H.iconForState("processing"), "⏳", "iconForState(processing)")
  eq(H.iconForState("garbage"), "🎙️", "iconForState(unknown) falls back to idle")
  eq(H.iconForState(nil), "🎙️", "iconForState(nil) falls back to idle")
end

-- =====================================================================
-- soxLevel (hs-recording): parse a sox `-S` stderr blob into the PEAK VU
-- level (0..1). The meter is the bracketed segment containing a '|'; fill =
-- non-space, non-'|' chars over the segment width.
-- =====================================================================
do
  eq(H.soxLevel(nil), nil, "soxLevel(nil) -> nil")
  eq(H.soxLevel(""), nil, "soxLevel(empty) -> nil")
  eq(H.soxLevel("In:0.00% 00:00:01 [no meter here]"), nil,
     "soxLevel without a '|' segment -> nil")

  -- A meter of width 10 with 5 filled '=' chars -> 0.5. (Bracket content must
  -- contain a '|' to be recognised as the VU segment.)
  local lvl = H.soxLevel("In: 12% [=====|    ]")
  check(lvl ~= nil and math.abs(lvl - 5/10) < 1e-9,
        "soxLevel fill 5 of 10 -> 0.5 (got " .. tostring(lvl) .. ")")

  -- All filled (only the '|' is "empty"): 9 of 10 -> 0.9.
  local hot = H.soxLevel("[=========|]")
  check(hot ~= nil and math.abs(hot - 9/10) < 1e-9,
        "soxLevel nearly full -> 0.9 (got " .. tostring(hot) .. ")")

  -- Silence: only the divider, no fill -> 0.
  local quiet = H.soxLevel("[    |     ]")
  check(quiet ~= nil and math.abs(quiet) < 1e-9,
        "soxLevel silence -> 0 (got " .. tostring(quiet) .. ")")

  -- Across multiple lines, the PEAK wins.
  local peak = H.soxLevel("[==|       ]\n[========|=]\n[===|      ]")
  check(peak ~= nil and peak > 0.8, "soxLevel takes the peak across lines")
end

-- =====================================================================
-- hudHeight / hudCanvasFrame / hudBaseElements / hudMeterTrackElements
-- (hud): the pure layout + element-table math behind showHUD(). Constants:
-- HUD_W=280, HUD_H_BASE=44, MET_X/MET_Y/MET_H=16/46/9, MET_MAXW=248.
-- =====================================================================
do
  eq(H.hudHeight(true), H.HUD_H_BASE + 26, "hudHeight(true) adds the meter row")
  eq(H.hudHeight(false), H.HUD_H_BASE, "hudHeight(false) is just the base row")
  eq(H.hudHeight(nil), H.HUD_H_BASE, "hudHeight(nil) behaves like false")

  local f = H.hudCanvasFrame({ x = 0, y = 0, w = 1000, h = 700 }, 50)
  eq(f.x, (1000 - H.HUD_W) / 2, "hudCanvasFrame centers x on a zero-origin screen")
  eq(f.y, 90, "hudCanvasFrame sits 90px from the screen's top")
  eq(f.w, H.HUD_W, "hudCanvasFrame width is always HUD_W")
  eq(f.h, 50, "hudCanvasFrame carries through the given height")

  -- A second monitor's frame is rarely at the origin -- the offset must ride
  -- along, not just the centering math.
  local f2 = H.hudCanvasFrame({ x = 200, y = -50, w = 800, h = 600 }, 44)
  eq(f2.x, 200 + (800 - H.HUD_W) / 2, "hudCanvasFrame honours a non-zero screen x")
  eq(f2.y, -50 + 90, "hudCanvasFrame honours a non-zero (even negative) screen y")

  local base = H.hudBaseElements()
  eq(#base, 4, "hudBaseElements returns exactly 4 elements")
  eq(base[1].type, "rectangle", "hudBaseElements[1] is the background")
  eq(base[2].type, "circle", "hudBaseElements[2] is the record dot")
  eq(base[3].text, "REC", "hudBaseElements[3] is the REC label")
  eq(base[4].text, "00:00", "hudBaseElements[4] is the initial timer text")

  local meter = H.hudMeterTrackElements()
  eq(#meter, 2, "hudMeterTrackElements returns exactly 2 elements")
  eq(meter[1].frame.w, H.MET_MAXW, "hudMeterTrackElements[1] (track) spans the full width")
  eq(meter[2].frame.w, 0, "hudMeterTrackElements[2] (fill) starts at zero width")
end

-- =====================================================================
-- showHUD (hud): builds the floating HUD canvas via hs.canvas. `hs` is
-- normally the real Hammerspoon API (absent under plain lua) -- faked here
-- so the exact sequence of canvas calls, and their arguments, can be
-- captured and asserted without a live Hammerspoon.
-- =====================================================================
do
  local calls = {}
  local function record(name, ...)
    calls[#calls + 1] = { name = name, n = select("#", ...), ... }
  end
  local canvas = {}
  canvas.level = function(_, v) record("level", v) end
  canvas.behavior = function(_, v) record("behavior", v) end
  canvas.clickActivating = function(_, v) record("clickActivating", v) end
  canvas.appendElements = function(_, ...) record("appendElements", ...) end
  canvas.show = function(_) record("show") end

  local capturedRect
  hs = {
    screen = { mainScreen = function()
      return { frame = function() return { x = 100, y = 50, w = 1440, h = 900 } end }
    end },
    canvas = {
      new = function(rect) capturedRect = rect; return canvas end,
      windowLevels = { overlay = "OVERLAY_LEVEL" },
      windowBehaviors = { canJoinAllSpaces = "JOIN_ALL_SPACES" },
    },
  }

  H.VB.hud = nil
  H.showHUD()

  -- HUD_W=280, HUD_H_BASE=44; SHOW_METER defaults true so h = 44+26 = 70.
  eq(capturedRect.x, 100 + (1440 - 280) / 2, "showHUD centers the canvas on the screen frame")
  eq(capturedRect.y, 50 + 90, "showHUD positions the canvas 90px from the screen top")
  eq(capturedRect.w, 280, "showHUD sizes the canvas to HUD_W")
  eq(capturedRect.h, 70, "showHUD sizes the canvas for the (default-on) meter row")

  eq(calls[1].name, "level", "showHUD sets the canvas level 1st")
  eq(calls[1][1], "OVERLAY_LEVEL", "showHUD uses hs.canvas.windowLevels.overlay")
  eq(calls[2].name, "behavior", "showHUD sets the canvas behavior 2nd")
  eq(calls[2][1], "JOIN_ALL_SPACES", "showHUD uses windowBehaviors.canJoinAllSpaces")
  eq(calls[3].name, "clickActivating", "showHUD disables click-activation 3rd")
  eq(calls[3][1], false, "showHUD passes false to clickActivating")
  eq(calls[4].name, "appendElements", "showHUD appends the base elements 4th")
  eq(calls[4].n, 4, "showHUD appends exactly the 4 base elements")
  eq(calls[5].name, "appendElements", "showHUD appends the meter elements 5th (SHOW_METER on)")
  eq(calls[5].n, 2, "showHUD appends exactly the 2 meter elements")
  eq(calls[6].name, "show", "showHUD shows the canvas last")
  eq(#calls, 6, "showHUD makes exactly 6 canvas calls")

  check(H.VB.hud == canvas, "showHUD stores the new canvas on VB.hud")

  H.VB.hud = nil
  hs = nil
end

-- =====================================================================
-- hudMeterVisible / hudMeterFrame / hudMeterColor / hudPulseColor (hud):
-- the pure per-tick math behind updateHUD().
-- =====================================================================
do
  eq(H.hudMeterVisible(true, { [6] = {} }), true,
     "hudMeterVisible: meter on + element present -> true")
  eq(H.hudMeterVisible(true, {}), false,
     "hudMeterVisible: meter on but no 6th element -> false")
  eq(H.hudMeterVisible(false, { [6] = {} }), false,
     "hudMeterVisible: meter off -> false regardless of the element")

  local silent = H.hudMeterFrame(0)
  eq(silent.x, H.MET_X, "hudMeterFrame.x is always MET_X")
  eq(silent.y, H.MET_Y, "hudMeterFrame.y is always MET_Y")
  eq(silent.h, H.MET_H, "hudMeterFrame.h is always MET_H")
  eq(silent.w, 0, "hudMeterFrame(0) has zero width")

  local full = H.hudMeterFrame(1)
  eq(full.w, H.MET_MAXW, "hudMeterFrame(1) reaches the full track width")

  local over = H.hudMeterFrame(5)
  eq(over.w, H.MET_MAXW, "hudMeterFrame clamps levels above 1 to the full width too")

  local mid = H.hudMeterFrame(0.3)
  eq(mid.w, math.min(1, 0.3 * 1.7) * H.MET_MAXW,
     "hudMeterFrame(0.3) scales width by level*1.7")

  eq(H.hudMeterColor(0.9).red, 0.95, "hudMeterColor > 0.55 is amber (red 0.95)")
  eq(H.hudMeterColor(0.56).red, 0.95, "hudMeterColor just above 0.55 is amber")
  eq(H.hudMeterColor(0.55).red, 0.22, "hudMeterColor AT 0.55 is still green (strict >)")
  eq(H.hudMeterColor(0).red, 0.22, "hudMeterColor at 0 is green")

  local c0 = H.hudPulseColor(0)
  eq(c0.alpha, 0.55, "hudPulseColor(0): sin(0)=0 -> alpha at its minimum (0.55)")
  eq(c0.red, 0.96, "hudPulseColor red channel is fixed")
  local cMax = H.hudPulseColor(math.pi / 2)
  check(math.abs(cMax.alpha - 1.0) < 1e-9,
        "hudPulseColor(pi/2): sin=1 -> alpha at its maximum (~1.0)")
end

-- =====================================================================
-- updateHUD (hud): refreshes the HUD's timer/dot/meter each tick. No hs.*
-- calls of its own -- VB.hud's "elements" are just index-assignable stand-in
-- tables from here, and VB.menubar is a stub with setTitle.
-- =====================================================================
do
  H.VB.win = nil   -- pushWindowState() no-ops without a window
  H.VB.hud = nil
  H.updateHUD()    -- guard clause: nothing to update yet, must not error
  check(true, "updateHUD no-ops safely with no VB.hud")

  H.VB.hud = { [2] = {}, [4] = {}, [6] = {} }
  H.VB.recStart = os.time() - 5   -- a few seconds ago, not epoch 0 (fmtTime
                                   -- pads minutes to >= 2 digits, so an
                                   -- elapsed-since-epoch value would overflow
                                   -- a "two digit minutes" pattern below)
  H.VB.pulse = 0
  H.VB.level = 0.9
  local titles = {}
  H.VB.menubar = { setTitle = function(_, t) titles[#titles + 1] = t end }

  H.updateHUD()

  check(H.VB.hud[4].text ~= nil and H.VB.hud[4].text:match("^%d%d:%d%d$") ~= nil,
        "updateHUD sets the timer text in MM:SS format")
  eq(#titles, 1, "updateHUD sets the menubar title once")
  check(titles[1]:find("🔴", 1, true) ~= nil, "menubar title carries the recording glyph")
  check(H.VB.hud[2].fillColor ~= nil, "updateHUD sets the pulsing dot's fillColor")
  eq(H.VB.pulse, 0.18, "updateHUD advances VB.pulse by 0.18")
  check(H.VB.hud[6].frame ~= nil, "updateHUD sets the meter fill frame (meter visible)")
  check(H.VB.hud[6].fillColor ~= nil, "updateHUD sets the meter fill color (meter visible)")
  check(math.abs(H.VB.level - 0.9 * 0.72) < 1e-9,
        "updateHUD decays VB.level by 0.72 before redrawing the meter")

  H.VB.hud = nil; H.VB.menubar = nil; H.VB.pulse = 0; H.VB.level = 0
end

-- =====================================================================
-- destroyHUD (hud): tears down the HUD timer + canvas and resets pulse
-- state. No hs.* calls of its own -- VB.hudTimer/VB.hud are stand-ins here.
-- =====================================================================
do
  local stopped, deleted = false, false
  H.VB.hudTimer = { stop = function() stopped = true end }
  H.VB.hud = { delete = function() deleted = true end }
  H.VB.level = 0.5
  H.VB.pulse = 1.2

  H.destroyHUD()

  check(stopped, "destroyHUD stops the HUD timer")
  check(deleted, "destroyHUD deletes the HUD canvas")
  eq(H.VB.hudTimer, nil, "destroyHUD clears VB.hudTimer")
  eq(H.VB.hud, nil, "destroyHUD clears VB.hud")
  eq(H.VB.level, 0, "destroyHUD resets VB.level")
  eq(H.VB.pulse, 0, "destroyHUD resets VB.pulse")

  -- Guard clauses: tearing down an already-torn-down HUD must not error.
  H.destroyHUD()
  check(true, "destroyHUD no-ops safely when already torn down")
end

-- =====================================================================
-- soxStream (hud): folds sox's `-S` stderr into VB.level. No hs.* calls --
-- just the one VB.level write.
-- =====================================================================
do
  H.VB.level = 0.2
  local ok = H.soxStream(nil, nil, "In: 90% [=========|] Out:0")
  eq(ok, true, "soxStream always returns true")
  check(math.abs(H.VB.level - 9 / 10) < 1e-9,
        "soxStream raises VB.level to the observed peak (9/10)")

  -- No bracketed meter in the blob -> soxLevel finds nothing -> unchanged.
  H.VB.level = 0.3
  H.soxStream(nil, nil, "no meter in this line")
  eq(H.VB.level, 0.3, "soxStream leaves VB.level unchanged when soxLevel returns nil")

  -- A lower reading never pulls VB.level back down -- decay is updateHUD's
  -- job; soxStream only ever raises toward a fresh peak.
  H.VB.level = 0.9
  H.soxStream(nil, nil, "[    |     ]")
  eq(H.VB.level, 0.9, "soxStream never lowers VB.level")

  H.VB.level = 0
end

-- =====================================================================
-- buildCaptureFlags (hs-app-window): mode flags + translate toggle -> argv.
-- =====================================================================
do
  local on = H.buildCaptureFlags({ "--mode", "email", "--rewrite" }, true)
  eq(#on, 4, "buildCaptureFlags appends one translate flag")
  eq(on[1], "--mode", "mode flags preserved [1]")
  eq(on[2], "email", "mode flags preserved [2]")
  eq(on[3], "--rewrite", "mode flags preserved [3]")
  eq(on[4], "--translate", "translate=true -> --translate")

  local off = H.buildCaptureFlags({ "--mode", "raw" }, false)
  eq(off[#off], "--no-translate", "translate=false -> --no-translate")

  local empty = H.buildCaptureFlags(nil, true)
  eq(#empty, 1, "nil mode flags -> just the translate flag")
  eq(empty[1], "--translate", "nil mode flags + translate -> --translate")
end

-- =====================================================================
-- normalizeBackend (hs-app-window): "" / "default" / nil all mean config
-- default (nil); any other value is the backend name verbatim.
-- =====================================================================
do
  eq(H.normalizeBackend(nil), nil, "normalizeBackend(nil) -> nil")
  eq(H.normalizeBackend(""), nil, "normalizeBackend('') -> nil")
  eq(H.normalizeBackend("default"), nil, "normalizeBackend('default') -> nil")
  eq(H.normalizeBackend("auto"), "auto", "normalizeBackend('auto') -> 'auto'")
  eq(H.normalizeBackend("claude"), "claude", "normalizeBackend('claude') -> 'claude'")
  eq(H.normalizeBackend("codex"), "codex", "normalizeBackend('codex') -> 'codex'")
end

-- =====================================================================
-- normalizeTranslate (hs-app-window): any value coerced to a strict boolean.
-- =====================================================================
do
  eq(H.normalizeTranslate(true), true, "normalizeTranslate(true) -> true")
  eq(H.normalizeTranslate(false), false, "normalizeTranslate(false) -> false")
  eq(H.normalizeTranslate(nil), false, "normalizeTranslate(nil) -> false")
  eq(H.normalizeTranslate("yes"), true, "normalizeTranslate(truthy) -> true")
  eq(H.normalizeTranslate(0), true, "normalizeTranslate(0) -> true (0 is truthy in lua)")
end

-- =====================================================================
-- buildMenu (hs-hotkey-menubar): the menubar model for (state, backend).
-- Header reflects state+backend; the backend submenu radio is checked for the
-- active backend; injected `actions` are wired to the right items.
-- =====================================================================
do
  local menu = H.buildMenu("idle", nil, {})
  check(type(menu) == "table", "buildMenu returns a table")
  check(menu[1].title:match("^Alfred — idle"), "header shows state")
  eq(menu[1].disabled, true, "header is a disabled label")
  eq(menu[2].title, "-", "second item is a separator")

  -- Backend submenu: with backend=nil, "Default (config)" is checked, others not.
  local function backendSub(m)
    for _, it in ipairs(m) do if it.title == "Backend" then return it.menu end end
  end
  local sub = backendSub(menu)
  -- Default + the 4 real backends (auto/claude/codex/local). "local" is the
  -- engine's DEFAULT backend and used to be missing from BOTH UI lists.
  check(sub ~= nil and #sub == 5, "backend submenu has Default + 4 backends")
  eq(sub[1].title, "Default (config)", "backend[1] is Default")
  eq(sub[1].checked, true, "backend nil -> Default checked")
  eq(sub[2].checked, false, "backend nil -> auto unchecked")
  eq(sub[#sub].title, "local", "backend submenu includes 'local' (drift bug fixed)")

  -- With backend='claude', the header shows it and the claude radio is checked.
  local m2 = H.buildMenu("recording", "claude", {})
  check(m2[1].title:match("claude"), "header shows active backend")
  local sub2 = backendSub(m2)
  eq(sub2[1].checked, false, "backend claude -> Default unchecked")
  eq(sub2[3].checked, true, "backend claude -> claude radio checked")

  -- Injected actions are wired: clicking a backend radio invokes setBackend(value).
  local picked = "UNSET"
  local m3 = H.buildMenu("idle", nil, { setBackend = function(v) picked = v end })
  local sub3 = backendSub(m3)
  sub3[2].fn()                     -- "auto" radio
  eq(picked, "auto", "auto radio calls setBackend('auto')")
  sub3[1].fn()                     -- "Default (config)" radio
  eq(picked, nil, "Default radio calls setBackend(nil)")

  -- Top-level action items are wired to the injected callbacks.
  local hits = {}
  local m4 = H.buildMenu("idle", nil, {
    toggleDictate = function() hits.dictate = true end,
    toggleTranscribeOnly = function() hits.rawtx = true end,
    cancel        = function() hits.cancel = true end,
  })
  local function itemByTitle(m, t)
    for _, it in ipairs(m) do if it.title == t then return it end end
  end
  itemByTitle(m4, "Dictate (toggle)").fn()
  itemByTitle(m4, "Transcribe only").fn()
  itemByTitle(m4, "Cancel recording").fn()
  eq(hits.dictate, true, "Dictate item wired to toggleDictate")
  eq(hits.rawtx, true, "Transcribe only item wired to toggleTranscribeOnly")
  eq(hits.cancel, true, "Cancel item wired to cancel")
end

-- =====================================================================
-- BACKENDS (hs-hotkey-menubar / hs-app-window): the single backend list that
-- feeds both the menubar submenu and the window <select>. Must include "local".
-- =====================================================================
do
  check(type(H.BACKENDS) == "table", "BACKENDS is a table")
  local has = {}
  for _, b in ipairs(H.BACKENDS) do has[b] = true end
  check(has["auto"], "BACKENDS has auto")
  check(has["claude"], "BACKENDS has claude")
  check(has["codex"], "BACKENDS has codex")
  check(has["local"], "BACKENDS has local (the engine's default backend)")
end

-- =====================================================================
-- resultBanner (engine-result): the "copied" banner, raw vs polished.
-- =====================================================================
do
  eq(H.resultBanner(false), "Copied to clipboard ✓", "resultBanner(ok)")
  eq(H.resultBanner(true), "Copied raw transcript (LLM step failed)", "resultBanner(llmFailed)")
end

-- =====================================================================
-- errorMessage (engine-result): subtype -> human message, with an optional
-- stderr tail appended in parens; unknown subtype -> generic fallback.
-- =====================================================================
do
  eq(H.errorMessage("audio_not_found", nil), "No audio to transcribe.", "errorMessage(audio_not_found)")
  eq(H.errorMessage("stt_failed", nil), "Transcription failed.", "errorMessage(stt_failed)")
  eq(H.errorMessage("llm_failed", nil), "The rewrite step failed.", "errorMessage(llm_failed)")
  eq(H.errorMessage("deliver_failed", nil),
     "Couldn't deliver the result (see History — it was saved there).",
     "errorMessage(deliver_failed)")
  eq(H.errorMessage("runtime", nil), "The engine hit an error.", "errorMessage(runtime)")
  eq(H.errorMessage("weird_new_kind", nil), "Something went wrong.", "errorMessage(unknown) -> generic")
  eq(H.errorMessage("runtime", "boom: nope"), "The engine hit an error. (boom: nope)",
     "errorMessage appends the stderr tail")
  eq(H.errorMessage("runtime", ""), "The engine hit an error.", "errorMessage ignores an empty tail")
end

-- =====================================================================
-- resultPayload (engine-result): pull the still-ENCODED VB_RESULT payload from
-- stdout (the line BEFORE VB_STATUS), or nil when the engine emitted none.
-- =====================================================================
do
  eq(H.resultPayload(nil), nil, "resultPayload(nil) -> nil")
  eq(H.resultPayload(""), nil, "resultPayload(empty) -> nil")
  eq(H.resultPayload("VB_STATUS\tcopied"), nil, "resultPayload with no result line -> nil")
  eq(H.resultPayload('VB_RESULT\t"hi"\nVB_STATUS\tcopied'), '"hi"',
     "resultPayload returns the encoded payload (still JSON)")
  -- Found even amid other stderr/stdout noise.
  eq(H.resultPayload('note: blah\nVB_RESULT\t"x"\nVB_STATUS\tcopied'), '"x"',
     "resultPayload found amid other lines")
end

-- =====================================================================
-- classifyResult (engine-result): the whole finished-run classification, hs-free
-- via an injected json decoder. `decode` here just strips the surrounding quotes
-- of the simple test payloads (a stand-in for hs.json.decode).
-- =====================================================================
do
  local decode = function(s)
    if type(s) ~= "string" then error("not a string") end
    return (s:gsub('^"', ''):gsub('"$', ''))    -- naive de-quote for the fixtures
  end

  -- Plain copied: text comes from VB_RESULT (preferred over the clipboard).
  local c = H.classifyResult(0, 'VB_RESULT\t"hello world"\nVB_STATUS\tcopied', "", decode)
  eq(c.kind, "copied", "classify copied kind")
  eq(c.text, "hello world", "classify copied text from VB_RESULT")
  eq(c.llmFailed, false, "classify copied not llmFailed")
  eq(c.pasteFailed, false, "classify copied not pasteFailed")
  eq(c.path, nil, "classify copied has no path")
  eq(c.subtype, nil, "classify copied has no subtype")

  -- No VB_RESULT line -> text nil (caller falls back to the clipboard).
  local c2 = H.classifyResult(0, "VB_STATUS\tcopied", "", decode)
  eq(c2.kind, "copied", "classify copied (no result line) kind")
  eq(c2.text, nil, "no VB_RESULT -> text nil (clipboard fallback)")

  -- llm_failed suffix: raw transcript delivered.
  local c3 = H.classifyResult(0, 'VB_RESULT\t"raw"\nVB_STATUS\tcopied\tllm_failed', "", decode)
  eq(c3.kind, "copied", "classify llm_failed kind is copied")
  eq(c3.llmFailed, true, "classify detects llm_failed suffix")
  eq(c3.text, "raw", "classify llm_failed still has the raw text")

  -- paste_failed suffix (and combined with llm_failed, order: paste then llm).
  local c4 = H.classifyResult(0, "VB_STATUS\tcopied\tpaste_failed", "", decode)
  eq(c4.pasteFailed, true, "classify detects paste_failed suffix")
  eq(c4.llmFailed, false, "paste_failed alone is not llmFailed")
  local c5 = H.classifyResult(0, "VB_STATUS\tcopied\tpaste_failed\tllm_failed", "", decode)
  eq(c5.pasteFailed, true, "classify detects paste_failed with llm_failed")
  eq(c5.llmFailed, true, "classify detects llm_failed as the last suffix")

  -- saved: path in parts[2].
  local c6 = H.classifyResult(0, "VB_STATUS\tsaved\t/tmp/out.md", "", decode)
  eq(c6.kind, "saved", "classify saved kind")
  eq(c6.path, "/tmp/out.md", "classify saved path")

  -- empty.
  eq(H.classifyResult(0, "VB_STATUS\tempty", "", decode).kind, "empty", "classify empty kind")

  -- error: subtype in parts[2]; tail is the last non-blank stderr line.
  local c7 = H.classifyResult(1, "VB_STATUS\terror\tstt_failed",
    "some noise\nerror: transcription failed: boom\n", decode)
  eq(c7.kind, "error", "classify error kind")
  eq(c7.subtype, "stt_failed", "classify error subtype")
  eq(c7.tail, "error: transcription failed: boom", "classify error tail = last stderr line")

  -- Compound error: delivery failed AFTER the LLM stage also failed (the
  -- raw-transcript fallback whose own delivery then blew up) — subtype must
  -- read parts[2] ("deliver_failed"), independent of the trailing llm_failed.
  local c9 = H.classifyResult(1, "VB_STATUS\terror\tdeliver_failed\tllm_failed", "", decode)
  eq(c9.kind, "error", "classify compound error kind")
  eq(c9.subtype, "deliver_failed", "classify compound error subtype is deliver_failed, not llm_failed")
  eq(c9.llmFailed, true, "classify compound error still detects the trailing llm_failed")

  -- No status line at all -> kind nil, tail still surfaced from stderr.
  local c8 = H.classifyResult(1, "just a traceback", "Traceback...\nValueError: x", decode)
  eq(c8.kind, nil, "classify no-status kind nil")
  eq(c8.tail, "ValueError: x", "classify no-status still exposes the stderr tail")
end

-- =====================================================================
-- classifyPost (engine-client): interpret an hs.http POST outcome. Only a
-- genuine connection-refused (-1004) is "down" (safe to one-shot re-run); the
-- ~60s timeout and everything else is "busy" (do NOT re-run -> no double write).
-- =====================================================================
do
  eq(H.classifyPost(200, true), "ok", "200 + body -> ok")
  eq(H.classifyPost(200, false), "busy", "200 without a body -> busy (don't re-run)")
  eq(H.classifyPost(-1004, false), "down", "connection refused (-1004) -> down")
  eq(H.classifyPost(-1001, false), "busy", "timeout (-1001) -> busy (the double-run bug)")
  eq(H.classifyPost(-1005, false), "busy", "mid-flight reset (-1005) -> busy (engine owns the job)")
  eq(H.classifyPost(500, false), "busy", "5xx -> busy")
end

-- =====================================================================
-- buildEngineArgv (engine-client): base subcommand + per-capture flags +
-- optional backend override, in order.
-- =====================================================================
do
  local a = H.buildEngineArgv({ "process", "/x.wav" }, nil, nil)
  eq(#a, 2, "buildEngineArgv base only")
  eq(a[1], "process", "argv[1]")
  eq(a[2], "/x.wav", "argv[2]")

  local b = H.buildEngineArgv({ "text", "hi" }, { "--mode", "email", "--rewrite" }, "claude")
  eq(#b, 7, "buildEngineArgv base(2) + flags(3) + --backend + value")
  eq(b[1], "text", "base subcommand still first")
  eq(b[3], "--mode", "flags appended after base")
  eq(b[4], "email", "flag value preserved")
  eq(b[5], "--rewrite", "rewrite flag preserved")
  eq(b[6], "--backend", "backend flag name precedes value")
  eq(b[7], "claude", "backend value appended last")
  -- backend flag name precedes the value.
  local c = H.buildEngineArgv({ "process", "/x.wav" }, {}, "codex")
  eq(c[3], "--backend", "backend adds --backend")
  eq(c[4], "codex", "backend value follows --backend")
  -- nil backend adds nothing.
  eq(#H.buildEngineArgv({ "process", "/x.wav" }, {}, nil), 2, "nil backend adds nothing")
end

-- =====================================================================
-- parseHistory (hs-app-window): newest-first records from JSONL lines via an
-- injected decoder; honours the limit window and skips undecodable lines.
-- =====================================================================
do
  -- Decoder stand-in: a line "bad" throws; "empty" decodes without text; others
  -- become {text=line, ts="T"..line, chars=#line}.
  local decode = function(l)
    if l == "bad" then error("boom") end
    if l == "notext" then return { ts = "T", chars = 0 } end
    return { text = l, ts = "T" .. l, chars = #l }
  end

  local lines = { "a", "b", "c", "d" }
  local items = H.parseHistory(lines, 30, decode)
  eq(#items, 4, "parseHistory returns all within limit")
  eq(items[1].text, "d", "parseHistory is newest-first (last line first)")
  eq(items[2].text, "c", "parseHistory order [2]")
  eq(items[1].ts, "Td", "parseHistory carries ts")
  eq(items[1].chars, 1, "parseHistory carries chars")

  -- Limit window: only the most recent N.
  local two = H.parseHistory({ "a", "b", "c", "d" }, 2, decode)
  eq(#two, 2, "parseHistory honours the limit")
  eq(two[1].text, "d", "parseHistory limit keeps the newest")
  eq(two[2].text, "c", "parseHistory limit second newest")

  -- Undecodable + text-less lines are skipped, the rest survive.
  local mixed = H.parseHistory({ "x", "bad", "notext", "y" }, 30, decode)
  eq(#mixed, 2, "parseHistory skips bad + text-less lines")
  eq(mixed[1].text, "y", "parseHistory skip: newest good first")
  eq(mixed[2].text, "x", "parseHistory skip: older good second")

  -- Empty / nil input.
  eq(#H.parseHistory({}, 30, decode), 0, "parseHistory empty -> {}")
  eq(#H.parseHistory(nil, 30, decode), 0, "parseHistory nil -> {}")

  -- chars falls back to #text when the record omits it.
  local nochars = H.parseHistory({ "hello" }, 30, function(l) return { text = l } end)
  eq(nochars[1].chars, 5, "parseHistory chars falls back to #text")
  eq(nochars[1].ts, "", "parseHistory ts falls back to ''")
end

-- =====================================================================
-- buildStartDaemonCmd (daemon-launch): the daemon's stdout/stderr log must
-- live under the owner-only ~/.voicebridge, never world-readable /tmp.
-- Regression: startDaemon() used to redirect to /tmp/alfred_daemon.log, and
-- the daemon logs "transcript (…): <first 120 chars>" on every capture — a
-- verbatim-dictation leak to any other local account.
-- =====================================================================
do
  local cmd = H.buildStartDaemonCmd("ENV_PREFIX ", "/path/.venv/bin/python3",
    "/path/voicebridge.py", 8763, "/Users/x/.voicebridge/daemon.log")
  check(cmd:find("/Users/x/.voicebridge/daemon.log", 1, true) ~= nil,
    "buildStartDaemonCmd redirects into the given (owner-only) log file")
  check(cmd:find("/tmp/", 1, true) == nil,
    "buildStartDaemonCmd never references /tmp — that would be world-readable")
  check(cmd:find("nohup", 1, true) ~= nil, "buildStartDaemonCmd still detaches via nohup")
  check(cmd:find("8763", 1, true) ~= nil, "buildStartDaemonCmd carries the port")
end

-- =====================================================================
-- jsStr (small helpers): JSON-encode a single string for embedding in JS.
-- Pure string escaping -- no Hammerspoon dependency.
-- =====================================================================
do
  eq(H.jsStr("hi"), '"hi"', "jsStr wraps a plain string in quotes")
  eq(H.jsStr(nil), '""', "jsStr(nil) -> empty quoted string")
  check(H.jsStr('say "hi"'):find("\\\"", 1, true) ~= nil, "jsStr escapes embedded double quotes")
  check(H.jsStr("a\\b"):find("\\\\", 1, true) ~= nil, "jsStr escapes backslashes")
  check(H.jsStr("line1\nline2"):find("\\n", 1, true) ~= nil, "jsStr escapes newlines")
  check(H.jsStr("a\tb"):find("\\t", 1, true) ~= nil, "jsStr escapes tabs")
  check(H.jsStr("a\rb"):find("\\r", 1, true) ~= nil, "jsStr escapes carriage returns")
end

-- =====================================================================
-- tmpWav (small helpers): a fresh scratch .wav path under TMPDIR. Pure
-- (os.getenv/os.time only) -- no Hammerspoon dependency.
-- =====================================================================
do
  local a = H.tmpWav()
  check(type(a) == "string", "tmpWav returns a string")
  check(a:match("voicebridge_%d+%.wav$") ~= nil, "tmpWav ends with voicebridge_<epoch>.wav")
end

-- =====================================================================
-- setBackendValue (menu-bar action handlers): the pure assignment behind the
-- Backend submenu's radios. No Hammerspoon dependency.
-- =====================================================================
do
  H.VB.backend = "stale"
  H.setBackendValue("claude")
  eq(H.VB.backend, "claude", "setBackendValue sets VB.backend")
  H.setBackendValue(nil)
  eq(H.VB.backend, nil, "setBackendValue(nil) clears VB.backend (Default/config)")
end

-- =====================================================================
-- ensureVBDir (small helpers): create the owner-only ~/.voicebridge dir on
-- first use, then remember not to touch the filesystem again. `hs` is faked
-- (assigned as a plain global here, like the showHUD test above) so this
-- never touches a real directory.
-- =====================================================================
do
  local calls = {}
  hs = { fs = {
    attributes = function(path) calls[#calls + 1] = { "attributes", path }; return nil end,
    mkdir = function(path) calls[#calls + 1] = { "mkdir", path } end,
    chmod = function(path, mode) calls[#calls + 1] = { "chmod", path, mode } end,
  } }

  check(H.vbDirReady == false, "vbDirReady starts false")
  H.ensureVBDir()
  eq(calls[1][1], "attributes", "ensureVBDir checks hs.fs.attributes first")
  eq(calls[2][1], "mkdir", "ensureVBDir creates the dir when attributes is missing")
  eq(calls[3][1], "chmod", "ensureVBDir chmods the new dir 0700")
  eq(calls[3][3], 448, "ensureVBDir chmods with 448 (0700)")

  -- Second call is a no-op once vbDirReady is true (no further hs.fs.* calls).
  H.ensureVBDir()
  eq(#calls, 3, "ensureVBDir does not touch hs.fs.* again once already ready")

  hs = nil
end

-- =====================================================================
-- dbg (small helpers): off by default (DEBUG=false) so a call is a silent
-- no-op unless explicitly enabled; when enabled it writes one timestamped
-- line to LOG_FILE. `io.open` is temporarily patched (not `hs`) so this
-- test never touches the real ~/.voicebridge/voicebridge.log on the
-- developer's own machine -- restored immediately after each use.
-- =====================================================================
do
  hs = { fs = { attributes = function() return true end, mkdir = function() end, chmod = function() end } }
  local realOpen = io.open

  -- DEBUG off (the real default): dbg is a no-op, io.open is never touched.
  local opened = false
  io.open = function(...) opened = true; return realOpen(...) end
  H.dbg("should not be written")
  io.open = realOpen
  check(not opened, "dbg(msg) with DEBUG off never opens the log file")

  -- DEBUG on: writes one "<HH:MM:SS> <msg>\n" line via io.open/write/close.
  H.setDebugForTest(true)
  local written, closed, path, mode = nil, false, nil, nil
  io.open = function(p, m)
    path, mode = p, m
    return { write = function(_, s) written = s end, close = function(_) closed = true end }
  end
  H.dbg("hello debug")
  io.open = realOpen
  H.setDebugForTest(false)

  eq(mode, "a", "dbg opens the log file in append mode")
  check(path ~= nil and path:find("voicebridge.log", 1, true) ~= nil,
    "dbg opens LOG_FILE (…/voicebridge.log)")
  check(written ~= nil and written:find("hello debug", 1, true) ~= nil,
    "dbg writes the message text")
  check(written ~= nil and written:match("^%d%d:%d%d:%d%d ") ~= nil,
    "dbg prefixes an HH:MM:SS timestamp")
  check(written ~= nil and written:sub(-1) == "\n", "dbg terminates the line with a newline")
  check(closed, "dbg closes the file handle")

  hs = nil
end

-- =====================================================================
-- notify (small helpers): builds an hs.notify (with an optional onClick
-- handler) and sends it.
-- =====================================================================
do
  local calls = {}
  local function fakeNotify(...)
    calls[#calls + 1] = { ... }
    return { send = function() calls[#calls + 1] = { "SEND" } end }
  end
  hs = { notify = { new = fakeNotify } }

  H.notify("Alfred", "hello")
  eq(#calls[1], 1, "notify without onClick calls hs.notify.new(attrs) with 1 arg")
  eq(calls[1][1].title, "Alfred", "notify sets the title")
  eq(calls[1][1].informativeText, "hello", "notify sets informativeText")
  eq(calls[2][1], "SEND", "notify sends the notification")

  calls = {}
  local clicked = function() end
  H.notify("Alfred", nil, clicked)
  eq(calls[1][1], clicked, "notify with onClick calls hs.notify.new(onClick, attrs)")
  eq(calls[1][2].informativeText, "", "notify defaults informativeText to '' when text is nil")

  hs = nil
end

-- =====================================================================
-- setState (small helpers): sets VB.state, refreshes the menubar title only
-- if a real menubar exists yet, and pushes state to the app window (a no-op
-- here since VB.win stays nil throughout). No hs.* calls of its own.
-- =====================================================================
do
  local savedMenubar = H.VB.menubar
  H.VB.win = nil
  H.VB.menubar = nil
  H.setState("recording")
  eq(H.VB.state, "recording", "setState sets VB.state (no menubar yet)")

  local titles = {}
  H.VB.menubar = { setTitle = function(_, icon) titles[#titles + 1] = icon end }
  H.setState("idle")
  eq(H.VB.state, "idle", "setState sets VB.state (with a menubar)")
  eq(titles[1], H.iconForState("idle"), "setState refreshes the menubar title via iconForState")

  H.VB.menubar = savedMenubar
end

-- =====================================================================
-- pasteText (small helpers): clipboard + a delayed Cmd-V. `hs.timer.doAfter`
-- is faked to fire its callback synchronously, so the nested keyStroke
-- callback is covered too, without a real delay or a live Hammerspoon.
-- =====================================================================
do
  local calls = {}
  hs = {
    pasteboard = { setContents = function(t) calls[#calls + 1] = { "setContents", t } end },
    timer = { doAfter = function(delay, fn) calls[#calls + 1] = { "doAfter", delay }; fn() end },
    eventtap = { keyStroke = function(mods, key) calls[#calls + 1] = { "keyStroke", mods, key } end },
  }

  H.pasteText("hello", 0.2)
  eq(calls[1][1], "setContents", "pasteText sets the clipboard first")
  eq(calls[1][2], "hello", "pasteText puts the given text on the clipboard")
  eq(calls[2][1], "doAfter", "pasteText schedules the paste 2nd")
  eq(calls[2][2], 0.2, "pasteText honours the given delay")
  eq(calls[3][1], "keyStroke", "pasteText synthesizes Cmd-V after the delay")
  eq(calls[3][2][1], "cmd", "pasteText's keyStroke uses the cmd modifier")
  eq(calls[3][3], "v", "pasteText's keyStroke is 'v'")

  calls = {}
  H.pasteText(nil)
  eq(calls[1][2], "", "pasteText(nil) clears the clipboard instead of pasting 'nil'")
  eq(calls[2][2], 0.08, "pasteText defaults the delay to 0.08")

  hs = nil
end

-- =====================================================================
-- cancel (menu-bar action handlers): stop any in-flight recording/run and
-- snap back to idle. Exercises both the idle-with-a-live-recorder path and
-- the processing-with-a-live-engine-task path so every line runs; the
-- cross-file calls it makes (cancelWatchdog/destroyHUD/setState) are all
-- real ones -- with VB in its default nil sub-state they touch no hs.* of
-- their own, so only hs.alert.show itself needs a fake.
-- =====================================================================
do
  local alerts = {}
  hs = { alert = { show = function(msg, secs) alerts[#alerts + 1] = { msg, secs } end } }

  -- Defensive: start from the same nil sub-state cancel's cross-file calls
  -- assume, regardless of what earlier tests left behind.
  H.VB.menubar, H.VB.win, H.VB.hud, H.VB.hudTimer, H.VB.watchdog = nil, nil, nil, nil, nil

  -- idle + a still-running recorder: terminate it, then the trailing
  -- destroyHUD/setState/alert sequence.
  local terminated = false
  H.VB.state = "idle"
  H.VB.recTask = { isRunning = function() return true end, terminate = function() terminated = true end }
  H.cancel()
  check(terminated, "cancel terminates a still-running recTask")
  eq(H.VB.state, "idle", "cancel leaves state idle when it was already idle")
  eq(alerts[#alerts][1], "Cancelled", "cancel shows the Cancelled alert")
  H.VB.recTask = nil

  -- processing + a live engine task: bump runId, cancel the watchdog, and
  -- terminate the engine task too.
  local engineTerminated = false
  H.VB.state = "processing"
  H.VB.runId = 1
  H.VB.engineTask = { terminate = function() engineTerminated = true end }
  H.cancel()
  check(engineTerminated, "cancel terminates a live engineTask when state was processing")
  eq(H.VB.runId, 2, "cancel bumps VB.runId to invalidate any in-flight result")
  check(H.VB.engineTask == nil, "cancel clears VB.engineTask")
  eq(H.VB.state, "idle", "cancel always ends in idle")

  H.VB.runId = nil
  hs = nil
end

-- =====================================================================
-- openRecordings (menu-bar action handlers): shells out to reveal the
-- recordings folder, falling back to ~/Documents if it doesn't exist yet.
-- =====================================================================
do
  local calls = {}
  hs = { execute = function(cmd) calls[#calls + 1] = cmd end }
  H.openRecordings()
  eq(#calls, 1, "openRecordings shells exactly one command")
  check(calls[1]:find("VoiceBridge", 1, true) ~= nil, "openRecordings targets the VoiceBridge folder")
  check(calls[1]:find("~/Documents", 1, true) ~= nil, "openRecordings falls back to ~/Documents")
  hs = nil
end

-- =====================================================================
-- reloadHammerspoon (menu-bar action handlers): a thin call to hs.reload().
-- =====================================================================
do
  local calls = 0
  hs = { reload = function() calls = calls + 1 end }
  H.reloadHammerspoon()
  eq(calls, 1, "reloadHammerspoon calls hs.reload() exactly once")
  hs = nil
end

-- =====================================================================
-- buildLiveMenu (menu-bar action handlers): the live menu, wired to the
-- real named handlers (not fresh per-open closures) so buildMenu's own
-- `actions` plumbing is what fires them. Only the WIRING is checked here --
-- cancel/setBackendValue/openRecordings/reloadHammerspoon/etc. are
-- exercised directly, with their own hs fakes, above.
-- =====================================================================
do
  local savedState, savedBackend = H.VB.state, H.VB.backend
  H.VB.state = "idle"; H.VB.backend = nil
  local menu = H.buildLiveMenu()

  local function itemByTitle(m, t)
    for _, it in ipairs(m) do if it.title == t then return it end end
  end
  eq(itemByTitle(menu, "Open Alfred window").fn, H.openWindow, "buildLiveMenu wires openWindow")
  eq(itemByTitle(menu, "Cancel recording").fn, H.cancel, "buildLiveMenu wires cancel")
  eq(itemByTitle(menu, "Open recordings folder").fn, H.openRecordings,
    "buildLiveMenu wires openRecordings")
  eq(itemByTitle(menu, "Edit config…").fn, H.openConfig, "buildLiveMenu wires editConfig to openConfig")
  eq(itemByTitle(menu, "Reload intent modes").fn, H.refreshModes,
    "buildLiveMenu wires reloadModes to refreshModes")
  eq(itemByTitle(menu, "Restart engine (warm)").fn, H.restartDaemon,
    "buildLiveMenu wires restartDaemon")
  eq(itemByTitle(menu, "Reload Hammerspoon").fn, H.reloadHammerspoon,
    "buildLiveMenu wires reloadHammerspoon")

  -- The Backend submenu's radios call actions.setBackend, which is
  -- setBackendValue -- exercise it through the real menu plumbing too.
  local sub
  for _, it in ipairs(menu) do if it.title == "Backend" then sub = it.menu end end
  H.VB.backend = "stale-for-wiring-check"
  sub[2].fn()   -- the first real backend radio ("auto")
  eq(H.VB.backend, "auto", "clicking a backend radio in the live menu calls setBackendValue")

  H.VB.state, H.VB.backend = savedState, savedBackend
end

-- =====================================================================
-- voicebridgeTest / voicebridgeProcess / voicebridgeWindow (debug hooks):
-- callable via the `hs` CLI (hs -c "voicebridgeTest()"). voicebridgeTest and
-- voicebridgeWindow are pcall-wrapped so they degrade to an "ERROR: ..."
-- string instead of throwing when Hammerspoon isn't live -- exactly the
-- plain-lua situation here for the deep hs.canvas/hs.webview calls inside
-- showResult/openWindow, so no fake is needed for those two. voicebridgeProcess
-- is not pcall-wrapped in production, so its own hs.* touches (via runEngine)
-- ARE faked here, to reach its own final line without throwing.
-- =====================================================================
do
  local r1 = H.voicebridgeTest()
  check(type(r1) == "string", "voicebridgeTest returns a string")
  check(r1 == "panel shown" or r1:find("^ERROR: ") ~= nil,
    "voicebridgeTest reports success or a caught error, never throws")

  local r3 = H.voicebridgeWindow()
  check(r3 == "open" or r3 == "no-win" or r3:find("^ERROR: ") ~= nil,
    "voicebridgeWindow reports open/no-win or a caught error, never throws")

  hs = {
    http = { asyncPost = function() end },   -- swallow; the callback is engine.lua's own coverage
    json = { encode = function() return "{}" end },
    timer = { doAfter = function() end },    -- don't fire; startWatchdog's callback isn't in scope here
  }
  local ok2, r2 = pcall(H.voicebridgeProcess, "/tmp/nonexistent.wav")
  hs = nil
  check(ok2, "voicebridgeProcess does not throw for a well-formed call")
  eq(r2, "engine started on /tmp/nonexistent.wav", "voicebridgeProcess reports the wav path")
  H.VB.state = "idle"
end

-- =====================================================================
-- The tests below cover voicebridge_lua/capture.lua and voicebridge_lua/
-- window.lua's entry points, which all touch real Hammerspoon APIs. Same
-- convention as the showHUD test above: `hs` is a real (writable, per
-- .luacheckrc) global that resolves through the shared _ENV's fallback to
-- _G, so assigning a fake `hs` table here makes every hs.* call in every
-- loaded module resolve to it for the duration of the do-block; it's always
-- reset to nil (and any VB.* field touched is reset to its default) before
-- the block ends, so later tests see a clean slate.
-- =====================================================================

-- =====================================================================
-- onRecDone (capture): decides whether a just-finished recording has enough
-- audio to process, from the WAV's size on disk (io.open faked so the real
-- filesystem/~/.voicebridge is never touched).
-- =====================================================================
do
  local realOpen = io.open

  -- Not currently "processing": returns immediately, never opens the WAV,
  -- never touches hs.* at all (hs stays nil -- any access would throw).
  H.VB.state = "idle"
  local opened = false
  io.open = function(...) opened = true; return realOpen(...) end
  H.onRecDone()
  io.open = realOpen
  check(not opened, "onRecDone(idle) never opens the WAV file")

  -- Processing + a large-enough file -> runEngine (daemon POST), which
  -- itself touches hs.timer (watchdog)/hs.json/hs.http.
  H.VB.state = "processing"
  H.VB.wav = "/fake/whatever.wav"
  io.open = function() return { seek = function() return 5000 end, close = function() end } end
  hs = {
    timer = { doAfter = function() end },
    json = { encode = function() return "{}" end },
    http = { asyncPost = function() end },
  }
  H.onRecDone()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "processing", "onRecDone(processing, big file) leaves runEngine's own state alone")

  -- Processing + a too-small (or unreadable) file -> "nothing recorded":
  -- setState("idle") + notify (hs.notify.new).
  H.VB.state = "processing"
  io.open = function() return nil end   -- file missing/unreadable -> size stays 0
  local notified = false
  hs = { notify = { new = function(attrs) notified = (attrs.informativeText == "Nothing recorded.")
    return { send = function() end } end } }
  H.onRecDone()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "idle", "onRecDone(processing, no file) sets state back to idle")
  check(notified, "onRecDone(processing, no file) notifies 'Nothing recorded.'")
end

-- =====================================================================
-- startRecording (capture): the sox-missing bailout, and the task-launch
-- success/failure branches.
-- =====================================================================
do
  -- sox missing: notify + clear captureFlags, never spawns a task.
  H.VB.state = "idle"
  H.VB.captureFlags = { "--stale" }
  local taskSpawned, soxMissingNotified = false, false
  hs = {
    fs = { attributes = function() return nil end },
    task = { new = function() taskSpawned = true end },
    notify = { new = function(attrs) soxMissingNotified = (attrs.informativeText:find("sox not found", 1, true) ~= nil)
      return { send = function() end } end },
  }
  H.startRecording()
  hs = nil
  check(not taskSpawned, "startRecording(no sox) never spawns an hs.task")
  check(soxMissingNotified, "startRecording(no sox) notifies that sox is missing")
  eq(H.VB.captureFlags, nil, "startRecording(no sox) clears captureFlags")

  -- sox present, task starts: full success path (setState, skip showHUD
  -- since VB.win is a stub, arm the HUD poll timer).
  H.VB.win = { evaluateJavaScript = function() end }
  local fakeTask = { setEnvironment = function() end, start = function() return true end }
  local capturedArgv
  hs = {
    fs = { attributes = function() return true end },
    task = { new = function(_, _, _, argv) capturedArgv = argv; return fakeTask end },
    timer = { doEvery = function() return "TIMER" end },
  }
  H.startRecording()
  hs = nil
  eq(H.VB.state, "recording", "startRecording(success) sets state to recording")
  eq(H.VB.recTask, fakeTask, "startRecording(success) stores the spawned task")
  eq(H.VB.hudTimer, "TIMER", "startRecording(success) arms the HUD poll timer")
  check(capturedArgv ~= nil and capturedArgv[#capturedArgv - 2] == "trim",
    "startRecording builds the sox argv with the MAX_RECORD_SECS trim")

  -- sox present, task fails to start: notify + clear captureFlags.
  H.VB.captureFlags = { "--stale2" }
  local failTask = { setEnvironment = function() end, start = function() return false end }
  local failNotified = false
  hs = {
    fs = { attributes = function() return true end },
    task = { new = function() return failTask end },
    notify = { new = function(attrs) failNotified = (attrs.informativeText:find("Could not start") ~= nil)
      return { send = function() end } end },
  }
  H.startRecording()
  hs = nil
  check(failNotified, "startRecording(task fails to start) notifies the failure")
  eq(H.VB.captureFlags, nil, "startRecording(task fails to start) clears captureFlags")

  H.VB.win = nil
  H.VB.state = "idle"
  H.VB.recTask = nil
  H.VB.hudTimer = nil
end

-- =====================================================================
-- stopRecording (capture): kills a live sox task via SIGINT, or -- when
-- there's no task still running -- falls straight through to onRecDone.
-- =====================================================================
do
  -- A live task: hs.execute sends SIGINT to its pid; onRecDone is NOT called.
  H.VB.state = "recording"
  H.VB.recTask = { isRunning = function() return true end, pid = function() return 4242 end }
  local execCmd, onDoneCalled = nil, false
  local realOpen = io.open
  io.open = function() onDoneCalled = true; return nil end
  hs = {
    alert = { closeAll = function() end, show = function() end },
    execute = function(cmd) execCmd = cmd end,
  }
  H.stopRecording()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "processing", "stopRecording sets state to processing")
  check(execCmd ~= nil and execCmd:find("4242", 1, true) ~= nil,
    "stopRecording(live task) sends SIGINT to the task's pid")
  check(not onDoneCalled, "stopRecording(live task) does not fall through to onRecDone")

  -- No task running: falls through to onRecDone -> (state stays "processing",
  -- no WAV) -> "nothing recorded" -> notify.
  H.VB.recTask = nil
  local notified = false
  io.open = function() return nil end
  hs = {
    alert = { closeAll = function() end, show = function() end },
    notify = { new = function() notified = true; return { send = function() end } end },
  }
  H.stopRecording()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "idle", "stopRecording(no task) falls through onRecDone to idle")
  check(notified, "stopRecording(no task) falls through onRecDone's notify")

  H.VB.state = "idle"
end

-- =====================================================================
-- toggleDictate / toggleTranscribeOnly (capture): the 3-way state dispatch
-- shared by both hotkeys (idle -> start, recording -> stop, else -> busy
-- alert). captureFlags differ (nil vs --transcribe-only); everything else
-- is identical, so both are exercised the same way.
-- =====================================================================
for _, toggle in ipairs({
  { fn = "toggleDictate", wantFirstFlag = nil },
  { fn = "toggleTranscribeOnly", wantFirstFlag = "--transcribe-only" },
}) do
  -- idle -> pins the mode's captureFlags, then startRecording (made to
  -- actually succeed here -- its own bailout paths clear captureFlags again,
  -- which would mask the very thing being asserted).
  H.VB.state = "idle"
  H.VB.captureFlags = { "--previous" }
  H.VB.win = { evaluateJavaScript = function() end }   -- skip showHUD's own hs.canvas needs
  hs = {
    fs = { attributes = function() return true end },
    task = { new = function() return { setEnvironment = function() end, start = function() return true end } end },
    timer = { doEvery = function() end },
  }
  H[toggle.fn]()
  hs = nil
  if toggle.wantFirstFlag == nil then
    eq(H.VB.captureFlags, nil, toggle.fn .. "(idle) leaves captureFlags nil (uses config defaults)")
  else
    eq(H.VB.captureFlags[1], toggle.wantFirstFlag, toggle.fn .. "(idle) pins its own captureFlags")
  end
  H.VB.win = nil
  H.VB.recTask = nil
  H.VB.hudTimer = nil

  -- recording -> stopRecording (no live task -> falls through onRecDone).
  H.VB.state = "recording"
  H.VB.recTask = nil
  local realOpen = io.open
  io.open = function() return nil end
  hs = { alert = { closeAll = function() end, show = function() end },
         notify = { new = function() return { send = function() end } end } }
  H[toggle.fn]()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "idle", toggle.fn .. "(recording) stops and falls through to idle")

  -- processing -> "Still working…" alert, no state change.
  H.VB.state = "processing"
  local alertMsg = nil
  hs = { alert = { show = function(msg) alertMsg = msg end } }
  H[toggle.fn]()
  hs = nil
  eq(alertMsg, "Still working…", toggle.fn .. "(processing) shows the busy alert")
  eq(H.VB.state, "processing", toggle.fn .. "(processing) leaves state untouched")

  H.VB.state = "idle"
end

-- =====================================================================
-- pickMode (capture): shows the hs.chooser format list; the "cancelled"
-- (nil choice) and "picked" (calls onPick with the flags) paths of its
-- selection callback.
-- =====================================================================
do
  local capturedCb, shown = nil, false
  hs = { chooser = { new = function(cb) capturedCb = cb
    return { placeholderText = function() end, searchSubText = function() end,
      rows = function() end, width = function() end, choices = function() end,
      show = function() shown = true end } end } }
  local picked = "UNSET"
  H.pickMode(function(flags) picked = flags end)
  check(shown, "pickMode shows the chooser")
  check(capturedCb ~= nil, "pickMode registers a selection callback")

  capturedCb(nil)   -- cancelled
  eq(picked, "UNSET", "pickMode(cancelled) never calls onPick")

  capturedCb({ flags = { "--mode", "email" } })   -- picked
  eq(picked[1], "--mode", "pickMode(picked) calls onPick with the chosen flags[1]")
  eq(picked[2], "email", "pickMode(picked) calls onPick with the chosen flags[2]")

  -- A choice with no flags field defaults to {}.
  picked = "UNSET"
  hs = { chooser = { new = function(cb) capturedCb = cb
    return { placeholderText = function() end, searchSubText = function() end,
      rows = function() end, width = function() end, choices = function() end,
      show = function() end } end } }
  H.pickMode(function(flags) picked = flags end)
  capturedCb({})
  eq(#picked, 0, "pickMode(picked, no flags) defaults onPick's flags to {}")

  hs = nil
end

-- =====================================================================
-- dictateWithMode (capture): busy-alert when not idle; otherwise shows the
-- mode picker and, on selection, pins captureFlags and starts recording.
-- =====================================================================
do
  -- Busy: alert only, pickMode never invoked.
  H.VB.state = "processing"
  local alertMsg, chooserShown = nil, false
  hs = { alert = { show = function(msg) alertMsg = msg end },
         chooser = { new = function() chooserShown = true end } }
  H.dictateWithMode()
  hs = nil
  eq(alertMsg, "Busy…", "dictateWithMode(busy) shows the busy alert")
  check(not chooserShown, "dictateWithMode(busy) never opens the chooser")

  -- Idle: opens the chooser; picking a format pins captureFlags and starts
  -- recording. startRecording() must actually succeed here (sox present,
  -- task starts) -- its own "sox missing"/"task failed" bailouts clear
  -- captureFlags again, which would mask the very thing being asserted.
  H.VB.state = "idle"
  H.VB.captureFlags = nil
  H.VB.win = { evaluateJavaScript = function() end }   -- skip showHUD's own hs.canvas needs
  local capturedCb = nil
  hs = {
    chooser = { new = function(cb) capturedCb = cb
      return { placeholderText = function() end, searchSubText = function() end,
        rows = function() end, width = function() end, choices = function() end,
        show = function() end } end },
    fs = { attributes = function() return true end },
    task = { new = function() return { setEnvironment = function() end, start = function() return true end } end },
    timer = { doEvery = function() end },
  }
  H.dictateWithMode()
  check(capturedCb ~= nil, "dictateWithMode(idle) opens the chooser")
  capturedCb({ flags = { "--mode", "commit", "--rewrite" } })
  hs = nil
  eq(H.VB.captureFlags[1], "--mode", "dictateWithMode(idle, picked) pins the chosen flags")
  eq(H.VB.captureFlags[3], "--rewrite", "dictateWithMode(idle, picked) pins all the chosen flags")

  H.VB.state = "idle"
  H.VB.captureFlags = nil
  H.VB.win = nil
  H.VB.recTask = nil
  H.VB.hudTimer = nil
end

-- =====================================================================
-- typePrompt (capture): busy-alert when not idle; otherwise a free-text
-- chooser whose callback: does nothing when cancelled, alerts on empty
-- (whitespace-only) text, and otherwise pins captureFlags + runs the engine.
-- Each case below opens a FRESH picker (the callback nils out its own
-- `chooser` upvalue on first fire, so reusing one across cases wouldn't
-- reflect real usage).
-- =====================================================================
do
  -- Busy: alert only.
  H.VB.state = "processing"
  local alertMsg = nil
  hs = { alert = { show = function(msg) alertMsg = msg end } }
  H.typePrompt()
  hs = nil
  eq(alertMsg, "Busy…", "typePrompt(busy) shows the busy alert")

  -- runEngine (its own coverage lives in engine.lua's tests) consumes
  -- VB.captureFlags immediately -- it's cleared as soon as the daemon POST is
  -- built -- so "did typePrompt pin the chosen flags" has to be observed via
  -- that POST body (captured by the fake hs.json.encode below), not by
  -- reading VB.captureFlags afterwards.
  local encodedPayload
  local function freshChooser(queryText)
    local cb
    hs = {
      alert = { show = function() end },
      chooser = { new = function(fn) cb = fn
        return { query = function() return queryText end,
          placeholderText = function() end, queryChangedCallback = function() end,
          choices = function() end, rows = function() end, width = function() end,
          show = function() end } end },
      timer = { doAfter = function() end },
      json = { encode = function(t) encodedPayload = t; return "{}" end },
      http = { asyncPost = function() end },
    }
    H.VB.state = "idle"
    H.typePrompt()
    return cb
  end

  -- Cancelled (nil choice): no-op.
  H.VB.captureFlags = nil
  local cb1 = freshChooser("hello")
  cb1(nil)
  eq(H.VB.captureFlags, nil, "typePrompt(cancelled) never touches captureFlags")

  -- Whitespace-only query: "Type some text first" alert, no engine run.
  local emptyAlert = nil
  local cb2 = freshChooser("   ")
  hs.alert.show = function(msg) emptyAlert = msg end
  cb2({ flags = {} })
  eq(emptyAlert, "Type some text first", "typePrompt(empty text) alerts instead of running")
  eq(H.VB.captureFlags, nil, "typePrompt(empty text) never touches captureFlags")

  -- Real text: pins captureFlags (consumed into the engine argv) and runs it.
  local cb3 = freshChooser("  hello world  ")
  cb3({ flags = { "--mode", "email" } })
  check(encodedPayload ~= nil and encodedPayload.argv ~= nil, "typePrompt(real text) posts an engine argv")
  eq(encodedPayload.argv[1], "text", "typePrompt(real text) runs the 'text' subcommand")
  eq(encodedPayload.argv[2], "hello world", "typePrompt(real text) trims and forwards the typed text")
  check(encodedPayload.argv[3] == "--mode", "typePrompt(real text) forwards the chosen mode flags")

  hs = nil
  H.VB.state = "idle"
  H.VB.captureFlags = nil
end

-- =====================================================================
-- closeResult (capture): tears down the result timer/canvas (each only if
-- present) and clears the injected handlers.
-- =====================================================================
do
  local stopped, deleted = false, false
  H.VB.resultTimer = { stop = function() stopped = true end }
  H.VB.result = { delete = function() deleted = true end }
  H.VB.resultHandlers = { onCopy = function() end }
  H.closeResult()
  check(stopped, "closeResult stops a live resultTimer")
  check(deleted, "closeResult deletes a live result canvas")
  eq(H.VB.resultTimer, nil, "closeResult clears resultTimer")
  eq(H.VB.result, nil, "closeResult clears result")
  eq(H.VB.resultHandlers, nil, "closeResult clears resultHandlers")

  -- Nothing to tear down: a no-op, no crash.
  H.closeResult()
  eq(H.VB.resultTimer, nil, "closeResult(already clear) stays a no-op")
end

-- =====================================================================
-- resultClick (capture): the result panel's hs.canvas mouse callback --
-- only a "mouseUp" dispatches, via resultDispatch, to VB.resultHandlers.
-- =====================================================================
do
  local got = nil
  H.VB.resultHandlers = { onCopy = function(t) got = t end }
  H.VB.resultText = "hello result"
  H.resultClick(nil, "mouseUp", "copy")
  eq(got, "hello result", "resultClick(mouseUp) dispatches to the matching handler")

  got = nil
  H.resultClick(nil, "mouseDown", "copy")
  eq(got, nil, "resultClick(non-mouseUp) never dispatches")

  H.VB.resultHandlers = nil
  H.VB.resultText = ""
end

-- =====================================================================
-- historyStartIndex / historyRecordValid / historyItemFrom (window): the
-- pure pieces parseHistory's loop was split into, to keep its own
-- complexity under the gate's limit.
-- =====================================================================
do
  eq(H.historyStartIndex(10, 30), 1, "historyStartIndex clamps to 1 when the window covers everything")
  eq(H.historyStartIndex(100, 30), 71, "historyStartIndex walks back exactly `limit` lines")
  eq(H.historyStartIndex(100, nil), 71, "historyStartIndex defaults limit to 30")

  eq(H.historyRecordValid(false, { text = "x" }), false, "historyRecordValid: pcall failed -> false")
  eq(H.historyRecordValid(true, "not a table"), false, "historyRecordValid: non-table record -> false")
  eq(H.historyRecordValid(true, { chars = 1 }), false, "historyRecordValid: table without text -> false")
  eq(H.historyRecordValid(true, { text = "hi" }), true, "historyRecordValid: table with text -> true")

  local item = H.historyItemFrom({ text = "hello" })
  eq(item.text, "hello", "historyItemFrom carries text")
  eq(item.ts, "", "historyItemFrom defaults ts to ''")
  eq(item.chars, 5, "historyItemFrom falls back chars to #text")
  local item2 = H.historyItemFrom({ text = "hi", ts = "T", chars = 99 })
  eq(item2.ts, "T", "historyItemFrom carries an explicit ts")
  eq(item2.chars, 99, "historyItemFrom carries an explicit chars")
end

-- =====================================================================
-- windowCaptureFlags (window): the app window's own mode-flags + translate
-- state, routed through the shared buildCaptureFlags.
-- =====================================================================
do
  H.VB.winModeFlags = { "--mode", "notes", "--rewrite" }
  H.VB.winTranslate = true
  local flags = H.windowCaptureFlags()
  eq(flags[#flags], "--translate", "windowCaptureFlags reflects VB.winTranslate")
  eq(flags[1], "--mode", "windowCaptureFlags reflects VB.winModeFlags")

  H.VB.winTranslate = false
  eq(H.windowCaptureFlags()[#H.windowCaptureFlags()], "--no-translate",
    "windowCaptureFlags flips to --no-translate")

  H.VB.winModeFlags = {}
  H.VB.winTranslate = true
end

-- =====================================================================
-- windowToggleRecord (window): the app window's record button -- idle
-- starts (pinning windowCaptureFlags first), recording stops.
-- =====================================================================
do
  H.VB.state = "idle"
  H.VB.winModeFlags = { "--mode", "email" }
  H.VB.winTranslate = true
  H.VB.captureFlags = nil
  -- startRecording must actually succeed here -- its own bailout paths clear
  -- captureFlags again, which would mask the very thing being asserted.
  H.VB.win = { evaluateJavaScript = function() end }
  hs = {
    fs = { attributes = function() return true end },
    task = { new = function() return { setEnvironment = function() end, start = function() return true end } end },
    timer = { doEvery = function() end },
  }
  H.windowToggleRecord()
  hs = nil
  eq(H.VB.captureFlags[1], "--mode", "windowToggleRecord(idle) pins windowCaptureFlags before starting")
  H.VB.win = nil
  H.VB.recTask = nil
  H.VB.hudTimer = nil

  H.VB.state = "recording"
  H.VB.recTask = nil
  local realOpen = io.open
  io.open = function() return nil end
  hs = { alert = { closeAll = function() end, show = function() end },
         notify = { new = function() return { send = function() end } end } }
  H.windowToggleRecord()
  io.open = realOpen
  hs = nil
  eq(H.VB.state, "idle", "windowToggleRecord(recording) stops (falls through to idle)")

  H.VB.winModeFlags = {}
  H.VB.captureFlags = nil
end

-- =====================================================================
-- pushWindowState (window): pushes the current state/timer/level into the
-- open app window's JS -- a no-op when the window isn't open.
-- =====================================================================
do
  H.VB.win = nil
  H.pushWindowState()   -- no window: no-op, must not throw

  local pushed = nil
  H.VB.win = { evaluateJavaScript = function(_, js) pushed = js end }
  H.VB.state = "recording"
  H.VB.recStart = os.time() - 5
  H.VB.level = 0.7
  H.pushWindowState()
  check(pushed ~= nil and pushed:find('"recording"', 1, true) ~= nil,
    "pushWindowState(recording) pushes the recording state")
  check(pushed:find("00:0", 1, true) ~= nil, "pushWindowState(recording) pushes an elapsed timer")

  H.VB.state = "idle"
  H.VB.level = nil
  H.pushWindowState()
  check(pushed ~= nil, "pushWindowState(idle) still pushes (level defaults to 0)")

  H.VB.win = nil
  H.VB.level = 0
end

-- =====================================================================
-- updateResult (window): refreshes the open app window in place, or (no
-- window open) shows the lightweight toast panel via showResult.
-- =====================================================================
do
  -- Window open: two evaluateJavaScript pushes (result, then history), then
  -- show():bringToFront(). historyPath is redirected to a file that can't
  -- exist, so readHistory takes its "file missing -> {}" branch and never
  -- needs a real hs.json.decode.
  H.VB.contract = { resolved = { history = "/tmp/definitely-not-a-real-alfred-history.jsonl" } }
  local jsCalls, frontCalled = {}, false
  H.VB.win = {
    evaluateJavaScript = function(_, js) jsCalls[#jsCalls + 1] = js end,
    show = function(self) return self end,
    bringToFront = function() frontCalled = true end,
  }
  hs = { json = { encode = function() return "[]" end, decode = function() error("should not be called") end } }
  H.updateResult("hello result", true)
  hs = nil
  eq(H.VB.resultText, "hello result", "updateResult(window open) sets VB.resultText")
  eq(#jsCalls, 2, "updateResult(window open) pushes exactly 2 evaluateJavaScript calls")
  check(jsCalls[1]:find("vbResult", 1, true) ~= nil, "updateResult(window open) 1st push is vbResult")
  check(jsCalls[2]:find("vbHistory", 1, true) ~= nil, "updateResult(window open) 2nd push is vbHistory")
  check(frontCalled, "updateResult(window open) brings the window to front")

  -- No window: falls through to the toast panel (showResult), which is
  -- itself a big hs.canvas builder -- faked the same way as the standalone
  -- showResult/showHUD tests elsewhere in this file.
  H.VB.win = nil
  local canvasShown = false
  hs = {
    screen = { mainScreen = function() return { frame = function() return { x = 0, y = 0, w = 1000, h = 800 } end } end },
    canvas = {
      new = function() return {
        level = function() end, behavior = function() end, clickActivating = function() end,
        mouseCallback = function() end, appendElements = function() end,
        show = function() canvasShown = true end,
      } end,
      windowLevels = { overlay = 1 },
      windowBehaviors = { canJoinAllSpaces = 1 },
    },
    timer = { doAfter = function() end },
  }
  H.updateResult("toast text", false)
  hs = nil
  check(canvasShown, "updateResult(no window) falls through to the toast panel")

  H.VB.win = nil
  H.VB.contract = nil
  H.VB.result = nil
  H.VB.resultTimer = nil
  H.VB.resultHandlers = nil
end

-- =====================================================================
-- openWindow (window): already-open short-circuit, and the full webview
-- construction/config sequence -- including firing the window-closing
-- callback it registers.
-- =====================================================================
do
  -- Already open: just refocuses, never touches hs.webview.new.
  local shown, fronted, webviewBuilt = false, false, false
  H.VB.win = {
    show = function(self) shown = true; return self end,
    bringToFront = function() fronted = true end,
  }
  hs = { webview = { new = function() webviewBuilt = true end } }
  H.openWindow()
  hs = nil
  check(shown and fronted, "openWindow(already open) shows and refocuses the existing window")
  check(not webviewBuilt, "openWindow(already open) never builds a new webview")

  -- Not open: builds the whole webview, wires the JS bridge, stores VB.win.
  H.VB.win = nil
  H.VB.contract = { resolved = { history = "/tmp/definitely-not-a-real-alfred-history.jsonl" } }
  local capturedClosing, fakeWin
  fakeWin = {
    windowStyle = function() end, allowTextEntry = function() end, level = function() end,
    closeOnEscape = function() end, deleteOnClose = function() end,
    windowCallback = function(_, fn) capturedClosing = fn end,
    html = function() end,
    show = function(self) return self end, bringToFront = function() end,
  }
  hs = {
    webview = {
      usercontent = { new = function() return { setCallback = function() end } end },
      new = function() return fakeWin end,
      windowMasks = { titled = 1, closable = 2, resizable = 4, utility = 8 },
    },
    screen = { mainScreen = function() return { frame = function() return { x = 0, y = 0, w = 1000, h = 800 } end } end },
    canvas = { windowLevels = { floating = 2 } },
    json = { encode = function() return "{}" end, decode = function() error("should not be called") end },
  }
  H.openWindow()
  hs = nil
  eq(H.VB.win, fakeWin, "openWindow(not open) stores the new webview on VB.win")
  check(capturedClosing ~= nil, "openWindow(not open) registers a windowCallback")

  -- Firing that callback with "closing" clears VB.win; anything else doesn't.
  capturedClosing("closing")
  eq(H.VB.win, nil, "openWindow's windowCallback('closing') clears VB.win")
  H.VB.win = fakeWin
  capturedClosing("other")
  eq(H.VB.win, fakeWin, "openWindow's windowCallback(other) leaves VB.win alone")

  H.VB.win = nil
  H.VB.contract = nil
end

-- =====================================================================
-- saveIntent (window): persists an intent's prompt via the engine CLI, then
-- (on the task's exit) reloads the mode catalog.
-- =====================================================================
do
  local capturedArgv, capturedCb, started = nil, nil, false
  hs = {
    task = { new = function(_, cb, argv) capturedArgv = argv; capturedCb = cb
      return { setEnvironment = function() end, start = function() started = true end } end },
  }
  H.saveIntent("email", "write a polished email")
  check(started, "saveIntent starts the set-intent task")
  eq(capturedArgv[2], "set-intent", "saveIntent's argv[2] is the set-intent subcommand")
  eq(capturedArgv[3], "email", "saveIntent's argv[3] is the intent key")
  eq(capturedArgv[5], "write a polished email", "saveIntent's argv[5] is the prompt")

  -- Firing the task's exit callback reloads modes (refreshModes -> its own
  -- hs.task.new, tolerated by the same generic fake above).
  capturedCb(0)
  hs = nil

  -- A key with no prompt still runs, with an empty prompt string.
  hs = { task = { new = function(_, _, argv) capturedArgv = argv
    return { setEnvironment = function() end, start = function() end } end } }
  H.saveIntent("commit", nil)
  hs = nil
  eq(capturedArgv[5], "", "saveIntent(no prompt) defaults the prompt to ''")
end

-- =====================================================================
-- taskOutputReady (window): does a settings task's (exit code, stdout) look
-- like a decodable payload worth accepting?
-- =====================================================================
do
  eq(H.taskOutputReady(0, "{}"), true, "taskOutputReady(0, non-empty) -> true")
  eq(H.taskOutputReady(1, "{}"), false, "taskOutputReady(nonzero code) -> false")
  eq(H.taskOutputReady(0, ""), false, "taskOutputReady(empty output) -> false")
  check(not H.taskOutputReady(0, nil), "taskOutputReady(nil output) -> falsy")
end

-- =====================================================================
-- refreshSettings (window): fetches backend/model settings from the engine;
-- its task-exit callback parses + applies them, and (if the app window is
-- open) pushes them into it.
-- =====================================================================
do
  local capturedArgv, capturedCb = nil, nil
  hs = { task = { new = function(_, cb, argv) capturedArgv = argv; capturedCb = cb
    return { setEnvironment = function() end, start = function() end } end } }
  H.refreshSettings()
  eq(capturedArgv[2], "settings", "refreshSettings' argv[2] is the settings subcommand")

  -- Non-zero exit: taskOutputReady is false, nothing happens.
  H.VB.settings = nil
  hs.json = { decode = function() error("should not decode on a failed run") end }
  capturedCb(1, "")
  eq(H.VB.settings, nil, "refreshSettings(failed run) leaves VB.settings untouched")

  -- Success, undecodable body: pcall catches it, VB.settings untouched.
  hs.json = { decode = function() error("boom") end }
  capturedCb(0, "not json")
  eq(H.VB.settings, nil, "refreshSettings(undecodable body) leaves VB.settings untouched")

  -- Success, decodes to a non-table: also left untouched.
  hs.json = { decode = function() return "a string, not a table" end }
  capturedCb(0, "\"a string, not a table\"")
  eq(H.VB.settings, nil, "refreshSettings(non-table payload) leaves VB.settings untouched")

  -- Success, decodes to a table, no open window: settings applied, no push.
  hs.json = { decode = function() return { claude_model = "opus" } end }
  H.VB.win = nil
  capturedCb(0, "{...}")
  eq(H.VB.settings.claude_model, "opus", "refreshSettings(good payload) applies VB.settings")

  -- Success, decodes to a table, window open: also pushed via evaluateJavaScript.
  local pushed = nil
  H.VB.win = { evaluateJavaScript = function(_, js) pushed = js end }
  hs.json = { decode = function() return { claude_model = "sonnet" } end, encode = function() return "{}" end }
  capturedCb(0, "{...}")
  check(pushed ~= nil and pushed:find("vbSettings", 1, true) ~= nil,
    "refreshSettings(good payload, window open) pushes vbSettings to the window")

  hs = nil
  H.VB.win = nil
  H.VB.settings = nil
end

-- =====================================================================
-- setModel (window): persists a backend's model via the engine CLI, then
-- (on the task's exit) refreshes settings.
-- =====================================================================
do
  local capturedArgv, capturedCb = nil, nil
  hs = { task = { new = function(_, cb, argv) capturedArgv = argv; capturedCb = cb
    return { setEnvironment = function() end, start = function() end } end } }
  H.setModel("claude", "opus")
  eq(capturedArgv[2], "set-model", "setModel's argv[2] is the set-model subcommand")
  eq(capturedArgv[3], "claude", "setModel's argv[3] is the backend")
  eq(capturedArgv[5], "opus", "setModel's argv[5] is the model")

  -- Firing the task's exit callback refreshes settings (its own hs.task.new,
  -- tolerated by the same generic fake above).
  capturedCb()
  hs = nil

  -- No model: defaults to an empty string.
  hs = { task = { new = function(_, _, argv) capturedArgv = argv
    return { setEnvironment = function() end, start = function() end } end } }
  H.setModel("codex", nil)
  hs = nil
  eq(capturedArgv[5], "", "setModel(no model) defaults the model to ''")
end

-- =====================================================================
-- The tests below cover voicebridge_lua/engine.lua's daemon-communication
-- and contract logic: the processing watchdog, onResult's per-kind
-- dispatch (and its extracted handleCopied/handleSaved/handleUnknown/
-- dumpCapture helpers), runEngine's POST-response dispatch, the daemon
-- launch/health-check/restart trio, and fetchContract/applyContract/
-- resolveConfigPath. Same `hs`-faking convention as capture.lua/window.lua's
-- tests above.
-- =====================================================================

-- =====================================================================
-- startWatchdog (engine-client): arms a 150s timeout; firing it is a no-op
-- unless the SAME run is still "processing" (a newer run, or one that
-- already finished, must not be touched) -- in which case it terminates any
-- live engineTask (via a pcall'd terminate, tolerating a task that's already
-- gone) and snaps the UI back to idle with a timeout notice.
-- =====================================================================
do
  local scheduledDelay, scheduledFn
  hs = { timer = { doAfter = function(delay, fn) scheduledDelay = delay; scheduledFn = fn end } }
  H.VB.watchdog = nil
  H.VB.state = "idle"
  H.startWatchdog(5)
  hs = nil
  eq(scheduledDelay, 150, "startWatchdog arms a 150s timer")
  check(scheduledFn ~= nil, "startWatchdog registers the timeout callback")

  -- Fired while no longer "processing": clears VB.watchdog, nothing else.
  H.VB.state = "idle"
  scheduledFn()
  eq(H.VB.watchdog, nil, "watchdog fire clears VB.watchdog even when stale")
  eq(H.VB.state, "idle", "watchdog fire(not processing) leaves state untouched")

  -- Fired while processing, but for a NEWER run (VB.runId moved on): no-op.
  H.VB.state = "processing"
  H.VB.runId = 6
  scheduledFn()   -- was armed for runId=5
  eq(H.VB.state, "processing", "watchdog fire(stale run) leaves state untouched")

  -- Fired for its OWN run, still processing, no live engineTask: times out.
  local notified = false
  H.VB.runId = 5
  H.VB.state = "processing"
  H.VB.engineTask = nil
  hs = { notify = { new = function(attrs)
    notified = (attrs.informativeText:find("Timed out", 1, true) ~= nil)
    return { send = function() end } end } }
  scheduledFn()
  hs = nil
  eq(H.VB.state, "idle", "watchdog fire(own run, no task) sets state idle")
  check(notified, "watchdog fire(own run) notifies the timeout")

  -- Fired for its own run, with a live engineTask: terminate()s it via pcall.
  local terminated = false
  H.VB.runId = 5
  H.VB.state = "processing"
  H.VB.engineTask = { terminate = function() terminated = true end }
  hs = { notify = { new = function() return { send = function() end } end } }
  scheduledFn()
  hs = nil
  check(terminated, "watchdog fire(own run, live task) terminates the engineTask")
  eq(H.VB.engineTask, nil, "watchdog fire clears VB.engineTask")

  H.VB.watchdog = nil
  H.VB.runId = nil
  H.VB.state = "idle"
end

-- =====================================================================
-- onResult (engine-result): dispatches a finished capture to the UI, per
-- classifyResult's `kind` -- copied/saved/empty/error/(no status line) --
-- and the DEBUG-gated capture dump. Also exercises the "saved" toast's
-- click-to-reveal closure.
-- =====================================================================
do
  local function resetVB()
    H.VB.watchdog, H.VB.engineTask, H.VB.state = nil, nil, "idle"
    H.VB.menubar, H.VB.win = nil, nil
    H.VB.result, H.VB.resultTimer, H.VB.resultHandlers = nil, nil, nil
  end
  local decode = function(s) return (s:gsub('^"', ''):gsub('"$', '')) end
  local function toastFake(shownFlag)
    return {
      json = { decode = decode },
      screen = { mainScreen = function() return { frame = function() return { x=0,y=0,w=1000,h=800 } end } end },
      canvas = { new = function() return {
          level = function() end, behavior = function() end, clickActivating = function() end,
          mouseCallback = function() end, appendElements = function() end,
          show = function() shownFlag.v = true end,
        } end, windowLevels = { overlay = 1 }, windowBehaviors = { canJoinAllSpaces = 1 } },
      timer = { doAfter = function() end },
    }
  end

  -- "copied", VB_RESULT present: delivers the exact text (no window open ->
  -- falls through to the toast panel).
  resetVB()
  local shown = { v = false }
  hs = toastFake(shown)
  H.onResult(0, 'VB_RESULT\t"hello"\nVB_STATUS\tcopied', "")
  hs = nil
  check(shown.v, "onResult(copied) delivers the text (toast panel shown)")
  eq(H.VB.resultText, "hello", "onResult(copied) sets the delivered text")
  eq(H.VB.state, "idle", "onResult always ends in idle")
  resetVB()

  -- "copied" + paste_failed: an extra notify for the paste failure.
  resetVB()
  local pf = { v = false }
  hs = toastFake(pf)
  local notifies = {}
  hs.notify = { new = function(attrs) notifies[#notifies + 1] = attrs.informativeText
    return { send = function() end } end }
  H.onResult(0, 'VB_RESULT\t"hi"\nVB_STATUS\tcopied\tpaste_failed', "")
  hs = nil
  check(#notifies >= 1 and notifies[#notifies]:find("Auto%-paste failed") ~= nil,
    "onResult(copied, paste_failed) notifies the paste failure")
  resetVB()

  -- "copied", updateResult itself throws (no hs.screen for showResult to use):
  -- falls back to the plain banner notify instead of crashing.
  resetVB()
  local bannerNotified = false
  hs = {
    json = { decode = decode },
    notify = { new = function(attrs) bannerNotified = (attrs.informativeText == "Copied to clipboard ✓")
      return { send = function() end } end },
  }
  H.onResult(0, 'VB_RESULT\t"boom"\nVB_STATUS\tcopied', "")
  hs = nil
  check(bannerNotified, "onResult(copied, updateResult throws) falls back to the plain banner notify")

  -- "copied", no VB_RESULT line: falls back to the clipboard.
  resetVB()
  local pasteShown = { v = false }
  hs = toastFake(pasteShown)
  hs.json = { decode = function() error("should not be called") end }
  hs.pasteboard = { getContents = function() return "clipboard text" end }
  H.onResult(0, "VB_STATUS\tcopied", "")
  hs = nil
  eq(H.VB.resultText, "clipboard text", "onResult(copied, no VB_RESULT) falls back to the clipboard")
  resetVB()

  -- "saved": notify carries a click-to-reveal action that shells `open -R`.
  resetVB()
  local capturedOnClick, revealCmd
  hs = {
    json = { decode = function() end },
    execute = function(cmd) revealCmd = cmd end,
    notify = { new = function(onClick) capturedOnClick = onClick; return { send = function() end } end },
  }
  H.onResult(0, "VB_STATUS\tsaved\t/tmp/out.md", "")
  capturedOnClick()
  hs = nil
  check(revealCmd ~= nil and revealCmd:find("/tmp/out.md", 1, true) ~= nil,
    "onResult(saved)'s click action reveals the saved file")

  -- "empty": a plain notify.
  resetVB()
  local emptyMsg
  hs = { json = { decode = function() end },
    notify = { new = function(attrs) emptyMsg = attrs.informativeText; return { send = function() end } end } }
  H.onResult(0, "VB_STATUS\tempty", "")
  hs = nil
  eq(emptyMsg, "No speech detected.", "onResult(empty) notifies no speech detected")

  -- "error": errorMessage(subtype, tail).
  resetVB()
  local errMsg
  hs = { json = { decode = function() end },
    notify = { new = function(attrs) errMsg = attrs.informativeText; return { send = function() end } end } }
  H.onResult(1, "VB_STATUS\terror\tstt_failed", "boom")
  hs = nil
  eq(errMsg, "Transcription failed. (boom)",
    "onResult(error) notifies the mapped error message, with the stderr tail")

  -- No VB_STATUS at all: surfaces the stderr tail.
  resetVB()
  local unknownMsg
  hs = { json = { decode = function() end },
    notify = { new = function(attrs) unknownMsg = attrs.informativeText; return { send = function() end } end } }
  H.onResult(1, "just a traceback", "Traceback...\nValueError: x")
  hs = nil
  eq(unknownMsg, "Error: ValueError: x", "onResult(no status) surfaces the stderr tail")

  -- No VB_STATUS AND no stderr at all: falls back to the generic pointer at
  -- the daemon log (r.tail is nil, so the literal fallback text is used).
  resetVB()
  local noTailMsg
  hs = { json = { decode = function() end },
    notify = { new = function(attrs) noTailMsg = attrs.informativeText; return { send = function() end } end } }
  H.onResult(1, "nothing recognizable", "")
  hs = nil
  eq(noTailMsg, "Error: see the engine log (~/.voicebridge/daemon.log)",
    "onResult(no status, no stderr) falls back to the generic daemon-log pointer")

  -- DEBUG dump branch: writes the capture (code + STDOUT/STDERR markers) to
  -- DUMP_FILE, gated behind DEBUG (io.open temporarily patched, like the
  -- dbg() test above, so the real ~/.voicebridge is never touched).
  resetVB()
  H.setDebugForTest(true)
  hs = {
    json = { decode = function() end },
    fs = { attributes = function() return true end },
    notify = { new = function() return { send = function() end } end },
  }
  local realOpen = io.open
  local writes = {}
  io.open = function(_, mode)
    local rec = { mode = mode, written = "" }
    writes[#writes + 1] = rec
    return {
      write = function(_, s) rec.written = rec.written .. s end,
      close = function(_) rec.closed = true end,
    }
  end
  H.onResult(0, "VB_STATUS\tempty", "boom err")
  io.open = realOpen
  hs = nil
  H.setDebugForTest(false)
  local dumpWrite
  for _, rec in ipairs(writes) do
    if rec.written:find("STDOUT", 1, true) then dumpWrite = rec end
  end
  check(dumpWrite ~= nil, "onResult(DEBUG on) writes the capture dump")
  eq(dumpWrite.mode, "w", "onResult's capture dump opens in write (overwrite) mode")
  check(dumpWrite.written:find("code=0", 1, true) ~= nil, "onResult's capture dump includes the exit code")
  check(dumpWrite.written:find("STDERR", 1, true) ~= nil, "onResult's capture dump includes a STDERR section")
  check(dumpWrite.closed, "onResult's capture dump closes the file handle")

  resetVB()
end

-- =====================================================================
-- runEngine (engine-client): the daemon POST response dispatch -- a stale
-- result (a newer run superseded this one) is dropped; otherwise dispatched
-- by classifyPost's outcome (ok/down/busy). Captured from a faked
-- hs.http.asyncPost so the callback itself can be driven directly.
-- =====================================================================
do
  local function freshRun(argv)
    local capturedCb
    hs = {
      json = { encode = function() return "{}" end },
      http = { asyncPost = function(_, _, _, cb) capturedCb = cb end },
      timer = { doAfter = function() end },   -- watchdog: don't fire
    }
    H.runEngine(argv or { "process", "/x.wav" })
    hs = nil
    return capturedCb, H.VB.runId
  end

  -- Stale result: a newer run has already started -> dropped, no hs.* touch.
  local cb1, myRun1 = freshRun()
  H.VB.runId = myRun1 + 1
  cb1(200, '{"code":0}')
  check(true, "runEngine's callback drops a stale result without touching hs.*")

  -- "ok" + a decodable body: dispatches through onResult.
  local cb2, myRun2 = freshRun()
  H.VB.runId = myRun2
  local notified
  hs = {
    json = { decode = function() return { code = 0, out = "VB_STATUS\tempty", err = "" } end },
    notify = { new = function(attrs) notified = attrs.informativeText; return { send = function() end } end },
  }
  cb2(200, '{"code":0,"out":"VB_STATUS\\tempty","err":""}')
  hs = nil
  eq(notified, "No speech detected.", "runEngine's callback(ok, decodable) dispatches through onResult")

  -- "ok" but an undecodable body: bail to idle without re-running.
  local cb3, myRun3 = freshRun()
  H.VB.runId = myRun3
  local undecodedMsg
  hs = {
    json = { decode = function() error("bad json") end },
    notify = { new = function(attrs) undecodedMsg = attrs.informativeText; return { send = function() end } end },
  }
  cb3(200, "not json")
  hs = nil
  eq(undecodedMsg, "The engine returned an unreadable response.",
    "runEngine's callback(ok, undecodable) notifies instead of re-running")
  eq(H.VB.state, "idle", "runEngine's callback(ok, undecodable) leaves state idle")

  -- "down": no daemon listening -> falls back to a one-shot + ensureDaemon.
  local cb4 = freshRun()
  local oneShotStarted, asyncGetCalled = false, false
  hs = {
    task = { new = function() oneShotStarted = true
      return { setEnvironment = function() end, start = function() return true end } end },
    http = { asyncGet = function() asyncGetCalled = true end },
  }
  cb4(-1004, "")
  hs = nil
  check(oneShotStarted, "runEngine's callback(down) falls back to a one-shot run")
  check(asyncGetCalled, "runEngine's callback(down) calls ensureDaemon to bring it back up")
  H.VB.engineTask = nil

  -- "down", and the one-shot task itself fails to start (e.g. a bad PYTHON
  -- path): runEngineOneShot bails to idle with a notify instead of leaving
  -- the UI stuck on the processing spinner.
  local cb4b = freshRun()
  local oneShotFailedMsg
  hs = {
    task = { new = function() return { setEnvironment = function() end, start = function() return false end } end },
    http = { asyncGet = function() end },
    notify = { new = function(attrs) oneShotFailedMsg = attrs.informativeText; return { send = function() end } end },
  }
  cb4b(-1004, "")
  hs = nil
  check(oneShotFailedMsg ~= nil and oneShotFailedMsg:find("Could not launch", 1, true) ~= nil,
    "runEngine's callback(down, one-shot fails to start) notifies instead of hanging")
  eq(H.VB.state, "idle", "runEngine's callback(down, one-shot fails to start) leaves state idle")
  H.VB.engineTask = nil

  -- "busy": still working (POST timeout / any other status) -> keep processing.
  local cb5, myRun5 = freshRun()
  H.VB.runId = myRun5
  local busyMsg
  hs = { notify = { new = function(attrs) busyMsg = attrs.informativeText; return { send = function() end } end } }
  cb5(-1001, "")
  hs = nil
  eq(busyMsg, "Still transcribing… the engine is taking a while.",
    "runEngine's callback(busy) notifies without re-running")

  H.VB.state = "idle"
  H.VB.runId = nil
  H.VB.captureFlags = nil
end

-- =====================================================================
-- resultPanelHandlers (engine-result): onCopy/onPaste/onEmail -- the button
-- actions injected into the result panel. onDiscard is covered elsewhere
-- (it's a one-line closure, exercised by simply building the handler set).
-- =====================================================================
do
  local h = H.resultPanelHandlers()

  -- onCopy: clipboard, close the panel, a quick "Copied ✓" alert.
  local calls = {}
  H.VB.result = { delete = function() calls[#calls + 1] = "deleted" end }
  H.VB.resultTimer = { stop = function() calls[#calls + 1] = "stopped" end }
  hs = {
    pasteboard = { setContents = function(t) calls[#calls + 1] = { "setContents", t } end },
    alert = { show = function(msg, secs) calls[#calls + 1] = { "alert", msg, secs } end },
  }
  h.onCopy("copied text")
  hs = nil
  eq(calls[1][1], "setContents", "onCopy sets the clipboard first")
  eq(calls[1][2], "copied text", "onCopy copies the given text")
  eq(calls[#calls][1], "alert", "onCopy shows an alert last")
  eq(calls[#calls][2], "Copied ✓", "onCopy shows the Copied alert")
  H.VB.result, H.VB.resultTimer, H.VB.resultHandlers = nil, nil, nil

  -- onPaste: closes the panel, then pastes (clipboard + delayed Cmd-V).
  local pcalls = {}
  H.VB.result = { delete = function() pcalls[#pcalls + 1] = "deleted" end }
  hs = {
    pasteboard = { setContents = function(t) pcalls[#pcalls + 1] = { "setContents", t } end },
    timer = { doAfter = function(d, fn) pcalls[#pcalls + 1] = { "doAfter", d }; fn() end },
    eventtap = { keyStroke = function(mods, key) pcalls[#pcalls + 1] = { "keyStroke", mods, key } end },
  }
  h.onPaste("paste text")
  hs = nil
  eq(pcalls[1], "deleted", "onPaste closes the result panel first")
  eq(pcalls[2][2], "paste text", "onPaste puts the given text on the clipboard")
  eq(pcalls[4][3], "v", "onPaste synthesizes Cmd-V")
  H.VB.result = nil

  -- onEmail: closes the panel, pins the email-reformat flags, and re-runs
  -- the engine on the given text (the panel's only edge back to the engine).
  H.VB.result = { delete = function() end }
  local encoded
  hs = {
    json = { encode = function(t) encoded = t; return "{}" end },
    http = { asyncPost = function() end },
    timer = { doAfter = function() end },   -- watchdog: don't fire
  }
  H.VB.runId = nil
  h.onEmail("raw text")
  hs = nil
  check(encoded ~= nil and encoded.argv ~= nil, "onEmail re-runs the engine via runEngine")
  eq(encoded.argv[1], "text", "onEmail runs the 'text' subcommand")
  eq(encoded.argv[2], "raw text", "onEmail forwards the given text")
  check(encoded.argv[3] == "--mode" and encoded.argv[4] == "email",
    "onEmail pins the email reformat mode")
  H.VB.result, H.VB.runId, H.VB.state = nil, nil, "idle"
end

-- =====================================================================
-- refreshModes (engine-contract): fetches the mode catalog from the engine;
-- its task-exit callback only swaps MODES in when the payload decodes to a
-- non-empty table, otherwise keeps the existing (fallback) catalog.
-- =====================================================================
do
  local function freshTask()
    local capturedCb
    hs = { task = { new = function(_, cb) capturedCb = cb
      return { setEnvironment = function() end, start = function() end } end } }
    H.refreshModes()
    hs = nil
    return capturedCb
  end

  -- Success: a non-empty catalog swaps MODES in (observed via modesForJS)
  -- and, with the app window open, pushes it in too.
  local cb1 = freshTask()
  H.VB.win = { evaluateJavaScript = function() end }
  hs = { json = {
    decode = function() return { { key = "custom1", label = "Custom One" } } end,
    encode = function() return "[]" end,
  } }
  cb1(0, '[{"key":"custom1","label":"Custom One"}]')
  hs = nil
  local js = H.modesForJS()
  local found = false
  for _, e in ipairs(js) do if e.key == "custom1" then found = true end end
  check(found, "refreshModes(success) swaps MODES in with the decoded catalog")
  H.VB.win = nil

  -- Failure: non-zero exit code -> keeps the fallback, no decode attempted.
  local cb2 = freshTask()
  hs = { json = { decode = function() error("should not decode on a failed run") end } }
  cb2(1, "")
  hs = nil
  check(true, "refreshModes(nonzero exit) keeps the fallback without decoding")

  -- Failure: empty stdout -> also keeps the fallback.
  local cb3 = freshTask()
  hs = { json = { decode = function() error("should not decode empty output") end } }
  cb3(0, "")
  hs = nil
  check(true, "refreshModes(empty output) keeps the fallback without decoding")

  -- Failure: undecodable stdout (pcall catches it) -> keeps the fallback.
  local cb4 = freshTask()
  hs = { json = { decode = function() error("boom") end } }
  cb4(0, "not json")
  hs = nil
  check(true, "refreshModes(undecodable output) keeps the fallback")

  -- Failure: decodes, but to an empty table -> also keeps the fallback.
  local cb5 = freshTask()
  hs = { json = { decode = function() return {} end } }
  cb5(0, "[]")
  hs = nil
  check(true, "refreshModes(empty catalog) keeps the fallback")
end

-- =====================================================================
-- fetchContract (engine-contract): shells `voicebridge.py contract` and
-- decodes its JSON, or nil when the engine produced nothing usable.
-- =====================================================================
do
  local shelledCmd
  hs = {
    execute = function(cmd) shelledCmd = cmd; return '{"daemon":{"port":9999}}' end,
    json = { decode = function() return { daemon = { port = 9999 } } end },
  }
  local c = H.fetchContract()
  check(c ~= nil and c.daemon.port == 9999, "fetchContract decodes a valid contract")
  check(shelledCmd ~= nil and shelledCmd:find(" contract ", 1, true) ~= nil,
    "fetchContract shells the 'contract' subcommand")
  hs = nil

  hs = { execute = function() return "" end }
  eq(H.fetchContract(), nil, "fetchContract(empty output) -> nil")
  hs = nil

  hs = { execute = function() return "not json" end, json = { decode = function() error("boom") end } }
  eq(H.fetchContract(), nil, "fetchContract(undecodable output) -> nil")
  hs = nil

  hs = { execute = function() return "42" end, json = { decode = function() return 42 end } }
  eq(H.fetchContract(), nil, "fetchContract(non-table payload) -> nil")
  hs = nil
end

-- =====================================================================
-- applyContract (engine-contract): derives STATUS_SENTINEL/STATUS_SEP/
-- RESULT_SENTINEL/LLM_FAILED/PASTE_FAILED and DAEMON_PORT/DAEMON_URL from a
-- decoded contract, keeping each field's current value when the contract
-- doesn't carry it. Verified through the LIVE globals' own readers
-- (parseStatus reads the sentinel/sep at call time; the daemon URL is
-- observed via runEngine's own POST target) rather than by inspecting
-- STATUS_SENTINEL etc. directly -- exporting a plain value would only
-- snapshot it once, at module-load time, not track later reassignment.
-- =====================================================================
do
  -- Full contract: every status-line field + the daemon host/port overridden.
  H.applyContract({
    status_line = { sentinel = "MY_STATUS", sep = "|", result_sentinel = "MY_RESULT",
                     llm_failed_suffix = "llmoops", paste_failed_suffix = "pasteoops" },
    daemon = { host = "10.0.0.5", port = 9999 },
  })
  local p = H.parseStatus("MY_STATUS|copied|llmoops")
  check(p ~= nil and p[1] == "copied" and p[#p] == "llmoops",
    "applyContract(full) rewires parseStatus to the new sentinel/sep/suffix")
  eq(H.parseStatus("VB_STATUS\tcopied"), nil,
    "applyContract(full) means the OLD default sentinel no longer parses")

  local capturedUrl
  hs = {
    json = { encode = function() return "{}" end },
    http = { asyncPost = function(url) capturedUrl = url end },
    timer = { doAfter = function() end },
  }
  H.runEngine({ "process", "/x.wav" })
  hs = nil
  eq(capturedUrl, "http://10.0.0.5:9999/", "applyContract(full) rewires DAEMON_URL to the new host/port")

  -- Partial contract (a non-table status_line, a non-numeric port): every
  -- field keeps its CURRENT value (no override applies).
  H.applyContract({ status_line = "not a table", daemon = { port = "not a number" } })
  local p2 = H.parseStatus("MY_STATUS|copied")
  check(p2 ~= nil, "applyContract(non-table status_line) leaves the current sentinel untouched")

  local capturedUrl2
  hs = {
    json = { encode = function() return "{}" end },
    http = { asyncPost = function(url) capturedUrl2 = url end },
    timer = { doAfter = function() end },
  }
  H.runEngine({ "process", "/x.wav" })
  hs = nil
  eq(capturedUrl2, "http://127.0.0.1:9999/",
    "applyContract(non-numeric port) keeps the current port but resets the host to its default")

  -- Reset the live globals back to their original literal defaults so this
  -- test doesn't leave shared module state mutated.
  H.applyContract({
    status_line = { sentinel = "VB_STATUS", sep = "\t", result_sentinel = "VB_RESULT",
                     llm_failed_suffix = "llm_failed", paste_failed_suffix = "paste_failed" },
    daemon = { host = "127.0.0.1", port = 8763 },
  })
  eq(H.parseStatus("VB_STATUS\tcopied")[1], "copied", "applyContract reset restores the default sentinel/sep")
  H.VB.state = "idle"
  H.VB.runId = nil
  H.VB.captureFlags = nil
end

-- =====================================================================
-- resolveConfigPath (engine-contract): the (cached or freshly fetched)
-- contract's first config_search path, with a leading ~ expanded to HOME,
-- or the literal fallback when no usable one is available.
-- =====================================================================
do
  local home = os.getenv("HOME")

  -- Cached contract with a usable config_search[1]: expands ~ to HOME.
  H.VB.contract = { config_search = { "~/custom/config.toml" } }
  eq(H.resolveConfigPath(), home .. "/custom/config.toml",
    "resolveConfigPath uses the cached contract's first config_search entry")

  -- No cached contract: fetchContract() is called (faked here) -- a
  -- contract with no config_search at all falls back to the literal path.
  H.VB.contract = nil
  hs = {
    execute = function() return '{"contract":true}' end,
    json = { decode = function() return { contract = true } end },
  }
  eq(H.resolveConfigPath(), home .. "/.config/voicebridge/config.toml",
    "resolveConfigPath(no cache, fetched contract w/o config_search) falls back to the literal path")
  hs = nil

  -- A cached contract WITH a config_search table, but an empty/unusable
  -- first entry: also falls back.
  H.VB.contract = { config_search = {} }
  eq(H.resolveConfigPath(), home .. "/.config/voicebridge/config.toml",
    "resolveConfigPath(config_search present but empty) falls back to the literal path")

  H.VB.contract = nil
end

-- =====================================================================
-- startDaemon / restartDaemon / ensureDaemon (daemon-launch): the warm
-- background engine's launch, health-check, and manual-restart trio.
-- =====================================================================
do
  -- startDaemon: ensures ~/.voicebridge exists, then shells the detached
  -- nohup launch command built by buildStartDaemonCmd.
  local execCmd
  hs = {
    fs = { attributes = function() return true end },
    execute = function(cmd) execCmd = cmd end,
  }
  H.startDaemon()
  hs = nil
  check(execCmd ~= nil and execCmd:find("nohup", 1, true) ~= nil,
    "startDaemon shells the detached nohup launch command")

  -- restartDaemon: pkills the old daemon, alerts, then reschedules startDaemon.
  local killCmd, alertMsg, scheduledDelay, scheduledFn
  hs = {
    execute = function(cmd) killCmd = cmd end,
    alert = { show = function(msg) alertMsg = msg end },
    timer = { doAfter = function(delay, fn) scheduledDelay = delay; scheduledFn = fn end },
  }
  H.restartDaemon()
  hs = nil
  check(killCmd ~= nil and killCmd:find("pkill", 1, true) ~= nil, "restartDaemon pkills the daemon process")
  eq(alertMsg, "Restarting Alfred engine…", "restartDaemon shows the restart alert")
  eq(scheduledDelay, 0.6, "restartDaemon schedules the relaunch after 0.6s")
  eq(scheduledFn, H.startDaemon, "restartDaemon schedules startDaemon itself")

  -- ensureDaemon: pings the daemon; only a non-200 relaunches it.
  local capturedCb
  hs = { http = { asyncGet = function(_, _, cb) capturedCb = cb end } }
  H.ensureDaemon()
  check(capturedCb ~= nil, "ensureDaemon registers an asyncGet callback")

  local execCalls = 0
  hs.fs = { attributes = function() return true end }
  hs.execute = function() execCalls = execCalls + 1 end
  capturedCb(200)
  eq(execCalls, 0, "ensureDaemon(status 200) does not relaunch the daemon")
  capturedCb(500)
  eq(execCalls, 1, "ensureDaemon(status ~= 200) relaunches the daemon via startDaemon")

  hs = nil
end

-- =====================================================================
-- openConfig (engine-contract): opens the resolved config path in the
-- default editor, falling back (at the shell level) to the shipped example.
-- =====================================================================
do
  local home = os.getenv("HOME")
  H.VB.contract = { config_search = { "~/.config/voicebridge/config.toml" } }
  local execCmd
  hs = { execute = function(cmd) execCmd = cmd end }
  H.openConfig()
  hs = nil
  check(execCmd ~= nil and execCmd:find("open %-t", 1) ~= nil, "openConfig shells an 'open -t' command")
  check(execCmd:find(home .. "/.config/voicebridge/config.toml", 1, true) ~= nil,
    "openConfig targets the resolved config path")
  check(execCmd:find("config.example.toml", 1, true) ~= nil,
    "openConfig's command also carries the shipped-example fallback")
  H.VB.contract = nil
end

-- ---- summary -------------------------------------------------------------
local total = passed + failed
print(string.format("test_helpers.lua: %d/%d assertions passed", passed, total))
if failed > 0 then
  print(string.format("FAILED: %d assertion(s) failed", failed))
  os.exit(1)
end
print("OK")
