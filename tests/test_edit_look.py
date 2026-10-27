"""Phase 4 of the format builder: Edit look opens the builder on the real short. The clip keeps the text that was made for it, the
builder previews the clip itself, only a clip that has not gone out can be edited, and there are three ways to keep a look."""

from __future__ import annotations

import itertools
import json

import pytest
from fastapi.testclient import TestClient
from test_formats_bench import library  # noqa: F401  (a fixture the next one uses)
from test_live_preview import clip_library, needs_ffmpeg  # noqa: F401  (clip_library is a fixture)
from test_research import make_sv

from villomillo import clipper, relook, store
from villomillo.formats import designs, elements, preview, render, templates
from villomillo.web import app as web_app
from villomillo.web.pages import STATIC

_ids = itertools.count(1)
SCRIPT = (STATIC / "formats-builder.js").read_text(encoding="utf-8")
MADE_TEXT = render.ClipText(["THE REAL HEADLINE", "SAID IN TWO LINES"], [("Real point one", 3.5), ("Real point two", 9.0)], "The real takeaway",
                            [("punch_in", 4.5, "the strongest line")], serious=False)


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch)


@pytest.fixture
def client(sv):
    return TestClient(web_app.create_app(sv), base_url="http://127.0.0.1:8780")


def boot(client, query: str = "") -> dict:
    page = client.get("/formats" + query).text
    raw = page.split('<script id="boot" type="application/json">')[1].split("</script>")[0]
    return json.loads(raw.replace("\\u0027", "'").replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))


def add_clip(sv, *, state="review", file_path="/tmp/clip.mp4", grade=None, template_id=None, campaign_id=None, video_id="abcdefghijk",
             start=0.0, end=30.0, post: str | None = None, applied=1) -> int:
    with sv.db.conn() as c:
        cur = c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, validation_json, grade_json, state, reason, "
                        "created_at, category, file_path, format_applied, template_id, campaign_id) VALUES ('solved', ?, ?, ?, ?, '{}', ?, ?, 'because', ?, "
                        "'Podcasts', ?, ?, ?, ?)",
                        (next(_ids), video_id, start, end, json.dumps(grade or {}), state, store.iso(), file_path, applied,
                         template_id, campaign_id))
        clip_id = cur.lastrowid
        if post:
            c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at) "
                      "VALUES ('solved', ?, 'youtube_shorts', 'api', ?, 't', 'c', '[]', '2030-01-01T10:00:00+00:00', ?)", (clip_id, post, store.iso()))
    return clip_id


def one(sv, sql: str) -> dict:
    with sv.db.conn() as c:
        return dict(c.execute(sql).fetchone())


def make_template(sv, name="Mine", **design) -> dict:
    return templates.save_design(sv.db, name=name, design=design)


# --- the clip keeps the text that was made for it ---------------------------------------------------------------------------

def test_the_text_of_a_clip_is_kept_and_comes_back_the_same():
    kept = MADE_TEXT.saved()
    again = render.ClipText.restore(json.loads(json.dumps(kept)))
    assert again == MADE_TEXT and kept["points"] == [["Real point one", 3.5], ["Real point two", 9.0]]


def test_a_made_clip_records_its_text_and_the_design_it_was_made_with():
    design = designs.normalize({})
    kept = clipper.look_record({"text": MADE_TEXT, "design": design, "effects": []})
    assert kept["look_text"]["title_lines"] == ["THE REAL HEADLINE", "SAID IN TWO LINES"] and kept["look_design"] == design
    assert clipper.look_record({"template": "x"}) == {}   # a formatter that reports neither leaves nothing behind


