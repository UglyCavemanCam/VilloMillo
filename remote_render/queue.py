"""The main PC's record of render workers and render jobs.

Authoritative: a worker only ever holds a job the queue gave it, and a job's
state changes here. A SQLite file of its own (data/remote_render/queue.db),
so the feature never touches the library's state.db schema, and so the
gateway thread and the pipeline can each open their own connection.

Job states:

    queued -> assigned -> transferring -> rendering -> uploading -> verifying -> completed
                                                                  \\-> failed (after retries)
    queued (retry, or requeued when a worker goes silent)   cancelled   local
"""

import hashlib
import hmac
import json
import secrets as _secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from remote_render import protocol

HEARTBEAT_TIMEOUT = 45.0   # a worker not heard from for this long is offline
PAIRING_TTL = 600.0        # a pairing code lasts ten minutes
PAIRING_TRIES = 5          # and this many wrong guesses void it
MAX_ATTEMPTS = 3           # the first try and two automatic retries

ACTIVE = ("assigned", "transferring", "rendering", "uploading", "verifying")

SCHEMA = """
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    caps TEXT DEFAULT '{}',
    last_seen REAL DEFAULT 0,
    draining INTEGER DEFAULT 0,
    held INTEGER DEFAULT 0,
    max_jobs INTEGER DEFAULT 1,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pairing (
    code_hash TEXT PRIMARY KEY,
    expires REAL NOT NULL,
    tries INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    label TEXT DEFAULT '',
    state TEXT NOT NULL,
    target TEXT DEFAULT '',
    needs_framing INTEGER DEFAULT 1,
    spec TEXT NOT NULL,
    piece_path TEXT NOT NULL,
    piece_sha TEXT NOT NULL,
    piece_size INTEGER NOT NULL,
    assets TEXT DEFAULT '{}',
    worker_id TEXT DEFAULT '',
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    error TEXT DEFAULT '',
    log TEXT DEFAULT '',
    result_path TEXT DEFAULT '',
    render_opts TEXT DEFAULT '',
    created REAL NOT NULL,
    updated REAL NOT NULL
);
"""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normal_code(code: str) -> str:
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # no 0/O, 1/I: read aloud and typed


