/**
 * Alfred's Dictate command — the non-JSX recording lifecycle: reading the
 * live meter/WAV files, abandoning a stale recorder, adopting or starting a
 * capture, and the small pure helpers those need.
 *
 * Split out of dictate.tsx (which stayed over the repository's 600-line File
 * LOC gate) alongside dictate-views.tsx (the JSX render helpers). dictate.tsx
 * itself keeps the Dictate component and everything that needs its state.
 */
import { closeSync, fstatSync, openSync, readSync, unlinkSync } from "node:fs";
import {
  buildFormats,
  callEngine,
  clearRecState,
  CONFIG_FORMAT_ID,
  FormatChoice,
  isAlive,
  loadModes,
  normalizeBackend,
  normalizeTranslate,
  getPrefs,
  pingDaemon,
  readRecState,
  RecState,
  refreshMenuBar,
} from "./engine";
import { parseLevel } from "./view-logic";

export type Phase = "recording" | "transcribing" | "done" | "error";

// Named aliases for the callback props below. An inline `prop: (args) => T`
// member inside an object-type parameter mis-scopes this repo's static
// complexity tool; a named alias for the callback avoids that.
export type VoidCallback = () => void;
type StopCallback = () => void | Promise<void>;
export type StringSetter = (value: string) => void;
type PhaseSetter = (phase: Phase) => void;
export type FormatsSetter = (formats: FormatChoice[]) => void;
export type FormatChooser = (fmt: FormatChoice) => void;

// Shared shape for adoptRecording/bootstrapRecording's setter/callback bag.
export interface RecordingCallbacks {
  launchContext?: { stop?: boolean };
  stateRef: { current: RecState | null };
  setBackend: StringSetter;
  setFormatId: StringSetter;
  setPhase: PhaseSetter;
  stopAndTranscribe: StopCallback;
}

function tailFile(file: string, bytes = 8192): string {
  try {
    const fd = openSync(file, "r");
    try {
      const size = fstatSync(fd).size;
      const start = Math.max(0, size - bytes);
      const len = size - start;
      if (len <= 0) return "";
      const buf = Buffer.alloc(len);
      readSync(fd, buf, 0, len, start);
      return buf.toString("utf8");
    } finally {
      closeSync(fd);
    }
  } catch {
    return "";
  }
}

// sox -S writes a VU meter to stderr as a bracketed segment containing a '|'.
export function readLevel(meterFile?: string): number {
  if (!meterFile) return 0;
  return parseLevel(tailFile(meterFile));
}

// Best-effort delete of a recording-session file. Missing/already-gone is
// not an error.
export function removeIfPresent(path?: string): void {
  if (!path) return;
  try {
    unlinkSync(path);
  } catch {
    // already gone / never existed
  }
}

function waitForExit(pid: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (!isAlive(pid) || Date.now() - start > timeoutMs) {
        setTimeout(resolve, 150);
        return;
      }
      setTimeout(tick, 100);
    };
    tick();
  });
}

// Defensively stop a recorder still tracked in RecState (a confused prior
// session) so it can't orphan a `sox` that keeps holding the mic. SIGINT lets
// a still-alive one finalize its WAV; abandoned outright, so we must delete
// its files ourselves (no stream-finish will ever consume them).
export function abandonStaleRecording(stale: RecState | null): void {
  if (!stale) return;
  if (isAlive(stale.pid)) {
    try {
      process.kill(stale.pid, "SIGINT");
    } catch {
      // already gone
    }
  }
  removeIfPresent(stale.wav);
  removeIfPresent(stale.meter);
}

// Start transcribing the growing WAV in the warm daemon so most of it is done
// by the time we stop. Best-effort; on stop, stream-finish falls back to batch.
export async function beginStreamTranscription(
  wav: string,
  flags: string[],
): Promise<void> {
  try {
    if (await pingDaemon()) await callEngine(["stream-start", wav, ...flags]);
  } catch {
    // streaming unavailable — batch on stop
  }
}

// Stop the recorder (SIGINT, so it finalizes its WAV), wait for it to exit,
// then retire RecState/the menu-bar indicator/the .meter file. The WAV is
// NOT removed here — it's about to be handed to stream-finish.
export async function finalizeCaptureState(st: RecState): Promise<void> {
  try {
    process.kill(st.pid, "SIGINT");
  } catch {
    // already gone
  }
  await waitForExit(st.pid, 4000);
  clearRecState();
  refreshMenuBar(); // clear the 🔴 indicator immediately
  removeIfPresent(st.meter);
}

/** The backend/translate overrides to resume a persisted recording with:
 * its own saved choices, else the current preferences. */
function resolveSavedOverrides(persisted: RecState): {
  backend: string;
  translate: string;
} {
  return {
    backend: normalizeBackend(persisted.backend ?? getPrefs().backend),
    translate: normalizeTranslate(persisted.translate ?? getPrefs().translate),
  };
}

// Adopt an already-running recording (reopen, or a menu-bar-triggered stop):
// restore its backend/translate/format overrides, then either act on the
// pending stop or leave it recording. currentFormat() falls back to the
// persisted format, so a menu-bar-triggered stop uses the right flags even
// before the mode catalog (re)loads.
function adoptRecording(
  args: RecordingCallbacks & { persisted: RecState },
): void {
  const { backend: savedBackend, translate: savedTranslate } =
    resolveSavedOverrides(args.persisted);
  args.setBackend(savedBackend);
  args.stateRef.current = {
    ...args.persisted,
    backend: savedBackend,
    translate: savedTranslate,
  };
  if (args.persisted.format) args.setFormatId(args.persisted.format.id);
  args.setPhase("recording");
  if (args.launchContext?.stop) void args.stopAndTranscribe();
}

// No live recording to adopt: discard any leftover (dead) state and start a
// fresh one (mic on immediately; the format list loads in parallel).
function startFreshCapture(
  existing: RecState | null,
  startRecording: VoidCallback,
): void {
  if (existing) clearRecState();
  startRecording();
}

// Load the format list and start/adopt a recording immediately. The default
// stays "Default (config)" so we never contradict the user's config.
export async function bootstrapRecording(
  args: RecordingCallbacks & {
    setFormats: FormatsSetter;
    startRecording: VoidCallback;
  },
): Promise<void> {
  const modesPromise = loadModes();
  const existing = readRecState();
  if (existing && isAlive(existing.pid)) {
    adoptRecording({ ...args, persisted: existing });
  } else {
    startFreshCapture(existing, args.startRecording);
  }
  args.setFormats(buildFormats(await modesPromise));
}

/** Seconds elapsed since a recording started, or 0 before one exists. */
export function elapsedSeconds(st: RecState | null): number {
  if (!st) return 0;
  return Math.floor((Date.now() - st.startedAt) / 1000);
}

/** The format id to start on: the forced format's (Transcribe Only), else
 * "use config". */
export function initialFormatId(forceFormat: FormatChoice | undefined): string {
  if (forceFormat) return forceFormat.id;
  return CONFIG_FORMAT_ID;
}
