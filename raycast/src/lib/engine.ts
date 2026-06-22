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
import { homedir, tmpdir } from "node:os";
import { dirname, join } from "node:path";

export interface Preferences {
  daemonPort: string;
  backend: string; // default | auto | claude | codex
  defaultMode: string; // email|message|… or "" (use config)
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
  return { ...process.env, PATH: enrichedPath(), HOME: homedir() };
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

export interface CommonOverrides {
  mode?: string;
  backend?: string;
  translate?: string;
}

/** Per-run flags from preferences (+ optional per-command overrides). */
export function commonFlags(o: CommonOverrides = {}): string[] {
  const prefs = getPrefs();
  const flags: string[] = [];
  const backend = (o.backend ?? prefs.backend ?? "default").trim();
  if (backend && backend !== "default") flags.push("--backend", backend);
  const mode = (o.mode ?? prefs.defaultMode ?? "").trim();
  if (mode) flags.push("--mode", mode); // --mode <x> also enables --rewrite for x != raw
  const translate = (o.translate ?? prefs.translate ?? "default").trim();
  if (translate === "on") flags.push("--translate");
  else if (translate === "off") flags.push("--no-translate");
  return flags;
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
