"""Giving a clip the channel's look again, when it came out in the engine's own look (the look could not be applied the first time).

The clip is cut again from the downloaded video in the look its video was planned with, checked, and put in place of the old
file. A clip that already went to YouTube stays there as it was: use Post again to send the new version.
"""

from __future__ import annotations

import json
import pathlib

from core.paths import discard, replace_with_retry

from . import formats, store, validate
from .services import Services


class RelookError(RuntimeError):
    """The message is safe to show."""


def apply(sv: Services, clip_id: int) -> str:
    """Re-make one clip in its look. Returns a short line; raises RelookError with the reason when it cannot."""
    with sv.db.conn() as c:
        clip = c.execute("SELECT * FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
        if clip is None or clip["state"] in ("removed", "superseded"):
            raise RelookError("this clip is gone")
        run = c.execute("SELECT plan_json FROM runs WHERE video_id=? AND plan_json IS NOT NULL ORDER BY id DESC LIMIT 1", (clip["video_id"],)).fetchone()
    owner = sv.profiles.get(clip["profile_id"])
    if owner is None:
        raise RelookError("its channel no longer exists")
    recipe = (json.loads(run["plan_json"]).get("look") if run else None) or owner.format
    formatter = formats.formatter_for(sv.db, owner, jev=sv.jev, breaker=sv.app.spend_breaker, recipe=recipe)
    if formatter is None:
        raise RelookError("this channel has no look set")
    path = pathlib.Path(clip["file_path"] or "")
    if not clip["file_path"]:
        raise RelookError("the clip's file was deleted")
    tmp = path.with_name(f"{path.stem}.look.mp4")
    try:
        try:
            formatter({"start_s": clip["start_s"], "end_s": clip["end_s"]}, {"video_id": clip["video_id"]}, tmp)
        except Exception as e:
            raise RelookError(f"the look could not be applied: {str(e)[:200] or type(e).__name__}") from None
        if not replace_with_retry(tmp, path, label="Look"):
            raise RelookError("the clip's file is open elsewhere and could not be replaced")
    finally:
        discard(tmp)
    verdict = validate.check(path)
    grade = json.loads(clip["grade_json"] or "{}")
    grade.update(look=f"recipe {recipe}", look_failed=False)
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET grade_json=?, validation_json=?, reason=CASE WHEN reason LIKE '%look not applied%' OR reason LIKE '%look could not%' "
                  "THEN 'passed checks; waiting for your decision' ELSE reason END WHERE id=?", (json.dumps(grade), json.dumps(verdict), clip_id))
    store.event(sv.db, "info", "clips", f"clip {clip_id} given its look again ({recipe})")
    return f"clip {clip_id}: the look was applied again ({recipe})"
