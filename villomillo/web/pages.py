"""Data for each dashboard page.

Everything here reads the database and returns plain dicts and lists for the templates. Nothing writes, nothing
calls a paid service. Times are shown in Eastern Time (see villomillo/timefmt.py).
"""

from __future__ import annotations

import json
import pathlib
from collections import defaultdict
from datetime import date, timedelta
from urllib.parse import quote

import keyring

from .. import (
    accounts,
    activity,
    campaigns,
    channels,
    clipper,
    engine_ai,
    engine_manager,
    health,
    posting,
    removal,
    research,
    routing,
    runs,
    safety,
    secrets,
    spend,
    store,
    studio,
    timefmt,
    validate,
)
from ..config import ROOT, Profile
from ..db import Database
from ..services import Services

STATIC = pathlib.Path(__file__).parent / "static"


def badge(text: str, kind: str = "") -> dict:
    return {"text": text, "kind": kind}


def rows(db: Database, sql: str, *args) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def one(db: Database, sql: str, *args) -> dict | None:
    found = rows(db, sql, *args)
    return found[0] if found else None


def money(value: float) -> str:
    return f"${value:,.2f}"


def mmss(seconds: float | None) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


# --- states ----------------------------------------------------------------------------------------------------

CLIP_STATE = {
    "review": badge("Waiting", "accent"), "approved": badge("Approved", "good"), "rejected": badge("Rejected", "bad"),
    "rejected_auto": badge("Stopped by rules", "bad"), "failed_validation": badge("Failed checks", "bad"),
    "superseded": badge("Replaced", ""), "removed": badge("Removed", ""),
}


def post_state(status: str, route: str) -> dict:
    """How a post reads everywhere: badge kind and text, the calendar colour class, and what it is called."""
    table = {
        "uploaded": ("Uploaded", "good", "s-done"), "posted": ("Posted", "good", "s-done"),
        "manual_ready": ("To post by hand", "warn", "s-hand"), "failed": ("Failed", "bad", "s-fail"),
        "blocked": ("Blocked", "bad", "s-fail"), "dry_run": ("Dry run", "", "s-plan"), "cancelled": ("Cancelled", "", "s-plan"),
    }
    if status == "queued":
        text, kind, css = ("Planned, by hand", "warn", "s-hand") if route == "manual" else ("Planned", "", "s-plan")
    else:
        text, kind, css = table.get(status, (status, "", "s-plan"))
    return {"text": text, "kind": kind, "css": css, "key": status if status != "queued" else f"queued-{route}"}


health_checks = health  # home_page has a list of its own called health


# --- the shell: side panel, top bar, bell ------------------------------------------------------------------------

def engine_up() -> bool:
    """Whether the clipping engine answers (two failed checks in a row are needed before it counts as off)."""
    return engine_manager.up()


def engine_card() -> dict:
    """The Clip engine panel on Settings: state, uptime, last error and which buttons make sense."""
    st = engine_manager.status()
    up = st["uptime_s"]
    st["uptime_text"] = (f"{up // 3600}h {up % 3600 // 60}m" if up >= 3600 else f"{up // 60}m {up % 60}s") if up is not None else ""
    st["can_start"] = st["managed"] and st["state"] in ("off", "error")
    st["can_restart"] = st["managed"] and st["state"] != "off"
    return st


def channel_name(p: Profile) -> str:
    return p.brand.name or p.display_name


def asset_version() -> str:
    stamp = max((f.stat().st_mtime for f in (STATIC / "app.css", STATIC / "app.js") if f.exists()), default=0)
    return str(int(stamp))


def youtube_connected(p: Profile) -> bool:
    return (ROOT / "data" / "villomillo" / f"youtube_token_{p.id}.json").exists()


def has_secret(*names: str) -> bool:
    try:
        return any(keyring.get_password(secrets.SERVICE, n) for n in names)
    except Exception:  # a locked or missing keychain must never break a page
        return False


def needs(sv: Services, p: Profile) -> list[dict]:
    """Everything waiting on you, most important first. Each has a kind: accent, warn, bad or neutral."""
    db, out = sv.db, []
    for r in runs.recent(db, 30):
        if r["state"] == "planned":
            out.append({"title": f"Press Start on \"{(r['title'] or r['video_id'])[:60]}\"", "sub": "Its clips are scored and waiting for you",
                        "btn": "See the plan", "kind": "accent", "primary": True, "href": f"/runs/{r['id']}?profile={p.id}"})
        elif r["state"] == "failed":
            out.append({"title": f"A pasted video failed: {(r['title'] or r['url'])[:60]}", "sub": (r["error"] or "")[:120],
                        "btn": "Open", "kind": "bad", "primary": False, "href": f"/runs/{r['id']}?profile={p.id}"})
    ready = campaigns.ready_to_submit(db)
    for cid in dict.fromkeys(r["campaign_id"] for r in ready):
        mine = [r for r in ready if r["campaign_id"] == cid]
        out.append({"title": f"Submit {len(mine)} link{'s' if len(mine) != 1 else ''} to \"{mine[0]['campaign_title'][:50]}\"",
                    "sub": "Posted clips whose links this campaign is waiting for", "btn": "Open", "kind": "accent", "primary": True,
                    "href": f"/campaigns/{cid}?profile={p.id}"})
    every = campaigns.all_campaigns(db)
    for c in every:
        if c["state"] == "joined" and not c["sources"].strip():
            out.append({"title": f"Tell VilloMillo where \"{c['title'][:50]}\" videos are", "sub": "Add the show's channel or video links so it can start clipping",
                        "btn": "Open", "kind": "accent", "primary": False, "href": f"/campaigns/{c['id']}?profile={p.id}"})
    for miss in research.missing_channels(db, sv.profiles):
        n = miss["videos"]
        out.append({"title": f"Create a channel for \"{miss['category']}\"", "sub": f"Research found {n} good video{'s' if n != 1 else ''} it could not use without one",
                    "btn": "Add channel", "kind": "neutral", "primary": False,
                    "href": f"/channels/new?profile={p.id}&category={quote(miss['category'])}"})
    worth = [c for c in every if c["state"] == "recommended"]
    if worth:
        out.append({"title": f"{len(worth)} campaign{'s' if len(worth) != 1 else ''} worth joining", "sub": f"Best pays ${max(c['rate_cents'] for c in worth) / 100:.2f} per 1,000 views on YouTube. Joining is done by hand on Content Rewards",
                    "btn": "See campaigns", "kind": "neutral", "primary": False, "href": f"/campaigns?profile={p.id}"})
    for held in routing.held_categories(db):
        n, lo, hi = held["clips"], held["low"], held["high"]
        out.append({"title": f"Create a channel for \"{held['category']}\"",
                    "sub": f"{n} clip{'s are' if n != 1 else ' is'} held" + (f", scored {lo} to {hi}" if lo is not None else ""),
                    "btn": "Add channel", "kind": "accent", "primary": True,
                    "href": f"/channels/new?profile={p.id}&category={quote(held['category'])}"})
    waiting = one(db, "SELECT COUNT(*) AS n, MIN(id) AS first FROM clips_ready WHERE profile_id=? AND state='review'", p.id)
    if waiting["n"]:
        n = waiting["n"]
        out.append({"title": f"Review {n} clip{'s' if n != 1 else ''}", "sub": "Passed every check and waiting for your decision",
                    "btn": "Review", "kind": "accent", "primary": True,
                    "href": f"/clips?profile={p.id}&tab=review&panel=/clips/{waiting['first']}/panel"})
    hand = rows(db, "SELECT platform FROM posts WHERE profile_id=? AND route='manual' AND status IN ('queued','manual_ready')", p.id)
    if hand:
        names = sorted({h["platform"].replace("_", " ") for h in hand})
        out.append({"title": f"Post {len(hand)} clip{'s' if len(hand) != 1 else ''} by hand",
                    "sub": f"{', '.join(names)}. Titles and captions are ready to copy", "btn": "Open calendar",
                    "kind": "accent", "primary": True, "href": f"/calendar?profile={p.id}&view=list"})
    failed = one(db, "SELECT COUNT(*) AS n, MAX(error) AS why FROM posts WHERE profile_id=? AND status='failed'", p.id)
    if failed["n"]:
        out.append({"title": f"{failed['n']} post{'s' if failed['n'] != 1 else ''} failed", "sub": (failed["why"] or "")[:120],
                    "btn": "See posts", "kind": "bad", "primary": False, "href": f"/calendar?profile={p.id}&view=list"})
    for r in accounts.overview(db, sv.profiles):
        if r["profile"] == p.id and r["status"] == "paused":
            out.append({"title": f"{r['platform'].replace('_', ' ').title()} account is paused", "sub": r["reason"] or "Paused",
                        "btn": "Open channel", "kind": "bad", "primary": False, "href": f"/channels/{r['profile']}?profile={r['profile']}"})
    held = one(db, "SELECT COUNT(*) AS n FROM clips_ready WHERE profile_id=? AND state='approved' AND reason LIKE 'approved, but not posted%' AND reason NOT LIKE '%no channel for the category%'", p.id)
    if held["n"]:
        out.append({"title": f"{held['n']} approved clip{'s' if held['n'] != 1 else ''} fit no channel", "sub": "Not posted: see the reason on the clip",
                    "btn": "Open clips", "kind": "warn", "primary": False, "href": f"/clips?profile={p.id}&tab=approved"})
    if p.posting and not p.posting.dry_run:
        for platform, route in p.posting.platforms.items():
            if route == "api" and not accounts.audit_passed(db, p.id, platform):
                out.append({"title": f"{platform.replace('_', ' ').title()} audit has not passed",
                            "sub": "Posts stay manual until you record the audit as passed", "btn": "Open channel", "kind": "warn",
                            "primary": False, "href": f"/channels/{p.id}?profile={p.id}"})
    if not engine_up():
        out.append({"title": "Clip engine is off", "sub": "Start it so videos you paste can be looked at and clipped",
                    "btn": "Settings", "kind": "neutral", "primary": False, "href": f"/settings?profile={p.id}"})
    return out


