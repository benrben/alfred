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
import {
  Clipboard,
  getPreferenceValues,
  getSelectedText,
  LaunchType,
  launchCommand,
} from "@raycast/api";
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

export interface DaemonIdentity {
  ok: boolean;
  app: string;
  schema_version: number;
  pid: number;
}

const DAEMON_TIMEOUT_MS = 120_000;

// ---- Engine CONTRACT -------------------------------------------------------
// The engine describes its own wire shape (file paths, daemon coords, status
// line) via `voicebridge.py contract` (and GET /contract on the warm daemon).
// We consume it so the file paths/port aren't hard-coded here — with a literal
// fallback to the historical values so an older engine still works.

export interface ContractStatusLine {
  sentinel: string;
  sep: string;
  kinds?: Record<string, string[]>;
  llm_failed_suffix?: string;
  paste_failed_suffix?: string;
  /** Sentinel for the optional machine-readable result line (VB_RESULT). */
  result_sentinel?: string;
}

export interface ContractFile {
  path: string;
  schema?: Record<string, unknown>;
}

export interface ContractAudio {
  rate: number;
  channels: number;
  bits: number;
  format: string;
  sox_args: string[];
}

/** Absolute, config-aware paths the engine resolves for us (honours
 * [history].dir). Present on newer engines; absent on older ones. */
export interface ContractResolved {
  progress?: string;
  stream?: string;
  history?: string;
  daemon_info?: string;
  config?: string;
}

export interface Contract {
  schema_version: number;
  daemon: { host: string; port: number; url?: string; [k: string]: unknown };
  status_line: ContractStatusLine;
  audio?: ContractAudio;
  files: {
    progress: ContractFile;
    stream: ContractFile;
    history: ContractFile;
    [k: string]: ContractFile;
  };
  resolved?: ContractResolved;
  config_search?: string[];
}

/** The literal contract baked in here, used when the engine can't supply one
 * (older engine, daemon down + spawn failure). Mirrors today's hard-coded
 * paths/port so behaviour is unchanged on the fallback path. */
export function fallbackContract(): Contract {
  return {
    schema_version: 1,
    daemon: { host: "127.0.0.1", port: 8763, url: "http://127.0.0.1:8763/" },
    status_line: {
      sentinel: "VB_STATUS",
      sep: "\t",
      kinds: {
        copied: [],
        saved: ["path"],
        empty: [],
        streaming: [],
        error: ["subtype"],
      },
      llm_failed_suffix: "llm_failed",
      paste_failed_suffix: "paste_failed",
      result_sentinel: "VB_RESULT",
    },
    audio: {
      rate: 16000,
      channels: 1,
      bits: 16,
      format: "wav",
      sox_args: ["-d", "-S", "-r", "16000", "-c", "1", "-b", "16"],
    },
    files: {
      progress: { path: "~/.voicebridge/progress.json" },
      stream: { path: "~/.voicebridge/stream.json" },
      history: { path: "~/.voicebridge/history/history.jsonl" },
    },
    config_search: ["~/.config/voicebridge/config.toml"],
  };
}

/** Resolve a contract file path to an absolute path, expanding a leading ~. */
export function contractPath(contract: Contract, key: string): string {
  const raw = contract.files?.[key]?.path;
  if (!raw) return expandHome(fallbackContract().files[key]?.path ?? "");
  return expandHome(raw);
}

/** Absolute path for a state file: prefer the engine's `resolved` block (which
 * honours [history].dir) when present, else derive from the file templates.
 * Backward compatible — older engines have no `resolved` block. */
export function resolvedPath(
  contract: Contract,
  key: "progress" | "stream" | "history",
): string {
  const abs = contract.resolved?.[key];
  if (abs) return expandHome(abs);
  return contractPath(contract, key);
}

/** The recorder command args (int16 mono 16 kHz WAV) from the contract's
 * `audio.sox_args`, with the output WAV appended. Falls back to the literal
 * invariant when an older engine omits `audio`. */
// Hard cap on a single recording (seconds). A long note/meeting dictation can
// run many minutes, so this is generous (60 min); the cap only exists so a
// recording that's dismissed and never stopped (the "Esc keeps recording" flow)
// can't run forever and fill the disk / hold the mic. Applied as a sox
// `trim 0 <secs>` output effect. Mirrors the Hammerspoon front-end's cap.
export const MAX_RECORD_SECS = 3600;

