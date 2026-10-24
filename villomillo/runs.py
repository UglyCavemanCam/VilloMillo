"""A pasted link, from "look at it" to posted clips.

Steps, each one a short task for the worker so nothing blocks it while the engine works:
  analyse   the engine finds and scores the moments (a preview, no clips), then the plan is made: kind, category,
            channel, fit and a score for every moment;
  plan      you see the plan and press Start (or Remove video when nothing clears the floors);
  make      the engine makes only the clips that cleared the floors, in the look the kind of video decides;
  finish    each clip that passed its checks is approved, routed to its category's channel and planned for posting.
The state of a run is kept in the `runs` table, so the dashboard can show it and a restart loses nothing.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import timedelta

from . import channels, clipper, links, routing, scoring, store, tasks
from .services import Services

POLL_S = 20  # how often a waiting engine job is looked at
ACTIVE = ("analysing", "planned", "making")


class RunError(ValueError):
    """Something about this run is wrong. The message is safe to show."""


def _run(db, run_id: int) -> dict:
    with db.conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise LookupError("no such run")
    return dict(row)


def get(db, run_id: int) -> dict:
    """A run with its plan opened up."""
    r = _run(db, run_id)
    r["plan"] = json.loads(r["plan_json"]) if r["plan_json"] else None
    return r


def _set(db, run_id: int, **fields) -> None:
    fields["updated_at"] = store.iso()
    with db.conn() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), run_id))


def _later(sv: Services, kind: str, run_id: int, seconds: float = POLL_S) -> None:
    now = store.now()
    tasks.enqueue(sv.db, kind, {"run": run_id}, dedupe_key=f"{kind}:{run_id}:{int(now.timestamp() // max(seconds, 1))}",
                  run_after=now + timedelta(seconds=seconds))


def create(sv: Services, owner_id: str, text: str, *, source: str = "paste") -> int:
    """Start a run for a pasted link. Raises links.LinkError for a link that is not a video we can clip. A video
    that is already being worked on returns its existing run."""
    link = links.parse(text)
    with sv.db.conn() as c:
        live = c.execute(f"SELECT id FROM runs WHERE video_id=? AND state IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC",
                         (link.video_id, *ACTIVE)).fetchone()
        if live:
            return live["id"]
        c.execute("INSERT INTO runs (source, url, platform, video_id, profile_id, state, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                  (source, link.url, link.platform, link.video_id, owner_id, "analysing", store.iso(), store.iso()))
        run_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    tasks.enqueue(sv.db, "paste_analyse", {"run": run_id}, dedupe_key=f"paste_analyse:{run_id}:first")
    return run_id


def fail(sv: Services, run_id: int, message: str) -> None:
    _set(sv.db, run_id, state="failed", error=message[:300])
    store.event(sv.db, "error", "run", f"run {run_id} failed: {message[:200]}")


# ---- analyse -----------------------------------------------------------------------------------------------------


def analyse(sv: Services, run_id: int, engine: clipper.Engine | None = None) -> None:
    """One step of looking at the video. Asks the engine, then looks again later until it is done."""
    run = _run(sv.db, run_id)
    if run["state"] != "analysing":
        return
    engine = engine or clipper.Engine()
    link = links.parse(run["url"])
    if run["preview_job_id"] is None:
        job_id = clipper.start_preview(engine, link, focus="")
        _set(sv.db, run_id, preview_job_id=job_id)
        return _later(sv, "paste_analyse", run_id)
    job = engine.job(run["preview_job_id"])
    if job["status"] in ("queued", "running"):
        return _later(sv, "paste_analyse", run_id)
    if job["status"] != "done":
        return fail(sv, run_id, f"the engine could not look at this video: {job.get('error') or job['status']}")
    preview = engine.preview(link.video_id)
    video = preview.get("video") or {}
    _set(sv.db, run_id, title=video.get("title") or run["title"], channel=video.get("channel"), duration=video.get("duration"))
    if not preview["moments"]:
        return _set(sv.db, run_id, state="nothing", note="No moments worth clipping were found in this video.", plan_json=None)
    plan = make_plan(sv, run_id, preview)
    state = "planned" if plan["make_ids"] else "nothing"
    note = None if plan["make_ids"] else "No clip here is likely to earn enough."
    _set(sv.db, run_id, state=state, note=note, plan_json=json.dumps(plan), profile_id=plan["owner"])


def make_plan(sv: Services, run_id: int, preview: dict) -> dict:
    """Score the preview and decide where its clips would go. Everything the plan screen shows is in the result."""
    run = _run(sv.db, run_id)
    rules, breaker = sv.app.scoring, sv.app.spend_breaker
    result = scoring.score_video(sv.db, sv.jev, rules, preview, source=run["source"], breaker=breaker)
    kind = result.kind
    channel = channels.for_category(sv.profiles, kind.category)
    fit = fit_why = None
    if channel is not None:
        excerpts = [m.get("text", "") for m in preview["moments"] if m.get("text")]
        fit, fit_why = scoring.channel_fit(sv.db, sv.jev, channel, excerpts, breaker=breaker)
    moments = [asdict(m) for m in result.moments]
    make = [m["id"] for m in moments if m["make"]]
    return {"kind": asdict(kind), "category": kind.category, "look": kind.look,
            "channel": {"id": channel.id, "name": channel.display_name} if channel else None,
            "fit": fit, "fit_why": fit_why, "fit_ok": fit is None or fit >= scoring.FIT_MIN,
            "owner": channel.id if channel else run["profile_id"],
            "moments": moments, "make_ids": sorted(make),
            "expected_total": round(sum(m["revenue"] for m in moments if m["make"]), 2)}


# ---- start and remove --------------------------------------------------------------------------------------------


def start(sv: Services, run_id: int, engine: clipper.Engine | None = None) -> None:
    """You pressed Start: make only the moments that cleared the floors."""
    run = get(sv.db, run_id)
    if run["state"] == "nothing":
        raise RunError("no clip here cleared the floors")
    if run["state"] != "planned":
        raise RunError(f"this video is {run['state']}, not waiting for Start")
    plan = run["plan"]
    if not plan or not plan["make_ids"]:
        raise RunError("no clip here cleared the floors")
    owner = sv.profiles.get(plan["owner"]) or sv.profiles[run["profile_id"]]
    engine = engine or clipper.Engine()
    job_id = clipper.start_render(engine, links.parse(run["url"]), plan["make_ids"])
    with sv.db.conn() as c:
        store.upsert_pasted_video(c, owner.id, run["video_id"], run["title"], run["channel"], run["duration"])
        c.execute("DELETE FROM clip_jobs WHERE profile_id=? AND video_id=?", (owner.id, run["video_id"]))  # a video can be made again
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                  (owner.id, run["video_id"], job_id, "submitted", store.iso(), store.iso()))
    _set(sv.db, run_id, state="making", render_job_id=job_id, profile_id=owner.id, error=None)
    _later(sv, "paste_collect", run_id)


def remove(sv: Services, run_id: int, engine: clipper.Engine | None = None) -> None:
    """Remove video: the run is closed and what was analysed is thrown away."""
    run = _run(sv.db, run_id)
    if run["state"] not in ("planned", "nothing", "failed"):
        raise RunError(f"this video is {run['state']} and cannot be removed now")
    (engine or clipper.Engine()).discard(run["video_id"])
    _set(sv.db, run_id, state="removed", plan_json=None, note="Removed.")


def retry(sv: Services, run_id: int) -> None:
    run = _run(sv.db, run_id)
    if run["state"] != "failed":
        raise RunError("only a failed video can be tried again")
    _set(sv.db, run_id, state="analysing", error=None, preview_job_id=None, render_job_id=None, plan_json=None, note=None)
    tasks.enqueue(sv.db, "paste_analyse", {"run": run_id}, dedupe_key=f"paste_analyse:{run_id}:retry:{int(store.now().timestamp())}")


# ---- make, check, route ------------------------------------------------------------------------------------------


def collect(sv: Services, run_id: int, engine: clipper.Engine | None = None) -> None:
    """One step of waiting for the engine to make the clips; when it has, check them and send the good ones on."""
    from . import formats

    run = get(sv.db, run_id)
    if run["state"] != "making":
        return
    plan, engine = run["plan"], engine or clipper.Engine()
    owner = sv.profiles[run["profile_id"]].model_copy(update={"campaign": None})  # campaign rules belong to campaigns, not pasted links
    with sv.db.conn() as c:
        job = c.execute("SELECT * FROM clip_jobs WHERE profile_id=? AND video_id=?", (owner.id, run["video_id"])).fetchone()
    if job is None:
        return fail(sv, run_id, "the clip job went missing")
    formatter = formats.formatter_for(sv.db, owner, jev=sv.jev, breaker=sv.app.spend_breaker, recipe=plan["look"])
    outcome = clipper.collect(sv.db, engine, sv.jev, owner, dict(job), formatter, category=plan["category"])
    if outcome == "waiting":
        return _later(sv, "paste_collect", run_id)
    if outcome.startswith("failed"):
        return fail(sv, run_id, outcome)
    finish(sv, run_id, outcome)


def finish(sv: Services, run_id: int, outcome: str) -> None:
    """Clips that passed their checks are approved and planned on their category's channel, unless the video fits the
    channel too loosely: those wait in the review queue for you. A clip with no channel for its category is held."""
    run = get(sv.db, run_id)
    plan = run["plan"]
    with sv.db.conn() as c:
        ready = [dict(r) for r in c.execute(
            "SELECT id FROM clips_ready WHERE profile_id=? AND video_id=? AND state='review' ORDER BY engine_score DESC, id",
            (run["profile_id"], run["video_id"]))]
    approved = held = 0
    for clip in ready:
        if not plan["fit_ok"]:
            continue  # stays in the review queue, with the reason on the plan
        clipper.decide(sv.db, run["profile_id"], clip["id"], "approve")
        post = routing.assign_and_plan(sv.db, lambda: sv.jev, sv.profiles, run["profile_id"], clip["id"])
        if post is None:
            held += 1
        else:
            approved += 1
            if post["status"] == "queued":
                tasks.enqueue(sv.db, "copy", {"profile": post["profile"], "post_id": post["id"]}, dedupe_key=f"copy:{post['id']}")
    waiting = len(ready) if not plan["fit_ok"] else 0
    parts = [outcome + "."]
    if approved:
        parts.append(f"{approved} planned for posting.")
    if held:
        parts.append(f"{held} held: there is no {plan['category']} channel yet.")
    if waiting:
        parts.append(f"{waiting} waiting for your review: the video fits the channel only {plan['fit']} of 5.")
    _set(sv.db, run_id, state="finished", note=" ".join(parts))
    store.event(sv.db, "info", "run", f"run {run_id}: {' '.join(parts)}")


# ---- what the pages show -------------------------------------------------------------------------------------------


def recent(db, limit: int = 8) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT * FROM runs WHERE state!='removed' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def clips_of(db, run: dict) -> list[dict]:
    """The clips a run made, with where each one stands (held, waiting for review, planned, posted ...)."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT cr.id, cr.engine_score, cr.hook, cr.state, cr.reason, cr.start_s, cr.end_s, "
            "(SELECT group_concat(p.platform || ':' || p.status) FROM posts p WHERE p.clip_id=cr.id) AS posts "
            "FROM clips_ready cr WHERE cr.profile_id=? AND cr.video_id=? AND cr.state!='superseded' ORDER BY cr.id",
            (run["profile_id"], run["video_id"])).fetchall()
    return [dict(r) for r in rows]
