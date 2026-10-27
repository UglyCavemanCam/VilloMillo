"""The Formats page: saving and using templates and presets, the plain-English errors, the preview, and Edit look on a clip."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from test_research import make_sv

from villomillo import relook, store
from villomillo.formats import designs, templates
from villomillo.web.app import create_app
from villomillo.web.pages import STATIC


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch)


@pytest.fixture
def client(sv):
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780")


def boot(client, query: str = "") -> dict:
    page = client.get("/formats" + query).text
    return json.loads(page.split('<script id="boot" type="application/json">')[1].split("</script>")[0].replace("\\u0027", "'").replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))


def test_the_page_has_the_menu_entry_the_tabs_the_presets_and_all_twelve_styles(client):
    page = client.get("/formats").text
    assert 'href="/formats?profile=solved"' in page and "Quick Presets" in page and "My Templates" in page
    data = boot(client)
    assert [p["name"] for p in data["presets"]] == ["Podcast", "Stream reaction"] and len(data["styles"]) == 12
    assert {"Podcasts", "Stream clips", "Talks", "Other"} <= set(data["categories"]) and data["templates"] == []


def test_using_a_preset_makes_a_template_and_gives_the_category_its_format(sv, client):
    r = client.post("/api/formats/preset", json={"preset_id": "podcast", "action": "use", "category": "Podcasts"})
    assert r.status_code == 200
    assert templates.for_category(sv.db, "Podcasts")["name"] == "Podcast"
    assert boot(client)["templates"][0]["categories"] == ["Podcasts"]


def test_copying_a_preset_leaves_every_category_without_a_format(sv, client):
    assert client.post("/api/formats/preset", json={"preset_id": "podcast", "action": "copy"}).status_code == 200
    assert len(templates.list_templates(sv.db)) == 1 and templates.assignments(sv.db) == {}


def test_a_deleted_preset_is_gone_for_good(sv, client):
    assert client.post("/api/formats/preset-delete", json={"preset_id": "podcast"}).status_code == 200
    assert [p["id"] for p in boot(client)["presets"]] == ["stream"]
    assert client.post("/api/formats/preset", json={"preset_id": "podcast", "action": "copy"}).status_code == 404
    assert client.post("/api/formats/preset-delete", json={"preset_id": "podcast"}).status_code == 200   # deleting twice changes nothing
    assert client.post("/api/formats/preset-delete", json={"preset_id": "nonsense"}).status_code == 404
    assert [p["id"] for p in boot(client)["presets"]] == ["stream"]


def test_saving_a_design_stores_it_and_moves_the_categories_to_it(sv, client):
    first = client.post("/api/formats/save", json={"name": "Talk", "design": {"captions": {"style": "slam"}}, "categories": ["Podcasts"]}).json()
    assert templates.for_category(sv.db, "podcasts")["id"] == first["id"]
    second = client.post("/api/formats/save", json={"name": "Other talk", "design": {}, "categories": ["Podcasts", "Talks"]}).json()
    assert templates.for_category(sv.db, "Podcasts")["id"] == second["id"] and templates.for_category(sv.db, "Talks")["id"] == second["id"]
    client.post("/api/formats/save", json={"id": second["id"], "name": "Other talk", "design": {}, "categories": ["Talks"]})
    assert templates.for_category(sv.db, "Podcasts") is None and templates.for_category(sv.db, "Talks") is not None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"name": "", "design": {}}, "give the template a name"),
        ({"name": "X", "design": {"captions": {"size": 999}}}, "caption size must be a whole number from 40 to 160"),
        ({"name": "X", "design": {"headline": {"color": "#FFB000", "background": "#FFB400"}}}, "too close in colour to read"),
        ({"name": "X", "design": {"captions": {"style": "disco"}}}, "no caption style called disco"),
    ],
)
def test_a_bad_design_is_refused_in_plain_words_and_nothing_is_saved(sv, client, body, message):
    r = client.post("/api/formats/save", json=body)
    assert r.status_code == 422 and message in r.json()["error"]
    assert templates.list_templates(sv.db) == []


def test_deleting_a_template_is_permanent_and_names_the_categories_that_lose_it(sv, client):
    t = client.post("/api/formats/save", json={"name": "Talk", "design": {}, "categories": ["Podcasts", "Talks"]}).json()
    lost = client.post("/api/formats/delete", json={"id": t["id"]}).json()["lost"]
    assert sorted(lost) == ["podcasts", "talks"] and templates.get(sv.db, t["id"]) is None


def test_a_preview_without_a_usable_video_or_with_a_bad_design_says_why(client):
    r = client.post("/api/formats/preview", json={"design": {}, "video_id": "nothing-here"})
    assert r.status_code == 422 and "not downloaded" in r.json()["error"]
    r = client.post("/api/formats/preview", json={"design": {"captions": {"size": 1}}, "video_id": "x"})
    assert r.status_code == 422 and "caption size" in r.json()["error"]


@pytest.mark.parametrize("name", ["../x.mp4", "format-preview-zzzz.mp4", "format-preview-0123456789ab.mp4"])
def test_only_real_previews_can_be_fetched(client, name):
    assert client.get(f"/formats/preview/{name}").status_code == 404


def add_clip(sv, state="review", file_path="/tmp/clip.mp4") -> int:
    with sv.db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, validation_json, grade_json, state, reason, "
                  "created_at, category, file_path, format_applied) VALUES ('solved', 5, 'abcdefghijk', 0, 30, '{}', '{}', ?, "
                  "'passed checks; waiting for your decision; needs a format: x', ?, 'Podcasts', ?, 0)", (state, store.iso(), file_path))
        return c.execute("SELECT id FROM clips_ready").fetchone()["id"]


def test_edit_look_queues_a_look_for_one_clip_and_refuses_bad_designs_or_clips_not_in_review(sv, client):
    clip_id = add_clip(sv)
    bad = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": {"captions": {"size": 999}}})
    assert bad.status_code == 422 and "caption size" in bad.json()["error"]
    ok = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": {"captions": {"style": "burst"}}})
    assert ok.status_code == 200
    with sv.db.conn() as c:
        task = c.execute("SELECT payload_json FROM tasks WHERE kind='relook'").fetchone()
    assert json.loads(task["payload_json"])["design"]["captions"]["style"] == "burst"
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET state='rejected'")
    gone = client.post("/api/formats/apply-clip", json={"profile": "solved", "clip_id": clip_id, "design": {}})
    assert gone.status_code == 409 and "was rejected" in gone.json()["error"]


def test_the_formats_page_opened_from_a_clip_knows_the_clip(sv, client):
    clip_id = add_clip(sv)
    data = boot(client, f"?clip={clip_id}&profile=solved")
    assert data["clip"]["id"] == clip_id and data["clip"]["profile"] == "solved"


def test_the_clip_panel_links_to_edit_look(sv, client):
    clip_id = add_clip(sv)
    panel = client.get(f"/clips/{clip_id}/panel?profile=solved").text
    assert f"/formats?clip={clip_id}&amp;profile=solved" in panel.replace("&profile", "&amp;profile") and "Edit look" in panel


def test_a_look_made_for_one_clip_is_applied_to_that_clip_only(sv, monkeypatch, tmp_path):
    from villomillo import formats

    clip_id = add_clip(sv, file_path=str(tmp_path / "clip.mp4"))
    seen = {}

    def fake_make_formatter(db, profile, template, **kw):
        seen["template"] = template

        def formatter(clip, job, out):
            out.write_bytes(b"edited")
            seen["job"] = job
            return {"template": template["id"]}
        return formatter

    monkeypatch.setattr(formats, "make_formatter", fake_make_formatter)
    monkeypatch.setattr(relook.validate, "check", lambda path, **k: {"ok": True, "problems": [], "warnings": [], "facts": {"duration_s": 30}})
    relook.apply(sv, clip_id, design={"captions": {"style": "burst"}})
    assert (tmp_path / "clip.mp4").read_bytes() == b"edited" and seen["template"]["settings"]["captions"]["style"] == "burst"
    with sv.db.conn() as c:
        row = c.execute("SELECT format_applied, template_id, grade_json, reason FROM clips_ready").fetchone()
    grade = json.loads(row["grade_json"])
    assert row["format_applied"] == 1 and row["template_id"] is None and grade["look"] == "edited look"
    assert grade["look_design"]["captions"]["style"] == "burst" and "needs a format" not in row["reason"]
    assert templates.list_templates(sv.db) == []   # the template list is untouched
    with pytest.raises(relook.RelookError, match="caption size"):
        relook.apply(sv, clip_id, design={"captions": {"size": 1}})
    assert designs.normalize({})["captions"]["size"] is None


def test_a_logo_is_uploaded_served_and_deleted_from_the_assets_tab(sv, client, tmp_path, monkeypatch):
    from villomillo.formats import assets

    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    picture = tmp_path / "logo.png"
    from PIL import Image

    Image.new("RGBA", (300, 120), (255, 176, 0, 255)).save(picture)
    up = client.post("/api/assets/logo?name=logo.png", content=picture.read_bytes(), headers={"Content-Type": "application/octet-stream"})
    assert up.status_code == 200 and up.json()["name"] == "logo.png"
    logo_id = up.json()["id"]
    assert [l["id"] for l in boot(client)["logos"]] == [logo_id]
    served = client.get(f"/assets/{logo_id}/file")
    assert served.status_code == 200 and served.headers["content-type"] == "image/png"
    bad = client.post("/api/assets/logo?name=x.txt", content=b"not a picture", headers={"Content-Type": "application/octet-stream"})
    assert bad.status_code == 422 and "not a picture" in bad.json()["error"]
    assert client.post("/api/assets/delete", json={"id": logo_id}).status_code == 200
    assert boot(client)["logos"] == [] and client.get(f"/assets/{logo_id}/file").status_code == 404


def test_a_template_with_a_logo_that_is_not_there_is_not_saved(sv, client):
    body = {"name": "Logo", "design": {"logo": {"enabled": True, "asset_id": 999}}}
    r = client.post("/api/formats/save", json=body)
    assert r.status_code == 422 and "The logo is missing" in r.json()["error"]
    assert templates.list_templates(sv.db) == []


# --- the Music tab ----------------------------------------------------------------------------------------------------------

def test_the_music_tab_starts_with_the_whole_shortlist_still_to_download(client):
    data = boot(client)["music"]
    assert data["tracks"] == [] and len(data["checklist"]) == 42 and not any(c["have"] for c in data["checklist"])
    assert "calm" in data["moods"] and "Podcasts" in data["categories"] and data["source"] == "YouTube Audio Library"
    assert 'id="tab-music"' in client.get("/formats").text


def test_importing_from_downloads_files_shortlisted_tracks_and_reports_what_it_did(sv, client, tmp_path, monkeypatch):
    from test_music import tone

    from villomillo.formats import music

    monkeypatch.setattr(music, "FOLDER", tmp_path / "library")
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    monkeypatch.setattr(music, "DOWNLOADS", downloads)
    tone(downloads / "Pulsar - The Grey Room _ Density & Time.mp3")
    r = client.post("/api/music/import", json={})
    assert r.status_code == 200 and r.json()["imported"] == ["Pulsar"]
    data = boot(client)["music"]
    assert [t["title"] for t in data["tracks"]] == ["Pulsar"] and next(c for c in data["checklist"] if c["title"] == "Pulsar")["have"]
    assert client.post("/api/music/import", json={}).json()["imported"] == []   # a second press adds nothing new


def test_a_track_is_added_played_edited_and_deleted_from_the_music_tab(sv, client, tmp_path, monkeypatch):
    from test_music import LICENCE, tone

    from villomillo.formats import music

    monkeypatch.setattr(music, "FOLDER", tmp_path / "library")
    audio = tone(tmp_path / "song.mp3").read_bytes()
    query = f"filename=song.mp3&title=My+song&artist=Me&mood=calm&categories=Podcasts,Talks&source=Mine&licence={LICENCE}"
    up = client.post("/api/music/upload?" + query, content=audio, headers={"Content-Type": "application/octet-stream"})
    assert up.status_code == 200
    track_id = up.json()["id"]
    played = client.get(f"/music/{track_id}/file")
    assert played.status_code == 200 and played.headers["content-type"] == "audio/mpeg"
    bad = client.post("/api/music/upload?" + query.replace("mood=calm", "mood=grumpy"), content=audio, headers={"Content-Type": "application/octet-stream"})
    assert bad.status_code == 422 and "Pick one of the moods" in bad.json()["error"]
    wrong = client.post("/api/music/upload?" + query.replace("song.mp3", "song.flac"), content=audio, headers={"Content-Type": "application/octet-stream"})
    assert wrong.status_code == 422 and "MP3 or WAV" in wrong.json()["error"]
    edit = client.post("/api/music/update", json={"id": track_id, "mood": "sad", "categories": ["Campaigns"]})
    assert edit.status_code == 200
    track = boot(client)["music"]["tracks"][0]
    assert track["mood"] == "sad" and track["categories"] == ["Campaigns"]
    assert client.post("/api/music/update", json={"id": track_id, "mood": "sad", "categories": []}).status_code == 422
    assert client.post("/api/music/delete", json={"id": track_id}).status_code == 200
    assert boot(client)["music"]["tracks"] == [] and client.get(f"/music/{track_id}/file").status_code == 404


def test_remake_without_effects_queues_the_clip_with_its_look_and_no_effects(sv, client):
    clip_id = add_clip(sv)
    with sv.db.conn() as c:
        c.execute("UPDATE clips_ready SET grade_json=? WHERE id=?",
                  (json.dumps({"look_design": {"captions": {"style": "burst"}, "effects": {"intensity": "balanced", "kinds": ["pop"]}}}), clip_id))
    r = client.post("/api/clips/remake-without-effects", data={"profile": "solved", "clip_id": clip_id})
    assert r.status_code == 200
    with sv.db.conn() as c:
        task = c.execute("SELECT payload_json FROM tasks WHERE kind='relook' ORDER BY id DESC").fetchone()
    design = json.loads(task["payload_json"])["design"]
    assert design["effects"]["intensity"] == "off" and design["captions"]["style"] == "burst"
    assert client.post("/api/clips/remake-without-effects", data={"profile": "solved", "clip_id": 999}).status_code == 404


def test_the_stage_refuses_a_video_that_is_not_offered_and_serves_only_real_stills(client):
    r = client.post("/api/formats/stage", json={"video_id": "nothing-here"})
    assert r.status_code == 422 and "not available" in r.json()["error"]
    assert client.get("/formats/still/format-still-zzzz.jpg").status_code == 404
    assert client.get("/formats/still/../x.jpg").status_code == 404


def test_only_the_fonts_the_formats_offer_are_served_to_the_stage(client):
    ok = client.get("/formats/font/montserrat-black")
    assert ok.status_code == 200 and ok.headers["content-type"] == "font/ttf"
    assert client.get("/formats/font/not-a-font").status_code == 404


@pytest.fixture
def stage_video(tmp_path, monkeypatch):
    """A 40-second sample video the stage can use, with a few spoken words in the stretch it picks."""
    from villomillo.formats import preview
    from villomillo.web import app as web_app

    source = tmp_path / "sample.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25",
                    "-t", "40", "-pix_fmt", "yuv420p", str(source)], check=True)
    words = [{"t0": 31.0 + i, "t1": 31.5 + i, "text": f"word{i}"} for i in range(5)]
    monkeypatch.setattr(preview, "sample_videos", lambda db: [{"id": "sample", "title": "Sample"}])
    monkeypatch.setattr(preview, "_source", lambda video_id: source)
    monkeypatch.setattr(preview, "clip_words", lambda video_id, start, end: [w for w in words if start <= w["t0"] < end])
    monkeypatch.setattr(web_app, "PREVIEWS", tmp_path / "previews")
    return words


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


@needs_ffmpeg
def test_the_stage_makes_a_still_cropped_to_the_picture_with_the_sample_words(client, stage_video):
    from villomillo.formats import preview

    r = client.post("/api/formats/stage", json={"video_id": "sample"})
    assert r.status_code == 200
    body = r.json()
    assert body["length"] == preview.SAMPLE_LENGTH == designs.DEMO_LENGTH and body["at"] == preview.STILL_AT and body["warning"] is None
    assert [w["text"] for w in body["words"]] == [w["text"] for w in stage_video]
    assert body["words"][0]["t0"] == 0.0   # times count from the still's sample start
    still = client.get(body["still"])
    assert still.status_code == 200 and still.headers["content-type"] == "image/jpeg"


@needs_ffmpeg
def test_the_stage_frame_follows_the_layout_and_the_webcam_box(client, stage_video):
    import io

    from PIL import Image

    def frame(**design):
        r = client.post("/api/formats/stage", json={"video_id": "sample", "design": design})
        assert r.status_code == 200
        body = r.json()
        return body["still"], Image.open(io.BytesIO(client.get(body["still"]).content)), body["warning"]

    fill_name, fill_img, fill_warning = frame(layout="face_fill")
    blur_name, blur_img, _ = frame(layout="fit_blur")
    cam_name, cam_img, _ = frame(layout="stacked_webcam")
    moved_name, _, _ = frame(layout="stacked_webcam", webcam={"x": 40, "y": 30, "w": 30, "h": 30})
    assert fill_img.size == blur_img.size == cam_img.size == (540, 960) and fill_warning is None
    assert len({fill_name, blur_name, cam_name, moved_name}) == 4   # each layout, and each webcam box, is its own frame
    # fit with blur keeps the whole 16:9 picture in the middle with the blurred copy above it: the top rows are not the picture
    assert blur_img.getpixel((270, 20)) != fill_img.getpixel((270, 20))


@needs_ffmpeg
def test_a_layout_that_cannot_frame_the_video_says_why_and_shows_the_plain_middle(client, stage_video):
    r = client.post("/api/formats/stage", json={"video_id": "sample", "design": {"layout": "split"}})
    body = r.json()
    assert r.status_code == 200 and "two people" in body["warning"]
    assert client.get(body["still"]).status_code == 200


def test_the_save_check_assumes_a_clip_as_long_as_the_demo(client):
    import io

    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (200, 200), (255, 0, 0)).save(png, "PNG")
    up = client.post("/api/assets/logo", params={"name": "logo.png"}, content=png.getvalue(), headers={"Content-Type": "application/octet-stream"})
    assert up.status_code == 200
    # a logo that starts at second 30 and a button in the last 3 seconds share the same corner: they only meet in a clip longer than 30 s
    design = {"logo": {"enabled": True, "asset_id": up.json()["id"], "size": 14, "from_s": 30},
              "cta": {"enabled": True, "text": "Follow", "seconds": 3}, "place": {"logo_y": 190, "cta_y": 230}}
    found = client.post("/api/formats/check", json={"design": design}).json()["problems"]
    assert found and "logo" in found[0].lower()


def test_a_dragged_position_is_kept_and_a_bad_one_is_refused_in_plain_words():
    d = designs.normalize({"place": {"headline_y": 500, "logo_x": 40}})
    assert d["place"]["headline_y"] == 500 and d["place"]["logo_x"] == 40 and d["place"]["captions_y"] is None
    assert designs.place_of(d, "headline_y", 430) == 500 and designs.place_of(d, "captions_y", 1345) == 1345
    with pytest.raises(designs.DesignError, match="The headline's height must be a whole number from 0 to 1920"):
        designs.normalize({"place": {"headline_y": 2500}})


def test_a_logo_dragged_onto_the_captions_is_reported_and_the_default_spot_is_clear():
    from villomillo.formats import placement
    clear = designs.normalize({"logo": {"enabled": True, "asset_id": 1, "size": 14}})
    assert placement.problems(clear, "face_fill", (151, 60), 30.0, 7.0) == []
    covered = designs.normalize({"logo": {"enabled": True, "asset_id": 1, "size": 14}, "place": {"logo_y": 1300}})
    found = placement.problems(covered, "face_fill", (151, 60), 30.0, 7.0)
    assert found and "would cover the captions" in found[0]


def test_the_live_check_says_what_a_drag_covers_and_saves_nothing(sv, client):
    body = {"design": {"place": {"headline_y": 1300}}}   # the headline now sits on the captions
    r = client.post("/api/formats/check", json=body)
    assert r.status_code == 200 and r.json()["problems"] and "headline" in r.json()["problems"][0].lower()
    assert client.get("/formats").status_code == 200 and templates.list_templates(sv.db) == []
    clear = client.post("/api/formats/check", json={"design": {}})
    assert clear.status_code == 200 and clear.json()["problems"] == []
    bad = client.post("/api/formats/check", json={"design": {"place": {"cta_y": -5}}})
    assert bad.status_code == 422 and "The button's height" in bad.json()["error"]


def test_a_dragged_position_is_saved_with_the_template_and_comes_back_in_the_builder(sv, client):
    r = client.post("/api/formats/save", json={"id": None, "name": "Dragged", "design": {"place": {"headline_y": 500, "logo_x": 40}}, "categories": []})
    assert r.status_code == 200
    saved = templates.list_templates(sv.db)[0]["settings"]
    assert saved["place"]["headline_y"] == 500 and saved["place"]["logo_x"] == 40 and saved["place"]["cta_y"] is None
    assert boot(client)["templates"][0]["design"]["place"]["headline_y"] == 500


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_builders_script_is_valid_javascript(tmp_path):
    script = (STATIC / "formats-builder.js").read_text(encoding="utf-8")
    for name in ("renderStage", "loadStage", "renderSettings", "renderLayers", "addElement", "checkPlace", "fill", "read"):
        assert script.count(f"function {name}(") == 1   # a mis-spliced edit once repeated a block of the script
    path = tmp_path / "formats.js"
    path.write_text(script, encoding="utf-8")
    done = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
