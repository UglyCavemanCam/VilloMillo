"""The research cycle: discover -> filter -> grade -> score, plus re-scoring from saved answers."""

from __future__ import annotations

import json

from . import discovery, grading, scoring, store
from .config import AppConfig, Profile
from .db import Database
from .jev import Jev, JevError
from .youtube import YouTube

MAX_CONSECUTIVE_JEV_ERRORS = 3


def _context(db: Database, profile_id: str, video: dict) -> tuple[list[dict], float | None]:
    with db.conn() as c:
        snaps = [
            dict(r)
            for r in c.execute(
                "SELECT taken_at, views, likes, comments FROM metric_snapshots WHERE video_id=? ORDER BY taken_at",
                (video["video_id"],),
            )
        ]
        row = c.execute(
            "SELECT baseline_views FROM watched_sources WHERE profile_id=? AND channel_id=?",
            (profile_id, video["channel_id"]),
        ).fetchone()
    return snaps, (row["baseline_views"] if row else None)


def score_from_answers(db: Database, profile: Profile, video: dict, answers: dict) -> scoring.Result:
    snaps, baseline = _context(db, profile.id, video)
    feats = scoring.features(video, snaps, baseline, store.now())
    result = scoring.assemble(feats, answers, profile.grading)
    result.components["_features"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in feats.items()}
    with db.conn() as c:
        store.save_score(c, profile.id, video["video_id"], result, grading.QUESTION_SET_VERSION)
        store.set_state(c, profile.id, video["video_id"], "scored", None)
    return result


def grade_new(db: Database, jev: Jev, app: AppConfig, profile: Profile, *, limit: int | None = None) -> dict:
    """Ask Jev about eligible videos that have no score yet, newest first, within the daily budget."""
    limit = limit or app.schedule.grade_batch
    with db.conn() as c:
        todo = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM videos WHERE profile_id=? AND state='eligible' ORDER BY published_at DESC LIMIT ?",
                (profile.id, limit),
            )
        ]
    out = {"graded": 0, "errors": 0, "stopped": None}
    streak = 0
    for video in todo:
        if store.ai_spend_today(db, "jev") >= app.budgets.jev_usd_per_day:
            out["stopped"] = "Jev budget for today is used up"
            store.event(db, "warn", "grading", out["stopped"])
            break
        try:
            answers = grading.grade_one(db, jev, profile, video)
            score_from_answers(db, profile, video, answers)
            out["graded"] += 1
            streak = 0
        except (JevError, KeyError, ValueError) as e:
            out["errors"] += 1
            streak += 1
            store.event(db, "error", "grading", f"{video['video_id']}: {e}")
            if streak >= MAX_CONSECUTIVE_JEV_ERRORS:
                out["stopped"] = "Jev keeps failing; will try again next run"
                break
    return out


def rescore(db: Database, profile: Profile) -> int:
    """Recompute every score from its saved Jev answers. Free: no AI calls."""
    with db.conn() as c:
        rows = [
            (dict(r), json.loads(r["answers_json"]))
            for r in c.execute(
                "SELECT v.*, e.answers_json FROM videos v JOIN evaluations e "
                "ON e.profile_id=v.profile_id AND e.video_id=v.video_id AND e.engine='jev' "
                "AND e.question_set_version=? AND e.id=(SELECT MAX(id) FROM evaluations "
                "WHERE profile_id=v.profile_id AND video_id=v.video_id AND engine='jev' "
                "AND question_set_version=?) WHERE v.profile_id=? AND v.state='scored'",
                (grading.QUESTION_SET_VERSION, grading.QUESTION_SET_VERSION, profile.id),
            )
        ]
    for video, answers in rows:
        score_from_answers(db, profile, video, answers)
    return len(rows)


def run_cycle(db: Database, yt: YouTube, jev: Jev, app: AppConfig, profile: Profile, *, topics: bool = True,
              limit: int | None = None) -> dict:
    """Everything once: find videos, filter, snapshot growth, grade the new ones, refresh scores."""
    store.sync_profile(db, profile.id, profile.display_name, profile.digest())
    summary = {"discovery": discovery.discover(db, yt, profile, topics=topics)}
    summary["grading"] = grade_new(db, jev, app, profile, limit=limit)
    summary["rescored"] = rescore(db, profile)
    summary["states"] = store.counts_by_state(db, profile.id)
    return summary
