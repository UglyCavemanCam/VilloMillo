"""AI Generate: three checked formats from a few answers, each with a preview, saved only when one is picked."""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_research import make_sv

from villomillo import claude
from villomillo.db import Database
from villomillo.formats import designs, generate, preview, templates
from villomillo.web.app import create_app

ANSWERS = {"kind": "podcast", "feel": "balanced", "campaign": False, "logo_id": None, "note": ""}


def fake_preview(out_dir):
    """A stand-in for the real preview render: a file with a name the preview route accepts."""
    def make(db, profile, design, video_id, folder):
        path = pathlib.Path(folder) / f"format-preview-{abs(hash(json.dumps(design, sort_keys=True))) % 16**12:012x}.mp4"
        path.write_bytes(b"x")
        return path
    return make


@pytest.fixture
def ready(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "sample_videos", lambda db: [{"id": "vid", "title": "A video"}])
    monkeypatch.setattr(preview, "make_preview", fake_preview(tmp_path))
    return Database(tmp_path / "g.db")


def reply(*options, cost=0.01):
    data = {"options": list(options)}
    return (data, cost)


SLAM = {"name": "Bold", "summary": "Big yellow words.", "design": {"captions": {"style": "spotlight"}}}
NEON = {"name": "Night", "summary": "Plain words.", "design": {"captions": {"style": "underline"}, "layout": "fit_blur"}}
BURST = {"name": "Punch", "summary": "Bursts.", "design": {"captions": {"style": "pop"}, "effects": {"intensity": "punchy"}}}
CALM_PUNCHY = {"name": "Calm punch", "summary": "x", "design": {"captions": {"style": "underline"}, "effects": {"intensity": "punchy"}}}


def test_three_options_come_back_each_with_a_preview(ready):
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda facts: reply(SLAM, NEON, BURST))
    assert [o["name"] for o in result["options"]] == ["Bold", "Night", "Punch"]
    assert all(o["preview"].startswith("/formats/preview/format-preview-") for o in result["options"])
    assert all(o["design"]["made_by"] == "ai" for o in result["options"])


def test_a_calm_feel_never_gets_punchy_effects_whatever_the_ai_proposed(ready):
    calm = {**ANSWERS, "feel": "calm"}
    result = generate.run(ready, None, calm, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(CALM_PUNCHY))
    assert result["options"][0]["design"]["effects"]["intensity"] == "subtle"


def test_campaign_options_never_carry_a_logo_button_handle_or_progress_bar(ready):
    campaign = {**ANSWERS, "campaign": True}
    loud = {"name": "Loud", "summary": "x", "design": {"brand": {"watermark": True, "progress_bar": True},
                                                      "cta": {"enabled": True, "text": "Follow"}, "captions": {"style": "spotlight"}}}
    option = generate.run(ready, None, campaign, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(loud))["options"][0]
    assert option["design"]["brand"] == {"watermark": False, "progress_bar": False}
    assert option["design"]["cta"]["enabled"] is False and option["design"]["logo"]["enabled"] is False


def test_an_option_that_breaks_a_rule_is_left_out_with_its_reason_and_the_rest_stay(ready):
    unreadable = {"name": "Dark", "summary": "x", "design": {"headline": {"color": "#111111", "background": "#151515"}}}
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(unreadable, SLAM))
    assert [o["name"] for o in result["options"]] == ["Bold"]
    assert result["dropped"] and "too close in colour" in result["dropped"][0]


def test_two_identical_options_keep_only_one(ready):
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(SLAM, SLAM))
    assert len(result["options"]) == 1 and "same as another option" in result["dropped"][0]


def test_with_no_downloaded_video_it_says_to_download_one_and_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(preview, "sample_videos", lambda db: [])
    called = []
    with pytest.raises(generate.GenerateError, match="Download a video first"):
        generate.run(Database(tmp_path / "e.db"), None, ANSWERS, SimpleNamespace(id="p"), tmp_path, ask=lambda f: called.append(f))
    assert called == []


def test_a_request_over_the_cost_limit_keeps_nothing(ready):
    with pytest.raises(generate.GenerateError, match="cent limit"):
        generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(SLAM, cost=0.5))


def test_bad_answers_are_asked_again_in_plain_words():
    with pytest.raises(generate.GenerateError, match="kind of clips"):
        generate.check_answers({"kind": "vlog", "feel": "calm"})
    with pytest.raises(generate.GenerateError, match="how it should feel"):
        generate.check_answers({"kind": "podcast", "feel": "angry"})


