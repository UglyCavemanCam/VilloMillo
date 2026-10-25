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
import re
from dataclasses import asdict
from datetime import timedelta

from . import (
    activity,
    campaigns,
    channels,
    claims,
    clipper,
    links,
    research,
    routing,
    scoring,
    store,
    studio,
    tasks,
    timefmt,
)
from .services import Services

DAMAGED = re.compile(r"decode failed|Invalid data found|moov atom not found|InvalidDataError|Error submitting packet", re.IGNORECASE)
HEALED = "The downloaded file was damaged"  # the start of the line written when it is deleted and fetched again
POLL_S = 20  # how often a waiting engine job is looked at
ENGINE_WAIT_S = 1800  # a video waits this long, with no progress, for an engine that is not answering before it is marked failed
ACTIVE = ("analysing", "planned", "making")


class RunError(ValueError):
    """Something about this run is wrong. The message is safe to show."""


class Claimed(RunError):
    """An automatic way in wanted a video that another run has. `why` ends the sentence "Skipped: ..."; `run` is the run that has it."""

    def __init__(self, run: dict, why: str):
        super().__init__(why)
        self.run, self.why = run, why


class AlreadyClipped(RunError):
    """You pasted a video that was already clipped. Paste it again with redo=True to clip it again."""

    def __init__(self, run: dict, why: str):
        super().__init__(why)
        self.run, self.why = run, why


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


def note(sv: Services, run: dict, message: str, step: str, level: str = "info") -> None:
    """One line of this video's timeline (paste, Research and Campaign videos all write the same way)."""
    activity.log(sv.db, run["source"], message, run_id=run["id"], step=step, level=level)


def _engine_down(sv: Services, run_id: int, kind: str, step: str, error: Exception) -> None:
    """The engine is not answering. Wait for it (it is started again automatically) rather than failing the video at once."""
    run = _run(sv.db, run_id)
    waited = activity.age_s(run["updated_at"]) or 0
    if waited > ENGINE_WAIT_S:
        return fail(sv, run_id, f"the clip engine was not answering for {ENGINE_WAIT_S // 60} minutes: {error}")
    note(sv, run, "Waiting: the clip engine is not answering. This video carries on when it is back.", step, "warn")
    _later(sv, kind, run_id)


def _healed(sv: Services, run: dict) -> bool:
    """Whether this video's damaged download was already deleted once: a second failure is not fixed by downloading again."""
    return any(x["message"].startswith(HEALED) for x in activity.for_run(sv.db, run["id"]))


def _stage_line(engine, job_id) -> str:
    """What the engine says it is doing for this job, in plain words (read from the wording its log reliably prints)."""
    log = engine.log(job_id) if hasattr(engine, "log") else None
    if not log:
        return STAGES_WAITING
    progress = studio.read_log(log)
    line = activity.STAGES[progress["stage"]]
    if progress["candidates"]:
        line += f" ({progress['candidates']} candidate moments so far)"
    return line


STAGES_WAITING = activity.STAGES[0]


def _later(sv: Services, kind: str, run_id: int, seconds: float = POLL_S) -> None:
    now = store.now()
    tasks.enqueue(sv.db, kind, {"run": run_id}, dedupe_key=f"{kind}:{run_id}:{int(now.timestamp() // max(seconds, 1))}",
                  run_after=now + timedelta(seconds=seconds))


def create(sv: Services, owner_id: str, text: str, *, source: str = "paste", campaign_id: str | None = None, redo: bool = False) -> int:
    """Start a run for a link, if nobody else has the video (one video, one owner: see claims.py).

    Raises links.LinkError for a link that is not a video we can clip. A pasted link for a video that is being worked on returns that run;
    for one that was already clipped it raises AlreadyClipped unless `redo`. Research and Campaigns raise Claimed instead of taking a video
    that another run owns. The check and the claim are one database step, so two ways in cannot both start the same video."""
    link = links.parse(text)
    held = None
    with sv.db.conn(immediate=True) as c:
        live = c.execute(f"SELECT * FROM runs WHERE video_id=? AND state IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC", (link.video_id, *ACTIVE)).fetchone()
        if live and source == "paste":
            return live["id"]
        if live:
            held = dict(live)
        else:
            marks = ",".join("?" * len(claims.OWNING_FOR_AUTOMATIC))
            done = c.execute(f"SELECT * FROM runs WHERE video_id=? AND state IN ({marks}) ORDER BY id DESC LIMIT 1", (link.video_id, *claims.OWNING_FOR_AUTOMATIC)).fetchone()
            if done and (source != "paste" or (done["state"] == "finished" and not redo)):
                held = dict(done)
        if held is None:
            c.execute("INSERT INTO runs (source, url, platform, video_id, profile_id, state, created_at, updated_at, campaign_id) VALUES (?,?,?,?,?,?,?,?,?)",
                      (source, link.url, link.platform, link.video_id, owner_id, "analysing", store.iso(), store.iso(), campaign_id))
            run_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    if held is not None:  # raised once the step has ended: describing the owner reads other tables
        why = claims.phrase(sv.db, held)
        raise AlreadyClipped(held, why[0].upper() + why[1:]) if source == "paste" else Claimed(held, why)
    tasks.enqueue(sv.db, "paste_analyse", {"run": run_id}, dedupe_key=f"paste_analyse:{run_id}:first")
    note(sv, {"id": run_id, "source": source}, f"Link accepted ({link.name}). Waiting for the engine to start on it.", "look")
    return run_id


