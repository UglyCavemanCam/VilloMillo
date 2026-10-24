"""Recognise a pasted video link: YouTube, Twitch or Kick, and nothing else.

The ids match the clipping engine's own (YouTube: 11 characters, Twitch: tw_<number>, Kick: kick_<uuid>), so a
video means the same thing on both sides. Live channel pages, Twitch clips and other sites are refused with a reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_YOUTUBE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([0-9A-Za-z_-]{11})")
_TWITCH = re.compile(r"twitch\.tv/videos?/(\d+)", re.IGNORECASE)
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_KICK = re.compile(rf"kick\.com/(?:video/|[\w.-]+/videos/)({_UUID})", re.IGNORECASE)
HOSTS = {"youtube": ("youtube.com", "youtu.be"), "twitch": ("twitch.tv",), "kick": ("kick.com",)}


class LinkError(ValueError):
    """The link is not a video VilloMillo can clip. The message says why, in plain words."""


@dataclass(frozen=True)
class Link:
    platform: str  # youtube | twitch | kick
    video_id: str  # the engine's id for it
    url: str  # the link to hand the engine

    @property
    def name(self) -> str:
        return {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick"}[self.platform]


def _host(url: str) -> str:
    parts = urlsplit(url if "//" in url else "//" + url.lstrip("/"))
    return (parts.hostname or "").lower()


def _on(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def parse(text: str) -> Link:
    url = (text or "").strip().strip('"')
    if not url or " " in url:
        raise LinkError("paste one video link")
    if "//" not in url:
        url = "https://" + url.lstrip("/")
    host = _host(url)
    if _on(host, HOSTS["twitch"]):
        m = _TWITCH.search(url)
        if not m:
            raise LinkError("that Twitch link is not a past broadcast: paste a link like twitch.tv/videos/123456789 (not a live channel or a clip)")
        return Link("twitch", f"tw_{m.group(1)}", f"https://www.twitch.tv/videos/{m.group(1)}")
    if _on(host, HOSTS["kick"]):
        m = _KICK.search(url)
        if not m:
            raise LinkError("that Kick link is not a past broadcast: paste a link to a Kick video (not a live channel or a clip)")
        return Link("kick", f"kick_{m.group(1).lower()}", url)
    if _on(host, HOSTS["youtube"]):
        m = _YOUTUBE.search(url)
        if not m:
            raise LinkError("that YouTube link does not point to a video")
        return Link("youtube", m.group(1), f"https://www.youtube.com/watch?v={m.group(1)}")
    raise LinkError("VilloMillo clips YouTube, Twitch and Kick videos")
