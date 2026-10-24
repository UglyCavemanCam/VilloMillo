"""Learning from outcomes, within safe limits. Everything here reads the self-check's history;
nothing is learned from anyone's opinion.

- Calibration: what share of past uploads beat the channel's median, grouped by grade band.
- Length multiplier: a small adjustment for episode length, only once a length group has enough
  examples, and never more than 15% either way.
- Exploration: part of the shortlist is chosen for variety, so the system keeps testing options it
  has little evidence about instead of locking onto early winners.
"""

from __future__ import annotations

import json

from .db import Database

MIN_EXAMPLES = 20  # per group before any learned adjustment is used
MAX_ADJUSTMENT = 0.15
LENGTH_GROUPS = ((0, 60, "under 1 hour"), (60, 120, "1 to 2 hours"), (120, 10_000, "over 2 hours"))
BANDS = ((0, 50), (50, 60), (60, 70), (70, 80), (80, 101))


def latest_rows(db: Database, profile_id: str) -> list[dict]:
    with db.conn() as c:
        row = c.execute(
            "SELECT details_json FROM selfchecks WHERE profile_id=? ORDER BY id DESC LIMIT 1", (profile_id,)
        ).fetchone()
    if not row:
        return []
    return json.loads(row["details_json"]).get("rows", [])


def calibration(db: Database, profile_id: str) -> list[dict]:
    rows = latest_rows(db, profile_id)
    out = []
    for lo, hi in BANDS:
        in_band = [r for r in rows if lo <= r["grade"] < hi]
        beat = sum(r["beat_median"] for r in in_band)
        out.append({
            "band": f"{lo}-{hi - 1 if hi <= 100 else 100}",
            "n": len(in_band),
            "beat_median": (beat / len(in_band)) if in_band else None,
        })
    return out


def _group(minutes: float) -> tuple[int, int, str] | None:
    for lo, hi, name in LENGTH_GROUPS:
        if lo <= minutes < hi:
            return lo, hi, name
    return None


def length_multiplier(db: Database, profile_id: str, duration_s: int | None) -> float | None:
    """A bounded adjustment for this video's length group, or None while there is too little evidence."""
    rows = latest_rows(db, profile_id)
    if not rows or not duration_s:
        return None
    group = _group(duration_s / 60)
    if group is None:
        return None
    lo, hi, _ = group
    overall = sum(r["beat_median"] for r in rows) / len(rows)
    inside = [r for r in rows if lo <= (r.get("duration_min") or 0) < hi]
    if len(inside) < MIN_EXAMPLES:
        return None
    rate = sum(r["beat_median"] for r in inside) / len(inside)
    return round(max(1 - MAX_ADJUSTMENT, min(1 + MAX_ADJUSTMENT, 1 + (rate - overall))), 3)


def length_status(db: Database, profile_id: str) -> list[dict]:
    rows = latest_rows(db, profile_id)
    out = []
    for lo, hi, name in LENGTH_GROUPS:
        inside = [r for r in rows if lo <= (r.get("duration_min") or 0) < hi]
        active = len(inside) >= MIN_EXAMPLES
        out.append({
            "group": name,
            "examples": len(inside),
            "status": "active" if active else f"off until {MIN_EXAMPLES} examples",
        })
    return out


def shortlist_preview(candidates: list[dict], n: int = 10, explore_share: float = 0.2) -> list[dict]:
    """Pick n videos: mostly the highest scores, plus some chosen for variety. Deterministic.

    `candidates` must be sorted by score, best first, and have a `found_by` field.
    """
    if n <= 0 or not candidates:
        return []
    explore_n = min(int(round(n * explore_share)), n - 1) if n > 1 else 0
    chosen = [dict(c, why="top score") for c in candidates[: n - explore_n]]
    seen = {c["video_id"] for c in chosen}
    counts: dict[str, int] = {}
    for c in chosen:
        counts[c.get("found_by", "?")] = counts.get(c.get("found_by", "?"), 0) + 1
    rest = [c for c in candidates if c["video_id"] not in seen]
    for _ in range(explore_n):
        if not rest:
            break
        # the source least represented so far, then the best score within it
        rest.sort(key=lambda c: (counts.get(c.get("found_by", "?"), 0), -c["score"]))
        pick = rest.pop(0)
        counts[pick.get("found_by", "?")] = counts.get(pick.get("found_by", "?"), 0) + 1
        chosen.append(dict(pick, why="explore: under-tested source"))
    return sorted(chosen, key=lambda c: -c["score"])
