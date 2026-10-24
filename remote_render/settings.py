"""The user's remote-rendering choices, kept with the app's other secrets
(core/secrets: DPAPI on Windows), because this PC's worker credential is one.

Defaults live here, not in settings.yaml: an installed build never receives
new settings.yaml keys, and the whole feature is off until someone turns it
on under Settings → Advanced settings.
"""

import copy
import threading
from pathlib import Path

from core import secrets

NAME = "remote_render"

DEFAULTS: dict = {
    # Settings → Advanced settings → Remote rendering. Off: nothing below is
    # used, the gateway and the worker never start, every render is local.
    "enabled": False,
    # Where this PC's clips render: "local", "auto" (a worker when one can,
    # else here), or "worker:<id>" (only that worker; jobs wait for it).
    "mode": "local",
    # The port the gateway listens on for workers.
    "port": 8766,
    # This PC as a render worker for another Clips Kitty.
    "this_pc": {
        "enabled": False,
        "main": "",          # the main PC's address, host:port
        "fingerprint": "",   # its certificate, pinned when paired
        "worker_id": "",
        "secret": "",
        "max_jobs": 1,       # renders at once; 1 is safe on any GPU
        "draining": False,   # "Stop accepting jobs": finish the current one only
    },
}

_lock = threading.Lock()


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


def load(data_dir) -> dict:
    return _merge(DEFAULTS, secrets.load(Path(data_dir), NAME) or {})


def save(data_dir, value: dict) -> dict:
    with _lock:
        merged = _merge(DEFAULTS, value)
        secrets.save(Path(data_dir), NAME, merged)
        return merged


def update(data_dir, **changes) -> dict:
    """Change top-level fields (and `this_pc` as a partial dict)."""
    with _lock:
        current = _merge(DEFAULTS, secrets.load(Path(data_dir), NAME) or {})
        for k, v in changes.items():
            if k == "this_pc" and isinstance(v, dict):
                current["this_pc"] = _merge(current["this_pc"], v)
            elif k in current:
                current[k] = v
        secrets.save(Path(data_dir), NAME, current)
        return current


def valid_mode(mode: str) -> bool:
    return mode in ("local", "auto") or (mode.startswith("worker:") and len(mode) > len("worker:"))


def public(value: dict) -> dict:
    """What the UI may see: never this PC's worker secret."""
    out = copy.deepcopy(value)
    tp = out.get("this_pc") or {}
    tp["paired"] = bool(tp.get("worker_id") and tp.get("secret"))
    tp.pop("secret", None)
    return out
