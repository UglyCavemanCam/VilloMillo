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

CREATE TABLE IF NOT EXISTS videos (
    profile_id TEXT NOT NULL, video_id TEXT NOT NULL,
    found_by TEXT NOT NULL,          -- paste | research | campaign | manual
    channel_id TEXT, channel_title TEXT,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
    duration_s INTEGER, published_at TEXT NOT NULL, language TEXT, live_status TEXT,
    views INTEGER, likes INTEGER, comments INTEGER,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'discovered',  -- discovered | clipped | held | rejected
    state_reason TEXT,
    PRIMARY KEY (profile_id, video_id)
);
CREATE INDEX IF NOT EXISTS videos_state ON videos (profile_id, state);

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

-- The channels you have added in the dashboard. Each is a category with its YouTube channel.
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY, data_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);

-- One pasted link on its way from "look at it" to posted clips. The plan (scores, kind, channel) is kept as JSON.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL DEFAULT 'paste', url TEXT NOT NULL, platform TEXT NOT NULL,
    video_id TEXT NOT NULL, profile_id TEXT NOT NULL, title TEXT, channel TEXT, duration REAL,
    state TEXT NOT NULL,   -- analysing | planned | nothing | making | finished | removed | failed
    note TEXT, preview_job_id INTEGER, render_job_id INTEGER, plan_json TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    campaign_id TEXT           -- the Content Rewards campaign this video is being clipped for, if any
);
CREATE INDEX IF NOT EXISTS runs_state ON runs (state, id);

-- Content Rewards campaigns: seen on the public list or pasted by link, vetted, and (once you join by hand) clipped for.
CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, brand TEXT, category TEXT, platforms_json TEXT NOT NULL DEFAULT '[]', type TEXT,
    status TEXT, requires_application INTEGER NOT NULL DEFAULT 0, description TEXT,
    rate_cents INTEGER,                -- what it pays per 1,000 views on YouTube (null when YouTube is not allowed)
    min_payout_cents INTEGER, max_payout_cents INTEGER, budget_total_cents INTEGER, budget_spent_cents INTEGER, budget_left_cents INTEGER,
    creators INTEGER, url TEXT NOT NULL,
    state TEXT NOT NULL,               -- recommended | skipped | joined | dropped
    reason TEXT,                       -- why it is recommended, skipped or paused
    rules_json TEXT, tags TEXT NOT NULL DEFAULT '', sources TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL, fetched_at TEXT NOT NULL, detail_at TEXT, joined_at TEXT
);

-- The videos queued for a campaign. The run that handles each one carries the rest of the story.
CREATE TABLE IF NOT EXISTS campaign_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, video_id TEXT NOT NULL, url TEXT NOT NULL, title TEXT,
    run_id INTEGER, created_at TEXT NOT NULL, UNIQUE (campaign_id, video_id)
);

-- A posted clip's link, handed in to the campaign by you (Content Rewards has no way for us to do it).
CREATE TABLE IF NOT EXISTS campaign_submissions (
    post_id INTEGER PRIMARY KEY, campaign_id TEXT NOT NULL, submitted_at TEXT NOT NULL
);

-- Research: channels it watches (yours, and ones it learned from a Creative Commons hit), and every video it looked at.
CREATE TABLE IF NOT EXISTS research_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT NOT NULL, channel_key TEXT NOT NULL, name TEXT NOT NULL,
    learned INTEGER NOT NULL DEFAULT 0,   -- 1 when Research added it itself after a Creative Commons video from it
    added_at TEXT NOT NULL, checked_at TEXT, UNIQUE (platform, channel_key)
);
CREATE TABLE IF NOT EXISTS research_candidates (
    video_id TEXT PRIMARY KEY, platform TEXT NOT NULL, url TEXT NOT NULL, title TEXT, channel TEXT, duration_s INTEGER, views INTEGER,
    published_at TEXT, via TEXT NOT NULL,     -- watch | search
    topic TEXT, rank REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL,                      -- queued | started | skipped
    reason TEXT, category TEXT, run_id INTEGER, found_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_candidates_state ON research_candidates (state, found_at);
CREATE TABLE IF NOT EXISTS research_searches (topic TEXT PRIMARY KEY, searched_at TEXT NOT NULL);

-- What the system is doing, one line at a time: every step of a video, Research and Campaigns writes here so you can watch it work.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
    kind TEXT NOT NULL,        -- paste | research | campaign | system
    run_id INTEGER, step TEXT, level TEXT NOT NULL DEFAULT 'info', message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS activity_run ON activity (run_id, id);
CREATE INDEX IF NOT EXISTS activity_kind ON activity (kind, id);

-- What Jev said about a piece of text, kept so the same text always gets the same scores and is never paid for twice.
CREATE TABLE IF NOT EXISTS readings (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, answers_json TEXT NOT NULL, model TEXT, created_at TEXT NOT NULL
);

-- Every paid AI call with a time, so the spend breaker can count what was spent since your last approval.
CREATE TABLE IF NOT EXISTS spend_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, engine TEXT NOT NULL, cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_log_engine ON spend_log (engine, at);

-- One clipping run on the engine per source video.
CREATE TABLE IF NOT EXISTS clip_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, video_id TEXT NOT NULL,
    engine_job_id INTEGER, status TEXT NOT NULL,   -- submitted | done | failed
    error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (profile_id, video_id)
);

