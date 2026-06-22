import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  closeMainWindow,
  openExtensionPreferences,
  popToRoot,
} from "@raycast/api";
import { spawn } from "node:child_process";
import { closeSync, existsSync, fstatSync, openSync, readSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { useEffect, useRef, useState } from "react";
import {
  callEngine,
  clearRecState,
  commonFlags,
  DeliveredResult,
  engineEnv,
  expandHome,
  fileSize,
  getPrefs,
  isAlive,
  lastErrorLine,
  readRecState,
  RecState,
  resolveDelivery,
  writeRecState,
} from "./lib/engine";

type Phase = "recording" | "transcribing" | "done" | "error";

function fmtTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

// Bounded tail read so a long recording's meter file stays cheap to poll.
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
// Turn its "fill" into a 0..1 level, like the Hammerspoon front-end does.
function readLevel(meterFile?: string): number {
  if (!meterFile) return 0;
  const data = tailFile(meterFile);
  if (!data) return 0;
  const segs = data.split(/[\r\n]+/);
  for (let i = segs.length - 1; i >= 0 && i > segs.length - 8; i--) {
    const m = segs[i].match(/\[([^[\]]*\|[^[\]]*)\]/);
    if (m) {
      const meter = m[1];
      let fill = 0;
      let total = 0;
      for (const ch of meter) {
        total++;
        if (ch !== " " && ch !== "|") fill++;
      }
      if (total > 0) return Math.min(1, fill / total);
    }
  }
  return 0;
}

function levelBar(level: number, width = 22): string {
  const filled = Math.max(0, Math.min(width, Math.round(level * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

function waitForExit(pid: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (!isAlive(pid) || Date.now() - start > timeoutMs) {
        setTimeout(resolve, 150); // small grace so the WAV is flushed
        return;
      }
      setTimeout(tick, 100);
    };
    tick();
  });
}

export default function Dictate() {
  const [phase, setPhase] = useState<Phase>("recording");
  const [, setTick] = useState(0); // forces re-render for the live timer/meter
  const [result, setResult] = useState<DeliveredResult | null>(null);
  const [error, setError] = useState<string>("");
  const stateRef = useRef<RecState | null>(null);

  // Start (or adopt) a recording on mount.
  useEffect(() => {
    const existing = readRecState();
    if (existing && isAlive(existing.pid)) {
      stateRef.current = existing; // re-adopt a recording already in progress
      setPhase("recording");
    } else {
      if (existing) clearRecState();
      startRecording();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Tick the timer/meter while recording.
  useEffect(() => {
    if (phase !== "recording") return;
    const id = setInterval(() => setTick((t) => t + 1), 200);
    return () => clearInterval(id);
  }, [phase]);

  function startRecording() {
    const prefs = getPrefs();
    const sox = expandHome(prefs.soxBin);
    if (!existsSync(sox)) {
      setError(
        `sox not found at ${sox}. Install it (brew install sox) or fix the path in preferences.`,
      );
      setPhase("error");
      return;
    }
    const stamp = Date.now();
    const wav = join(tmpdir(), `alfred_rec_${stamp}.wav`);
    const meter = join(tmpdir(), `alfred_rec_${stamp}.meter`);
    try {
      const fd = openSync(meter, "w");
      const child = spawn(
        sox,
        ["-d", "-S", "-r", "16000", "-c", "1", "-b", "16", wav],
        {
          detached: true,
          stdio: ["ignore", "ignore", fd],
          env: engineEnv(),
        },
      );
      child.unref();
      closeSync(fd);
      if (!child.pid) throw new Error("recorder did not start");
      stateRef.current = { pid: child.pid, wav, meter, startedAt: stamp };
      writeRecState(stateRef.current);
      setPhase("recording");
    } catch (e) {
      setError(`Could not start the recorder: ${String(e)}`);
      setPhase("error");
    }
  }

  async function stopAndTranscribe() {
    const st = stateRef.current;
    if (!st) return;
    setPhase("transcribing");
    try {
      process.kill(st.pid, "SIGINT"); // sox finalizes the WAV on SIGINT
    } catch {
      // already gone
    }
    await waitForExit(st.pid, 4000);
    clearRecState();
    if (fileSize(st.wav) <= 1024) {
      setError("Nothing recorded.");
      setPhase("error");
      return;
    }
    const res = await callEngine(["process", st.wav, ...commonFlags()]);
    const delivered = await resolveDelivery(res);
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      setResult(delivered);
      setPhase("done");
    } else if (delivered.kind === "empty") {
      setError("No speech detected.");
      setPhase("error");
    } else {
      setError(lastErrorLine(res.err));
      setPhase("error");
    }
  }

  function cancel() {
    const st = stateRef.current;
    if (st) {
      try {
        process.kill(st.pid, "SIGKILL");
      } catch {
        // already gone
      }
      clearRecState();
    }
    closeMainWindow();
    popToRoot();
  }

  function dictateAgain() {
    setResult(null);
    setError("");
    startRecording();
  }

  // ---- render -------------------------------------------------------------
  if (phase === "recording") {
    const st = stateRef.current;
    const elapsed = st ? Math.floor((Date.now() - st.startedAt) / 1000) : 0;
    const level = readLevel(st?.meter);
    const md = [
      "# 🔴 Recording",
      "",
      `## ${fmtTime(elapsed)}`,
      "",
      `\`${levelBar(level)}\``,
      "",
      "Speak now — **⏎** stop & transcribe · **⌃C** cancel · **Esc** keeps recording (reopen Dictate to stop).",
    ].join("\n");
    return (
      <Detail
        markdown={md}
        navigationTitle={`🔴 Recording — ${fmtTime(elapsed)}`}
        actions={
          <ActionPanel>
            <Action
              title="Stop & Transcribe"
              icon={Icon.Stop}
              onAction={stopAndTranscribe}
            />
            <Action
              title="Cancel"
              icon={Icon.XMarkCircle}
              shortcut={{ modifiers: ["ctrl"], key: "c" }}
              onAction={cancel}
            />
          </ActionPanel>
        }
      />
    );
  }

  if (phase === "transcribing") {
    return (
      <Detail
        isLoading
        navigationTitle="Transcribing…"
        markdown={
          "# ⏳ Transcribing…\n\nRunning speech-to-text" +
          (commonFlags().length ? " + cleanup" : "") +
          " through the Alfred engine."
        }
      />
    );
  }

  if (phase === "error") {
    return (
      <Detail
        navigationTitle="Dictation error"
        markdown={`# ⚠️ ${error}`}
        actions={
          <ActionPanel>
            <Action
              title="Dictate Again"
              icon={Icon.Microphone}
              onAction={dictateAgain}
            />
            <Action
              title="Open Preferences"
              icon={Icon.Gear}
              onAction={openExtensionPreferences}
            />
          </ActionPanel>
        }
      />
    );
  }

  // done
  const text = result?.text ?? "";
  const header = result?.llmFailed
    ? "> ⚠️ LLM step failed — raw transcript below.\n\n"
    : result?.kind === "saved"
      ? `> 💾 Saved to \`${result?.path}\`\n\n`
      : "";
  return (
    <Detail
      navigationTitle="Transcript"
      markdown={`${header}${text || "_(empty)_"}`}
      actions={
        <ActionPanel>
          <Action.Paste title="Paste to Frontmost App" content={text} />
          <Action.CopyToClipboard
            title="Copy"
            content={text}
            shortcut={{ modifiers: ["cmd"], key: "c" }}
          />
          <Action
            title="Dictate Again"
            icon={Icon.Microphone}
            onAction={dictateAgain}
          />
        </ActionPanel>
      }
    />
  );
}
