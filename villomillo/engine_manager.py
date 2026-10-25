"""The clipping engine: is it running, and (under `villomillo run`) keeping it running.

The engine is its own program (`python main.py serve`) on a local port. This module does two jobs:
  status        one honest answer for every page: on, starting, not responding, error or off. A single failed check never turns
                the light off: it takes two in a row, and the check waits up to 3 seconds because a busy engine answers slowly.
  supervision   only under `villomillo run`: start the engine if nothing answers, write its output to engine.log, start it again
                if it exits, and give up (state "error", with the last error line) after 3 exits in 10 minutes until you press
                Start or Restart. The dashboard alone (`villomillo serve`) only watches.
An engine you started yourself is left alone: it is shown as on, but Restart cannot stop what this program did not start.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .config import ROOT

PROBE_TIMEOUT_S = 3.0
FAILS_TO_DROP = 2  # failed checks in a row before the light changes
STARTUP_GRACE_S = 300  # a first start loads models and can take minutes
CHECK_EVERY_S = 4  # pages share one check this often
CRASHES_TO_GIVE_UP = 3
CRASH_WINDOW_S = 600
BACKOFF_S = (2, 5, 10)
LOG_PATH = ROOT / "data" / "villomillo" / "engine.log"
ERROR_WORDS = re.compile(r"(Traceback|Error|Exception|CRITICAL|ModuleNotFound|No module named|Address already in use)")

TEXT = {"on": ("Clip engine is on", "good"), "starting": ("Clip engine is starting", "warn"), "not_responding": ("Clip engine is not responding", "warn"),
        "error": ("Clip engine has stopped", "bad"), "off": ("Clip engine is off", "")}


SEM_FAILCRITICALERRORS = 0x0001
SEM_NOOPENFILEERRORBOX = 0x8000


def quiet_dialogs(kernel32=None) -> bool:
    """Windows only: make a library that cannot be loaded (a wrong-version CUDA DLL, say) raise an error instead of opening a modal
    "Entry Point Not Found" box that freezes the program until someone presses OK. Programs this one starts inherit the setting, so
    the engine's real error lands in engine.log and shows in Settings. Returns whether it was set."""
    if kernel32 is None:
        if os.name != "nt":
            return False
        import ctypes

        kernel32 = ctypes.windll.kernel32
    kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX)
    return True


def _probe(url: str) -> bool:
    try:
        return httpx.get(f"{url}/jobs", params={"limit": 1}, timeout=PROBE_TIMEOUT_S).status_code == 200
    except httpx.HTTPError:
        return False


