"""Is everything VilloMillo needs actually working? One set of checks, used by the Settings panel and `villomillo doctor`.

Each check says what it found and, when something is missing, the one thing to do about it. The worker proves it is alive by
writing a heartbeat every few seconds; a worker that is not running (only `serve` was started) shows up here.
"""

from __future__ import annotations

import shutil
from datetime import timedelta

from . import accounts, engine_ai, engine_manager, posting, rewards, safety, secrets, spend, store
from .config import ROOT
from .db import Database

BEAT_KEY = "worker_beat"
TASK_KEY = "worker_task"
STALE_S = 60  # a heartbeat older than this means the worker is stuck or gone


def beat(db: Database, task: str | None = None) -> None:
    """The worker calls this every loop (and around each task) so the dashboard can tell it is alive and what it is doing."""
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (BEAT_KEY, store.iso()))
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (TASK_KEY, task or ""))


def worker(db: Database) -> dict:
    """{"state": "running" | "stuck" | "off", "age_s", "task"}."""
    with db.conn() as c:
        rows = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM meta WHERE key IN (?, ?)", (BEAT_KEY, TASK_KEY))}
    if BEAT_KEY not in rows:
        return {"state": "off", "age_s": None, "task": ""}
    age = (store.now() - store.parse_time(rows[BEAT_KEY])).total_seconds()
    return {"state": "running" if age < STALE_S else "stuck", "age_s": int(age), "task": rows.get(TASK_KEY, "")}


def _check(key: str, name: str, state: str, detail: str, fix: str = "") -> dict:
    return {"key": key, "name": name, "state": state, "detail": detail, "fix": fix}  # state: ok | warn | bad


