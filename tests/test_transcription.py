import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from clipforge.config import Settings
from clipforge.highlights import local_highlights
from clipforge.media import (
    HINGLISH_PROMPT,
    MIXED_HI_EN_PROMPT,
    clean_words,
    subtitle_groups,
    transcribe,
    transcription_params,
    write_subtitles,
)


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

    def test_hinglish_and_mixed_language_transcription_params(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder))
            settings.save({"language": "hinglish"})
            hinglish = transcription_params(settings)
            self.assertEqual(hinglish["language"], "en")
            self.assertFalse(hinglish["multilingual"])
            self.assertEqual(hinglish["initial_prompt"], HINGLISH_PROMPT)
            self.assertGreaterEqual(hinglish["beam_size"], 5)

            settings.save({"language": "hi-en"})
            mixed = transcription_params(settings)
            self.assertIsNone(mixed["language"])
            self.assertTrue(mixed["multilingual"])
            self.assertEqual(mixed["initial_prompt"], MIXED_HI_EN_PROMPT)

    def test_clean_words_filters_low_confidence_and_stutters(self):
        raw = [
            {"start": 0.0, "end": 0.4, "text": "Arey", "probability": 0.92},
            {"start": 0.4, "end": 0.7, "text": "noise", "probability": 0.05},
            {"start": 0.7, "end": 1.0, "text": "bhai", "probability": 0.85},
            {"start": 1.0, "end": 1.2, "text": "bhai", "probability": 0.80},
            {"start": 1.2, "end": 1.4, "text": "bhai", "probability": 0.75},
            {"start": 1.4, "end": 1.6, "text": "bhai", "probability": 0.70},
        ]
        cleaned = clean_words(raw)
        self.assertEqual([w["text"] for w in cleaned], ["Arey", "bhai", "bhai"])

    def test_devanagari_subtitles_use_nirmala_ui_and_break_on_punctuation(self):
        with tempfile.TemporaryDirectory() as folder:
            ass_path = Path(folder) / "hindi.ass"
            words = [
                {"start": 0.0, "end": 0.4, "text": "अरे"},
                {"start": 0.4, "end": 0.8, "text": "भाई!"},
                {"start": 0.8, "end": 1.2, "text": "Let's"},
                {"start": 1.2, "end": 1.6, "text": "go!"},
            ]
            groups = list(subtitle_groups(words, 0, 2))
            self.assertEqual(len(groups), 2)
            write_subtitles(words, 0, 2, "अरे भाई clutch!", "Creator", ass_path)
            content = ass_path.read_text(encoding="utf-8-sig")
            self.assertIn("Style: Caption,Nirmala UI,", content)

    def test_hinglish_keywords_boost_local_highlights(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = {
                "segments": [
                    {"start": 0.0, "end": 5.0, "text": "just walking around slowly now"},
                    {"start": 30.0, "end": 35.0, "text": "Arey bhai kya clutch maar diya!"},
                ],
                "words": [
                    {"start": i * 0.5, "end": i * 0.5 + 0.4, "text": w}
                    for i, w in enumerate(["just", "walking", "around", "slowly", "now"])
                ] + [
                    {"start": 30.0 + i * 0.5, "end": 30.4 + i * 0.5, "text": w}
                    for i, w in enumerate(["Arey", "bhai", "kya", "clutch", "maar", "diya!"])
                ],
            }
            chosen = local_highlights(transcript, 60.0, 1, 20, Path(folder) / "missing.wav")
            self.assertEqual(len(chosen), 1)
            self.assertIn("clutch", chosen[0]["title"])
