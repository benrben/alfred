import { afterEach, describe, expect, it, vi } from "vitest";
import { homedir } from "node:os";
import { join } from "node:path";

type Engine = typeof import("../engine");
type Stub = typeof import("./raycast-api.stub");

// engine.ts caches the contract in a module-level variable and reads prefs /
// clipboard from "@raycast/api" (aliased to raycast-api.stub). vi.resetModules()
// gives each test a fresh engine AND a fresh stub; we return BOTH from the same
// reset so a test mutates the very stub instance the engine reads. Defaults are
// reset to "unset" so backend/translate toggles don't bleed between tests.
async function freshEngine(): Promise<{ engine: Engine; stub: Stub }> {
  vi.resetModules();
  const engine = (await import("../engine")) as Engine;
  const stub = (await import("./raycast-api.stub")) as Stub;
  stub.mockPrefs.daemonPort = "";
  stub.mockPrefs.backend = "default";
  stub.mockPrefs.translate = "default";
  stub.setClipboardText("");
  return { engine, stub };
}

afterEach(() => {
  vi.restoreAllMocks();
});

// The CONTRACT fixture the engine emits (from GET /contract or the `contract`
// CLI). Distinctive history dir proves derivation-from-contract, not a
// coincidental match with the literal fallback.
const CONTRACT_FIXTURE = {
  schema_version: 1,
  daemon: { host: "127.0.0.1", port: 9999, url: "http://127.0.0.1:9999/" },
  status_line: {
    sentinel: "VB_STATUS",
    sep: "\t",
    kinds: {
      copied: [],
      saved: ["path"],
      empty: [],
      streaming: [],
      error: ["subtype"],
    },
    llm_failed_suffix: "llm_failed",
  },
  files: {
    progress: { path: "~/.voicebridge/progress.json" },
    stream: { path: "~/.voicebridge/stream.json" },
    history: { path: "~/custom/history/history.jsonl" },
  },
  config_search: ["~/.config/voicebridge/config.toml"],
};

describe("parseStatus", () => {
  it("parses a bare 'copied' status", async () => {
    const { engine } = await freshEngine();
    expect(engine.parseStatus("VB_STATUS\tcopied")).toEqual(["copied"]);
  });

  it("parses 'saved' with a path field", async () => {
    const { engine } = await freshEngine();
    expect(engine.parseStatus("VB_STATUS\tsaved\t/p.md")).toEqual([
      "saved",
      "/p.md",
    ]);
  });

  it("returns null when no status line is present", async () => {
    const { engine } = await freshEngine();
    expect(engine.parseStatus("just some output\nno sentinel here")).toBeNull();
  });

  it("finds the status line among other output lines", async () => {
    const { engine } = await freshEngine();
    const out = "transcribing…\nVB_STATUS\tsaved\t/tmp/out.md\ndone";
    expect(engine.parseStatus(out)).toEqual(["saved", "/tmp/out.md"]);
  });
});

describe("buildFormats / flagsForFormat", () => {
  it("buildFormats puts config + raw first, then a row per mode", async () => {
    const { engine } = await freshEngine();
    const modes = [
      { key: "email", label: "Email", description: "polish", prompt: "" },
      { key: "commit", label: "Commit", description: "git", prompt: "" },
    ];
    const formats = engine.buildFormats(modes);
    expect(formats.map((f) => f.id)).toEqual([
      engine.CONFIG_FORMAT_ID,
      engine.RAW_FORMAT_ID,
      "email",
      "commit",
    ]);

    const config = formats[0];
    expect(config.ai).toBe(true);
    expect(config.flags).toEqual([]);

    const raw = formats[1];
    expect(raw.ai).toBe(false);
    expect(raw.flags).toEqual([
      "--no-rewrite",
      "--no-translate",
      "--no-optimize",
    ]);

    const email = formats[2];
    expect(email.title).toBe("Email");
    expect(email.ai).toBe(true);
    expect(email.flags).toEqual(["--mode", "email", "--rewrite"]);
  });

  it("flagsForFormat: a mode format with no overrides yields just its flags", async () => {
    const { engine } = await freshEngine();
    const fmt = engine.buildFormats([
      { key: "email", label: "Email", description: "", prompt: "" },
    ])[2];
    expect(engine.flagsForFormat(fmt)).toEqual([
      "--mode",
      "email",
      "--rewrite",
    ]);
  });

  it("flagsForFormat: backend + translate overrides are layered on an AI format", async () => {
    const { engine } = await freshEngine();
    const fmt = engine.buildFormats([
      { key: "email", label: "Email", description: "", prompt: "" },
    ])[2];
    expect(
      engine.flagsForFormat(fmt, { translate: "on", backend: "claude" }),
    ).toEqual([
      "--mode",
      "email",
      "--rewrite",
      "--translate",
      "--backend",
      "claude",
    ]);
  });

  it("flagsForFormat: a raw (non-AI) format ignores a translate toggle", async () => {
    const { engine } = await freshEngine();
    const raw = engine.buildFormats([])[1]; // RAW
    // raw already pins --no-translate; an "on" toggle must not contradict it.
    expect(engine.flagsForFormat(raw, { translate: "on" })).toEqual([
      "--no-rewrite",
      "--no-translate",
      "--no-optimize",
    ]);
  });

  it("flagsForFormat: backend/translate come from prefs when no override given", async () => {
    const { engine, stub } = await freshEngine();
    stub.mockPrefs.backend = "codex";
    stub.mockPrefs.translate = "off";
    const fmt = engine.buildFormats([
      { key: "email", label: "Email", description: "", prompt: "" },
    ])[2];
    expect(engine.flagsForFormat(fmt)).toEqual([
      "--mode",
      "email",
      "--rewrite",
      "--no-translate",
      "--backend",
      "codex",
    ]);
  });
});

