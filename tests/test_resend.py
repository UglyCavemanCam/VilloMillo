"""Posts planned before a channel could post for real: Send waiting clips and Send now."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from villomillo import accounts, channels, config, posting, runner, store
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "resend.db")


def channel(db, *, route="api", dry_run=False, audit=True):
    profiles = channels.load(db)
    p = profiles["solved"]
    p = p.model_copy(update={"posting": p.posting.model_copy(update={"platforms": {"youtube_shorts": route}, "dry_run": dry_run, "min_gap_minutes": 0})})
    profiles["solved"] = p
    accounts.set_audit(db, "solved", "youtube_shorts", audit, "test")
    return profiles, p


def post(db, status, *, score=80, clip_state="approved") -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, "
                  "validation_json, grade_json, state, reason, created_at) VALUES ('solved', (SELECT COALESCE(MAX(engine_clip_id), 0) + 1 "
                  "FROM clips_ready), 'vid00000001', 0, 30, ?, 'a hook', 'f.mp4', '{}', '{}', ?, 'r', ?)", (score, clip_state, store.iso()))
        clip_id = c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at) "
                  "VALUES ('solved', ?, 'youtube_shorts', 'manual', ?, 't', 'c', '[]', ?, ?)", (clip_id, status, store.iso(NOW), store.iso(NOW)))
        return c.execute("SELECT MAX(id) AS id FROM posts").fetchone()["id"]


def status_of(db, post_id):
    with db.conn() as c:
        return c.execute("SELECT status, route, scheduled_at FROM posts WHERE id=?", (post_id,)).fetchone()


def test_waiting_posts_are_queued_best_clip_first_and_others_left_alone(db):
    profiles, p = channel(db)
    low, high = post(db, "manual_ready", score=60), post(db, "dry_run", score=90)
    out, queued, failed = post(db, "uploaded"), post(db, "queued"), post(db, "manual_ready", clip_state="removed")
    assert posting.resend_waiting(db, p, "youtube_shorts", now=NOW) == 2
    assert status_of(db, high)["status"] == status_of(db, low)["status"] == "queued" and status_of(db, high)["route"] == "api"
    assert status_of(db, high)["scheduled_at"] <= status_of(db, low)["scheduled_at"]
    assert [status_of(db, i)["status"] for i in (out, queued, failed)] == ["uploaded", "queued", "manual_ready"]


@pytest.mark.parametrize("kwargs,words", [({"route": "manual"}, "by hand"), ({"dry_run": True}, "Dry run"), ({"audit": False}, "audit")])
def test_nothing_is_queued_while_the_channel_cannot_send_for_real(db, kwargs, words):
    profiles, p = channel(db, **kwargs)
    waiting = post(db, "manual_ready")
    with pytest.raises(ValueError, match=words):
        posting.resend_waiting(db, p, "youtube_shorts", now=NOW)
    assert status_of(db, waiting)["status"] == "manual_ready"


def test_the_overview_counts_the_posts_that_need_sending(db):
    profiles, p = channel(db)
    post(db, "manual_ready"), post(db, "dry_run"), post(db, "uploaded")
    row = accounts.overview(db, profiles)[0]
    assert row["resendable"] == 2


class FakeYouTube:
    def post(self, *, path, copy, privacy):
        return {"url": "https://youtube.com/shorts/abc", "notes": [], "remote_id": "abc"}


def test_send_now_posts_one_clip_through_the_queue(db, tmp_path, monkeypatch):
    profiles, p = channel(db)
    monkeypatch.setattr(posting, "default_adapters", lambda profile: {"youtube_shorts": FakeYouTube()})
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    one = post(db, "manual_ready")
    assert client.post("/api/post-send-now", data={"profile": "solved", "post_id": one}).status_code == 200
    runner.run_one(sv)
    after = status_of(db, one)
    assert after["status"] == "uploaded" and after["route"] == "api"
    assert client.post("/api/post-send-now", data={"profile": "solved", "post_id": one}).status_code == 422  # already out
    assert client.post("/api/post-send-now", data={"profile": "solved", "post_id": 999}).status_code == 404


def test_send_now_and_send_waiting_explain_why_not_over_the_api(db):
    profiles, p = channel(db, dry_run=True)
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    one = post(db, "dry_run")
    r = client.post("/api/post-send-now", data={"profile": "solved", "post_id": one})
    assert r.status_code == 422 and "Dry run" in r.json()["detail"]
    r = client.post("/api/posts-send-waiting", data={"profile": "solved", "platform": "youtube_shorts"})
    assert r.status_code == 422 and "Dry run" in r.json()["detail"]


def test_the_buttons_show_on_the_channel_page_and_the_post_panel(db):
    profiles, p = channel(db)
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    one = post(db, "manual_ready")
    page = client.get("/channels/solved?profile=solved").text
    assert "Send 1 waiting clip" in page
    assert client.post("/api/posts-send-waiting", data={"profile": "solved", "platform": "youtube_shorts"}).json()["queued"] == 1
    assert status_of(db, one)["status"] == "queued"
    two = post(db, "manual_ready")
    panel = client.get(f"/calendar/post/{two}?profile=solved")
    assert panel.status_code == 200 and "Send now" in panel.text


# ---- the Clips page: sorting, filters and the Post clip button ------------------------------------------------------


def web(db):
    profiles, _ = channel(db)
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def scored(db, score, *, video="vid00000001", state="approved", seconds=30) -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, "
                  "validation_json, grade_json, state, reason, created_at) VALUES ('solved', (SELECT COALESCE(MAX(engine_clip_id), 0) + 1 "
                  "FROM clips_ready), ?, 0, ?, ?, ?, 'f.mp4', '{}', '{}', ?, 'r', ?)", (video, seconds, score, f"hook {score}", state, store.iso()))
        return c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]


def order(client, query=""):
    import re

    page = client.get("/clips?profile=solved" + query).text
    return re.findall(r'class="grade">(\d+)</span>', page)


def test_clips_show_the_highest_score_first_by_default_and_other_sorts_work(db):
    client, _ = web(db)
    for s, secs in ((72, 50), (91, 20), (85, 35)):
        scored(db, s, seconds=secs)
    assert order(client) == ["91", "85", "72"]
    assert order(client, "&sort=lowest") == ["72", "85", "91"]
    assert order(client, "&sort=newest") == ["85", "91", "72"]
    assert order(client, "&sort=longest") == ["72", "85", "91"]
    assert order(client, "&sort=nonsense") == ["91", "85", "72"]


def test_clips_can_be_filtered_by_score_video_and_post(db):
    client, _ = web(db)
    a = scored(db, 95)
    scored(db, 65, video="vid00000002")
    scored(db, 80)
    with db.conn() as c:
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at) "
                  "VALUES ('solved', ?, 'youtube_shorts', 'api', 'uploaded', 't', 'c', '[]', ?, ?)", (a, store.iso(NOW), store.iso(NOW)))
    assert order(client, "&min_score=80") == ["95", "80"]
    assert order(client, "&video=vid00000002") == ["65"]
    assert order(client, "&post=posted") == ["95"]
    assert order(client, "&post=none") == ["80", "65"]
    assert "No clips match these filters" in client.get("/clips?profile=solved&min_score=90&video=vid00000002").text
    assert "Showing 2 of 3" in client.get("/clips?profile=solved&min_score=70").text


def test_the_clip_panel_has_post_clip_for_an_approved_clip_with_a_waiting_post(db):
    client, _ = web(db)
    waiting = post(db, "manual_ready")
    with db.conn() as c:
        clip_id = c.execute("SELECT clip_id FROM posts WHERE id=?", (waiting,)).fetchone()["clip_id"]
    panel = client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert "Post clip" in panel and "/api/clip-post" in panel
    out = post(db, "uploaded")
    with db.conn() as c:
        done = c.execute("SELECT clip_id FROM posts WHERE id=?", (out,)).fetchone()["clip_id"]
    assert "Post clip" not in client.get(f"/clips/{done}/panel?profile=solved").text


def test_post_clip_writes_the_titles_first_when_they_are_missing(db, monkeypatch):
    profiles, p = channel(db)
    monkeypatch.setattr(posting, "default_adapters", lambda profile: {"youtube_shorts": FakeYouTube()})
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    wrote = []
    from villomillo import copywriter

    monkeypatch.setattr(copywriter, "generate_with_keys", lambda *a, **k: wrote.append(a[2]) or "2 options written")
    one = post(db, "manual_ready")
    posting_tasks = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    assert posting_tasks.post("/api/post-send-now", data={"profile": "solved", "post_id": one}).status_code == 200
    runner.run_one(sv)
    assert wrote == [one] and status_of(db, one)["status"] == "uploaded"


def test_post_clip_plans_an_approved_clip_that_has_no_post_then_sends_it(db, monkeypatch):
    client, sv = web(db)
    monkeypatch.setattr(posting, "default_adapters", lambda profile: {"youtube_shorts": FakeYouTube()})
    from villomillo import copywriter

    monkeypatch.setattr(copywriter, "generate_with_keys", lambda *a, **k: "2 options written")
    clip_id = scored(db, 99)  # approved, and no post was ever planned for it
    panel = client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert "Post clip" in panel and "/api/clip-post" in panel
    assert client.post("/api/clip-post", data={"profile": "solved", "clip_id": clip_id}).status_code == 200
    runner.run_one(sv)
    with db.conn() as c:
        row = c.execute("SELECT status, url FROM posts WHERE clip_id=?", (clip_id,)).fetchone()
    assert row["status"] == "uploaded" and row["url"].endswith("/abc")
    assert client.post("/api/clip-post", data={"profile": "solved", "clip_id": clip_id}).status_code == 422  # already out


def test_post_clip_says_why_when_no_channel_can_take_the_clip(db):
    client, sv = web(db)
    channels.set_catch_all(sv.db, sv.profiles, on=False)
    clip_id = scored(db, 90)
    with db.conn() as c:
        c.execute("UPDATE clips_ready SET category='Outdoors' WHERE id=?", (clip_id,))
    r = client.post("/api/clip-post", data={"profile": "solved", "clip_id": clip_id})
    assert r.status_code == 422 and "no channel" in r.json()["detail"]
    assert "Why: approved, but not posted: no channel" in client.get(f"/clips/{clip_id}/panel?profile=solved").text
    rejected = scored(db, 80, state="rejected")
    assert client.post("/api/clip-post", data={"profile": "solved", "clip_id": rejected}).status_code == 422


def test_a_run_stops_starting_new_posts_once_its_time_budget_is_spent(db, monkeypatch):
    profiles, p = channel(db)
    sent = []

    class Slow:
        def post(self, *, path, copy, privacy):
            sent.append(1)
            return {"url": "https://youtube.com/shorts/abc", "notes": [], "remote_id": "abc"}

    for _ in range(3):
        post(db, "queued")
    from types import SimpleNamespace

    ticks = iter([0, 0, 1000, 2000, 3000])  # the start, the first post's check, then well past the budget
    monkeypatch.setattr(posting, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    lines = posting.run_due(db, p, now=NOW, adapters={"youtube_shorts": Slow()}, budget_s=600)
    assert len(sent) == 1 and any("next run" in x for x in lines)
