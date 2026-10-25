"""Campaigns: Content Rewards campaigns that pay per 1,000 views, found, vetted, and clipped for.

What can be automatic and what cannot (Whop's API offers nothing for clippers, so this reads the public pages):
  automatic   read the public campaign list and any campaign you paste a link for; vet each one; keep its budget fresh; find
              the source videos you pointed it at; clip them (the same scoring, clipping and posting as a pasted link, with the
              campaign's own pay, rules and required tags); list each posted clip's link ready for you to submit.
  by hand     joining a campaign, and submitting each clip's link to it. A campaign's own rules and source links are shown to
              members only, so you enter them on the campaign's page once you have joined.
A campaign is skipped, with the reason shown, unless it pays by views, allows YouTube, pays at least the CPM floor ($1.00 per
1,000 views by default) and still has budget. Nothing is clipped for a campaign you have not marked as joined.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

from . import channels, contentrewards, links, store
from .config import Campaign, Posting, Scoring
from .db import Database

SOLVED_ID = "6bdc8e4b-3ebf-44f1-aedb-067589ca9c89"
SOLVED_RULES = {"min_s": 20, "max_s": 60, "face_window_s": 5, "require_captions": True, "no_ad_reads": True}
SOLVED_TAGS = "@solvedclips @iammarkmanson"
MIN_EPISODE_S = 600  # from a channel, only videos at least this long are queued: full episodes, not Shorts or trailers
QUEUE_AHEAD = 5  # videos kept waiting per campaign
DETAIL_FRESH_H = 6
DETAIL_PER_SCAN = 15
STATES = ("recommended", "skipped", "joined", "dropped")


class CampaignError(ValueError):
    """Something about this campaign is wrong. The message is safe to show."""


# ---- storage -----------------------------------------------------------------------------------------------------


def _row(r) -> dict:
    c = dict(r)
    c["platforms"] = json.loads(c.pop("platforms_json") or "[]")
    c["rules"] = json.loads(c["rules_json"]) if c["rules_json"] else None
    return c


def get(db: Database, cid: str) -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM campaigns WHERE id=?", (cid.lower(),)).fetchone()
    return _row(row) if row else None


def all_campaigns(db: Database) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT * FROM campaigns").fetchall()
    order = {"joined": 0, "recommended": 1, "skipped": 2, "dropped": 3}
    return sorted((_row(r) for r in rows), key=lambda x: (order[x["state"]], -(x["rate_cents"] or 0), x["title"].lower()))


def _clean_title(text: str) -> str:
    return text.replace("$$", "$").strip()


def upsert(db: Database, remote: dict, *, state: str | None = None) -> dict:
    """Keep what Content Rewards says about a campaign (from the list or from its own page). A new campaign starts as
    `recommended` or `skipped` (decided by vet); one you joined or dropped keeps your choice."""
    now = store.iso()
    existing = get(db, remote["id"])
    rates = remote.get("rates")  # present when read from the campaign's own page
    platforms = remote.get("platforms") or []
    if rates is not None:
        yt = rates.get("youtube") or {}
        rate, lo, hi = yt.get("rate_cents"), yt.get("min_payout_cents"), yt.get("max_payout_cents")
    else:  # the list only knows the best rate: good enough to decide whether the page is worth reading
        rate = remote.get("best_rate_cents") if "youtube" in platforms else None
        lo = hi = None
    if existing and rates is None and existing.get("detail_at"):
        rate, lo, hi = existing["rate_cents"], existing["min_payout_cents"], existing["max_payout_cents"]  # keep the page's own numbers
    fields = {
        "title": _clean_title(remote.get("title") or (existing or {}).get("title") or remote["id"]),
        "brand": remote.get("brand") or "", "category": remote.get("category") or (existing or {}).get("category") or "",
        "platforms_json": json.dumps(platforms), "type": remote.get("type") or "", "status": remote.get("status") or "",
        "requires_application": int(bool(remote.get("requires_application"))), "description": (remote.get("description") or "")[:2000],
        "rate_cents": rate, "min_payout_cents": lo, "max_payout_cents": hi,
        "budget_total_cents": remote.get("budget_total_cents"), "budget_spent_cents": remote.get("budget_spent_cents"),
        "budget_left_cents": remote.get("budget_left_cents"), "creators": remote.get("creators"), "fetched_at": now,
    }
    if rates is not None:
        fields["detail_at"] = now
    with db.conn() as c:
        if existing:
            c.execute(f"UPDATE campaigns SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), remote["id"]))
        else:
            fields.update(id=remote["id"], url=contentrewards.campaign_url(remote["id"]), state=state or "skipped", first_seen_at=now)
            c.execute(f"INSERT INTO campaigns ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})", tuple(fields.values()))
    return get(db, remote["id"])


def _set(db: Database, cid: str, **fields) -> None:
    with db.conn() as c:
        c.execute(f"UPDATE campaigns SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), cid))


# ---- vetting -------------------------------------------------------------------------------------------------------


def problems(c: dict, rules: Scoring) -> list[str]:
    """Why VilloMillo will not clip for this campaign. Empty means it can."""
    out = []
    if c["status"] and c["status"] != "active":
        out.append(f"it is not taking clips right now ({c['status']})")
    if c["type"] and c["type"] != "cpm":
        out.append(f"it pays from a {c['type']}, not per 1,000 views")
    if "youtube" not in c["platforms"]:
        out.append("it does not allow YouTube, the only place VilloMillo posts")
    elif c["rate_cents"] is None or c["rate_cents"] / 100 < rules.cpm_floor:
        pays = "nothing stated" if c["rate_cents"] is None else f"${c['rate_cents'] / 100:.2f}"
        out.append(f"it pays {pays} per 1,000 views on YouTube, under the ${rules.cpm_floor:.2f} floor")
    left = c["budget_left_cents"]
    if left is not None and left / 100 < rules.campaign_min_budget_usd:
        out.append(f"only ${left / 100:,.0f} of its budget is left")
    return out


def vet(c: dict, rules: Scoring) -> tuple[str, str]:
    """('recommended' | 'skipped', the reason in a sentence) for a campaign you have not joined."""
    bad = problems(c, rules)
    if bad:
        return "skipped", "Skipped: " + "; ".join(bad) + "."
    note = f"Pays ${c['rate_cents'] / 100:.2f} per 1,000 views on YouTube"
    if c["budget_left_cents"] is not None:
        note += f", ${c['budget_left_cents'] / 100:,.0f} of budget left"
    note += "."
    if c["requires_application"]:
        note += " It needs an application: apply on Content Rewards first."
    return "recommended", note


def clippable(c: dict, rules: Scoring) -> tuple[bool, str]:
    """Whether a campaign you joined can be clipped for right now, and if not, why."""
    bad = problems(c, rules)
    return (False, "Paused: " + "; ".join(bad) + ".") if bad else (True, "")


# ---- what you set on a campaign --------------------------------------------------------------------------------------


def parse_sources(text: str) -> list[tuple[str, object]]:
    """Each line is a video link (YouTube, Twitch, Kick) or a YouTube channel (@name, a /@name or /channel/UC... link, or a UC id)."""
    out = []
    for n, line in enumerate((text or "").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        m = (re.search(r"youtube\.com/(@[\w.-]+)", line) or re.fullmatch(r"(@[\w.-]+)", line))
        if m:
            out.append(("channel", {"handle": m.group(1)}))
            continue
        m = re.search(r"youtube\.com/channel/(UC[\w-]{22})", line) or re.fullmatch(r"(UC[\w-]{22})", line)
        if m:
            out.append(("channel", {"channel_id": m.group(1)}))
            continue
        try:
            out.append(("video", links.parse(line)))
        except links.LinkError:
            raise CampaignError(f"line {n} is not a video link or a YouTube channel (such as @name): {line[:60]}") from None
    return out


def save_setup(db: Database, cid: str, *, sources: str, tags: str, min_s=None, max_s=None, face_window_s=None,
               require_captions=False, no_ad_reads=False, use_rules=False) -> None:
    """Sources (where its videos are), required caption tags, and the rules a program can check on a clip."""
    c = get(db, cid)
    if c is None:
        raise LookupError("no such campaign")
    parse_sources(sources)  # refuses a line that is neither
    tags = " ".join((tags or "").split())[:200]
    rules = None
    if use_rules:
        try:
            rules = Campaign(name=c["title"], min_s=float(min_s or 20), max_s=float(max_s or 60), face_window_s=float(face_window_s or 5),
                             require_captions=bool(require_captions), no_ad_reads=bool(no_ad_reads)).model_dump(exclude={"name"})
        except (ValueError, TypeError) as e:
            raise CampaignError(f"the rules do not make sense: {str(e).splitlines()[0][:120]}") from None
    _set(db, c["id"], sources=sources.strip(), tags=tags, rules_json=json.dumps(rules) if rules else None)


def rules_for(c: dict) -> Campaign | None:
    return Campaign(name=c["title"], **c["rules"]) if c and c.get("rules") else None


def posting_for(db: Database, posting: Posting, platform: str, campaign_id: str | None) -> Posting:
    """The channel's posting settings with this campaign's required tags in place of the channel's own, for its clips."""
    c = get(db, campaign_id) if campaign_id else None
    if not c or not c["tags"] or platform != "youtube_shorts":
        return posting
    return posting.model_copy(update={"tags": {**posting.tags, platform: c["tags"]}})


