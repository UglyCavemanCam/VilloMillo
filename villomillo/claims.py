"""One video, one owner. Paste, Research and Campaigns all start videos, and none may take a video another way has.

A video is owned while a run for it is being worked on (analysing, planned, making), and after it was clipped (finished); the
automatic ways also treat a video that was looked at and found worth nothing as owned, so they do not try it again. A run that
failed or was removed frees the video. You can always take a video yourself: if it is being worked on you are shown that run, and
if it was already clipped you are asked before it is clipped again.

This module only reads. `runs.create` makes the claim itself, in one database step, so two ways in cannot both win.
"""

from __future__ import annotations

from .db import Database

ACTIVE = ("analysing", "planned", "making")
OWNING = (*ACTIVE, "finished")
OWNING_FOR_AUTOMATIC = (*OWNING, "nothing")


def holder(db: Database, video_id: str, states: tuple[str, ...] = OWNING_FOR_AUTOMATIC) -> dict | None:
    """The newest run that owns this video, or None when it is free."""
    marks = ",".join("?" * len(states))
    with db.conn() as c:
        row = c.execute(f"SELECT * FROM runs WHERE video_id=? AND state IN ({marks}) ORDER BY id DESC LIMIT 1", (video_id, *states)).fetchone()
    return dict(row) if row else None


def phrase(db: Database, run: dict) -> str:
    """Who has the video and what they did with it, as the end of a sentence: 'Research is already working on it'."""
    done = run["state"] in ("finished", "nothing")
    who = {"research": "Research"}.get(run["source"], run["source"])
    if who == "campaign" and run.get("campaign_id"):
        with db.conn() as c:
            row = c.execute("SELECT title FROM campaigns WHERE id=?", (run["campaign_id"],)).fetchone()
        title = row["title"] if row else "a campaign"
        who = f'the campaign "{title}"'
    if run["state"] == "nothing":
        return {"paste": "you already looked at this video and nothing in it was worth making", "research": "Research already looked at it and nothing in it was worth making"}.get(
            run["source"], f"{who} already looked at it and nothing in it was worth making")
    if run["source"] == "paste":
        return "you already clipped this video" if done else "you are already working on this video"
    return f"{who} already clipped it" if done else f"{who} is already working on it"


def reserved_by(db: Database, video_id: str) -> str | None:
    """Who has queued this video without starting it yet (a campaign's queue, or a Research candidate), or None."""
    with db.conn() as c:
        q = c.execute("SELECT cm.title FROM campaign_queue q LEFT JOIN campaigns cm ON cm.id=q.campaign_id "
                      "WHERE q.video_id=? AND q.run_id IS NULL AND q.skipped_reason IS NULL LIMIT 1", (video_id,)).fetchone()
        if q:
            return f'the campaign "{q["title"] or "a campaign"}" has queued it'
        r = c.execute("SELECT 1 FROM research_candidates WHERE video_id=? AND state='queued'", (video_id,)).fetchone()
    return "Research has queued it" if r else None
