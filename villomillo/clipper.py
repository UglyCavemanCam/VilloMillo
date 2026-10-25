"""Clipping: hand an allowed video to the clipping engine, collect its clips, check them and grade them.

The engine (the Clips Kitty fork in this repository) runs as its own local program on 127.0.0.1:8765.
We talk to it over its documented HTTP API, so the two can be updated separately.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil

import httpx

from . import campaign, store, validate
from .config import ROOT, Profile
from .db import Database
from .jev import Jev

ENGINE_URL = os.environ.get("CLIPS_STUDIO_API", "http://127.0.0.1:8765")
OUTBOX = ROOT / "data" / "villomillo" / "outbox"
MAX_CLIPS_PER_VIDEO = 3
SELF_CONTAINED_MIN = 0.5


class EngineUnavailable(RuntimeError):
    pass


class Engine:
    def __init__(self, base: str = ENGINE_URL, client: httpx.Client | None = None):
        self.base = base.rstrip("/")
        self.http = client or httpx.Client(timeout=60)

    def _call(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            return self.http.request(method, f"{self.base}{path}", **kw)
        except httpx.ConnectError:
            raise EngineUnavailable(f"the clipping engine is not running at {self.base}") from None
        except httpx.HTTPError as e:
            raise EngineUnavailable(f"clipping engine error ({type(e).__name__})") from None

    def submit(self, url: str, *, focus: str, max_clips: int, force: bool = False, preview: bool = False,
               moments: list[int] | None = None, min_score: int | None = None) -> int:
        """Queue a job. `preview` finds and scores the moments and makes no clips; `moments` makes only the
        previewed moments with those ids. Anything else makes the clips as always."""
        body = {"url": url, "focus": focus[:300], "max_clips": max_clips, "captions": True, "force": force}
        if preview:
            body["preview"] = True
        if moments is not None:
            body["moments"] = moments
        if min_score is not None:
            body["min_score"] = min_score
        r = self._call("POST", "/jobs", json=body)
        if r.status_code >= 400:
            raise EngineUnavailable(f"engine refused the job: HTTP {r.status_code} {r.text[:160]}")
        job_id = r.json().get("job_id")
        if job_id is None:  # the engine already made clips for this video, or it is already queued
            raise EngineUnavailable("the engine already has clips for this video; re-make it with force")
        self._call("POST", "/queue/resume")  # the engine's queue starts paused after every restart
        return int(job_id)

    def job(self, job_id: int) -> dict:
        r = self._call("GET", f"/jobs/{job_id}")
        if r.status_code != 200:
            raise EngineUnavailable(f"engine has no job {job_id} (HTTP {r.status_code})")
        return r.json()

    def preview(self, video_id: str) -> dict:
        """What a preview found: {"video": {title, channel, duration, status} or None, "moments": [...with the words]}."""
        r = self._call("GET", f"/videos/{video_id}/preview")
        return r.json() if r.status_code == 200 else {"video": None, "moments": []}

    def discard(self, video_id: str) -> None:
        """Throw away what a preview kept for this video (best effort: an engine that is off has nothing to throw away)."""
        try:
            self._call("DELETE", f"/videos/{video_id}/moments")
        except EngineUnavailable:
            pass

    def discard_download(self, video_id: str) -> int:
        """Delete the engine's downloaded copy of a video so the next job fetches it again. Returns how many files went."""
        r = self._call("DELETE", f"/videos/{video_id}/download")
        return int(r.json().get("removed", 0)) if r.status_code == 200 else 0

    def moments(self, video_id: str) -> list[dict]:
        """The scored moments a preview found for this video, best first (empty until a preview has run)."""
        r = self._call("GET", f"/videos/{video_id}/moments")
        return r.json() if r.status_code == 200 else []

    def log(self, job_id: int) -> str | None:
        """The engine's log text for a job, or None when the engine cannot be reached."""
        try:
            r = self._call("GET", f"/jobs/{job_id}/log")
        except EngineUnavailable:
            return None
        return r.json().get("log", "") if r.status_code == 200 else None

    def clips(self, video_id: str) -> list[dict]:
        r = self._call("GET", f"/videos/{video_id}/clips")
        return r.json() if r.status_code == 200 else []

    def captions_text(self, clip_id: int) -> str:
        r = self._call("GET", f"/clips/{clip_id}/captions")
        if r.status_code != 200:
            return ""
        return " ".join(str(line.get("text", "")) for line in r.json().get("lines", []))

    def media(self, clip_id: int) -> bytes:
        r = self._call("GET", f"/media/{clip_id}")
        if r.status_code != 200:
            raise EngineUnavailable(f"engine has no media for clip {clip_id} (HTTP {r.status_code})")
        return r.content


