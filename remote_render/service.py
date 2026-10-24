"""Remote rendering inside the engine: the gateway, this PC's worker process,
and the routes the Settings page uses (on the app's own local API).

Nothing starts unless Settings → Advanced settings → Remote rendering is on.
"""

import json
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

from remote_render import settings, tls

_lock = threading.Lock()
_gateway = None
_worker_proc: subprocess.Popen | None = None


# ---- the gateway (main PC) --------------------------------------------------------------


def ensure_gateway(data_dir, port: int):
    """The running gateway (started if needed), or None if it can't listen."""
    global _gateway
    from remote_render.gateway import Gateway

    with _lock:
        if _gateway is not None and (_gateway.port != int(port) or not _gateway.running):
            _gateway.stop()
            _gateway = None
        if _gateway is None:
            gw = Gateway(Path(data_dir), int(port))
            if not gw.start():
                print(f"      Remote rendering: {gw.error}")
                _last_error["gateway"] = gw.error
                return None
            _last_error["gateway"] = ""
            _gateway = gw
        return _gateway


def stop_gateway() -> None:
    global _gateway
    with _lock:
        if _gateway is not None:
            _gateway.stop()
            _gateway = None


_last_error = {"gateway": ""}


# ---- this PC as a worker ----------------------------------------------------------------


def _worker_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "render-worker"]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "main.py"), "render-worker"]


def start_worker(data_dir) -> bool:
    global _worker_proc
    import os

    with _lock:
        if _worker_proc is not None and _worker_proc.poll() is None:
            return True
        log = Path(data_dir) / "remote_render" / "worker.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        # The child keeps its own handle to the log; this one closes here.
        with open(log, "ab") as out:
            _worker_proc = subprocess.Popen(_worker_command(), stdout=out, stderr=subprocess.STDOUT,
                                            env={**os.environ, "CLIPS_STUDIO_DATA_DIR": str(data_dir)})
        return True


def stop_worker() -> None:
    global _worker_proc
    with _lock:
        if _worker_proc is not None and _worker_proc.poll() is None:
            from remote_render.worker import _kill_tree

            _kill_tree(_worker_proc)
        _worker_proc = None


def worker_running() -> bool:
    return _worker_proc is not None and _worker_proc.poll() is None


def apply(data_dir) -> None:
    """Bring the gateway and the worker in line with the settings."""
    s = settings.load(data_dir)
    if s["enabled"]:
        ensure_gateway(data_dir, int(s["port"]))
    else:
        stop_gateway()
    tp = s["this_pc"]
    if s["enabled"] and tp.get("enabled") and tp.get("worker_id") and tp.get("secret"):
        start_worker(data_dir)
    else:
        stop_worker()


def shutdown() -> None:
    stop_worker()
    stop_gateway()


# ---- what the Settings page sees -------------------------------------------------------


def state(data_dir) -> dict:
    from remote_render.gateway import addresses
    from remote_render.queue import RenderQueue

    s = settings.load(data_dir)
    gw = _gateway
    fingerprint = gw.fingerprint if gw is not None else ""
    status_file = Path(data_dir) / "remote_render" / "worker_status.json"
    this_status = {}
    # No status file yet (the worker hasn't checked in): nothing to show.
    with suppress(OSError, ValueError):
        this_status = json.loads(status_file.read_text(encoding="utf-8"))
        if time.time() - float(this_status.get("time") or 0) > 45:
            this_status["connected"] = False
    return {
        "settings": settings.public(s),
        "gateway": {"running": bool(gw and gw.running), "port": s["port"], "addresses": addresses(),
                    "fingerprint": tls.short(fingerprint) if fingerprint else "",
                    "error": _last_error["gateway"]},
        "workers": RenderQueue(data_dir).workers() if s["enabled"] else [],
        "this_pc": {"running": worker_running(), **this_status},
    }


