"""The music library: tracks you download from the YouTube Audio Library, filed with a mood, the categories they suit and their licence.

Files live in data/villomillo/music/ named by their hash. Every track has a licence record (source, licence text, attribution, date),
so nothing is in the library without saying where it came from. The AI is only offered a mood when this library has a track that
fits the clip, and code, not the AI, picks the track. With an empty library nothing is decided about music and nothing fails.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shutil
import subprocess

import yaml

from .. import store
from ..config import ROOT

FOLDER = ROOT / "data" / "villomillo" / "music"
SHORTLIST = ROOT / "config" / "villomillo" / "music_shortlist.yaml"
DOWNLOADS = pathlib.Path.home() / "Downloads"
MOODS = ("calm", "inspirational", "dramatic", "dark", "sad", "bright", "happy", "funky", "angry", "romantic")
CATEGORIES = ("Podcasts", "Talks", "Stream clips", "Other", "Campaigns")
TYPES = (".mp3", ".wav")
MAX_BYTES = 40 * 1024 * 1024
MIN_SECONDS, MAX_SECONDS = 20.0, 900.0
MEDIA = {".mp3": "audio/mpeg", ".wav": "audio/wav"}
# What a clip's own sound can contain that music must never be laid over: the source already has music, or a crowd.
SOURCE_SOUNDS = ("Music", "Singing", "Cheering", "Applause", "Crowd")
SOURCE_LIMIT = 0.35


class MusicError(ValueError):
    """The message says what to change, in plain words."""


def norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


# --- the shortlist ----------------------------------------------------------------------------------------------

def shortlist(path: pathlib.Path | None = None) -> dict:
    data = yaml.safe_load((path or SHORTLIST).read_text(encoding="utf-8"))
    for t in data["tracks"]:
        if t["mood"] not in MOODS or not set(t["categories"]) <= set(CATEGORIES):
            raise MusicError(f"the shortlist has a bad mood or category for {t['title']!r}")
    return data


def match_shortlist(file_stem: str, entries: list[dict]) -> dict | None:
    """The shortlist entry a downloaded file belongs to. Chrome names it "Title - Artist.mp3", turns "/" into "_", and may add " (1)"."""
    stem = norm(re.sub(r"\s*\(\d+\)$", "", file_stem))
    best = None
    for e in entries:
        title, artist = norm(e["title"]), norm(e["artist"])
        if stem == f"{title} {artist}" or (stem.startswith(title + " ") and artist in stem):
            if best is None or len(title) > len(norm(best["title"])):
                best = e
    return best


# --- looking at a file --------------------------------------------------------------------------------------------

def probe(path: pathlib.Path) -> dict:
    from core import binaries

    done = subprocess.run([binaries.ffprobe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise MusicError("That file is not audio the app can read.")
    info = json.loads(done.stdout or "{}")
    if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
        raise MusicError("That file has no sound in it.")
    return {"duration_s": float(info.get("format", {}).get("duration") or 0)}


# --- the tracks ---------------------------------------------------------------------------------------------------

def _row(r) -> dict:
    d = dict(r)
    d["categories"] = json.loads(d.pop("categories_json") or "[]")
    return d


def tracks(db) -> list[dict]:
    with db.conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM music_tracks ORDER BY mood, title")]


def get(db, track_id: int) -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM music_tracks WHERE id=?", (track_id,)).fetchone()
    return _row(row) if row else None


def file_path(track: dict, folder: pathlib.Path | None = None) -> pathlib.Path:
    return (folder or FOLDER) / f"{track['sha']}.{track['ext']}"


def add_track(db, path: pathlib.Path, *, title: str, artist: str, mood: str, categories: list[str], genre: str = "",
              source: str, licence: str, attribution: str = "none", original_name: str = "", folder: pathlib.Path | None = None) -> dict:
    """File a track in the library. Raises MusicError when the file or its details cannot be accepted."""
    title, artist, genre = title.strip(), artist.strip(), (genre or "").strip()
    if not title or not artist:
        raise MusicError("A track needs a title and an artist.")
    if mood not in MOODS:
        raise MusicError("Pick one of the moods: " + ", ".join(MOODS) + ".")
    if not categories or not set(categories) <= set(CATEGORIES):
        raise MusicError("Pick at least one category the track suits.")
    if not source.strip() or not licence.strip():
        raise MusicError("Say where the track came from and what its licence is: every track needs a licence record.")
    ext = path.suffix.lower()
    if ext not in TYPES:
        raise MusicError("Music must be an MP3 or WAV file.")
    if path.stat().st_size > MAX_BYTES:
        raise MusicError("A track must be 40 MB or smaller.")
    seconds = probe(path)["duration_s"]
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise MusicError("A track must be between 20 seconds and 15 minutes long.")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    singing = singing_score(path)
    with db.conn() as c:
        existing = c.execute("SELECT * FROM music_tracks WHERE sha=?", (sha,)).fetchone()
    if existing:
        raise MusicError(f"That file is already in the library as {existing['title']!r}.")
    folder = folder or FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, folder / f"{sha}.{ext.lstrip('.')}")
    with db.conn() as c:
        cur = c.execute("INSERT INTO music_tracks (title, artist, genre, mood, duration_s, sha, ext, categories_json, source, licence, "
                        "attribution, original_name, added_at, singing) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (title, artist, genre, mood, round(seconds, 2), sha, ext.lstrip("."), json.dumps(sorted(set(categories))),
                         source.strip(), licence.strip(), attribution, original_name or path.name, store.iso(), singing))
        row = c.execute("SELECT * FROM music_tracks WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row(row)


def update_track(db, track_id: int, *, mood: str, categories: list[str]) -> dict:
    if mood not in MOODS:
        raise MusicError("Pick one of the moods: " + ", ".join(MOODS) + ".")
    if not categories or not set(categories) <= set(CATEGORIES):
        raise MusicError("Pick at least one category the track suits.")
    with db.conn() as c:
        c.execute("UPDATE music_tracks SET mood=?, categories_json=? WHERE id=?", (mood, json.dumps(sorted(set(categories))), track_id))
    track = get(db, track_id)
    if track is None:
        raise MusicError("That track is gone.")
    return track


def delete_track(db, track_id: int, folder: pathlib.Path | None = None) -> None:
    track = get(db, track_id)
    if track is None:
        return
    with db.conn() as c:
        c.execute("DELETE FROM music_tracks WHERE id=?", (track_id,))
    file_path(track, folder).unlink(missing_ok=True)


def import_downloads(db, downloads: pathlib.Path | None = None, *, list_path: pathlib.Path | None = None,
                     folder: pathlib.Path | None = None) -> dict:
    """File every shortlisted track found in the downloads folder. Files that are not on the shortlist are left alone."""
    data = shortlist(list_path)
    entries = data["tracks"]
    imported, already, problems = [], [], []
    for path in sorted((downloads or DOWNLOADS).iterdir()):
        if not path.is_file() or path.suffix.lower() not in TYPES:
            continue
        entry = match_shortlist(path.stem, entries)
        if entry is None:
            continue
        try:
            track = add_track(db, path, title=entry["title"], artist=entry["artist"], mood=entry["mood"], categories=entry["categories"],
                              genre=entry.get("genre", ""), source=data["source"], licence=" ".join(data["licence"].split()),
                              attribution=data.get("attribution", "none"), original_name=path.name, folder=folder)
            imported.append(track["title"])
        except MusicError as e:
            (already if "already in the library" in str(e) else problems).append(f"{path.name}: {e}")
    return {"imported": imported, "already": already, "problems": problems}


def checklist(db, list_path: pathlib.Path | None = None) -> list[dict]:
    """Every shortlisted track, and whether it is in the library yet."""
    have = {(norm(t["title"]), norm(t["artist"])) for t in tracks(db)}
    return [{**e, "have": (norm(e["title"]), norm(e["artist"])) in have} for e in shortlist(list_path)["tracks"]]


# --- choosing for a clip ------------------------------------------------------------------------------------------

def eligible(db, category: str | None, campaign: bool) -> list[dict]:
    """Tracks usable for a clip: those filed for its category, plus the Campaigns ones for a campaign clip. The file must exist."""
    wanted = {c.lower() for c in ([category] if category else [])} | ({"campaigns"} if campaign else set())
    out = []
    for t in tracks(db):
        cats = {c.lower() for c in t["categories"]}
        if (not category and not campaign) or cats & wanted:
            if file_path(t).exists():
                out.append(t)
    return out


def available_moods(db, category: str | None, campaign: bool) -> list[str]:
    have = {t["mood"] for t in eligible(db, category, campaign)}
    return [m for m in MOODS if m in have]


def pick(db, category: str | None, campaign: bool, mood: str) -> dict | None:
    """The track for a mood: the one used least recently, so clips do not all carry the same song."""
    options = [t for t in eligible(db, category, campaign) if t["mood"] == mood]
    if not options:
        return None
    return sorted(options, key=lambda t: (t["last_used_at"] or "", t["id"]))[0]


def mark_used(db, track_id: int) -> None:
    with db.conn() as c:
        c.execute("UPDATE music_tracks SET last_used_at=? WHERE id=?", (store.iso(), track_id))


def singing_score(path: pathlib.Path) -> float | None:
    """How much the track sounds sung (0 to 1), from its middle 30 seconds. None when the helper file is not installed."""
    try:
        from analysis import panns
    except Exception:
        return None
    if not panns.available():
        return None
    import numpy as np

    from core import binaries

    seconds = probe(path)["duration_s"]
    start = max(seconds / 2 - 15, 0.0)
    done = subprocess.run([binaries.ffmpeg(), "-v", "error", "-ss", f"{start:.2f}", "-t", "30", "-i", str(path), "-vn", "-ac", "1",
                           "-ar", str(panns.SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, timeout=120)
    audio = np.frombuffer(done.stdout, dtype=np.float32)
    window = panns.SAMPLE_RATE * 10
    if len(audio) < window // 2:
        return None
    windows = np.pad(audio, (0, (-len(audio)) % window)).reshape(-1, window)
    scores = panns.tag(windows).max(axis=0)
    names = panns.labels()
    return float(scores[names.index("Singing")]) if "Singing" in names else None


SINGING_WARNING = 0.35


def source_has_music(source: pathlib.Path, start: float, end: float) -> bool | None:
    """True when the clip's own sound has music, singing or a crowd in it; None when that cannot be listened for here
    (the audio tagger's weights are not installed)."""
    try:
        from analysis import panns
    except Exception:
        return None
    if not panns.available():
        return None
    import numpy as np

    from core import binaries

    done = subprocess.run([binaries.ffmpeg(), "-v", "error", "-ss", f"{start:.2f}", "-t", f"{min(end - start, 30):.2f}", "-i", str(source),
                           "-vn", "-ac", "1", "-ar", str(panns.SAMPLE_RATE), "-f", "f32le", "-"], capture_output=True, timeout=120)
    audio = np.frombuffer(done.stdout, dtype=np.float32)
    window = panns.SAMPLE_RATE * 10
    if len(audio) < window // 2:
        return None
    pad = (-len(audio)) % window
    windows = np.pad(audio, (0, pad)).reshape(-1, window)
    scores = panns.tag(windows).max(axis=0)
    names = panns.labels()
    return any(scores[names.index(n)] >= SOURCE_LIMIT for n in SOURCE_SOUNDS if n in names)


def backfill_singing(db, folder: pathlib.Path | None = None) -> int:
    """Score every track filed before the check existed. Returns how many were scored."""
    done = 0
    for t in tracks(db):
        if t["singing"] is None:
            score = singing_score(file_path(t, folder))
            if score is not None:
                with db.conn() as c:
                    c.execute("UPDATE music_tracks SET singing=? WHERE id=?", (score, t["id"]))
                done += 1
    return done
