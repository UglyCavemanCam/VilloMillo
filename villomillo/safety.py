"""The emergency stop: one switch that pauses every automatic step on every channel until you resume."""

from __future__ import annotations

from . import store
from .db import Database

KEY = "emergency_stop"


def is_stopped(db: Database) -> bool:
    with db.conn() as c:
        return c.execute("SELECT 1 FROM meta WHERE key=?", (KEY,)).fetchone() is not None


def stop(db: Database) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, store.iso()))
    store.event(db, "warn", "safety", "Emergency stop pressed: everything is paused")


def resume(db: Database) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM meta WHERE key=?", (KEY,))
    store.event(db, "info", "safety", "Emergency stop released: work continues")
