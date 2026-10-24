"""Titles and captions: Claude's options are checked by code, screened by Jev, and chosen for accuracy and hook.
Claude and Jev are replaced by fakes; nothing leaves the machine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from villomillo import config, copywriter, posting, store
from villomillo.db import Database
from villomillo.jev import JevResult
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
CLIP_TEXT = "Luck is partly something you can train. There is research on how to expand it."


def option(title="You can actually increase your luck", idea="Luck is less random than most people think",
           question="What would you try first?", hashtags=("#luck", "#selfimprovement"), keywords=("luck", "growth")):
    return {"title": title, "idea": idea, "question": question, "hashtags": list(hashtags), "keywords": list(keywords)}


class FakeClaude:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        text = json.dumps({"options": self.replies.pop(0)})
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50), stop_reason="end_turn")


class FakeJev:
    def __init__(self, *, misleading, stop):
        self.misleading, self.stop = misleading, stop

    def ask(self, state, questions):
        answers = {}
        for i in range(len(state["options"])):
            answers[f"misleading_{i}"] = {"noul": self.misleading[i]}
            answers[f"stop_{i}"] = {"score": self.stop[i]}
        return JevResult("jev", answers, {"input_tokens": 500})


@pytest.fixture
def solved():
    return config.load_profiles()["solved"]


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "copy.db")
    with database.conn() as c:
        c.execute("INSERT INTO videos (profile_id, video_id, found_by, channel_id, channel_title, title, description, "
                  "tags_json, duration_s, published_at, first_seen_at, last_seen_at) VALUES "
                  "('solved','vid00000001','campaign','UC','Solved','How to Create Your Own Luck','','[]',3000,?,?,?)",
                  (store.iso(), store.iso(), store.iso()))
    return database


def planned_post(db, solved, *, text=CLIP_TEXT, hook="Now, what's interesting is that earth luck"):
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', 1, 'vid00000001', 0, 30, 80, ?, 'f.mp4', '{}', ?, 'approved', 'r', ?)",
                  (hook, json.dumps({"caption_text": text}), store.iso()))
        clip_id = c.execute("SELECT id FROM clips_ready").fetchone()["id"]
    return posting.plan(db, solved, clip_id, now=NOW)["id"]


def run(db, solved, post_id, claude, jev, **kw):
    return copywriter.generate(db, claude, jev, solved, post_id, budget_usd=kw.pop("budget_usd", 25.0), **kw)


# --- the rules code enforces on Claude's options ----------------------------------------------------

@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"title": "x" * 71}, "limit is 70"),
        ({"title": "Luck can be trained…"}, "cut off"),
        ({"title": "LUCK CAN BE TRAINED NOW"}, "capitals"),
        ({"title": "Now luck can be trained"}, "filler"),
        ({"title": "Luck can be trained #luck"}, "hashtag in it"),
        ({"title": ""}, "no title"),
        ({"idea": "x" * 101}, "first line"),
        ({"question": "Try it"}, "question mark"),
        ({"hashtags": ["#one"]}, "2 to 4"),
        ({"hashtags": ["#one", "two words"]}, "2 to 4"),
        ({"idea": "You won't believe this luck trick"}, "clickbait"),
    ],
)
def test_youtube_options_are_rejected_for_each_rule(changes, expected):
    problems = copywriter.option_problems({**option(), **changes}, "youtube_shorts")
    assert any(expected in p for p in problems), problems


def test_a_good_youtube_option_passes_and_a_platform_without_titles_takes_none():
    assert copywriter.option_problems(option(), "youtube_shorts") == []
    assert copywriter.option_problems(option(title=""), "tiktok") == []
    assert any("no title" in p for p in copywriter.option_problems(option(), "tiktok"))


def test_the_youtube_caption_always_carries_the_campaign_tags_and_shorts_first(solved):
    built = copywriter.assemble(solved.posting, "youtube_shorts", "Ep", option(hashtags=["#shorts", "#luck", "#growth"]))
    assert built["title"] == "You can actually increase your luck"
    assert "@solvedclips @iammarkmanson" in built["caption"]
    hashtag_line = built["caption"].splitlines()[-1]
    assert hashtag_line.split()[0] == "#Shorts" and hashtag_line.lower().count("#shorts") == 1
    assert len(hashtag_line.split()) <= 5
    assert built["caption"].splitlines()[0] == "Luck is less random than most people think"
    assert posting.copy_problems(built, "youtube_shorts", solved.posting.tags["youtube_shorts"]) == []


def test_a_caption_for_a_platform_without_titles_leads_with_the_idea_and_tags_the_show(solved):
    tagged = solved.posting.model_copy(update={"tags": {"tiktok": "@solvedpodcast"}})
    built = copywriter.assemble(tagged, "tiktok", "Ep", option(title=""))
    assert built["title"] == "" and built["caption"].startswith("Luck is less random")
    assert "@solvedpodcast" in built["caption"]
    assert posting.copy_problems(built, "tiktok", "@solvedpodcast") == []


# --- writing, screening and choosing ----------------------------------------------------------------

def test_the_most_attention_grabbing_accurate_option_is_chosen(db, solved):
    post_id = planned_post(db, solved)
    claude = FakeClaude([option(title="Option A is fine"), option(title="Option B overclaims"),
                         option(title="Option C wins")])
    jev = FakeJev(misleading=[0.05, 0.8, 0.1], stop=[3, 5, 4])  # B scores highest but misstates the speaker
    status = run(db, solved, post_id, claude, jev)
    assert status == "2 options written; chose the one with hook score 4/5"
    with db.conn() as c:
        post = c.execute("SELECT title, caption FROM posts WHERE id=?", (post_id,)).fetchone()
        rows = c.execute("SELECT title, stop_score, chosen FROM copy_options WHERE post_id=? ORDER BY id",
                         (post_id,)).fetchall()
    assert post["title"] == "Option C wins" and "@solvedclips @iammarkmanson" in post["caption"]
    assert [(r["title"], r["stop_score"], r["chosen"]) for r in rows] == [("Option C wins", 4, 1), ("Option A is fine", 3, 0)]


def test_the_cost_is_recorded_against_claude_and_jev(db, solved):
    post_id = planned_post(db, solved)
    run(db, solved, post_id, FakeClaude([option()]), FakeJev(misleading=[0.0], stop=[4]))
    with db.conn() as c:
        engines = {r["engine"]: r["requests"] for r in c.execute("SELECT engine, requests FROM ai_usage")}
    assert engines == {"claude": 1, "jev": 1}
    assert store.ai_spend_month(db, "claude") > 0


def test_options_are_written_once_unless_forced(db, solved):
    post_id = planned_post(db, solved)
    run(db, solved, post_id, FakeClaude([option(title="First version")]), FakeJev(misleading=[0], stop=[3]))
    claude = FakeClaude([option(title="Second version")])
    assert run(db, solved, post_id, claude, FakeJev(misleading=[0], stop=[4])).startswith("skipped: options are already")
    assert claude.calls == 0
    run(db, solved, post_id, claude, FakeJev(misleading=[0], stop=[4]), force=True)
    with db.conn() as c:
        titles = [r["title"] for r in c.execute("SELECT title FROM copy_options WHERE post_id=?", (post_id,))]
    assert titles == ["Second version"]


def test_a_second_try_is_made_when_nothing_usable_comes_back(db, solved):
    post_id = planned_post(db, solved)
    claude = FakeClaude([option(title="Now this is cut off…")], [option(title="Luck can be trained")])
    status = run(db, solved, post_id, claude, FakeJev(misleading=[0], stop=[4]))
    assert claude.calls == 2 and status.startswith("1 options written")


def test_when_nothing_passes_the_basic_copy_is_kept(db, solved):
    post_id = planned_post(db, solved)
    with db.conn() as c:
        before = c.execute("SELECT title, caption FROM posts WHERE id=?", (post_id,)).fetchone()
    claude = FakeClaude([option(title="Now luck…")], [option(title="So luck…")])
    status = run(db, solved, post_id, claude, FakeJev(misleading=[0], stop=[4]))
    assert status.startswith("kept the basic copy") and claude.calls == 2
    with db.conn() as c:
        after = c.execute("SELECT title, caption FROM posts WHERE id=?", (post_id,)).fetchone()
        assert c.execute("SELECT COUNT(*) AS n FROM copy_options").fetchone()["n"] == 0
    assert tuple(after) == tuple(before)


def test_an_option_that_may_misstate_the_speaker_is_never_used(db, solved):
    post_id = planned_post(db, solved)
    claude = FakeClaude([option(title="Overclaim")], [option(title="Overclaim again")])
    status = run(db, solved, post_id, claude, FakeJev(misleading=[0.9], stop=[5]))
    assert status.startswith("kept the basic copy") and "may misstate the speaker" in status


def test_nothing_is_written_when_the_monthly_claude_budget_is_used(db, solved):
    post_id = planned_post(db, solved)
    with db.conn() as c:
        store.add_ai_usage(c, "claude", 0, 0, 30.0)
    claude = FakeClaude([option()])
    assert run(db, solved, post_id, claude, FakeJev(misleading=[0], stop=[3])).startswith("skipped: this month's")
    assert claude.calls == 0


def test_a_post_that_is_already_out_keeps_its_copy(db, solved):
    post_id = planned_post(db, solved)
    posting.mark_posted(db, "solved", post_id, "https://youtube.com/shorts/abc")
    assert run(db, solved, post_id, FakeClaude([option()]), FakeJev(misleading=[0], stop=[3])).startswith("skipped: this post is already out")


def test_you_can_switch_to_another_option_until_it_goes_out(db, solved):
    post_id = planned_post(db, solved)
    run(db, solved, post_id, FakeClaude([option(title="Top pick"), option(title="Runner up")]),
        FakeJev(misleading=[0, 0], stop=[5, 3]))
    with db.conn() as c:
        runner_up = c.execute("SELECT id FROM copy_options WHERE title='Runner up'").fetchone()["id"]
    copywriter.choose(db, "solved", post_id, runner_up)
    with db.conn() as c:
        assert c.execute("SELECT title FROM posts WHERE id=?", (post_id,)).fetchone()["title"] == "Runner up"
        assert [r["chosen"] for r in c.execute("SELECT chosen FROM copy_options ORDER BY id")] == [0, 1]
    with pytest.raises(LookupError):
        copywriter.choose(db, "solved", post_id, 9999)
    posting.mark_posted(db, "solved", post_id, "https://youtube.com/shorts/abc")
    with pytest.raises(ValueError, match="already out"):
        copywriter.choose(db, "solved", post_id, runner_up)


# --- the Manual uploads page ------------------------------------------------------------------------

def test_the_manual_page_lists_the_options_and_lets_you_pick_one(db, solved):
    post_id = planned_post(db, solved)
    run(db, solved, post_id, FakeClaude([option(title="Top pick"), option(title="Runner up")]),
        FakeJev(misleading=[0, 0], stop=[5, 3]))
    with db.conn() as c:  # a due manual post shows up on the page
        c.execute("UPDATE posts SET status='manual_ready' WHERE id=?", (post_id,))
    sv = Services(config.AppConfig(), config.load_profiles(), db, Quota(db, general_cap=9, search_cap=9))
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    html = client.get(f"/calendar/post/{post_id}?profile=solved").text
    assert "Top pick" in html and "Runner up" in html and "hook 5/5" in html and "/api/post-copy" in html
    with db.conn() as c:
        runner_up = c.execute("SELECT id FROM copy_options WHERE title='Runner up'").fetchone()["id"]
    form = {"profile": "solved", "post_id": post_id, "option_id": runner_up}
    assert client.post("/api/post-copy", data=form).status_code == 200
    assert client.post("/api/post-copy", data=form, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/post-copy", data={**form, "option_id": 9999}).status_code == 404
    with db.conn() as c:
        assert c.execute("SELECT title FROM posts WHERE id=?", (post_id,)).fetchone()["title"] == "Runner up"


def test_a_post_with_no_options_offers_to_write_them(db, solved):
    post_id = planned_post(db, solved)
    with db.conn() as c:
        c.execute("UPDATE posts SET status='manual_ready' WHERE id=?", (post_id,))
    sv = Services(config.AppConfig(), config.load_profiles(), db, Quota(db, general_cap=9, search_cap=9))
    html = TestClient(create_app(sv), base_url="http://127.0.0.1:8780").get(f"/calendar/post/{post_id}?profile=solved").text
    assert "Write title options" in html