def shell(sv: Services, p: Profile, path: str, active: str) -> dict:
    """What every page needs around its own content."""
    to_do = needs(sv, p)
    waiting = one(sv.db, "SELECT COUNT(*) AS n FROM clips_ready WHERE profile_id=? AND state='review'", p.id)["n"]
    paused = any(r["status"] == "paused" for r in accounts.overview(sv.db, sv.profiles))
    q = f"?profile={p.id}"
    eng = engine_manager.status()
    br = sv.app.spend_breaker
    claude_s, jev_s = spend.since_approval(sv.db, "claude"), spend.since_approval(sv.db, "jev")
    pause = spend.current(sv.db, br)
    return {
        "pause": pause and {"title": pause.title, "text": pause.text, "kind": pause.kind},
        "stopped": safety.is_stopped(sv.db),
        "current": p, "path": path, "channel_name": channel_name(p), "channel_sub": p.category if p.category and p.category != channel_name(p) else "",
        "channels": [{"id": c.id, "name": channel_name(c), "sub": c.display_name if c.brand.name else ""} for c in sv.profiles.values()],
        "nav": [
            {"label": "Find", "links": [{"key": "home", "label": "Home", "href": f"/{q}", "icon": "home"},
                                         {"key": "research", "label": "Research", "href": f"/research{q}", "icon": "trend"},
                                         {"key": "campaigns", "label": "Campaigns", "href": f"/campaigns{q}", "icon": "search"}]},
            {"label": "Make", "links": [
                {"key": "clips", "label": "Clips", "href": f"/clips{q}", "icon": "clips", "pill": waiting or None}]},
            {"label": "Post", "links": [
                {"key": "channels", "label": "Channels", "href": f"/channels{q}", "icon": "plug",
                 "dot": "An account is paused" if paused else None},
                {"key": "calendar", "label": "Calendar", "href": f"/calendar{q}", "icon": "calendar"}]},
            {"label": "Learn", "links": [{"key": "results", "label": "Results", "href": f"/results{q}", "icon": "trend"},
                                       {"key": "insights", "label": "Insights", "href": f"/insights{q}", "icon": "chart"}]},
        ],
        "engine": {"kind": eng["kind"], "text": eng["text"], "title": eng["title"], "href": f"/settings{q}#engine"},
        "budget": {"text": f"Claude {money(claude_s)} of {money(br.claude_usd)} · Jev {money(jev_s)} of {money(br.jev_usd)}",
                   "title": "Spent since your last approval. Everything pauses at these amounts and waits for you to approve more."},
        "alerts": [a for a in to_do if a["kind"] != "neutral"], "needs": to_do,
        "asset_v": asset_version(), "has_geist": (STATIC / "fonts" / "Geist-Variable.woff2").exists(),
    }


# --- clips -----------------------------------------------------------------------------------------------------

def _grade(r: dict) -> dict:
    return json.loads(r["grade_json"] or "{}")


def _clip_card(p: Profile, r: dict, post: dict | None) -> dict:
    grade, val = _grade(r), json.loads(r["validation_json"] or "{}")
    rules = grade.get("campaign")
    failed = bool(rules and not rules.get("ok")) or r["state"] in ("failed_validation", "rejected_auto")
    title = ""
    if post:
        title = post["title"] or (post["caption"] or "").split("\n")[0]
    return {
        "id": r["id"], "engine_id": r["engine_clip_id"], "grade": r["engine_score"], "state": CLIP_STATE.get(r["state"], badge(r["state"])),
        "key": r["state"], "title": (title or r["hook"] or f"Clip {r['engine_clip_id']}").strip(" “”\"")[:90],
        "duration": mmss(val.get("facts", {}).get("duration_s") or (r["end_s"] - r["start_s"])),
        "thumb": f"/clips/{r['id']}/thumb?profile={p.id}", "look": "A2 look" if (grade.get("look") or "").startswith("recipe") else "Engine look",
        "rules": badge("Rules failed" if failed else ("Rules passed" if rules else "Checked"), "bad" if failed else "good"),
        "panel": f"/clips/{r['id']}/panel?profile={p.id}",
        "score": r["engine_score"] or 0, "seconds": val.get("facts", {}).get("duration_s") or (r["end_s"] - r["start_s"]),
        "video": r["video_id"], "post_key": ("none" if not post else "posted" if post["status"] in ("uploaded", "posted") else "waiting"),
    }


CLIP_SORTS = {  # key: (label, sort key, newest first among equals)
    "best": ("Highest score first", lambda c: -c["score"]), "lowest": ("Lowest score first", lambda c: c["score"]),
    "newest": ("Newest first", lambda c: -c["id"]), "oldest": ("Oldest first", lambda c: c["id"]),
    "longest": ("Longest first", lambda c: -c["seconds"]), "shortest": ("Shortest first", lambda c: c["seconds"]),
}
CLIP_MIN_SCORES = (0, 60, 70, 80, 90)
CLIP_POST_FILTERS = (("any", "Any"), ("none", "No post planned"), ("waiting", "Post waiting to go out"), ("posted", "Posted"))


def clips_page(sv: Services, p: Profile, tab: str, *, sort: str = "best", min_score: int = 0, video: str = "", post: str = "any") -> dict:
    db = sv.db
    found = rows(db, "SELECT * FROM clips_ready WHERE profile_id=? AND state!='superseded' ORDER BY id DESC", p.id)  # removed ones show only in their tab
    posts = {r["clip_id"]: r for r in rows(db, "SELECT * FROM posts WHERE profile_id=? ORDER BY id", p.id)}
    cards = [_clip_card(p, r, posts.get(r["id"])) for r in found]
    jobs_active = one(db, "SELECT COUNT(*) AS n FROM clip_jobs WHERE profile_id=? AND status IN ('submitted','running','queued')", p.id)["n"]
    groups = {
        "all": lambda c: c["key"] != "removed", "review": lambda c: c["key"] == "review", "approved": lambda c: c["key"] == "approved",
        "rejected": lambda c: c["key"] in ("rejected", "rejected_auto", "failed_validation"), "removed": lambda c: c["key"] == "removed",
    }
    counts = {k: sum(1 for c in cards if f(c)) for k, f in groups.items()}
    counts["progress"] = jobs_active
    tabs = [{"key": k, "label": label, "count": counts[k]} for k, label in
            (("all", "All"), ("review", "Waiting for you"), ("approved", "Approved"), ("rejected", "Rejected"), ("removed", "Removed"), ("progress", "In progress"))]
    tab = tab if tab in counts else "all"
    studio_data = studio.build(db, clipper.Engine().log, p.id) if tab == "progress" else None
    in_tab = [c for c in cards if groups.get(tab, lambda _: False)(c)]
    sort = sort if sort in CLIP_SORTS else "best"
    post = post if post in dict(CLIP_POST_FILTERS) else "any"
    min_score = min_score if min_score in CLIP_MIN_SCORES else 0
    titles = {r["video_id"]: r["title"] for r in rows(db, "SELECT video_id, title FROM videos WHERE profile_id=?", p.id)}
    videos = [{"id": v, "title": (titles.get(v) or v)[:60]} for v in dict.fromkeys(c["video"] for c in in_tab)]
    video = video if video in {v["id"] for v in videos} else ""
    shown = [c for c in in_tab if c["score"] >= min_score and (not video or c["video"] == video) and (post == "any" or c["post_key"] == post)]
    shown.sort(key=lambda c: (CLIP_SORTS[sort][1](c), -c["id"]))  # ties: the newer clip first
    filters = {"sort": sort, "min_score": min_score, "video": video, "post": post}
    return {"tab": tab, "tabs": tabs, "cards": shown, "total": len(in_tab), "filters": filters, "filtered": (min_score, video, post) != (0, "", "any"),
            "sorts": [(k, v[0]) for k, v in CLIP_SORTS.items()], "min_scores": CLIP_MIN_SCORES, "videos": videos, "post_filters": CLIP_POST_FILTERS,
            "studio": studio_data, "live": bool(studio_data and studio_data["job"] and
                                                studio_data["job"]["status"] in ("submitted", "running", "queued"))}


