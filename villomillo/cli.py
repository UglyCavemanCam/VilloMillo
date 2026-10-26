"""Command line: python -m villomillo <status | serve | run | ...>."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import threading

from . import runner, services, store


def _services(with_apis: bool = True) -> services.Services:
    try:
        return services.build(with_apis=with_apis)
    except Exception as e:  # a missing key or a bad settings file should be a plain message, not a traceback
        sys.exit(f"Cannot start: {e}")


def cmd_status(args) -> None:
    from . import spend

    sv = _services(with_apis=False)
    for p in sv.profiles.values():
        print(f"{p.display_name} ({p.id})")
        print("  videos by stage:", store.counts_by_state(sv.db, p.id) or "none yet")
    print("YouTube used today: general", sv.quota.used("general"), "of", sv.quota.caps["general"],
          "| search", sv.quota.used("search"), "of", sv.quota.caps["search"])
    br = sv.app.spend_breaker
    print(f"Spent since your last approval: Claude ${spend.since_approval(sv.db, 'claude'):.2f} of ${br.claude_usd:.2f}, "
          f"Jev ${spend.since_approval(sv.db, 'jev'):.2f} of ${br.jev_usd:.2f}")
    pause = spend.current(sv.db, br)
    if pause:
        print(f"PAUSED: {pause.text} (python -m villomillo approve-spend)")


def _pick(sv: services.Services, profile_id: str | None):
    """The channel profile a command works on. With more than one profile, it must be named."""
    if profile_id:
        if profile_id not in sv.profiles:
            sys.exit(f"No channel profile called {profile_id!r}. Known: {', '.join(sorted(sv.profiles))}")
        return sv.profiles[profile_id]
    if len(sv.profiles) == 1:
        return next(iter(sv.profiles.values()))
    sys.exit(f"Choose a channel profile with --profile ({', '.join(sorted(sv.profiles))})")


def _wait_for_job(engine, job_id: int, *, every: float = 5.0, limit_s: float = 3 * 3600) -> dict:
    import time

    waited = 0.0
    while True:
        job = engine.job(job_id)
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        if waited >= limit_s:
            sys.exit(f"gave up waiting for engine job {job_id}")
        time.sleep(every)
        waited += every


def cmd_preview(args) -> None:
    """Find and score the moments of one YouTube, Twitch or Kick video, without making any clips."""
    from . import clipper, links

    try:
        link = links.parse(args.link)
    except links.LinkError as e:
        sys.exit(str(e))
    engine = clipper.Engine()
    try:
        job_id = clipper.start_preview(engine, link, focus=args.focus or "")
        print(f"{link.name} video {link.video_id}: finding moments (engine job {job_id})...", flush=True)
        job = _wait_for_job(engine, job_id)
        if job["status"] != "done":
            sys.exit(f"the engine did not finish: {job.get('error') or job['status']}")
        moments = engine.moments(link.video_id)
    except clipper.EngineUnavailable as e:
        sys.exit(str(e))
    if not moments:
        sys.exit("No moments worth clipping were found.")
    print(f"{len(moments)} moments, best first. Make some with: python -m villomillo make {args.link} --moments 1,2")
    for m in moments:
        print(f"  {m['id']:>2}  score {m['score']:>3}  {m['start']:7.1f}s - {m['end']:7.1f}s  {m['hook'][:70]}")


def cmd_score(args) -> None:
    """Score a video's moments: viral score, estimated CPM, expected revenue, and what kind of video it is."""
    from . import clipper, links, scoring

    try:
        link = links.parse(args.link)
    except links.LinkError as e:
        sys.exit(str(e))
    sv = _services()
    engine = clipper.Engine()
    try:
        preview = engine.preview(link.video_id)
        if not preview["moments"] or args.again:
            job_id = clipper.start_preview(engine, link, focus=args.focus or "", force=True)
            print(f"{link.name} video {link.video_id}: finding moments (engine job {job_id})...", flush=True)
            job = _wait_for_job(engine, job_id)
            if job["status"] != "done":
                sys.exit(f"the engine did not finish: {job.get('error') or job['status']}")
            preview = engine.preview(link.video_id)
    except clipper.EngineUnavailable as e:
        sys.exit(str(e))
    if not preview["moments"]:
        sys.exit("No moments worth clipping were found.")
    from . import spend

    try:
        result = scoring.score_video(sv.db, sv.jev, sv.app.scoring, preview, source=args.source, breaker=sv.app.spend_breaker)
    except spend.SpendPaused as e:
        sys.exit(str(e))
    print(f"\nKind: {result.kind.explain}\nCategory: {result.kind.category}.\n")
    for m in result.moments:
        print(f"moment {m.id}  {m.start:.0f}s - {m.end:.0f}s  {m.hook[:60]}")
        for line in m.explain:
            print(f"    {line}")
    print(f"\n{len(result.makeable)} of {len(result.moments)} moments clear the bar.")


