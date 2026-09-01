import {
  Action,
  ActionPanel,
  Form,
  Icon,
  LaunchType,
  Toast,
  launchCommand,
  showToast,
  useNavigation,
} from "@raycast/api";
import { useEffect, useState } from "react";
import {
  BACKENDS,
  buildFormats,
  callEngine,
  CONFIG_FORMAT_ID,
  type DeliveredResult,
  type EngineResult,
  flagsForFormat,
  FormatChoice,
  getInputText,
  loadModes,
  resolveDelivery,
} from "./engine";
import { deliveryFailure, resolveFormat } from "./view-logic";
import { ResultView } from "./ResultView";

interface PipelineFormProps {
  /** Prefill the text field from the current selection / clipboard. */
  prefillSelection: boolean;
}

type SubmitValues = {
  text: string;
  format: string;
  translate: string;
  backend: string;
};

/** Validation result for a submitted form: either the resolved body text +
 * format to run, or the failure-toast title to show instead. Pure so the
 * "is this submission even runnable" logic can be checked without a fake
 * engine. */
type Submission =
  | { ok: true; body: string; fmt: FormatChoice }
  | { ok: false; title: string };

/** Resolve+validate a submission before touching the engine: non-empty text,
 * and a resolvable format. */
function resolveSubmission(
  values: Pick<SubmitValues, "text" | "format">,
  formats: FormatChoice[],
): Submission {
  const body = (values.text ?? "").trim();
  if (!body) return { ok: false, title: "Type or select some text first" };
  const fmt = resolveFormat(formats, values.format);
  if (!fmt) return { ok: false, title: "No formats loaded" };
  return { ok: true, body, fmt };
}

interface PipelineRunOptions {
  translate: string;
  backend: string;
}

interface PipelineRunResult {
  res: EngineResult;
  delivered: DeliveredResult;
}

/** Run one capture through the engine and resolve what it delivered. */
async function runPipeline(
  fmt: FormatChoice,
  body: string,
  opts: PipelineRunOptions,
): Promise<PipelineRunResult> {
  const flags = flagsForFormat(fmt, opts);
  const res = await callEngine(["text", body, ...flags]);
  const delivered = await resolveDelivery(res);
  return { res, delivered };
}

/** Show a failure toast and stop — the shared tail of every early-return in
 * onSubmit (bad input, no formats, or an unsuccessful delivery). */
async function failSubmission(title: string, message?: string): Promise<void> {
  await showToast({ style: Toast.Style.Failure, title, message });
}

/** Build the ResultView to push after a successful run. */
function buildResultView(args: {
  delivered: DeliveredResult;
  formats: FormatChoice[];
  values: Pick<SubmitValues, "backend" | "translate">;
  fmt: FormatChoice;
}) {
  const { delivered, formats, values, fmt } = args;
  return (
    <ResultView
      initialText={delivered.text ?? ""}
      path={delivered.path}
      llmFailed={delivered.llmFailed}
      pasteFailed={delivered.pasteFailed}
      backend={values.backend}
      translate={values.translate}
      formats={formats}
      note={fmt.ai ? fmt.title : "Raw transcript"}
    />
  );
}

export function PipelineForm({ prefillSelection }: PipelineFormProps) {
  const [formats, setFormats] = useState<FormatChoice[]>([]);
  const [formatId, setFormatId] = useState<string>(CONFIG_FORMAT_ID);
  const [text, setText] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const { push } = useNavigation();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [modes, input] = await Promise.all([
        loadModes(),
        prefillSelection ? getInputText() : Promise.resolve(""),
      ]);
      if (cancelled) return;
      setFormats(buildFormats(modes));
      if (input) setText(input);
      setIsLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [prefillSelection]);

  async function onSubmit(values: SubmitValues) {
    const submission = resolveSubmission(values, formats);
    if (!submission.ok) return failSubmission(submission.title);
    const { body, fmt } = submission;
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: fmt.ai ? `Running — ${fmt.title}…` : "Cleaning up…",
    });
    const { res, delivered } = await runPipeline(fmt, body, {
      translate: values.translate,
      backend: values.backend,
    });
    await toast.hide();
    // Classifies empty/error AND the exit-0-no-VB_STATUS "unknown" case, which
    // used to slip through to an empty result screen.
    const failure = deliveryFailure(delivered.kind, res);
    if (failure) return failSubmission(failure.title, failure.message);
    push(buildResultView({ delivered, formats, values, fmt }));
  }

  return (
    <Form
      isLoading={isLoading}
      actions={
        <ActionPanel>
          <Action.SubmitForm
            title="Run Through Alfred"
            icon={Icon.Wand}
            onSubmit={onSubmit}
          />
          <Action
            title="Manage Intents…"
            icon={Icon.Pencil}
            shortcut={{ modifiers: ["cmd"], key: "i" }}
            onAction={() =>
              launchCommand({
                name: "manage-intents",
                type: LaunchType.UserInitiated,
              })
            }
          />
        </ActionPanel>
      }
    >
      <Form.TextArea
        id="text"
        title="Text"
        placeholder="Text to clean up / reshape…"
        value={text}
        onChange={setText}
      />
      <Form.Dropdown
        id="format"
        title="Format"
        value={formatId}
        onChange={setFormatId}
        info="Raw = no AI. Anything else runs Claude/Codex to clean up and reshape."
      >
        {formats.map((f) => (
          <Form.Dropdown.Item
            key={f.id}
            value={f.id}
            title={f.ai ? f.title : `${f.title} — no AI`}
            icon={f.ai ? Icon.Wand : Icon.Text}
          />
        ))}
      </Form.Dropdown>
      <Form.Dropdown id="translate" title="Translate" defaultValue="default">
        <Form.Dropdown.Item value="default" title="Default (config)" />
        <Form.Dropdown.Item value="on" title="Translate to English" />
        <Form.Dropdown.Item value="off" title="Do not translate" />
      </Form.Dropdown>
      <Form.Dropdown id="backend" title="LLM backend" defaultValue="default">
        <Form.Dropdown.Item value="default" title="Default (config)" />
        {BACKENDS.map((b) => (
          <Form.Dropdown.Item key={b} value={b} title={b} />
        ))}
      </Form.Dropdown>
    </Form>
  );
}
