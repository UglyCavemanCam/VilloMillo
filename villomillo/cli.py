"""Command line: python -m villomillo <status | once | serve | run>."""

from __future__ import annotations

import argparse
import json
import pathlib
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
        check = store.last_selfcheck(sv.db, p.id)
        print("  last self-check:", check["verdict"] if check else "not run yet (python -m villomillo check)")
    print("YouTube used today: general", sv.quota.used("general"), "of", sv.quota.caps["general"],
          "| search", sv.quota.used("search"), "of", sv.quota.caps["search"])
    print(f"Jev spend today: ${store.ai_spend_today(sv.db, 'jev'):.4f} of ${sv.app.budgets.jev_usd_per_day:.2f}")


def cmd_check(args) -> None:
    from . import selfcheck

    sv = _services()
    for p in sv.profiles.values():
        if args.profile and p.id != args.profile:
            continue
        result = selfcheck.run(sv.db, sv.yt, sv.jev, p, sample=args.sample)
        print(f"{p.display_name}: {result['verdict']}")
        print(f"  graded {result['sample']} older uploads without using their views")
        if "rank_correlation" in result:
            print(f"  rank correlation with views: {result['rank_correlation']:+.2f}  (0 = no link, 1 = perfect)")
            print(f"  top third by grade that beat the median: {result['top_hit_rate']:.0%}  (50% = chance)")


def _pick(sv: services.Services, profile_id: str | None):
    """The channel profile a command works on. With more than one profile, it must be named."""
    if profile_id:
        if profile_id not in sv.profiles:
            sys.exit(f"No channel profile called {profile_id!r}. Known: {', '.join(sorted(sv.profiles))}")
        return sv.profiles[profile_id]
    if len(sv.profiles) == 1:
        return next(iter(sv.profiles.values()))
    sys.exit(f"Choose a channel profile with --profile ({', '.join(sorted(sv.profiles))})")


def cmd_rights(args) -> None:
    from . import rights

    sv = _services(with_apis=False)
    p = _pick(sv, args.profile)
    if args.action == "set-channel":
        if not (args.channel_id and args.status):
            sys.exit("usage: rights set-channel <channel_id> <owned|licensed|none> --note '...'")
        try:
            rights.set_channel(sv.db, p.id, args.channel_id, args.status, args.note or "")
        except ValueError as e:
            sys.exit(str(e))
        print(f"{args.channel_id} is now recorded as '{args.status}' for {p.id}")
    else:
        with sv.db.conn() as c:
            for r in c.execute("SELECT channel_id, status, note FROM channel_rights WHERE profile_id=?", (p.id,)):
                print(f"channel {r['channel_id']}  {r['status']}  {r['note']}")
            n = c.execute("SELECT COUNT(*) AS n FROM campaign_videos WHERE profile_id=?", (p.id,)).fetchone()["n"]
            print(f"{n} videos listed by campaigns for {p.id}")


def cmd_campaign(args) -> None:
    """Record the source videos a campaign lists, and load them into the system."""
    from . import rights

    sv = _services()
    p = _pick(sv, args.profile)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    if not ids:
        sys.exit("give the video ids with --ids a,b,c")
    try:
        added = rights.add_campaign_videos(sv.db, p.id, ids, args.url, args.note)
    except ValueError as e:
        sys.exit(str(e))
    found = sv.yt.videos(ids)
    with sv.db.conn() as c:
        for v in found:
            if v["published_at"]:
                store.upsert_video(c, p.id, v, "campaign")
    missing = sorted(set(ids) - {v["video_id"] for v in found})
    print(f"{added} new videos recorded as listed by the campaign; {len(found)} loaded from YouTube")
    if missing:
        print("not available on YouTube (skipped when clipping):", ", ".join(missing))


def cmd_youtube_connect(args) -> None:
    """Sign in to YouTube once for a channel profile, so approved clips can be uploaded."""
    from . import posting

    sv = _services(with_apis=False)
    p = _pick(sv, args.profile)
    if p.posting is None:
        sys.exit(f"{p.id} has no posting settings")
    print("A browser window opens. Sign in with the Google account that owns the channel you will post to.")
    print("Google may warn that the app is unverified: that is expected for your own app. Choose Continue.")
    try:
        channel = posting.connect_youtube(p)
    except posting.PostError as e:
        sys.exit(str(e))
    print(f"Connected to {channel['title']} {channel['handle']} (channel id {channel['id']}).")
    print("If that is not the channel you meant, run this again and pick the other account.")


