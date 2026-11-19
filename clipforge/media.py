from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .config import DATA, atomic_json


class Cancelled(Exception):
    pass


def ffmpeg():
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def run(args, *, cwd=None, cancel=None, timeout=7200):
    """Drain child output to disk; never log signed URLs or access tokens."""
    import tempfile
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with tempfile.TemporaryFile(mode="w+b") as output:
        process = subprocess.Popen([str(x) for x in args], cwd=cwd, stdout=output,
                                   stderr=subprocess.STDOUT, creationflags=flags)
        started = time.monotonic()
        try:
            while process.poll() is None:
                if cancel and cancel.is_set():
                    raise Cancelled("Job cancelled")
                if time.monotonic() - started > timeout:
                    raise RuntimeError("Operation timed out; try a shorter capture window.")
                time.sleep(0.15)
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        output.seek(0)
        result = output.read().decode("utf-8", errors="replace")
        if process.returncode:
            # Full subprocess output may contain signed media URLs; expose only a safe error.
            errors = re.findall(r"ERROR: ([^\r\n]+)", result)
            detail = errors[-1] if errors else "Media operation failed. Check that the link is public and has audio/video."
            detail = re.sub(r"https?://\S+", "[media URL]", detail)
            raise RuntimeError(detail[:500])
        return result


def validate_source(source):
    if Path(source).is_file():
        return
    parsed = urlparse(source)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Paste a public video/stream URL or select a local video file.")
    host = parsed.hostname.lower()
    allowed = ("youtube.com", "youtu.be", "twitch.tv", "kick.com", "facebook.com", "fb.watch", "tiktok.com")
    if not any(host == domain or host.endswith("." + domain) for domain in allowed):
        raise ValueError("Supported links: YouTube, Twitch, Kick, Facebook, and TikTok. You can also select a local recording.")


def inspect_source(source, cancel=None):
    validate_source(source)
    if Path(source).is_file():
        return {"title": Path(source).stem, "uploader": "Local recording", "is_live": False, "local": True}
    raw = run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--js-runtimes", "node", "--skip-download", "--dump-single-json", source],
              cancel=cancel, timeout=150)
    # yt-dlp warnings can precede JSON on stderr.
    for line in reversed(raw.splitlines()):
        if line.startswith("{"):
            info = json.loads(line)
            if info.get("live_status") == "is_upcoming":
                raise ValueError("This stream has not started yet. Submit it when it is live.")
            return info
    raise RuntimeError("Could not read video information.")


def duration(path):
    import av
    with av.open(str(path)) as media:
        if media.duration:
            return media.duration / av.time_base
        video = media.streams.video[0]
        if video.duration:
            return float(video.duration * video.time_base)
    raise RuntimeError("Could not determine video duration.")


def download_section(source, start, end, destination, binary, cancel):
    destination.parent.mkdir(parents=True, exist_ok=True)
    stem = destination.with_suffix("")
    # Clean any stale partial files for this stem.
    for existing in destination.parent.glob(f"{stem.name}.*"):
        if existing != destination:
            existing.unlink(missing_ok=True)
    span = max(10.0, float(end) - float(start))
    run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
         "--js-runtimes", "node", "--ffmpeg-location", binary,
         "-f", "best[height<=1080][acodec!=none][vcodec!=none]/best",
         "--download-sections", f"*{start:.2f}-{end:.2f}",
         "--force-keyframes-at-cuts", "--remux-video", "mp4",
         "-o", f"{stem}.%(ext)s", source], cancel=cancel, timeout=max(300, int(span * 6)))
    if not destination.exists():
        candidates = sorted(destination.parent.glob(f"{stem.name}.*"))
        if candidates:
            candidates[0].replace(destination)
    if not destination.exists() or duration(destination) < 3:
        raise RuntimeError("The selected high-engagement section did not produce a usable video clip.")
    return destination


