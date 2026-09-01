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
import {
  closeSync,
  existsSync,
  fstatSync,
  openSync,
  readSync,
  unlinkSync,
} from "node:fs";
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
  loadModes,
  pingDaemon,
  Progress,
  readProgress,
  readRecState,
  readStream,
  recorderArgs,
  RecState,
  refreshMenuBar,
  DeliveredResult,
  resolveDelivery,
  writeRecState,
} from "./lib/engine";
import { ResultView } from "./lib/ResultView";
import {
  buildRecordingMarkdown,
  buildTranscribingMarkdown,
  engineErrorExcerpt,
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

// Best-effort delete of a recording-session file (the .meter is Raycast-only —
// the engine never touches it — and a cancelled/abandoned WAV that no
// `stream-finish` will ever consume). Missing/already-gone is not an error.
function removeIfPresent(path?: string): void {
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

export default function Dictate(props: {
  launchContext?: { stop?: boolean };
  // When set (the Transcribe Only command), the capture starts pinned to this
  // format instead of the config default — so streaming and the final transcribe
  // use its flags from the very first frame, before the mode catalog loads.
  forceFormat?: FormatChoice;
}) {
  const [phase, setPhase] = useState<Phase>("recording");
  const [, setTick] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<DeliveredResult | null>(null);
  const [resultNote, setResultNote] = useState("");
  const [formats, setFormats] = useState<FormatChoice[]>([]);
  const [formatId, setFormatId] = useState<string>(
    props.forceFormat?.id ?? CONFIG_FORMAT_ID,
  );
  const [prog, setProg] = useState<Progress | null>(null);
  const stateRef = useRef<RecState | null>(null);

  // Load the format list (async) and start/adopt a recording immediately. The
  // default stays "Default (config)" so we never contradict the user's config.
  useEffect(() => {
    (async () => {
      const modesPromise = loadModes();
      const existing = readRecState();
      if (existing && isAlive(existing.pid)) {
        stateRef.current = existing;
        // Restore the format chosen when this recording started, so a reopen or
        // a menu-bar stop doesn't silently fall back to the config default.
        if (existing.format) setFormatId(existing.format.id);
        setPhase("recording");
        // Act NOW — don't wait on the modes round-trip. currentFormat() falls
        // back to the persisted RecState.format, so a menu-bar-triggered stop
        // still uses the right flags; the format list only feeds the UI picker.
        if (props.launchContext?.stop) void stopAndTranscribe();
        setFormats(buildFormats(await modesPromise));
      } else {
        if (existing) clearRecState();
        // Mic on immediately; load the picker list in parallel (startRecording's
        // stream-start uses currentFormat(), which handles an empty list).
        startRecording();
        setFormats(buildFormats(await modesPromise));
      }
    })();
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
    // Defensively stop any recorder still tracked in RecState before starting a
    // new one, so a confused state can never orphan a `sox` that keeps holding
    // the mic and growing a file. SIGINT lets it finalize its WAV.
    const stale = readRecState();
    if (stale && isAlive(stale.pid)) {
      try {
        process.kill(stale.pid, "SIGINT");
      } catch {
        // already gone
      }
    }
    if (stale) {
      // Abandoned outright — no stream-finish will ever consume this WAV, so
      // (unlike a normal stop) we must clean it up ourselves, alongside its
      // Raycast-only .meter file.
      removeIfPresent(stale.wav);
      removeIfPresent(stale.meter);
    }
    const stamp = Date.now();
    const wav = join(tmpdir(), `alfred_rec_${stamp}.wav`);
    const meter = join(tmpdir(), `alfred_rec_${stamp}.meter`);
    try {
      const fd = openSync(meter, "w");
      // Recorder args come from the engine's contract (audio.sox_args) so the
      // int16/mono/16 kHz invariant lives in one place, not copied here.
      const child = spawn(sox, recorderArgs(wav), {
        detached: true,
        stdio: ["ignore", "ignore", fd],
        env: engineEnv(),
      });
      child.unref();
      closeSync(fd);
      if (!child.pid) throw new Error("recorder did not start");
      const fmt = currentFormat();
      stateRef.current = {
        pid: child.pid,
        wav,
        meter,
        startedAt: stamp,
        format: fmt,
      };
      writeRecState(stateRef.current);
      refreshMenuBar(); // show the 🔴 indicator immediately, not up to 60s later
      setPhase("recording");
      // Start transcribing the growing WAV in the warm daemon so most of it is
      // done by the time we stop. Best-effort: only via the daemon (a one-shot
      // would exit immediately); on stop, stream-finish falls back to batch.
      void (async () => {
        try {
          if (await pingDaemon())
            await callEngine(["stream-start", wav, ...flagsForFormat(fmt)]);
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
    // Prefer the loaded list; else a forced format (Transcribe Only) so its flags
    // apply before the catalog loads; else the format persisted in the recording
    // state (menu-bar cold-stop, before modes load); else "use config".
    return (
      formats.find((f) => f.id === formatId) ??
      props.forceFormat ??
      stateRef.current?.format ??
      configFormat()
    );
  }

  // Choose an output format while recording: update state AND persist it into
  // the recording state so a reopen / menu-bar stop still honours it.
  function chooseFormat(f: FormatChoice) {
    setFormatId(f.id);
    const st = stateRef.current;
    if (st) {
      stateRef.current = { ...st, format: f };
      writeRecState(stateRef.current);
    }
  }

  async function stopAndTranscribe() {
    const st = stateRef.current;
    if (!st) return;
    const fmt = currentFormat();
    // Claim this recording synchronously before the first await/render. A
    // double Enter or racing auto-stop then sees no state to finish twice.
    stateRef.current = null;
    setPhase("transcribing");
    setProg(null);
    try {
      process.kill(st.pid, "SIGINT");
    } catch {
      // already gone
    }
    await waitForExit(st.pid, 4000);
    clearRecState();
    refreshMenuBar(); // clear the 🔴 indicator immediately
    // The .meter is Raycast-only (the engine never reads it) and we're done
    // reading it too (phase has left "recording") — clean it up. The WAV
    // itself is NOT removed here: it's about to be handed to stream-finish,
    // and the engine deletes it after transcribing (unless keep_audio).
    removeIfPresent(st.meter);
    if (fileSize(st.wav) <= 1024) {
      // No stream-finish call below means the engine never gets a chance to
      // clean this WAV up (that only happens after it transcribes one) — do
      // it ourselves so an empty/near-empty capture doesn't linger either.
      removeIfPresent(st.wav);
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
      // "error" or an exit-0 "unknown" (no VB_STATUS): show stderr, else a
      // stdout tail, so it's never a bare "unknown error".
      setError(engineErrorExcerpt(res));
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
      refreshMenuBar(); // clear the 🔴 indicator immediately
      // A cancelled recording is never handed to the engine, so nothing else
      // will ever delete this WAV (verbatim audio) or its .meter file.
      removeIfPresent(st.wav);
      removeIfPresent(st.meter);
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
        pasteFailed={result.pasteFailed}
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
                onAction={() => chooseFormat(f)}
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