def fail(sv: Services, run_id: int, message: str) -> None:
    run = _run(sv.db, run_id)
    note(sv, run, f"Failed: {message[:200]}", "look" if run["state"] == "analysing" else "make", "bad")
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
    try:
        if run["preview_job_id"] is None:
            job_id = clipper.start_preview(engine, link, focus="", force=_healed(sv, run))
            _set(sv.db, run_id, preview_job_id=job_id)
            note(sv, run, f"Asked the engine to look at the video (its job {job_id}).", "look")
            return _later(sv, "paste_analyse", run_id)
        job = engine.job(run["preview_job_id"])
        if job["status"] in ("queued", "running"):
            line = "Waiting in the engine's queue behind other work" if job["status"] == "queued" else _stage_line(engine, run["preview_job_id"])
            note(sv, run, line, "look")
            return _later(sv, "paste_analyse", run_id)
        if job["status"] != "done":
            if job["status"] == "failed" and DAMAGED.search(job.get("error") or "") and not _healed(sv, run) and engine.discard_download(link.video_id):
                note(sv, run, f"{HEALED} (the engine could not read it), so it was deleted and is being downloaded again.", "look", "warn")
                _set(sv.db, run_id, preview_job_id=None)
                return _later(sv, "paste_analyse", run_id, 2)
            return fail(sv, run_id, f"the engine could not look at this video: {job.get('error') or job['status']}")
        preview = engine.preview(link.video_id)
    except clipper.EngineUnavailable as e:
        return _engine_down(sv, run_id, "paste_analyse", "look", e)
    video = preview.get("video") or {}
    _set(sv.db, run_id, title=video.get("title") or run["title"], channel=video.get("channel"), duration=video.get("duration"))
    if not preview["moments"]:
        note(sv, run, "The engine found no moment worth clipping in this video.", "look")
        return _set(sv.db, run_id, state="nothing", note="No moments worth clipping were found in this video.", plan_json=None)
    note(sv, run, f"The engine found {len(preview['moments'])} possible clips.", "look")
    note(sv, run, f"Scoring the {len(preview['moments'])} moments: Jev reads each clip, then the video's kind and channel fit are worked out.", "score")
    plan = make_plan(sv, run_id, preview)
    state = "planned" if plan["make_ids"] else "nothing"
    why = None if plan["make_ids"] else "No clip here is likely to earn enough."
    _set(sv.db, run_id, state=state, note=why, plan_json=json.dumps(plan), profile_id=plan["owner"])
    where = f"goes to the {plan['channel']['name']} channel" if plan["channel"] else f"has no {plan['category']} channel yet"
    note(sv, run, f"Scored: {len(plan['make_ids'])} of {len(plan['moments'])} clips clear the floors. It is {plan['kind']['label']} "
                  f"({round(plan['kind']['certainty'] * 100)}% sure) and {where}.", "score")
    note(sv, run, (f"Plan ready: {len(plan['make_ids'])} clips would be made." if state == "planned" else "Plan: nothing here is worth making."), "plan")
    if state == "planned" and run["source"] != "paste":
        _go_on_its_own(sv, run_id, run, engine)
    elif state == "planned":
        note(sv, run, "Waiting for you to press Start.", "plan")


def _go_on_its_own(sv: Services, run_id: int, run: dict, engine: clipper.Engine) -> None:
    """A video that came from a campaign or from Research needs no Start: it goes ahead if the campaign can still be clipped for,
    or if Research is still on and the video has a channel it fits."""
    if run["source"] == "research":
        plan = get(sv.db, run_id)["plan"]
        research.note_category(sv.db, run_id, plan["category"])
        ok, why = research.gate(sv, plan)
        if not ok:
            note(sv, run, why, "plan", "warn")
            return _set(sv.db, run_id, state="nothing", note=why)
    if run["campaign_id"]:
        c = campaigns.get(sv.db, run["campaign_id"])
        ok, why = campaigns.clippable(c, sv.app.scoring) if c and c["state"] == "joined" else (False, "the campaign is no longer being clipped for")
        if not ok:
            note(sv, run, why, "plan", "warn")
            return _set(sv.db, run_id, state="nothing", note=why)
    note(sv, run, "Going ahead without a Start press: this video came from " + ("a campaign." if run["campaign_id"] else "Research."), "plan")
    start(sv, run_id, engine)


