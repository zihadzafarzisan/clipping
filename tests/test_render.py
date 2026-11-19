import tempfile
import unittest
from pathlib import Path

import av

from clipforge.demo import create_demo


class RenderTests(unittest.TestCase):
    def test_real_ffmpeg_output_has_vertical_video_audio_and_subtitles(self):
        with tempfile.TemporaryDirectory() as folder:
            path = create_demo(Path(folder))
            with av.open(str(path)) as result:
                video = result.streams.video[0]
                audio = result.streams.audio[0]
                self.assertEqual((video.width, video.height), (1080, 1920))
                self.assertEqual(video.codec_context.name, "h264")
                self.assertEqual(audio.codec_context.name, "aac")
                self.assertEqual(audio.codec_context.sample_rate, 48000)
                self.assertEqual(str(video.average_rate), "30/1")
                self.assertAlmostEqual(result.duration / av.time_base, 7, delta=0.1)
                frame = next(result.decode(video=0)).to_ndarray(format="rgb24")
                self.assertEqual(frame.shape, (1920, 1080, 3))
            captions = path.with_suffix(".srt").read_text(encoding="utf-8")
            self.assertIn("subtitle rendering", captions)
            self.assertIn("00:00:00,000", captions)
            self.assertNotIn("{\\", captions)


if __name__ == "__main__":
    unittest.main()