def test_a_look_made_for_a_clip_keeps_the_text_and_leaves_an_approved_clips_post_alone(sv, tmp_path, monkeypatch):
    from villomillo import formats

    path = tmp_path / "clip.mp4"
    path.write_bytes(b"old")
    clip_id = add_clip(sv, state="approved", file_path=str(path), post="queued")
    with sv.db.conn() as c:
        before = dict(c.execute("SELECT * FROM posts").fetchone())

    def fake_make_formatter(db, profile, template, **kw):
        def formatter(clip, job, out):
            out.write_bytes(b"new")
            return {"template": template["id"], "text": MADE_TEXT, "design": template["settings"], "effects": [], "music": None}
        return formatter

    monkeypatch.setattr(formats, "make_formatter", fake_make_formatter)
    monkeypatch.setattr(relook.validate, "check", lambda p, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    relook.apply(sv, clip_id, design={"captions": {"style": "burst"}})
    assert path.read_bytes() == b"new"                                   # the file the post will send is the new one
    with sv.db.conn() as c:
        row = c.execute("SELECT state, reason, grade_json FROM clips_ready").fetchone()
        after = dict(c.execute("SELECT * FROM posts").fetchone())
    assert row["state"] == "approved" and row["reason"] == "because"    # an approved clip stays approved and keeps its reason
    assert after == before and after["status"] == "queued" and after["scheduled_at"] == "2030-01-01T10:00:00+00:00"   # its place in the queue
    grade = json.loads(row["grade_json"])
    assert grade["look_text"]["takeaway"] == "The real takeaway" and grade["look_design"]["captions"]["style"] == "burst"


# --- which clips can be edited -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("state,post,expected", [
    ("review", None, None), ("approved", None, None), ("approved", "queued", None), ("approved", "blocked", None), ("approved", "failed", None),
    ("approved", "uploaded", "already been posted"), ("approved", "posted", "already been posted"),
    ("rejected", None, "was rejected"), ("rejected_auto", None, "stopped by the automatic checks"), ("failed_validation", None, "failed its file checks"),
    ("removed", None, "is gone"),
])
def test_which_clips_can_have_their_look_edited(sv, state, post, expected):
    clip_id = add_clip(sv, state=state, post=post)
    with sv.db.conn() as c:
        row = c.execute("SELECT * FROM clips_ready WHERE id=?", (clip_id,)).fetchone()
    problem = relook.edit_problem(sv.db, row)
    assert (problem is None) if expected is None else (expected in problem)


def test_a_clip_with_no_file_cannot_be_edited(sv):
    clip_id = add_clip(sv, file_path=None)
    with sv.db.conn() as c:
        assert "file was deleted" in relook.edit_problem(sv.db, c.execute("SELECT * FROM clips_ready WHERE id=?", (clip_id,)).fetchone())


def test_the_page_opens_the_builder_on_a_clip_that_has_not_gone_out_and_says_why_not_on_one_that_has(sv, client):
    waiting = add_clip(sv, state="approved", post="queued", grade={"look_text": MADE_TEXT.saved()}, video_id="v1video", start=40.0, end=58.0)
    data = boot(client, f"?clip={waiting}&profile=solved")
    clip = data["clip"]
    assert clip["id"] == waiting and clip["video_id"] == "v1video" and (clip["start_s"], clip["end_s"], clip["length"]) == (40.0, 58.0, 18.0)
    assert clip["text"] == {"title_lines": ["THE REAL HEADLINE", "SAID IN TWO LINES"], "points": ["Real point one", "Real point two"], "takeaway": "The real takeaway"}
    assert clip["waiting_post"] is True and clip["campaign"] is False and data["clip_problem"] == ""
    with sv.db.conn() as c:
        c.execute("UPDATE posts SET status='uploaded'")
    gone = boot(client, f"?clip={waiting}&profile=solved")
    assert gone["clip"] is None and "already been posted" in gone["clip_problem"]
    assert boot(client, "?clip=9999&profile=solved")["clip_problem"] == "There is no such clip."


def test_the_server_refuses_to_change_the_look_of_a_clip_that_has_gone_out_and_accepts_one_still_waiting(sv, client):
    clip_id = add_clip(sv, state="approved", post="queued")
    ok = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": {"captions": {"style": "burst"}}})
    assert ok.status_code == 200
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='relook'").fetchone()["n"] == 1
        c.execute("UPDATE posts SET status='posted'")
        c.execute("DELETE FROM tasks")
    refused = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": {}})
    assert refused.status_code == 409 and "already been posted" in refused.json()["error"]
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='relook'").fetchone()["n"] == 0   # nothing was queued