def last_error(path: pathlib.Path, lines: int = 60) -> str | None:
    """The most telling recent line of the engine's log: the last line that looks like an error, else the last line."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 16000))
            tail = f.read().decode("utf-8", "replace").splitlines()[-lines:]
    except OSError:
        return None
    tail = [t.strip() for t in tail if t.strip() and not t.startswith("---")]
    for line in reversed(tail):
        if ERROR_WORDS.search(line):
            return line[:240]
    return tail[-1][:240] if tail else None


def tail(path: pathlib.Path, lines: int = 300) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            return "\n".join(f.read().decode("utf-8", "replace").splitlines()[-lines:])
    except OSError:
        return ""


class EngineManager:
    def __init__(self, *, url: str | None = None, command: list[str] | None = None, log_path: pathlib.Path | None = None,
                 popen=subprocess.Popen, probe=None, clock=time.monotonic, wall=None):
        from . import clipper

        self.url = (url or clipper.ENGINE_URL).rstrip("/")
        port = urlsplit(self.url).port or 8765
        self.command = command or [sys.executable, "main.py", "serve", "--port", str(port)]
        self.log_path = pathlib.Path(log_path or LOG_PATH)
        self._popen, self._probe, self._clock = popen, probe or (lambda: _probe(self.url)), clock
        self._wall = wall or (lambda: datetime.now(timezone.utc))
        self.env_for = lambda: {}  # extra environment for the engine (which AI finds the moments); set by `villomillo run`
        self.supervised = False
        self.proc = None
        self._lock = threading.RLock()
        self._reset()

    def _reset(self) -> None:
        self.fails = 0
        self.last_ok = None  # clock time of the last successful check
        self.up_since = None  # wall time the engine was first seen answering in this run
        self.checked_at = -1e9
        self.launched_at = None
        self.exits: list[float] = []
        self.restart_at = None
        self.gave_up = False
        self.restarts = 0
        self.error = None
        self.exited = False

    # ---- watching -----------------------------------------------------------------------------------------------------

    def check(self, *, force: bool = False) -> bool:
        """Ask the engine, at most every few seconds. The answer is remembered, so a page never waits twice."""
        with self._lock:
            now = self._clock()
            if not force and now - self.checked_at < CHECK_EVERY_S:
                return self.fails == 0
            self.checked_at = now
        ok = self._probe()
        with self._lock:
            if ok:
                if self.last_ok is None or self.fails >= FAILS_TO_DROP:
                    self.up_since = self._wall()
                self.fails, self.last_ok, self.error = 0, self._clock(), None
            else:
                self.fails += 1
        return ok

    def _alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def status(self) -> dict:
        """What every page shows: state, plain text, light colour, how long it has been up and the last error line."""
        self.check()
        with self._lock:
            reachable = self.last_ok is not None and self.fails < FAILS_TO_DROP
            if reachable:
                state = "on"
            elif self.supervised and self._alive():
                waiting = self._clock() - (self.launched_at or 0)
                state = "starting" if self.last_ok is None and waiting < STARTUP_GRACE_S else "not_responding"
            elif self.supervised and (self.gave_up or self.exited) and self.restart_at is None:
                state = "error"
            elif self.supervised and self.exited:
                state = "starting"  # about to be started again
            elif self.last_ok is not None:
                state = "not_responding"  # it answered before and no longer does; we did not start it, so we cannot tell why
            else:
                state = "off"
            err = self.error or (last_error(self.log_path) if state in ("error", "not_responding", "starting") else None)
            text, kind = TEXT[state]
            up = (self._wall() - self.up_since).total_seconds() if state == "on" and self.up_since else None
            return {"state": state, "text": text, "kind": kind, "managed": self.supervised, "uptime_s": int(up) if up is not None else None,
                    "last_error": err, "restarts": self.restarts, "url": self.url, "log_path": str(self.log_path), "gave_up": self.gave_up,
                    "title": self._title(state, err)}

    def _title(self, state: str, err: str | None) -> str:
        base = {"on": f"The clipping engine at {self.url} answers.", "off": f"Nothing answers at {self.url}.",
                "starting": "The engine was just started; its first start loads models and can take minutes.",
                "not_responding": f"The engine at {self.url} is not answering right now.",
                "error": "The engine stopped and was not started again."}[state]
        return base + (f" Last error: {err}" if err and state != "on" else "")

    @property
    def up(self) -> bool:
        return self.status()["state"] == "on"

    # ---- keeping it running (only under `villomillo run`) -----------------------------------------------------------------

    def _launch(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        quiet_dialogs()  # the engine inherits it: a DLL problem becomes a line in engine.log, not a box on screen
        log = open(self.log_path, "ab")
        log.write(f"\n--- engine starting {self._wall().isoformat(timespec='seconds')} ---\n".encode())
        log.flush()
        try:
            self.proc = self._popen(self.command, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    env={**os.environ, **self.env_for()})
        finally:
            log.close()  # the child has its own copy of the handle
        self.launched_at, self.exited, self.restart_at = self._clock(), False, None
        self.fails, self.last_ok = 0, None

    def start(self) -> str:
        """Start the engine if nothing answers (the Start button, and the first thing `villomillo run` does)."""
        if not self.supervised:
            return "VilloMillo can only start the engine when it runs with run-app.bat (python -m villomillo run)."
        if self.check(force=True):
            return "The engine is already running."
        with self._lock:
            if self._alive():
                return "The engine was started a moment ago and is still loading."
            self.gave_up, self.exits, self.error = False, [], None
            self._launch()
        return "Starting the engine. Its first start can take a minute or two."

    def restart(self) -> str:
        if not self.supervised:
            return "VilloMillo can only restart the engine when it runs with run-app.bat (python -m villomillo run)."
        with self._lock:
            managed = self.proc is not None
        if self.check(force=True) and not managed:
            return "This engine was started outside VilloMillo, so it cannot be restarted from here. Close it and press Start."
        self._end()
        with self._lock:
            self.gave_up, self.exits, self.error, self.restarts = False, [], None, self.restarts + 1
            self._launch()
        return "Restarting the engine."

    def _end(self) -> None:
        with self._lock:
            proc, self.proc = self.proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            import psutil

            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.terminate()
        except Exception:  # no psutil, or the process is already gone
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    def stop(self) -> None:
        """The app is closing: stop the engine this program started."""
        self.supervised = False
        self._end()

    def enable(self) -> str:
        """Begin supervising. An engine that already answers is left running as it is."""
        self.supervised = True
        return self.start()

    def tick(self) -> None:
        """One supervision step: notice an exit, start again after a short wait, give up after repeated crashes."""
        if not self.supervised:
            return
        with self._lock:
            if self.proc is not None and self.proc.poll() is not None and not self.exited:
                now = self._clock()
                self.exited = True
                self.error = last_error(self.log_path) or f"the engine exited with code {self.proc.poll()}"
                self.exits = [t for t in self.exits if now - t < CRASH_WINDOW_S] + [now]
                if len(self.exits) >= CRASHES_TO_GIVE_UP:
                    self.gave_up, self.restart_at = True, None
                else:
                    self.restart_at = now + BACKOFF_S[min(len(self.exits), len(BACKOFF_S)) - 1]
            if self.exited and not self.gave_up and self.restart_at is not None and self._clock() >= self.restart_at:
                self.restarts += 1
                self._launch()
        self.check()

    def run_forever(self, stop: threading.Event, interval: float = 5.0) -> None:
        while not stop.is_set():
            try:
                self.tick()
            except Exception:  # the supervisor must outlive any one failure
                pass
            stop.wait(interval)


manager = EngineManager()


def status() -> dict:
    return manager.status()


def up() -> bool:
    return manager.up

