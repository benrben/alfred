import { Icon, LaunchType, MenuBarExtra, launchCommand } from "@raycast/api";
import { isAlive, readRecState } from "./lib/engine";

function open(name: string) {
  return () => launchCommand({ name, type: LaunchType.UserInitiated });
}

export default function MenuBar() {
  const state = readRecState();
  const recording = !!state && isAlive(state.pid);

  return (
    <MenuBarExtra
      icon={recording ? Icon.CircleFilled : Icon.Microphone}
      title={recording ? "🔴" : ""}
      tooltip="Alfred"
    >
      <MenuBarExtra.Item
        title={recording ? "Stop Recording…" : "Dictate"}
        icon={recording ? Icon.Stop : Icon.Microphone}
        onAction={open("dictate")}
      />
      <MenuBarExtra.Separator />
      <MenuBarExtra.Item
        title="Transform Text"
        icon={Icon.Wand}
        onAction={open("transform-text")}
      />
      <MenuBarExtra.Item
        title="Type & Process"
        icon={Icon.Pencil}
        onAction={open("type-and-process")}
      />
      <MenuBarExtra.Item
        title="History"
        icon={Icon.Clock}
        onAction={open("history")}
      />
      <MenuBarExtra.Item
        title="Manage Intents"
        icon={Icon.Pencil}
        onAction={open("manage-intents")}
      />
      <MenuBarExtra.Separator />
      <MenuBarExtra.Item
        title="Engine Status"
        icon={Icon.Heartbeat}
        onAction={open("engine-status")}
      />
    </MenuBarExtra>
  );
}
