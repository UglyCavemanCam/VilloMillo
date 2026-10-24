"""Campaign rules that code can check on a finished clip.

Checked here: length, the host's face in the first seconds, subtitles present, and no ad read.
Not checked here, and left to you at review: whether a cut misrepresents the guest, whether a caption is
fair, the audience split, and the 30-day and engagement rules. Those are in the campaign brief.

The face check finds any face, not a particular person. You confirm it is the host when you review.
"""

from __future__ import annotations

import pathlib

from .config import Campaign
from .db import Database
from .store import iso

FACE_SAMPLE_S = 0.5  # look at a frame this often inside the window
FACE_MIN_SHARE = 0.5  # a face must be in at least half of those frames
AD_READ_LIMIT = 0.5  # Jev's probability that this is an ad read, above which the clip is rejected
CASCADE = "haarcascade_frontalface_default.xml"


def face_share(path: pathlib.Path, window_s: float) -> float:
    """The share of sampled frames in the first `window_s` seconds that contain a face."""
    import cv2

    from video.capture import video_capture

    cascade = cv2.CascadeClassifier(str(pathlib.Path(cv2.data.haarcascades) / CASCADE))
    with video_capture(path, required=False) as cap:
        if cap is None:
            return 0.0
        hits = total = 0
        at = 0.0
        while at < window_s:
            cap.set(cv2.CAP_PROP_POS_MSEC, at * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            total += 1
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            side = max(60, int(frame.shape[0] * 0.08))  # ignore faces too small to be the host
            hits += len(cascade.detectMultiScale(gray, 1.1, 5, minSize=(side, side))) > 0
            at += FACE_SAMPLE_S
        return hits / total if total else 0.0


def check(rules: Campaign, *, path: pathlib.Path, duration_s: float, caption_text: str, ad_read: float | None,
          face=face_share) -> dict:
    """Returns {"ok", "problems", "warnings", "facts"}. `ad_read` is Jev's probability (0-1), or None if not run."""
    problems: list[str] = []
    warnings: list[str] = []
    if not rules.min_s <= duration_s <= rules.max_s:
        problems.append(f"{duration_s:.0f} s; the campaign wants {rules.min_s:.0f} to {rules.max_s:.0f} s")

    share = face(path, rules.face_window_s)
    if share < FACE_MIN_SHARE:
        problems.append(f"no clear face in the first {rules.face_window_s:.0f} s (seen in {share:.0%} of frames)")
    else:
        warnings.append("a face is on screen early: confirm it is Mark when you review")

    if rules.require_captions and not caption_text.strip():
        problems.append("no subtitle text: the campaign needs subtitles on the whole clip")

    if rules.no_ad_reads:
        if ad_read is None:
            problems.append("the ad-read check has not run")
        elif ad_read >= AD_READ_LIMIT:
            problems.append(f"looks like an ad read or sponsor segment ({ad_read:.0%})")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "facts": {"face_share": round(share, 2), "ad_read": ad_read},
    }


def ad_read_from(answers: dict) -> float | None:
    """Jev's ad-read probability from a clip's grading answers, or None if that question was not asked."""
    value = answers.get("ad_read", {}).get("noul")
    return None if value is None else float(value)


def record(db: Database, profile_id: str, clip_id: int, result: dict) -> None:
    """Keep the rule result on the clip's grade, next to the other grades, so the studio shows it."""
    import json

    with db.conn() as c:
        row = c.execute("SELECT grade_json FROM clips_ready WHERE profile_id=? AND id=?", (profile_id, clip_id)).fetchone()
        grade = json.loads(row["grade_json"]) if row else {}
        grade["campaign"] = {**result, "checked_at": iso()}
        c.execute("UPDATE clips_ready SET grade_json=? WHERE profile_id=? AND id=?",
                  (json.dumps(grade), profile_id, clip_id))
