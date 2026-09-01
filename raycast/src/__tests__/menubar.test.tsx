// Coverage for the menu-bar command: `open()` (+ its returned launchCommand
// callback), and MenuBar itself in both the idle and recording states —
// exercising the dictateItemProps() extraction (see menubar.tsx) that keeps
// MenuBar's own complexity down. readRecState/isAlive are mocked on the
// "../lib/engine" barrel, same pattern as history.test.tsx.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import MenuBar from "../menubar";
import * as engine from "../lib/engine";
import * as stub from "../lib/__tests__/raycast-api.stub";

beforeEach(() => {
  stub.resetRaycastApiMocks();
});
afterEach(() => {
  cleanup();
});

describe("MenuBar — not recording", () => {
  beforeEach(() => {
    vi.spyOn(engine, "readRecState").mockReturnValue(null);
  });

  it("offers Dictate and Transcribe Only, and every item launches its command", () => {
    render(<MenuBar />);

    expect(screen.getByRole("button", { name: "Dictate" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Transcribe Only" }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dictate" }));
    expect(stub.launchCommandCalls).toContainEqual({
      name: "dictate",
      type: "userInitiated",
      context: undefined,
    });

    fireEvent.click(screen.getByRole("button", { name: "Transcribe Only" }));
    fireEvent.click(screen.getByRole("button", { name: "Transform Text" }));
    fireEvent.click(screen.getByRole("button", { name: "Type & Process" }));
    fireEvent.click(screen.getByRole("button", { name: "History" }));
    fireEvent.click(screen.getByRole("button", { name: "Manage Intents" }));
    fireEvent.click(screen.getByRole("button", { name: "Engine Status" }));

    const names = stub.launchCommandCalls.map(
      (c) => (c as { name: string }).name,
    );
    expect(names).toEqual([
      "dictate",
      "transcribe",
      "transform-text",
      "type-and-process",
      "history",
      "manage-intents",
      "engine-status",
    ]);
  });
});

describe("MenuBar — recording", () => {
  beforeEach(() => {
    vi.spyOn(engine, "readRecState").mockReturnValue({
      pid: 4242,
      wav: "/tmp/rec.wav",
      startedAt: Date.now(),
    });
    vi.spyOn(engine, "isAlive").mockReturnValue(true);
  });

  it("swaps to Stop & Transcribe, hides Transcribe Only, and opens Dictate already in stop mode", () => {
    render(<MenuBar />);

    expect(
      screen.getByRole("button", { name: "Stop & Transcribe" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Transcribe Only" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Dictate" }),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Stop & Transcribe" }));
    expect(stub.launchCommandCalls).toContainEqual({
      name: "dictate",
      type: "userInitiated",
      context: { stop: true },
    });
  });

  it("treats a dead pid as not recording", () => {
    vi.spyOn(engine, "isAlive").mockReturnValue(false);
    render(<MenuBar />);

    expect(screen.getByRole("button", { name: "Dictate" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Transcribe Only" }),
    ).toBeInTheDocument();
  });
});
