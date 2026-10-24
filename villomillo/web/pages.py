"""Data for each dashboard page.

Everything here reads the database and returns plain dicts and lists for the templates. Nothing writes, nothing
calls a paid service. Times are shown in Eastern Time (see villomillo/timefmt.py).
"""

from __future__ import annotations

import json
import pathlib
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import httpx
import keyring

from .. import accounts, clipper, learning, posting, secrets, store, studio, timefmt
from ..config import ROOT, Profile
from ..db import Database
from ..services import Services

STATIC = pathlib.Path(__file__).parent / "static"
GATES = ("all", "pass", "escalate", "fail")
PAGE_SIZE = 50


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
    "superseded": badge("Replaced", ""),
}


def post_state(status: str, route: str) -> dict:
    """How a post reads everywhere: badge kind and text, the calendar colour class, and what it is called."""
    table = {
        "uploaded": ("Uploaded", "good", "s-done"), "posted": ("Posted", "good", "s-done"),
        "manual_ready": ("To post by hand", "warn", "s-hand"), "failed": ("Failed", "bad", "s-fail"),
        "blocked": ("Blocked", "bad", "s-fail"), "dry_run": ("Dry run", "", "s-plan"),
    }
    if status == "queued":
        text, kind, css = ("Planned, by hand", "warn", "s-hand") if route == "manual" else ("Planned", "", "s-plan")
    else:
        text, kind, css = table.get(status, (status, "", "s-plan"))
    return {"text": text, "kind": kind, "css": css, "key": status if status != "queued" else f"queued-{route}"}


# --- the shell: side panel, top bar, bell ------------------------------------------------------------------------

_engine_seen: tuple[float, bool] = (0.0, False)


def engine_up() -> bool:
    """Whether the clipping engine answers. Remembered for a few seconds so pages stay quick."""
    global _engine_seen
    if time.monotonic() - _engine_seen[0] < 5:
        return _engine_seen[1]
    try:
        up = httpx.get(f"{clipper.ENGINE_URL}/jobs", params={"limit": 1}, timeout=0.6).status_code == 200
    except httpx.HTTPError:
        up = False
    _engine_seen = (time.monotonic(), up)
    return up


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
                        "btn": "Accounts", "kind": "bad", "primary": False, "href": f"/accounts?profile={p.id}"})
    held = one(db, "SELECT COUNT(*) AS n FROM clips_ready WHERE profile_id=? AND state='approved' AND reason LIKE 'approved, but not posted%'", p.id)
    if held["n"]:
        out.append({"title": f"{held['n']} approved clip{'s' if held['n'] != 1 else ''} fit no channel", "sub": "Not posted: see the reason on the clip",
                    "btn": "Open clips", "kind": "warn", "primary": False, "href": f"/clips?profile={p.id}&tab=approved"})
    if p.posting and not p.posting.dry_run:
        for platform, route in p.posting.platforms.items():
            if route == "api" and not accounts.audit_passed(db, p.id, platform):
                out.append({"title": f"{platform.replace('_', ' ').title()} audit has not passed",
                            "sub": "Posts stay manual until you record the audit as passed", "btn": "Accounts", "kind": "warn",
                            "primary": False, "href": f"/accounts?profile={p.id}"})
    if not engine_up():
        out.append({"title": "Clip engine is off", "sub": "Start it to make more clips from the campaign episodes",
                    "btn": "Settings", "kind": "neutral", "primary": False, "href": f"/settings?profile={p.id}"})
    return out


