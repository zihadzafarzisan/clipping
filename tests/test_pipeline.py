from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from clipforge.config import Settings
from clipforge.highlights import validate_highlights
from clipforge.media import ass_escape, subtitle_groups, write_subtitles
from clipforge.pipeline import Pipeline, validate_options
from clipforge.store import Store


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = Store(self.folder / "jobs.sqlite3")
        self.settings = Settings(self.folder / "config")

    def tearDown(self):
        self.temp.cleanup()

    def test_subtitles_clip_times_and_escape_untrusted_text(self):
        words = [{"start": 8, "end": 9, "text": "before"}, {"start": 9.5, "end": 10.4, "text": "first"},
                 {"start": 10.5, "end": 11, "text": r"{\pos(1,2)}"},
                 {"start": 13.5, "end": 14.5, "text": "last"}]
        groups = list(subtitle_groups(words, 10, 14))
        self.assertEqual(groups[0][0]["start"], 0)
        self.assertEqual(groups[-1][-1]["end"], 4)
        self.assertNotIn("{", ass_escape(words[2]["text"]))
        path = self.folder / "captions.ass"
        write_subtitles(words, 10, 14, "A title", "Creator", path)
        srt = path.with_suffix(".srt").read_text()
        self.assertNotIn("before", srt)
        self.assertIn("00:00:00,000", srt)
        self.assertIn("00:00:04,000", srt)

    def test_highlight_bounds_overlap_and_nonfinite(self):
        candidates = [
            {"start": 5, "end": 45, "title": "Best", "score": 100},
            {"start": 10, "end": 50, "title": "Duplicate", "score": 90},
            {"start": 90, "end": 200, "title": "Beyond end", "score": 80},
            {"start": float("nan"), "end": 50, "title": "Bad", "score": 70},
            {"start": 50, "end": 80, "title": "Another", "score": 60},
        ]
        chosen = validate_highlights(candidates, 100, 3, 40)
        self.assertEqual([c["title"] for c in chosen], ["Best", "Another"])

    def test_queue_claim_is_atomic(self):
        job_id = self.store.create("test", validate_options({}))
        claims = []
        def claim():
            result = self.store.claim()
            if result:
                claims.append(result["id"])
        workers = [threading.Thread(target=claim) for _ in range(6)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(claims, [job_id])

    def test_windows_credential_roundtrip(self):
        self.settings.save({"language": "en"}, {"openai_api_key": "fixture-secret"})
        reloaded = Settings(self.settings.directory)
        self.assertEqual(reloaded.secret("openai_api_key"), "fixture-secret")
        self.assertEqual(reloaded.get("language"), "en")
        import os
        if os.name == "nt":
            self.assertNotIn(b"fixture-secret", reloaded.vault_path.read_bytes())

    def test_pipeline_resume_does_not_repeat_successful_or_uncertain_posts(self):
        options = validate_options({"destinations": ["youtube", "facebook"], "clip_count": 1})
        self.settings.save({"facebook_page_id": "1234"}, {"youtube_credentials": {"fixture": True}, "facebook_page_token": "fixture"})
        job_id = self.store.create("https://youtu.be/fixture", options)
        pipeline = Pipeline(self.store, self.settings, self.folder)
        source = self.folder / "source.mp4"
        rendered = self.folder / "rendered.mp4"
        rendered.write_bytes(b"fixture-video")
        transcript = {"words": [{"start": 1, "end": 2, "text": "Hello"}], "segments": []}
        chosen = [{"start": 0, "end": 40, "title": "Fixture", "score": 90}]
        with patch("clipforge.media.download", return_value=(source, {"uploader": "Creator"})), \
             patch("clipforge.media.duration", return_value=60), \
             patch("clipforge.media.transcribe", return_value=transcript), \
             patch("clipforge.highlights.local_highlights", return_value=chosen), \
             patch("clipforge.media.render", return_value=rendered), \
             patch("clipforge.publish.upload_youtube", return_value={"remote_id": "yt123", "url": "https://youtube.com/watch?v=yt123"}) as yt, \
             patch("clipforge.publish.upload_facebook", side_effect=TimeoutError("Fixture timeout")) as fb:
            pipeline.process(self.store.job(job_id))
            pipeline.process(self.store.job(job_id))
            self.assertEqual(yt.call_count, 1)
            self.assertEqual(fb.call_count, 1)
        clips = self.store.clips(job_id)
        self.assertEqual(len(clips), 1)
        states = {d["platform"]: d["status"] for d in clips[0]["deliveries"]}
        self.assertEqual(states, {"youtube": "published", "facebook": "needs_check", "tiktok": "needs_action"})
        self.assertTrue((self.folder / "jobs" / job_id / "tiktok" / "rendered.mp4").exists())
        self.assertIn("need attention", self.store.job(job_id)["message"])

    def test_recovery_marks_ambiguous_uploads(self):
        job_id = self.store.create("test", validate_options({}))
        clip = self.store.add_clip(job_id, 0, 30, "Test", "clip.mp4", "Description")
        self.store.update(job_id, "publishing", "Uploading")
        self.store.set_delivery(clip["id"], "facebook", "working", remote_id="123")
        self.store.recover()
        self.assertEqual(self.store.job(job_id)["status"], "interrupted")
        delivery = self.store.delivery(clip["id"], "facebook")
        self.assertEqual(delivery["status"], "needs_check")
        self.assertEqual(delivery["remote_id"], "123")


if __name__ == "__main__":
    unittest.main()