def cmd_make(args) -> None:
    """Make only the chosen moments of a video that was previewed."""
    from . import clipper, links

    try:
        link = links.parse(args.link)
        ids = sorted({int(x) for x in args.moments.split(",") if x.strip()})
    except (links.LinkError, ValueError) as e:
        sys.exit(str(e) if isinstance(e, links.LinkError) else "give the moment numbers like 1,3")
    engine = clipper.Engine()
    try:
        job_id = clipper.start_render(engine, link, ids, focus=args.focus or "")
        print(f"Making moments {ids} (engine job {job_id})...", flush=True)
        job = _wait_for_job(engine, job_id)
    except clipper.EngineUnavailable as e:
        sys.exit(str(e))
    if job["status"] != "done":
        sys.exit(f"the engine did not finish: {job.get('error') or job['status']}")
    print(f"Done: {len(engine.clips(link.video_id))} clip(s) now exist for {link.video_id}.")


def cmd_approve_spend(args) -> None:
    """Start a new spending count and let paused work continue (after topping up an empty account)."""
    from . import spend

    sv = _services(with_apis=False)
    pause = spend.current(sv.db, sv.app.spend_breaker)
    spend.approve(sv.db)
    print("Approved. Paused work will continue." if pause else "Approved. Nothing was paused; the spending count starts again from now.")


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
                sv.db, p, post_id, jev=sv.jev, force=args.force, breaker=sv.app.spend_breaker))
        except LookupError as e:
            sys.exit(str(e))


def cmd_clean_slate(args) -> None:
    """Preview, back up, or wipe the clips, posts and queues. Wipe needs the digest that preview printed."""
    from . import cleanslate

    sv = _services(with_apis=False)
    try:
        if args.action == "preview":
            report = cleanslate.preview(sv.db)
            print(json.dumps(report, indent=2))
            print(f"\nTo wipe these exact numbers: run backup, then: python -m villomillo clean-slate wipe --digest {report['digest']}")
        elif args.action == "backup":
            print(f"Backup made and checked: {cleanslate.backup(sv.db)}")
        else:
            if not args.digest:
                sys.exit("wipe needs --digest, the digest that preview printed")
            print(json.dumps(cleanslate.wipe(sv.db, args.digest), indent=2))
    except cleanslate.CleanSlateError as e:
        sys.exit(str(e))


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
    """Run a campaign's rules again on its clips waiting for you or already approved."""
    from . import campaigns, clipper

    sv = _services()
    p = _pick(sv, args.profile)
    c = campaigns.get(sv.db, args.campaign)
    rules = campaigns.rules_for(c)
    if rules is None:
        sys.exit("that campaign has no rules set (add them on its page)")
    for line in clipper.recheck(sv.db, clipper.Engine(), sv.jev, p, c["id"], rules):
        print(line)


def cmd_campaigns(args) -> None:
    """Read Content Rewards now and list every campaign with its verdict."""
    from . import campaigns

    sv = _services()
    for line in campaigns.work(sv):
        print(line)
    for c in campaigns.all_campaigns(sv.db):
        pays = f"${c['rate_cents'] / 100:.2f}" if c["rate_cents"] is not None else "-"
        print(f"[{c['state']:11}] {pays:>6}  {c['title'][:50]}  {c['reason'] or ''}")


def cmd_research(args) -> None:
    """Switch Research on or off, or run one pass now, and show what it has looked at."""
    from . import research

    sv = _services()
    if args.switch:
        research.save_settings(sv.db, sv.app.research, sv.app.scoring, on=args.switch == "on")
    state = "on" if research.is_on(sv.db) else "off"
    print(f"Research is {state}. The bar is ${research.bar(sv.db, sv.app.scoring, sv.app.research):.2f}.")
    if args.switch is None:
        for line in research.work(sv):
            print(line)
    for c in research.candidates(sv.db, 15):
        print(f"[{c['state']:8}] {c['platform']:7} {(c['title'] or c['video_id'])[:55]}  {c['reason'] or c['run_note'] or ''}")


def cmd_rewards_login(args) -> None:
    """Open a browser window on Content Rewards and wait for you to sign in. The same as the Sign in button on the Campaigns page."""
    from . import rewards

    sv = _services(with_apis=False)
    if (why := rewards.missing()):
        raise SystemExit(why)
    print("A browser window is opening. Sign in to Content Rewards there; it closes by itself when you are in.")
    if rewards.login(sv.db):
        print("Signed in. Clips for campaigns are now handed in automatically.")
    else:
        raise SystemExit(rewards.LOGIN["message"])


