// Coverage for the History command: the empty state, the populated list (one
// item per recent result, with copy/paste/open actions), and readHistory()
// being the sole data source (mocked on the "../lib/engine" barrel, same
// pattern as engine-status.test.tsx / result-view.test.ts).
//
// The stub's `List.Item` doesn't implement a `.Detail` sub-component (no
// command exercised it before this file). history.tsx's per-item JSX builds
// <List.Item.Detail>/<List.Item.Detail.Metadata>/<...Metadata.Label> as PROP
// VALUES (evaluated eagerly to construct the element tree, same as any JSX),
// so without a `.Detail` this throws a bare "Cannot read properties of
// undefined" the instant an item renders — nothing to do with React actually
// mounting the subtree (List.Item ignores its `detail` prop entirely, so
// nothing about it reaches the DOM either way). We patch the missing chain
// onto the *imported stub module's own objects* before rendering, exactly
// like engine-state.test.ts patches node:fs — no edit to raycast-api.stub.tsx
// itself, and Vitest's per-file module isolation keeps this from leaking into
// any other test file. `stub.xxx` (namespace import, not destructured names)
// throughout so reads of the mutable `lastPushedElement` export see live
// updates, the same reasoning engine.test.ts documents for its own `stub`.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import Command from "../history";
import * as engine from "../lib/engine";
import * as stub from "../lib/__tests__/raycast-api.stub";

function ItemDetailLabel({ title, text }: { title?: string; text?: string }) {
  return (
    <div data-testid="metadata-label">
      {title}:{text}
    </div>
  );
}
function ItemDetailMetadata({ children }: { children?: ReactNode }) {
  return <div data-testid="item-metadata">{children}</div>;
}
ItemDetailMetadata.Label = ItemDetailLabel;
function ItemDetail({ markdown }: { markdown?: string }) {
  return <div data-testid="item-detail">{markdown}</div>;
}
ItemDetail.Metadata = ItemDetailMetadata;
(stub.List.Item as unknown as { Detail: typeof ItemDetail }).Detail =
  ItemDetail;

describe("History Command", () => {
  beforeEach(() => {
    stub.resetRaycastApiMocks();
  });
  afterEach(() => {
    cleanup();
  });

  it("shows the empty view when there is no history", async () => {
    vi.spyOn(engine, "readHistory").mockReturnValue([]);
    render(<Command />);

    await waitFor(() =>
      expect(screen.getByText(/No history yet/)).toBeInTheDocument(),
    );
  });

  it("lists items and wires Copy / Paste / Open actions", async () => {
    vi.spyOn(engine, "readHistory").mockReturnValue([
      {
        ts: "2026-01-02T03:04:00",
        chars: 11,
        text: "hello world",
        source: "dictate",
      },
      {
        ts: "2026-01-03T04:05:00",
        chars: 5,
        text: "short",
        // no `source` -> exercises the `item.source ?? "—"` fallback.
      },
    ]);
    render(<Command />);

    const items = await screen.findAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(screen.getByText("hello world")).toBeInTheDocument();
    expect(screen.getByText("short")).toBeInTheDocument();

    const copyButtons = screen.getAllByRole("button", { name: "Copy" });
    fireEvent.click(copyButtons[0]);
    await waitFor(async () =>
      expect(await stub.Clipboard.readText()).toBe("hello world"),
    );
    stub.setClipboardText("");

    const pasteButtons = screen.getAllByRole("button", {
      name: "Paste to Frontmost App",
    });
    fireEvent.click(pasteButtons[1]);
    await waitFor(async () =>
      expect(await stub.Clipboard.readText()).toBe("short"),
    );

    const openButtons = screen.getAllByRole("button", { name: "Open" });
    fireEvent.click(openButtons[0]);
    expect(stub.lastPushedElement).not.toBeNull();
    expect(
      (stub.lastPushedElement as unknown as { props: { markdown: string } })
        .props.markdown,
    ).toBe("hello world");
  });

  it("truncates a very long title via formatHistoryTitle", async () => {
    const longText = "x".repeat(120);
    vi.spyOn(engine, "readHistory").mockReturnValue([
      { ts: "2026-01-01T00:00:00", chars: 120, text: longText },
    ]);
    render(<Command />);

    await screen.findAllByRole("listitem");
    expect(screen.queryByText(longText)).not.toBeInTheDocument();
    expect(screen.getByText(/x{57}…/)).toBeInTheDocument();
  });
});
