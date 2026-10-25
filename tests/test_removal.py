"""Removing clips: hidden not deleted, posts cancelled, live posts left alone, restore, purge, and the pages that show it."""

from __future__ import annotations

import json
import pathlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_research import make_sv
from test_runs import LINK, FakeEngine

from villomillo import campaigns, clipper, copywriter, removal, runs, store
from villomillo.web.app import create_app


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch)


def clips_posted(sv, engine=None):
    """A pasted video taken all the way to approved clips with a planned post each. Returns the clip ids."""
    engine = engine or FakeEngine()
    run_id = runs.create(sv, "solved", LINK)
    for _ in range(3):
        runs.analyse(sv, run_id, engine)
    runs.start(sv, run_id, engine)
    runs.collect(sv, run_id, engine)
    with sv.db.conn() as c:
        return run_id, [r["id"] for r in c.execute("SELECT id FROM clips_ready ORDER BY id")], engine


def states(sv):
    with sv.db.conn() as c:
        return ([r["state"] for r in c.execute("SELECT state FROM clips_ready ORDER BY id")], [r["status"] for r in c.execute("SELECT status FROM posts ORDER BY id")])


def test_removing_hides_the_clip_and_cancels_posts_that_have_not_gone_out(sv):
    run_id, ids, _ = clips_posted(sv)
    assert states(sv) == (["approved", "approved"], ["queued", "queued"])
    got = removal.remove(sv.db, "solved", [ids[0]])
    assert got == {"removed": 1, "cancelled": 1, "live": 0}
    assert states(sv) == (["removed", "approved"], ["cancelled", "queued"])
    with sv.db.conn() as c:
        row = dict(c.execute("SELECT * FROM clips_ready WHERE id=?", (ids[0],)).fetchone())
    assert row["removed_from"] == "approved" and row["removed_at"]
    with pytest.raises(removal.RemovalError):
        removal.remove(sv.db, "solved", [ids[0]])  # already removed
    with pytest.raises(removal.RemovalError):
        removal.remove(sv.db, "solved", [])


