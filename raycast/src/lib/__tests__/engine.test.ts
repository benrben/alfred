import { afterEach, describe, expect, it, vi } from "vitest";
import { homedir, userInfo } from "node:os";
import { join } from "node:path";
import { existsSync, mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";

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
  vi.unstubAllEnvs();
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

describe("BACKENDS", () => {
  it("matches the engine's actual --backend choices, including 'local'", async () => {
    // Regression: PipelineForm's dropdown once hardcoded auto/claude/codex and
    // silently dropped "local" — the engine's actual default (on-device MLX,
    // no login, no network) — so the form could never select it. BACKENDS is
    // the one list every picker (here and the Hammerspoon front-end) draws
    // from now, so this single assertion guards them all.
    const { engine } = await freshEngine();
    expect(new Set(engine.BACKENDS)).toEqual(
      new Set(["local", "auto", "claude", "codex"]),
    );
  });
});

describe("loadModes", () => {
  it("returns the parsed array from the engine", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    vi.spyOn(contract, "callEngine").mockResolvedValue({
      code: 0,
      out: JSON.stringify([
        { key: "email", label: "Email", description: "d", prompt: "p" },
      ]),
      err: "",
    });
    expect(await engine.loadModes()).toEqual([
      { key: "email", label: "Email", description: "d", prompt: "p" },
    ]);
  });

  it("returns [] when the engine's output isn't valid JSON", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    vi.spyOn(contract, "callEngine").mockResolvedValue({
      code: 0,
      out: "not json",
      err: "",
    });
    expect(await engine.loadModes()).toEqual([]);
  });

  it("returns [] when the engine's output is valid JSON but not an array", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    vi.spyOn(contract, "callEngine").mockResolvedValue({
      code: 0,
      out: JSON.stringify({ not: "an array" }),
      err: "",
    });
    expect(await engine.loadModes()).toEqual([]);
  });
});

describe("loadSettings", () => {
  const settings = {
    backend: "auto",
    claude_model: "sonnet",
    codex_model: "",
    claude_models: [],
    codex_models: [],
    processing: {
      mode: "email",
      rewrite: true,
      translate: false,
      optimize: false,
      translate_via: "",
    },
  };

  it("returns the parsed settings object", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    vi.spyOn(contract, "callEngine").mockResolvedValue({
      code: 0,
      out: JSON.stringify(settings),
      err: "",
    });
    expect(await engine.loadSettings()).toEqual(settings);
  });

  it("returns null when the engine's output isn't valid JSON", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    vi.spyOn(contract, "callEngine").mockResolvedValue({
      code: 0,
      out: "not json",
      err: "",
    });
    expect(await engine.loadSettings()).toBeNull();
  });
});

describe("defaultFormatId", () => {
  const withProcessing = (
    over: Partial<{ mode: string; rewrite: boolean }>,
  ) => ({
    backend: "default",
    claude_model: "",
    codex_model: "",
    claude_models: [],
    codex_models: [],
    processing: {
      mode: "email",
      rewrite: true,
      translate: false,
      optimize: false,
      translate_via: "",
      ...over,
    },
  });

  it("returns RAW when settings are null", async () => {
    const { engine } = await freshEngine();
    expect(engine.defaultFormatId(null)).toBe(engine.RAW_FORMAT_ID);
  });

  it("returns RAW when rewrite is off", async () => {
    const { engine } = await freshEngine();
    expect(engine.defaultFormatId(withProcessing({ rewrite: false }))).toBe(
      engine.RAW_FORMAT_ID,
    );
  });

  it("returns the configured mode when rewrite is on", async () => {
    const { engine } = await freshEngine();
    expect(engine.defaultFormatId(withProcessing({ mode: "email" }))).toBe(
      "email",
    );
  });

  it("falls back to 'raw' when rewrite is on but mode is empty", async () => {
    const { engine } = await freshEngine();
    expect(engine.defaultFormatId(withProcessing({ mode: "" }))).toBe("raw");
  });
});

