"""Sound effects for the picture effects: a whoosh with a zoom or a shake, a pop with a flash.

Every sound is CC0 (free, no credit, commercial use allowed). Each is filed with its source and licence, copied under
data/villomillo/sounds/ by hash, and kept quiet under the voice when it is mixed in (see render.py).
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess

import yaml

from .. import store
from ..config import ROOT
from . import music

FOLDER = ROOT / "data" / "villomillo" / "sounds"
LIST = ROOT / "config" / "villomillo" / "sounds.yaml"
KINDS = ("whoosh", "pop")
# the effects each sound goes with: a sound is only mixed in when its effect is on the clip
EFFECT_SOUND = {"punch_in": "whoosh", "zoom_out": "whoosh", "shake": "whoosh", "flash": "pop"}
VOLUME_DB = -12   # a sound effect sits well under the voice: audible as a hit, never louder than the speech


class SoundError(ValueError):
    """The message says what to change, in plain words."""


def listing(path: pathlib.Path | None = None) -> list[dict]:
    data = yaml.safe_load((path or LIST).read_text(encoding="utf-8"))
    return data["sounds"]


def _row(r) -> dict:
    return dict(r)


def sounds(db) -> list[dict]:
    with db.conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM sound_effects ORDER BY kind, name")]


def file_path(sound: dict, folder: pathlib.Path | None = None) -> pathlib.Path:
    return (folder or FOLDER) / f"{sound['sha']}.{sound['ext']}"


def probe(path: pathlib.Path) -> float:
    from core import binaries

    done = subprocess.run([binaries.ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, timeout=60)
    try:
        return float(done.stdout.strip())
    except ValueError:
        raise SoundError("That file is not sound the app can read.") from None


def add(db, path: pathlib.Path, *, name: str, kind: str, source: str, licence: str, original_name: str = "",
        folder: pathlib.Path | None = None) -> dict:
    if kind not in KINDS:
        raise SoundError("A sound must be a whoosh or a pop.")
    if not source.strip() or not licence.strip():
        raise SoundError("Say where the sound came from and its licence: every sound needs a licence record.")
    if path.suffix.lower() not in (".wav", ".mp3"):
        raise SoundError("A sound must be a WAV or MP3 file.")
    if path.stat().st_size > 5 * 1024 * 1024:
        raise SoundError("A sound effect must be 5 MB or smaller.")
    seconds = probe(path)
    if not 0.02 <= seconds <= 3.0:
        raise SoundError("A sound effect must be between 0.02 and 3 seconds long.")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    folder = folder or FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower().lstrip(".")
    shutil.copyfile(path, folder / f"{sha}.{ext}")
    with db.conn() as c:
        cur = c.execute("INSERT OR IGNORE INTO sound_effects (name, kind, sha, ext, duration_s, source, licence, original_name, added_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (name, kind, sha, ext, round(seconds, 3), source.strip(), licence.strip(), original_name or path.name, store.iso()))
        row = c.execute("SELECT * FROM sound_effects WHERE sha=?", (sha,)).fetchone()
    return _row(row)


def import_downloads(db, downloads: pathlib.Path | None = None, *, list_path: pathlib.Path | None = None,
                     folder: pathlib.Path | None = None) -> dict:
    """File every listed sound that is in the downloads folder. Anything else in Downloads is left alone."""
    folder_in = downloads or music.DOWNLOADS
    added, already, problems = [], [], []
    for entry in listing(list_path):
        path = folder_in / entry["file"]
        if not path.exists():
            continue
        with db.conn() as c:
            known = c.execute("SELECT name FROM sound_effects WHERE sha=?", (hashlib.sha256(path.read_bytes()).hexdigest(),)).fetchone()
        if known:
            already.append(entry["name"])
            continue
        try:
            add(db, path, name=entry["name"], kind=entry["kind"], source=entry["source"], licence="CC0", original_name=path.name, folder=folder)
            added.append(entry["name"])
        except SoundError as e:
            problems.append(f"{path.name}: {e}")
    return {"imported": added, "already": already, "problems": problems}


def pick(db, kind: str) -> dict | None:
    """The sound of a kind to use: the first one in the library, so the same sound is used every time."""
    options = [s for s in sounds(db) if s["kind"] == kind and file_path(s).exists()]
    return options[0] if options else None


def hits(db, effects: list[dict], folder: pathlib.Path | None = None) -> list[dict]:
    """The sound each picture effect goes with, timed to its start: [{path, start, volume, name}]. An effect with no sound
    in the library simply has none."""
    out = []
    for e in effects:
        kind = EFFECT_SOUND.get(e["kind"])
        sound = pick(db, kind) if kind else None
        if sound is not None:
            out.append({"path": str(file_path(sound, folder)), "start": e["start"], "volume": VOLUME_DB, "name": sound["name"]})
    return out
