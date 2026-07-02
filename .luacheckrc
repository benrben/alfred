-- luacheck config for the Hammerspoon front-end (voicebridge.lua).
-- Lua silently resolves a typo'd global to nil at runtime, and Hammerspoon
-- surfaces that only in its console — exactly the bug class luacheck catches.
-- Run: make lint  (or: luacheck voicebridge.lua tests/lua/)

std = "lua54"

-- Globals provided by the Hammerspoon runtime (and our own test seam).
read_globals = {
  "hs",              -- the Hammerspoon API
}
globals = {
  "voicebridgeTest", -- the _G debug hook the file installs
}

-- The test seam sets an intentional global for plain-lua loading.
files["tests/lua/"] = {
  std = "lua54",
}

exclude_files = { ".venv", "raycast", "node_modules" }

-- Keep the signal high: don't fail on unused self-documenting args / line length.
unused_args = false
max_line_length = false
