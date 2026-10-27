"""Phase 0 of the format builder: the sample videos it opens on are the ones with the most speech, and `formats-bench` times
the real renderer on this computer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest
from test_research import make_sv

from villomillo import cli, formats, store
from villomillo.formats import preview

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


def write_transcript(folder, video_id, starts):
    words = [{"start": t, "end": t + 0.3, "word": f" w{i}"} for i, t in enumerate(starts)]
    (folder / f"{video_id}.json").write_text(json.dumps({"segments": [{"words": words}]}), encoding="utf-8")


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Three downloaded videos with transcripts: one talkative, one in between, one quiet, and a transcript with no video."""
    downloads, transcripts = tmp_path / "downloads", tmp_path / "transcripts"
    downloads.mkdir()
    transcripts.mkdir()
    monkeypatch.setattr(formats, "DOWNLOADS", downloads)
    monkeypatch.setattr(formats, "TRANSCRIPTS", transcripts)
    write_transcript(transcripts, "quietvideo3", [31 + 3 * i for i in range(10)])
    write_transcript(transcripts, "talkyvideo1", [31 + 0.4 * i for i in range(80)])
    write_transcript(transcripts, "middlevide2", [31 + 1.0 * i for i in range(34)])
    write_transcript(transcripts, "novideovid4", [31 + 0.1 * i for i in range(300)])
    for name in ("quietvideo3", "talkyvideo1", "middlevide2"):
        (downloads / f"{name}.mp4").write_bytes(b"x")
    return downloads, transcripts


def test_the_samples_are_the_downloaded_videos_with_the_most_speech_first(tmp_path, monkeypatch, library):
    sv = make_sv(tmp_path, monkeypatch)
    with sv.db.conn() as c:
        store.upsert_video(c, "solved", {"video_id": "talkyvideo1", "title": "The talkative one", "channel_id": "c", "channel_title": "c",
                                         "description": "", "tags": [], "language": "en", "live_status": "none",
                                         "published_at": store.iso(), "duration_s": 3000, "views": 1, "likes": 1, "comments": 1}, "manual")
    samples = preview.sample_videos(sv.db)
    assert [v["id"] for v in samples] == ["talkyvideo1", "middlevide2", "quietvideo3"]          # a transcript without its video is not offered
    assert samples[0]["title"] == "The talkative one" and samples[0]["words"] > samples[1]["words"] > samples[2]["words"]
    best = preview.best_samples(sv.db, 2)
    assert [v["id"] for v in best] == ["talkyvideo1", "middlevide2"] and best[0]["end"] - best[0]["start"] == preview.SAMPLE_LENGTH
    assert best[0]["start"] >= 30                                              # not the first half minute of a long video


def test_the_densest_stretch_is_remembered_until_the_transcript_changes(library):
    _, transcripts = library
    first = preview.sample_window("talkyvideo1")
    assert preview.sample_window("talkyvideo1") == first
    write_transcript(transcripts, "talkyvideo1", [100 + 0.2 * i for i in range(120)])
    os.utime(transcripts / "talkyvideo1.json", ns=(1, 1))   # a new file, whatever the clock says
    assert preview.sample_window("talkyvideo1")[0] >= 100


def test_the_live_preview_is_made_of_short_pieces_at_a_smaller_size():
    assert 2 <= preview.SEGMENT_SECONDS <= 10 and 0.25 <= preview.DRAFT_SCALE < 1.0


def test_the_bench_command_is_registered():
    parser_help = subprocess.run([sys.executable, "-m", "villomillo", "formats-bench", "--help"], capture_output=True, text=True, timeout=120)
    assert parser_help.returncode == 0 and "--seconds" in parser_help.stdout
    assert callable(cli.cmd_formats_bench)


@needs_ffmpeg
def test_the_bench_times_each_way_of_rendering_a_piece(tmp_path, monkeypatch, library):
    downloads, _ = library
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=40",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=40", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(downloads / "talkyvideo1.mp4")], check=True, timeout=180)
    sv = make_sv(tmp_path, monkeypatch)
    profile = next(iter(sv.profiles.values()))
    result = preview.bench(sv.db, profile, "talkyvideo1", 3.0)
    assert [r["way"] for r in result["rows"]] == ["draft: half size", "preview: full size", "final quality: full size"]
    assert all(r["seconds"] > 0 for r in result["rows"]) and result["length"] == 3.0 and result["video"] == "talkyvideo1"


def test_the_bench_says_what_to_do_when_there_is_no_video_to_measure_on(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(formats, "DOWNLOADS", empty)
    monkeypatch.setattr(formats, "TRANSCRIPTS", empty)
    sv = make_sv(tmp_path, monkeypatch)
    with pytest.raises(preview.PreviewError, match="make a clip first"):
        preview.bench(sv.db, next(iter(sv.profiles.values())))


def test_finding_the_busiest_stretch_is_fast_on_a_long_video_and_gives_the_same_answer(monkeypatch):
    """The Formats page asks for the busiest stretch of every downloaded video each time it opens. A three-hour podcast has about
    thirty thousand words, so counting the words in a window for every word must not be done by scanning them all each time."""
    import random
    import time

    rng = random.Random(7)
    long = [{"t0": i * 0.35 + rng.random() * 0.1} for i in range(60000)]
    monkeypatch.setattr(preview, "clip_words", lambda video_id, start, end: long)
    began = time.time()
    start, words = preview._compute_densest("long", 35.0)
    assert time.time() - began < 3.0 and words >= 90 and start >= 30

    few = [{"t0": rng.random() * 200} for _ in range(400)]
    monkeypatch.setattr(preview, "clip_words", lambda video_id, start, end: few)
    starts = sorted(w["t0"] for w in few)
    best, best_n = starts[0], 0
    for t in starts:   # the plain way, for a short video
        if t < 30 and starts[-1] > 60:
            continue
        n = sum(1 for s in starts if t <= s < t + 35.0)
        if n > best_n:
            best, best_n = t, n
    assert preview._compute_densest("short", 35.0) == (best, best_n)