def make_plan(sv: Services, run_id: int, preview: dict) -> dict:
    """Score the preview and decide where its clips would go. Everything the plan screen shows is in the result."""
    run = _run(sv.db, run_id)
    rules, breaker = sv.app.scoring, sv.app.spend_breaker
    campaign = campaigns.get(sv.db, run["campaign_id"]) if run["campaign_id"] else None
    pays = campaign["rate_cents"] / 100 if campaign and campaign["rate_cents"] else None
    bar = research.bar(sv.db, rules, sv.app.research) if run["source"] == "research" else None
    result = scoring.score_video(sv.db, sv.jev, rules, preview, source=run["source"], campaign_cpm=pays, research_bar=bar, breaker=breaker)
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
    note(sv, run, f"Asked the engine to make {len(plan['make_ids'])} clips (its job {job_id}).", "make")
    _later(sv, "paste_collect", run_id)


def remove(sv: Services, run_id: int, engine: clipper.Engine | None = None) -> None:
    """Remove video: the run is closed and what was analysed is thrown away."""
    run = _run(sv.db, run_id)
    if run["state"] not in ("planned", "nothing", "failed"):
        raise RunError(f"this video is {run['state']} and cannot be removed now")
    (engine or clipper.Engine()).discard(run["video_id"])
    note(sv, run, "Removed by you.", "plan")
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
    campaign = campaigns.get(sv.db, run["campaign_id"]) if run["campaign_id"] else None
    owner = sv.profiles[run["profile_id"]]  # the campaign's own rules apply to its clips; a pasted link has none
    with sv.db.conn() as c:
        job = c.execute("SELECT * FROM clip_jobs WHERE profile_id=? AND video_id=?", (owner.id, run["video_id"])).fetchone()
    if job is None:
        return fail(sv, run_id, "the clip job went missing")
    formatter = formats.formatter_for(sv.db, owner, jev=sv.jev, breaker=sv.app.spend_breaker, recipe=plan["look"])
    try:
        outcome = clipper.collect(sv.db, engine, sv.jev, owner, dict(job), formatter, category=plan["category"],
                                  campaign_id=run["campaign_id"], campaign_rules=campaigns.rules_for(campaign),
                                  note=lambda message, level="info": note(sv, run, message, "check", level))
    except clipper.EngineUnavailable as e:
        return _engine_down(sv, run_id, "paste_collect", "make", e)
    if outcome == "waiting":
        note(sv, run, _stage_line(engine, job["engine_job_id"]) if job["engine_job_id"] else "Waiting for the engine", "make")
        return _later(sv, "paste_collect", run_id)
    if outcome.startswith("failed"):
        return fail(sv, run_id, outcome)
    note(sv, run, f"The engine finished: {outcome}.", "check")
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
            note(sv, run, f"Clip {clip['id']} approved but held: there is no {plan['category']} channel yet.", "post", "warn")
        else:
            approved += 1
            for each in post["posts"]:
                channel = sv.profiles[each["profile"]].display_name
                note(sv, run, f"Clip {clip['id']} planned on {channel}, due {timefmt.full(each['scheduled_at'])} ({each['status']}).", "post")
                if each["status"] == "queued":
                    tasks.enqueue(sv.db, "copy", {"profile": each["profile"], "post_id": each["id"]}, dedupe_key=f"copy:{each['id']}")
    waiting = len(ready) if not plan["fit_ok"] else 0
    parts = [outcome + "."]
    if approved:
        parts.append(f"{approved} planned for posting.")
    if held:
        parts.append(f"{held} held: there is no {plan['category']} channel yet.")
    if waiting:
        parts.append(f"{waiting} waiting for your review: the video fits the channel only {plan['fit']} of 5.")
    note(sv, run, "Done. " + " ".join(parts[1:]) if len(parts) > 1 else "Done.", "post")
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
            "FROM clips_ready cr WHERE cr.profile_id=? AND cr.video_id=? AND cr.state NOT IN ('superseded','removed') ORDER BY cr.id",
            (run["profile_id"], run["video_id"])).fetchall()
    return [dict(r) for r in rows]
