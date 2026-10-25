"""Small checks for run-app.bat, so the batch file needs no tricky quoting.

  python scripts/engine_check.py deps   exit 0 if the clipping engine's main packages are installed (names printed if not)
  python scripts/engine_check.py up     exit 0 if the engine answers on its port
"""

from __future__ import annotations

import importlib.util
import os
import sys
import urllib.request

PACKAGES = ("faster_whisper", "ultralytics", "psutil", "requests", "defusedxml", "uvicorn", "fastapi")


def main(argv: list[str]) -> int:
    what = argv[0] if argv else ""
    if what == "deps":
        missing = [name for name in PACKAGES if importlib.util.find_spec(name) is None]
        if missing:
            print("missing: " + ", ".join(missing))
        return 1 if missing else 0
    if what == "up":
        url = os.environ.get("CLIPS_STUDIO_API", "http://127.0.0.1:8765").rstrip("/") + "/jobs?limit=1"
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                return 0 if r.status == 200 else 1
        except Exception:
            return 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
