import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from clipforge.config import Settings
from clipforge.media import transcribe


class TranscriptionTests(unittest.TestCase):
    def test_pcm_decode_uses_waveform_and_rechecks_language_for_mixed_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            with wave.open(str(folder / "audio.wav"), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(np.zeros(16000, dtype=np.int16).tobytes())
            segment = SimpleNamespace(start=0, end=1, text="A spoken phrase", words=[
                SimpleNamespace(start=0, end=.5, word=" A"), SimpleNamespace(start=.5, end=1, word=" phrase")])
            model = Mock()
            model.transcribe.return_value = (iter([segment]), SimpleNamespace(language="en"))
            with patch("faster_whisper.WhisperModel", return_value=model), \
                 patch("faster_whisper.utils.download_model", return_value="cached-model") as download, \
                 patch("clipforge.media.run"):
                result = transcribe(folder / "source.mp4", folder, settings, threading.Event(), lambda _: None)
            waveform = model.transcribe.call_args.args[0]
            self.assertIsInstance(waveform, np.ndarray)
            self.assertEqual(waveform.dtype, np.float32)
            self.assertTrue(model.transcribe.call_args.kwargs["multilingual"])
            self.assertFalse(model.transcribe.call_args.kwargs["condition_on_previous_text"])
            self.assertEqual(result["words"][0]["text"], "A")
            self.assertTrue(download.call_args.kwargs["local_files_only"])
            self.assertTrue((folder / "transcript.json").exists())
