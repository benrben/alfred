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
  eq(H.fmtTime(-5), "00:00", "fmtTime clamps negative durations")
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
-- daemon identity: a localhost HTTP 200 is not sufficient to trust a service.
-- Restart must also accept only a recorded Alfred PID.
-- =====================================================================
do
  local good = { ok = true, app = "alfred", schema_version = 1, pid = 4321,
                 port = 8763 }
  check(H.isAlfredIdentity(good), "isAlfredIdentity accepts Alfred")
  check(not H.isAlfredIdentity({ ok = true, app = "other", schema_version = 1,
                                 pid = 4321 }),
        "isAlfredIdentity rejects a foreign service")
  eq(H.daemonPidFromInfo("good", function() return good end), 4321,
     "daemonPidFromInfo returns the recorded Alfred PID")
  eq(H.daemonPidFromInfo("foreign", function()
    return { ok = true, app = "other", schema_version = 1, pid = 4321,
             port = 8763 }
  end), nil, "daemonPidFromInfo rejects a foreign PID record")
end

-- ---- summary -------------------------------------------------------------
local total = passed + failed
print(string.format("test_helpers.lua: %d/%d assertions passed", passed, total))
if failed > 0 then
  print(string.format("FAILED: %d assertion(s) failed", failed))
  os.exit(1)
end
print("OK")
