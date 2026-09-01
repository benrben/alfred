-- luacheck config for the Hammerspoon front-end (voicebridge.lua +
-- voicebridge_lua/*.lua).
-- Lua silently resolves a typo'd global to nil at runtime, and Hammerspoon
-- surfaces that only in its console — exactly the bug class luacheck catches.
-- Run: make lint  (or: luacheck voicebridge.lua voicebridge_lua tests/lua/)

std = "lua54"

-- Globals provided by the Hammerspoon runtime (and our own test seam).
read_globals = {
  "hs",              -- the Hammerspoon API
}
globals = {
  "voicebridgeTest",    -- the _G debug hooks the file installs
  "voicebridgeProcess",
  "voicebridgeWindow",
  -- Everything below is module-scope, not a real Hammerspoon global: it lives
  -- in a private table (see voicebridge.lua's "Shared module scope" comment),
  -- reached through a custom _ENV, because Lua has no package system that
  -- lets separate files share plain `local`s. luacheck can't see that _ENV
  -- trick statically, so every name declared without `local` at module scope
  -- across voicebridge.lua and voicebridge_lua/*.lua must be listed here or
  -- it reads as an accidental real global.
  "BACKENDS",
  "BUILTIN_CATALOG",
  "DAEMON_DOWN_STATUS",
  "DAEMON_LOG",
  "DAEMON_PORT",
  "DAEMON_URL",
  "DEBUG",
  "DICTATE_HOTKEY",
  "DIR",
  "DUMP_FILE",
  "ENV_PREFIX",
  "ERROR_MESSAGES",
  "HOME",
  "HUD_H_BASE",
  "HUD_W",
  "ICONS",
  "INTENT_HOTKEY",
  "LLM_FAILED",
  "LOG_FILE",
  "MAX_RECORD_SECS",
  "MET_H",
  "MET_MAXW",
  "MET_X",
  "MET_Y",
  "MODES",
  "MODE_RAWTX",
  "PASTE_FAILED",
  "PYTHON",
  "RAWTX_HOTKEY",
  "RESULT_ACTIONS",
  "RESULT_SENTINEL",
  "SCRIPT",
  "SHOW_METER",
  "SOX",
  "STATUS_SENTINEL",
  "STATUS_SEP",
  "TASK_ENV",
  "TYPE_HOTKEY",
  "USER_PATH",
  "VB",
  "VB_DIR",
  "WINDOW_HOTKEY",
  "WIN_H",
  "WIN_HEAD",
  "WIN_TAIL",
  "WIN_W",
  "applyContract",
  "buildCaptureFlags",
  "buildEngineArgv",
  "buildLiveMenu",
  "buildMenu",
  "buildModes",
  "buildStartDaemonCmd",
  "cancel",
  "cancelWatchdog",
  "classifyPost",
  "classifyResult",
  "closeResult",
  "dbg",
  "destroyHUD",
  "dictateWithMode",
  "ensureDaemon",
  "ensureVBDir",
  "errorMessage",
  "fetchContract",
  "fmtTime",
  "historyItemFrom",
  "historyPath",
  "historyRecordValid",
  "historyStartIndex",
  "hudBaseElements",
  "hudCanvasFrame",
  "hudHeight",
  "hudMeterColor",
  "hudMeterFrame",
  "hudMeterTrackElements",
  "hudMeterVisible",
  "hudPulseColor",
  "iconForState",
  "jsStr",
  "modesForJS",
  "normalizeBackend",
  "normalizeTranslate",
  "notify",
  "onRecDone",
  "onResult",
  "onWebMessage",
  "openConfig",
  "openRecordings",
  "openWindow",
  "parseHistory",
  "parseStatus",
  "pasteText",
  "pickMode",
  "pushWindowState",
  "readHistory",
  "refreshModes",
  "refreshSettings",
  "reloadHammerspoon",
  "resolveConfigPath",
  "restartDaemon",
  "resultBanner",
  "resultClick",
  "resultDispatch",
  "resultPanelHandlers",
  "resultPayload",
  "runEngine",
  "runEngineOneShot",
  "saveIntent",
  "setBackendValue",
  "setDebugForTest",
  "setModel",
  "setState",
  "showHUD",
  "showResult",
  "soxLevel",
  "soxStream",
  "startDaemon",
  "startRecording",
  "startWatchdog",
  "stopRecording",
  "taskOutputReady",
  "toggleDictate",
  "toggleTranscribeOnly",
  "tmpWav",
  "typePrompt",
  "updateHUD",
  "updateResult",
  "vbDirReady",
  "windowCaptureFlags",
  "windowToggleRecord",
}

-- The test seam sets an intentional global for plain-lua loading. `hs` is
-- normally read_globals-only (see above); tests need to WRITE it too, to
-- install a fake Hammerspoon API around the showHUD() coverage test.
-- `io.open` is temporarily patched (and restored) around the dbg() coverage
-- test, so a real ~/.voicebridge/voicebridge.log is never touched.
files["tests/lua/"] = {
  std = "lua54",
  globals = { "hs", "io.open" },
}

exclude_files = { ".venv", "raycast", "node_modules" }

-- Keep the signal high: don't fail on unused self-documenting args / line length.
unused_args = false
max_line_length = false
