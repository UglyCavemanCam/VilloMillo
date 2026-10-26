"""The element model: a format is a list of elements, and the converted Podcast and Stream formats (and any old-style design)
draw exactly the pictures the fixed switches drew, with the same timing. `old_overlays` below is a frozen copy of the
renderer's overlay code from before elements, kept only to prove that. Bad values are refused in plain words, each style
option changes the picture, and a campaign clip drops the brand elements."""

from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw
from test_formats import (  # noqa: F401  (source is a fixture)
    WORDS,
    FakeClaude,
    FakeJev,
    frame_at,
    good_raw,
    needs_ffmpeg,
    source,
)

from villomillo import config, formats
from villomillo.formats import blueprints, designs, elements, fonts, overlays, render

W, H = 1080, 1920
AMBER, INK = "#FFB000", "#17120E"
TEXT = render.ClipText(["LUCK CAN BE TRAINED", "TWO WAYS TO DO IT"], [("SOCIAL SKILLS", 1.0), ("AN OPEN MIND", 9.0), ("PRACTICE", 17.0)], "SKILLS EXPAND LUCK")


class _Old:
    """The painters as they were: pill_image, headline_image, watermark_image and logo_image, rebuilt from the old code."""

    @staticmethod
    def headline_image(lines: list[str], d: dict) -> Image.Image:
        """The headline, one rounded box per line behind the words. `d` is the design's headline section."""
        texts = [t.upper() for t in lines[:2] if t.strip()]
        size, max_w = d["size"], int(W * d["width_pct"] / 100)
        while True:
            font = fonts.pil(d["font"], size)
            pad_x = int(size * 0.45)
            if max(font.getlength(t) for t in texts) + 2 * pad_x <= max_w or size <= 24:
                break
            size -= 2
        ascent, descent = font.getmetrics()
        pad_y, gap = int(size * 0.22), int(size * 0.18)
        box_h = ascent + descent + 2 * pad_y
        img = Image.new("RGBA", (W, len(texts) * box_h + (len(texts) - 1) * gap + 12), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        fg, bg = overlays.rgb(d["color"]), (overlays.rgb(d["background"]) if d["background"] else None)
        left = (W - max_w) // 2
        for i, text in enumerate(texts):
            box_w = int(font.getlength(text) + 2 * pad_x)
            x = left if d["align"] == "left" else (W - box_w) // 2 if d["align"] == "center" else W - left - box_w
            y = i * (box_h + gap)
            if bg:
                draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=min(d["radius"], box_h // 2), fill=(*bg, 255))
            if d["shadow"]:
                draw.text((x + pad_x + 4, y + pad_y + 4), text, font=font, fill=(0, 0, 0, 170))
            draw.text((x + pad_x, y + pad_y), text, font=font, fill=(*fg, 255),
                      stroke_width=0 if bg else 4, stroke_fill=(0, 0, 0, 255))
        return img

    @staticmethod
    def pill_image(text, size, fill, ink, font_id="montserrat-black", pad=26):
        font = fonts.pil(font_id, size)
        while font.getlength(text) + pad * 2 > W - 80 and size > 24:
            size -= 2
            font = fonts.pil(font_id, size)
        w, h = int(font.getlength(text) + pad * 2), size + pad
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(*overlays.rgb(fill), 255))
        draw.text((w / 2, h / 2 + 2), text, font=font, fill=(*overlays.rgb(ink), 255), anchor="mm")
        return img

    @staticmethod
    def watermark_image(handle, alpha, size):
        font = fonts.pil("montserrat-bold", size)
        box = font.getbbox(handle)
        img = Image.new("RGBA", (box[2] - box[0] + 16, box[3] - box[1] + 16), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((8 - box[0], 8 - box[1]), handle, font=font, fill=(255, 255, 255, alpha), stroke_width=1,
                                 stroke_fill=(0, 0, 0, alpha // 2))
        return img

    @staticmethod
    def logo_image(path, width, max_h=260):
        img = Image.open(path).convert("RGBA")
        if round(img.height * width / img.width) > max_h:
            width = round(img.width * max_h / img.height)
        return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


old = _Old()


def old_overlays(job: render.Job, d: dict, total: float, framing: str, work: Path) -> list[dict]:
    """Every image to lay over the video: its file, where it goes and when it shows (None = the whole clip)."""
    plan, text, items = job.plan, job.text, []

    def add(name: str, image, x: str, y: str, window: tuple[float, float] | None) -> None:
        path = work / f"{name}.png"
        image.save(path)
        items.append({"file": path.name, "x": x, "y": y, "window": window})

    head_end = 0.0
    if plan.has("headline") and text.title_lines:
        words = sum(len(line.split()) for line in text.title_lines)
        base, per, low, high = (plan.param("headline", k) for k in ("base_s", "per_word_s", "min_s", "max_s"))
        head_end = min(high, max(low, base + per * words))
        add("headline", old.headline_image(text.title_lines, d["headline"]), "0",
            str(_place(d, "headline_y", designs.HEADLINE_TOP[framing])), (0.0, head_end))
    row = _place(d, "labels_y", designs.CHIPS_Y[framing])
    cta_row = _place(d, "cta_y", designs.CHIPS_Y[framing])
    cta = d["cta"]["text"] if plan.has("cta_end") and total > 6.0 else None
    cta_start = total - float(d["cta"]["seconds"]) if cta else None
    take_end = cta_start - 0.3 if cta else total   # the call to action has the last seconds; the takeaway lands just before it
    take_start = take_end - 2.6 if plan.has("takeaway") and text.takeaway and take_end > 3.0 else None
    if plan.has("point_chips"):
        for i, (label, at) in enumerate(text.points):
            end = text.points[i + 1][1] - 0.3 if i + 1 < len(text.points) else total
            if take_start is not None:
                end = min(end, take_start - 0.1)
            start = max(at, head_end)
            if end - start >= 0.6:
                add(f"chip{i}", old.pill_image(f"{i + 1}  ·  {label.upper()}", 40, AMBER, INK), "(W-w)/2", f"{row}-h/2", (start, end))
    if take_start is not None:
        add("takeaway", old.pill_image(text.takeaway.upper(), 46, AMBER, INK, pad=34), "(W-w)/2", f"{row}-h/2", (take_start, take_end))
    if cta:
        add("cta", old.pill_image(cta.upper(), int(d["cta"]["size"]), AMBER, INK, pad=34), "(W-w)/2", f"{cta_row}-h/2", (cta_start, total))
    if plan.has("logo_corner") and job.logo is not None:
        image = old.logo_image(job.logo, round(W * int(d["logo"]["size"]) / 100))
        margin = int(plan.param("logo_corner", "margin"))
        right = framing in ("stacked_webcam", "split") or d["logo"]["corner"] == "right"   # the top band is taken: the logo goes right
        x = f"W-w-{margin}" if right else str(margin)
        if d.get("place", {}).get("logo_x") is not None:   # dragged: its left edge, as the owner placed it
            x = str(d["place"]["logo_x"])
        from_s = float(d["logo"]["from_s"])
        add("logo", image, x, str(_place(d, "logo_y", designs.LOGO_Y[framing])), (from_s, total) if from_s > 0 else None)
    if plan.has("watermark_center") and job.handle:
        add("watermark", old.watermark_image(job.handle, int(plan.param("watermark_center", "alpha")),
                                                  int(plan.param("watermark_center", "size"))), "(W-w)/2", "(H-h)/2", None)
    return items


def _place(d, key, default):
    value = (d.get("place") or {}).get(key)
    return default if value is None else value




# --- the converted formats draw exactly what the switches drew --------------------------------------------------------

def logo_file(tmp_path, size=(400, 200)):
    path = tmp_path / "logo.png"
    Image.new("RGBA", size, (255, 176, 0, 255)).save(path)
    return path


EVERYTHING = {"layout": "fit_blur", "headline": {"align": "left", "background": "", "color": "#FFFFFF", "shadow": True, "radius": 0, "size": 60},
              "logo": {"enabled": True, "asset_id": 1, "corner": "right", "size": 20, "from_s": 5},
              "cta": {"enabled": True, "text": "Follow for more", "size": 50, "seconds": 4},
              "place": {"headline_y": 500, "labels_y": 1000, "cta_y": 1500, "logo_x": 100, "logo_y": 300, "captions_y": 1200}}
DESIGNS = {
    "podcast": {},
    "stream": {"layout": "stacked_webcam", "captions": {"style": "slam"}},
    "everything": EVERYTHING,
    "split with a button": {"layout": "split", "cta": {"enabled": True, "text": "Join us", "seconds": 2}, "chips": False},
    "bare": {"headline": {"enabled": False}, "chips": False, "brand": {"watermark": False, "progress_bar": False}},
    "labels only": {"headline": {"enabled": False}, "takeaway": False, "captions": {"enabled": False}},
}


def job_for(design, tmp_path, *, total=30.0, forbid=()):
    d = designs.normalize(design)
    extra = ({"cta"} if d["cta"]["enabled"] else set()) | ({"logo"} if d["logo"]["enabled"] else set())
    plan = blueprints.resolve("t", designs.layers_from(d), TEXT.available(True) | extra, forbid=set(forbid))
    return render.Job(tmp_path / "unused.mp4", 0.0, total, [], TEXT, plan, "@cavemancam", tmp_path / "out.mp4", design=d,
                      logo=logo_file(tmp_path) if d["logo"]["enabled"] else None)


def pixels(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGBA"))


@pytest.mark.parametrize("name", list(DESIGNS))
@pytest.mark.parametrize("total", [30.0, 5.0])
def test_a_converted_format_draws_the_same_pictures_at_the_same_times_as_the_old_switches(name, total, tmp_path):
    job = job_for(DESIGNS[name], tmp_path, total=total)
    d, framing = job.design, job.plan.framing().id
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    was = old_overlays(job, d, total, framing, tmp_path / "old")
    now = render._overlays(job, d, total, framing, tmp_path / "new")
    assert [(i["x"], i["y"], i["window"]) for i in now] == [(i["x"], i["y"], i["window"]) for i in was]
    for a, b in zip(was, now, strict=True):
        assert np.array_equal(pixels(tmp_path / "old" / a["file"]), pixels(tmp_path / "new" / b["file"])), f"{a['file']} differs"
    if name == "podcast" and total == 30.0:
        assert len(now) == 6   # the headline, three labels, the takeaway and the handle


@pytest.mark.parametrize("name", list(DESIGNS))
def test_the_old_sections_survive_a_trip_through_the_elements(name):
    d = designs.normalize(DESIGNS[name])
    again = designs.normalize({k: v for k, v in d.items() if k != "elements"})
    assert again == d                                     # old shape in, same design out
    assert designs.normalize(d) == d                      # the new shape is stable too
    back = elements.legacy_from(d["elements"])
    for key in ("chips", "takeaway", "brand", "place"):
        assert back[key] == d[key]
    for key in ("captions", "headline", "logo", "cta"):
        if d[key]["enabled"]:
            assert back[key] == d[key], key


def test_an_element_that_is_off_but_was_set_up_is_kept_hidden_and_a_plain_one_is_left_out():
    d = designs.normalize({"logo": {"enabled": False, "asset_id": 3, "size": 22}, "cta": {"enabled": False}})
    assert [e["type"] for e in d["elements"] if not e["visible"]] == ["logo"]
    assert d["logo"]["size"] == 22 and d["logo"]["asset_id"] == 3
    assert [e["type"] for e in designs.normalize({})["elements"]] == ["headline", "label", "takeaway", "handle", "captions", "progress_bar"]


def test_the_saved_old_templates_are_converted_when_they_are_used_and_stay_as_they_were_until_saved(tmp_path):
    from villomillo.db import Database
    from villomillo.formats import templates

    db = Database(tmp_path / "t.db")
    saved = templates.save(db, name="Old", layers=designs.layers_from(designs.normalize({})), settings={"layout": "fit_blur"})
    assert "elements" not in saved["settings"]
    used = designs.normalize(saved["settings"])
    assert used["layout"] == "fit_blur" and len(used["elements"]) == 6
    again = templates.save_design(db, name="Old", design=used, template_id=saved["id"])
    assert [e["type"] for e in again["settings"]["elements"]] == [e["type"] for e in used["elements"]]


# --- adding, removing and styling elements ----------------------------------------------------------------------------

def with_elements(*kinds_or_elements, **base):
    return designs.normalize({**base, "elements": [e if isinstance(e, dict) else elements.default_element(e) for e in kinds_or_elements]})


def test_a_format_can_have_no_labels_one_label_or_several_and_each_is_drawn(tmp_path):
    none = with_elements("headline", "takeaway", "captions")
    assert not none["chips"] and "point_chips" not in designs.layers_from(none)["text"]
    assert not [i for i in render._overlays(job_for(none, tmp_path), none, 30.0, "face_fill", tmp_path) if "label" in i["file"]]
    (tmp_path / "x").mkdir()
    two = with_elements("headline", "label", elements.default_element("label", id="tag", options={"numbered": False}, y=700))
    assert two["chips"]
    drawn = render._overlays(job_for(two, tmp_path), two, 30.0, "face_fill", tmp_path / "x")
    assert sorted(i["file"] for i in drawn if "label" in i["file"] or "tag" in i["file"]) == [f"{n}-{i}.png" for n in ("label", "tag") for i in range(3)]
    assert {i["y"] for i in drawn if "tag" in i["file"]} == {"700-h/2"}


def test_free_text_and_boxes_are_drawn_with_their_position_and_timing(tmp_path):
    d = with_elements("headline", elements.default_element("shape", x=300, y=400, w=200, h=100, timing={"mode": "first", "seconds": 4}),
                      elements.default_element("text", x=300, y=400, text={"source": "fixed", "default": "hello there"},
                                               timing={"mode": "range", "from": 2, "to": 8}))
    got = {i["file"]: i for i in render._overlays(job_for(d, tmp_path), d, 20.0, "face_fill", tmp_path)}
    assert got["shape.png"]["window"] == (0.0, 4.0) and got["shape.png"]["x"] == "300-w/2"
    assert got["text.png"]["window"] == (2.0, 8.0) and got["text.png"]["y"] == "400-h/2"
    assert Image.open(tmp_path / "shape.png").size == (200, 100)


@pytest.mark.parametrize(("timing", "window"), [
    ({"mode": "whole"}, None), ({"mode": "first", "seconds": 3.5}, (0.0, 3.5)), ({"mode": "last", "seconds": 3.0}, (17.0, 20.0)),
    ({"mode": "range", "from": 2.0, "to": 9.0}, (2.0, 9.0)), ({"mode": "range", "from": 5.0, "to": None}, (5.0, 20.0)),
    ({"mode": "range", "from": 0.0, "to": None}, None), ({"mode": "spoken"}, (1.0, 2.0)),
])
def test_when_an_element_is_on_screen(timing, window):
    full = {"mode": "whole", "seconds": 3.0, "from": 0.0, "to": None, **timing}
    assert elements.window(full, 20.0, (1.0, 2.0)) == window


def test_each_style_option_changes_the_picture():
    base = elements.default_element("label")["style"]
    pic = lambda **o: pixels_of(overlays.box_image("A LABEL", {**base, **o}))  # noqa: E731
    plain = pic()
    for change in ({"font": "anton"}, {"size": 60}, {"color": "#FFFFFF"}, {"box": "#112233", "color": "#FFFFFF"}, {"radius": 4}, {"pad_x": 60},
                   {"pad_y": 50}, {"outline_w": 6}, {"shadow": True}, {"opacity": 40}, {"box": ""}):
        assert pic(**change).shape != plain.shape or not np.array_equal(pic(**change), plain), change


def pixels_of(img: Image.Image) -> np.ndarray:
    return np.array(img.convert("RGBA"))


def test_a_handle_headline_logo_and_box_can_be_styled_too(tmp_path):
    handle = elements.default_element("handle")["style"]
    plain = pixels_of(overlays.handle_image("@me", handle))
    for change in ({"font": "anton"}, {"size": 50}, {"color": "#FFB000"}, {"opacity": 90}, {"outline_w": 3}, {"box": "#000000"}, {"pad_x": 30}):
        other = pixels_of(overlays.handle_image("@me", {**handle, **change}))
        assert other.shape != plain.shape or not np.array_equal(other, plain), change
    head = {**elements.default_element("headline")["style"], "background": "#FFB000"}
    plain = pixels_of(overlays.headline_image(["A HEADLINE"], head))
    for change in ({"opacity": 50}, {"outline_w": 6, "background": ""}):
        assert not np.array_equal(pixels_of(overlays.headline_image(["A HEADLINE"], {**head, **change})), plain), change
    box = elements.default_element("shape")["style"]
    assert not np.array_equal(pixels_of(overlays.shape_image(300, 100, box)), pixels_of(overlays.shape_image(300, 100, {**box, "radius": 0})))
    assert pixels_of(overlays.shape_image(300, 100, {**box, "opacity": 50}))[50, 150, 3] == 128
    logo = logo_file(tmp_path)
    assert pixels_of(overlays.logo_image(logo, 200, opacity=50))[10, 10, 3] == 128 and pixels_of(overlays.logo_image(logo, 200))[10, 10, 3] == 255


@pytest.mark.parametrize(("change", "message"), [
    ({"type": "label", "style": {"size": 500}}, "size of the point label must be a whole number from 16 to 160"),
    ({"type": "label", "style": {"color": "#FFB001", "box": "#FFB000"}}, "too close in colour"),
    ({"type": "label", "style": {"font": "comic-sans"}}, "font is not available"),
    ({"type": "label", "style": {"box": "yellow"}}, "box colour of the point label must be a colour"),
    ({"type": "label", "style": {"opacity": 3}}, "opacity of the point label"),
    ({"type": "label", "timing": {"mode": "whole"}}, "follows what is said"),
    ({"type": "handle", "timing": {"mode": "spoken"}}, "cannot follow the spoken words"),
    ({"type": "text", "timing": {"mode": "first", "seconds": 0}}, "seconds of the text must be from 0.5 to 120"),
    ({"type": "text", "timing": {"mode": "range", "from": 8, "to": 4}}, "must come after its start"),
    ({"type": "text", "timing": {"mode": "sometimes"}}, "timing of the text"),
    ({"type": "text", "text": {"default": " "}}, "Type the words"),
    ({"type": "button", "text": {"default": "x" * 41}}, "40 characters"),
    ({"type": "shape", "w": 0}, "width of the box"),
    ({"type": "logo", "options": {"asset_id": None}}, "Pick a logo"),
    ({"type": "logo", "style": {"size": 90}, "options": {"asset_id": 1}}, "logo size"),
    ({"type": "captions", "options": {"style": "nope"}}, "no caption style"),
    ({"type": "headline", "style": {"width_pct": 5}}, "headline width"),
    ({"type": "nothing"}, "not a kind the app knows"),
])
def test_bad_element_values_are_refused_in_plain_words(change, message):
    with pytest.raises(designs.DesignError, match=message):
        designs.normalize({"elements": [elements.default_element("headline"), copy.deepcopy(_merge_into_default(change))]})


def _merge_into_default(change):
    if change["type"] == "nothing":
        return change
    e = elements.default_element(change["type"])
    for key, value in change.items():
        e[key] = {**e[key], **value} if isinstance(value, dict) else value
    return e


def test_a_list_of_elements_must_be_a_sane_list():
    with pytest.raises(designs.DesignError, match="must be a list"):
        designs.normalize({"elements": "headline"})
    with pytest.raises(designs.DesignError, match="at most 40"):
        designs.normalize({"elements": [elements.default_element("text") for _ in range(41)]})
    with pytest.raises(designs.DesignError, match="share the id"):
        designs.normalize({"elements": [elements.default_element("headline"), elements.default_element("headline")]})
    with pytest.raises(designs.DesignError, match="headline, the point labels or the takeaway"):
        designs.normalize({"elements": [elements.default_element("captions")]})


def test_two_elements_of_one_kind_get_their_own_ids():
    d = designs.normalize({"elements": [{"type": "headline"}, {"type": "headline"}, {"type": "text", "id": "My Note!"}]})
    assert [e["id"] for e in d["elements"]] == ["headline", "headline-2", "my-note"]


def test_the_brand_elements_never_go_on_campaign_clips_and_other_elements_can_opt_out(tmp_path):
    d = with_elements("headline", "label", "handle", "progress_bar", elements.default_element("logo", options={"asset_id": 1}),
                      "button", elements.default_element("text", id="promo"), elements.default_element("text", id="plain", rules={"never_on_campaign": False}),
                      elements.default_element("handle", id="h2", rules={"never_on_campaign": False}))
    assert [e["id"] for e in d["elements"] if e["rules"]["never_on_campaign"]] == ["handle", "progress_bar", "logo", "button", "h2"]   # brand marks cannot be switched back on
    kept, gone = elements.for_campaign(d["elements"])
    assert [e["id"] for e in kept] == ["headline", "label", "promo", "plain"]
    assert gone == formats.CAMPAIGN_FORBIDS
    clean = designs.normalize({**d, "elements": kept})
    assert not clean["brand"]["watermark"] and not clean["brand"]["progress_bar"] and not clean["logo"]["enabled"] and not clean["cta"]["enabled"]
    job = job_for(clean, tmp_path, forbid=gone)
    names = {i["file"] for i in render._overlays(job, clean, 30.0, "face_fill", tmp_path)}
    assert not {"handle.png", "logo.png", "button.png"} & names and "promo.png" in names


def test_a_text_element_marked_off_campaign_is_dropped_by_the_clip_formatter(tmp_path, monkeypatch):
    kept, _ = elements.for_campaign(with_elements("headline", elements.default_element("text", rules={"never_on_campaign": True}))["elements"])
    assert [e["type"] for e in kept] == ["headline"]


# --- real renders -------------------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_free_text_and_a_box_show_up_in_a_real_clip_and_a_second_progress_bar_runs(source, tmp_path):  # noqa: F811
    words = [{"t0": 0.5 + i * 0.25, "t1": 0.7 + i * 0.25, "text": w} for i, w in enumerate("Luck is something you can train today".split())]
    d = with_elements("headline", "label", "takeaway", "captions",
                      elements.default_element("shape", x=540, y=960, w=400, h=200, style={"color": "#00FF00", "radius": 0}),
                      elements.default_element("progress_bar", id="top", y=0, style={"height": 20, "color": "#FF0000"}))
    plan = blueprints.resolve("t", designs.layers_from(d), TEXT.available(True))
    job = render.Job(source, 0.2, 3.2, words, TEXT, plan, "@me", tmp_path / "out.mp4", design=d)
    out = render.render(job, preview=True)
    frame = frame_at(out, 2.0)                       # BGR
    assert frame[960, 540, 1] > 200 and frame[960, 540, 0] < 80      # the green box is in the middle of the picture
    assert frame[10, 100, 2] > 200 and frame[10, 100, 1] < 80       # the red bar's fill has started (the clip is 1/3 in: x=100 is inside it)


def test_ai_generate_still_works_on_a_design_that_already_carries_elements(tmp_path):
    from villomillo.db import Database
    from villomillo.formats import generate

    db = Database(tmp_path / "g.db")
    answers = {"kind": "podcast", "feel": "balanced", "campaign": True, "logo_id": None, "note": ""}
    full = designs.normalize({"captions": {"style": "spotlight"}})   # what the page sends back when an option is picked: elements and all
    assert "elements" in full
    out = generate.checked(full, answers, db)
    assert not out["brand"]["watermark"] and not out["brand"]["progress_bar"]            # a campaign look is cut down, as before
    assert not any(e["type"] in elements.BRAND and e["visible"] for e in out["elements"])
    assert out["made_by"] == "ai"


def clip_job(tmp_path, monkeypatch, settings, **kw):
    """Run formats.format_clip with the render itself replaced by a recorder, and return the job it would have rendered."""
    from villomillo.db import Database

    seen = []
    monkeypatch.setattr(formats.render, "render", lambda job, **k: seen.append(job) or job.out)
    media = tmp_path / "s.mp4"
    media.write_bytes(b"x")
    profile = config.Profile(id="tt", display_name="T", niche={"definition": "x"}, category="Podcasts")
    template = {"id": "t", "name": "Talk", "layers": designs.layers_from(settings), "settings": settings}
    formats.format_clip(Database(tmp_path / "c.db"), profile, template, claude_client=FakeClaude(good_raw()), jev=FakeJev(), video_id="v",
                        start_s=0.2, end_s=7.0, out_path=tmp_path / "o.mp4", episode="E", source=media, words=WORDS, category="Podcasts", **kw)
    return seen[0]


def test_a_campaign_clip_is_made_without_the_brand_elements_and_a_pasted_clip_keeps_them(tmp_path, monkeypatch):
    settings = designs.normalize({"music": {"mode": "off"}, "cta": {"enabled": True, "text": "Follow"}, "logo": {"enabled": False},
                                  "elements": [*designs.normalize({"music": {"mode": "off"}})["elements"], elements.default_element("button"),
                                               elements.default_element("text", id="promo", rules={"never_on_campaign": True}),
                                               elements.default_element("text", id="note")]})
    pasted = clip_job(tmp_path, monkeypatch, settings)
    assert {"handle", "progress_bar", "button", "promo", "note"} <= {e["id"] for e in pasted.design["elements"]}
    assert pasted.plan.has("watermark_center") and pasted.plan.has("cta_end")
    campaign = clip_job(tmp_path, monkeypatch, settings, campaign=True)
    assert {e["id"] for e in campaign.design["elements"]} == {"headline", "label", "takeaway", "captions", "note"}
    assert not campaign.design["brand"]["watermark"] and not campaign.design["cta"]["enabled"]
    assert {b for b, _ in campaign.plan.dropped} == {"watermark_center", "progress_bar_bottom", "cta_end"}   # the logo was never on


def test_the_default_progress_bar_is_the_same_filter_as_before_and_can_be_restyled(tmp_path):
    job = job_for({}, tmp_path)
    total = 12.5
    assert render._bars(job, job.design, total) == (
        f"drawbox=x=0:y={H - 14}:w={W}:h=14:color=0xF4EBDD@0.27:t=fill,"
        f"drawbox=x=0:y={H - 14}:w='{W}*min(1,t/12.500)':h=14:color=0xFFB000:t=fill,")
    styled = with_elements("headline", elements.default_element("progress_bar", y=100, style={"height": 8, "color": "#112233", "track_opacity": 50},
                                                                timing={"mode": "first", "seconds": 4}))
    job = job_for(styled, tmp_path)
    bars = render._bars(job, styled, total)
    assert "y=100:w=1080:h=8:color=0xF4EBDD@0.50:t=fill:enable='between(t,0.00,4.00)'" in bars and "color=0x112233" in bars
    assert render._bars(job_for(with_elements("headline"), tmp_path), with_elements("headline"), total) == ""
