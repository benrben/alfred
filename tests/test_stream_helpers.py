"""Unit tests for the streaming-STT PURE helpers (no live thread, no model):

  - _wav_data_offset: finds the 'data' chunk in a WAV header (byte after the
    4-byte tag + 4-byte size), falling back to 44 when absent/unreadable;
  - _pcm_sample_count: bytes-after-data / 2 (int16 mono);
  - _read_pcm_f32: int16 PCM in [start, end) -> float32 in [-1, 1];
  - _silence_cut: picks the cut at the quietest 50 ms (800-sample) frame inside
    [target, hard_max], so chunks break at a pause not mid-word.

We drive these on synthetic bytes/arrays we build by hand — the live background
chunk loop (StreamSession._run) is integration, out of scope here.

Run: ./.venv/bin/python -m pytest tests/test_stream_helpers.py -q
"""

import importlib.util
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import voicebridge as vb  # noqa: E402

_HAVE_NP = importlib.util.find_spec("numpy") is not None


def _wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """A minimal canonical 16-bit-mono WAV (44-byte header + PCM)."""
    byte_rate = sample_rate * 2
    return (
        b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, byte_rate, 2, 16)
        + b"data" + struct.pack("<I", len(pcm)) + pcm
    )


def _write(data: bytes) -> str:
    path = str(Path(tempfile.mkdtemp()) / "s.wav")
    with open(path, "wb") as f:
        f.write(data)
    return path


class WavDataOffset(unittest.TestCase):
    def test_canonical_header_offset_is_44(self):
        path = _write(_wav_bytes(b"\x00\x00" * 10))
        # 'data' starts at byte 36; offset = 36 + 8 = 44 for a canonical header.
        self.assertEqual(vb._wav_data_offset(path), 44)

    def test_data_chunk_after_extra_chunk(self):
        # Insert a LIST chunk before 'data' so the offset is NOT the default 44.
        extra = b"LIST" + struct.pack("<I", 4) + b"INFO"
        head = (b"RIFF" + struct.pack("<I", 4) + b"WAVE"
                + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16)
                + extra
                + b"data" + struct.pack("<I", 8) + b"\x00" * 8)
        path = _write(head)
        off = vb._wav_data_offset(path)
        self.assertEqual(off, head.find(b"data") + 8)
        self.assertNotEqual(off, 44)

    def test_missing_data_chunk_falls_back_to_44(self):
        path = _write(b"RIFF" + b"\x00" * 100)   # no 'data' tag
        self.assertEqual(vb._wav_data_offset(path), 44)

    def test_unreadable_path_falls_back_to_44(self):
        self.assertEqual(vb._wav_data_offset("/no/such/file.wav"), 44)


class PcmSampleCount(unittest.TestCase):
    def test_counts_int16_samples_after_data(self):
        pcm = b"\x01\x00" * 100          # 100 int16 samples
        path = _write(_wav_bytes(pcm))
        off = vb._wav_data_offset(path)
        self.assertEqual(vb._pcm_sample_count(path, off), 100)

    def test_empty_pcm_is_zero(self):
        path = _write(_wav_bytes(b""))
        off = vb._wav_data_offset(path)
        self.assertEqual(vb._pcm_sample_count(path, off), 0)

    def test_missing_file_is_zero(self):
        self.assertEqual(vb._pcm_sample_count("/no/such.wav", 44), 0)


@unittest.skipUnless(_HAVE_NP, "numpy not installed")
class ReadPcmF32(unittest.TestCase):
    def test_int16_to_float32_normalized(self):
        import numpy as np
        # Samples: 0, 16384 (~0.5), -32768 (-1.0), 32767 (~+1.0)
        vals = [0, 16384, -32768, 32767]
        pcm = b"".join(struct.pack("<h", v) for v in vals)
        path = _write(_wav_bytes(pcm))
        off = vb._wav_data_offset(path)
        buf = vb._read_pcm_f32(path, off, 0, None)
        self.assertEqual(buf.dtype, np.float32)
        self.assertEqual(len(buf), 4)
        np.testing.assert_allclose(
            buf, np.array(vals, dtype=np.float32) / 32768.0, rtol=0, atol=1e-6)

    def test_windowed_read_start_end(self):
        import numpy as np
        vals = list(range(0, 10))         # 10 ascending samples
        pcm = b"".join(struct.pack("<h", v) for v in vals)
        path = _write(_wav_bytes(pcm))
        off = vb._wav_data_offset(path)
        buf = vb._read_pcm_f32(path, off, 2, 5)     # samples [2,5) -> 3 samples
        self.assertEqual(len(buf), 3)
        np.testing.assert_allclose(
            buf, np.array([2, 3, 4], dtype=np.float32) / 32768.0, atol=1e-6)

    def test_empty_window_returns_empty_float32(self):
        import numpy as np
        path = _write(_wav_bytes(b"\x01\x00" * 5))
        off = vb._wav_data_offset(path)
        buf = vb._read_pcm_f32(path, off, 5, 5)     # zero-width window
        self.assertEqual(len(buf), 0)
        self.assertEqual(buf.dtype, np.float32)


@unittest.skipUnless(_HAVE_NP, "numpy not installed")
class SilenceCut(unittest.TestCase):
    FRAME = 800            # vb._STREAM_FRAME

    def test_short_buffer_returns_full_length(self):
        import numpy as np
        buf = np.ones(500, dtype=np.float32)
        # len <= target -> no cut, return len(buf).
        self.assertEqual(vb._silence_cut(buf, target=1000, hard_max=2000),
                         len(buf))

    def test_cuts_at_quietest_frame(self):
        import numpy as np
        target, hard_max = 1600, 1600 + self.FRAME * 4   # 4 candidate frames
        # Loud everywhere, but frame index 2 (within the search region) is silent.
        buf = np.ones(hard_max + self.FRAME, dtype=np.float32) * 0.8
        quiet_frame = 2
        s = target + quiet_frame * self.FRAME
        buf[s:s + self.FRAME] = 0.0
        cut = vb._silence_cut(buf, target, hard_max)
        # Cut lands at the centre of the quiet frame: target + i*FRAME + FRAME//2.
        self.assertEqual(cut, target + quiet_frame * self.FRAME + self.FRAME // 2)

    def test_cut_within_target_hard_max_window(self):
        import numpy as np
        target, hard_max = 1600, 1600 + self.FRAME * 3
        buf = np.random.RandomState(0).randn(hard_max + 200).astype(np.float32)
        cut = vb._silence_cut(buf, target, hard_max)
        self.assertGreaterEqual(cut, target)
        self.assertLessEqual(cut, hard_max)

    def test_no_full_frame_in_region_returns_hard_max(self):
        import numpy as np
        # Region [target, hard_max] shorter than one frame -> returns hi.
        target, hard_max = 1000, 1000 + 300        # 300 < 800-sample frame
        buf = np.ones(hard_max, dtype=np.float32)
        cut = vb._silence_cut(buf, target, hard_max)
        self.assertEqual(cut, min(hard_max, len(buf)))


if __name__ == "__main__":
    unittest.main()
