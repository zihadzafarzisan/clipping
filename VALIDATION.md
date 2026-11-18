# Verification

Verified locally on Windows with Python 3.14 on November 19, 2026.

## Automated checks

`python -m unittest discover -s tests -v`: **10 tests passed**.

Coverage includes:

- Atomic claiming of queued jobs across competing threads.
- Subtitle offsets at clip boundaries and escaping of ASS control characters.
- Highlight bounds, overlapping candidates, and non-finite timestamps.
- Windows DPAPI credential save/load without plaintext fixture secrets in the credential file.
- Resume behavior that does not repeat successful or ambiguous account uploads.
- Recovery of interrupted uploads while retaining the remote video ID.
- Mocked Facebook initialization/upload/publish/status flow, plus rejection of an unexpected upload host before sending credentials.
- A real FFmpeg render, read back with PyAV: 1080 × 1920 H.264, 30 fps, AAC at 48 kHz, expected duration, and subtitle sidecars.
- PCM-waveform transcription input, cached model loading, and per-segment language detection for mixed-language streams.

`python -m pip check`: **passed**. Python bytecode compilation also passed.

## Source-link smoke test

The supplied link `https://www.youtube.com/live/irNhWMG560k` resolved through yt-dlp as a completed, 35,278-second CarryisLive recording. Downloading the 20:00–21:00 window succeeded. Local speech recognition, timestamped captions, a 30-second vertical render, and an MP4/caption TikTok export package were produced without uploading to any account. Video dimensions, codecs, duration, and an extracted rendered frame were inspected.

The first test with the `base` speech model produced weak captions on mixed Hindi commentary and English game dialogue. The default was changed to `small`, and automatic mode now re-detects language per segment rather than fixing the entire recording to the initial language. Captions and highlight choice still need assessment on representative content: no transcription accuracy or highlight-quality score is claimed.

## Verification limits

- The native desktop window launched and was identified by Windows Computer Use. Visual UI inspection was not completed because the Computer Use app approval timed out.
- YouTube OAuth/production uploads and live Facebook Page publishing require the user's account configuration and were not exercised. Facebook request sequencing/status handling is covered with mocked responses; this does not establish live account permissions or production API acceptance.
- Optional OpenAI highlight selection was implemented against the documented Responses API structured-output contract, but was not called without an API key.
- Capture from an actively broadcasting stream was not exercised; the supplied link was a completed recording.
- TikTok publication is not implemented. The completed artifact is a local upload package, never a claim that the clip was posted.
- The GitHub workflow is configured; its remote result must be checked separately after pushing.

Downloaded media, models, credentials, and rendered samples remain in ignored `data/` and are excluded from Git.
