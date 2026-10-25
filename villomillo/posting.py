"""Posting: turn an approved clip into a planned post, then send it when it is due.

Routes per platform (set in the channel's settings):
- manual: the clip, title and caption wait on the Manual uploads page. You post it yourself and paste the link.
- api: sent through the platform's official API. While the channel's posting is dry-run, nothing is sent:
  the exact payload is written to the outbox and the post is marked dry_run.

A clip goes to every platform its channel has (today that is YouTube). A channel's posts are spaced out and capped per day.
"""

from __future__ import annotations

import json
import pathlib
import random
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timedelta

from core.paths import discard

from . import accounts, campaigns, research, rewards, store, timefmt
from .config import ROOT, Posting, Profile
from .db import Database

YT_TITLE_MAX = 100
YT_DESCRIPTION_MAX = 5000
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


def build_copy(posting: Posting, clip: dict, episode: str, platform: str, credit: str = "") -> dict:
    """Title and caption for one platform, built from the clip's own hook. Nothing is invented about the guest."""
    hook = (clip.get("hook") or "").strip() or episode
    tag = posting.tags.get(platform, "")
    if platform == "youtube_shorts":
        title = f"{_fit(hook, YT_TITLE_MAX - 10)} #Shorts"
        description = f"{hook}\n\nClip from: {episode}\n{credit}\n{tag}\n\n#Shorts" if credit else f"{hook}\n\nClip from: {episode}\n{tag}\n\n#Shorts"
        return {"title": title, "caption": _fit(description, YT_DESCRIPTION_MAX), "tags": ["Shorts"]}
    raise ValueError(f"no copy rules for {platform}")


def copy_problems(copy: dict, platform: str, tag: str) -> list[str]:
    problems = []
    if tag and tag.lower() not in (copy["title"] + " " + copy["caption"]).lower():
        problems.append(f"the caption is missing the campaign's tags ({tag})")
    if platform == "youtube_shorts" and len(copy["title"]) > YT_TITLE_MAX:
        problems.append("the YouTube title is too long")
    return problems


def next_slot(db: Database, posting: Posting, profile_id: str, platform: str, now: datetime, rng: random.Random | None = None) -> datetime:
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
        top = posting.max_gap_minutes if posting.max_gap_minutes is not None else posting.min_gap_minutes
        wait = (rng or random).uniform(posting.min_gap_minutes, top)  # a different wait for every clip, so the gaps are never the same
        candidate = max(now, store.parse_time(max(times)) + timedelta(minutes=wait))
    while taken[timefmt.local_date(candidate)] >= posting.daily_cap:
        candidate = timefmt.start_of_day_utc(timefmt.local_date(candidate) + timedelta(days=1))
    return candidate


def respace_queued(db: Database, profile: Profile, *, now: datetime | None = None, rng: random.Random | None = None) -> int:
    """Give every post still waiting its time again under the channel's current gap, in its current order, starting now.
    Posts already sent, or waiting to be posted by hand, are not touched. Returns how many were moved."""
    posting = profile.posting
    if posting is None:
        return 0
    now = now or store.now()
    with db.conn() as c:
        waiting = [dict(r) for r in c.execute("SELECT id, platform FROM posts WHERE profile_id=? AND status='queued' ORDER BY scheduled_at, id", (profile.id,))]
        last = {r["platform"]: store.parse_time(r["m"]) for r in c.execute(
            "SELECT platform, MAX(COALESCE(posted_at, scheduled_at)) AS m FROM posts WHERE profile_id=? AND status IN ('uploaded','posted') GROUP BY platform",
            (profile.id,))}
    top = posting.max_gap_minutes if posting.max_gap_minutes is not None else posting.min_gap_minutes
    for row in waiting:
        platform = row["platform"]
        wait = timedelta(minutes=(rng or random).uniform(posting.min_gap_minutes, top))
        when = max(now, last[platform] + wait) if platform in last else now
        with db.conn() as c:
            taken = Counter(timefmt.local_date(r["scheduled_at"]) for r in c.execute(
                "SELECT scheduled_at FROM posts WHERE profile_id=? AND platform=? AND status NOT IN ('blocked','cancelled') AND id!=?",
                (profile.id, platform, row["id"])))
        while taken[timefmt.local_date(store.iso(when))] >= posting.daily_cap:
            when = timefmt.start_of_day_utc(timefmt.local_date(store.iso(when)) + timedelta(days=1))
        with db.conn() as c:
            c.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (store.iso(when), row["id"]))
        last[platform] = when
    return len(waiting)


