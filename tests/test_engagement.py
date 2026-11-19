from __future__ import annotations

import tempfile
import threading
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

from clipforge.config import Settings
from clipforge.highlights import audio_engagement_peaks, select_engagement_windows
from clipforge.media import download
from clipforge.pipeline import Pipeline, validate_options
from clipforge.store import Store


class EngagementDownloadTests(unittest.TestCase):
    def test_heatmap_selects_only_high_engagement_non_overlapping_peaks(self):
        info = {
            "duration": 3600,
            "chapters": [
                {"start_time": 0, "end_time": 1200, "title": "Warmup"},
                {"start_time": 1200, "end_time": 2400, "title": "Insane Clutch"},
                {"start_time": 2400, "end_time": 3600, "title": "Final Boss"},
            ],
            "heatmap": [
                {"start_time": 0, "end_time": 300, "value": 0.12},
                {"start_time": 300, "end_time": 600, "value": 0.18},
                {"start_time": 1300, "end_time": 1360, "value": 0.95},
                {"start_time": 1320, "end_time": 1380, "value": 0.88},  # overlaps previous peak
                {"start_time": 1800, "end_time": 2100, "value": 0.20},
                {"start_time": 2800, "end_time": 2860, "value": 0.82},
                {"start_time": 3200, "end_time": 3500, "value": 0.15},
            ],
        }
        windows = select_engagement_windows(info, count=3, length=40, padding=5.0)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["title"], "Insane Clutch")
        self.assertEqual(windows[1]["title"], "Final Boss")
        self.assertGreater(windows[0]["score"], 80)
        # Each downloaded section is tight (~clip length + 2*padding), not the 1-hour video.
        for w in windows:
            self.assertLessEqual(w["end"] - w["start"], 50.5)

    def test_heatmap_returns_empty_when_engagement_is_low(self):
        info = {
            "duration": 600,
            "heatmap": [
                {"start_time": 0, "end_time": 200, "value": 0.10},
                {"start_time": 200, "end_time": 400, "value": 0.20},
                {"start_time": 400, "end_time": 600, "value": 0.15},
            ],
        }
        self.assertEqual(select_engagement_windows(info, count=3, length=40), [])

    def test_audio_engagement_peaks_detects_loud_spikes(self):
        with tempfile.TemporaryDirectory() as folder:
            wav_path = Path(folder) / "preview.wav"
            rate = 16000
            total_sec = 180
            samples = np.full(rate * total_sec, 300, dtype=np.int16)
            # Inject two loud excitement spikes at 40s and 130s.
            samples[38 * rate:44 * rate] = 24000
            samples[128 * rate:134 * rate] = 20000
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(rate)
                wav.writeframes(samples.tobytes())
            peaks = audio_engagement_peaks(wav_path, total=total_sec, count=2, length=30, padding=4.0)
            self.assertEqual(len(peaks), 2)
            self.assertAlmostEqual(peaks[0]["peak_time"], 40, delta=6)
            self.assertAlmostEqual(peaks[1]["peak_time"], 130, delta=6)

    def test_download_only_fetches_high_engagement_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            info = {
                "id": "vid123",
                "title": "Long Stream",
                "uploader": "Creator",
                "is_live": False,
                "duration": 7200,
                "heatmap": [
                    {"start_time": 0, "end_time": 600, "value": 0.10},
                    {"start_time": 1200, "end_time": 1240, "value": 0.92},
                    {"start_time": 4500, "end_time": 4540, "value": 0.85},
                ],
            }
            sections_downloaded = []

            def fake_download_section(source, start, end, destination, binary, cancel):
                sections_downloaded.append((start, end))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fake-mp4")
                return destination

            options = validate_options({"clip_count": 2, "clip_seconds": 30, "engagement_only": True})
            with patch("clipforge.media.inspect_source", return_value=info), \
                 patch("clipforge.media.ffmpeg", return_value="ffmpeg"), \
                 patch("clipforge.media.download_section", side_effect=fake_download_section), \
                 patch("clipforge.media.run") as full_run:
                path, saved_info = download("https://youtu.be/vid123", folder, options, threading.Event(), lambda _: None)
            self.assertEqual(len(sections_downloaded), 2)
            self.assertEqual(saved_info["engagement_source"], "heatmap")
            self.assertEqual(len(saved_info["engagement_segments"]), 2)
            self.assertTrue(path.exists())
            full_run.assert_not_called()

    def test_pipeline_processes_engagement_segments_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            store = Store(folder / "jobs.sqlite3")
            settings = Settings(folder / "config")
            options = validate_options({"clip_count": 2, "clip_seconds": 30, "engagement_only": True})
            job_id = store.create("https://youtu.be/vid123", options)
            pipeline = Pipeline(store, settings, folder)

            seg1 = folder / "seg-01.mp4"
            seg2 = folder / "seg-02.mp4"
            seg1.write_bytes(b"seg1")
            seg2.write_bytes(b"seg2")
            rendered = folder / "short.mp4"
            rendered.write_bytes(b"rendered")
            info = {
                "uploader": "Carry",
                "title": "Stream",
                "engagement_source": "heatmap",
                "engagement_segments": [
                    {"path": str(seg1), "start": 1000.0, "end": 1042.0, "clip_start": 1006.0, "clip_end": 1036.0, "score": 95.0, "title": "Peak 1"},
                    {"path": str(seg2), "start": 3000.0, "end": 3042.0, "clip_start": 3006.0, "clip_end": 3036.0, "score": 88.0, "title": "Peak 2"},
                ],
            }
            transcript = {"words": [{"start": 1.0, "end": 2.0, "text": "Arey bhai!"}], "segments": [{"start": 1.0, "end": 3.0, "text": "Arey bhai!"}]}
            with patch("clipforge.media.download", return_value=(seg1, info)), \
                 patch("clipforge.media.duration", return_value=42.0), \
                 patch("clipforge.media.transcribe", return_value=transcript) as tr, \
                 patch("clipforge.highlights.local_highlights", return_value=[{"start": 5.0, "end": 35.0, "title": "Arey bhai!", "score": 10.0}]), \
                 patch("clipforge.media.render", return_value=rendered) as rnd:
                pipeline.process(store.job(job_id))
            self.assertEqual(tr.call_count, 2)
            self.assertEqual(rnd.call_count, 2)
            clips = store.clips(job_id)
            self.assertEqual(len(clips), 2)
            self.assertEqual(clips[0]["start"], 1005.0)
            self.assertEqual(clips[1]["start"], 3005.0)
            self.assertIn("high-engagement clips only", store.job(job_id)["message"])


if __name__ == "__main__":
    unittest.main()
