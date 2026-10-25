"""Posts planned before a channel could post for real: Send waiting clips and Send now."""

from __future__ import annotations

import json
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
    p = p.model_copy(update={"posting": p.posting.model_copy(update={"platforms": {"youtube_shorts": route}, "dry_run": dry_run, "min_gap_minutes": 0, "max_gap_minutes": 0})})
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


# ---- the calendar: every post of a busy day, and a centred page -----------------------------------------------------


def _busy_day(db, n=5):
    clips = [scored(db, 70 + i) for i in range(n)]
    with db.conn() as c:
        for i, clip_id in enumerate(clips):
            c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at) "
                      "VALUES ('solved', ?, 'youtube_shorts', 'api', 'queued', ?, 'c', '[]', ?, ?)",
                      (clip_id, f"Hook number {i}", store.iso(store.now().replace(hour=15, minute=i * 10)), store.iso(NOW)))
    from villomillo import timefmt

    return timefmt.to_et(store.iso(store.now().replace(hour=15, minute=0))).date()  # the Eastern Time day those posts fall on


def test_a_busy_day_shows_three_posts_and_a_button_that_lists_all_of_them(db):
    client, _ = web(db)
    day = _busy_day(db)
    page = client.get("/calendar?profile=solved").text
    assert "+2 more" in page and f"/calendar/day/{day}?profile=solved" in page and page.count("cal-chip ") >= 3
    panel = client.get(f"/calendar/day/{day}?profile=solved").text
    assert all(f"Hook number {i}" in panel for i in range(5)) and "5 posts" in panel
    assert client.get("/calendar/day/not-a-date?profile=solved").status_code == 404


def test_a_post_opened_from_the_day_list_has_a_way_back(db):
    client, _ = web(db)
    _busy_day(db, 1)
    with db.conn() as c:
        post_id = c.execute("SELECT MAX(id) AS id FROM posts").fetchone()["id"]
    back = "/calendar/day/2026-10-25?profile=solved"
    assert "Back to the day" in client.get(f"/calendar/post/{post_id}?profile=solved&back={back}").text
    assert "Back to the day" not in client.get(f"/calendar/post/{post_id}?profile=solved&back=https://elsewhere.example/").text
    assert "Back to the day" not in client.get(f"/calendar/post/{post_id}?profile=solved").text


def test_every_page_is_centred_in_the_space_beside_the_sidebar():
    import pathlib

    css = (pathlib.Path(__file__).resolve().parent.parent / "villomillo" / "web" / "static" / "app.css").read_text()
    rule = next(line for line in css.splitlines() if line.startswith(".content {"))
    assert "margin: 0 auto" in rule and "max-width" in rule


def test_a_post_that_has_not_gone_out_can_be_deleted_and_its_clip_can_be_posted_again(db):
    client, sv = web(db)
    one = post(db, "queued")
    with db.conn() as c:
        clip_id = c.execute("SELECT clip_id FROM posts WHERE id=?", (one,)).fetchone()["clip_id"]
        c.execute("INSERT INTO copy_options (post_id, title, caption, tags_json, stop_score, misleading, chosen, source, created_at) "
                  "VALUES (?, 't', 'c', '[]', 4, 0.0, 1, 'test', ?)", (one, store.iso()))
    assert "Delete post" in client.get(f"/calendar/post/{one}?profile=solved").text
    assert client.post("/api/post-delete", data={"profile": "solved", "post_id": one}).status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM posts WHERE id=?", (one,)).fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) AS n FROM copy_options WHERE post_id=?", (one,)).fetchone()["n"] == 0
        assert c.execute("SELECT state FROM clips_ready WHERE id=?", (clip_id,)).fetchone()["state"] == "approved"
    assert "Post clip" in client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert client.post("/api/post-delete", data={"profile": "solved", "post_id": one}).status_code == 404


def test_a_post_already_on_youtube_cannot_be_deleted(db):
    client, _ = web(db)
    out = post(db, "uploaded")
    assert "Delete post" not in client.get(f"/calendar/post/{out}?profile=solved").text
    r = client.post("/api/post-delete", data={"profile": "solved", "post_id": out})
    assert r.status_code == 422 and "YouTube Studio" in r.json()["detail"]
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM posts WHERE id=?", (out,)).fetchone()["n"] == 1


