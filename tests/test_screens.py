"""The screens as drawn in the wireframes: Home's three ways in, the plan header, Campaigns, Research, Channels, Results and Rules."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_campaigns import SOLVED, FakeSite, detail, listed
from test_removal import clips_posted
from test_research import FakeYT, make_sv, switch_on

from villomillo import campaigns, config, research, runs, store
from villomillo.web.app import create_app

RULES = config.Scoring()


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch, yt=FakeYT())


@pytest.fixture
def client(sv):
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780")


def test_home_shows_the_three_ways_in_each_with_where_it_stands(sv, client):
    page = client.get("/").text
    assert "Three ways in" in page and "Nothing pasted right now" in page and "Switched off" in page and "0 joined · 0 worth joining" in page
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=aB3dEfGhIjK")
    switch_on(sv)
    campaigns.seed_solved(sv.db, RULES)
    page = client.get("/").text
    assert "1 analysing · 0 waiting for your Start" in page and "Bar $1.00 · 0 videos looked at today · 0 clipped" in page and "1 joined" in page
    assert "Paste · YouTube" in page  # each video row says how it arrived
    assert "Worker" in page and "Last post" in page and "Nothing posted yet" in page


def test_the_plan_header_says_how_the_video_arrived_how_long_it_took_and_the_look(sv, client):
    run_id, ids, _ = clips_posted(sv)
    page = client.get(f"/runs/{run_id}").text
    assert "Pasted link · YouTube" in page and "analysed in" in page
    sv2_run = runs.create(sv, "solved", "https://www.youtube.com/watch?v=bBcDeFgHiJk", source="research")
    assert "Found by Research · YouTube" in client.get(f"/runs/{sv2_run}").text


def test_the_plan_screen_names_the_look(sv, client):
    from test_runs import FakeEngine

    run_id = runs.create(sv, "solved", "https://www.youtube.com/watch?v=aB3dEfGhIjK")
    engine = FakeEngine()
    for _ in range(3):
        runs.analyse(sv, run_id, engine)
    page = client.get(f"/runs/{run_id}").text
    assert "Look: Clean frame." in page and "Start clipping 2 clips" in page


def test_campaigns_shows_the_connection_the_channel_each_goes_to_and_a_plain_status(sv, client):
    campaigns.seed_solved(sv.db, RULES)
    items = [listed(SOLVED, "Solved"), listed("44a3ffcb-d4a1-492b-94a4-704919950246", "Streamer highlights", category="Stream clips", rate=1.8),
             listed("22222222-3333-4444-8555-666666666666", "Cheap show", rate=0.4)]
    site = FakeSite(items, {SOLVED: detail(SOLVED, "Solved"), "44a3ffcb-d4a1-492b-94a4-704919950246": detail("44a3ffcb-d4a1-492b-94a4-704919950246", "Streamer highlights", youtube=180, category="Stream clips")})
    campaigns.scan(sv.db, RULES, site.client())
    page = client.get("/campaigns").text
    assert "Connection" in page and "Public campaign list: 3 campaigns read" in page and "<th>Goes to channel</th>" in page and "<th>Per 1,000 views</th>" in page
    assert "Joined by you" in page and "No channel for Stream clips" in page and "Worth joining: add the channel" in page and "Add the channel" in page
    assert "Skipped: it pays $0.40" in page
    one = client.get(f"/campaigns/{SOLVED}").text
    assert "How it will clip" in one and "20 to 60 seconds" in one and "a face in the first 5 seconds" in one and "no ad reads" in one and "budget is read again" in one


def test_research_shows_the_bar_card_todays_numbers_where_it_looks_and_results_with_cpm(sv, client):
    switch_on(sv, bar_max="2")
    research.set_bar(sv.db, 1.1)
    page = client.get("/research").text
    assert "The bar right now" in page and "$1.10" in page and "Ceiling you set: $2.00" in page and "YouTube search allowance" in page and "Where it looks" in page
    assert "Creative Commons videos only" in page and "key twitch" in page and "no official search" in page
    run_id, ids, _ = clips_posted(sv)  # a finished video, turned into a Research candidate with one post out
    with sv.db.conn() as c:
        c.execute("UPDATE runs SET source='research'")
        c.execute("UPDATE posts SET status='uploaded', url='u', posted_at=? WHERE clip_id=?", (store.iso(), ids[0]))
    research._record(sv.db, {"video_id": "aB3dEfGhIjK", "platform": "youtube", "url": "u", "title": "Interview", "channel": "A Show", "duration_s": 3000, "views": 5000},
                     state="started", reason=None, via="search", topic="podcast")
    with sv.db.conn() as c:
        c.execute("UPDATE research_candidates SET run_id=?", (run_id,))
    page = client.get("/research").text
    assert "<th>Est. CPM</th>" in page and "$1.16" in page and "Clipped: 1 posted, 1 planned, credited" in page
    assert "1 clipped" in page


def test_a_researched_video_that_found_nothing_shows_why_in_the_result(sv, client):
    research._record(sv.db, {"video_id": "cc000000002", "platform": "youtube", "url": "u", "title": "Rant", "channel": "A", "duration_s": 600, "views": 9},
                     state="skipped", reason="Skipped: not Creative Commons, so it cannot be used with credit.", via="search", topic="x")
    page = client.get("/research").text
    assert "Skipped: not Creative Commons" in page and "3" not in page.split("Today")[0][-3:]  # nothing odd before the numbers
    assert "1 not Creative Commons" in page


def test_channel_cards_show_posts_today_the_look_the_tags_and_a_pause_button(sv, client):
    page = client.get("/channels").text
    assert "0 of 100 posted today" in page and "Clean frame" in page and "Pause this channel" in page and "Caption tags" in page
    from villomillo import accounts

    accounts.pause(sv.db, "solved", "youtube_shorts", "paused by you")
    assert "Resume this channel" in client.get("/channels").text
    new = client.get("/channels/new").text
    assert "1. Name and category" in new and "2. Connect YouTube" in new and "3. Caption tags and look" in new


def test_the_results_screen_has_the_cards_the_clips_and_the_skipped_moments(sv, client):
    run_id, ids, _ = clips_posted(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE posts SET status='uploaded', url='https://youtube.com/shorts/x', posted_at=? WHERE clip_id=?", (store.iso(), ids[0]))
        post = c.execute("SELECT id FROM posts WHERE clip_id=?", (ids[0],)).fetchone()["id"]
        c.execute("INSERT INTO post_stats (post_id, taken_at, views) VALUES (?,?,?)", (post, store.iso(), 3100))
        c.execute("UPDATE clips_ready SET campaign_id=?", (SOLVED,))
    campaigns.seed_solved(sv.db, RULES)
    page = client.get("/results").text
    assert "Posted today" in page and ">1<" in page and "Real views so far" in page and "3,100" in page
    assert "3,100 of 8,000" in page and "YouTube · link ready to submit" in page and "Paste" in page and "Podcasts" in page
    assert "Not made" in page and "Skipped: the viral score 68 is under 75" in page
    assert "Needs you: the only four kinds" in page
    assert 'href="/results?profile=solved"' in client.get("/").text  # in the sidebar under Learn


def test_the_results_screen_counts_held_clips_and_works_when_empty(sv, client):
    assert "No clips yet" in client.get("/results").text
    run_id, ids, _ = clips_posted(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE posts SET status='cancelled'")
        c.execute("UPDATE clips_ready SET category='Sports', reason='approved, but not posted: no channel for the category Sports'")
    page = client.get("/results").text
    assert "Held: no channel yet" in page and '<div class="v num">2</div><div class="s">they post when you add the channel' in page


def test_the_rules_screen_shows_the_live_numbers_and_is_linked_from_settings(sv, client):
    research.set_bar(sv.db, 1.4)
    page = client.get("/rules").text
    assert "Scoring and safety rules" in page and "75</b> or more" in page and "now $1.40" in page and "$25 of Claude or $5 of Jev" in page
    assert "100 posts a day" in page and "Emergency stop" in page and "4 of 5" in page
    assert 'href="/rules?profile=solved"' in client.get("/settings").text


def test_nothing_is_left_in_the_old_sidebar_groups(client):
    page = client.get("/").text
    for name in ("Find", "Make", "Post", "Learn", "Research", "Campaigns", "Results", "Insights"):
        assert name in page