export function recorderArgs(wav: string): string[] {
  const a = currentContract().audio?.sox_args;
  const base =
    Array.isArray(a) && a.length
      ? a
      : ["-d", "-S", "-r", "16000", "-c", "1", "-b", "16"];
  return [...base, wav, "trim", "0", String(MAX_RECORD_SECS)];
}

/**
 * Warn when the engine's contract schema_version differs from the one this
 * extension was built against — any difference is a backward-incompatible bump,
 * so the two are out of sync and the extension should be rebuilt. Returns null
 * when the versions match or either is missing.
 */
export function schemaMismatchWarning(
  local: number,
  engine: number,
): string | null {
  if (!local || !engine || local === engine) return null;
  return `⚠️ Engine schema v${engine} but this extension expects v${local} — they're out of sync. Rebuild/update the Alfred Raycast extension.`;
}

// The mismatch warning computed once loadContract() has the real contract.
let schemaWarning: string | null = null;
export function contractSchemaWarning(): string | null {
  return schemaWarning;
}

// Cached contract: undefined = not yet fetched; null = fetched and failed (so
// we stop re-spawning and use the literal fallback for the rest of the run).
let cachedContract: Contract | undefined;
let contractInFlight: Promise<Contract> | undefined;

function parseContract(out: string): Contract | null {
  try {
    const c = JSON.parse(out) as Contract;
    if (c && c.files && c.daemon && c.status_line) return c;
  } catch {
    // not JSON / malformed
  }
  return null;
}

/** Fetch the engine's contract once and cache it. Prefers GET /contract on the
 * warm daemon; falls back to the one-shot `contract` CLI. Returns the literal
 * fallbackContract() if neither works (older engine), so callers never throw. */
export async function loadContract(): Promise<Contract> {
  if (cachedContract) return cachedContract;
  if (contractInFlight) return contractInFlight;
  contractInFlight = (async () => {
    let contract: Contract | null = null;
    // 1) warm daemon, if up
    try {
      const res = await fetch(daemonUrl("/contract"), {
        signal: AbortSignal.timeout(2000),
      });
      if (res.ok) contract = parseContract(await res.text());
    } catch {
      // daemon down or no /contract route — try the one-shot CLI
    }
    // 2) one-shot CLI `contract`
    if (!contract) {
      const one = await runOneShot(["contract"]);
      contract = one.code === 0 ? parseContract(one.out) : null;
    }
    cachedContract = contract ?? fallbackContract();
    // Compare the engine's schema_version against the one we were built with,
    // here (before the promise resolves) so EVERY awaiter observes it set. Equal
    // — or the literal fallback — yields no warning.
    schemaWarning = schemaMismatchWarning(
      fallbackContract().schema_version,
      cachedContract.schema_version,
    );
    return cachedContract;
  })();
  try {
    return await contractInFlight;
  } finally {
    contractInFlight = undefined;
  }
}

/** The cached contract if already loaded, else the literal fallback. Lets the
 * synchronous *File()/daemonPort() helpers derive paths without awaiting; a
 * background loadContract() warms the cache so later polls use the real one. */
function currentContract(): Contract {
  return cachedContract ?? fallbackContract();
}

export function daemonPort(): string {
  const pref = (getPrefs().daemonPort || "").trim();
  if (pref) return pref;
  return String(currentContract().daemon.port || 8763);
}

/** The single source of truth for the daemon's base URL (built on daemonPort()),
 * so callEngine/pingDaemon/loadContract don't each re-derive "127.0.0.1:8763". */
export function daemonUrl(path = "/"): string {
  return `http://127.0.0.1:${daemonPort()}${path}`;
}

/** Accept only the JSON identity emitted by Alfred's daemon. */
export function isAlfredIdentity(value: unknown): value is DaemonIdentity {
  if (!value || typeof value !== "object") return false;
  const identity = value as Partial<DaemonIdentity>;
  return (
    identity.ok === true &&
    identity.app === "alfred" &&
    identity.schema_version === currentContract().schema_version &&
    Number.isInteger(identity.pid) &&
    (identity.pid ?? 0) > 0
  );
}

/** Quick health check of the warm daemon (GET /). */
export async function pingDaemon(): Promise<boolean> {
  try {
    const res = await fetch(daemonUrl("/"), {
      signal: AbortSignal.timeout(2000),
    });
    if (!res.ok) return false;
    return isAlfredIdentity(await res.json());
  } catch {
    return false;
  }
}