describe("normalizeBackend", () => {
  it("accepts every known backend unchanged", async () => {
    const { engine } = await freshEngine();
    for (const b of engine.BACKENDS) {
      expect(engine.normalizeBackend(b)).toBe(b);
    }
  });

  it("accepts 'default' unchanged", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeBackend("default")).toBe("default");
  });

  it("falls back to 'default' for an unrecognised value", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeBackend("bogus")).toBe("default");
  });

  it("falls back to 'default' when no value is given", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeBackend(undefined)).toBe("default");
  });
});

describe("normalizeTranslate", () => {
  it("accepts 'on', 'off' and 'default' unchanged", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeTranslate("on")).toBe("on");
    expect(engine.normalizeTranslate("off")).toBe("off");
    expect(engine.normalizeTranslate("default")).toBe("default");
  });

  it("falls back to 'default' for an unrecognised value", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeTranslate("bogus")).toBe("default");
  });

  it("falls back to 'default' when no value is given", async () => {
    const { engine } = await freshEngine();
    expect(engine.normalizeTranslate(undefined)).toBe("default");
  });
});

describe("setDefaultFormat", () => {
  it("persists a raw format with --no-rewrite and reports success", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    const spy = vi
      .spyOn(contract, "callEngine")
      .mockResolvedValue({ code: 0, out: "saved", err: "" });
    const ok = await engine.setDefaultFormat(engine.rawFormat());
    expect(spy).toHaveBeenCalledWith([
      "set-processing",
      "--mode",
      "raw",
      "--no-rewrite",
    ]);
    expect(ok).toBe(true);
  });

  it("persists a mode format with --rewrite and reports failure when not saved", async () => {
    const { engine } = await freshEngine();
    const contract = await import("../engine-contract");
    const spy = vi
      .spyOn(contract, "callEngine")
      .mockResolvedValue({ code: 0, out: "", err: "" });
    const fmt = engine.buildFormats([
      { key: "email", label: "Email", description: "", prompt: "" },
    ])[2];
    const ok = await engine.setDefaultFormat(fmt);
    expect(spy).toHaveBeenCalledWith([
      "set-processing",
      "--mode",
      "email",
      "--rewrite",
    ]);
    expect(ok).toBe(false);
  });
});

describe("engineEnv", () => {
  it("restores Codex lookup paths and its default auth home", async () => {
    const { engine } = await freshEngine();
    vi.stubEnv("PATH", "/raycast/bin");
    vi.stubEnv("CODEX_HOME", "");

    const env = engine.engineEnv();
    const pathEntries = env.PATH?.split(":") ?? [];

    expect(pathEntries[0]).toBe("/raycast/bin");
    expect(pathEntries).toContain(join(homedir(), ".codex/bin"));
    expect(pathEntries).toContain(
      join(homedir(), ".codex/packages/standalone/current/bin"),
    );
    expect(env.HOME).toBe(homedir());
    expect(env.CODEX_HOME).toBe(join(homedir(), ".codex"));
  });

  it("preserves an explicitly configured CODEX_HOME", async () => {
    const { engine } = await freshEngine();
    vi.stubEnv("CODEX_HOME", "/custom/codex-home");

    expect(engine.engineEnv().CODEX_HOME).toBe("/custom/codex-home");
  });

  it("falls back to userInfo()/defaults when USER, LOGNAME, LANG and LC_ALL are unset", async () => {
    const { engine } = await freshEngine();
    vi.stubEnv("USER", "");
    vi.stubEnv("LOGNAME", "");
    vi.stubEnv("LANG", "");
    vi.stubEnv("LC_ALL", "");

    const env = engine.engineEnv();
    expect(env.USER).toBe(userInfo().username);
    expect(env.LOGNAME).toBe(userInfo().username);
    expect(env.LANG).toBe("en_US.UTF-8");
    expect(env.LC_ALL).toBe("en_US.UTF-8");
    expect(env.PYTHONUTF8).toBe("1");
  });

  it("passes through explicitly configured USER/LOGNAME/LANG/LC_ALL", async () => {
    const { engine } = await freshEngine();
    vi.stubEnv("USER", "alice");
    vi.stubEnv("LOGNAME", "alice-log");
    vi.stubEnv("LANG", "fr_FR.UTF-8");
    vi.stubEnv("LC_ALL", "fr_FR.UTF-8");

    const env = engine.engineEnv();
    expect(env.USER).toBe("alice");
    expect(env.LOGNAME).toBe("alice-log");
    expect(env.LANG).toBe("fr_FR.UTF-8");
    expect(env.LC_ALL).toBe("fr_FR.UTF-8");
  });
});

