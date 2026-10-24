"""A small YouTube Data API client that counts every call and never leaks the API key."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import httpx

from .quota import GENERAL, SEARCH, Quota, QuotaExceeded

API = "https://www.googleapis.com/youtube/v3"
_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class YouTubeError(RuntimeError):
    pass


def parse_duration(text: str | None) -> int:
    """'PT2H43M42S' -> 9822 seconds. Unparseable or missing -> 0."""
    m = _DURATION.match(text or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def _count(stats: dict, name: str) -> int | None:
    value = stats.get(name)
    return int(value) if value is not None else None  # hidden counts are absent, not zero


def normalize(item: dict) -> dict:
    """One `videos.list` item as the flat dict the rest of the app uses."""
    sn, cd, st = item.get("snippet", {}), item.get("contentDetails", {}), item.get("statistics", {})
    return {
        "video_id": item["id"],
        "channel_id": sn.get("channelId"),
        "channel_title": sn.get("channelTitle"),
        "title": sn.get("title", ""),
        "description": sn.get("description", ""),
        "tags": sn.get("tags", []),
        "published_at": sn.get("publishedAt", ""),
        "language": sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
        "live_status": sn.get("liveBroadcastContent", "none"),
        "duration_s": parse_duration(cd.get("duration")),
        "views": _count(st, "viewCount"),
        "likes": _count(st, "likeCount"),
        "comments": _count(st, "commentCount"),
    }


class YouTube:
    def __init__(self, api_key: str, quota: Quota, client: httpx.Client | None = None):
        self._key = api_key
        self.quota = quota
        self.http = client or httpx.Client(timeout=30)

    def _get(self, path: str, bucket: str, **params) -> dict:
        self.quota.charge(bucket, 1)
        params = {k: v for k, v in params.items() if v is not None}
        try:
            r = self.http.get(f"{API}/{path}", params={**params, "key": self._key})
        except httpx.HTTPError as e:
            # never format the exception itself: some include the full URL, and the URL has the key
            raise YouTubeError(f"{path}: network error ({type(e).__name__})") from None
        if r.status_code == 200:
            return r.json()
        reasons = []
        try:
            reasons = [x.get("reason") for x in r.json().get("error", {}).get("errors", [])]
        except ValueError:
            pass
        if r.status_code == 403 and "quotaExceeded" in reasons:
            self.quota.exhaust(bucket)
            raise QuotaExceeded(bucket, self.quota.caps[bucket], self.quota.caps[bucket])
        raise YouTubeError(f"{path}: HTTP {r.status_code} {', '.join(map(str, reasons))}".strip())

    # --- channels -------------------------------------------------------------------------------
    def channel(self, *, handle: str | None = None, channel_id: str | None = None) -> dict:
        data = self._get(
            "channels", GENERAL, part="snippet,contentDetails,statistics",
            forHandle=handle, id=channel_id,
        )
        items = data.get("items") or []
        if not items:
            raise YouTubeError(f"no YouTube channel found for {handle or channel_id}")
        ch = items[0]
        return {
            "channel_id": ch["id"],
            "title": ch["snippet"]["title"],
            "uploads_playlist": ch["contentDetails"]["relatedPlaylists"]["uploads"],
            "subscribers": _count(ch.get("statistics", {}), "subscriberCount"),
        }

    def recent_upload_ids(self, playlist_id: str, *, limit: int = 50) -> list[str]:
        """Newest-first video ids from a channel's uploads playlist (1 unit per page of 50)."""
        ids: list[str] = []
        token = None
        while len(ids) < limit:
            data = self._get(
                "playlistItems", GENERAL, part="contentDetails", playlistId=playlist_id,
                maxResults=min(50, limit - len(ids)), pageToken=token,
            )
            ids += [i["contentDetails"]["videoId"] for i in data.get("items", [])]
            token = data.get("nextPageToken")
            if not token:
                break
        return ids[:limit]

    # --- videos ---------------------------------------------------------------------------------
    def videos(self, ids: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 50):
            data = self._get(
                "videos", GENERAL, part="snippet,contentDetails,statistics", id=",".join(ids[i : i + 50])
            )
            out += [normalize(x) for x in data.get("items", [])]
        return out

    def most_popular(self, *, region: str, category: int | None, limit: int = 50) -> list[dict]:
        data = self._get(
            "videos", GENERAL, part="snippet,contentDetails,statistics", chart="mostPopular",
            regionCode=region, videoCategoryId=category, maxResults=limit,
        )
        return [normalize(x) for x in data.get("items", [])]

    def search(self, query: str, *, days: int, language: str | None = None, region: str | None = None) -> list[str]:
        """Video ids for a keyword search of long videos from the last `days` days, most viewed first."""
        after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = self._get(
            "search", SEARCH, part="id", q=query, type="video", order="viewCount", videoDuration="long",
            publishedAfter=after, maxResults=25, relevanceLanguage=language, regionCode=region,
        )
        return [i["id"]["videoId"] for i in data.get("items", []) if i.get("id", {}).get("videoId")]