def cmd_post_now(args) -> None:
    """Send one planned post through its platform's API right now (the explicit go for a single post)."""
    from . import posting

    sv = _services(with_apis=False)
    p = _pick(sv, args.profile)
    with sv.db.conn() as c:
        row = c.execute("SELECT platform FROM posts WHERE profile_id=? AND id=?", (p.id, args.post_id)).fetchone()
    if row is None or p.posting is None:
        sys.exit(f"no post {args.post_id} for {p.id}")
    forced = p.model_copy(update={"posting": p.posting.model_copy(
        update={"platforms": {**p.posting.platforms, row["platform"]: "api"}})})
    print(f"Sending post {args.post_id} to {row['platform']} with privacy '{p.posting.privacy}'...")
    try:
        print(posting.send_now(sv.db, forced, args.post_id, posting.default_adapters(forced)))
    except (LookupError, ValueError) as e:
        sys.exit(str(e))


def cmd_copy(args) -> None:
    """Write title and caption options for planned posts that have none yet (or one post, again)."""
    from . import copywriter

    sv = _services(with_apis=True)
    p = _pick(sv, args.profile)
    if p.posting is None:
        sys.exit(f"{p.id} has no posting settings")
    with sv.db.conn() as c:
        if args.post_id:
            ids = [args.post_id]
        else:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM posts WHERE profile_id=? AND status IN ('queued','manual_ready','dry_run') AND id NOT IN "
                "(SELECT post_id FROM copy_options) ORDER BY id", (p.id,))]
    for post_id in ids:
        try:
            print(f"post {post_id}: " + copywriter.generate_with_keys(
                sv.db, p, post_id, jev=sv.jev, force=args.force, budget_usd=sv.app.budgets.claude_usd_per_month))
        except LookupError as e:
            sys.exit(str(e))


def cmd_format_clip(args) -> None:
    """Render a clip we already hold in a recipe, to a preview file. Nothing in the review queue changes."""
    from . import claude, formats
    from .config import ROOT

    sv = _services()
    p = _pick(sv, args.profile)
    if args.recipe:
        p = p.model_copy(update={"format": args.recipe})
    if not p.format:
        sys.exit(f"{p.id} has no format recipe; name one with --recipe")
    with sv.db.conn() as c:
        row = c.execute("SELECT * FROM clips_ready WHERE profile_id=? AND engine_clip_id=?", (p.id, args.clip)).fetchone()
        title = c.execute("SELECT title FROM videos WHERE profile_id=? AND video_id=?",
                          (p.id, row["video_id"] if row else "")).fetchone()
    if row is None:
        sys.exit(f"{p.id} has no clip {args.clip}")
    out = pathlib.Path(args.out) if args.out else ROOT / "data" / "villomillo" / "previews" / f"clip{args.clip}_{p.format}.mp4"
    try:
        info = formats.format_clip(sv.db, p, claude_client=claude._client(), jev=sv.jev, video_id=row["video_id"],
                                   start_s=row["start_s"], end_s=row["end_s"], out_path=out,
                                   episode=title["title"] if title else "the episode",
                                   budget_usd=sv.app.budgets.claude_usd_per_month)
    except formats.FormatError as e:
        sys.exit(f"could not apply the look: {e}")
    text = info["text"]
    print(out)
    print("recipe:", info["recipe"], "| left out:", ", ".join(f"{b} ({why})" for b, why in info["dropped"]) or "nothing")
    print("title:", " / ".join(text.title_lines), "| points:", [label for label, _ in text.points], "| takeaway:", text.takeaway)


def cmd_audit(args) -> None:
    """Record that a platform's audit passed (or not) for a channel. Only a passed audit lets the API post for real."""
    from . import accounts

    sv = _services(with_apis=False)
    p = _pick(sv, args.profile)
    if p.posting is None or args.platform not in p.posting.platforms:
        sys.exit(f"{p.id} does not post to {args.platform}")
    accounts.set_audit(sv.db, p.id, args.platform, not args.not_passed, args.note or "")
    route, why = accounts.effective_route(sv.db, p, args.platform)
    print(f"{p.id} {args.platform}: audit marked {'NOT passed' if args.not_passed else 'passed'}. Posts now go {route}: {why}.")