def test_the_day_list_shows_each_post_with_its_score_length_and_how_it_goes_out(db):
    client, _ = web(db)
    day = _busy_day(db, 2)
    with db.conn() as c:
        c.execute("UPDATE posts SET status='failed', error='YouTube refused the upload: quota' WHERE title='Hook number 1'")
    panel = client.get(f"/calendar/day/{day}?profile=solved").text
    assert "day-card" in panel and "Hook number 0" in panel and "score" in panel and "Automatic" in panel
    assert "<b>1</b> waiting" in panel and "<b>1</b> need attention" in panel and "quota" in panel


# ---- posting again a clip whose post already went out -------------------------------------------------------------


def _out_post(db):
    one = post(db, "uploaded")
    with db.conn() as c:
        c.execute("UPDATE posts SET url='https://youtube.com/shorts/old', remote_id='old', posted_at=? WHERE id=?", (store.iso(NOW), one))
        c.execute("INSERT INTO post_stats (post_id, taken_at, views, likes, comments, shares) VALUES (?, ?, 5, 1, 0, 0)", (one, store.iso(NOW)))
        clip_id = c.execute("SELECT clip_id FROM posts WHERE id=?", (one,)).fetchone()["clip_id"]
    return one, clip_id


def test_post_again_sends_a_clip_whose_post_already_went_out(db, monkeypatch):
    client, sv = web(db)
    monkeypatch.setattr(posting, "default_adapters", lambda profile: {"youtube_shorts": FakeYouTube()})
    from villomillo import copywriter

    monkeypatch.setattr(copywriter, "generate_with_keys", lambda *a, **k: "ok")
    one, clip_id = _out_post(db)
    panel = client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert "Post again" in panel and "/api/clip-repost" in panel and "Post clip" not in panel
    assert client.post("/api/clip-repost", data={"profile": "solved", "clip_id": clip_id}).status_code == 200
    row = status_of(db, one)
    assert row["status"] == "queued"
    with db.conn() as c:
        assert c.execute("SELECT url FROM posts WHERE id=?", (one,)).fetchone()["url"] is None
        assert c.execute("SELECT COUNT(*) AS n FROM post_stats WHERE post_id=?", (one,)).fetchone()["n"] == 0
    runner.run_one(sv)
    with db.conn() as c:
        done = c.execute("SELECT status, url, remote_id FROM posts WHERE id=?", (one,)).fetchone()
    assert (done["status"], done["url"], done["remote_id"]) == ("uploaded", "https://youtube.com/shorts/abc", "abc")


def test_post_again_is_only_for_a_post_that_went_out_and_says_why_when_the_channel_cannot_send(db):
    client, sv = web(db)
    waiting = post(db, "queued")
    with db.conn() as c:
        waiting_clip = c.execute("SELECT clip_id FROM posts WHERE id=?", (waiting,)).fetchone()["clip_id"]
    assert client.post("/api/clip-repost", data={"profile": "solved", "clip_id": waiting_clip}).status_code == 404
    assert "Post again" not in client.get(f"/clips/{waiting_clip}/panel?profile=solved").text
    one, clip_id = _out_post(db)
    sv.profiles["solved"] = sv.profiles["solved"].model_copy(update={"posting": sv.profiles["solved"].posting.model_copy(update={"dry_run": True})})
    r = client.post("/api/clip-repost", data={"profile": "solved", "clip_id": clip_id})
    assert r.status_code == 422 and "Dry run" in r.json()["detail"]
    assert status_of(db, one)["status"] == "uploaded"  # nothing changed


# ---- the random wait between posts ----------------------------------------------------------------------------------


def _with_gaps(db, lo, hi):
    profiles, p = channel(db)
    p = p.model_copy(update={"posting": p.posting.model_copy(update={"min_gap_minutes": lo, "max_gap_minutes": hi})})
    profiles["solved"] = p
    return profiles, p


def test_each_clips_wait_is_random_between_the_shortest_and_the_longest(db):
    import random
    from datetime import timedelta

    profiles, p = _with_gaps(db, 10, 20)
    post(db, "queued")  # the last post is at NOW
    gaps = []
    for seed in range(40):
        slot = posting.next_slot(db, p.posting, "solved", "youtube_shorts", NOW, random.Random(seed))
        gaps.append((slot - NOW) / timedelta(minutes=1))
    assert all(10 <= g <= 20 for g in gaps) and len({round(g, 3) for g in gaps}) > 30  # never the same twice
    fixed = _with_gaps(db, 15, None)[1]
    assert {round((posting.next_slot(db, fixed.posting, "solved", "youtube_shorts", NOW, random.Random(s)) - NOW) / timedelta(minutes=1), 3) for s in range(5)} == {15.0}


