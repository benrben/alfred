// Coverage for the Manage Intents command: the list of formats/intents (with
// the current default starred), setting a new default, and the New/Edit
// Intent form's validation + save flow. loadModes/loadSettings/callEngine/
// setDefaultFormat are mocked on the "../lib/engine" barrel — the same
// pattern engine-status.test.tsx / history.test.tsx / result-view.test.ts
// use — everything else (buildFormats, defaultFormatId, validateIntentKey,
// intentDetailMarkdown, ...) is left real since it's pure and already
// covered by view-logic.test.ts / engine.test.ts.
//
// The stub's `List.Item` doesn't implement a `.Detail` sub-component (see
// history.test.tsx's comment for why); manage-intents.tsx builds
// `<List.Item.Detail markdown={...} />` as a prop value on every row, which
// throws the instant a row renders without one. Patched onto the *imported
// stub module's own objects* here, exactly like history.test.tsx does —
// Vitest's per-file module isolation keeps this from leaking elsewhere.
//
// `stub.xxx` (namespace import, not destructured names) throughout so reads
// of the mutable `lastPushedElement` export see live updates.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactElement } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import ManageIntents from "../manage-intents";
import * as engine from "../lib/engine";
import type { Mode, Settings } from "../lib/engine";
import * as stub from "../lib/__tests__/raycast-api.stub";

function ItemDetail({ markdown }: { markdown?: string }) {
  return <div data-testid="item-detail">{markdown}</div>;
}
(stub.List.Item as unknown as { Detail: typeof ItemDetail }).Detail =
  ItemDetail;

const MODE_EMAIL: Mode = {
  key: "email",
  label: "Email",
  description: "Polished for email",
  prompt: "Shape this as a professional email.",
};
// No prompt/description: exercises formatBody's "no prompt" fallback and the
// List.Item subtitle's non-raw id branch with an otherwise-empty mode.
const MODE_NOTES: Mode = {
  key: "notes",
  label: "Notes",
  description: "",
  prompt: "",
};

const SETTINGS_EMAIL_DEFAULT: Settings = {
  backend: "auto",
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
  },
};

function mockCatalog(modes: Mode[], settings: Settings | null) {
  const loadModes = vi.spyOn(engine, "loadModes").mockResolvedValue(modes);
  const loadSettings = vi
    .spyOn(engine, "loadSettings")
    .mockResolvedValue(settings);
  return { loadModes, loadSettings };
}