def test_picking_an_option_saves_it_as_an_ai_template_for_the_chosen_kinds(ready):
    saved = generate.save(ready, ANSWERS, "My bold look", SLAM["design"] | {}, ["Podcasts"])
    assert saved["name"] == "My bold look" and saved["settings"]["made_by"] == "ai"
    assert templates.for_category(ready, "podcasts")["id"] == saved["id"]


def test_a_picked_option_is_checked_again_before_it_is_saved(ready):
    with pytest.raises(designs.DesignError, match="too close in colour"):
        generate.save(ready, ANSWERS, "Dark", {"headline": {"color": "#111111", "background": "#151515"}}, ["Podcasts"])
    assert templates.list_templates(ready) == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch)
    monkeypatch.setattr(preview, "sample_videos", lambda db: [{"id": "vid", "title": "A video"}])
    monkeypatch.setattr(preview, "make_preview", fake_preview(tmp_path))
    monkeypatch.setattr(claude, "_client", lambda: None)
    monkeypatch.setattr(claude, "generate_formats", lambda client, facts: reply(SLAM, NEON, BURST))
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780"), sv


def test_the_generate_route_returns_three_options_and_the_pick_route_saves_one(client):
    http, sv = client
    r = http.post("/api/formats/generate", json={**ANSWERS, "profile": "solved"})
    assert r.status_code == 200 and len(r.json()["options"]) == 3
    option = r.json()["options"][0]
    saved = http.post("/api/formats/generate/pick", json={**ANSWERS, "name": "Pick me", "design": option["design"], "categories": ["Podcasts"]})
    assert saved.status_code == 200
    assert templates.get(sv.db, saved.json()["id"])["name"] == "Pick me"


def test_the_generate_route_says_plainly_when_the_ai_cannot_answer(client, monkeypatch):
    http, _ = client

    def broken(client, facts):
        raise RuntimeError("network down")
    monkeypatch.setattr(claude, "generate_formats", broken)
    r = http.post("/api/formats/generate", json={**ANSWERS, "profile": "solved"})
    assert r.status_code == 502 and "could not make the formats right now" in r.json()["error"]


def test_the_template_list_marks_an_ai_made_template(client):
    http, sv = client
    http.post("/api/formats/generate/pick", json={**ANSWERS, "name": "AI one", "design": SLAM["design"], "categories": ["Podcasts"]})
    page = http.get("/formats").text
    assert "New AI Generate" in page and 'id="ai-generate"' in page
    data = json.loads(page.split('<script id="boot" type="application/json">')[1].split("</script>")[0]
                      .replace("\u0027", "'").replace("\u003c", "<").replace("\u003e", ">").replace("\u0026", "&"))
    ai = [x for x in data["templates"] if x["design"].get("made_by") == "ai"]
    assert [x["name"] for x in ai] == ["AI one"]


def test_a_setting_the_app_does_not_have_is_refused_not_ignored(ready):
    made_up = {"name": "Typo", "summary": "x", "design": {"caption_style": "slam"}}
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(made_up, SLAM))
    assert [o["name"] for o in result["options"]] == ["Bold"]
    assert "'caption_style' is not a setting the app has" in result["dropped"][0]


def test_a_malformed_option_is_dropped_and_does_not_stop_the_others(ready):
    broken = {"name": "Broken", "summary": "x", "design": {"captions": "slam"}}
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(broken, SLAM))
    assert [o["name"] for o in result["options"]] == ["Bold"] and result["dropped"]


def test_the_ai_never_turns_always_music_on_by_itself(ready):
    always = {"name": "Loud music", "summary": "x", "design": {"music": {"mode": "always", "volume": -24}, "captions": {"style": "spotlight"}}}
    option = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(always))["options"][0]
    assert option["design"]["music"]["mode"] == "when_helps"


def test_a_stream_is_previewed_on_a_video_with_a_webcam_and_says_so_when_there_is_none(ready):
    videos = [{"id": "podcast", "title": "A podcast"}, {"id": "stream", "title": "A stream"}]
    picked, note = generate.preview_video(ready, "stream", videos, webcam=lambda src: {"x": 0} if "stream" in str(src) else None)
    assert picked["id"] == "stream" and note == ""
    picked, note = generate.preview_video(ready, "stream", videos, webcam=lambda src: None)
    assert picked["id"] == "podcast" and "No downloaded stream video has a webcam" in note
    assert generate.preview_video(ready, "podcast", videos)[0]["id"] == "podcast"


# --- the playbook, feedback and the frame check -----------------------------------------------------------------------

STREAM = {**ANSWERS, "kind": "stream", "feel": "high"}


