// Stand-in for "@raycast/api" under vitest. The real package ships only types
// (its runtime is provided by Raycast's bundler), so it can't be imported in a
// plain-node test. vitest.config.ts aliases "@raycast/api" to this file so
// engine.ts imports cleanly; tests drive it via the mutable state below.

export const mockPrefs: Record<string, string> = {
  daemonPort: "",
  backend: "default",
  translate: "default",
  pythonBin: "",
  engineScript: "",
  soxBin: "",
};

let clipboardText = "";
export function setClipboardText(t: string): void {
  clipboardText = t;
}

export function getPreferenceValues<T = Record<string, string>>(): T {
  return { ...mockPrefs } as T;
}

export const Clipboard = {
  readText: async (): Promise<string> => clipboardText,
  copy: async (): Promise<void> => undefined,
  paste: async (): Promise<void> => undefined,
};

export async function getSelectedText(): Promise<string> {
  return "";
}
