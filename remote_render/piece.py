"""One clip's stretch of the source, cut without re-encoding.

A worker needs only the part of a three-hour VOD that one clip comes from,
not the VOD. FFmpeg's stream copy cuts it in seconds (no re-encode), but it
can only start on a keyframe, so the piece begins at the keyframe at or
before the clip's start (less a margin), and every time the render uses is
shifted by where that keyframe lands in the piece. The worker then renders
exactly the frames this PC would have.
"""

import hashlib
import subprocess
from pathlib import Path

from core.binaries import ffmpeg, ffprobe

MARGIN = 3.0      # seconds kept before the clip and after it
TAIL = 1.0        # the render pads the end by 0.4 s; a little more than that


def _probe_float(args: list[str]) -> float | None:
    try:
        out = subprocess.run([ffprobe(), "-v", "error", *args], capture_output=True, text=True,
                             timeout=120).stdout
    except Exception:
        return None
    values = []
    for line in out.splitlines():
        line = line.strip().strip(",")
        try:
            values.append(float(line))
        except ValueError:
            continue
    return values


def start_time(path: Path) -> float:
    v = _probe_float(["-show_entries", "format=start_time", "-of", "csv=p=0", str(path)])
    return v[0] if v else 0.0


def keyframe_before(source: Path, t: float) -> float:
    """The last keyframe at or before `t` (seconds from the file's start)."""
    base = start_time(source)
    lo, hi = max(0.0, t - 30.0), t + 0.05
    stamps = _probe_float(["-select_streams", "v:0", "-skip_frame", "nokey",
                           "-show_entries", "frame=pts_time", "-of", "csv=p=0",
                           "-read_intervals", f"{base + lo:.3f}%{base + hi:.3f}", str(source)]) or []
    rel = [s - base for s in stamps]
    before = [s for s in rel if s <= t + 1e-3]
    # Near the start of a file the first keyframe can sit just after 0.
    return max(before) if before else (min(rel) if rel else 0.0)


def first_video_time(path: Path) -> float:
    base = start_time(path)
    v = _probe_float(["-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "csv=p=0",
                      "-read_intervals", "%+#1", str(path)])
    return (v[0] - base) if v else 0.0


def _packets(path: Path, interval: str) -> list[tuple[float, str]]:
    """(pts seconds from the file's start, SHA-256 of the packet's bytes)."""
    base = start_time(path)
    try:
        out = subprocess.run([ffprobe(), "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "packet=pts_time,data_hash", "-show_data_hash", "SHA256",
                              "-read_intervals", interval, "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=120).stdout
    except Exception:
        return []
    found = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        try:
            found.append((float(parts[0]) - base, parts[1]))
        except (ValueError, IndexError):
            continue
    return found


def _measured_offset(source: Path, piece: Path, kf: float) -> float | None:
    """Where the piece really starts in the source: a stream copy keeps every
    packet byte for byte, so the piece's first video packet is found in the
    source by its hash. FFmpeg can start the copy on an earlier sync point
    than the keyframe asked for (an open-GOP stream), and this doesn't
    assume it didn't."""
    first = _packets(piece, "%+#1")
    if not first:
        return None
    t_piece, digest = first[0]
    base = start_time(source)
    for t_source, d in _packets(source, f"{base + max(0.0, kf - 30):.3f}%{base + kf + 1:.3f}"):
        if d == digest:
            return t_source - t_piece
    return None


def cut(source: Path, start: float, end: float, dest: Path) -> float:
    """Write the piece for a clip over [start, end] and return its offset: a
    time t in the source is t - offset in the piece."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    kf = keyframe_before(source, max(0.0, start - MARGIN))
    # Near the start of the file, copy from the very beginning without
    # seeking: a file cut out of a longer one (a downloaded section) starts
    # with pre-roll frames that have negative times, and seeking to 0 drops
    # the keyframe they decode from.
    head = kf <= MARGIN
    length = (end + MARGIN + TAIL) - (0.0 if head else kf)
    seek = [] if head else ["-ss", f"{kf:.3f}"]
    cmd = [ffmpeg(), "-y", "-v", "error", *seek, "-i", str(Path(source).resolve()),
           "-t", f"{length:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
           *([] if head else ["-avoid_negative_ts", "make_zero"]),
           "-movflags", "+faststart", str(Path(dest).resolve())]
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("could not cut the clip's stretch of the video: "
                           + result.stderr.decode(errors="replace")[-400:])
    # To within a frame of a local render: the renderer seeks with its times
    # rounded to 1/100 s, and shifted times round differently.
    if head:
        return 0.0      # the same timeline as the source, pre-roll and all
    measured = _measured_offset(Path(source), Path(dest), kf)
    return measured if measured is not None else kf - first_video_time(dest)


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def duration(path: Path) -> float:
    v = _probe_float(["-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return v[0] if v else 0.0