def mark_joined(db: Database, cid: str) -> None:
    c = get(db, cid)
    if c is None:
        raise LookupError("no such campaign")
    _set(db, c["id"], state="joined", joined_at=c["joined_at"] or store.iso(), reason=None)


def drop(db: Database, cid: str) -> None:
    if get(db, cid) is None:
        raise LookupError("no such campaign")
    _set(db, cid.lower(), state="dropped", reason="You stopped clipping for this campaign.")


# ---- reading Content Rewards ------------------------------------------------------------------------------------------


def revet(db: Database, c: dict, rules: Scoring) -> dict:
    if c["state"] in ("recommended", "skipped"):
        state, reason = vet(c, rules)
        _set(db, c["id"], state=state, reason=reason)
    elif c["state"] == "joined":
        ok, why = clippable(c, rules)
        _set(db, c["id"], reason=None if ok else why)
    return get(db, c["id"])


def refresh_detail(db: Database, cid: str, rules: Scoring, client=None) -> dict:
    """Read the campaign's own page (its true YouTube pay and budget) and decide again."""
    detail = contentrewards.fetch_detail(cid, client)
    return revet(db, upsert(db, detail), rules)


def add_by_link(db: Database, text: str, rules: Scoring, client=None) -> dict:
    """You pasted a campaign link: read that page and add the campaign."""
    cid = contentrewards.campaign_id(text)
    if cid is None:
        raise CampaignError("paste a Content Rewards campaign link, like contentrewards.com/c/campaigns/...")
    try:
        return refresh_detail(db, cid, rules, client)
    except contentrewards.CampaignPageError as e:
        raise CampaignError(str(e)) from None


