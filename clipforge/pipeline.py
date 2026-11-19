from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from . import media, highlights, publish
from .config import DATA, atomic_json

DEFAULT_OPTIONS = {"minutes": 10, "start_minutes": 0, "clip_count": 3, "clip_seconds": 40,
                   "layout": "fit", "highlight_mode": "local", "engagement_only": True, "destinations": []}


def validate_options(options):
    result = {**DEFAULT_OPTIONS, **options}
    for key, lower, upper in (("minutes", 1, 120), ("start_minutes", 0, 1440),
                              ("clip_count", 1, 10), ("clip_seconds", 15, 60)):
        try:
            result[key] = int(result[key])
        except (ValueError, TypeError):
            raise ValueError(f"{key.replace('_', ' ').title()} must be a whole number.") from None
        if not lower <= result[key] <= upper:
            raise ValueError(f"{key.replace('_', ' ').title()} must be between {lower} and {upper}.")
    if result["layout"] not in ("fit", "crop") or result["highlight_mode"] not in ("local", "ai"):
        raise ValueError("Invalid layout or highlight mode.")
    result["engagement_only"] = bool(result.get("engagement_only", True))
    if not isinstance(result["destinations"], list) or any(d not in ("youtube", "facebook", "tiktok") for d in result["destinations"]):
        raise ValueError("Automatic publishing supports YouTube, Facebook, and TikTok.")
    result["destinations"] = sorted(set(result["destinations"]))
    return result


