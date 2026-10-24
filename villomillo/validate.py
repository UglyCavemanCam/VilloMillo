"""Checks on a finished clip file, using FFmpeg's own tools. Nothing here decides whether to post;
it only says what is wrong, so you can see why a clip is held back."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

TARGET = {"width": 1080, "height": 1920, "codec": "h264", "min_s": 15, "max_s": 90}
LOUDNESS = (-24.0, -10.0)  # integrated loudness, LUFS. Wide on purpose: the target is -16 to -12


class ValidationError(RuntimeError):
    pass


def _tool(name: str) -> str:
    from core import binaries

    path = {"ffmpeg": binaries.ffmpeg, "ffprobe": binaries.ffprobe}[name]()
    if not (Path(path).exists() or shutil.which(path)):
        raise ValidationError(f"{name} was not found: install FFmpeg or put it on PATH")
    return path


def probe(path: Path) -> dict:
    out = subprocess.run(
        [_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=120,
    )
    if out.returncode != 0:
        raise ValidationError("ffprobe could not read the file")
    return json.loads(out.stdout)


def loudness(path: Path) -> float | None:
    out = subprocess.run(
        [_tool("ffmpeg"), "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=600,
    )
    m = re.findall(r"I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", out.stderr)
    return float(m[-1]) if m else None


def check(path: Path, *, with_loudness: bool = True) -> dict:
    """Returns {"ok": bool, "problems": [...], "warnings": [...], "facts": {...}}."""
    problems: list[str] = []
    warnings: list[str] = []
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(info.get("format", {}).get("duration") or 0)

    if video is None:
        problems.append("no video stream")
    else:
        if video.get("codec_name") != TARGET["codec"]:
            problems.append(f"video codec is {video.get('codec_name')}, not H.264")
        if (video.get("width"), video.get("height")) != (TARGET["width"], TARGET["height"]):
            problems.append(f"frame is {video.get('width')}x{video.get('height')}, not 1080x1920 (9:16)")
    if audio is None:
        problems.append("no audio stream: clips must have sound")
    if duration < TARGET["min_s"]:
        problems.append(f"{duration:.0f} s is shorter than {TARGET['min_s']} s")
    elif duration > TARGET["max_s"]:
        problems.append(f"{duration:.0f} s is longer than {TARGET['max_s']} s")
    elif not 20 <= duration <= 60:
        warnings.append(f"{duration:.0f} s is outside the 20 to 60 second sweet spot")

    lufs = loudness(path) if (with_loudness and audio is not None) else None
    if lufs is None and with_loudness and audio is not None:
        warnings.append("loudness could not be measured")
    elif lufs is not None and not LOUDNESS[0] <= lufs <= LOUDNESS[1]:
        problems.append(f"loudness {lufs:.1f} LUFS is outside {LOUDNESS[0]:.0f} to {LOUDNESS[1]:.0f}")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "facts": {"duration_s": round(duration, 2), "lufs": lufs,
                  "width": video.get("width") if video else None, "height": video.get("height") if video else None},
    }
