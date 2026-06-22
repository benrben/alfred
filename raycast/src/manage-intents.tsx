import {
  Action,
  ActionPanel,
  Color,
  Form,
  Icon,
  List,
  Toast,
  showToast,
  useNavigation,
} from "@raycast/api";
import { useEffect, useState } from "react";
import {
  buildFormats,
  callEngine,
  defaultFormatId,
  lastErrorLine,
  loadModes,
  loadSettings,
  Mode,
  RAW_FORMAT_ID,
  setDefaultFormat,
  Settings,
} from "./lib/engine";

// See every format/intent, which one is the current default, edit the rewrite
// prompt behind each, add new ones, and set the default — all saved to
// ~/.config/voicebridge/config.toml via the engine.
export default function ManageIntents() {
  const [modes, setModes] = useState<Mode[]>([]);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const { push } = useNavigation();

  async function reload() {
    setIsLoading(true);
    const [m, s] = await Promise.all([loadModes(), loadSettings()]);
    setModes(m);
    setSettings(s);
    setIsLoading(false);
  }

  useEffect(() => {
    reload();
  }, []);

  const formats = buildFormats(modes);
  const defaultId = defaultFormatId(settings);

  async function makeDefault(fmtId: string) {
    const fmt = formats.find((f) => f.id === fmtId);
    if (!fmt) return;
    const toast = await showToast({
      style: Toast.Style.Animated,
      title: "Setting default…",
    });
    const ok = await setDefaultFormat(fmt);
    if (ok) {
      toast.style = Toast.Style.Success;
      toast.title = `Default: ${fmt.ai ? fmt.title : "Raw transcript"}`;
      await reload();
    } else {
      toast.style = Toast.Style.Failure;
      toast.title = "Could not set default";
    }
  }

  return (
    <List
      isLoading={isLoading}
      isShowingDetail
      searchBarPlaceholder="Formats & intents — the default is starred"
    >
      {formats.length <= 1 && !isLoading ? (
        <List.EmptyView
          title="Engine not reachable"
          description="Run the Engine Status command to diagnose."
          icon={Icon.Stars}
        />
      ) : (
        formats.map((f) => {
          const mode = modes.find((m) => m.key === f.id);
          const isDefault = f.id === defaultId;
          const body = f.ai
            ? mode?.prompt
              ? mode.prompt
              : "_Generic cleanup — no extra shaping prompt._"
            : "_Exactly what you said. No LLM call._";
          return (
            <List.Item
              key={f.id}
              icon={f.ai ? Icon.Wand : Icon.Text}
              title={f.title}
              subtitle={f.id === RAW_FORMAT_ID ? "no AI" : f.id}
              accessories={
                isDefault
                  ? [{ tag: { value: "Default", color: Color.Green } }]
                  : []
              }
              detail={
                <List.Item.Detail
                  markdown={[
                    `# ${f.title}${isDefault ? "  ⭐️" : ""}`,
                    "",
                    f.subtitle ? `_${f.subtitle}_` : "",
                    "",
                    "---",
                    "",
                    body,
                  ].join("\n")}
                />
              }
              actions={
                <ActionPanel>
                  <Action
                    title="Set as Default"
                    icon={Icon.Star}
                    onAction={() => makeDefault(f.id)}
                  />
                  {f.ai && mode ? (
                    <Action
                      title="Edit Prompt"
                      icon={Icon.Pencil}
                      shortcut={{ modifiers: ["cmd"], key: "e" }}
                      onAction={() =>
                        push(<IntentForm mode={mode} onSaved={reload} />)
                      }
                    />
                  ) : null}
                  <Action
                    title="New Intent"
                    icon={Icon.Plus}
                    shortcut={{ modifiers: ["cmd"], key: "n" }}
                    onAction={() => push(<IntentForm onSaved={reload} />)}
                  />
                  <Action
                    title="Reload"
                    icon={Icon.ArrowClockwise}
                    shortcut={{ modifiers: ["cmd"], key: "r" }}
                    onAction={reload}
                  />
                </ActionPanel>
              }
            />
          );
        })
      )}
    </List>
  );
}

function IntentForm({ mode, onSaved }: { mode?: Mode; onSaved: () => void }) {
  const { pop } = useNavigation();
  const isNew = !mode;
  const [key, setKey] = useState(mode?.key ?? "");
  const [keyError, setKeyError] = useState<string | undefined>();

  async function onSubmit(values: {
    key?: string;
    label: string;
    description: string;
    prompt: string;
  }) {
    const k = (isNew ? (values.key ?? "") : mode!.key).trim();
    if (!/^[A-Za-z0-9_-]+$/.test(k)) {
      setKeyError("Use letters, numbers, - or _ only.");
      return;
    }
    const argv = ["set-intent", k, "--prompt", values.prompt ?? ""];
    if (values.label?.trim()) argv.push("--label", values.label.trim());
    if (values.description?.trim())
      argv.push("--description", values.description.trim());

    const toast = await showToast({
      style: Toast.Style.Animated,
      title: "Saving…",
    });
    const res = await callEngine(argv);
    if ((res.out || "").includes("saved")) {
      toast.style = Toast.Style.Success;
      toast.title = isNew ? `Added “${k}”` : `Saved “${k}”`;
      onSaved();
      pop();
    } else {
      toast.style = Toast.Style.Failure;
      toast.title = "Could not save";
      toast.message = lastErrorLine(res.err);
    }
  }

  return (
    <Form
      navigationTitle={isNew ? "New Intent" : `Edit “${mode!.key}”`}
      actions={
        <ActionPanel>
          <Action.SubmitForm
            title="Save"
            icon={Icon.SaveDocument}
            onSubmit={onSubmit}
          />
        </ActionPanel>
      }
    >
      {isNew ? (
        <Form.TextField
          id="key"
          title="Key"
          placeholder="e.g. standup"
          value={key}
          error={keyError}
          onChange={(v) => {
            setKey(v);
            setKeyError(
              /^[A-Za-z0-9_-]*$/.test(v)
                ? undefined
                : "letters/numbers/-/_ only",
            );
          }}
        />
      ) : (
        <Form.Description title="Key" text={mode!.key} />
      )}
      <Form.TextField
        id="label"
        title="Label"
        placeholder="Shown in the format picker"
        defaultValue={mode?.label ?? ""}
      />
      <Form.TextField
        id="description"
        title="Description"
        placeholder="Short hint"
        defaultValue={mode?.description ?? ""}
      />
      <Form.TextArea
        id="prompt"
        title="Prompt"
        placeholder="How rewrite should shape the text for this format…"
        defaultValue={mode?.prompt ?? ""}
      />
    </Form>
  );
}
