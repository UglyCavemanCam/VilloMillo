"""Phase 7 of the format builder: the checks that keep the preview honest and the messages plain. A refused look says why in words (too small to read,
hidden behind the buttons, two elements on top of each other); the numbers the stage draws from are the places the real render puts things; and every
bundled and uploaded font draws in the headline, the labels, the captions and a finished clip."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_formats_bench import library, needs_ffmpeg, write_transcript  # noqa: F401  (library is a fixture)
from test_live_preview import frame_at, length_of  # noqa: F401
from test_research import make_sv

from villomillo.formats import assets, captions, designs, elements, font_uploads, fonts, overlays, placement, preview, render
from villomillo.web import app as web_app
from villomillo.web.pages import STATIC

ACTIONS = STATIC / "formats-actions.js"
GEO = {"positions": designs.POSITIONS, "headline_top": designs.HEADLINE_TOP, "logo_y": designs.LOGO_Y, "chips_y": designs.CHIPS_Y}


def design_with(*els, layout="face_fill") -> dict:
    return designs.normalize({"layout": layout, "elements": list(els)})


def element(kind: str, **over) -> dict:
    e = elements.default_element(kind)
    for key, value in over.items():
        e[key] = {**e[key], **value} if isinstance(value, dict) and isinstance(e.get(key), dict) else value
    return e


def said(d: dict) -> list[str]:
    return placement.element_problems(d, d["layout"], None, designs.DEMO_LENGTH, placement.SAVE_HEADLINE_END)


# --- plain words for every refusal -----------------------------------------------------------------------------------------------

def test_the_formats_that_ship_and_every_layout_pass_the_new_checks():
    for layout in ("face_fill", "fit_blur", "stacked_webcam", "split"):
        assert said(designs.normalize({"layout": layout})) == [], layout
    for preset in designs.PRESETS:
        assert said(designs.normalize(preset["design"])) == [], preset["id"]


@pytest.mark.parametrize("kind,size,floor", [("label", 20, 28), ("takeaway", 27, 28), ("button", 26, 28), ("text", 24, 28), ("headline", 25, 28), ("handle", 15, 20)])
def test_text_too_small_to_read_on_a_phone_is_refused_with_how_big_it_must_be(kind, size, floor):
    e = element(kind, name="Tiny one")
    e["style"]["size"] = size
    got = said(design_with(element("headline"), e) if kind != "headline" else design_with(e))
    assert f"The tiny one is too small to read on a phone: its text is {size} pixels tall, so make it at least {floor}" in got


def test_text_at_the_smallest_readable_size_is_fine():
    e = element("label")
    e["style"]["size"] = 28
    assert said(design_with(e)) == []


@pytest.mark.parametrize("x,y,where", [
    (1000, 1200, "the app's buttons on the right"), (1050, 1000, "the app's buttons on the right"),
    (540, 1800, "the clip's title and description at the bottom"), (540, 60, "the app's top bar"),
])
def test_an_element_put_where_the_apps_own_buttons_or_title_would_hide_it_is_refused_in_plain_words(x, y, where):
    got = said(design_with(element("takeaway", name="Closing line", x=x, y=y)))
    assert got == [f"The closing line would be hidden behind {where}: " + ("move it left or up" if "buttons" in where else "move it up" if "bottom" in where else "move it down")]


def test_an_element_is_only_judged_for_where_it_is_once_it_has_been_moved():
    assert said(design_with(element("takeaway"))) == []                       # the layout's own spot is clear by design
    assert said(design_with(element("takeaway", x=540, y=1200))) == []        # moved, but somewhere clear
    assert said(design_with(element("shape", x=1000, y=1800, w=300, h=300), element("label"))) == []     # a box behind things may go anywhere
    bar = element("progress_bar", y=1906)
    assert said(design_with(bar, element("label"))) == []                      # the progress bar belongs at the edge


def test_two_elements_on_top_of_each_other_at_the_same_time_are_refused_and_when_is_said():
    d = design_with(element("label", name="Point label"), element("label", id="label-2", name="Number tag"))
    assert said(d) == ["The point label and the number tag would be on top of each other from 7 to 32 seconds: move one of them"]
    same = design_with(element("label"), element("label", id="label-2"))     # both still called "Point label"
    assert said(same) == ["The two point label elements would be on top of each other from 7 to 32 seconds: move one of them"]
    clear = design_with(element("label", name="Point label"), element("label", id="label-2", name="Number tag", y=900))
    assert said(clear) == []
    sooner = element("text", name="Intro words", y=designs.CHIPS_Y["face_fill"])
    sooner["timing"] = {"mode": "first", "seconds": 3.0, "from": 0.0, "to": None}
    assert said(design_with(element("label", name="Point label"), sooner)) == []         # it is gone before the label comes
    later = dict(sooner, timing={"mode": "range", "seconds": 3.0, "from": 10.0, "to": 14.0})
    assert said(design_with(element("label", name="Point label"), later)) == ["The point label and the intro words would be on top of each other from 10 to 14 seconds: move one of them"]


def test_a_box_behind_text_and_the_handle_under_everything_are_not_overlaps():
    assert said(design_with(element("label"), element("shape", x=540, y=designs.CHIPS_Y["face_fill"], w=600, h=120))) == []
    assert said(design_with(element("headline"), element("handle", y=designs.HEADLINE_TOP["stacked_webcam"] + 120), layout="stacked_webcam")) == []


def test_a_clash_the_old_check_already_reports_is_not_said_twice():
    d = designs.normalize({"elements": [element("headline", y=1300), element("captions"), element("label")]})   # the headline now sits on the captions
    old = placement.problems(d, d["layout"], None, designs.DEMO_LENGTH, placement.SAVE_HEADLINE_END)
    assert old and "headline" in old[0]
    assert not [m for m in said(d) if "captions" in m]


@pytest.fixture
def client(tmp_path, monkeypatch):
    return TestClient(web_app.create_app(make_sv(tmp_path, monkeypatch)), base_url="http://127.0.0.1:8780")


def test_the_live_check_and_the_save_say_it_in_words(client):
    small = element("label", name="Point label")
    small["style"]["size"] = 20
    body = {"design": {"elements": [element("headline"), small]}}
    live = client.post("/api/formats/check", json=body)
    assert live.status_code == 200 and live.json()["problems"] == ["The point label is too small to read on a phone: its text is 20 pixels tall, so make it at least 28"]
    refused = client.post("/api/formats/save", json={"name": "Tiny", "design": body["design"], "categories": []})
    assert refused.status_code == 422 and refused.json()["error"] == "The point label is too small to read on a phone: its text is 20 pixels tall, so make it at least 28."
    two = {"design": {"elements": [element("headline"), element("label", name="One"), element("label", id="label-2", name="Two")]}}
    assert "on top of each other" in client.post("/api/formats/check", json=two).json()["problems"][0]
    ok = {"design": {"elements": [element("headline"), element("label", name="One"), element("label", id="label-2", name="Two", y=900)]}}
    assert client.post("/api/formats/check", json=ok).json()["problems"] == []
    assert client.post("/api/formats/save", json={"name": "Fine", "design": ok["design"], "categories": []}).status_code == 200


def test_a_clip_that_is_already_being_made_is_never_refused_by_these_checks():
    """The checks run when a look is saved and in the builder's live note. Making a clip uses `placement.problems`, which they do not touch."""
    d = designs.normalize({"elements": [element("headline"), element("label", x=1000, y=1800)]})   # hidden behind the buttons, which `element_problems` would refuse
    assert placement.element_problems(d, d["layout"], None, 30.0, 4.0)
    assert placement.problems(d, d["layout"], None, 30.0, 4.0) == []


