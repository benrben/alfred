// Coverage for the "Transcribe Only" command: a thin wrapper that pins
// Dictate to the raw (non-AI) format and forwards launchContext untouched.
// Dictate itself is a large, independently-tested component under active
// work elsewhere, so it's mocked out entirely here — this file only proves
// Transcribe wires the right props to it, not Dictate's own behavior.
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import Transcribe from "../transcribe";
import { rawFormat } from "../lib/engine";

vi.mock("../dictate", () => ({
  default: vi.fn(() => <div data-testid="dictate-mock" />),
}));

import Dictate from "../dictate";

describe("Transcribe", () => {
  it("renders Dictate pinned to the raw format and forwards launchContext", () => {
    render(<Transcribe launchContext={{ stop: true }} />);

    expect(Dictate).toHaveBeenCalledTimes(1);
    const props = vi.mocked(Dictate).mock.calls[0][0];
    expect(props).toEqual({
      launchContext: { stop: true },
      forceFormat: rawFormat(),
    });
  });

  it("forwards an undefined launchContext when none is given", () => {
    render(<Transcribe />);

    const props = vi.mocked(Dictate).mock.calls.at(-1)?.[0];
    expect(props?.launchContext).toBeUndefined();
    expect(props?.forceFormat).toEqual(rawFormat());
  });
});
