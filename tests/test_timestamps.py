"""Tests for --timestamps: _format_ts / _format_segments (the pure formatting
layer) and the transcribe_samples plumbing that turns Whisper segments into
'[m:ss] …' lines. Front-ends (Private Room's audio import) pair the flag with
--transcribe-only, so no LLM-stage interaction is tested here.

Run: ./.venv/bin/python -m pytest tests/test_timestamps.py -q
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"


class FormatTs(unittest.TestCase):
    def test_minutes_seconds(self):
        self.assertEqual(vb._format_ts(0), "[0:00]")
        self.assertEqual(vb._format_ts(65.9), "[1:05]")
        self.assertEqual(vb._format_ts(754), "[12:34]")

    def test_hours(self):
        self.assertEqual(vb._format_ts(3600), "[1:00:00]")
        self.assertEqual(vb._format_ts(3600 + 754), "[1:12:34]")

    def test_negative_clamped(self):
        self.assertEqual(vb._format_ts(-3), "[0:00]")


class FormatSegments(unittest.TestCase):
    def test_lines_per_segment(self):
        segs = [
            {"start": 0.0, "text": " Hello there. "},
            {"start": 65.2, "text": "Second thought."},
        ]
        self.assertEqual(vb._format_segments(segs),
                         "[0:00] Hello there.\n[1:05] Second thought.")

    def test_empty_and_malformed_segments_skipped(self):
        segs = [
            {"start": 1.0, "text": "   "},          # empty -> dropped
            {"start": "not-a-number", "text": "x"},  # malformed -> dropped
            None,                                    # malformed -> dropped
            {"start": 2.0, "text": "kept"},
        ]
        self.assertEqual(vb._format_segments(segs), "[0:02] kept")

    def test_none_and_empty_input(self):
        self.assertEqual(vb._format_segments(None), "")
        self.assertEqual(vb._format_segments([]), "")


class TranscribeSamplesTimestamps(unittest.TestCase):
    """transcribe_samples(timestamps=True) formats segments; falls back to the
    plain text when segments are absent."""

    def _run(self, result, timestamps):
        cfg = vb.load_config(NO_CFG)
        fake = mock.MagicMock()
        fake.transcribe.return_value = result
        with mock.patch.dict(sys.modules, {"mlx_whisper": fake}):
            return vb.transcribe_samples(
                [0.0], cfg, language=None, whisper_translate=False,
                timestamps=timestamps)

    def test_timestamps_on(self):
        text, lang = self._run(
            {"text": "Hello there. Bye.", "language": "en",
             "segments": [{"start": 0.0, "text": "Hello there."},
                          {"start": 3.5, "text": "Bye."}]},
            timestamps=True)
        self.assertEqual(text, "[0:00] Hello there.\n[0:03] Bye.")
        self.assertEqual(lang, "en")

    def test_timestamps_off_unchanged(self):
        text, _ = self._run(
            {"text": "Hello there.", "language": "en",
             "segments": [{"start": 0.0, "text": "Hello there."}]},
            timestamps=False)
        self.assertEqual(text, "Hello there.")

    def test_fallback_to_plain_text_without_segments(self):
        text, _ = self._run(
            {"text": "Plain only.", "language": "en", "segments": []},
            timestamps=True)
        self.assertEqual(text, "Plain only.")


class CliFlag(unittest.TestCase):
    def test_parser_accepts_timestamps(self):
        parser = vb.build_parser()
        args = parser.parse_args(
            ["process", "x.wav", "--transcribe-only", "--timestamps",
             "--stdout"])
        self.assertTrue(args.timestamps)

    def test_default_is_none(self):
        parser = vb.build_parser()
        args = parser.parse_args(["process", "x.wav"])
        self.assertIsNone(args.timestamps)


if __name__ == "__main__":
    unittest.main()
