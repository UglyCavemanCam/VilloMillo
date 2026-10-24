"""The automatic check on the grading. No human ratings are needed.

Take the channel's own older uploads. Grade each one using only what was known at upload time
(title, description, length, niche rules), not its views. Then compare the grades with what
happened: did the videos graded highest tend to do better than this channel's usual?

What this can and cannot tell you:
- It can tell you the grading is pointing the wrong way, which is the failure that matters most.
- It cannot tell you whether a clip will perform. That is only known after a clip is posted, and
  those results are what the later phases learn from.
- Views reward popular guests as well as good moments, so a positive result is evidence, not proof.
"""

from __future__ import annotations

import json
import statistics

from . import grading, scoring, store, transcripts
from .config import Profile
from .db import Database
from .jev import Jev
from .transcripts import TranscriptUnavailable
from .youtube import YouTube

MIN_AGE_H, MAX_AGE_H = 14 * 24, 180 * 24  # old enough to have collected most of its views, not so old that the channel has changed
SAMPLE = 60
MIN_SAMPLE = 20
ENGINE = "jev-selfcheck"


def spearman(x: list[float], y: list[float]) -> float:
    """Rank correlation from -1 to 1. 0 when either side has no variation."""

    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1  # average rank for ties
            i = j + 1
        return out

    rx, ry = ranks(x), ranks(y)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return num / den if den else 0.0


def grade_without_views(answers: dict, profile: Profile) -> float:
    """Jev's answers only: niche, moments, appeal and hook, with the profile's weights and the risk penalty."""
    parts = {
        "niche": scoring._noul(answers, "fits_niche"),
        "moments": scoring._score(answers, "has_moments")[0],
        "appeal": scoring._score(answers, "topic_appeal")[0],
        "hook": scoring._score(answers, "hook_potential")[0],
    }
    weights = {k: profile.grading.weights.get(k, 0.0) for k in parts}
    total = sum(weights.values()) or 1.0
    base = 100 * sum(weights[k] * parts[k] for k in parts) / total
    worst = max((scoring._noul(answers, k) for k in answers if k.startswith("risk_")), default=0.0)
    if worst >= scoring.RISK_LIMIT:
        base -= 40 * worst
    return max(0.0, min(100.0, base))


def verdict_for(n: int, rho: float | None, hit: float | None) -> str:
    if n < MIN_SAMPLE or rho is None:
        return "too few older uploads to judge yet"
    if rho <= 0:
        return "backwards: do not trust the grades yet"
    if rho >= 0.2 and hit is not None and hit >= 0.6:
        return "pointing the right way"
    return "not clear yet"


def run(db: Database, yt: YouTube, jev: Jev, profile: Profile, *, sample: int = SAMPLE) -> dict:
    with db.conn() as c:
        src = c.execute(
            "SELECT uploads_playlist FROM watched_sources WHERE profile_id=? ORDER BY rowid LIMIT 1", (profile.id,)
        ).fetchone()
    if not src:
        raise ValueError("this profile watches no channel, so there is nothing to check against")

    now = store.now()
    videos = yt.videos(yt.recent_upload_ids(src["uploads_playlist"], limit=300))
    eligible = [
        v
        for v in videos
        if v["views"] is not None
        and v["live_status"] == "none"
        and MIN_AGE_H <= (now - store.parse_time(v["published_at"])).total_seconds() / 3600 <= MAX_AGE_H
        and (v["duration_s"] or 0) >= profile.research.min_duration_min * 60
    ][:sample]

    if len(eligible) < MIN_SAMPLE:
        result = {"verdict": verdict_for(len(eligible), None, None), "sample": len(eligible), "graded": 0}
        _save(db, profile.id, result)
        return result

    median = statistics.median(v["views"] for v in eligible)
    questions = grading.build_questions()
    rows = []
    tq = grading.build_transcript_questions()
    for v in eligible:
        answers = jev.ask(grading.build_state(v, profile), questions)
        with db.conn() as c:
            store.save_evaluation(c, profile.id, v["video_id"], ENGINE, answers.model, grading.QUESTION_SET_VERSION,
                                  answers.answers, answers.usage, answers.cost_usd)
            store.add_ai_usage(c, "jev", answers.usage.get("input_tokens") or 0,
                               answers.usage.get("output_tokens") or 0, answers.cost_usd)
        # the same transcript pass the live pipeline uses, so the check measures the real grading
        merged, read = answers.answers, False
        try:
            pieces = transcripts.chunks(transcripts.get(db, v["video_id"]))
            chunk_answers = [jev.ask(grading.transcript_state(v, profile, piece), tq).answers for piece in pieces]
            merged, _ = grading.combine_transcript(answers.answers, chunk_answers)
            read = True
        except TranscriptUnavailable:
            pass
        rows.append({
            "video_id": v["video_id"],
            "title": v["title"],
            "grade": round(grade_without_views(merged, profile), 1),
            "views": v["views"],
            "duration_min": round((v["duration_s"] or 0) / 60),
            "beat_median": v["views"] > median,
            "transcript": read,
        })

    grades = [r["grade"] for r in rows]
    views = [float(r["views"]) for r in rows]
    rho = spearman(grades, views)
    ranked = sorted(rows, key=lambda r: r["grade"], reverse=True)
    top = ranked[: max(1, len(ranked) // 3)]
    hit = sum(r["beat_median"] for r in top) / len(top)
    result = {
        "verdict": verdict_for(len(rows), rho, hit),
        "sample": len(rows),
        "graded": len(rows),
        "rank_correlation": round(rho, 3),
        "top_hit_rate": round(hit, 3),
        "median_views": int(median),
        "top_picks": ranked[:5],
        "lowest": ranked[-5:],
        "rows": rows,
    }
    _save(db, profile.id, result)
    store.event(db, "info", "selfcheck", f"Self-check: {result['verdict']}", sample=len(rows), rho=result["rank_correlation"])
    return result


def _save(db: Database, profile_id: str, result: dict) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO selfchecks (profile_id, ran_at, sample, rank_correlation, top_hit_rate, verdict, details_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (profile_id, store.iso(), result["sample"], result.get("rank_correlation"), result.get("top_hit_rate"),
             result["verdict"], json.dumps(result, default=str)),
        )
