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


# ---- the larger upload ---------------------------------------------------------------------------------------------


def _clip(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run([_ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30:duration=1",
                    "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)], check=True)
    return clip


def test_the_enlarged_copy_is_1440_by_2560_tagged_and_keeps_its_sound(tmp_path, monkeypatch):
    from villomillo import posting

    monkeypatch.setattr(posting, "UPLOAD_W", 540)  # the real size is 1440x2560; a small one keeps the test quick
    monkeypatch.setattr(posting, "UPLOAD_H", 960)
    clip = _clip(tmp_path)
    big = posting.enlarged_copy(clip, tmp_path / "big.mp4")
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,color_space", "-of", "json", str(big)],
                          capture_output=True, text=True, check=True)
    streams = {s["codec_type"]: s for s in json.loads(info.stdout)["streams"]}
    assert (streams["video"]["width"], streams["video"]["height"], streams["video"]["color_space"]) == (540, 960, "bt709")
    assert "audio" in streams and clip.exists()
    assert (posting.UPLOAD_W, posting.UPLOAD_H) == (540, 960)  # (restored below by monkeypatch)


def _send_with(tmp_path, monkeypatch, size):
    from types import SimpleNamespace

    from villomillo import config, posting

    monkeypatch.setattr(posting, "UPLOAD_W", 540)
    monkeypatch.setattr(posting, "UPLOAD_H", 960)
    clip = _clip(tmp_path)
    seen = {}

    class Adapter:
        def post(self, *, path, copy, privacy):
            seen["path"], seen["existed"] = path, path.exists()
            return {"url": "https://youtube.com/shorts/x", "notes": [], "remote_id": "x"}

    profile = SimpleNamespace(posting=config.Posting(upload_size=size, privacy="public"))
    from villomillo.db import Database

    status = posting._send(Database(tmp_path / "s.db"), profile, {"id": 1, "platform": "youtube_shorts"}, {"title": "t"},
                           {"file_path": str(clip)}, {"youtube_shorts": Adapter()})
    return status, seen, clip


def test_a_channel_set_to_larger_uploads_the_enlarged_copy_and_removes_it_afterwards(tmp_path, monkeypatch):
    status, seen, clip = _send_with(tmp_path, monkeypatch, "1440")
    assert status[0] == "uploaded" and seen["path"].name == "clip.upload.mp4" and seen["existed"]
    assert not seen["path"].exists() and clip.exists()


def test_a_channel_on_normal_size_uploads_the_clip_itself(tmp_path, monkeypatch):
    status, seen, clip = _send_with(tmp_path, monkeypatch, "1080")
    assert status[0] == "uploaded" and seen["path"] == clip


def test_if_the_enlarging_fails_the_clip_still_goes_out_at_its_own_size(tmp_path, monkeypatch):
    from villomillo import posting

    def broken(path, out):
        raise posting.PostError("ffmpeg failed")

    monkeypatch.setattr(posting, "enlarged_copy", broken)
    status, seen, clip = _send_with(tmp_path, monkeypatch, "1440")
    assert status[0] == "uploaded" and seen["path"] == clip
