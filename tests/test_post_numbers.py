"""A channel's daily limit, and the real numbers collected for its posts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
import solved_profile

from villomillo import accounts, posting, store
from villomillo.db import Database

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "numbers.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, tags_json, duration_s, "
                  "published_at, first_seen_at, last_seen_at) VALUES ('solved','vid00000001','paste','UC','Show','An Episode','','[]',3000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    return database


def channel(**changes):
    p = solved_profile.solved()
    return p.model_copy(update={"posting": p.posting.model_copy(update={"platforms": {"youtube_shorts": "api"}, "dry_run": False,
                                                                        "min_gap_minutes": 0, "daily_cap": 100, "tags": {}, **changes})})


def clip(db, n) -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, "
                  "grade_json, state, reason, created_at) VALUES ('solved', ?, 'vid00000001', 0, 30, 88, 'A hard truth', ?, '{}', ?, 'approved', 'r', ?)",
                  (n, f"/tmp/{n}.mp4", json.dumps({"caption_text": "words"}), store.iso()))
        return c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]


class FakeYouTube:
    def post(self, *, path, copy, privacy):
        return {"url": "https://youtube.com/shorts/aB3dEfGhIjK", "notes": [], "remote_id": "aB3dEfGhIjK"}


def test_a_due_post_is_sent_and_counted_for_today(db):
    p = channel()
    posting.plan_all(db, p, clip(db, 1), now=NOW)
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    later = NOW + timedelta(hours=1)
    posting.run_due(db, p, now=later, adapters={"youtube_shorts": FakeYouTube()})
    with db.conn() as c:
        row = c.execute("SELECT status, remote_id FROM posts").fetchone()
    assert (row["status"], row["remote_id"]) == ("uploaded", "aB3dEfGhIjK")
    assert posting.posted_today(db, "solved", "youtube_shorts", now=later) == 1
    assert posting.posted_today(db, "solved", "youtube_shorts", now=later + timedelta(days=2)) == 0


def test_the_daily_limit_holds_the_next_post_until_tomorrow(db):
    p = channel(daily_cap=1)
    posting.plan_all(db, p, clip(db, 1), now=NOW)
    posting.plan_all(db, p, clip(db, 2), now=NOW)
    accounts.set_audit(db, "solved", "youtube_shorts", True)
    later = NOW + timedelta(hours=1)
    posting.run_due(db, p, now=later, adapters={"youtube_shorts": FakeYouTube()})
    with db.conn() as c:
        assert sorted(r["status"] for r in c.execute("SELECT status FROM posts")) == ["queued", "uploaded"]
        slots = [r["scheduled_at"] for r in c.execute("SELECT scheduled_at FROM posts ORDER BY id")]
        assert store.parse_time(slots[1]) - store.parse_time(slots[0]) >= timedelta(hours=6)  # planned for the next day
        c.execute("UPDATE posts SET scheduled_at=? WHERE status='queued'", (store.iso(NOW),))  # even if it were due now
    again = posting.run_due(db, p, now=later, adapters={"youtube_shorts": FakeYouTube()})
    assert any("limit of 1" in line for line in again)
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM posts WHERE status='uploaded'").fetchone()["n"] == 1


class FakeYT:
    def videos(self, ids):
        return [{"video_id": i, "views": 4000, "likes": 100, "comments": 5} for i in ids]


def uploaded(db, p, n=1):
    posting.plan_all(db, p, clip(db, n), now=NOW)
    with db.conn() as c:
        c.execute("UPDATE posts SET status='uploaded', posted_at=? WHERE status='queued'", (store.iso(NOW),))
        c.execute("UPDATE posts SET url='https://youtube.com/shorts/aB3dEfGhIjK' WHERE url IS NULL")


def test_real_views_are_collected_for_posts_with_a_link_including_ones_made_by_hand(db):
    p = channel()
    uploaded(db, p)
    lines = posting.collect_stats(db, p, yt=FakeYT(), now=NOW + timedelta(hours=6))
    with db.conn() as c:
        row = c.execute("SELECT views, likes, comments FROM post_stats").fetchone()
    assert (row["views"], row["likes"], row["comments"]) == (4000, 100, 5) and len(lines) == 1
    assert posting.collect_stats(db, p, now=NOW) == []  # no YouTube key: nothing is read, nothing breaks


def test_a_sudden_fall_in_views_pauses_the_account_and_says_why(db):
    p = channel()
    for n in range(1, 11):
        posting.plan(db, p, clip(db, n), now=NOW)
    with db.conn() as c:
        ids = [r["id"] for r in c.execute("SELECT id FROM posts ORDER BY id")]
        c.execute("UPDATE posts SET status='uploaded', posted_at=?", (store.iso(NOW),))
        for i, post_id in enumerate(ids):
            c.execute("INSERT INTO post_stats (post_id, taken_at, views) VALUES (?,?,?)", (post_id, store.iso(NOW), 1000 if i < 5 else 50))

    class Quiet:
        def videos(self, ids):
            return []

    lines = posting.collect_stats(db, p, yt=Quiet(), now=NOW)
    assert accounts.is_paused(db, "solved", "youtube_shorts") and any("paused" in line for line in lines)


def test_insights_shows_the_real_numbers_that_were_collected(tmp_path):
    from fastapi.testclient import TestClient

    from villomillo import channels, config
    from villomillo.quota import Quota
    from villomillo.services import Services
    from villomillo.web.app import create_app

    database = Database(tmp_path / "web.db")
    sv = Services(config.AppConfig(), channels.load(database), database, Quota(database, general_cap=9, search_cap=9))
    with database.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', 1, 'v', 0, 30, 80, 'h', 'f', '{}', '{}', 'approved', 'r', ?)", (store.iso(),))
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at, url) "
                  "VALUES ('solved', 1, 'youtube_shorts', 'api', 'uploaded', 'A title', 'c', '[]', ?, ?, 'https://youtube.com/shorts/aB3dEfGhIjK')", (store.iso(), store.iso()))
        c.execute("INSERT INTO post_stats (post_id, taken_at, views, likes) VALUES (1, ?, 12345, 99)", (store.iso(),))
    page = TestClient(create_app(sv), base_url="http://127.0.0.1:8780").get("/insights?profile=solved").text
    assert "12,345" in page and "A title" in page