def seed_solved(db: Database, rules: Scoring) -> None:
    """The Solved campaign you joined by hand is the first record, with the rules and tags its brief set. Once only."""
    with db.conn() as c:
        done = c.execute("SELECT 1 FROM meta WHERE key='campaigns_seeded'").fetchone()
        if done:
            return
        c.execute("INSERT INTO meta (key, value) VALUES ('campaigns_seeded', '1')")
    if get(db, SOLVED_ID):
        return
    now = store.iso()
    with db.conn() as c:
        c.execute("INSERT INTO campaigns (id, title, brand, category, platforms_json, type, status, state, reason, rules_json, tags, sources, url, "
                  "first_seen_at, fetched_at, joined_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (SOLVED_ID, "ClipFarm x Solved w/ Mark Manson", "Clip Farm", "Podcasts", json.dumps(["youtube"]), "cpm", "active", "joined",
                   None, json.dumps(SOLVED_RULES), SOLVED_TAGS, "", contentrewards.campaign_url(SOLVED_ID), now, now, now))


def scan(db: Database, rules: Scoring, client=None) -> list[str]:
    """Read the public list, keep every campaign on it, and read the pages of the ones worth a closer look."""
    lines = []
    listed = contentrewards.fetch_list(client)
    if not listed:
        raise contentrewards.CampaignPageError("the Discover page had no campaigns on it: its layout may have changed")
    for remote in listed:
        c = upsert(db, remote)
        revet(db, c, rules)
    lines.append(f"{len(listed)} campaigns on the public list")
    stale = store.iso(store.now() - timedelta(hours=DETAIL_FRESH_H))
    todo = [c for c in all_campaigns(db) if c["state"] in ("recommended", "joined") and (not c["detail_at"] or c["detail_at"] < stale)]
    for c in todo[:DETAIL_PER_SCAN]:
        try:
            refresh_detail(db, c["id"], rules, client)
        except contentrewards.CampaignPageError as e:
            lines.append(f"{c['title']}: {e}")
    return lines


# ---- the videos to clip ------------------------------------------------------------------------------------------------


def _queued(db: Database, cid: str) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM campaign_queue WHERE campaign_id=? AND run_id IS NULL", (cid,)).fetchone()["n"]