def checks(sv) -> list[dict]:
    out = []
    eng = engine_manager.status()
    out.append(_check("engine", "Clip engine", "ok" if eng["state"] == "on" else ("warn" if eng["state"] in ("starting", "not_responding") else "bad"),
                      eng["text"] + (f" ({eng['last_error']})" if eng["last_error"] and eng["state"] != "on" else ""),
                      "" if eng["state"] == "on" else "Open Settings and press Start, or start the app with run-app.bat."))
    ai = engine_ai.settings(sv.db)
    if engine_ai.active(sv.db):
        out.append(_check("moments_ai", "Finding moments", "ok", f"Claude: {ai['find']} reads the video, {ai['judge']} judges the finalists"))
    elif ai["use_claude"]:
        out.append(_check("moments_ai", "Finding moments", "warn", "Using the engine's own local model: slow on long videos (no Claude key stored)",
                          "Store your Claude key and restart: .\\.venv\\Scripts\\python.exe -m villomillo key claude"))
    else:
        out.append(_check("moments_ai", "Finding moments", "warn", "Using the engine's own local model: slow on long videos", "Settings, Finding moments: switch Claude on."))
    w = worker(sv.db)
    out.append(_check("worker", "Background worker", "ok" if w["state"] == "running" else "bad",
                      {"running": "Working" + (f" on: {w['task']}" if w["task"] else ", waiting for the next job"),
                       "stuck": f"No sign of life for {w['age_s']} seconds", "off": "Not running: links are never looked at and nothing posts"}[w["state"]],
                      "" if w["state"] == "running" else "Start the app with run-app.bat (python -m villomillo run), not serve."))
    for key, name, need in ((secrets.YOUTUBE, "YouTube Data API key", True), (secrets.JEV, "Jev key", True), (secrets.CLAUDE, "Claude key", True),
                            (secrets.TWITCH_ID, "Twitch client ID", False), (secrets.TWITCH_SECRET, "Twitch client secret", False)):
        try:
            stored = secrets.is_stored(key)
        except Exception:  # a locked or missing keychain must not break the check
            stored = False
        short = {secrets.YOUTUBE: "youtube", secrets.JEV: "jev", secrets.CLAUDE: "claude", secrets.TWITCH_ID: "twitch", secrets.TWITCH_SECRET: "twitch"}[key]
        out.append(_check(f"key:{key}", name, "ok" if stored else ("bad" if need else "warn"), "Stored" if stored else "Missing",
                          "" if stored else f"Run: .\\.venv\\Scripts\\python.exe -m villomillo key {short}"
                          + ("" if need else " (only needed for Research to find Twitch channels)")))
    out.append(_check("ffmpeg", "ffmpeg", "ok" if shutil.which("ffmpeg") else "warn", "Found" if shutil.which("ffmpeg") else "Not on the PATH (the engine may bring its own)"))
    client = posting.YOUTUBE_CLIENT.exists()
    out.append(_check("youtube_client", "YouTube sign-in file", "ok" if client else "warn", "Found" if client else "Missing",
                      "" if client else f"Save your Google OAuth client file as {posting.YOUTUBE_CLIENT}"))
    for p in sv.profiles.values():
        name = p.brand.name or p.display_name
        connected = (ROOT / "data" / "villomillo" / f"youtube_token_{p.id}.json").exists()
        out.append(_check(f"yt:{p.id}", f"{name}: YouTube", "ok" if connected else "warn", "Connected" if connected else "Not connected",
                          "" if connected else f"Open the {name} channel page and press Connect YouTube."))
        if p.posting is None:
            continue
        platform = "youtube_shorts"
        route, why = accounts.effective_route(sv.db, p, platform)
        if p.posting.dry_run:
            mode, state, fix = "Dry run: posts are only written to a folder", "warn", "Turn on Send posts for real on the channel page when ready."
        elif route == "manual":
            mode, state, fix = f"By hand: {why}", "warn", "Set YouTube to automatic and record the audit as passed on the channel page."
        else:
            mode, state, fix = f"Automatic ({p.posting.privacy} uploads)", "ok", ""
        out.append(_check(f"post:{p.id}", f"{name}: posting", state, mode, fix))
    rw = rewards.settings(sv.db)
    if rw["missing"]:
        out.append(_check("rewards", "Content Rewards hand-in", "warn", "Browser tool not installed", "Start the app with run-app.bat: it installs it."))
    elif rw["signed_in"]:
        out.append(_check("rewards", "Content Rewards hand-in", "warn" if rw["dry_run"] else "ok", "Signed in, trial mode: nothing is sent" if rw["dry_run"] else "Signed in",
                          "Turn trial mode off on the Campaigns page when the first picture looks right." if rw["dry_run"] else ""))
    else:
        out.append(_check("rewards", "Content Rewards hand-in", "warn", "Not signed in", "Press Sign in to Content Rewards on the Campaigns page."))
    if safety.is_stopped(sv.db):
        out.append(_check("stop", "Emergency stop", "bad", "On: nothing runs", "Press Resume in the top bar."))
    pause = spend.current(sv.db, sv.app.spend_breaker)
    out.append(_check("spend", "Spending", "bad" if pause else "ok", pause.title if pause else "Inside the limits", "Approve and continue from the banner." if pause else ""))
    return out


def summary(items: list[dict]) -> str:
    bad = sum(1 for i in items if i["state"] == "bad")
    warn = sum(1 for i in items if i["state"] == "warn")
    if not bad and not warn:
        return "Everything is ready."
    return f"{bad} to fix, {warn} to look at." if bad else f"Nothing is broken; {warn} to look at."


def last_post(db: Database) -> str | None:
    with db.conn() as c:
        row = c.execute("SELECT MAX(posted_at) AS at FROM posts WHERE posted_at IS NOT NULL").fetchone()
    if not row or not row["at"]:
        return None
    ago = store.now() - store.parse_time(row["at"])
    if ago < timedelta(minutes=1):
        return "just now"
    if ago < timedelta(hours=1):
        return f"{int(ago.total_seconds() // 60)} minutes ago"
    if ago < timedelta(days=1):
        return f"{int(ago.total_seconds() // 3600)} hours ago"
    return f"{ago.days} days ago"
