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
import { useEffect, useState } from "react";
import {
  callEngine,
  commonFlags,
  getInputText,
  getPrefs,
  lastErrorLine,
  loadModes,
  Mode,
  parseStatus,
  resolveDelivery,
} from "./engine";

interface PipelineFormProps {
  /** Prefill the text field from the current selection / clipboard. */
  prefillSelection: boolean;
}

export function PipelineForm({ prefillSelection }: PipelineFormProps) {
  const [modes, setModes] = useState<Mode[]>([]);
  const [text, setText] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const { push } = useNavigation();
  const prefs = getPrefs();

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [loaded, input] = await Promise.all([
        loadModes(),
        prefillSelection ? getInputText() : Promise.resolve(""),
      ]);
      if (cancelled) return;
      setModes(loaded);
      if (input) setText(input);
      setIsLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, [prefillSelection]);

  async function onSubmit(values: {
    text: string;
    mode: string;
    backend: string;
    translate: string;
  }) {
    const body = (values.text ?? "").trim();
    if (!body) {
      await showToast({
        style: Toast.Style.Failure,
        title: "Type or select some text first",
      });
      return;
    }
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: "Processing…",
    });
    const flags = commonFlags({
      mode: values.mode || undefined,
      backend: values.backend || undefined,
      translate: values.translate || undefined,
    });
    const res = await callEngine(["text", body, ...flags]);
    if (res.code !== 0 && !parseStatus(res.out)) {
      toast.style = Toast.Style.Failure;
      toast.title = "Engine error";
      toast.message = lastErrorLine(res.err);
      return;
    }
    const delivered = await resolveDelivery(res);
    await toast.hide();
    if (delivered.kind === "empty") {
      await showToast({
        style: Toast.Style.Failure,
        title: "Nothing to process",
      });
      return;
    }
    if (delivered.kind === "error") {
      await showToast({
        style: Toast.Style.Failure,
        title: "Engine error",
        message: lastErrorLine(res.err),
      });
      return;
    }
    push(
      <ResultDetail
        text={delivered.text ?? ""}
        path={delivered.path}
        llmFailed={delivered.llmFailed}
      />,
    );
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
        id="mode"
        title="Format"
        defaultValue={prefs.defaultMode || ""}
      >
        <Form.Dropdown.Item value="" title="Default (use config)" />
        {modes.map((m) => (
          <Form.Dropdown.Item
            key={m.key}
            value={m.key}
            title={m.label || m.key}
            // description shown via the title; keep it simple
          />
        ))}
      </Form.Dropdown>
      <Form.Dropdown
        id="backend"
        title="LLM backend"
        defaultValue={prefs.backend || "default"}
      >
        <Form.Dropdown.Item value="default" title="Default (config)" />
        <Form.Dropdown.Item value="auto" title="auto" />
        <Form.Dropdown.Item value="claude" title="claude" />
        <Form.Dropdown.Item value="codex" title="codex" />
      </Form.Dropdown>
      <Form.Dropdown
        id="translate"
        title="Translate"
        defaultValue={prefs.translate || "default"}
      >
        <Form.Dropdown.Item value="default" title="Default (config)" />
        <Form.Dropdown.Item value="on" title="Translate to English" />
        <Form.Dropdown.Item value="off" title="Do not translate" />
      </Form.Dropdown>
    </Form>
  );
}

interface ResultDetailProps {
  text: string;
  path?: string;
  llmFailed: boolean;
}

export function ResultDetail({ text, path, llmFailed }: ResultDetailProps) {
  const header = llmFailed
    ? "> ⚠️ LLM step failed — this is the raw transcript.\n\n"
    : path
      ? `> 💾 Saved to \`${path}\`\n\n`
      : "";
  const markdown = `${header}${text || "_(empty)_"}`;
  return (
    <Detail
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action.CopyToClipboard title="Copy" content={text} />
          <Action.Paste title="Paste to Frontmost App" content={text} />
          {path ? <Action.ShowInFinder path={path} /> : null}
        </ActionPanel>
      }
    />
  );
}
