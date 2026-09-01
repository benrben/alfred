/**
 * Alfred's Dictate command — one render function per phase (done/
 * transcribing/error/recording), so Dictate's own control flow stays a short
 * dispatch. Each takes exactly the data/callbacks it renders with.
 *
 * Split out of dictate.tsx (see dictate-logic.ts's header comment for why).
 */
import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  openExtensionPreferences,
} from "@raycast/api";
import { BACKENDS, DeliveredResult, FormatChoice, Progress } from "./engine";
import type {
  FormatChooser,
  StringSetter,
  VoidCallback,
} from "./dictate-logic";
import { ResultView } from "./ResultView";
import {
  buildRecordingMarkdown,
  buildTranscribingMarkdown,
  fmtMs,
  fmtTime,
  transcribingStatus,
} from "./view-logic";

export function renderDone(args: {
  result: DeliveredResult;
  resultNote: string;
  backend: string;
  translate: string;
  formats: FormatChoice[];
  onDictateAgain: VoidCallback;
}) {
  return (
    <ResultView
      initialText={args.result.text ?? ""}
      path={args.result.path}
      llmFailed={args.result.llmFailed}
      pasteFailed={args.result.pasteFailed}
      backend={args.backend}
      translate={args.translate}
      formats={args.formats}
      note={args.resultNote}
      onDictateAgain={args.onDictateAgain}
    />
  );
}

export function renderTranscribing(prog: Progress | null) {
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

export function renderError(args: { error: string; onRetry: VoidCallback }) {
  const { error, onRetry } = args;
  return (
    <Detail
      navigationTitle="Dictation error"
      markdown={`# ⚠️ ${error}`}
      actions={
        <ActionPanel>
          <Action
            title="Dictate Again"
            icon={Icon.Microphone}
            onAction={onRetry}
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

export function renderRecording(args: {
  elapsed: number;
  level: number;
  fmt: FormatChoice;
  live: string;
  formats: FormatChoice[];
  backend: string;
  currentBackendValue: string;
  onStop: VoidCallback;
  onChooseFormat: FormatChooser;
  onChooseBackend: StringSetter;
  onCancel: VoidCallback;
}) {
  const { elapsed, fmt, backend, currentBackendValue } = args;
  const md = buildRecordingMarkdown(args);
  return (
    <Detail
      markdown={md}
      navigationTitle={`🔴 Recording — ${fmtTime(elapsed)}`}
      actions={
        <ActionPanel>
          <Action
            title="Stop & Transcribe"
            icon={Icon.Stop}
            onAction={args.onStop}
          />
          <ActionPanel.Submenu
            title={`Output: ${fmt.ai ? fmt.title : "Raw (no AI)"}`}
            icon={Icon.Wand}
            shortcut={{ modifiers: ["cmd"], key: "f" }}
          >
            {args.formats.map((f) => (
              <Action
                key={f.id}
                title={f.ai ? f.title : `${f.title} — no AI`}
                icon={f.ai ? Icon.Wand : Icon.Text}
                onAction={() => args.onChooseFormat(f)}
              />
            ))}
          </ActionPanel.Submenu>
          <ActionPanel.Submenu
            title={`Backend: ${
              currentBackendValue === "default"
                ? "Default (config)"
                : currentBackendValue
            }`}
            icon={Icon.Gear}
            shortcut={{ modifiers: ["cmd"], key: "b" }}
          >
            <Action
              title="Default (config)"
              icon={backend === "default" ? Icon.Checkmark : Icon.Gear}
              onAction={() => args.onChooseBackend("default")}
            />
            {BACKENDS.map((b) => (
              <Action
                key={b}
                title={b}
                icon={backend === b ? Icon.Checkmark : Icon.Gear}
                onAction={() => args.onChooseBackend(b)}
              />
            ))}
          </ActionPanel.Submenu>
          <Action
            title="Cancel"
            icon={Icon.XMarkCircle}
            shortcut={{ modifiers: ["ctrl"], key: "c" }}
            onAction={args.onCancel}
          />
        </ActionPanel>
      }
    />
  );
}
