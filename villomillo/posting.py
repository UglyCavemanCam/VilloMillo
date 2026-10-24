"""Posting: turn an approved clip into a planned post, then send it when it is due.

Routes per platform (set in the channel's settings):
- manual: the clip, title and caption wait on the Manual uploads page. You post it yourself and paste the link.
- api: sent through the platform's official API. While the channel's posting is dry-run, nothing is sent:
  the exact payload is written to the outbox and the post is marked dry_run.

A clip goes to every platform its channel has (YouTube and TikTok together). A channel's posts are spaced out and
capped per day on each platform (YouTube 100 and TikTok about 15 are the platforms' own limits).
"""

from __future__ import annotations

import json
import pathlib
import re
from collections import Counter
from datetime import datetime, timedelta

from . import accounts, secrets, store, tiktok, timefmt
from .config import ROOT, Posting, Profile
from .db import Database

YT_TITLE_MAX = 100
YT_DESCRIPTION_MAX = 5000
TIKTOK_CAPTION_MAX = 2200
DATA = ROOT / "data" / "villomillo"
OUTBOX_ROOT = DATA / "outbox"


class PostError(RuntimeError):
    """A platform refused or could not take the post. The message is shown on the post."""


def _fit(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def pick_platform(posting: Posting, text: str) -> str | None:
    """The platform whose topic words appear most in the clip. Ties and no matches go to the first platform."""
    if not posting.platforms:
        return None
    lowered = text.lower()
    best, best_hits = None, -1
    for platform in posting.platforms:
        hits = sum(lowered.count(word) for word in posting.topics.get(platform, []))
        if hits > best_hits:
            best, best_hits = platform, hits
    return best


def build_copy(posting: Posting, clip: dict, episode: str, platform: str) -> dict:
    """Title and caption for one platform, built from the clip's own hook. Nothing is invented about the guest."""
    hook = (clip.get("hook") or "").strip() or episode
    tag = posting.tags.get(platform, "")
    if platform == "youtube_shorts":
        title = f"{_fit(hook, YT_TITLE_MAX - 10)} #Shorts"
        description = f"{hook}\n\nClip from: {episode}\n{tag}\n\n#Shorts"
        return {"title": title, "caption": _fit(description, YT_DESCRIPTION_MAX), "tags": ["Shorts"]}
    if platform == "tiktok":
        caption = f"{hook}\n{tag}\n\n#fyp".replace("\n\n\n", "\n\n")
        return {"title": "", "caption": _fit(caption, TIKTOK_CAPTION_MAX), "tags": []}
    raise ValueError(f"no copy rules for {platform}")


def copy_problems(copy: dict, platform: str, tag: str) -> list[str]:
    problems = []
    if tag and tag.lower() not in (copy["title"] + " " + copy["caption"]).lower():
        problems.append(f"the caption is missing the campaign's tags ({tag})")
    if platform == "youtube_shorts" and len(copy["title"]) > YT_TITLE_MAX:
        problems.append("the YouTube title is too long")
    return problems


def next_slot(db: Database, posting: Posting, profile_id: str, platform: str, now: datetime) -> datetime:
    """The next time this platform can take a post: spaced from the last one, and under the daily cap.

    A day is an Eastern Time calendar day, so "two a day" means two between midnight and midnight on the East Coast.
    """
    with db.conn() as c:
        times = [r["scheduled_at"] for r in c.execute(
            "SELECT scheduled_at FROM posts WHERE profile_id=? AND platform=? AND status!='blocked'",
            (profile_id, platform))]
    taken = Counter(timefmt.local_date(t) for t in times)
    candidate = now
    if times:
        candidate = max(now, store.parse_time(max(times)) + timedelta(minutes=posting.min_gap_minutes))
    while taken[timefmt.local_date(candidate)] >= posting.cap_for(platform):
        candidate = timefmt.start_of_day_utc(timefmt.local_date(candidate) + timedelta(days=1))
    return candidate


def posted_today(db: Database, profile_id: str, platform: str, *, now: datetime | None = None) -> int:
    """How many posts this account has put out today (an Eastern Time day, like the daily caps)."""
    today = timefmt.local_date(store.iso(now or store.now()))
    with db.conn() as c:
        rows = c.execute("SELECT posted_at FROM posts WHERE profile_id=? AND platform=? AND status='uploaded' AND posted_at IS NOT NULL",
                         (profile_id, platform)).fetchall()
    return sum(1 for r in rows if timefmt.local_date(r["posted_at"]) == today)


def plan_all(db: Database, profile: Profile, clip_id: int, *, now: datetime | None = None, owner: str | None = None) -> list[dict]:
    """Plan the clip's post on every platform the channel has. Returns the posts that were new."""
    if profile.posting is None:
        return []
    posts = [plan(db, profile, clip_id, now=now, owner=owner, platform=platform) for platform in profile.posting.platforms]
    return [p for p in posts if p]


def plan(db: Database, profile: Profile, clip_id: int, *, now: datetime | None = None,
         owner: str | None = None, platform: str | None = None) -> dict | None:
    """Plan the post for one approved clip on one platform (by default the one its topic words suit best).
    Returns the new post, or None if the clip already has a post there.

    `owner` is the channel that made the clip, when the post goes out on another channel (routing)."""
    posting = profile.posting
    if posting is None:
        return None
    now = now or store.now()
    with db.conn() as c:
        clip = c.execute("SELECT * FROM clips_ready WHERE profile_id=? AND id=?",
                         (owner or profile.id, clip_id)).fetchone()
        if clip is None:
            raise LookupError("no such clip")
        if clip["state"] != "approved":
            raise ValueError("only approved clips can be posted")
        if platform and c.execute("SELECT 1 FROM posts WHERE clip_id=? AND platform=?", (clip_id, platform)).fetchone():
            return None
        if not platform and c.execute("SELECT 1 FROM posts WHERE clip_id=?", (clip_id,)).fetchone():
            return None
        video = c.execute("SELECT title FROM videos WHERE profile_id=? AND video_id=?",
                          (clip["profile_id"], clip["video_id"])).fetchone()
    episode = video["title"] if video else "the Solved podcast"
    grade = json.loads(clip["grade_json"])
    platform = platform or pick_platform(posting, f"{clip['hook'] or ''} {grade.get('caption_text', '')[:1500]}")
    if platform is None:
        raise ValueError("this channel has no posting platforms set")
    copy = build_copy(posting, dict(clip), episode, platform)
    problems = copy_problems(copy, platform, posting.tags.get(platform, ""))
    campaign_rules = grade.get("campaign")
    if campaign_rules and not campaign_rules["ok"]:
        problems.append("the campaign rules are not met: " + "; ".join(campaign_rules["problems"]))
    route = accounts.effective_route(db, profile, platform)[0]
    status, when = ("blocked", now) if problems else ("queued", next_slot(db, posting, profile.id, platform, now))
    with db.conn() as c:
        c.execute(
            "INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, "
            "scheduled_at, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (profile.id, clip_id, platform, route, status, copy["title"], copy["caption"], json.dumps(copy["tags"]),
             store.iso(when), "; ".join(problems) or None, store.iso(now)),
        )
        post_id = c.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    return {"id": post_id, "profile": profile.id, "platform": platform, "route": route, "status": status,
            "scheduled_at": store.iso(when)}


