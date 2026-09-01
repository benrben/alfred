import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  LaunchType,
  launchCommand,
  openExtensionPreferences,
} from "@raycast/api";
import { useEffect, useState } from "react";
import {
  buildFormats,
  callEngine,
  contractSchemaWarning,
  daemonPort,
  defaultFormatId,
  loadContract,
  loadModes,
  loadSettings,
  pingDaemon,
  resolvePython,
  resolveScript,
} from "./lib/engine";
import { buildEngineStatusMarkdown, processingStages } from "./lib/view-logic";

// Health + defaults at a glance: daemon up?, resolved paths, what the engine's
// doctor says, and the current default format / stages / backend.
export default function Command() {
  const [markdown, setMarkdown] = useState("Checking the Alfred engine…");
  const [isLoading, setIsLoading] = useState(true);

  async function refresh() {
    setIsLoading(true);
    // loadContract() also computes the schema_version compatibility warning.
    // Load it first: a configured engine may use a non-default daemon port,
    // and the subsequent health/settings requests must use that resolved port.
    await loadContract();
    const port = daemonPort();
    const [up, settings, modes, doctor] = await Promise.all([
      pingDaemon(),
      loadSettings(),
      loadModes(),
      callEngine(["doctor"]),
    ]);
    const script = resolveScript();
    const python = resolvePython(script);
    const fmt = buildFormats(modes).find(
      (f) => f.id === defaultFormatId(settings),
    );
    setMarkdown(
      buildEngineStatusMarkdown({
        port,
        up,
        fmt,
        stages: processingStages(settings?.processing),
        backend: settings?.backend,
        claudeModel: settings?.claude_model,
        script,
        python,
        report: (doctor.out || doctor.err || "(no output)").trim(),
        warning: contractSchemaWarning(),
      }),
    );
    setIsLoading(false);
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <Detail
      isLoading={isLoading}
      navigationTitle="Engine Status"
      markdown={markdown}
      actions={
        <ActionPanel>
          <Action
            title="Refresh"
            icon={Icon.ArrowClockwise}
            shortcut={{ modifiers: ["cmd"], key: "r" }}
            onAction={refresh}
          />
          <Action
            title="Manage Intents & Default"
            icon={Icon.Pencil}
            onAction={() =>
              launchCommand({
                name: "manage-intents",
                type: LaunchType.UserInitiated,
              })
            }
          />
          <Action
            title="Open Extension Preferences"
            icon={Icon.Gear}
            onAction={openExtensionPreferences}
          />
        </ActionPanel>
      }
    />
  );
}
