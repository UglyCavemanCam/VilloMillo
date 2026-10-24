"""The research cycle: discover -> filter -> grade -> transcript pass -> second opinion -> score.

Every AI answer is saved raw. Scores are always rebuilt from the saved answers, so changing a
weight, or a second opinion arriving later, never needs a new AI call for anything already done.
"""

from __future__ import annotations

import json

from . import claude, discovery, grading, learning, scoring, spend, store, transcripts
from .config import AppConfig, Profile
from .db import Database
from .jev import Jev, JevError
from .transcripts import TranscriptUnavailable
from .youtube import YouTube

MAX_CONSECUTIVE_JEV_ERRORS = 3
TRANSCRIPT_ENGINE = "jev-transcript"
CLAUDE_ENGINE = "claude"


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


def _latest(db: Database, profile_id: str, video_id: str, engine: str, qsv: int | None = None) -> dict | None:
    sql = (
        "SELECT answers_json FROM evaluations WHERE profile_id=? AND video_id=? AND engine=?"
        + (" AND question_set_version=?" if qsv is not None else "")
        + " ORDER BY id DESC LIMIT 1"
    )
    args = (profile_id, video_id, engine) + ((qsv,) if qsv is not None else ())
    with db.conn() as c:
        row = c.execute(sql, args).fetchone()
    return json.loads(row["answers_json"]) if row else None


def _second_opinion(db: Database, profile_id: str, video_id: str) -> dict | None:
    return _latest(db, profile_id, video_id, CLAUDE_ENGINE)


def _apply_second_opinion(result: scoring.Result, opinion: dict | None, grading_cfg, flags: list[str]) -> None:
    """Claude can settle a borderline video, but only when it is confident, and never overrides a safety flag."""
    if not opinion or result.gate != "escalate" or result.flags:
        return
    if opinion["confidence"] < claude.CONFIDENT:
        return
    if opinion["clip_worthy"] and result.score >= grading_cfg.min_score - grading_cfg.escalate_band:
        result.gate = "pass"
        flags.append("second opinion: clip-worthy")
    elif not opinion["clip_worthy"]:
        result.gate = "fail"
        flags.append("second opinion: not clip-worthy")


def score_from_answers(db: Database, profile: Profile, video: dict, answers: dict) -> scoring.Result:
    """Build the score from the title answers, blended with the transcript answers when we have them."""
    vid = video["video_id"]
    transcript = _latest(db, profile.id, vid, TRANSCRIPT_ENGINE, grading.TRANSCRIPT_VERSION)
    summary = None
    if transcript and transcript.get("chunks"):
        answers, summary = grading.combine_transcript(answers, transcript["chunks"])

    snaps, baseline = _context(db, profile.id, video)
    feats = scoring.features(video, snaps, baseline, store.now())
    result = scoring.assemble(feats, answers, profile.grading)
    result.components["_features"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in feats.items()}
    if summary:
        result.components["_transcript"] = summary

    mult = learning.length_multiplier(db, profile.id, video["duration_s"])
    if mult is not None:
        result.score = round(scoring.clamp(result.score * mult, 0, 100), 1)
        result.components["_learned_multiplier"] = mult

    notes: list[str] = []
    _apply_second_opinion(result, _second_opinion(db, profile.id, vid), profile.grading, notes)
    result.flags += notes

    with db.conn() as c:
        store.save_score(c, profile.id, vid, result, grading.QUESTION_SET_VERSION)
        store.set_state(c, profile.id, vid, "scored", None)
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
        if (pause := spend.current(db, app.spend_breaker)):
            out["stopped"] = pause.title
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


def shortlist(db: Database, profile: Profile, app: AppConfig) -> list[dict]:
    """Videos close enough to the line to deserve the transcript pass and, if still unsure, a second opinion."""
    g = profile.grading
    with db.conn() as c:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT v.*, s.score, s.gate FROM videos v JOIN scores s "
                "ON s.profile_id=v.profile_id AND s.video_id=v.video_id "
                "WHERE v.profile_id=? AND s.score >= ? ORDER BY s.score DESC",
                (profile.id, g.min_score - g.escalate_band),
            )
        ]
    return rows


def transcript_pass(db: Database, jev: Jev, app: AppConfig, profile: Profile, video: dict) -> str:
    """Read the video's transcript with Jev. Returns a short status for the log."""
    vid = video["video_id"]
    if _latest(db, profile.id, vid, TRANSCRIPT_ENGINE, grading.TRANSCRIPT_VERSION) is not None:
        return "already read"
    try:
        text = transcripts.get(db, vid)
    except TranscriptUnavailable as e:
        with db.conn() as c:
            store.set_state(c, profile.id, vid, "scored", f"no transcript: {e}")
        return f"no transcript ({e})"
    questions = grading.build_transcript_questions()
    chunks, spent = [], 0.0
    for piece in transcripts.chunks(text):
        if spent >= app.budgets.transcript_jev_usd_per_video:
            break
        r = jev.ask(grading.transcript_state(video, profile, piece), questions)
        spent += r.cost_usd
        chunks.append(r.answers)
    if not chunks:
        return "no pieces read"
    with db.conn() as c:
        store.save_evaluation(c, profile.id, vid, TRANSCRIPT_ENGINE, jev.model, grading.TRANSCRIPT_VERSION,
                              {"chunks": chunks}, {"input_tokens": 0, "output_tokens": 0}, spent)
        store.add_ai_usage(c, "jev", 0, 0, spent)
    return f"read {len(chunks)} pieces for ${spent:.4f}"


