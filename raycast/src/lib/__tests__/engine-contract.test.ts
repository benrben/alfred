// Coverage for engine-contract.ts's daemon-calling surface: recorderArgs's
// literal-fallback branch, the contract-discovery chain (parseContract,
// fetchContractFromDaemon/fetchContractFromCli via loadContract), pingDaemon,
// callEngine, runOneShot and startDaemon.
//
// callEngine/runOneShot/startDaemon spawn a real child process and hit
// `fetch` — the two side effects this file must never let escape into the
// real system (this repo IS a candidate `resolveScript()` location, so an
// unmocked spawn would run the actual voicebridge.py). Every test therefore
// mocks both `node:child_process`'s `spawn` (via a fake EventEmitter-based
// child, scripted through queued microtasks so listeners are attached before
// events fire) and `global.fetch`, following the `vi.spyOn(globalThis,
// "fetch")` convention already used in engine.test.ts. `vi.resetModules()`
// per test isolates engine-contract.ts's module-level contract cache, mirroring
// engine.test.ts's own freshEngine() helper.
import { afterEach, describe, expect, it, vi } from "vitest";
import { EventEmitter } from "node:events";
import type { ChildProcess } from "node:child_process";

vi.mock("node:child_process", async (importOriginal) => {
  const actual = await importOriginal<typeof import("node:child_process")>();
  return { ...actual, spawn: vi.fn() };
});

type Engine = typeof import("../engine");
type SpawnMock = ReturnType<typeof vi.fn>;

interface FakeChild {
  child: ChildProcess;
  stdout: EventEmitter;
  stderr: EventEmitter;
}

/** A minimal stand-in for Node's ChildProcess: an EventEmitter (for
 * 'error'/'close', as runOneShot listens for) plus stdout/stderr sub-emitters
 * (for their 'data' events) and a no-op unref() (startDaemon calls it). */
function fakeChildProcess(): FakeChild {
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  const child = new EventEmitter() as unknown as ChildProcess;
  Object.assign(child, { stdout, stderr, unref: () => undefined });
  return { child, stdout, stderr };
}

/** Every spawn() call not explicitly scripted by a test resolves harmlessly:
 * a `serve` (startDaemon) call gets an inert child; any other (one-shot) call
 * closes quickly with a nonzero code and no output. Keeps incidental spawns
 * (e.g. the fire-and-forget contract warm-up inside callEngine) from hanging
 * or reaching the real spawn(). */
function installBenignDefaultSpawn(spawnMock: SpawnMock): void {
  spawnMock.mockImplementation((_cmd: string, args: string[]) => {
    const { child } = fakeChildProcess();
    if (!args.includes("serve")) {
      queueMicrotask(() => child.emit("close", 1));
    }
    return child;
  });
}

interface OneShotScript {
  stdout?: string;
  stderr?: string;
  closeCode?: number;
  errorEvent?: Error;
  throwSync?: boolean;
}

/** Script spawn()'s behaviour for the one-shot (non-`serve`) call: a `serve`
 * call (from startDaemon) still gets an inert child so it can't hang the
 * test. The one-shot call plays back the given stdout/stderr/close/error
 * sequence via a queued microtask, so it fires only after runOneShot has
 * synchronously attached its listeners. */
function scriptOneShotSpawn(spawnMock: SpawnMock, script: OneShotScript): void {
  spawnMock.mockImplementation((_cmd: string, args: string[]) => {
    const { child, stdout, stderr } = fakeChildProcess();
    if (args.includes("serve")) return child;
    if (script.throwSync) throw new Error("spawn ENOENT");
    queueMicrotask(() => {
      if (script.stdout) stdout.emit("data", script.stdout);
      if (script.stderr) stderr.emit("data", script.stderr);
      if (script.errorEvent) child.emit("error", script.errorEvent);
      else child.emit("close", script.closeCode ?? 0);
    });
    return child;
  });
}

async function freshEngine(): Promise<{
  engine: Engine;
  spawnMock: SpawnMock;
}> {
  vi.resetModules();
  const engine = (await import("../engine")) as Engine;
  const cp = (await import("node:child_process")) as unknown as {
    spawn: SpawnMock;
  };
  cp.spawn.mockReset();
  installBenignDefaultSpawn(cp.spawn);
  return { engine, spawnMock: cp.spawn };
}

afterEach(() => {
  vi.restoreAllMocks();
});

function validContractJson(overrides: { port?: number } = {}): string {
  return JSON.stringify({
    schema_version: 1,
    daemon: {
      host: "127.0.0.1",
      port: overrides.port ?? 8763,
      url: `http://127.0.0.1:${overrides.port ?? 8763}/`,
    },
    status_line: { sentinel: "VB_STATUS", sep: "\t" },
    files: {
      progress: { path: "~/.voicebridge/progress.json" },
      stream: { path: "~/.voicebridge/stream.json" },
      history: { path: "~/.voicebridge/history/history.jsonl" },
    },
  });
}

