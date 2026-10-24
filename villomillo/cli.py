"""Command line: python -m villomillo <status | once | serve | run>."""

from __future__ import annotations

import argparse
import json
import sys
import threading

from . import engine, runner, services, store


def _services(with_apis: bool = True) -> services.Services:
    try:
        return services.build(with_apis=with_apis)
    except Exception as e:  # a missing key or a bad settings file should be a plain message, not a traceback
        sys.exit(f"Cannot start: {e}")


def cmd_status(args) -> None:
    sv = _services(with_apis=False)
    for p in sv.profiles.values():
        rows = store.ranked(sv.db, p.id, limit=500)
        print(f"{p.display_name} ({p.id})")
        print("  videos by stage:", store.counts_by_state(sv.db, p.id) or "none yet")
        print("  gate:", {g: sum(1 for r in rows if r["gate"] == g) for g in ("pass", "escalate", "fail")})
        print("  top-20 ratings:", store.precision_at(rows, 20))
    print("YouTube used today: general", sv.quota.used("general"), "of", sv.quota.caps["general"],
          "| search", sv.quota.used("search"), "of", sv.quota.caps["search"])
    print(f"Jev spend today: ${store.ai_spend_today(sv.db, 'jev'):.4f} of ${sv.app.budgets.jev_usd_per_day:.2f}")


def cmd_once(args) -> None:
    sv = _services()
    for p in sv.profiles.values():
        if args.profile and p.id != args.profile:
            continue
        summary = engine.run_cycle(sv.db, sv.yt, sv.jev, sv.app, p, topics=not args.no_topics, limit=args.limit)
        print(json.dumps(summary, indent=2, default=str))


def _serve(sv: services.Services) -> None:
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(sv), host=sv.app.dashboard.host, port=sv.app.dashboard.port, log_level="warning")


def cmd_serve(args) -> None:
    sv = _services(with_apis=False)
    print(f"Dashboard: http://{sv.app.dashboard.host}:{sv.app.dashboard.port}  (Ctrl+C to stop)")
    _serve(sv)


def cmd_run(args) -> None:
    sv = _services()
    stop = threading.Event()
    worker = threading.Thread(target=runner.loop, args=(sv, stop), name="worker", daemon=True)
    worker.start()
    print(f"Working in the background. Dashboard: http://{sv.app.dashboard.host}:{sv.app.dashboard.port}  (Ctrl+C to stop)")
    try:
        _serve(sv)
    finally:
        stop.set()
        worker.join(timeout=10)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="villomillo")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show counts and today's usage").set_defaults(fn=cmd_status)
    once = sub.add_parser("once", help="run one research cycle right now")
    once.add_argument("--profile")
    once.add_argument("--no-topics", action="store_true", help="skip keyword searches (they use scarce allowance)")
    once.add_argument("--limit", type=int, help="grade at most this many videos")
    once.set_defaults(fn=cmd_once)
    sub.add_parser("serve", help="open the dashboard only").set_defaults(fn=cmd_serve)
    sub.add_parser("run", help="work in the background and serve the dashboard").set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    args.fn(args)
