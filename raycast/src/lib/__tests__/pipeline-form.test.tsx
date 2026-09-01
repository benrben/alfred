// Coverage for PipelineForm: loading the format catalog (with/without a
// prefilled selection, and the mount-cancelled guard), and onSubmit's full
// decision tree (empty text, no formats loaded yet, a successful run for
// both an AI and the raw format, and a failed delivery). loadModes/
// getInputText/callEngine/resolveDelivery are mocked on the "../engine"
// barrel — PipelineForm imports and calls each of them directly, so spying
// on the barrel reliably intercepts them (unlike a function reached only
// indirectly through another barrel export — see manage-intents.test.tsx's
// comment on setDefaultFormat/callEngine for why that distinction matters).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { PipelineForm } from "../PipelineForm";
import * as engine from "../engine";
import type { Mode } from "../engine";
import * as stub from "../__tests__/raycast-api.stub";

const MODE_EMAIL: Mode = {
  key: "email",
  label: "Email",
  description: "Polish for email",
  prompt: "Shape this as an email.",
};

function mockLoad(modes: Mode[], input: string) {
  vi.spyOn(engine, "loadModes").mockResolvedValue(modes);
  vi.spyOn(engine, "getInputText").mockResolvedValue(input);
}

describe("PipelineForm", () => {
  beforeEach(() => {
    stub.resetRaycastApiMocks();
  });
  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("loads the format catalog without touching the selection when prefillSelection is false", async () => {
    const getInputText = vi.spyOn(engine, "getInputText");
    mockLoad([MODE_EMAIL], "should be ignored");
    render(<PipelineForm prefillSelection={false} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );
    expect(getInputText).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Text")).toHaveValue("");
  });

  it("prefills the text field from the selection when non-empty", async () => {
    mockLoad([MODE_EMAIL], "selected text");
    render(<PipelineForm prefillSelection={true} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Text")).toHaveValue("selected text"),
    );
  });

  it("leaves the text field blank when the selection is empty", async () => {
    mockLoad([MODE_EMAIL], "");
    render(<PipelineForm prefillSelection={true} />);

    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );
    expect(screen.getByLabelText("Text")).toHaveValue("");
  });

  it("does not update state after unmounting mid-load (the cancelled guard)", async () => {
    let resolveModes!: (modes: Mode[]) => void;
    vi.spyOn(engine, "loadModes").mockReturnValue(
      new Promise<Mode[]>((resolve) => {
        resolveModes = resolve;
      }),
    );
    vi.spyOn(engine, "getInputText").mockResolvedValue("");
    const consoleError = vi
      .spyOn(console, "error")
      .mockImplementation(() => {});

    const { unmount } = render(<PipelineForm prefillSelection={false} />);
    unmount();
    resolveModes([MODE_EMAIL]);
    await new Promise((r) => setTimeout(r, 0));

    expect(consoleError).not.toHaveBeenCalled();
  });

  it("shows a failure toast and never calls the engine when the text is blank", async () => {
    mockLoad([MODE_EMAIL], "");
    const callEngine = vi.spyOn(engine, "callEngine");
    render(<PipelineForm prefillSelection={false} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run Through Alfred" }));

    await waitFor(() => {
      const last = stub.toastHistory[stub.toastHistory.length - 1];
      expect(last.style).toBe("failure");
      expect(last.title).toBe("Type or select some text first");
    });
    expect(callEngine).not.toHaveBeenCalled();
  });

  it("shows 'No formats loaded' when submitted before the catalog finishes loading", async () => {
    // Deliberately don't await the mount effect: `formats` is still its
    // initial [] when the click below runs, so resolveFormat() has nothing
    // to resolve to — the one path to this branch in real use (a very fast
    // double-tap, or a slow engine).
    mockLoad([MODE_EMAIL], "");
    render(<PipelineForm prefillSelection={false} />);

    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "hello" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run Through Alfred" }));

    await waitFor(() => {
      const last = stub.toastHistory[stub.toastHistory.length - 1];
      expect(last.style).toBe("failure");
      expect(last.title).toBe("No formats loaded");
    });
  });

  it("runs an AI format and pushes ResultView on a successful delivery", async () => {
    mockLoad([MODE_EMAIL], "");
    const callEngine = vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 0,
      out: "VB_STATUS\tcopied",
      err: "",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "copied",
      text: "cleaned up text",
      llmFailed: false,
      pasteFailed: false,
    });
    render(<PipelineForm prefillSelection={false} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "raw dictation" },
    });
    // Leave Format at its default (CONFIG_FORMAT_ID) — an AI format.
    fireEvent.click(screen.getByRole("button", { name: "Run Through Alfred" }));

    await waitFor(() =>
      expect(callEngine).toHaveBeenCalledWith(["text", "raw dictation"]),
    );
    await waitFor(() => expect(stub.lastPushedElement).not.toBeNull());
    const pushed = stub.lastPushedElement as unknown as {
      props: {
        initialText: string;
        note: string;
        backend: string;
        translate: string;
      };
    };
    expect(pushed.props.initialText).toBe("cleaned up text");
    expect(pushed.props.note).toBe("Default (config)");
    expect(pushed.props.backend).toBe("default");
    expect(pushed.props.translate).toBe("default");

    const animated = stub.toastHistory[0];
    expect(animated.title).toBe("Running — Default (config)…");
  });

  it("runs the raw format with 'Cleaning up…' and a 'Raw transcript' note", async () => {
    mockLoad([MODE_EMAIL], "");
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 0,
      out: "VB_STATUS\tcopied",
      err: "",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "copied",
      text: "raw text",
      llmFailed: false,
      pasteFailed: false,
    });
    render(<PipelineForm prefillSelection={false} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "raw dictation" },
    });
    fireEvent.change(screen.getByLabelText("Format"), {
      target: { value: "__raw__" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run Through Alfred" }));

    await waitFor(() => {
      const animated = stub.toastHistory[0];
      expect(animated.title).toBe("Cleaning up…");
    });
    await waitFor(() => expect(stub.lastPushedElement).not.toBeNull());
    const pushed = stub.lastPushedElement as unknown as {
      props: { note: string };
    };
    expect(pushed.props.note).toBe("Raw transcript");
  });

  it("shows a failure toast with the engine's excerpt when delivery fails", async () => {
    mockLoad([MODE_EMAIL], "");
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 1,
      out: "",
      err: "boom: engine crashed",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "error",
      llmFailed: false,
      pasteFailed: false,
    });
    render(<PipelineForm prefillSelection={false} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Text"), {
      target: { value: "raw dictation" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run Through Alfred" }));

    await waitFor(() => {
      const last = stub.toastHistory[stub.toastHistory.length - 1];
      expect(last.style).toBe("failure");
      expect(last.title).toBe("Engine error");
      expect(last.message).toBe("boom: engine crashed");
    });
    expect(stub.lastPushedElement).toBeNull();
  });

  it("launches the Manage Intents command from its action", async () => {
    mockLoad([MODE_EMAIL], "");
    render(<PipelineForm prefillSelection={false} />);
    await waitFor(() =>
      expect(screen.getByLabelText("Format")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: "Manage Intents…" }));

    expect(stub.launchCommandCalls).toEqual([
      { name: "manage-intents", type: "userInitiated" },
    ]);
  });
});
