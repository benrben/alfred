/**
 * Alfred engine client for Raycast — preferences, environment, and locating
 * the engine script/interpreter.
 *
 * Split out of engine.ts (which re-exports everything from here) to stay
 * under the repository's 600-line File LOC gate — see engine.ts's own comment
 * for the full picture and the other split files.
 */
import { getPreferenceValues } from "@raycast/api";
import { existsSync } from "node:fs";
import { homedir, userInfo } from "node:os";
import { dirname, join } from "node:path";

export interface Preferences {
  daemonPort: string;
  backend: string; // default | local | auto | claude | codex
  translate: string; // default | on | off
  pythonBin: string;
  engineScript: string;
  soxBin: string;
}

export function getPrefs(): Preferences {
  return getPreferenceValues<Preferences>();
}

/** Expand a leading ~ and $HOME in a user-supplied path. */
export function expandHome(p: string): string {
  if (!p) return p;
  let out = p;
  if (out.startsWith("~")) out = join(homedir(), out.slice(1));
  return out.replace(/\$HOME/g, homedir());
}

/** A login-ish PATH so the engine (and the claude/codex it spawns) resolves even
 * though Raycast launches us with a trimmed environment. Mirrors the .lua. */
function enrichedPath(): string {
  const extra = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    join(homedir(), ".local/bin"),
    join(homedir(), ".codex/bin"),
    join(homedir(), ".codex/packages/standalone/current/bin"),
    join(homedir(), ".npm-global/bin"),
    join(homedir(), ".volta/bin"),
    join(homedir(), ".asdf/shims"),
    join(homedir(), ".cargo/bin"),
    join(homedir(), ".bun/bin"),
  ];
  return [process.env.PATH ?? "", ...extra].filter(Boolean).join(":");
}

export function engineEnv(): NodeJS.ProcessEnv {
  // Raycast launches us with a trimmed environment. Restore what the engine and
  // the claude/codex it spawns need:
  //  - PATH: so the CLIs resolve by name (Raycast's PATH is minimal).
  //  - HOME: so claude/codex find ~/.claude, ~/.codex.
  //  - USER/LOGNAME: claude/codex read their OAuth login from the macOS Keychain,
  //    and the Keychain lookup needs the user identity in the env — without it
  //    claude reports "Not logged in" and the LLM step fails.
  //  - LANG/LC_ALL: force UTF-8 so a bare locale doesn't mangle curly quotes /
  //    Hebrew into mac-roman.
  const user = process.env.USER || userInfo().username;
  return {
    ...process.env,
    PATH: enrichedPath(),
    HOME: homedir(),
    // Raycast may not inherit the shell variable used by a terminal-launched
    // Codex. Set the default explicitly so `codex exec` reads the same login
    // at ~/.codex/auth.json; preserve a deliberate custom CODEX_HOME.
    CODEX_HOME: process.env.CODEX_HOME || join(homedir(), ".codex"),
    USER: user,
    LOGNAME: process.env.LOGNAME || user,
    LANG: process.env.LANG || "en_US.UTF-8",
    LC_ALL: process.env.LC_ALL || "en_US.UTF-8",
    PYTHONUTF8: "1",
  };
}

/** Candidate locations for the engine script, best first. */
function candidateScripts(): string[] {
  const fromPref = expandHome(getPrefs().engineScript);
  return [
    fromPref,
    join(homedir(), "Claude/Projects/alfred/voicebridge.py"),
    join(homedir(), "alfred/voicebridge.py"),
    join(homedir(), "Projects/alfred/voicebridge.py"),
    join(homedir(), "src/alfred/voicebridge.py"),
  ].filter(Boolean);
}

/** Resolve voicebridge.py: the preference if it exists, else a known location.
 * Keeps the extension working even if the path preference is stale. */
export function resolveScript(): string {
  for (const c of candidateScripts()) {
    if (existsSync(c)) return c;
  }
  return candidateScripts()[0] || "voicebridge.py";
}

/** Resolve the python that runs the engine: the preference, else the venv beside
 * the script, else python3 on PATH. */
export function resolvePython(scriptPath: string): string {
  const pref = expandHome(getPrefs().pythonBin);
  if (pref && existsSync(pref)) return pref;
  const venv = join(dirname(scriptPath), ".venv", "bin", "python3");
  if (existsSync(venv)) return venv;
  return "python3";
}

export interface EngineResult {
  code: number;
  out: string;
  err: string;
}
