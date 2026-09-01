// Coverage for the Engine Status command: refresh() gathers daemon/contract
// health + defaults and renders them as Detail markdown, and its three
// ActionPanel entries (Refresh, Manage Intents & Default, Open Extension
// Preferences). Every async dependency refresh() touches is mocked on the
// "../lib/engine" barrel (the same module engine-status.tsx imports from),
// the same pattern result-view.test.ts uses for callEngine — buildFormats /
// defaultFormatId / buildEngineStatusMarkdown are left real since they're
// pure and already covered by view-logic.test.ts / engine.test.ts.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Command from "../engine-status";
import * as engine from "../lib/engine";
import {
  launchCommandCalls,
  resetRaycastApiMocks,
} from "../lib/__tests__/raycast-api.stub";

const SETTINGS = {
  backend: "claude",
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
const MODES = [
  { key: "email", label: "Email", description: "Polish for email", prompt: "" },
];

function mockRefreshDeps() {
  vi.spyOn(engine, "loadContract").mockResolvedValue(engine.fallbackContract());
  vi.spyOn(engine, "daemonPort").mockReturnValue("8763");
  vi.spyOn(engine, "pingDaemon").mockResolvedValue(true);
  vi.spyOn(engine, "loadSettings").mockResolvedValue(SETTINGS);
  vi.spyOn(engine, "loadModes").mockResolvedValue(MODES);
  vi.spyOn(engine, "callEngine").mockResolvedValue({
    code: 0,
    out: "all systems go",
    err: "",
  });
  vi.spyOn(engine, "resolveScript").mockReturnValue(
    "/opt/alfred/voicebridge.py",
  );
  vi.spyOn(engine, "resolvePython").mockReturnValue(
    "/opt/alfred/.venv/python3",
  );
  vi.spyOn(engine, "contractSchemaWarning").mockReturnValue(null);
}

describe("Engine Status Command", () => {
  beforeEach(() => {
    resetRaycastApiMocks();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads on mount and renders the resolved status as markdown", async () => {
    mockRefreshDeps();
    const { container } = render(<Command />);

    expect(container.textContent).toContain("Checking the Alfred engine");

    await waitFor(() =>
      expect(container.textContent).toContain("all systems go"),
    );
    expect(container.textContent).toContain("🪄 Email");
    expect(container.textContent).toContain("/opt/alfred/voicebridge.py");
    expect(engine.loadContract).toHaveBeenCalledTimes(1);
    expect(engine.callEngine).toHaveBeenCalledWith(["doctor"]);
  });

  it("re-runs refresh when the Refresh action is clicked", async () => {
    mockRefreshDeps();
    const { container } = render(<Command />);
    await waitFor(() =>
      expect(container.textContent).toContain("all systems go"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));

    await waitFor(() => expect(engine.loadContract).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(container.textContent).toContain("all systems go"),
    );
  });

  it("Manage Intents & Default launches the manage-intents command", async () => {
    mockRefreshDeps();
    const { container } = render(<Command />);
    await waitFor(() =>
      expect(container.textContent).toContain("all systems go"),
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Manage Intents & Default" }),
    );

    expect(launchCommandCalls).toContainEqual({
      name: "manage-intents",
      type: "userInitiated",
    });
  });

  it("Open Extension Preferences is wired to the real handler", async () => {
    mockRefreshDeps();
    const { container } = render(<Command />);
    await waitFor(() =>
      expect(container.textContent).toContain("all systems go"),
    );

    // The stub's openExtensionPreferences just resolves; clicking must not throw.
    expect(() =>
      fireEvent.click(
        screen.getByRole("button", { name: "Open Extension Preferences" }),
      ),
    ).not.toThrow();
  });

  it("falls back to the doctor stderr and shows a schema-mismatch warning when out is empty", async () => {
    mockRefreshDeps();
    vi.spyOn(engine, "loadSettings").mockResolvedValue(null);
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 1,
      out: "",
      err: "doctor blew up",
    });
    vi.spyOn(engine, "contractSchemaWarning").mockReturnValue(
      "engine v2 / extension v1 out of sync",
    );
    const { container } = render(<Command />);

    await waitFor(() =>
      expect(container.textContent).toContain("doctor blew up"),
    );
    expect(container.textContent).toContain("out of sync");
    // No default format when settings are null.
    expect(container.textContent).toContain("Raw transcript (no AI)");
  });
});
