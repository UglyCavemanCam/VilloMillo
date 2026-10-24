"""A durable task queue in SQLite. A task is claimed with a lease; if the worker dies, the lease
runs out and the task is picked up again, so a crash or restart never loses or doubles work."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import store
from .config import AppConfig, Profile
from .db import Database

KEEP_FINISHED_DAYS = 7


def enqueue(db: Database, kind: str, payload: dict | None = None, *, dedupe_key: str | None = None,
            run_after: datetime | None = None, priority: int = 100) -> int | None:
    """Add a task. With a dedupe_key, adding the same key twice does nothing and returns None."""
    with db.conn() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO tasks (kind, payload_json, dedupe_key, priority, run_after, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (kind, json.dumps(payload or {}), dedupe_key, priority, store.iso(run_after), store.iso()),
        )
        return cur.lastrowid if cur.rowcount else None


def claim(db: Database, worker: str, *, lease_s: int = 900, at: datetime | None = None) -> dict | None:
    """Take the next ready task (or one whose worker vanished) and lease it to `worker`."""
    at = at or store.now()
    stamp = store.iso(at)
    with db.conn(immediate=True) as c:
        c.execute(
            "UPDATE tasks SET status='failed', last_error='lease ran out too many times', finished_at=? "
            "WHERE status='running' AND lease_expires<? AND attempts>=max_attempts",
            (stamp, stamp),
        )
        row = c.execute(
            "UPDATE tasks SET status='running', lease_owner=?, lease_expires=?, attempts=attempts+1 "
            "WHERE id=(SELECT id FROM tasks WHERE (status='queued' AND run_after<=?) "
            "OR (status='running' AND lease_expires<?) ORDER BY priority, id LIMIT 1) RETURNING *",
            (worker, store.iso(at + timedelta(seconds=lease_s)), stamp, stamp),
        ).fetchone()
    return dict(row) if row else None


def complete(db: Database, task_id: int) -> None:
    with db.conn() as c:
        c.execute(
            "UPDATE tasks SET status='done', lease_owner=NULL, lease_expires=NULL, finished_at=? WHERE id=?",
            (store.iso(), task_id),
        )


def fail(db: Database, task: dict, error: str, *, at: datetime | None = None) -> str:
    """Put a failed task back for a later retry (1, 2, 4... minutes), or give up after max_attempts."""
    at = at or store.now()
    with db.conn() as c:
        if task["attempts"] >= task["max_attempts"]:
            c.execute(
                "UPDATE tasks SET status='failed', last_error=?, lease_owner=NULL, lease_expires=NULL, "
                "finished_at=? WHERE id=?",
                (error[:500], store.iso(at), task["id"]),
            )
            return "failed"
        retry = at + timedelta(minutes=2 ** (task["attempts"] - 1))
        c.execute(
            "UPDATE tasks SET status='queued', last_error=?, lease_owner=NULL, lease_expires=NULL, run_after=? "
            "WHERE id=?",
            (error[:500], store.iso(retry), task["id"]),
        )
    return "queued"


# --- schedules: "run this every N minutes" -------------------------------------------------------


def drop_removed_profiles(db: Database, known: set[str]) -> None:
    """Forget schedules and waiting tasks for channel profiles that no longer exist in the settings."""
    with db.conn() as c:
        c.execute(
            f"DELETE FROM schedules WHERE profile_id IS NOT NULL AND profile_id NOT IN ({','.join('?' * len(known)) or 'NULL'})",
            tuple(known),
        )
        for row in c.execute("SELECT id, payload_json FROM tasks WHERE status IN ('queued', 'running')").fetchall():
            profile = json.loads(row["payload_json"]).get("profile")
            if profile is not None and profile not in known:
                c.execute("DELETE FROM tasks WHERE id=?", (row["id"],))


def ensure_schedules(db: Database, app: AppConfig, profiles: dict[str, Profile], *, at: datetime | None = None) -> None:
    """Make sure each recurring job exists. Safe to call at every start."""
    at = at or store.now()
    drop_removed_profiles(db, set(profiles))
    s = app.schedule
    with db.conn() as c:
        for profile_id, profile in profiles.items():
            jobs = [("post", s.post_every_minutes)]
            for kind, minutes in jobs:
                c.execute(
                    "INSERT INTO schedules (name, kind, profile_id, interval_s, next_run_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(name) DO UPDATE SET interval_s=excluded.interval_s",
                    (f"{kind}:{profile_id}", kind, profile_id, minutes * 60, store.iso(at)),
                )


def tick_schedules(db: Database, *, at: datetime | None = None) -> int:
    """Queue every job that is due and move its next run forward. Returns how many were queued."""
    at = at or store.now()
    stamp = store.iso(at)
    queued = 0
    with db.conn(immediate=True) as c:
        due = c.execute("SELECT * FROM schedules WHERE enabled=1 AND next_run_at<=?", (stamp,)).fetchall()
        for s in due:
            slot = int(at.timestamp() // s["interval_s"])
            cur = c.execute(
                "INSERT OR IGNORE INTO tasks (kind, payload_json, dedupe_key, run_after, created_at) VALUES (?,?,?,?,?)",
                (s["kind"], json.dumps({"profile": s["profile_id"]}), f"{s['name']}:{slot}", stamp, stamp),
            )
            queued += cur.rowcount
            c.execute(
                "UPDATE schedules SET next_run_at=? WHERE name=?",
                (store.iso(at + timedelta(seconds=s["interval_s"])), s["name"]),
            )
        c.execute(
            "DELETE FROM tasks WHERE status IN ('done','failed') AND finished_at<?",
            (store.iso(at - timedelta(days=KEEP_FINISHED_DAYS)),),
        )
    return queued