def clip_panel(sv: Services, p: Profile, clip_id: int) -> dict | None:
    r = one(sv.db, "SELECT * FROM clips_ready WHERE id=? AND (profile_id=? OR id IN (SELECT clip_id FROM posts WHERE profile_id=?))",
            clip_id, p.id, p.id)
    if r is None:
        return None
    grade, val = _grade(r), json.loads(r["validation_json"] or "{}")
    facts, rules = val.get("facts", {}), grade.get("campaign") or {}
    rule_facts = rules.get("facts", {})
    problems = rules.get("problems", []) + val.get("problems", [])
    duration = facts.get("duration_s") or (r["end_s"] - r["start_s"])
    checks = [{"name": "Length", "value": f"{duration:.0f} s", "ok": not any("campaign wants" in x or "shorter" in x or "longer" in x for x in problems)}]
    if "face_share" in rule_facts:
        checks.append({"name": "Face in the first seconds", "value": f"seen in {rule_facts['face_share']:.0%} of frames",
                       "ok": not any("face" in x for x in problems)})
    if rule_facts.get("ad_read") is not None:
        checks.append({"name": "Ad read", "value": f"{rule_facts['ad_read']:.0%} likely", "ok": not any("ad read" in x for x in problems)})
    if "self_contained" in grade:
        checks.append({"name": "Stands on its own", "value": f"{grade['self_contained']:.0%}", "ok": grade["self_contained"] >= clipper.SELF_CONTAINED_MIN})
    if "caption_text" in grade:
        checks.append({"name": "Subtitles", "value": "on the whole clip" if grade["caption_text"].strip() else "none",
                       "ok": bool(grade["caption_text"].strip())})
    if facts.get("bitrate_mbps") is not None:
        checks.append({"name": "Picture detail", "value": f"{facts['bitrate_mbps']:.1f} Mbps",
                       "ok": facts["bitrate_mbps"] >= validate.LOW_BITRATE_MBPS})
    if facts.get("lufs") is not None:
        checks.append({"name": "Loudness", "value": f"{facts['lufs']:.1f} LUFS", "ok": not any("loudness" in x for x in problems)})
    post = one(sv.db, "SELECT po.*, cp.title AS chosen_title FROM posts po LEFT JOIN copy_options cp ON cp.post_id=po.id AND cp.chosen=1 "
                      "WHERE po.clip_id=? ORDER BY po.id LIMIT 1", clip_id)
    video = one(sv.db, "SELECT title FROM videos WHERE profile_id=? AND video_id=?", r["profile_id"], r["video_id"])
    card = _clip_card(p, r, post)
    return {"clip": card, "row": r, "checks": checks, "note": "The face check finds a face, not a person: confirm it is the host when you watch."
            if rule_facts else "", "post": ({**post, "state": post_state(post["status"], post["route"]),
                                              "platform_name": post["platform"].replace("_", " ").title(),
                                              "when": timefmt.full(post["scheduled_at"])} if post else None),
            "episode": video["title"] if video else "", "file_url": f"/clips/{r['id']}/file?profile={p.id}", "is_review": r["state"] == "review",
            "facts": {"look": card["look"], "size": "1080 by 1920", "start": mmss(r["start_s"]), "end": mmss(r["end_s"])},
            "decided": r["state"] != "review", "reason": r["reason"], "impact": removal.impact(sv.db, p.id, [r["id"]]),
            "can_repost": r["state"] == "approved" and bool(post) and post["status"] in posting.RESENT,
            "handed_in": one(sv.db, "SELECT COUNT(*) AS n FROM campaign_submissions WHERE post_id=?", post["id"])["n"] if post else 0,
            "can_post": r["state"] == "approved" and (post is None or post["status"] in (*posting.RESENDABLE, "queued", "failed")),
            "post_to": (channel_name(sv.profiles[post["profile_id"]]) if post["profile_id"] in sv.profiles else post["profile_id"]) if post else ""}


# --- calendar --------------------------------------------------------------------------------------------------

def _post_view(r: dict) -> dict:
    s = post_state(r["status"], r["route"])
    moment = r["posted_at"] if r["status"] in ("uploaded", "posted") and r["posted_at"] else r["scheduled_at"]
    et = timefmt.to_et(moment)
    caption = r["caption"] or ""
    return {**r, "state": s, "date": et.date(), "time": timefmt.clock(moment), "platform_name": r["platform"].replace("_", " ").title(),
            "mark": "YT" if r["platform"].startswith("youtube") else r["platform"][:2].upper(),
            "short": (r["title"] or caption.split("\n")[0] or "Clip")[:48], "when": timefmt.full(moment),
            "panel": f"/calendar/post/{r['id']}?profile={r['profile_id']}"}


