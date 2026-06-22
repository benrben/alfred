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
  daemonPort,
  defaultFormatId,
  loadModes,
  loadSettings,
  pingDaemon,
  resolvePython,
  resolveScript,
} from "./lib/engine";

// Health + defaults at a glance: daemon up?, resolved paths, what the engine's
// doctor says, and the current default format / stages / backend.
export default function Command() {
  const [markdown, setMarkdown] = useState("Checking the Alfred engine…");
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const port = daemonPort();
      const [up, settings, modes, doctor] = await Promise.all([
        pingDaemon(),
        loadSettings(),
        loadModes(),
        callEngine(["doctor"]),
      ]);
      const script = resolveScript();
      const python = resolvePython(script);
      const p = settings?.processing;
      const fmt = buildFormats(modes).find(
        (f) => f.id === defaultFormatId(settings),
      );
      const stages = p
        ? [
            p.rewrite ? "rewrite" : null,
            p.translate ? `translate (${p.translate_via})` : null,
            p.optimize ? "optimize" : null,
          ].filter(Boolean)
        : [];
      const report = (doctor.out || doctor.err || "(no output)").trim();

      setMarkdown(
        [
          "# Alfred engine",
          "",
          `- **Warm daemon** (\`127.0.0.1:${port}\`): ${up ? "🟢 up" : "🔴 down (starts on next use)"}`,
          `- **Default output**: ${fmt ? (fmt.ai ? `🪄 ${fmt.title}` : "📝 Raw transcript (no AI)") : "—"}`,
          `- **Stages on**: ${stages.length ? stages.join(", ") : "none (pure transcription)"}`,
          `- **Backend**: ${settings?.backend ?? "—"}${settings?.claude_model ? ` · claude=${settings.claude_model}` : ""}`,
          `- **Engine**: \`${script}\``,
          `- **Python**: \`${python}\``,
          "",
          "## `doctor`",
          "",
          "```",
          report,
          "```",
        ].join("\n"),
      );
      setIsLoading(false);
    })();
  }, []);

  return (
    <Detail
      isLoading={isLoading}
      navigationTitle="Engine Status"
      markdown={markdown}
      actions={
        <ActionPanel>
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