class RenderQueue:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "remote_render"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "queue.db"
        self._lock = threading.RLock()
        with self._db() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        """A connection per use, always closed (sqlite3's own context manager
        commits but leaves the connection open). Autocommit; the lock keeps
        read-then-write steps in this process atomic."""
        c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
        finally:
            c.close()

    # ---- pairing and workers ---------------------------------------------------------

    def new_pairing_code(self) -> str:
        """A one-time code for "Add worker": 8 characters shown as ABCD-EFGH,
        valid ten minutes, void after five wrong tries."""
        code = "".join(_secrets.choice(_ALPHABET) for _ in range(8))
        now = time.time()
        with self._lock, self._db() as c:
            c.execute("DELETE FROM pairing WHERE expires < ?", (now,))
            c.execute("INSERT INTO pairing (code_hash, expires) VALUES (?, ?)",
                      (_hash(code), now + PAIRING_TTL))
        return f"{code[:4]}-{code[4:]}"

    def redeem(self, code: str, name: str, caps: dict) -> tuple[str, str] | None:
        """(worker id, secret) for a valid code, which is then used up. A
        wrong code counts against every live code, so guessing is capped."""
        now = time.time()
        wanted = _hash(_normal_code(code))
        with self._lock, self._db() as c:
            c.execute("DELETE FROM pairing WHERE expires < ? OR tries >= ?", (now, PAIRING_TRIES))
            row = c.execute("SELECT code_hash FROM pairing WHERE code_hash = ?", (wanted,)).fetchone()
            if row is None:
                c.execute("UPDATE pairing SET tries = tries + 1")
                return None
            c.execute("DELETE FROM pairing WHERE code_hash = ?", (wanted,))
            worker_id = _secrets.token_hex(8)
            secret = _secrets.token_urlsafe(32)
            c.execute("INSERT INTO workers (id, name, secret_hash, caps, last_seen, created) "
                      "VALUES (?, ?, ?, ?, ?, ?)",
                      (worker_id, str(name)[:60] or "Render PC", _hash(secret), json.dumps(caps or {}), now, now))
        return worker_id, secret

    def authenticate(self, worker_id: str, secret: str) -> bool:
        with self._db() as c:
            row = c.execute("SELECT secret_hash FROM workers WHERE id = ?", (str(worker_id),)).fetchone()
        return row is not None and hmac.compare_digest(row["secret_hash"], _hash(str(secret or "")))

    def remove_worker(self, worker_id: str) -> bool:
        """Unpair: its credential stops working at once; its jobs go back."""
        with self._lock, self._db() as c:
            c.execute(f"UPDATE jobs SET state='queued', worker_id='', stage='', progress=0, updated=? "
                      f"WHERE worker_id = ? AND state IN ({','.join('?' * len(ACTIVE))})",
                      (time.time(), worker_id, *ACTIVE))
            return c.execute("DELETE FROM workers WHERE id = ?", (worker_id,)).rowcount > 0

    def set_held(self, worker_id: str, held: bool) -> None:
        """"Stop accepting jobs" from the main PC's side."""
        with self._db() as c:
            c.execute("UPDATE workers SET held = ? WHERE id = ?", (int(held), worker_id))

    def workers(self) -> list[dict]:
        now = time.time()
        with self._db() as c:
            rows = c.execute("SELECT * FROM workers ORDER BY created").fetchall()
            busy = {r["worker_id"]: r["n"] for r in c.execute(
                f"SELECT worker_id, COUNT(*) AS n FROM jobs WHERE state IN ({','.join('?' * len(ACTIVE))}) "
                f"GROUP BY worker_id", ACTIVE)}
            current = {r["worker_id"]: dict(r) for r in c.execute(
                f"SELECT worker_id, label, stage, progress FROM jobs WHERE state IN "
                f"({','.join('?' * len(ACTIVE))}) ORDER BY updated DESC", ACTIVE)}
        out = []
        for r in rows:
            online = now - (r["last_seen"] or 0) <= HEARTBEAT_TIMEOUT
            caps = json.loads(r["caps"] or "{}")
            status = ("offline" if not online else "busy" if busy.get(r["id"]) else
                      "draining" if (r["draining"] or r["held"]) else "idle")
            out.append({"id": r["id"], "name": r["name"], "status": status, "online": online,
                        "last_seen": r["last_seen"], "draining": bool(r["draining"] or r["held"]),
                        "held": bool(r["held"]), "caps": caps, "max_jobs": r["max_jobs"],
                        "running": busy.get(r["id"], 0), "current": current.get(r["id"])})
        return out

    def online(self, needs_framing: bool = False, target: str = "") -> list[dict]:
        """Workers that could take a job right now (or soon: busy counts)."""
        return [w for w in self.workers()
                if w["online"] and not w["draining"] and (not target or w["id"] == target)
                and protocol.compatible(needs_framing, w["caps"])[0]]

    def heartbeat(self, worker_id: str, caps: dict, draining: bool, max_jobs: int,
                  running: list[dict]) -> dict:
        """A worker checking in. Returns the jobs it must stop (cancelled, or
        no longer its own), so a cancel reaches it within a heartbeat."""
        now = time.time()
        stop = []
        with self._lock, self._db() as c:
            old = c.execute("SELECT caps FROM workers WHERE id = ?", (worker_id,)).fetchone()
            merged = {**json.loads(old["caps"] or "{}"), **(caps or {})} if old else caps
            c.execute("UPDATE workers SET caps = ?, last_seen = ?, draining = ?, max_jobs = ? WHERE id = ?",
                      (json.dumps(merged), now, int(bool(draining)), max(1, int(max_jobs or 1)), worker_id))
            for r in running or []:
                row = c.execute("SELECT state, worker_id FROM jobs WHERE id = ?", (str(r.get("id")),)).fetchone()
                if row is None or row["worker_id"] != worker_id or row["state"] not in ACTIVE:
                    stop.append(str(r.get("id")))
                    continue
                c.execute("UPDATE jobs SET stage = ?, progress = ?, updated = ? WHERE id = ?",
                          (str(r.get("stage") or "")[:40], float(r.get("progress") or 0), now, r["id"]))
        return {"stop": stop}

    def sweep(self) -> int:
        """Workers gone silent: their jobs go back in the queue (the same job
        id, so a late result from the old worker is recognised, not doubled)."""
        cutoff = time.time() - HEARTBEAT_TIMEOUT
        with self._lock, self._db() as c:
            gone = [r["id"] for r in c.execute("SELECT id FROM workers WHERE last_seen < ?", (cutoff,))]
            n = 0
            for wid in gone:
                n += c.execute(
                    f"UPDATE jobs SET state='queued', worker_id='', stage='worker went offline', progress=0, "
                    f"updated=? WHERE worker_id = ? AND state IN ({','.join('?' * len(ACTIVE))})",
                    (time.time(), wid, *ACTIVE)).rowcount
        return n

    # ---- jobs ------------------------------------------------------------------------

    def submit(self, job: dict) -> str:
        """Queue a job, or recognise one already known by its id. A completed
        job keeps its result; a failed or cancelled one is queued again."""
        now = time.time()
        with self._lock, self._db() as c:
            row = c.execute("SELECT state FROM jobs WHERE id = ?", (job["id"],)).fetchone()
            if row is not None and row["state"] in ("completed", *ACTIVE, "queued"):
                c.execute("UPDATE jobs SET target = ?, updated = ? WHERE id = ?",
                          (job.get("target", ""), now, job["id"]))
                return row["state"]
            c.execute("DELETE FROM jobs WHERE id = ?", (job["id"],))
            c.execute(
                "INSERT INTO jobs (id, video_id, label, state, target, needs_framing, spec, piece_path, "
                "piece_sha, piece_size, assets, created, updated) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (job["id"], job["video_id"], job.get("label", ""), job.get("target", ""),
                 int(bool(job.get("needs_framing", True))), json.dumps(job["spec"]), str(job["piece_path"]),
                 job["piece_sha"], int(job["piece_size"]), json.dumps(job.get("assets") or {}), now, now))
        return "queued"

    def claim(self, worker_id: str) -> dict | None:
        """The next job this worker can do, or None. Respects its limit, a
        pause from either side, and each job's target."""
        with self._lock, self._db() as c:
            w = c.execute("SELECT * FROM workers WHERE id = ?", (worker_id,)).fetchone()
            if w is None or w["draining"] or w["held"]:
                return None
            running = c.execute(f"SELECT COUNT(*) FROM jobs WHERE worker_id = ? AND state IN "
                                f"({','.join('?' * len(ACTIVE))})", (worker_id, *ACTIVE)).fetchone()[0]
            if running >= max(1, w["max_jobs"] or 1):
                return None
            caps = json.loads(w["caps"] or "{}")
            for r in c.execute("SELECT * FROM jobs WHERE state = 'queued' AND (target = '' OR target = ?) "
                               "ORDER BY created", (worker_id,)).fetchall():
                if not protocol.compatible(bool(r["needs_framing"]), caps)[0]:
                    continue
                c.execute("UPDATE jobs SET state='assigned', worker_id=?, stage='assigned', progress=0, "
                          "attempts=attempts+1, updated=? WHERE id = ? AND state='queued'",
                          (worker_id, time.time(), r["id"]))
                return self._public_job(c, r["id"])
        return None

    def _public_job(self, c, job_id: str) -> dict:
        r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return {"id": r["id"], "spec": json.loads(r["spec"]), "piece_sha": r["piece_sha"],
                "piece_size": r["piece_size"], "assets": json.loads(r["assets"] or "{}"),
                "attempt": r["attempts"]}

    def owned(self, job_id: str, worker_id: str) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if r is None or r["worker_id"] != worker_id or r["state"] not in ACTIVE:
            return None
        return dict(r)

    def progress(self, job_id: str, worker_id: str, stage: str, progress: float) -> bool:
        state = {"transferring": "transferring", "rendering": "rendering", "uploading": "uploading"}.get(stage)
        with self._lock, self._db() as c:
            n = c.execute(f"UPDATE jobs SET stage=?, progress=?, updated=?{', state=?' if state else ''} "
                          f"WHERE id=? AND worker_id=? AND state IN ({','.join('?' * len(ACTIVE))})",
                          (stage[:40], float(progress), time.time(), *([state] if state else []),
                           job_id, worker_id, *ACTIVE)).rowcount
        return n > 0

    def complete(self, job_id: str, worker_id: str, result_path: Path, render_opts: str) -> bool:
        """The first valid result wins; a late duplicate is ignored."""
        with self._lock, self._db() as c:
            n = c.execute(f"UPDATE jobs SET state='completed', stage='done', progress=1, result_path=?, "
                          f"render_opts=?, updated=? WHERE id=? AND worker_id=? AND state IN "
                          f"({','.join('?' * len(ACTIVE))})",
                          (str(result_path), render_opts or "", time.time(), job_id, worker_id, *ACTIVE)).rowcount
        return n > 0

    def fail(self, job_id: str, worker_id: str, error: str, log: str = "") -> str:
        """Retry (queued again) until MAX_ATTEMPTS, then failed."""
        with self._lock, self._db() as c:
            r = c.execute("SELECT attempts, state, worker_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if r is None or r["worker_id"] != worker_id or r["state"] not in ACTIVE:
                return "ignored"
            state = "queued" if r["attempts"] < MAX_ATTEMPTS else "failed"
            c.execute("UPDATE jobs SET state=?, worker_id=?, stage=?, progress=0, error=?, log=?, updated=? "
                      "WHERE id=?", (state, "" if state == "queued" else worker_id,
                                     "retrying" if state == "queued" else "failed",
                                     str(error)[:500], str(log)[-8000:], time.time(), job_id))
        return state

    def job(self, job_id: str) -> dict | None:
        with self._db() as c:
            r = c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(r) if r is not None else None

    def jobs_for(self, video_id: str) -> list[dict]:
        with self._db() as c:
            return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE video_id = ? ORDER BY created",
                                               (video_id,))]

    def cancel_video(self, video_id: str) -> int:
        with self._lock, self._db() as c:
            return c.execute(f"UPDATE jobs SET state='cancelled', stage='cancelled', updated=? WHERE video_id=? "
                             f"AND state IN ('queued', {','.join('?' * len(ACTIVE))})",
                             (time.time(), video_id, *ACTIVE)).rowcount

    def release_to_local(self, video_id: str, job_id: str = "") -> int:
        """"Render locally": the video's jobs not yet done come back to this
        PC (a worker still holding one is told to stop at its heartbeat)."""
        with self._lock, self._db() as c:
            sql = (f"UPDATE jobs SET state='local', stage='rendering here', updated=? WHERE video_id=? "
                   f"AND state IN ('queued', 'failed', {','.join('?' * len(ACTIVE))})")
            args: list = [time.time(), video_id, *ACTIVE]
            if job_id:
                sql += " AND id = ?"
                args.append(job_id)
            return c.execute(sql, args).rowcount

    def retry(self, job_id: str, target: str = "") -> bool:
        """"Retry" (on any worker, or `target`) for a failed job."""
        with self._lock, self._db() as c:
            return c.execute("UPDATE jobs SET state='queued', worker_id='', target=?, attempts=0, stage='', "
                             "error='', updated=? WHERE id=? AND state IN ('failed', 'cancelled')",
                             (target, time.time(), job_id)).rowcount > 0

    def forget(self, job_id: str) -> None:
        with self._db() as c:
            c.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