def calendar_page(sv: Services, p: Profile, view: str, ref: date | None) -> dict:
    today = timefmt.today_et()
    ref = ref or today
    posts = [_post_view(r) for r in rows(sv.db, "SELECT po.* FROM posts po WHERE po.profile_id=? AND po.status!='cancelled' ORDER BY po.scheduled_at", p.id)]
    by_day: dict[date, list[dict]] = defaultdict(list)
    for post in posts:
        by_day[post["date"]].append(post)
    for items in by_day.values():
        items.sort(key=lambda x: x["scheduled_at"])
    view = view if view in ("month", "week", "list") else "month"
    q = f"/calendar?profile={p.id}"
    out = {"view": view, "today": today, "zone": timefmt.zone_label(), "posts": posts,
           "counts": {k: sum(1 for x in posts if x["state"]["key"] in keys) for k, keys in
                      {"hand": ("manual_ready", "queued-manual"), "done": ("uploaded", "posted"), "plan": ("queued-api", "dry_run"),
                       "fail": ("failed", "blocked")}.items()}}
    if view == "month":
        days = timefmt.month_grid(ref.year, ref.month)
        out["cells"] = [{"date": d, "in_month": d.month == ref.month, "is_today": d == today, "posts": by_day.get(d, [])[:3],
                         "more": max(0, len(by_day.get(d, [])) - 3), "month_label": d.strftime("%b") if d.day == 1 else "",
                         "day_panel": f"/calendar/day/{d}?profile={p.id}"} for d in days]
        prev, nxt = (ref.replace(day=1) - timedelta(days=1)).replace(day=1), (ref.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.update(period=f"{ref:%B %Y}", prev=f"{q}&view=month&date={prev}", next=f"{q}&view=month&date={nxt}")
    elif view == "week":
        days = timefmt.week_of(ref)
        out["columns"] = [{"date": d, "is_today": d == today, "posts": by_day.get(d, [])} for d in days]
        out.update(period=f"{days[0]:%b} {days[0].day} to {days[-1]:%b} {days[-1].day}",
                   prev=f"{q}&view=week&date={ref - timedelta(days=7)}", next=f"{q}&view=week&date={ref + timedelta(days=7)}")
    else:
        out["rows"] = sorted(posts, key=lambda x: x["scheduled_at"])
        out.update(period="All posts", prev=None, next=None)
    out["today_link"] = f"{q}&view={view}"
    return out


def day_panel(sv: Services, p: Profile, day: date) -> dict:
    """Every post planned or sent on one Eastern Time day, earliest first: what a month cell has no room to show."""
    posts = [_post_view(r) for r in rows(sv.db, "SELECT po.* FROM posts po WHERE po.profile_id=? AND po.status!='cancelled' ORDER BY po.scheduled_at", p.id)]
    mine = sorted((x for x in posts if x["date"] == day), key=lambda x: x["scheduled_at"])
    back = f"/calendar/day/{day}?profile={p.id}"
    clips = {r["id"]: r for r in rows(sv.db, "SELECT * FROM clips_ready WHERE id IN (SELECT clip_id FROM posts WHERE profile_id=?)", p.id)}
    out = []
    for x in mine:
        clip = clips.get(x["clip_id"])
        card = _clip_card(p, clip, x) if clip else None
        out.append({**x, "panel": f"{x['panel']}&back={quote(back, safe='')}", "clip": card,
                    "goes": "By hand" if x["route"] == "manual" else "Automatic",
                    "note": (x["error"] or "")[:110] if x["status"] in ("failed", "blocked") else ""})
    summary = [{"text": text, "n": n, "kind": kind} for text, n, kind in (
        ("uploaded", sum(1 for x in out if x["status"] in ("uploaded", "posted")), "good"),
        ("waiting", sum(1 for x in out if x["status"] in ("queued", "manual_ready", "dry_run")), ""),
        ("need attention", sum(1 for x in out if x["status"] in ("failed", "blocked")), "bad")) if n]
    return {"day": day, "title": f"{day:%A}, {day:%B} {day.day}", "posts": out, "summary": summary}


def post_panel(sv: Services, p: Profile, post_id: int) -> dict | None:
    r = one(sv.db, "SELECT po.* FROM posts po WHERE po.id=? AND po.profile_id=?", post_id, p.id)
    if r is None:
        return None
    clip = one(sv.db, "SELECT * FROM clips_ready WHERE id=?", r["clip_id"])
    view = _post_view(r)
    options = rows(sv.db, "SELECT id, title, caption, stop_score, misleading, chosen FROM copy_options WHERE post_id=? "
                          "ORDER BY chosen DESC, stop_score DESC, id", post_id)
    route, why = accounts.effective_route(sv.db, p, r["platform"])
    paused = accounts.is_paused(sv.db, p.id, r["platform"])
    return {"post": view, "tags": json.loads(r["tags_json"] or "[]"), "options": options, "route": route, "route_why": why, "paused": paused,
            "clip": _clip_card(p, clip, r) if clip else None, "privacy": p.posting.privacy if p.posting else "", "can_edit": r["status"] not in ("uploaded", "posted"),
            "can_resend": r["status"] in ("queued", "manual_ready", "dry_run") and bool(clip) and clip["state"] == "approved",
            "can_delete": r["status"] in removal.NOT_OUT,
            "is_hand": r["status"] in ("manual_ready", "queued") and r["route"] == "manual",
            "file_url": f"/clips/{r['clip_id']}/file?profile={p.id}"}


# --- home ------------------------------------------------------------------------------------------------------

RUN_STATE = {
    "analysing": badge("Analysing", "accent"), "planned": badge("Your Start", "accent"), "nothing": badge("Nothing worth making", ""),
    "making": badge("Making clips", "accent"), "finished": badge("Done", "good"), "failed": badge("Failed", "bad"), "removed": badge("Removed", ""),
}


def _thumb(run: dict) -> str | None:
    return f"https://i.ytimg.com/vi/{run['video_id']}/mqdefault.jpg" if run["platform"] == "youtube" else None


SOURCE_LABEL = {"paste": "Paste", "research": "Research", "campaign": "Campaign"}


def _run_row(r: dict, latest: str | None = None) -> dict:
    sub = {"analysing": "Looking at the video and scoring its clips", "planned": "Scored, waiting for your Start",
           "making": "Making and checking the clips"}.get(r["state"], r["note"] or r["error"] or "")
    if latest and r["state"] in ("analysing", "making"):
        sub = latest  # what it is doing right now
    plan = json.loads(r["plan_json"]) if r.get("plan_json") else None
    parts = [SOURCE_LABEL.get(r["source"], r["source"].title())]
    if plan:
        parts.append(plan["category"])
    return {"id": r["id"], "title": r["title"] or r["url"], "platform": {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}.get(r["platform"], r["platform"].title()),
            "state": RUN_STATE[r["state"]], "sub": sub, "source": SOURCE_LABEL.get(r["source"], r["source"].title()), "origin": " · ".join(parts), "thumb": _thumb(r)}


LOOK_LABEL = {"podcast_talk": "Clean frame", "generic_safe": "Plain frame"}


def _analysed_in(sv: Services, run: dict) -> str:
    """How long the looking and scoring took, from the first line to the plan line, e.g. '6 min 20 s'."""
    with sv.db.conn() as c:
        row = c.execute("SELECT MIN(ts) AS at FROM activity WHERE run_id=? AND step='plan'", (run["id"],)).fetchone()
    if not row or not row["at"]:
        return ""
    seconds = int((store.parse_time(row["at"]) - store.parse_time(run["created_at"])).total_seconds())
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{max(seconds, 0)} s"


def _run_rows(db) -> list[dict]:
    recent_runs = runs.recent(db, 6)
    latest = activity.latest_by_run(db, [r["id"] for r in recent_runs])
    return [_run_row(r, latest.get(r["id"])) for r in recent_runs]


def ways_view(sv: Services) -> list[dict]:
    """Home's three cards, one for each way a video arrives: Paste, Research, Campaigns, each with where it stands."""
    db = sv.db
    since = store.iso(store.now() - timedelta(hours=24))
    active = rows(db, "SELECT source, state, COUNT(*) AS n FROM runs WHERE state IN ('analysing','making','planned') GROUP BY source, state")
    count = {(r["source"], r["state"]): r["n"] for r in active}
    paste_working = count.get(("paste", "analysing"), 0) + count.get(("paste", "making"), 0)
    paste_wait = count.get(("paste", "planned"), 0)
    cfg = sv.app.research
    on = research.is_on(db)
    looked = one(db, "SELECT COUNT(*) AS n FROM research_candidates WHERE state IN ('started','skipped') AND found_at>=?", since)["n"]
    clipped = one(db, "SELECT COUNT(*) AS n FROM runs WHERE source='research' AND state='finished' AND created_at>=?", since)["n"]
    every = campaigns.all_campaigns(db)
    joined = [c for c in every if c["state"] == "joined"]
    worth = [c for c in every if c["state"] == "recommended"]
    best = max((c["rate_cents"] or 0 for c in joined + worth), default=0)
    return [
        {"key": "paste", "name": "Paste", "href": "/", "badge": badge("You start it", ""),
         "text": f"{paste_working} analysing · {paste_wait} waiting for your Start" if (paste_working or paste_wait) else "Nothing pasted right now"},
        {"key": "research", "name": "Research", "href": "/research", "badge": badge("On", "good") if on else badge("Off", ""),
         "text": (f"Bar {money(research.bar(db, sv.app.scoring, cfg))} · {looked} videos looked at today · {clipped} clipped") if on else "Switched off. Turn it on to find videos by itself"},
        {"key": "campaigns", "name": "Campaigns", "href": "/campaigns", "badge": badge("Runs by itself", "good") if joined else badge("Join one", ""),
         "text": (f"{len(joined)} joined · {len(worth)} worth joining" + (f" · best {money(best / 100)} per 1,000 views" if best else ""))},
    ]


def run_page(sv: Services, run_id: int) -> dict:
    run = runs.get(sv.db, run_id)
    plan = run["plan"]
    moments = []
    for m in (plan["moments"] if plan else []):
        moments.append({**m, "when": f"{mmss(m['start'])} to {mmss(m['end'])}", "revenue_text": money(m["revenue"]), "cpm_text": money(m["cpm"]),
                        "views_text": f"{m['views']:,}"})
    return {"run": run, "row": _run_row(run), "plan": plan, "moments": moments, "thumb": _thumb(run),
            "makes": sum(1 for m in moments if m["make"]), "total_text": money(plan["expected_total"]) if plan else "",
            "clips": [{**c, "when": f"{mmss(c['start_s'])} to {mmss(c['end_s'])}"} for c in runs.clips_of(sv.db, run)],
            "active": run["state"] in ("analysing", "making"), "viral_min": sv.app.scoring.viral_min, "cpm_floor": sv.app.scoring.cpm_floor,
            "duration": mmss(run["duration"]) if run["duration"] else "", "tl": timeline_view(sv, run),
            "origin": {"paste": "Pasted link", "research": "Found by Research", "campaign": "Campaign video"}.get(run["source"], "Video"),
            "analysed_in": _analysed_in(sv, run), "look_label": LOOK_LABEL.get(plan["look"], plan["look"].replace("_", " ").capitalize()) if plan else ""}


def home_page(sv: Services, p: Profile) -> dict:
    db = sv.db
    by_state = {r["state"]: r["n"] for r in rows(db, "SELECT state, COUNT(*) AS n FROM clips_ready WHERE profile_id=? GROUP BY state", p.id)}
    made = sum(n for s, n in by_state.items() if s not in ("superseded", "removed"))
    passed = made - by_state.get("failed_validation", 0) - by_state.get("rejected_auto", 0)
    posts = rows(db, "SELECT status, route FROM posts WHERE profile_id=? AND status!='cancelled'", p.id)
    hand = sum(1 for x in posts if x["route"] == "manual" and x["status"] in ("queued", "manual_ready"))
    posted = sum(1 for x in posts if x["status"] in ("uploaded", "posted"))
    listed = one(db, "SELECT COUNT(*) AS n FROM videos WHERE profile_id=?", p.id)["n"]
    clipped = one(db, "SELECT COUNT(*) AS n FROM clip_jobs WHERE profile_id=? AND status='done'", p.id)["n"]
    br = sv.app.spend_breaker
    claude_s, jev_s = spend.since_approval(db, "claude"), spend.since_approval(db, "jev")
    waiting = by_state.get("review", 0)
    stats = [
        {"k": "Posts waiting for you", "v": hand, "s": "to post by hand" if hand else "nothing to post by hand"},
        {"k": "Clips waiting for review", "v": waiting, "s": "passed every check" if waiting else "nothing to review"},
        {"k": "Clips approved", "v": by_state.get("approved", 0), "s": f"of {made} made"},
        {"k": "AI spend since last approval", "v": money(claude_s + jev_s),
         "s": f"Claude {money(claude_s)} of {money(br.claude_usd)}, Jev {money(jev_s)} of {money(br.jev_usd)}",
         "bar": min(100, round(100 * max(claude_s / br.claude_usd, jev_s / br.jev_usd)))},
    ]
    funnel = [
        ("Videos found", listed, listed), ("Videos clipped", clipped, listed),
        ("Clips made", made, made), ("Passed every check", passed, made), ("Approved by you", by_state.get("approved", 0), made),
        ("Posted", posted, made)]
    pipeline = [{"label": a, "count": n, "width": max(2 if n else 0, round(100 * n / top)) if top else 0} for a, n, top in funnel]
    recent = [_clip_card(p, r, None) for r in rows(db, "SELECT * FROM clips_ready WHERE profile_id=? AND state NOT IN ('superseded','removed') ORDER BY id DESC LIMIT 5", p.id)]
    today = timefmt.today_et()
    mine = [_post_view(r) for r in rows(db, "SELECT * FROM posts WHERE profile_id=? AND status!='cancelled'", p.id)]
    week = []
    for i in range(7):
        d = today + timedelta(days=i)
        day_posts = [x for x in mine if x["date"] == d]
        week.append({"date": d, "is_today": i == 0, "hand": sum(1 for x in day_posts if x["state"]["css"] == "s-hand"),
                     "done": sum(1 for x in day_posts if x["state"]["css"] == "s-done"),
                     "other": sum(1 for x in day_posts if x["state"]["css"] not in ("s-hand", "s-done")), "total": len(day_posts)})
    w = health_checks.worker(db)
    health = [{"kind": "good" if engine_up() else "", "label": "Clip engine", "value": "On" if engine_up() else "Off"},
              {"kind": {"running": "good", "stuck": "warn", "off": "bad"}[w["state"]], "label": "Worker",
               "value": {"running": "On", "stuck": "Not responding", "off": "Off: start the app with run-app.bat"}[w["state"]]}]
    if p.posting:
        health.append({"kind": "good" if youtube_connected(p) else "warn", "label": "YouTube",
                       "value": f"Connected as {channel_name(p)} ({p.brand.handle})" if youtube_connected(p) else "Not connected yet"})
        for platform, route in p.posting.platforms.items():
            if route == "api" or platform == "youtube_shorts":
                ok = accounts.audit_passed(db, p.id, platform)
                health.append({"kind": "good" if ok else "warn", "label": f"{platform.replace('_', ' ').title()} audit",
                               "value": "Passed" if ok else "Not passed, so public posts stay manual"})
    paused = [r for r in accounts.overview(db, sv.profiles) if r["profile"] == p.id and r["status"] == "paused"]
    health.append({"kind": "bad" if paused else "good", "label": "Safety stops", "value": f"{len(paused)} paused" if paused else "No account is paused"})
    near = max(claude_s / br.claude_usd, jev_s / br.jev_usd) >= 0.8
    health.append({"kind": "warn" if near else "good", "label": "Spend breaker", "value": "Close to the limit" if near else "Well inside the limits"})
    last = health_checks.last_post(db)
    health.append({"kind": "good" if last else "", "label": "Last post", "value": last or "Nothing posted yet"})
    events = [{"when": timefmt.short(e["ts"]), "level": e["level"], "text": f"{e['component']}: {e['message']}"}
              for e in rows(db, "SELECT * FROM events ORDER BY id DESC LIMIT 8")]
    return {"now": now_view(sv, p), "runs": _run_rows(db), "ways": ways_view(sv), "stats": stats, "pipeline": pipeline, "recent": recent, "week": week, "health": health, "events": events,
            "has_week_items": any(d["total"] for d in week), "needs": needs(sv, p)}


def blueprints_summary(recipe: str) -> list[dict]:
    """The layers of a format recipe, for the Settings page."""
    from ..formats import blueprints

    _, recipes = blueprints.load()
    spec = recipes.get(recipe)
    if not spec:
        return []
    out = [{"layer": "Framing", "items": [spec["framing"]]}]
    out += [{"layer": layer.title(), "items": spec[layer]} for layer in ("text", "motion", "brand") if spec.get(layer)]
    return out


# --- channels --------------------------------------------------------------------------------------------------

def _channel_card(sv: Services, c: Profile) -> dict:
    db = sv.db
    clips = one(db, "SELECT COUNT(*) AS n FROM clips_ready WHERE profile_id=? AND state NOT IN ('superseded','removed')", c.id)["n"]
    posted = one(db, "SELECT COUNT(*) AS n FROM posts WHERE profile_id=? AND status IN ('uploaded','posted')", c.id)["n"]
    waiting = one(db, "SELECT COUNT(*) AS n FROM posts WHERE profile_id=? AND status IN ('queued','manual_ready','dry_run')", c.id)["n"]
    held = sum(h["clips"] for h in routing.held_categories(db) if c.category and h["category"].lower() == c.category.lower())
    return {"id": c.id, "name": channel_name(c), "category": c.category or "No category yet", "about": c.niche.definition,
            "youtube": youtube_connected(c), "clips": clips, "posted": posted, "waiting": waiting,
            "held": held, "ready": bool(c.category and youtube_connected(c)),
            "paused": any(r["status"] == "paused" for r in accounts.overview(db, {c.id: c})),
            "today": posting.posted_today(db, c.id, "youtube_shorts") if c.posting else 0, "cap": c.posting.daily_cap if c.posting else 0,
            "privacy": c.posting.privacy if c.posting else "private",
            "look": LOOK_LABEL.get(c.format, (c.format or "Engine's own look").replace("_", " ").capitalize()),
            "tags": (c.posting.tags.get("youtube_shorts", "") if c.posting else "") or "None"}


def _account_rows(sv: Services, c: Profile) -> list[dict]:
    """One row per platform a channel posts to: its connection, route, audit, and the safety stop that can pause it."""
    out = []
    for r in accounts.overview(sv.db, {c.id: c}):
        out.append({**r, "platform_name": {"youtube_shorts": "YouTube Shorts"}.get(r["platform"], r["platform"]),
                    "mark": "YT", "connected": youtube_connected(c), "connect_hint": "Use Connect YouTube above.",
                    "today": posting.posted_today(sv.db, c.id, r["platform"]), "cap": c.posting.daily_cap})
    return out


def channels_page(sv: Services, p: Profile) -> dict:
    cards = [_channel_card(sv, c) for c in sv.profiles.values()]
    state = channels.catch_all(sv.db, sv.profiles)
    for card in cards:
        card["everything"] = state["on"] and card["id"] == state["channel"]
    return {"channels": cards, "held": [] if state["on"] else routing.held_categories(sv.db), "catch_all": {**state, "name": channel_name(sv.profiles[state["channel"]]) if state["channel"] else ""},
            "options": [{"id": c.id, "name": channel_name(c)} for c in sv.profiles.values()]}


def channel_page(sv: Services, c: Profile, *, form: dict | None = None, error: str | None = None) -> dict:
    from ..formats import blueprints

    _, recipes = blueprints.load()
    tags = (c.posting.tags.get("youtube_shorts", "") if c.posting else "")
    steps = [
        {"n": 1, "title": "Name and category", "done": bool(c.category and c.niche.definition)},
        {"n": 2, "title": "YouTube", "done": youtube_connected(c)},
        {"n": 3, "title": "Tags and look", "done": bool(tags or c.format)},
    ]
    values = {"name": channel_name(c), "category": c.category, "about": c.niche.definition, "brand_handle": c.brand.handle,
              "youtube_tags": tags, "format": c.format or "",
              "youtube_daily_cap": c.posting.daily_cap if c.posting else 100,
              "youtube_route": c.posting.platforms.get("youtube_shorts", "manual") if c.posting else "manual",
              "dry_run": "1" if (c.posting.dry_run if c.posting else True) else "0",
              "privacy": c.posting.privacy if c.posting else "public",
              "upload_size": c.posting.upload_size if c.posting else "1080",
              "min_gap_minutes": c.posting.min_gap_minutes if c.posting else 10,
              "max_gap_minutes": (c.posting.max_gap_minutes if c.posting.max_gap_minutes is not None else c.posting.min_gap_minutes) if c.posting else 20,
              "auto_post_min_score": c.posting.auto_post_min_score if c.posting else 80, **(form or {})}
    return {"c": c, "steps": steps, "form": values, "recipes": sorted(recipes), "error": error,
            "youtube": youtube_connected(c), "card": _channel_card(sv, c), "accounts": _account_rows(sv, c),
            "client_file": posting.YOUTUBE_CLIENT.exists(), "client_path": str(posting.YOUTUBE_CLIENT)}


# --- campaigns -------------------------------------------------------------------------------------------------

PLATFORM_LABEL = {"youtube": "YouTube", "tiktok": "TikTok"}
CAMPAIGN_BADGE = {"recommended": badge("Worth joining", "good"), "skipped": badge("Skipped", ""), "joined": badge("Joined", "accent"),
                  "dropped": badge("Stopped", "")}


def _campaign_view(sv: Services, c: dict) -> dict:
    rate = f"${c['rate_cents'] / 100:.2f}" if c["rate_cents"] is not None else "-"
    left = f"${c['budget_left_cents'] / 100:,.0f}" if c["budget_left_cents"] is not None else "-"
    category = c["category"] or "-"
    everything = channels.catch_all_target(sv.db, sv.profiles)
    waiting = bool(c["category"]) and everything is None and channels.for_category(sv.profiles, c["category"]) is None
    channel = everything or (channels.for_category(sv.profiles, c["category"]) if c["category"] else None)
    goes_to = (channel_name(channel) if channel else (f"No channel for {c['category']}" if c["category"] else "-"))
    status = ("Paused: " + c["reason"].removeprefix("Paused: ")) if c["state"] == "joined" and c["reason"] else {
        "joined": "Joined by you", "recommended": "Worth joining", "dropped": "You stopped clipping for it", "skipped": c["reason"] or "Skipped"}[c["state"]]
    if waiting and c["state"] in ("joined", "recommended"):
        status += ": add the channel"
    return {**c, "rate": rate, "left": left, "category_text": category, "no_channel": waiting, "badge": CAMPAIGN_BADGE[c["state"]],
            "goes_to": goes_to, "status_text": status,
            "platform_text": ", ".join(PLATFORM_LABEL.get(p, p.title()) for p in c["platforms"]) or "-",
            "paused": c["state"] == "joined" and bool(c["reason"])}


def _connection(sv: Services) -> dict:
    every = campaigns.all_campaigns(sv.db)
    last = max((c["fetched_at"] for c in every), default=None)
    return {"count": len(every), "last": _ago(activity.age_s(last)) if last else "never"}


def campaigns_page(sv: Services, p: Profile) -> dict:
    return {"campaigns": [_campaign_view(sv, c) for c in campaigns.all_campaigns(sv.db)],
            "ready": campaigns.ready_to_submit(sv.db), "floor": sv.app.scoring.cpm_floor, "min_budget": sv.app.scoring.campaign_min_budget_usd,
            "feed": feed_lines(activity.recent(sv.db, "campaign", 25)), "connection": _connection(sv),
            "pass_line": pass_line(sv, "campaigns", "read", (activity.recent(sv.db, "campaign", 1) or [None])[0])}


def campaign_page(sv: Services, cid: str) -> dict | None:
    c = campaigns.get(sv.db, cid)
    if c is None:
        return None
    rules = c["rules"] or {}
    queue = []
    for q in campaigns.queue_of(sv.db, c["id"]):
        if q["skipped_reason"]:
            state, q = badge("Skipped", ""), {**q, "run_note": q["skipped_reason"]}
        else:
            state = RUN_STATE.get(q["run_state"]) if q["run_state"] else badge("Waiting", "")
        queue.append({**q, "state": state, "title": q["title"] or q["video_id"]})
    how = [("Rules", (f"{rules['min_s']:g} to {rules['max_s']:g} seconds" + (f" · a face in the first {rules['face_window_s']:g} seconds" if rules.get("require_face", True) and rules.get("face_window_s") else "")
                      + (" · captions on the whole clip" if rules.get("require_captions") else "") + (" · no ad reads" if rules.get("no_ad_reads") else ""))
                     if rules else "No rules entered yet: add the ones from the campaign's brief below"),
           ("Required tags", c["tags"] or "None entered yet"),
           ("Source", ", ".join(x.strip() for x in c["sources"].splitlines() if x.strip()) or "None entered yet"),
           ("Before each clip", "its budget is read again; clipping stops when the money runs out")]
    return {"how": how, "c": _campaign_view(sv, c), "form": {"sources": c["sources"], "tags": c["tags"], "use_rules": bool(rules),
                                                 "min_s": rules.get("min_s", 20), "max_s": rules.get("max_s", 60),
                                                 "face_window_s": rules.get("face_window_s", 5), "require_face": rules.get("require_face", True) if rules else False,
                                                 "require_captions": rules.get("require_captions", True), "no_ad_reads": rules.get("no_ad_reads", True)},
            "queue": queue, "ready": [r for r in campaigns.ready_to_submit(sv.db) if r["campaign_id"] == c["id"]],
            "keys": has_secret(secrets.YOUTUBE), "sends": any((not ch.posting.dry_run) for ch in sv.profiles.values() if ch.posting)}


# --- activity: what the system is doing right now ----------------------------------------------------------------

LEVEL_KIND = {"info": "", "warn": "warn", "bad": "bad"}


def _stamp(ts: str) -> str:
    m = timefmt.to_et(ts)
    return f"{m.hour % 12 or 12}:{m:%M:%S} {'AM' if m.hour < 12 else 'PM'}" if m else ""


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 5:
        return "just now"
    if seconds < 90:
        return f"{seconds} seconds ago"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes ago"
    return f"{round(seconds / 3600)} hours ago"


def feed_lines(lines: list[dict]) -> list[dict]:
    return [{"time": _stamp(x["ts"]), "message": x["message"], "kind": LEVEL_KIND.get(x["level"], ""), "run_id": x["run_id"]} for x in lines]


def timeline_view(sv: Services, run: dict) -> dict:
    tl = activity.timeline(sv.db, run, engine_manager.status()["state"])
    steps = [{**s, "time": _stamp(s["at"]) if s["at"] else ""} for s in tl["steps"]]
    return {"steps": steps, "lines": feed_lines(list(reversed(tl["lines"]))[:60]), "now": tl["now"], "updated": _ago(tl["age_s"]),
            "stalled": tl["stalled"], "active": tl["active"], "state": run["state"]}


def pass_line(sv: Services, name: str, label: str, latest: dict | None) -> str:
    """'Next pass in 14 min · last pass 3 minutes ago' for a scheduled kind of work."""
    minutes = activity.next_pass(sv.db, name)
    nxt = "not scheduled" if minutes is None else ("due now" if minutes <= 0 else f"in {minutes} min")
    last = f" · last activity {_ago(activity.age_s(latest['ts']))}" if latest else ""
    return f"Next {label} {nxt}{last}"


def now_view(sv: Services, p: Profile) -> dict:
    """Home's Now panel: engine, worker, the videos being worked on, and what Research and Campaigns last did."""
    items = []
    eng = engine_manager.status()
    items.append({"label": "Clip engine", "text": {"on": "On", "starting": "Starting: its first start can take minutes", "not_responding": "Not responding", "error": "Stopped with an error: " + (eng["last_error"] or "see the log"), "off": "Off: open Settings and press Start"}[eng["state"]], "kind": eng["kind"], "href": f"/settings?profile={p.id}#engine"})
    w = health.worker(sv.db)
    items.append({"label": "Worker", "kind": {"running": "good", "stuck": "warn", "off": "bad"}[w["state"]], "href": f"/settings?profile={p.id}#health-h",
                  "text": {"running": "Working" + (f" on {w['task']}" if w["task"] else ", waiting for the next job"),
                           "stuck": f"No sign of life for {w['age_s']} seconds",
                           "off": "Not running: start the app with run-app.bat so links get looked at"}[w["state"]]})
    for r in runs.recent(sv.db, 20):
        if r["state"] in ("analysing", "making", "planned"):
            tl = activity.timeline(sv.db, r, eng["state"])
            items.append({"label": f"{r['source'].title()} video", "kind": "warn" if tl["stalled"] else "accent", "href": f"/runs/{r['id']}?profile={p.id}",
                          "text": f"{(r['title'] or r['url'])[:50]}: {tl['now']}" + (f" ({_ago(tl['age_s'])})" if tl["age_s"] is not None and tl["active"] else "")})
    on = research.is_on(sv.db)
    latest = (activity.recent(sv.db, "research", 1) or [None])[0]
    items.append({"label": "Research", "kind": "good" if on else "", "href": f"/research?profile={p.id}",
                  "text": ("On. " + pass_line(sv, "research", "pass", latest)) if on else "Off"})
    joined = [c for c in campaigns.all_campaigns(sv.db) if c["state"] == "joined"]
    latest = (activity.recent(sv.db, "campaign", 1) or [None])[0]
    items.append({"label": "Campaigns", "kind": "good" if joined else "", "href": f"/campaigns?profile={p.id}",
                  "text": (f"{len(joined)} joined. " if joined else "None joined yet. ") + pass_line(sv, "campaigns", "read", latest)})
    return {"rows": items}


# --- research --------------------------------------------------------------------------------------------------

SOURCE_NAMES = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}