describe("resolveScript / candidateScripts", () => {
  it("prefers the configured engineScript path when it exists", async () => {
    const { engine, stub } = await freshEngine();
    const dir = mkdtempSync(join(tmpdir(), "alfred-script-"));
    const script = join(dir, "voicebridge.py");
    writeFileSync(script, "# stub");
    stub.mockPrefs.engineScript = script;

    expect(engine.resolveScript()).toBe(script);
  });

  it("skips a non-existent candidate and finds the next known location under HOME", async () => {
    const { engine, stub } = await freshEngine();
    const home = mkdtempSync(join(tmpdir(), "alfred-home-"));
    // Only the *second* known candidate ("alfred/voicebridge.py") exists here;
    // the first ("Claude/Projects/alfred/voicebridge.py") is left absent, so
    // resolveScript() must skip it and keep scanning.
    mkdirSync(join(home, "alfred"), { recursive: true });
    const found = join(home, "alfred", "voicebridge.py");
    writeFileSync(found, "# stub");
    vi.stubEnv("HOME", home);
    stub.mockPrefs.engineScript = "";

    expect(engine.resolveScript()).toBe(found);
    expect(
      existsSync(join(home, "Claude/Projects/alfred/voicebridge.py")),
    ).toBe(false);
  });

  it("falls back to the first known candidate path when nothing exists", async () => {
    const { engine, stub } = await freshEngine();
    const home = mkdtempSync(join(tmpdir(), "alfred-home-empty-"));
    vi.stubEnv("HOME", home);
    stub.mockPrefs.engineScript = "";

    expect(engine.resolveScript()).toBe(
      join(home, "Claude/Projects/alfred/voicebridge.py"),
    );
  });
});

describe("resolvePython", () => {
  it("uses the configured pythonBin when it exists", async () => {
    const { engine, stub } = await freshEngine();
    const dir = mkdtempSync(join(tmpdir(), "alfred-py-"));
    const py = join(dir, "python3");
    writeFileSync(py, "#!/bin/sh");
    stub.mockPrefs.pythonBin = py;

    expect(engine.resolvePython(join(dir, "voicebridge.py"))).toBe(py);
  });

  it("falls through to the venv beside the script when pythonBin is missing", async () => {
    const { engine, stub } = await freshEngine();
    const dir = mkdtempSync(join(tmpdir(), "alfred-py-missing-"));
    stub.mockPrefs.pythonBin = join(dir, "no-such-python");

    const venvDir = join(dir, ".venv", "bin");
    mkdirSync(venvDir, { recursive: true });
    const venvPy = join(venvDir, "python3");
    writeFileSync(venvPy, "#!/bin/sh");

    expect(engine.resolvePython(join(dir, "voicebridge.py"))).toBe(venvPy);
  });

  it("falls back to the literal 'python3' when neither pythonBin nor a venv exist", async () => {
    const { engine, stub } = await freshEngine();
    const dir = mkdtempSync(join(tmpdir(), "alfred-py-none-"));
    stub.mockPrefs.pythonBin = "";

    expect(engine.resolvePython(join(dir, "voicebridge.py"))).toBe("python3");
  });
});