/** A fetch mock that answers GET /contract with a valid contract (so
 * callEngine's fire-and-forget loadContract() resolves quietly via the
 * daemon, never touching the CLI/spawn fallback) and answers every other
 * path with `body`. A fresh Response per call — reusing one Response across
 * calls throws "body already read". */
function fetchServingContractAnd(
  body: () => Response,
): (input: RequestInfo | URL) => Promise<Response> {
  return async (input: RequestInfo | URL) => {
    if (String(input).includes("/contract")) {
      return new Response(validContractJson(), { status: 200 });
    }
    return body();
  };
}

describe("recorderArgs", () => {
  it("falls back to the literal sox_args when the loaded contract has none", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () =>
        new Response(
          JSON.stringify({
            schema_version: 1,
            daemon: { host: "127.0.0.1", port: 8763 },
            status_line: { sentinel: "VB_STATUS", sep: "\t" },
            files: {
              progress: { path: "~/.voicebridge/progress.json" },
              stream: { path: "~/.voicebridge/stream.json" },
              history: { path: "~/.voicebridge/history/history.jsonl" },
            },
            // no `audio` block — mirrors an older engine.
          }),
          { status: 200 },
        ),
    );
    const { engine } = await freshEngine();
    await engine.loadContract();
    expect(engine.recorderArgs("/tmp/y.wav")).toEqual([
      "-d",
      "-S",
      "-r",
      "16000",
      "-c",
      "1",
      "-b",
      "16",
      "/tmp/y.wav",
      "trim",
      "0",
      String(engine.MAX_RECORD_SECS),
    ]);
  });
});

describe("loadContract contract discovery (parseContract branches)", () => {
  it("daemon down + CLI fails -> the literal fallback contract is cached", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("ECONNREFUSED");
    });
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, { closeCode: 1 });

    const contract = await engine.loadContract();
    expect(contract).toEqual(engine.fallbackContract());
    expect(engine.currentContract()).toEqual(engine.fallbackContract());
  });

  it("daemon replies with malformed JSON but the CLI succeeds -> the CLI's contract wins", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response("not-json-at-all{{{", { status: 200 }),
    );
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, {
      stdout: validContractJson({ port: 5555 }),
      closeCode: 0,
    });

    const contract = await engine.loadContract();
    expect(contract.daemon.port).toBe(5555);
    expect(engine.currentContract().daemon.port).toBe(5555);
  });

  it("caches the contract: a second loadContract() call makes no further fetch/spawn calls", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(
        async () => new Response(validContractJson(), { status: 200 }),
      );
    const { engine, spawnMock } = await freshEngine();

    await engine.loadContract();
    const callsAfterFirst = fetchMock.mock.calls.length;
    await engine.loadContract();

    expect(fetchMock.mock.calls.length).toBe(callsAfterFirst);
    expect(spawnMock).not.toHaveBeenCalled();
  });

  it("de-dupes concurrent calls: a second loadContract() before the first resolves shares its in-flight promise", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(
        async () => new Response(validContractJson(), { status: 200 }),
      );
    const { engine } = await freshEngine();

    const [first, second] = await Promise.all([
      engine.loadContract(),
      engine.loadContract(),
    ]);

    expect(second).toBe(first);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("pingDaemon", () => {
  it("resolves true when the daemon replies ok", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response("", { status: 200 }),
    );
    const { engine } = await freshEngine();
    expect(await engine.pingDaemon()).toBe(true);
  });

  it("resolves false when the daemon replies with a non-ok status", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      async () => new Response("", { status: 500 }),
    );
    const { engine } = await freshEngine();
    expect(await engine.pingDaemon()).toBe(false);
  });

  it("resolves false when the daemon is unreachable", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("ECONNREFUSED");
    });
    const { engine } = await freshEngine();
    expect(await engine.pingDaemon()).toBe(false);
  });
});