def _candidate_view(c: dict) -> dict:
    if c["state"] == "skipped":
        state, why = badge("Skipped", ""), c["reason"] or ""
    elif c["state"] == "queued":
        state, why = badge("Waiting", ""), "Next in line when nothing else is being clipped."
    else:
        state, why = RUN_STATE.get(c["run_state"], badge("Started", "accent")), c["run_note"] or ""
    via = "watched channel" if c["via"] == "watch" else f"search: {c['topic']}"
    views = f"{c['views']:,} views" if c["views"] else ""
    plan = json.loads(c["run_plan"]) if c.get("run_plan") else None
    cpm = max((m["cpm"] for m in plan["moments"] if m["make"]), default=max((m["cpm"] for m in plan["moments"]), default=None)) if plan else None
    made = (c.get("posted") or 0) + (c.get("planned") or 0)
    if c["state"] == "skipped" or c["run_state"] in ("nothing", "failed", "removed"):
        result = why
    elif c["run_state"] == "finished":
        result = f"Clipped: {c['posted']} posted" + (f", {c['planned']} planned" if c["planned"] else "") + (", credited" if c["platform"] == "youtube" and made else "") if made else why
    else:
        result = why
    return {**c, "state_badge": state, "why": why, "via_text": via, "platform_text": SOURCE_NAMES.get(c["platform"], c["platform"].title()),
            "minutes": round((c["duration_s"] or 0) / 60), "views_text": views, "cpm_text": money(cpm) if cpm is not None else "-", "result": result}


