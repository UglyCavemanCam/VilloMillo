"""A pasted link from start to finish: analyse, plan, Start, make, check, route; plus the emergency stop and the pages."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from villomillo import (
    channels,
    clipper,
    config,
    links,
    routing,
    runner,
    runs,
    safety,
    scoring,
    spend,
)
from villomillo.db import Database
from villomillo.jev import JevResult
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

LINK = "https://www.youtube.com/watch?v=aB3dEfGhIjK"
VIDEO = "aB3dEfGhIjK"
TEXT = "Most people think discipline is about willpower. It is not. It is about designing your day so you never need it."


def moment(i, score, text=TEXT):
    return {"id": i, "start": 100.0 * i, "end": 100.0 * i + 30, "duration": 30, "score": score, "hook": f"hook {i}", "text": text + f" Point {i}."}


class FakeJev:
    def __init__(self, kinds=None, fit=3.5, hook=3.0, alone=0.9):
        self.kinds = kinds or {"podcast": 0.95, "stream": 0.02, "talk": 0.03}
        self.fit, self.hook, self.alone, self.calls = fit, hook, alone, 0

    def ask(self, state, questions):
        self.calls += 1
        out = {}
        for name in questions:
            if name in ("hook", "payoff"):
                out[name] = {"type": "score", "score": self.hook}
            elif name == "fit":
                out[name] = {"type": "score", "score": self.fit}
            elif name in self.kinds:
                out[name] = {"type": "noul", "noul": self.kinds[name]}
            else:  # stands_alone, self_contained
                out[name] = {"type": "noul", "noul": 0.02 if name == "ad_read" else self.alone}
        return JevResult("jev-test", out, {"input_tokens": 100})


class FakeEngine:
    """Scripted: jobs report the statuses queued for them, then 'done'."""

    def __init__(self, moments=None, statuses=None):
        self.preview_data = {"video": {"title": "Episode 12", "channel": "A Show", "duration": 3600},
                             "moments": moments if moments is not None else [moment(1, 91), moment(2, 86), moment(3, 60)]}
        self.statuses = list(statuses or [])
        self.submitted, self.discarded, self.next_id = [], [], 10
        self.error = ""

    def submit(self, url, **kw):
        self.next_id += 1
        self.submitted.append({"url": url, **kw})
        return self.next_id

    def job(self, job_id):
        status = self.statuses.pop(0) if self.statuses else "done"
        return {"status": status, "error": self.error}

    def preview(self, video_id):
        return self.preview_data

    def discard(self, video_id):
        self.discarded.append(video_id)

    def clips(self, video_id):
        return [{"id": 100 + m, "start_s": 100.0 * m, "end_s": 100.0 * m + 30, "score": 90 - m, "hook": f"hook {m}"}
                for m in self.submitted[-1]["moments"]]

    def media(self, clip_id):
        return b"clip"

    def captions_text(self, clip_id):
        return TEXT


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = Database(tmp_path / "runs.db")
    with db.conn() as c:  # the "every clip to one channel" switch is on by default; this test file is about category channels
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('catch_all', '{\"on\": false}')")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    monkeypatch.setattr(clipper.validate, "check", lambda path, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    monkeypatch.setattr("villomillo.formats.formatter_for", lambda *a, **k: None)  # these tests are not about the look: the engine's own render is used
    sv = Services(config.AppConfig(), channels.load(db), db, Quota(db, general_cap=9, search_cap=9), None, FakeJev())
    return sv, db


def to_plan(sv, engine=None, link=LINK):
    engine = engine or FakeEngine()
    run_id = runs.create(sv, "solved", link)
    for _ in range(4):
        if runs.get(sv.db, run_id)["state"] != "analysing":
            break
        runs.analyse(sv, run_id, engine)
    return run_id, engine


# ---- analyse --------------------------------------------------------------------------------------------------


def test_a_pasted_link_becomes_a_run_and_the_same_video_is_not_started_twice(env):
    sv, db = env
    first = runs.create(sv, "solved", LINK)
    assert runs.create(sv, "solved", "youtu.be/" + VIDEO) == first
    with pytest.raises(links.LinkError):
        runs.create(sv, "solved", "https://example.com/video")
    with db.conn() as c:
        assert c.execute("SELECT kind FROM tasks").fetchone()["kind"] == "paste_analyse"


def test_analysing_asks_the_engine_waits_and_then_makes_the_plan(env):
    sv, db = env
    engine = FakeEngine(statuses=["running"])
    run_id = runs.create(sv, "solved", LINK)
    runs.analyse(sv, run_id, engine)  # submits the preview
    assert engine.submitted[0]["preview"] is True and runs.get(db, run_id)["state"] == "analysing"
    runs.analyse(sv, run_id, engine)  # still running: looks again later
    assert runs.get(db, run_id)["state"] == "analysing"
    runs.analyse(sv, run_id, engine)  # done
    run = runs.get(db, run_id)
    plan = run["plan"]
    assert run["state"] == "planned" and run["title"] == "Episode 12"
    assert plan["category"] == "Podcasts" and plan["look"] == "podcast_talk" and plan["channel"]["id"] == "solved"
    assert plan["fit"] == 5 and plan["fit_ok"]
    assert [m["id"] for m in plan["moments"]] == [1, 2, 3] or len(plan["moments"]) == 3
    assert plan["make_ids"] == [1, 2] and plan["expected_total"] > 0
    assert all(len(m["explain"]) == 4 for m in plan["moments"])
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='paste_analyse' AND status='queued'").fetchone()["n"] >= 1


def test_nothing_clearing_the_floors_says_so_and_remove_throws_the_analysis_away(env):
    sv, db = env
    run_id, engine = to_plan(sv, FakeEngine(moments=[moment(1, 60), moment(2, 55)]))
    run = runs.get(db, run_id)
    assert run["state"] == "nothing" and run["note"] == "No clip here is likely to earn enough." and run["plan"]["make_ids"] == []
    with pytest.raises(runs.RunError, match="cleared"):
        runs.start(sv, run_id, engine)
    runs.remove(sv, run_id, engine)
    assert runs.get(db, run_id)["state"] == "removed" and engine.discarded == [VIDEO]


def test_a_video_with_no_moments_at_all_is_reported(env):
    sv, db = env
    run_id, _ = to_plan(sv, FakeEngine(moments=[]))
    assert runs.get(db, run_id)["state"] == "nothing" and "No moments" in runs.get(db, run_id)["note"]


def test_an_engine_failure_fails_the_run_and_it_can_be_tried_again(env):
    sv, db = env
    engine = FakeEngine(statuses=["failed"])
    engine.error = "download blocked"
    run_id = runs.create(sv, "solved", LINK)
    runs.analyse(sv, run_id, engine)
    runs.analyse(sv, run_id, engine)
    run = runs.get(db, run_id)
    assert run["state"] == "failed" and "download blocked" in run["error"]
    runs.retry(sv, run_id)
    assert runs.get(db, run_id)["state"] == "analysing" and runs.get(db, run_id)["preview_job_id"] is None


def test_a_twitch_link_is_analysed_like_any_other(env):
    sv, db = env
    run_id, engine = to_plan(sv, link="https://www.twitch.tv/videos/2345678901")
    assert engine.submitted[0]["url"] == "https://www.twitch.tv/videos/2345678901"
    assert runs.get(db, run_id)["video_id"] == "tw_2345678901"


# ---- start, make, check, route ---------------------------------------------------------------------------------


def test_start_makes_only_the_clips_that_cleared_the_floors(env):
    sv, db = env
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    sent = engine.submitted[-1]
    assert sent["moments"] == [1, 2] and sent["force"] is True and "preview" not in sent
    run = runs.get(db, run_id)
    assert run["state"] == "making" and run["render_job_id"] == engine.next_id
    with db.conn() as c:
        assert c.execute("SELECT status FROM clip_jobs WHERE video_id=?", (VIDEO,)).fetchone()["status"] == "submitted"
        assert c.execute("SELECT title FROM videos WHERE video_id=?", (VIDEO,)).fetchone()["title"] == "Episode 12"
    with pytest.raises(runs.RunError, match="not waiting for Start"):
        runs.start(sv, run_id, engine)


def test_finished_clips_are_approved_routed_to_their_channel_and_planned_for_posting(env):
    sv, db = env
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    engine.statuses = ["running"]
    runs.collect(sv, run_id, engine)
    assert runs.get(db, run_id)["state"] == "making"  # still waiting
    runs.collect(sv, run_id, engine)
    run = runs.get(db, run_id)
    assert run["state"] == "finished" and "2 planned for posting" in run["note"]
    with db.conn() as c:
        clips = c.execute("SELECT state, category, reason FROM clips_ready ORDER BY id").fetchall()
        assert [(r["state"], r["category"]) for r in clips] == [("approved", "Podcasts")] * 2
        assert c.execute("SELECT COUNT(*) AS n FROM posts WHERE profile_id='solved'").fetchone()["n"] == 2
        assert {r["platform"] for r in c.execute("SELECT platform FROM posts")} == {"youtube_shorts"}
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='copy'").fetchone()["n"] == 2
    assert len(runs.clips_of(db, run)) == 2


def test_a_clip_scoring_under_the_channels_minimum_waits_for_you_instead_of_posting(env):
    sv, db = env
    assert sv.profiles["solved"].posting.auto_post_min_score == 70
    sv.profiles["solved"] = sv.profiles["solved"].model_copy(update={"posting": sv.profiles["solved"].posting.model_copy(update={"auto_post_min_score": 89})})
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)  # the engine scores the two clips 89 and 88
    run = runs.get(db, run_id)
    assert run["state"] == "finished" and "1 planned for posting" in run["note"] and "1 scored under 89" in run["note"]
    with db.conn() as c:
        rows = {r["engine_score"]: dict(r) for r in c.execute("SELECT engine_score, state, reason FROM clips_ready")}
        assert rows[89]["state"] == "approved" and rows[88]["state"] == "review"
        assert "88 is under the 89 needed to post by itself" in rows[88]["reason"]
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 1


def test_a_finished_campaign_video_starts_the_next_one_at_once(env):
    sv, db = env
    with db.conn() as c:
        c.execute("INSERT INTO runs (source, url, platform, video_id, profile_id, state, created_at, updated_at, campaign_id) "
                  "VALUES ('campaign', ?, 'youtube', ?, 'solved', 'making', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'c1')", (LINK, VIDEO))
        run_id = c.execute("SELECT MAX(id) AS id FROM runs").fetchone()["id"]
    runs._set(db, run_id, state="finished")
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='campaigns' AND status='queued'").fetchone()["n"] == 1
    other = runs.create(sv, "solved", "https://www.youtube.com/watch?v=zZ9yXwVuTsR")  # a pasted video ending does not trigger a campaign pass
    with db.conn() as c:
        c.execute("DELETE FROM tasks WHERE kind='campaigns'")
    runs._set(db, other, state="nothing")
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='campaigns'").fetchone()["n"] == 0


def test_the_look_the_kind_decides_is_what_the_clips_are_made_in(env, monkeypatch):
    sv, db = env
    seen = {}
    from villomillo import formats

    def fake_formatter_for(db_, profile, **kw):
        seen["recipe"] = kw.get("recipe")
        return None

    monkeypatch.setattr(formats, "formatter_for", fake_formatter_for)
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    assert seen["recipe"] == "podcast_talk"


def test_a_video_that_fits_the_channel_loosely_waits_for_your_review_instead_of_posting(env):
    sv, db = env
    sv.jev = FakeJev(fit=1.0)  # fit 2 of 5
    run_id, engine = to_plan(sv)
    plan = runs.get(db, run_id)["plan"]
    assert plan["fit"] == 2 and not plan["fit_ok"] and "wait for your review" in plan["fit_why"]
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    run = runs.get(db, run_id)
    assert "waiting for your review" in run["note"]
    with db.conn() as c:
        assert [r["state"] for r in c.execute("SELECT state FROM clips_ready")] == ["review", "review"]
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0


def test_a_video_whose_category_has_no_channel_is_held_and_home_offers_add_channel(env):
    sv, db = env
    sv.jev = FakeJev(kinds={"podcast": 0.02, "stream": 0.96, "talk": 0.02})
    run_id, engine = to_plan(sv)
    plan = runs.get(db, run_id)["plan"]
    assert plan["category"] == "Stream clips" and plan["channel"] is None and plan["fit"] is None
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    assert "held" in runs.get(db, run_id)["note"] and routing.held_categories(db)[0]["category"] == "Stream clips"
    channels.create(db, sv.profiles, name="Stream clips", category="Stream clips", about="Gaming highlights")
    assert len(routing.release_held(db, lambda: sv.jev, sv.profiles, "Stream clips")) == 2


def test_pasted_clips_carry_no_campaign_rules(env):
    sv, db = env
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    with db.conn() as c:
        assert all("campaign" not in (r["reason"] or "") for r in c.execute("SELECT reason FROM clips_ready"))


# ---- the worker, the breaker and the stop -------------------------------------------------------------------------


def test_the_worker_runs_the_steps_and_the_emergency_stop_pauses_them(env, monkeypatch):
    sv, db = env
    engine = FakeEngine()
    monkeypatch.setattr(clipper, "Engine", lambda *a, **k: engine)
    runs.create(sv, "solved", LINK)
    safety.stop(db)
    assert runner.run_one(sv) is False  # nothing runs while stopped
    safety.resume(db)
    assert runner.run_one(sv) is True  # the first step: asks the engine
    assert engine.submitted and not safety.is_stopped(db)


def test_a_breaker_that_trips_mid_step_waits_a_minute_without_counting_a_failure(env, monkeypatch):
    sv, db = env
    monkeypatch.setattr(runs, "analyse", lambda sv_, run_id: (_ for _ in ()).throw(spend.SpendPaused("paused")))
    monkeypatch.setitem(runner.HANDLERS, "paste_analyse", runner._paste_step(runs.analyse, "paste_analyse"))
    run_id = runs.create(sv, "solved", LINK)
    assert runner.run_one(sv) is True
    with db.conn() as c:
        rows = c.execute("SELECT status, attempts, run_after FROM tasks ORDER BY id").fetchall()
    assert rows[0]["status"] == "done" and rows[1]["status"] == "queued"
    assert runs.get(db, run_id)["state"] == "analysing"


def test_a_task_that_gives_up_marks_its_run_failed(env, monkeypatch):
    sv, db = env
    monkeypatch.setitem(runner.HANDLERS, "paste_analyse", lambda sv_, payload: (_ for _ in ()).throw(RuntimeError("engine exploded")))
    run_id = runs.create(sv, "solved", LINK)
    with db.conn() as c:
        c.execute("UPDATE tasks SET max_attempts=1")
    runner.run_one(sv)
    run = runs.get(db, run_id)
    assert run["state"] == "failed" and "engine exploded" in run["error"]


# ---- the pages ------------------------------------------------------------------------------------------------------


@pytest.fixture
def web(env):
    sv, db = env
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv, db


def test_pasting_a_link_creates_a_run_and_a_bad_link_explains_itself(web):
    client, sv, db = web
    r = client.post("/paste", data={"link": LINK, "profile": "solved"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/1")
    r = client.post("/paste", data={"link": "https://vimeo.com/123456", "profile": "solved"}, follow_redirects=True)
    assert r.status_code == 200 and "YouTube, Twitch and Kick" in r.text
    home = client.get("/?profile=solved").text
    assert 'name="link"' in home and "Analysing" in home


def test_every_state_of_a_run_has_a_page(web, monkeypatch):
    client, sv, db = web
    run_id, engine = to_plan(sv)
    monkeypatch.setattr(clipper, "Engine", lambda *a, **k: engine)
    page = client.get(f"/runs/{run_id}?profile=solved").text
    assert "The clips it wants to make" in page and "Start clipping 2 clips" in page and "Why these numbers" in page
    assert "Type: podcast or interview, 95% sure" in page and "Goes to the Podcasts channel" in page
    assert "Press Start on" in client.get("/?profile=solved").text
    client.post(f"/runs/{run_id}/start", follow_redirects=False)
    assert "Making clips" in client.get(f"/runs/{run_id}").text
    runs.collect(sv, run_id, engine)
    done = client.get(f"/runs/{run_id}").text
    assert "Clips from this video" in done and "2 planned for posting" in done
    assert client.get("/runs/999").status_code == 404


def test_remove_video_closes_the_run_and_a_wrong_action_shows_a_message(web, monkeypatch):
    client, sv, db = web
    monkeypatch.setattr(clipper, "Engine", lambda *a, **k: FakeEngine())
    run_id, _ = to_plan(sv, FakeEngine(moments=[moment(1, 50)]))
    assert "No clip here is likely to earn enough." in client.get(f"/runs/{run_id}").text
    r = client.post(f"/runs/{run_id}/start", follow_redirects=True)
    assert "cleared the floors" in r.text
    client.post(f"/runs/{run_id}/remove", follow_redirects=False)
    assert runs.get(db, run_id)["state"] == "removed"


def test_the_emergency_stop_shows_everywhere_and_resumes(web):
    client, sv, db = web
    assert "Emergency stop" in client.get("/clips?profile=solved").text
    assert client.post("/api/emergency-stop").status_code == 200
    page = client.get("/clips?profile=solved").text
    assert "Everything is stopped" in page and "/api/emergency-resume" in page
    assert client.post("/api/emergency-resume").status_code == 200
    assert "Everything is stopped" not in client.get("/clips?profile=solved").text
    assert client.post("/api/emergency-stop", headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_channel_page_can_turn_real_sending_on(web):
    client, sv, db = web
    form = {"name": "Podcasts", "category": "Podcasts", "about": "Talk", "youtube_route": "api", "dry_run": "0"}
    assert client.post("/channels/solved/save", data=form, follow_redirects=False).status_code == 303
    posting = sv.profiles["solved"].posting
    assert posting.dry_run is False and posting.platforms["youtube_shorts"] == "api"
    assert client.post("/channels/solved/save", data={**form, "youtube_route": "carrier pigeon"}).status_code == 422


def test_the_starting_channel_posts_to_youtube_only_with_no_campaign_tags():
    p = config.load_profiles()["solved"]
    assert list(p.posting.platforms) == ["youtube_shorts"] and p.posting.daily_cap == 100 and p.posting.tags == {}


def test_scoring_constants_match_what_the_plan_screen_says(env):
    sv, _ = env
    assert (sv.app.scoring.viral_min, sv.app.scoring.cpm_floor) == (75, 1.0) and scoring.FIT_MIN == 4


def _made(env, formatter_factory, monkeypatch):
    from villomillo import formats

    sv, db = env
    monkeypatch.setattr(formats, "formatter_for", lambda *a, **k: formatter_factory())
    run_id, engine = to_plan(sv)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    return sv, db, run_id


def test_a_clip_whose_look_failed_waits_for_you_and_is_never_posted_unseen(env, monkeypatch):
    def broken(clip, job, out):
        raise RuntimeError("no trustworthy on-screen text")

    sv, db, run_id = _made(env, lambda: broken, monkeypatch)
    run = runs.get(db, run_id)
    assert run["state"] == "finished" and "did not get the channel's look" in run["note"] and "planned for posting" not in run["note"]
    with db.conn() as c:
        rows = c.execute("SELECT state, reason, grade_json FROM clips_ready").fetchall()
        assert {r["state"] for r in rows} == {"review"} and all("look not applied" in r["reason"] for r in rows)
        assert all(json.loads(r["grade_json"])["look_failed"] for r in rows)
        assert c.execute("SELECT COUNT(*) AS n FROM posts").fetchone()["n"] == 0


def test_the_look_is_tried_a_second_time_before_giving_up(env, monkeypatch):
    calls = []

    def flaky(clip, job, out):
        calls.append(clip["id"])
        if len(calls) == 1:
            raise RuntimeError("a hiccup")
        return {"recipe": "podcast_talk"}

    sv, db, run_id = _made(env, lambda: flaky, monkeypatch)
    assert len(calls) == 3  # two clips: the first tried twice, the second once
    with db.conn() as c:
        assert {r["state"] for r in c.execute("SELECT state FROM clips_ready")} == {"approved"}


def test_a_download_that_is_not_an_mp4_is_still_found(tmp_path, monkeypatch):
    from villomillo import formats

    monkeypatch.setattr(formats, "DOWNLOADS", tmp_path)
    (tmp_path / "vidAAAAAAAA.webm").write_bytes(b"x")
    profile = channels.load(Database(tmp_path / "x.db"))["solved"]
    kw = {"claude_client": None, "jev": None, "start_s": 0.0, "end_s": 5.0, "out_path": tmp_path / "o.mp4", "episode": "e", "words": []}
    with pytest.raises(formats.FormatError, match="no words"):  # it got past finding the source
        formats.format_clip(Database(tmp_path / "y.db"), profile.model_copy(update={"format": "podcast_talk"}), video_id="vidAAAAAAAA", **kw)
    with pytest.raises(formats.FormatError, match="not on disk"):
        formats.format_clip(Database(tmp_path / "y.db"), profile.model_copy(update={"format": "podcast_talk"}), video_id="vidBBBBBBBB", **kw)


def test_apply_the_look_again_replaces_the_file_and_clears_the_warning(env, monkeypatch, tmp_path):
    from villomillo import formats, relook

    def broken(clip, job, out):
        raise RuntimeError("it failed")

    sv, db, run_id = _made(env, lambda: broken, monkeypatch)
    with db.conn() as c:
        clip_id = c.execute("SELECT id FROM clips_ready ORDER BY id").fetchone()["id"]
        file = c.execute("SELECT file_path FROM clips_ready WHERE id=?", (clip_id,)).fetchone()["file_path"]

    def good(clip, job, out):
        out.write_bytes(b"new look")
        return {"recipe": "podcast_talk"}

    monkeypatch.setattr(formats, "formatter_for", lambda *a, **k: good)
    monkeypatch.setattr(relook.validate, "check", lambda path, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    assert "applied again" in relook.apply(sv, clip_id)
    assert pathlib_read(file) == b"new look"
    with db.conn() as c:
        row = c.execute("SELECT grade_json, reason FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
    assert json.loads(row["grade_json"])["look_failed"] is False and "look not applied" not in row["reason"]
    monkeypatch.setattr(formats, "formatter_for", lambda *a, **k: broken)
    with pytest.raises(relook.RelookError, match="could not be applied"):
        relook.apply(sv, clip_id)


def pathlib_read(path):
    import pathlib

    return pathlib.Path(path).read_bytes()


def test_the_panel_offers_apply_the_look_again_only_for_a_clip_in_the_wrong_look(env, monkeypatch):
    def broken(clip, job, out):
        raise RuntimeError("it failed")

    sv, db, run_id = _made(env, lambda: broken, monkeypatch)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    with db.conn() as c:
        clip_id = c.execute("SELECT id FROM clips_ready ORDER BY id").fetchone()["id"]
    panel = client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert "Apply the look again" in panel and "it failed" in panel
    assert client.post("/api/clip-relook", data={"profile": "solved", "clip_id": clip_id}).status_code == 200
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='relook'").fetchone()["n"] == 1
    assert "relook" in runner.HANDLERS
    assert client.post("/api/clip-relook", data={"profile": "solved", "clip_id": 9999}).status_code == 404
