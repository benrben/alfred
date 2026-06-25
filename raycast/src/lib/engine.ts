/**
 * Alfred engine client for Raycast.
 *
 * Speaks the same tiny contract the Hammerspoon front-end uses:
 *   - prefer the warm daemon (`voicebridge.py serve`) over localhost HTTP:
 *       POST {"argv":[...]} -> {"code":int,"out":"<captured stdout>"}
 *   - fall back to spawning `voicebridge.py <argv>` as a one-shot if it's down,
 *     and (best effort) start the daemon for next time.
 *
 * After a `process`/`text` run (no --stdout) the engine copies the result to the
 * clipboard or saves it to a file and prints a machine-readable `VB_STATUS` line;
 * resolveDelivery() reads that back, mirroring the .lua's onResult().
 */
import { Clipboard, getPreferenceValues, getSelectedText } from "@raycast/api";
import { spawn } from "node:child_process";
import {
  existsSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir, userInfo } from "node:os";
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

const DAEMON_TIMEOUT_MS = 120_000;

export function daemonPort(): string {
  return (getPrefs().daemonPort || "8763").trim();
}

/** Quick health check of the warm daemon (GET /). */
export async function pingDaemon(): Promise<boolean> {
  try {
    const res = await fetch(`http://127.0.0.1:${daemonPort()}/`, {
      signal: AbortSignal.timeout(2000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function callEngine(argv: string[]): Promise<EngineResult> {
  const prefs = getPrefs();
  const port = (prefs.daemonPort || "8763").trim();
  const url = `http://127.0.0.1:${port}/`;
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ argv }),
      signal: AbortSignal.timeout(DAEMON_TIMEOUT_MS),
    });
    if (res.ok) {
      const data = (await res.json()) as { code?: number; out?: string };
      return { code: data.code ?? 0, out: data.out ?? "", err: "" };
    }
  } catch {
    // daemon unavailable — fall through to a one-shot process
  }
  startDaemon(); // bring it up for next time
  return runOneShot(argv);
}

function runOneShot(argv: string[]): Promise<EngineResult> {
  const script = resolveScript();
  const py = resolvePython(script);
  return new Promise((resolve) => {
    let out = "";
    let err = "";
    let child;
    try {
      child = spawn(py, [script, ...argv], { env: engineEnv() });
    } catch (e) {
      resolve({ code: 1, out: "", err: String(e) });
      return;
    }
    child.stdout.on("data", (d) => (out += d.toString()));
    child.stderr.on("data", (d) => (err += d.toString()));
    child.on("error", (e) => resolve({ code: 1, out, err: err + String(e) }));
    child.on("close", (code) => resolve({ code: code ?? 0, out, err }));
  });
}

/** Launch the warm engine daemon, detached, so it survives this command. */
export function startDaemon(): void {
  const prefs = getPrefs();
  const script = resolveScript();
  const py = resolvePython(script);
  const port = (prefs.daemonPort || "8763").trim();
  try {
    const child = spawn(py, [script, "serve", "--port", port], {
      detached: true,
      stdio: "ignore",
      env: engineEnv(),
    });
    child.unref();
  } catch {
    // best effort
  }
}

export interface Mode {
  key: string;
  label: string;
  description: string;
  prompt: string;
  default?: boolean;
}

export async function loadModes(): Promise<Mode[]> {
  const res = await callEngine(["modes"]);
  try {
    const arr = JSON.parse(res.out) as Mode[];
    if (Array.isArray(arr)) return arr;
  } catch {
    // ignore — caller falls back to a built-in list
  }
  return [];
}

// ---- Settings (backend/model + processing defaults) ----------------------

export interface Processing {
  mode: string;
  rewrite: boolean;
  translate: boolean;
  optimize: boolean;
  translate_via: string;
}

export interface Settings {
  backend: string;
  claude_model: string;
  codex_model: string;
  claude_models: string[];
  codex_models: string[];
  processing: Processing;
}

export async function loadSettings(): Promise<Settings | null> {
  const res = await callEngine(["settings"]);
  try {
    return JSON.parse(res.out) as Settings;
  } catch {
    return null;
  }
}

// ---- Formats (what the pickers offer) -------------------------------------
// A "format" bundles the engine flags for a capture:
//   - CONFIG: send NO flags — the engine uses your config.toml as-is. This is
//     the safe default: it never contradicts your translate/rewrite settings.
//   - RAW: force every stage off (a pure transcript, no LLM).
//   - the rest come from the engine's mode catalog and turn rewrite on.
// Translate/backend are layered on separately (per preference/toggle).

export const CONFIG_FORMAT_ID = "__config__";
export const RAW_FORMAT_ID = "__raw__";

export interface FormatChoice {
  id: string;
  title: string;
  subtitle: string;
  ai: boolean; // does it (or can it) invoke the LLM?
  flags: string[]; // mode/rewrite flags (no translate/backend)
}

/** The "use my config" format — no overrides. Safe fallback everywhere. */
export function configFormat(): FormatChoice {
  return {
    id: CONFIG_FORMAT_ID,
    title: "Default (config)",
    subtitle: "Use your saved settings",
    ai: true, // may invoke the LLM, depending on config
    flags: [],
  };
}

