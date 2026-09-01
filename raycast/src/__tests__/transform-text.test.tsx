// Coverage for the "Transform Text" command: a one-line wrapper that renders
// PipelineForm with prefillSelection=true (prefill from the current
// selection/clipboard). PipelineForm itself is under active work elsewhere
// and has its own real behavior/dependencies, so it's mocked out here — this
// file only proves Command passes the right prop, not PipelineForm's own
// behavior.
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import Command from "../transform-text";

vi.mock("../lib/PipelineForm", () => ({
  PipelineForm: vi.fn(() => <div data-testid="pipeline-form-mock" />),
}));

import { PipelineForm } from "../lib/PipelineForm";

describe("Transform Text Command", () => {
  it("renders PipelineForm prefilled from the current selection", () => {
    render(<Command />);

    expect(PipelineForm).toHaveBeenCalledTimes(1);
    expect(vi.mocked(PipelineForm).mock.calls[0][0]).toEqual({
      prefillSelection: true,
    });
  });
});
