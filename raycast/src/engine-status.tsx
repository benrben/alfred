import {
  Action,
  ActionPanel,
  Detail,
  Icon,
  openExtensionPreferences,
} from "@raycast/api";
import { useEffect, useState } from "react";
import {
  callEngine,
  daemonPort,
  pingDaemon,
  resolvePython,
  resolveScript,
} from "./lib/engine";

// Health check: is the warm daemon up, where is the engine, and what does the
// engine's own `doctor` report. Handy after a fresh install.
export default function Command() {
  const [markdown, setMarkdown] = useState("Checking the Alfred engine…");
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    (async () => {
      const port = daemonPort();
      const up = await pingDaemon();
      const script = resolveScript();
      const python = resolvePython(script);
      const res = await callEngine(["doctor"]);
      const report = (res.out || res.err || "(no output)").trim();
      setMarkdown(
        [
          "# Alfred engine status",
          "",
          `- **Warm daemon** (\`127.0.0.1:${port}\`): ${up ? "🟢 up" : "🔴 down (will start on next use)"}`,
          `- **Engine script**: \`${script}\``,
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
      markdown={markdown}
      actions={
        <ActionPanel>
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
