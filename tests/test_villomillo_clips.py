"""Phase 3 tests: the clipping steps, the clip checks and the review queue. No real network."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from villomillo import clipper, config, store, validate
from villomillo.db import Database
from villomillo.jev import JevResult

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg is not installed")

NOW = store.now()
ANSWERS = {
    "fits_niche": {"type": "noul", "noul": 0.9},
    "needs_visuals": {"type": "noul", "noul": 0.1},
    "has_moments": {"type": "score", "score": 3.5, "confidence": 0.7, "legend": {str(i): "x" for i in range(5)}},
    "topic_appeal": {"type": "score", "score": 2.5, "confidence": 0.6, "legend": {str(i): "x" for i in range(5)}},
    "hook_potential": {"type": "score", "score": 2.0, "confidence": 0.6, "legend": {str(i): "x" for i in range(5)}},
    **{k: {"type": "noul", "noul": 0.02} for k in
       ("risk_violence", "risk_sexual", "risk_minors", "risk_tragedy", "risk_hate", "risk_misinfo")},
}


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "clips.db")


def profile():
    return config.Profile(id="test", display_name="Test", niche={"definition": "podcasts"},
                          research={"watch_channels": [{"handle": "@chan"}]})


CLIP_ANSWERS = {
    "self_contained": {"type": "noul", "noul": 0.9},
    "hook": {"type": "score", "score": 3.0, "confidence": 0.8, "legend": {str(i): "x" for i in range(5)}},
    "payoff": {"type": "score", "score": 3.0, "confidence": 0.8, "legend": {str(i): "x" for i in range(5)}},
}


class FakeJev:
    model = "jev-test"

    def ask(self, state, questions):
        return JevResult("jev-test", {**copy.deepcopy(ANSWERS), **copy.deepcopy(CLIP_ANSWERS)},
                         {"input_tokens": 1000, "output_tokens": 5})


def make_clip(path: Path, *, size=(1080, 1920), seconds=30, audio=True) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
           "-i", f"testsrc=size={size[0]}x{size[1]}:rate=30:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(seconds)]
    cmd += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    subprocess.run([*cmd, str(path)], check=True, timeout=120)
    return path


def store_video(db: Database, profile_id: str, video_id: str) -> None:
    v = {
        "video_id": video_id, "channel_id": "UCchan", "channel_title": "Chan", "title": "A video",
        "description": "", "tags": [], "duration_s": 3000, "language": "en", "live_status": "none",
        "published_at": (NOW - timedelta(hours=5)).isoformat(timespec="seconds"), "views": 100,
        "likes": 1, "comments": 0,
    }
    with db.conn() as c:
        store.upsert_video(c, profile_id, v, "channel")


class FakeEngine:
    def __init__(self):
        self.submitted: list[str] = []

    def submit(self, url, *, focus, max_clips, force=False):
        self.submitted.append(url)
        return 77


def queue_job(db: Database, profile_id: str, video_id: str) -> dict:
    with db.conn() as c:
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES (?,?,?,?,?,?)", (profile_id, video_id, 1, "submitted", store.iso(), store.iso()))
        return dict(c.execute("SELECT * FROM clip_jobs").fetchone())


# ---- the engine client and submission ----------------------------------------------------------------------------


def test_a_video_is_submitted_once(db):
    p = profile()
    store_video(db, p.id, "abcdefghijk")
    eng = FakeEngine()
    assert clipper.submit(db, eng, p, "abcdefghijk").startswith("submitted")
    assert clipper.submit(db, eng, p, "abcdefghijk") == "already submitted"
    assert eng.submitted == ["https://www.youtube.com/watch?v=abcdefghijk"]


def test_the_engine_client_sends_the_right_request_and_fails_clearly():
    resumed: list[bool] = []

    def handler(request):
        if request.url.path == "/jobs" and request.method == "POST":
            body = json.loads(request.content)
            assert body["url"].startswith("https://www.youtube.com/watch?v=") and body["captions"] is True
            return httpx.Response(200, json={"job_id": 5})
        if request.url.path == "/queue/resume":
            resumed.append(True)
            return httpx.Response(200, json={"paused": False})
        if request.url.path == "/jobs/5":
            return httpx.Response(200, json={"status": "done", "error": ""})
        return httpx.Response(404, json={"detail": "no"})

    eng = clipper.Engine("http://engine.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert eng.submit("https://www.youtube.com/watch?v=abcdefghijk", focus="x", max_clips=3) == 5
    assert resumed == [True], "submitting must also un-pause the engine's queue"
    assert eng.job(5)["status"] == "done"
    down = clipper.Engine("http://127.0.0.1:1", client=httpx.Client(timeout=2))
    with pytest.raises(clipper.EngineUnavailable, match="clipping engine"):
        down.job(1)


# ---- checks on the finished file -----------------------------------------------------------------------------------


@needs_ffmpeg
def test_a_good_vertical_clip_passes_the_checks(tmp_path):
    result = validate.check(make_clip(tmp_path / "good.mp4"))
    assert result["ok"], result["problems"]
    assert result["facts"]["width"] == 1080 and result["facts"]["lufs"] is not None


@needs_ffmpeg
@pytest.mark.parametrize(
    ("kwargs", "expect"),
    [({"size": (1280, 720)}, "1080x1920"), ({"seconds": 5}, "shorter"), ({"audio": False}, "no audio"),
     ({"seconds": 120}, "longer")],
)
def test_bad_clips_say_what_is_wrong(tmp_path, kwargs, expect):
    result = validate.check(make_clip(tmp_path / "bad.mp4", **kwargs), with_loudness=False)
    assert not result["ok"]
    assert any(expect in p for p in result["problems"]), result["problems"]


# ---- collecting clips, grading them and the review queue ---------------------------------------------------------


@needs_ffmpeg
def test_collected_clips_are_checked_graded_and_queued_for_review(db, tmp_path, monkeypatch):
    p = profile()
    store_video(db, p.id, "abcdefghijk")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    good = make_clip(tmp_path / "g.mp4").read_bytes()
    short = make_clip(tmp_path / "s.mp4", seconds=5).read_bytes()

    class EngineStub:
        def job(self, job_id):
            return {"status": "done", "error": ""}

        def clips(self, video_id):
            return [{"id": 1, "start_s": 10, "end_s": 40, "score": 81, "hook": "a hook"},
                    {"id": 2, "start_s": 50, "end_s": 55, "score": 60, "hook": None}]

        def media(self, clip_id):
            return good if clip_id == 1 else short

        def captions_text(self, clip_id):
            return "the whole story in one go"

    job = queue_job(db, p.id, "abcdefghijk")
    assert clipper.collect(db, EngineStub(), FakeJev(), p, job) == "2 clips made, 1 passed the checks"
    with db.conn() as c:
        states = {r["engine_clip_id"]: r["state"] for r in c.execute("SELECT * FROM clips_ready")}
    assert states == {1: "review", 2: "failed_validation"}
    clipper.decide(db, p.id, 1, "approve")
    with pytest.raises(ValueError):
        clipper.decide(db, p.id, 1, "reject")  # already decided
    with db.conn() as c:
        assert c.execute("SELECT state FROM clips_ready WHERE engine_clip_id=1").fetchone()["state"] == "approved"


@needs_ffmpeg
def test_a_clip_that_does_not_stand_alone_is_not_offered_for_review(db, tmp_path, monkeypatch):
    p = profile()
    store_video(db, p.id, "abcdefghijk")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    good = make_clip(tmp_path / "g.mp4").read_bytes()

    class EngineStub:
        def job(self, job_id):
            return {"status": "done", "error": ""}

        def clips(self, video_id):
            return [{"id": 9, "start_s": 0, "end_s": 30, "score": 90, "hook": None}]

        def media(self, clip_id):
            return good

        def captions_text(self, clip_id):
            return "mm"

    class NoStory(FakeJev):
        def ask(self, state, questions):
            answers = {**copy.deepcopy(ANSWERS), **copy.deepcopy(CLIP_ANSWERS)}
            answers["self_contained"] = {"type": "noul", "noul": 0.1}
            return JevResult("jev-test", answers, {"input_tokens": 10, "output_tokens": 1})

    job = queue_job(db, p.id, "abcdefghijk")
    clipper.collect(db, EngineStub(), NoStory(), p, job)
    with db.conn() as c:
        assert c.execute("SELECT state FROM clips_ready").fetchone()["state"] == "rejected_auto"
    with pytest.raises(ValueError):
        clipper.decide(db, p.id, 1, "approve")  # only clips waiting for review can be decided


def test_the_engine_refusing_a_duplicate_is_reported_and_force_is_sent_on_request():
    import httpx

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs":
            body = json.loads(request.content)
            seen.append(body["force"])
            return httpx.Response(200, json={"job_id": 9} if body["force"] else {"job_id": None, "already_processed": True})
        return httpx.Response(200, json={})

    engine = clipper.Engine(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(clipper.EngineUnavailable, match="already has clips"):
        engine.submit("https://www.youtube.com/watch?v=abcdefghijk", focus="f", max_clips=3)
    assert engine.submit("https://www.youtube.com/watch?v=abcdefghijk", focus="f", max_clips=3, force=True) == 9
    assert seen == [False, True]
