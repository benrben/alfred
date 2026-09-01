// Coverage for engine-state.ts's live-progress/stream readers, the dictate
// command's recording-state file, and the small process helpers
// (refreshMenuBar/isAlive/fileSize).
//
// These functions read/write real files (progress.json, stream.json, and a
// fixed-path recording-state file under the OS tmp dir) — this file mocks
// `node:fs` throughout so no test ever touches a real path on disk (several
// of these, like ~/.voicebridge/progress.json or the tmp rec-state file,
// would otherwise be genuine paths a running Alfred instance also uses).
// None of these functions cache module-level state, so — unlike
// engine-contract's contract cache — a single top-level import (no
// vi.resetModules() dance) is enough for test isolation; the `@raycast/api`
// stub's own exports are spied on directly, the same way engine.test.ts's
// result-view test spies on "../engine" (both are plain source modules under
// src, not native Node builtins).
import { afterEach, describe, expect, it, vi } from "vitest";
import { tmpdir } from "node:os";
import { join } from "node:path";

vi.mock("node:fs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:fs")>();
  return {
    ...actual,
    existsSync: vi.fn(),
    readFileSync: vi.fn(),
    writeFileSync: vi.fn(),
    unlinkSync: vi.fn(),
    statSync: vi.fn(),
  };
});

import * as fs from "node:fs";
import * as engine from "../engine";
import * as stub from "./raycast-api.stub";

afterEach(() => {
  vi.restoreAllMocks();
  vi.resetAllMocks();
  stub.setClipboardText("");
});

const REC_STATE_PATH = join(tmpdir(), "alfred_raycast_dictate.json");

describe("readProgress", () => {
  it("returns null when the progress file doesn't exist", () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    expect(engine.readProgress()).toBeNull();
  });

  it("defaults missing steps to [] on an otherwise-valid progress blob", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({
        phase: "transcribing",
        label: "Transcribing",
        ts: 111,
        start: 100,
      }),
    );
    const p = engine.readProgress();
    expect(p).not.toBeNull();
    expect(p?.label).toBe("Transcribing");
    expect(p?.steps).toEqual([]);
  });

  it("preserves an existing steps array unchanged", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({
        phase: "processing",
        label: "Processing",
        ts: 222,
        start: 200,
        steps: [{ label: "record", ms: 500 }],
      }),
    );
    expect(engine.readProgress()?.steps).toEqual([
      { label: "record", ms: 500 },
    ]);
  });

  it("returns null on malformed (mid-write) JSON", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue('{"phase":"transcri');
    expect(engine.readProgress()).toBeNull();
  });

  it("returns null when required fields (label/ts) are missing", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({ phase: "done" }),
    );
    expect(engine.readProgress()).toBeNull();
  });
});

describe("readStream", () => {
  it("returns null when the stream file doesn't exist", () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    expect(engine.readStream()).toBeNull();
  });

  it("returns the parsed stream state when valid", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({
        transcript: "hello wor",
        recording: true,
        done: false,
        ts: 999,
      }),
    );
    expect(engine.readStream()).toEqual({
      transcript: "hello wor",
      recording: true,
      done: false,
      ts: 999,
    });
  });

  it("returns null on malformed JSON", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue("{not json");
    expect(engine.readStream()).toBeNull();
  });

  it("returns null when required fields (transcript/ts) are missing", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({ recording: false }),
    );
    expect(engine.readStream()).toBeNull();
  });
});

describe("getInputText", () => {
  it("returns the selection as-is when non-empty after trimming", async () => {
    vi.spyOn(stub, "getSelectedText").mockResolvedValue("  hello selection  ");
    expect(await engine.getInputText()).toBe("  hello selection  ");
  });

  it("falls back to the clipboard when the selection is empty", async () => {
    vi.spyOn(stub, "getSelectedText").mockResolvedValue("");
    stub.setClipboardText("clipboard text");
    expect(await engine.getInputText()).toBe("clipboard text");
  });

  it("falls back to the clipboard when the selection is whitespace-only", async () => {
    vi.spyOn(stub, "getSelectedText").mockResolvedValue("   \n\t");
    stub.setClipboardText("clipboard text 2");
    expect(await engine.getInputText()).toBe("clipboard text 2");
  });

  it("falls back to the clipboard when getSelectedText throws (unsupported app)", async () => {
    vi.spyOn(stub, "getSelectedText").mockRejectedValue(
      new Error("no frontmost app"),
    );
    stub.setClipboardText("clipboard text 3");
    expect(await engine.getInputText()).toBe("clipboard text 3");
  });

  it("returns '' when the clipboard itself has no text", async () => {
    vi.spyOn(stub, "getSelectedText").mockResolvedValue("");
    vi.spyOn(stub.Clipboard, "readText").mockResolvedValue(
      null as unknown as string,
    );
    expect(await engine.getInputText()).toBe("");
  });
});

