"""Durable SQLite-backed analysis job registry and bounded worker pool."""
from __future__ import annotations
import sqlite3, threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from .config import DATA_DIR

DB = DATA_DIR / "jobs.sqlite3"
LOCK = threading.Lock()
POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="aves-analysis")

def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT 'queued', error TEXT, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0
    )""")
    db.commit()
    return db

def now(): return datetime.now(timezone.utc).isoformat()

def create(job_id, project_id, owner_id):
    with connect() as db:
        db.execute("INSERT INTO jobs(id,project_id,owner_id,status,stage,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (job_id, project_id, owner_id, "queued", "queued", now(), now()))

def update(job_id, status=None, progress=None, stage=None, error=None, increment=False):
    fields, values = [], []
    for key, value in (("status",status),("progress",progress),("stage",stage),("error",error)):
        if value is not None: fields.append(f"{key}=?"); values.append(value)
    if increment: fields.append("attempts=attempts+1")
    fields.append("updated_at=?"); values.extend([now(),job_id])
    with connect() as db: db.execute(f"UPDATE jobs SET {','.join(fields)} WHERE id=?", values)

def get(job_id, owner_id):
    with connect() as db:
        row=db.execute("SELECT * FROM jobs WHERE id=? AND owner_id=?",(job_id,owner_id)).fetchone()
        return dict(row) if row else None

def recoverable():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created_at")]


def claim(job_id):
    """Atomically claim a queued job so multiple app workers cannot duplicate it."""
    with connect() as db:
        cur = db.execute("UPDATE jobs SET status='running',stage='starting',updated_at=?,attempts=attempts+1 WHERE id=? AND status='queued'", (now(), job_id))
        return cur.rowcount == 1


def delete_project(project_id, owner_id):
    with connect() as db:
        db.execute("DELETE FROM jobs WHERE project_id=? AND owner_id=?", (project_id, owner_id))
