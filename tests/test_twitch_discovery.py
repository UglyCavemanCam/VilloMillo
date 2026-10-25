"""Twitch and Kick channel discovery: the Twitch client, finding channels by topic, and adding them to the watch list. No network."""

from __future__ import annotations

import httpx
import pytest
from test_research import FakeFeed, FakeYT, make_sv, switch_on

from villomillo import cli, config, research, secrets
from villomillo.twitch import Twitch, TwitchError


class FakeTwitch:
    def __init__(self, categories=None, streams=None, search=None, error=None):
        self.cats, self.streams, self.search, self.error, self.calls = categories or {}, streams or {}, search or [], error, []

    def categories(self, query, first=3):
        self.calls.append(("categories", query))
        if self.error:
            raise self.error
        return self.cats.get(query, [])

    def top_streams(self, game_id=None, first=10):
        self.calls.append(("streams", game_id))
        return self.streams.get(game_id, [])

    def search_channels(self, query, first=10):
        self.calls.append(("search", query))
        return self.search


def live(login, viewers, name=None):
    return {"login": login, "name": name or login.title(), "viewers": viewers, "game": "Just Chatting", "title": "t"}


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch, yt=FakeYT())


# ---- the client ------------------------------------------------------------------------------------------------------------


def twitch_over(handler):
    return Twitch("the-id", "the-secret", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_the_client_signs_in_once_reads_public_data_and_never_shows_the_secret():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "id.twitch.tv":
            assert b"client_secret=the-secret" in request.content and b"client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok" and request.headers["Client-Id"] == "the-id"
        if request.url.path.endswith("/search/categories"):
            return httpx.Response(200, json={"data": [{"id": "509658", "name": "Just Chatting"}]})
        if request.url.path.endswith("/streams"):
            return httpx.Response(200, json={"data": [{"user_login": "BigStreamer", "user_name": "BigStreamer", "viewer_count": 9000, "game_name": "Just Chatting", "title": "hi"}]})
        return httpx.Response(200, json={"data": [{"broadcaster_login": "Chatty", "display_name": "Chatty", "game_name": "Talk", "title": "x"}]})

    t = twitch_over(handler)
    assert t.categories("chatting") == [{"id": "509658", "name": "Just Chatting"}]
    assert t.top_streams("509658") == [{"login": "bigstreamer", "name": "BigStreamer", "viewers": 9000, "game": "Just Chatting", "title": "hi"}]
    assert t.search_channels("talk")[0]["login"] == "chatty"
    assert sum(1 for r in seen if r.url.host == "id.twitch.tv") == 1  # one sign-in serves all three calls


def test_a_refused_key_and_an_expired_token_are_handled_without_leaking_anything():
    bad = twitch_over(lambda r: httpx.Response(400, json={"message": "invalid client secret"}))
    with pytest.raises(TwitchError) as e:
        bad.categories("x")
    assert "refused the Client ID and Secret" in str(e.value) and "the-secret" not in str(e.value) and "key twitch" in str(e.value)
    state = {"tokens": 0}

    def handler(request):
        if request.url.host == "id.twitch.tv":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"tok{state['tokens']}", "expires_in": 3600})
        return httpx.Response(401) if request.headers["Authorization"] == "Bearer tok1" else httpx.Response(200, json={"data": []})

    assert twitch_over(handler).categories("x") == [] and state["tokens"] == 2  # signed in again once after the 401

    def down(request):
        raise httpx.ConnectError("no route to http://secret-url")

    with pytest.raises(TwitchError) as e:
        twitch_over(down).categories("x")
    assert "secret-url" not in str(e.value)


# ---- discovery -------------------------------------------------------------------------------------------------------------


def test_twitch_discovery_adds_the_busiest_live_channels_in_a_topics_category(sv):
    switch_on(sv)
    tw = FakeTwitch(categories={"podcasts": [{"id": "g1", "name": "Podcasts"}]},
                    streams={"g1": [live("bigshow", 5200, "BigShow"), live("tiny", 40), live("midshow", 800)]})
    lines = research.discover(sv, yt=None, feed=FakeFeed(), twitch=tw)
    got = {s["channel_key"]: s for s in research.sources(sv.db)}
    assert set(got) == {"bigshow", "midshow"} and all(s["learned"] and s["platform"] == "twitch" for s in got.values())
    assert any("Added BigShow (Twitch, 5,200 watching)" in line for line in lines) and any("2 added to the watch list" in line for line in lines)
    assert tw.calls == [("categories", "podcasts"), ("streams", "g1")]