def download_audio_preview(source, offset, seconds, wav_path, binary, cancel):
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    raw_stem = wav_path.with_name("preview_audio")
    for existing in wav_path.parent.glob("preview_audio.*"):
        existing.unlink(missing_ok=True)
    run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
         "--js-runtimes", "node", "--ffmpeg-location", binary,
         "-f", "bestaudio/best",
         "--download-sections", f"*{offset}-{offset + seconds}",
         "-o", f"{raw_stem}.%(ext)s", source], cancel=cancel, timeout=max(300, seconds * 2))
    downloaded = sorted(wav_path.parent.glob("preview_audio.*"))
    if not downloaded:
        raise RuntimeError("Could not download audio preview for engagement detection.")
    run([binary, "-y", "-i", downloaded[0], "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
        cancel=cancel, timeout=300)
    downloaded[0].unlink(missing_ok=True)
    return wav_path


def download(source, directory, options, cancel, report):
    directory.mkdir(parents=True, exist_ok=True)
    media_path = directory / "source.mp4"
    info_path = directory / "source.json"
    if info_path.exists():
        cached_info = json.loads(info_path.read_text(encoding="utf-8"))
        cached_segments = cached_info.get("engagement_segments") or []
        if cached_segments and all(Path(seg["path"]).exists() for seg in cached_segments):
            duration(cached_segments[0]["path"])
            return Path(cached_segments[0]["path"]), cached_info
        if media_path.exists():
            duration(media_path)
            return media_path, cached_info
    info = inspect_source(source, cancel)
    seconds = int(options["minutes"]) * 60
    offset = int(options.get("start_minutes", 0)) * 60
    binary = ffmpeg()
    safe_info = {k: info.get(k) for k in ("id", "title", "uploader", "channel", "webpage_url", "is_live", "duration")}

    if options.get("engagement_only", True) and not info.get("local") and not info.get("is_live"):
        from . import highlights
        window_seconds = seconds if offset > 0 else None
        peaks = highlights.select_engagement_windows(
            info, int(options.get("clip_count", 3)), int(options.get("clip_seconds", 40)),
            start_offset=offset, window_seconds=window_seconds)
        engagement_source = "heatmap"
        if not peaks:
            report("No engagement heatmap available; scanning lightweight audio for peak moments")
            try:
                preview_wav = directory / "preview_audio.wav"
                download_audio_preview(source, offset, seconds, preview_wav, binary, cancel)
                import wave
                with wave.open(str(preview_wav)) as wav:
                    preview_dur = wav.getnframes() / float(wav.getframerate() or 16000)
                peaks = highlights.audio_engagement_peaks(
                    preview_wav, preview_dur, int(options.get("clip_count", 3)),
                    int(options.get("clip_seconds", 40)), base_offset=offset)
                engagement_source = "audio_peaks"
            except Cancelled:
                raise
            except Exception:
                peaks = []
        if peaks:
            seg_dir = directory / "segments"
            seg_dir.mkdir(parents=True, exist_ok=True)
            downloaded = []
            for idx, peak in enumerate(peaks, 1):
                report(f"Downloading high-engagement clip {idx} of {len(peaks)} (score {peak['score']:.0f}%)")
                seg_path = seg_dir / f"seg-{idx:02d}.mp4"
                if not seg_path.exists():
                    download_section(source, peak["start"], peak["end"], seg_path, binary, cancel)
                downloaded.append({**peak, "path": str(seg_path)})
            safe_info["source_offset"] = downloaded[0]["start"]
            safe_info["captured_at"] = time.time()
            safe_info["engagement_source"] = engagement_source
            safe_info["engagement_segments"] = downloaded
            atomic_json(info_path, safe_info)
            return Path(downloaded[0]["path"]), safe_info

    temporary = directory / "capture.mp4"
    if info.get("local"):
        report("Preparing selected recording")
        run([binary, "-y", "-ss", offset, "-i", str(Path(source).resolve()), "-t", seconds,
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-c:a", "aac", "-movflags", "+faststart", temporary], cancel=cancel)
    elif info.get("is_live"):
        report(f"Capturing up to {options['minutes']} minutes from the live edge")
        raw = run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
                   "--js-runtimes", "node", "-f", "best[height<=1080][acodec!=none][vcodec!=none]/best",
                   "--get-url", source], cancel=cancel, timeout=150)
        urls = [s for s in raw.splitlines() if s.startswith("https://") or s.startswith("http://")]
        if len(urls) != 1:
            raise RuntimeError("This live stream has no supported combined audio/video feed. Try its recording after the stream ends.")
        run([binary, "-y", "-rw_timeout", "30000000", "-i", urls[0], "-t", seconds,
             "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-c:a", "aac", "-movflags", "+faststart", temporary],
            cancel=cancel, timeout=seconds + 180)
    else:
        report("Downloading the selected time window")
        run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-progress",
             "--js-runtimes", "node", "--ffmpeg-location", binary,
             "-f", "best[height<=1080][acodec!=none][vcodec!=none]/best",
             "--download-sections", f"*{offset}-{offset + seconds}",
             "--force-keyframes-at-cuts", "--remux-video", "mp4",
             "-o", str(directory / "capture.%(ext)s"), source], cancel=cancel,
            timeout=max(600, seconds * 3))
    if not temporary.exists() or duration(temporary) < 3:
        raise RuntimeError("The source did not produce a usable video window.")
    temporary.replace(media_path)
    safe_info["source_offset"] = 0 if info.get("is_live") else offset
    safe_info["captured_at"] = time.time()
    atomic_json(info_path, safe_info)
    return media_path, safe_info


