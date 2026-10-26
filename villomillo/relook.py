"""Applying a format to a clip that has none: the clip is cut again from the downloaded video in its category's format, checked,
and put in place of the file it has now. Only a clip waiting for your review can be given a format.

A clip that already went to YouTube stays there as it was: use Post again to send the new version.

The work runs in the background (the "relook" task), so this module also says where a clip's format stands: waiting for its turn,
being made, done, or failed with the reason (state). The clip panel shows that live.
"""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime

from core.paths import discard, replace_with_retry

from . import formats, store, tasks, validate
from .services import Services

KINDS = ("relook",)   # handled by their own worker, so applying a format never waits behind a long clip job
TRIES = 2             # a second try settles a text or network hiccup (making a clip does the same)
WAITING = "passed checks; waiting for your decision"


class RelookError(RuntimeError):
    """The message is safe to show."""


def apply(sv: Services, clip_id: int, *, template_id: str | None = None, design: dict | None = None) -> str:
    """Give one clip a format: a chosen template, a one-off design made for just this clip (Edit look), or else its category's
    template. Returns a short line; raises RelookError with the reason when it cannot."""
    with sv.db.conn() as c:
        clip = c.execute("SELECT * FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
    if clip is None or clip["state"] in ("removed", "superseded"):
        raise RelookError("this clip is gone")
    if clip["state"] != "review":
        raise RelookError(f"this clip is {clip['state']}, so it cannot be given a format now")
    owner = sv.profiles.get(clip["profile_id"])
    if owner is None:
        raise RelookError("its channel no longer exists")
    category = clip["category"] or owner.category
    if design is not None:
        try:
            design = formats.designs.normalize(design)
        except formats.designs.DesignError as e:
            raise RelookError(str(e)) from None
        template = {"id": template_id, "name": "Edit look", "layers": formats.designs.layers_from(design), "settings": design}
    else:
        template = (formats.templates.get(sv.db, template_id) if template_id else formats.templates.for_category(sv.db, category))
    if template is None:
        raise RelookError(f"there is no format for the {category or 'this'} category yet: add one on the Formats page")
    if not clip["file_path"]:
        raise RelookError("the clip's file was deleted")
    formatter = formats.make_formatter(sv.db, owner, template, jev=sv.jev, breaker=sv.app.spend_breaker)
    path = pathlib.Path(clip["file_path"])
    tmp = path.with_name(f"{path.stem}.format.mp4")
    try:
        result, last = None, ""
        for _ in range(TRIES):
            try:
                result = formatter({"start_s": clip["start_s"], "end_s": clip["end_s"]}, {"video_id": clip["video_id"], "campaign_id": clip["campaign_id"], "category": clip["category"]}, tmp)
                break
            except Exception as e:
                last = str(e)[:200] or type(e).__name__
        if result is None:
            raise RelookError(f"the format could not be applied: {last}")
        verdict = validate.check(tmp)
        if not verdict["ok"]:
            raise RelookError("the new version failed its checks: " + "; ".join(verdict["problems"]))
        if not replace_with_retry(tmp, path, label="Format"):
            raise RelookError("the clip's file is open elsewhere and could not be replaced")
    finally:
        discard(tmp)
    grade = json.loads(clip["grade_json"] or "{}")
    grade.update(look=f"template {template['id']}" if template["id"] else "edited look", look_design=design,
                 effects=result.get("effects", []), music=result.get("music"))
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET grade_json=?, validation_json=?, template_id=?, format_applied=1, format_note=NULL, reason=? WHERE id=?",
                  (json.dumps(grade), json.dumps(verdict), template["id"], WAITING, clip_id))
    store.event(sv.db, "info", "clips", f"clip {clip_id} given the {template['name']} format")
    return f"clip {clip_id}: the {template['name']} format was applied"


def fail(db, clip_id: int, message: str) -> None:
    """Remember why the last try failed, so the clip panel can say it."""
    with db.conn() as c:
        c.execute("UPDATE clips_ready SET format_note=?, reason=? WHERE id=?",
                  (message[:240], f"{WAITING}; needs a format: {message[:160]}", clip_id))


