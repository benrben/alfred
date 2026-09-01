// Coverage for the "Type & Process" command: a one-line wrapper that renders
// PipelineForm with prefillSelection=false (starts from an empty field).
// PipelineForm itself is under active work elsewhere and has its own real
// behavior/dependencies, so it's mocked out here — this file only proves
// Command passes the right prop, not PipelineForm's own behavior.
import { describe, expect, it, vi } from "vitest";
import { render } from "@testing-library/react";
import Command from "../type-and-process";

vi.mock("../lib/PipelineForm", () => ({
  PipelineForm: vi.fn(() => <div data-testid="pipeline-form-mock" />),
}));

import { PipelineForm } from "../lib/PipelineForm";

describe("Type & Process Command", () => {
  it("renders PipelineForm with an empty starting field", () => {
    render(<Command />);

    expect(PipelineForm).toHaveBeenCalledTimes(1);
    expect(vi.mocked(PipelineForm).mock.calls[0][0]).toEqual({
      prefillSelection: false,
    });
  });
});
