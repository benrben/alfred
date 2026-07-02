import { describe, expect, it } from "vitest";
import type {
  FormatChoice,
  Processing,
  Progress,
  RecState,
  StreamState,
} from "../engine";
import {
  buildEngineStatusMarkdown,
  buildRecordingMarkdown,
  buildTranscribingMarkdown,
  composeResultMarkdown,
  deliveryFailure,
  engineErrorExcerpt,
  fmtMs,
  fmtTime,
  formatHistoryTitle,
  formatHistoryWhen,
  initialBanner,
  intentDetailMarkdown,
  levelBar,
  parseLevel,
  processingStages,
  refinedBanner,
  reprocessBanner,
  resolveFormat,
  resolveLiveTranscript,
  transcribingStatus,
  validateIntentKey,
} from "../view-logic";

// ---- dictate: time / level formatting -------------------------------------

describe("fmtTime", () => {
  it("zero-pads minutes and seconds", () => {
    expect(fmtTime(0)).toBe("00:00");
    expect(fmtTime(5)).toBe("00:05");
    expect(fmtTime(65)).toBe("01:05");
  });

  it("rolls minutes past 60s and keeps two-digit minutes", () => {
    expect(fmtTime(600)).toBe("10:00");
    expect(fmtTime(3599)).toBe("59:59");
  });
});

describe("fmtMs", () => {
  it("renders milliseconds as tenths-of-a-second", () => {
    expect(fmtMs(0)).toBe("0.0s");
    expect(fmtMs(1234)).toBe("1.2s");
    expect(fmtMs(1500)).toBe("1.5s");
  });

  it("clamps negative input to 0 (stale ts > now)", () => {
    expect(fmtMs(-500)).toBe("0.0s");
  });
});

describe("levelBar", () => {
  it("renders an empty bar at level 0 and a full bar at level 1", () => {
    expect(levelBar(0)).toBe("░".repeat(22));
    expect(levelBar(1)).toBe("█".repeat(22));
  });

  it("scales the fill to the width and rounds", () => {
    expect(levelBar(0.5, 10)).toBe("█████░░░░░");
    // round(0.55*10)=6
    expect(levelBar(0.55, 10)).toBe("██████░░░░");
  });

  it("clamps out-of-range levels", () => {
    expect(levelBar(2, 5)).toBe("█████");
    expect(levelBar(-1, 5)).toBe("░░░░░");
  });
});

describe("parseLevel", () => {
  it("returns 0 for empty input", () => {
    expect(parseLevel("")).toBe(0);
  });

  it("returns 0 when no bracketed |-segment is present", () => {
    expect(parseLevel("no meter here\njust text")).toBe(0);
  });

  it("computes fill fraction from the bracketed segment", () => {
    // segment "==|  " -> total 5, fill (non-space, non-'|') = 2 -> 0.4
    expect(parseLevel("In:0% [==|  ] Out")).toBeCloseTo(0.4, 5);
  });

  it("uses the most recent matching line (newest wins)", () => {
    const data = "[==========|]\n[|         ]";
    // last line: 9 spaces + '|' -> fill 0 of 10 -> 0
    expect(parseLevel(data)).toBe(0);
  });

  it("only scans the last 7 lines", () => {
    const old = "[==========|]"; // a full bar, far back
    const filler = Array(10).fill("plain line").join("\n");
    expect(parseLevel(`${old}\n${filler}`)).toBe(0);
  });

  it("counts every non-space, non-'|' char as fill (near-full segment)", () => {
    // "====|====" -> total 9, fill 8 (the '=' chars) -> 8/9
    expect(parseLevel("[====|====]")).toBeCloseTo(8 / 9, 5);
  });
});

// ---- dictate: live transcript + phase markdown ----------------------------

const REC: RecState = { pid: 1, wav: "/t.wav", startedAt: 1000, meter: "/m" };

describe("resolveLiveTranscript", () => {
  const stream = (over: Partial<StreamState>): StreamState => ({
    transcript: "hello",
    recording: true,
    done: false,
    ts: 2000,
    ...over,
  });

  it("returns the transcript when it belongs to the current recording", () => {
    expect(resolveLiveTranscript(stream({}), REC)).toBe("hello");
  });

  it("returns '' when the stream predates this recording", () => {
    expect(resolveLiveTranscript(stream({ ts: 500 }), REC)).toBe("");
  });

  it("returns '' for an empty transcript", () => {
    expect(resolveLiveTranscript(stream({ transcript: "" }), REC)).toBe("");
  });

  it("returns '' when there is no stream or no rec state", () => {
    expect(resolveLiveTranscript(null, REC)).toBe("");
    expect(resolveLiveTranscript(stream({}), null)).toBe("");
  });

  it("includes a transcript written exactly at startedAt (>= boundary)", () => {
    expect(resolveLiveTranscript(stream({ ts: 1000 }), REC)).toBe("hello");
  });
});

