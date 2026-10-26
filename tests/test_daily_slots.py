"""At most 10 Shorts a day over every channel together, sent at the busy times of day instead of minutes apart."""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from villomillo import accounts, channels, posting, store, timefmt
from villomillo.config import DEFAULT_POST_TIMES, POST_TIME_JITTER_MIN, SHORTS_PER_DAY
from villomillo.db import Database

NOW = datetime(2026, 10, 26, 6, 2, tzinfo=timezone.utc)  # 2:02 AM Eastern, Oct 26


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "slots.db")


def add_post(db, profile_id, status, when, *, posted_at=None) -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, "
                  "validation_json, grade_json, state, reason, created_at, format_applied) VALUES (?, (SELECT COALESCE(MAX(engine_clip_id), 0) + 1 "
                  "FROM clips_ready), 'vid00000001', 0, 30, 80, 'a hook', 'f.mp4', '{}', '{}', 'approved', 'r', ?, 1)", (profile_id, store.iso()))
        clip_id = c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, posted_at, created_at) "
                  "VALUES (?, ?, 'youtube_shorts', 'api', ?, 't', 'c', '[]', ?, ?, ?)",
                  (profile_id, clip_id, status, store.iso(when), store.iso(posted_at) if posted_at else None, store.iso(when)))
        return c.execute("SELECT MAX(id) AS id FROM posts").fetchone()["id"]


def times_of(db, status="queued"):
    with db.conn() as c:
        return [store.parse_time(r["scheduled_at"]) for r in c.execute("SELECT scheduled_at FROM posts WHERE status=? ORDER BY scheduled_at, id", (status,))]


def per_day(db) -> Counter:
    with db.conn() as c:
        rows = c.execute("SELECT COALESCE(posted_at, scheduled_at) AS t FROM posts WHERE status IN ('queued','uploaded','posted')").fetchall()
    return Counter(timefmt.local_date(r["t"]) for r in rows)


def test_new_and_saved_channels_get_the_ten_daily_times_and_a_limit_of_ten(db):
    profiles = channels.load(db)
    p = profiles["solved"].posting
    assert p.daily_cap == SHORTS_PER_DAY == 10 and p.post_times == DEFAULT_POST_TIMES
    made = channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    assert made.posting.daily_cap == 10 and made.posting.post_times == DEFAULT_POST_TIMES
    with pytest.raises(channels.ChannelError, match="between 1 and 10"):
        channels.update(db, profiles, "solved", youtube_daily_cap="11")


def test_slots_land_on_the_daily_times_in_eastern_time_and_never_less_than_an_hour_apart(db):
    p = channels.load(db)["solved"].posting
    rng = random.Random(1)
    got = []
    for _ in range(25):
        when = posting.next_slot(db, p, "solved", "youtube_shorts", NOW, rng)
        add_post(db, "solved", "queued", when)
        got.append(when)
    assert got == sorted(got)
    for when in got:  # a daily time plus 0 to 10 minutes
        local = timefmt.to_et(when)
        offsets = [(local.hour * 60 + local.minute) - (int(t[:2]) * 60 + int(t[3:])) for t in DEFAULT_POST_TIMES]
        assert any(0 <= o <= POST_TIME_JITTER_MIN for o in offsets), timefmt.full(when)
    assert all(b - a >= timedelta(minutes=50) for a, b in zip(got, got[1:]))
    assert max(per_day(db).values()) == 10 and len(per_day(db)) == 3  # 25 posts: 10, 10, 5
    assert timefmt.to_et(got[0]).hour == 8  # nothing is left of last night, so the first one is the morning slot


def test_posts_already_sent_today_count_toward_todays_ten(db):
    p = channels.load(db)["solved"].posting
    for i in range(2):  # two went out in the early hours of Oct 26
        add_post(db, "solved", "uploaded", NOW - timedelta(minutes=40 + i), posted_at=NOW - timedelta(minutes=40 + i))
    rng = random.Random(2)
    for _ in range(12):
        add_post(db, "solved", "queued", posting.next_slot(db, p, "solved", "youtube_shorts", NOW, rng))
    today = timefmt.local_date(NOW)
    assert per_day(db)[today] == 10 and per_day(db)[today + timedelta(days=1)] == 4


def test_the_limit_is_shared_by_every_channel(db):
    profiles = channels.load(db)
    other = channels.create(db, profiles, name="Outdoors", category="Outdoors", about="Hiking")
    rng = random.Random(3)
    for i in range(14):
        profile = profiles["solved"] if i % 2 == 0 else other
        add_post(db, profile.id, "queued", posting.next_slot(db, profile.posting, profile.id, "youtube_shorts", NOW, rng))
    assert max(per_day(db).values()) == 10 and sum(per_day(db).values()) == 14


def test_a_channel_without_fixed_times_still_cannot_pass_ten_a_day_over_all_channels(db):
    profiles = channels.load(db)
    p = profiles["solved"].posting.model_copy(update={"post_times": [], "min_gap_minutes": 10, "max_gap_minutes": 20, "daily_cap": 10})
    q = p.model_copy(update={"daily_cap": 10})
    for _ in range(8):  # eight from another channel already sit on Oct 26
        add_post(db, "elsewhere", "queued", NOW + timedelta(hours=2))
    rng = random.Random(4)
    for _ in range(5):
        add_post(db, "solved", "queued", posting.next_slot(db, q, "solved", "youtube_shorts", NOW, rng))
    assert max(per_day(db).values()) == 10


def test_a_summer_and_a_winter_day_both_use_eastern_clock_times(db):
    p = channels.load(db)["solved"].posting
    for now in (datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc), datetime(2026, 12, 1, 3, 0, tzinfo=timezone.utc)):
        when = posting.next_slot(db, p, "solved", "youtube_shorts", now, random.Random(0))
        assert timefmt.to_et(when).hour == 8 and when.hour in (12, 13)  # 8 AM Eastern is 12:00 UTC in summer, 13:00 in winter


def test_waiting_posts_are_re_timed_once_so_today_holds_ten_and_the_rest_move_on(db):
    profiles = channels.load(db)
    for i in range(2):
        add_post(db, "solved", "uploaded", NOW - timedelta(minutes=40 + i), posted_at=NOW - timedelta(minutes=40 + i))
    for i in range(12):  # all bunched up in the next two hours, the way the old 10 to 20 minute gap left them
        add_post(db, "solved", "queued", NOW + timedelta(minutes=10 * i))
    assert posting.respace_once(db, profiles) == 12
    today = timefmt.local_date(NOW)
    assert per_day(db)[today] == 10 and per_day(db)[today + timedelta(days=1)] == 4
    assert all(t >= NOW for t in times_of(db))
    assert posting.respace_once(db, profiles) == 0


def test_nothing_is_sent_once_ten_went_out_today_even_if_the_channel_would_allow_more(db):
    profiles = channels.load(db)
    p = profiles["solved"]
    p = p.model_copy(update={"posting": p.posting.model_copy(update={"dry_run": False, "daily_cap": 10, "platforms": {"youtube_shorts": "api"}})})
    accounts.set_audit(db, "solved", "youtube_shorts", True, "test")
    for i in range(10):
        add_post(db, "elsewhere", "uploaded", NOW - timedelta(hours=1, minutes=i), posted_at=NOW - timedelta(minutes=30 + i))
    waiting = add_post(db, "solved", "queued", NOW - timedelta(minutes=5))
    lines = posting.run_due(db, p, now=NOW, adapters={})
    assert any("across all channels" in line for line in lines)
    with db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE id=?", (waiting,)).fetchone()["status"] == "queued"
