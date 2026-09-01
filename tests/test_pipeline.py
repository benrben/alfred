"""Interface tests for the processing pipeline's translation routing (s3).

These assert the *observable contract* of eng-pipeline at its interface
(process_text / active_stages / build_combined_prompt), not the implementation:

  - Hebrew->English translation is FOLDED into the single combined LLM prompt.
  - The Whisper translate task is used ONLY when the model can actually do it
    (non-turbo); on the default turbo model, translation routes through the LLM.

Run: ./.venv/bin/python -m unittest discover -s tests
(No third-party test deps; the LLM call is stubbed so claude/codex need not exist.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

# A path guaranteed not to exist, so load_config falls through to its
# built-in DEFAULTS instead of picking up a real ~/.config/voicebridge
# config.toml (load_config(None) searches the real CONFIG_SEARCH paths, NOT
# "no config" — passing a nonexistent path is what actually isolates a test
# from whatever the machine running it happens to have configured).
NO_CFG = "/nonexistent/alfred-test-config.toml"


def _cfg(**processing):
    """A minimal config tree (built-in DEFAULTS, no real TOML) with
    processing overrides."""
    cfg = vb.load_config(NO_CFG)
    cfg["processing"].update(processing)
    return cfg


class WhisperTranslateRouting(unittest.TestCase):
    def test_turbo_cannot_whisper_translate(self):
        cfg = _cfg(translate=True, translate_via="whisper")
        cfg["stt"]["model"] = "mlx-community/whisper-large-v3-turbo"
        self.assertFalse(vb._whisper_can_translate(cfg))
        self.assertFalse(vb.whisper_translate_active(cfg))
        # ...so translation is folded into the LLM stage instead.
        self.assertTrue(vb.active_stages(cfg)["translate"])

    def test_full_model_does_whisper_translate(self):
        cfg = _cfg(translate=True, translate_via="whisper")
        cfg["stt"]["model"] = "mlx-community/whisper-large-v3"
        self.assertTrue(vb._whisper_can_translate(cfg))
        self.assertTrue(vb.whisper_translate_active(cfg))
        # Whisper already translated -> the LLM translate stage is redundant.
        self.assertFalse(vb.active_stages(cfg)["translate"])

    def test_llm_route_always_uses_llm(self):
        cfg = _cfg(translate=True, translate_via="llm")
        cfg["stt"]["model"] = "mlx-community/whisper-large-v3"  # could translate
        self.assertFalse(vb.whisper_translate_active(cfg))      # but not asked to
        self.assertTrue(vb.active_stages(cfg)["translate"])

    def test_translate_off_no_translate_stage(self):
        cfg = _cfg(translate=False, translate_via="llm")
        self.assertFalse(vb.active_stages(cfg)["translate"])
        self.assertFalse(vb.whisper_translate_active(cfg))


class CombinedPromptFolding(unittest.TestCase):
    def setUp(self):
        # Capture the prompt(s) the pipeline would send, without any real CLI.
        self.calls = []
        self._orig_fallback = vb.run_llm_fallback
        self._orig_cands = vb.candidate_backends
        vb.candidate_backends = lambda cfg: ["fake"]
        vb.run_llm_fallback = self._capture

    def tearDown(self):
        vb.run_llm_fallback = self._orig_fallback
        vb.candidate_backends = self._orig_cands

    def _capture(self, backends, prompt, cfg):
        self.calls.append(prompt)
        return "OUT"

    def test_translate_and_rewrite_fold_into_one_call(self):
        cfg = _cfg(translate=True, rewrite=True, optimize=False,
                   mode="raw", translate_via="whisper", combine_stages=True)
        cfg["stt"]["model"] = "mlx-community/whisper-large-v3-turbo"
        out = vb.process_text("שלום עולם", cfg)
        self.assertEqual(out, "OUT")
        self.assertEqual(len(self.calls), 1, "must be ONE combined LLM call")
        prompt = self.calls[0]
        self.assertIn(vb._TRANSLATE, prompt)        # translate folded in
        self.assertIn("Clean up this raw voice", prompt)  # rewrite folded in
        self.assertIn("שלום עולם", prompt)          # source text carried

    def test_build_combined_prompt_orders_all_three(self):
        prompt = vb.build_combined_prompt(
            {"translate": True, "rewrite": True, "optimize": True},
            "REWRITE_INSTR", "TXT")
        self.assertIn("1. " + vb._TRANSLATE, prompt)
        self.assertIn("2. REWRITE_INSTR", prompt)
        self.assertIn("3. " + vb._OPTIMIZE, prompt)
        self.assertTrue(prompt.rstrip().endswith("TXT"))

    def test_no_stages_is_passthrough_no_llm(self):
        cfg = _cfg(translate=False, rewrite=False, optimize=False)
        out = vb.process_text("raw text", cfg)
        self.assertEqual(out, "raw text")
        self.assertEqual(self.calls, [], "no LLM call when nothing is enabled")

    def test_empty_text_short_circuits_before_any_stage(self):
        # Even with every stage enabled, blank input never reaches the LLM.
        cfg = _cfg(translate=True, rewrite=True, optimize=True)
        for blank in ("", "   ", "\n\t "):
            self.assertEqual(vb.process_text(blank, cfg), "")
        self.assertEqual(self.calls, [])

    def test_long_transcript_is_chunked_across_calls(self):
        # An 18-minute dictation used to translate in ONE call and truncate at the
        # local token cap. It must now split into several bounded calls, each
        # under the char budget, and join into one result (nothing lost).
        cfg = _cfg(translate=True, rewrite=False, optimize=False,
                   translate_via="llm", combine_stages=True)
        budget = vb._chunk_char_budget(cfg)
        long_text = ("This is a spoken sentence in the meeting. " * 400).strip()
        self.assertGreater(len(long_text), budget)
        vb.process_text(long_text, cfg)
        self.assertGreater(len(self.calls), 1, "long input must split into chunks")
        for prompt in self.calls:
            body = prompt.split("INPUT TEXT:")[-1]
            self.assertLessEqual(len(body), budget + 200)  # +instruction slack

    def test_short_transcript_stays_single_call(self):
        cfg = _cfg(translate=True, rewrite=False, optimize=False,
                   translate_via="llm", combine_stages=True)
        vb.process_text("A short line.", cfg)
        self.assertEqual(len(self.calls), 1)


class SplitForProcessing(unittest.TestCase):
    """_split_for_processing returns [(chunk_text, starts_new_paragraph), ...] —
    the flag is what lets process_text rejoin chunk outputs with the input's
    original paragraph structure instead of flattening every boundary to a
    space (see ChunkedProcessingPreservesParagraphs below)."""

    def test_short_text_is_one_chunk(self):
        self.assertEqual(vb._split_for_processing("hi there", 100),
                         [("hi there", False)])

    def test_splits_on_sentence_bounds_under_budget(self):
        text = ". ".join(f"Sentence number {i}" for i in range(200)) + "."
        chunks = vb._split_for_processing(text, 300)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 300 for c, _ in chunks))
        # No text is dropped: every sentence's core survives somewhere.
        joined = " ".join(c for c, _ in chunks)
        self.assertIn("Sentence number 0", joined)
        self.assertIn("Sentence number 199", joined)
        # One continuous paragraph split purely for size: only the (moot)
        # first chunk is flagged; the rest are same-paragraph continuations.
        self.assertEqual([starts for _, starts in chunks][1:],
                         [False] * (len(chunks) - 1))

    def test_lone_oversized_sentence_is_hard_split(self):
        text = "x" * 1000  # no sentence break at all
        chunks = vb._split_for_processing(text, 250)
        self.assertTrue(all(len(c) <= 250 for c, _ in chunks))
        self.assertEqual("".join(c for c, _ in chunks), text)

    def test_paragraph_aligned_chunks_are_each_flagged(self):
        # Each paragraph here is big enough to force its own chunk, so every
        # chunk boundary coincides with a real paragraph break in the input.
        para = ("Sentence one. Sentence two. Sentence three. " * 20).strip()
        text = "\n\n".join([para] * 3)
        chunks = vb._split_for_processing(text, len(para) + 50)
        self.assertEqual(len(chunks), 3)
        self.assertEqual([starts for _, starts in chunks], [True, True, True])

    def test_short_paragraphs_packed_into_one_chunk_keep_blank_lines(self):
        # Multiple whole paragraphs fit in a single chunk together: the blank
        # line between them must survive INSIDE that one chunk's text too.
        paras = [f"Paragraph {i} has a few short words in it." for i in range(4)]
        text = "\n\n".join(paras)
        budget = len(paras[0]) + 2 + len(paras[1]) + 5
        chunks = vb._split_for_processing(text, budget)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], paras[0] + "\n\n" + paras[1])
        self.assertEqual(chunks[1][0], paras[2] + "\n\n" + paras[3])

    def test_extra_blank_lines_between_paragraphs_are_skipped_not_kept_empty(self):
        # 4+ consecutive newlines make text.split("\n\n") yield an EMPTY
        # paragraph string between the two real ones; it must be dropped, not
        # turned into a stray empty unit/chunk.
        p1 = "Para one here with some words to pad it out for length here."
        p2 = "Para two continues here with additional padding words too."
        text = f"{p1}\n\n\n\n{p2}"
        chunks = vb._split_for_processing(text, 60)
        joined = "".join(c for c, _ in chunks)
        self.assertEqual(joined, p1 + p2)
        # Exactly the two REAL paragraphs start a new paragraph, not three.
        self.assertEqual(sum(1 for _, starts in chunks if starts), 2)

    def test_paragraph_units_skips_empty_sentence_split_artifact(self):
        # _SENT_SPLIT.split() can hand back a whitespace-only trailing piece
        # when a paragraph's final sentence-ending punctuation is itself
        # followed by more whitespace. _split_for_processing's own paragraph
        # loop always calls this helper with an already-.strip()ped paragraph
        # (which can never trigger that), so exercise the helper directly to
        # prove the defensive skip works if it's ever handed one.
        para = "One sentence here that is somewhat long for the budget test. Two.   "
        self.assertEqual(vb.pipeline._SENT_SPLIT.split(para)[-1], "")  # the artifact
        units = vb.pipeline._paragraph_units(para, 40)
        self.assertTrue(all(u.strip() for u, _ in units), "no blank unit produced")
        self.assertEqual("".join(u for u, _ in units), "One sentence here that is somewhat long for the budget test.Two.")


class ChunkedProcessingPreservesParagraphs(unittest.TestCase):
    """Regression: process_text's chunk-output join used to flatten EVERY
    chunk boundary to a plain space, destroying paragraph structure on any
    transcript long enough to need chunking — even when a boundary landed
    exactly on a blank line in the original text. It must now rejoin with a
    blank line there, and a plain space only where one paragraph was split
    purely for size."""

    def setUp(self):
        self._orig_fallback = vb.run_llm_fallback
        self._orig_cands = vb.candidate_backends
        self._orig_budget = vb._chunk_char_budget
        vb.candidate_backends = lambda cfg: ["fake"]
        # An identity "LLM": echoes back exactly the chunk it was given, so the
        # assertion is purely about process_text's OWN join logic.
        vb.run_llm_fallback = lambda backends, prompt, cfg: (
            prompt.split("INPUT TEXT:\n", 1)[1])

    def tearDown(self):
        vb.run_llm_fallback = self._orig_fallback
        vb.candidate_backends = self._orig_cands
        vb._chunk_char_budget = self._orig_budget

    def test_paragraph_aligned_chunks_rejoin_with_blank_lines(self):
        cfg = _cfg(rewrite=True, mode="notes", combine_stages=True)
        para = ("Sentence one. Sentence two. Sentence three. " * 20).strip()
        text = "\n\n".join([para] * 3)
        # Force each paragraph into its own chunk (a real long dictation's
        # paragraphs routinely land one-per-chunk against the real budget).
        vb._chunk_char_budget = lambda c: len(para) + 50

        out = vb.process_text(text, cfg)

        self.assertEqual(out, text, "paragraph structure must survive intact")
        self.assertEqual(out.count("\n\n"), 2)

    def test_size_split_within_one_paragraph_rejoins_with_a_space(self):
        cfg = _cfg(rewrite=True, mode="notes", combine_stages=True)
        # A single continuous paragraph (no blank lines at all) long enough to
        # force multiple chunks purely by size.
        text = ("This is a spoken sentence in the meeting. " * 200).strip()
        out = vb.process_text(text, cfg)

        self.assertEqual(out, text)
        self.assertNotIn("\n\n", out, "one paragraph split by size stays one")

    def test_whitespace_only_chunk_output_is_skipped_not_kept_blank(self):
        # A chunk whose LLM output comes back whitespace-only (truthy, so
        # _process_chunk's `or text` fallback does NOT kick in) must be
        # dropped when rejoining, not turned into a stray blank segment.
        para = ("Sentence one. Sentence two. Sentence three. " * 20).strip()
        text = "\n\n".join([para, para])
        vb._chunk_char_budget = lambda c: len(para) + 50

        calls = []

        def fake(backends, prompt, cfg):
            calls.append(prompt)
            return "   " if len(calls) == 1 else "SECOND CHUNK OUTPUT"

        vb.run_llm_fallback = fake
        cfg = _cfg(rewrite=True, mode="notes", combine_stages=True)

        out = vb.process_text(text, cfg)

        self.assertEqual(len(calls), 2, "both chunks must still be processed")
        self.assertEqual(out, "SECOND CHUNK OUTPUT")


class ModeLookupHelpers(unittest.TestCase):
    """mode_prompt and rewrite_instruction both look up a mode's catalog
    entry; the [intent] shorthand (a bare string instead of a table) is
    normalized to {"prompt": ...}."""

    def test_mode_prompt_returns_builtin_prompt(self):
        cfg = _cfg()
        self.assertIn("clear, courteous email", vb.mode_prompt(cfg, "email"))

    def test_mode_prompt_unknown_mode_is_empty(self):
        self.assertEqual(vb.mode_prompt(_cfg(), "no-such-mode"), "")

    def test_intent_shorthand_string_becomes_prompt(self):
        cfg = _cfg()
        cfg["intent"] = {"custom": "Custom shorthand prompt text"}
        entry = next(m for m in vb.mode_catalog(cfg) if m["key"] == "custom")
        self.assertEqual(entry["prompt"], "Custom shorthand prompt text")
        self.assertEqual(vb.mode_prompt(cfg, "custom"), "Custom shorthand prompt text")

    def test_replace_mode_uses_its_prompt_wholesale(self):
        # The built-in "prompt" mode (Prompt Optimizer) sets replace=True, so
        # rewrite_instruction must return ITS prompt as-is, not appended to
        # the generic cleanup instruction.
        cfg = _cfg(mode="prompt")
        instr = vb.rewrite_instruction(cfg)
        self.assertEqual(instr, vb._PROMPT_OPTIMIZER)
        self.assertNotIn(vb._REWRITE, instr)


class SingleStagePromptDirect(unittest.TestCase):
    """single_stage_prompt picks the right instruction per stage kind."""

    def test_translate_kind_uses_translate_instruction(self):
        out = vb.single_stage_prompt("translate", "REWRITE_INSTR", "TXT")
        self.assertIn(vb._TRANSLATE, out)
        self.assertNotIn("REWRITE_INSTR", out)
        self.assertTrue(out.rstrip().endswith("TXT"))

    def test_optimize_kind_uses_optimize_instruction(self):
        out = vb.single_stage_prompt("optimize", "REWRITE_INSTR", "TXT")
        self.assertIn(vb._OPTIMIZE, out)
        self.assertNotIn("REWRITE_INSTR", out)

    def test_rewrite_kind_uses_the_rewrite_instruction_argument(self):
        out = vb.single_stage_prompt("rewrite", "REWRITE_INSTR", "TXT")
        self.assertIn("REWRITE_INSTR", out)
        self.assertNotIn(vb._TRANSLATE, out)
        self.assertNotIn(vb._OPTIMIZE, out)


class PerStageProcessingWithoutCombine(unittest.TestCase):
    """With combine_stages off, _process_chunk makes one LLM call PER enabled
    stage, chaining each stage's output into the next stage's input (rather
    than the single combined-prompt call combine_stages=True makes)."""

    def setUp(self):
        self._orig_fallback = vb.run_llm_fallback
        self._orig_cands = vb.candidate_backends
        vb.candidate_backends = lambda cfg: ["fake"]

    def tearDown(self):
        vb.run_llm_fallback = self._orig_fallback
        vb.candidate_backends = self._orig_cands

    def test_combine_stages_off_calls_llm_once_per_enabled_stage(self):
        calls = []

        def fake(backends, prompt, cfg):
            calls.append(prompt)
            return f"STAGE{len(calls)}"

        vb.run_llm_fallback = fake
        cfg = _cfg(translate=True, rewrite=True, optimize=True, mode="raw",
                   translate_via="llm", combine_stages=False)

        out = vb.process_text("hello world", cfg)

        self.assertEqual(len(calls), 3, "one LLM call per enabled stage")
        self.assertIn(vb._TRANSLATE, calls[0])
        self.assertIn("hello world", calls[0])   # stage 1 sees the original input
        self.assertIn("STAGE1", calls[1])        # stage 2 chains off stage 1's output
        self.assertIn("STAGE2", calls[2])        # stage 3 chains off stage 2's output
        self.assertEqual(out, "STAGE3")


if __name__ == "__main__":
    unittest.main()