# --- the stage and the real render put things in the same place ----------------------------------------------------------------------

def js_positions(els: list[dict], layout: str, defaults: dict | None = None) -> dict:
    code = ("const A = require(process.argv[1]); const g = JSON.parse(require('fs').readFileSync(0, 'utf8')); if (g.defaults) A.configure(g.defaults);"
            "console.log(JSON.stringify(Object.fromEntries(g.els.map(e => [e.id, A.posOf(e, g.layout, g.geo)]))))")
    done = subprocess.run(["node", "-e", code, str(ACTIONS)], input=json.dumps({"els": els, "layout": layout, "geo": GEO, "defaults": defaults}), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def evaluate(expression: str, w: int, h: int) -> float:
    assert re.fullmatch(r"[0-9WHwh+\-*/(). ]+", expression), expression
    return float(eval(expression, {"__builtins__": {}}, {"W": 1080, "H": 1920, "w": w, "h": h}))


@pytest.fixture
def grey(tmp_path, monkeypatch, library):  # noqa: F811
    """A plain grey 75 second video with a transcript and a logo, so a coloured element is easy to find in a frame."""
    downloads, transcripts = library
    write_transcript(transcripts, "talkyvideo1", [31 + 0.4 * i for i in range(90)])
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=0x303030:size=1280x720:rate=24:duration=75",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=75", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(downloads / "talkyvideo1.mp4")], check=True, timeout=180)
    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    sv = make_sv(tmp_path, monkeypatch)
    picture = tmp_path / "logo.png"
    Image.new("RGBA", (300, 120), (224, 160, 16, 255)).save(picture)
    logo = assets.save_logo(sv.db, "logo.png", picture.read_bytes())
    return sv, next(iter(sv.profiles.values())), logo["id"]