def deepen(db: Database, jev: Jev, app: AppConfig, profile: Profile) -> dict:
    """Transcript pass on the shortlist, then a rescore so the transcript counts."""
    out = {"read": 0, "skipped": 0, "stopped": None}
    for video in shortlist(db, profile, app):
        if (pause := spend.current(db, app.spend_breaker)):
            out["stopped"] = pause.title
            break
        status = transcript_pass(db, jev, app, profile, video)
        if status.startswith("read"):
            out["read"] += 1
            answers = _latest(db, profile.id, video["video_id"], "jev", grading.QUESTION_SET_VERSION)
            if answers:
                score_from_answers(db, profile, video, answers)
        else:
            out["skipped"] += 1
        store.event(db, "info", "transcripts", f"{video['title'][:60]}: {status}")
    return out


def second_opinions(db: Database, app: AppConfig, profile: Profile, client=None) -> dict:
    """Ask Claude about borderline videos, within the daily and monthly caps."""
    out = {"asked": 0, "stopped": None}

    with db.conn() as c:
        todo = [
            dict(r)
            for r in c.execute(
                "SELECT v.* FROM videos v JOIN scores s ON s.profile_id=v.profile_id AND s.video_id=v.video_id "
                "WHERE v.profile_id=? AND s.gate='escalate' AND NOT EXISTS (SELECT 1 FROM evaluations e "
                "WHERE e.profile_id=v.profile_id AND e.video_id=v.video_id AND e.engine=?) "
                "ORDER BY s.score DESC",
                (profile.id, CLAUDE_ENGINE),
            )
        ]
    if not todo:
        return out
    if store.ai_requests_today(db, CLAUDE_ENGINE) >= app.budgets.claude_escalations_per_day:
        out["stopped"] = "daily second-opinion limit reached"
        return out
    if (pause := spend.current(db, app.spend_breaker)):
        out["stopped"] = pause.title
        return out
    client = client or claude._client()
    for video in todo:
        if store.ai_requests_today(db, CLAUDE_ENGINE) >= app.budgets.claude_escalations_per_day:
            out["stopped"] = "daily second-opinion limit reached"
            break
        with db.conn() as c:
            row = c.execute("SELECT text FROM transcripts WHERE video_id=?", (video["video_id"],)).fetchone()
        excerpt = row["text"][:6000] if row else ""
        try:
            verdict, cost = claude.judge(
                client, niche=profile.niche.definition, title=video["title"],
                description=video["description"] or "", excerpt=excerpt,
            )
        except Exception as e:  # any failed call is logged and skipped; the others still run
            store.event(db, "error", "second-opinion", f"{video['video_id']}: {type(e).__name__}: {str(e)[:160]}")
            continue
        with db.conn() as c:
            store.save_evaluation(c, profile.id, video["video_id"], CLAUDE_ENGINE, claude.MODEL, 1, verdict,
                                  {"input_tokens": 0, "output_tokens": 0}, cost)
            store.add_ai_usage(c, CLAUDE_ENGINE, 0, 0, cost)
        out["asked"] += 1
        score_from_answers_by_id(db, app, profile, video["video_id"])
    return out


def score_from_answers_by_id(db: Database, app: AppConfig, profile: Profile, video_id: str) -> None:
    with db.conn() as c:
        video = c.execute("SELECT * FROM videos WHERE profile_id=? AND video_id=?", (profile.id, video_id)).fetchone()
    answers = _latest(db, profile.id, video_id, "jev", grading.QUESTION_SET_VERSION)
    if video and answers:
        score_from_answers(db, profile, dict(video), answers)


def rescore(db: Database, profile: Profile) -> int:
    """Recompute every score from its saved answers. Free: no AI calls."""
    with db.conn() as c:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT v.*, e.answers_json FROM videos v JOIN evaluations e "
                "ON e.profile_id=v.profile_id AND e.video_id=v.video_id AND e.engine='jev' "
                "AND e.question_set_version=? AND e.id=(SELECT MAX(id) FROM evaluations "
                "WHERE profile_id=v.profile_id AND video_id=v.video_id AND engine='jev' "
                "AND question_set_version=?) WHERE v.profile_id=? AND v.state='scored'",
                (grading.QUESTION_SET_VERSION, grading.QUESTION_SET_VERSION, profile.id),
            )
        ]
    for video in rows:
        answers = json.loads(video.pop("answers_json"))
        score_from_answers(db, profile, video, answers)
    return len(rows)


def run_cycle(db: Database, yt: YouTube, jev: Jev, app: AppConfig, profile: Profile, *, topics: bool = True,
              limit: int | None = None, second_opinion: bool = True) -> dict:
    """Everything once: find videos, filter, grade, read transcripts for the shortlist, ask Claude, rescore."""
    store.sync_profile(db, profile.id, profile.display_name, profile.digest())
    summary = {"discovery": discovery.discover(db, yt, profile, topics=topics)}
    summary["grading"] = grade_new(db, jev, app, profile, limit=limit)
    summary["transcripts"] = deepen(db, jev, app, profile)
    summary["second_opinions"] = second_opinions(db, app, profile) if second_opinion else {"skipped": True}
    summary["rescored"] = rescore(db, profile)
    summary["states"] = store.counts_by_state(db, profile.id)
    return summary
