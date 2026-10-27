"""The builder's live preview: a render queue for the pieces of the sample clip.

Every edit starts a new round. A round lists the pieces of the sample (see preview.spans), says which are already made (a piece
whose fingerprint is on disk is reused, never made twice), and queues the rest nearest-the-playhead first: the piece under the
playhead, then the ones beside it, then the rest. A new round throws away the old round's waiting pieces and stops the piece
being made if the new round does not want it next, so two quick edits only ever finish the last one. One piece is made at a time
(ffmpeg already uses every core), on a background thread.

    queue = LivePreview(folder)
    status = queue.submit(preview.prepare(db, profile, design, video_id), playhead_seconds)
    queue.status(status["round"])      # which pieces are ready, as the page polls it
"""

from __future__ import annotations

import itertools
import pathlib
import threading
from collections.abc import Callable

from . import preview, render

KEEP_ROUNDS = 8


def order(count: int, playhead: int) -> list[int]:
    """Piece numbers, the one under the playhead first, then outwards, the later piece before the earlier at each distance."""
    return sorted(range(count), key=lambda i: (abs(i - playhead), i < playhead))


class _Round:
    def __init__(self, number: int, prepared, playhead: int):
        self.number, self.prepared, self.playhead = number, prepared, playhead
        self.state = ["queued"] * len(prepared.keys)
        self.error: list[str | None] = [None] * len(prepared.keys)
        self.order = order(len(prepared.keys), playhead)


class LivePreview:
    def __init__(self, folder: pathlib.Path, make: Callable | None = None):
        """`make(prepared, index, folder, cancel)` renders one piece; it defaults to the real renderer."""
        self.folder = pathlib.Path(folder)
        self._make = make
        self._lock = threading.Condition()
        self._numbers = itertools.count(1)
        self._rounds: dict[int, _Round] = {}
        self._current: _Round | None = None
        self._running: tuple[str, threading.Event] | None = None   # the key of the piece being made, and how to stop it
        self._busy = False   # a worker thread is running (set and cleared under the lock, so a new round never misses a thread that is ending)

    # --- asking -------------------------------------------------------------------------------------------------------
    def submit(self, prepared, playhead_seconds: float = 0.0) -> dict:
        """Start a new round for this design and sample. Returns its status at once; the rest is made in the background."""
        spans = prepared.spans
        at = max((i for i, (a, _) in enumerate(spans) if a <= playhead_seconds), default=0)   # the piece the playhead is in
        with self._lock:
            rnd = _Round(next(self._numbers), prepared, at)
            for i, key in enumerate(prepared.keys):
                if preview.segment_path(self.folder, key).exists():
                    rnd.state[i] = "ready"
            self._rounds[rnd.number] = rnd
            for old in sorted(self._rounds)[:-KEEP_ROUNDS]:
                del self._rounds[old]
            self._current = rnd
            wanted = next((prepared.keys[i] for i in rnd.order if rnd.state[i] != "ready"), None)
            if self._running and self._running[0] != wanted:   # what is being made is not what is needed next: stop it
                self._running[1].set()
            self._start_worker()
            self._lock.notify_all()
            return self._status(rnd)

    def status(self, number: int) -> dict:
        with self._lock:
            rnd = self._rounds.get(number)
            if rnd is None or rnd is not self._current:
                return {"round": number, "superseded": True, "current": self._current.number if self._current else None}
            return self._status(rnd)

    def _status(self, rnd: _Round) -> dict:
        pieces = [{"index": i, "start": a, "end": b, "state": rnd.state[i], "error": rnd.error[i],
                   "url": f"/formats/preview/format-seg-{rnd.prepared.keys[i]}.mp4" if rnd.state[i] == "ready" else None}
                  for i, (a, b) in enumerate(rnd.prepared.spans)]
        return {"round": rnd.number, "superseded": False, "segments": pieces, "length": rnd.prepared.spans[-1][1],
                "segment_seconds": preview.SEGMENT_SECONDS, "done": all(s in ("ready", "failed") for s in rnd.state),
                "elements": rnd.prepared.windows or {}}   # when each element is on screen, for the timeline

    # --- making -------------------------------------------------------------------------------------------------------
    def _start_worker(self) -> None:
        if not self._busy:
            self._busy = True
            threading.Thread(target=self._work, name="format-preview", daemon=True).start()

    def _next(self) -> tuple[_Round, int] | None:
        rnd = self._current
        if rnd is None:
            return None
        return next(((rnd, i) for i in rnd.order if rnd.state[i] == "queued"), None)

    def _work(self) -> None:
        while True:
            with self._lock:
                picked = self._next()
                if picked is None:
                    self._running, self._busy = None, False
                    return   # nothing left to make: the thread ends and the next round starts a new one
                rnd, i = picked
                key, stop = rnd.prepared.keys[i], threading.Event()
                rnd.state[i] = "rendering"
                self._running = (key, stop)
            error = None
            try:
                (self._make or preview.make_segment)(rnd.prepared, i, self.folder, stop)
            except render.RenderCancelled:
                error = "cancelled"
            except Exception as e:   # one piece that cannot be made is reported; the rest carry on
                error = str(e) or type(e).__name__
            with self._lock:
                self._running = None
                for r in (rnd, self._current):   # a piece is the same piece in the next round when its fingerprint is the same
                    if r is None:
                        continue
                    for j, other in enumerate(r.prepared.keys):
                        if other == key and r.state[j] in ("rendering", "queued"):
                            if error is None:
                                r.state[j] = "ready"
                            elif error == "cancelled":
                                r.state[j] = "queued" if r is self._current else r.state[j]
                            else:
                                r.state[j], r.error[j] = "failed", error
                if error is None and self._current is not None:
                    preview.prune_segments(self.folder, set(self._current.prepared.keys))
                self._lock.notify_all()
