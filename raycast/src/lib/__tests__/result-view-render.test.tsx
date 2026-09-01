// Real-rendering companion to result-view.test.ts. That file calls ResultView
// as a plain function (with useState mocked out) and walks the returned
// element tree by hand — the cheapest way to prove an action handler does the
// right thing, but awkward for FeedbackForm: it's a separately-pushed screen
// with its own useState/useNavigation, and the "does the form actually work"
// question is best answered by really rendering it and typing into it. This
// file runs under jsdom (it's a .test.tsx — see vitest.config.ts's
// environmentMatchGlobs) and uses real React end to end: no `vi.mock("react")`
// override here, so ResultView and FeedbackForm both render through React
// Testing Library exactly as Raycast would mount them.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactElement } from "react";
import { ResultView } from "../ResultView";
import * as engine from "../engine";
// Import the stub directly (not via the "@raycast/api" alias): typecheck
// resolves that specifier to the real package's .d.ts, which doesn't export
// these test-only helpers — see raycast-api.stub.tsx's own header comment.
import {
  lastPushedElement,
  resetRaycastApiMocks,
  toastHistory,
} from "./raycast-api.stub";

const emailFormat = {
  id: "email",
  title: "Email",
  subtitle: "",
  ai: true,
  flags: ["--mode", "email"],
};

describe("ResultView (real render)", () => {
  beforeEach(() => resetRaycastApiMocks());
  afterEach(() => {
    // Each test renders more than once (ResultView, then the pushed
    // FeedbackForm) and RTL doesn't auto-cleanup without `test.globals`, so
    // explicitly unmount everything between tests to avoid duplicate matches.
    cleanup();
    vi.restoreAllMocks();
  });

  it("refines the text via the pushed FeedbackForm and applies the result back on the result screen", async () => {
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 0,
      out: `VB_RESULT\t${JSON.stringify("revised text")}\nVB_STATUS\tcopied`,
      err: "",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "copied",
      text: "revised text",
      llmFailed: false,
      pasteFailed: false,
    });

    render(
      <ResultView
        initialText="hello"
        backend="codex"
        formats={[emailFormat]}
      />,
    );

    fireEvent.click(
      screen.getByRole("button", { name: "Refine with Feedback…" }),
    );
    expect(lastPushedElement).toBeTruthy();

    const { container } = render(lastPushedElement as ReactElement);
    const textarea = within(container).getByLabelText("Feedback");
    fireEvent.change(textarea, { target: { value: "make it shorter" } });
    fireEvent.click(
      within(container).getByRole("button", { name: "Apply Feedback" }),
    );

    await waitFor(() => {
      expect(engine.callEngine).toHaveBeenCalledWith([
        "text",
        "hello",
        "--instruction",
        "make it shorter",
        "--backend",
        "codex",
      ]);
    });

    // applyRefined runs on the ORIGINAL ResultView instance (onRefined is
    // wired to it), so the result screen's own text/banner update in place.
    await waitFor(() => {
      expect(screen.getByText(/revised text/)).toBeInTheDocument();
    });
    expect(screen.getByText(/✎ Refined: make it shorter/)).toBeInTheDocument();
    expect(toastHistory[toastHistory.length - 1].style).toBe("success");
    expect(toastHistory[toastHistory.length - 1].title).toBe("Refined");
  });

  it("does nothing when the feedback instruction is left blank", () => {
    const callEngine = vi.spyOn(engine, "callEngine");

    render(<ResultView initialText="hello" formats={[emailFormat]} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Refine with Feedback…" }),
    );
    const { container } = render(lastPushedElement as ReactElement);

    fireEvent.click(
      within(container).getByRole("button", { name: "Apply Feedback" }),
    );

    expect(callEngine).not.toHaveBeenCalled();
    expect(toastHistory).toHaveLength(0);
  });

  it("shows a failure toast with the engine error excerpt when refining fails", async () => {
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 1,
      out: "",
      err: "refine exploded\n",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "error",
      llmFailed: false,
      pasteFailed: false,
    });

    render(<ResultView initialText="hello" formats={[emailFormat]} />);
    fireEvent.click(
      screen.getByRole("button", { name: "Refine with Feedback…" }),
    );
    const { container } = render(lastPushedElement as ReactElement);
    fireEvent.change(within(container).getByLabelText("Feedback"), {
      target: { value: "fix the date" },
    });
    fireEvent.click(
      within(container).getByRole("button", { name: "Apply Feedback" }),
    );

    await waitFor(() => {
      const toast = toastHistory[toastHistory.length - 1];
      expect(toast.style).toBe("failure");
      expect(toast.title).toBe("Refine failed");
      expect(toast.message).toBe("refine exploded");
    });
    // The original result screen never saw the refined text.
    expect(screen.queryByText(/✎ Refined:/)).not.toBeInTheDocument();
  });

  it("switches the active backend from the Backend submenu", () => {
    render(<ResultView initialText="hello" formats={[emailFormat]} />);

    expect(
      screen.getByRole("group", { name: "Backend: Default (config)" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "codex" }));
    expect(
      screen.getByRole("group", { name: "Backend: codex" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Default (config)" }));
    expect(
      screen.getByRole("group", { name: "Backend: Default (config)" }),
    ).toBeInTheDocument();
  });

  it("shows and fires the Dictate Again action when onDictateAgain is provided", () => {
    const onDictateAgain = vi.fn();
    render(
      <ResultView
        initialText="hello"
        formats={[emailFormat]}
        onDictateAgain={onDictateAgain}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Dictate Again" }));
    expect(onDictateAgain).toHaveBeenCalledTimes(1);
  });
});