GAP_RESPACED = "queue_respaced_gap_v1"


def respace_once(db: Database, profiles: dict[str, Profile]) -> int:
    """After the channels got their new 10 to 20 minute gap, space the posts already waiting the same way, once."""
    with db.conn(immediate=True) as c:
        if c.execute("SELECT 1 FROM meta WHERE key=?", (GAP_RESPACED,)).fetchone():
            return 0
        c.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (GAP_RESPACED,))
    moved = sum(respace_queued(db, p) for p in profiles.values())
    if moved:
        store.event(db, "info", "post", f"{moved} waiting post(s) re-spaced to the channel's new gap")
    return moved


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
    if platform is not None:
        posting = campaigns.posting_for(db, posting, platform, clip["campaign_id"])  # a campaign's clips carry its required tags
    if platform is None:
        raise ValueError("this channel has no posting platforms set")
    copy = build_copy(posting, dict(clip), episode, platform, research.credit_for(db, clip["video_id"]))
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
                  "remote_id=COALESCE(?, remote_id) WHERE id=?",
                  (status, error, url, store.iso() if status == "uploaded" else None,
                   extra.get("remote_id"), post_id))
    if status == "uploaded":
        rewards.queue(db, post_id)
    return f"post {post_id} ({post['platform']}): {status}" + (f", {error}" if error else f", {url}")


RESENT = ("uploaded", "posted")  # already out: posting again is a separate, deliberate step
RESENDABLE = ("manual_ready", "dry_run")  # planned before the channel could post for real: nothing sends these again by itself


def why_not_sending(db: Database, profile: Profile, platform: str) -> str | None:
    """Why this channel cannot send to the platform for real right now, in plain words; None when it can."""
    posting = profile.posting
    if posting is None or posting.platforms.get(platform, "manual") != "api":
        return "This channel's posts are set to go out by hand. Change that to Automatically on the channel's page."
    if posting.dry_run:
        return "Dry run is on, so nothing is sent. Turn it off on the channel's page."
    if not accounts.audit_passed(db, profile.id, platform):
        return "The platform's audit is not recorded as passed. Record it under Accounts and safety stops."
    if accounts.is_paused(db, profile.id, platform):
        return "This account is paused. Resume it first."
    return None


def resend_waiting(db: Database, profile: Profile, platform: str, *, now: datetime | None = None) -> int:
    """Put the posts that were planned before the channel could post for real back in the queue, best clip first, spaced the way
    new posts are (gap and daily limit). Returns how many. Raises ValueError (with the reason) when the channel cannot send yet."""
    if (why := why_not_sending(db, profile, platform)):
        raise ValueError(why)
    now = now or store.now()
    with db.conn() as c:
        waiting = c.execute(
            "SELECT po.id FROM posts po JOIN clips_ready cl ON cl.id=po.clip_id WHERE po.profile_id=? AND po.platform=? "
            f"AND po.status IN ({','.join('?' * len(RESENDABLE))}) AND cl.state='approved' ORDER BY cl.engine_score DESC, po.id",
            (profile.id, platform, *RESENDABLE)).fetchall()
    for row in waiting:
        when = next_slot(db, profile.posting, profile.id, platform, now)
        with db.conn() as c:
            c.execute("UPDATE posts SET status='queued', route='api', error=NULL, scheduled_at=? WHERE id=?", (store.iso(when), row["id"]))
    return len(waiting)


def repost(db: Database, profile: Profile, post_id: int, *, now: datetime | None = None) -> str:
    """Put a post that already went out back in the queue so it goes out again (its video was deleted from YouTube, say). It keeps
    its title and caption. The link and the numbers of the earlier video are cleared. Refuses with the reason when the channel
    cannot send for real. Returns the platform, so the caller can name it."""
    with db.conn() as c:
        post = c.execute("SELECT platform, status FROM posts WHERE profile_id=? AND id=?", (profile.id, post_id)).fetchone()
    if post is None:
        raise LookupError("no such post")
    if post["status"] not in RESENT:
        raise ValueError("only a post that already went out can be posted again")
    if (why := why_not_sending(db, profile, post["platform"])):
        raise ValueError(why)
    with db.conn() as c:
        c.execute("UPDATE posts SET status='queued', route='api', url=NULL, remote_id=NULL, posted_at=NULL, error=NULL, scheduled_at=? WHERE id=?",
                  (store.iso(now or store.now()), post_id))
        c.execute("DELETE FROM post_stats WHERE post_id=?", (post_id,))  # those were the old video's numbers
        c.execute("DELETE FROM campaign_submissions WHERE post_id=?", (post_id,))  # the old link is dead: the new one can be handed in
    store.event(db, "info", "post", f"post {post_id} put back in the queue to go out again")
    return post["platform"]


