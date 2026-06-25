import {
  Action,
  ActionPanel,
  Detail,
  Form,
  Icon,
  Toast,
  showToast,
  useNavigation,
} from "@raycast/api";
import { useState } from "react";
import {
  backendFlags,
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

// A free-text "tell it what to change" form. Re-runs the current result through
// the engine with a one-off instruction (no stage pipeline) and returns the
// revised text to the result screen.
function FeedbackForm({
  currentText,
  onRefined,
}: {
  currentText: string;
  onRefined: (text: string, instruction: string) => void;
}) {
  const { pop } = useNavigation();
  const [busy, setBusy] = useState(false);

  async function submit(values: { instruction: string }) {
    const instruction = (values.instruction || "").trim();
    if (!instruction) return;
    setBusy(true);
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: "Refining…",
    });
    const res = await callEngine([
      "text",
      currentText,
      "--instruction",
      instruction,
      ...backendFlags(),
    ]);
    const delivered = await resolveDelivery(res);
    setBusy(false);
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      toast.style = Toast.Style.Success;
      toast.title = "Refined";
      onRefined(delivered.text ?? currentText, instruction);
      pop();
    } else {
      toast.style = Toast.Style.Failure;
      toast.title = "Refine failed";
      toast.message = lastErrorLine(res.err);
    }
  }

  return (
    <Form
      isLoading={busy}
      navigationTitle="Refine with feedback"
      actions={
        <ActionPanel>
          <Action.SubmitForm title="Apply Feedback" icon={Icon.Wand} onSubmit={submit} />
        </ActionPanel>
      }
    >
      <Form.Description text="Tell Alfred what to change. It revises the current result." />
      <Form.TextArea
        id="instruction"
        title="Feedback"
        placeholder="e.g. make it shorter · more formal · fix the date · translate to Hebrew"
        autoFocus
      />
    </Form>
  );
}

// A transcript/result screen with Paste / Copy, a "Reprocess as…" submenu that
// re-runs the text through any format, and "Refine with feedback…" to adjust it
// with a free-text instruction.
export function ResultView({
  initialText,
  path,
  llmFailed,
  formats,
  note,
  onDictateAgain,
}: ResultViewProps) {
  const { push } = useNavigation();
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

  function applyRefined(newText: string, instruction: string) {
    setText(newText);
    setBanner(`✎ Refined: ${instruction}`);
  }

  const adjust = [
    "",
    "---",
    "### Adjust this result",
    "- ✎ **Refine with feedback** — `⌘E`  ·  tell it what to change",
    "- ↻ **Change intent / format** — `⌘R`",
    `- 📋 Copy \`⌘C\`  ·  Paste \`⏎\`${onDictateAgain ? "  ·  🎙 Dictate again `⌘D`" : ""}`,
  ].join("\n");
  const markdown = `${banner ? `> ${banner}\n\n` : ""}${text || "_(empty)_"}\n${adjust}`;

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
          <Action
            title="Refine with Feedback…"
            icon={Icon.Pencil}
            shortcut={{ modifiers: ["cmd"], key: "e" }}
            onAction={() =>
              push(<FeedbackForm currentText={text} onRefined={applyRefined} />)
            }
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