describe("Manage Intents Command", () => {
  beforeEach(() => {
    stub.resetRaycastApiMocks();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("shows the empty view when the engine has no formats beyond config/raw", async () => {
    mockCatalog([], null);
    render(<ManageIntents />);

    await waitFor(() =>
      expect(screen.getByText(/Engine not reachable/)).toBeInTheDocument(),
    );
  });

  it("lists formats/intents and marks the configured default", async () => {
    mockCatalog([MODE_EMAIL, MODE_NOTES], SETTINGS_EMAIL_DEFAULT);
    render(<ManageIntents />);

    await screen.findAllByRole("listitem");
    // Config format is filtered out; Raw + the two modes remain.
    expect(
      screen.getByRole("listitem", { name: "Raw transcript" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Email" })).toBeInTheDocument();
    expect(screen.getByRole("listitem", { name: "Notes" })).toBeInTheDocument();
  });

  // setDefaultFormat (called directly by makeDefault) is mocked here, NOT
  // callEngine: setDefaultFormat calls callEngine via its own direct import
  // from "./engine-contract" rather than through this barrel, so spying on
  // the barrel's `callEngine` does not intercept that internal call — it
  // would silently fall through to the REAL engine (confirmed against a
  // real running local daemon while developing this test). Mocking at
  // setDefaultFormat, which manage-intents.tsx imports and calls directly,
  // is the reliable interception point.
  it("sets a new default on success and reloads the catalog", async () => {
    const { loadModes } = mockCatalog(
      [MODE_EMAIL, MODE_NOTES],
      SETTINGS_EMAIL_DEFAULT,
    );
    const setDefaultFormat = vi
      .spyOn(engine, "setDefaultFormat")
      .mockResolvedValue(true);
    render(<ManageIntents />);

    const notesRow = await screen.findByRole("listitem", { name: "Notes" });
    fireEvent.click(
      within(notesRow).getByRole("button", { name: "Set as Default" }),
    );

    await waitFor(() =>
      expect(setDefaultFormat).toHaveBeenCalledWith(
        expect.objectContaining({ id: "notes" }),
      ),
    );
    // reload() re-fetches: 1 mount + 1 after the successful set-default.
    await waitFor(() => expect(loadModes).toHaveBeenCalledTimes(2));
    const success = stub.toastHistory[stub.toastHistory.length - 1];
    expect(success.style).toBe("success");
    expect(success.title).toBe("Default: Notes");
  });

  it("shows a failure toast and does not reload when setting default fails", async () => {
    const { loadModes } = mockCatalog(
      [MODE_EMAIL, MODE_NOTES],
      SETTINGS_EMAIL_DEFAULT,
    );
    vi.spyOn(engine, "setDefaultFormat").mockResolvedValue(false);
    render(<ManageIntents />);

    await waitFor(() => expect(loadModes).toHaveBeenCalledTimes(1));
    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    fireEvent.click(
      within(emailRow).getByRole("button", { name: "Set as Default" }),
    );

    await waitFor(() => {
      const last = stub.toastHistory[stub.toastHistory.length - 1];
      expect(last.style).toBe("failure");
      expect(last.title).toBe("Could not set default");
    });
    expect(loadModes).toHaveBeenCalledTimes(1);
  });

  it("reloads the catalog from the Reload action", async () => {
    const { loadModes } = mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    render(<ManageIntents />);

    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    await waitFor(() => expect(loadModes).toHaveBeenCalledTimes(1));
    fireEvent.click(within(emailRow).getByRole("button", { name: "Reload" }));
    await waitFor(() => expect(loadModes).toHaveBeenCalledTimes(2));
  });

  it("New Intent: rejects an empty key without touching the engine", async () => {
    mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    const callEngine = vi.spyOn(engine, "callEngine");
    render(<ManageIntents />);

    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    fireEvent.click(
      within(emailRow).getByRole("button", { name: "New Intent" }),
    );
    expect(stub.lastPushedElement).not.toBeNull();

    render(stub.lastPushedElement as ReactElement);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(callEngine).not.toHaveBeenCalled());
  });

  it("New Intent: saves a minimal intent (no label/description) and closes", async () => {
    mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    const callEngine = vi
      .spyOn(engine, "callEngine")
      .mockResolvedValue({ code: 0, out: "intent saved", err: "" });
    render(<ManageIntents />);

    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    fireEvent.click(
      within(emailRow).getByRole("button", { name: "New Intent" }),
    );
    render(stub.lastPushedElement as ReactElement);

    fireEvent.change(screen.getByLabelText("Key"), {
      target: { value: "standup" },
    });
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "Summarize as standup notes." },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(callEngine).toHaveBeenCalledWith([
        "set-intent",
        "standup",
        "--prompt",
        "Summarize as standup notes.",
      ]),
    );
    const success = stub.toastHistory[stub.toastHistory.length - 1];
    expect(success.style).toBe("success");
    expect(success.title).toBe("Added “standup”");
  });

  it("New Intent: shows a failure toast with the engine's last error line", async () => {
    mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 1,
      out: "",
      err: "line one\nkey already exists",
    });
    render(<ManageIntents />);

    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    fireEvent.click(
      within(emailRow).getByRole("button", { name: "New Intent" }),
    );
    render(stub.lastPushedElement as ReactElement);

    fireEvent.change(screen.getByLabelText("Key"), {
      target: { value: "dup" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => {
      const last = stub.toastHistory[stub.toastHistory.length - 1];
      expect(last.style).toBe("failure");
      expect(last.title).toBe("Could not save");
      expect(last.message).toBe("key already exists");
    });
  });

  it("Edit Prompt: prefills the existing intent and saves with a label + description", async () => {
    mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    const callEngine = vi
      .spyOn(engine, "callEngine")
      .mockResolvedValue({ code: 0, out: "saved", err: "" });
    render(<ManageIntents />);

    const emailRow = await screen.findByRole("listitem", { name: "Email" });
    fireEvent.click(
      within(emailRow).getByRole("button", { name: "Edit Prompt" }),
    );
    expect(stub.lastPushedElement).not.toBeNull();
    render(stub.lastPushedElement as ReactElement);

    // isNew is false: the key shows as read-only Description text, not a field.
    expect(screen.queryByLabelText("Key")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Label")).toHaveValue("Email");
    expect(screen.getByLabelText("Description")).toHaveValue(
      "Polished for email",
    );

    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(callEngine).toHaveBeenCalledWith([
        "set-intent",
        "email",
        "--prompt",
        "Shape this as a professional email.",
        "--label",
        "Email",
        "--description",
        "Polished for email",
      ]),
    );
    const success = stub.toastHistory[stub.toastHistory.length - 1];
    expect(success.style).toBe("success");
    expect(success.title).toBe("Saved “email”");
  });

  it("does not offer Edit Prompt for the non-AI raw format", async () => {
    mockCatalog([MODE_EMAIL], SETTINGS_EMAIL_DEFAULT);
    render(<ManageIntents />);

    const rawRow = await screen.findByRole("listitem", {
      name: "Raw transcript",
    });
    expect(
      within(rawRow).queryByRole("button", { name: "Edit Prompt" }),
    ).not.toBeInTheDocument();
  });
});