def send_post_now(db: Database, profile: Profile, post_id: int) -> str:
    """The explicit go for one post: refuses with the reason when the channel cannot send for real yet."""
    with db.conn() as c:
        post = c.execute("SELECT platform FROM posts WHERE profile_id=? AND id=?", (profile.id, post_id)).fetchone()
    if post is None:
        raise LookupError("no such post")
    if (why := why_not_sending(db, profile, post["platform"])):
        raise ValueError(why)
    return send_now(db, profile, post_id, default_adapters(profile))


def run_due(db: Database, profile: Profile, *, now: datetime | None = None, adapters: dict | None = None,
            budget_s: float | None = None) -> list[str]:
    """Send every post that is due. Returns one short line per post. With `budget_s`, stops starting new posts once that many seconds
    have passed (enlarging and uploading a clip takes a minute or more), and the next run does the rest."""
    started = time.monotonic()
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
        if budget_s is not None and time.monotonic() - started > budget_s:
            lines.append("more posts are due: the next run sends them")
            break
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
        elif posted_today(db, profile.id, post["platform"], now=now) >= posting.daily_cap:
            lines.append(f"post {post['id']} ({post['platform']}): waiting, today's limit of {posting.daily_cap} is reached")
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
                      "remote_id=COALESCE(?, remote_id) WHERE id=?",
                      (status, error, url, store.iso(now) if status == "uploaded" else None,
                       extra.get("remote_id"), post["id"]))
        if status == "uploaded":
            rewards.queue(db, post["id"])  # a campaign clip's link is handed in to Content Rewards straight away
        lines.append(f"post {post['id']} ({post['platform']}): {status}" + (f", {error}" if error else ""))
    return lines


UPLOAD_W, UPLOAD_H = 1440, 2560


def enlarged_copy(path: pathlib.Path, out: pathlib.Path) -> pathlib.Path:
    """A 1440x2560 copy of a finished clip, for the upload only (YouTube treats a bigger upload better). The clip itself is untouched.
    The picture is enlarged with Lanczos, kept at high quality and tagged BT.709; the sound is copied as it is."""
    from core import binaries

    cmd = [binaries.ffmpeg(), "-y", "-v", "error", "-i", str(path), "-vf", f"scale={UPLOAD_W}:{UPLOAD_H}:flags=lanczos,format=yuv420p",
           "-c:v", "libx264", "-preset", "slow", "-crf", "12", "-profile:v", "high", "-colorspace", "bt709", "-color_primaries", "bt709",
           "-color_trc", "bt709", "-color_range", "tv", "-c:a", "copy", "-movflags", "+faststart", str(out)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if run.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        raise PostError("could not enlarge the clip for upload: " + (run.stderr.strip()[-200:] or "ffmpeg failed"))
    return out


def _send(db, profile, post, copy, clip, adapters) -> tuple[str, str | None, str | None, dict]:
    """(status, error, url, extra): extra carries the platform's own id for the live video (remote_id) when it gave one."""
    adapter = adapters.get(post["platform"])
    if adapter is None:
        return "failed", f"no {post['platform']} connection is set up for this channel", None, {}
    path, temporary = pathlib.Path(clip["file_path"]), None
    if profile.posting.upload_size == "1440":
        try:
            temporary = enlarged_copy(path, path.with_name(f"{path.stem}.upload.mp4"))
            path = temporary
        except (PostError, OSError, subprocess.SubprocessError) as e:  # the clip still goes out, at its own size
            store.event(db, "warn", "post", f"post {post['id']}: sent at its own size, {e}")
    try:
        result = adapter.post(path=path, copy=copy, privacy=profile.posting.privacy)
    except PostError as e:
        return "failed", str(e), None, {}
    finally:
        discard(temporary)  # never raises: a locked scratch file must not replace the real result
    for note in result["notes"]:
        store.event(db, "warn", "post", f"post {post['id']}: {note}")
    return "uploaded", None, result["url"], {"remote_id": result.get("remote_id")}


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


def collect_stats(db: Database, profile: Profile, *, yt=None, now: datetime | None = None) -> list[str]:
    """Read the real views, likes and comments of this channel's recent posts and keep them. YouTube is read with the
    YouTube API key; a post you made by hand counts too, because its video is found from the link you recorded.
    Returns one line per post read."""
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
