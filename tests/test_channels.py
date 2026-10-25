"""Channels: stored in the database, one per category, and the hold-then-release behaviour for clips with no channel."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from villomillo import channels, config, routing, store, tasks
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def switch_off(database):
    """These tests are about each category's own channel; the "every clip to one channel" switch is on by default."""
    with database.conn() as c:
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("catch_all", '{"on": false}'))
    return database


@pytest.fixture
def db(tmp_path):
    return switch_off(Database(tmp_path / "channels.db"))


@pytest.fixture
def profiles(db):
    return channels.load(db)


def clip(db, category: str | None, *, state="approved", score=85, owner="solved") -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at, category) "
                  "VALUES (?, (SELECT COALESCE(MAX(engine_clip_id), 0) + 1 FROM clips_ready), 'vid00000001', 0, 30, ?, 'a hook', "
                  "'f.mp4', '{}', ?, ?, 'r', ?, ?)",
                  (owner, score, json.dumps({"caption_text": "a clear point about hard work"}), state, store.iso(), category))
        return c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]


def no_ai():
    raise AssertionError("one channel for a category needs no AI choice")


# ---- storage ---------------------------------------------------------------------------------------------------


def test_a_new_database_starts_with_the_podcasts_channel(profiles):
    assert list(profiles) == ["solved"]
    assert profiles["solved"].category == "Podcasts" and profiles["solved"].display_name == "Podcasts"


def test_channels_are_read_from_the_database_after_the_first_start(db, tmp_path):
    channels.load(db)
    channels.create(db, channels.load(db), name="Outdoors", category="Outdoors", about="Hiking and camping")
    again = channels.load(db)
    assert set(again) == {"solved", "outdoors"}  # the starting files are not read a second time


def test_deleting_the_starting_channel_does_not_bring_it_back(db):
    profiles = channels.load(db)
    channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    channels.delete(db, profiles, "solved")
    assert set(channels.load(db)) == {"outdoors"}


def test_one_channel_per_category_ignoring_case(db, profiles):
    with pytest.raises(channels.ChannelError, match="one channel per category"):
        channels.create(db, profiles, name="More pods", category="podcasts", about="x")
    made = channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    with pytest.raises(channels.ChannelError, match="one channel per category"):
        channels.update(db, profiles, made.id, category=" PODCASTS ")


def test_names_become_unique_ids_and_a_new_channel_starts_as_a_safe_dry_run(db, profiles):
    a = channels.create(db, profiles, name="Stream clips", category="Stream clips", about="Gaming highlights")
    b = channels.create(db, profiles, name="Stream clips", category="Streams", about="More gaming")
    assert (a.id, b.id) == ("stream-clips", "stream-clips-2")
    assert a.posting.dry_run and a.posting.privacy == "public" and a.posting.daily_cap == 100
    assert list(a.posting.platforms) == ["youtube_shorts"]


def test_bad_input_is_refused_with_a_plain_message(db, profiles):
    for bad in ({"name": "", "category": "x", "about": "y"}, {"name": "x", "category": "", "about": "y"},
                {"name": "x", "category": "y", "about": ""}, {"name": "!", "category": "!!", "about": "y"}):
        with pytest.raises(channels.ChannelError):
            channels.create(db, profiles, **bad)
    with pytest.raises(channels.ChannelError, match="between 1 and 100"):
        channels.update(db, profiles, "solved", youtube_daily_cap="101")
    with pytest.raises(channels.ChannelError, match="no look called"):
        channels.update(db, profiles, "solved", format="nonsense")


def test_edits_are_saved_and_the_live_set_follows(db, profiles):
    channels.update(db, profiles, "solved", brand_handle="@pods", youtube_tags="@a @b", youtube_daily_cap="50")
    saved = channels.load(db)["solved"]
    assert saved.brand.handle == "@pods" and saved.posting.tags["youtube_shorts"] == "@a @b"
    assert saved.posting.daily_cap == 50 and profiles["solved"].posting.daily_cap == 50


def test_a_channel_with_clips_cannot_be_deleted_and_the_last_one_stays(db, profiles):
    made = channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    clip(db, "Outdoors", owner=made.id)
    with pytest.raises(channels.ChannelError, match="already has clips"):
        channels.delete(db, profiles, made.id)
    channels.delete(db, profiles, "solved")
    with pytest.raises(channels.ChannelError, match="at least one"):
        channels.delete(db, profiles, made.id)