HINGLISH_PROMPT = (
    "Arey bhai, kya clutch tha! Let's go bro, enemy right side pe hai, jaldi cover do. "
    "Oh no, mar gaya! Wait wait, watch this gameplay."
)
MIXED_HI_EN_PROMPT = (
    "अरे भाई, क्या clutch था! Let's go bro, enemy right side पर है, जल्दी cover दो। "
    "Oh no, मर गया! Watch this gameplay."
)


def transcription_params(settings):
    """Build Whisper decoding parameters tuned for mixed Hindi/English and gaming audio."""
    raw_lang = (settings.get("language") or "").strip().lower()
    custom_prompt = (settings.get("subtitle_prompt") or "").strip()
    params = {
        "condition_on_previous_text": False,
        "word_timestamps": True,
        "vad_filter": True,
        "vad_parameters": {"min_silence_duration_ms": 400, "speech_pad_ms": 250},
        "beam_size": 5,
        "best_of": 5,
        "temperature": [0.0, 0.2, 0.4],
        "compression_ratio_threshold": 2.2,
        "no_speech_threshold": 0.55,
        "hallucination_silence_threshold": 1.5,
    }
    if raw_lang == "hinglish":
        params["language"] = "en"
        params["multilingual"] = False
        params["initial_prompt"] = custom_prompt or HINGLISH_PROMPT
    elif raw_lang in ("hi-en", "hi+en", "mixed"):
        params["language"] = None
        params["multilingual"] = True
        params["initial_prompt"] = custom_prompt or MIXED_HI_EN_PROMPT
    elif raw_lang in ("", "auto"):
        params["language"] = None
        params["multilingual"] = True
        if custom_prompt:
            params["initial_prompt"] = custom_prompt
    else:
        params["language"] = raw_lang
        params["multilingual"] = False
        if custom_prompt:
            params["initial_prompt"] = custom_prompt
    return params


def clean_words(words):
    """Remove empty, very low-confidence, or stutter-loop hallucinated tokens."""
    cleaned = []
    repeat_count = 0
    prev_norm = ""
    for item in words:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        prob = item.get("probability")
        if isinstance(prob, (int, float)) and prob < 0.15:
            continue
        norm = re.sub(r"[^\w]", "", text.lower())
        if norm and norm == prev_norm:
            repeat_count += 1
            if repeat_count >= 2:
                continue
        else:
            repeat_count = 0
            prev_norm = norm
        cleaned.append({"start": float(item["start"]), "end": float(item["end"]), "text": text})
    return cleaned