def test_a_podcast_is_refused_a_stream_style_and_a_stream_is_allowed_it(ready):
    slam_podcast = {"name": "Slam", "summary": "x", "design": {"captions": {"style": "slam"}}}
    result = generate.run(ready, None, ANSWERS, SimpleNamespace(id="p"), ready.path.parent, ask=lambda f: reply(slam_podcast, SLAM))
    assert [o["name"] for o in result["options"]] == ["Bold"]
    assert "does not suit podcasts" in result["dropped"][0]
    slam_stream = {"name": "Slam", "summary": "x", "design": {"captions": {"style": "slam"}, "layout": "stacked_webcam"}}
    assert generate.checked(slam_stream["design"], STREAM, ready)["captions"]["style"] == "slam"


def test_a_layout_outside_the_playbook_is_refused_with_the_allowed_ones(ready):
    with pytest.raises(designs.DesignError, match="does not suit podcasts; use one of: face_fill, fit_blur"):
        generate.checked({"layout": "stacked_webcam"}, ANSWERS, ready)


def test_headline_text_must_reach_the_contrast_the_playbook_asks_for(ready):
    weak = {"headline": {"color": "#777777", "background": "#FFFFFF"}}
    with pytest.raises(designs.DesignError, match="only 4.48 to 1 against its box"):
        generate.checked(weak, ANSWERS, ready)
    assert generate.checked({"headline": {"color": "#000000", "background": "#FFFFFF"}}, ANSWERS, ready)["headline"]["background"] == "#FFFFFF"


def test_likes_and_dislikes_are_remembered_and_shown_to_the_ai_for_that_kind(ready):
    generate.record_feedback(ready, ANSWERS, SLAM["design"] | {"layout": "face_fill"}, "more")
    generate.record_feedback(ready, ANSWERS, NEON["design"], "less")
    generate.record_feedback(ready, STREAM, BURST["design"], "more")
    b = generate.brief(ANSWERS, ready)
    assert [x["captions"] for x in b["looks_the_owner_liked"]] == ["spotlight"]
    assert [x["captions"] for x in b["looks_the_owner_disliked"]] == ["underline"]
    assert generate.recent_feedback(ready, "stream")["liked"][0]["captions"] == "pop"
    with pytest.raises(generate.GenerateError, match="More like this or Not this"):
        generate.record_feedback(ready, ANSWERS, SLAM["design"], "maybe")


def test_the_brief_tells_the_ai_the_playbook_rules_and_only_the_allowed_choices():
    b = generate.brief({**ANSWERS, "feel": "calm"})
    assert b["playbook_rules"] and b["catalog"]["layouts"] == ["face_fill", "fit_blur"]
    assert {s["id"] for s in b["catalog"]["caption_styles"]} <= {"dim", "plate", "underline", "build"}
    assert b["catalog"]["effect_levels"] == ["off", "subtle"]


def test_a_caption_over_a_face_in_the_picture_is_caught(tmp_path):
    import cv2
    import numpy as np

    blank = tmp_path / "blank.mp4"
    writer = cv2.VideoWriter(str(blank), cv2.VideoWriter_fourcc(*"mp4v"), 10, (1080, 1920))
    for _ in range(40):
        writer.write(np.full((1920, 1080, 3), 120, np.uint8))
    writer.release()
    assert generate.picture_problems(blank, designs.normalize({})) == []


def test_the_feedback_route_remembers_a_look(client):
    http, sv = client
    r = http.post("/api/formats/generate/feedback", json={**ANSWERS, "design": SLAM["design"], "verdict": "more"})
    assert r.status_code == 200
    assert generate.recent_feedback(sv.db, "podcast")["liked"][0]["captions"] == "spotlight"
    assert http.post("/api/formats/generate/feedback", json={**ANSWERS, "design": {"layout": "nope"}, "verdict": "more"}).status_code == 422


def test_neon_captions_are_refused_over_a_bright_picture_and_kept_over_a_dark_one(tmp_path):
    import cv2
    import numpy as np

    def clip(name, shade):
        path = tmp_path / name
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (1080, 1920))
        for _ in range(40):
            writer.write(np.full((1920, 1080, 3), shade, np.uint8))
        writer.release()
        return path
    neon = designs.normalize({"captions": {"style": "neon"}})
    assert any("hard to read" in p for p in generate.picture_problems(clip("bright.mp4", 230), neon))
    assert generate.picture_problems(clip("dark.mp4", 30), neon) == []
    assert generate.picture_problems(clip("bright2.mp4", 230), designs.normalize({"captions": {"style": "spotlight"}})) == []
