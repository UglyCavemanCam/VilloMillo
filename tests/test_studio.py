"""Clip studio: what the page is told about progress, steps, timeline and clips. No real engine needed."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from villomillo import config, store, studio
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

RUNNING = "[1/4] Downloading: x\n[2/4] Transcribing...\n[3/4] Multimodal analysis (transcript + audio)\n  Chunk 1/10: 5 candidate(s)\n  Chunk 2/10: 3 candidate(s)\n  Ranking 3 finalist(s) in 1 batch(es)...\n[4/4] Rendering clips..."
FINISHED = RUNNING + "\n  Done in 4.9 min (3 clips)"


def test_progress_is_read_from_the_engine_log():
    assert studio.read_log(RUNNING) == {"stage": 4, "finished": False, "candidates": 8, "finalists": 3}
    assert studio.read_log(FINISHED)["finished"] is True
    assert studio.read_log("")["stage"] == 0


@pytest.mark.parametrize(
    ("log", "job_status", "expected"),
    [
        (RUNNING, "running", ["done", "done", "done", "done", "now", "waiting", "waiting", "waiting"]),
        ("[2/4] Transcribing...", "running", ["done", "now", "waiting", "waiting", "waiting", "waiting", "waiting", "waiting"]),
        (FINISHED, "done", ["done", "done", "done", "done", "done", "waiting", "waiting", "waiting"]),
    ],
)
def test_engine_steps_show_done_now_and_waiting(log, job_status, expected):
    got = [s["state"] for s in studio._step_states(studio.read_log(log), job_status, [])]
    assert got == expected


def test_review_is_the_current_step_once_clips_are_waiting_for_you():
    clips = [{"state": "review", "grade": {"hook": 2}}]
    states = [s["state"] for s in studio._step_states(studio.read_log(FINISHED), "done", clips)]
    assert states[5:] == ["done", "done", "now"]


def test_percentages_are_approximate_and_hit_100_when_finished():
    assert studio._percent(studio.read_log(RUNNING), "running") == 80
    assert studio._percent(studio.read_log(FINISHED), "done") == 100
    assert studio._percent(None, None) == 0


def test_elapsed_time_counts_from_the_job_start():
    started = (store.now() - timedelta(seconds=125)).isoformat(timespec="seconds")
    assert 120 <= studio._elapsed_s(started) <= 130


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "studio.db")


def add_clip(db, profile_id, engine_id, video_id, start, end, state):
    with db.conn() as c:
        c.execute(
            "INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
            "file_path, validation_json, grade_json, state, reason, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (profile_id, engine_id, video_id, start, end, 80, "a hook", f"/tmp/{engine_id}.mp4",
             json.dumps({"ok": True, "problems": [], "facts": {"duration_s": end - start}}),
             json.dumps({"self_contained": 0.74}), state, "why", store.iso()),
        )


def test_timeline_places_clips_by_their_position_in_the_episode(db):
    with db.conn() as c:
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES ('solved', 'vid00000001', 7, 'done', ?, ?)", (store.iso(), store.iso()))
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
                  "tags_json, duration_s, published_at, first_seen_at, last_seen_at) VALUES "
                  "('solved','vid00000001','campaign','UC','Show','Episode','','[]',1000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    add_clip(db, "solved", 1, "vid00000001", 500, 530, "review")
    add_clip(db, "solved", 2, "vid00000001", 0, 50, "approved")
    data = studio.build(db, lambda job_id: FINISHED, "solved")
    ticks = {t["id"]: t for t in data["video"]["clips"]}
    assert ticks[1]["left_pct"] == 50.0 and ticks[1]["width_pct"] == 3.0
    assert ticks[2]["left_pct"] == 0.0 and ticks[2]["state"] == "approved"


def test_counts_and_clips_come_from_our_own_records(db):
    add_clip(db, "solved", 1, "vid00000001", 0, 30, "review")
    add_clip(db, "solved", 2, "vid00000001", 40, 70, "rejected_auto")
    data = studio.build(db, lambda job_id: None, "solved")
    assert data["counts"]["clips_made"] == 2 and data["counts"]["passed_checks"] == 2
    assert data["engine_ok"] is True  # no job means there is nothing to contact the engine about


def test_an_unreachable_engine_is_reported_plainly(db):
    with db.conn() as c:
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES ('solved', 'vid00000001', 7, 'running', ?, ?)", (store.iso(), store.iso()))
    data = studio.build(db, lambda job_id: None, "solved")
    assert data["engine_ok"] is False
    assert "not running" in data["engine_message"]


def test_the_studio_endpoint_returns_json_and_the_page_loads(db, tmp_path):
    sv = Services(config.AppConfig(), config.load_profiles(), db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    assert client.get("/clips?profile=solved&tab=progress").status_code == 200
    assert client.get("/studio?profile=solved", follow_redirects=False).headers["location"].startswith("/clips?tab=progress")
    data = client.get("/api/studio?profile=solved").json()
    assert set(data) >= {"counts", "steps", "clips", "percent", "engine_ok"}
    assert [s["name"] for s in data["steps"]] == studio.STEPS
    assert client.get("/api/studio?profile=nobody").status_code == 404