export function buildFormats(modes: Mode[]): FormatChoice[] {
  const list: FormatChoice[] = [
    configFormat(),
    {
      id: RAW_FORMAT_ID,
      title: "Raw transcript",
      subtitle: "No AI — exactly what you said",
      ai: false,
      flags: ["--no-rewrite", "--no-translate", "--no-optimize"],
    },
  ];
  for (const m of modes) {
    list.push({
      id: m.key,
      title: m.label || m.key,
      subtitle: m.description || "",
      ai: true,
      flags: ["--mode", m.key, "--rewrite"],
    });
  }
  return list;
}

/** Which AI mode the config currently resolves to (for the "Default" star in
 * Manage Intents). Note: this only reflects rewrite/mode, not translate. */
export function defaultFormatId(settings: Settings | null): string {
  const p = settings?.processing;
  if (!p || !p.rewrite) return RAW_FORMAT_ID;
  return p.mode || "raw";
}

/** Backend flag: explicit override, else preference (empty when "default"). */
export function backendFlags(override?: string): string[] {
  const b = (override ?? getPrefs().backend ?? "default").trim();
  return b && b !== "default" ? ["--backend", b] : [];
}

/** Translate flag: explicit override, else preference, else config (none). */
export function translateFlags(override?: string): string[] {
  const t = (override ?? getPrefs().translate ?? "default").trim();
  if (t === "on") return ["--translate"];
  if (t === "off") return ["--no-translate"];
  return [];
}

/** Full per-run flags for a chosen format. */
export function flagsForFormat(
  fmt: FormatChoice,
  opts: { translate?: string; backend?: string } = {},
): string[] {
  // Raw already pins --no-translate; don't let a translate toggle contradict it.
  const translate = fmt.ai ? translateFlags(opts.translate) : [];
  return [...fmt.flags, ...translate, ...backendFlags(opts.backend)];
}

/** Persist a format as the new default ([processing] mode + rewrite). */
export async function setDefaultFormat(fmt: FormatChoice): Promise<boolean> {
  const argv =
    fmt.id === RAW_FORMAT_ID
      ? ["set-processing", "--mode", "raw", "--no-rewrite"]
      : ["set-processing", "--mode", fmt.id, "--rewrite"];
  const res = await callEngine(argv);
  return (res.out || "").includes("saved");
}

/** Parse the engine's machine-readable last line: "VB_STATUS\tkind[\textra…]". */
export function parseStatus(out: string): string[] | null {
  const prefix = "VB_STATUS\t";
  for (const line of out.split(/\r?\n/)) {
    if (line.startsWith(prefix)) return line.slice(prefix.length).split("\t");
  }
  return null;
}

export interface DeliveredResult {
  kind: string; // copied | saved | empty | error | unknown
  text?: string;
  path?: string;
  llmFailed: boolean;
}

export async function resolveDelivery(
  res: EngineResult,
): Promise<DeliveredResult> {
  const parts = parseStatus(res.out);
  const kind = parts?.[0] ?? (res.code === 0 ? "unknown" : "error");
  const llmFailed = !!parts && parts[parts.length - 1] === "llm_failed";
  if (kind === "copied") {
    const text = (await Clipboard.readText()) ?? "";
    return { kind, text, llmFailed };
  }
  if (kind === "saved") {
    const path = parts?.[1];
    let text: string | undefined;
    if (path && existsSync(path)) {
      try {
        text = readFileSync(path, "utf8");
      } catch {
        // ignore
      }
    }
    return { kind, path, text, llmFailed };
  }
  return { kind, llmFailed };
}

/** Last non-empty stderr line, for surfacing engine errors. */
export function lastErrorLine(err: string): string {
  const lines = (err || "").split(/\r?\n/).filter((l) => l.trim());
  return lines.length ? lines[lines.length - 1].trim() : "unknown error";
}

export interface HistoryItem {
  ts: string;
  chars: number;
  text: string;
  source?: string;
}

export function historyFile(): string {
  return join(homedir(), ".voicebridge", "history", "history.jsonl");
}

export function readHistory(limit = 50): HistoryItem[] {
  const file = historyFile();
  if (!existsSync(file)) return [];
  let raw: string;
  try {
    raw = readFileSync(file, "utf8");
  } catch {
    return [];
  }
  const items: HistoryItem[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line) as HistoryItem;
      if (rec && typeof rec.text === "string") items.push(rec);
    } catch {
      // skip malformed line
    }
  }
  return items.reverse().slice(0, limit);
}

export async function getInputText(): Promise<string> {
  try {
    const sel = await getSelectedText();
    if (sel && sel.trim()) return sel;
  } catch {
    // no selection / unsupported app — fall back to the clipboard
  }
  return (await Clipboard.readText()) ?? "";
}

// ---- Recording state, shared between the dictate and menu-bar commands -----

export interface RecState {
  pid: number;
  wav: string;
  startedAt: number;
  meter?: string; // file sox's -S VU meter is written to (for the live level bar)
}

function recStateFile(): string {
  return join(tmpdir(), "alfred_raycast_dictate.json");
}

export function readRecState(): RecState | null {
  const f = recStateFile();
  if (!existsSync(f)) return null;
  try {
    return JSON.parse(readFileSync(f, "utf8")) as RecState;
  } catch {
    return null;
  }
}

export function writeRecState(s: RecState): void {
  writeFileSync(recStateFile(), JSON.stringify(s));
}

export function clearRecState(): void {
  try {
    unlinkSync(recStateFile());
  } catch {
    // already gone
  }
}

export function isAlive(pid: number): boolean {
  if (!pid) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export function fileSize(p: string): number {
  try {
    return statSync(p).size;
  } catch {
    return 0;
  }
}
