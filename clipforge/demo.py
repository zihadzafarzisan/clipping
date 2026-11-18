from __future__ import annotations

import threading
from pathlib import Path

from .config import DATA
from .media import ffmpeg, render, run


def create_demo(directory: Path = DATA / "demo"):
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "synthetic-source.mp4"
    if not source.exists():
        run([ffmpeg(), "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
             "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "7",
             "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", source])
    tokens = "This is a subtitle rendering test with a synthetic video and timed sample captions".split()
    words = [{"start": i * .4, "end": i * .4 + .35, "text": text} for i, text in enumerate(tokens)]
    return render(source, directory / "shorts", 1, {"start": 0, "end": 7, "title": "Subtitle rendering test"},
                  {"words": words}, "Synthetic test video", "fit", threading.Event())