# --- the queue: one try at a time per clip, and where each clip stands ----------------------------------------------

def _pending(db) -> dict[int, dict]:
    """Clip id -> its unfinished apply task (waiting its turn, being made, or waiting to retry)."""
    with db.conn() as c:
        found = c.execute("SELECT id, status, attempts, created_at, last_error, payload_json FROM tasks "
                          "WHERE kind='relook' AND status IN ('queued','running')").fetchall()
    return {json.loads(r["payload_json"])["clip_id"]: dict(r) for r in found}


def queue(db, clip_id: int, *, template_id: str | None = None, design: dict | None = None) -> bool:
    """Ask for a format to be applied. False when this clip already has one on its way (it is not asked twice)."""
    if clip_id in _pending(db):
        return False
    payload: dict = {"clip_id": clip_id}
    if template_id:
        payload["template_id"] = template_id
    if design is not None:
        payload["design"] = design
    # the key is unique each time: a try right after a failed one must not be taken for the same request
    tasks.enqueue(db, "relook", payload, dedupe_key=f"relook:{clip_id}:{time.time_ns()}", priority=10)
    return True


def state(db, clip_id: int) -> dict:
    """{"state": formatted | waiting | working | failed | idle | gone, "note": why it failed, "elapsed": seconds in the queue}."""
    with db.conn() as c:
        clip = c.execute("SELECT format_applied, format_note FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
    if clip is None:
        return {"state": "gone", "note": "", "elapsed": 0}
    task = _pending(db).get(clip_id)
    if task:
        try:
            elapsed = max(0, int((store.now() - datetime.fromisoformat(task["created_at"])).total_seconds()))
        except ValueError:
            elapsed = 0
        return {"state": "working" if task["status"] == "running" else "waiting", "note": task["last_error"] or "", "elapsed": elapsed}
    if clip["format_note"]:
        return {"state": "failed", "note": clip["format_note"], "elapsed": 0}
    return {"state": "formatted" if clip["format_applied"] else "idle", "note": "", "elapsed": 0}


def _plan(sv: Services, *, profile_id: str | None, category: str | None, retry_failed: bool) -> list[int]:
    """The clips waiting for review that have no format and whose category has one: the ones worth applying a format to now."""
    key = formats.templates.category_key(category)
    with sv.db.conn() as c:
        found = c.execute("SELECT id, profile_id, category, format_note FROM clips_ready WHERE state='review' AND format_applied=0 "
                          "AND file_path IS NOT NULL" + (" AND profile_id=?" if profile_id else "") + " ORDER BY id",
                          (profile_id,) if profile_id else ()).fetchall()
    pending, ids = _pending(sv.db), []
    for r in found:
        owner = sv.profiles.get(r["profile_id"])
        own = r["category"] or (owner.category if owner else None)
        if owner is None or r["id"] in pending or (r["format_note"] and not retry_failed):
            continue
        if key and formats.templates.category_key(own) != key:
            continue
        if formats.templates.for_category(sv.db, own) is not None:
            ids.append(r["id"])
    return ids


def fixable(sv: Services, profile_id: str) -> int:
    """How many waiting clips of this channel could be given their category's format right now."""
    return len(_plan(sv, profile_id=profile_id, category=None, retry_failed=True))


def queue_waiting(sv: Services, *, profile_id: str | None = None, category: str | None = None, retry_failed: bool = False) -> int:
    """Apply the category's format to every waiting clip that has none. Clips whose last try failed are left alone unless
    retry_failed (a format being saved should not keep trying a clip that cannot be formatted). Returns how many were queued."""
    return sum(queue(sv.db, i) for i in _plan(sv, profile_id=profile_id, category=category, retry_failed=retry_failed))


def in_progress(sv: Services, profile_id: str) -> int:
    """How many of this channel's clips have a format on its way."""
    pending = _pending(sv.db)
    if not pending:
        return 0
    with sv.db.conn() as c:
        mine = {r["id"] for r in c.execute("SELECT id FROM clips_ready WHERE profile_id=?", (profile_id,))}
    return len(mine & set(pending))