def install(app, config: dict, data_dir: Path) -> None:
    """The local-API routes (127.0.0.1:8765 only, like every other route)."""
    from fastapi import HTTPException
    from pydantic import BaseModel

    from remote_render.queue import RenderQueue

    class RemotePatch(BaseModel):
        enabled: bool | None = None
        mode: str | None = None
        port: int | None = None

    class ThisPcPatch(BaseModel):
        enabled: bool | None = None
        main: str | None = None
        code: str | None = None
        max_jobs: int | None = None
        draining: bool | None = None

    class Hold(BaseModel):
        held: bool

    class Retry(BaseModel):
        target: str = ""

    @app.on_event("startup")
    async def _start_remote():
        threading.Thread(target=lambda: _safe(apply, data_dir), daemon=True, name="remote-render-start").start()

    @app.on_event("shutdown")
    async def _stop_remote():
        shutdown()

    @app.get("/remote-render")
    def get_remote():
        return state(data_dir)

    @app.put("/remote-render")
    def put_remote(body: RemotePatch):
        changes = {}
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.mode is not None:
            if not settings.valid_mode(body.mode):
                raise HTTPException(400, "mode must be local, auto or worker:<id>")
            changes["mode"] = body.mode
        if body.port is not None:
            if not 1024 <= body.port <= 65535:
                raise HTTPException(400, "port must be 1024-65535")
            changes["port"] = body.port
        settings.update(data_dir, **changes)
        apply(data_dir)
        return state(data_dir)

    @app.post("/remote-render/pairing-code")
    def pairing_code():
        s = settings.load(data_dir)
        if not s["enabled"]:
            raise HTTPException(409, "Turn on remote rendering first.")
        gw = ensure_gateway(data_dir, int(s["port"]))
        if gw is None:
            raise HTTPException(503, _last_error["gateway"] or "The render gateway couldn't start.")
        return {"code": gw.queue.new_pairing_code(), "expires_in": 600, **state(data_dir)["gateway"]}

    @app.delete("/remote-render/workers/{worker_id}")
    def remove_worker(worker_id: str):
        if not RenderQueue(data_dir).remove_worker(worker_id):
            raise HTTPException(404, "no such worker")
        s = settings.load(data_dir)
        if s["mode"] == f"worker:{worker_id}":
            settings.update(data_dir, mode="auto")
        return state(data_dir)

    @app.post("/remote-render/workers/{worker_id}/hold")
    def hold_worker(worker_id: str, body: Hold):
        RenderQueue(data_dir).set_held(worker_id, body.held)
        return state(data_dir)

    @app.put("/remote-render/this-pc")
    def put_this_pc(body: ThisPcPatch):
        from remote_render import worker as worker_mod

        if body.main is not None and body.code:
            try:
                worker_mod.pair(data_dir, body.main, body.code)
            except worker_mod.PairingError as e:
                raise HTTPException(400, str(e)) from e
        changes = {k: v for k, v in (("enabled", body.enabled), ("draining", body.draining)) if v is not None}
        if body.max_jobs is not None:
            changes["max_jobs"] = max(1, min(8, int(body.max_jobs)))
        if changes:
            settings.update(data_dir, this_pc=changes)
        apply(data_dir)
        return state(data_dir)

    @app.post("/remote-render/this-pc/unpair")
    def unpair_this_pc():
        settings.update(data_dir, this_pc={"enabled": False, "main": "", "fingerprint": "", "worker_id": "",
                                           "secret": "", "draining": False})
        apply(data_dir)
        return state(data_dir)

    @app.post("/remote-render/videos/{video_id}/render-locally")
    def render_locally(video_id: str):
        return {"released": RenderQueue(data_dir).release_to_local(video_id)}

    @app.post("/remote-render/jobs/{job_id}/retry")
    def retry_job(job_id: str, body: Retry):
        return {"ok": RenderQueue(data_dir).retry(job_id, body.target)}


def _safe(fn, *args) -> None:
    try:
        fn(*args)
    except Exception as e:
        print(f"      (remote rendering: {e})")
