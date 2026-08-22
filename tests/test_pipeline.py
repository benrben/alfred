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


def _cfg(**processing):
    """A minimal config tree with processing overrides."""
    cfg = vb.load_config(None)  # built-in DEFAULTS (no TOML)
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


if __name__ == "__main__":
    unittest.main()
