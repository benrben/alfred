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
  buildFormats,
  callEngine,
  clearRecState,
  CONFIG_FORMAT_ID,
  configFormat,
  engineEnv,
  expandHome,
  fileSize,
  flagsForFormat,
  FormatChoice,
  getPrefs,
  isAlive,
  lastErrorLine,
  loadModes,
  pingDaemon,
  Progress,
  readProgress,
  readRecState,
  readStream,
  RecState,
  DeliveredResult,
  resolveDelivery,
  writeRecState,
} from "./lib/engine";
import { ResultView } from "./lib/ResultView";
import {
  buildRecordingMarkdown,
  buildTranscribingMarkdown,
  fmtMs,
  fmtTime,
  parseLevel,
  resolveLiveTranscript,
  transcribingStatus,
} from "./lib/view-logic";

type Phase = "recording" | "transcribing" | "done" | "error";

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
function readLevel(meterFile?: string): number {
  if (!meterFile) return 0;
  return parseLevel(tailFile(meterFile));
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

export default function Dictate(props: { launchContext?: { stop?: boolean } }) {
  const [phase, setPhase] = useState<Phase>("recording");
  const [, setTick] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<DeliveredResult | null>(null);
  const [resultNote, setResultNote] = useState("");
  const [formats, setFormats] = useState<FormatChoice[]>([]);
  const [formatId, setFormatId] = useState<string>(CONFIG_FORMAT_ID);
  const [prog, setProg] = useState<Progress | null>(null);
  const stateRef = useRef<RecState | null>(null);

  // Load the format list (async) and start/adopt a recording immediately. The
  // default stays "Default (config)" so we never contradict the user's config.
  useEffect(() => {
    (async () => {
      const modes = await loadModes();
      setFormats(buildFormats(modes));
    })();

    const existing = readRecState();
    if (existing && isAlive(existing.pid)) {
      stateRef.current = existing;
      setPhase("recording");
      // Opened from the menu bar's "Stop & Transcribe" — stop immediately.
      if (props.launchContext?.stop) void stopAndTranscribe();
    } else {
      if (existing) clearRecState();
      startRecording();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (phase !== "recording") return;
    const id = setInterval(() => setTick((t) => t + 1), 200);
    return () => clearInterval(id);
  }, [phase]);

  function startRecording() {
    const sox = expandHome(getPrefs().soxBin);
    if (!existsSync(sox)) {
      setError(
        `sox not found at ${sox}. Install it (brew install sox) or set its path in preferences.`,
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
        { detached: true, stdio: ["ignore", "ignore", fd], env: engineEnv() },
      );
      child.unref();
      closeSync(fd);
      if (!child.pid) throw new Error("recorder did not start");
      stateRef.current = { pid: child.pid, wav, meter, startedAt: stamp };
      writeRecState(stateRef.current);
      setPhase("recording");
      // Start transcribing the growing WAV in the warm daemon so most of it is
      // done by the time we stop. Best-effort: only via the daemon (a one-shot
      // would exit immediately); on stop, stream-finish falls back to batch.
      void (async () => {
        try {
          if (await pingDaemon())
            await callEngine([
              "stream-start",
              wav,
              ...flagsForFormat(currentFormat()),
            ]);
        } catch {
          // streaming unavailable — batch on stop
        }
      })();
    } catch (e) {
      setError(`Could not start the recorder: ${String(e)}`);
      setPhase("error");
    }
  }

  function currentFormat(): FormatChoice {
    // Fall back to "use config" (no flags) — never to a stage-disabling raw.
    return formats.find((f) => f.id === formatId) ?? configFormat();
  }

  async function stopAndTranscribe() {
    const st = stateRef.current;
    if (!st) return;
    const fmt = currentFormat();
    setPhase("transcribing");
    setProg(null);
    try {
      process.kill(st.pid, "SIGINT");
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
    // Poll the engine's progress file for a live per-step stopwatch. Re-reading
    // every 200ms also re-renders, so the current step's timer ticks. We ignore
    // any stale file left by an earlier capture (ts older than this one).
    const procStart = Date.now();
    const poll = setInterval(() => {
      const p = readProgress();
      if (p && p.ts >= procStart - 1500) setProg(p);
    }, 200);
    let res;
    try {
      // stream-finish: the daemon already transcribed most of the WAV while we
      // recorded, so only the short tail remains. Falls back to a full batch
      // transcribe if there was no live session.
      res = await callEngine(["stream-finish", st.wav, ...flagsForFormat(fmt)]);
    } finally {
      clearInterval(poll);
    }
    const delivered = await resolveDelivery(res);
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      setResult(delivered);
      setResultNote(fmt.ai ? fmt.title : "Raw transcript");
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
    setResultNote("");
    setError("");
    startRecording();
  }

  if (phase === "done" && result) {
    return (
      <ResultView
        initialText={result.text ?? ""}
        path={result.path}
        llmFailed={result.llmFailed}
        formats={formats}
        note={resultNote}
        onDictateAgain={dictateAgain}
      />
    );
  }

  if (phase === "transcribing") {
    const now = Date.now();
    const status = transcribingStatus(prog);
    return (
      <Detail
        isLoading
        navigationTitle={`${status} · ${prog ? fmtMs(now - prog.start) : "0.0s"}`}
        markdown={buildTranscribingMarkdown(prog, now)}
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
              onAction={startRecording}
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

  // recording
  const st = stateRef.current;
  const elapsed = st ? Math.floor((Date.now() - st.startedAt) / 1000) : 0;
  const level = readLevel(st?.meter);
  const fmt = currentFormat();
  // Live partial transcript the daemon produces while we record (if streaming).
  const live = resolveLiveTranscript(readStream(), st);
  const md = buildRecordingMarkdown({ elapsed, level, fmt, live });

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
          <ActionPanel.Submenu
            title={`Output: ${fmt.ai ? fmt.title : "Raw (no AI)"}`}
            icon={Icon.Wand}
            shortcut={{ modifiers: ["cmd"], key: "f" }}
          >
            {formats.map((f) => (
              <Action
                key={f.id}
                title={f.ai ? f.title : `${f.title} — no AI`}
                icon={f.ai ? Icon.Wand : Icon.Text}
                onAction={() => setFormatId(f.id)}
              />
            ))}
          </ActionPanel.Submenu>
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
