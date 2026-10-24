"""The worker: runs queued tasks one at a time, and keeps the schedules ticking."""

from __future__ import annotations

import json
import threading
import traceback
from datetime import timedelta

from . import discovery, engine, learning, store, tasks
from .services import Services


def _profile(sv: Services, payload: dict):
    return sv.profiles[payload["profile"]]


def do_watch(sv: Services, payload: dict) -> None:
    discovery.discover(sv.db, sv.yt, _profile(sv, payload), topics=False)


def do_topics(sv: Services, payload: dict) -> None:
    p = _profile(sv, payload)
    discovery.search_topics(sv.db, sv.yt, p)
    discovery.apply_filters(sv.db, p)


def do_snapshots(sv: Services, payload: dict) -> None:
    p = _profile(sv, payload)
    discovery.refresh_stats(sv.db, sv.yt, p)
    engine.rescore(sv.db, p)


def do_grade(sv: Services, payload: dict) -> None:
    p = _profile(sv, payload)
    discovery.apply_filters(sv.db, p)
    engine.grade_new(sv.db, sv.jev, sv.app, p)
    engine.deepen(sv.db, sv.jev, sv.app, p)
    engine.second_opinions(sv.db, sv.app, p)
    engine.rescore(sv.db, p)
    queue_clips(sv, p)


def do_clip(sv: Services, payload: dict) -> None:
    """Hand one video to the clipping engine (rights are checked there), then look at it later."""
    from . import clipper, tasks

    p = _profile(sv, payload)
    status = clipper.submit(sv.db, clipper.Engine(), sv.yt, p, payload["video_id"])
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

    formatter = formats.formatter_for(sv.db, p, jev=sv.jev, budget_usd=sv.app.budgets.claude_usd_per_month)
    outcome = clipper.collect(sv.db, clipper.Engine(), sv.jev, p, dict(job), formatter)
    store.event(sv.db, "info", "clip", f"{payload['video_id']}: {outcome}")
    if outcome == "waiting":
        tasks.enqueue(sv.db, "collect", payload,
                      dedupe_key=f"collect:{p.id}:{payload['video_id']}:{store.now():%Y%m%d%H%M}",
                      run_after=store.now() + timedelta(minutes=5))


def queue_clips(sv: Services, profile) -> int:
    """Queue a clip task for each video that passed the gate. Dedupe means each video is queued once."""
    from . import tasks

    with sv.db.conn() as c:
        ready = [r["video_id"] for r in c.execute(
            "SELECT video_id FROM scores WHERE profile_id=? AND gate='pass'", (profile.id,))]
    return sum(
        1 for v in ready
        if tasks.enqueue(sv.db, "clip", {"profile": profile.id, "video_id": v},
                         dedupe_key=f"clip:{profile.id}:{v}") is not None
    )


def do_review(sv: Services, payload: dict) -> None:
    """The weekly review: Claude reads the week's numbers and suggests ideas. Nothing is applied automatically."""
    from . import claude

    p = _profile(sv, payload)
    if sv.app.budgets.claude_usd_per_month <= store.ai_spend_month(sv.db, "claude"):
        store.event(sv.db, "warn", "review", "Weekly review skipped: monthly Claude budget used up")
        return
    check = store.last_selfcheck(sv.db, p.id)
    rows = store.ranked(sv.db, p.id, limit=10)
    facts = {
        "channel": p.display_name,
        "niche": p.niche.definition,
        "self_check": check and {k: check[k] for k in ("verdict", "sample", "rank_correlation", "top_hit_rate")},
        "calibration": learning.calibration(sv.db, p.id),
        "videos_by_stage": store.counts_by_state(sv.db, p.id),
        "top_graded": [{"title": r["title"], "grade": r["score"], "gate": r["gate"]} for r in rows],
    }
    review, cost = claude.weekly_review(claude._client(), facts)
    with sv.db.conn() as c:
        c.execute("INSERT INTO suggestions (profile_id, created_at, body_json) VALUES (?,?,?)",
                  (p.id, store.iso(), json.dumps(review)))
        store.add_ai_usage(c, "claude", 0, 0, cost)
    store.event(sv.db, "info", "review", "Weekly review written")


def do_copy(sv: Services, payload: dict) -> None:
    """Write the title and caption options for one planned post."""
    from . import copywriter

    p = _profile(sv, payload)
    status = copywriter.generate_with_keys(sv.db, p, payload["post_id"], jev=sv.jev,
                                           budget_usd=sv.app.budgets.claude_usd_per_month)
    store.event(sv.db, "info", "copy", f"post {payload['post_id']}: {status}")


def do_post(sv: Services, payload: dict) -> None:
    """Send the channel's posts that are due. Dry-run channels only write the payload to the outbox."""
    from . import posting

    p = _profile(sv, payload)
    if p.posting is None:
        return
    adapters = {} if p.posting.dry_run else posting.default_adapters(p)
    for line in posting.run_due(sv.db, p, adapters=adapters):
        store.event(sv.db, "info", "post", line)


HANDLERS = {
    "copy": do_copy,
    "post": do_post,
    "watch": do_watch,
    "topics": do_topics,
    "snapshots": do_snapshots,
    "grade": do_grade,
    "review": do_review,
    "clip": do_clip,
    "collect": do_collect,
}


def run_one(sv: Services, worker: str = "worker-1") -> bool:
    """Run the next ready task. Returns False when there was nothing to do."""
    task = tasks.claim(sv.db, worker)
    if not task:
        return False
    try:
        HANDLERS[task["kind"]](sv, json.loads(task["payload_json"]))
    except Exception as e:  # a bad task must not stop the worker; it is retried, then given up on
        outcome = tasks.fail(sv.db, task, f"{type(e).__name__}: {e}")
        store.event(sv.db, "error", "runner", f"{task['kind']} failed ({outcome})", error=str(e),
                    trace=traceback.format_exc()[-800:])
    else:
        tasks.complete(sv.db, task["id"])
    return True


def loop(sv: Services, stop: threading.Event, *, idle_seconds: float = 5.0) -> None:
    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    store.event(sv.db, "info", "runner", "Worker started")
    while not stop.is_set():
        tasks.tick_schedules(sv.db)
        if not run_one(sv):
            stop.wait(idle_seconds)
    store.event(sv.db, "info", "runner", "Worker stopped")