describe("callEngine", () => {
  it("returns the daemon's parsed response when the POST succeeds", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchServingContractAnd(
        () =>
          new Response(
            JSON.stringify({ code: 0, out: "hello", err: "stderr text" }),
            { status: 200 },
          ),
      ),
    );
    const { engine, spawnMock } = await freshEngine();

    const result = await engine.callEngine(["status"]);

    expect(result).toEqual({ code: 0, out: "hello", err: "stderr text" });
    // The daemon succeeded, so callEngine never falls back to spawning.
    expect(spawnMock).not.toHaveBeenCalled();
  });

  it("defaults a missing code/out/err (older daemon reply) to 0/''/''", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchServingContractAnd(
        () => new Response(JSON.stringify({}), { status: 200 }),
      ),
    );
    const { engine } = await freshEngine();

    expect(await engine.callEngine(["status"])).toEqual({
      code: 0,
      out: "",
      err: "",
    });
  });

  it("posts argv as JSON to the daemon root", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchServingContractAnd(
        () =>
          new Response(JSON.stringify({ code: 0, out: "", err: "" }), {
            status: 200,
          }),
      ),
    );
    const { engine } = await freshEngine();

    await engine.callEngine(["text", "hello world"]);

    const postCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith(":8763/"),
    );
    expect(postCall).toBeDefined();
    const [, init] = postCall as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect(init.body).toBe(JSON.stringify({ argv: ["text", "hello world"] }));
  });

  it("falls back to a one-shot spawn (and warms the daemon) when the POST replies non-ok", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchServingContractAnd(() => new Response("", { status: 500 })),
    );
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, { stdout: "one-shot output", closeCode: 0 });

    const result = await engine.callEngine(["status"]);

    expect(result).toEqual({ code: 0, out: "one-shot output", err: "" });
    const serveCall = spawnMock.mock.calls.find((c) =>
      (c[1] as string[]).includes("serve"),
    );
    expect(serveCall).toBeDefined(); // startDaemon warmed it for next time
  });

  it("falls back to a one-shot spawn when the daemon is unreachable (fetch throws)", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(
      fetchServingContractAnd(() => {
        throw new Error("ECONNREFUSED");
      }),
    );
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, { stdout: "fallback output", closeCode: 0 });

    expect(await engine.callEngine(["status"])).toEqual({
      code: 0,
      out: "fallback output",
      err: "",
    });
  });
});

describe("runOneShot (exercised via callEngine's fallback)", () => {
  it("accumulates multi-chunk stdout/stderr and resolves with the close code", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("daemon down");
    });
    const { engine, spawnMock } = await freshEngine();
    spawnMock.mockImplementation((_cmd: string, args: string[]) => {
      const { child, stdout, stderr } = fakeChildProcess();
      if (args.includes("serve")) return child;
      queueMicrotask(() => {
        stdout.emit("data", "chunk1-");
        stdout.emit("data", "chunk2");
        stderr.emit("data", "warn1-");
        stderr.emit("data", "warn2");
        child.emit("close", 3);
      });
      return child;
    });

    expect(await engine.callEngine(["status"])).toEqual({
      code: 3,
      out: "chunk1-chunk2",
      err: "warn1-warn2",
    });
  });

  it("resolves a synthetic error result (appended to any captured stderr) on the child's 'error' event", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("daemon down");
    });
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, {
      stderr: "partial stderr\n",
      errorEvent: new Error("ENOENT: no such file"),
    });

    const result = await engine.callEngine(["status"]);
    expect(result.code).toBe(1);
    expect(result.err).toContain("partial stderr");
    expect(result.err).toContain("ENOENT: no such file");
  });

  it("defaults a null close code (killed by signal) to 0", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("daemon down");
    });
    const { engine, spawnMock } = await freshEngine();
    spawnMock.mockImplementation((_cmd: string, args: string[]) => {
      const { child } = fakeChildProcess();
      if (args.includes("serve")) return child;
      queueMicrotask(() => child.emit("close", null));
      return child;
    });

    expect(await engine.callEngine(["status"])).toEqual({
      code: 0,
      out: "",
      err: "",
    });
  });

  it("resolves a synthetic error result when spawn() itself throws synchronously", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async () => {
      throw new Error("daemon down");
    });
    const { engine, spawnMock } = await freshEngine();
    scriptOneShotSpawn(spawnMock, { throwSync: true });

    const result = await engine.callEngine(["status"]);
    expect(result.code).toBe(1);
    expect(result.out).toBe("");
    expect(result.err).toContain("spawn ENOENT");
  });
});

describe("startDaemon", () => {
  it("spawns the engine in serve mode, detached, and unref()s it", async () => {
    const { engine, spawnMock } = await freshEngine();
    const unref = vi.fn();
    spawnMock.mockImplementation(() => {
      const { child } = fakeChildProcess();
      Object.assign(child, { unref });
      return child;
    });

    engine.startDaemon();

    expect(spawnMock).toHaveBeenCalledTimes(1);
    const [, args, opts] = spawnMock.mock.calls[0] as [
      string,
      string[],
      { detached?: boolean; stdio?: string },
    ];
    expect(args).toEqual(expect.arrayContaining(["serve", "--port"]));
    expect(opts.detached).toBe(true);
    expect(opts.stdio).toBe("ignore");
    expect(unref).toHaveBeenCalledTimes(1);
  });

  it("swallows a spawn failure silently (best effort)", async () => {
    const { engine, spawnMock } = await freshEngine();
    spawnMock.mockImplementation(() => {
      throw new Error("EPERM");
    });

    expect(() => engine.startDaemon()).not.toThrow();
  });
});

describe("daemonUrl", () => {
  it("defaults to '/' and accepts an explicit path", async () => {
    const { engine } = await freshEngine();
    expect(engine.daemonUrl()).toBe("http://127.0.0.1:8763/");
    expect(engine.daemonUrl("/status")).toBe("http://127.0.0.1:8763/status");
  });
});
