"""Interface tests for the STT seam (transcribe / transcribe_samples /
_load_audio_16k), with mlx-whisper stubbed so no model loads:

  - transcribe_samples routes the Whisper task: whisper_translate=True selects
    the 'translate' task, otherwise 'transcribe';
  - the language override is passed through ('auto'/empty -> None);
  - the (text, lang) result is unpacked and the text is stripped;
  - the initial_prompt override / config default is forwarded;
  - _load_audio_16k loads a synthetic WAV to mono float32 @ 16 kHz (resampling a
    non-16k input). Skipped with a clear reason if soundfile is missing.

Run: ./.venv/bin/python -m pytest tests/test_transcription.py -q
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

NO_CFG = "/nonexistent/alfred-test-config.toml"

_HAVE_SF = (importlib.util.find_spec("soundfile") is not None
            and importlib.util.find_spec("numpy") is not None)


def _cfg(**stt):
    cfg = vb.load_config(NO_CFG)
    cfg["stt"].update(stt)
    return cfg


class _FakeWhisper:
    """Stands in for the mlx_whisper module: records the kwargs of each
    transcribe() call and returns a scripted result."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append(kwargs)
        return self.result


class TranscribeSamplesRouting(unittest.TestCase):
    def setUp(self):
        self.fake = _FakeWhisper({"text": "  hello world  ", "language": "en"})
        self._saved = sys.modules.get("mlx_whisper")
        sys.modules["mlx_whisper"] = self.fake

    def tearDown(self):
        sys.modules.pop("mlx_whisper", None)
        if self._saved is not None:
            sys.modules["mlx_whisper"] = self._saved

    def test_returns_stripped_text_and_language(self):
        text, lang = vb.transcribe_samples(
            object(), _cfg(), language="en", whisper_translate=False)
        self.assertEqual(text, "hello world")     # stripped
        self.assertEqual(lang, "en")

    def test_translate_true_selects_translate_task(self):
        vb.transcribe_samples(object(), _cfg(), language=None,
                              whisper_translate=True)
        self.assertEqual(self.fake.calls[-1]["task"], "translate")

    def test_translate_false_selects_transcribe_task(self):
        vb.transcribe_samples(object(), _cfg(), language=None,
                              whisper_translate=False)
        self.assertEqual(self.fake.calls[-1]["task"], "transcribe")

    def test_language_override_passed_through(self):
        vb.transcribe_samples(object(), _cfg(), language="he",
                              whisper_translate=False)
        self.assertEqual(self.fake.calls[-1]["language"], "he")

    def test_auto_language_becomes_none(self):
        for lang in ("auto", "", None):
            vb.transcribe_samples(object(), _cfg(), language=lang,
                                  whisper_translate=False)
            self.assertIsNone(self.fake.calls[-1]["language"])

    def test_model_repo_from_config(self):
        cfg = _cfg(model="mlx-community/whisper-tiny")
        vb.transcribe_samples(object(), cfg, language=None,
                              whisper_translate=False)
        self.assertEqual(self.fake.calls[-1]["path_or_hf_repo"],
                         "mlx-community/whisper-tiny")

    def test_initial_prompt_override_forwarded(self):
        vb.transcribe_samples(object(), _cfg(), language=None,
                              whisper_translate=False,
                              initial_prompt="CONTEXT")
        self.assertEqual(self.fake.calls[-1].get("initial_prompt"), "CONTEXT")

    def test_no_initial_prompt_omits_kwarg(self):
        cfg = _cfg()
        cfg["stt"]["initial_prompt"] = ""        # ensure no config default
        vb.transcribe_samples(object(), cfg, language=None,
                              whisper_translate=False)
        self.assertNotIn("initial_prompt", self.fake.calls[-1])


class TranscribeBatchDelegates(unittest.TestCase):
    """transcribe(path,...) loads audio then delegates to transcribe_samples."""

    def test_loads_then_delegates(self):
        seen = {}
        orig_load = vb._load_audio_16k
        orig_samples = vb.transcribe_samples
        def fake_load(path):
            seen["loaded"] = path
            return "AUDIO"
        vb._load_audio_16k = fake_load
        def fake_samples(audio, cfg, *, language, whisper_translate,
                         initial_prompt=""):
            seen["audio"] = audio
            seen["wt"] = whisper_translate
            return ("done", "en")
        vb.transcribe_samples = fake_samples
        try:
            out = vb.transcribe("/tmp/x.wav", _cfg(), language="en",
                                whisper_translate=True)
        finally:
            vb._load_audio_16k = orig_load
            vb.transcribe_samples = orig_samples
        self.assertEqual(out, ("done", "en"))
        self.assertEqual(seen["loaded"], "/tmp/x.wav")
        self.assertEqual(seen["audio"], "AUDIO")   # the loaded buffer is passed on
        self.assertIs(seen["wt"], True)


@unittest.skipUnless(_HAVE_SF, "soundfile/numpy not installed")
class LoadAudio16k(unittest.TestCase):
    def _write_wav(self, samples, sr):
        import numpy as np
        import soundfile as sf
        path = str(Path(tempfile.mkdtemp()) / "a.wav")
        sf.write(path, np.asarray(samples, dtype="float32"), sr)
        return path

    def test_mono_16k_passthrough_shape_and_dtype(self):
        import numpy as np
        path = self._write_wav(np.linspace(-0.5, 0.5, 1600), 16000)
        audio = vb._load_audio_16k(path)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)
        self.assertEqual(len(audio), 1600)

    def test_resamples_non_16k_to_16k(self):
        import numpy as np
        # 1 second at 8 kHz -> 8000 samples -> resampled to ~16000.
        path = self._write_wav(np.zeros(8000), 8000)
        audio = vb._load_audio_16k(path)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(len(audio), 16000)        # round(8000 * 16000/8000)

    def test_stereo_is_downmixed_to_mono(self):
        import numpy as np
        stereo = np.zeros((1600, 2), dtype="float32")
        path = self._write_wav(stereo, 16000)
        audio = vb._load_audio_16k(path)
        self.assertEqual(audio.ndim, 1)
        self.assertEqual(len(audio), 1600)


if __name__ == "__main__":
    unittest.main()