def all_elements(logo_id: int, moved: bool, button: bool = True) -> list[dict]:
    logo = element("logo", options={"asset_id": logo_id})
    els = [element("headline"), element("label"), element("takeaway"), element("handle"), element("text", text={"default": "Words", "source": "fixed"}),
           element("button", visible=button), logo, element("shape", x=540, y=960, w=300, h=100), element("progress_bar")]
    if moved:
        spots = {"label": (400, 1500), "takeaway": (300, 700), "handle": (700, 500), "text": (250, 1100), "button": (800, 800), "logo": (600, 300), "shape": (200, 1300)}
        for e in els:
            if e["type"] in spots:
                e["x"], e["y"] = spots[e["type"]]
        els[0]["y"] = 520
        els[-1]["y"] = 1850
    return els


def drawn(prep) -> dict[str, tuple[float, float, int, int]]:
    """The first picture each element lays over the clip, as the renderer places it: id -> (left, top, width, height)."""
    job, d = prep.job, designs.normalize(prep.job.design)
    out = {}
    with tempfile.TemporaryDirectory() as folder:
        for item in render._overlays(job, d, job.end - job.start, job.plan.framing().id, pathlib.Path(folder)):
            w, h = Image.open(pathlib.Path(folder) / item["file"]).size
            out.setdefault(item["element"], (evaluate(item["x"], w, h), evaluate(item["y"], w, h), w, h))
    return out


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["face_fill", "fit_blur", "stacked_webcam", "split"])
@pytest.mark.parametrize("moved", [False, True])
def test_the_model_the_stage_draws_from_is_where_the_renderer_puts_every_element(grey, layout, moved):
    sv, profile, logo_id = grey
    wide = layout in ("face_fill", "fit_blur") or moved   # the stacked layouts put the logo and the button in the same place: a clash the old check refuses
    els = all_elements(logo_id, moved, button=wide)
    clip = preview.RealClip("talkyvideo1", 40.0, 60.0, render.ClipText(["A TITLE", "IN TWO LINES"], [("Point", 6.0)], "The takeaway"), False)
    prep = preview.prepare(sv.db, profile, {"layout": layout, "elements": els}, "x", clip=clip)
    model = js_positions(prep.job.design["elements"], layout, placement_defaults())
    seen = drawn(prep)
    assert {"headline", "label", "takeaway", "handle", "text", "shape", "logo", *(["button"] if wide else [])} <= set(seen), set(seen)
    for kind, (left, top, w, h) in seen.items():
        at = model[kind]
        if kind == "headline":
            assert (left, top) == (0, at["y"]) and w == 1080, kind                       # a headline: its top edge, across the whole width
        elif kind == "logo":
            assert (left, top) == pytest.approx((at["x"], at["y"]), abs=1), kind          # a logo: its left and top edge
        else:
            assert (left + w / 2, top + h / 2) == pytest.approx((at["x"], at["y"]), abs=1), kind   # everything else: its centre
    bars = render._bars(prep.job, designs.normalize(prep.job.design), 20.0)
    assert int(re.search(r"drawbox=x=0:y=(\d+)", bars).group(1)) == model["progress_bar"]["y"]   # a progress bar: its top edge


