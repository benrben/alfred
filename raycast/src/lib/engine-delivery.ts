/**
 * Alfred engine client for Raycast — parsing the engine's machine-readable
 * VB_STATUS/VB_RESULT lines and resolving what a capture actually delivered.
 *
 * Split out of engine.ts (see engine.ts's own comment).
 */
import { Clipboard } from "@raycast/api";
import { existsSync, readFileSync } from "node:fs";
import type { EngineResult } from "./engine-env";
import { currentContract, type ContractStatusLine } from "./engine-contract";

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

/** The result 'kind' from the status line's first field, else a code-based
 * guess when no status line is present at all. */
function deliveryKind(parts: string[] | null, code: number): string {
  return parts?.[0] ?? (code === 0 ? "unknown" : "error");
}

/** Whether a status-line flag suffix (e.g. "llm_failed") is present among the
 * parsed status parts. */
function hasStatusFlag(parts: string[] | null, suffix: string): boolean {
  return !!parts && parts.includes(suffix);
}

/** llm_failed / paste_failed trailing flags — both are trailing flags on the
 * status line; scan for either (llm_failed is last when present, paste_failed
 * may precede it) — using the contract's (or the literal fallback) names. */
function deliveryFlags(
  parts: string[] | null,
  sl: ContractStatusLine | undefined,
): { llmFailed: boolean; pasteFailed: boolean } {
  const llmSuffix = sl?.llm_failed_suffix || "llm_failed";
  const pasteSuffix = sl?.paste_failed_suffix || "paste_failed";
  return {
    llmFailed: hasStatusFlag(parts, llmSuffix),
    pasteFailed: hasStatusFlag(parts, pasteSuffix),
  };
}

/** Text for a 'copied' delivery: the VB_RESULT payload when present, else a
 * best-effort clipboard read (never rejects to "" when the clipboard is
 * empty). */
async function copiedText(resultText: string | null): Promise<string> {
  return resultText ?? (await Clipboard.readText()) ?? "";
}

/** Text for a 'saved' delivery: the VB_RESULT payload when present, else the
 * file at `path` when it exists (a read failure is silently treated as "no
 * text", matching the original ignored catch). */
function readSavedText(
  path: string | undefined,
  resultText: string | null,
): string | undefined {
  if (resultText !== null) return resultText;
  if (path && existsSync(path)) {
    try {
      return readFileSync(path, "utf8");
    } catch {
      // ignore
    }
  }
  return undefined;
}

export async function resolveDelivery(
  res: EngineResult,
): Promise<DeliveredResult> {
  const parts = parseStatus(res.out);
  const kind = deliveryKind(parts, res.code);
  const sl = currentContract().status_line;
  const { llmFailed, pasteFailed } = deliveryFlags(parts, sl);
  // Prefer the exact delivered text the engine emitted (VB_RESULT) over racing
  // the clipboard / reading the saved file.
  const resultText = parseResult(res.out);
  if (kind === "copied") {
    const text = await copiedText(resultText);
    return { kind, text, llmFailed, pasteFailed };
  }
  if (kind === "saved") {
    const path = parts?.[1];
    const text = readSavedText(path, resultText);
    return { kind, path, text, llmFailed, pasteFailed };
  }
  return { kind, llmFailed, pasteFailed };
}

/** Last non-empty stderr line, for surfacing engine errors. */
export function lastErrorLine(err: string): string {
  const lines = (err || "").split(/\r?\n/).filter((l) => l.trim());
  return lines.length ? lines[lines.length - 1].trim() : "unknown error";
}
