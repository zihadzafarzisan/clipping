from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path = DATA / "jobs.sqlite3"):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, options TEXT NOT NULL,
                    status TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
                    updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS clips (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    start REAL NOT NULL, end REAL NOT NULL, title TEXT NOT NULL,
                    path TEXT NOT NULL, description TEXT NOT NULL,
                    UNIQUE(job_id, start, end)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    clip_id TEXT NOT NULL REFERENCES clips(id), platform TEXT NOT NULL,
                    status TEXT NOT NULL, remote_id TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT '',
                    updated TEXT NOT NULL, PRIMARY KEY(clip_id, platform)
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='interrupted', message='App stopped during this job. Resume when ready.' WHERE status NOT IN ('queued','complete','failed','cancelled','interrupted')")
            db.execute("UPDATE deliveries SET status='needs_check', message='App stopped during upload. Check the destination before retrying.' WHERE status='working'")

    def create(self, source, options):
        job_id = uuid.uuid4().hex[:12]
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,source,options,status,created,updated) VALUES (?,?,?,'queued',?,?)", (job_id, source, json.dumps(options), now(), now()))
        return job_id

    def update(self, job_id, status, message, progress=None):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status=?,message=?,progress=COALESCE(?,progress),updated=? WHERE id=?", (status, message, progress, now(), job_id))

    def jobs(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC, rowid DESC")]

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET status='starting',updated=? WHERE id=?", (now(), row["id"]))
            return dict(row) if row else None

    def add_clip(self, job_id, start, end, title, path, description):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO clips VALUES (?,?,?,?,?,?,?)", (uuid.uuid4().hex[:12], job_id, start, end, title, str(path), description))
            return dict(db.execute("SELECT * FROM clips WHERE job_id=? AND start=? AND end=?", (job_id, start, end)).fetchone())

    def clips(self, job_id=None):
        with self.connect() as db:
            if job_id:
                rows = db.execute("SELECT * FROM clips WHERE job_id=? ORDER BY start", (job_id,))
            else:
                rows = db.execute("SELECT * FROM clips ORDER BY rowid DESC")
            result = [dict(r) for r in rows]
            for item in result:
                item["deliveries"] = [dict(r) for r in db.execute("SELECT * FROM deliveries WHERE clip_id=?", (item["id"],))]
            return result

    def delivery(self, clip_id, platform):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deliveries WHERE clip_id=? AND platform=?", (clip_id, platform)).fetchone()
            return dict(row) if row else None

    def set_delivery(self, clip_id, platform, status, remote_id="", url="", message=""):
        with self.connect() as db:
            db.execute("""INSERT INTO deliveries VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(clip_id,platform) DO UPDATE SET status=excluded.status,
                remote_id=CASE WHEN excluded.remote_id='' THEN deliveries.remote_id ELSE excluded.remote_id END,
                url=CASE WHEN excluded.url='' THEN deliveries.url ELSE excluded.url END,
                message=excluded.message,updated=excluded.updated""", (clip_id, platform, status, remote_id, url, message, now()))