describe("transcribingStatus", () => {
  it("falls back to 'Transcribing…' with no progress", () => {
    expect(transcribingStatus(null)).toBe("Transcribing…");
  });

  it("uses the current step label while not done", () => {
    const p = {
      phase: "processing",
      label: "Cleaning up",
      ts: 0,
      start: 0,
      steps: [],
    };
    expect(transcribingStatus(p)).toBe("Cleaning up");
  });

  it("falls back to 'Transcribing…' once the phase is done", () => {
    const p = { phase: "done", label: "Done", ts: 0, start: 0, steps: [] };
    expect(transcribingStatus(p)).toBe("Transcribing…");
  });
});

describe("buildTranscribingMarkdown", () => {
  it("renders a 'Starting…' placeholder with no progress", () => {
    expect(buildTranscribingMarkdown(null, 5000)).toBe(
      "# ⏳ Working…\n\nStarting…",
    );
  });

  it("lists completed steps, the live step, and a running total", () => {
    const prog: Progress = {
      phase: "processing",
      label: "Cleaning up",
      ts: 4000,
      start: 1000,
      steps: [{ label: "Transcribe", ms: 2000 }],
    };
    const md = buildTranscribingMarkdown(prog, 5000);
    expect(md).toBe(
      [
        "# ⏳ Working…",
        "",
        "- ✅ Transcribe — `2.0s`",
        "- ⏳ **Cleaning up** — `1.0s`", // now - ts = 5000-4000
        "",
        "**Total** `4.0s`", // now - start = 5000-1000
      ].join("\n"),
    );
  });

  it("omits the live step row once the phase is done", () => {
    const prog: Progress = {
      phase: "done",
      label: "Done",
      ts: 4000,
      start: 1000,
      steps: [{ label: "Transcribe", ms: 2000 }],
    };
    const md = buildTranscribingMarkdown(prog, 5000);
    expect(md).not.toContain("⏳ **");
    expect(md).toContain("- ✅ Transcribe — `2.0s`");
    expect(md).toContain("**Total** `4.0s`");
  });
});

describe("buildRecordingMarkdown", () => {
  const aiFmt: FormatChoice = {
    id: "email",
    title: "Email",
    subtitle: "",
    ai: true,
    flags: [],
  };
  const rawFmt: FormatChoice = { ...aiFmt, id: "__raw__", ai: false };

  it("shows the timer, level bar and AI format, and no transcript block", () => {
    const md = buildRecordingMarkdown({
      elapsed: 65,
      level: 0,
      fmt: aiFmt,
      live: "",
    });
    expect(md).toContain("## 01:05");
    expect(md).toContain("`" + "░".repeat(22) + "`");
    expect(md).toContain("**Output:** Email  ·  ⌘F to change");
    expect(md).not.toContain("Transcript so far");
  });

  it("labels a non-AI format as a raw transcript", () => {
    const md = buildRecordingMarkdown({
      elapsed: 0,
      level: 0,
      fmt: rawFmt,
      live: "",
    });
    expect(md).toContain("**Output:** Raw transcript (no AI)  ·  ⌘F to change");
  });

  it("appends the live transcript block when present", () => {
    const md = buildRecordingMarkdown({
      elapsed: 0,
      level: 0,
      fmt: aiFmt,
      live: "the quick brown",
    });
    expect(md).toContain("**Transcript so far**");
    expect(md).toContain("the quick brown");
  });
});

// ---- history: list-item formatting ----------------------------------------

describe("formatHistoryTitle", () => {
  it("collapses whitespace and trims", () => {
    expect(formatHistoryTitle("  hello \n  world  ")).toBe("hello world");
  });

  it("truncates to 57 chars + ellipsis past 60 chars", () => {
    const text = "a".repeat(80);
    const out = formatHistoryTitle(text);
    expect(out).toBe("a".repeat(57) + "…");
    expect(out.length).toBe(58); // 57 + ellipsis
  });

  it("keeps a title exactly 60 chars long un-truncated", () => {
    const text = "a".repeat(60);
    expect(formatHistoryTitle(text)).toBe(text);
  });

  it("shows '(empty)' for blank text", () => {
    expect(formatHistoryTitle("   ")).toBe("(empty)");
    expect(formatHistoryTitle("")).toBe("(empty)");
  });
});