class YouTubeShorts:
    """Uploads through the YouTube Data API (publish/youtube_shorts.py). Privacy follows the channel's setting."""

    def __init__(self, publisher):
        self.publisher = publisher

    def post(self, *, path: pathlib.Path, copy: dict, privacy: str) -> dict:
        from publish.base import PublishRequest

        try:
            result = self.publisher.publish(PublishRequest(
                video_path=path, title=copy["title"], description=copy["caption"], tags=copy["tags"], privacy=privacy,
            ))
        except Exception as e:  # the publisher raises its own error types; the message is what matters here
            raise PostError(f"YouTube refused the upload: {e}") from None
        notes = list(result.warnings)
        if result.locked_private:
            notes.append("YouTube kept this private: the API project has not passed its audit yet")
        return {"url": f"https://youtube.com/shorts/{result.video_id}", "notes": notes, "remote_id": result.video_id}


class TikTokPoster:
    """Posts through TikTok's Content Posting API (villomillo/tiktok.py). Until TikTok has audited the app, only privately."""

    def __init__(self, client: tiktok.TikTok, handle: str = ""):
        self.client, self.handle = client, handle.lstrip("@")

    def post(self, *, path: pathlib.Path, copy: dict, privacy: str) -> dict:
        try:
            result = self.client.post(path, copy["caption"], privacy)
        except tiktok.TikTokError as e:
            raise PostError(str(e)) from None
        notes = list(result["notes"])
        if result["status"] != "PUBLISH_COMPLETE":
            notes.append("TikTok was still processing the video when last asked")
        base = f"https://www.tiktok.com/@{self.handle}" if self.handle else "https://www.tiktok.com"
        url = f"{base}/video/{result['post_id']}" if result["post_id"] else base
        return {"url": url, "notes": notes, "publish_id": result["publish_id"], "remote_id": result["post_id"]}


