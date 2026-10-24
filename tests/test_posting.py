"""Posting: copy, routing, spacing, planning, the dry run, manual uploads and both platform adapters.
No real platform is contacted: the adapters are tested against fakes and a mock HTTP transport."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from villomillo import accounts, config, posting, store
from villomillo.db import Database

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def solved():
    return config.load_profiles()["solved"]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "posting.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
                  "tags_json, duration_s, published_at, first_seen_at, last_seen_at) VALUES "
                  "('solved','vid00000001','campaign','UC','Solved','Why Values Matter','','[]',3000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    return database


_clip_numbers = iter(range(1, 10_000))


def add_clip(db, *, hook="Values are a compass, not a rulebook", text="a long talk about values and purpose",
             state="approved", campaign_rules=None):
    clip_no = next(_clip_numbers)
    grade = {"caption_text": text, "ad_read": 0.02}
    if campaign_rules is not None:
        grade["campaign"] = campaign_rules
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', ?, 'vid00000001', 100, 130, 80, ?, ?, '{\"facts\": {\"duration_s\": 30}}', ?, ?, 'r', ?)",
                  (clip_no, hook, f"/tmp/{clip_no}.mp4", json.dumps(grade), state, store.iso()))
        return c.execute("SELECT id FROM clips_ready ORDER BY id DESC LIMIT 1").fetchone()["id"]


def audited(db):
    """Both platforms have passed their audit for the channel, so the API may post for real."""
    for platform in ("youtube_shorts", "instagram_reels"):
        accounts.set_audit(db, "solved", platform, True)


def api_profile(solved, **posting_changes):
    changes = {"dry_run": False, "platforms": {"youtube_shorts": "api", "instagram_reels": "api"}, **posting_changes}
    return solved.model_copy(update={"posting": solved.posting.model_copy(update=changes)})


# --- copy and routing -------------------------------------------------------------------------------

def test_youtube_copy_fits_the_limits_and_carries_the_campaign_tag(solved):
    copy = posting.build_copy(solved.posting, {"hook": "x" * 300}, "Why Values Matter", "youtube_shorts")
    assert len(copy["title"]) <= posting.YT_TITLE_MAX and copy["title"].endswith("#Shorts")
    assert "@solvedclips @iammarkmanson" in copy["caption"]
    assert posting.copy_problems(copy, "youtube_shorts", solved.posting.tags["youtube_shorts"]) == []


def test_instagram_copy_leads_with_the_hook_and_tags_the_show(solved):
    copy = posting.build_copy(solved.posting, {"hook": "Stop waiting to be chosen"}, "Ep", "instagram_reels")
    assert copy["caption"].startswith("Stop waiting to be chosen")
    assert "@solvedpodcast @markmanson" in copy["caption"]


def test_a_clip_with_no_hook_uses_the_episode_name_not_an_invented_line(solved):
    copy = posting.build_copy(solved.posting, {"hook": None}, "Why Values Matter", "youtube_shorts")
    assert copy["title"].startswith("Why Values Matter")


def test_a_caption_missing_the_campaign_tag_is_blocked(solved):
    copy = {"title": "t", "caption": "no tags here", "tags": []}
    problems = posting.copy_problems(copy, "instagram_reels", solved.posting.tags["instagram_reels"])
    assert any("missing the campaign's tags" in p for p in problems)


def test_a_too_long_youtube_title_is_blocked(solved):
    copy = {"title": "x" * 101, "caption": "@solvedclips @iammarkmanson", "tags": []}
    assert any("title is too long" in p for p in posting.copy_problems(copy, "youtube_shorts", "@solvedclips"))


def test_a_clip_goes_to_the_platform_whose_topic_words_it_uses(solved):
    assert posting.pick_platform(solved.posting, "a talk about dating and commitment") == "instagram_reels"
    assert posting.pick_platform(solved.posting, "a talk about purpose and growth") == "youtube_shorts"
    assert posting.pick_platform(solved.posting, "nothing matching at all") == "youtube_shorts"


# --- spacing ----------------------------------------------------------------------------------------

def test_posts_are_spaced_and_capped_per_day(solved, db):
    rules = solved.posting  # min gap 180 minutes, cap 2 a day
    first = posting.plan(db, solved, add_clip(db), now=NOW)
    second = posting.plan(db, solved, add_clip(db), now=NOW)
    third = posting.plan(db, solved, add_clip(db), now=NOW)
    assert first["platform"] == "youtube_shorts"
    assert store.parse_time(second["scheduled_at"]) == store.parse_time(first["scheduled_at"]) + \
        timedelta(minutes=rules.min_gap_minutes)
    # the third falls on the next day because the first day already holds two YouTube posts
    assert third["scheduled_at"][:10] > first["scheduled_at"][:10]


# --- planning ---------------------------------------------------------------------------------------

def test_only_approved_clips_are_planned(solved, db):
    clip_id = add_clip(db, state="review")
    with pytest.raises(ValueError, match="only approved"):
        posting.plan(db, solved, clip_id, now=NOW)


def test_a_clip_is_planned_once(solved, db):
    clip_id = add_clip(db)
    assert posting.plan(db, solved, clip_id, now=NOW)["status"] == "queued"
    assert posting.plan(db, solved, clip_id, now=NOW) is None


def test_a_clip_that_failed_the_campaign_rules_is_blocked_not_queued(solved, db):
    clip_id = add_clip(db, campaign_rules={"ok": False, "problems": ["no clear face in the first 5 s"]})
    post = posting.plan(db, solved, clip_id, now=NOW)
    assert post["status"] == "blocked"
    with db.conn() as c:
        error = c.execute("SELECT error FROM posts WHERE id=?", (post["id"],)).fetchone()["error"]
    assert "campaign rules are not met" in error


def test_a_channel_without_posting_settings_plans_nothing(solved, db):
    clip_id = add_clip(db)
    assert posting.plan(db, solved.model_copy(update={"posting": None}), clip_id, now=NOW) is None


# --- the dry run and manual uploads -----------------------------------------------------------------

def test_a_dry_run_writes_the_payload_and_sends_nothing(solved, db, tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "OUTBOX_ROOT", tmp_path / "outbox")
    dry = api_profile(solved, dry_run=True)
    clip_id = add_clip(db)
    post = posting.plan(db, dry, clip_id, now=NOW)
    assert post["route"] == "api"
    lines = posting.run_due(db, dry, now=datetime(2030, 1, 1, tzinfo=timezone.utc), adapters={})
    assert lines == [f"post {post['id']} (youtube_shorts): dry_run"]
    payload = json.loads((tmp_path / "outbox" / "solved" / "posts" / f"post-{post['id']}.json").read_text(encoding="utf-8"))
    assert payload["note"].startswith("dry run") and "@solvedclips @iammarkmanson" in payload["caption"]


def test_manual_posts_wait_on_the_manual_page(solved, db):
    clip_id = add_clip(db)
    post = posting.plan(db, solved, clip_id, now=NOW)  # manual route by default
    lines = posting.run_due(db, solved, now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert lines == [f"post {post['id']} (youtube_shorts): manual_ready"]


def test_marking_a_manual_post_done_records_the_link(solved, db):
    post = posting.plan(db, solved, add_clip(db), now=NOW)
    posting.mark_posted(db, "solved", post["id"], "https://youtube.com/shorts/abc")
    with db.conn() as c:
        row = c.execute("SELECT status, url FROM posts WHERE id=?", (post["id"],)).fetchone()
    assert (row["status"], row["url"]) == ("posted", "https://youtube.com/shorts/abc")


def test_a_manual_post_needs_a_real_link_and_a_waiting_post(solved, db):
    post = posting.plan(db, solved, add_clip(db), now=NOW)
    with pytest.raises(ValueError):
        posting.mark_posted(db, "solved", post["id"], "not a link")
    posting.mark_posted(db, "solved", post["id"], "https://youtube.com/shorts/abc")
    with pytest.raises(LookupError):
        posting.mark_posted(db, "solved", post["id"], "https://youtube.com/shorts/def")


# --- sending through the platform APIs (fakes only) -------------------------------------------------

class FakeYouTube:
    def __init__(self, *, locked=False, fail=None):
        self.locked, self.fail, self.calls = locked, fail, []

    def publish(self, request):
        self.calls.append(request)
        if self.fail:
            raise self.fail
        return SimpleNamespace(video_id="abc123", actual_privacy="private", warnings=[], locked_private=self.locked)


def test_an_api_post_records_the_link_and_notes_a_privacy_lock(solved, db):
    audited(db)
    clip_id = add_clip(db)
    post = posting.plan(db, api_profile(solved), clip_id, now=NOW)
    fake = FakeYouTube(locked=True)
    adapters = {"youtube_shorts": posting.YouTubeShorts(fake)}
    lines = posting.run_due(db, api_profile(solved), now=datetime(2030, 1, 1, tzinfo=timezone.utc), adapters=adapters)
    assert lines == [f"post {post['id']} (youtube_shorts): uploaded"]
    assert fake.calls[0].privacy == "private" and fake.calls[0].title.endswith("#Shorts")
    with db.conn() as c:
        row = c.execute("SELECT status, url FROM posts WHERE id=?", (post["id"],)).fetchone()
    assert row["url"] == "https://youtube.com/shorts/abc123"


def test_a_refused_upload_is_marked_failed_with_the_reason(solved, db):
    audited(db)
    clip_id = add_clip(db)
    post = posting.plan(db, api_profile(solved), clip_id, now=NOW)
    adapters = {"youtube_shorts": posting.YouTubeShorts(FakeYouTube(fail=RuntimeError("quota exceeded")))}
    lines = posting.run_due(db, api_profile(solved), now=datetime(2030, 1, 1, tzinfo=timezone.utc), adapters=adapters)
    assert lines == [f"post {post['id']} (youtube_shorts): failed, YouTube refused the upload: quota exceeded"]


def test_a_post_with_no_connection_fails_clearly(solved, db):
    audited(db)
    clip_id = add_clip(db)
    posting.plan(db, api_profile(solved), clip_id, now=NOW)
    lines = posting.run_due(db, api_profile(solved), now=datetime(2030, 1, 1, tzinfo=timezone.utc), adapters={})
    assert "no youtube_shorts connection is set up" in lines[0]


def graph_transport(seen, *, status="FINISHED", refuse_publish=False):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("authorization")))
        if request.method == "POST" and request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "container1"})
        if request.method == "GET" and request.url.path.endswith("/container1"):
            return httpx.Response(200, json={"status_code": status})
        if request.method == "POST" and request.url.path.endswith("/media_publish"):
            if refuse_publish:
                return httpx.Response(400, json={"error": {"message": "Media not ready"}})
            return httpx.Response(200, json={"id": "media9"})
        return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/XYZ/"})
    return httpx.MockTransport(handler)


def test_instagram_publishes_through_the_graph_api_with_the_token_in_a_header():
    seen = []
    client = httpx.Client(transport=graph_transport(seen))
    adapter = posting.InstagramReels("secret-token", "igu1", http=client, sleep=lambda s: None)
    result = adapter.post(video_url="https://files.example.com/solved/1.mp4", copy={"caption": "hi"})
    assert result["url"] == "https://www.instagram.com/reel/XYZ/"
    assert [m for m, _, _ in seen] == ["POST", "GET", "POST", "GET"]
    assert all(auth == "Bearer secret-token" for _, _, auth in seen)


def test_instagram_needs_a_public_link_before_it_will_try():
    adapter = posting.InstagramReels("t", "igu1", http=httpx.Client(transport=graph_transport([])), sleep=lambda s: None)
    with pytest.raises(posting.PostError, match="public https link"):
        adapter.post(video_url=None, copy={"caption": "hi"})


def test_instagram_reports_a_processing_failure():
    adapter = posting.InstagramReels("t", "igu1", http=httpx.Client(transport=graph_transport([], status="ERROR")),
                                     sleep=lambda s: None)
    with pytest.raises(posting.PostError, match="could not process"):
        adapter.post(video_url="https://files.example.com/x.mp4", copy={"caption": "hi"})


def test_instagram_reports_an_error_from_the_publish_step():
    adapter = posting.InstagramReels("t", "igu1", http=httpx.Client(transport=graph_transport([], refuse_publish=True)),
                                     sleep=lambda s: None)
    with pytest.raises(posting.PostError, match="Media not ready"):
        adapter.post(video_url="https://files.example.com/x.mp4", copy={"caption": "hi"})


def test_the_solved_channel_plans_both_platforms_as_manual_until_their_routes_change(solved):
    assert solved.posting.platforms == {"youtube_shorts": "manual", "instagram_reels": "manual"}
    assert solved.posting.dry_run is True


# --- the explicit go for one post, and connecting YouTube -------------------------------------------

def test_send_now_sends_one_post_whatever_its_route_and_records_the_link(solved, db):
    post = posting.plan(db, solved, add_clip(db), now=NOW)  # manual route, dry-run channel
    fake = FakeYouTube()
    line = posting.send_now(db, solved, post["id"], {"youtube_shorts": posting.YouTubeShorts(fake)})
    assert line == f"post {post['id']} (youtube_shorts): uploaded, https://youtube.com/shorts/abc123"
    assert fake.calls[0].privacy == "private"  # the channel's privacy setting, not public
    with db.conn() as c:
        row = c.execute("SELECT route, status, url FROM posts WHERE id=?", (post["id"],)).fetchone()
    assert (row["route"], row["status"]) == ("api", "uploaded")


def test_send_now_never_sends_a_post_twice(solved, db):
    post = posting.plan(db, solved, add_clip(db), now=NOW)
    adapters = {"youtube_shorts": posting.YouTubeShorts(FakeYouTube())}
    posting.send_now(db, solved, post["id"], adapters)
    with pytest.raises(ValueError, match="already out"):
        posting.send_now(db, solved, post["id"], adapters)


def test_send_now_refuses_a_blocked_post(solved, db):
    bad = add_clip(db, campaign_rules={"ok": False, "problems": ["no clear face"]})
    post = posting.plan(db, solved, bad, now=NOW)
    with pytest.raises(ValueError, match="blocked"):
        posting.send_now(db, solved, post["id"], {"youtube_shorts": posting.YouTubeShorts(FakeYouTube())})


def test_a_failed_send_now_can_be_tried_again(solved, db):
    post = posting.plan(db, solved, add_clip(db), now=NOW)
    broken = {"youtube_shorts": posting.YouTubeShorts(FakeYouTube(fail=RuntimeError("quota exceeded")))}
    assert "failed" in posting.send_now(db, solved, post["id"], broken)
    assert "uploaded" in posting.send_now(db, solved, post["id"], {"youtube_shorts": posting.YouTubeShorts(FakeYouTube())})


def test_connecting_youtube_explains_what_is_missing(solved, tmp_path, monkeypatch):
    monkeypatch.setattr(posting, "YOUTUBE_CLIENT", tmp_path / "missing.json")
    with pytest.raises(posting.PostError, match="save your Google OAuth client file"):
        posting.connect_youtube(solved)