class Pipeline:
    def __init__(self, store, settings, data=DATA):
        self.store, self.settings, self.data = store, settings, data
        self.cancel = threading.Event()
        self.stop = threading.Event()
        self.current_id = None
        self.thread = None

    def validate_connections(self, options):
        if options["highlight_mode"] == "ai" and not self.settings.secret("openai_api_key"):
            raise ValueError("Add your OpenAI API key in Settings or use Local highlight mode.")
        if "youtube" in options["destinations"] and not self.settings.secret("youtube_credentials"):
            raise ValueError("Connect your YouTube channel in Settings first.")
        if "facebook" in options["destinations"]:
            publish.facebook_page(self.settings)
        if "tiktok" in options["destinations"]:
            publish.tiktok_token(self.settings)

    def start(self):
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        while not self.stop.is_set():
            job = self.store.claim()
            if not job:
                self.stop.wait(0.5)
                continue
            self.current_id = job["id"]
            self.cancel.clear()
            if self.stop.is_set():
                self.cancel.set()
            try:
                self.process(job)
            except media.Cancelled:
                self.store.update(job["id"], "cancelled", "Cancelled. Completed clips remain in the output folder.")
            except Exception as error:
                self.store.update(job["id"], "failed", safe_error(error))
            finally:
                self.current_id = None

    def process(self, job):
        job_id = job["id"]
        options = validate_options(json.loads(job["options"]))
        self.validate_connections(options)
        folder = self.data / "jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        def update(status, message, progress):
            if self.cancel.is_set():
                raise media.Cancelled()
            self.store.update(job_id, status, message, progress)
        update("downloading", "Checking source link & engagement data", 0.02)
        source, info = media.download(job["source"], folder, options, self.cancel,
                                      lambda msg: update("downloading", msg, 0.1))
        creator = info.get("uploader") or info.get("channel") or ""
        auto_tiktok = "tiktok" in options["destinations"]
        engagement_segments = info.get("engagement_segments") or []

        if engagement_segments:
            total_segs = len(engagement_segments)
            for index, seg in enumerate(engagement_segments, 1):
                seg_path = Path(seg["path"])
                seg_dir = folder / "segments" / f"seg-{index:02d}"
                base_prog = 0.25 + (index - 1) / total_segs * 0.60
                update("transcribing", f"Transcribing high-engagement clip {index} of {total_segs}", base_prog)
                transcript = media.transcribe(
                    seg_path, seg_dir, self.settings, self.cancel,
                    lambda msg, p=base_prog: update("transcribing", msg, p + 0.05),
                )
                seg_dur = media.duration(seg_path)
                update("selecting", f"Refining highlight {index} of {total_segs}", base_prog + 0.10)
                if options["highlight_mode"] == "ai":
                    refined = highlights.ai_highlights(transcript, seg_dur, 1, options["clip_seconds"],
                                                       self.settings, info.get("title", ""))
                else:
                    refined = highlights.local_highlights(transcript, seg_dur, 1, options["clip_seconds"], seg_dir / "audio.wav")
                if refined:
                    item = refined[0]
                else:
                    clip_len = min(seg_dur, float(options["clip_seconds"]))
                    rel_start = max(0.0, min(seg.get("clip_start", seg["start"]) - seg["start"], max(0.0, seg_dur - clip_len)))
                    item = {"start": round(rel_start, 2), "end": round(min(seg_dur, rel_start + clip_len), 2),
                            "title": seg.get("title") or "High engagement highlight", "score": seg.get("score", 80)}
                update("rendering", f"Rendering short {index} of {total_segs}", base_prog + 0.14)
                output = media.render(seg_path, folder / "shorts", index, item, transcript, creator, options["layout"], self.cancel)
                abs_start = round(float(seg["start"]) + float(item["start"]), 2)
                abs_end = round(float(seg["start"]) + float(item["end"]), 2)
                description = f"{item['title']}\n\nCreator: {creator}\nSource: {job['source']}\n#gaming #shorts"
                clip = self.store.add_clip(job_id, abs_start, abs_end, item["title"], output, description)
                self.export_tiktok(clip, folder, auto_tiktok=auto_tiktok)
        else:
            total = media.duration(source)
            update("transcribing", "Preparing subtitles", 0.25)
            transcript = media.transcribe(source, folder, self.settings, self.cancel,
                                          lambda msg: update("transcribing", msg, 0.35))
            plan_path = folder / "highlights.json"
            update("selecting", "Selecting the strongest moments", 0.5)
            if plan_path.exists():
                chosen = json.loads(plan_path.read_text(encoding="utf-8"))
            elif options["highlight_mode"] == "ai":
                chosen = highlights.ai_highlights(transcript, total, options["clip_count"], options["clip_seconds"],
                                                   self.settings, info.get("title", ""))
            else:
                chosen = highlights.local_highlights(transcript, total, options["clip_count"], options["clip_seconds"], folder / "audio.wav")
            if not chosen:
                raise RuntimeError("No suitable spoken highlights were found. Try another time window or increase the capture length.")
            atomic_json(plan_path, chosen)
            for index, item in enumerate(chosen, 1):
                update("rendering", f"Rendering short {index} of {len(chosen)}", 0.55 + (index - 1) / len(chosen) * 0.3)
                output = media.render(source, folder / "shorts", index, item, transcript, creator, options["layout"], self.cancel)
                description = f"{item['title']}\n\nCreator: {creator}\nSource: {job['source']}\n#gaming #shorts"
                clip = self.store.add_clip(job_id, item["start"], item["end"], item["title"], output, description)
                self.export_tiktok(clip, folder, auto_tiktok=auto_tiktok)

        update("publishing", "Publishing to selected connected accounts", 0.88)
        uploaders = {
            "youtube": publish.upload_youtube,
            "facebook": publish.upload_facebook,
            "tiktok": publish.upload_tiktok,
        }
        failures = 0
        for clip in self.store.clips(job_id):
            for platform in options["destinations"]:
                if self.cancel.is_set():
                    raise media.Cancelled()
                previous = self.store.delivery(clip["id"], platform)
                if previous and previous["status"] == "published":
                    continue
                if previous and previous["status"] in ("working", "needs_check"):
                    failures += 1
                    continue
                self.store.set_delivery(clip["id"], platform, "working")
                def checkpoint(remote_id, clip_id=clip["id"], platform=platform):
                    self.store.set_delivery(clip_id, platform, "working", remote_id=remote_id)
                try:
                    result = uploaders[platform](clip, self.settings, self.cancel, checkpoint)
                    self.store.set_delivery(clip["id"], platform, "published", **result)
                except media.Cancelled:
                    self.store.set_delivery(clip["id"], platform, "needs_check", message="Cancelled during upload. Check destination before retrying.")
                    raise
                except Exception as error:
                    # A timeout can occur after remote publication: never blindly post a duplicate.
                    self.store.set_delivery(clip["id"], platform, "needs_check", message=safe_error(error))
                    failures += 1
        count = len(self.store.clips(job_id))
        source_note = " (high-engagement clips only)" if engagement_segments else ""
        message = f"{count} shorts ready{source_note}."
        if not auto_tiktok:
            message += " TikTok files are exported for upload."
        if failures:
            message += f" {failures} account upload(s) need attention; see clip details."
        elif options["destinations"]:
            names = "/".join(d.title() for d in options["destinations"])
            message += f" {names} uploads completed for the selected destinations."
        update("complete", message, 1.0)

    def export_tiktok(self, clip, folder, auto_tiktok=False):
        destination = folder / "tiktok" / Path(clip["path"]).name
        destination.parent.mkdir(exist_ok=True)
        if not destination.exists():
            shutil.copy2(clip["path"], destination)
        destination.with_suffix(".txt").write_text(clip["description"], encoding="utf-8")
        if not auto_tiktok:
            self.store.set_delivery(clip["id"], "tiktok", "needs_action", message="MP4 + caption exported. Open TikTok Studio to finish posting.")

    def cancel_job(self, job_id):
        if self.current_id == job_id:
            self.cancel.set()
        elif self.store.job(job_id)["status"] == "queued":
            self.store.update(job_id, "cancelled", "Cancelled before processing.")

    def shutdown(self):
        self.stop.set()
        self.cancel.set()


def safe_error(error):
    import re
    # SDK exceptions often include a full request URI. Keep diagnostics useful without credentials.
    value = re.sub(r"https?://\S+", "[service URL]", str(error))
    value = re.sub(r"(?i)(access_token|refresh_token|key|authorization)[=: ]+[^\s,;]+", r"\1=[redacted]", value)
    return value[:700] or error.__class__.__name__
