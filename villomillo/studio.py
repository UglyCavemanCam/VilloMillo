"""Data for the Clip studio page: where each video is in the clipping pipeline, and its clips.

Everything the page shows comes from two places: our own database, and the clipping engine's
job log. The engine's log is plain text, so the stage is read from wording it reliably prints
(for example "Downloading", "Transcribing", "Rendering clips", "Done in"). Percentages are
therefore approximate: the engine does not report a percentage for each stage.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from . import store
from .db import Database

STEPS = ["Download", "Transcribe", "Moments", "Rank", "Render", "Check", "Grade", "Review"]
STAGE_PERCENT = {0: 0, 1: 10, 2: 30, 3: 55, 4: 80}
STAGE_PATTERNS = [
    (1, re.compile(r"Downloading|\[1/4\]")),
    (2, re.compile(r"Transcrib|\[2/4\]")),
    (3, re.compile(r"Multimodal analysis|\[3/4\]|Chunk \d+/\d+|Ranking \d+ finalist")),
    (4, re.compile(r"Rendering clips|\[4/4\]")),
]
FINISHED = re.compile(r"Done in ")
CANDIDATES = re.compile(r"Chunk \d+/\d+: (\d+) candidate")
FINALISTS = re.compile(r"Ranking (\d+) finalist")


def read_log(log: str) -> dict:
    """What the engine's log says about progress. Pure text in, plain numbers out."""
    stage = 0
    for n, pattern in STAGE_PATTERNS:
        if pattern.search(log):
            stage = max(stage, n)
    finished = bool(FINISHED.search(log))
    candidates = sum(int(x) for x in CANDIDATES.findall(log))
    finalists = max((int(x) for x in FINALISTS.findall(log)), default=0)
    return {"stage": stage, "finished": finished, "candidates": candidates, "finalists": finalists}


def _elapsed_s(created_at: str) -> int:
    started = datetime.fromisoformat(created_at)
    if started.tzinfo is None:
        started = started.replace(tzinfo=store.now().tzinfo)
    return max(0, int((store.now() - started).total_seconds()))


def _step_states(progress: dict | None, job_status: str | None, clips: list[dict]) -> list[dict]:
    """Each of the eight steps as done, now, or waiting.

    The engine's log is read for the first four steps. The last three happen in this app, after
    the engine has finished: checks, then grading, then your review.
    """
    stage = progress["stage"] if progress else 0
    finished = bool(progress and progress["finished"]) or job_status == "done"
    active = job_status in ("submitted", "running", "queued")
    engine_done = {
        "Download": finished or stage >= 2,
        "Transcribe": finished or stage >= 3,
        "Moments": finished or stage >= 4,
        "Rank": finished or stage >= 4,
        "Render": finished,
    }
    states = {}
    current_set = False
    for s in STEPS[:5]:
        if engine_done.get(s):
            states[s] = "done"
        elif active and not current_set:
            states[s] = "now"
            current_set = True
        else:
            states[s] = "waiting"
    if finished:
        states["Check"] = "done" if clips else "waiting"
        states["Grade"] = "done" if clips and all(c["grade"] for c in clips) else "waiting"
        if any(c["state"] == "review" for c in clips):
            states["Review"] = "now"
        elif clips and all(c["state"] != "review" for c in clips):
            states["Review"] = "done"
        else:
            states["Review"] = "waiting"
    else:
        states.update({"Check": "waiting", "Grade": "waiting", "Review": "waiting"})
    return [{"name": s, "state": states[s]} for s in STEPS]


def _percent(progress: dict | None, job_status: str | None) -> int:
    if job_status == "done" or (progress and progress["finished"]):
        return 100
    return STAGE_PERCENT.get(progress["stage"], 0) if progress else 0


def _clip_view(r: dict) -> dict:
    checks = json.loads(r["validation_json"])
    grade = json.loads(r["grade_json"])
    return {
        "id": r["id"],
        "engine_clip_id": r["engine_clip_id"],
        "start_s": r["start_s"],
        "end_s": r["end_s"],
        "score": r["engine_score"],
        "hook": r["hook"],
        "state": r["state"],
        "reason": r["reason"],
        "checks_ok": checks.get("ok", False),
        "problems": checks.get("problems", []),
        "facts": checks.get("facts", {}),
        "grade": grade,
        "file_url": f"/clips/{r['id']}/file?profile={r['profile_id']}",
    }


def build(db: Database, engine_log, profile_id: str, *, duration_s: int | None = None) -> dict:
    """The whole page's data. `engine_log(job_id)` returns the engine's log text, or None if unreachable."""
    with db.conn() as c:
        job = c.execute(
            "SELECT * FROM clip_jobs WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)
        ).fetchone()
        clip_rows = [dict(r) for r in c.execute(
            "SELECT * FROM clips_ready WHERE profile_id=? ORDER BY id", (profile_id,))]
        counts = {
            "videos_sent": c.execute("SELECT COUNT(*) n FROM clip_jobs WHERE profile_id=?", (profile_id,)).fetchone()["n"],
            "clips_made": len(clip_rows),
            "passed_checks": sum(1 for r in clip_rows if r["state"] != "failed_validation"),
            "graded": sum(1 for r in clip_rows if json.loads(r["grade_json"] or "{}")),
            "approved": sum(1 for r in clip_rows if r["state"] == "approved"),
            "rejected": sum(1 for r in clip_rows if r["state"] == "rejected"),
        }

    engine_ok, log = True, None
    if job is not None:
        log = engine_log(job["engine_job_id"]) if job["engine_job_id"] else None
        engine_ok = log is not None
    progress = read_log(log) if log else None
    counts["moments_found"] = progress["candidates"] if progress else 0
    counts["moments_ranked"] = progress["finalists"] if progress else 0

    video = None
    clips_for_video = []
    if job is not None:
        with db.conn() as c:
            row = c.execute("SELECT title, duration_s FROM videos WHERE profile_id=? AND video_id=?",
                            (profile_id, job["video_id"])).fetchone()
        length = duration_s or (row["duration_s"] if row else None) or 0
        clips_for_video = [_clip_view(r) for r in clip_rows if r["video_id"] == job["video_id"]]
        timeline = []
        for c_ in clips_for_video:
            if length:
                timeline.append({
                    "id": c_["id"],
                    "left_pct": round(100 * c_["start_s"] / length, 2),
                    "width_pct": max(0.6, round(100 * (c_["end_s"] - c_["start_s"]) / length, 2)),
                    "state": c_["state"],
                })
        video = {
            "video_id": job["video_id"],
            "title": row["title"] if row else job["video_id"],
            "duration_s": length,
            "clips": timeline,
        }
    status = job["status"] if job is not None else None
    return {
        "engine_ok": engine_ok,
        "engine_message": None if engine_ok else "The clipping engine is not running, so live progress is paused.",
        "counts": counts,
        "video": video,
        "job": {"id": job["id"], "status": status, "elapsed_s": _elapsed_s(job["created_at"]) if job else 0}
        if job is not None else None,
        "percent": _percent(progress, status),
        "steps": _step_states(progress, status, clips_for_video),
        "clips": clips_for_video,
        "error": job["error"] if job is not None and job["error"] else None,
    }