PREVIEW_MOMENTS = 10  # the most moments one preview returns: the engine's own limit per video


def start_preview(engine: Engine, link, *, focus: str = "", force: bool = False) -> int:
    """Ask the engine to find and score the moments of one video (YouTube, Twitch or Kick) without making any clips.
    Returns the engine's job id; `engine.moments(link.video_id)` has the answer once the job is done. Nothing is
    filtered out by score here, so the plan screen can show every moment and why it will or will not be made."""
    return engine.submit(link.url, focus=focus, max_clips=PREVIEW_MOMENTS, force=force, preview=True, min_score=0)


def start_render(engine: Engine, link, moment_ids: list[int], *, focus: str = "") -> int:
    """Ask the engine to make only these previewed moments. Returns the engine's job id."""
    return engine.submit(link.url, focus=focus, max_clips=len(moment_ids), moments=moment_ids, force=True)


def submit(db: Database, engine: Engine, profile: Profile, video_id: str, *, force: bool = False) -> str:
    """Hand one video to the engine. Returns a short status."""
    with db.conn() as c:
        if c.execute("SELECT 1 FROM clip_jobs WHERE profile_id=? AND video_id=?", (profile.id, video_id)).fetchone():
            return "already submitted"
        video = c.execute("SELECT * FROM videos WHERE profile_id=? AND video_id=?", (profile.id, video_id)).fetchone()
    if video is None:
        return "unknown video"

    job_id = engine.submit(f"https://www.youtube.com/watch?v={video_id}", focus=profile.niche.definition,
                           max_clips=MAX_CLIPS_PER_VIDEO, force=force)
    with db.conn() as c:
        c.execute(
            "INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (profile.id, video_id, job_id, "submitted", store.iso(), store.iso()),
        )
    return f"submitted as engine job {job_id}"


HOOK = [
    "Nothing in the opening suggests a striking opening or claim.",
    "A mild premise; the opening would need luck to hold a viewer.",
    "An interesting premise, but not obviously striking.",
    "A clear promise of something striking: a bold claim, a surprising guest, a conflict.",
    "An unmistakable hook: a shocking claim, a famous clash or an irresistible question.",
]


def grade_clip(db: Database, jev: Jev, profile: Profile, clip_id: int, text: str) -> dict:
    """Jev reads the clip's own words: does it stand alone, does it start strongly, does it land?"""
    questions = {
        "self_contained": {
            "type": "noul",
            "instructions": "Could a viewer who has not seen the rest of the video understand and enjoy this clip?",
        },
        "hook": {
            "type": "score",
            "instructions": "How strong is the opening of this clip as a hook?",
            "criteria": HOOK,
        },
        "payoff": {
            "type": "score",
            "instructions": "Does the clip end on a payoff, or does it trail off?",
            "criteria": [
                "Trails off with no point",
                "Ends on a weak or unfinished thought",
                "Ends on a clear but small point",
                "Ends on a satisfying point",
                "Ends on a strong punchline or conclusion",
            ],
        },
        "ad_read": {
            "type": "noul",
            "instructions": "Is this clip, or most of it, an ad read, sponsor message or promotion for a product or service?",
        },
    }
    state = {"niche": {"definition": profile.niche.definition}, "clip_text": text[:4000]}
    result = jev.ask(state, questions)
    with db.conn() as c:
        store.add_ai_usage(c, "jev", result.usage.get("input_tokens") or 0, 0, result.cost_usd)
    return result.answers