def cmd_account(args) -> None:
    """Pause or resume a channel's account on a platform."""
    from . import accounts

    sv = _services(with_apis=False)
    p = _pick(sv, args.profile)
    if p.posting is None or args.platform not in p.posting.platforms:
        sys.exit(f"{p.id} does not post to {args.platform}")
    if args.action == "pause":
        accounts.pause(sv.db, p.id, args.platform, args.reason or "paused by you")
    else:
        accounts.resume(sv.db, p.id, args.platform)
    print(f"{p.id} {args.platform}: {accounts.state(sv.db, p.id, args.platform)['status']}")


def cmd_accounts(args) -> None:
    """Every channel and platform: route, audit, and whether the account is paused."""
    from . import accounts

    sv = _services(with_apis=False)
    for r in accounts.overview(sv.db, sv.profiles):
        print(f"{r['profile']:<12} {r['platform']:<16} route {r['route']:<7} audit {'passed' if r['audit'] else 'not yet':<8} "
              f"{r['status']}{' (' + r['reason'] + ')' if r['reason'] else ''}  [{r['waiting']} waiting]")


def cmd_route(args) -> None:
    """Show which channel would post an approved clip, and with --apply plan it there."""
    from . import routing

    sv = _services()
    p = _pick(sv, args.profile)
    with sv.db.conn() as c:
        row = c.execute("SELECT id, state FROM clips_ready WHERE profile_id=? AND engine_clip_id=?", (p.id, args.clip)).fetchone()
    if row is None:
        sys.exit(f"{p.id} has no clip {args.clip}")
    decision = routing.route_clip(sv.db, sv.jev, sv.profiles, row["id"])
    print(f"clip {args.clip}: {decision.profile_id or 'held'}: {decision.reason}", decision.scores or "")
    if args.apply:
        if row["state"] != "approved":
            sys.exit("only an approved clip can be planned")
        print("planned:", routing.assign_and_plan(sv.db, lambda: sv.jev, sv.profiles, p.id, row["id"]))


def cmd_recheck(args) -> None:
    """Run the campaign rules again on clips waiting for you or already approved."""
    from . import clipper

    sv = _services()
    p = _pick(sv, args.profile)
    if p.campaign is None:
        sys.exit(f"{p.id} has no campaign rules in its settings")
    for line in clipper.recheck(sv.db, clipper.Engine(), sv.jev, p):
        print(line)


def cmd_clip(args) -> None:
    """Submit one video to the clipping engine and wait for its clips. For testing and manual runs."""
    import time

    from . import clipper, formats

    sv = _services()
    p = _pick(sv, args.profile)
    with sv.db.conn() as c:
        known = c.execute("SELECT 1 FROM videos WHERE profile_id=? AND video_id=?", (p.id, args.video_id)).fetchone()
    if not known:  # a video you name yourself; rights are still checked before anything is clipped
        found = sv.yt.videos([args.video_id])
        if not found:
            sys.exit("YouTube has no such video")
        with sv.db.conn() as c:
            store.upsert_video(c, p.id, found[0], "manual")
    print(clipper.submit(sv.db, clipper.Engine(), sv.yt, p, args.video_id), flush=True)
    for _ in range(args.waits):
        time.sleep(args.interval)
        formatter = formats.formatter_for(sv.db, p, jev=sv.jev, budget_usd=sv.app.budgets.claude_usd_per_month)
        outcome = clipper.collect(sv.db, clipper.Engine(), sv.jev, p, _job(sv, p, args.video_id), formatter)
        print(outcome, flush=True)
        if not outcome.startswith("waiting"):
            break