def cmd_rewards_check(args) -> None:
    """Say whether the saved Content Rewards sign-in is still accepted."""
    from . import rewards

    sv = _services(with_apis=False)
    ok = rewards.check_signed_in(sv.db)
    print({True: "Signed in.", False: "Not signed in: run  python -m villomillo cr-login", None: "Could not check (is the browser tool installed?)."}[ok])


def cmd_key(args) -> None:
    """Store an API key in the credential store, or say which keys are stored. The key is never printed or saved to a file."""
    import getpass

    from . import secrets

    if args.name is None:
        for short, name in secrets.KEYS.items():
            print(f"{short:8} {'stored' if secrets.is_stored(name) else 'not stored'}")
        both = secrets.is_stored(secrets.TWITCH_ID) and secrets.is_stored(secrets.TWITCH_SECRET)
        print(f"{'twitch':8} {'stored' if both else 'not stored'}")
        return
    if args.name == "twitch":  # two values: the app's Client ID and Client Secret from dev.twitch.tv
        try:
            secrets.store(secrets.TWITCH_ID, getpass.getpass("Paste the Twitch Client ID (typing is hidden), then press Enter: "))
            secrets.store(secrets.TWITCH_SECRET, getpass.getpass("Paste the Twitch Client Secret (typing is hidden), then press Enter: "))
        except ValueError as e:
            sys.exit(f"Nothing stored: {e}.")
        print("Stored the Twitch Client ID and Secret in the credential store. Neither was printed or saved anywhere else.")
        return
    value = getpass.getpass(f"Paste the {args.name} key (typing is hidden), then press Enter: ")
    try:
        secrets.store(secrets.KEYS[args.name], value)
    except ValueError as e:
        sys.exit(f"Nothing stored: {e}.")
    print(f"Stored the {args.name} key in the credential store. It was not printed or saved anywhere else.")


def cmd_clip(args) -> None:
    """Submit one video to the clipping engine and wait for its clips. For testing and manual runs."""
    import time

    from . import clipper, formats

    sv = _services()
    p = _pick(sv, args.profile)
    with sv.db.conn() as c:
        known = c.execute("SELECT 1 FROM videos WHERE profile_id=? AND video_id=?", (p.id, args.video_id)).fetchone()
    if not known:  # a video you name yourself
        found = sv.yt.videos([args.video_id])
        if not found:
            sys.exit("YouTube has no such video")
        with sv.db.conn() as c:
            store.upsert_video(c, p.id, found[0], "manual")
    print(clipper.submit(sv.db, clipper.Engine(), p, args.video_id), flush=True)
    for _ in range(args.waits):
        time.sleep(args.interval)
        formatter = formats.formatter_for(sv.db, p, category=p.category, jev=sv.jev, breaker=sv.app.spend_breaker)
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


def _serve(sv: services.Services) -> None:
    import uvicorn

    from .web.app import create_app

    uvicorn.run(create_app(sv), host=sv.app.dashboard.host, port=sv.app.dashboard.port, log_level="warning")


def cmd_serve(args) -> None:
    sv = _services(with_apis=False)
    print(f"Dashboard: http://{sv.app.dashboard.host}:{sv.app.dashboard.port}  (Ctrl+C to stop)")
    _serve(sv)


def cmd_run(args) -> None:
    from . import engine_ai, engine_manager

    sv = _services()
    stop = threading.Event()
    from . import relook, rewards

    worker = threading.Thread(target=runner.loop, args=(sv, stop), kwargs={"skip": rewards.KINDS + relook.KINDS}, name="worker", daemon=True)
    worker.start()
    threading.Thread(target=runner.lane, args=(sv, stop, rewards.KINDS), kwargs={"name": "rewards"}, name="rewards", daemon=True).start()
    threading.Thread(target=runner.lane, args=(sv, stop, relook.KINDS), kwargs={"name": "formats"}, name="formats", daemon=True).start()  # applying a format never waits behind a clip job
    try:  # Claude finds the moments when you want it and have a key: the engine needs its own copy of the key
        engine_ai.sync_key()
    except Exception as e:  # never stops the app starting
        print(f"Could not give the engine your Claude key: {e}")
    engine_manager.manager.env_for = lambda: engine_ai.env(sv.db)
    print("Claude finds the moments in videos." if engine_ai.active(sv.db) else "The engine's own model finds the moments in videos.")
    print(engine_manager.manager.enable())
    keeper = threading.Thread(target=engine_manager.manager.run_forever, args=(stop,), name="engine", daemon=True)
    keeper.start()
    print(f"Working in the background. Dashboard: http://{sv.app.dashboard.host}:{sv.app.dashboard.port}  (Ctrl+C to stop)")
    print(f"The clip engine's output is saved in {engine_manager.manager.log_path}")
    try:
        _serve(sv)
    finally:
        stop.set()
        engine_manager.manager.stop()
        worker.join(timeout=10)


