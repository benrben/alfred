import { Icon, LaunchType, MenuBarExtra, launchCommand } from "@raycast/api";
import { isAlive, readRecState } from "./lib/engine";

function open(name: string, context?: Record<string, unknown>) {
  return () => launchCommand({ name, type: LaunchType.UserInitiated, context });
}

interface DictateItemProps {
  title: string;
  icon: string;
  context?: { stop: boolean };
}

/** Label, icon, and launch context for the top "Dictate"/"Stop & Transcribe"
 * menu item — the one item whose wording flips with the current recording
 * state. Extracted so MenuBar's own branching stays small. */
function dictateItemProps(recording: boolean): DictateItemProps {
  return {
    title: recording ? "Stop & Transcribe" : "Dictate",
    icon: recording ? Icon.Stop : Icon.Microphone,
    // While recording, open Dictate already in "stop" mode (one click stops).
    context: recording ? { stop: true } : undefined,
  };
}

export default function MenuBar() {
  const state = readRecState();
  const recording = !!state && isAlive(state.pid);
  const dictate = dictateItemProps(recording);

  return (
    <MenuBarExtra
      icon={recording ? Icon.CircleFilled : Icon.Microphone}
      title={recording ? "🔴" : ""}
      tooltip="Alfred"
    >
      <MenuBarExtra.Item
        title={dictate.title}
        icon={dictate.icon}
        onAction={open("dictate", dictate.context)}
      />
      {!recording && (
        <MenuBarExtra.Item
          title="Transcribe Only"
          icon={Icon.Text}
          onAction={open("transcribe")}
        />
      )}
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