def test_a_post_already_on_youtube_stays_and_the_confirm_step_says_so(sv):
    run_id, ids, _ = clips_posted(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE posts SET status='uploaded', url='https://youtube.com/shorts/x', posted_at=? WHERE clip_id=?", (store.iso(), ids[0]))
    info = removal.impact(sv.db, "solved", ids)
    assert info == {"clips": 2, "cancel": 1, "live": ["https://youtube.com/shorts/x"], "handed_in": 0}
    assert removal.remove(sv.db, "solved", ids) == {"removed": 2, "cancelled": 1, "live": 1}
    assert states(sv)[1] == ["uploaded", "cancelled"]  # the live post is untouched


def test_a_handed_in_campaign_link_is_warned_about(sv):
    run_id, ids, _ = clips_posted(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET campaign_id='cid' WHERE id=?", (ids[0],))
        c.execute("UPDATE posts SET status='uploaded', url='u' WHERE clip_id=?", (ids[0],))
        post = c.execute("SELECT id FROM posts WHERE clip_id=?", (ids[0],)).fetchone()["id"]
        c.execute("INSERT INTO campaign_submissions (post_id, campaign_id, submitted_at) VALUES (?, 'cid', ?)", (post, store.iso()))
    assert removal.impact(sv.db, "solved", [ids[0]])["handed_in"] == 1


def test_restoring_brings_the_clip_back_to_your_decision_and_clears_its_cancelled_post(sv):
    run_id, ids, _ = clips_posted(sv)
    removal.remove(sv.db, "solved", ids)
    assert removal.restore(sv.db, "solved", ids) == 2
    assert states(sv) == (["review", "review"], [])  # an approved clip returns to Waiting for you, with no stale post
    from villomillo import clipper as c2
    from villomillo import routing

    c2.decide(sv.db, "solved", ids[0], "approve")  # approving again plans a fresh post
    assert routing.assign_and_plan(sv.db, lambda: sv.jev, sv.profiles, "solved", ids[0]) is not None


def test_a_clip_the_rules_stopped_goes_back_to_being_stopped_not_to_review(sv):
    run_id, ids, _ = clips_posted(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET state='rejected_auto' WHERE id=?", (ids[0],))
    removal.remove(sv.db, "solved", [ids[0]])
    removal.restore(sv.db, "solved", [ids[0]])
    assert states(sv)[0][0] == "rejected_auto"


def test_the_engines_clips_are_not_collected_again_after_a_removal(sv):
    run_id, ids, engine = clips_posted(sv)
    removal.remove(sv.db, "solved", ids)
    with sv.db.conn() as c:
        job = dict(c.execute("SELECT * FROM clip_jobs").fetchone())
    assert clipper.collect(sv.db, engine, sv.jev, sv.profiles["solved"], job).startswith("0 clips made")


def test_files_are_deleted_a_month_later_and_a_purged_clip_cannot_come_back(sv, tmp_path):
    run_id, ids, _ = clips_posted(sv)
    removal.remove(sv.db, "solved", ids)
    with sv.db.conn() as c:
        files = [pathlib.Path(r["file_path"]) for r in c.execute("SELECT file_path FROM clips_ready")]
    assert all(f.exists() for f in files)
    assert removal.purge_old(sv.db) == 0  # too soon
    assert removal.purge_old(sv.db, now=store.now() + timedelta(days=31)) == 2
    assert not any(f.exists() for f in files)
    with pytest.raises(removal.RemovalError, match="deleted after 30 days"):
        removal.restore(sv.db, "solved", ids)


def test_cancelled_posts_leave_the_calendar_and_removed_clips_leave_the_counts(sv):
    run_id, ids, _ = clips_posted(sv)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    assert "To post by hand" in client.get("/calendar?profile=solved&view=list").text or "Planned" in client.get("/calendar?profile=solved&view=list").text
    removal.remove(sv.db, "solved", ids)
    cal = client.get("/calendar?profile=solved&view=list").text
    assert 'Planned <b class="num">0</b>' in cal and "Clip from" not in cal
    clips = client.get("/clips?profile=solved").text
    assert "No clips yet" in clips
    tab = client.get("/clips?profile=solved&tab=removed").text
    assert "Restore selected" in tab and 'data-clip-id="' in tab
    assert "Removed<small>2</small>" in client.get("/clips?profile=solved&tab=all").text


def test_the_campaign_link_list_ignores_removed_clips(sv):
    run_id, ids, _ = clips_posted(sv)
    campaigns.seed_solved(sv.db, sv.app.scoring)
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET campaign_id=?", (campaigns.SOLVED_ID,))
        c.execute("UPDATE posts SET status='uploaded', url='https://youtube.com/shorts/x'")
    assert len(campaigns.ready_to_submit(sv.db)) == 2
    removal.remove(sv.db, "solved", [ids[0]])
    assert len(campaigns.ready_to_submit(sv.db)) == 1


def test_a_cancelled_post_never_gets_copy_written(sv):
    run_id, ids, _ = clips_posted(sv)
    removal.remove(sv.db, "solved", [ids[0]])
    with sv.db.conn() as c:
        post = c.execute("SELECT id FROM posts WHERE clip_id=?", (ids[0],)).fetchone()["id"]
    assert copywriter.generate(sv.db, None, None, sv.profiles["solved"], post).startswith("skipped: this post was cancelled")


def test_the_panel_buttons_routes_and_run_page(sv):
    run_id, ids, _ = clips_posted(sv)
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    panel = client.get(f"/clips/{ids[0]}/panel?profile=solved").text
    assert 'data-act="/api/clips/remove"' in panel and "Its planned post is cancelled." in panel and "Restore" not in panel
    assert json.loads(client.get(f"/api/clips/impact?profile=solved&clip_ids={ids[0]},{ids[1]}").text)["cancel"] == 2
    assert client.post("/api/clips/remove", data={"profile": "solved", "clip_ids": f"{ids[0]}"}).json() == {"ok": True, "removed": 1, "cancelled": 1, "live": 0}
    assert client.post("/api/clips/remove", data={"profile": "solved", "clip_ids": f"{ids[0]}"}).status_code == 422
    assert client.post("/api/clips/remove", data={"profile": "solved", "clip_ids": "x"}).status_code == 422
    assert "Restore" in client.get(f"/clips/{ids[0]}/panel?profile=solved").text
    assert "Remove all clips" in client.get(f"/runs/{run_id}").text
    client.post(f"/runs/{run_id}/remove-clips")
    assert states(sv)[0] == ["removed", "removed"]
    assert client.post("/api/clips/restore", data={"profile": "solved", "clip_ids": f"{ids[0]},{ids[1]}"}).json()["restored"] == 2
    assert client.post("/api/clips/remove", data={"profile": "nobody", "clip_ids": "1"}).status_code == 404
