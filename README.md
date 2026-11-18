# ClipForge

Paste a stream/video link, create subtitled vertical shorts, and upload to connected YouTube and Facebook accounts. This is a **Windows desktop app**, with a command-line worker using the same pipeline.

## What works

- YouTube, Twitch, Kick, Facebook, and TikTok links supported through yt-dlp, plus local recordings. Availability depends on the source being public and downloadable; site restrictions can prevent capture.
- Bounded recording of supported live feeds from the live edge; selection of a time window from completed recordings. A job processes **one window**, not an entire stream continuously.
- Local multilingual speech recognition with real word timestamps (faster-whisper, CPU/int8). The first run downloads a speech model into `data/models`.
- Automatic highlight selection using speech/audio signals, or optional OpenAI transcript analysis.
- 1080 × 1920 H.264/AAC MP4s, burned-in phrase subtitles, editable SRT/ASS sidecars, and creator/source attribution.
- Full gameplay over a blurred background, or a center crop. These are fixed layouts; there is no automatic face/gameplay tracking.
- Automatic YouTube upload and Facebook **Page** Reels publication after account setup and enabling the destination for the job.
- Persistent jobs, saved transcription/rendering checkpoints, cancellation, and per-platform publishing status. Interrupted/uncertain uploads are held for checking rather than blindly reposted.