def test_a_timing_tag_on_the_stage_hangs_below_its_element_and_does_not_move_it():
    css = (STATIC / "formats-builder.css").read_text(encoding="utf-8")
    assert ".st-item>.st-tag{position:absolute;left:50%;top:100%" in css     # it once sat inside the box and pushed a timed button about 40 units up


def placement_defaults() -> dict:
    from villomillo.web import pages

    return pages.recipe_defaults()


def test_the_stage_draws_with_the_recipes_own_numbers_where_an_element_leaves_a_choice_open():
    from villomillo.formats import blueprints

    library_ = blueprints.load()
    got = placement_defaults()
    assert got == {"bar_height": library_["progress_bar_bottom"].params["height"], "handle_size": library_["watermark_center"].params["size"],
                   "handle_alpha": library_["watermark_center"].params["alpha"], "logo_margin": library_["logo_corner"].params["margin"]}
    bar = element("progress_bar")
    assert js_positions([bar], "face_fill", got)["progress_bar"]["y"] == 1920 - got["bar_height"]


def blobs(frame: Image.Image, colour: tuple[int, int, int]) -> tuple[float, float, float, float] | None:
    """The box (left, top, right, bottom) of the pixels of a colour in a frame, or None."""
    a = np.asarray(frame.convert("RGB")).astype(int)
    mask = (np.abs(a - np.array(colour)).max(axis=2) < 40)
    rows, cols = np.where(mask.sum(axis=1) >= 6)[0], np.where(mask.sum(axis=0) >= 6)[0]
    return None if not len(rows) or not len(cols) else (cols.min(), rows.min(), cols.max() + 1, rows.max() + 1)


@needs_ffmpeg
def test_in_a_real_frame_each_coloured_element_is_where_the_model_says(grey, tmp_path):
    sv, profile, logo_id = grey
    colours = {"label": (250, 37, 143), "text": (18, 209, 165), "shape": (122, 43, 209), "takeaway": (240, 120, 20), "headline": (30, 120, 250), "logo": (224, 160, 16)}
    hexes = {k: "#%02X%02X%02X" % v for k, v in colours.items()}
    label = element("label", x=400, y=1500, style={"box": hexes["label"], "color": "#000000"})
    text = element("text", x=250, y=1100, text={"default": "Words", "source": "fixed"}, style={"box": hexes["text"], "color": "#000000", "outline_w": 0})
    shape = element("shape", x=720, y=800, w=300, h=100, style={"color": hexes["shape"], "radius": 0, "outline_w": 0})
    headline = element("headline", y=300, style={"box": hexes["headline"], "color": "#000000"})
    logo = element("logo", x=600, y=1150, options={"asset_id": logo_id})
    els = [headline, label, text, shape, logo]
    clip = preview.RealClip("talkyvideo1", 40.0, 60.0, render.ClipText(["HEADLINE", "TWO LINES"], [("Point", 0.5)], "Last"), False)
    prep = preview.prepare(sv.db, profile, {"elements": els, "effects": {"intensity": "off", "kinds": []}}, "x", clip=clip)
    model = js_positions(prep.job.design["elements"], "face_fill", placement_defaults())
    piece = preview.make_segment(prep, 0, tmp_path / "pieces")
    early, later = frame_at(piece, 2.0, tmp_path / "early.png"), frame_at(piece, 4.8, tmp_path / "later.png")   # the headline is up first, then the label
    for kind, tol, frame in (("text", 4, early), ("shape", 3, early), ("label", 4, later)):
        box = blobs(frame, colours[kind])
        assert box is not None, kind
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        assert centre == pytest.approx((model[kind]["x"], model[kind]["y"]), abs=tol), f"{kind}: found {box}"
    assert blobs(early, colours["label"]) is None and blobs(later, colours["headline"]) is None
    frame = early
    box = blobs(frame, colours["logo"])
    assert (box[0], box[1]) == pytest.approx((model["logo"]["x"], model["logo"]["y"]), abs=3)
    box = blobs(frame, colours["headline"])
    assert box[1] == pytest.approx(model["headline"]["y"], abs=3) and abs((box[0] + box[2]) / 2 - 540) < 4


