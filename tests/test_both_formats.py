"""16:9 and 9:16 clips of the same video (issue #98).

A Longform run marks a video done too, so asking for its Shorts afterwards
was refused as "already processed", and an added file was queued and
finished in two seconds with nothing made. "Already processed" now means
Shorts made before. And "Also make 9:16 Shorts" makes both in one job.
"""

import json
import shutil
import sys
import time
import types
from pathlib import Path

import pytest

from core.state import StateDB

ROOT = Path(__file__).resolve().parent.parent
URL = "https://www.youtube.com/watch?v=abcdefghijk"
VID = "abcdefghijk"


def _done(db: StateDB, vid: str = VID) -> None:
    db.upsert_video(vid, title="A video")
    db.set_video_status(vid, "done")


def _longform_clip(db: StateDB, vid: str = VID, start: float = 10.011) -> None:
    db.add_clip(vid, start, start + 30, 70, "hook", path="l.mp4",
                render_opts=json.dumps({"profile": "short_clips"}))


def _short_clip(db: StateDB, vid: str = VID, start: float = 10.0) -> None:
    db.add_clip(vid, start, start + 30, 70, "hook", path="s.mp4", render_opts="")


# ---- what "already processed" means for Shorts ------------------------------


def test_a_video_with_only_longform_output_has_no_shorts_made(tmp_path):
    db = StateDB(tmp_path / "state.db")
    _done(db)
    _longform_clip(db)
    assert db.shorts_made(VID) is False


def test_a_shorts_run_is_known_by_its_outcome_even_with_no_clips(tmp_path):
    db = StateDB(tmp_path / "state.db")
    _done(db)
    db.set_outcome(VID, {"candidates": 0})
    assert db.shorts_made(VID) is True


def test_a_video_from_before_outcomes_is_known_by_its_vertical_clips(tmp_path):
    db = StateDB(tmp_path / "state.db")
    _done(db)
    _longform_clip(db)
    _short_clip(db)
    assert db.shorts_made(VID) is True


def test_an_unfinished_or_unknown_video_has_no_shorts_made(tmp_path):
    db = StateDB(tmp_path / "state.db")
    assert db.shorts_made(VID) is False
    db.upsert_video(VID, title="A video")
    db.set_video_status(VID, "analyzed")
    db.set_outcome(VID, {"candidates": 3})
    assert db.shorts_made(VID) is False


# ---- the API's checks --------------------------------------------------------


@pytest.fixture
def api(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("yaml")
    pytest.importorskip("yt_dlp")  # sources.dispatch imports the YouTube source
    from fastapi.testclient import TestClient

    from main import BUNDLED_CONFIG, load_config
    from server.api import create_app

    settings = tmp_path / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings)
    config = load_config(BUNDLED_CONFIG)
    config["paths"]["data_dir"] = str(tmp_path / "data")
    app = create_app(config, settings)
    # No `with`: startup never runs, so no worker thread starts.
    client = TestClient(app, base_url="http://127.0.0.1")
    db = StateDB(Path(config["paths"]["data_dir"]) / "state.db")
    return client, db


def test_shorts_of_a_longform_only_video_are_queued(api):
    client, db = api
    _done(db)
    _longform_clip(db)
    res = client.post("/jobs", json={"url": URL}).json()
    assert res.get("job_id") and not res.get("already_processed")


def test_shorts_of_a_longform_only_video_are_queued_from_a_list(api):
    client, db = api
    _done(db)
    _longform_clip(db)
    res = client.post("/jobs/batch", json={"items": [{"url": URL}]}).json()
    assert len(res["created"]) == 1 and res["skipped"] == []


def test_longform_of_a_processed_video_is_queued_from_a_list(api):
    client, db = api
    _done(db)
    db.set_outcome(VID, {"candidates": 2})
    res = client.post("/jobs/batch", json={"items": [{"url": URL, "longform": {"mode": "short_clips"}}]}).json()
    assert len(res["created"]) == 1 and res["skipped"] == []


def test_shorts_made_before_still_ask_first(api):
    client, db = api
    _done(db)
    db.set_outcome(VID, {"candidates": 2})
    assert client.post("/jobs", json={"url": URL}).json()["already_processed"] is True
    skipped = client.post("/jobs/batch", json={"items": [{"url": URL}]}).json()["skipped"]
    assert [s["reason"] for s in skipped] == ["already_processed"]
    assert client.post("/jobs", json={"url": URL, "force": True}).json()["job_id"]


# ---- both formats in one job -------------------------------------------------


@pytest.fixture
def both(tmp_path, monkeypatch):
    jobs = pytest.importorskip("server.jobs")  # CI installs only the light dependencies
    w = jobs.Worker({"paths": {"data_dir": str(tmp_path)}})
    w._progress[7] = {"started": time.time(), "fraction": 0.0, "stage": "", "label": "Starting"}
    calls: list = []
    seen: list = []

    def process_video(url, cfg, db, force=False):
        calls.append(("shorts", url, force))
        seen.append(cfg)
        w._record_progress(7, {"stage": "render", "clip": 4, "total": 4})
        calls.append(("percent", w.progress_snapshot(7)["percent"]))
        db.set_video_status(VID, "done")
        return []

    def process_longform(url, cfg, db, options):
        calls.append(("longform", url, options["mode"]))
        seen.append(cfg)
        db.set_video_status(VID, "analyzed")
        w._record_progress(7, {"stage": "download", "fraction": 0.5})
        calls.append(("percent", w.progress_snapshot(7)["percent"]))
        if options.get("fail"):
            raise RuntimeError("encoder exploded")

    monkeypatch.setitem(sys.modules, "core.pipeline", types.SimpleNamespace(process_video=process_video))
    monkeypatch.setitem(sys.modules, "longform.process", types.SimpleNamespace(process_longform=process_longform))
    db = StateDB(tmp_path / "state.db")
    db.upsert_video(VID, title="A video")
    return w, db, calls, seen


def test_both_formats_make_the_shorts_then_the_16x9(both):
    w, db, calls, seen = both
    cfg = {"clips": {"min_score": 55}}
    w._both_formats(db, 7, VID, {"url": URL, "longform": {"mode": "short_clips", "shorts": True}}, cfg)
    steps = [c for c in calls if c[0] != "percent"]
    assert steps == [("shorts", URL, False), ("longform", URL, "short_clips")]
    assert seen[0] is not seen[1] and seen[0] == seen[1]  # each pass its own settings


def test_each_pass_gets_half_the_progress_bar(both):
    w, db, calls, _ = both
    w._both_formats(db, 7, VID, {"url": URL, "longform": {"mode": "short_clips", "shorts": True}}, {})
    first, second = [c[1] for c in calls if c[0] == "percent"]
    assert first <= 50 < second


def test_a_failed_16x9_pass_keeps_the_shorts_and_says_which_part_failed(both):
    w, db, _, _ = both
    payload = {"url": URL, "longform": {"mode": "short_clips", "shorts": True, "fail": True}}
    with pytest.raises(RuntimeError, match="9:16 Shorts were made; the 16:9 pass failed: encoder exploded"):
        w._both_formats(db, 7, VID, payload, {})
    assert db.video_status(VID) == "done"
