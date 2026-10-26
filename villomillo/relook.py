"""Applying a format to a clip that has none: the clip is cut again from the downloaded video in its category's format, checked,
and put in place of the file it has now. Only a clip waiting for your review can be given a format.

A clip that already went to YouTube stays there as it was: use Post again to send the new version.
"""

from __future__ import annotations

import json
import pathlib

from core.paths import discard, replace_with_retry

from . import formats, store, validate
from .services import Services


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
        try:
            result = formatter({"start_s": clip["start_s"], "end_s": clip["end_s"]}, {"video_id": clip["video_id"], "campaign_id": clip["campaign_id"], "category": clip["category"]}, tmp)
        except Exception as e:
            raise RelookError(f"the format could not be applied: {str(e)[:200] or type(e).__name__}") from None
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
        c.execute("UPDATE clips_ready SET grade_json=?, validation_json=?, template_id=?, format_applied=1, "
                  "reason=CASE WHEN reason LIKE 'passed checks%' THEN 'passed checks; waiting for your decision' ELSE reason END "
                  "WHERE id=?", (json.dumps(grade), json.dumps(verdict), template["id"], clip_id))
    store.event(sv.db, "info", "clips", f"clip {clip_id} given the {template['name']} format")
    return f"clip {clip_id}: the {template['name']} format was applied"
