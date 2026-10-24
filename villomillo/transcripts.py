"""Caption text for a video, without downloading the video itself. Stored once per video."""

from __future__ import annotations

import json

import httpx
import yt_dlp

from . import store
from .db import Database

CHUNK_CHARS = 24_000  # about 6,000 tokens: small enough to read in one Jev question
MAX_CHUNKS = 6  # long episodes are sampled evenly; this keeps the cost per video bounded


class TranscriptUnavailable(RuntimeError):
    pass


def _caption_url(info: dict) -> tuple[str, str] | None:
    for source, key in (("manual", "subtitles"), ("auto", "automatic_captions")):
        tracks = info.get(key) or {}
        for lang in ("en", "en-US", "en-GB"):
            for fmt in tracks.get(lang, []):
                if fmt.get("ext") == "json3":
                    return fmt["url"], source
    return None


def fetch(video_id: str, *, http: httpx.Client | None = None) -> tuple[str, str]:
    """(text, source) for a video's English captions. Raises TranscriptUnavailable when there are none."""
    opts = {"quiet": True, "skip_download": True, "no_warnings": True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise TranscriptUnavailable(f"could not read the video page ({str(e)[:120]})") from None
    found = _caption_url(info or {})
    if not found:
        raise TranscriptUnavailable("no English captions")
    url, source = found
    client = http or httpx.Client(timeout=60)
    r = client.get(url)
    if r.status_code != 200:
        raise TranscriptUnavailable(f"caption download failed (HTTP {r.status_code})")
    events = r.json().get("events", [])
    text = "".join(seg.get("utf8", "") for e in events for seg in (e.get("segs") or []))
    text = " ".join(text.split())
    if len(text) < 500:
        raise TranscriptUnavailable("captions are too short to use")
    return text, source


def get(db: Database, video_id: str) -> str:
    """The stored transcript, fetching and saving it the first time."""
    with db.conn() as c:
        row = c.execute("SELECT text FROM transcripts WHERE video_id=?", (video_id,)).fetchone()
    if row:
        return row["text"]
    text, source = fetch(video_id)
    with db.conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO transcripts (video_id, source, chars, text, fetched_at) VALUES (?,?,?,?,?)",
            (video_id, source, len(text), text, store.iso()),
        )
    return text


def chunks(text: str, *, size: int = CHUNK_CHARS, limit: int = MAX_CHUNKS) -> list[str]:
    """Split at word boundaries. If there are more pieces than `limit`, keep evenly spaced ones."""
    words, out, current, length = text.split(), [], [], 0
    for w in words:
        current.append(w)
        length += len(w) + 1
        if length >= size:
            out.append(" ".join(current))
            current, length = [], 0
    if current:
        out.append(" ".join(current))
    if len(out) <= limit:
        return out
    step = (len(out) - 1) / (limit - 1)
    return [out[round(i * step)] for i in range(limit)]


def dumps(answers: dict) -> str:
    return json.dumps(answers, sort_keys=True)
