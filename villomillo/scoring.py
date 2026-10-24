"""Turning numbers and Jev's answers into one confidence score. All plain maths, no AI calls.

Scores are always recomputed from saved answers, so changing a weight never costs anything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from .config import Grading
from .store import parse_time

RISK_LIMIT = 0.5  # a risk question answered "probably yes" holds the video back
MOMENTUM_MAX_AGE_H = 7 * 24  # growth rate is only meaningful for fairly new videos


@dataclass
class Result:
    score: float
    certainty: float
    gate: str  # pass | escalate | fail
    components: dict
    flags: list[str] = field(default_factory=list)


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ratio_to_unit(ratio: float | None) -> float:
    """1x the usual -> 0.5, 4x -> 1.0, a quarter -> 0.0. Unknown -> 0.5 (neither helps nor hurts)."""
    if ratio is None or ratio <= 0:
        return 0.5
    return clamp(0.5 + math.log2(ratio) / 4)


# How a long podcast upload collects views: about a third in the first day, and its full total
# after roughly two weeks. Fitted by eye to The Joe Rogan Experience (about 31% at 10 hours, 54%
# at 3.5 days, 87% at 9 days). It is only a default: later phases learn each channel's own curve
# from the snapshots we keep.
GROWTH_A, GROWTH_B = 0.148, 0.327


def growth_fraction(age_h: float) -> float:
    """Share of an upload's eventual views it should have by now."""
    return min(1.0, GROWTH_A * max(age_h, 3.0) ** GROWTH_B)


def growth_rate(age_h: float) -> float:
    """Share of the eventual views it should be gaining per hour right now."""
    return 0.0 if age_h >= 342 else GROWTH_A * GROWTH_B * max(age_h, 3.0) ** (GROWTH_B - 1)


def features(video: dict, snapshots: list[dict], baseline_views: float | None, at: datetime) -> dict:
    """Numbers about how a video is doing. `snapshots` are oldest first.

    `outlier_ratio` is views so far divided by what this channel's usual upload would have by
    now (1.0 = typical, 2.0 = twice as many). `momentum_ratio` is the same idea for how fast
    views are arriving this hour.
    """
    age_h = max((at - parse_time(video["published_at"])).total_seconds() / 3600, 1.0)
    views = video["views"] or 0
    rate = views / max(age_h, 6.0)  # plain views per hour, for display
    outlier = None
    if baseline_views:
        outlier = views / (baseline_views * growth_fraction(age_h))
    momentum = None
    if baseline_views and age_h <= MOMENTUM_MAX_AGE_H and len(snapshots) >= 2:
        last = snapshots[-1]
        earlier = [
            s for s in snapshots[:-1]
            if (parse_time(last["taken_at"]) - parse_time(s["taken_at"])).total_seconds() >= 3600
        ]
        expected = baseline_views * growth_rate(age_h)
        if earlier and expected > 0:
            prev = earlier[-1]
            hours = (parse_time(last["taken_at"]) - parse_time(prev["taken_at"])).total_seconds() / 3600
            momentum = (((last["views"] or 0) - (prev["views"] or 0)) / hours) / expected
    return {
        "age_hours": age_h,
        "views_per_hour": rate,
        "like_rate": (video["likes"] or 0) / views if views else 0.0,
        "comment_rate": (video["comments"] or 0) / views if views else 0.0,
        "outlier_ratio": outlier,
        "momentum_ratio": momentum,
    }


def _noul(answers: dict, name: str) -> float:
    return float(answers[name]["noul"])


def _score(answers: dict, name: str) -> tuple[float, float]:
    a = answers[name]
    top = max(int(k) for k in a["legend"])
    return float(a["score"]) / top if top else 0.0, float(a.get("confidence", 0.0))


def assemble(feats: dict, answers: dict, grading: Grading) -> Result:
    moments, moments_conf = _score(answers, "has_moments")
    appeal, appeal_conf = _score(answers, "topic_appeal")
    hook, hook_conf = _score(answers, "hook_potential")
    fits = _noul(answers, "fits_niche")
    components = {
        "outlier": ratio_to_unit(feats["outlier_ratio"]),
        "momentum": ratio_to_unit(feats["momentum_ratio"]),
        "engagement": clamp((feats["like_rate"] + 5 * feats["comment_rate"]) / 0.08),
        "niche": fits,
        "moments": moments,
        "appeal": appeal,
        "hook": hook,
    }
    weights = grading.weights
    total = sum(weights.values())
    score = 100 * sum(weights.get(k, 0.0) * v for k, v in components.items()) / total

    flags: list[str] = []
    risks = {k: _noul(answers, k) for k in answers if k.startswith("risk_")}
    worst = max(risks.values(), default=0.0)
    if worst >= RISK_LIMIT:
        flags += [f"{k} ({p:.0%})" for k, p in risks.items() if p >= RISK_LIMIT]
        score -= 40 * worst
    visuals = _noul(answers, "needs_visuals")
    if visuals >= RISK_LIMIT:
        flags.append(f"needs_visuals ({visuals:.0%})")
        score -= 10 * visuals
    score = clamp(score, 0, 100)

    certainty = (abs(2 * fits - 1) + moments_conf + appeal_conf + hook_conf) / 4
    components["_risk"] = worst
    return Result(round(score, 1), round(certainty, 3), gate(score, certainty, flags, grading), components, flags)


def gate(score: float, certainty: float, flags: list[str], g: Grading) -> str:
    """pass = may be clipped; escalate = ask a stronger grader first; fail = no."""
    if any(f.startswith("risk_") for f in flags):
        return "fail"
    if score >= g.min_score and certainty >= g.min_certainty:
        return "pass"
    if score >= g.min_score - g.escalate_band:  # near the line, or sure enough about the score but not the grade
        return "escalate"
    return "fail"
