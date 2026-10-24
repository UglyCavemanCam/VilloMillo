"""Phase 5, part one: the audit switch and the safety stops. No platform is contacted; adapters are fakes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from villomillo import accounts, config, posting, store
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2030, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "accounts.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
                  "tags_json, duration_s, published_at, first_seen_at, last_seen_at) VALUES "
                  "('solved','vid00000001','campaign','UC','Solved','Episode','','[]',3000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    return database


def channel(*, route="api", dry_run=False):
    solved = config.load_profiles()["solved"]
    posting_block = solved.posting.model_copy(update={"dry_run": dry_run, "platforms": {
        "youtube_shorts": route, "instagram_reels": "manual"}, "daily_cap": 9, "min_gap_minutes": 0})
    return solved.model_copy(update={"posting": posting_block})


_numbers = iter(range(1, 10_000))


def approved_clip(db) -> int:
    n = next(_numbers)
    grade = {"caption_text": "a talk about values and purpose"}
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', ?, 'vid00000001', 0, 30, 80, 'a hook', 'f.mp4', '{}', ?, 'approved', 'r', ?)",
                  (n, json.dumps(grade), store.iso()))
        return c.execute("SELECT id FROM clips_ready ORDER BY id DESC LIMIT 1").fetchone()["id"]


class Uploader:
    def __init__(self, error=None):
        self.error, self.sent = error, 0

    def publish(self, request):
        if self.error:
            raise RuntimeError(self.error)
        self.sent += 1
        return SimpleNamespace(video_id="abc", actual_privacy="private", warnings=[], locked_private=False)


def adapters(uploader):
    return {"youtube_shorts": posting.YouTubeShorts(uploader)}


# --- the audit switch -------------------------------------------------------------------------------------------

def test_the_api_is_only_used_for_real_once_the_audit_has_passed(db):
    profile = channel()
    route, why = accounts.effective_route(db, profile, "youtube_shorts")
    assert route == "manual" and "audit has not passed" in why
    accounts.set_audit(db, "solved", "youtube_shorts", True, "passed on 1 Dec")
    assert accounts.effective_route(db, profile, "youtube_shorts")[0] == "api"
    accounts.set_audit(db, "solved", "youtube_shorts", False)
    assert accounts.effective_route(db, profile, "youtube_shorts")[0] == "manual"


def test_a_dry_run_may_rehearse_the_api_before_the_audit_and_manual_stays_manual(db):
    assert accounts.effective_route(db, channel(dry_run=True), "youtube_shorts")[0] == "api"
    accounts.set_audit(db, "solved", "instagram_reels", True)
    assert accounts.effective_route(db, channel(), "instagram_reels")[0] == "manual"   # set to manual in settings


def test_a_post_planned_before_an_audit_goes_out_by_hand_and_after_it_by_the_api(db):
    profile = channel()
    first = posting.plan(db, profile, approved_clip(db), now=NOW)
    assert first["route"] == "manual"
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    assert posting.plan(db, profile, approved_clip(db), now=NOW)["route"] == "api"


def test_losing_the_audit_stops_an_api_post_from_being_sent(db):
    profile = channel()
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    post = posting.plan(db, profile, approved_clip(db), now=NOW)
    accounts.set_audit(db, "solved", "youtube_shorts", False)
    uploader = Uploader()
    lines = posting.run_due(db, profile, now=LATER, adapters=adapters(uploader))
    assert lines == [f"post {post['id']} (youtube_shorts): manual_ready"] and uploader.sent == 0


# --- what counts as a failure -----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("error", "kind"),
    [
        ("YouTube refused the upload: quotaExceeded", "quota"),
        ("Your YouTube sign-in has expired. Reconnect it", "auth"),
        ("No YouTube account is connected yet", "auth"),
        ("Your channel has been suspended", "policy"),
        ("this account received a community guidelines strike", "policy"),
        ("The YouTube Data API has not been used in this project or it is disabled", "other"),
        ("HTTP 500 backend error", "other"),
    ],
)
def test_failures_are_told_apart(error, kind):
    assert accounts.classify_failure(error) == kind


# --- the safety stops -------------------------------------------------------------------------------------------

def test_three_ordinary_failures_in_a_row_pause_the_account_and_say_why(db):
    for n in (1, 2):
        assert accounts.record_failure(db, "solved", "youtube_shorts", "HTTP 500") is None, n
    reason = accounts.record_failure(db, "solved", "youtube_shorts", "HTTP 500 backend error")
    assert accounts.is_paused(db, "solved", "youtube_shorts")
    assert "3 posts in a row failed" in reason and "HTTP 500 backend error" in reason


def test_a_success_clears_the_count(db):
    accounts.record_failure(db, "solved", "youtube_shorts", "HTTP 500")
    accounts.record_failure(db, "solved", "youtube_shorts", "HTTP 500")
    accounts.record_success(db, "solved", "youtube_shorts")
    assert accounts.record_failure(db, "solved", "youtube_shorts", "HTTP 500") is None
    assert not accounts.is_paused(db, "solved", "youtube_shorts")


def test_a_strike_or_a_broken_sign_in_pauses_at_once_but_a_quota_error_never_does(db):
    assert accounts.record_failure(db, "solved", "instagram_reels", "quotaExceeded") is None
    assert accounts.state(db, "solved", "instagram_reels")["failures"] == 0
    assert "strike or suspension" in accounts.record_failure(db, "solved", "instagram_reels", "channel suspended")
    accounts.resume(db, "solved", "instagram_reels")
    assert "reconnect the account" in accounts.record_failure(db, "solved", "instagram_reels", "invalid_grant")


def test_a_paused_account_sends_nothing_and_its_posts_wait_until_you_resume(db):
    profile = channel()
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    post = posting.plan(db, profile, approved_clip(db), now=NOW)
    accounts.pause(db, "solved", "youtube_shorts", "paused by you")
    uploader = Uploader()
    assert "paused" in posting.run_due(db, profile, now=LATER, adapters=adapters(uploader))[0] and uploader.sent == 0
    with db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE id=?", (post["id"],)).fetchone()["status"] == "queued"
    accounts.resume(db, "solved", "youtube_shorts")
    assert posting.run_due(db, profile, now=LATER, adapters=adapters(uploader)) == [f"post {post['id']} (youtube_shorts): uploaded"]


def test_repeated_failed_posts_pause_the_account_by_themselves_and_the_next_post_waits(db):
    profile = channel()
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    ids = [posting.plan(db, profile, approved_clip(db), now=NOW)["id"] for _ in range(4)]
    broken = Uploader(error="HTTP 503 backend error")
    lines = posting.run_due(db, profile, now=LATER, adapters=adapters(broken))
    assert [line.split(":")[1].split(",")[0].strip() for line in lines] == ["failed", "failed", "failed", "waiting"]
    state = accounts.state(db, "solved", "youtube_shorts")
    assert state["status"] == "paused" and "3 posts in a row failed" in state["reason"]
    with db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE id=?", (ids[3],)).fetchone()["status"] == "queued"


# --- a sudden drop in views -------------------------------------------------------------------------------------

def test_a_sudden_drop_in_views_is_noticed_only_with_enough_posts():
    usual = [1000, 1200, 900, 1100, 1000, 1300]
    assert accounts.views_dropped([150, 200, 120, 180, 160], usual)
    assert not accounts.views_dropped([900, 1000, 800, 1100, 950], usual)
    assert not accounts.views_dropped([100, 100], usual), "too few recent posts to tell"
    assert not accounts.views_dropped([100] * 5, [1000] * 3), "too little history to tell"


# --- the Accounts page ------------------------------------------------------------------------------------------

def test_the_accounts_page_shows_state_and_pause_and_resume_work(db):
    sv = Services(config.AppConfig(), config.load_profiles(), db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    page = client.get("/accounts?profile=solved").text
    assert "Youtube Shorts" in page and "Not passed yet" in page and ">Pause<" in page
    form = {"profile": "solved", "platform": "youtube_shorts", "action": "pause"}
    assert client.post("/api/account-state", data=form).status_code == 200
    assert accounts.is_paused(db, "solved", "youtube_shorts")
    assert "Resume" in client.get("/accounts?profile=solved").text
    assert client.post("/api/account-state", data={**form, "action": "resume"}).status_code == 200
    assert not accounts.is_paused(db, "solved", "youtube_shorts")
    assert client.post("/api/account-state", data=form, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/account-state", data={**form, "platform": "tiktok"}).status_code == 404
    assert client.post("/api/account-state", data={**form, "action": "explode"}).status_code == 422