def test_a_topic_with_no_category_falls_back_to_channel_search_and_rotates(sv):
    switch_on(sv)
    research.save_settings(sv.db, sv.app.research, config.Scoring(), topics="alpha\nbeta")
    tw = FakeTwitch(search=[live("someone", 0)])
    research.discover(sv, yt=None, feed=FakeFeed(), twitch=tw)
    assert ("search", "alpha") in tw.calls and "someone" in {s["channel_key"] for s in research.sources(sv.db)}
    tw.calls.clear()
    research.discover(sv, yt=None, feed=FakeFeed(), twitch=tw)
    assert tw.calls[0] == ("categories", "beta")  # the topic looked up longest ago goes next


def test_a_twitch_failure_is_reported_and_the_rest_of_the_pass_goes_on(sv):
    switch_on(sv)
    lines = research.discover(sv, yt=None, feed=FakeFeed(), twitch=FakeTwitch(error=TwitchError("Twitch refused the Client ID and Secret (HTTP 400).")))
    assert any(line.startswith('Twitch lookup for "podcasts" failed: Twitch refused') for line in lines)


def test_kick_search_adds_channels_reads_odd_answers_safely_and_reports_a_dead_endpoint(sv):
    switch_on(sv)
    answer = {"channels": [{"slug": "KickPod", "user": {"username": "Kick Pod"}}, {"channel": {"slug": "second", "username": "Second"}}, "junk", {"nothing": 1}]}
    lines = research.discover(sv, yt=None, feed=FakeFeed(), kick_get=lambda url: answer)
    assert {(s["platform"], s["channel_key"], s["name"]) for s in research.sources(sv.db)} == {("kick", "kickpod", "Kick Pod"), ("kick", "second", "Second")}
    assert any('Looked up "podcasts" on Kick: 2 channels, 2 added' in line for line in lines)

    def dead(url):
        raise ValueError("Cloudflare says no")

    lines = research.discover(sv, yt=None, feed=FakeFeed(), kick_get=dead)
    assert any("Kick search" in line and "did not answer" in line and "no official search" in line for line in lines)
    assert research._kick_search("x", lambda url: ["not", "a", "dict"]) == []


def test_found_channels_are_capped_marked_and_can_be_removed_and_yours_are_never_replaced(sv):
    for n in range(research.LEARNED_MAX):
        assert research._learn_channel(sv.db, "twitch", f"c{n}", f"C{n}")
    assert not research._learn_channel(sv.db, "twitch", "overflow", "Overflow")  # the list is full
    assert not research._learn_channel(sv.db, "twitch", "c1", "C1")  # already there
    mine = research.add_source(sv.db, "kick", "MyStreamer", resolver=lambda p, t: type("C", (), {"platform": p, "channel_key": "mystreamer", "name": t})())
    assert not mine["learned"]
    assert not research._learn_channel(sv.db, "kick", "mystreamer", "MyStreamer")  # yours stays yours
    assert sum(1 for s in research.sources(sv.db) if s["learned"]) == research.LEARNED_MAX
    research.remove_source(sv.db, next(s["id"] for s in research.sources(sv.db) if s["channel_key"] == "c0"))
    assert research._learn_channel(sv.db, "twitch", "overflow", "Overflow")


def test_work_uses_the_services_twitch_and_discovery_can_be_turned_off(sv):
    switch_on(sv)
    sv.twitch = FakeTwitch(categories={"podcasts": [{"id": "g", "name": "P"}]}, streams={"g": [live("big", 999)]})
    research.work(sv, yt=sv.yt, feed=FakeFeed())
    assert "big" in {s["channel_key"] for s in research.sources(sv.db)}
    sv.app.research.discover_per_pass = 0
    sv.twitch.calls.clear()
    research.work(sv, yt=sv.yt, feed=FakeFeed())
    assert sv.twitch.calls == []


# ---- the key command -------------------------------------------------------------------------------------------------------


def test_the_twitch_key_command_stores_both_values_without_printing_them(monkeypatch, capsys):
    stored = {}
    answers = iter(["the-client-id", "the-client-secret"])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(secrets, "store", lambda name, value: stored.update({name: value}))
    cli.cmd_key(type("A", (), {"name": "twitch"})())
    out = capsys.readouterr().out
    assert stored == {secrets.TWITCH_ID: "the-client-id", secrets.TWITCH_SECRET: "the-client-secret"}
    assert "the-client-secret" not in out and "the-client-id" not in out and "Stored the Twitch" in out
    monkeypatch.setattr(secrets, "is_stored", lambda name: name in (secrets.TWITCH_ID, secrets.TWITCH_SECRET))
    cli.cmd_key(type("A", (), {"name": None})())
    assert "twitch   stored" in capsys.readouterr().out


def test_discovery_stamps_do_not_collide_with_youtube_searches(sv):
    switch_on(sv)
    research.discover(sv, yt=None, feed=FakeFeed(), twitch=FakeTwitch(), kick_get=lambda url: {})
    with sv.db.conn() as c:
        assert {r["topic"] for r in c.execute("SELECT topic FROM research_searches")} == {"twitch:podcasts", "kick:podcasts"}
