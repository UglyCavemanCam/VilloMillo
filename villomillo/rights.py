"""The rights gate. Nothing is downloaded or clipped unless its rights are known and allowed.

Statuses:
- owned: your own channel or footage (you set this)
- licensed: written permission you have (you set this, with a note)
- campaign: a clipping campaign you joined lists this exact video as source material (you record the
  campaign link and its wording; it covers only the listed videos, never a whole channel)
- cc_by: YouTube says the video is Creative Commons (checked on every clip, not trusted from earlier)
- none: anything else. Researched and graded, never clipped.

The default is strict. The only other setting the plan allows is "off", and that is a decision
for you to make explicitly; it is not implemented here.
"""

from __future__ import annotations

from .db import Database
from .store import iso

ALLOWED = {"owned", "licensed", "campaign", "cc_by"}
MANUAL = {"owned", "licensed"}


def set_channel(db: Database, profile_id: str, channel_id: str, status: str, note: str = "") -> None:
    if status not in MANUAL | {"none"}:
        raise ValueError("a channel's rights are 'owned', 'licensed' or 'none'")
    if status == "licensed" and len(note.strip()) < 10:
        raise ValueError("say where the permission comes from (a note of at least a few words)")
    with db.conn() as c:
        c.execute(
            "INSERT INTO channel_rights (profile_id, channel_id, status, note, set_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(profile_id, channel_id) DO UPDATE SET status=excluded.status, note=excluded.note, "
            "set_at=excluded.set_at",
            (profile_id, channel_id, status, note, iso()),
        )


def decide(db: Database, profile_id: str, video: dict) -> tuple[str, str]:
    """(status, reason) for one video. `video` must carry channel_id and the YouTube licence."""
    with db.conn() as c:
        row = c.execute(
            "SELECT status, note FROM channel_rights WHERE profile_id=? AND channel_id=?",
            (profile_id, video.get("channel_id")),
        ).fetchone()
    if row and row["status"] in MANUAL:
        return row["status"], f"you set this channel as {row['status']}" + (f" ({row['note']})" if row["note"] else "")
    with db.conn() as c:
        listed = c.execute(
            "SELECT campaign_url, note FROM campaign_videos WHERE profile_id=? AND video_id=?",
            (profile_id, video.get("video_id")),
        ).fetchone()
    if listed:
        return "campaign", f"listed by campaign {listed['campaign_url']}: {listed['note'][:140]}"
    if video.get("license") == "creativeCommon":
        return "cc_by", "YouTube lists this video as Creative Commons"
    return "none", "no permission on record and not Creative Commons"


def add_campaign_videos(db: Database, profile_id: str, video_ids: list[str], campaign_url: str, note: str) -> int:
    """Record that a campaign lists these videos as source material. Returns how many were added."""
    if not campaign_url.startswith("https://"):
        raise ValueError("give the campaign's web link (https://...)")
    if len(note.strip()) < 20:
        raise ValueError("copy the campaign's own wording about its source material into the note")
    added = 0
    with db.conn() as c:
        for vid in video_ids:
            cur = c.execute(
                "INSERT OR IGNORE INTO campaign_videos (profile_id, video_id, campaign_url, note, added_at) "
                "VALUES (?,?,?,?,?)",
                (profile_id, vid, campaign_url, note.strip(), iso()),
            )
            added += cur.rowcount
    return added


def allowed(status: str) -> bool:
    return status in ALLOWED


def remember(db: Database, profile_id: str, video_id: str, status: str, reason: str) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO video_rights (profile_id, video_id, status, reason, checked_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(profile_id, video_id) DO UPDATE SET status=excluded.status, reason=excluded.reason, "
            "checked_at=excluded.checked_at",
            (profile_id, video_id, status, reason, iso()),
        )