describe("resolveDelivery", () => {
  it("copied -> reads the clipboard text", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("hello world");
    const res = { code: 0, out: "VB_STATUS\tcopied", err: "" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "copied",
      text: "hello world",
      llmFailed: false,
    });
  });

  it("saved -> returns the path from the status line", async () => {
    const { engine } = await freshEngine();
    const res = { code: 0, out: "VB_STATUS\tsaved\t/nope/missing.md", err: "" };
    const d = await engine.resolveDelivery(res);
    expect(d.kind).toBe("saved");
    expect(d.path).toBe("/nope/missing.md");
    expect(d.text).toBeUndefined(); // file doesn't exist -> no text read
    expect(d.llmFailed).toBe(false);
  });

  it("flags llm_failed from the trailing status field", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("partial");
    const res = { code: 0, out: "VB_STATUS\tcopied\tllm_failed", err: "" };
    const d = await engine.resolveDelivery(res);
    expect(d.kind).toBe("copied");
    expect(d.llmFailed).toBe(true);
  });

  it("no status line + nonzero code -> error", async () => {
    const { engine } = await freshEngine();
    const res = { code: 1, out: "boom", err: "stack" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "error",
      llmFailed: false,
    });
  });

  it("no status line + zero code -> unknown", async () => {
    const { engine } = await freshEngine();
    const res = { code: 0, out: "no sentinel", err: "" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "unknown",
      llmFailed: false,
    });
  });
});

describe("contract path-derivation", () => {
  it("contractPath expands ~ to homedir for each file key", async () => {
    const { engine } = await freshEngine();
    expect(engine.contractPath(CONTRACT_FIXTURE, "progress")).toBe(
      join(homedir(), ".voicebridge", "progress.json"),
    );
    expect(engine.contractPath(CONTRACT_FIXTURE, "stream")).toBe(
      join(homedir(), ".voicebridge", "stream.json"),
    );
    expect(engine.contractPath(CONTRACT_FIXTURE, "history")).toBe(
      join(homedir(), "custom", "history", "history.jsonl"),
    );
  });

  it("progress/stream/historyFile derive from a loaded contract (GET /contract)", async () => {
    // Serve the fixture from GET /contract so loadContract() caches it, then
    // assert the synchronous *File() wrappers resolve from that cached contract.
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify(CONTRACT_FIXTURE), { status: 200 }),
      );

    const { engine } = await freshEngine();
    await engine.loadContract();

    expect(engine.progressFile()).toBe(
      join(homedir(), ".voicebridge", "progress.json"),
    );
    expect(engine.streamFile()).toBe(
      join(homedir(), ".voicebridge", "stream.json"),
    );
    expect(engine.historyFile()).toBe(
      join(homedir(), "custom", "history", "history.jsonl"),
    );
    // port comes from the contract when no daemonPort pref is set
    expect(engine.daemonPort()).toBe("9999");
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/contract"),
      expect.anything(),
    );
  });

  it("falls back to the literal paths when no contract is loaded (cold cache)", async () => {
    const { engine } = await freshEngine();
    // No loadContract() call -> currentContract() is the literal fallback,
    // whose paths match the historical hard-coded values.
    expect(engine.progressFile()).toBe(
      join(homedir(), ".voicebridge", "progress.json"),
    );
    expect(engine.streamFile()).toBe(
      join(homedir(), ".voicebridge", "stream.json"),
    );
    expect(engine.historyFile()).toBe(
      join(homedir(), ".voicebridge", "history", "history.jsonl"),
    );
    expect(engine.daemonPort()).toBe("8763");
  });

  it("a daemonPort pref overrides the contract's port", async () => {
    const { engine, stub } = await freshEngine();
    stub.mockPrefs.daemonPort = "7000";
    expect(engine.daemonPort()).toBe("7000");
  });
});
