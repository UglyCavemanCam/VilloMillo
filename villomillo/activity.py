"""A live record of what the system is doing, so you can tell it is working and where it is.

Every step of a video (paste, Research or Campaign), and every pass of Research and Campaigns, writes short lines here. The video
page turns them into a timeline, the Research and Campaigns pages show their own feed, and Home shows what is happening now.
A line that repeats the one before it (a poll that found nothing new) is not written again, so polling never floods the log.
Lines older than two weeks are deleted.
"""

from __future__ import annotations

from datetime import timedelta

from . import store
from .db import Database

KEEP_DAYS = 14
STALLED_AFTER_S = 600  # a running video with no news for this long is flagged

# A video's road, in order. The step a line belongs to decides how the timeline reads.
STEPS = [("look", "Look at the video"), ("score", "Score the moments"), ("plan", "Plan"), ("make", "Make the clips"),
         ("check", "Check the clips"), ("post", "Plan the posts")]
ORDER = [k for k, _ in STEPS]
STAGES = {0: "Waiting for the engine to start on it", 1: "Downloading the video", 2: "Transcribing the speech",
          3: "Finding and ranking the best moments", 4: "Making the clips"}


def log(db: Database, kind: str, message: str, *, run_id: int | None = None, step: str | None = None, level: str = "info") -> None:
    """Write one line, unless it says exactly what the previous line of the same video (or the same kind of pass) said."""
    with db.conn() as c:
        last = c.execute("SELECT message FROM activity WHERE kind=? AND run_id IS ? ORDER BY id DESC LIMIT 1", (kind, run_id)).fetchone()
        if last and last["message"] == message:
            return
        c.execute("INSERT INTO activity (ts, kind, run_id, step, level, message) VALUES (?,?,?,?,?,?)",
                  (store.iso(), kind, run_id, step, level, message[:300]))


def for_run(db: Database, run_id: int, limit: int = 200) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT * FROM activity WHERE run_id=? ORDER BY id DESC LIMIT ?", (run_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def recent(db: Database, kind: str, limit: int = 25) -> list[dict]:
    """The latest lines of one kind, newest first: for Research and Campaigns this includes the lines of their videos."""
    with db.conn() as c:
        rows = c.execute("SELECT * FROM activity WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit)).fetchall()
    return [dict(r) for r in rows]


def latest_by_run(db: Database, run_ids: list[int]) -> dict[int, str]:
    """The newest line of each of these videos: what a video is doing, for a list."""
    if not run_ids:
        return {}
    marks = ",".join("?" * len(run_ids))
    with db.conn() as c:
        rows = c.execute(f"SELECT run_id, message FROM activity WHERE id IN (SELECT MAX(id) FROM activity WHERE run_id IN ({marks}) GROUP BY run_id)", run_ids).fetchall()
    return {r["run_id"]: r["message"] for r in rows}


def prune(db: Database, days: int = KEEP_DAYS) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM activity WHERE ts<?", (store.iso(store.now() - timedelta(days=days)),))


def age_s(ts: str | None) -> int | None:
    return None if not ts else max(0, int((store.now() - store.parse_time(ts)).total_seconds()))


def next_pass(db: Database, name: str) -> int | None:
    """Minutes until the named schedule next runs (0 when it is due), or None when there is no such schedule."""
    with db.conn() as c:
        row = c.execute("SELECT next_run_at, enabled FROM schedules WHERE name=?", (name,)).fetchone()
    if not row or not row["enabled"]:
        return None
    return max(0, int((store.parse_time(row["next_run_at"]) - store.now()).total_seconds() // 60) + 1)


def timeline(db: Database, run: dict, engine_state: str = "on") -> dict:
    """The video's road as steps (done / now / waiting / skipped) with times, the line that says what is happening now, and any stall."""
    lines = for_run(db, run["id"])
    first, last = {}, {}
    for line in lines:
        step = line["step"]
        if step in ORDER:
            first.setdefault(step, line["ts"])
            last[step] = line["ts"]
    state = run["state"]
    if state in ("analysing",):
        now_step = "score" if "score" in first else "look"
    elif state == "planned":
        now_step = "plan"
    elif state == "making":
        now_step = "check" if "check" in first else "make"
    elif state == "finished":
        now_step = None
    else:  # nothing, failed, removed
        now_step = ORDER[max((ORDER.index(s) for s in first), default=0)] if first else "look"
    steps = []
    for key, name in STEPS:
        if state == "finished":
            status = "done" if key in first or key in ("look", "score", "plan", "make", "check") else "skipped"
        elif key == now_step and state in ("analysing", "planned", "making"):
            status = "now"
        elif now_step and ORDER.index(key) < ORDER.index(now_step):
            status = "done"
        elif state in ("nothing", "failed", "removed") and key == now_step:
            status = "stopped"
        elif state in ("nothing", "failed", "removed") and now_step and ORDER.index(key) > ORDER.index(now_step):
            status = "skipped"
        else:
            status = "waiting"
        steps.append({"key": key, "name": name, "status": status, "at": first.get(key), "last": last.get(key)})
    latest = lines[-1] if lines else None
    active = state in ("analysing", "making")
    age = age_s(latest["ts"]) if latest else age_s(run["updated_at"])
    stalled = None
    if active and engine_state != "on":
        stalled = {"on": "", "starting": "The clip engine is starting up; this video waits for it.", "not_responding": "The clip engine is not responding; this video waits for it.",
                   "error": "The clip engine has stopped; this video waits until it is started (Settings, Clip engine).",
                   "off": "The clip engine is off; this video waits until it is started (Settings, Clip engine)."}[engine_state]
    elif active and age is not None and age > STALLED_AFTER_S:
        stalled = f"No news for {age // 60} minutes. If the clip engine is busy with a long video this is normal; otherwise check Settings, Clip engine."
    now = {"analysing": "Looking at the video", "planned": "Waiting for you to press Start", "making": "Making the clips",
           "finished": "Done", "nothing": "Stopped: nothing worth making", "failed": "Failed", "removed": "Removed"}.get(state, state)
    if latest and active:
        now = latest["message"]
    return {"steps": steps, "lines": lines, "now": now, "age_s": age, "stalled": stalled, "active": active}
