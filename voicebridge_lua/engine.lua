-- Alfred (Hammerspoon front-end) — engine plumbing: status-line parsing,
-- the warm daemon (start/health-check/restart), and running the Python engine
-- (voicebridge.py) either through the daemon or as a one-shot process.
-- Split out of voicebridge.lua (see voicebridge.lua's loader comment for the
-- shared-module-scope convention every split file uses).

-- ---- Engine plumbing -----------------------------------------------------

-- The status-line grammar: sentinel, field separator, and the suffix literals.
-- Defaults mirror voicebridge.py's CONTRACT; applyContract() overrides them from
-- the live engine at load so the two can't silently drift. Read at CALL time by
-- parseStatus/classifyResult, so the load-time override takes effect everywhere.
STATUS_SENTINEL = "VB_STATUS"
STATUS_SEP      = "\t"
RESULT_SENTINEL = "VB_RESULT"
LLM_FAILED      = "llm_failed"
PASTE_FAILED    = "paste_failed"

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
function resultPayload(out)
  for line in (out or ""):gmatch("[^\r\n]+") do
    local enc = line:match("^" .. RESULT_SENTINEL .. STATUS_SEP .. "(.+)$")
    if enc then return enc end
  end
  return nil
end

-- Pure: map an engine error subtype (+ optional stderr tail) to a human message.
ERROR_MESSAGES = {
  audio_not_found = "No audio to transcribe.",
  stt_failed      = "Transcription failed.",
  llm_failed      = "The rewrite step failed.",
  -- The clipboard/paste/save-to-file step itself failed (e.g. an unwritable
  -- save_dir). The text isn't lost — the engine keeps it in history first.
  deliver_failed  = "Couldn't deliver the result (see History — it was saved there).",
  runtime         = "The engine hit an error.",
}
function errorMessage(subtype, tail)
  local base = ERROR_MESSAGES[subtype] or "Something went wrong."
  if tail and #tail > 0 then return base .. " (" .. tail .. ")" end
  return base
end

-- Pure: the "copied" banner text, reused by the toast panel, the window's
-- notify fallback, and the plain notification (was duplicated 3× with drift).
function resultBanner(llmFailed)
  return llmFailed and "Copied raw transcript (LLM step failed)"
                   or  "Copied to clipboard ✓"
end

-- Pure: does the parsed status-line parts list carry the paste_failed suffix?
-- Factored out of classifyResult to keep its own branch count low.
local function hasPasteFailed(parts)
  for _, p in ipairs(parts) do
    if p == PASTE_FAILED then return true end
  end
  return false
end

-- Pure: decode the still-ENCODED VB_RESULT payload via the injected `decode`;
-- nil when there's no payload, no decoder, or decoding fails / yields a
-- non-string (the caller then falls back to the clipboard). Factored out of
-- classifyResult for the same reason.
local function decodeResultText(out, decode)
  local enc = resultPayload(out)
  if not enc or not decode then return nil end
  local ok, dec = pcall(decode, enc)
  if not (ok and type(dec) == "string") then return nil end
  return dec
end