def research_page(sv: Services, p: Profile) -> dict:
    cfg, db = sv.app.research, sv.db
    s = research.settings(db, cfg)
    return {"on": s["on"], "per_day": s["videos_per_day"], "bar_max": s["bar_max"], "bar": research.bar(db, sv.app.scoring, cfg),
            "floor": sv.app.scoring.cpm_floor, "topics": "\n".join(research.topics(db, cfg, sv.profiles)), "own_topics": bool(s["topics"]),
            "used_today": research.started_recently(db), "waiting": research.queued(db),
            "searches": (sv.quota.used("search"), sv.quota.caps["search"]), "has_key": has_secret(secrets.YOUTUBE),
            "has_channel": bool(sv.profiles), "viral_min": sv.app.scoring.viral_min,
            "sources": [{**r, "platform_text": SOURCE_NAMES.get(r["platform"], r["platform"])} for r in research.sources(db)],
            "candidates": [_candidate_view(c) for c in research.candidates(db)],
            "feed": feed_lines(activity.recent(db, "research", 25)), "has_twitch": sv.twitch is not None,
            "looked": one(db, "SELECT COUNT(*) AS n FROM research_candidates WHERE state IN ('started','skipped') AND found_at>=?", store.iso(store.now() - timedelta(hours=24)))["n"],
            "clipped": one(db, "SELECT COUNT(*) AS n FROM runs WHERE source='research' AND state='finished' AND created_at>=?", store.iso(store.now() - timedelta(hours=24)))["n"],
            "under_bar": one(db, "SELECT COUNT(*) AS n FROM runs WHERE source='research' AND state='nothing' AND created_at>=? AND note LIKE 'No clip here%'", store.iso(store.now() - timedelta(hours=24)))["n"],
            "not_cc": one(db, "SELECT COUNT(*) AS n FROM research_candidates WHERE state='skipped' AND reason LIKE '%not Creative Commons%' AND found_at>=?", store.iso(store.now() - timedelta(hours=24)))["n"],
            "pass_line": pass_line(sv, "research", "pass", (activity.recent(db, "research", 1) or [None])[0])}