describe("resolveDelivery", () => {
  it("copied -> reads the clipboard text (no VB_RESULT)", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("hello world");
    const res = { code: 0, out: "VB_STATUS\tcopied", err: "" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "copied",
      text: "hello world",
      llmFailed: false,
      pasteFailed: false,
    });
  });

  it("copied -> prefers the VB_RESULT line over the clipboard", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("stale clipboard");
    const out = `VB_RESULT\t${JSON.stringify("the delivered text")}\nVB_STATUS\tcopied`;
    const res = { code: 0, out, err: "" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "copied",
      text: "the delivered text",
      llmFailed: false,
      pasteFailed: false,
    });
  });

  it("saved -> prefers VB_RESULT text without reading the file", async () => {
    const { engine } = await freshEngine();
    const out = `VB_RESULT\t${JSON.stringify("body from result line")}\nVB_STATUS\tsaved\t/nope/missing.md`;
    const d = await engine.resolveDelivery({ code: 0, out, err: "" });
    expect(d.kind).toBe("saved");
    expect(d.path).toBe("/nope/missing.md");
    expect(d.text).toBe("body from result line");
  });

  it("flags paste_failed from the status line", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("x");
    const res = { code: 0, out: "VB_STATUS\tcopied\tpaste_failed", err: "" };
    const d = await engine.resolveDelivery(res);
    expect(d.kind).toBe("copied");
    expect(d.pasteFailed).toBe(true);
    expect(d.llmFailed).toBe(false);
  });

  it("flags both paste_failed and llm_failed together", async () => {
    const { engine, stub } = await freshEngine();
    stub.setClipboardText("x");
    const res = {
      code: 0,
      out: "VB_STATUS\tcopied\tpaste_failed\tllm_failed",
      err: "",
    };
    const d = await engine.resolveDelivery(res);
    expect(d.pasteFailed).toBe(true);
    expect(d.llmFailed).toBe(true);
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
      pasteFailed: false,
    });
  });

  it("no status line + zero code -> unknown", async () => {
    const { engine } = await freshEngine();
    const res = { code: 0, out: "no sentinel", err: "" };
    expect(await engine.resolveDelivery(res)).toEqual({
      kind: "unknown",
      llmFailed: false,
      pasteFailed: false,
    });
  });

  it("saved -> an existing but unreadable path (e.g. a directory) is treated as no text", async () => {
    const { engine } = await freshEngine();
    // A directory exists (existsSync -> true) but readFileSync on it throws
    // (EISDIR); the caught error must fall through to "no text", not throw.
    const dir = mkdtempSync(join(tmpdir(), "alfred-saved-dir-"));
    const res = { code: 0, out: `VB_STATUS\tsaved\t${dir}`, err: "" };
    const d = await engine.resolveDelivery(res);
    expect(d.kind).toBe("saved");
    expect(d.path).toBe(dir);
    expect(d.text).toBeUndefined();
  });

  it("compound error (deliver_failed + llm_failed) is still classified as error, llmFailed detected", async () => {
    // The raw-transcript fallback whose OWN delivery then also failed emits a
    // 3-part status line: error, deliver_failed, llm_failed. resolveDelivery
    // doesn't expose the subtype (callers show the generic engineErrorExcerpt
    // instead), but kind/llmFailed/pasteFailed must still classify correctly.
    const { engine } = await freshEngine();
    const res = {
      code: 1,
      out: "VB_STATUS\terror\tdeliver_failed\tllm_failed",
      err: "error: delivery failed (kept in history): disk full",
    };
    const d = await engine.resolveDelivery(res);
    expect(d.kind).toBe("error");
    expect(d.llmFailed).toBe(true);
    expect(d.pasteFailed).toBe(false);
  });
});

describe("parseResult", () => {
  it("decodes the JSON-encoded VB_RESULT line (newlines survive)", async () => {
    const { engine } = await freshEngine();
    const out = `noise\nVB_RESULT\t${JSON.stringify("line 1\nline 2")}\nVB_STATUS\tcopied`;
    expect(engine.parseResult(out)).toBe("line 1\nline 2");
  });

  it("returns null when no VB_RESULT line is present (older engine)", async () => {
    const { engine } = await freshEngine();
    expect(engine.parseResult("VB_STATUS\tcopied")).toBeNull();
  });

  it("returns null on a malformed (non-JSON) result payload", async () => {
    const { engine } = await freshEngine();
    expect(engine.parseResult("VB_RESULT\tnot json")).toBeNull();
  });
});

describe("lastErrorLine", () => {
  it("returns the last non-empty, trimmed stderr line", async () => {
    const { engine } = await freshEngine();
    expect(engine.lastErrorLine("first warning\n  real error  \n")).toBe(
      "real error",
    );
  });

  it("skips blank lines when picking the last one (filter callback: true then false)", async () => {
    const { engine } = await freshEngine();
    expect(engine.lastErrorLine("real error\n\n   \n")).toBe("real error");
  });

  it("falls back to 'unknown error' when there's nothing but blank lines", async () => {
    const { engine } = await freshEngine();
    expect(engine.lastErrorLine("\n   \n")).toBe("unknown error");
  });

  it("falls back to 'unknown error' for empty input", async () => {
    const { engine } = await freshEngine();
    expect(engine.lastErrorLine("")).toBe("unknown error");
  });
});

