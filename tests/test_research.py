"""Research: the switch and its limits, finding Creative Commons and watched-channel videos, starting them one at a time, and the
decision on each. No network: YouTube, the channel feeds and the engine are stand-ins."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_runs import FakeEngine, FakeJev

from sources import channel_feed
from villomillo import (
    campaigns,
    channels,
    clipper,
    config,
    copywriter,
    posting,
    research,
    runner,
    runs,
    store,
    tasks,
)
from villomillo.db import Database
from villomillo.quota import Quota, QuotaExceeded
from villomillo.services import Services
from villomillo.web.app import create_app

RULES = config.Scoring()


def vid(n, *, minutes=45, views=50_000, days=1.0, license="creativeCommon", live="none", channel="UCchannel0000000000000001", title=None):
    return {"video_id": f"cc{n:09d}", "channel_id": channel, "channel_title": "A Show", "title": title or f"Episode {n}",
            "published_at": store.iso(store.now() - timedelta(days=days)), "license": license,
            "duration_s": minutes * 60, "views": views, "live_status": live}


class FakeYT:
    def __init__(self, videos=(), found=None, search_out=False):
        self.catalog = {v["video_id"]: v for v in videos}
        self.found, self.search_out, self.searches, self.looked_up = found or {}, search_out, [], []

    def videos(self, ids):
        self.looked_up.append(list(ids))
        return [self.catalog[i] for i in ids if i in self.catalog]

    def search(self, query, *, days, language=None, region=None, creative_commons=False):
        if self.search_out:
            raise QuotaExceeded("search", 80, 80)
        self.searches.append((query, creative_commons))
        return self.found.get(query, [])


def listing(platform, key, *items):
    return [channel_feed.NewSourceVideo(platform, key, vid_, url, title, when, short) for vid_, url, title, when, short in items]


class FakeFeed:
    def __init__(self, latest=None, ready=None):
        self.listings, self.ready, self.checked = latest or {}, ready or {}, []

    def latest(self, platform, key):
        got = self.listings[(platform, key)]
        if isinstance(got, Exception):
            raise got
        return got

    def readiness(self, url, min_seconds=0, **kw):
        self.checked.append(url)
        return self.ready.get(url, channel_feed.Readiness("ready", duration=3 * 3600))


TWITCH = "tw_123456789"
TWITCH_URL = "https://www.twitch.tv/videos/123456789"


def make_sv(tmp_path, monkeypatch, yt=None, jev=None, app=None):
    database = Database(tmp_path / "research.db")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(clipper.validate, "check", lambda path, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    return Services(app or config.AppConfig(), channels.load(database), database, Quota(database, general_cap=9, search_cap=9), yt, jev or FakeJev())


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch, yt=FakeYT())


def switch_on(sv, **kw):
    research.save_settings(sv.db, sv.app.research, RULES, on=True, **kw)


def add_youtube_channel(sv, key="UCchannel0000000000000001", name="A Show"):
    with sv.db.conn() as c:
        c.execute("INSERT INTO research_sources (platform, channel_key, name, learned, added_at) VALUES ('youtube',?,?,0,?)", (key, name, store.iso()))


# ---- the switch, the limits, the bar ---------------------------------------------------------------------------------------


def test_research_is_off_until_you_switch_it_on_and_the_limits_are_checked(sv):
    assert not research.is_on(sv.db) and research.settings(sv.db, sv.app.research)["videos_per_day"] == 6
    research.save_settings(sv.db, sv.app.research, RULES, on=True, videos_per_day="3", bar_max="2.5", topics="  podcast  interview \n\nsports talk\npodcast  interview")
    s = research.settings(sv.db, sv.app.research)
    assert s == {"on": True, "videos_per_day": 3, "bar_max": 2.5, "topics": ["podcast interview", "sports talk"]}
    research.save_settings(sv.db, sv.app.research, RULES, on=False)  # a switch change keeps the rest
    assert not research.is_on(sv.db) and research.settings(sv.db, sv.app.research)["videos_per_day"] == 3
    for bad in ({"videos_per_day": "0"}, {"videos_per_day": "x"}, {"bar_max": "0.5"}, {"bar_max": "abc"}):
        with pytest.raises(research.ResearchError):
            research.save_settings(sv.db, sv.app.research, RULES, **bad)


def test_the_bar_starts_at_the_floor_only_goes_up_and_stops_at_the_highest_allowed(sv):
    cfg = sv.app.research
    assert research.bar(sv.db, RULES, cfg) == 1.0
    research.set_bar(sv.db, 1.75)
    assert research.bar(sv.db, RULES, cfg) == 1.75
    research.set_bar(sv.db, 9.0)
    assert research.bar(sv.db, RULES, cfg) == cfg.bar_max  # never above what you allow
    research.save_settings(sv.db, cfg, RULES, bar_max="2.0")
    assert research.bar(sv.db, RULES, cfg) == 2.0
    research.set_bar(sv.db, 0.2)
    assert research.bar(sv.db, RULES, cfg) == 1.0  # never under the floor


def test_topics_default_to_the_channels_categories_until_you_write_your_own(sv):
    assert research.topics(sv.db, sv.app.research, sv.profiles) == ["podcasts"]
    research.save_settings(sv.db, sv.app.research, RULES, topics="interview")
    assert research.topics(sv.db, sv.app.research, sv.profiles) == ["interview"]


# ---- what is worth looking at ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("changes,via,reason", [
    ({"live": "live"}, "watch", "live now"),
    ({"license": "youtube"}, "watch", "not Creative Commons"),
    ({"minutes": 3}, "watch", "only 3 minutes"),
    ({"minutes": 600}, "watch", "10.0 hours"),
    ({"days": 30}, "watch", "30 days old"),
    ({"views": 10}, "search", "only 10 views"),
])
def test_a_video_is_turned_down_with_its_reason(changes, via, reason):
    why = research.judge(vid(1, **changes), config.Research(), via=via, now=store.now())
    assert why and reason in why


def test_a_good_video_passes_and_a_watched_video_needs_no_views():
    assert research.judge(vid(1), config.Research(), via="search", now=store.now()) is None
    assert research.judge(vid(2, views=3), config.Research(), via="watch", now=store.now()) is None


# ---- finding videos --------------------------------------------------------------------------------------------------------


def test_a_watched_youtube_channel_queues_creative_commons_videos_and_says_why_it_skipped_the_rest(tmp_path, monkeypatch):
    videos = [vid(1), vid(2, license="youtube"), vid(3, minutes=2), vid(4)]
    yt = FakeYT(videos)
    sv = make_sv(tmp_path, monkeypatch, yt=yt)
    add_youtube_channel(sv)
    feed = FakeFeed({("youtube", "UCchannel0000000000000001"): listing("youtube", "UCchannel0000000000000001",
        *[(v["video_id"], f"https://www.youtube.com/watch?v={v['video_id']}", v["title"], 0.0, False) for v in videos],
        ("short0000000", "https://www.youtube.com/shorts/short0000000", "A Short", 0.0, True))})
    lines = research.discover(sv, yt=yt, feed=feed)
    got = {c["video_id"]: c for c in research.candidates(sv.db)}
    assert got["cc000000001"]["state"] == "queued" and got["cc000000004"]["state"] == "queued"
    assert got["cc000000002"]["state"] == "skipped" and "not Creative Commons" in got["cc000000002"]["reason"]
    assert got["cc000000003"]["state"] == "skipped" and "2 minutes" in got["cc000000003"]["reason"]
    assert "short0000000" not in got and sum("found" in line for line in lines) == 2
    research.discover(sv, yt=yt, feed=feed)  # nothing is looked at twice
    assert len(research.candidates(sv.db)) == 4 and len(yt.looked_up) == 1


def test_only_as_many_candidates_wait_as_the_queue_allows(tmp_path, monkeypatch):
    videos = [vid(n) for n in range(1, 6)]
    yt = FakeYT(videos)
    sv = make_sv(tmp_path, monkeypatch, yt=yt, app=config.AppConfig(research=config.Research(queue_ahead=2)))
    add_youtube_channel(sv)
    feed = FakeFeed({("youtube", "UCchannel0000000000000001"): listing("youtube", "UCchannel0000000000000001",
        *[(v["video_id"], f"https://www.youtube.com/watch?v={v['video_id']}", v["title"], 0.0, False) for v in videos])})
    research.discover(sv, yt=yt, feed=feed)
    assert research.queued(sv.db) == 2
    assert research.discover(sv, yt=yt, feed=feed) == []  # full: no more is read


def test_a_channel_that_cannot_be_read_is_reported_and_the_others_still_are(tmp_path, monkeypatch):
    yt = FakeYT([vid(1)])
    sv = make_sv(tmp_path, monkeypatch, yt=yt)
    add_youtube_channel(sv, "UCbroken00000000000000001", "Broken")
    add_youtube_channel(sv)
    feed = FakeFeed({("youtube", "UCbroken00000000000000001"): ValueError("no feed"),
                     ("youtube", "UCchannel0000000000000001"): listing("youtube", "UCchannel0000000000000001", ("cc000000001", "https://www.youtube.com/watch?v=cc000000001", "Episode 1", 0.0, False))})
    lines = research.discover(sv, yt=yt, feed=feed)
    assert any("Broken: could not be read" in line for line in lines) and research.queued(sv.db) == 1


def test_twitch_and_kick_videos_come_only_from_channels_you_list(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch)
    with sv.db.conn() as c:
        c.execute("INSERT INTO research_sources (platform, channel_key, name, learned, added_at) VALUES ('twitch','somestreamer','SomeStreamer',0,?)", (store.iso(),))
    old = store.now().timestamp() - 40 * 86400
    feed = FakeFeed({("twitch", "somestreamer"): listing("twitch", "somestreamer", (TWITCH, TWITCH_URL, "Late night", store.now().timestamp() - 3600, False),
                                                         ("tw_999", "https://www.twitch.tv/videos/999", "Ancient", old, False))})
    research.discover(sv, yt=None, feed=feed)
    got = {c["video_id"]: c for c in research.candidates(sv.db)}
    assert got[TWITCH]["state"] == "queued" and got[TWITCH]["via"] == "watch"
    assert got["tw_999"]["state"] == "skipped" and "days old" in got["tw_999"]["reason"]


def test_searches_are_rationed_rotate_through_the_topics_and_learn_good_channels(tmp_path, monkeypatch):
    yt = FakeYT([vid(1), vid(2, views=5), vid(3, channel="UCother00000000000000002")],
                found={"podcast": ["cc000000001", "cc000000002"], "sports": ["cc000000003"], "talks": []})
    sv = make_sv(tmp_path, monkeypatch, yt=yt)
    research.save_settings(sv.db, sv.app.research, RULES, topics="podcast\nsports\ntalks")
    research.discover(sv, yt=yt, feed=FakeFeed())
    assert yt.searches == [("podcast", True), ("sports", True)]  # two a pass, Creative Commons only
    got = {c["video_id"]: c for c in research.candidates(sv.db)}
    assert got["cc000000001"]["state"] == "queued" and got["cc000000001"]["topic"] == "podcast"
    assert got["cc000000002"]["state"] == "skipped" and "5 views" in got["cc000000002"]["reason"]
    assert {s["channel_key"] for s in research.sources(sv.db)} == {"UCchannel0000000000000001", "UCother00000000000000002"}
    assert all(s["learned"] for s in research.sources(sv.db))
    research.discover(sv, yt=yt, feed=FakeFeed({("youtube", "UCchannel0000000000000001"): [], ("youtube", "UCother00000000000000002"): []}))
    assert [q for q, _ in yt.searches[2:]] == ["talks", "podcast"]  # the topic searched longest ago goes next


def test_an_used_up_search_allowance_ends_the_searching_not_the_pass(tmp_path, monkeypatch):
    yt = FakeYT(search_out=True)
    sv = make_sv(tmp_path, monkeypatch, yt=yt)
    lines = research.discover(sv, yt=yt, feed=FakeFeed())
    assert any("search allowance" in line for line in lines)


def test_without_a_youtube_key_or_a_channel_it_says_there_is_nothing_to_look_in(sv):
    lines = research.discover(sv, yt=None, feed=FakeFeed())
    assert lines and "nothing to look in" in lines[0]


def test_old_skipped_videos_are_forgotten_but_a_video_is_never_queued_twice(sv):
    research._record(sv.db, {"video_id": "cc000000001", "platform": "youtube", "url": "u", "title": "t"}, state="skipped", reason="x", via="watch")
    with sv.db.conn() as c:
        c.execute("UPDATE research_candidates SET found_at=?", (store.iso(store.now().replace(year=2020)),))
    research.discover(sv, yt=None, feed=FakeFeed())
    assert research.candidates(sv.db) == []
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=cc000000009")  # one already looked at, pasted by you
    assert research._seen(sv.db, "cc000000009")


# ---- starting a video ------------------------------------------------------------------------------------------------------


def queue_one(sv, n=1, **kw):
    v = vid(n, **kw)
    research._record(sv.db, research._youtube_candidate(v, store.now()), state="queued", reason=None, via="watch", rank=5.0)
    return v["video_id"]


def test_nothing_starts_while_research_is_off(sv):
    queue_one(sv)
    assert research.work(sv, yt=sv.yt, feed=FakeFeed()) == [] and research.candidates(sv.db)[0]["state"] == "queued"


def test_the_best_video_starts_as_a_research_run_one_at_a_time_and_campaigns_go_first(sv):
    switch_on(sv)
    queue_one(sv, 1)
    research._record(sv.db, research._youtube_candidate(vid(2), store.now()), state="queued", reason=None, via="watch", rank=900.0)
    lines = research.start_next(sv, FakeFeed())
    assert lines == ["started Episode 2"]  # the higher rank first
    with sv.db.conn() as c:
        run = dict(c.execute("SELECT * FROM runs").fetchone())
        assert run["source"] == "research" and run["video_id"] == "cc000000002" and run["campaign_id"] is None
        assert c.execute("SELECT run_id, state FROM research_candidates WHERE video_id='cc000000002'").fetchone()["state"] == "started"
    assert research.start_next(sv, FakeFeed()) == ["Waiting: another Research video is being clipped."]  # one at a time
    with sv.db.conn() as c:
        c.execute("UPDATE runs SET state='finished'")
        c.execute("INSERT INTO runs (source, url, platform, video_id, profile_id, state, created_at, updated_at, campaign_id) "
                  "VALUES ('campaign','https://www.youtube.com/watch?v=xxxxxxxxxxx','youtube','xxxxxxxxxxx','solved','making',?,?,'cid')", (store.iso(), store.iso()))
    assert research.start_next(sv, FakeFeed()) == ["Waiting: a campaign video is being clipped first."]


def test_the_daily_limit_holds(sv):
    switch_on(sv, videos_per_day="1")
    queue_one(sv, 1)
    queue_one(sv, 2)
    assert research.start_next(sv, FakeFeed())
    with sv.db.conn() as c:
        c.execute("UPDATE runs SET state='finished'")
    assert research.start_next(sv, FakeFeed())[0].startswith("Daily limit reached (1 video in 24 hours)") and research.started_recently(sv.db) == 1
    with sv.db.conn() as c:
        c.execute("UPDATE runs SET created_at=?", (store.iso(store.now().replace(year=2020)),))
    assert research.start_next(sv, FakeFeed())  # a day later it may go on


def test_a_twitch_video_is_checked_before_the_engine_is_asked_to_download_it(sv):
    switch_on(sv)
    cand = {"video_id": TWITCH, "platform": "twitch", "url": TWITCH_URL, "title": "Late night", "channel": "SomeStreamer"}
    research._record(sv.db, cand, state="queued", reason=None, via="watch", rank=1.0)
    still_live = channel_feed.Readiness("not_yet", "Still live or about to go live. Waiting for it to finish.")
    assert research.start_next(sv, FakeFeed(ready={TWITCH_URL: still_live})) == []
    assert research.candidates(sv.db)[0]["state"] == "queued"  # looked at again next pass
    assert research.start_next(sv, FakeFeed(ready={TWITCH_URL: channel_feed.Readiness("skip", "Shorter than 10 minutes.")})) == []
    assert research.candidates(sv.db)[0]["state"] == "skipped" and "Shorter than 10 minutes" in research.candidates(sv.db)[0]["reason"]
    research._record(sv.db, {**cand, "video_id": "tw_5", "url": "https://www.twitch.tv/videos/5"}, state="queued", reason=None, via="watch", rank=1.0)
    assert research.start_next(sv, FakeFeed(ready={"https://www.twitch.tv/videos/5": channel_feed.Readiness("ready", duration=9 * 3600)})) == []
    assert "9.0 hours" in next(c for c in research.candidates(sv.db) if c["video_id"] == "tw_5")["reason"]
    research._record(sv.db, {**cand, "video_id": "tw_6", "url": "https://www.twitch.tv/videos/6"}, state="queued", reason=None, via="watch", rank=1.0)
    assert research.start_next(sv, FakeFeed()) == ["started Late night"]


# ---- the decision, the clips and the credit --------------------------------------------------------------------------------


def go(sv, engine=None, video_id=None):
    switch_on(sv)
    queue_one(sv)
    research.start_next(sv, FakeFeed())
    engine = engine or FakeEngine()
    for _ in range(4):
        if runs.get(sv.db, 1)["state"] != "analysing":
            break
        runs.analyse(sv, 1, engine)
    return engine


def test_a_good_video_goes_ahead_with_no_start_press_at_the_research_bar(sv):
    engine = go(sv)
    run = runs.get(sv.db, 1)
    assert run["state"] == "making" and engine.submitted[-1]["moments"] == [1, 2]
    made = [m for m in run["plan"]["moments"] if m["make"]]
    assert len(made) == 2 and all("clear 75 and $1.00" in m["explain"][3] for m in made)  # the bar starts at the floor


def test_a_higher_bar_stops_clips_that_cleared_the_pasted_floor(sv):
    research.set_bar(sv.db, 1.5)
    engine = go(sv)
    run = runs.get(sv.db, 1)
    assert run["state"] == "nothing" and run["note"] == "No clip here is likely to earn enough." and len(engine.submitted) == 1
    assert "$1.50" in run["plan"]["moments"][0]["explain"][3]


def test_a_video_whose_kind_has_no_channel_is_not_made_and_the_missing_channel_is_asked_for(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch, jev=FakeJev(kinds={"stream": 0.95, "podcast": 0.02, "talk": 0.03}))
    engine = go(sv)
    run = runs.get(sv.db, 1)
    assert run["state"] == "nothing" and "no Stream clips channel yet" in run["note"]
    assert len(engine.submitted) == 1  # only the preview: nothing was made
    assert research.missing_channels(sv.db, sv.profiles) == [{"category": "Stream clips", "videos": 1}]


def test_a_video_that_fits_the_channel_too_loosely_is_not_made(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch, jev=FakeJev(fit=1.0))
    engine = go(sv)
    run = runs.get(sv.db, 1)
    assert run["state"] == "nothing" and "only 2 of 5" in run["note"] and len(engine.submitted) == 1


def test_switching_research_off_stops_a_video_that_was_already_being_looked_at(sv):
    switch_on(sv)
    queue_one(sv)
    research.start_next(sv, FakeFeed())
    research.save_settings(sv.db, sv.app.research, RULES, on=False)
    engine = FakeEngine()
    runs.analyse(sv, 1, engine)
    runs.analyse(sv, 1, engine)
    assert runs.get(sv.db, 1)["state"] == "nothing" and len(engine.submitted) == 1


def test_a_researched_clip_posts_with_the_creator_credited_and_a_pasted_one_does_not(sv):
    engine = go(sv)
    runs.collect(sv, 1, engine)
    with sv.db.conn() as c:
        captions = [r["caption"] for r in c.execute("SELECT caption FROM posts")]
    assert captions and all("Original: Episode 1 by A Show (https://www.youtube.com/watch?v=cc000000001), Creative Commons (CC BY)." in x for x in captions)
    pasted = runs.create(sv, "solved", "https://www.youtube.com/watch?v=aB3dEfGhIjK")
    assert research.credit_for(sv.db, "aB3dEfGhIjK") == "" and pasted
    assert "Original" not in posting.build_copy(sv.profiles["solved"].posting, {"hook": "h"}, "Ep", "youtube_shorts")["caption"]


def test_credit_names_the_stream_for_twitch_and_is_always_in_the_final_caption(sv):
    research._record(sv.db, {"video_id": TWITCH, "platform": "twitch", "url": TWITCH_URL, "title": "Late night", "channel": "SomeStreamer"},
                     state="started", reason=None, via="watch")
    assert research.credit_for(sv.db, TWITCH) == f"Original stream: SomeStreamer ({TWITCH_URL})."
    option = {"title": "A title", "idea": "An idea", "question": "A question?", "hashtags": ["#a", "#b"], "keywords": ["k"]}
    caption = copywriter.assemble(sv.profiles["solved"].posting, "youtube_shorts", "Episode", option, "Original stream: X (u).")["caption"]
    assert "Clip from: Episode\nOriginal stream: X (u)." in caption
    assert "Original" not in copywriter.assemble(sv.profiles["solved"].posting, "youtube_shorts", "Episode", option)["caption"]


# ---- the worker, the schedule and the pages --------------------------------------------------------------------------------


def test_the_research_job_is_scheduled_once_for_everything_and_logs_what_it_does(sv):
    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    with sv.db.conn() as c:
        row = c.execute("SELECT * FROM schedules WHERE name='research'").fetchone()
    assert row["kind"] == "research" and row["profile_id"] is None and row["interval_s"] == 30 * 60
    runner.HANDLERS["research"](sv, {"profile": None})  # off: does nothing
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM activity WHERE kind='research'").fetchone()["n"] == 0
    switch_on(sv)
    sv.yt = None  # no key and no channel: it says so
    runner.HANDLERS["research"](sv, {"profile": None})
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM activity WHERE kind='research' AND message LIKE '%nothing to look in%'").fetchone()["n"] == 1


@pytest.fixture
def web(sv):
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_the_research_page_shows_the_switch_what_it_looked_at_and_why(web):
    client, sv = web
    assert "Run Research" in client.get("/research").text and "Nothing looked at yet" in client.get("/research").text
    research._record(sv.db, {"video_id": "cc000000002", "platform": "youtube", "url": "u", "title": "A skipped talk", "channel": "A Show", "duration_s": 120, "views": 5},
                     state="skipped", reason="Skipped: not Creative Commons.", via="search", topic="podcast")
    text = client.get("/research").text
    assert "A skipped talk" in text and "not Creative Commons" in text and "search: podcast" in text
    assert client.get("/research?problem=Oops").text.count("Oops") == 1


def test_the_switch_the_limits_and_the_watch_list_are_set_on_the_page(web, monkeypatch):
    client, sv = web
    assert client.post("/research/settings", data={"on": "1"}, follow_redirects=False).status_code == 303 and research.is_on(sv.db)
    client.post("/research/settings", data={"videos_per_day": "4", "bar_max": "2", "topics": "interview"})
    assert research.settings(sv.db, sv.app.research) == {"on": True, "videos_per_day": 4, "bar_max": 2.0, "topics": ["interview"]}
    assert "Videos it may start" in client.get("/research").text
    assert "highest the bar" in client.post("/research/settings", data={"bar_max": "0.1"}, follow_redirects=True).text.lower()
    client.post("/research/settings", data={"on": "0"})
    assert not research.is_on(sv.db)
    monkeypatch.setattr(channel_feed, "resolve", lambda platform, text: channel_feed.Channel(platform, text.lower(), text))
    client.post("/research/sources/add", data={"platform": "twitch", "channel": "SomeStreamer"})
    [src] = research.sources(sv.db)
    assert (src["platform"], src["channel_key"], src["learned"]) == ("twitch", "somestreamer", 0) and "SomeStreamer" in client.get("/research").text
    monkeypatch.setattr(channel_feed, "resolve", lambda platform, text: (_ for _ in ()).throw(ValueError("That doesn't look like a Kick channel name.")))
    assert "doesn&#39;t look like a Kick" in client.post("/research/sources/add", data={"platform": "kick", "channel": "?"}, follow_redirects=True).text
    client.post(f"/research/sources/{src['id']}/remove")
    assert research.sources(sv.db) == []
    client.post("/research/look-now")
    with sv.db.conn() as c:
        assert c.execute("SELECT kind FROM tasks WHERE kind='research'").fetchone()


def test_home_asks_for_a_channel_when_research_found_a_kind_of_video_it_could_not_use(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch, jev=FakeJev(kinds={"stream": 0.95, "podcast": 0.02, "talk": 0.03}))
    go(sv)
    home = TestClient(create_app(sv), base_url="http://127.0.0.1:8780").get("/").text
    assert 'Create a channel for &#34;Stream clips&#34;' in home and "Research found 1 good video" in home


def test_a_campaign_video_is_never_taken_for_a_research_one(sv):
    campaigns.seed_solved(sv.db, RULES)
    assert research.active_runs(sv.db) == 0 and campaigns.active_runs(sv.db) == 0
