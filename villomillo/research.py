"""Research: finds videos worth clipping with nobody pasting anything, and sends them down the same road as a pasted link.

What it looks at, cheapest first:
  watched channels   channels you list (YouTube, Twitch or Kick) and YouTube channels it learned from a Creative Commons
                     hit. Their newest videos come from public feeds: no YouTube search, and only a cheap lookup for the
                     licence and length of a YouTube video.
  YouTube search     a few searches per pass, for Creative Commons videos only, because search is rationed. Searches rotate
                     through your topics so each one is covered in turn.
YouTube videos must be Creative Commons, and the caption of every clip made from one carries the credit. Twitch and Kick
videos come only from channels you listed yourself.

A found video is a candidate. The best waiting candidate is started when nothing else is being clipped, inside a daily limit, and
then it is a run like a pasted link: look at it, score every moment, and make the clips only if one clears the viral floor and the
rising CPM bar and there is a channel it fits. Research has no Start button: the scores are the decision.
Everything it looked at, and why it said yes or no, is on the Research page.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from . import activity, campaigns, channels, claims, links, store
from .config import Research, Scoring
from .db import Database
from .quota import QuotaExceeded
from .youtube import YouTubeError

KEY = "research"
BAR_KEY = "research_bar"
LEARNED_MAX = 30  # channels Research may add to its own watch list
WATCH_PER_PASS = 6  # watched channels read per pass, the longest-unchecked first
NEWEST_PER_CHANNEL = 5
SKIPPED_KEEP_DAYS = 14
ACTIVE = ("analysing", "planned", "making")


class ResearchError(ValueError):
    """Something about this setting is wrong. The message is safe to show."""


# ---- settings: the switch, the daily limit, the highest bar, the topics ------------------------------------------------


def _saved(db: Database) -> dict:
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    return json.loads(row["value"]) if row else {}


def settings(db: Database, cfg: Research) -> dict:
    saved = _saved(db)
    return {"on": bool(saved.get("on", False)), "videos_per_day": int(saved.get("videos_per_day", cfg.videos_per_day)),
            "bar_max": float(saved.get("bar_max", cfg.bar_max)), "topics": list(saved.get("topics", []))}


def is_on(db: Database) -> bool:
    return bool(_saved(db).get("on", False))


def save_settings(db: Database, cfg: Research, rules: Scoring, *, on: bool | None = None, videos_per_day=None, bar_max=None,
                  topics: str | None = None) -> None:
    """Change any of the switch, the daily limit, the highest the bar may rise to, and the topics searched (one per line)."""
    saved = _saved(db)
    if on is not None:
        saved["on"] = bool(on)
    try:
        if videos_per_day not in (None, ""):
            n = int(videos_per_day)
            if not 1 <= n <= 100:
                raise ResearchError("videos per day must be between 1 and 100")
            saved["videos_per_day"] = n
        if bar_max not in (None, ""):
            top = float(bar_max)
            if top < rules.cpm_floor:
                raise ResearchError(f"the highest bar cannot be under the ${rules.cpm_floor:.2f} floor")
            saved["bar_max"] = round(top, 2)
    except (TypeError, ValueError):
        raise ResearchError("the daily limit and the highest bar must be numbers") from None
    if topics is not None:
        lines = [" ".join(t.split())[:60] for t in topics.splitlines() if t.strip()]
        saved["topics"] = list(dict.fromkeys(lines))[:10]
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(saved)))
    store.event(db, "info", "research", "Research is " + ("on" if saved.get("on") else "off"))


def topics(db: Database, cfg: Research, profiles: dict) -> list[str]:
    """What it searches YouTube for: your own list, or by default the category of each channel."""
    own = settings(db, cfg)["topics"]
    if own:
        return own
    return list(dict.fromkeys(p.category.lower() for p in profiles.values() if p.category))


def bar(db: Database, rules: Scoring, cfg: Research) -> float:
    """The CPM a Research clip must clear. It starts at the pasted-clip floor and only ever moves up (Phase 9 moves it, when real
    results beat the predictions), never above the highest you allow."""
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (BAR_KEY,)).fetchone()
    value = float(row["value"]) if row else rules.cpm_floor
    top = max(settings(db, cfg)["bar_max"], rules.cpm_floor)
    return round(min(max(value, rules.cpm_floor), top), 2)


def set_bar(db: Database, value: float) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (BAR_KEY, str(value)))


# ---- the channels it watches ---------------------------------------------------------------------------------------------


def sources(db: Database) -> list[dict]:
    with db.conn() as c:
        rows = c.execute("SELECT * FROM research_sources ORDER BY learned, platform, name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def add_source(db: Database, platform: str, text: str, resolver=None) -> dict:
    """Watch a channel. `text` is a link, an @handle or a name. The channel is looked up so a wrong name is refused now."""
    from sources import channel_feed

    try:
        found = (resolver or channel_feed.resolve)(platform, text)
    except ValueError as e:
        raise ResearchError(str(e)) from None
    with db.conn() as c:
        c.execute("INSERT INTO research_sources (platform, channel_key, name, learned, added_at) VALUES (?,?,?,0,?) "
                  "ON CONFLICT(platform, channel_key) DO UPDATE SET learned=0, name=excluded.name",
                  (found.platform, found.channel_key, found.name, store.iso()))
        return dict(c.execute("SELECT * FROM research_sources WHERE platform=? AND channel_key=?", (found.platform, found.channel_key)).fetchone())


def remove_source(db: Database, source_id: int) -> None:
    with db.conn() as c:
        c.execute("DELETE FROM research_sources WHERE id=?", (source_id,))


def _learn_channel(db: Database, platform: str, key: str, name: str) -> bool:
    """Add a channel Research found to its watch list (marked "added by Research"). False when it is already there or the list is full."""
    with db.conn() as c:
        if c.execute("SELECT 1 FROM research_sources WHERE platform=? AND channel_key=?", (platform, key)).fetchone():
            return False
        if c.execute("SELECT COUNT(*) AS n FROM research_sources WHERE learned=1").fetchone()["n"] >= LEARNED_MAX:
            return False
        c.execute("INSERT INTO research_sources (platform, channel_key, name, learned, added_at) VALUES (?,?,?,1,?)", (platform, key, name, store.iso()))
    return True


def _learn(db: Database, v: dict) -> None:
    """A Creative Commons video that was worth looking at: watch its channel too, which is much cheaper than searching."""
    if v.get("channel_id"):
        _learn_channel(db, "youtube", v["channel_id"], v.get("channel_title") or v["channel_id"])


# ---- candidates ----------------------------------------------------------------------------------------------------------


def _seen(db: Database, video_id: str) -> bool:
    with db.conn() as c:
        known = bool(c.execute("SELECT 1 FROM research_candidates WHERE video_id=?", (video_id,)).fetchone()
                     or c.execute("SELECT 1 FROM runs WHERE video_id=?", (video_id,)).fetchone())
    return known or claims.reserved_by(db, video_id) is not None  # a campaign has queued it: leave it to the campaign


def _record(db: Database, v: dict, *, state: str, reason: str | None, via: str, topic: str | None = None, rank: float = 0.0) -> None:
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO research_candidates (video_id, platform, url, title, channel, duration_s, views, published_at, via, topic, "
                  "rank, state, reason, found_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (v["video_id"], v["platform"], v["url"], v.get("title"), v.get("channel"), v.get("duration_s"), v.get("views"),
                   v.get("published_at"), via, topic, rank, state, reason, store.iso()))


def queued(db: Database) -> int:
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM research_candidates WHERE state='queued'").fetchone()["n"]


def _age_days(published_at: str, now) -> float | None:
    try:
        return (now - store.parse_time(published_at)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def judge(v: dict, cfg: Research, *, via: str, now) -> str | None:
    """Why a YouTube video (as `YouTube.videos` returns it) will not be looked at. None means it can be."""
    if (v.get("live_status") or "none") != "none":
        return "it is live now or about to be"
    if v.get("license") != "creativeCommon":
        return "it is not Creative Commons, so it cannot be used with credit"
    minutes = (v.get("duration_s") or 0) / 60
    if minutes < cfg.min_minutes:
        return f"only {minutes:.0f} minutes long; Research wants at least {cfg.min_minutes}"
    if minutes > cfg.max_hours * 60:
        return f"{minutes / 60:.1f} hours long; Research takes at most {cfg.max_hours:g}"
    age = _age_days(v.get("published_at") or "", now)
    if age is not None and age > cfg.max_age_days:
        return f"{age:.0f} days old; Research takes videos up to {cfg.max_age_days} days old"
    if via == "search" and (v.get("views") or 0) < cfg.min_views:
        return f"only {v.get('views') or 0:,} views; a searched video needs {cfg.min_views:,}"
    return None


def _youtube_candidate(v: dict, now) -> dict:
    age = _age_days(v.get("published_at") or "", now) or 1.0
    return {"video_id": v["video_id"], "platform": "youtube", "url": f"https://www.youtube.com/watch?v={v['video_id']}", "title": v.get("title"),
            "channel": v.get("channel_title"), "duration_s": v.get("duration_s"), "views": v.get("views"), "published_at": v.get("published_at"),
            "rank": (v.get("views") or 0) / max(age, 0.5)}


def _short(error: Exception) -> str:
    return f"{type(error).__name__}: {str(error)[:120]}"


def _watch(sv, yt, feed, now, room: int) -> tuple[list[str], int]:
    """Read the newest videos of the watched channels that have gone longest without a look."""
    db, cfg, lines = sv.db, sv.app.research, []
    with db.conn() as c:
        due = [dict(r) for r in c.execute("SELECT * FROM research_sources ORDER BY COALESCE(checked_at, ''), id LIMIT ?", (WATCH_PER_PASS,))]
    for src in due:
        if room <= 0:
            break
        try:
            found = feed.latest(src["platform"], src["channel_key"])
        except Exception as e:  # one channel that cannot be read must not stop the rest
            lines.append(f"{src['name']}: could not be read ({_short(e)})")
            found = []
        with db.conn() as c:
            c.execute("UPDATE research_sources SET checked_at=? WHERE id=?", (store.iso(now), src["id"]))
        fresh = [f for f in found if f.video_id and not f.short and not _seen(db, f.video_id)][:NEWEST_PER_CHANNEL]
        if found:
            lines.append(f"Read {src['name']}: {len(found)} recent videos, {len(fresh)} not seen before.")
        if not fresh:
            continue
        if src["platform"] == "youtube":
            if yt is None:
                lines.append(f"{src['name']}: needs the YouTube key")
                continue
            try:
                facts = {v["video_id"]: v for v in yt.videos([f.video_id for f in fresh])}
            except (QuotaExceeded, YouTubeError) as e:
                lines.append(f"{src['name']}: {_short(e)}")
                continue
            for f in fresh:
                v = facts.get(f.video_id)
                if v is None:
                    continue
                cand, why = _youtube_candidate(v, now), judge(v, cfg, via="watch", now=now)
                if why is None and room <= 0:
                    break
                _record(db, cand, state="skipped" if why else "queued", reason=("Skipped: " + why + ".") if why else None, via="watch", rank=cand["rank"])
                if why is None:
                    room -= 1
                    lines.append(f"{src['name']}: found {cand['title']}")
        else:
            for f in fresh:
                if room <= 0:
                    break
                age = (now.timestamp() - f.published_at) / 86400 if f.published_at else None
                when = store.iso(datetime.fromtimestamp(f.published_at, timezone.utc)) if f.published_at else None
                cand = {"video_id": f.video_id, "platform": src["platform"], "url": f.url, "title": f.title, "channel": src["name"], "published_at": when}
                if age is not None and age > cfg.max_age_days:
                    _record(db, cand, state="skipped", reason=f"Skipped: {age:.0f} days old; Research takes videos up to {cfg.max_age_days} days old.", via="watch")
                    continue
                _record(db, cand, state="queued", reason=None, via="watch", rank=1.0)
                room -= 1
                lines.append(f"{src['name']}: found {f.title or f.video_id}")
    return lines, room


def _search(sv, yt, now, room: int) -> tuple[list[str], int]:
    """A few Creative Commons searches, the topic searched longest ago first."""
    db, cfg, lines = sv.db, sv.app.research, []
    wanted = topics(db, cfg, sv.profiles)
    with db.conn() as c:
        last = {r["topic"]: r["searched_at"] for r in c.execute("SELECT topic, searched_at FROM research_searches")}
    for topic in sorted(wanted, key=lambda t: last.get(t, ""))[:cfg.searches_per_pass]:
        if room <= 0:
            break
        try:
            ids = yt.search(topic, days=cfg.max_age_days, creative_commons=True)
        except QuotaExceeded:
            lines.append("YouTube's search allowance is used up for today")
            break
        except YouTubeError as e:
            lines.append(f"searching \"{topic}\": {_short(e)}")
            continue
        with db.conn() as c:
            c.execute("INSERT INTO research_searches (topic, searched_at) VALUES (?,?) ON CONFLICT(topic) DO UPDATE SET searched_at=excluded.searched_at",
                      (topic, store.iso(now)))
        new = [i for i in ids if not _seen(db, i)]
        lines.append(f"Searched YouTube for \"{topic}\" (Creative Commons): {len(ids)} results, {len(new)} not seen before.")
        if not new:
            continue
        try:
            facts = yt.videos(new)
        except (QuotaExceeded, YouTubeError) as e:
            lines.append(f"searching \"{topic}\": {_short(e)}")
            continue
        for v in sorted(facts, key=lambda x: -(x.get("views") or 0)):
            cand, why = _youtube_candidate(v, now), judge(v, cfg, via="search", now=now)
            if why is None and room <= 0:
                break
            _record(db, cand, state="skipped" if why else "queued", reason=("Skipped: " + why + ".") if why else None, via="search", topic=topic,
                    rank=cand["rank"])
            if why is None:
                room -= 1
                _learn(db, v)
                lines.append(f"searched \"{topic}\": found {cand['title']}")
    return lines, room


def _kick_search(topic: str, get_json) -> list[dict]:
    """Channels Kick's own search returns for a topic: [{slug, name}]. Kick has no official search, so this reads the website's and
    can stop working without warning; anything unexpected is an empty answer, not an error."""
    from urllib.parse import quote

    data = get_json(f"https://kick.com/api/search?searched_word={quote(topic)}")
    items = data.get("channels") if isinstance(data, dict) else None
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        inner = item.get("channel") if isinstance(item.get("channel"), dict) else item
        slug = inner.get("slug")
        if isinstance(slug, str) and slug:
            user = inner.get("user") if isinstance(inner.get("user"), dict) else {}
            out.append({"slug": slug.lower(), "name": inner.get("username") or user.get("username") or slug})
    return out


def _discover_channels(sv, twitch, kick_get, now) -> list[str]:
    """Find channels worth watching on Twitch (the most watched live channels in a topic's category) and on Kick (its search), one topic
    each per pass, the topic looked up longest ago first. Whatever it finds joins the watch list, marked as added by Research."""
    db, cfg, lines = sv.db, sv.app.research, []
    if cfg.discover_per_pass <= 0:
        return lines
    wanted = topics(db, cfg, sv.profiles)
    with db.conn() as c:
        last = {r["topic"]: r["searched_at"] for r in c.execute("SELECT topic, searched_at FROM research_searches")}

    def stamp(key: str) -> None:
        with db.conn() as c:
            c.execute("INSERT INTO research_searches (topic, searched_at) VALUES (?,?) ON CONFLICT(topic) DO UPDATE SET searched_at=excluded.searched_at",
                      (key, store.iso(now)))

    if twitch is not None:
        from .twitch import TwitchError

        for topic in sorted(wanted, key=lambda t: last.get(f"twitch:{t}", ""))[:cfg.discover_per_pass]:
            try:
                found = []
                for category in twitch.categories(topic, first=2):
                    found += twitch.top_streams(category["id"], first=10)
                if not found:
                    found = twitch.search_channels(topic, first=10)
            except TwitchError as e:
                lines.append(f"Twitch lookup for \"{topic}\" failed: {e}")
                continue
            stamp(f"twitch:{topic}")
            big = [s for s in found if s["viewers"] >= cfg.twitch_min_viewers or not s["viewers"]]  # a search gives no viewer count: keep those
            added = 0
            for s in sorted(big, key=lambda s: -s["viewers"]):
                if _learn_channel(db, "twitch", s["login"], s["name"]):
                    added += 1
                    lines.append(f"Added {s['name']} (Twitch, {s['viewers']:,} watching) to the watch list.")
            lines.append(f"Looked up \"{topic}\" on Twitch: {len(found)} live channels, {added} added to the watch list.")
    from sources import channel_feed

    get_json = kick_get or channel_feed._get_json
    for topic in sorted(wanted, key=lambda t: last.get(f"kick:{t}", ""))[:cfg.discover_per_pass]:
        try:
            found = _kick_search(topic, get_json)
        except Exception as e:  # Kick's site is unofficial: say so and carry on
            lines.append(f"Kick search for \"{topic}\" did not answer ({_short(e)}). Kick has no official search, so this can stop working.")
            continue
        stamp(f"kick:{topic}")
        added = 0
        for ch in found[:cfg.kick_per_topic]:
            if _learn_channel(db, "kick", ch["slug"], ch["name"]):
                added += 1
                lines.append(f"Added {ch['name']} (Kick) to the watch list.")
        lines.append(f"Looked up \"{topic}\" on Kick: {len(found)} channels, {added} added to the watch list.")
    return lines


def discover(sv, *, yt=None, feed=None, now=None, twitch=None, kick_get=None) -> list[str]:
    """Top up the queue: watched channels first, then a few searches, then look for more channels to watch."""
    from sources import channel_feed

    feed, now, db = feed or channel_feed, now or store.now(), sv.db
    with db.conn() as c:
        c.execute("DELETE FROM research_candidates WHERE state='skipped' AND found_at<?", (store.iso(now - timedelta(days=SKIPPED_KEEP_DAYS)),))
    room = sv.app.research.queue_ahead - queued(db)
    if room <= 0:
        return []
    lines, room = _watch(sv, yt, feed, now, room)
    if yt is not None:
        more, room = _search(sv, yt, now, room)
        lines += more
    elif not sources(db) and twitch is None:
        lines.append("add a channel to watch or store a YouTube key: there is nothing to look in yet")
    lines += _discover_channels(sv, twitch, kick_get, now)
    return lines


# ---- starting a video ----------------------------------------------------------------------------------------------------


def started_recently(db: Database, now=None) -> int:
    since = store.iso((now or store.now()) - timedelta(hours=24))
    with db.conn() as c:
        return c.execute("SELECT COUNT(*) AS n FROM runs WHERE source='research' AND created_at>=?", (since,)).fetchone()["n"]


def active_runs(db: Database) -> int:
    with db.conn() as c:
        return c.execute(f"SELECT COUNT(*) AS n FROM runs WHERE source='research' AND state IN ({','.join('?' * len(ACTIVE))})", ACTIVE).fetchone()["n"]


def _skip(db: Database, video_id: str, reason: str) -> None:
    with db.conn() as c:
        c.execute("UPDATE research_candidates SET state='skipped', reason=? WHERE video_id=?", (reason, video_id))


def start_next(sv, feed=None) -> list[str]:
    """Start the best waiting video, if Research is on, nothing else is being clipped and today's limit is not used up."""
    from sources import channel_feed

    from . import runs

    db, cfg = sv.db, sv.app.research
    s = settings(db, cfg)
    if not s["on"] or not sv.profiles:
        return []
    if active_runs(db):
        return ["Waiting: another Research video is being clipped."] if queued(db) else []
    if campaigns.active_runs(db):
        return ["Waiting: a campaign video is being clipped first."] if queued(db) else []
    if started_recently(db) >= s["videos_per_day"]:
        return [f"Daily limit reached ({s['videos_per_day']} video{'s' if s['videos_per_day'] != 1 else ''} in 24 hours): the next one starts when the oldest of those is a day old."] if queued(db) else []
    feed = feed or channel_feed
    with db.conn() as c:
        waiting = [dict(r) for r in c.execute("SELECT * FROM research_candidates WHERE state='queued' ORDER BY (via='watch') DESC, rank DESC, found_at")]
    for cand in waiting:
        owner = claims.holder(db, cand["video_id"])
        if owner:  # you pasted it, a campaign has it, or it was already clipped: one video, one owner
            _skip(db, cand["video_id"], f"Skipped: {claims.phrase(db, owner)}.")
            continue
        if cand["platform"] != "youtube":  # Twitch and Kick list no length, so ask now, before the engine downloads anything
            ready = feed.readiness(cand["url"], min_seconds=cfg.min_minutes * 60)
            if ready.state == "not_yet":
                continue
            if ready.state == "skip":
                _skip(db, cand["video_id"], "Skipped: " + ready.reason.rstrip(".") + ".")
                continue
            if ready.duration and ready.duration > cfg.max_hours * 3600:
                _skip(db, cand["video_id"], f"Skipped: {ready.duration / 3600:.1f} hours long; Research takes at most {cfg.max_hours:g}.")
                continue
        try:
            run_id = runs.create(sv, next(iter(sv.profiles)), cand["url"], source="research")
        except links.LinkError as e:
            _skip(db, cand["video_id"], f"Skipped: {e}.")
            continue
        except runs.Claimed as e:
            _skip(db, cand["video_id"], f"Skipped: {e.why}.")
            continue
        with db.conn() as c:
            c.execute("UPDATE research_candidates SET state='started', run_id=? WHERE video_id=?", (run_id, cand["video_id"]))
        return [f"started {cand['title'] or cand['video_id']}"]
    return []


def work(sv, *, yt=None, feed=None, twitch=None, kick_get=None) -> list[str]:
    """One pass: when Research is on, top up the candidates and start the next video."""
    if not is_on(sv.db):
        return []
    lines = discover(sv, yt=yt if yt is not None else sv.yt, feed=feed, twitch=twitch if twitch is not None else sv.twitch, kick_get=kick_get)
    lines += start_next(sv, feed)
    left = queued(sv.db)
    lines.append(f"Pass finished: {left} video{'s' if left != 1 else ''} waiting." if left else "Pass finished: nothing waiting.")
    for line in lines:
        activity.log(sv.db, "research", line, level="warn" if "could not" in line or "allowance" in line else "info")
    return lines


# ---- the decision on a video that was looked at ----------------------------------------------------------------------------


def gate(sv, plan: dict) -> tuple[bool, str]:
    """A researched video goes ahead on its own only if Research is still on, its category has a channel and it fits that channel."""
    if not is_on(sv.db):
        return False, "Research was switched off before this video was started."
    if plan["channel"] is None:
        return False, f"There is no {plan['category']} channel yet, so no clips were made. Add one and Research can use videos like this."
    if not plan["fit_ok"]:
        return False, f"The video fits the {plan['channel']['name']} channel only {plan['fit']} of 5, so no clips were made."
    return True, ""


def note_category(db: Database, run_id: int, category: str) -> None:
    with db.conn() as c:
        c.execute("UPDATE research_candidates SET category=? WHERE run_id=?", (category, run_id))


def missing_channels(db: Database, profiles: dict, days: int = 7) -> list[dict]:
    """Categories of videos Research found good but could not use, because there is no channel for them yet."""
    if channels.catch_all_target(db, profiles) is not None:
        return []  # every clip goes to the one channel: no category lacks a channel
    since = store.iso(store.now() - timedelta(days=days))
    with db.conn() as c:
        rows = c.execute("SELECT category, COUNT(*) AS n FROM research_candidates rc JOIN runs r ON r.id=rc.run_id "
                         "WHERE rc.category IS NOT NULL AND r.state='nothing' AND rc.found_at>=? GROUP BY rc.category", (since,)).fetchall()
    return [{"category": r["category"], "videos": r["n"]} for r in rows if channels.for_category(profiles, r["category"]) is None]


def credit_for(db: Database, video_id: str) -> str:
    """The credit line for clips made from a Research video: the original, its creator and, for YouTube, the licence."""
    with db.conn() as c:
        row = c.execute("SELECT platform, url, title, channel FROM research_candidates WHERE video_id=?", (video_id,)).fetchone()
    if row is None:
        return ""
    who = row["channel"] or "the original creator"
    if row["platform"] == "youtube":
        return f"Original: {(row['title'] or 'video')[:100]} by {who} ({row['url']}), Creative Commons (CC BY)."
    return f"Original stream: {who} ({row['url']})."


def candidates(db: Database, limit: int = 40) -> list[dict]:
    """What Research looked at, newest first, with how the run that handled each one stands."""
    with db.conn() as c:
        rows = c.execute(
            "SELECT rc.*, r.state AS run_state, r.note AS run_note, r.plan_json AS run_plan, "
            "(SELECT COUNT(*) FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id WHERE cr.video_id=rc.video_id AND p.status IN ('uploaded','posted')) AS posted, "
            "(SELECT COUNT(*) FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id WHERE cr.video_id=rc.video_id AND p.status NOT IN ('uploaded','posted','cancelled')) AS planned "
            "FROM research_candidates rc LEFT JOIN runs r ON r.id=rc.run_id ORDER BY rc.found_at DESC, rc.rowid DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
