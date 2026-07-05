import { rawFormat } from "./lib/engine";
import Dictate from "./dictate";

/**
 * Transcribe Only — a one-touch pure-transcription capture.
 *
 * Same recording UI as Dictate, but pinned to the "Raw transcript" format so it
 * skips every LLM stage (no translate / rewrite / optimize). This is the fast,
 * private path: exactly what you said, no model in the loop. Bind it a hotkey to
 * get instant dictation-to-clipboard. You can still switch to an AI format from
 * the in-recording picker if you change your mind.
 */
export default function Transcribe(props: {
  launchContext?: { stop?: boolean };
}) {
  return (
    <Dictate launchContext={props.launchContext} forceFormat={rawFormat()} />
  );
}
