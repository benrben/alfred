/**
 * Alfred engine client for Raycast — the engine's IPC contract (fetched once,
 * cached, with a literal fallback for an older engine) and calling the engine
 * itself, preferring the warm daemon over spawning a one-shot process.
 *
 * Split out of engine.ts (see engine.ts's own comment).
 */
import { spawn } from "node:child_process";
import {
  type EngineResult,
  engineEnv,
  expandHome,
  getPrefs,
  resolvePython,
  resolveScript,
} from "./engine-env";

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
// `trim 0 <secs>` output effect.
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

/** Step 1 of contract discovery: GET /contract on the warm daemon. Returns null
 * (never throws) when the daemon is down, has no /contract route, or replies
 * with something parseContract rejects. */
async function fetchContractFromDaemon(): Promise<Contract | null> {
  try {
    const res = await fetch(daemonUrl("/contract"), {
      signal: AbortSignal.timeout(2000),
    });
    if (res.ok) return parseContract(await res.text());
  } catch {
    // daemon down or no /contract route — try the one-shot CLI
  }
  return null;
}

/** Step 2 of contract discovery: the one-shot `contract` CLI, tried only when
 * the daemon didn't yield one. Returns null on a nonzero exit or unparsable
 * output. */
async function fetchContractFromCli(): Promise<Contract | null> {
  const one = await runOneShot(["contract"]);
  return one.code === 0 ? parseContract(one.out) : null;
}

/** Run the two-step discovery (daemon, then CLI) and cache the result. Split
 * out of loadContract() as its own named (not inline/nested) function so the
 * orchestration in loadContract stays trivial to read. */
async function discoverContract(): Promise<Contract> {
  const contract =
    (await fetchContractFromDaemon()) ?? (await fetchContractFromCli());
  cachedContract = contract ?? fallbackContract();
  // Compare the engine's schema_version against the one we were built with,
  // here (before the promise resolves) so EVERY awaiter observes it set. Equal
  // — or the literal fallback — yields no warning.
  schemaWarning = schemaMismatchWarning(
    fallbackContract().schema_version,
    cachedContract.schema_version,
  );
  return cachedContract;
}

/** Fetch the engine's contract once and cache it. Prefers GET /contract on the
 * warm daemon; falls back to the one-shot `contract` CLI. Returns the literal
 * fallbackContract() if neither works (older engine), so callers never throw. */
export async function loadContract(): Promise<Contract> {
  if (cachedContract) return cachedContract;
  if (contractInFlight) return contractInFlight;
  contractInFlight = discoverContract();
  try {
    return await contractInFlight;
  } finally {
    contractInFlight = undefined;
  }
}

/** The cached contract if already loaded, else the literal fallback. Lets the
 * synchronous *File()/daemonPort() helpers derive paths without awaiting; a
 * background loadContract() warms the cache so later polls use the real one. */
export function currentContract(): Contract {
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

/** Quick health check of the warm daemon (GET /). */
export async function pingDaemon(): Promise<boolean> {
  try {
    const res = await fetch(daemonUrl("/"), {
      signal: AbortSignal.timeout(2000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

/** Decode the warm daemon's JSON response body into an EngineResult. The
 * daemon now returns the request's captured stderr in `err`; older daemons
 * omit it (`?? ""`), so every error toast stays informative. */
async function parseEngineResponse(res: Response): Promise<EngineResult> {
  const data = (await res.json()) as {
    code?: number;
    out?: string;
    err?: string;
  };
  return { code: data.code ?? 0, out: data.out ?? "", err: data.err ?? "" };
}

/** Try the warm daemon over HTTP. Returns null (never throws) when it's down
 * or replies with a non-ok status, so callers fall back to a one-shot spawn. */
async function callDaemon(argv: string[]): Promise<EngineResult | null> {
  try {
    const res = await fetch(daemonUrl("/"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ argv }),
      signal: AbortSignal.timeout(DAEMON_TIMEOUT_MS),
    });
    if (res.ok) return parseEngineResponse(res);
  } catch {
    // daemon unavailable — fall through to a one-shot process
  }
  return null;
}

export async function callEngine(argv: string[]): Promise<EngineResult> {
  // Warm the contract cache opportunistically (fire-and-forget) so the
  // synchronous *File()/daemonPort() helpers derive real paths on later polls.
  // Never blocks the call and never throws (loadContract resolves to the
  // literal fallback on any error).
  if (cachedContract === undefined) void loadContract();
  const daemonResult = await callDaemon(argv);
  if (daemonResult) return daemonResult;
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
    child.unref();
  } catch {
    // best effort
  }
}