# --- fonts ----------------------------------------------------------------------------------------------------------------------------------

@pytest.fixture
def font_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(fonts, "USER_DIR", tmp_path / "fonts")
    monkeypatch.setattr(fonts, "MERGED_DIR", tmp_path / "fonts-all")
    yield tmp_path
    fonts.register_uploads([])


def ink(image: Image.Image) -> int:
    return int((np.asarray(image.convert("RGBA"))[:, :, 3] > 40).sum())


def caption_digest(font_id: str, tmp: pathlib.Path) -> tuple[str, int]:
    """Burn one word in this font into a black frame the way the renderer burns captions, and return a fingerprint of the picture and its ink."""
    words = [{"t0": 0.0, "t1": 1.0, "o0": 0.0, "o1": 1.0, "text": "Hamburgefonstiv"}]
    (tmp / "captions.ass").write_text(captions.build_ass(words, "dim", y=960, font=font_id, size=90, upper=False), encoding="utf-8")
    out = tmp / f"cap-{font_id}.png"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=1:r=10",
                    "-vf", f"subtitles=captions.ass:fontsdir={fonts.fonts_dir_option()}", "-frames:v", "1", str(out)], cwd=str(tmp), check=True, timeout=120)
    image = Image.open(out).convert("L")
    return hashlib.sha1(image.tobytes()).hexdigest(), int((np.asarray(image) > 60).sum())


@needs_ffmpeg
def test_every_bundled_font_draws_in_the_headline_the_labels_and_the_captions(font_folders, tmp_path, monkeypatch):
    assert len(fonts.FONTS) >= 11
    for font_id, font in list(fonts.FONTS.items()):
        assert (fonts.FONTS_DIR / font.file).exists(), f"{font_id}: its file is missing"
        style = {**elements.default_element("label")["style"], "font": font_id}
        assert ink(overlays.box_image("Hamburgefonstiv 123", style)) > 500, f"{font_id}: nothing drawn for a label"
        headline = {**elements.default_element("headline")["style"], "font": font_id, "background": "#FFB000"}
        assert ink(overlays.headline_image(["Hamburgefonstiv", "Second line"], headline)) > 2000, f"{font_id}: nothing drawn for a headline"
        got, inked = caption_digest(font_id, tmp_path)
        gone = dataclasses.replace(font, ass_name="Zzyzx Not A Family")
        monkeypatch.setitem(fonts.FONTS, font_id, gone)
        fallback, _ = caption_digest(font_id, tmp_path)
        monkeypatch.setitem(fonts.FONTS, font_id, font)
        assert inked > 1500 and got != fallback, f"{font_id}: the caption engine did not find the font (it drew the fallback)"


