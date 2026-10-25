"""The live activity log: what each step writes, the video timeline, stalls, waiting for the engine, the feeds and the Now panel."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_removal import clips_posted
from test_research import FakeFeed, FakeYT, make_sv, queue_one, switch_on
from test_runs import LINK, FakeEngine

from villomillo import (
    activity,
    campaigns,
    clipper,
    engine_manager,
    research,
    runner,
    runs,
    spend,
    store,
    tasks,
)
from villomillo.web.app import create_app


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch, yt=FakeYT())


class LogEngine(FakeEngine):
    def __init__(self, *a, log_text="", **k):
        super().__init__(*a, **k)
        self.log_text = log_text

    def log(self, job_id):
        return self.log_text


def messages(sv, run_id):
    return [x["message"] for x in activity.for_run(sv.db, run_id)]


def test_a_repeated_line_is_written_once_and_old_lines_are_pruned(sv):
    for _ in range(3):
        activity.log(sv.db, "research", "Pass finished: nothing waiting.")
    activity.log(sv.db, "research", "Searched.")
    activity.log(sv.db, "research", "Pass finished: nothing waiting.")
    assert [x["message"] for x in activity.recent(sv.db, "research")] == ["Pass finished: nothing waiting.", "Searched.", "Pass finished: nothing waiting."]
    assert activity.for_run(sv.db, 7) == []
    with sv.db.conn() as c:
        c.execute("UPDATE activity SET ts=? WHERE message='Searched.'", (store.iso(store.now() - timedelta(days=20)),))
    activity.prune(sv.db)
    assert [x["message"] for x in activity.recent(sv.db, "research")].count("Searched.") == 0


def test_a_pasted_video_writes_every_step_and_the_timeline_follows_it(sv):
    engine = LogEngine(statuses=["running", "running"], log_text="Downloading video\nTranscribing")
    run_id = runs.create(sv, "solved", LINK)
    assert messages(sv, run_id) == ["Link accepted (YouTube). Waiting for the engine to start on it."]
    runs.analyse(sv, run_id, engine)
    runs.analyse(sv, run_id, engine)  # running: the stage is read from the engine's log
    assert messages(sv, run_id)[-1] == "Transcribing the speech"
    tl = activity.timeline(sv.db, runs.get(sv.db, run_id))
    assert [s["status"] for s in tl["steps"]][:2] == ["now", "waiting"] and tl["now"] == "Transcribing the speech" and tl["active"]
    runs.analyse(sv, run_id, engine)
    runs.analyse(sv, run_id, engine)
    got = messages(sv, run_id)
    assert any("The engine found 3 possible clips" in m for m in got) and any(m.startswith("Scored: 2 of 3 clips clear the floors") for m in got)
    assert got[-1] == "Waiting for you to press Start."
    tl = activity.timeline(sv.db, runs.get(sv.db, run_id))
    assert [s["status"] for s in tl["steps"]] == ["done", "done", "now", "waiting", "waiting", "waiting"]
    runs.start(sv, run_id, engine)
    assert "Asked the engine to make 2 clips" in messages(sv, run_id)[-1]
    runs.collect(sv, run_id, engine)
    got = messages(sv, run_id)
    assert any(m.endswith("passed every check.") for m in got) and sum("planned on Podcasts" in m for m in got) == 2 and got[-1].startswith("Done. 2 planned")
    tl = activity.timeline(sv.db, runs.get(sv.db, run_id))
    assert [s["status"] for s in tl["steps"]] == ["done"] * 6 and tl["now"] == "Done" and not tl["active"]


def test_research_and_campaign_videos_write_under_their_own_kind(sv):
    switch_on(sv)
    queue_one(sv)
    research.start_next(sv, FakeFeed())
    engine = FakeEngine()
    for _ in range(3):
        runs.analyse(sv, 1, engine)
    kinds = {x["kind"] for x in activity.for_run(sv.db, 1)}
    assert kinds == {"research"} and any("Going ahead without a Start press" in m for m in messages(sv, 1))
    assert any(x["run_id"] == 1 for x in activity.recent(sv.db, "research"))


def test_an_engine_that_is_not_answering_makes_the_video_wait_not_fail_and_it_says_so(sv):
    class Down(FakeEngine):
        def job(self, job_id):
            raise clipper.EngineUnavailable("the clipping engine is not running at http://127.0.0.1:8765")

    run_id = runs.create(sv, "solved", LINK)
    down = Down()
    runs.analyse(sv, run_id, down)  # starts the preview (submit works), then waits
    runs.analyse(sv, run_id, down)
    assert runs.get(sv.db, run_id)["state"] == "analysing"
    assert messages(sv, run_id)[-1] == "Waiting: the clip engine is not answering. This video carries on when it is back."
    with sv.db.conn() as c:
        c.execute("UPDATE runs SET updated_at=?", (store.iso(store.now() - timedelta(minutes=45)),))
    runs.analyse(sv, run_id, down)
    run = runs.get(sv.db, run_id)
    assert run["state"] == "failed" and "not answering for 30 minutes" in run["error"] and messages(sv, run_id)[-1].startswith("Failed:")


def test_a_video_with_no_news_is_flagged_and_so_is_a_waiting_engine(sv):
    run_id = runs.create(sv, "solved", LINK)
    run = runs.get(sv.db, run_id)
    assert activity.timeline(sv.db, run)["stalled"] is None
    with sv.db.conn() as c:
        c.execute("UPDATE activity SET ts=?", (store.iso(store.now() - timedelta(minutes=14)),))
    assert "No news for 14 minutes" in activity.timeline(sv.db, run)["stalled"]
    assert "engine is off" in activity.timeline(sv.db, run, "off")["stalled"]
    assert "starting up" in activity.timeline(sv.db, run, "starting")["stalled"]


def test_a_spending_pause_is_written_on_the_video(sv):
    run_id = runs.create(sv, "solved", LINK)

    def pause(sv_, rid):
        raise spend.SpendPaused(spend.Pause("claude", "Claude spending is paused", "text", "bad"))

    runner._paste_step(pause, "paste_analyse")(sv, {"run": run_id})
    assert messages(sv, run_id)[-1].startswith("Paused: spending reached its limit")


def test_research_and_campaign_passes_write_to_their_feed(sv):
    switch_on(sv)
    research.work(sv, yt=sv.yt, feed=FakeFeed())
    feed = [x["message"] for x in activity.recent(sv.db, "research")]
    assert feed[0] == "Pass finished: nothing waiting." and any("Searched YouTube" in m for m in feed)
    campaigns.work(sv, client=__import__("test_campaigns").FakeSite([], {}).client())
    lines = [x["message"] for x in activity.recent(sv.db, "campaign")]
    assert any("could not be read" in m or "campaigns on the public list" in m for m in lines)
    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    assert activity.next_pass(sv.db, "research") == 0  # due now
    tasks.tick_schedules(sv.db)
    assert activity.next_pass(sv.db, "research") in range(29, 32) and activity.next_pass(sv.db, "nothing") is None


def test_the_pages_show_the_timeline_the_feeds_and_the_now_panel(sv):
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    run_id = runs.create(sv, "solved", LINK)
    page = client.get(f"/runs/{run_id}").text
    assert "What is happening" in page and "Now:" in page and "Look at the video" in page and "Link accepted" in page and 'data-live-url="/runs/1/live' in page
    live = client.get(f"/runs/{run_id}/live")
    assert live.headers["X-State"] == "analysing" and "Now:" in live.text and "<html" not in live.text
    assert client.get("/runs/99/live").status_code == 404
    home = client.get("/").text
    assert ">Now<" in home and "Worker" in home and "Not running" in home and "Paste video" in home and "Link accepted" in home and "Analysing" in home
    assert "Three ways in" in home and "waiting for your Start" in home and "Research" in home and "Switched off" in home and "Last post" in home
    assert "Clip engine" in client.get("/now").text
    switch_on(sv)
    research.work(sv, yt=sv.yt, feed=FakeFeed())
    assert "Pass finished" in client.get("/research").text and "Pass finished" in client.get("/research/feed").text
    assert "Latest activity" in client.get("/campaigns").text and client.get("/campaigns/feed").status_code == 200


def test_the_now_panel_names_a_stuck_or_running_worker_and_the_engine(sv, monkeypatch):
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    from villomillo import health

    health.beat(sv.db, "paste_analyse")
    text = client.get("/now").text
    assert "Working on paste_analyse" in text and "Clip engine" in text
    monkeypatch.setattr(engine_manager.manager, "_probe", lambda: True)
    engine_manager.manager.check(force=True)
    assert "On" in client.get("/now").text


def test_a_finished_video_page_says_what_happened(sv):
    run_id, ids, _ = clips_posted(sv)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    page = client.get(f"/runs/{run_id}").text
    assert "What happened" in page and "planned on Podcasts" in page and 'data-live-url' not in page.split("Timeline")[1].split("</section>")[0]