def collect(db: Database, engine: Engine, jev: Jev, profile: Profile, job: dict, formatter=None, *,
            category: str | None = None, campaign_id: str | None = None, campaign_rules=None, note=None) -> str:
    """Look at a submitted job. Returns 'waiting', 'done' or 'failed', with a short status.

    `formatter(clip, job, out_path)` gives each clip the channel's look from the source video. If it fails, the clip
    keeps the engine's own render and says so; a failed look never loses a clip.
    """
    remote = engine.job(job["engine_job_id"])
    if remote["status"] in ("queued", "running"):
        return "waiting"
    if remote["status"] == "failed":
        with db.conn() as c:
            c.execute("UPDATE clip_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                      (remote.get("error", "")[:300], store.iso(), job["id"]))
        return f"failed in the engine: {remote.get('error', '')[:120]}"

    OUTBOX.joinpath(profile.id).mkdir(parents=True, exist_ok=True)
    saved = checked = 0
    for clip in engine.clips(job["video_id"]):
        with db.conn() as c:
            seen = c.execute("SELECT 1 FROM clips_ready WHERE profile_id=? AND engine_clip_id=?",
                             (profile.id, clip["id"])).fetchone()
        if seen:
            continue
        path = OUTBOX / profile.id / f"{clip['id']}.mp4"
        look = "engine's own look"
        if formatter is None:
            path.write_bytes(engine.media(clip["id"]))
        else:
            raw = path.with_name(f"{clip['id']}.engine.mp4")
            raw.write_bytes(engine.media(clip["id"]))
            try:
                look = f"recipe {formatter(clip, job, path)['recipe']}"
            except Exception as e:  # a failed look never loses the clip
                shutil.copyfile(raw, path)
                look = f"look not applied: {str(e)[:160]}"
        verdict = validate.check(path)
        text = engine.captions_text(clip["id"])
        if verdict["ok"]:
            answers = grade_clip(db, jev, profile, clip["id"], text)
            self_contained = float(answers["self_contained"]["noul"])
            grade = {"self_contained": round(self_contained, 3),
                     "hook": answers["hook"]["score"], "payoff": answers["payoff"]["score"],
                     "ad_read": campaign.ad_read_from(answers), "caption_text": text[:4000], "look": look}
            state, reason = "review", "passed checks; waiting for your decision" + (
                f" ({look})" if look.startswith("look not applied") else "")
            if self_contained < SELF_CONTAINED_MIN:
                state, reason = "rejected_auto", f"does not stand alone (self-contained {self_contained:.0%})"
            elif campaign_rules:
                rules = campaign.check(campaign_rules, path=path, duration_s=verdict["facts"]["duration_s"],
                                       caption_text=text, ad_read=grade["ad_read"])
                grade["campaign"] = {**rules, "checked_at": store.iso()}
                if not rules["ok"]:
                    state, reason = "rejected_auto", "campaign rule: " + "; ".join(rules["problems"])
        else:
            grade = {}
            state, reason = "failed_validation", "; ".join(verdict["problems"])
        with db.conn() as c:
            c.execute(
                "INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                "file_path, validation_json, grade_json, state, reason, created_at, category, campaign_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (profile.id, clip["id"], job["video_id"], clip["start_s"], clip["end_s"], clip["score"],
                 clip.get("hook"), str(path), json.dumps(verdict), json.dumps(grade), state, reason, store.iso(),
                 category or profile.category or None, campaign_id),
            )
        saved += 1
        checked += verdict["ok"]
        if note:
            note(f"Clip {clip['id']}: " + ("passed every check." if state == "review" else f"stopped: {reason}"), "info" if state == "review" else "warn")
    with db.conn() as c:
        c.execute("UPDATE clip_jobs SET status='done', updated_at=? WHERE id=?", (store.iso(), job["id"]))
    return f"{saved} clips made, {checked} passed the checks"


def refresh_files(db: Database, engine: Engine, profile: Profile, video_id: str) -> list[str]:
    """Copy the engine's current file for every clip we already hold from one video.

    After a video is re-rendered (for example from a better download) the engine keeps its clip ids, so
    `collect` skips them and our copies stay old. A new file replaces ours only if it passes the file checks;
    otherwise the old one is kept. Returns one line per clip.
    """
    with db.conn() as c:
        rows = c.execute("SELECT * FROM clips_ready WHERE profile_id=? AND video_id=? ORDER BY id",
                         (profile.id, video_id)).fetchall()
    lines = []
    for r in rows:
        path = pathlib.Path(r["file_path"])
        data = engine.media(r["engine_clip_id"])
        if path.exists() and path.read_bytes() == data:
            lines.append(f"clip {r['engine_clip_id']}: unchanged")
            continue
        fresh = path.with_name(path.stem + ".new.mp4")
        fresh.write_bytes(data)
        verdict = validate.check(fresh)
        if not verdict["ok"]:
            fresh.unlink()
            lines.append(f"clip {r['engine_clip_id']}: the new file failed its checks, kept the old one: "
                         + "; ".join(verdict["problems"]))
            continue
        fresh.replace(path)
        with db.conn() as c:
            c.execute("UPDATE clips_ready SET validation_json=? WHERE id=?", (json.dumps(verdict), r["id"]))
        lines.append(f"clip {r['engine_clip_id']}: replaced with the new file ({len(data) / 1e6:.1f} MB)")
    return lines


def recheck(db: Database, engine: Engine, jev: Jev, profile: Profile, campaign_id: str, campaign_rules) -> list[str]:
    """Run the campaign rules again on clips waiting for you or already approved.

    A clip waiting for you that now fails is held back. An approved clip is never changed here: the
    failure is shown on its card so you can decide. Returns one line per clip.
    """
    with db.conn() as c:
        rows = c.execute("SELECT * FROM clips_ready WHERE profile_id=? AND campaign_id=? AND state IN ('review','approved') ORDER BY id",
                         (profile.id, campaign_id)).fetchall()
    lines = []
    for r in rows:
        grade = json.loads(r["grade_json"])
        text = grade.get("caption_text")
        if text is None:  # clips made before the caption text was kept
            text = engine.captions_text(r["engine_clip_id"])
            grade["caption_text"] = text[:4000]
        if "ad_read" not in grade:  # clips graded before the ad-read question existed
            grade["ad_read"] = campaign.ad_read_from(grade_clip(db, jev, profile, r["engine_clip_id"], text))
        duration = json.loads(r["validation_json"]).get("facts", {}).get("duration_s", r["end_s"] - r["start_s"])
        rules = campaign.check(campaign_rules, path=pathlib.Path(r["file_path"]), duration_s=duration,
                               caption_text=text, ad_read=grade["ad_read"])
        grade["campaign"] = {**rules, "checked_at": store.iso()}
        state, reason = r["state"], r["reason"]
        if state == "review" and not rules["ok"]:
            state, reason = "rejected_auto", "campaign rule: " + "; ".join(rules["problems"])
        with db.conn() as c:
            c.execute("UPDATE clips_ready SET grade_json=?, state=?, reason=? WHERE id=?",
                      (json.dumps(grade), state, reason, r["id"]))
        verdict = "passed" if rules["ok"] else "; ".join(rules["problems"])
        lines.append(f"clip {r['engine_clip_id']} ({r['state']}): {verdict}")
    return lines


def decide(db: Database, profile_id: str, clip_id: int, decision: str) -> None:
    """Your decision on a clip: approve or reject. Only clips in the review queue can be decided."""
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be approve or reject")
    state = "approved" if decision == "approve" else "rejected"
    with db.conn() as c:
        row = c.execute("SELECT state FROM clips_ready WHERE profile_id=? AND id=?", (profile_id, clip_id)).fetchone()
        if row is None:
            raise LookupError("no such clip")
        if row["state"] != "review":
            raise ValueError(f"this clip is {row['state']}, not waiting for a decision")
        c.execute("UPDATE clips_ready SET state=?, decided_at=? WHERE id=?", (state, store.iso(), clip_id))
