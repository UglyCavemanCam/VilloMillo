"""The SQLite database: the schema, and one place that opens connections."""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS profiles (
    id TEXT PRIMARY KEY, display_name TEXT NOT NULL, digest TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- YouTube channels we follow, per profile.
CREATE TABLE IF NOT EXISTS watched_sources (
    profile_id TEXT NOT NULL, channel_id TEXT NOT NULL, handle TEXT, title TEXT,
    uploads_playlist TEXT NOT NULL, subscribers INTEGER,
    baseline_views REAL,             -- median total views of this channel's uploads 2 weeks to 3 months old
    backfilled INTEGER NOT NULL DEFAULT 0, last_checked_at TEXT,
    PRIMARY KEY (profile_id, channel_id)
);

CREATE TABLE IF NOT EXISTS videos (
    profile_id TEXT NOT NULL, video_id TEXT NOT NULL,
    found_by TEXT NOT NULL,          -- channel | topic | trending
    channel_id TEXT, channel_title TEXT,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
    duration_s INTEGER, published_at TEXT NOT NULL, language TEXT, live_status TEXT,
    views INTEGER, likes INTEGER, comments INTEGER,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'discovered',  -- discovered | eligible | held | rejected | scored
    state_reason TEXT,
    PRIMARY KEY (profile_id, video_id)
);
CREATE INDEX IF NOT EXISTS videos_state ON videos (profile_id, state);

CREATE TABLE IF NOT EXISTS metric_snapshots (
    video_id TEXT NOT NULL, taken_at TEXT NOT NULL, views INTEGER, likes INTEGER, comments INTEGER,
    PRIMARY KEY (video_id, taken_at)
);

-- Raw answers from Jev / Claude, kept so scores can be recomputed without new AI calls.
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, video_id TEXT NOT NULL,
    engine TEXT NOT NULL, model TEXT, question_set_version INTEGER NOT NULL,
    answers_json TEXT NOT NULL, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS evaluations_video ON evaluations (profile_id, video_id);

CREATE TABLE IF NOT EXISTS scores (
    profile_id TEXT NOT NULL, video_id TEXT NOT NULL,
    score REAL NOT NULL, certainty REAL NOT NULL, gate TEXT NOT NULL,  -- pass | escalate | fail
    components_json TEXT NOT NULL, flags_json TEXT NOT NULL, question_set_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, video_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    profile_id TEXT NOT NULL, video_id TEXT NOT NULL, rating INTEGER NOT NULL,  -- 1 up, -1 down
    note TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (profile_id, video_id)
);

CREATE TABLE IF NOT EXISTS quota_usage (
    day TEXT NOT NULL, bucket TEXT NOT NULL, units INTEGER NOT NULL, PRIMARY KEY (day, bucket)
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day TEXT NOT NULL, engine TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0, PRIMARY KEY (day, engine)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT UNIQUE, status TEXT NOT NULL DEFAULT 'queued',  -- queued | running | done | failed
    priority INTEGER NOT NULL DEFAULT 100, run_after TEXT NOT NULL,
    lease_owner TEXT, lease_expires TEXT, attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error TEXT, created_at TEXT NOT NULL, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_ready ON tasks (status, run_after);

CREATE TABLE IF NOT EXISTS schedules (
    name TEXT PRIMARY KEY, kind TEXT NOT NULL, profile_id TEXT, interval_s INTEGER NOT NULL,
    next_run_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, level TEXT NOT NULL,
    component TEXT NOT NULL, message TEXT NOT NULL, data_json TEXT
);
"""


class Database:
    def __init__(self, path: pathlib.Path | str):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        setup = sqlite3.connect(self.path, timeout=30)  # executescript manages its own transaction
        try:
            setup.executescript(SCHEMA)
            setup.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO NOTHING",
                (str(SCHEMA_VERSION),),
            )
            setup.commit()
        finally:
            setup.close()

    @contextmanager
    def conn(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """A transaction: commits when the block ends, rolls back if it raises."""
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()