-- Pure: the last non-blank line of stderr (the real error detail).
local function stderrTail(err)
  return (err or ""):gsub("%s+$", ""):match("[^\r\n]+$")
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
function classifyResult(code, out, err, decode)
  local parts = parseStatus(out) or {}
  local kind = parts[1]
  return {
    kind = kind,
    path = (kind == "saved") and parts[2] or nil,
    subtype = (kind == "error") and parts[2] or nil,
    llmFailed = parts[#parts] == LLM_FAILED,
    pasteFailed = hasPasteFailed(parts),
    tail = stderrTail(err),
    text = decodeResultText(out, decode),
  }
end

-- Processing watchdog: if a run neither delivers nor connection-fails within the
-- bound (a wedged engine / a lost callback), terminate any one-shot task and
-- reset to idle so the UI can't get stuck on ⏳ forever. Tied to VB.runId so a
-- newer capture's watchdog supersedes an older one.
function cancelWatchdog()
  if VB.watchdog then VB.watchdog:stop(); VB.watchdog = nil end
end
function startWatchdog(runId)
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

-- Debug-only: dump this capture's raw code/stdout/stderr verbatim to
-- DUMP_FILE (owner-only ~/.voicebridge), gated behind DEBUG so it's opt-in.
local function dumpCapture(code, out, err)
  if not DEBUG then return end
  ensureVBDir()
  local fh = io.open(DUMP_FILE, "w")
  if not fh then return end
  fh:write("code=" .. tostring(code) .. "\n--- STDOUT ---\n" .. (out or "") ..
           "\n--- STDERR ---\n" .. (err or ""))
  fh:close()
end

-- The "copied" branch: prefer the engine's exact delivered text (VB_RESULT);
-- the clipboard is a fallback for older engines that don't emit the result
-- line. updateResult is pcall-wrapped so a UI-layer failure still leaves the
-- user with SOME feedback (the plain banner) instead of a silently stuck HUD.
local function handleCopied(r)
  local txt = r.text or hs.pasteboard.getContents() or ""
  local ok, e = pcall(updateResult, txt, r.llmFailed)
  if not ok then
    dbg("updateResult ERROR: " .. tostring(e))
    notify("Alfred", resultBanner(r.llmFailed))
  end
  if r.pasteFailed then
    notify("Alfred", "Auto-paste failed — granted Accessibility to Hammerspoon?")
  end
end

-- The "saved" branch: notify with a click-to-reveal action (Finder select).
local function handleSaved(r)
  local path = r.path
  notify("Alfred", "Too long — saved to file (click to reveal)",
    function() if path then hs.execute("open -R '" .. path .. "'") end end)
end

-- No VB_STATUS line at all (a crash before the sentinel, or a foreign
-- response): surface whatever stderr detail we have.
local function handleUnknown(r)
  notify("Alfred", "Error: " .. (r.tail or
    "see the engine log (~/.voicebridge/daemon.log)"))
end

-- Thin shell over classifyResult: dispatch the finished run to the UI. `err` is
-- the daemon's captured stderr (the actual failure detail); we prefer the exact
-- delivered text from VB_RESULT and only read the clipboard as a fallback.
function onResult(code, out, err)
  cancelWatchdog()
  setState("idle")
  VB.engineTask = nil   -- release the retained task now that it has finished
  dumpCapture(code, out, err)
  local r = classifyResult(code, out, err, hs.json.decode)
  dbg("onResult code=" .. tostring(code) .. " kind=" .. tostring(r.kind) ..
      " sub=" .. tostring(r.subtype) .. " err=[" ..
      ((err or ""):gsub("\n", "\\n")):sub(1, 200) .. "]")

  if r.kind == "copied" then
    handleCopied(r)
  elseif r.kind == "saved" then
    handleSaved(r)
  elseif r.kind == "empty" then
    notify("Alfred", "No speech detected.")
  elseif r.kind == "error" then
    notify("Alfred", errorMessage(r.subtype, r.tail))
  else
    handleUnknown(r)
  end
end

-- Pure: the detached-launch shell command for the warm daemon, redirecting its
-- stdout/stderr into `logFile`. Extracted so a test can assert that file is
-- NEVER under /tmp: the daemon logs "transcript (…): <first 120 chars>" on
-- every capture, so a world-readable /tmp log would leak verbatim dictation to
-- any other local account. logFile must be under the owner-only (0700)
-- ~/.voicebridge (ensureVBDir()) — sufficient on its own, since no other user
-- can open a path through a 0700 directory regardless of the file's own mode.
function buildStartDaemonCmd(envPrefix, python, script, port, logFile)
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
    cancelWatchdog()
    setState("idle")
    notify("Alfred", "Could not launch the engine. Check PYTHON path in voicebridge.lua")
  end
end

-- Pure: assemble the engine argv from the base subcommand, the per-capture flags,
-- and the active backend. (python/script are prepended by the caller.)
function buildEngineArgv(argv, captureFlags, backend)
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
DAEMON_DOWN_STATUS = { [-1004] = true }
function classifyPost(status, hasBody)
  if status == 200 and hasBody then return "ok" end
  if DAEMON_DOWN_STATUS[status] then return "down" end
  return "busy"
end

-- The daemon POST's "ok" outcome (200 + a body): decode it and dispatch via
-- onResult, or (a 200 with an unreadable body) bail out to idle without
-- re-running -- the engine may already have done the work.
local function handlePostOk(body)
  local ok, resp = pcall(hs.json.decode, body)
  if ok and type(resp) == "table" then
    dbg("daemon result code=" .. tostring(resp.code))
    onResult(resp.code or 0, resp.out or "", resp.err or "")   -- thread err
    return
  end
  cancelWatchdog(); setState("idle")
  dbg("daemon 200 but undecodable body")
  notify("Alfred", "The engine returned an unreadable response.")
end

-- The daemon POST's "down" outcome: nothing is listening, so re-running the
-- job as a one-shot is safe (the watchdog stays armed to bound it); also
-- bring the daemon back up for next time.
local function handlePostDown(cmd, status)
  dbg("daemon down (status=" .. tostring(status) .. ") -> one-shot")
  runEngineOneShot(cmd)
  ensureDaemon()
end

-- The daemon POST's "busy" outcome (the ~60s asyncPost timeout, or any other
-- non-down status): the engine is still working. Keep 'processing' and let
-- the watchdog bound it -- re-running here would double-write history/clipboard.
local function handlePostBusy(status)
  dbg("daemon busy (status=" .. tostring(status) .. ") -> keep processing, no re-run")
  notify("Alfred", "Still transcribing… the engine is taking a while.")
end

