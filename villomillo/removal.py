"""Removing clips you do not want, and putting them back.

Removing hides a clip; it does not erase it:
  - the clip moves to the Removed tab, where Restore returns it (an approved clip returns to "Waiting for you", so you decide again);
  - a post that has not gone out is cancelled: it leaves the Calendar and no longer uses up the day's limit;
  - a post that is already on YouTube stays there: nothing here can safely delete a YouTube video, so you delete it in YouTube Studio;
  - the clip's row stays, so the engine's clips are not collected a second time;
  - the video file is deleted 30 days later.
"""

from __future__ import annotations

import pathlib
from datetime import timedelta

from . import store
from .db import Database

PURGE_AFTER_DAYS = 30
NOT_OUT = ("queued", "blocked", "dry_run", "manual_ready", "failed")  # posts that can still be cancelled
OUT = ("uploaded", "posted")


class RemovalError(ValueError):
    """The message is safe to show."""


def _mine(c, profile_id: str, clip_ids: list[int]) -> list[dict]:
    if not clip_ids:
        return []
    marks = ",".join("?" * len(clip_ids))
    return [dict(r) for r in c.execute(f"SELECT * FROM clips_ready WHERE profile_id=? AND id IN ({marks})", (profile_id, *clip_ids))]


def impact(db: Database, profile_id: str, clip_ids: list[int]) -> dict:
    """What removing these clips would touch, for the confirm step: unposted posts, live YouTube posts, handed-in campaign links."""
    with db.conn() as c:
        clips = [r for r in _mine(c, profile_id, clip_ids) if r["state"] != "removed"]
        ids = [r["id"] for r in clips]
        marks = ",".join("?" * len(ids)) or "NULL"
        posts = [dict(r) for r in c.execute(f"SELECT id, clip_id, status, url FROM posts WHERE clip_id IN ({marks})", ids)]
        handed = c.execute(f"SELECT COUNT(*) AS n FROM campaign_submissions s JOIN posts p ON p.id=s.post_id WHERE p.clip_id IN ({marks})", ids).fetchone()["n"]
    return {"clips": len(clips), "cancel": sum(1 for p in posts if p["status"] in NOT_OUT), "live": [p["url"] for p in posts if p["status"] in OUT],
            "handed_in": handed}


def remove(db: Database, profile_id: str, clip_ids: list[int]) -> dict:
    """Hide these clips and cancel their posts that have not gone out. Returns what happened."""
    if not clip_ids:
        raise RemovalError("choose at least one clip")
    now = store.iso()
    with db.conn() as c:
        clips = [r for r in _mine(c, profile_id, clip_ids) if r["state"] not in ("removed", "superseded")]
        if not clips:
            raise RemovalError("those clips are already removed or do not exist")
        cancelled = live = 0
        for r in clips:
            c.execute("UPDATE clips_ready SET state='removed', removed_from=?, removed_at=? WHERE id=?", (r["state"], now, r["id"]))
            for post in c.execute("SELECT id, status FROM posts WHERE clip_id=?", (r["id"],)).fetchall():
                if post["status"] in NOT_OUT:
                    c.execute("UPDATE posts SET status='cancelled', error='Cancelled: the clip was removed' WHERE id=?", (post["id"],))
                    cancelled += 1
                elif post["status"] in OUT:
                    live += 1
    store.event(db, "info", "clips", f"removed {len(clips)} clip(s); {cancelled} post(s) cancelled, {live} already on YouTube")
    return {"removed": len(clips), "cancelled": cancelled, "live": live}


def delete_post(db: Database, profile_id: str, post_id: int) -> dict:
    """Delete a post that has not gone out. The clip stays as it is (approved), so it can be posted again from the Clips page.
    A post already on YouTube cannot be deleted from here: nothing can safely delete a YouTube video, so that is done in YouTube Studio."""
    with db.conn() as c:
        post = c.execute("SELECT id, clip_id, status, url FROM posts WHERE profile_id=? AND id=?", (profile_id, post_id)).fetchone()
        if post is None:
            raise LookupError("no such post")
        if post["status"] in OUT:
            raise RemovalError("this post is already on YouTube: delete the video in YouTube Studio")
        c.execute("DELETE FROM copy_options WHERE post_id=?", (post_id,))
        c.execute("DELETE FROM posts WHERE id=?", (post_id,))
    store.event(db, "info", "posts", f"post {post_id} deleted; its clip {post['clip_id']} is unchanged")
    return {"clip_id": post["clip_id"]}


def restore(db: Database, profile_id: str, clip_ids: list[int]) -> int:
    """Put removed clips back. An approved clip returns to Waiting for you; one the rules stopped goes back to being stopped."""
    restored = 0
    with db.conn() as c:
        for r in _mine(c, profile_id, clip_ids):
            if r["state"] != "removed":
                continue
            if not r["file_path"] or not pathlib.Path(r["file_path"]).exists():
                raise RemovalError(f"clip {r['engine_clip_id']}'s video file was deleted after {PURGE_AFTER_DAYS} days, so it cannot come back")
            state = "review" if r["removed_from"] in (None, "approved", "removed") else r["removed_from"]
            c.execute("UPDATE clips_ready SET state=?, removed_from=NULL, removed_at=? WHERE id=?", (state, None, r["id"]))
            posts = [x["id"] for x in c.execute("SELECT id FROM posts WHERE clip_id=? AND status='cancelled'", (r["id"],))]
            for post_id in posts:  # a fresh plan is made when you approve it again
                c.execute("DELETE FROM copy_options WHERE post_id=?", (post_id,))
                c.execute("DELETE FROM posts WHERE id=?", (post_id,))
            restored += 1
    return restored


def purge_old(db: Database, days: int = PURGE_AFTER_DAYS, now=None) -> int:
    """Delete the video files of clips removed more than `days` ago. The rows stay, so nothing is collected twice."""
    cutoff = store.iso((now or store.now()) - timedelta(days=days))
    with db.conn() as c:
        old = [dict(r) for r in c.execute("SELECT id, file_path FROM clips_ready WHERE state='removed' AND removed_at<? AND file_path IS NOT NULL", (cutoff,))]
        for r in old:
            path = pathlib.Path(r["file_path"])
            for f in (path, path.with_name(path.stem + ".engine.mp4"), path.with_name(path.stem + ".new.mp4")):
                f.unlink(missing_ok=True)
            c.execute("UPDATE clips_ready SET file_path=NULL WHERE id=?", (r["id"],))
    return len(old)
