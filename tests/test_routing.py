"""Phase 5, part two: which channel posts a clip. Two channels, a fake Jev, no network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from villomillo import accounts, config, posting, routing, store
from villomillo.db import Database
from villomillo.jev import JevResult
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, tzinfo=timezone.utc)
VIDEO = "vid00000001"


def channel(pid: str, niche: str, **posting_changes) -> config.Profile:
    return config.Profile(
        id=pid, display_name=pid.title(), niche={"definition": niche, "include": [niche], "exclude": []},
        posting={"platforms": {"youtube_shorts": "manual"}, "tags": {"youtube_shorts": "@tag"}, "daily_cap": 9,
                 "min_gap_minutes": 0, **posting_changes})


class FakeJev:
    def __init__(self, **scores):
        self.scores, self.calls = scores, 0

    def ask(self, state, questions):
        self.calls += 1
        answers = {name: {"score": self.scores[name.removeprefix("fit_")]} for name in questions}
        return JevResult("jev", answers, {"input_tokens": 100})


def no_ai():
    raise AssertionError("no AI call should be needed here")


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "routing.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
                  "tags_json, duration_s, published_at, first_seen_at, last_seen_at) VALUES "
                  "('pod', ?, 'channel', 'UCsrc', 'Source', 'Episode', '', '[]', 3000, ?, ?, ?)",
                  (VIDEO, store.iso(), store.iso(), store.iso()))
    return database


@pytest.fixture
def profiles():
    return {"pod": channel("pod", "podcasts and talks"), "game": channel("game", "gaming highlights")}


def approved_clip(db, state="approved") -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('pod', 1, ?, 0, 30, 80, 'a hook', 'f.mp4', '{}', ?, ?, 'r', ?)",
                  (VIDEO, json.dumps({"caption_text": "that boss fight was unreal"}), state, store.iso()))
        return c.execute("SELECT id FROM clips_ready").fetchone()["id"]


def post_owner(db, clip_id) -> str | None:
    with db.conn() as c:
        row = c.execute("SELECT profile_id FROM posts WHERE clip_id=?", (clip_id,)).fetchone()
    return row["profile_id"] if row else None


# --- choosing a channel -----------------------------------------------------------------------------------------

def test_the_channel_that_fits_best_posts_the_clip_and_the_clip_keeps_its_owner(db, profiles):
    clip = approved_clip(db)
    jev = FakeJev(pod=1, game=5)
    planned = routing.assign_and_plan(db, lambda: jev, profiles, "pod", clip, now=NOW)
    assert planned["profile"] == "game" and post_owner(db, clip) == "game"
    with db.conn() as c:
        assert c.execute("SELECT profile_id FROM clips_ready WHERE id=?", (clip,)).fetchone()["profile_id"] == "pod"


def test_a_clip_goes_back_to_its_own_channel_when_the_fit_is_a_tie(db, profiles):
    clip = approved_clip(db)
    decision = routing.route_clip(db, FakeJev(pod=4, game=4), profiles, clip, now=NOW)
    assert decision.profile_id == "pod"


def test_a_clip_with_no_channel_able_to_post_it_is_held(db, profiles):
    profiles = {k: v.model_copy(update={"posting": None}) for k, v in profiles.items()}
    clip = approved_clip(db)
    assert routing.assign_and_plan(db, no_ai, profiles, "pod", clip, now=NOW) is None
    with db.conn() as c:
        reason = c.execute("SELECT reason FROM clips_ready WHERE id=?", (clip,)).fetchone()["reason"]
    assert "approved, but not posted: no channel can post it" in reason and post_owner(db, clip) is None


def test_a_clip_that_fits_no_channel_well_is_held_with_the_reason(db, profiles):
    clip = approved_clip(db)
    assert routing.assign_and_plan(db, lambda: FakeJev(pod=2, game=2), profiles, "pod", clip, now=NOW) is None
    with db.conn() as c:
        assert "fits no channel well" in c.execute("SELECT reason FROM clips_ready WHERE id=?", (clip,)).fetchone()["reason"]
    assert post_owner(db, clip) is None


def test_a_paused_channel_is_skipped_while_another_is_active_but_used_if_all_are_paused(db, profiles):
    accounts.pause(db, "game", "youtube_shorts", "paused by you")
    jev = FakeJev(pod=3, game=5)
    clip = approved_clip(db)
    assert routing.route_clip(db, jev, profiles, clip, now=NOW).profile_id == "pod"
    accounts.pause(db, "pod", "youtube_shorts", "paused by you")
    assert routing.route_clip(db, jev, profiles, clip, now=NOW).profile_id == "game", "the post waits on its channel"


def test_a_channel_whose_slots_are_full_for_days_is_skipped(db):
    profiles = {"pod": channel("pod", "podcasts"), "game": channel("game", "gaming", daily_cap=1)}
    with db.conn() as c:
        for day in range(6):
            when = (NOW + timedelta(days=day)).isoformat(timespec="seconds")
            c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, "
                      "scheduled_at, created_at) VALUES ('game', ?, 'youtube_shorts', 'manual', 'queued', 't', 'c', "
                      "'[]', ?, ?)", (900 + day, when, when))
    decision = routing.route_clip(db, FakeJev(pod=3, game=5), profiles, approved_clip(db), now=NOW)
    assert decision.profile_id == "pod"


def test_with_one_posting_channel_there_is_nothing_to_decide_and_no_ai_call(db):
    only = {"pod": channel("pod", "podcasts and talks")}
    clip = approved_clip(db)
    planned = routing.assign_and_plan(db, no_ai, only, "pod", clip, now=NOW)
    assert planned["profile"] == "pod" and post_owner(db, clip) == "pod"


def test_a_clip_routed_to_another_channel_goes_out_through_that_channels_account(db, profiles):
    clip = approved_clip(db)
    routing.assign_and_plan(db, lambda: FakeJev(pod=1, game=5), profiles, "pod", clip, now=NOW)
    lines = posting.run_due(db, profiles["game"], now=LATER)
    assert len(lines) == 1 and lines[0].endswith("manual_ready"), lines
    with db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE clip_id=?", (clip,)).fetchone()["status"] == "manual_ready"


def test_the_ai_cost_of_choosing_is_recorded(db, profiles):
    routing.route_clip(db, FakeJev(pod=3, game=4), profiles, approved_clip(db), now=NOW)
    with db.conn() as c:
        assert c.execute("SELECT requests FROM ai_usage WHERE engine='jev'").fetchone()["requests"] == 1


# --- the approve button -----------------------------------------------------------------------------------------

def test_approving_a_clip_routes_it_and_the_other_channel_can_show_its_file(db, profiles):
    clip = approved_clip(db, state="review")
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    sv.jev = FakeJev(pod=1, game=5)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    form = {"profile": "pod", "clip_id": clip, "decision": "approve"}
    assert client.post("/api/clip-decision", data=form).status_code == 200
    assert post_owner(db, clip) == "game"
    # the file route answers for the channel that posts it, not only the one that made it
    assert client.get(f"/clips/{clip}/file?profile=game").json()["detail"] == "file not found"
    assert client.get(f"/clips/{clip}/file?profile=pod").json()["detail"] == "file not found"
    page = client.get("/calendar?profile=game&view=list").text
    assert "Calendar" in page
