import { showHUD } from "@raycast/api";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  callEngine,
  clearRecState,
  commonFlags,
  engineEnv,
  expandHome,
  fileSize,
  getPrefs,
  isAlive,
  lastErrorLine,
  readRecState,
  RecState,
  resolveDelivery,
  writeRecState,
} from "./lib/engine";

// Toggle command: first run starts recording, the next run stops + processes.
// Bind a Raycast hotkey for push-to-talk-style capture.
export default async function dictate() {
  const state = readRecState();
  if (state && isAlive(state.pid)) {
    await stopAndProcess(state);
  } else {
    if (state) clearRecState(); // stale (sox died) — reset
    await startRecording();
  }
}

async function startRecording() {
  const prefs = getPrefs();
  const sox = expandHome(prefs.soxBin);
  if (!existsSync(sox)) {
    await showHUD(
      "⚠️ sox not found — run: brew install sox (then set its path in preferences)",
    );
    return;
  }
  // Stamp the filename with seconds; Date.now() keeps repeated runs distinct.
  const wav = join(tmpdir(), `alfred_rec_${Date.now()}.wav`);
  let pid: number | undefined;
  try {
    const child = spawn(
      sox,
      ["-d", "-r", "16000", "-c", "1", "-b", "16", wav],
      {
        detached: true,
        stdio: "ignore",
        env: engineEnv(),
      },
    );
    child.unref();
    pid = child.pid;
  } catch (e) {
    await showHUD(`⚠️ Could not start the recorder: ${String(e)}`);
    return;
  }
  if (!pid) {
    await showHUD("⚠️ Could not start the recorder (sox).");
    return;
  }
  writeRecState({ pid, wav, startedAt: Date.now() });
  await showHUD("🔴 Recording… run Dictate again to stop");
}

async function stopAndProcess(state: RecState) {
  try {
    process.kill(state.pid, "SIGINT"); // sox finalizes the WAV header on SIGINT
  } catch {
    // already gone
  }
  await waitForExit(state.pid, 4000);
  clearRecState();
  await showHUD("⏳ Transcribing…");

  if (fileSize(state.wav) <= 1024) {
    await showHUD("Nothing recorded.");
    return;
  }
  const res = await callEngine(["process", state.wav, ...commonFlags()]);
  const delivered = await resolveDelivery(res);
  if (delivered.kind === "copied") {
    const snippet = (delivered.text ?? "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, 60);
    await showHUD(
      delivered.llmFailed
        ? "Copied raw transcript (LLM step failed)"
        : `Copied ✓ — ${snippet}`,
    );
  } else if (delivered.kind === "saved") {
    await showHUD(
      `💾 Saved to file: ${delivered.path ?? "(see notification)"}`,
    );
  } else if (delivered.kind === "empty") {
    await showHUD("No speech detected.");
  } else {
    await showHUD(`⚠️ Error: ${lastErrorLine(res.err)}`);
  }
}

function waitForExit(pid: number, timeoutMs: number): Promise<void> {
  return new Promise((resolve) => {
    const start = Date.now();
    const tick = () => {
      if (!isAlive(pid) || Date.now() - start > timeoutMs) {
        // small grace period so the WAV is fully flushed to disk
        setTimeout(resolve, 150);
        return;
      }
      setTimeout(tick, 100);
    };
    tick();
  });
}