def transcribe(media_path, directory, settings, cancel, report):
    directory.mkdir(parents=True, exist_ok=True)
    transcript_path = directory / "transcript.json"
    if transcript_path.exists():
        return json.loads(transcript_path.read_text(encoding="utf-8"))
    from faster_whisper import WhisperModel
    from faster_whisper.utils import download_model
    from huggingface_hub.errors import LocalEntryNotFoundError
    audio = directory / "audio.wav"
    report("Extracting & conditioning speech audio for subtitles")
    run([ffmpeg(), "-y", "-i", media_path, "-vn",
         "-af", "highpass=f=80,lowpass=f=7800,dynaudnorm=f=150:g=15",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", audio], cancel=cancel)
    report("Loading local speech model (first use downloads model files)")
    try:
        model_path = download_model(settings.get("whisper_model"), cache_dir=str(DATA / "models"), local_files_only=True)
    except LocalEntryNotFoundError:
        model_path = download_model(settings.get("whisper_model"), cache_dir=str(DATA / "models"))
    model = WhisperModel(model_path, device="cpu", compute_type="int8",
                         cpu_threads=max(1, min(6, (os.cpu_count() or 4) - 1)))
    # Decode the known PCM/16 kHz/mono file without coupling faster-whisper's
    # decoder to version-specific PyAV keyword arguments.
    import wave
    import numpy as np
    with wave.open(str(audio)) as wav:
        waveform = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    params = transcription_params(settings)
    segments, detected = model.transcribe(waveform, **params)
    result = {"language": detected.language, "words": [], "segments": []}
    raw_words = []
    for segment in segments:
        if cancel.is_set():
            raise Cancelled()
        text = segment.text.strip()
        if text:
            result["segments"].append({"start": segment.start, "end": segment.end, "text": text})
        for word in segment.words or []:
            entry = {"start": word.start, "end": word.end, "text": word.word.strip()}
            if hasattr(word, "probability") and word.probability is not None:
                entry["probability"] = word.probability
            raw_words.append(entry)
        report(f"Transcribing: {int(segment.end // 60):02d}:{int(segment.end % 60):02d}")
    result["words"] = clean_words(raw_words)
    if not result["words"]:
        raise RuntimeError("No speech was detected. Try a section with commentary so subtitles can be created.")
    atomic_json(transcript_path, result)
    return result


def ass_time(seconds):
    ticks = max(0, round(seconds * 100))
    return f"{ticks // 360000}:{ticks // 6000 % 60:02d}:{ticks // 100 % 60:02d}.{ticks % 100:02d}"


def ass_escape(text):
    # Prevent transcript text from injecting ASS formatting or file directives.
    return str(text).replace("\\", "＼").replace("{", "(").replace("}", ")").replace("\r", " ").replace("\n", " ")


def has_indic_script(text: str) -> bool:
    return bool(re.search(r"[\u0900-\u0DFF]", text))


def subtitle_groups(words, start, end):
    group = []
    for word in words:
        if word["end"] <= start or word["start"] >= end:
            continue
        item = {"start": max(start, word["start"]) - start,
                "end": min(end, word["end"]) - start, "text": word["text"]}
        if group and (len(group) >= 5 or item["end"] - group[0]["start"] > 2.4
                      or item["start"] - group[-1]["end"] > 0.7
                      or len(" ".join(w["text"] for w in group)) + len(item["text"]) > 34):
            yield group
            group = []
        group.append(item)
        if item["text"][-1:] in (".", "?", "!", "।") and len(group) >= 2:
            yield group
            group = []
    if group:
        yield group


def write_subtitles(words, start, end, title, creator, ass_path):
    import textwrap
    groups = list(subtitle_groups(words, start, end))
    all_text = " ".join([title, creator] + [w["text"] for g in groups for w in g])
    font = "Nirmala UI" if has_indic_script(all_text) else "Arial"
    title = "\\N".join(ass_escape(line) for line in textwrap.wrap(title[:95], width=24))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},64,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,4,2,2,95,95,360,1
Style: Title,{font},74,&H00FFFFFF,&H0000FFFF,&H00101010,&H80000000,-1,0,0,0,100,100,0,0,1,3,1,8,90,90,250,1
Style: Credit,{font},34,&H00FFFFFF,&H00FFFFFF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,90,90,200,1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [f"Dialogue: 0,0:00:00.00,{ass_time(end-start)},Title,,0,0,0,,{title}"]
    if creator:
        events.append(f"Dialogue: 0,0:00:00.00,{ass_time(end-start)},Credit,,0,0,0,,{ass_escape(creator[:70])}")
    srt = []
    for index, group in enumerate(groups, 1):
        caption = ass_escape(" ".join(w["text"] for w in group))
        a, b = group[0]["start"], group[-1]["end"]
        if b <= a:
            continue
        events.append(f"Dialogue: 1,{ass_time(a)},{ass_time(b)},Caption,,0,0,0,,{caption}")
        def stamp(t):
            ms = round(t * 1000)
            return f"{ms//3600000:02d}:{ms//60000%60:02d}:{ms//1000%60:02d},{ms%1000:03d}"
        srt.append(f"{index}\n{stamp(a)} --> {stamp(b)}\n{' '.join(w['text'] for w in group)}\n")
    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    ass_path.with_suffix(".srt").write_text("\n".join(srt), encoding="utf-8")


def render(media_path, directory, index, highlight, transcript, creator, layout, cancel):
    directory.mkdir(parents=True, exist_ok=True)
    ass_path = directory / f"short-{index:02d}.ass"
    output = directory / f"short-{index:02d}.mp4"
    if output.exists():
        try:
            if abs(duration(output) - (highlight["end"] - highlight["start"])) < 0.5:
                return output
        except Exception:
            pass
    write_subtitles(transcript["words"], highlight["start"], highlight["end"], highlight["title"], creator, ass_path)
    if layout == "crop":
        graph = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1"
    else:
        graph = ("[0:v]split=2[bg][fg];[bg]scale=360:640:force_original_aspect_ratio=increase,"
                 "crop=360:640,boxblur=12:1,scale=1080:1920[back];"
                 "[fg]scale=1080:1050:force_original_aspect_ratio=decrease[front];"
                 "[back][front]overlay=(W-w)/2:(H-h)/2,setsar=1")
    # A relative trusted filename avoids Windows drive-colon escaping in libass.
    graph += f",ass={ass_path.name}[video]"
    temp = output.with_name(output.stem + ".partial.mp4")
    run([ffmpeg(), "-y", "-ss", highlight["start"], "-i", Path(media_path).resolve(),
         "-t", highlight["end"] - highlight["start"], "-filter_complex", graph,
         "-map", "[video]", "-map", "0:a:0", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "21", "-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-ar", "48000", "-ac", "2", "-movflags", "+faststart", temp.resolve()],
        cwd=directory, cancel=cancel)
    temp.replace(output)
    return output
