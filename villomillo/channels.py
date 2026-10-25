"""Channels: a category (such as Podcasts) with its own YouTube channel.

Channels live in the database and are added and edited in the dashboard. The files in config/villomillo/channels/ are
only a starting set, read once into an empty database. A clip can only post on the channel for its own category; when
there is none, the clip is held and the dashboard offers to create it (see routing.release_held).
"""

from __future__ import annotations

import json
import re

from . import store
from .config import CONFIG_DIR, Brand, Niche, Posting, Profile, load_profiles
from .db import Database

SEEDED = "channels_seeded"
DEFAULT_YOUTUBE_PER_DAY = 100  # YouTube's own limit for a channel, as you set it


class ChannelError(ValueError):
    """Something about the channel you tried to save is wrong. The message is safe to show."""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30]


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _seed(db: Database) -> None:
    """Read the starting channels once. After that the database is the only source."""
    with db.conn() as c:
        if c.execute("SELECT 1 FROM meta WHERE key=?", (SEEDED,)).fetchone():
            return
        if not c.execute("SELECT 1 FROM channels").fetchone():
            for profile in load_profiles(CONFIG_DIR / "channels").values():
                c.execute("INSERT INTO channels (id, data_json, created_at, updated_at) VALUES (?,?,?,?)",
                          (profile.id, profile.model_dump_json(), store.iso(), store.iso()))
        c.execute("INSERT INTO meta (key, value) VALUES (?, '1')", (SEEDED,))


def _without_retired(data: dict) -> dict:
    """A channel saved by an older version: drop settings that have since moved (campaign rules to Campaigns) or gone (TikTok)."""
    data.pop("tiktok_handle", None)
    data.pop("campaign", None)
    posting = data.get("posting")
    if posting:
        posting["platforms"] = {k: v for k, v in posting.get("platforms", {}).items() if k != "tiktok"}
        caps = posting.pop("platform_caps", None) or {}
        if "youtube_shorts" in caps:
            posting["daily_cap"] = caps["youtube_shorts"]
        posting["topics"] = {k: v for k, v in posting.get("topics", {}).items() if k != "tiktok"}
        posting["tags"] = {k: v for k, v in posting.get("tags", {}).items() if k != "tiktok"}
    return data


def load(db: Database) -> dict[str, Profile]:
    _seed(db)
    with db.conn() as c:
        rows = c.execute("SELECT id, data_json FROM channels ORDER BY created_at, id").fetchall()
    return {r["id"]: Profile(**_without_retired(json.loads(r["data_json"]))) for r in rows}


def save(db: Database, profile: Profile) -> None:
    with db.conn() as c:
        c.execute(
            "INSERT INTO channels (id, data_json, created_at, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET data_json=excluded.data_json, updated_at=excluded.updated_at",
            (profile.id, profile.model_dump_json(), store.iso(), store.iso()),
        )


def for_category(profiles: dict[str, Profile], category: str) -> Profile | None:
    return next((p for p in profiles.values() if p.category and _same(p.category, category)), None)


def _clean(text: str, what: str, limit: int) -> str:
    text = " ".join(text.split())
    if not text:
        raise ChannelError(f"give the channel a {what}")
    if len(text) > limit:
        raise ChannelError(f"the {what} is too long (at most {limit} characters)")
    return text


def create(db: Database, profiles: dict[str, Profile], *, name: str, category: str, about: str) -> Profile:
    """Add a channel. `profiles` is the live set, which gains the new channel."""
    name, category = _clean(name, "name", 40), _clean(category, "category", 40)
    about = _clean(about, "description of what it posts", 600)
    if for_category(profiles, category):
        raise ChannelError(f"there is already a channel for {category}: one channel per category")
    base = slug(name) or slug(category)
    if len(base) < 2:
        raise ChannelError("the name needs at least two letters or numbers")
    cid, n = base, 2
    while cid in profiles:
        cid, n = f"{base[:27]}-{n}", n + 1
    profile = Profile(
        id=cid, display_name=name, category=category, niche=Niche(definition=about),
        posting=Posting(dry_run=True, platforms={"youtube_shorts": "manual"}, privacy="private",
                        daily_cap=DEFAULT_YOUTUBE_PER_DAY, min_gap_minutes=0),
        brand=Brand(name=name),
    )
    save(db, profile)
    profiles[cid] = profile
    return profile


def update(db: Database, profiles: dict[str, Profile], channel_id: str, **changes) -> Profile:
    """Change a channel. Accepted: name, category, about, brand_handle, youtube_tags, youtube_daily_cap, youtube_route, dry_run, format."""
    p = profiles[channel_id]
    data = p.model_dump()
    if "name" in changes:
        data["display_name"] = _clean(changes["name"], "name", 40)
        data["brand"]["name"] = data["display_name"]
    if "category" in changes:
        category = _clean(changes["category"], "category", 40)
        other = for_category(profiles, category)
        if other and other.id != channel_id:
            raise ChannelError(f"there is already a channel for {category}: one channel per category")
        data["category"] = category
    if "about" in changes:
        data["niche"]["definition"] = _clean(changes["about"], "description of what it posts", 600)
    if "brand_handle" in changes:
        data["brand"]["handle"] = changes["brand_handle"].strip()[:40]
    if "youtube_tags" in changes:
        tags = " ".join(changes["youtube_tags"].split())[:200]
        data["posting"]["tags"] = {**data["posting"]["tags"], "youtube_shorts": tags} if tags else {
            k: v for k, v in data["posting"]["tags"].items() if k != "youtube_shorts"}
    if "youtube_route" in changes:
        if changes["youtube_route"] not in ("manual", "api"):
            raise ChannelError("YouTube posts go out by hand (manual) or automatically (api)")
        data["posting"]["platforms"] = {**data["posting"]["platforms"], "youtube_shorts": changes["youtube_route"]}
    if "dry_run" in changes:
        data["posting"]["dry_run"] = str(changes["dry_run"]).lower() not in ("0", "false", "no", "off", "")
    if "youtube_daily_cap" in changes:
        try:
            cap = int(changes["youtube_daily_cap"])
        except (TypeError, ValueError):
            raise ChannelError("posts per day must be a whole number") from None
        if not 1 <= cap <= DEFAULT_YOUTUBE_PER_DAY:
            raise ChannelError(f"posts per day must be between 1 and {DEFAULT_YOUTUBE_PER_DAY}")
        data["posting"]["daily_cap"] = cap
    if "format" in changes:
        recipe = changes["format"] or None
        if recipe:
            from .formats import blueprints

            if recipe not in blueprints.load()[1]:
                raise ChannelError(f"there is no look called {recipe}")
        data["format"] = recipe
    updated = Profile(**data)
    save(db, updated)
    profiles[channel_id] = updated
    return updated


def delete(db: Database, profiles: dict[str, Profile], channel_id: str) -> None:
    """Remove a channel that has never made or posted anything. Its history is never thrown away."""
    if len(profiles) <= 1:
        raise ChannelError("keep at least one channel")
    with db.conn() as c:
        used = c.execute("SELECT (SELECT COUNT(*) FROM clips_ready WHERE profile_id=?) + "
                         "(SELECT COUNT(*) FROM posts WHERE profile_id=?) AS n", (channel_id, channel_id)).fetchone()["n"]
        if used:
            raise ChannelError("this channel already has clips or posts, so it cannot be deleted. Pause its accounts instead.")
        c.execute("DELETE FROM channels WHERE id=?", (channel_id,))
        c.execute("DELETE FROM schedules WHERE profile_id=?", (channel_id,))
    profiles.pop(channel_id, None)