@needs_ffmpeg
def test_an_uploaded_font_draws_in_the_headline_the_labels_the_captions_and_a_finished_clip(font_folders, tmp_path, monkeypatch, grey):
    sv, profile, _ = grey
    data = (fonts.FONTS_DIR / fonts.FONTS["anton"].file).read_bytes()
    row = font_uploads.save(sv.db, "My Anton", data, ext=".ttf", source="the app's own copy, for this test", licence="SIL Open Font License", folder=tmp_path / "fonts")
    uploaded = f"upload-{row['sha'][:10]}"
    assert uploaded in fonts.FONTS and fonts.FONTS[uploaded].folder
    style = {**elements.default_element("label")["style"], "font": uploaded}
    assert ink(overlays.box_image("Hamburgefonstiv", style)) > 500
    assert ink(overlays.headline_image(["Hamburgefonstiv", "Two"], {**elements.default_element("headline")["style"], "font": uploaded, "background": "#FFB000"})) > 2000
    got, inked = caption_digest(uploaded, tmp_path)
    monkeypatch.setitem(fonts.FONTS, uploaded, dataclasses.replace(fonts.FONTS[uploaded], ass_name="Zzyzx Not A Family"))
    fallback, _ = caption_digest(uploaded, tmp_path)
    monkeypatch.setitem(fonts.FONTS, uploaded, dataclasses.replace(fonts.FONTS[uploaded], ass_name=row["family"]))
    assert inked > 1500 and got != fallback
    # a finished clip, in the preview way and the final way, with the font on the captions, a label and the headline
    design = {"elements": [element("headline", style={"font": uploaded}), element("label", style={"font": uploaded}), element("captions", style={"font": uploaded})]}
    clip = preview.RealClip("talkyvideo1", 40.0, 43.0, render.ClipText(["A TITLE", "TWO"], [("Point", 0.4)], "Last"), False)
    prep = preview.prepare(sv.db, profile, design, "x", clip=clip)
    for fast in (True, False):
        out = render.render(dataclasses.replace(prep.job, out=tmp_path / f"clip-{fast}.mp4"), preview=fast)
        assert out.exists() and length_of(out) == pytest.approx(3.0, abs=0.3)


def test_an_uploaded_font_that_a_label_or_the_handle_uses_cannot_be_deleted_from_under_it(font_folders, tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch)
    data = (fonts.FONTS_DIR / fonts.FONTS["anton"].file).read_bytes()
    row = font_uploads.save(sv.db, "My Anton", data, ext=".ttf", source="test", licence="OFL", folder=tmp_path / "fonts")
    uploaded = f"upload-{row['sha'][:10]}"
    from villomillo.formats import templates

    for kind in ("label", "takeaway", "handle", "text", "button", "headline", "captions"):
        made = templates.save_design(sv.db, name=f"Uses it in the {kind}", design={"elements": [element("label"), element(kind, id=f"{kind}-x", style={"font": uploaded})]})
        with pytest.raises(font_uploads.FontUploadError, match=f"Uses it in the {kind}' uses this font"):
            font_uploads.delete(sv.db, row["id"], folder=tmp_path / "fonts")
        templates.delete(sv.db, made["id"])
    font_uploads.delete(sv.db, row["id"], folder=tmp_path / "fonts")   # nothing uses it any more
    assert uploaded not in fonts.FONTS


# --- the documents say how a format is made now -------------------------------------------------------------------------------------------

def test_the_documents_describe_the_element_model():
    root = pathlib.Path(__file__).resolve().parent.parent
    blueprints = (root / "docs" / "FORMAT-BLUEPRINTS.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "list of elements" in blueprints and "villomillo/formats/elements.py" in blueprints and "FORMAT-BUILDER-PLAN.md" in blueprints
    for kind in elements.TYPES:
        assert kind in blueprints, kind
    assert "format builder" in readme.lower() and "elements" in readme.lower()