# --- results and rules ------------------------------------------------------------------------------------------


def results_page(sv: Services, p: Profile) -> dict:
    """Everything that was made: where each clip came from, where it went, and how its real views compare with the guess."""
    db, expected = sv.db, sv.app.scoring.predicted_views
    today = timefmt.today_et()
    posted_today = sum(1 for r in rows(db, "SELECT posted_at FROM posts WHERE status IN ('uploaded','posted') AND posted_at IS NOT NULL") if timefmt.local_date(r["posted_at"]) == today)
    held = sum(h["clips"] for h in routing.held_categories(db))
    skipped_moments = []
    for r in rows(db, "SELECT id, source, title, plan_json FROM runs WHERE plan_json IS NOT NULL ORDER BY id DESC LIMIT 30"):
        for m in json.loads(r["plan_json"])["moments"]:
            if not m["make"]:
                skipped_moments.append({"run_id": r["id"], "source": SOURCE_LABEL.get(r["source"], r["source"]), "title": (m["hook"] or f"Moment {m['id']}")[:70], "video": r["title"] or "",
                                        "viral": m["viral"], "why": m["explain"][3].replace("Will not be made: ", "Skipped: ")})
    views = one(db, "SELECT COALESCE(SUM(v), 0) AS n FROM (SELECT (SELECT views FROM post_stats s WHERE s.post_id=po.id ORDER BY taken_at DESC LIMIT 1) AS v FROM posts po WHERE po.status IN ('uploaded','posted'))")["n"]
    clips = []
    for r in rows(db, """
        SELECT cr.id, cr.hook, cr.engine_score, cr.state, cr.reason, cr.start_s, cr.end_s, cr.campaign_id, po.id AS post_id, po.profile_id AS post_profile, po.status AS post_status, po.url,
          (SELECT views FROM post_stats s WHERE s.post_id=po.id ORDER BY taken_at DESC LIMIT 1) AS views,
          (SELECT source FROM runs r WHERE r.video_id=cr.video_id ORDER BY r.id DESC LIMIT 1) AS source,
          (SELECT 1 FROM campaign_submissions cs WHERE cs.post_id=po.id) AS handed_in
        FROM clips_ready cr LEFT JOIN posts po ON po.clip_id=cr.id AND po.status!='cancelled'
        WHERE cr.state NOT IN ('superseded','removed') ORDER BY cr.id DESC LIMIT 40"""):
        live = r["post_status"] in ("uploaded", "posted")
        if live:
            where = "YouTube" + (" · link ready to submit" if r["campaign_id"] and r["url"] and not r["handed_in"] else "")
        elif r["post_status"]:
            where = post_state(r["post_status"], "manual")["text"]
        elif (r["reason"] or "").startswith(routing.HELD):
            where = "Held: no channel yet"
        else:
            where = CLIP_STATE.get(r["state"], badge(r["state"]))["text"]
        channel = sv.profiles.get(r["post_profile"]) if r["post_profile"] else None
        clips.append({"id": r["id"], "title": (r["hook"] or f"Clip {r['id']}")[:70], "when": f"{mmss(r['end_s'] - r['start_s'])} long", "source": SOURCE_LABEL.get(r["source"], "Paste"),
                      "channel": channel_name(channel) if channel else "-", "score": r["engine_score"], "where": where, "held": where.startswith("Held"),
                      "views": f"{r['views']:,} of {expected:,}" if live and r["views"] is not None else (f"- of {expected:,}" if live else "-"),
                      "panel": f"/clips?profile={p.id}&panel=/clips/{r['id']}/panel"})
    return {"posted_today": posted_today, "held": held, "skipped": len(skipped_moments), "views": views, "clips": clips, "skipped_rows": skipped_moments[:10]}