-- Dispatch a finished daemon POST by classifyPost's outcome.
local function handlePostResult(status, body, cmd)
  local outcome = classifyPost(status, body ~= nil and #body > 0)
  if outcome == "ok" then
    handlePostOk(body)
  elseif outcome == "down" then
    handlePostDown(cmd, status)
  else
    handlePostBusy(status)
  end
end

-- `argv` starts at the subcommand (e.g. {"process", wav}); no python/script.
function runEngine(argv)
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
  hs.http.asyncPost(DAEMON_URL, hs.json.encode({ argv = cmd }),
    { ["Content-Type"] = "application/json" },
    function(status, body)
      if myRun ~= VB.runId then
        dbg("runEngine[" .. myRun .. "]: stale result (cur=" .. tostring(VB.runId) .. ") — dropped")
        return
      end
      handlePostResult(status, body, cmd)
    end)
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
      closeResult()
      VB.captureFlags = { "--mode", "email" }
      runEngine({ "text", text })
    end,
    onDiscard = function() closeResult() end,
  }
end

-- Pure: does a "modes" task's (exit code, stdout) look like it's worth
-- decoding? Factored out of refreshModes' task callback to keep its own
-- branch count low (mirrors taskOutputReady in window.lua for "settings").
local function modesOutputReady(code, out)
  return code == 0 and out and #out > 0
end

-- Pure: is a decoded "modes" payload actually usable (a non-empty table)?
local function decodedModesUsable(ok, parsed)
  return ok and type(parsed) == "table" and #parsed > 0
end

-- Apply a freshly decoded mode catalog: rebuild MODES, log it, and (if the
-- app window is open) push the new list into it.
local function applyModes(parsed)
  MODES = buildModes(parsed)
  dbg("refreshModes: " .. #parsed .. " modes loaded")
  if VB.win then
    VB.win:evaluateJavaScript("window.vbModes&&vbModes(" .. hs.json.encode(modesForJS()) .. ",1)")
  end
end

local function refreshModesCallback(code, out)
  if modesOutputReady(code, out) then
    local ok, parsed = pcall(hs.json.decode, out)
    if decodedModesUsable(ok, parsed) then
      applyModes(parsed)
      return
    end
  end
  dbg("refreshModes failed (code=" .. tostring(code) .. "); keeping fallback")
end

-- Load the rewrite-mode catalog (built-in + custom [intent]) from the engine,
-- so the picker reflects config edits. Async; falls back to BUILTIN_CATALOG.
function refreshModes()
  local t = hs.task.new(PYTHON, refreshModesCallback, { SCRIPT, "modes" })
  VB.modesTask = t          -- retain so GC doesn't kill it before it returns
  t:setEnvironment(TASK_ENV)
  t:start()
end

-- Fetch + decode the engine's CONTRACT once (fast: heavy libs are lazy-imported,
-- so `contract` just prints JSON). Returns the decoded table or nil. Cached in
-- VB.contract at load; applyContract() derives our port/paths/sentinels from it
-- so the front-end can't silently drift from the engine.
function fetchContract()
  local out = hs.execute(ENV_PREFIX .. "'" .. PYTHON .. "' '" .. SCRIPT ..
    "' contract 2>/dev/null")
  if out and #out > 0 then
    local ok, c = pcall(hs.json.decode, out)
    if ok and type(c) == "table" then return c end
  end
  return nil
end

-- Pure config-table narrowing: `v` if it's a table, else {} -- kept as its own
-- helper so applyContract's own branch count stays low.
local function tableOrEmpty(v)
  return (type(v) == "table") and v or {}
end

-- Apply the status-line section of the contract (sentinel/sep/result
-- sentinel/llm+paste suffixes), keeping each field's current value when the
-- contract doesn't carry it (older engine) -- see applyContract's own comment.
local function applyStatusLineContract(sl)
  if type(sl.sentinel) == "string" then STATUS_SENTINEL = sl.sentinel end
  if type(sl.sep) == "string" then STATUS_SEP = sl.sep end
  if type(sl.result_sentinel) == "string" then RESULT_SENTINEL = sl.result_sentinel end
  if type(sl.llm_failed_suffix) == "string" then LLM_FAILED = sl.llm_failed_suffix end
  if type(sl.paste_failed_suffix) == "string" then PASTE_FAILED = sl.paste_failed_suffix end
end

-- Apply the daemon section: default host "127.0.0.1" unless the contract sets
-- one, DAEMON_PORT only when the contract carries a numeric port, and always
-- rebuild DAEMON_URL from the (possibly updated) host/port.
local function applyDaemonContract(d)
  local host = type(d.host) == "string" and d.host or "127.0.0.1"
  if type(d.port) == "number" then DAEMON_PORT = math.floor(d.port) end
  DAEMON_URL = "http://" .. host .. ":" .. DAEMON_PORT .. "/"
end

-- Derive our copies of the engine's constants from a decoded contract, KEEPING
-- the literal fallbacks where a field is missing (older engine). Touches only
-- primitives already defaulted above, so a partial contract degrades gracefully.
function applyContract(c)
  applyStatusLineContract(tableOrEmpty(c.status_line))
  applyDaemonContract(tableOrEmpty(c.daemon))
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
function openConfig()
  hs.execute("open -t '" .. resolveConfigPath() .. "' 2>/dev/null || open -t '"
    .. DIR .. "/config.example.toml'")
end

