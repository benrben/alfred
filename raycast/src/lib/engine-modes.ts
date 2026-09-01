/**
 * Alfred engine client for Raycast — rewrite modes, settings, and the
 * "format" (mode + rewrite/translate/backend flags) pickers build from them.
 *
 * Split out of engine.ts (see engine.ts's own comment).
 */
import { getPrefs } from "./engine-env";
import { callEngine } from "./engine-contract";

export interface Mode {
  key: string;
  label: string;
  description: string;
  prompt: string;
  default?: boolean;
}

export async function loadModes(): Promise<Mode[]> {
  const res = await callEngine(["modes"]);
  try {
    const arr = JSON.parse(res.out) as Mode[];
    if (Array.isArray(arr)) return arr;
  } catch {
    // ignore — caller falls back to a built-in list
  }
  return [];
}

// ---- Settings (backend/model + processing defaults) ----------------------

export interface Processing {
  mode: string;
  rewrite: boolean;
  translate: boolean;
  optimize: boolean;
  translate_via: string;
}

export interface Settings {
  backend: string;
  claude_model: string;
  codex_model: string;
  claude_models: string[];
  codex_models: string[];
  processing: Processing;
}

export async function loadSettings(): Promise<Settings | null> {
  const res = await callEngine(["settings"]);
  try {
    return JSON.parse(res.out) as Settings;
  } catch {
    return null;
  }
}

// ---- Formats (what the pickers offer) -------------------------------------
// A "format" bundles the engine flags for a capture:
//   - CONFIG: send NO flags — the engine uses your config.toml as-is. This is
//     the safe default: it never contradicts your translate/rewrite settings.
//   - RAW: force every stage off (a pure transcript, no LLM).
//   - the rest come from the engine's mode catalog and turn rewrite on.
// Translate/backend are layered on separately (per preference/toggle).

export const CONFIG_FORMAT_ID = "__config__";
export const RAW_FORMAT_ID = "__raw__";

export interface FormatChoice {
  id: string;
  title: string;
  subtitle: string;
  ai: boolean; // does it (or can it) invoke the LLM?
  flags: string[]; // mode/rewrite flags (no translate/backend)
}

/** The "use my config" format — no overrides. Safe fallback everywhere. */
export function configFormat(): FormatChoice {
  return {
    id: CONFIG_FORMAT_ID,
    title: "Default (config)",
    subtitle: "Use your saved settings",
    ai: true, // may invoke the LLM, depending on config
    flags: [],
  };
}

/** The "pure transcript" format — every LLM stage forced off. Static (no modes
 * needed), so the dedicated Transcribe Only command can pin it synchronously
 * before the mode catalog loads. Keeps the three --no-* flags (not the newer
 * --transcribe-only) so it still works against an older engine. */
export function rawFormat(): FormatChoice {
  return {
    id: RAW_FORMAT_ID,
    title: "Raw transcript",
    subtitle: "No AI — exactly what you said",
    ai: false,
    flags: ["--no-rewrite", "--no-translate", "--no-optimize"],
  };
}

export function buildFormats(modes: Mode[]): FormatChoice[] {
  const list: FormatChoice[] = [configFormat(), rawFormat()];
  for (const m of modes) {
    list.push({
      id: m.key,
      title: m.label || m.key,
      subtitle: m.description || "",
      ai: true,
      flags: ["--mode", m.key, "--rewrite"],
    });
  }
  return list;
}

/** Which AI mode the config currently resolves to (for the "Default" star in
 * Manage Intents). Note: this only reflects rewrite/mode, not translate. */
export function defaultFormatId(settings: Settings | null): string {
  const p = settings?.processing;
  if (!p || !p.rewrite) return RAW_FORMAT_ID;
  return p.mode || "raw";
}

/** The selectable LLM backends (besides "Default = use config"). ONE source
 * feeding every picker (PipelineForm's dropdown here; the Hammerspoon
 * front-end has its own equivalent list) so adding/renaming a backend can't
 * silently drift out of sync with the engine's `--backend` choices again —
 * "local" (the engine's actual default: on-device MLX, no login, no network)
 * had dropped out of this form's dropdown before. */
export const BACKENDS = ["auto", "claude", "codex", "local"] as const;

/** Normalize a backend value from preferences or a form before emitting flags. */
export function normalizeBackend(value?: string): string {
  const backend = (value || "default").trim();
  return backend === "default" ||
    BACKENDS.includes(backend as (typeof BACKENDS)[number])
    ? backend
    : "default";
}

/** Normalize the three-state translation override used by forms and captures. */
export function normalizeTranslate(value?: string): string {
  const translate = (value || "default").trim();
  return translate === "on" || translate === "off" || translate === "default"
    ? translate
    : "default";
}

/** Backend flag: explicit override, else preference (empty when "default"). */
export function backendFlags(override?: string): string[] {
  const b = normalizeBackend(override ?? getPrefs().backend);
  return b && b !== "default" ? ["--backend", b] : [];
}

/** Translate flag: explicit override, else preference, else config (none). */
export function translateFlags(override?: string): string[] {
  const t = normalizeTranslate(override ?? getPrefs().translate);
  if (t === "on") return ["--translate"];
  if (t === "off") return ["--no-translate"];
  return [];
}

/** Full per-run flags for a chosen format. */
export function flagsForFormat(
  fmt: FormatChoice,
  opts: { translate?: string; backend?: string } = {},
): string[] {
  // Raw already pins --no-translate; don't let a translate toggle contradict it.
  const translate = fmt.ai ? translateFlags(opts.translate) : [];
  return [...fmt.flags, ...translate, ...backendFlags(opts.backend)];
}

/** Persist a format as the new default ([processing] mode + rewrite). */
export async function setDefaultFormat(fmt: FormatChoice): Promise<boolean> {
  const argv =
    fmt.id === RAW_FORMAT_ID
      ? ["set-processing", "--mode", "raw", "--no-rewrite"]
      : ["set-processing", "--mode", fmt.id, "--rewrite"];
  const res = await callEngine(argv);
  return (res.out || "").includes("saved");
}