def shell(sv: Services, p: Profile, path: str, active: str) -> dict:
    """What every page needs around its own content."""
    to_do = needs(sv, p)
    waiting = one(sv.db, "SELECT COUNT(*) AS n FROM clips_ready WHERE profile_id=? AND state='review'", p.id)["n"]
    paused = any(r["status"] == "paused" for r in accounts.overview(sv.db, sv.profiles) if r["profile"] == p.id)
    q = f"?profile={p.id}"
    up = engine_up()
    claude_m, jev_m = store.ai_spend_month(sv.db, "claude"), store.ai_spend_month(sv.db, "jev")
    return {
        "current": p, "path": path, "channel_name": channel_name(p), "channel_sub": p.display_name if p.brand.name else "",
        "channels": [{"id": c.id, "name": channel_name(c), "sub": c.display_name if c.brand.name else ""} for c in sv.profiles.values()],
        "nav": [
            {"label": "Find", "links": [
                {"key": "home", "label": "Home", "href": f"/{q}", "icon": "home"},
                {"key": "research", "label": "Research", "href": f"/research{q}", "icon": "search"}]},
            {"label": "Make", "links": [
                {"key": "clips", "label": "Clips", "href": f"/clips{q}", "icon": "clips", "pill": waiting or None}]},
            {"label": "Post", "links": [
                {"key": "calendar", "label": "Calendar", "href": f"/calendar{q}", "icon": "calendar"},
                {"key": "accounts", "label": "Accounts", "href": f"/accounts{q}", "icon": "users",
                 "dot": "An account is paused" if paused else None}]},
            {"label": "Learn", "links": [{"key": "insights", "label": "Insights", "href": f"/insights{q}", "icon": "chart"}]},
        ],
        "engine": {"kind": "good" if up else "", "text": "Clip engine is on" if up else "Clip engine is off",
                   "title": f"The clipping engine at {clipper.ENGINE_URL} {'answers' if up else 'is not running'}"},
        "budget": {"month": money(claude_m + jev_m),
                   "title": f"Claude {money(claude_m)} of {money(sv.app.budgets.claude_usd_per_month)} this month; "
                            f"Jev {money(store.ai_spend_today(sv.db, 'jev'))} of {money(sv.app.budgets.jev_usd_per_day)} today"},
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
    }


def clips_page(sv: Services, p: Profile, tab: str) -> dict:
    db = sv.db
    found = rows(db, "SELECT * FROM clips_ready WHERE profile_id=? AND state!='superseded' ORDER BY id DESC", p.id)
    posts = {r["clip_id"]: r for r in rows(db, "SELECT * FROM posts WHERE profile_id=? ORDER BY id", p.id)}
    cards = [_clip_card(p, r, posts.get(r["id"])) for r in found]
    jobs_active = one(db, "SELECT COUNT(*) AS n FROM clip_jobs WHERE profile_id=? AND status IN ('submitted','running','queued')", p.id)["n"]
    groups = {
        "all": lambda c: True, "review": lambda c: c["key"] == "review", "approved": lambda c: c["key"] == "approved",
        "rejected": lambda c: c["key"] in ("rejected", "rejected_auto", "failed_validation"),
    }
    counts = {k: sum(1 for c in cards if f(c)) for k, f in groups.items()}
    counts["progress"] = jobs_active
    tabs = [{"key": k, "label": label, "count": counts[k]} for k, label in
            (("all", "All"), ("review", "Waiting for you"), ("approved", "Approved"), ("rejected", "Rejected"), ("progress", "In progress"))]
    tab = tab if tab in counts else "all"
    studio_data = studio.build(db, clipper.Engine().log, p.id) if tab == "progress" else None
    return {"tab": tab, "tabs": tabs, "cards": [c for c in cards if groups.get(tab, lambda _: False)(c)],
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
            "decided": r["state"] != "review", "reason": r["reason"]}


# --- calendar --------------------------------------------------------------------------------------------------

def _post_view(r: dict) -> dict:
    s = post_state(r["status"], r["route"])
    moment = r["posted_at"] if r["status"] in ("uploaded", "posted") and r["posted_at"] else r["scheduled_at"]
    et = timefmt.to_et(moment)
    caption = r["caption"] or ""
    return {**r, "state": s, "date": et.date(), "time": timefmt.clock(moment), "platform_name": r["platform"].replace("_", " ").title(),
            "mark": "YT" if r["platform"].startswith("youtube") else "IG" if r["platform"].startswith("instagram") else r["platform"][:2].upper(),
            "short": (r["title"] or caption.split("\n")[0] or "Clip")[:48], "when": timefmt.full(moment),
            "panel": f"/calendar/post/{r['id']}?profile={r['profile_id']}"}


def calendar_page(sv: Services, p: Profile, view: str, ref: date | None) -> dict:
    today = timefmt.today_et()
    ref = ref or today
    posts = [_post_view(r) for r in rows(sv.db, "SELECT po.* FROM posts po WHERE po.profile_id=? ORDER BY po.scheduled_at", p.id)]
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
                         "more": max(0, len(by_day.get(d, [])) - 3), "month_label": d.strftime("%b") if d.day == 1 else ""} for d in days]
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
            "clip": _clip_card(p, clip, r) if clip else None, "can_edit": r["status"] not in ("uploaded", "posted"),
            "is_hand": r["status"] in ("manual_ready", "queued") and r["route"] == "manual",
            "file_url": f"/clips/{r['clip_id']}/file?profile={p.id}"}


# --- home ------------------------------------------------------------------------------------------------------

