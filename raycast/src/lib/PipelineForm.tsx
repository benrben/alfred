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
  buildFormats,
  callEngine,
  CONFIG_FORMAT_ID,
  flagsForFormat,
  FormatChoice,
  getInputText,
  lastErrorLine,
  loadModes,
  parseStatus,
  resolveDelivery,
} from "./engine";
import { ResultView } from "./ResultView";

interface PipelineFormProps {
  /** Prefill the text field from the current selection / clipboard. */
  prefillSelection: boolean;
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

  async function onSubmit(values: {
    text: string;
    format: string;
    translate: string;
    backend: string;
  }) {
    const body = (values.text ?? "").trim();
    if (!body) {
      await showToast({
        style: Toast.Style.Failure,
        title: "Type or select some text first",
      });
      return;
    }
    const fmt =
      formats.find((f) => f.id === values.format) ?? formats[0] ?? null;
    if (!fmt) {
      await showToast({
        style: Toast.Style.Failure,
        title: "No formats loaded",
      });
      return;
    }
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: fmt.ai ? `Running — ${fmt.title}…` : "Cleaning up…",
    });
    const flags = flagsForFormat(fmt, {
      translate: values.translate,
      backend: values.backend,
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
      <ResultView
        initialText={delivered.text ?? ""}
        path={delivered.path}
        llmFailed={delivered.llmFailed}
        formats={formats}
        note={fmt.ai ? fmt.title : "Raw transcript"}
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
        <Form.Dropdown.Item value="auto" title="auto" />
        <Form.Dropdown.Item value="claude" title="claude" />
        <Form.Dropdown.Item value="codex" title="codex" />
      </Form.Dropdown>
    </Form>
  );
}
