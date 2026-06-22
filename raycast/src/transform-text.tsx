import { PipelineForm } from "./lib/PipelineForm";

// Prefills from the current selection (or clipboard) so you can clean up text
// in place: edit, pick a format, run, then Paste back over the selection.
export default function Command() {
  return <PipelineForm prefillSelection={true} />;
}