-- Clips the engine made for us, checked and waiting for your decision.
CREATE TABLE IF NOT EXISTS clips_ready (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, engine_clip_id INTEGER NOT NULL,
    video_id TEXT NOT NULL, start_s REAL NOT NULL, end_s REAL NOT NULL, engine_score INTEGER,
    hook TEXT, file_path TEXT, validation_json TEXT NOT NULL, grade_json TEXT NOT NULL,
    state TEXT NOT NULL,        -- review | rejected_auto | failed_validation | approved | rejected | removed
    reason TEXT, created_at TEXT NOT NULL, decided_at TEXT,
    category TEXT,              -- what kind of clip this is; it can only post on the channel for that category
    campaign_id TEXT,           -- the campaign this clip was made for (its rules, tags and link submission)
    removed_from TEXT,          -- the state it was in when you removed it (so Restore knows where it goes back)
    removed_at TEXT,
    UNIQUE (profile_id, engine_clip_id)
);

-- Approved clips on their way out. One row per clip and platform.
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id TEXT NOT NULL, clip_id INTEGER NOT NULL,
    platform TEXT NOT NULL, route TEXT NOT NULL,    -- api | manual
    status TEXT NOT NULL,       -- queued | blocked | dry_run | manual_ready | uploaded | failed | posted | cancelled
    title TEXT NOT NULL, caption TEXT NOT NULL, tags_json TEXT NOT NULL,
    scheduled_at TEXT NOT NULL, posted_at TEXT, url TEXT, error TEXT, created_at TEXT NOT NULL,
    remote_id TEXT,             -- the platform's id for the live video, used to read its numbers
    UNIQUE (clip_id, platform)
);

-- Real numbers for a post, taken now and then. Views, likes, comments and shares can be missing for a platform that hides them.
CREATE TABLE IF NOT EXISTS post_stats (
    post_id INTEGER NOT NULL, taken_at TEXT NOT NULL, views INTEGER, likes INTEGER, comments INTEGER, shares INTEGER,
    PRIMARY KEY (post_id, taken_at)
);

-- Whether a platform has passed its audit for a channel. Until it has, that platform's posts stay manual.
CREATE TABLE IF NOT EXISTS platform_audits (
    profile_id TEXT NOT NULL, platform TEXT NOT NULL, passed INTEGER NOT NULL DEFAULT 0, note TEXT,
    set_at TEXT NOT NULL, PRIMARY KEY (profile_id, platform)
);

-- Whether a channel's account on a platform is allowed to post, and why not when it is paused.
CREATE TABLE IF NOT EXISTS account_state (
    profile_id TEXT NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',  -- active | paused
    reason TEXT, failures INTEGER NOT NULL DEFAULT 0, changed_at TEXT NOT NULL, PRIMARY KEY (profile_id, platform)
);

-- The title and caption options written for a post. One of them is chosen; the others stay for you to pick.
CREATE TABLE IF NOT EXISTS copy_options (
    id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER NOT NULL, title TEXT NOT NULL, caption TEXT NOT NULL,
    tags_json TEXT NOT NULL, stop_score INTEGER, misleading REAL, chosen INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL, created_at TEXT NOT NULL
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
            post_cols = {r[1] for r in setup.execute("PRAGMA table_info(posts)")}
            if "remote_id" not in post_cols:
                setup.execute("ALTER TABLE posts ADD COLUMN remote_id TEXT")
            clip_cols = {r[1] for r in setup.execute("PRAGMA table_info(clips_ready)")}
            for column in ("category", "campaign_id", "removed_from", "removed_at"):
                if column not in clip_cols:
                    setup.execute(f"ALTER TABLE clips_ready ADD COLUMN {column} TEXT")  # a database made before channels or campaigns
            if "campaign_id" not in {r[1] for r in setup.execute("PRAGMA table_info(runs)")}:
                setup.execute("ALTER TABLE runs ADD COLUMN campaign_id TEXT")
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
