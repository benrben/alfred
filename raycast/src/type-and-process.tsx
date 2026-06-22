import { PipelineForm } from "./lib/PipelineForm";

// Starts with an empty field — type a line and run it through the pipeline.
export default function Command() {
  return <PipelineForm prefillSelection={false} />;
}
