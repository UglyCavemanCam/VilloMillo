"""The clean slate: clear clips, posts and queues so the app starts again with the new formats.

preview() and backup() remove nothing. wipe() runs only with the digest that preview() printed, after a backup of exactly
those numbers has been made and checked, and once the clipping engine has removed its own copies of the clips.

Kept: channels and their sign-ins, campaigns, Research channels and settings, spending history, saved AI answers,
downloaded videos and transcripts.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import shutil
import sqlite3
import zipfile
from datetime import datetime

from . import clipper, store
from .config import ROOT
from .db import Database

BACKUP_ROOT = pathlib.Path.home() / "VilloMillo-backups"  # outside the project folder
APP_FILE_DIRS = [ROOT / "data" / "villomillo" / "outbox", ROOT / "data" / "villomillo" / "previews",
                 ROOT / "data" / "villomillo" / "thumbs"]
ENGINE_CLIP_DIR = ROOT / "data" / "clips"  # the engine's own copies; removed through its API, never by deleting files here
BACKUP_FILE_DIRS = [*APP_FILE_DIRS, ENGINE_CLIP_DIR]

COUNTS = {
    "clips": "SELECT COUNT(*) FROM clips_ready",
    "posts": "SELECT COUNT(*) FROM posts",
    "clip_jobs": "SELECT COUNT(*) FROM clip_jobs",
    "runs": "SELECT COUNT(*) FROM runs",
    "campaign_queue": "SELECT COUNT(*) FROM campaign_queue",
    "campaign_hand_ins": "SELECT COUNT(*) FROM campaign_submissions",
    "content_rewards_attempts": "SELECT COUNT(*) FROM rewards_submissions",
    "research_waiting": "SELECT COUNT(*) FROM research_candidates WHERE state IN ('queued', 'started')",
    "tasks_waiting": "SELECT COUNT(*) FROM tasks WHERE status = 'queued'",
    "tasks_running": "SELECT COUNT(*) FROM tasks WHERE status = 'running'",
}
MANIFEST = "manifest.json"


class CleanSlateError(RuntimeError):
    pass


def _counts(c: sqlite3.Connection) -> dict[str, int]:
    return {name: c.execute(sql).fetchone()[0] for name, sql in COUNTS.items()}


def _digest(counts: dict[str, int]) -> str:
    return hashlib.sha256(json.dumps(counts, sort_keys=True).encode()).hexdigest()[:10]


def _files(dirs: list[pathlib.Path]) -> dict[str, int]:
    files = [f for d in dirs if d.exists() for f in d.rglob("*") if f.is_file()]
    return {"files": len(files), "megabytes": round(sum(f.stat().st_size for f in files) / 1e6, 1)}


def preview(db: Database) -> dict:
    """What a clean slate would remove right now, with the digest that confirms those exact numbers."""
    with db.conn() as c:
        counts = _counts(c)
    return {"counts": counts, "digest": _digest(counts), "app_files": _files(APP_FILE_DIRS),
            "engine_clip_files": _files([ENGINE_CLIP_DIR])}


def backup(db: Database, *, root: pathlib.Path = BACKUP_ROOT, when: datetime | None = None) -> pathlib.Path:
    """A copy of the database and every clip file, checked before it is trusted. Returns the folder it was made in."""
    folder = root / f"clean-slate-{(when or datetime.now()).strftime('%Y%m%d-%H%M%S')}"
    folder.mkdir(parents=True, exist_ok=False)
    snapshot = folder / pathlib.Path(db.path).name
    src, dst = sqlite3.connect(db.path, timeout=60), sqlite3.connect(snapshot)
    try:
        src.backup(dst)  # a consistent copy, even while the app is writing
    finally:
        dst.close()
        src.close()

    zip_path = folder / "clip-files.zip"
    entries = [(f, d) for d in BACKUP_FILE_DIRS if d.exists() for f in d.rglob("*") if f.is_file()]
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as z:
        for f, d in entries:
            z.write(f, f"{d.name}/{f.relative_to(d).as_posix()}")  # e.g. outbox/solved/12.mp4, clips/<video>/...

    check = sqlite3.connect(snapshot)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        counts = _counts(check)
    finally:
        check.close()
    with zipfile.ZipFile(zip_path) as z:
        problem = z.testzip()
        zipped = len(z.namelist())
    if integrity != "ok" or problem is not None or zipped != len(entries):
        raise CleanSlateError(f"the backup did not check out (database: {integrity}, zip: {problem or 'ok'}, "
                              f"{zipped} of {len(entries)} files); nothing was removed")
    manifest = {"made_at": store.iso(), "digest": _digest(counts), "counts": counts,
                "files_in_zip": zipped, "database": snapshot.name, "verified": True}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return folder


def _verified_backup(root: pathlib.Path, digest: str) -> pathlib.Path | None:
    for manifest in sorted(root.glob(f"clean-slate-*/{MANIFEST}"), reverse=True):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("verified") and data.get("digest") == digest:
            return manifest.parent
    return None


def wipe(db: Database, digest: str, *, root: pathlib.Path = BACKUP_ROOT, engine: clipper.Engine | None = None) -> dict:
    """Remove everything the numbers for `digest` describe. Refuses unless the numbers, the backup and the engine all agree."""
    now = preview(db)
    if now["digest"] != digest:
        raise CleanSlateError("the numbers have changed since they were shown: run preview again")
    if now["counts"]["tasks_running"]:
        raise CleanSlateError("work is running right now: stop the app first, then try again")
    if _verified_backup(root, digest) is None:
        raise CleanSlateError("no checked backup matches these numbers: run backup first")

    engine = engine or clipper.Engine()
    with db.conn() as c:
        videos = sorted({r[0] for r in c.execute(
            "SELECT video_id FROM clips_ready UNION SELECT video_id FROM clip_jobs UNION SELECT video_id FROM runs")})
    removed_engine_clips = 0
    try:
        for video_id in videos:
            for clip in engine.clips(video_id):
                if engine.delete_clip(clip["id"]):
                    removed_engine_clips += 1
    except clipper.EngineUnavailable as e:
        raise CleanSlateError(f"{e}. Start the clipping engine, so its own copies can be removed too") from None

    with db.conn(immediate=True) as c:
        c.execute("DELETE FROM copy_options WHERE post_id IN (SELECT id FROM posts)")
        c.execute("DELETE FROM post_stats WHERE post_id IN (SELECT id FROM posts)")
        c.execute("DELETE FROM campaign_submissions")
        c.execute("DELETE FROM rewards_submissions")
        c.execute("DELETE FROM posts")
        c.execute("DELETE FROM clips_ready")
        c.execute("DELETE FROM clip_jobs")
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM campaign_queue")
        c.execute("DELETE FROM research_candidates WHERE state IN ('queued', 'started')")
        c.execute("DELETE FROM tasks WHERE status = 'queued'")

    removed_files = _files(APP_FILE_DIRS)["files"]
    for d in APP_FILE_DIRS:
        if d.exists():
            for child in d.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()

    after = preview(db)
    leftover = {k: v for k, v in after["counts"].items() if k in ("clips", "posts", "clip_jobs", "runs", "campaign_queue",
                                                                  "campaign_hand_ins", "content_rewards_attempts") and v}
    if leftover:
        raise CleanSlateError(f"something was left behind: {leftover}. The backup in {root} still holds everything")
    return {"removed": now["counts"], "engine_clips_removed": removed_engine_clips,
            "app_files_removed": removed_files, "left": after["counts"]}
