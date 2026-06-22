import { Icon, LaunchType, MenuBarExtra, launchCommand } from "@raycast/api";
import { isAlive, readRecState } from "./lib/engine";

export default function MenuBar() {
  const state = readRecState();
  const recording = !!state && isAlive(state.pid);

  return (
    <MenuBarExtra
      icon={recording ? Icon.CircleFilled : Icon.Microphone}
      title={recording ? "🔴" : ""}
    >
      <MenuBarExtra.Item
        title={recording ? "Stop & Process" : "Start Dictation"}
        icon={recording ? Icon.Stop : Icon.Microphone}
        onAction={() =>
          launchCommand({ name: "dictate", type: LaunchType.Background })
        }
      />
      <MenuBarExtra.Separator />
      <MenuBarExtra.Item
        title="Transform Text"
        icon={Icon.Wand}
        onAction={() =>
          launchCommand({
            name: "transform-text",
            type: LaunchType.UserInitiated,
          })
        }
      />
      <MenuBarExtra.Item
        title="Type & Process"
        icon={Icon.Pencil}
        onAction={() =>
          launchCommand({
            name: "type-and-process",
            type: LaunchType.UserInitiated,
          })
        }
      />
      <MenuBarExtra.Item
        title="History"
        icon={Icon.Clock}
        onAction={() =>
          launchCommand({ name: "history", type: LaunchType.UserInitiated })
        }
      />
    </MenuBarExtra>
  );
}
