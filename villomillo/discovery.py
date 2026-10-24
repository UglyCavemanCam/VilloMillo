"""Finding videos: watched channels, keyword searches, trending, and growth snapshots.

Discovery only reads metadata. Nothing is downloaded.
"""

from __future__ import annotations

import statistics
from datetime import timedelta

from . import store
from .config import Profile
from .db import Database
from .filters import check
from .quota import QuotaExceeded
from .youtube import YouTube, YouTubeError

BACKFILL = 50  # uploads read the first time we see a channel
RECENT = 15  # uploads read on every later look
BASELINE_MIN_AGE_H, BASELINE_MAX_AGE_H = 14 * 24, 90 * 24  # old enough to have its full views
BASELINE_MIN_VIDEOS = 5


def resolve_sources(db: Database, yt: YouTube, profile: Profile) -> int:
    """Turn each watched channel (by @handle or id) into a stored source. Returns how many are known."""
    known = 0
    for w in profile.research.watch_channels:
        with db.conn() as c:
            row = c.execute(
                "SELECT 1 FROM watched_sources WHERE profile_id=? AND (handle=? OR channel_id=?)",
                (profile.id, w.handle, w.channel_id),
            ).fetchone()
        if row:
            known += 1
            continue
        ch = yt.channel(handle=w.handle, channel_id=w.channel_id)
        with db.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO watched_sources (profile_id, channel_id, handle, title, uploads_playlist, "
                "subscribers) VALUES (?,?,?,?,?,?)",
                (profile.id, ch["channel_id"], w.handle, ch["title"], ch["uploads_playlist"], ch["subscribers"]),
            )
        store.event(db, "info", "discovery", f"Following {ch['title']}", channel_id=ch["channel_id"])
        known += 1
    return known


def _save(db: Database, profile: Profile, videos: list[dict], found_by: str) -> int:
    new = 0
    with db.conn() as c:
        for v in videos:
            if not v["published_at"]:
                continue
            new += store.upsert_video(c, profile.id, v, found_by)
            store.add_snapshot(c, v["video_id"], v["views"], v["likes"], v["comments"])
    return new


def _known_ids(db: Database, profile_id: str, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    with db.conn() as c:
        marks = ",".join("?" * len(ids))
        rows = c.execute(
            f"SELECT video_id FROM videos WHERE profile_id=? AND video_id IN ({marks})", (profile_id, *ids)
        ).fetchall()
    return {r["video_id"] for r in rows}


def watch_channels(db: Database, yt: YouTube, profile: Profile) -> int:
    """Channel-based discovery: pick up new uploads from every watched channel."""
    new = 0
    with db.conn() as c:
        sources = [dict(r) for r in c.execute("SELECT * FROM watched_sources WHERE profile_id=?", (profile.id,))]
    for s in sources:
        ids = yt.recent_upload_ids(s["uploads_playlist"], limit=RECENT if s["backfilled"] else BACKFILL)
        videos = yt.videos(ids)  # one cheap call refreshes stats for the known ones too
        new += _save(db, profile, videos, "channel")
        with db.conn() as c:
            c.execute(
                "UPDATE watched_sources SET backfilled=1, last_checked_at=? WHERE profile_id=? AND channel_id=?",
                (store.iso(), profile.id, s["channel_id"]),
            )
        update_baseline(db, profile.id, s["channel_id"])
    return new


def update_baseline(db: Database, profile_id: str, channel_id: str) -> float | None:
    """A channel's usual total views for one upload, from its uploads that are 2 weeks to 3 months old."""
    now = store.now()
    totals = []
    with db.conn() as c:
        rows = c.execute(
            "SELECT views, published_at FROM videos WHERE profile_id=? AND channel_id=? AND views IS NOT NULL",
            (profile_id, channel_id),
        ).fetchall()
    for r in rows:
        age_h = (now - store.parse_time(r["published_at"])).total_seconds() / 3600
        if BASELINE_MIN_AGE_H <= age_h <= BASELINE_MAX_AGE_H:
            totals.append(r["views"])
    if len(totals) < BASELINE_MIN_VIDEOS:
        return None
    baseline = statistics.median(totals)
    with db.conn() as c:
        c.execute(
            "UPDATE watched_sources SET baseline_views=? WHERE profile_id=? AND channel_id=?",
            (baseline, profile_id, channel_id),
        )
    return baseline


def search_topics(db: Database, yt: YouTube, profile: Profile) -> int:
    """Topic-based discovery. Search allowance is scarce, so the list is capped per run."""
    r = profile.research
    new = 0
    for query in r.topics[: r.max_searches_per_run]:
        try:
            ids = yt.search(query, days=r.topic_search_days, language=r.languages[0] if r.languages else None)
        except QuotaExceeded:
            store.event(db, "warn", "discovery", "Search allowance used up for today", query=query)
            break
        known = _known_ids(db, profile.id, ids)
        fresh = [i for i in ids if i not in known]
        if fresh:
            new += _save(db, profile, yt.videos(fresh), "topic")
    return new


def trending(db: Database, yt: YouTube, profile: Profile) -> int:
    t = profile.research.trending
    if not t:
        return 0
    new = 0
    for region in t.regions:
        new += _save(db, profile, yt.most_popular(region=region, category=t.youtube_category), "trending")
    return new


def refresh_stats(db: Database, yt: YouTube, profile: Profile, *, days: int = 7) -> int:
    """Growth tracking: re-read view counts for recent videos and keep a snapshot of each."""
    cutoff = store.iso(store.now() - timedelta(days=days))
    with db.conn() as c:
        ids = [
            r["video_id"]
            for r in c.execute(
                "SELECT video_id FROM videos WHERE profile_id=? AND published_at>=? AND state!='rejected'",
                (profile.id, cutoff),
            )
        ]
    if ids:
        _save(db, profile, yt.videos(ids), "channel")  # known videos are updated, not duplicated
    return len(ids)


def apply_filters(db: Database, profile: Profile) -> dict[str, int]:
    """Tier 0: move newly found and held videos to eligible, held or rejected."""
    now = store.now()
    counts = {"eligible": 0, "held": 0, "rejected": 0}
    with db.conn() as c:
        rows = [
            dict(r)
            for r in c.execute(
                "SELECT * FROM videos WHERE profile_id=? AND state IN ('discovered', 'held')", (profile.id,)
            )
        ]
        for v in rows:
            state, reason = check(v, profile, at=now)
            store.set_state(c, profile.id, v["video_id"], state, reason)
            counts[state] += 1
    return counts


def discover(db: Database, yt: YouTube, profile: Profile, *, topics: bool = True) -> dict:
    """One full discovery pass for a profile. Safe to repeat: videos are stored once."""
    result = {"sources": resolve_sources(db, yt, profile)}
    try:
        result["channel"] = watch_channels(db, yt, profile)
        result["trending"] = trending(db, yt, profile)
        if topics:
            result["topic"] = search_topics(db, yt, profile)
    except QuotaExceeded as e:
        store.event(db, "warn", "discovery", str(e))
        result["stopped"] = str(e)
    except YouTubeError as e:
        store.event(db, "error", "discovery", str(e))
        result["error"] = str(e)
    result["filters"] = apply_filters(db, profile)
    return result