def cmd_doctor(args) -> None:
    """Check that everything VilloMillo needs is working, and say what to do about anything that is not."""
    from . import health

    sv = _services(with_apis=False)
    items = health.checks(sv)
    mark = {"ok": "OK  ", "warn": "WARN", "bad": "FIX "}
    for i in items:
        print(f"[{mark[i['state']]}] {i['name']}: {i['detail']}")
        if i["fix"] and i["state"] != "ok":
            print(f"        -> {i['fix']}")
    print(f"\n{health.summary(items)}")
    if any(i["state"] == "bad" for i in items):
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="villomillo")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show counts and today's usage").set_defaults(fn=cmd_status)
    preview = sub.add_parser("preview", help="find and score the moments of a YouTube, Twitch or Kick video; makes no clips")
    preview.add_argument("link")
    preview.add_argument("--focus", help="what the clips should be about, in your words")
    preview.set_defaults(fn=cmd_preview)
    score = sub.add_parser("score", help="score a video's moments (viral score, estimated CPM, expected revenue) with an explanation")
    score.add_argument("link")
    score.add_argument("--source", choices=["paste", "research", "campaign"], default="paste")
    score.add_argument("--focus")
    score.add_argument("--again", action="store_true", help="look at the video again instead of using the earlier preview")
    score.set_defaults(fn=cmd_score)
    make = sub.add_parser("make", help="make only the chosen moments of a video you previewed")
    make.add_argument("link")
    make.add_argument("--moments", required=True, help="the moment numbers from 'preview', like 1,3")
    make.add_argument("--focus")
    make.set_defaults(fn=cmd_make)
    sub.add_parser("approve-spend", help="approve more spending and let paused work continue").set_defaults(fn=cmd_approve_spend)
    clip = sub.add_parser("clip", help="clip one video now and wait for the engine")
    clip.add_argument("video_id")
    clip.add_argument("--profile")
    clip.add_argument("--waits", type=int, default=60)
    clip.add_argument("--interval", type=int, default=30)
    clip.set_defaults(fn=cmd_clip)
    from . import secrets as _secrets

    key = sub.add_parser("key", help="store an API key safely (no name: show which keys are stored)")
    key.add_argument("name", nargs="?", choices=sorted([*_secrets.KEYS, "twitch"]))
    key.set_defaults(fn=cmd_key)
    sub.add_parser("cr-login", help="sign in to Content Rewards once, in a browser window, so clips are handed in by themselves").set_defaults(fn=cmd_rewards_login)
    sub.add_parser("cr-check", help="check that the saved Content Rewards sign-in is still accepted").set_defaults(fn=cmd_rewards_check)
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
    clean = sub.add_parser("clean-slate", help="clear clips, posts and queues so the app starts fresh: preview, then backup, then wipe")
    clean.add_argument("action", choices=["preview", "backup", "wipe"])
    clean.add_argument("--digest", help="the digest that preview printed; required for wipe")
    clean.set_defaults(fn=cmd_clean_slate)
    copy = sub.add_parser("copy", help="write title and caption options for planned posts")
    copy.add_argument("--profile")
    copy.add_argument("--post-id", type=int)
    copy.add_argument("--force", action="store_true", help="write new options even if some exist")
    copy.set_defaults(fn=cmd_copy)
    recheck = sub.add_parser("recheck", help="run the campaign rules again on clips waiting or approved")
    recheck.add_argument("--profile")
    recheck.add_argument("--campaign", required=True, help="the campaign's id")
    recheck.set_defaults(fn=cmd_recheck)
    sub.add_parser("campaigns", help="read Content Rewards now and list every campaign with its verdict").set_defaults(fn=cmd_campaigns)
    research = sub.add_parser("research", help="run one Research pass now (it must be switched on), or switch it on or off")
    research.add_argument("switch", nargs="?", choices=["on", "off"])
    research.set_defaults(fn=cmd_research)
    sub.add_parser("doctor", help="check the engine, worker, keys, YouTube sign-ins and posting settings").set_defaults(fn=cmd_doctor)
    sub.add_parser("serve", help="open the dashboard only").set_defaults(fn=cmd_serve)
    sub.add_parser("run", help="work in the background and serve the dashboard").set_defaults(fn=cmd_run)
    args = parser.parse_args(argv)
    args.fn(args)