describe("formatHistoryWhen", () => {
  it("turns an ISO timestamp into 'YYYY-MM-DD HH:MM'", () => {
    expect(formatHistoryWhen("2026-06-26T14:35:09.123Z")).toBe(
      "2026-06-26 14:35",
    );
  });

  it("handles a missing/empty timestamp", () => {
    expect(formatHistoryWhen("")).toBe("");
    expect(formatHistoryWhen(undefined as unknown as string)).toBe("");
  });
});

// ---- ResultView: banners + markdown ---------------------------------------

describe("initialBanner", () => {
  it("prioritises the LLM-failure warning", () => {
    expect(
      initialBanner({ llmFailed: true, path: "/x.md", note: "Email" }),
    ).toBe("⚠️ LLM step failed — raw transcript below.");
  });

  it("shows the saved path when not failed", () => {
    expect(initialBanner({ path: "/out.md", note: "Email" })).toBe(
      "💾 Saved to `/out.md`",
    );
  });

  it("warns about a failed auto-paste (below the LLM warning)", () => {
    expect(initialBanner({ pasteFailed: true, note: "Email" })).toMatch(
      /Couldn't auto-paste/,
    );
    // LLM failure still wins over paste failure.
    expect(initialBanner({ llmFailed: true, pasteFailed: true })).toBe(
      "⚠️ LLM step failed — raw transcript below.",
    );
  });

  it("falls back to the note, then empty string", () => {
    expect(initialBanner({ note: "Email" })).toBe("Email");
    expect(initialBanner({})).toBe("");
  });
});

describe("reprocessBanner", () => {
  it("names the format for an AI run", () => {
    const fmt: FormatChoice = {
      id: "email",
      title: "Email",
      subtitle: "",
      ai: true,
      flags: [],
    };
    expect(reprocessBanner(fmt)).toBe("↻ Reprocessed as Email");
  });

  it("uses a raw-transcript banner for a non-AI run", () => {
    const fmt: FormatChoice = {
      id: "__raw__",
      title: "Raw",
      subtitle: "",
      ai: false,
      flags: [],
    };
    expect(reprocessBanner(fmt)).toBe("↻ Raw transcript");
  });
});

describe("refinedBanner", () => {
  it("echoes the instruction", () => {
    expect(refinedBanner("make it shorter")).toBe("✎ Refined: make it shorter");
  });
});

describe("composeResultMarkdown", () => {
  it("prepends a blockquote banner and appends the footer", () => {
    const md = composeResultMarkdown({
      banner: "💾 Saved",
      text: "the body",
      canDictateAgain: false,
    });
    expect(md.startsWith("> 💾 Saved\n\nthe body\n")).toBe(true);
    expect(md).toContain("### Adjust this result");
    expect(md).not.toContain("Dictate again");
  });

  it("omits the banner line when there is no banner", () => {
    const md = composeResultMarkdown({
      banner: "",
      text: "body",
      canDictateAgain: true,
    });
    expect(md.startsWith("body\n")).toBe(true);
    expect(md).toContain("🎙 Dictate again `⌘D`");
  });

  it("uses the '_(empty)_' placeholder for empty text", () => {
    const md = composeResultMarkdown({
      banner: "",
      text: "",
      canDictateAgain: false,
    });
    expect(md.startsWith("_(empty)_\n")).toBe(true);
  });
});

// ---- PipelineForm: format resolution --------------------------------------

describe("resolveFormat", () => {
  const fmts: FormatChoice[] = [
    { id: "__config__", title: "Default", subtitle: "", ai: true, flags: [] },
    { id: "email", title: "Email", subtitle: "", ai: true, flags: [] },
  ];

  it("returns the matching format by id", () => {
    expect(resolveFormat(fmts, "email")?.id).toBe("email");
  });

  it("falls back to the first format when the id is unknown", () => {
    expect(resolveFormat(fmts, "nope")?.id).toBe("__config__");
  });

  it("returns null when no formats are loaded", () => {
    expect(resolveFormat([], "email")).toBeNull();
  });
});

// ---- delivery outcome: excerpt + failure classification -------------------

describe("engineErrorExcerpt", () => {
  it("prefers the last non-empty stderr line", () => {
    expect(
      engineErrorExcerpt({ out: "stuff", err: "warn\nreal error here\n" }),
    ).toBe("real error here");
  });

  it("falls back to the stdout tail when stderr is empty", () => {
    expect(
      engineErrorExcerpt({ out: "line 1\nlast out line\n", err: "" }),
    ).toBe("last out line");
  });

  it("returns a generic fallback when both are empty", () => {
    expect(engineErrorExcerpt({ out: "", err: "" })).toBe("unknown error");
  });
});