def fill_queue(db: Database, c: dict, yt) -> list[str]:
    """Top up the campaign's queue from the sources you gave it: video links as they are, channels by their newest full episodes."""
    lines = []
    room = QUEUE_AHEAD - _queued(db, c["id"])
    for kind, value in parse_sources(c["sources"]):
        if room <= 0:
            break
        found: list[tuple[str, str, str]] = []  # (video id, url, title)
        if kind == "video":
            found.append((value.video_id, value.url, ""))
        elif yt is not None:
            channel = yt.channel(**value)
            ids = yt.recent_upload_ids(channel["uploads_playlist"], limit=15)
            for v in yt.videos(ids):
                if v.get("duration_s", 0) >= MIN_EPISODE_S and (v.get("live_status") or "none") == "none":
                    found.append((v["video_id"], f"https://www.youtube.com/watch?v={v['video_id']}", v.get("title", "")))
        for video_id, url, title in found:
            if room <= 0:
                break
            with db.conn() as conn:
                made = conn.execute("INSERT OR IGNORE INTO campaign_queue (campaign_id, video_id, url, title, created_at) VALUES (?,?,?,?,?)",
                                    (c["id"], video_id, url, title, store.iso())).rowcount
            if made:
                room -= 1
                lines.append(f"{c['title']}: queued {title or video_id}")
    return lines


def queue_of(db: Database, cid: str) -> list[dict]:
    """The campaign's queue with where each video stands (from the run that handles it)."""
    with db.conn() as c:
        rows = c.execute("SELECT q.*, r.state AS run_state, r.note AS run_note FROM campaign_queue q LEFT JOIN runs r ON r.id=q.run_id "
                         "WHERE q.campaign_id=? ORDER BY q.id DESC LIMIT 30", (cid,)).fetchall()
    return [dict(r) for r in rows]


def active_runs(db: Database) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM runs WHERE campaign_id IS NOT NULL AND state IN ('analysing','planned','making')").fetchone()["n"]


def start_next(sv, client=None) -> list[str]:
    """Start the next queued video of a joined campaign, if none is being worked on. One at a time keeps spending steady."""
    from . import runs

    db, rules = sv.db, sv.app.scoring
    if active_runs(db):
        return []
    for c in all_campaigns(db):
        if c["state"] != "joined":
            continue
        with db.conn() as conn:
            item = conn.execute("SELECT * FROM campaign_queue WHERE campaign_id=? AND run_id IS NULL ORDER BY id LIMIT 1", (c["id"],)).fetchone()
        if item is None:
            continue
        try:  # the budget is checked again right before each video
            c = refresh_detail(db, c["id"], rules, client)
        except contentrewards.CampaignPageError:
            pass  # use what was read last
        ok, why = clippable(c, rules)
        if not ok:
            continue
        channel = channels.for_category(sv.profiles, c["category"]) or next(iter(sv.profiles.values()))
        run_id = runs.create(sv, channel.id, item["url"], source="campaign", campaign_id=c["id"])
        with db.conn() as conn:
            conn.execute("UPDATE campaign_queue SET run_id=? WHERE id=?", (run_id, item["id"]))
        return [f"{c['title']}: started {item['title'] or item['video_id']}"]
    return []


def work(sv, client=None) -> list[str]:
    """One pass: read Content Rewards, keep the queues full, and start the next video."""
    db, rules = sv.db, sv.app.scoring
    seed_solved(db, rules)
    lines = []
    try:
        lines += scan(db, rules, client)
    except contentrewards.CampaignPageError as e:
        lines.append(f"Content Rewards could not be read: {e}")
    for c in all_campaigns(db):
        if c["state"] == "joined" and clippable(c, rules)[0] and c["sources"].strip():
            try:
                lines += fill_queue(db, c, sv.yt)
            except Exception as e:  # a bad channel name or a YouTube hiccup stops this campaign's top-up, not the pass
                lines.append(f"{c['title']}: could not look for videos ({type(e).__name__}: {str(e)[:120]})")
    lines += start_next(sv, client)
    return lines


# ---- links to hand in ----------------------------------------------------------------------------------------------------


def ready_to_submit(db: Database) -> list[dict]:
    """Posted clips made for a campaign whose link you have not handed in yet, newest first."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT p.id AS post_id, p.url, p.platform, p.posted_at, cr.hook, cr.campaign_id, cm.title AS campaign_title, cm.url AS campaign_url "
            "FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id JOIN campaigns cm ON cm.id=cr.campaign_id "
            "WHERE p.status IN ('uploaded','posted') AND cr.state!='removed' AND p.url IS NOT NULL AND p.id NOT IN (SELECT post_id FROM campaign_submissions) "
            "ORDER BY p.id DESC").fetchall()
    return [dict(r) for r in rows]


def mark_submitted(db: Database, post_id: int) -> None:
    with db.conn() as c:
        row = c.execute("SELECT cr.campaign_id FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id WHERE p.id=? AND cr.campaign_id IS NOT NULL",
                        (post_id,)).fetchone()
        if row is None:
            raise LookupError("that post was not made for a campaign")
        c.execute("INSERT OR IGNORE INTO campaign_submissions (post_id, campaign_id, submitted_at) VALUES (?,?,?)",
                  (post_id, row["campaign_id"], store.iso()))
