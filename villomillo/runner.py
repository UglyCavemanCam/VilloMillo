"""The worker: runs queued tasks one at a time, and keeps the schedules ticking."""

from __future__ import annotations

import json
import threading
import traceback

from . import discovery, engine, store, tasks
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
    engine.rescore(sv.db, p)


HANDLERS = {"watch": do_watch, "topics": do_topics, "snapshots": do_snapshots, "grade": do_grade}


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