# ---- hold, then release ----------------------------------------------------------------------------------------


def test_a_clip_with_no_channel_for_its_category_is_held_and_named(db, profiles):
    held = clip(db, "Outdoors")
    assert routing.assign_and_plan(db, no_ai, profiles, "solved", held, now=NOW) is None
    with db.conn() as c:
        assert c.execute("SELECT reason FROM clips_ready WHERE id=?", (held,)).fetchone()["reason"] == \
            "approved, but not posted: no channel for the category Outdoors"
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0
    assert routing.held_categories(db) == [{"category": "Outdoors", "clips": 1, "low": 85, "high": 85}]


def test_a_clip_only_posts_on_the_channel_for_its_category(db, profiles):
    channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    pod, out = clip(db, "Podcasts"), clip(db, "outdoors")
    assert routing.assign_and_plan(db, no_ai, profiles, "solved", pod, now=NOW)["profile"] == "solved"
    assert routing.assign_and_plan(db, no_ai, profiles, "solved", out, now=NOW)["profile"] == "outdoors"


def test_adding_the_channel_releases_the_held_clips_best_first(db, profiles):
    low, high = clip(db, "Outdoors", score=70), clip(db, "Outdoors", score=90)
    other = clip(db, "Cooking")
    for c in (low, high, other):
        routing.assign_and_plan(db, no_ai, profiles, "solved", c, now=NOW)
    channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    planned = routing.release_held(db, no_ai, profiles, "Outdoors", now=NOW)
    assert [p["profile"] for p in planned] == ["outdoors", "outdoors"]
    with db.conn() as c:
        order = [r["clip_id"] for r in c.execute("SELECT clip_id FROM posts ORDER BY id")]
        assert order == [high, low]
        assert c.execute("SELECT reason FROM clips_ready WHERE id=?", (high,)).fetchone()["reason"] == "approved; planned on Outdoors"
    assert routing.held_categories(db) == [{"category": "Cooking", "clips": 1, "low": 85, "high": 85}]
    assert routing.release_held(db, no_ai, profiles, "Outdoors", now=NOW) == []  # nothing is planned twice


def test_a_clip_without_a_category_still_goes_to_any_channel(db, profiles):
    assert routing.assign_and_plan(db, no_ai, profiles, "solved", clip(db, None), now=NOW)["profile"] == "solved"


def test_a_database_from_before_channels_gains_the_category_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    Database(path)
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE clips_ready DROP COLUMN category")
    raw.commit()
    raw.close()
    Database(path)
    with Database(path).conn() as c:
        assert "category" in {r["name"] for r in c.execute("PRAGMA table_info(clips_ready)")}


# ---- the dashboard ---------------------------------------------------------------------------------------------


@pytest.fixture
def web(db, profiles):
    sv = Services(config.AppConfig(), profiles, db, Quota(db, general_cap=9, search_cap=9))
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_the_channels_pages_render(web):
    client, _ = web
    for path in ("/channels", "/channels/new", "/channels/solved", "/channels/new?category=Outdoors"):
        assert client.get(path).status_code == 200, path
    assert "Podcasts" in client.get("/channels").text


