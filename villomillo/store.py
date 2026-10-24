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


def add_snapshot(c, video_id: str, views, likes, comments, *, min_gap_minutes: int = 30) -> bool:
    """Record view counts for growth tracking, unless we just did."""
    last = c.execute(
        "SELECT taken_at FROM metric_snapshots WHERE video_id=? ORDER BY taken_at DESC LIMIT 1", (video_id,)
    ).fetchone()
    if last and (now() - parse_time(last["taken_at"])).total_seconds() < min_gap_minutes * 60:
        return False
    c.execute(
        "INSERT OR IGNORE INTO metric_snapshots (video_id, taken_at, views, likes, comments) VALUES (?,?,?,?,?)",
        (video_id, iso(), views, likes, comments),
    )
    return True


def set_state(c, profile_id: str, video_id: str, state: str, reason: str | None = None) -> None:
    c.execute(
        "UPDATE videos SET state=?, state_reason=? WHERE profile_id=? AND video_id=?",
        (state, reason, profile_id, video_id),
    )


def save_evaluation(c, profile_id, video_id, engine, model, qsv, answers, usage, cost) -> None:
    c.execute(
        "INSERT INTO evaluations (profile_id, video_id, engine, model, question_set_version, answers_json, "
        "input_tokens, output_tokens, cost_usd, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (profile_id, video_id, engine, model, qsv, json.dumps(answers), usage.get("input_tokens"),
         usage.get("output_tokens"), cost, iso()),
    )


def save_score(c, profile_id, video_id, result, qsv) -> None:
    c.execute(
        "INSERT INTO scores (profile_id, video_id, score, certainty, gate, components_json, flags_json, "
        "question_set_version, created_at) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(profile_id, video_id) DO UPDATE SET score=excluded.score, certainty=excluded.certainty, "
        "gate=excluded.gate, components_json=excluded.components_json, flags_json=excluded.flags_json, "
        "question_set_version=excluded.question_set_version, created_at=excluded.created_at",
        (profile_id, video_id, result.score, result.certainty, result.gate, json.dumps(result.components),
         json.dumps(result.flags), qsv, iso()),
    )


def add_ai_usage(c, engine: str, input_tokens: int, output_tokens: int, cost: float) -> None:
    c.execute(
        "INSERT INTO ai_usage (day, engine, requests, input_tokens, output_tokens, cost_usd) "
        "VALUES (?, ?, 1, ?, ?, ?) ON CONFLICT(day, engine) DO UPDATE SET requests = requests + 1, "
        "input_tokens = input_tokens + excluded.input_tokens, output_tokens = output_tokens + excluded.output_tokens, "
        "cost_usd = cost_usd + excluded.cost_usd",
        (pacific_day(), engine, input_tokens, output_tokens, cost),
    )


def ai_spend_today(db: Database, engine: str) -> float:
    with db.conn() as c:
        row = c.execute("SELECT cost_usd FROM ai_usage WHERE day=? AND engine=?", (pacific_day(), engine)).fetchone()
    return row["cost_usd"] if row else 0.0


def ranked(db: Database, profile_id: str, *, limit: int = 100) -> list[dict]:
    """Scored videos, best first."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT v.*, s.score, s.certainty, s.gate, s.components_json, s.flags_json "
            "FROM scores s JOIN videos v ON v.profile_id=s.profile_id AND v.video_id=s.video_id "
            "WHERE s.profile_id=? ORDER BY s.score DESC, v.published_at DESC LIMIT ?",
            (profile_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def ai_requests_today(db: Database, engine: str) -> int:
    with db.conn() as c:
        row = c.execute("SELECT requests FROM ai_usage WHERE day=? AND engine=?", (pacific_day(), engine)).fetchone()
    return row["requests"] if row else 0


def ai_spend_month(db: Database, engine: str) -> float:
    month = pacific_day()[:7]  # YYYY-MM
    with db.conn() as c:
        row = c.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM ai_usage WHERE engine=? AND substr(day,1,7)=?",
            (engine, month),
        ).fetchone()
    return float(row["total"])


def last_selfcheck(db: Database, profile_id: str) -> dict | None:
    with db.conn() as c:
        row = c.execute(
            "SELECT ran_at, verdict, sample, rank_correlation, top_hit_rate FROM selfchecks "
            "WHERE profile_id=? ORDER BY id DESC LIMIT 1",
            (profile_id,),
        ).fetchone()
    return dict(row) if row else None


def counts_by_state(db: Database, profile_id: str) -> dict[str, int]:
    with db.conn() as c:
        rows = c.execute(
            "SELECT state, COUNT(*) AS n FROM videos WHERE profile_id=? GROUP BY state", (profile_id,)
        ).fetchall()
    return {r["state"]: r["n"] for r in rows}