def _job(sv, p, video_id):
    with sv.db.conn() as c:
        row = c.execute("SELECT * FROM clip_jobs WHERE profile_id=? AND video_id=?", (p.id, video_id)).fetchone()
    if row is None:
        sys.exit("This video was not sent to the engine (see the message above for why).")
    return dict(row)


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
    rights_cmd = sub.add_parser("rights", help="record what you have permission to clip")
    rights_cmd.add_argument("--profile")
    rights_cmd.add_argument("action", choices=["set-channel", "list"])
    rights_cmd.add_argument("channel_id", nargs="?")
    rights_cmd.add_argument("status", nargs="?", choices=["owned", "licensed", "none"])
    rights_cmd.add_argument("--note")
    rights_cmd.set_defaults(fn=cmd_rights)
    clip = sub.add_parser("clip", help="clip one video now and wait for the engine")
    clip.add_argument("video_id")
    clip.add_argument("--profile")
    clip.add_argument("--waits", type=int, default=60)
    clip.add_argument("--interval", type=int, default=30)
    clip.set_defaults(fn=cmd_clip)
    campaign = sub.add_parser("campaign", help="record the source videos a clipping campaign lists")
    campaign.add_argument("action", choices=["add"])
    campaign.add_argument("--profile")
    campaign.add_argument("--url", required=True, help="the campaign's web link")
    campaign.add_argument("--note", required=True, help="the campaign's own wording about its source material")
    campaign.add_argument("--ids", required=True, help="comma-separated YouTube video ids")
    campaign.set_defaults(fn=cmd_campaign)
    yt_connect = sub.add_parser("youtube-connect", help="sign in to YouTube once so clips can be uploaded")
    yt_connect.add_argument("--profile")
    yt_connect.set_defaults(fn=cmd_youtube_connect)
    post_now = sub.add_parser("post-now", help="send one planned post through its platform API right now")
    post_now.add_argument("--profile")
    post_now.add_argument("--post-id", type=int, required=True)
    post_now.set_defaults(fn=cmd_post_now)
    audit = sub.add_parser("audit", help="record that a platform's audit passed for a channel")
    audit.add_argument("--profile")
    audit.add_argument("--platform", required=True)
    audit.add_argument("--not-passed", action="store_true", help="record that it has not passed (or was lost)")
    audit.add_argument("--note")
    audit.set_defaults(fn=cmd_audit)
    account = sub.add_parser("account", help="pause or resume a channel's account on a platform")
    account.add_argument("action", choices=["pause", "resume"])
    account.add_argument("--profile")
    account.add_argument("--platform", required=True)
    account.add_argument("--reason")
    account.set_defaults(fn=cmd_account)
    sub.add_parser("accounts", help="every channel's route, audit and account state").set_defaults(fn=cmd_accounts)
    route = sub.add_parser("route", help="which channel would post an approved clip")
    route.add_argument("--profile")
    route.add_argument("--clip", type=int, required=True, help="the engine's clip number")
    route.add_argument("--apply", action="store_true", help="also plan the post on that channel")
    route.set_defaults(fn=cmd_route)
    fmt = sub.add_parser("format-clip", help="render a clip we hold in a format recipe, to a preview file")
    fmt.add_argument("--profile")
    fmt.add_argument("--clip", type=int, required=True, help="the engine's clip number")
    fmt.add_argument("--recipe", help="a recipe from config/villomillo/blueprints.yaml (default: the channel's own)")
    fmt.add_argument("--out", help="where to write the video")
    fmt.set_defaults(fn=cmd_format_clip)
    copy = sub.add_parser("copy", help="write title and caption options for planned posts")
    copy.add_argument("--profile")
    copy.add_argument("--post-id", type=int)
    copy.add_argument("--force", action="store_true", help="write new options even if some exist")
    copy.set_defaults(fn=cmd_copy)
    recheck = sub.add_parser("recheck", help="run the campaign rules again on clips waiting or approved")
    recheck.add_argument("--profile")
    recheck.set_defaults(fn=cmd_recheck)
    check = sub.add_parser("check", help="test whether the grades point the right way on past uploads")
    check.add_argument("--profile")
    check.add_argument("--sample", type=int, default=60)
    check.set_defaults(fn=cmd_check)
    once = sub.add_parser("once", help="run one research cycle right now")
    once.add_argument("--profile")
    once.add_argument("--no-topics", action="store_true", help="skip keyword searches (they use scarce allowance)")
    once.add_argument("--limit", type=int, help="grade at most this many videos")
    once.set_defaults(fn=cmd_once)
    sub.add_parser("serve", help="open the dashboard only").set_defaults(fn=cmd_serve)
    sub.add_parser("run", help="work in the background and serve the dashboard").set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    args.fn(args)
