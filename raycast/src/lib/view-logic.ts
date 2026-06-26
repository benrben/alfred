/**
 * Pure presentation/transform logic extracted from the Raycast VIEW commands so
 * it can be unit-tested without a component-render harness. Every function here
 * is a pure transform (no I/O, no React, no @raycast/api) — the views import
 * these and keep only their JSX glue. Behaviour is identical to the inline code
 * these were lifted from.
 */
import type { FormatChoice, Progress, StreamState, RecState } from "./engine";

// ---- dictate.tsx: time / level formatting ---------------------------------

/** Elapsed seconds as a zero-padded mm:ss clock. */
export function fmtTime(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** A millisecond duration as a "1.2s" tenths-of-a-second label (clamped ≥ 0). */
export function fmtMs(ms: number): string {
  return `${(Math.max(0, ms) / 1000).toFixed(1)}s`;
}

/** A unicode level meter: `level` (0..1) scaled to `width` filled blocks. */
export function levelBar(level: number, width = 22): string {
  const filled = Math.max(0, Math.min(width, Math.round(level * width)));
  return "█".repeat(filled) + "░".repeat(width - filled);
}

/**
 * Parse a sox `-S` VU-meter dump into a 0..1 level. sox writes a bracketed
 * segment containing a '|' centre mark; the fraction of non-space, non-'|'
 * characters in the most recent such segment is the level. Scans the last few
 * lines (newest first) and returns 0 if none match.
 */
export function parseLevel(data: string): number {
  if (!data) return 0;
  const segs = data.split(/[\r\n]+/);
  for (let i = segs.length - 1; i >= 0 && i > segs.length - 8; i--) {
    const m = segs[i].match(/\[([^[\]]*\|[^[\]]*)\]/);
    if (m) {
      let fill = 0;
      let total = 0;
      for (const ch of m[1]) {
        total++;
        if (ch !== " " && ch !== "|") fill++;
      }
      if (total > 0) return Math.min(1, fill / total);
    }
  }
  return 0;
}

// ---- dictate.tsx: live transcript + phase markdown ------------------------

/**
 * The live partial transcript to show while recording: the stream's transcript,
 * but only if it belongs to the current recording (written at/after this
 * recording started) and is non-empty. Otherwise "".
 */
export function resolveLiveTranscript(
  stream: StreamState | null,
  st: RecState | null,
): string {
  return stream && st && stream.ts >= st.startedAt && stream.transcript
    ? stream.transcript
    : "";
}

/**
 * The "⏳ Working…" Detail markdown for the transcribing phase. Pure given the
 * progress snapshot and `now` (epoch ms): completed steps with their durations,
 * the live current step with a running stopwatch, and a running total.
 */
export function buildTranscribingMarkdown(
  prog: Progress | null,
  now: number,
): string {
  const current = prog && prog.phase !== "done" ? prog.label : "";
  const lines: string[] = ["# ⏳ Working…", ""];
  if (prog) {
    for (const s of prog.steps) {
      lines.push(`- ✅ ${s.label} — \`${fmtMs(s.ms)}\``);
    }
    if (current) {
      lines.push(`- ⏳ **${current}** — \`${fmtMs(now - prog.ts)}\``);
    }
    lines.push("", `**Total** \`${fmtMs(now - prog.start)}\``);
  } else {
    lines.push("Starting…");
  }
  return lines.join("\n");
}

/** Navigation-title status text for the transcribing phase. */
export function transcribingStatus(prog: Progress | null): string {
  const current = prog && prog.phase !== "done" ? prog.label : "";
  return current || "Transcribing…";
}

/**
 * The "🔴 Recording" Detail markdown: live timer, level bar, the chosen output
 * format, an optional live transcript, and the key hints. Pure given the
 * elapsed seconds, level, chosen format and (optional) live transcript.
 */
export function buildRecordingMarkdown(args: {
  elapsed: number;
  level: number;
  fmt: FormatChoice;
  live: string;
}): string {
  const { elapsed, level, fmt, live } = args;
  return [
    "# 🔴 Recording",
    "",
    `## ${fmtTime(elapsed)}`,
    "",
    `\`${levelBar(level)}\``,
    "",
    `**Output:** ${fmt.ai ? fmt.title : "Raw transcript (no AI)"}  ·  ⌘F to change`,
    ...(live ? ["", "---", "**Transcript so far**", "", live] : []),
    "",
    "**⏎** stop & transcribe · **⌃C** cancel · **Esc** keeps recording (reopen to stop).",
  ].join("\n");
}

// ---- history.tsx: list-item formatting ------------------------------------

/**
 * The List.Item title for a history record: whitespace-collapsed, trimmed, then
 * truncated to 60 chars (57 + "…"), with "(empty)" as the placeholder.
 */
export function formatHistoryTitle(text: string): string {
  const title = text.replace(/\s+/g, " ").trim();
  return title.length > 60 ? title.slice(0, 57) + "…" : title || "(empty)";
}

/** A history record's ISO timestamp as a "YYYY-MM-DD HH:MM" label. */
export function formatHistoryWhen(ts: string): string {
  return (ts || "").replace("T", " ").slice(0, 16);
}

// ---- ResultView.tsx: banner + markdown composition ------------------------

/**
 * The banner shown when the result screen first opens: an LLM-failure warning,
 * else the saved-path note, else the caller's note (or empty).
 */
export function initialBanner(args: {
  llmFailed?: boolean;
  path?: string;
  note?: string;
}): string {
  const { llmFailed, path, note } = args;
  return llmFailed
    ? "⚠️ LLM step failed — raw transcript below."
    : path
      ? `💾 Saved to \`${path}\``
      : (note ?? "");
}

/** Banner after a successful "Reprocess as…": format title, or raw note. */
export function reprocessBanner(fmt: FormatChoice): string {
  return fmt.ai ? `↻ Reprocessed as ${fmt.title}` : "↻ Raw transcript";
}

/** Banner after a successful free-text refine: echoes the instruction. */
export function refinedBanner(instruction: string): string {
  return `✎ Refined: ${instruction}`;
}

/**
 * The full result-screen markdown: an optional blockquote banner, the body (or
 * an "_(empty)_" placeholder), and the "Adjust this result" footer (whose
 * "Dictate again" hint only appears when that action is available).
 */
export function composeResultMarkdown(args: {
  banner: string;
  text: string;
  canDictateAgain: boolean;
}): string {
  const { banner, text, canDictateAgain } = args;
  const adjust = [
    "",
    "---",
    "### Adjust this result",
    "- ✎ **Refine with feedback** — `⌘E`  ·  tell it what to change",
    "- ↻ **Change intent / format** — `⌘R`",
    `- 📋 Copy \`⌘C\`  ·  Paste \`⏎\`${canDictateAgain ? "  ·  🎙 Dictate again `⌘D`" : ""}`,
  ].join("\n");
  return `${banner ? `> ${banner}\n\n` : ""}${text || "_(empty)_"}\n${adjust}`;
}

// ---- PipelineForm.tsx: format resolution + error guard --------------------

/**
 * Resolve the submitted format id to a FormatChoice: the matching format, else
 * the first loaded one, else null (no formats loaded at all).
 */
export function resolveFormat(
  formats: FormatChoice[],
  id: string,
): FormatChoice | null {
  return formats.find((f) => f.id === id) ?? formats[0] ?? null;
}