**TikTok is export-only in this version.** Every short gets an MP4 and caption file under the job's `tiktok` folder. Use **Open TikTok upload** and **Copy caption** in the clip library to finish posting. The app does not offer or claim unattended TikTok publication. TikTok's official posting guidelines require user control/upload consent, and exclude arbitrary cross-platform copying and private account-management utilities. An audited API client alone does not resolve those intended-use restrictions. [TikTok guidelines](https://developers.tiktok.com/docs/en/content-sharing-guidelines)

## Start on Windows

Install Python 3.11+ (including Tcl/Tk) and Node.js, then double-click **`start.cmd`**. It creates a project-local virtual environment and installs the dependencies when needed. FFmpeg is bundled through `imageio-ffmpeg`; a separate system FFmpeg installation is not needed. Node is used by yt-dlp's JavaScript support for YouTube.

In the app:

1. Paste a public link or choose a recording.
2. Choose the recording's start minute and capture window. Live streams ignore the start-minute setting and capture from the live edge.
3. Choose the maximum number of shorts and duration, plus a framing layout.
4. Start in **Local** mode. An API key is only needed for **AI** highlight mode.
5. Connect accounts in **Settings & accounts**, then enable automatic destinations when creating a job. Leave both unchecked to export only.
6. Click **Create shorts**. Find finished clips, captions, and platform status in **Clip library**.

Keep the app open while it processes. Local mode ranks spoken reactions and audio energy; it can miss silent visual moments and its titles are transcript excerpts. AI mode uses transcript context, not video understanding. Speech model accuracy varies with language, music, and overlapping speech. The default `small` multilingual model favors accuracy over startup time; choose `base` for a lighter model or `medium` for a larger one. Blank language detects automatically; use a language code such as `en`, `hi`, or `bn` when detection is unreliable. Captions preserve the spoken language; this version does not translate them.

## Connect publishing accounts

### YouTube

1. In a Google Cloud project, enable **YouTube Data API v3**.
2. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing.
3. Create an OAuth client of type **Desktop app** and download its JSON. Store it outside the tracked source, such as in `data/`.
4. Select that JSON in Settings, save, then click **Connect YouTube** and authorize the intended channel in the browser.
5. Choose `public`, `unlisted`, or `private` visibility. Enable **YouTube Shorts** for each job that should upload automatically.

OAuth consent is not a publication: uploading begins only when a job has YouTube enabled. Upload completion is tracked separately from YouTube's subsequent processing/moderation. Vertical short uploads may be classified as Shorts by YouTube; this app cannot guarantee recommendation, monetization, or reach. See the [official upload guide](https://developers.google.com/youtube/data/guides/uploading_a_video).

### Facebook

Use a **Facebook Page**, rather than a personal profile. Configure a Meta app and obtain a Page access token from a user with the Page's `CREATE_CONTENT` task. Meta documents `pages_show_list`, `pages_read_engagement`, and `pages_manage_posts` for Reels publishing; production access may require app review and the appropriate access level.

Save your Page ID and Page access token in Settings, then click **Check Facebook Page**. This confirms Page read access; actual publishing permissions are exercised on upload. Enable **Facebook Reels** for jobs that should publish automatically. The default Graph API version is `v27.0`; update it when required. Expired/revoked tokens need replacement. The connector waits for video processing and publication status; an uncertain result retains the remote video ID for investigation. [Meta Reels publishing guide](https://developers.facebook.com/documentation/video-api/guides/reels-publishing)

### Optional AI highlight selection

Add your OpenAI API key in Settings, or set `OPENAI_API_KEY` in your environment. Choose a model with Responses API structured-output support; the default is `gpt-6.2-sol`. Then select **AI** highlight mode. This sends the source title and timestamped transcript to the API and incurs API charges. Speech recognition and video rendering stay local. [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

## Command line

From this folder in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m clipforge

# Check availability without downloading the video.
.\.venv\Scripts\python.exe -m clipforge inspect "https://www.youtube.com/live/irNhWMG560k"

# Process 10 minutes from minute 20 of a recording; no account upload.
.\.venv\Scripts\python.exe -m clipforge run "https://www.youtube.com/live/irNhWMG560k" --start-minutes 20 --minutes 10 --clips 3 --seconds 40

# After connecting accounts, opt in to automatic publication.
.\.venv\Scripts\python.exe -m clipforge run "VIDEO_URL" --minutes 10 --post youtube facebook

# Synthetic rendering check; does not download media or post anything.
.\.venv\Scripts\python.exe -m clipforge demo
.\.venv\Scripts\python.exe -m clipforge status
.\.venv\Scripts\python.exe -m clipforge resume JOB_ID
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Data, credentials, and recovery

All runtime data lives in ignored `data/`: SQLite history, video windows, models, transcripts, shorts, TikTok export copies, and settings. Video files are retained so you can resume work and inspect outputs; remove completed job folders manually when no longer needed. A long/high-resolution window can use substantial disk space. Do not delete an active job's folder.

Credentials saved by this app are encrypted using Windows DPAPI for the current Windows user. On other operating systems the credential file uses owner-only file permissions instead. Google client JSON files you supply are not encrypted by this app: keep them private. Never commit tokens, API keys, downloaded footage, or the `data/` directory.

Only one processing app/CLI instance can run at once. After an interrupted process, use **Resume selected** on the affected job; saved capture/transcript/render outputs are reused. Successful per-platform uploads are not repeated on resume. `needs check` means an upload may have reached the remote service: inspect the saved remote ID and destination account before creating a replacement job. There is deliberately no automatic retry of an ambiguous publication.

Use footage you are authorized to edit and repost. Attribution is included but does not establish permission.

## Project structure

| File | Purpose |
| --- | --- |
| `clipforge/gui.py` | Native Windows desktop UI |
| `clipforge/pipeline.py` | Persistent processing and publication workflow |
| `clipforge/media.py` | Source capture, speech recognition, subtitle writing, rendering |
| `clipforge/highlights.py` | Local scoring and optional AI selection |
| `clipforge/publish.py` | YouTube OAuth/upload and Facebook Page Reels API |
| `clipforge/store.py` | SQLite jobs, clips, and per-platform delivery state |
| `clipforge/config.py` | Settings and Windows-encrypted credentials |
| `clipforge/runtime.py` | Single-worker process lock |

Live API publishing requires your accounts and credentials and is not covered by the offline tests. See [VALIDATION.md](VALIDATION.md) for observed verification results.