def test_the_worker_checks_again_when_it_gets_to_the_clip(sv):
    clip_id = add_clip(sv, state="approved", post="uploaded")   # it went out while the look waited in the queue
    with pytest.raises(relook.RelookError, match="already been posted"):
        relook.apply(sv, clip_id, design={})


def test_the_clip_panel_offers_edit_look_for_every_clip_that_can_be_edited_and_not_for_a_posted_one(sv, client):
    waiting = add_clip(sv, state="approved", post="queued", video_id="aaaaaaaaaaa")
    link = f"/formats?clip={waiting}"
    assert link in client.get(f"/clips/{waiting}/panel?profile=solved").text.replace("&amp;", "&")
    with sv.db.conn() as c:
        c.execute("UPDATE posts SET status='posted'")
    assert link not in client.get(f"/clips/{waiting}/panel?profile=solved").text.replace("&amp;", "&")
    review = add_clip(sv, state="review", video_id="bbbbbbbbbbb", start=5.0)
    assert f"/formats?clip={review}" in client.get(f"/clips/{review}/panel?profile=solved").text.replace("&amp;", "&")


# --- the three ways to keep a look -------------------------------------------------------------------------------------------------

def test_each_of_the_three_save_buttons_does_exactly_its_own_thing(sv, client):
    mine = make_template(sv, "Mine")
    other = make_template(sv, "Other")
    clip_id = add_clip(sv, state="approved", post="queued", template_id=mine["id"])
    edited = designs.normalize({"captions": {"style": "burst"}})
    clip_now = lambda: one(sv, "SELECT * FROM clips_ready")  # noqa: E731
    clip_before = clip_now()
    tasks_before = lambda: one(sv, "SELECT COUNT(*) AS n FROM tasks WHERE kind='relook'")["n"]  # noqa: E731

    # 1. Save as a new format: a new template, no kind of clip changes, the clip is not made again
    new = client.post("/api/formats/save", json={"id": None, "name": "Mine (edited)", "design": edited, "categories": []})
    assert new.status_code == 200 and len(templates.list_templates(sv.db)) == 3
    assert templates.get(sv.db, mine["id"])["settings"]["captions"]["style"] != "burst" and templates.assignments(sv.db) == {}
    assert tasks_before() == 0 and clip_now() == clip_before

    # 2. Update the format the clip came from: that template changes, nothing else does, and the clip is not made again
    up = client.post("/api/formats/save", json={"id": mine["id"], "name": "Mine", "design": edited, "categories": []})
    assert up.status_code == 200 and up.json()["id"] == mine["id"] and len(templates.list_templates(sv.db)) == 3
    assert templates.get(sv.db, mine["id"])["settings"]["captions"]["style"] == "burst"
    assert templates.get(sv.db, other["id"])["settings"]["captions"]["style"] != "burst"
    assert tasks_before() == 0 and clip_now() == clip_before   # clips already made keep their look

    # 3. Apply to this clip only: the clip is made again, and no template changes
    templates_before = [t["settings"] for t in templates.list_templates(sv.db)]
    apply = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": designs.normalize({"captions": {"style": "slam"}}),
                                                         "template_id": mine["id"]})
    assert apply.status_code == 200 and tasks_before() == 1
    assert [t["settings"] for t in templates.list_templates(sv.db)] == templates_before
    with sv.db.conn() as c:
        assert json.loads(c.execute("SELECT payload_json FROM tasks WHERE kind='relook'").fetchone()["payload_json"])["design"]["captions"]["style"] == "slam"


