"""Preview: the engine can find and score a video's moments without making clips, and later make only the chosen ones.

The pipeline is run end to end with its slow parts (download, Whisper, the model, rendering) replaced by stubs, so what
is tested is the stopping and the choosing, not the video work.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.models import ClipCandidate, DownloadedVideo, Segment

VIDEO = "aB3dEfGhIjK"
URL = f"https://www.youtube.com/watch?v={VIDEO}"


def moment(start: float, score: int, **kw) -> ClipCandidate:
    return ClipCandidate(start=start, end=start + 30, score=score, hook=f"hook {start:.0f}", reason="a clear point",
                         subscores={"text": score, "audio": 50, "plugin_ratings": [{"name": "x"}]}, **kw)


MOMENTS = [moment(100, 91), moment(400, 78), moment(900, 66, trending=True)]


# ---- stored moments --------------------------------------------------------------------------------------------


def test_moments_come_back_exactly_as_found(db):
    db.save_moments(VIDEO, MOMENTS)
    listed = db.moments(VIDEO)
    assert [(m["id"], m["start"], m["score"], m["hook"]) for m in listed] == [(1, 100, 91, "hook 100"), (2, 400, 78, "hook 400"), (3, 900, 66, "hook 900")]
    assert listed[2]["trending"] is True and listed[0]["duration"] == 30
    assert db.moment_candidates(VIDEO, [3, 1]) == [MOMENTS[2], MOMENTS[0]]  # in the order asked, whole


def test_a_new_preview_replaces_the_old_one_and_an_unknown_moment_is_refused(db):
    db.save_moments(VIDEO, MOMENTS)
    db.save_moments(VIDEO, MOMENTS[:1])
    assert len(db.moments(VIDEO)) == 1
    with pytest.raises(LookupError, match="preview the video first"):
        db.moment_candidates(VIDEO, [2])
    assert db.moments("nothing-previewed") == []


# ---- the pipeline ----------------------------------------------------------------------------------------------


@pytest.fixture
def run(tmp_path, monkeypatch, db):
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from core import pipeline

    calls = SimpleNamespace(find=0, render=[], registered=[], transcribed=0, signals=0)
    source = tmp_path / f"{VIDEO}.mp4"
    source.write_bytes(b"x")

    def find(video_path, segments, llm, config, **kw):
        calls.find += 1
        return [m for m in MOMENTS if m.score >= config["clips"].get("min_score", 55)], []

    def transcribe(*a, **k):
        calls.transcribed += 1
        return [Segment(95, 120, "hello there"), Segment(405, 420, "second point")]

    def render(video_path, candidate, segments, clip_dir, config, opts, lang):
        calls.render.append(candidate.start)
        return tmp_path / f"clip{candidate.start:.0f}.mp4", "{}"

    def register(db_, video_id, candidate, path, meta, opts_json, config=None):
        calls.registered.append(candidate.start)
        return None

    import analysis.audio_features as audio
    import analysis.hype as hype
    import analysis.visual_features as visual
    import transcription.transcriber as transcriber

    def signals(*a, **k):
        calls.signals += 1
        return {}

    monkeypatch.setattr(audio, "extract_audio_features", signals)
    monkeypatch.setattr(visual, "extract_visual_features", signals)
    monkeypatch.setattr(hype, "audience_signals", lambda *a, **k: (None, []))

    monkeypatch.setattr(pipeline, "_cached_or_download", lambda *a, **k: DownloadedVideo(VIDEO, "A talk", source, 3600, "Chan"))
    monkeypatch.setattr(pipeline, "convert_slow_source", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "transcribe", transcribe)
    monkeypatch.setattr(transcriber, "detected_language", lambda *a, **k: "en")
    monkeypatch.setattr(pipeline, "create_backend", lambda cfg: object())
    monkeypatch.setattr(pipeline, "_with_usable_model", lambda cfg: cfg)
    monkeypatch.setattr(pipeline, "clip_direction", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "find_clips", find)
    monkeypatch.setattr(pipeline, "_refine_windows", lambda profile, candidates, *a, **k: candidates)
    monkeypatch.setattr(pipeline, "generate_metadata_batch", lambda cands, *a, **k: [SimpleNamespace(title="t", hashtags=[]) for _ in cands])
    monkeypatch.setattr(pipeline, "_render_files", render)
    monkeypatch.setattr(pipeline, "_register_clip", register)
    monkeypatch.setattr(pipeline, "_share_the_cpu", lambda n: None)
    monkeypatch.setattr(pipeline, "_remote_renderer", lambda config: None)

    def go(**clips):
        config = {"clips": {"captions": False, **clips}, "paths": {"data_dir": str(tmp_path)}, "llm": {},
                  "whisper": {"model": "tiny", "device": "cpu"}}
        return pipeline.process_video(URL, config, db)

    return go, calls


def test_a_preview_keeps_the_moments_and_makes_no_clips(run, db):
    go, calls = run
    assert go(preview_only=True) == []
    assert calls.render == [] and calls.registered == []
    assert [m["score"] for m in db.moments(VIDEO)] == [91, 78, 66]
    assert db.conn.execute("SELECT status FROM videos WHERE video_id=?", (VIDEO,)).fetchone()["status"] == "analyzed"
    assert not db.shorts_made(VIDEO)  # a preview is not "already processed"


def test_a_preview_keeps_the_words_spoken_in_each_moment(run, db):
    go, _ = run
    go(preview_only=True, min_score=0)
    assert [m["text"] for m in db.moments(VIDEO)] == ["hello there", "second point", ""]


def test_a_preview_can_keep_the_moments_below_the_bar_too(run, db):
    go, _ = run
    go(preview_only=True, min_score=0)
    assert len(db.moments(VIDEO)) == 3
    go(preview_only=True, min_score=80)
    assert [m["score"] for m in db.moments(VIDEO)] == [91]


def test_a_preview_that_finds_nothing_says_so(run, db):
    go, _ = run
    go(preview_only=True, min_score=100)
    assert db.moments(VIDEO) == []


def test_only_the_chosen_moments_are_made_and_they_are_not_found_again(run, db):
    go, calls = run
    go(preview_only=True, min_score=0)
    finds = calls.find
    seen = calls.signals
    go(render_only=[1, 3])
    assert calls.find == finds, "the moments are not looked for a second time"
    assert calls.signals == seen, "nothing is measured again for moments that are already chosen"
    assert sorted(calls.render) == [100, 900] and sorted(calls.registered) == [100, 900]


def test_choosing_a_moment_that_was_never_previewed_fails_clearly(run):
    go, _ = run
    with pytest.raises(LookupError, match="preview the video first"):
        go(render_only=[2])


def test_without_either_option_the_pipeline_makes_every_clip_as_before(run, db):
    go, calls = run
    go()
    assert sorted(calls.render) == [100, 400, 900] and db.moments(VIDEO) == []


# ---- the job options -------------------------------------------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("numpy")
    pytest.importorskip("yt_dlp")
    from fastapi.testclient import TestClient

    from main import BUNDLED_CONFIG, load_config
    from server.api import create_app

    root = Path(__file__).resolve().parent.parent
    settings = tmp_path / "settings.yaml"
    settings.write_text((root / "config" / "settings.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    config = load_config(BUNDLED_CONFIG)
    config["paths"]["data_dir"] = str(tmp_path / "data")
    return TestClient(create_app(config, settings), base_url="http://127.0.0.1")


def _payload(client, job_id) -> dict:
    return json.loads(client.get(f"/jobs/{job_id}").json()["payload"])


def test_a_job_can_ask_for_a_preview_or_for_chosen_moments(api):
    preview = api.post("/jobs", json={"url": URL, "preview": True, "min_score": 0}).json()["job_id"]
    assert _payload(api, preview)["preview"] is True and _payload(api, preview)["min_score"] == 0
    other = "https://www.twitch.tv/videos/123456789"
    render = api.post("/jobs", json={"url": other, "moments": [3, 1, 3]}).json()["job_id"]
    assert _payload(api, render)["moments"] == [1, 3]


def test_bad_moment_choices_are_refused(api):
    for body in ({"moments": []}, {"moments": [0]}, {"moments": list(range(1, 12))}, {"moments": [1], "preview": True}):
        r = api.post("/jobs", json={"url": URL, **body})
        assert r.status_code == 400, body


def test_a_preview_is_allowed_for_a_video_that_already_has_clips(api, tmp_path):
    from core.state import StateDB

    d = StateDB(tmp_path / "data" / "state.db")
    d.upsert_video(VIDEO, title="t", channel_name="c", duration=100)
    d.set_outcome(VIDEO, {"clips": 1})
    d.set_video_status(VIDEO, "done")
    d.close()
    assert api.post("/jobs", json={"url": URL}).json().get("already_processed") is True
    assert api.post("/jobs", json={"url": URL, "preview": True}).json()["job_id"]


def test_the_moments_endpoint_lists_what_a_preview_kept(api, tmp_path):
    from core.state import StateDB

    assert api.get(f"/videos/{VIDEO}/moments").json() == []
    d = StateDB(tmp_path / "data" / "state.db")
    d.save_moments(VIDEO, MOMENTS)
    d.close()
    got = api.get(f"/videos/{VIDEO}/moments").json()
    assert [m["id"] for m in got] == [1, 2, 3] and got[0]["score"] == 91 and got[0]["hook"] == "hook 100"


def test_the_preview_endpoint_has_the_video_and_its_moments_with_their_words(api, tmp_path):
    from core.state import StateDB

    assert api.get(f"/videos/{VIDEO}/preview").json() == {"video": None, "moments": []}
    d = StateDB(tmp_path / "data" / "state.db")
    d.upsert_video(VIDEO, title="Episode 12", channel_name="A Show", duration=3600)
    d.save_moments(VIDEO, MOMENTS, ["one", "two", "three"])
    d.close()
    got = api.get(f"/videos/{VIDEO}/preview").json()
    assert got["video"]["title"] == "Episode 12" and got["video"]["channel"] == "A Show"
    assert [m["text"] for m in got["moments"]] == ["one", "two", "three"]


def test_a_preview_database_from_before_the_words_were_kept_gains_the_column(tmp_path):
    import sqlite3

    from core.state import StateDB

    path = tmp_path / "old.db"
    StateDB(path).close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE moments DROP COLUMN text")
    raw.commit()
    raw.close()
    d = StateDB(path)
    d.save_moments(VIDEO, MOMENTS, ["a", "b", "c"])
    assert d.moments(VIDEO)[0]["text"] == "a"
