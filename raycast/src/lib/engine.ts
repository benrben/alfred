/**
 * Alfred engine client for Raycast.
 *
 * Speaks the engine's tiny IPC contract:
 *   - prefer the warm daemon (`voicebridge.py serve`) over localhost HTTP:
 *       POST {"argv":[...]} -> {"code":int,"out":"<captured stdout>"}
 *   - fall back to spawning `voicebridge.py <argv>` as a one-shot if it's down,
 *     and (best effort) start the daemon for next time.
 *
 * After a `process`/`text` run (no --stdout) the engine copies the result to the
 * clipboard or saves it to a file and prints a machine-readable `VB_STATUS` line;
 * resolveDelivery() reads that back.
 *
 * This file is a barrel: the implementation lives in engine-env.ts (prefs,
 * environment, locating the script), engine-contract.ts (the IPC contract +
 * calling the engine), engine-modes.ts (rewrite modes, settings, formats),
 * engine-delivery.ts (parsing VB_STATUS/VB_RESULT), and engine-state.ts
 * (history, live progress/stream, recording state). Split out to stay under
 * the repository's 600-line File LOC gate; every one of the ~55 command/lib
 * files that imports from "./lib/engine" keeps working unchanged.
 */
export * from "./engine-env";
export * from "./engine-contract";
export * from "./engine-modes";
export * from "./engine-delivery";
export * from "./engine-state";
