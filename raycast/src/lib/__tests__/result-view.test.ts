import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// ResultView is mostly JSX glue, but its action handlers are the public path
// where per-run settings must survive into a reprocess. Keep this test small by
// providing a hook runner and invoking the returned action directly. Only
// useState is overridden (react-testing-library's Form/ActionPanel stub in
// "@raycast/api" needs the real createContext/useContext/useEffect to import
// at all, even though this test never renders through them).
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: <T>(initial: T | (() => T)) => [
      typeof initial === "function" ? (initial as () => T)() : initial,
      vi.fn(),
    ],
  };
});

import { ResultView } from "../ResultView";
import * as engine from "../engine";
// Import the stub directly (not via the "@raycast/api" alias): typecheck
// resolves that specifier to the real package's .d.ts, which doesn't export
// these test-only helpers — see raycast-api.stub.tsx's own header comment.
import { resetRaycastApiMocks, toastHistory } from "./raycast-api.stub";

describe("ResultView run settings", () => {
  afterEach(() => vi.restoreAllMocks());
  beforeEach(() => resetRaycastApiMocks());

  it("uses the backend and translation from the preceding run when reprocessing", async () => {
    const callEngine = vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 0,
      out: `VB_RESULT\t${JSON.stringify("new text")}\nVB_STATUS\tcopied`,
      err: "",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "copied",
      text: "new text",
      llmFailed: false,
      pasteFailed: false,
    });

    const result = ResultView({
      initialText: "hello",
      backend: "codex",
      translate: "on",
      formats: [
        {
          id: "email",
          title: "Email",
          subtitle: "",
          ai: true,
          flags: ["--mode", "email", "--rewrite"],
        },
      ],
    });
    const actionPanel = result.props.actions;
    const submenus = actionPanel.props.children.filter(Boolean);
    const reprocess = submenus.find(
      (child: { props?: { title?: string } }) =>
        child.props?.title === "Reprocess As…",
    );
    expect(reprocess).toBeDefined();

    await (
      reprocess.props.children as Array<{
        props: { onAction: () => Promise<void> };
      }>
    )[0].props.onAction();

    expect(callEngine).toHaveBeenCalledWith([
      "text",
      "hello",
      "--mode",
      "email",
      "--rewrite",
      "--translate",
      "--backend",
      "codex",
    ]);
  });

  it("falls back to the config backend/translate when the caller passes neither, and renders a non-AI format without the AI title/icon", async () => {
    const callEngine = vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 0,
      out: `VB_RESULT\t${JSON.stringify("raw text")}\nVB_STATUS\tcopied`,
      err: "",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "copied",
      text: "raw text",
      llmFailed: false,
      pasteFailed: false,
    });

    const result = ResultView({
      initialText: "hello",
      formats: [
        {
          id: "raw",
          title: "Raw",
          subtitle: "",
          ai: false,
          flags: ["--no-rewrite"],
        },
      ],
    });
    const submenus = result.props.actions.props.children.filter(Boolean);
    const backendMenu = submenus.find(
      (child: { props?: { title?: string } }) =>
        typeof child.props?.title === "string" &&
        child.props.title.startsWith("Backend:"),
    );
    expect(backendMenu.props.title).toBe("Backend: Default (config)");

    const reprocess = submenus.find(
      (child: { props?: { title?: string } }) =>
        child.props?.title === "Reprocess As…",
    );
    const rawAction = (
      reprocess.props.children as Array<{
        props: { title: string; onAction: () => Promise<void> };
      }>
    )[0];
    expect(rawAction.props.title).toBe("Raw (no AI)");

    await rawAction.props.onAction();

    // ai:false skips the translate flag entirely, and normalizeBackend("default")
    // drops the --backend flag too, so only the format's own flags survive.
    expect(callEngine).toHaveBeenCalledWith(["text", "hello", "--no-rewrite"]);
  });

  it("shows a failure toast with the engine error excerpt when reprocessing fails", async () => {
    vi.spyOn(engine, "callEngine").mockResolvedValue({
      code: 1,
      out: "",
      err: "engine exploded\n",
    });
    vi.spyOn(engine, "resolveDelivery").mockResolvedValue({
      kind: "error",
      llmFailed: false,
      pasteFailed: false,
    });

    const result = ResultView({
      initialText: "hello",
      formats: [
        {
          id: "email",
          title: "Email",
          subtitle: "",
          ai: true,
          flags: ["--mode", "email"],
        },
      ],
    });
    const submenus = result.props.actions.props.children.filter(Boolean);
    const reprocess = submenus.find(
      (child: { props?: { title?: string } }) =>
        child.props?.title === "Reprocess As…",
    );

    await (
      reprocess.props.children as Array<{
        props: { onAction: () => Promise<void> };
      }>
    )[0].props.onAction();

    const toast = toastHistory[toastHistory.length - 1];
    expect(toast.style).toBe("failure");
    expect(toast.title).toBe("Reprocess failed");
    expect(toast.message).toBe("engine exploded");
  });
});
