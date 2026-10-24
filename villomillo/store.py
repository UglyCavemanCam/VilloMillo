"""Reads and writes for videos, scores, AI spend and the event log."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from publish.quota import pacific_day

from .db import Database

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime | None = None) -> str:
    return (moment or now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def event(db: Database, level: str, component: str, message: str, **data) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO events (ts, level, component, message, data_json) VALUES (?, ?, ?, ?, ?)",
            (iso(), level, component, message, json.dumps(data) if data else None),
        )


def sync_profile(db: Database, profile_id: str, display_name: str, digest: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO profiles (id, display_name, digest, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name, digest=excluded.digest, "
            "updated_at=excluded.updated_at",
            (profile_id, display_name, digest, iso()),
        )


def upsert_video(c, profile_id: str, v: dict, found_by: str) -> bool:
    """Save a video once per profile. Returns True when it is new."""
    if not VIDEO_ID.match(v["video_id"]):
        raise ValueError(f"not a YouTube video id: {v['video_id']!r}")
    stamp = iso()
    row = c.execute(
        "SELECT 1 FROM videos WHERE profile_id=? AND video_id=?", (profile_id, v["video_id"])
    ).fetchone()
    if row:
        c.execute(
            "UPDATE videos SET title=?, description=?, tags_json=?, duration_s=?, language=?, live_status=?, "
            "views=?, likes=?, comments=?, last_seen_at=? WHERE profile_id=? AND video_id=?",
            (v["title"], v["description"], json.dumps(v["tags"]), v["duration_s"], v["language"],
             v["live_status"], v["views"], v["likes"], v["comments"], stamp, profile_id, v["video_id"]),
        )
        return False
    c.execute(
        "INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
        "tags_json, duration_s, published_at, language, live_status, views, likes, comments, "
        "first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (profile_id, v["video_id"], found_by, v["channel_id"], v["channel_title"], v["title"], v["description"],
         json.dumps(v["tags"]), v["duration_s"], v["published_at"], v["language"], v["live_status"],
         v["views"], v["likes"], v["comments"], stamp, stamp),
    )
    return True


def set_state(c, profile_id: str, video_id: str, state: str, reason: str | None = None) -> None:
    c.execute(
        "UPDATE videos SET state=?, state_reason=? WHERE profile_id=? AND video_id=?",
        (state, reason, profile_id, video_id),
    )


def add_ai_usage(c, engine: str, input_tokens: int, output_tokens: int, cost: float) -> None:
    c.execute(
        "INSERT INTO ai_usage (day, engine, requests, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(day, engine) DO UPDATE SET requests = requests + 1, "
        "input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens, "
        "cost_usd = cost_usd + excluded.cost_usd",
        (pacific_day(), engine, input_tokens, output_tokens, cost),
    )
    c.execute("INSERT INTO spend_log (at, engine, cost_usd) VALUES (?, ?, ?)", (iso(), engine, cost))


def counts_by_state(db: Database, profile_id: str) -> dict[str, int]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT state, COUNT(*) AS n FROM videos WHERE profile_id=? GROUP BY state", (profile_id,)
        ).fetchall()
    return {r["state"]: r["n"] for r in rows}
