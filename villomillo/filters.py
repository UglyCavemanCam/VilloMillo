"""Tier 0: free checks in plain code that throw out videos we would never clip."""

from __future__ import annotations

from datetime import datetime

from .config import Profile
from .store import parse_time

SHORT_MAX_SECONDS = 180  # YouTube Shorts can be up to 3 minutes


def check(video: dict, profile: Profile, *, at: datetime) -> tuple[str, str | None]:
    """Returns ('eligible', None), ('held', reason) to look again later, or ('rejected', reason)."""
    r = profile.research
    if video["live_status"] in ("live", "upcoming"):
        return "held", f"{video['live_status']} broadcast: wait until it has finished"
    duration = video["duration_s"] or 0
    if duration == 0:
        return "held", "no duration yet (still processing)"
    if duration <= SHORT_MAX_SECONDS:
        return "rejected", "a Short"
    if duration < r.min_duration_min * 60:
        return "rejected", f"shorter than {r.min_duration_min:g} minutes"
    if duration > r.max_duration_min * 60:
        return "rejected", f"longer than {r.max_duration_min:g} minutes"
    age_hours = (at - parse_time(video["published_at"])).total_seconds() / 3600
    if age_hours > r.freshness_hours:
        return "rejected", f"older than {r.freshness_hours} hours"
    language = (video["language"] or "").lower()
    if language and not any(language.startswith(x.lower()) for x in r.languages):
        return "rejected", f"language {language}"
    title = video["title"].lower()
    for word in r.blocked_title_words:
        if word.lower() in title:
            return "rejected", f"title contains {word!r}"
    if video["channel_id"] in r.blocked_channel_ids:
        return "rejected", "blocked channel"
    if video["views"] is None:
        return "held", "view count hidden"
    return "eligible", None