def home_page(sv: Services, p: Profile) -> dict:
    db = sv.db
    by_state = {r["state"]: r["n"] for r in rows(db, "SELECT state, COUNT(*) AS n FROM clips_ready WHERE profile_id=? GROUP BY state", p.id)}
    made = sum(n for s, n in by_state.items() if s != "superseded")
    passed = made - by_state.get("failed_validation", 0) - by_state.get("rejected_auto", 0)
    posts = rows(db, "SELECT status, route FROM posts WHERE profile_id=?", p.id)
    hand = sum(1 for x in posts if x["route"] == "manual" and x["status"] in ("queued", "manual_ready"))
    posted = sum(1 for x in posts if x["status"] in ("uploaded", "posted"))
    listed = one(db, "SELECT COUNT(*) AS n FROM videos WHERE profile_id=?", p.id)["n"]
    clipped = one(db, "SELECT COUNT(*) AS n FROM clip_jobs WHERE profile_id=? AND status='done'", p.id)["n"]
    claude_m, jev_m = store.ai_spend_month(db, "claude"), store.ai_spend_month(db, "jev")
    cap = sv.app.budgets.claude_usd_per_month
    waiting = by_state.get("review", 0)
    stats = [
        {"k": "Posts waiting for you", "v": hand, "s": "to post by hand" if hand else "nothing to post by hand"},
        {"k": "Clips waiting for review", "v": waiting, "s": "passed every check" if waiting else "nothing to review"},
        {"k": "Clips approved", "v": by_state.get("approved", 0), "s": f"of {made} made"},
        {"k": "AI spend this month", "v": money(claude_m + jev_m), "s": f"Claude {money(claude_m)} of {money(cap)}, Jev {money(jev_m)}",
         "bar": min(100, round(100 * claude_m / cap)) if cap else 0},
    ]
    funnel = [
        ("Videos found", listed, listed), ("Videos clipped", clipped, listed),
        ("Clips made", made, made), ("Passed every check", passed, made), ("Approved by you", by_state.get("approved", 0), made),
        ("Posted", posted, made)]
    pipeline = [{"label": a, "count": n, "width": max(2 if n else 0, round(100 * n / top)) if top else 0} for a, n, top in funnel]
    recent = [_clip_card(p, r, None) for r in rows(db, "SELECT * FROM clips_ready WHERE profile_id=? AND state!='superseded' ORDER BY id DESC LIMIT 5", p.id)]
    today = timefmt.today_et()
    mine = [_post_view(r) for r in rows(db, "SELECT * FROM posts WHERE profile_id=?", p.id)]
    week = []
    for i in range(7):
        d = today + timedelta(days=i)
        day_posts = [x for x in mine if x["date"] == d]
        week.append({"date": d, "is_today": i == 0, "hand": sum(1 for x in day_posts if x["state"]["css"] == "s-hand"),
                     "done": sum(1 for x in day_posts if x["state"]["css"] == "s-done"),
                     "other": sum(1 for x in day_posts if x["state"]["css"] not in ("s-hand", "s-done")), "total": len(day_posts)})
    health = [{"kind": "good" if engine_up() else "", "label": "Clip engine", "value": "On" if engine_up() else "Off"}]
    if p.posting:
        health.append({"kind": "good" if youtube_connected(p) else "warn", "label": "YouTube",
                       "value": f"Connected as {channel_name(p)} ({p.brand.handle})" if youtube_connected(p) else "Not connected yet"})
        for platform, route in p.posting.platforms.items():
            if route == "api" or platform == "youtube_shorts":
                ok = accounts.audit_passed(db, p.id, platform)
                health.append({"kind": "good" if ok else "warn", "label": f"{platform.replace('_', ' ').title()} audit",
                               "value": "Passed" if ok else "Not passed, so public posts stay manual"})
        health.append({"kind": "good" if has_secret(f"{secrets.INSTAGRAM}_{p.id}", secrets.INSTAGRAM) else "", "label": "Instagram",
                       "value": "Connected" if has_secret(f"{secrets.INSTAGRAM}_{p.id}", secrets.INSTAGRAM) else "Not connected yet"})
    paused = [r for r in accounts.overview(db, sv.profiles) if r["profile"] == p.id and r["status"] == "paused"]
    health.append({"kind": "bad" if paused else "good", "label": "Safety stops", "value": f"{len(paused)} paused" if paused else "No account is paused"})
    health.append({"kind": "good" if claude_m < cap * 0.8 else "warn", "label": "AI budget", "value": "Well inside the caps" if claude_m < cap * 0.8 else "Close to the cap"})
    events = [{"when": timefmt.short(e["ts"]), "level": e["level"], "text": f"{e['component']}: {e['message']}"}
              for e in rows(db, "SELECT * FROM events ORDER BY id DESC LIMIT 8")]
    return {"stats": stats, "pipeline": pipeline, "recent": recent, "week": week, "health": health, "events": events,
            "has_week_items": any(d["total"] for d in week), "needs": needs(sv, p)}