describe("daemonUrl", () => {
  it("builds on the resolved port with a default '/' path", async () => {
    const { engine } = await freshEngine();
    expect(engine.daemonUrl()).toBe("http://127.0.0.1:8763/");
    expect(engine.daemonUrl("/contract")).toBe(
      "http://127.0.0.1:8763/contract",
    );
  });

  it("honours a daemonPort preference", async () => {
    const { engine, stub } = await freshEngine();
    stub.mockPrefs.daemonPort = "7001";
    expect(engine.daemonUrl("/")).toBe("http://127.0.0.1:7001/");
  });
});

describe("schema_version compatibility", () => {
  it("the fallback contract matches the engine's current schema_version (1)", async () => {
    const { engine } = await freshEngine();
    expect(engine.fallbackContract().schema_version).toBe(1);
  });

  it("no warning when versions match", async () => {
    const { engine } = await freshEngine();
    expect(engine.schemaMismatchWarning(1, 1)).toBeNull();
  });

  it("warns (naming both versions) when the engine's version differs", async () => {
    const { engine } = await freshEngine();
    const w = engine.schemaMismatchWarning(1, 2);
    expect(w).toContain("v2");
    expect(w).toContain("v1");
    expect(w).toMatch(/out of sync/i);
  });

  it("no warning when either version is missing", async () => {
    const { engine } = await freshEngine();
    expect(engine.schemaMismatchWarning(0, 2)).toBeNull();
    expect(engine.schemaMismatchWarning(1, 0)).toBeNull();
  });

  it("loadContract stores a warning when the fetched contract's version differs", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ...CONTRACT_FIXTURE, schema_version: 2 }), {
        status: 200,
      }),
    );
    const { engine } = await freshEngine();
    expect(engine.contractSchemaWarning()).toBeNull(); // not computed yet
    await engine.loadContract();
    expect(engine.contractSchemaWarning()).toMatch(/out of sync/i);
  });
});

describe("resolvedPath", () => {
  it("prefers the contract's absolute `resolved` block (honours [history].dir)", async () => {
    const { engine } = await freshEngine();
    const withResolved = {
      ...CONTRACT_FIXTURE,
      resolved: {
        progress: "/abs/progress.json",
        stream: "/abs/stream.json",
        history: "/abs/history/history.jsonl",
      },
    };
    expect(engine.resolvedPath(withResolved, "history")).toBe(
      "/abs/history/history.jsonl",
    );
    expect(engine.resolvedPath(withResolved, "progress")).toBe(
      "/abs/progress.json",
    );
  });

  it("falls back to the file templates when there is no `resolved` block", async () => {
    const { engine } = await freshEngine();
    expect(engine.resolvedPath(CONTRACT_FIXTURE, "history")).toBe(
      join(homedir(), "custom", "history", "history.jsonl"),
    );
  });
});

describe("recorderArgs", () => {
  it("appends the wav + a recording-duration cap to the fallback sox_args", async () => {
    const { engine } = await freshEngine();
    expect(engine.recorderArgs("/tmp/x.wav")).toEqual([
      "-d",
      "-S",
      "-r",
      "16000",
      "-c",
      "1",
      "-b",
      "16",
      "/tmp/x.wav",
      "trim",
      "0",
      String(engine.MAX_RECORD_SECS),
    ]);
  });

  it("caps recordings generously (>= 30 min) so long dictation isn't cut off", async () => {
    const { engine } = await freshEngine();
    expect(engine.MAX_RECORD_SECS).toBeGreaterThanOrEqual(1800);
  });
});

describe("rawFormat", () => {
  it("is a non-AI format that pins every LLM stage off", async () => {
    const { engine } = await freshEngine();
    const raw = engine.rawFormat();
    expect(raw.id).toBe(engine.RAW_FORMAT_ID);
    expect(raw.ai).toBe(false);
    expect(raw.flags).toEqual([
      "--no-rewrite",
      "--no-translate",
      "--no-optimize",
    ]);
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
