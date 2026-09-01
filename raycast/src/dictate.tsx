import { closeMainWindow, popToRoot } from "@raycast/api";
import { spawn } from "node:child_process";
import { closeSync, existsSync, openSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { useEffect, useRef, useState } from "react";
import {
  callEngine,
  clearRecState,
  configFormat,
  DeliveredResult,
  EngineResult,
  engineEnv,
  expandHome,
  fileSize,
  flagsForFormat,
  FormatChoice,
  getPrefs,
  normalizeBackend,
  normalizeTranslate,
  Progress,
  readProgress,
  readRecState,
  readStream,
  recorderArgs,
  RecState,
  refreshMenuBar,
  resolveDelivery,
  writeRecState,
} from "./lib/engine";
import {
  abandonStaleRecording,
  beginStreamTranscription,
  bootstrapRecording,
  elapsedSeconds,
  finalizeCaptureState,
  initialFormatId,
  Phase,
  readLevel,
  removeIfPresent,
} from "./lib/dictate-logic";
import {
  renderDone,
  renderError,
  renderRecording,
  renderTranscribing,
} from "./lib/dictate-views";
import { engineErrorExcerpt, resolveLiveTranscript } from "./lib/view-logic";

interface DictateProps {
  launchContext?: { stop?: boolean };
  // Transcribe Only: pins the capture to this format instead of the config
  // default, from the first frame (before the mode catalog loads).
  forceFormat?: FormatChoice;
}

export default function Dictate(props: DictateProps) {
  const [phase, setPhase] = useState<Phase>("recording");
  const [, setTick] = useState(0);
  const [error, setError] = useState("");
  const [result, setResult] = useState<DeliveredResult | null>(null);
  const [resultNote, setResultNote] = useState("");
  const [formats, setFormats] = useState<FormatChoice[]>([]);
  const [formatId, setFormatId] = useState<string>(() =>
    initialFormatId(props.forceFormat),
  );
  const [backend, setBackend] = useState(() =>
    normalizeBackend(getPrefs().backend),
  );
  const [translate] = useState(() => normalizeTranslate(getPrefs().translate));
  const [prog, setProg] = useState<Progress | null>(null);
  const stateRef = useRef<RecState | null>(null);

  // Load the format list and start/adopt a recording immediately.
  useEffect(() => {
    void bootstrapRecording({
      launchContext: props.launchContext,
      stateRef,
      setBackend,
      setFormatId,
      setPhase,
      setFormats,
      stopAndTranscribe,
      startRecording,
    });
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
    abandonStaleRecording(readRecState());
    const stamp = Date.now();
    const wav = join(tmpdir(), `alfred_rec_${stamp}.wav`);
    const meter = join(tmpdir(), `alfred_rec_${stamp}.meter`);
    try {
      const fd = openSync(meter, "w");
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
        backend: currentBackend(),
        translate: currentTranslate(),
      };
      writeRecState(stateRef.current);
      refreshMenuBar(); // show the 🔴 indicator immediately, not up to 60s later
      setPhase("recording");
      void beginStreamTranscription(wav, captureFlags(fmt));
    } catch (e) {
      setError(`Could not start the recorder: ${String(e)}`);
      setPhase("error");
    }
  }

  // Prefer the loaded list; else forceFormat (Transcribe Only); else the
  // persisted RecState.format (menu-bar cold-stop); else "use config".
  function currentFormat(): FormatChoice {
    const fromList = formats.find((f) => f.id === formatId);
    if (fromList) return fromList;
    if (props.forceFormat) return props.forceFormat;
    const persistedFormat = stateRef.current?.format;
    if (persistedFormat) return persistedFormat;
    return configFormat();
  }

  function currentBackend(): string {
    return normalizeBackend(stateRef.current?.backend ?? backend);
  }

  function currentTranslate(): string {
    return normalizeTranslate(stateRef.current?.translate ?? translate);
  }

  // Pins the backend for the whole recording (matters for a menu-bar stop,
  // a new Raycast process); the choice is persisted alongside the format.
  function captureFlags(fmt: FormatChoice): string[] {
    return flagsForFormat(fmt, {
      backend: currentBackend(),
      translate: currentTranslate(),
    });
  }

  // Persist the chosen format into RecState too, so a reopen/menu-bar stop
  // still honours it.
  function chooseFormat(f: FormatChoice) {
    setFormatId(f.id);
    const st = stateRef.current;
    if (st) {
      stateRef.current = { ...st, format: f };
      writeRecState(stateRef.current);
    }
  }

  function chooseBackend(value: string) {
    const selected = normalizeBackend(value);
    setBackend(selected);
    const st = stateRef.current;
    if (st) {
      stateRef.current = { ...st, backend: selected };
      writeRecState(stateRef.current);
    }
  }

  // Classify the delivered result into the next phase.
  function applyDeliveryOutcome(
    delivered: DeliveredResult,
    fmt: FormatChoice,
    res: EngineResult,
  ): void {
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      setResult(delivered);
      setResultNote(fmt.ai ? fmt.title : "Raw transcript");
      setPhase("done");
    } else if (delivered.kind === "empty") {
      setError("No speech detected.");
      setPhase("error");
    } else {
      // "error" or an exit-0 "unknown": stderr, else stdout tail.
      setError(engineErrorExcerpt(res));
      setPhase("error");
    }
  }

  async function stopAndTranscribe() {
    const st = stateRef.current;
    if (!st) return;
    const fmt = currentFormat();
    setPhase("transcribing");
    setProg(null);
    await finalizeCaptureState(st);
    if (fileSize(st.wav) <= 1024) {
      // No stream-finish below means the engine never cleans this WAV up.
      removeIfPresent(st.wav);
      setError("Nothing recorded.");
      setPhase("error");
      return;
    }
    // Live per-step stopwatch: re-reading also re-renders. Ignore a stale
    // progress file left by an earlier capture (ts older than this one).
    const procStart = Date.now();
    const poll = setInterval(() => {
      const p = readProgress();
      if (p && p.ts >= procStart - 1500) setProg(p);
    }, 200);
    let res;
    try {
      // Most of the WAV is likely already transcribed by the warm daemon;
      // falls back to a full batch transcribe if there was no live session.
      res = await callEngine(["stream-finish", st.wav, ...captureFlags(fmt)]);
    } finally {
      clearInterval(poll);
    }
    const delivered = await resolveDelivery(res);
    applyDeliveryOutcome(delivered, fmt, res);
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
    return renderDone({
      result,
      resultNote,
      backend: currentBackend(),
      translate: currentTranslate(),
      formats,
      onDictateAgain: dictateAgain,
    });
  }
  if (phase === "transcribing") return renderTranscribing(prog);
  if (phase === "error") {
    return renderError({ error, onRetry: startRecording });
  }
  // recording
  const elapsed = elapsedSeconds(stateRef.current);
  const level = readLevel(stateRef.current?.meter);
  const fmt = currentFormat();
  const live = resolveLiveTranscript(readStream(), stateRef.current);
  return renderRecording({
    elapsed,
    level,
    fmt,
    live,
    formats,
    backend,
    currentBackendValue: currentBackend(),
    onStop: stopAndTranscribe,
    onChooseFormat: chooseFormat,
    onChooseBackend: chooseBackend,
    onCancel: cancel,
  });
}