# --- research --------------------------------------------------------------------------------------------------

def _age(published_at: str) -> str:
    hours = (datetime.now(timezone.utc) - timefmt.parse(published_at)).total_seconds() / 3600
    return f"{int(hours)} h" if hours < 48 else f"{int(hours // 24)} d"


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


def research_page(sv: Services, p: Profile, gate: str, query: str, page: int) -> dict:
    everything = store.ranked(sv.db, p.id, limit=500)
    gate = gate if gate in GATES else "all"
    counts = {g: sum(1 for r in everything if g == "all" or r["gate"] == g) for g in GATES}
    needle = query.strip().lower()
    chosen = [r for r in everything if (gate == "all" or r["gate"] == gate)
              and (not needle or needle in (r["title"] or "").lower() or needle in (r["channel_title"] or "").lower())]
    start = max(0, page - 1) * PAGE_SIZE
    shown = []
    for r in chosen[start: start + PAGE_SIZE]:
        comps = json.loads(r["components_json"])
        feats = comps.pop("_features", {})
        comps.pop("_risk", None)
        reasons = [f"{k} {v:.0%}" for k, v in comps.items()]
        if feats.get("outlier_ratio") is not None:
            reasons.insert(0, f"views x{feats['outlier_ratio']:.2f} of usual")
        shown.append({**r, "age": _age(r["published_at"]), "reasons": reasons, "flags": json.loads(r["flags_json"]),
                      "thumb": f"https://i.ytimg.com/vi/{r['video_id']}/mqdefault.jpg", "url": f"https://www.youtube.com/watch?v={r['video_id']}",
                      "minutes": round((r["duration_s"] or 0) / 60), "views_text": f"{(r['views'] or 0):,}",
                      "gate_badge": badge(r["gate"], {"pass": "good", "escalate": "warn", "fail": "bad"}.get(r["gate"], "")),
                      "certainty_text": f"{r['certainty'] * 100:.0f}%"})
    return {"rows": shown, "gate": gate, "gates": [{"key": g, "count": counts[g]} for g in GATES], "q": query, "page": page,
            "pages": max(1, -(-len(chosen) // PAGE_SIZE)), "total": len(chosen), "selfcheck": store.last_selfcheck(sv.db, p.id)}


# --- accounts --------------------------------------------------------------------------------------------------

def accounts_page(sv: Services, p: Profile) -> dict:
    out = []
    for r in accounts.overview(sv.db, sv.profiles):
        owner = sv.profiles[r["profile"]]
        yt = r["platform"].startswith("youtube")
        connected = youtube_connected(owner) if yt else has_secret(f"{secrets.INSTAGRAM}_{owner.id}", secrets.INSTAGRAM)
        out.append({**r, "platform_name": r["platform"].replace("_", " ").title(), "mark": "YT" if yt else "IG", "connected": connected,
                    "channel": channel_name(owner), "handle": owner.brand.handle,
                    "connect_hint": (f"python -m villomillo youtube-connect --profile {owner.id}" if yt else
                                     "Needs a Meta developer app, an access token and a public link to each clip file"),
                    "dry_run": owner.posting.dry_run if owner.posting else True,
                    "mine": r["profile"] == p.id})
    return {"accounts": out}


# --- insights --------------------------------------------------------------------------------------------------

def insights_page(sv: Services, p: Profile) -> dict:
    db = sv.db
    rows_ = store.ranked(db, p.id, limit=500)
    preview = learning.shortlist_preview([dict(r, found_by=r["found_by"]) for r in rows_ if r["gate"] in ("pass", "escalate")], n=10)
    last = one(db, "SELECT created_at, body_json FROM suggestions WHERE profile_id=? ORDER BY id DESC LIMIT 1", p.id)
    spend = rows(db, "SELECT day, engine, ROUND(SUM(cost_usd), 4) AS usd FROM ai_usage GROUP BY day, engine ORDER BY day DESC LIMIT 28")
    by_day: dict[str, float] = defaultdict(float)
    for r in spend:
        by_day[r["day"]] += r["usd"]
    days = sorted(by_day)[-14:]
    peak = max((by_day[d] for d in days), default=0) or 1
    chart = [{"label": d[5:], "usd": by_day[d], "height": max(2, round(100 * by_day[d] / peak)), "text": money(by_day[d])} for d in days]
    return {"selfcheck": store.last_selfcheck(db, p.id), "calibration": learning.calibration(db, p.id), "length": learning.length_status(db, p.id),
            "preview": preview, "review": json.loads(last["body_json"]) if last else None,
            "review_at": timefmt.full(last["created_at"]) if last else None, "chart": chart,
            "claude_today": store.ai_requests_today(db, "claude"), "claude_limit": sv.app.budgets.claude_escalations_per_day,
            "claude_month": store.ai_spend_month(db, "claude"), "claude_budget": sv.app.budgets.claude_usd_per_month,
            "quota": {"general": (sv.quota.used("general"), sv.quota.caps["general"]), "search": (sv.quota.used("search"), sv.quota.caps["search"])},
            "jev_today": store.ai_spend_today(db, "jev"), "jev_cap": sv.app.budgets.jev_usd_per_day}


# --- settings --------------------------------------------------------------------------------------------------

def settings_page(sv: Services, p: Profile) -> dict:
    channels = []
    for c in sv.profiles.values():
        channels.append({"id": c.id, "name": channel_name(c), "display": c.display_name, "niche": c.niche.definition,
                         "campaign": c.campaign, "posting": c.posting, "format": c.format, "handle": c.brand.handle,
                         "watched": len(c.research.watch_channels), "topics": len(c.research.topics), "min_score": c.grading.min_score,
                         "recipe": blueprints_summary(c.format) if c.format else None})
    keys = [{"name": "YouTube Data API key", "present": has_secret(secrets.YOUTUBE)},
            {"name": "Jev (TypeSafe) key", "present": has_secret(secrets.JEV)},
            {"name": "Claude (Anthropic) key", "present": has_secret(secrets.CLAUDE)},
            {"name": "Instagram token", "present": has_secret(secrets.INSTAGRAM, *[f"{secrets.INSTAGRAM}_{c}" for c in sv.profiles])},
            {"name": "YouTube sign-in client file", "present": posting.YOUTUBE_CLIENT.exists()}]
    s = sv.app.schedule
    return {"channels": channels, "key_list": keys, "budgets": sv.app.budgets, "engine_up": engine_up(), "engine_url": clipper.ENGINE_URL,
            "zone": f"Eastern Time ({timefmt.zone_label()})", "dashboard": f"{sv.app.dashboard.host}:{sv.app.dashboard.port}",
            "schedule": [("Look for new uploads", s.watch_every_minutes), ("Refresh view counts", s.snapshots_every_minutes),
                         ("Grade new videos", s.grade_every_minutes), ("Send due posts", s.post_every_minutes),
                         ("Keyword searches", s.topics_every_minutes), ("Weekly Claude review", s.review_every_minutes)],
            "database": str(sv.db.path)}


# --- search ----------------------------------------------------------------------------------------------------

def search(sv: Services, p: Profile, text: str) -> list[dict]:
    like = f"%{text.strip()}%"
    out = []
    for r in rows(sv.db, "SELECT cr.id, cr.engine_clip_id, cr.hook, cr.state FROM clips_ready cr WHERE cr.profile_id=? AND cr.state!='superseded' "
                         "AND (cr.hook LIKE ? OR 'clip ' || cr.engine_clip_id LIKE ?) ORDER BY cr.id DESC LIMIT 5", p.id, like, like):
        out.append({"type": "Clips", "title": (r["hook"] or f"Clip {r['engine_clip_id']}")[:80], "sub": f"Clip {r['engine_clip_id']}",
                    "href": f"/clips?profile={p.id}&panel=/clips/{r['id']}/panel"})
    for r in rows(sv.db, "SELECT id, title, platform FROM posts WHERE profile_id=? AND (title LIKE ? OR caption LIKE ?) ORDER BY id DESC LIMIT 5", p.id, like, like):
        out.append({"type": "Posts", "title": (r["title"] or "Post")[:80], "sub": r["platform"].replace("_", " "),
                    "href": f"/calendar?profile={p.id}&view=list&panel=/calendar/post/{r['id']}"})
    for r in rows(sv.db, "SELECT video_id, title FROM videos WHERE profile_id=? AND title LIKE ? ORDER BY published_at DESC LIMIT 6", p.id, like):
        out.append({"type": "Videos", "title": r["title"][:80], "sub": "YouTube", "href": f"https://www.youtube.com/watch?v={r['video_id']}", "external": True})
    return out