// recStateFile() itself isn't exported; it's exercised (and its fixed
// tmpdir-based path implicitly pinned via REC_STATE_PATH below) through
// readRecState/writeRecState/clearRecState in the groups that follow.

describe("readRecState", () => {
  it("returns null when no state file exists", () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    expect(engine.readRecState()).toBeNull();
  });

  it("returns the parsed state when the file is valid JSON", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue(
      JSON.stringify({ pid: 123, wav: "/tmp/rec.wav", startedAt: 1000 }),
    );
    expect(engine.readRecState()).toEqual({
      pid: 123,
      wav: "/tmp/rec.wav",
      startedAt: 1000,
    });
  });

  it("returns null when the file contains malformed JSON", () => {
    vi.mocked(fs.existsSync).mockReturnValue(true);
    vi.mocked(fs.readFileSync).mockReturnValue("{not json");
    expect(engine.readRecState()).toBeNull();
  });
});

describe("writeRecState", () => {
  it("serializes the state to JSON and writes it to the rec-state file", () => {
    const state = { pid: 42, wav: "/tmp/x.wav", startedAt: 5000 };
    engine.writeRecState(state);
    expect(fs.writeFileSync).toHaveBeenCalledWith(
      REC_STATE_PATH,
      JSON.stringify(state),
    );
  });
});

describe("clearRecState", () => {
  it("deletes the rec-state file", () => {
    engine.clearRecState();
    expect(fs.unlinkSync).toHaveBeenCalledWith(REC_STATE_PATH);
  });

  it("silently ignores an already-gone file", () => {
    vi.mocked(fs.unlinkSync).mockImplementation(() => {
      throw new Error("ENOENT");
    });
    expect(() => engine.clearRecState()).not.toThrow();
  });
});

describe("refreshMenuBar", () => {
  it("launches the menubar command in the background to refresh it", async () => {
    const launchCommand = vi
      .spyOn(stub, "launchCommand")
      .mockResolvedValue(undefined);
    engine.refreshMenuBar();
    await new Promise((resolve) => setImmediate(resolve));
    expect(launchCommand).toHaveBeenCalledWith({
      name: "menubar",
      type: stub.LaunchType.Background,
    });
  });

  it("swallows a launchCommand rejection (best effort)", async () => {
    vi.spyOn(stub, "launchCommand").mockRejectedValue(
      new Error("no menubar command"),
    );
    expect(() => engine.refreshMenuBar()).not.toThrow();
    // Flushing microtasks must not surface an unhandled rejection either.
    await new Promise((resolve) => setImmediate(resolve));
  });
});

describe("isAlive", () => {
  it("returns false for a falsy pid without checking the process", () => {
    const kill = vi.spyOn(process, "kill");
    expect(engine.isAlive(0)).toBe(false);
    expect(kill).not.toHaveBeenCalled();
  });

  it("returns true for a live process (this test process itself)", () => {
    expect(engine.isAlive(process.pid)).toBe(true);
  });

  it("returns false for a pid that doesn't exist", () => {
    // A pid this large is not a real process on any platform we run tests on.
    expect(engine.isAlive(999999999)).toBe(false);
  });
});

describe("fileSize", () => {
  it("returns the file size in bytes", () => {
    vi.mocked(fs.statSync).mockReturnValue({ size: 12345 } as ReturnType<
      typeof fs.statSync
    >);
    expect(engine.fileSize("/tmp/whatever")).toBe(12345);
  });

  it("returns 0 when the path doesn't exist", () => {
    vi.mocked(fs.statSync).mockImplementation(() => {
      throw new Error("ENOENT");
    });
    expect(engine.fileSize("/tmp/missing")).toBe(0);
  });
});
