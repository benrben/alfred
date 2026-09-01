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
  BACKENDS,
  backendFlags,
  callEngine,
  FormatChoice,
  getPrefs,
  flagsForFormat,
  normalizeBackend,
  normalizeTranslate,
  resolveDelivery,
} from "./engine";
import {
  backendLabel,
  composeResultMarkdown,
  engineErrorExcerpt,
  initialBanner,
  refinedBanner,
  reprocessBanner,
} from "./view-logic";

interface ResultViewProps {
  initialText: string;
  path?: string;
  llmFailed?: boolean;
  /** Auto-paste was requested but the keystroke didn't land (surfaced as a note). */
  pasteFailed?: boolean;
  /** Backend used for the preceding run, when the caller selected one. */
  backend?: string;
  /** Translation override used for the preceding run. */
  translate?: string;
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
  backend,
  onRefined,
}: {
  currentText: string;
  backend: string;
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
      ...backendFlags(backend),
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
      toast.message = engineErrorExcerpt(res);
    }
  }

  return (
    <Form
      isLoading={busy}
      navigationTitle="Refine with feedback"
      actions={
        <ActionPanel>
          <Action.SubmitForm
            title="Apply Feedback"
            icon={Icon.Wand}
            onSubmit={submit}
          />
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

// A transcript/result screen with Paste / Copy, a "Reprocess As…" submenu that
// re-runs the text through any format, and "Refine with feedback…" to adjust it
// with a free-text instruction.
export function ResultView({
  initialText,
  path,
  llmFailed,
  pasteFailed,
  backend: initialBackend,
  translate: initialTranslate,
  formats,
  note,
  onDictateAgain,
}: ResultViewProps) {
  const { push } = useNavigation();
  const [text, setText] = useState(initialText);
  const [backend, setBackend] = useState(() =>
    normalizeBackend(initialBackend ?? getPrefs().backend),
  );
  const [translate] = useState(() =>
    normalizeTranslate(initialTranslate ?? getPrefs().translate),
  );
  const [banner, setBanner] = useState<string>(
    initialBanner({ llmFailed, pasteFailed, path, note }),
  );
  const [busy, setBusy] = useState(false);

  async function reprocess(fmt: FormatChoice) {
    setBusy(true);
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: `Reprocessing — ${fmt.title}…`,
    });
    const res = await callEngine([
      "text",
      text,
      ...flagsForFormat(fmt, { backend, translate }),
    ]);
    const delivered = await resolveDelivery(res);
    setBusy(false);
    if (delivered.kind === "copied" || delivered.kind === "saved") {
      setText(delivered.text ?? text);
      setBanner(reprocessBanner(fmt));
      toast.style = Toast.Style.Success;
      toast.title = "Done";
    } else {
      toast.style = Toast.Style.Failure;
      toast.title = "Reprocess failed";
      toast.message = engineErrorExcerpt(res);
    }
  }

  function applyRefined(newText: string, instruction: string) {
    setText(newText);
    setBanner(refinedBanner(instruction));
  }

  // Checkmark on the currently-selected backend choice, plain gear otherwise —
  // shared by "Default (config)" and every BACKENDS entry below.
  function backendIcon(choice: string) {
    return backend === choice ? Icon.Checkmark : Icon.Gear;
  }

  const markdown = composeResultMarkdown({
    banner,
    text,
    canDictateAgain: !!onDictateAgain,
  });

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
              push(
                <FeedbackForm
                  currentText={text}
                  backend={backend}
                  onRefined={applyRefined}
                />,
              )
            }
          />
          <ActionPanel.Submenu
            title={`Backend: ${backendLabel(backend)}`}
            icon={Icon.Gear}
            shortcut={{ modifiers: ["cmd"], key: "b" }}
          >
            <Action
              title="Default (config)"
              icon={backendIcon("default")}
              onAction={() => setBackend("default")}
            />
            {BACKENDS.map((choice) => (
              <Action
                key={choice}
                title={choice}
                icon={backendIcon(choice)}
                onAction={() => setBackend(choice)}
              />
            ))}
          </ActionPanel.Submenu>
          <ActionPanel.Submenu
            title="Reprocess As…"
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