describe("deliveryFailure", () => {
  const res = { out: "boom-out", err: "boom-err" };

  it("returns null for delivered results (copied/saved)", () => {
    expect(deliveryFailure("copied", res)).toBeNull();
    expect(deliveryFailure("saved", res)).toBeNull();
  });

  it("labels an empty result", () => {
    expect(deliveryFailure("empty", res)).toEqual({
      title: "Nothing to process",
    });
  });

  it("surfaces an excerpt for 'error'", () => {
    expect(deliveryFailure("error", res)).toEqual({
      title: "Engine error",
      message: "boom-err",
    });
  });

  it("treats the exit-0 'unknown' kind as a failure (not an empty screen)", () => {
    expect(deliveryFailure("unknown", res)).toEqual({
      title: "Engine error",
      message: "boom-err",
    });
  });
});

// ---- manage-intents: key validation + detail markdown ---------------------

describe("validateIntentKey", () => {
  it("accepts a valid key on submit", () => {
    expect(validateIntentKey("stand_up-1")).toBeUndefined();
  });

  it("requires a non-empty key on submit", () => {
    expect(validateIntentKey("   ")).toBe("A key is required.");
  });

  it("rejects illegal characters on submit", () => {
    expect(validateIntentKey("bad key!")).toBe(
      "Use letters, numbers, - or _ only.",
    );
  });

  it("allows an empty value while typing (allowEmpty)", () => {
    expect(validateIntentKey("", { allowEmpty: true })).toBeUndefined();
  });

  it("rejects illegal characters while typing", () => {
    expect(validateIntentKey("a b", { allowEmpty: true })).toBe(
      "letters/numbers/-/_ only",
    );
  });
});

describe("intentDetailMarkdown", () => {
  it("stars the default and italicises the subtitle", () => {
    const md = intentDetailMarkdown({
      title: "Email",
      isDefault: true,
      subtitle: "polish it",
      body: "the prompt",
    });
    expect(md).toContain("# Email  ⭐️");
    expect(md).toContain("_polish it_");
    expect(md).toContain("the prompt");
  });

  it("omits the star when not the default", () => {
    const md = intentDetailMarkdown({
      title: "Email",
      isDefault: false,
      subtitle: "",
      body: "b",
    });
    expect(md).toContain("# Email");
    expect(md).not.toContain("⭐️");
  });
});

// ---- engine-status: stages + markdown -------------------------------------

describe("processingStages", () => {
  it("returns [] for no processing block", () => {
    expect(processingStages(null)).toEqual([]);
    expect(processingStages(undefined)).toEqual([]);
  });

  it("lists only the enabled stages, with the translate route", () => {
    const p: Processing = {
      mode: "email",
      rewrite: true,
      translate: true,
      optimize: false,
      translate_via: "llm",
    };
    expect(processingStages(p)).toEqual(["rewrite", "translate (llm)"]);
  });
});

describe("buildEngineStatusMarkdown", () => {
  const aiFmt: FormatChoice = {
    id: "email",
    title: "Email",
    subtitle: "",
    ai: true,
    flags: [],
  };

  it("renders daemon/format/stages/backend/paths and the doctor report", () => {
    const md = buildEngineStatusMarkdown({
      port: "8763",
      up: true,
      fmt: aiFmt,
      stages: ["rewrite"],
      backend: "claude",
      claudeModel: "sonnet",
      script: "/x/voicebridge.py",
      python: "/x/.venv/bin/python3",
      report: "all good",
    });
    expect(md).toContain("`127.0.0.1:8763`): 🟢 up");
    expect(md).toContain("🪄 Email");
    expect(md).toContain("**Stages on**: rewrite");
    expect(md).toContain("**Backend**: claude · claude=sonnet");
    expect(md).toContain("all good");
    expect(md).not.toContain(">"); // no warning blockquote
  });

  it("shows a down daemon, no-stages placeholder, and dash format", () => {
    const md = buildEngineStatusMarkdown({
      port: "9",
      up: false,
      fmt: null,
      stages: [],
      script: "s",
      python: "p",
      report: "r",
    });
    expect(md).toContain("🔴 down");
    expect(md).toContain("none (pure transcription)");
    expect(md).toContain("**Default output**: —");
  });

  it("surfaces a schema-mismatch warning as a top blockquote", () => {
    const md = buildEngineStatusMarkdown({
      port: "8763",
      up: true,
      fmt: aiFmt,
      stages: [],
      script: "s",
      python: "p",
      report: "r",
      warning: "⚠️ out of sync",
    });
    expect(md).toContain("> ⚠️ out of sync");
  });
});
