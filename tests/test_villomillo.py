"""Tests for the VilloMillo research engine. Nothing here touches the network or a real key."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from villomillo import config, store, tasks
from villomillo.db import Database
from villomillo.jev import Jev, JevError
from villomillo.quota import GENERAL, SEARCH, Quota, QuotaExceeded
from villomillo.services import Services
from villomillo.web.app import create_app
from villomillo.youtube import YouTube, YouTubeError, normalize, parse_duration

NOW = store.now()
SECRET = "AIzaSECRET-KEY-VALUE-1234567890abcdefg"

def profile() -> config.Profile:
    return config.Profile(id="test", display_name="Test", niche={"definition": "podcasts"})


def vid(i: int, *, age_h: float = 10, views: int | None = 300_000, minutes: float = 150, title: str | None = None,
        live: str = "none", lang: str | None = "en", channel: str = "UCchan") -> dict:
    return {
        "video_id": f"vid{i:08d}", "channel_id": channel, "channel_title": "Chan", "title": title or f"Episode {i}",
        "description": "d", "tags": [], "duration_s": int(minutes * 60), "language": lang, "live_status": live,
        "published_at": (NOW - timedelta(hours=age_h)).isoformat(timespec="seconds"),
        "views": views, "likes": (views or 0) // 40, "comments": (views or 0) // 400,
    }


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


# ---- configuration ---------------------------------------------------------------------------------------------


def test_the_shipped_settings_load():
    assert "solved" in config.load_profiles()
    assert config.load_app().dashboard.host == "127.0.0.1"


def test_typos_and_public_hosts_are_errors():
    with pytest.raises(ValidationError):
        config.Profile(id="test", display_name="Test", niche={"definition": "x"}, research={"topics": ["x"]})
    with pytest.raises(ValidationError):
        config.Dashboard(host="0.0.0.0")


# ---- quota -----------------------------------------------------------------------------------------------------


def test_quota_counts_and_refuses_at_the_cap(db):
    q = Quota(db, general_cap=3, search_cap=1)
    assert [q.charge(GENERAL) for _ in range(3)] == [1, 2, 3]
    with pytest.raises(QuotaExceeded):
        q.charge(GENERAL)
    assert q.used(GENERAL) == 3  # the refused call was not counted
    q.charge(SEARCH)
    with pytest.raises(QuotaExceeded):
        q.charge(SEARCH)


def test_quota_day_rolls_over_at_pacific_midnight(db):
    q = Quota(db, general_cap=1, search_cap=1)
    before = datetime(2026, 10, 23, 6, 59, tzinfo=timezone.utc)  # 23:59 the day before in Pacific time
    after = datetime(2026, 10, 23, 7, 1, tzinfo=timezone.utc)
    q.charge(GENERAL, now=before)
    with pytest.raises(QuotaExceeded):
        q.charge(GENERAL, now=before)
    q.charge(GENERAL, now=after)  # a new day: allowed again


# ---- YouTube client --------------------------------------------------------------------------------------------


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(("text", "seconds"), [("PT2H43M42S", 9822), ("PT45S", 45), ("P1DT1H", 90000), ("", 0), (None, 0), ("junk", 0)])
def test_durations(text, seconds):
    assert parse_duration(text) == seconds


def test_normalize_keeps_hidden_counts_as_none():
    item = {"id": "abc", "snippet": {"title": "t", "publishedAt": "2026-01-01T00:00:00Z"}, "contentDetails": {"duration": "PT1M"},
            "statistics": {"viewCount": "5"}}
    v = normalize(item)
    assert (v["views"], v["likes"], v["comments"], v["duration_s"]) == (5, None, None, 60)


def test_search_uses_the_search_allowance_not_the_general_one(db):
    q = Quota(db, general_cap=10, search_cap=10)
    yt = YouTube(SECRET, q, client(lambda r: httpx.Response(200, json={"items": [{"id": {"videoId": "abcdefghijk"}}]})))
    assert yt.search("x", days=7) == ["abcdefghijk"]
    assert (q.used(SEARCH), q.used(GENERAL)) == (1, 0)


def test_google_saying_quota_is_gone_stops_us_for_the_day(db):
    q = Quota(db, general_cap=100, search_cap=10)
    body = {"error": {"errors": [{"reason": "quotaExceeded"}]}}
    yt = YouTube(SECRET, q, client(lambda r: httpx.Response(403, json=body)))
    with pytest.raises(QuotaExceeded):
        yt.videos(["abcdefghijk"])
    assert q.used(GENERAL) == 100


def test_the_api_key_never_appears_in_an_error(db):
    q = Quota(db, general_cap=100, search_cap=10)

    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    for handler in (boom, lambda r: httpx.Response(500, text=f"oops {r.url}")):
        with pytest.raises(YouTubeError) as err:
            YouTube(SECRET, q, client(handler)).videos(["abcdefghijk"])
        assert SECRET not in str(err.value)


def test_an_unknown_channel_is_a_clear_error(db):
    yt = YouTube(SECRET, Quota(db, general_cap=9, search_cap=9), client(lambda r: httpx.Response(200, json={})))
    with pytest.raises(YouTubeError, match="no YouTube channel"):
        yt.channel(handle="@nobody")


# ---- Jev client ------------------------------------------------------------------------------------------------


ANSWERS = {"fits_niche": {"type": "noul", "noul": 0.9}}
GOOD = {"model": "jev-1.14.0", "answers": ANSWERS, "usage": {"input_tokens": 1_000_000, "output_tokens": 9}}


def test_jev_parses_answers_and_prices_input_tokens_only():
    r = Jev("k", client=client(lambda req: httpx.Response(200, json=GOOD))).ask({"a": 1}, {"q": {}})
    assert r.model == "jev-1.14.0" and r.answers["fits_niche"]["noul"] == 0.9
    assert r.cost_usd == pytest.approx(0.042)


def test_jev_retries_busy_responses_then_succeeds():
    codes = iter([429, 529, 200])
    sleeps = []
    jev = Jev("k", client=client(lambda req: (lambda c: httpx.Response(c, json=GOOD))(next(codes))), sleep=sleeps.append)
    assert jev.ask({}, {}).model == "jev-1.14.0" and sleeps == [1, 2]


def test_jev_gives_up_and_never_leaks_the_key():
    jev = Jev(SECRET, client=client(lambda req: httpx.Response(401, json={"detail": "bad key"})), sleep=lambda s: None)
    with pytest.raises(JevError) as err:
        jev.ask({}, {})
    assert "401" in str(err.value) and SECRET not in str(err.value)
    busy = Jev(SECRET, retries=1, client=client(lambda req: httpx.Response(503)), sleep=lambda s: None)
    with pytest.raises(JevError):
        busy.ask({}, {})


# ---- store -----------------------------------------------------------------------------------------------------


def test_a_video_is_stored_once_and_updated_after(db):
    v = vid(1, age_h=5, views=100)
    with db.conn() as c:
        assert store.upsert_video(c, "p", v, "channel") is True
        assert store.upsert_video(c, "p", {**v, "views": 250}, "topic") is False
        row = c.execute("SELECT views, found_by FROM videos").fetchone()
    assert (row["views"], row["found_by"]) == (250, "channel")


def test_a_bad_video_id_is_refused(db):
    with db.conn() as c, pytest.raises(ValueError):
        store.upsert_video(c, "p", {**vid(1, age_h=1, views=1), "video_id": "../../etc"}, "channel")


# ---- task queue ------------------------------------------------------------------------------------------------


def test_a_task_added_twice_with_the_same_key_runs_once(db):
    assert tasks.enqueue(db, "watch", dedupe_key="k") is not None
    assert tasks.enqueue(db, "watch", dedupe_key="k") is None


def test_a_dead_workers_task_is_picked_up_again_after_its_lease(db):
    tasks.enqueue(db, "watch", run_after=NOW - timedelta(seconds=1))
    first = tasks.claim(db, "w1", lease_s=60, at=NOW)
    assert first and tasks.claim(db, "w2", at=NOW) is None  # still leased
    again = tasks.claim(db, "w2", at=NOW + timedelta(seconds=61))
    assert again and again["id"] == first["id"] and again["attempts"] == 2


def test_failures_retry_with_backoff_then_give_up(db):
    tasks.enqueue(db, "watch", run_after=NOW - timedelta(seconds=1))
    task = tasks.claim(db, "w", at=NOW)
    assert tasks.fail(db, task, "boom", at=NOW) == "queued"
    assert tasks.claim(db, "w", at=NOW) is None  # waiting out the backoff
    for day in range(1, 5):  # each retry happens a day later, well after any backoff
        at = NOW + timedelta(days=day)
        task = tasks.claim(db, "w", at=at)
        outcome = tasks.fail(db, task, "boom", at=at)
    assert outcome == "failed"


def test_schedules_queue_each_slot_once_even_if_ticked_twice(db):
    app, profiles = config.AppConfig(), {"test": profile()}
    tasks.ensure_schedules(db, app, profiles, at=NOW)
    assert tasks.tick_schedules(db, at=NOW) == 5  # the post job, the numbers job, the campaigns, research and usage jobs
    assert tasks.tick_schedules(db, at=NOW) == 0
    assert tasks.tick_schedules(db, at=NOW + timedelta(minutes=16)) == 2  # the 15-minute post job and the 2-minute usage job are due again


def test_a_removed_channel_profile_leaves_no_schedules_or_waiting_tasks_behind(db):
    app = config.AppConfig()
    kept = profile()
    gone = kept.model_copy(update={"id": "gone"})
    tasks.ensure_schedules(db, app, {"gone": gone, "test": kept}, at=NOW)
    tasks.enqueue(db, "post", {"profile": "gone"})
    tasks.enqueue(db, "post", {"profile": "test"})
    tasks.ensure_schedules(db, app, {"test": kept}, at=NOW)  # the "gone" profile was deleted from the settings
    with db.conn() as c:
        assert {r["profile_id"] for r in c.execute("SELECT profile_id FROM schedules WHERE profile_id IS NOT NULL")} == {"test"}
        assert [json.loads(r["payload_json"])["profile"] for r in c.execute("SELECT payload_json FROM tasks WHERE status='queued'")] == ["test"]


# ---- dashboard -------------------------------------------------------------------------------------------------


@pytest.fixture
def web(db):
    p = profile()
    sv = Services(config.AppConfig(), {"test": p}, db, Quota(db, general_cap=999, search_cap=99), None, None)
    with db.conn() as c:
        store.upsert_video(c, p.id, vid(1, title="<script>alert(1)</script> Big Episode"), "paste")
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_pages_render(web):
    client_, _ = web
    for path in ("/", "/health"):
        assert client_.get(path).status_code == 200


def test_a_tripped_breaker_shows_a_banner_and_approving_clears_it(web):
    client_, sv = web
    assert "pause-banner" not in client_.get("/?profile=test").text
    with sv.db.conn() as c:
        store.add_ai_usage(c, "claude", 0, 0, sv.app.spend_breaker.claude_usd + 1)
    html = client_.get("/clips?profile=test").text
    assert "Paused: spending reached the breaker" in html and "/api/spend-approve" in html
    assert client_.post("/api/spend-approve", data={"profile": "test"}).status_code == 200
    assert "pause-banner" not in client_.get("/clips?profile=test").text


def test_there_is_no_rating_endpoint_any_more(web):
    client_, _ = web
    assert client_.post("/api/rate", data={"profile": "test", "video_id": "x", "rating": 1}).status_code in (404, 405)


def test_every_page_renders_and_old_addresses_redirect(web):
    client_, _ = web
    for path in ("/", "/clips", "/calendar", "/calendar?view=week", "/calendar?view=list", "/accounts", "/insights",
                 "/settings", "/api/search?profile=test&q=big"):
        assert client_.get(f"{path}{'&' if '?' in path else '?'}profile=test").status_code == 200, path
    for old, new in (("/studio", "/clips"), ("/review", "/clips"), ("/posts", "/calendar"), ("/manual", "/calendar"),
                     ("/learning", "/insights")):
        r = client_.get(f"{old}?profile=test", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"].startswith(new) and "profile=test" in r.headers["location"]
    assert client_.get("/clips/999/panel?profile=test").status_code == 404
    assert client_.get("/calendar/post/999?profile=test").status_code == 404
    assert client_.get("/static/app.css").status_code == 200


def test_search_finds_a_video_and_ignores_one_letter(web):
    client_, _ = web
    found = client_.get("/api/search?profile=test&q=big episode").json()["results"]
    assert any("Big Episode" in r["title"] for r in found)
    assert client_.get("/api/search?profile=test&q=b").json()["results"] == []
    assert client_.get("/api/search?profile=nope&q=big").status_code == 404


def test_the_dashboard_ignores_requests_addressed_to_other_hosts(web):
    client_, _ = web
    assert client_.get("/", headers={"Host": "evil.example"}).status_code == 400