export async function callEngine(argv: string[]): Promise<EngineResult> {
  // Warm the contract cache opportunistically (fire-and-forget) so the
  // synchronous *File()/daemonPort() helpers derive real paths on later polls.
  // Never blocks the call and never throws (loadContract resolves to the
  // literal fallback on any error).
  if (cachedContract === undefined) void loadContract();
  // Complete the identity handshake before sending argv/text. A foreign
  // localhost service must not receive dictated content just because it returns
  // HTTP 200.
  if (!(await pingDaemon())) {
    startDaemon(); // bring it up for next time
    return runOneShot(argv);
  }
  const signal = AbortSignal.timeout(DAEMON_TIMEOUT_MS);
  try {
    const res = await fetch(daemonUrl("/"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ argv }),
      signal,
    });
    if (res.ok) {
      const data = (await res.json()) as {
        code?: number;
        out?: string;
        err?: string;
        identity?: unknown;
      };
      if (!isAlfredIdentity(data.identity)) {
        return {
          code: 1,
          out: "",
          err: "daemon response was not from the Alfred engine; request was not replayed",
        };
      }
      // The daemon now returns the request's captured stderr in `err`; older
      // daemons omit it (`?? ""`), so every error toast stays informative.
      return { code: data.code ?? 0, out: data.out ?? "", err: data.err ?? "" };
    }
    return {
      code: 1,
      out: "",
      err: `daemon returned HTTP ${res.status}; request was not replayed`,
    };
  } catch {
    // A timeout aborts only the client fetch; the daemon may still complete the
    // LLM and delivery. Replaying here would duplicate cost/history/paste.
    if (signal.aborted) {
      return {
        code: 1,
        out: "",
        err: "daemon request timed out; it may still be running and was not replayed",
      };
    }
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
  const script = resolveScript();
  const py = resolvePython(script);
  const port = daemonPort();
  try {
    const child = spawn(py, [script, "serve", "--port", port], {
      detached: true,
      stdio: "ignore",
      env: engineEnv(),
    });
    child.on("error", () => {
      // Detached bootstrap is best-effort; the foreground request has its own
      // one-shot fallback and must not crash on an asynchronous ENOENT.
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

/** The "pure transcript" format — every LLM stage forced off. Static (no modes
 * needed), so the dedicated Transcribe Only command can pin it synchronously
 * before the mode catalog loads. Keeps the three --no-* flags (not the newer
 * --transcribe-only) so it still works against an older engine. */
export function rawFormat(): FormatChoice {
  return {
    id: RAW_FORMAT_ID,
    title: "Raw transcript",
    subtitle: "No AI — exactly what you said",
    ai: false,
    flags: ["--no-rewrite", "--no-translate", "--no-optimize"],
  };
}

export function buildFormats(modes: Mode[]): FormatChoice[] {
  const list: FormatChoice[] = [configFormat(), rawFormat()];
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

/** The selectable LLM backends (besides "Default = use config"). ONE source
 * feeding every picker (PipelineForm's dropdown here; the Hammerspoon
 * front-end has its own equivalent list) so adding/renaming a backend can't
 * silently drift out of sync with the engine's `--backend` choices again —
 * "local" (the engine's actual default: on-device MLX, no login, no network)
 * had dropped out of this form's dropdown before. */
export const BACKENDS = ["auto", "claude", "codex", "local"] as const;

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

/** Parse the engine's machine-readable last line: "VB_STATUS\tkind[\textra…]".
 * Sentinel/separator come from the contract's status_line (literal fallback). */
export function parseStatus(out: string): string[] | null {
  const sl = currentContract().status_line;
  const sentinel = sl?.sentinel || "VB_STATUS";
  const sep = sl?.sep || "\t";
  const prefix = sentinel + sep;
  for (const line of out.split(/\r?\n/)) {
    if (line.startsWith(prefix)) return line.slice(prefix.length).split(sep);
  }
  return null;
}

/** Parse the engine's machine-readable result line "VB_RESULT<sep><json text>",
 * emitted just BEFORE the final VB_STATUS. Returns the decoded text, or null if
 * absent (older engine / the --stdout path). Preferred over the clipboard. */
export function parseResult(out: string): string | null {
  const sl = currentContract().status_line;
  const sentinel = sl?.result_sentinel || "VB_RESULT";
  const sep = sl?.sep || "\t";
  const prefix = sentinel + sep;
  for (const line of out.split(/\r?\n/)) {
    if (line.startsWith(prefix)) {
      try {
        const text = JSON.parse(line.slice(prefix.length));
        return typeof text === "string" ? text : null;
      } catch {
        return null;
      }
    }
  }
  return null;
}

export interface DeliveredResult {
  kind: string; // copied | saved | empty | error | unknown
  text?: string;
  path?: string;
  llmFailed: boolean;
  pasteFailed: boolean; // auto-paste was requested but the keystroke didn't land
}

export async function resolveDelivery(
  res: EngineResult,
): Promise<DeliveredResult> {
  const parts = parseStatus(res.out);
  const kind = parts?.[0] ?? (res.code === 0 ? "unknown" : "error");
  const sl = currentContract().status_line;
  const llmSuffix = sl?.llm_failed_suffix || "llm_failed";
  const pasteSuffix = sl?.paste_failed_suffix || "paste_failed";
  // Both are trailing flags on the status line; scan for either (llm_failed is
  // last when present, paste_failed may precede it).
  const llmFailed = !!parts && parts.includes(llmSuffix);
  const pasteFailed = !!parts && parts.includes(pasteSuffix);
  // Prefer the exact delivered text the engine emitted (VB_RESULT) over racing
  // the clipboard / reading the saved file.
  const resultText = parseResult(res.out);
  if (kind === "copied") {
    const text = resultText ?? (await Clipboard.readText()) ?? "";
    return { kind, text, llmFailed, pasteFailed };
  }
  if (kind === "saved") {
    const path = parts?.[1];
    let text: string | undefined = resultText ?? undefined;
    if (text === undefined && path && existsSync(path)) {
      try {
        text = readFileSync(path, "utf8");
      } catch {
        // ignore
      }
    }
    if (!path && text === undefined) {
      return { kind: "error", llmFailed, pasteFailed };
    }
    return { kind, path, text, llmFailed, pasteFailed };
  }
  return { kind, llmFailed, pasteFailed };
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
  return resolvedPath(currentContract(), "history");
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
      if (rec && typeof rec.text === "string" && typeof rec.ts === "string")
        items.push(rec);
    } catch {
      // skip malformed line
    }
  }
  return items.reverse().slice(0, limit);
}

// ---- Live progress (per-step stopwatch the engine writes during a capture) --

export interface ProgressStep {
  label: string;
  ms: number; // duration of a COMPLETED step
}
export interface Progress {
  phase: string; // starting | transcribing | processing | delivering | done | error | empty
  label: string; // human label of the CURRENT step
  ts: number; // epoch ms the current step started (for a live stopwatch)
  start: number; // epoch ms the capture's processing started (for the total)
  steps: ProgressStep[]; // completed steps, in order
}

export function progressFile(): string {
  return resolvedPath(currentContract(), "progress");
}

/** The engine's current pipeline progress, or null if none/unreadable. */
export function readProgress(): Progress | null {
  const f = progressFile();
  if (!existsSync(f)) return null;
  try {
    const p = JSON.parse(readFileSync(f, "utf8")) as Progress;
    if (p && typeof p.label === "string" && typeof p.ts === "number") {
      if (!Array.isArray(p.steps)) p.steps = [];
      return p;
    }
  } catch {
    // ignore malformed / mid-write
  }
  return null;
}

// ---- Live streaming transcript (engine transcribes the WAV while recording) --

export interface StreamState {
  transcript: string;
  recording: boolean;
  done: boolean;
  ts: number; // epoch ms of the last write
}

export function streamFile(): string {
  return resolvedPath(currentContract(), "stream");
}

/** The engine's live partial transcript during a streamed recording, or null. */
export function readStream(): StreamState | null {
  const f = streamFile();
  if (!existsSync(f)) return null;
  try {
    const s = JSON.parse(readFileSync(f, "utf8")) as StreamState;
    if (s && typeof s.transcript === "string" && typeof s.ts === "number")
      return s;
  } catch {
    // ignore mid-write / malformed
  }
  return null;
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
  // The chosen output format at record time. Persisted so a reopen (Esc keeps
  // recording) or a menu-bar-triggered stop still honours it instead of silently
  // falling back to the config default with empty flags.
  format?: FormatChoice;
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

/** Nudge the menu-bar command to re-read the recording state right away. Its own
 * poll interval is up to a minute, so its 🔴 indicator would otherwise be stale
 * after a start/stop/cancel. Best-effort — never throws. */
export function refreshMenuBar(): void {
  void launchCommand({ name: "menubar", type: LaunchType.Background }).catch(
    () => {},
  );
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