def rules_page(sv: Services, p: Profile) -> dict:
    s, cfg, br = sv.app.scoring, sv.app.research, sv.app.spend_breaker
    return {"viral_min": s.viral_min, "cpm_floor": s.cpm_floor, "cpm_base": s.cpm_base, "predicted_views": s.predicted_views, "certainty": round(s.type_certainty_min * 100),
            "bar": research.bar(sv.db, s, cfg), "bar_max": research.settings(sv.db, cfg)["bar_max"], "min_budget": s.campaign_min_budget_usd, "claude_usd": br.claude_usd, "jev_usd": br.jev_usd,
            "engine_weight": round(s.engine_weight * 100), "caps": [(channel_name(c), c.posting.daily_cap) for c in sv.profiles.values() if c.posting], "fit_min": 4}


# --- insights --------------------------------------------------------------------------------------------------

def insights_page(sv: Services, p: Profile) -> dict:
    db = sv.db
    daily = rows(db, "SELECT day, engine, ROUND(SUM(cost_usd), 4) AS usd FROM ai_usage GROUP BY day, engine ORDER BY day DESC LIMIT 28")
    by_day: dict[str, float] = defaultdict(float)
    for r in daily:
        by_day[r["day"]] += r["usd"]
    days = sorted(by_day)[-14:]
    peak = max((by_day[d] for d in days), default=0) or 1
    chart = [{"label": d[5:], "usd": by_day[d], "height": max(2, round(100 * by_day[d] / peak)), "text": money(by_day[d])} for d in days]
    results = rows(db, """
        SELECT p.id, p.platform, p.title, p.url, s.views, s.likes, s.comments, s.shares, s.taken_at FROM posts p
        JOIN post_stats s ON s.post_id = p.id AND s.taken_at = (SELECT MAX(taken_at) FROM post_stats WHERE post_id = p.id)
        ORDER BY COALESCE(s.views, 0) DESC LIMIT 20""")
    by_platform: dict[str, int] = defaultdict(int)
    for r in results:
        by_platform[r["platform"]] += r["views"] or 0
    return {"results": results, "views_by_platform": {k.replace("_", " ").title().replace("Youtube", "YouTube"): v for k, v in by_platform.items()},
            "chart": chart,
            "claude_since": spend.since_approval(db, "claude"), "claude_limit_usd": sv.app.spend_breaker.claude_usd,
            "quota": {"general": (sv.quota.used("general"), sv.quota.caps["general"]), "search": (sv.quota.used("search"), sv.quota.caps["search"])},
            "jev_since": spend.since_approval(db, "jev"), "jev_limit_usd": sv.app.spend_breaker.jev_usd}


# --- settings --------------------------------------------------------------------------------------------------

def settings_page(sv: Services, p: Profile) -> dict:
    channels = []
    for c in sv.profiles.values():
        channels.append({"id": c.id, "name": channel_name(c), "display": c.display_name, "niche": c.niche.definition,
                         "posting": c.posting, "format": c.format, "handle": c.brand.handle,
                         "recipe": blueprints_summary(c.format) if c.format else None})
    keys = [{"name": "YouTube Data API key", "present": has_secret(secrets.YOUTUBE)},
            {"name": "Jev (TypeSafe) key", "present": has_secret(secrets.JEV)},
            {"name": "Claude (Anthropic) key", "present": has_secret(secrets.CLAUDE)},
            {"name": "Whop API key", "present": has_secret(secrets.WHOP)},
            {"name": "YouTube sign-in client file", "present": posting.YOUTUBE_CLIENT.exists()}]
    s = sv.app.schedule
    ai = engine_ai.settings(sv.db)
    moment_ai = {**ai, "active": engine_ai.active(sv.db), "key": engine_ai.key_stored(),
                 "find_price": engine_ai.PRICES.get(ai["find"]), "judge_price": engine_ai.PRICES.get(ai["judge"])}
    return {"moment_ai": moment_ai, "channels": channels, "key_list": keys, "budgets": sv.app.budgets, "breaker": sv.app.spend_breaker, "engine": engine_card(), "engine_url": clipper.ENGINE_URL, "health": (items := health.checks(sv)), "health_summary": health.summary(items),
            "zone": f"Eastern Time ({timefmt.zone_label()})", "dashboard": f"{sv.app.dashboard.host}:{sv.app.dashboard.port}",
            "schedule": [("Send due posts", s.post_every_minutes)],
            "database": str(sv.db.path)}


# --- search ----------------------------------------------------------------------------------------------------

def search(sv: Services, p: Profile, text: str) -> list[dict]:
    like = f"%{text.strip()}%"
    out = []
    for r in rows(sv.db, "SELECT cr.id, cr.engine_clip_id, cr.hook, cr.state FROM clips_ready cr WHERE cr.profile_id=? AND cr.state NOT IN ('superseded','removed') "
                         "AND (cr.hook LIKE ? OR 'clip ' || cr.engine_clip_id LIKE ?) ORDER BY cr.id DESC LIMIT 5", p.id, like, like):
        out.append({"type": "Clips", "title": (r["hook"] or f"Clip {r['engine_clip_id']}")[:80], "sub": f"Clip {r['engine_clip_id']}",
                    "href": f"/clips?profile={p.id}&panel=/clips/{r['id']}/panel"})
    for r in rows(sv.db, "SELECT id, title, platform FROM posts WHERE profile_id=? AND (title LIKE ? OR caption LIKE ?) ORDER BY id DESC LIMIT 5", p.id, like, like):
        out.append({"type": "Posts", "title": (r["title"] or "Post")[:80], "sub": r["platform"].replace("_", " "),
                    "href": f"/calendar?profile={p.id}&view=list&panel=/calendar/post/{r['id']}"})
    for r in rows(sv.db, "SELECT video_id, title FROM videos WHERE profile_id=? AND title LIKE ? ORDER BY published_at DESC LIMIT 6", p.id, like):
        out.append({"type": "Videos", "title": r["title"][:80], "sub": "YouTube", "href": f"https://www.youtube.com/watch?v={r['video_id']}", "external": True})
    return out