def tiktok_client(profile: Profile) -> tiktok.TikTok | None:
    """The channel's TikTok account, or None when the app's keys are not stored or the account is not connected."""
    try:
        key, secret = secrets.get(secrets.TIKTOK_KEY), secrets.get(secrets.TIKTOK_SECRET)
    except secrets.MissingSecret:
        return None
    path = tiktok.token_path(DATA, profile.id)
    return tiktok.TikTok(path, key, secret) if tiktok.is_connected(path) else None


def tiktok_connected(profile: Profile) -> bool:
    return tiktok.is_connected(tiktok.token_path(DATA, profile.id))


def connect_tiktok(profile: Profile) -> dict:
    """Sign in to TikTok once, in the browser, and keep the tokens for this channel's account."""
    try:
        key, secret = secrets.get(secrets.TIKTOK_KEY), secrets.get(secrets.TIKTOK_SECRET)
    except secrets.MissingSecret:
        raise PostError("store your TikTok app's keys first: python -m villomillo key tiktok_key, then key tiktok_secret") from None
    try:
        tokens = tiktok.sign_in(tiktok.token_path(DATA, profile.id), key, secret)
    except tiktok.TikTokError as e:
        raise PostError(str(e)) from None
    return {"open_id": tokens.open_id}


YOUTUBE_CLIENT = ROOT / "config" / "villomillo" / "youtube_client_secret.json"


def _youtube_publisher(profile: Profile):
    from publish.youtube_shorts import YouTubeShortsPublisher

    return YouTubeShortsPublisher(
        client_secret=YOUTUBE_CLIENT,
        token_path=ROOT / "data" / "villomillo" / f"youtube_token_{profile.id}.json",
        privacy=profile.posting.privacy if profile.posting else "private",
    )


def connect_youtube(profile: Profile) -> dict:
    """Sign in to YouTube once, in the browser, and keep the token for this channel.

    Asks only for permission to upload and to read the channel's own details. Returns the channel that was
    connected, so you can see it is the right one before anything is posted.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    from publish.youtube_shorts import CONNECT_SCOPES

    if not YOUTUBE_CLIENT.exists():
        raise PostError(f"save your Google OAuth client file as {YOUTUBE_CLIENT} first")
    publisher = _youtube_publisher(profile)
    creds = InstalledAppFlow.from_client_secrets_file(str(YOUTUBE_CLIENT), CONNECT_SCOPES).run_local_server(
        port=0, timeout_seconds=300)
    publisher._save_token(creds)
    return publisher.channel_info()


def default_adapters(profile: Profile) -> dict:
    """The platform adapters for every route set to api. Credentials come from the keychain or the channel's files."""
    adapters = {}
    posting = profile.posting
    if posting is None:
        return adapters
    if posting.platforms.get("youtube_shorts") == "api":
        adapters["youtube_shorts"] = YouTubeShorts(_youtube_publisher(profile))
    if posting.platforms.get("tiktok") == "api":
        client = tiktok_client(profile)
        if client is not None:
            adapters["tiktok"] = TikTokPoster(client, profile.tiktok_handle)
    return adapters


def send_now(db: Database, profile: Profile, post_id: int, adapters: dict) -> str:
    """Send one planned post through its platform's API right now, whatever its route and the dry-run setting.

    This is the explicit go for a single post. It never touches the channel's settings, and it refuses a post
    that is already out or whose clip is no longer approved. Returns a short status line.
    """
    with db.conn() as c:
        post = c.execute("SELECT * FROM posts WHERE profile_id=? AND id=?", (profile.id, post_id)).fetchone()
        clip = post and c.execute("SELECT * FROM clips_ready WHERE id=?", (post["clip_id"],)).fetchone()
    if post is None:
        raise LookupError("no such post")
    if post["status"] in ("uploaded", "posted"):
        raise ValueError(f"this post is already out ({post['url']})")
    if post["status"] == "blocked" or clip is None or clip["state"] != "approved":
        raise ValueError("this post is blocked or its clip is no longer approved")
    copy = {"title": post["title"], "caption": post["caption"], "tags": json.loads(post["tags_json"])}
    status, error, url, extra = _send(db, profile, post, copy, clip, adapters)
    with db.conn() as c:
        c.execute("UPDATE posts SET route='api', status=?, error=?, url=COALESCE(?, url), posted_at=?, "
                  "publish_id=COALESCE(?, publish_id), remote_id=COALESCE(?, remote_id) WHERE id=?",
                  (status, error, url, store.iso() if status == "uploaded" else None,
                   extra.get("publish_id"), extra.get("remote_id"), post_id))
    return f"post {post_id} ({post['platform']}): {status}" + (f", {error}" if error else f", {url}")


