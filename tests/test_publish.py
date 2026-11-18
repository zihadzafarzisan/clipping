import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clipforge.config import Settings
from clipforge.publish import upload_facebook


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