def test_the_page_script_wires_the_three_buttons_and_the_clip_s_own_text():
    for word in ('$("apply-clip")', '$("clip-new")', '$("clip-update")', "function ghostOf(e)", "clip_id: clipId()", "Updated the", "Saved as a new format called"):
        assert word in SCRIPT, word
    assert 'categories: []' in SCRIPT.split('$("clip-new")')[1].split('$("clip-update")')[0]     # a new format takes no kind of clip from the old one
    assert "boot.clip_problem" in SCRIPT                                                      # a clip that cannot be edited gets its reason, not a builder


# --- the preview is the clip itself -----------------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_the_preview_of_a_real_clip_is_that_clips_own_stretch_words_and_text(clip_library):
    sv, profile = clip_library
    design = {"elements": [elements.default_element("headline"), elements.default_element("label"), elements.default_element("progress_bar"),
                           elements.default_element("handle")]}
    sample = preview.prepare(sv.db, profile, design, "talkyvideo1")
    clip = preview.RealClip("talkyvideo1", 40.0, 58.0, MADE_TEXT, False)
    real = preview.prepare(sv.db, profile, design, "ignored", clip=clip)
    assert (real.job.start, real.job.end) == (40.0, 58.0) and len(real.spans) == 4 and real.spans[-1] == (15.0, 18.0)
    assert real.job.text.title_lines == ["THE REAL HEADLINE", "SAID IN TWO LINES"] and real.job.text.points[0][0] == "Real point one"
    assert real.job.words and all(40.0 <= w["t0"] <= 58.0 for w in real.job.words)       # the clip's own words
    assert sample.job.text.title_lines != real.job.text.title_lines and set(sample.keys).isdisjoint(real.keys)
    campaign = preview.prepare(sv.db, profile, design, "ignored", clip=preview.RealClip("talkyvideo1", 40.0, 58.0, MADE_TEXT, True))
    kinds = {e["type"] for e in campaign.job.design["elements"]}
    assert kinds == {"headline", "label"} and {e["type"] for e in real.job.design["elements"]} >= {"progress_bar", "handle"}   # no brand marks on a campaign clip


@needs_ffmpeg
def test_a_real_clip_without_kept_text_is_previewed_with_the_designs_example_text(clip_library):
    sv, profile = clip_library
    old = preview.RealClip("talkyvideo1", 40.0, 58.0, None, False)
    got = preview.prepare(sv.db, profile, {"elements": [elements.default_element("headline")]}, "x", clip=old)
    assert got.job.text.title_lines == preview.SAMPLE_TEXT.title_lines


@needs_ffmpeg
def test_the_stage_of_a_real_clip_is_framed_from_that_clip(clip_library, tmp_path):
    sv, _ = clip_library
    clip = preview.RealClip("talkyvideo1", 40.0, 58.0, MADE_TEXT, False)
    got = preview.stage(sv.db, "ignored", tmp_path / "stills", {}, clip=clip)
    assert got["length"] == 18.0 and got["words"] and all(0 <= w["t0"] <= 18.5 for w in got["words"])
    assert (tmp_path / "stills" / got["still"].rsplit("/", 1)[1]).exists()


def test_the_live_preview_endpoint_builds_the_real_clip_when_given_one(sv, client, monkeypatch):
    clip_id = add_clip(sv, state="review", video_id="v1video", start=40.0, end=58.0, grade={"look_text": MADE_TEXT.saved()})
    seen = {}

    def fake_prepare(db, profile, design, video_id, draft=False, clip=None):
        seen["clip"] = clip
        raise preview.PreviewError("stop here")

    monkeypatch.setattr(preview, "prepare", fake_prepare)
    r = client.post("/api/formats/preview/start", json={"design": {}, "video_id": "", "clip_id": clip_id, "profile": "solved"})
    assert r.status_code == 422 and r.json()["error"] == "stop here"
    assert seen["clip"].video_id == "v1video" and (seen["clip"].start, seen["clip"].end) == (40.0, 58.0) and seen["clip"].text == MADE_TEXT
    gone = client.post("/api/formats/preview/start", json={"design": {}, "video_id": "", "clip_id": 99999, "profile": "solved"})
    assert gone.status_code == 422 and gone.json()["error"] == "There is no such clip."