def run_due(db: Database, profile: Profile, *, now: datetime | None = None, adapters: dict | None = None) -> list[str]:
    """Send every post that is due. Returns one short line per post."""
    posting = profile.posting
    if posting is None:
        return []
    now = now or store.now()
    adapters = adapters or {}
    with db.conn() as c:
        due = c.execute("SELECT * FROM posts WHERE profile_id=? AND status='queued' AND scheduled_at<=? "
                        "ORDER BY scheduled_at", (profile.id, store.iso(now))).fetchall()
    lines = []
    for post in due:
        if accounts.is_paused(db, profile.id, post["platform"]):  # the post waits; the reason is on the channel's page
            lines.append(f"post {post['id']} ({post['platform']}): waiting, the account is paused")
            continue
        with db.conn() as c:
            clip = c.execute("SELECT * FROM clips_ready WHERE id=?", (post["clip_id"],)).fetchone()
        route = accounts.effective_route(db, profile, post["platform"])[0]
        copy = {"title": post["title"], "caption": post["caption"], "tags": json.loads(post["tags_json"])}
        extra: dict = {}
        if clip is None or clip["state"] != "approved":
            status, error, url = "blocked", "the clip is no longer approved", None
        elif route == "manual":
            status, error, url = "manual_ready", None, None
        elif posting.dry_run:
            status, error, url = "dry_run", None, None
            _write_outbox(profile, post, copy, clip["file_path"])
        elif posted_today(db, profile.id, post["platform"], now=now) >= posting.cap_for(post["platform"]):
            lines.append(f"post {post['id']} ({post['platform']}): waiting, today's limit of {posting.cap_for(post['platform'])} is reached")
            continue
        else:
            status, error, url, extra = _send(db, profile, post, copy, clip, adapters)
            if status == "uploaded":
                accounts.record_success(db, profile.id, post["platform"])
            elif status == "failed":
                paused = accounts.record_failure(db, profile.id, post["platform"], error or "")
                if paused:
                    store.event(db, "warn", "post", f"{profile.id} {post['platform']} paused: {paused}")
        with db.conn() as c:
            c.execute("UPDATE posts SET status=?, error=?, url=COALESCE(?, url), posted_at=?, "
                      "publish_id=COALESCE(?, publish_id), remote_id=COALESCE(?, remote_id) WHERE id=?",
                      (status, error, url, store.iso(now) if status == "uploaded" else None,
                       extra.get("publish_id"), extra.get("remote_id"), post["id"]))
        lines.append(f"post {post['id']} ({post['platform']}): {status}" + (f", {error}" if error else ""))
    return lines


def _send(db, profile, post, copy, clip, adapters) -> tuple[str, str | None, str | None, dict]:
    """(status, error, url, extra): extra carries the platform's own ids (publish_id, remote_id) when it gave any."""
    adapter = adapters.get(post["platform"])
    if adapter is None:
        return "failed", f"no {post['platform']} connection is set up for this channel", None, {}
    try:
        result = adapter.post(path=pathlib.Path(clip["file_path"]), copy=copy, privacy=profile.posting.privacy)
    except PostError as e:
        return "failed", str(e), None, {}
    for note in result["notes"]:
        store.event(db, "warn", "post", f"post {post['id']}: {note}")
    return "uploaded", None, result["url"], {k: result.get(k) for k in ("publish_id", "remote_id")}


def _write_outbox(profile: Profile, post, copy: dict, file_path: str) -> None:
    folder = OUTBOX_ROOT / profile.id / "posts"
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"post_id": post["id"], "platform": post["platform"], "route": post["route"],
               "scheduled_at": post["scheduled_at"], "file": file_path, **copy,
               "note": "dry run: this was NOT sent"}
    (folder / f"post-{post['id']}.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---- real numbers --------------------------------------------------------------------------------------------------

YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/)([0-9A-Za-z_-]{11})")
STATS_DAYS = 30  # a post's numbers are followed for this long