def test_add_channel_creates_it_releases_held_clips_and_schedules_its_jobs(web):
    client, sv = web
    held = clip(sv.db, "Outdoors")
    routing.assign_and_plan(sv.db, no_ai, sv.profiles, "solved", held, now=NOW)
    home = client.get("/?profile=solved").text
    assert 'Create a channel for &#34;Outdoors&#34;' in home or 'Create a channel for "Outdoors"' in home
    r = client.post("/channels/new", data={"name": "Outdoors", "category": "Outdoors", "about": "Hiking"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/channels/outdoors?")
    assert "released=1" in r.headers["location"] and "outdoors" in sv.profiles
    with sv.db.conn() as c:
        assert c.execute("SELECT profile_id FROM posts WHERE clip_id=?", (held,)).fetchone()["profile_id"] == "outdoors"
        assert c.execute("SELECT 1 FROM schedules WHERE name='post:outdoors'").fetchone()
        assert c.execute("SELECT status FROM posts").fetchone()["status"] == "queued"  # no required tags is fine
        assert c.execute("SELECT 1 FROM tasks WHERE kind='copy'").fetchone()
    assert "Create a channel for" not in client.get("/?profile=solved").text


def test_add_channel_shows_a_plain_error_for_a_taken_category(web):
    client, _ = web
    r = client.post("/channels/new", data={"name": "Again", "category": "podcasts", "about": "x"})
    assert r.status_code == 422 and "one channel per category" in r.text


def test_saving_a_channel_changes_it_and_a_bad_value_changes_nothing(web):
    client, sv = web
    base = {"name": "Podcasts", "category": "Podcasts", "about": "Talk shows",
            "brand_handle": "@pods", "youtube_tags": "@x", "youtube_daily_cap": "40", "format": ""}
    assert client.post("/channels/solved/save", data=base, follow_redirects=False).status_code == 303
    assert sv.profiles["solved"].brand.handle == "@pods" and sv.profiles["solved"].posting.daily_cap == 40
    r = client.post("/channels/solved/save", data={**base, "youtube_daily_cap": "500"})
    assert r.status_code == 422 and "between 1 and 100" in r.text
    assert sv.profiles["solved"].posting.daily_cap == 40


def test_requests_from_other_sites_are_refused(web):
    client, _ = web
    r = client.post("/channels/new", data={"name": "x", "category": "y", "about": "z"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_deleting_a_channel_over_the_api(web):
    client, sv = web
    channels.create(sv.db, sv.profiles, name="Outdoors", category="Outdoors", about="Hiking")
    assert client.post("/api/channel-delete", data={"profile": "outdoors"}).status_code == 200
    assert "outdoors" not in sv.profiles
    assert client.post("/api/channel-delete", data={"profile": "solved"}).status_code == 422  # the last one stays


def test_connecting_youtube_explains_a_missing_client_file(web, monkeypatch, tmp_path):
    from villomillo import posting

    client, _ = web
    monkeypatch.setattr(posting, "YOUTUBE_CLIENT", tmp_path / "missing.json")
    r = client.post("/api/channel-youtube", data={"profile": "solved"})
    assert r.status_code == 422 and "OAuth client file" in r.text


def test_the_post_job_is_scheduled_for_every_channel(db, profiles):
    channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    tasks.ensure_schedules(db, config.AppConfig(), profiles, at=NOW)
    with db.conn() as c:
        assert {r["name"] for r in c.execute("SELECT name FROM schedules")} == {"post:solved", "post:outdoors", "stats:solved", "stats:outdoors", "campaigns", "research", "usage"}


def test_a_channel_saved_while_tiktok_was_part_of_the_app_still_loads_without_it(db):
    profiles = channels.load(db)
    old = profiles["solved"].model_dump()
    old["tiktok_handle"] = "@podscliphouse"
    old["posting"]["platforms"]["tiktok"] = "manual"
    old["posting"]["platform_caps"] = {"tiktok": 15, "youtube_shorts": 60}
    old["posting"]["tags"]["tiktok"] = "@x"
    with db.conn() as c:
        c.execute("UPDATE channels SET data_json=? WHERE id='solved'", (json.dumps(old),))
    again = channels.load(db)["solved"]
    assert list(again.posting.platforms) == ["youtube_shorts"] and again.posting.daily_cap == 60
    assert "tiktok" not in again.model_dump_json().lower()


def test_a_channel_saved_with_campaign_rules_still_loads_now_that_campaigns_hold_them(db):
    profiles = channels.load(db)
    old = profiles["solved"].model_dump()
    old["campaign"] = {"name": "Clipfarm x Solved", "min_s": 20, "max_s": 60, "face_window_s": 5,
                       "require_captions": True, "no_ad_reads": True}
    with db.conn() as c:
        c.execute("UPDATE channels SET data_json=? WHERE id='solved'", (json.dumps(old),))
    assert "campaign" not in channels.load(db)["solved"].model_dump()


# ---- the "every clip to one channel" switch -----------------------------------------------------------------------


def test_the_switch_is_on_by_default_and_points_at_the_starting_channel(tmp_path):
    fresh = Database(tmp_path / "fresh.db")
    profs = channels.load(fresh)
    assert channels.catch_all(fresh, profs) == {"on": True, "channel": "solved"}
    assert channels.catch_all_target(fresh, profs).id == "solved"


def test_with_the_switch_on_a_clip_goes_to_the_one_channel_whatever_its_category(tmp_path):
    fresh = Database(tmp_path / "fresh.db")
    profs = channels.load(fresh)
    for category in ("Outdoors", "Gaming", None):
        decision = routing.route_clip(fresh, no_ai, profs, clip(fresh, category), now=NOW)
        assert decision.profile_id == "solved", category
    assert routing.held_categories(fresh) == []


def test_turning_the_switch_off_sends_clips_to_their_category_and_holds_the_rest(db, profiles):
    channels.set_catch_all(db, profiles, on=True)
    assert routing.route_clip(db, no_ai, profiles, clip(db, "Outdoors"), now=NOW).profile_id == "solved"
    channels.set_catch_all(db, profiles, on=False)
    assert channels.catch_all_target(db, profiles) is None
    assert routing.route_clip(db, no_ai, profiles, clip(db, "Podcasts"), now=NOW).profile_id == "solved"
    held = routing.route_clip(db, no_ai, profiles, clip(db, "Outdoors"), now=NOW)
    assert held.profile_id is None and routing.NO_CHANNEL in held.reason


def test_the_switch_follows_the_chosen_channel_and_refuses_an_unknown_one(db, profiles):
    channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    channels.set_catch_all(db, profiles, on=True, channel="outdoors")
    assert channels.catch_all_target(db, profiles).id == "outdoors"
    with pytest.raises(channels.ChannelError):
        channels.set_catch_all(db, profiles, on=True, channel="nope")
    channels.delete(db, profiles, "outdoors")
    assert channels.catch_all(db, profiles)["channel"] == "solved"  # the chosen channel is gone: back to the starting one


def test_turning_the_switch_on_releases_clips_held_for_a_missing_channel(web):
    client, sv = web
    held = clip(sv.db, "Outdoors")
    routing.assign_and_plan(sv.db, no_ai, sv.profiles, "solved", held, now=NOW)
    assert routing.held_categories(sv.db)[0]["category"] == "Outdoors"
    r = client.post("/channels/catch-all", data={"on": "1", "channel": "solved"}, follow_redirects=False)
    assert r.status_code == 303 and "released=1" in r.headers["location"]
    assert routing.held_categories(sv.db) == []
    page = client.get("/channels").text
    assert "Send every clip to one channel" in page and "gets every clip" in page
    assert client.post("/channels/catch-all", data={"on": "0", "channel": "solved"}, follow_redirects=False).status_code == 303
    assert channels.catch_all_target(sv.db, sv.profiles) is None
    assert client.post("/channels/catch-all", data={"on": "1", "channel": "nope"}).status_code == 400


def test_a_channel_saved_before_the_privacy_setting_existed_becomes_public_once(tmp_path):
    old = Database(tmp_path / "old.db")
    first = channels.load(old)
    assert first["solved"].posting.privacy == "public"  # the starting file now says public
    with old.conn() as c:  # what an older version stored: private, with no way to have chosen it
        data = json.loads(c.execute("SELECT data_json FROM channels WHERE id='solved'").fetchone()["data_json"])
        data["posting"]["privacy"] = "private"
        c.execute("UPDATE channels SET data_json=? WHERE id='solved'", (json.dumps(data),))
        c.execute("DELETE FROM meta WHERE key='privacy_public_v1'")
    assert channels.load(old)["solved"].posting.privacy == "public"
    profs = channels.load(old)
    channels.update(old, profs, "solved", privacy="unlisted")
    assert channels.load(old)["solved"].posting.privacy == "unlisted"  # a later choice is kept


def test_who_can_watch_is_changed_on_the_channel_page_and_a_bad_value_is_refused(web):
    client, sv = web
    form = {"name": "Podcasts", "category": "Podcasts", "about": "Talks", "brand_handle": "@cavemancam", "youtube_daily_cap": "100",
            "youtube_route": "api", "dry_run": "1", "privacy": "unlisted"}
    assert "Who can watch the videos" in client.get("/channels/solved?profile=solved").text
    assert client.post("/channels/solved/save", data=form, follow_redirects=False).status_code == 303
    assert sv.profiles["solved"].posting.privacy == "unlisted"
    with pytest.raises(channels.ChannelError):
        channels.update(sv.db, sv.profiles, "solved", privacy="everyone")
