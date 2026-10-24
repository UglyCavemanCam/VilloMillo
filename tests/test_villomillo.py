"""Tests for the VilloMillo research engine. Nothing here touches the network or a real key."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from villomillo import config, discovery, engine, filters, scoring, selfcheck, store, tasks
from villomillo.db import Database
from villomillo.jev import Jev, JevError, JevResult
from villomillo.quota import GENERAL, SEARCH, Quota, QuotaExceeded
from villomillo.services import Services
from villomillo.web.app import create_app
from villomillo.youtube import YouTube, YouTubeError, normalize, parse_duration

NOW = store.now()
SECRET = "AIzaSECRET-KEY-VALUE-1234567890abcdefg"

ANSWERS = {
    "fits_niche": {"type": "noul", "noul": 0.9},
    "needs_visuals": {"type": "noul", "noul": 0.1},
    "has_moments": {"type": "score", "score": 3.5, "confidence": 0.7, "legend": {str(i): "x" for i in range(5)}},
    "topic_appeal": {"type": "score", "score": 2.5, "confidence": 0.6, "legend": {str(i): "x" for i in range(5)}},
    "hook_potential": {"type": "score", "score": 2.0, "confidence": 0.6, "legend": {str(i): "x" for i in range(5)}},
    **{k: {"type": "noul", "noul": 0.02} for k in
       ("risk_violence", "risk_sexual", "risk_minors", "risk_tragedy", "risk_hate", "risk_misinfo")},
}


def profile(**research) -> config.Profile:
    return config.Profile(
        id="test", display_name="Test", niche={"definition": "podcasts"},
        research={"watch_channels": [{"handle": "@chan"}], "topics": ["a", "b"], "min_duration_min": 20, **research},
    )


def vid(i: int, *, age_h: float, views: int | None, minutes: float = 150, title: str | None = None,
        live: str = "none", lang: str | None = "en", channel: str = "UCchan") -> dict:
    return {
        "video_id": f"vid{i:08d}", "channel_id": channel, "channel_title": "Chan", "title": title or f"Episode {i}",
        "description": "d", "tags": [], "duration_s": int(minutes * 60), "language": lang, "live_status": live,
        "published_at": (NOW - timedelta(hours=age_h)).isoformat(timespec="seconds"),
        "views": views, "likes": (views or 0) // 40, "comments": (views or 0) // 400,
    }


class FakeYT:
    def __init__(self, videos: list[dict]):
        self.v = {x["video_id"]: x for x in videos}
        self.search_ids: list[str] = []
        self.searches: list[str] = []

    def channel(self, *, handle=None, channel_id=None):
        return {"channel_id": "UCchan", "title": "Chan", "uploads_playlist": "UUchan", "subscribers": 10}

    def recent_upload_ids(self, playlist_id, *, limit=50):
        return list(self.v)[:limit]

    def videos(self, ids):
        return [self.v[i] for i in ids if i in self.v]

    def most_popular(self, **kw):
        return []

    def search(self, query, **kw):
        self.searches.append(query)
        return self.search_ids


class FakeJev:
    def __init__(self, fail: bool = False):
        self.calls: list[dict] = []
        self.fail = fail

    def ask(self, state, questions):
        self.calls.append(state)
        if self.fail:
            raise JevError("HTTP 500")
        return JevResult("jev-test", copy.deepcopy(ANSWERS), {"input_tokens": 1000, "output_tokens": 5})


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


@pytest.fixture
def library():
    """Six old uploads (so a baseline exists) and a few fresh ones, newest first."""
    fresh = [vid(i, age_h=10 + i * 20, views=300_000 + i * 50_000) for i in range(4)]
    old = [vid(10 + i, age_h=24 * (20 + i * 5), views=1_000_000 + i * 100_000) for i in range(6)]
    return fresh + old


# ---- configuration ---------------------------------------------------------------------------------------------


def test_the_shipped_settings_load():
    assert "solved" in config.load_profiles()
    assert config.load_app().dashboard.host == "127.0.0.1"


def test_a_channel_needs_exactly_one_name():
    with pytest.raises(ValidationError):
        config.WatchChannel(handle="@a", channel_id="UCx")
    with pytest.raises(ValidationError):
        config.WatchChannel()


def test_typos_unknown_weights_and_public_hosts_are_errors():
    with pytest.raises(ValidationError):
        config.Grading(weights={"vibes": 1})
    with pytest.raises(ValidationError):
        config.Research(topcis=["x"])
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


# ---- Tier 0 filters --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "state", "reason"),
    [
        ({"age_h": 5, "views": 10}, "eligible", None),
        ({"age_h": 5, "views": 10, "live": "live"}, "held", "live"),
        ({"age_h": 5, "views": 10, "minutes": 1}, "rejected", "Short"),
        ({"age_h": 5, "views": 10, "minutes": 5}, "rejected", "shorter"),
        ({"age_h": 5, "views": 10, "minutes": 999}, "rejected", "longer"),
        ({"age_h": 24 * 30, "views": 10}, "rejected", "older"),
        ({"age_h": 5, "views": 10, "lang": "fr"}, "rejected", "language"),
        ({"age_h": 5, "views": 10, "lang": None}, "eligible", None),
        ({"age_h": 5, "views": None}, "held", "hidden"),
        ({"age_h": 5, "views": 10, "title": "My Reaction video"}, "rejected", "title"),
    ],
)
def test_tier0(kwargs, state, reason):
    p = profile(blocked_title_words=["reaction"])
    got, why = filters.check(vid(1, **kwargs), p, at=NOW)
    assert got == state
    assert reason is None or reason in why


# ---- scoring ---------------------------------------------------------------------------------------------------


def test_growth_curve_rises_and_saturates():
    fractions = [scoring.growth_fraction(h) for h in (3, 10, 80, 240, 400, 2000)]
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0 and 0.2 < fractions[1] < 0.4


def test_a_typical_upload_scores_about_one_times_the_usual():
    video = vid(1, age_h=82, views=int(1_000_000 * scoring.growth_fraction(82)))
    f = scoring.features(video, [], 1_000_000, NOW)
    assert f["outlier_ratio"] == pytest.approx(1.0, rel=0.05)
    assert scoring.ratio_to_unit(1.0) == 0.5 and scoring.ratio_to_unit(4.0) == 1.0 and scoring.ratio_to_unit(None) == 0.5


def test_momentum_needs_two_snapshots_an_hour_apart():
    video = vid(1, age_h=20, views=300_000)
    one = [{"taken_at": store.iso(NOW), "views": 300_000}]
    assert scoring.features(video, one, 1e6, NOW)["momentum_ratio"] is None
    two = [{"taken_at": store.iso(NOW - timedelta(hours=2)), "views": 280_000}, {"taken_at": store.iso(NOW), "views": 300_000}]
    assert scoring.features(video, two, 1e6, NOW)["momentum_ratio"] > 0


def test_assemble_and_gate():
    g = config.Grading(min_score=60)
    feats = {"outlier_ratio": 1.0, "momentum_ratio": None, "like_rate": 0.025, "comment_rate": 0.0025}
    r = scoring.assemble(feats, ANSWERS, g)
    assert 0 <= r.score <= 100 and 0 <= r.certainty <= 1 and r.flags == []
    assert r.gate in ("pass", "escalate")


def test_a_risk_answer_holds_a_video_back_and_changes_the_score():
    g = config.Grading()
    feats = {"outlier_ratio": 2.0, "momentum_ratio": None, "like_rate": 0.03, "comment_rate": 0.003}
    risky = copy.deepcopy(ANSWERS)
    risky["risk_violence"]["noul"] = 0.9
    clean, flagged = scoring.assemble(feats, ANSWERS, g), scoring.assemble(feats, risky, g)
    assert flagged.gate == "fail" and flagged.flags and flagged.score < clean.score


@pytest.mark.parametrize(("score", "certainty", "gate"), [(80, 0.9, "pass"), (80, 0.3, "escalate"), (68, 0.9, "escalate"), (40, 0.9, "fail")])
def test_gate_rules(score, certainty, gate):
    assert scoring.gate(score, certainty, [], config.Grading(min_score=70, escalate_band=5)) == gate


def test_changing_weights_changes_scores_without_new_ai_calls():
    feats = {"outlier_ratio": 4.0, "momentum_ratio": None, "like_rate": 0.0, "comment_rate": 0.0}
    base = scoring.assemble(feats, ANSWERS, config.Grading()).score
    heavy = scoring.assemble(feats, ANSWERS, config.Grading(weights={"outlier": 1.0})).score
    assert heavy == 100.0 and heavy != base


# ---- Jev client ------------------------------------------------------------------------------------------------


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


def test_snapshots_are_not_taken_twice_in_a_row(db):
    with db.conn() as c:
        assert store.add_snapshot(c, "vid00000001", 1, 1, 1) is True
        assert store.add_snapshot(c, "vid00000001", 2, 1, 1) is False


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
    first = tasks.tick_schedules(db, at=NOW)
    assert first == 6  # watch, snapshots, grade, review, post, topics
    assert tasks.tick_schedules(db, at=NOW) == 0
    assert tasks.tick_schedules(db, at=NOW + timedelta(minutes=16)) == 2  # only the 15-minute grade and post jobs are due


# ---- discovery and the full cycle --------------------------------------------------------------------------------


def services(db, yt, jev, **budget) -> Services:
    app = config.AppConfig(budgets=config.Budgets(**budget))
    return Services(app, {"test": profile()}, db, Quota(db, general_cap=999, search_cap=99), yt, jev)


def test_first_look_backfills_then_later_looks_do_not_duplicate(db, library):
    p, yt = profile(), FakeYT(library)
    sv = services(db, yt, FakeJev())
    discovery.discover(db, yt, p, topics=False)
    discovery.discover(db, yt, p, topics=False)
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"] == len(library)
        assert c.execute("SELECT baseline_views FROM watched_sources").fetchone()["baseline_views"] == pytest.approx(1_250_000, rel=0.1)
    assert sv.db is db


def test_old_and_live_videos_are_filtered_and_live_ones_are_looked_at_again(db, library):
    live = vid(99, age_h=3, views=10, live="live")
    yt = FakeYT([live, *library])
    p = profile()
    out = discovery.discover(db, yt, p, topics=False)["filters"]
    assert out == {"eligible": 4, "held": 1, "rejected": 6}
    yt.v[live["video_id"]]["live_status"] = "none"  # the broadcast ends
    again = discovery.discover(db, yt, p, topics=False)["filters"]  # the next look refreshes it and re-checks
    assert again == {"eligible": 1, "held": 0, "rejected": 0}


def test_topic_search_stays_within_its_cap_and_skips_known_videos(db, library):
    yt = FakeYT(library)
    yt.search_ids = [library[0]["video_id"], library[1]["video_id"]]
    p = profile(max_searches_per_run=1)
    discovery.resolve_sources(db, yt, p)
    assert discovery.search_topics(db, yt, p) == 2
    assert discovery.search_topics(db, yt, p) == 0 and yt.searches == ["a", "a"]


def test_a_full_cycle_scores_ranks_and_costs_almost_nothing(db, library):
    yt, jev, p = FakeYT(library), FakeJev(), profile()
    app = config.AppConfig()
    summary = engine.run_cycle(db, yt, jev, app, p, topics=False)
    assert summary["grading"]["graded"] == 4 and len(jev.calls) == 4
    ranked = store.ranked(db, p.id)
    assert [r["score"] for r in ranked] == sorted((r["score"] for r in ranked), reverse=True)
    assert store.ai_spend_today(db, "jev") == pytest.approx(4 * 1000 * 0.042e-6)
    assert engine.run_cycle(db, yt, jev, app, p, topics=False)["grading"]["graded"] == 0  # nothing graded twice
    assert len(jev.calls) == 4


def test_rescoring_uses_saved_answers_and_makes_no_ai_calls(db, library):
    yt, jev, p = FakeYT(library), FakeJev(), profile()
    engine.run_cycle(db, yt, jev, config.AppConfig(), p, topics=False)
    p2 = p.model_copy(update={"grading": config.Grading(weights={"niche": 1.0})})
    assert engine.rescore(db, p2) == 4 and len(jev.calls) == 4
    assert all(r["score"] == pytest.approx(90.0) for r in store.ranked(db, p.id))  # niche answer was 0.9


def test_the_spend_breaker_stops_grading(db, library):
    yt, jev, p = FakeYT(library), FakeJev(), profile()
    app = config.AppConfig(spend_breaker=config.SpendBreaker(jev_usd=0.00005))  # room for one 4.2e-5 call
    out = engine.run_cycle(db, yt, jev, app, p, topics=False)["grading"]
    assert out["graded"] == 2 and "breaker" in out["stopped"]


def test_jev_failing_repeatedly_stops_the_run_without_losing_the_videos(db, library):
    yt, p = FakeYT(library), profile()
    out = engine.run_cycle(db, yt, FakeJev(fail=True), config.AppConfig(), p, topics=False)["grading"]
    assert out["graded"] == 0 and out["errors"] == 3 and out["stopped"]
    assert store.counts_by_state(db, p.id)["eligible"] == 4  # still waiting to be graded


# ---- dashboard -------------------------------------------------------------------------------------------------


@pytest.fixture
def web(db, library):
    library[0]["title"] = "<script>alert(1)</script> Big Episode"
    yt, p = FakeYT(library), profile()
    sv = services(db, yt, FakeJev())
    engine.run_cycle(db, yt, sv.jev, sv.app, p, topics=False)
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_pages_render_and_titles_are_escaped(web):
    client_, _ = web
    for path in ("/", "/research", "/research?gate=pass", "/health"):
        assert client_.get(path).status_code == 200
    html = client_.get("/research").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "<script>alert(1)" not in html


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


# ---- automatic self-check (no human ratings) ------------------------------------------------------------------


def test_spearman_matches_known_values():
    assert selfcheck.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert selfcheck.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert selfcheck.spearman([1, 1, 1, 1], [1, 2, 3, 4]) == 0.0  # no variation: no link
    assert selfcheck.spearman([1, 2, 3], [1, 3, 2]) == pytest.approx(0.5)


def test_grades_ignore_views_entirely():
    p = profile()
    a = copy.deepcopy(ANSWERS)
    base = selfcheck.grade_without_views(a, p)
    assert base > 0 and base == selfcheck.grade_without_views(copy.deepcopy(ANSWERS), p)
    a["risk_violence"]["noul"] = 0.9
    assert selfcheck.grade_without_views(a, p) < base


def test_verdicts():
    assert selfcheck.verdict_for(10, 0.9, 0.9).startswith("too few")
    assert selfcheck.verdict_for(40, 0.5, 0.8) == "pointing the right way"
    assert selfcheck.verdict_for(40, -0.1, 0.4) == "backwards: do not trust the grades yet"
    assert selfcheck.verdict_for(40, 0.05, 0.5) == "not clear yet"


class ViewsFollowTitles(FakeJev):
    """Grades a video high when its title says 'great'. Used to check the self-check can see a real link."""

    def ask(self, state, questions):
        good = "great" in state["video"]["title"].lower()
        answers = copy.deepcopy(ANSWERS)
        answers["has_moments"] = {"type": "score", "score": 4.5 if good else 0.5, "confidence": 0.9,
                                  "legend": {str(i): "x" for i in range(5)}}
        self.calls.append(state)
        return JevResult("jev-test", answers, {"input_tokens": 1000, "output_tokens": 5})


def test_the_self_check_finds_a_real_link_and_reports_it(db):
    old_good = [vid(100 + i, age_h=24 * (30 + i), views=2_000_000, title=f"Great episode {i}") for i in range(20)]
    old_poor = [vid(200 + i, age_h=24 * (30 + i), views=400_000, title=f"Plain episode {i}") for i in range(20)]
    yt = FakeYT(old_good + old_poor)
    p = profile()
    # the self-check reads the channel's upload list, so the profile needs a watched source first
    discovery.resolve_sources(db, yt, p)
    result = selfcheck.run(db, yt, ViewsFollowTitles(), p)
    assert result["verdict"] == "pointing the right way"
    assert result["rank_correlation"] > 0.5 and result["top_hit_rate"] == 1.0
    assert store.last_selfcheck(db, p.id)["verdict"] == "pointing the right way"


def test_the_self_check_says_so_when_there_is_too_little_history(db):
    yt = FakeYT([vid(1, age_h=24 * 30, views=100_000)])
    p = profile()
    discovery.resolve_sources(db, yt, p)
    result = selfcheck.run(db, yt, FakeJev(), p)
    assert result["verdict"].startswith("too few") and result["graded"] == 0


# ---- Phase 2: transcripts, second opinions, learning ------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from villomillo import claude, learning, transcripts  # noqa: E402
from villomillo.engine import _apply_second_opinion  # noqa: E402
from villomillo.grading import combine_transcript  # noqa: E402


def test_long_transcripts_are_split_and_sampled_evenly():
    text = " ".join(f"word{i}" for i in range(60_000))
    pieces = transcripts.chunks(text)
    assert len(pieces) == transcripts.MAX_CHUNKS
    assert pieces[0].startswith("word0 ") and pieces[-1].split()[0] != "word0"
    assert transcripts.chunks("short text") == ["short text"]


def test_transcript_answers_are_blended_into_the_title_answers():
    chunk = {
        "standalone_moment": {"type": "noul", "noul": 1.0},
        "best_hook": {"type": "score", "score": 4.0, "confidence": 0.9, "legend": {str(i): "x" for i in range(5)}},
        "speech": {"type": "choice", "choice": "talk", "confidence": 0.9, "probabilities": {"talk": 1.0, "non_speech": 0.0}},
    }
    merged, summary = combine_transcript(ANSWERS, [chunk, chunk])
    assert merged["has_moments"]["score"] == pytest.approx((3.5 + 4) / 2)  # transcript said yes to a moment
    assert merged["hook_potential"]["score"] == 4.0  # the stronger of the two hook answers
    assert summary["standalone_moment_share"] == 1.0 and summary["non_speech_share"] == 0.0


def test_a_claude_opinion_settles_only_a_confident_borderline_video():
    g = config.Grading(min_score=70, escalate_band=5)
    base = scoring.Result(score=68, certainty=0.5, gate="escalate", components={}, flags=[])
    yes = {"clip_worthy": True, "confidence": 0.9, "reason": "x"}
    unsure = {"clip_worthy": True, "confidence": 0.4, "reason": "x"}
    r = scoring.Result(**{**base.__dict__, "flags": []})
    _apply_second_opinion(r, yes, g, [])
    assert r.gate == "pass"
    r = scoring.Result(**{**base.__dict__, "flags": []})
    _apply_second_opinion(r, unsure, g, [])
    assert r.gate == "escalate"
    flagged = scoring.Result(**{**base.__dict__, "flags": ["risk_violence (90%)"]})
    _apply_second_opinion(flagged, yes, g, [])
    assert flagged.gate == "escalate"  # a safety flag is never overridden


def test_claude_replies_are_validated_before_use():
    def reply(text):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))

    class Fake:
        def __init__(self, text):
            self.messages = SimpleNamespace(create=lambda **kw: reply(text))

    ok = claude.judge(Fake('Sure: {"clip_worthy": true, "confidence": 0.8, "reason": "a clear story"}'),
                      niche="n", title="t", description="d", excerpt="e")
    assert ok[0]["clip_worthy"] is True and ok[1] > 0
    for bad in ('{"clip_worthy": "yes", "confidence": 0.8}', '{"clip_worthy": true, "confidence": 7}', "no json here"):
        with pytest.raises(claude.ClaudeError):
            claude.judge(Fake(bad), niche="n", title="t", description="d", excerpt="e")


def test_the_shortlist_keeps_some_variety_and_says_why():
    cands = [{"video_id": f"a{i:010d}", "score": 90 - i, "found_by": "channel"} for i in range(8)]
    cands += [{"video_id": f"b{i:010d}", "score": 50 - i, "found_by": "topic"} for i in range(4)]
    picked = learning.shortlist_preview(cands, n=5, explore_share=0.4)
    assert len(picked) == 5
    assert any(p["found_by"] == "topic" for p in picked), "an under-tested source should be explored"
    assert {p["why"].split(":")[0] for p in picked} == {"top score", "explore"}


def test_length_learning_stays_off_without_enough_examples(db):
    store.event(db, "info", "t", "x")  # make sure the DB exists
    assert learning.length_multiplier(db, "p", 3 * 3600) is None
    rows = [{"grade": 70, "beat_median": i % 2 == 0, "duration_min": 150} for i in range(30)]
    with db.conn() as c:
        c.execute("INSERT INTO selfchecks (profile_id, ran_at, sample, verdict, details_json) VALUES ('p', ?, 30, 'x', ?)",
                  (store.iso(), json.dumps({"rows": rows})))
    m = learning.length_multiplier(db, "p", 3 * 3600)
    assert m is not None and 0.85 <= m <= 1.15
    assert learning.length_status(db, "p")[2]["status"] == "active"
    assert learning.length_multiplier(db, "p", 30 * 60) is None  # that group has no examples


def test_calibration_groups_grades_into_bands(db):
    rows = [{"grade": g, "beat_median": b, "duration_min": 100} for g, b in [(55, True), (58, False), (75, True), (77, True)]]
    with db.conn() as c:
        c.execute("INSERT INTO selfchecks (profile_id, ran_at, sample, verdict, details_json) VALUES ('p', ?, 4, 'x', ?)",
                  (store.iso(), json.dumps({"rows": rows})))
    bands = {b["band"]: b for b in learning.calibration(db, "p")}
    assert bands["50-59"]["n"] == 2 and bands["50-59"]["beat_median"] == 0.5
    assert bands["70-79"]["beat_median"] == 1.0
    assert bands["80-100"]["n"] == 0


def test_a_failed_claude_call_is_logged_and_skipped_not_fatal(db):
    from villomillo.engine import second_opinions

    p = profile()
    app = config.AppConfig()
    store.sync_profile(db, p.id, p.display_name, p.digest())
    v = vid(1, age_h=5, views=100)
    with db.conn() as c:
        store.upsert_video(c, p.id, v, "channel")
        store.save_score(c, p.id, v["video_id"], scoring.Result(60, 0.5, "escalate", {}, []), 1)

    class Broken:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("network down")

    out = second_opinions(db, app, p, client=Broken())
    assert out["asked"] == 0
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM events WHERE component='second-opinion'").fetchone()["n"] == 1


def test_a_removed_channel_profile_leaves_no_schedules_or_waiting_tasks_behind(db):
    app = config.AppConfig()
    gone, kept = profile(), profile()
    gone = gone.model_copy(update={"id": "gone"})
    tasks.ensure_schedules(db, app, {"gone": gone, "test": kept}, at=NOW)
    tasks.enqueue(db, "grade", {"profile": "gone"})
    tasks.enqueue(db, "grade", {"profile": "test"})
    tasks.ensure_schedules(db, app, {"test": kept}, at=NOW)  # the "gone" profile was deleted from the settings
    with db.conn() as c:
        assert {r["profile_id"] for r in c.execute("SELECT profile_id FROM schedules")} == {"test"}
        assert [json.loads(r["payload_json"])["profile"] for r in c.execute("SELECT payload_json FROM tasks WHERE status='queued'")] == ["test"]