def test_posts_already_waiting_are_re_spaced_to_the_new_gap_in_their_order(db):
    from datetime import timedelta

    profiles, p = _with_gaps(db, 10, 20)
    ids = [post(db, "queued") for _ in range(4)]
    with db.conn() as c:  # planned hours apart under the old setting
        for i, post_id in enumerate(ids):
            c.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (store.iso(NOW + timedelta(hours=3 * (i + 1))), post_id))
    done = post(db, "uploaded")
    with db.conn() as c:
        c.execute("UPDATE posts SET posted_at=? WHERE id=?", (store.iso(NOW - timedelta(hours=1)), done))
    assert posting.respace_queued(db, p, now=NOW) == 4
    times = [store.parse_time(status_of(db, i)["scheduled_at"]) for i in ids]
    assert times == sorted(times) and times[0] >= NOW
    assert all(timedelta(minutes=10) <= b - a <= timedelta(minutes=20) for a, b in zip(times, times[1:]))
    assert times[-1] - NOW < timedelta(hours=1, minutes=20)  # hours became minutes
    assert status_of(db, done)["status"] == "uploaded"


def test_saved_channels_get_the_new_gap_once_and_their_waiting_posts_are_re_spaced_once(tmp_path):
    from villomillo import channels as ch

    old = Database(tmp_path / "old.db")
    first = ch.load(old)
    assert (first["solved"].posting.min_gap_minutes, first["solved"].posting.max_gap_minutes) == (10, 20)
    with old.conn() as c:  # what an older version stored: one fixed gap of three hours
        data = json.loads(c.execute("SELECT data_json FROM channels WHERE id='solved'").fetchone()["data_json"])
        data["posting"]["min_gap_minutes"], data["posting"]["max_gap_minutes"] = 180, None
        c.execute("UPDATE channels SET data_json=? WHERE id='solved'", (json.dumps(data),))
        c.execute("DELETE FROM meta WHERE key='gap_10_20_v1'")
    profs = ch.load(old)
    assert (profs["solved"].posting.min_gap_minutes, profs["solved"].posting.max_gap_minutes) == (10, 20)
    ch.update(old, profs, "solved", min_gap_minutes="30", max_gap_minutes="45")
    assert (ch.load(old)["solved"].posting.min_gap_minutes, ch.load(old)["solved"].posting.max_gap_minutes) == (30, 45)  # a later choice is kept
    with pytest.raises(ch.ChannelError):
        ch.update(old, profs, "solved", min_gap_minutes="50", max_gap_minutes="20")
    queued = [post(old, "queued") for _ in range(3)]
    with old.conn() as c:
        c.execute("UPDATE posts SET scheduled_at=?", (store.iso(NOW),))
    assert posting.respace_once(old, ch.load(old)) == 3
    assert posting.respace_once(old, ch.load(old)) == 0 and len(queued) == 3


def test_the_channel_page_saves_the_gap_and_the_score_and_respaces_the_queue(db):
    from datetime import timedelta

    client, sv = web(db)
    ids = [post(db, "queued") for _ in range(3)]
    with db.conn() as c:
        for i, post_id in enumerate(ids):
            c.execute("UPDATE posts SET scheduled_at=? WHERE id=?", (store.iso(store.now() + timedelta(hours=5 * (i + 1))), post_id))
    form = {"name": "Podcasts", "category": "Podcasts", "about": "Talks", "brand_handle": "@cavemancam", "youtube_daily_cap": "100",
            "youtube_route": "api", "dry_run": "0", "privacy": "public", "upload_size": "1080",
            "min_gap_minutes": "10", "max_gap_minutes": "20", "auto_post_min_score": "85"}
    assert "Score a clip needs to post by itself" in client.get("/channels/solved?profile=solved").text
    assert client.post("/channels/solved/save", data=form, follow_redirects=False).status_code == 303
    p = sv.profiles["solved"].posting
    assert (p.min_gap_minutes, p.max_gap_minutes, p.auto_post_min_score) == (10, 20, 85)
    last = store.parse_time(status_of(db, ids[-1])["scheduled_at"])
    assert last - store.now() < timedelta(hours=1)
    assert client.post("/channels/solved/save", data={**form, "min_gap_minutes": "30", "max_gap_minutes": "5"}, follow_redirects=False).status_code == 422
