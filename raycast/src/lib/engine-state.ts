/**
 * Alfred engine client for Raycast — history, live progress/stream state the
 * engine writes during a capture, the dictate command's own recording state,
 * and small process helpers (isAlive/fileSize).
 *
 * Split out of engine.ts (see engine.ts's own comment).
 */
import {
  Clipboard,
  getSelectedText,
  LaunchType,
  launchCommand,
} from "@raycast/api";
import {
  existsSync,
  readFileSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { FormatChoice } from "./engine-modes";
import { currentContract, resolvedPath } from "./engine-contract";

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
      if (rec && typeof rec.text === "string") items.push(rec);
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

/** Type-guard: has the shape readProgress needs (a parsed-but-untyped blob may
 * be mid-write and missing fields). */
function isProgress(p: Progress): boolean {
  return !!p && typeof p.label === "string" && typeof p.ts === "number";
}

/** The engine's current pipeline progress, or null if none/unreadable. */
export function readProgress(): Progress | null {
  const f = progressFile();
  if (!existsSync(f)) return null;
  try {
    const p = JSON.parse(readFileSync(f, "utf8")) as Progress;
    if (!isProgress(p)) return null;
    if (!Array.isArray(p.steps)) p.steps = [];
    return p;
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
  // Per-recording overrides. Optional for compatibility with older state files.
  backend?: string;
  translate?: string;
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
