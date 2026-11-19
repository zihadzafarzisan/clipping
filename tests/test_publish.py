import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clipforge.config import Settings
from clipforge.pipeline import Pipeline, validate_options
from clipforge.publish import check_tiktok, upload_facebook, upload_tiktok
from clipforge.store import Store


def response(value):
    result = Mock(ok=True, status_code=200)
    result.json.return_value = value
    return result


class FacebookPublishTests(unittest.TestCase):
    def test_publishing_waits_for_remote_confirmation_and_preserves_id(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            settings.save({"facebook_page_id": "123"}, {"facebook_page_token": "fixture-token"})
            video = folder / "clip.mp4"
            video.write_bytes(b"fixture")
            clip = {"path": str(video), "title": "Fixture", "description": "Source attribution"}
            replies = [response({"video_id": "456", "upload_url": "https://rupload.facebook.com/video-upload/456"}),
                       response({"success": True}), response({"success": True})]
            statuses = [response({"status": {"video_status": "ready"}}),
                        response({"status": {"publishing_phase": {"publish_status": "published"}}})]
            checkpoint = Mock()
            with patch("clipforge.publish.requests.post", side_effect=replies) as post, \
                 patch("clipforge.publish.requests.get", side_effect=statuses) as get:
                result = upload_facebook(clip, settings, threading.Event(), checkpoint)
            self.assertEqual(result["remote_id"], "456")
            checkpoint.assert_called_once_with("456")
            self.assertEqual(post.call_count, 3)
            self.assertEqual(get.call_count, 2)
            self.assertEqual(post.call_args.kwargs["json"]["video_state"], "PUBLISHED")
            for call in post.call_args_list + get.call_args_list:
                self.assertNotIn("fixture-token", call.args[0])
                self.assertIn("timeout", call.kwargs)

    def test_unexpected_upload_host_never_receives_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings(Path(folder))
            settings.save({"facebook_page_id": "123"}, {"facebook_page_token": "fixture-token"})
            created = response({"video_id": "456", "upload_url": "https://untrusted.example/upload"})
            with patch("clipforge.publish.requests.post", return_value=created) as post:
                with self.assertRaisesRegex(RuntimeError, "unexpected upload host"):
                    upload_facebook({}, settings, threading.Event(), Mock())
            self.assertEqual(post.call_count, 1)


class TikTokPublishTests(unittest.TestCase):
    def test_tiktok_inbox_upload_chunks_and_polls_status(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            settings.save({"tiktok_post_mode": "inbox", "tiktok_privacy": "SELF_ONLY"},
                          {"tiktok_access_token": "tt-fixture-token"})
            video = folder / "clip.mp4"
            video.write_bytes(b"0123456789")
            clip = {"path": str(video), "title": "Clutch", "description": "Clutch moment #shorts"}
            post_replies = [
                response({"data": {"publish_id": "pub_123", "upload_url": "https://open-upload.tiktokapis.com/video/pub_123"},
                          "error": {"code": "ok", "message": ""}}),
                response({"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok", "message": ""}}),
            ]
            put_reply = Mock(ok=True, status_code=201)
            checkpoint = Mock()
            with patch("clipforge.publish.requests.post", side_effect=post_replies) as post, \
                 patch("clipforge.publish.requests.put", return_value=put_reply) as put:
                result = upload_tiktok(clip, settings, threading.Event(), checkpoint)
            self.assertEqual(result["remote_id"], "pub_123")
            self.assertIn("Creator Inbox", result["message"])
            checkpoint.assert_called_once_with("pub_123")
            self.assertIn("/inbox/video/init/", post.call_args_list[0].args[0])
            self.assertEqual(put.call_count, 1)
            self.assertEqual(put.call_args.kwargs["headers"]["Content-Range"], "bytes 0-9/10")

    def test_tiktok_direct_upload_includes_post_info_and_privacy(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            settings.save({"tiktok_post_mode": "direct", "tiktok_privacy": "PUBLIC_TO_EVERYONE"},
                          {"tiktok_access_token": "tt-fixture-token"})
            video = folder / "clip.mp4"
            video.write_bytes(b"abcde")
            clip = {"path": str(video), "title": "Direct", "description": "Direct caption"}
            post_replies = [
                response({"data": {"publish_id": "pub_999", "upload_url": "https://open-upload.tiktokapis.com/video/pub_999"},
                          "error": {"code": "ok"}}),
                response({"data": {"status": "PUBLISH_COMPLETE"}, "error": {"code": "ok"}}),
            ]
            with patch("clipforge.publish.requests.post", side_effect=post_replies) as post, \
                 patch("clipforge.publish.requests.put", return_value=Mock(ok=True, status_code=201)):
                result = upload_tiktok(clip, settings, threading.Event(), Mock())
            self.assertEqual(result["remote_id"], "pub_999")
            self.assertIn("Published to TikTok", result["message"])
            init_body = post.call_args_list[0].kwargs["json"]
            self.assertEqual(init_body["post_info"]["privacy_level"], "PUBLIC_TO_EVERYONE")

    def test_tiktok_rejects_untrusted_upload_host(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            settings.save({}, {"tiktok_access_token": "tt-fixture-token"})
            video = folder / "clip.mp4"
            video.write_bytes(b"abcde")
            created = response({"data": {"publish_id": "pub_bad", "upload_url": "https://evil.example/upload"},
                                "error": {"code": "ok"}})
            with patch("clipforge.publish.requests.post", return_value=created), \
                 patch("clipforge.publish.requests.put") as put:
                with self.assertRaisesRegex(RuntimeError, "unexpected upload host"):
                    upload_tiktok({"path": str(video), "title": "T", "description": "D"},
                                  settings, threading.Event(), Mock())
            put.assert_not_called()

    def test_tiktok_check_and_pipeline_auto_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            settings = Settings(folder / "settings")
            settings.save({}, {"tiktok_access_token": "tt-fixture-token"})
            with patch("clipforge.publish.requests.post",
                       return_value=response({"data": {"creator_nickname": "ClipCreator"}, "error": {"code": "ok"}})):
                self.assertEqual(check_tiktok(settings), "ClipCreator")

            store = Store(folder / "jobs.sqlite3")
            options = validate_options({"destinations": ["tiktok"], "clip_count": 1})
            job_id = store.create("https://youtu.be/fixture", options)
            pipeline = Pipeline(store, settings, folder)
            source = folder / "source.mp4"
            rendered = folder / "short.mp4"
            rendered.write_bytes(b"video")
            transcript = {"words": [{"start": 1, "end": 2, "text": "Hi"}], "segments": []}
            chosen = [{"start": 0, "end": 30, "title": "Fixture", "score": 90}]
            with patch("clipforge.media.download", return_value=(source, {"uploader": "Creator"})), \
                 patch("clipforge.media.duration", return_value=60), \
                 patch("clipforge.media.transcribe", return_value=transcript), \
                 patch("clipforge.highlights.local_highlights", return_value=chosen), \
                 patch("clipforge.media.render", return_value=rendered), \
                 patch("clipforge.publish.upload_tiktok", return_value={"remote_id": "pub_1", "url": "", "message": "Published to TikTok."}) as tt:
                pipeline.process(store.job(job_id))
            self.assertEqual(tt.call_count, 1)
            deliveries = {d["platform"]: d["status"] for d in store.clips(job_id)[0]["deliveries"]}
            self.assertEqual(deliveries["tiktok"], "published")
