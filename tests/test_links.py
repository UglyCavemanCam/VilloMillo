"""Pasted links (YouTube, Twitch, Kick) and the engine client's preview and chosen-moments calls."""

from __future__ import annotations

import json

import httpx
import pytest

from villomillo import clipper, links


@pytest.mark.parametrize(("text", "platform", "video_id", "url"), [
    ("https://www.youtube.com/watch?v=aB3dEfGhIjK&t=30s", "youtube", "aB3dEfGhIjK", "https://www.youtube.com/watch?v=aB3dEfGhIjK"),
    ("youtu.be/aB3dEfGhIjK", "youtube", "aB3dEfGhIjK", "https://www.youtube.com/watch?v=aB3dEfGhIjK"),
    ("https://www.youtube.com/shorts/aB3dEfGhIjK", "youtube", "aB3dEfGhIjK", "https://www.youtube.com/watch?v=aB3dEfGhIjK"),
    ("https://www.twitch.tv/videos/2345678901", "twitch", "tw_2345678901", "https://www.twitch.tv/videos/2345678901"),
    ("twitch.tv/video/2345678901?t=1h", "twitch", "tw_2345678901", "https://www.twitch.tv/videos/2345678901"),
    ("https://kick.com/video/8F5C2B1A-0D3E-4A6B-9C7D-1E2F3A4B5C6D", "kick", "kick_8f5c2b1a-0d3e-4a6b-9c7d-1e2f3a4b5c6d", None),
    ("https://kick.com/somestreamer/videos/8f5c2b1a-0d3e-4a6b-9c7d-1e2f3a4b5c6d", "kick", "kick_8f5c2b1a-0d3e-4a6b-9c7d-1e2f3a4b5c6d", None),
])
def test_links_are_recognised(text, platform, video_id, url):
    link = links.parse(text)
    assert (link.platform, link.video_id) == (platform, video_id)
    assert link.url == (url or link.url) and link.url.startswith("https://")


@pytest.mark.parametrize(("text", "why"), [
    ("", "paste one video link"),
    ("two links https://youtu.be/aB3dEfGhIjK", "paste one video link"),
    ("https://www.twitch.tv/somestreamer", "not a past broadcast"),
    ("https://www.twitch.tv/somestreamer/clip/FunnyClipName", "not a past broadcast"),
    ("https://kick.com/somestreamer", "not a past broadcast"),
    ("https://www.youtube.com/@channel", "does not point to a video"),
    ("https://vimeo.com/123456", "YouTube, Twitch and Kick"),
    ("https://youtube.com.evil.net/watch?v=aB3dEfGhIjK", "YouTube, Twitch and Kick"),
])
def test_other_links_are_refused_with_a_reason(text, why):
    with pytest.raises(links.LinkError, match=why):
        links.parse(text)


def engine_with(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            seen.append(json.loads(request.content))
            return httpx.Response(200, json={"job_id": 7})
        if request.url.path == "/videos/tw_1/moments":
            return httpx.Response(200, json=[{"id": 1, "score": 90}])
        if request.url.path.endswith("/moments"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={})

    return clipper.Engine("http://engine.test", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_a_preview_asks_for_every_moment_and_no_clips():
    seen: list = []
    link = links.parse("https://www.twitch.tv/videos/1")
    assert clipper.start_preview(engine_with(seen), link, focus="talk") == 7
    assert seen[0]["preview"] is True and seen[0]["min_score"] == 0 and seen[0]["max_clips"] == clipper.PREVIEW_MOMENTS
    assert seen[0]["url"] == link.url and "moments" not in seen[0]


def test_choosing_moments_sends_only_their_numbers():
    seen: list = []
    clipper.start_render(engine_with(seen), links.parse("https://www.twitch.tv/videos/1"), [1, 3])
    assert seen[0]["moments"] == [1, 3] and "preview" not in seen[0] and seen[0]["max_clips"] == 2


def test_the_engines_moments_are_read_by_video_id():
    assert engine_with([]).moments("tw_1") == [{"id": 1, "score": 90}]
    assert engine_with([]).moments("unknown") == []
