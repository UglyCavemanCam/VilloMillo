"""The worker: runs queued tasks one at a time, and keeps the schedules ticking."""

from __future__ import annotations

import json
import threading
import traceback
from datetime import timedelta

from . import health, runs, safety, spend, store, tasks
from .services import Services


def _profile(sv: Services, payload: dict):
    return sv.profiles[payload["profile"]]


def do_clip(sv: Services, payload: dict) -> None:
    """Hand one video to the clipping engine, then look at it later."""
    from . import clipper, tasks

    p = _profile(sv, payload)
    status = clipper.submit(sv.db, clipper.Engine(), p, payload["video_id"])
    store.event(sv.db, "info", "clip", f"{payload['video_id']}: {status}")
    if status.startswith("submitted"):
        tasks.enqueue(sv.db, "collect", {"profile": p.id, "video_id": payload["video_id"]},
                      dedupe_key=f"collect:{p.id}:{payload['video_id']}:{store.now():%Y%m%d%H}",
                      run_after=store.now() + timedelta(minutes=10))


def do_collect(sv: Services, payload: dict) -> None:
    """Check on a clip job. The engine takes a while, so this asks again later until it is done."""
    from . import clipper, tasks

    p = _profile(sv, payload)
    with sv.db.conn() as c:
        job = c.execute("SELECT * FROM clip_jobs WHERE profile_id=? AND video_id=? AND status='submitted'",
                        (p.id, payload["video_id"])).fetchone()
    if job is None:
        return
    from . import formats

    formatter = formats.formatter_for(sv.db, p, jev=sv.jev, breaker=sv.app.spend_breaker)
    outcome = clipper.collect(sv.db, clipper.Engine(), sv.jev, p, dict(job), formatter)
    store.event(sv.db, "info", "clip", f"{payload['video_id']}: {outcome}")
    if outcome == "waiting":
        tasks.enqueue(sv.db, "collect", payload,
                      dedupe_key=f"collect:{p.id}:{payload['video_id']}:{store.now():%Y%m%d%H%M}",
                      run_after=store.now() + timedelta(minutes=5))


def do_copy(sv: Services, payload: dict) -> None:
    """Write the title and caption options for one planned post."""
    from . import copywriter

    p = _profile(sv, payload)
    try:
        status = copywriter.generate_with_keys(sv.db, p, payload["post_id"], jev=sv.jev,
                                               breaker=sv.app.spend_breaker)
    except LookupError:  # the post was deleted before its titles were written
        status = "skipped: the post was deleted"
    store.event(sv.db, "info", "copy", f"post {payload['post_id']}: {status}")


def do_post(sv: Services, payload: dict) -> None:
    """Send the channel's posts that are due. Dry-run channels only write the payload to the outbox."""
    from . import activity, posting, removal

    removal.purge_old(sv.db)  # files of clips removed more than 30 days ago
    activity.prune(sv.db)
    p = _profile(sv, payload)
    if p.posting is None:
        return
    adapters = {} if p.posting.dry_run else posting.default_adapters(p)
    for line in posting.run_due(sv.db, p, adapters=adapters, budget_s=600):  # under the task's 15 minute lease
        store.event(sv.db, "info", "post", line)


def do_send_post(sv: Services, payload: dict) -> None:
    """Send one post right now: the Send now button."""
    from . import copywriter, posting

    p = _profile(sv, payload)
    with sv.db.conn() as c:
        written = c.execute("SELECT 1 FROM copy_options WHERE post_id=?", (payload["post_id"],)).fetchone()
    if not written:  # the clip was approved a moment ago: write its titles first, as the copy task would
        try:
            store.event(sv.db, "info", "copy", f"post {payload['post_id']}: " + copywriter.generate_with_keys(
                sv.db, p, payload["post_id"], jev=sv.jev, breaker=sv.app.spend_breaker))
        except Exception as e:  # no keys, or Claude is unreachable: the basic title and caption go out instead
            store.event(sv.db, "warn", "copy", f"post {payload['post_id']}: sent with the basic copy, {type(e).__name__}: {str(e)[:160]}")
    try:
        line = posting.send_post_now(sv.db, p, payload["post_id"])
    except (ValueError, LookupError) as e:  # not allowed any more, or already out: nothing to retry
        line = f"post {payload['post_id']}: not sent, {e}"
    store.event(sv.db, "info", "post", line)


