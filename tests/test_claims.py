"""One video, one owner: Paste, Research and Campaigns never take a video another way has, and you are asked before a clipped video is clipped again."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from test_campaigns import SOLVED, FakeSite, detail
from test_campaigns import FakeYT as CampaignYT
from test_campaigns import vid as campaign_vid
from test_removal import clips_posted
from test_research import FakeFeed, make_sv, queue_one, switch_on
from test_runs import LINK

from villomillo import campaigns, claims, config, research, runs
from villomillo.web.app import create_app

RULES = config.Scoring()
VIDEO = "aB3dEfGhIjK"


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch, yt=CampaignYT([campaign_vid(1), campaign_vid(2)]))


def joined(sv, sources="@show"):
    campaigns.seed_solved(sv.db, RULES)
    campaigns.save_setup(sv.db, SOLVED, sources=sources, tags="@solvedclips", use_rules=True, min_s="20", max_s="60", face_window_s="5", require_captions=True, no_ad_reads=True)
    return campaigns.get(sv.db, SOLVED)


def site():
    return FakeSite([], {SOLVED: detail(SOLVED, "Solved")}).client()


# ---- Paste -------------------------------------------------------------------------------------------------------------------


def test_the_same_video_pasted_twice_is_one_run(sv):
    first = runs.create(sv, "solved", LINK)
    assert runs.create(sv, "solved", "youtu.be/" + VIDEO) == first


def test_pasting_a_clipped_video_asks_first_and_clip_again_makes_a_new_run(sv):
    run_id, ids, _ = clips_posted(sv)
    with pytest.raises(runs.AlreadyClipped) as e:
        runs.create(sv, "solved", LINK)
    assert e.value.why == "You already clipped this video" and e.value.run["id"] == run_id
    again = runs.create(sv, "solved", LINK, redo=True)
    assert again != run_id and runs.get(sv.db, again)["state"] == "analysing"


def test_pasting_a_video_that_found_nothing_or_failed_or_was_removed_just_works(sv):
    first = runs.create(sv, "solved", LINK)
    runs._set(sv.db, first, state="nothing")
    second = runs.create(sv, "solved", LINK)
    assert second != first
    runs.fail(sv, second, "boom")
    third = runs.create(sv, "solved", LINK)
    assert third not in (first, second)
    runs._set(sv.db, third, state="removed")
    assert runs.create(sv, "solved", LINK) not in (first, second, third)


def test_pasting_a_video_research_is_working_on_shows_you_that_run(sv):
    switch_on(sv)
    queue_one(sv, 1)
    research.start_next(sv, FakeFeed())
    run = runs.get(sv.db, 1)
    assert runs.create(sv, "solved", "https://www.youtube.com/watch?v=" + run["video_id"]) == 1


# ---- Research ----------------------------------------------------------------------------------------------------------------


def test_research_leaves_a_video_you_pasted_and_says_why(sv):
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=cc000000001")  # you pasted it
    switch_on(sv)
    research._record(sv.db, research._youtube_candidate(__import__("test_research").vid(1), research.store.now()), state="queued", reason=None, via="watch", rank=9.0)
    assert research.start_next(sv, FakeFeed()) == []
    cand = research.candidates(sv.db)[0]
    assert cand["state"] == "skipped" and cand["reason"] == "Skipped: you are already working on this video."
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1


def test_research_does_not_even_queue_a_video_a_campaign_has_queued_or_that_has_a_run(sv):
    joined(sv)
    campaigns.fill_queue(sv.db, campaigns.get(sv.db, SOLVED), sv.yt)  # vid00000001 and vid00000002 are the campaign's
    assert research._seen(sv.db, "vid00000001") and not research._seen(sv.db, "cc000000042")
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=cc000000042")
    assert research._seen(sv.db, "cc000000042")


# ---- Campaigns ---------------------------------------------------------------------------------------------------------------


def test_a_campaign_skips_a_video_you_pasted_and_goes_on_to_its_next(sv):
    c = joined(sv)
    campaigns.fill_queue(sv.db, c, sv.yt)
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000001")  # you pasted the campaign's first video
    lines = campaigns.start_next(sv, site())
    assert any("skipped Episode 1: you are already working on this video." in x for x in lines) and any("started Episode 2" in x for x in lines)
    queue = {q["video_id"]: q for q in campaigns.queue_of(sv.db, SOLVED)}
    assert queue["vid00000001"]["skipped_reason"] == "Skipped: you are already working on this video." and queue["vid00000001"]["run_id"] is None
    assert queue["vid00000002"]["run_id"] is not None
    with sv.db.conn() as conn:  # the pasted run stays yours
        assert conn.execute("SELECT source FROM runs WHERE video_id='vid00000001'").fetchone()["source"] == "paste"


def test_a_skipped_queue_item_does_not_take_a_slot_or_come_back(sv):
    c = joined(sv)
    campaigns.fill_queue(sv.db, c, sv.yt)
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000001")
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000002")
    lines = campaigns.start_next(sv, site())
    assert sum("skipped" in x for x in lines) == 2
    assert campaigns._queued(sv.db, SOLVED) == 0
    assert campaigns.start_next(sv, site()) == []  # nothing left to start


def test_a_campaign_skips_a_video_research_already_clipped_and_one_another_campaign_has(sv):
    c = joined(sv)
    campaigns.fill_queue(sv.db, c, sv.yt)
    first = runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000001", source="research")
    runs._set(sv.db, first, state="finished")
    other = runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000002", source="campaign", campaign_id=SOLVED)
    runs._set(sv.db, other, state="finished")  # clipped for the campaign already (this or another one)
    text = " ".join(campaigns.start_next(sv, site()))
    assert "Research already clipped it" in text and 'the campaign "Solved" already clipped it' in text


def test_the_queue_page_shows_why_a_video_was_skipped(sv):
    c = joined(sv)
    campaigns.fill_queue(sv.db, c, sv.yt)
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000001")
    campaigns.start_next(sv, site())
    page = TestClient(create_app(sv), base_url="http://127.0.0.1:8780").get(f"/campaigns/{SOLVED}").text
    assert "Skipped" in page and "you are already working on this video." in page


def test_a_video_already_owned_is_not_queued_for_a_campaign(sv):
    c = joined(sv)
    runs.create(sv, "solved", "https://www.youtube.com/watch?v=vid00000001")
    campaigns.fill_queue(sv.db, c, sv.yt)
    assert [q["video_id"] for q in campaigns.queue_of(sv.db, SOLVED)] == ["vid00000002"]


# ---- the rule itself ----------------------------------------------------------------------------------------------------------


def test_a_failed_or_removed_run_frees_the_video_for_the_automatic_ways(sv):
    first = runs.create(sv, "solved", LINK)
    with pytest.raises(runs.Claimed):
        runs.create(sv, "solved", LINK, source="research")
    runs.fail(sv, first, "boom")
    second = runs.create(sv, "solved", LINK, source="research")
    runs._set(sv.db, second, state="removed")
    assert runs.create(sv, "solved", LINK, source="campaign", campaign_id=SOLVED)


def test_a_video_that_found_nothing_is_not_tried_again_by_the_automatic_ways(sv):
    first = runs.create(sv, "solved", LINK, source="research")
    runs._set(sv.db, first, state="nothing")
    with pytest.raises(runs.Claimed) as e:
        runs.create(sv, "solved", LINK, source="campaign", campaign_id=SOLVED)
    assert "Research already looked at it and nothing in it was worth making" == e.value.why


def test_two_ways_in_starting_the_same_video_at_the_same_moment_only_one_wins(sv):
    results, errors = [], []

    def go(source):
        try:
            results.append(runs.create(sv, "solved", LINK, source=source))
        except runs.Claimed:
            errors.append(source)

    threads = [threading.Thread(target=go, args=(s,)) for s in ("research", "campaign", "research", "campaign", "research", "campaign")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM runs WHERE video_id=?", (VIDEO,)).fetchone()["n"] == 1
    assert len(results) == 1 and len(errors) == 5


def test_the_owner_is_described_in_plain_words(sv):
    for source, state, expected in [("paste", "analysing", "you are already working on this video"), ("paste", "finished", "you already clipped this video"),
                                    ("research", "making", "Research is already working on it"), ("research", "finished", "Research already clipped it"),
                                    ("research", "nothing", "Research already looked at it and nothing in it was worth making")]:
        assert claims.phrase(sv.db, {"source": source, "state": state}) == expected


# ---- the pages -----------------------------------------------------------------------------------------------------------------


def test_pasting_a_clipped_video_shows_a_clip_it_again_button_and_pressing_it_starts_a_new_run(sv):
    run_id, ids, _ = clips_posted(sv)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    answer = client.post("/paste", data={"link": LINK, "profile": "solved"}, follow_redirects=True)
    assert "You already clipped this video." in answer.text and "Clip it again" in answer.text and 'name="redo" value="1"' in answer.text
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1
    answer = client.post("/paste", data={"link": LINK, "profile": "solved", "redo": "1"}, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"].startswith("/runs/2")


def test_pasting_a_video_a_campaign_is_working_on_takes_you_to_it_with_a_note(sv):
    c = joined(sv)
    campaigns.fill_queue(sv.db, c, sv.yt)
    campaigns.start_next(sv, site())
    run = runs.get(sv.db, 1)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    answer = client.post("/paste", data={"link": run["url"], "profile": "solved"}, follow_redirects=True)
    assert "is already working on it. Here it is." in answer.text and "Campaign video" in answer.text