def _latest_views(db: Database, post_id: int) -> int | None:
    with db.conn() as c:
        row = c.execute("SELECT views FROM post_stats WHERE post_id=? ORDER BY taken_at DESC LIMIT 1", (post_id,)).fetchone()
    return row["views"] if row else None


def _save_stats(db: Database, post_id: int, stats: dict, now: datetime) -> None:
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO post_stats (post_id, taken_at, views, likes, comments, shares) VALUES (?,?,?,?,?,?)",
                  (post_id, store.iso(now), stats.get("views"), stats.get("likes"), stats.get("comments"), stats.get("shares")))


def collect_stats(db: Database, profile: Profile, *, yt=None, tiktok_account=None, now: datetime | None = None) -> list[str]:
    """Read the real views, likes and comments of this channel's recent posts and keep them. YouTube is read with the
    YouTube API key (a post you made by hand counts too: its video is found from the link you recorded); TikTok with the
    connected account, which also learns the id of an upload that was still processing. Returns one line per thing done."""
    now = now or store.now()
    since = store.iso(now - timedelta(days=STATS_DAYS))
    with db.conn() as c:
        posts = [dict(r) for r in c.execute(
            "SELECT * FROM posts WHERE profile_id=? AND status IN ('uploaded','posted') AND COALESCE(posted_at, created_at)>=? ORDER BY id",
            (profile.id, since))]
    lines = []
    if yt is not None:
        by_video = {}
        for p in posts:
            if p["platform"] == "youtube_shorts":
                m = YOUTUBE_ID.search(p["remote_id"] or p["url"] or "")
                if m:
                    by_video.setdefault(m.group(1), []).append(p)
        for i in range(0, len(by_video), 50):
            for item in yt.videos(list(by_video)[i:i + 50]):
                for p in by_video.get(item["video_id"], []):
                    _save_stats(db, p["id"], item, now)
                    lines.append(f"post {p['id']} (YouTube): {item.get('views')} views")
    if tiktok_account is not None:
        waiting = [p for p in posts if p["platform"] == "tiktok" and p["publish_id"] and not p["remote_id"]]
        for p in waiting:
            try:
                answer = tiktok_account.status(p["publish_id"])
            except tiktok.TikTokError as e:
                lines.append(f"post {p['id']} (TikTok): could not ask TikTok ({e})")
                continue
            if answer["post_id"]:
                with db.conn() as c:
                    c.execute("UPDATE posts SET remote_id=? WHERE id=?", (answer["post_id"], p["id"]))
                p["remote_id"] = answer["post_id"]
        known = {p["remote_id"]: p for p in posts if p["platform"] == "tiktok" and p["remote_id"]}
        try:
            numbers = tiktok_account.video_stats(list(known))
        except tiktok.TikTokError as e:
            numbers = {}
            lines.append(f"TikTok numbers could not be read: {e}")
        for remote_id, stats in numbers.items():
            if remote_id in known:
                _save_stats(db, known[remote_id]["id"], stats, now)
                lines.append(f"post {known[remote_id]['id']} (TikTok): {stats.get('views')} views")
    for platform in {p["platform"] for p in posts}:
        reason = _views_fell(db, profile, platform, posts)
        if reason and not accounts.is_paused(db, profile.id, platform):
            accounts.pause(db, profile.id, platform, reason)
            lines.append(f"{platform} paused: {reason}")
    return lines


def _views_fell(db: Database, profile: Profile, platform: str, posts: list[dict]) -> str | None:
    """A sudden fall in views is the usual sign of a shadow-ban: pause the account and say why. Needs enough history."""
    mine = sorted((p for p in posts if p["platform"] == platform), key=lambda p: p["id"])
    views = [v for v in (_latest_views(db, p["id"]) for p in mine) if v is not None]
    if len(views) >= 10 and accounts.views_dropped(views[-5:], views[:-5]):
        return "views fell to under a third of this account's usual: it may be limited by the platform; check it before posting more"
    return None


def mark_posted(db: Database, profile_id: str, post_id: int, url: str) -> None:
    """You posted a manual upload yourself. Record the link."""
    if not url.startswith("https://"):
        raise ValueError("paste the post's full link (https://...)")
    with db.conn() as c:
        cur = c.execute(
            "UPDATE posts SET status='posted', url=?, posted_at=? WHERE profile_id=? AND id=? AND route='manual' "
            "AND status IN ('queued','manual_ready')", (url.strip(), store.iso(), profile_id, post_id))
    if cur.rowcount == 0:
        raise LookupError("no manual post waiting with that id")