def do_stats(sv: Services, payload: dict) -> None:
    """Read the real numbers of this channel's recent posts."""
    from . import posting

    p = _profile(sv, payload)
    if p.posting is None:
        return
    for line in posting.collect_stats(sv.db, p, yt=sv.yt):
        store.event(sv.db, "info", "stats", line)


def do_campaigns(sv: Services, payload: dict) -> None:
    """Read Content Rewards, keep each joined campaign's queue full, and start the next video."""
    from . import campaigns

    campaigns.work(sv)  # writes what it does to the activity log


def do_research(sv: Services, payload: dict) -> None:
    """When Research is on, look for videos worth clipping and start the next one."""
    from . import research

    research.work(sv)  # writes what it does to the activity log


def do_usage(sv: Services, payload: dict) -> None:
    """Count what the engine's Claude calls used, so the spending limit sees them."""
    from . import engine_ai

    for line in engine_ai.poll_usage(sv):
        store.event(sv.db, "info", "spend", line)


def _paste_step(step, kind: str):
    """A step of a pasted link's run. If spending hits the breaker part-way, look again in a minute without counting a failure."""

    def handler(sv: Services, payload: dict) -> None:
        try:
            step(sv, payload["run"])
        except spend.SpendPaused:
            runs.note(sv, runs._run(sv.db, payload["run"]), "Paused: spending reached its limit. This video carries on after you approve more.",
                      "score" if kind == "paste_analyse" else "check", "warn")
            runs._later(sv, kind, payload["run"], 60)

    return handler


def _give_up_on_run(sv: Services, payload: dict, error: str) -> None:
    runs.fail(sv, payload["run"], error)


HANDLERS = {
    "paste_analyse": _paste_step(runs.analyse, "paste_analyse"),
    "paste_collect": _paste_step(runs.collect, "paste_collect"),
    "copy": do_copy,
    "post": do_post,
    "send_post": do_send_post,
    "stats": do_stats,
    "campaigns": do_campaigns,
    "research": do_research,
    "usage": do_usage,
    "clip": do_clip,
    "collect": do_collect,
}


def run_one(sv: Services, worker: str = "worker-1") -> bool:
    """Run the next ready task. Returns False when there was nothing to do."""
    if safety.is_stopped(sv.db):
        return False  # the emergency stop is on: nothing runs until you resume
    if spend.current(sv.db, sv.app.spend_breaker):
        return False  # the spend breaker is tripped or an account is out of funds: wait for your approval
    task = tasks.claim(sv.db, worker)
    if not task:
        return False
    health.beat(sv.db, task["kind"])
    try:
        HANDLERS[task["kind"]](sv, json.loads(task["payload_json"]))
    except Exception as e:  # a bad task must not stop the worker; it is retried, then given up on
        provider = spend.engine_of(e)
        if provider and spend.note_failure(sv.db, provider, e):
            store.event(sv.db, "error", "spend", f"{spend.NAMES[provider]} is out of funds: work is paused", error=str(e)[:300])
        outcome = tasks.fail(sv.db, task, f"{type(e).__name__}: {e}")
        if outcome == "failed" and task["kind"] in ("paste_analyse", "paste_collect"):
            _give_up_on_run(sv, json.loads(task["payload_json"]), f"{type(e).__name__}: {e}")
        store.event(sv.db, "error", "runner", f"{task['kind']} failed ({outcome})", error=str(e),
                    trace=traceback.format_exc()[-800:])
    else:
        tasks.complete(sv.db, task["id"])
    finally:
        health.beat(sv.db)
    return True


def loop(sv: Services, stop: threading.Event, *, idle_seconds: float = 5.0) -> None:
    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    store.event(sv.db, "info", "runner", "Worker started")
    while not stop.is_set():
        health.beat(sv.db)
        tasks.tick_schedules(sv.db)
        if not run_one(sv):
            stop.wait(idle_seconds)
    store.event(sv.db, "info", "runner", "Worker stopped")
