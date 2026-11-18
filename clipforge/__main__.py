from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="ClipForge: stream links to subtitled vertical shorts")
    sub = parser.add_subparsers(dest="command")
    run_parser = sub.add_parser("run", help="Process a link or a local recording")
    run_parser.add_argument("source")
    run_parser.add_argument("--minutes", type=int, default=10)
    run_parser.add_argument("--start-minutes", type=int, default=0)
    run_parser.add_argument("--clips", type=int, default=3)
    run_parser.add_argument("--seconds", type=int, default=40)
    run_parser.add_argument("--mode", choices=["local", "ai"], default="local")
    run_parser.add_argument("--layout", choices=["fit", "crop"], default="fit")
    run_parser.add_argument("--post", nargs="*", choices=["youtube", "facebook"], default=[])
    inspect = sub.add_parser("inspect", help="Check a link without downloading the video")
    inspect.add_argument("source")
    resume = sub.add_parser("resume", help="Resume a failed/cancelled/interrupted job")
    resume.add_argument("job_id")
    sub.add_parser("demo", help="Render a synthetic test clip without AI, downloads, or posting")
    sub.add_parser("status", help="List job status")
    sub.add_parser("download-model", help="Download the configured local speech model")
    args = parser.parse_args()
    from .config import DATA, Settings
    from .store import Store
    from . import media
    if args.command == "inspect":
        info = media.inspect_source(args.source)
        print(json.dumps({key: info.get(key) for key in ("title", "uploader", "duration", "live_status")}, indent=2))
    elif args.command == "status":
        for job in Store().jobs():
            print(f"{job['id']}  {job['status']:14}  {job['message']}")
    elif args.command == "download-model":
        from faster_whisper import WhisperModel
        model = Settings().get("whisper_model")
        WhisperModel(model, device="cpu", compute_type="int8", download_root=str(DATA / "models"))
        print(f"Speech model ready: {model}")
    elif args.command == "demo":
        from .demo import create_demo
        print(create_demo())
    elif args.command in ("run", "resume"):
        from .runtime import InstanceLock
        with InstanceLock():
            process_run(args)
    else:
        from .runtime import InstanceLock
        from .gui import launch
        with InstanceLock():
            launch()


def process_run(args):
    from .config import DATA, Settings
    from .store import Store
    from . import media
    from .pipeline import Pipeline, safe_error, validate_options
    store, settings = Store(), Settings()
    pipeline = Pipeline(store, settings)
    store.recover()
    if args.command == "resume":
        job_id = args.job_id
        job = store.job(job_id)
        if not job or job["status"] not in ("failed", "cancelled", "interrupted"):
            raise ValueError("Choose a failed, cancelled, or interrupted job from the status command.")
    else:
        options = validate_options({"minutes": args.minutes, "start_minutes": args.start_minutes,
            "clip_count": args.clips, "clip_seconds": args.seconds, "highlight_mode": args.mode,
            "layout": args.layout, "destinations": args.post})
        media.validate_source(args.source)
        pipeline.validate_connections(options)
        job_id = store.create(args.source, options)
    print(f"Job: {job_id}", flush=True)
    done = threading.Event()
    def progress():
        previous = ""
        while not done.wait(1):
            job = store.job(job_id)
            message = f"{job['status']}: {job['message']}"
            if message != previous:
                print(message, flush=True)
                previous = message
    threading.Thread(target=progress, daemon=True).start()
    try:
        pipeline.process(store.job(job_id))
    except KeyboardInterrupt:
        pipeline.cancel.set()
        store.update(job_id, "cancelled", "Cancelled from command line.")
        raise
    except Exception as error:
        store.update(job_id, "failed", safe_error(error))
        raise
    finally:
        done.set()
    print(store.job(job_id)["message"])
    print(f"Output: {DATA / 'jobs' / job_id}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from .pipeline import safe_error
        print(f"ClipForge: {safe_error(error)}", file=sys.stderr)
        sys.exit(1)
