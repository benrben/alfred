import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  Toast,
  showToast,
} from "@raycast/api";
import { useState } from "react";
import {
  callEngine,
  FormatChoice,
  flagsForFormat,
  lastErrorLine,
  resolveDelivery,
} from "./engine";

interface ResultViewProps {
  initialText: string;
  path?: string;
  llmFailed?: boolean;
  formats: FormatChoice[];
  /** Shown as the top note when first opened (e.g. the format that produced it). */
  note?: string;
  /** When provided, adds a "Dictate Again" action (used by the Dictate command). */
  onDictateAgain?: () => void;
}

// A transcript/result screen with Paste / Copy and a "Reprocess as…" submenu
// that re-runs the text through any format (raw → email, tighten, translate…).
export function ResultView({
  initialText,
  path,
  llmFailed,
  formats,
  note,
  onDictateAgain,
}: ResultViewProps) {
  const [text, setText] = useState(initialText);
  const [banner, setBanner] = useState<string>(
    llmFailed
      ? "⚠️ LLM step failed — raw transcript below."
      : path
        ? `💾 Saved to \`${path}\``
        : (note ?? ""),
  );
  const [busy, setBusy] = useState(false);

  async function reprocess(fmt: FormatChoice) {
    setBusy(true);
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: `Reprocessing — ${fmt.title}…`,
    });
    const res = await callEngine(["text", text, ...flagsForFormat(fmt)]);
    const delivered = await resolveDelivery(res);
    setBusy(false);
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      setText(delivered.text ?? text);
      setBanner(fmt.ai ? `↻ Reprocessed as ${fmt.title}` : "↻ Raw transcript");
      toast.style = Toast.Style.Success;
      toast.title = "Done";
    } else {
      toast.style = Toast.Style.Failure;
      toast.title = "Reprocess failed";
      toast.message = lastErrorLine(res.err);
    }
  }

  const markdown = `${banner ? `> ${banner}\n\n` : ""}${text || "_(empty)_"}`;

  return (
    <Detail
      isLoading={busy}
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action.Paste title="Paste to Frontmost App" content={text} />
          <Action.CopyToClipboard
            title="Copy"
            content={text}
            shortcut={{ modifiers: ["cmd"], key: "c" }}
          />
          <ActionPanel.Submenu
            title="Reprocess as…"
            icon={Icon.Wand}
            shortcut={{ modifiers: ["cmd"], key: "r" }}
          >
            {formats.map((f) => (
              <Action
                key={f.id}
                title={f.ai ? f.title : `${f.title} (no AI)`}
                icon={f.ai ? Icon.Wand : Icon.Text}
                onAction={() => reprocess(f)}
              />
            ))}
          </ActionPanel.Submenu>
          {onDictateAgain ? (
            <Action
              title="Dictate Again"
              icon={Icon.Microphone}
              shortcut={{ modifiers: ["cmd"], key: "d" }}
              onAction={onDictateAgain}
            />
          ) : null}
        </ActionPanel>
      }
    />
  );
}
