"""Routing: which channel posts a clip.

Hard rules come first, in code:
- a clip with a category can only go to the channel for that category; with no such channel it is held until one is
  created (release_held posts it then);
- a channel needs posting settings, and channels whose accounts are paused or whose slots are full for days are only
  used when nothing better is left.
Then fit to the channel's topic decides, judged by one Jev question per channel. With one candidate there is nothing
to decide and no AI call is made. A clip that fits no channel is held, with the reason, never posted anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from . import accounts, posting, store
from .channels import for_category
from .config import Profile
from .db import Database

FIT_MIN = 3                 # of 5: a clip that scores lower for every channel fits none
MAX_WAIT_DAYS = 3           # a channel whose next free slot is further off than this counts as full
FIT_CRITERIA = ["Not at all", "Barely", "Somewhat", "Fits well", "A perfect fit"]


NO_CHANNEL = "no channel for the category"  # the start of the reason a held clip carries; release_held looks for it
HELD = "approved, but not posted: "


@dataclass
class Decision:
    profile_id: str | None
    reason: str
    scores: dict[str, int] = field(default_factory=dict)


def candidates(db: Database, profiles: dict[str, Profile], clip, now: datetime) -> tuple[list[dict], dict[str, str]]:
    """Channels allowed to post this clip, each with how long until its next free slot, and why others cannot."""
    allowed, refused = [], {}
    category = clip["category"]
    for p in profiles.values():
        if category and not (p.category and p.category.strip().lower() == category.strip().lower()):
            refused[p.id] = f"it is not the {category} channel"
            continue
        if p.posting is None or not p.posting.platforms:
            refused[p.id] = "it has no posting settings"
            continue
        live = [pl for pl in p.posting.platforms if not accounts.is_paused(db, p.id, pl)]
        slots = [posting.next_slot(db, p.posting, p.id, pl, now) for pl in (live or list(p.posting.platforms))]
        allowed.append({"profile": p, "active": bool(live), "wait": min(slots) - now})
    return allowed, refused


def route_clip(db: Database, jev, profiles: dict[str, Profile], clip_id: int, *, now: datetime | None = None) -> Decision:
    """Which channel should post this approved clip, or None with the reason it fits nowhere.

    `jev` may be a client or a function that builds one: it is only built if two or more channels remain."""
    now = now or store.now()
    with db.conn() as c:
        clip = c.execute("SELECT * FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
    if clip is None:
        raise LookupError("no such clip")
    if clip["category"] and for_category(profiles, clip["category"]) is None:
        return Decision(None, f"{NO_CHANNEL} {clip['category']}")
    allowed, refused = candidates(db, profiles, clip, now)
    if not allowed:
        return Decision(None, "no channel can post it: " + "; ".join(f"{k}: {v}" for k, v in refused.items()))
    usable = [a for a in allowed if a["active"] and a["wait"] <= timedelta(days=MAX_WAIT_DAYS)] or allowed
    if len(usable) == 1:
        return Decision(usable[0]["profile"].id, "the only channel that can post it")

    grade = json.loads(clip["grade_json"])
    state = {"clip_text": (grade.get("caption_text") or clip["hook"] or "")[:3000],
             "channels": {a["profile"].id: {"niche": a["profile"].niche.definition,
                                            "wants": a["profile"].niche.include, "does_not_want": a["profile"].niche.exclude}
                          for a in usable}}
    questions = {f"fit_{a['profile'].id}": {
        "type": "score", "criteria": FIT_CRITERIA,
        "instructions": f"How well does this clip fit the channel '{a['profile'].id}': its niche, what it wants, "
                        "and what it does not want?"} for a in usable}
    result = (jev if hasattr(jev, "ask") else jev()).ask(state, questions)
    with db.conn() as c:
        store.add_ai_usage(c, "jev", result.usage.get("input_tokens") or 0, 0, result.cost_usd)
    scores = {a["profile"].id: int(result.answers[f"fit_{a['profile'].id}"]["score"]) for a in usable}
    best = max(usable, key=lambda a: (scores[a["profile"].id], a["profile"].id == clip["profile_id"], -a["wait"]))
    top = scores[best["profile"].id]
    if top < FIT_MIN:
        return Decision(None, f"it fits no channel well (best score {top} of 5)", scores)
    return Decision(best["profile"].id, f"the best fit ({top} of 5)", scores)


def assign_and_plan(db: Database, jev_factory, profiles: dict[str, Profile], owner_id: str, clip_id: int, *,
                    now: datetime | None = None) -> dict | None:
    """Route an approved clip and plan its post on the chosen channel. A clip that fits nowhere is held, and the
    reason is written on the clip so it shows in the studio."""
    decision = route_clip(db, jev_factory, profiles, clip_id, now=now)
    store.event(db, "info", "route", f"clip {clip_id}: {decision.profile_id or 'held'}: {decision.reason}")
    if decision.profile_id is None:
        with db.conn() as c:
            c.execute("UPDATE clips_ready SET reason=? WHERE id=?", (f"{HELD}{decision.reason}", clip_id))
        return None
    planned = posting.plan(db, profiles[decision.profile_id], clip_id, now=now, owner=owner_id)
    with db.conn() as c:  # a clip that was held no longer is
        c.execute("UPDATE clips_ready SET reason=? WHERE id=? AND reason LIKE ?",
                  (f"approved; planned on {profiles[decision.profile_id].display_name}", clip_id, f"{HELD}%"))
    return planned


def held_categories(db: Database) -> list[dict]:
    """Categories with approved clips waiting for a channel: [{category, clips, low, high}], biggest first."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT category, COUNT(*) AS n, MIN(engine_score) AS lo, MAX(engine_score) AS hi FROM clips_ready "
            "WHERE state='approved' AND category IS NOT NULL AND reason LIKE ? GROUP BY lower(category) "
            "ORDER BY n DESC, category", (f"{HELD}{NO_CHANNEL}%",)).fetchall()
    return [{"category": r["category"], "clips": r["n"], "low": r["lo"], "high": r["hi"]} for r in rows]


def release_held(db: Database, jev_factory, profiles: dict[str, Profile], category: str, *,
                 now: datetime | None = None) -> list[dict]:
    """Plan the clips that were held for this category now that it has a channel. Returns the posts that were planned."""
    with db.conn() as c:
        held = c.execute(
            "SELECT id, profile_id FROM clips_ready WHERE state='approved' AND lower(category)=lower(?) AND reason LIKE ? "
            "AND NOT EXISTS (SELECT 1 FROM posts WHERE posts.clip_id=clips_ready.id) ORDER BY engine_score DESC, id",
            (category, f"{HELD}{NO_CHANNEL}%")).fetchall()
    planned = []
    for row in held:
        post = assign_and_plan(db, jev_factory, profiles, row["profile_id"], row["id"], now=now)
        if post:
            planned.append(post)
    return planned
