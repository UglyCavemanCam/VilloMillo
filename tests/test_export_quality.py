"""The clips are exported with plenty of picture detail and a colour tag, and a clip with too little detail is flagged."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from villomillo import validate
from villomillo.formats import render


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if not exe or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg isn't installed here")
    return exe


def _encode(tmp_path, args, name):
    """What render.py's ffmpeg does with raw frames, at a small size: calm frames in, an mp4 out."""
    out = tmp_path / name
    frame = bytes([30, 40, 60]) * (270 * 480)
    proc = subprocess.run([_ffmpeg(), "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "270x480", "-r", "30", "-i", "pipe:0",
                           *args, "-an", str(out)], input=frame * 60, capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()[-300:]
    return out


def _stream(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name,profile,pix_fmt,color_space,color_primaries,color_transfer,color_range", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"][0]


def test_the_export_is_high_detail_h264_tagged_as_bt709(tmp_path):
    args = render.VIDEO_ARGS
    assert args[args.index("-crf") + 1] == "14"
    stream = _stream(_encode(tmp_path, args, "clip.mp4"))
    assert (stream["codec_name"], stream["pix_fmt"], stream["profile"]) == ("h264", "yuv420p", "High")
    assert (stream["color_space"], stream["color_primaries"], stream["color_transfer"], stream["color_range"]) == ("bt709", "bt709", "bt709", "tv")


def test_untagged_frames_come_out_with_no_colour_tag_which_is_what_the_export_fixes(tmp_path):
    old = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    assert _stream(_encode(tmp_path, old, "old.mp4")).get("color_space") in (None, "unknown")


def test_a_low_bitrate_clip_is_flagged_and_the_figure_is_reported(tmp_path):
    low = _encode(tmp_path, ["-c:v", "libx264", "-b:v", "300k", "-pix_fmt", "yuv420p"], "low.mp4")
    assert validate._video_bitrate({"bit_rate": "9400000"}, {}) == pytest.approx(9.4)
    assert validate._video_bitrate({}, {"format": {"duration": "10", "size": "5000000"}}) == pytest.approx(4.0)
    assert validate._video_bitrate(None, {}) is None
    info = validate.probe(low)
    rate = validate._video_bitrate(next(s for s in info["streams"] if s["codec_type"] == "video"), info)
    assert rate is not None and rate < validate.LOW_BITRATE_MBPS
