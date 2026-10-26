"""Formats: the building blocks and the resolver, the design rules, the twelve caption styles, the overlay pictures, real
renders from a synthetic video, the face check, and the clip pipeline's handling of a clip with no format.
Claude and Jev are fakes."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from villomillo import clipper, config, formats, store
from villomillo.db import Database
from villomillo.formats import blueprints, captions, designs, fonts, overlays, render, templates, text
from villomillo.jev import JevResult

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg is not installed")
WORDS = [{"t0": 0.5 + i * 0.25, "t1": 0.7 + i * 0.25, "text": w} for i, w in enumerate(
    "Luck is something you can train. Social skills open doors. Another skill is an open mind.".split())]
LAYERS = {"framing": "face_fill", "text": ["captions", "headline", "point_chips", "takeaway"],
          "brand": ["progress_bar_bottom", "watermark_center"]}
ALL_AVAILABLE = {"words", "title", "points", "takeaway"}
CLIP_TEXT = render.ClipText(["LUCK CAN BE TRAINED", "TWO WAYS"], [("SOCIAL SKILLS", 1.0)], "SKILLS EXPAND LUCK")


# --- blocks and the resolver -----------------------------------------------------------------------------------

def test_the_shipped_blocks_are_valid_and_the_old_formats_are_gone():
    library = blueprints.load()
    assert {"face_fill", "fit_blur", "captions", "headline", "point_chips", "takeaway"} <= set(library)
    assert not {"clean_frame", "hook_title", "captions_plain", "captions_pop"} & set(library)
    assert library["face_fill"].layer == "framing" and library["headline"].added_value


def test_a_full_template_gets_every_block_it_names():
    plan = blueprints.resolve("Talk", LAYERS, ALL_AVAILABLE)
    assert plan.ids() == ["face_fill", "captions", "headline", "point_chips", "takeaway", "progress_bar_bottom", "watermark_center"]
    assert plan.dropped == []


def test_blocks_whose_needs_are_missing_are_dropped_and_reported():
    plan = blueprints.resolve("Talk", LAYERS, {"words", "points"})
    assert plan.has("point_chips") and not plan.has("takeaway") and not plan.has("headline")
    assert ("takeaway", "the clip has no takeaway") in plan.dropped and ("headline", "the clip has no title") in plan.dropped


def test_a_plan_with_nothing_but_a_crop_and_captions_is_refused():
    with pytest.raises(blueprints.OriginalityError, match="nothing but a crop and captions"):
        blueprints.resolve("Plain", {"framing": "fit_blur", "text": ["captions"]}, {"words"})


def test_a_template_can_forbid_a_block():
    plan = blueprints.resolve("Talk", LAYERS, ALL_AVAILABLE, forbid={"watermark_center"})
    assert not plan.has("watermark_center") and ("watermark_center", "not allowed for this channel") in plan.dropped


def test_a_campaign_clip_never_carries_the_watermark_or_the_progress_bar():
    plan = blueprints.resolve("Talk", LAYERS, ALL_AVAILABLE, forbid=formats.CAMPAIGN_FORBIDS)
    assert not plan.has("watermark_center") and not plan.has("progress_bar_bottom") and plan.has("headline")


def test_template_overrides_change_a_blocks_parameters():
    plan = blueprints.resolve("Talk", LAYERS, ALL_AVAILABLE, overrides={"face_fill": {"keep_right_of": 0.4}})
    assert plan.framing_param("keep_right_of") == 0.4


@pytest.mark.parametrize(
    ("layers", "message"),
    [
        ({"framing": "fit_blur", "text": ["nope"]}, "no block called nope"),
        ({"framing": "takeaway"}, "not framing"),
        ({"text": ["takeaway"]}, "exactly one framing"),
        ({"framing": "face_fill", "text": "captions"}, "must be a list"),
        ("not a dict", "no layers"),
    ],
)
def test_a_template_with_broken_layers_is_refused(layers, message):
    assert message in blueprints.layers_problem(layers, blueprints.load())


# --- the design: every builder choice, checked in plain words ---------------------------------------------------

def test_an_empty_design_gets_every_default_and_turns_into_the_blocks():
    d = designs.normalize({})
    assert d["layout"] == "face_fill" and d["captions"]["style"] == "spotlight" and d["brand"] == {"watermark": True, "progress_bar": True}
    assert designs.layers_from(d) == {"framing": "face_fill", "text": ["captions", "headline", "point_chips", "takeaway"],
                                      "brand": ["watermark_center", "progress_bar_bottom"]}


def test_switching_things_off_removes_their_blocks():
    d = designs.normalize({"layout": "fit_blur", "captions": {"enabled": False}, "chips": False, "brand": {"watermark": False}})
    assert designs.layers_from(d) == {"framing": "fit_blur", "text": ["headline", "takeaway"], "brand": ["progress_bar_bottom"]}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"layout": "wide"}, "Pick a layout"),
        ({"captions": {"style": "disco"}}, "no caption style called disco"),
        ({"captions": {"font": "comic-sans"}}, "caption font is not available"),
        ({"captions": {"size": 400}}, "caption size must be a whole number from 40 to 160"),
        ({"captions": {"size": "big"}}, "caption size must be a whole number"),
        ({"captions": {"position": "bottom"}}, "lower, middle or upper"),
        ({"captions": {"highlight_color": "orange"}}, "colour like #FFB000"),
        ({"headline": {"size": 5}}, "headline size must be a whole number from 24 to 90"),
        ({"headline": {"radius": 80}}, "corner radius must be a whole number from 0 to 40"),
        ({"headline": {"width_pct": 20}}, "width must be a whole number from 40 to 100"),
        ({"headline": {"align": "diagonal"}}, "left, centre or right"),
        ({"headline": {"color": "#FFB000", "background": "#FFB400"}}, "too close in colour to read"),
        ({"chips": False, "takeaway": False, "headline": {"enabled": False}}, "captions on their own are not enough"),
        ({"captions": {"position": "upper"}}, "would overlap at the top"),
        ({"style_mode": "random"}, "fixed or picked by the AI"),
        ({"style_pool": []}, "at least one caption style"),
    ],
)
def test_a_bad_design_is_refused_with_a_message_that_says_what_to_change(change, message):
    with pytest.raises(designs.DesignError, match=message):
        designs.normalize(change)


def test_colours_are_tidied_and_a_headline_may_have_no_box():
    d = designs.normalize({"captions": {"highlight_color": "#ffb000"}, "headline": {"background": "", "color": "#ffffff"}})
    assert d["captions"]["highlight_color"] == "#FFB000" and d["headline"]["background"] == ""


def test_the_podcast_preset_is_valid_and_a_deleted_preset_never_comes_back(tmp_path):
    db = Database(tmp_path / "d.db")
    assert [p["id"] for p in designs.presets(db)] == ["podcast", "stream"] and designs.preset(db, "podcast")["design"]["captions"]["style"] == "spotlight"
    designs.delete_preset(db, "podcast")
    assert [p["id"] for p in designs.presets(db)] == ["stream"] and designs.preset(db, "podcast") is None
    with pytest.raises(designs.DesignError):
        designs.delete_preset(db, "nonsense")


def test_a_template_is_saved_from_a_design_and_a_bad_design_is_refused(tmp_path):
    db = Database(tmp_path / "d.db")
    t = templates.save_design(db, name="Talk", design={"captions": {"style": "slam"}})
    assert t["layers"]["framing"] == "face_fill" and t["settings"]["captions"]["style"] == "slam"
    with pytest.raises(templates.TemplateError, match="too close in colour"):
        templates.save_design(db, name="Bad", design={"headline": {"color": "#FFFFFF", "background": "#FEFEFE"}})


# --- the twelve caption styles -----------------------------------------------------------------------------------

REL = [{**w, "o0": w["t0"], "o1": w["t1"]} for w in WORDS]


def test_there_are_twelve_styles_and_every_font_they_use_is_bundled():
    assert len(captions.STYLES) == 12
    for style in captions.STYLES.values():
        assert style.font in fonts.FONTS and (fonts.FONTS_DIR / fonts.FONTS[style.font].file).exists()
    for font in fonts.FONTS.values():
        assert (fonts.FONTS_DIR / font.file).exists()


@pytest.mark.parametrize("style_id", list(captions.STYLES))
def test_every_style_writes_subtitle_events_in_the_safe_zone(style_id):
    ass = captions.build_ass(REL, style_id, y=designs.POSITIONS["lower"])
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert events and "PlayResY: 1920" in ass and f"{fonts.FONTS[captions.STYLES[style_id].font].ass_name}," in ass
    assert all("\\pos(540,1345)" in e for e in events)
    for e in events:
        start, end = e.split(",")[1:3]
        assert start < end   # a line is always on screen for a moment (the stamps sort as text)


def test_spotlight_lights_the_spoken_word_and_a_line_never_outgrows_the_frame():
    ass = captions.build_ass(REL, "spotlight", y=1345)
    first = [line for line in ass.splitlines() if line.startswith("Dialogue:")][0]
    assert "&H00B0FF&" in first and "LUCK" in first          # amber (BGR) on the spoken word, capitals by default
    long_words = [{"o0": 0.0, "o1": 1.0, "text": "EXTRAORDINARILY"}, {"o0": 1.0, "o1": 2.0, "text": "UNBELIEVABLY"},
                  {"o0": 2.0, "o1": 3.0, "text": "INTERNATIONALIZATION"}]
    size = int(captions.build_ass(long_words, "spotlight", y=1345).split("\\fs")[1].split("\\")[0].split("}")[0])
    assert size < captions.STYLES["spotlight"].size


def test_the_typewriter_never_draws_over_the_next_line():
    events = [e for e in captions.build_ass(REL, "type", y=1345).splitlines() if e.startswith("Dialogue:")]
    stamps = [tuple(e.split(",")[1:3]) for e in events]
    assert all(stamps[i][1] <= stamps[i + 1][0] for i in range(len(stamps) - 1))


def test_slam_shows_one_word_at_a_time_alternating_colours():
    events = [e for e in captions.build_ass(REL, "slam", y=1345).splitlines() if e.startswith("Dialogue:")]
    assert len(events) == len(WORDS)
    assert events[0].split("\\1c")[1].split("\\")[0] != events[1].split("\\1c")[1].split("\\")[0]


def test_custom_colours_font_size_and_capitals_reach_the_subtitles():
    ass = captions.build_ass(REL, "dim", y=1000, font="anton", size=90, upper=True, base="#112233", accent="#445566")
    assert "Anton,90," in ass and "&H00332211&" in ass and "LUCK" in ass


# --- the overlay pictures ----------------------------------------------------------------------------------------

HEADLINE = designs.normalize({})["headline"]


def test_the_headline_has_a_box_behind_each_line_and_stays_inside_the_frame():
    img = overlays.headline_image(["A HEADLINE THAT IS QUITE LONG TO SEE", "AND A SECOND LINE"], HEADLINE)
    alpha = np.array(img)[:, :, 3]
    cols = np.nonzero(alpha.any(axis=0))[0]
    assert img.width == 1080 and cols.min() >= 20 and cols.max() <= 1060
    assert (np.array(img)[:, :, :3][alpha == 255] == overlays.rgb(HEADLINE["background"])).all(axis=1).any()


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_headline_alignment_moves_the_box(align):
    img = overlays.headline_image(["SHORT"], {**HEADLINE, "align": align})
    cols = np.nonzero(np.array(img)[:, :, 3].any(axis=0))[0]
    middle = (cols.min() + cols.max()) / 2
    assert (middle < 400) if align == "left" else (middle > 680) if align == "right" else abs(middle - 540) < 3


def test_a_headline_with_no_box_gets_an_outline_instead():
    img = overlays.headline_image(["NO BOX HERE"], {**HEADLINE, "background": "", "color": "#FFFFFF"})
    arr = np.array(img)
    assert (arr[:, :, 3] > 0).any() and (arr[:, :, :3][arr[:, :, 3] == 255].sum(axis=1) == 0).any()   # black outline pixels


def test_a_long_label_shrinks_to_fit_inside_the_frame():
    img = overlays.pill_image("SOME LUCK CAN BE GROWN WITH SKILLS AND A LONG TAIL", 46, "#FFB000", "#17120E")
    assert img.width <= 1080 - 80


def test_the_watermark_is_faint_and_small():
    img = overlays.watermark_image("@cavemancam", 38, 30)
    assert np.array(img)[:, :, 3].max() <= 38 and img.width < 400


# --- the face check ----------------------------------------------------------------------------------------------

def fake_face(xs):
    return SimpleNamespace(x=np.array(xs, float), raw_x=np.array(xs, float), t=np.arange(len(xs), dtype=float), size=(2560, 1440))


def test_a_crop_that_would_cut_off_the_speaker_is_refused_and_says_where():
    with pytest.raises(render.FaceProblem, match="cut off the speaker at about 0:01"):
        render.check_window(fake_face([1500.0, 700.0]), 810, 1040.0, 0.0)   # the speaker is left of the crop limit
    render.check_window(fake_face([1500.0, 700.0]), 810, 0.0, 0.0)           # with no limit the crop simply follows them


def test_a_speaker_near_the_edge_of_the_picture_stays_inside_the_crop():
    render.check_window(fake_face([20.0, 2540.0]), 810, 0.0, 0.0)


# --- real renders ------------------------------------------------------------------------------------------------

def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


def frame_at(path: Path, t: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, img = cap.read()
    cap.release()
    assert ok
    return img


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=1280x720:rate=24:duration=5", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path


def make_job(source, tmp_path, design=None, name="out.mp4", **over):
    d = designs.normalize(design or {})
    plan = blueprints.resolve("Talk", designs.layers_from(d), CLIP_TEXT.available(True), forbid=over.pop("forbid", set()))
    return render.Job(source, 0.2, 3.2, WORDS, CLIP_TEXT, plan, "@cavemancam", tmp_path / name, design=d)


@needs_ffmpeg
@pytest.mark.parametrize("layout", ["face_fill", "fit_blur"])
def test_a_clip_is_rendered_as_vertical_video_with_sound(source, tmp_path, layout):
    out = render.render(make_job(source, tmp_path, {"layout": layout}), preview=True)
    streams = {s["codec_type"]: s for s in probe(out)}
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920)
    assert abs(float(streams["video"]["duration"]) - 3.0) < 0.2 and "audio" in streams


@needs_ffmpeg
@pytest.mark.parametrize("style_id", list(captions.STYLES))
def test_every_caption_style_is_burned_into_a_real_clip(source, tmp_path, style_id):
    on = render.render(make_job(source, tmp_path, {"captions": {"style": style_id}}, "on.mp4"), preview=True)
    off = render.render(make_job(source, tmp_path, {"captions": {"enabled": False}}, "off.mp4"), preview=True)
    band = lambda p: frame_at(p, 1.6)[1230:1460].astype(int)  # noqa: E731  where captions sit
    assert np.abs(band(on) - band(off)).max() > 100, f"{style_id} drew nothing"


@needs_ffmpeg
def test_the_headline_watermark_and_progress_bar_are_drawn_and_can_be_switched_off(source, tmp_path):
    full = render.render(make_job(source, tmp_path, {}, "full.mp4"), preview=True)
    bare = render.render(make_job(source, tmp_path, {"headline": {"enabled": False}, "brand": {"watermark": False, "progress_bar": False}},
                                  "bare.mp4"), preview=True)
    a, b = frame_at(full, 1.0), frame_at(bare, 1.0)
    assert np.abs(a[430:560].astype(int) - b[430:560].astype(int)).max() > 100      # the headline
    assert np.abs(a[1900:1920].astype(int) - b[1900:1920].astype(int)).max() > 50    # the progress bar
    assert np.abs(a[930:990].astype(int) - b[930:990].astype(int)).max() > 5         # the faint watermark in the middle


@needs_ffmpeg
def test_a_campaign_clip_made_with_the_forbidden_blocks_has_no_watermark_or_bar(source, tmp_path):
    plain = render.render(make_job(source, tmp_path, {}, "plain.mp4", forbid=formats.CAMPAIGN_FORBIDS), preview=True)
    bare = render.render(make_job(source, tmp_path, {"brand": {"watermark": False, "progress_bar": False}}, "bare.mp4"), preview=True)
    assert np.abs(frame_at(plain, 1.0).astype(int) - frame_at(bare, 1.0).astype(int)).max() < 12


@needs_ffmpeg
def test_a_clip_with_no_face_still_renders_with_the_crop_holding_the_middle(source, tmp_path):
    out = render.render(make_job(source, tmp_path, {"layout": "face_fill"}), preview=True)
    assert frame_at(out, 1.0).shape[:2] == (1920, 1080)


# --- the clip pipeline: a clip with no format is never lost, and never posted --------------------------------

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


def make_clip(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=1080x1920:rate=30:duration=25", "-f", "lavfi", "-i", "sine=frequency=440:duration=25",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path


class Stub:
    """An engine and a grader in one: one finished clip, graded as a good one."""

    def __init__(self, media: bytes):
        self.media_bytes = media

    def job(self, job_id):
        return {"status": "done", "error": ""}

    def clips(self, video_id):
        return [{"id": 1, "start_s": 10, "end_s": 35, "score": 80, "hook": "h"}]

    def media(self, clip_id):
        return self.media_bytes

    def captions_text(self, clip_id):
        return "a story that stands on its own"

    def ask(self, state, questions):
        answers = {"self_contained": {"noul": 0.9}, "hook": {"score": 4}, "payoff": {"score": 4}, "ad_read": {"noul": 0.0}}
        return JevResult("jev", answers, {"input_tokens": 10})


@pytest.fixture
def collected(tmp_path, monkeypatch):
    db = Database(tmp_path / "f.db")
    monkeypatch.setattr(clipper, "OUTBOX", tmp_path / "outbox")
    profile = config.Profile(id="test", display_name="Test", niche={"definition": "talks"}, category="Podcasts")
    with db.conn() as c:
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES ('test', 'abcdefghijk', 1, 'submitted', ?, ?)", (store.iso(), store.iso()))
        job = dict(c.execute("SELECT * FROM clip_jobs").fetchone())
    return db, profile, job, make_clip(tmp_path / "engine.mp4").read_bytes(), tmp_path


@needs_ffmpeg
def test_a_formatted_clip_replaces_the_engines_render_and_records_its_template(collected):
    db, profile, job, engine_media, tmp_path = collected
    template = templates.save_design(db, name="Talk", design={})
    styled = make_clip(tmp_path / "styled.mp4").read_bytes() + b"\0"   # different bytes, still a valid clip

    def formatter(clip, job, out_path):
        out_path.write_bytes(styled)
        return {"template": template["id"]}

    stub = Stub(engine_media)
    assert clipper.collect(db, stub, stub, profile, job, formatter).startswith("1 clips made")
    path = clipper.OUTBOX / "test" / "1.mp4"
    assert path.read_bytes() == styled and (clipper.OUTBOX / "test" / "1.engine.mp4").read_bytes() == engine_media
    with db.conn() as c:
        row = c.execute("SELECT state, template_id, format_applied, grade_json FROM clips_ready").fetchone()
    assert row["state"] == "review" and row["format_applied"] == 1 and row["template_id"] == template["id"]
    assert json.loads(row["grade_json"])["look"] == f"template {template['id']}"


@needs_ffmpeg
def test_the_formatter_is_told_whether_the_clip_is_for_a_campaign(collected):
    db, profile, job, engine_media, _ = collected
    seen = {}

    def formatter(clip, job, out_path):
        seen["campaign_id"] = job.get("campaign_id")
        out_path.write_bytes(engine_media)
        return {"template": "t"}

    stub = Stub(engine_media)
    clipper.collect(db, stub, stub, profile, job, formatter, campaign_id="camp1")
    assert seen["campaign_id"] == "camp1"


def test_make_formatter_passes_the_campaign_flag_to_the_format(tmp_path, monkeypatch):
    db = Database(tmp_path / "f.db")
    profile = config.Profile(id="tt", display_name="T", niche={"definition": "x"}, category="Podcasts")
    seen = []
    monkeypatch.setattr(formats, "format_clip", lambda *a, **k: seen.append(k["campaign"]) or {"template": "t"})
    monkeypatch.setattr(formats.claude, "_client", lambda: None)
    run = formats.make_formatter(db, profile, {"id": "t", "name": "T", "layers": LAYERS, "settings": {}}, jev=object())
    run({"start_s": 0, "end_s": 5}, {"video_id": "v", "campaign_id": "c1"}, tmp_path / "o.mp4")
    run({"start_s": 0, "end_s": 5}, {"video_id": "v", "campaign_id": None}, tmp_path / "o.mp4")
    assert seen == [True, False]


@needs_ffmpeg
def test_a_clip_whose_category_has_no_template_keeps_the_engines_render_and_waits_for_review(collected):
    db, profile, job, engine_media, _ = collected
    stub = Stub(engine_media)
    clipper.collect(db, stub, stub, profile, job, None, category="Podcasts")
    assert (clipper.OUTBOX / "test" / "1.mp4").read_bytes() == engine_media
    with db.conn() as c:
        row = c.execute("SELECT state, reason, format_applied FROM clips_ready").fetchone()
    assert row["state"] == "review" and row["format_applied"] == 0
    assert "needs a format: no format is set for the Podcasts category yet" in row["reason"]


@needs_ffmpeg
def test_a_format_that_fails_keeps_the_engines_render_and_says_why(collected):
    db, profile, job, engine_media, _ = collected

    def formatter(clip, job, out_path):
        raise formats.FormatError("the source video is not on disk (abcdefghijk.mp4)")

    stub = Stub(engine_media)
    clipper.collect(db, stub, stub, profile, job, formatter)
    assert (clipper.OUTBOX / "test" / "1.mp4").read_bytes() == engine_media
    with db.conn() as c:
        row = c.execute("SELECT state, reason, format_applied FROM clips_ready").fetchone()
    assert row["state"] == "review" and row["format_applied"] == 0
    assert "needs a format: the source video is not on disk" in row["reason"]


def test_a_category_without_a_template_gets_no_formatter(tmp_path):
    db = Database(tmp_path / "f.db")
    plain = config.Profile(id="plain", display_name="Plain", niche={"definition": "x"}, category="Podcasts")
    assert formats.formatter_for(db, plain, category="Podcasts") is None
    template = templates.save_design(db, name="Talk", design={})
    templates.assign(db, "Podcasts", template["id"])
    assert formats.formatter_for(db, plain, category="podcasts") is not None


def test_formatting_stops_cleanly_without_a_source_or_words(tmp_path):
    db = Database(tmp_path / "f.db")
    profile = config.Profile(id="tt", display_name="T", niche={"definition": "x"}, category="Podcasts")
    template = {"id": "talk", "name": "Talk", "layers": LAYERS, "settings": designs.normalize({})}
    kwargs = {"claude_client": None, "jev": None, "video_id": "v", "start_s": 0.0, "end_s": 10.0,
              "out_path": tmp_path / "o.mp4", "episode": "E"}
    with pytest.raises(formats.FormatError, match="not on disk"):
        formats.format_clip(db, profile, template, source=tmp_path / "missing.mp4", words=WORDS, **kwargs)
    (tmp_path / "s.mp4").write_bytes(b"x")
    with pytest.raises(formats.FormatError, match="no words"):
        formats.format_clip(db, profile, template, source=tmp_path / "s.mp4", words=[], **kwargs)


# --- the on-screen text (unchanged rules) -----------------------------------------------------------------------

def good_raw(**over):
    return {"title_lines": ["LUCK CAN BE TRAINED", "HERE ARE TWO WAYS"],
            "points": [{"label": "SOCIAL SKILLS", "at": "social skills"}, {"label": "AN OPEN MIND", "at": "open mind"}],
            "takeaway": "SKILLS EXPAND YOUR LUCK", **over}


def test_good_text_is_accepted_and_points_get_their_times_in_the_clip():
    clip_text = text.check(good_raw(), WORDS, start=0.0)
    assert clip_text.title_lines == ["LUCK CAN BE TRAINED", "HERE ARE TWO WAYS"]
    assert [label for label, _ in clip_text.points] == ["SOCIAL SKILLS", "AN OPEN MIND"]
    assert clip_text.points[0][1] < clip_text.points[1][1]


@pytest.mark.parametrize(
    "raw",
    [good_raw(title_lines=[]), good_raw(title_lines=["X" * 31]), good_raw(title_lines=["NOW LUCK CAN BE TRAINED"]),
     good_raw(title_lines=["LUCK #1"])],
)
def test_a_bad_title_is_refused(raw):
    with pytest.raises(text.TextProblem):
        text.check(raw, WORDS, start=0.0)


def test_a_point_the_speaker_never_said_is_dropped_and_so_is_a_long_takeaway():
    raw = good_raw(points=[{"label": "WALK MORE", "at": "walk more every day"}, {"label": "OPEN MIND", "at": "open mind"}],
                   takeaway="X" * 40)
    clip_text = text.check(raw, WORDS, start=0.0)
    assert [label for label, _ in clip_text.points] == ["OPEN MIND"] and clip_text.takeaway == ""


class FakeClaude:
    def __init__(self, *replies):
        self.replies, self.calls = list(replies), 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.replies.pop(0)))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50), stop_reason="end_turn")


class FakeJev:
    def __init__(self, **odds):
        self.odds = odds

    def ask(self, state, questions):
        answers = {name: {"noul": self.odds.get(name, 0.05)} for name in questions}
        return JevResult("jev", answers, {"input_tokens": 500})


def make(claude_client, jev):
    return text.make(claude_client, jev, channel="C", niche="n", episode="E", words=WORDS, start=0.0)


def test_text_that_passes_both_checks_is_returned_with_its_cost():
    clip_text, cost = make(FakeClaude(good_raw()), FakeJev())
    assert clip_text.takeaway and cost["claude"] > 0 and cost["jev_tokens"] == 1000   # the title screen and the moment screen


def test_jev_drops_an_effect_that_is_not_really_emphatic_and_flags_a_serious_subject():
    raw = good_raw(effects=[{"kind": "punch_in", "at": "Luck is", "reason": "a strong claim"},
                            {"kind": "pop", "at": "Social skills", "reason": "a flat aside"}])
    kept = make(FakeClaude(raw), FakeJev(moment_0=0.9, moment_1=0.2, serious=0.8))[0]
    assert [k for k, _, _ in kept.proposed_effects] == ["punch_in"] and kept.serious is True
    quiet = make(FakeClaude(raw), FakeJev(moment_0=0.2, moment_1=0.2))[0]
    assert quiet.proposed_effects == [] and quiet.serious is False


def test_a_title_that_may_misstate_the_speaker_is_rewritten_twice_then_refused():
    claude_client = FakeClaude(good_raw(), good_raw(), good_raw())
    with pytest.raises(text.TextProblem, match="may misstate"):
        make(claude_client, FakeJev(title=0.8))
    assert claude_client.calls == 3


def test_a_point_or_takeaway_that_may_misstate_the_speaker_is_left_out_not_fatal():
    clip_text, _ = make(FakeClaude(good_raw()), FakeJev(point_0=0.9, takeaway=0.9))
    assert [label for label, _ in clip_text.points] == ["AN OPEN MIND"] and clip_text.takeaway == ""


# --- settings that reach the picture --------------------------------------------------------------------------------

def test_the_crop_limit_is_a_percentage_that_reaches_the_face_fill_block():
    d = designs.normalize({"crop_limit": 40})
    assert designs.overrides_from(d) == {"face_fill": {"keep_right_of": 0.4}}
    plan = blueprints.resolve("Talk", designs.layers_from(d), ALL_AVAILABLE, overrides=designs.overrides_from(d))
    assert plan.framing_param("keep_right_of") == 0.4
    assert designs.overrides_from(designs.normalize({})) == {"face_fill": {"keep_right_of": 0.0}}
    with pytest.raises(designs.DesignError, match="crop limit must be a whole number from 0 to 60"):
        designs.normalize({"crop_limit": 90})


def test_the_headline_corner_radius_changes_the_picture():
    square = np.array(overlays.headline_image(["RADIUS"], {**HEADLINE, "radius": 0}))
    rounded = np.array(overlays.headline_image(["RADIUS"], {**HEADLINE, "radius": 40}))
    ys, xs = np.nonzero(square[:, :, 3])
    corner = (ys.min(), xs.min())
    assert square[corner][3] == 255 and rounded[corner][3] < 255      # the very corner is filled when square, empty when round


def test_the_headline_font_and_colours_change_the_picture():
    a = np.array(overlays.headline_image(["FONT"], {**HEADLINE, "font": "montserrat-extrabold"}))
    b = np.array(overlays.headline_image(["FONT"], {**HEADLINE, "font": "anton"}))
    c = np.array(overlays.headline_image(["FONT"], {**HEADLINE, "background": "#112233", "color": "#FFFFFF"}))
    assert a.shape != b.shape or (a != b).any()
    assert (c[:, :, :3][c[:, :, 3] == 255] == overlays.rgb("#112233")).all(axis=1).any()


# --- phase 3: the stream layout and the Stream format -----------------------------------------------------------

def test_the_stacked_webcam_layout_is_a_framing_block_and_excludes_the_others():
    library = blueprints.load()
    assert library["stacked_webcam"].layer == "framing"
    assert "stacked_webcam" in library["face_fill"].conflicts and "face_fill" in library["stacked_webcam"].conflicts
    assert designs.layers_from(designs.normalize({"layout": "stacked_webcam"}))["framing"] == "stacked_webcam"


def test_the_webcam_box_must_fit_inside_the_picture():
    with pytest.raises(designs.DesignError, match="webcam box must fit inside the picture"):
        designs.normalize({"layout": "stacked_webcam", "webcam": {"x": 90, "y": 0, "w": 20, "h": 20}})
    with pytest.raises(designs.DesignError, match="webcam box width must be a whole number"):
        designs.normalize({"webcam": {"w": "wide"}})


def test_the_stream_preset_is_slam_over_the_stacked_webcam():
    stream = next(p for p in designs.PRESETS if p["id"] == "stream")
    assert stream["design"]["layout"] == "stacked_webcam" and stream["design"]["captions"]["style"] == "slam"
    assert designs.normalize(stream["design"]) == stream["design"]


@needs_ffmpeg
def test_a_stream_clip_renders_the_webcam_band_over_the_game(source, tmp_path):
    d = designs.normalize({"layout": "stacked_webcam", "captions": {"style": "slam"}})
    plan = blueprints.resolve("Stream", designs.layers_from(d), CLIP_TEXT.available(True))
    out = render.render(render.Job(source, 0.2, 3.2, WORDS, CLIP_TEXT, plan, "@streamer", tmp_path / "stream.mp4", design=d), preview=True)
    streams = {s["codec_type"]: s for s in probe(out)}
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920) and "audio" in streams


# --- phase 4: the logo, the call to action and the Assets tab -------------------------------------------------------

def logo_file(tmp_path, name="logo.png", size=(400, 200)):
    from PIL import Image

    path = tmp_path / name
    Image.new("RGBA", size, (255, 176, 0, 255)).save(path)
    return path


def test_the_logo_and_call_to_action_are_brand_blocks_shown_only_when_the_design_asks():
    library = blueprints.load()
    assert library["logo_corner"].layer == "brand" and library["logo_corner"].needs == ("logo",)
    assert library["cta_end"].layer == "brand" and library["cta_end"].needs == ("cta",)
    off = designs.layers_from(designs.normalize({}))["brand"]
    assert "logo_corner" not in off and "cta_end" not in off
    on = designs.normalize({"logo": {"enabled": True, "asset_id": 1}, "cta": {"enabled": True, "text": "Follow"}})
    assert {"logo_corner", "cta_end"} <= set(designs.layers_from(on)["brand"])


def test_a_logo_or_call_to_action_that_is_switched_on_needs_its_details():
    with pytest.raises(designs.DesignError, match="Pick a logo"):
        designs.normalize({"logo": {"enabled": True, "asset_id": None}})
    with pytest.raises(designs.DesignError, match="Type the call to action"):
        designs.normalize({"cta": {"enabled": True, "text": "   "}})
    with pytest.raises(designs.DesignError, match="40 characters or fewer"):
        designs.normalize({"cta": {"enabled": True, "text": "x" * 41}})
    with pytest.raises(designs.DesignError, match="logo size"):
        designs.normalize({"logo": {"size": 90}})


def test_campaign_clips_never_get_the_logo_or_the_call_to_action_whatever_the_template_says():
    d = designs.normalize({"logo": {"enabled": True, "asset_id": 1}, "cta": {"enabled": True, "text": "Follow"}})
    plan = blueprints.resolve("Podcast", designs.layers_from(d), CLIP_TEXT.available(True) | {"logo", "cta"},
                              forbid=formats.CAMPAIGN_FORBIDS)
    assert "logo_corner" not in plan.ids() and "cta_end" not in plan.ids()
    assert {"logo_corner", "cta_end"} <= {b for b, _ in plan.dropped}
    pasted = blueprints.resolve("Podcast", designs.layers_from(d), CLIP_TEXT.available(True) | {"logo", "cta"})
    assert {"logo_corner", "cta_end"} <= set(pasted.ids())


def test_saving_a_logo_keeps_only_a_real_picture_of_a_sensible_size(tmp_path, monkeypatch):
    db = Database(tmp_path / "a.db")
    from PIL import Image

    from villomillo.formats import assets

    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    row = assets.save_logo(db, "my logo.png", logo_file(tmp_path).read_bytes())
    assert row["ext"] == "png" and row["name"] == "my logo.png" and assets.file_path(row).exists()
    again = assets.save_logo(db, "copy.png", logo_file(tmp_path).read_bytes())
    assert again["sha"] == row["sha"] and len(list((tmp_path / "assets").iterdir())) == 1   # stored once
    jpg = tmp_path / "logo.jpg"
    Image.new("RGB", (10, 10)).save(jpg)
    with pytest.raises(assets.AssetError, match="PNG, WebP or GIF"):
        assets.save_logo(db, "logo.jpg", jpg.read_bytes())
    with pytest.raises(assets.AssetError, match="not a picture"):
        assets.save_logo(db, "notes.txt", b"hello")
    with pytest.raises(assets.AssetError, match="2 MB"):
        assets.save_logo(db, "big.png", b"0" * (assets.MAX_BYTES + 1))
    with pytest.raises(assets.AssetError, match="empty"):
        assets.save_logo(db, "empty.png", b"")


def test_a_logo_a_template_uses_cannot_be_deleted(tmp_path, monkeypatch):
    db = Database(tmp_path / "a.db")
    from villomillo.formats import assets

    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    row = assets.save_logo(db, "logo.png", logo_file(tmp_path).read_bytes())
    templates.save_design(db, name="With logo", design={"logo": {"enabled": True, "asset_id": row["id"]}})
    with pytest.raises(assets.AssetError, match="uses this logo"):
        assets.delete(db, row["id"])
    assert assets.get(db, row["id"]) is not None


def test_a_design_whose_logo_file_is_gone_is_refused_in_plain_words(tmp_path, monkeypatch):
    db = Database(tmp_path / "a.db")
    from villomillo.formats import assets

    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    row = assets.save_logo(db, "logo.png", logo_file(tmp_path).read_bytes())
    design = designs.normalize({"logo": {"enabled": True, "asset_id": row["id"]}})
    extra, path = assets.for_design(db, design)
    assert extra == {"logo"} and path.exists()
    assets.file_path(row).unlink()
    with pytest.raises(designs.DesignError, match="The logo is missing"):
        assets.for_design(db, design)


@needs_ffmpeg
def test_a_pasted_clip_shows_the_logo_and_the_call_to_action_at_their_places(source, tmp_path):
    logo = logo_file(tmp_path)
    d = designs.normalize({"logo": {"enabled": True, "asset_id": 1, "size": 16}, "cta": {"enabled": True, "text": "Follow for more"}})
    plan = blueprints.resolve("pasted", designs.layers_from(d), CLIP_TEXT.available(True) | {"logo", "cta"})
    out = render.render(render.Job(source, 0.2, 5.0, WORDS, CLIP_TEXT, plan, "@yourhandle", tmp_path / "pasted.mp4", design=d,
                                   logo=logo), preview=True)
    streams = {s["codec_type"]: s for s in probe(out)}
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920) and "audio" in streams


# --- phase 5: effects -------------------------------------------------------------------------------------------------

from villomillo.formats import effects as fx_rules
from villomillo.formats import effects


def proposed_set():
    return [("punch_in", 10.0, "strong claim"), ("shake", 20.0, "impact word"), ("flash", 30.0, "sudden turn"),
            ("pop", 40.0, "emotional peak")]


def test_effects_off_means_none_and_a_new_template_starts_subtle_with_every_kind():
    assert fx_rules.plan(proposed_set(), False, {"intensity": "off", "kinds": list(fx_rules.KINDS)}, "face_fill", 60, 0) == []
    default = designs.normalize({})["effects"]
    assert default == {"intensity": "subtle", "kinds": list(fx_rules.KINDS)}


def test_the_cap_follows_the_intensity_and_the_spacing_keeps_effects_apart():
    many = [("pop", 2.0 + i * 2.6, "peak") for i in range(10)]
    assert len(fx_rules.plan(many, False, {"intensity": "subtle", "kinds": ["pop"]}, "face_fill", 40, 0)) == 5    # one per 8 s
    assert len(fx_rules.plan(many, False, {"intensity": "punchy", "kinds": ["pop"]}, "face_fill", 40, 0)) == 10   # 2.6 s apart: all fit the 13 cap
    spaced = fx_rules.plan(many, False, {"intensity": "punchy", "kinds": ["pop"]}, "face_fill", 40, 0)
    assert all(b["start"] - a["start"] >= fx_rules.SPACING for a, b in zip(spaced, spaced[1:]))


def test_only_kinds_the_template_allows_and_the_layout_can_show_are_kept():
    only_pop = fx_rules.plan(proposed_set(), False, {"intensity": "punchy", "kinds": ["pop"]}, "face_fill", 60, 0)
    assert [e["kind"] for e in only_pop] == ["pop"]
    stream = fx_rules.plan([("punch_in", 10.0, "a claim")], False, {"intensity": "punchy", "kinds": ["punch_in"]},
                           "stacked_webcam", 60, 0)
    assert stream == []   # a punch-in needs the face-fill crop


def test_a_serious_subject_gets_no_shake_or_flash_but_may_keep_a_punch_in():
    kept = fx_rules.plan(proposed_set(), True, {"intensity": "punchy", "kinds": list(fx_rules.KINDS)}, "face_fill", 60, 0)
    assert {e["kind"] for e in kept} == {"punch_in", "pop"}


def test_nothing_sits_under_the_headline_or_in_the_last_second():
    early = fx_rules.plan([("punch_in", 1.0, "a")], False, {"intensity": "punchy", "kinds": ["punch_in"]}, "face_fill", 60, 3.0)
    late = fx_rules.plan([("punch_in", 58.5, "a")], False, {"intensity": "punchy", "kinds": ["punch_in"]}, "face_fill", 60, 0)
    assert early == [] and late == []


def test_claude_proposals_are_read_only_when_the_quoted_words_are_in_the_clip():
    raw = {"title_lines": ["WHY IT MATTERS"], "points": [], "takeaway": "",
           "effects": [{"kind": "punch_in", "at": "x", "reason": "a strong claim, quoted"},
                       {"kind": "lasers", "at": "Luck is", "reason": "not a kind"},
                       {"kind": "shake", "at": "words not said", "reason": "anchor missing"},
                       {"kind": "flash", "at": "Luck is", "reason": "x" * 91}],
           "serious": True}
    raw["effects"][0]["at"] = "Luck is something"
    out = text.check(raw, WORDS, 0.0)
    assert out.proposed_effects == [("punch_in", 0.5, "a strong claim, quoted")] and out.serious is True


def test_the_effect_settings_are_checked_in_plain_words():
    with pytest.raises(designs.DesignError, match="off, subtle, balanced or punchy"):
        designs.normalize({"effects": {"intensity": "wild"}})
    with pytest.raises(designs.DesignError, match="not known"):
        designs.normalize({"effects": {"kinds": ["lasers"]}})


@needs_ffmpeg
def test_every_effect_renders_on_a_real_clip_and_a_punch_in_needs_the_face_fill_layout(source, tmp_path):
    d = designs.normalize({"effects": {"intensity": "punchy", "kinds": list(fx_rules.KINDS)}})
    plan = blueprints.resolve("Podcast", designs.layers_from(d), CLIP_TEXT.available(True))
    every = [{"kind": "punch_in", "label": "Punch-in", "start": 0.5, "end": 1.7, "reason": "r"},
             {"kind": "zoom_out", "label": "Zoom-out", "start": 2.2, "end": 3.8, "reason": "r"},
             {"kind": "shake", "label": "Shake", "start": 4.0, "end": 4.6, "reason": "r"},
             {"kind": "flash", "label": "Flash", "start": 4.8, "end": 4.95, "reason": "r"},
             {"kind": "pop", "label": "Colour pop", "start": 5.2, "end": 6.7, "reason": "r"}]
    out = render.render(render.Job(source, 0.2, 8.0, WORDS, CLIP_TEXT, plan, "@x", tmp_path / "fx.mp4", design=d, effects=every), preview=True)
    assert {s["codec_type"] for s in probe(out)} >= {"video", "audio"}
    stream = blueprints.resolve("Stream", designs.layers_from(designs.normalize({"layout": "stacked_webcam"})), CLIP_TEXT.available(True))
    with pytest.raises(render.RenderError, match="needs the face-fill layout"):
        render.render(render.Job(source, 0.2, 3.2, WORDS, CLIP_TEXT, stream, "@x", tmp_path / "bad.mp4",
                                 design=designs.normalize({"layout": "stacked_webcam"}),
                                 effects=[{"kind": "punch_in", "label": "Punch-in", "start": 0.5, "end": 1.7, "reason": "r"}]), preview=True)


# --- phase 5 finish: sound hits, glitch, soft focus and freeze frames ----------------------------------------------------

def test_each_picture_effect_brings_its_matching_sound_and_only_those():
    from villomillo.formats import sounds

    assert sounds.EFFECT_SOUND == {"punch_in": "whoosh", "zoom_out": "whoosh", "shake": "whoosh", "flash": "pop"}
    assert "glitch" not in sounds.EFFECT_SOUND and "freeze" not in sounds.EFFECT_SOUND


def test_a_freeze_holds_the_frames_it_names_at_the_clips_frame_rate():
    assert effects.freeze_frames([{"kind": "freeze", "start": 3.0, "end": 3.5}], fps=24) == [(72, 84)]
    assert effects.freeze_frames([{"kind": "pop", "start": 3.0, "end": 4.5}], fps=24) == []


def test_the_new_effects_are_kinds_the_templates_can_allow_and_their_filters_are_timed():
    for kind in ("glitch", "blur", "freeze"):
        assert kind in effects.KINDS and effects.KINDS[kind]["layouts"]
    timed = effects.colour_filters([{"kind": "glitch", "start": 1.0, "end": 1.25}, {"kind": "blur", "start": 2.0, "end": 2.8}])
    assert timed[0].startswith("rgbashift=") and "between(t,1.00,1.25)" in timed[0]
    assert timed[1].startswith("gblur=") and "between(t,2.00,2.80)" in timed[1]


@needs_ffmpeg
def test_glitch_blur_and_freeze_render_on_a_real_clip(source, tmp_path):
    d = designs.normalize({"effects": {"intensity": "punchy", "kinds": ["glitch", "blur", "freeze"]}})
    plan = blueprints.resolve("Podcast", designs.layers_from(d), CLIP_TEXT.available(True))
    fx = [{"kind": "freeze", "label": "Freeze frame", "start": 1.0, "end": 1.5, "reason": "r"},
          {"kind": "glitch", "label": "Glitch", "start": 2.5, "end": 2.75, "reason": "r"},
          {"kind": "blur", "label": "Soft focus", "start": 4.0, "end": 4.8, "reason": "r"}]
    out = render.render(render.Job(source, 0.2, 6.0, WORDS, CLIP_TEXT, plan, "@x", tmp_path / "new.mp4", design=d, effects=fx), preview=True)
    assert {s["codec_type"] for s in probe(out)} >= {"video", "audio"}


@needs_ffmpeg
def test_a_sound_hit_is_mixed_at_its_moment_and_quieter_than_the_voice(source, tmp_path):
    pop = tone(tmp_path / "pop.wav", seconds=0.2, freq=900)
    d = designs.normalize({})
    plan = blueprints.resolve("Podcast", designs.layers_from(d), CLIP_TEXT.available(True))
    job = render.Job(source, 0.2, 4.0, WORDS, CLIP_TEXT, plan, "@x", tmp_path / "hit.mp4", design=d,
                     sounds=[{"path": str(pop), "start": 1.5, "volume": -12, "name": "Pop"}])
    out = render.render(job, preview=True)
    assert {s["codec_type"] for s in probe(out)} >= {"video", "audio"}
    job.sounds = [{"path": str(tmp_path / "gone.wav"), "start": 1.5, "volume": -12, "name": "Gone"}]
    render.render(job, preview=True)   # a sound file that is no longer there is skipped, never a failed clip


def tone(path, seconds=1.0, freq=440):
    codec = "pcm_s16le" if path.suffix == ".wav" else "libmp3lame"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                    "-c:a", codec, str(path)], check=True, timeout=120)
    return path


# --- phase 4 finish: placement, font uploads --------------------------------------------------------------------------

from villomillo.formats import placement


def placed(**over):
    base = {"layout": "face_fill", "captions": {"enabled": True, "position": "lower"}, "headline": {"enabled": True, "size": 44, "width_pct": 84},
            "logo": {"enabled": False, "corner": "left", "from_s": 0, "size": 14}, "cta": {"enabled": False, "seconds": 3}}
    for key, value in over.items():
        base[key] = {**base[key], **value}
    return base


def test_the_default_placement_is_safe_and_a_big_logo_over_the_headline_is_refused():
    assert placement.problems(placed(logo={"enabled": True}), "face_fill", (151, 151), total=30, head_end=4) == []
    found = placement.problems(placed(logo={"enabled": True, "size": 30}), "face_fill", (324, 260), total=30, head_end=4)
    assert found and found[0].startswith("The logo would cover the headline")


def test_a_logo_that_starts_after_the_headline_may_be_bigger():
    assert placement.problems(placed(logo={"enabled": True, "size": 30, "from_s": 10}), "face_fill", (324, 260), total=30, head_end=4) == []


def test_a_call_to_action_over_the_captions_is_refused_and_a_safe_one_is_not():
    over = placed(captions={"enabled": True, "position": "middle"}, cta={"enabled": True, "seconds": 3})
    assert placement.problems(over, "stacked_webcam", None, total=30, head_end=0)
    assert placement.problems(placed(cta={"enabled": True, "seconds": 3}), "face_fill", None, total=30, head_end=0) == []


def test_a_logo_on_a_stream_goes_on_the_right_and_a_big_one_reaches_the_captions():
    assert placement.problems(placed(logo={"enabled": True, "size": 14}), "stacked_webcam", (151, 151), total=30, head_end=0) == []
    big = placement.problems(placed(logo={"enabled": True, "size": 30}), "stacked_webcam", (324, 260), total=30, head_end=0)
    assert big and "The logo would cover the captions" in big[0]


def test_a_design_whose_logo_would_cover_text_is_refused_before_it_is_saved(tmp_path, monkeypatch):
    from PIL import Image

    from villomillo.formats import assets

    monkeypatch.setattr(assets, "FOLDER", tmp_path / "assets")
    db = Database(tmp_path / "p.db")
    picture = tmp_path / "logo.png"
    Image.new("RGBA", (400, 400), (255, 176, 0, 255)).save(picture)
    row = assets.save_logo(db, "logo.png", picture.read_bytes())
    bad = designs.normalize({"logo": {"enabled": True, "asset_id": row["id"], "size": 30}})
    with pytest.raises(designs.DesignError, match="The logo would cover the headline"):
        placement.check_design(db, bad)
    placement.check_design(db, designs.normalize({"logo": {"enabled": True, "asset_id": row["id"], "size": 14}}))


def test_only_a_real_font_with_its_licence_is_uploaded_and_it_joins_the_picker(tmp_path, monkeypatch):
    from villomillo.formats import font_uploads
    from villomillo.formats import fonts as font_module

    monkeypatch.setattr(font_module, "USER_DIR", tmp_path / "fonts")
    monkeypatch.setattr(font_module, "MERGED_DIR", tmp_path / "fonts-all")
    db = Database(tmp_path / "u.db")
    anton = (font_module.FONTS_DIR / "Anton-400.ttf").read_bytes()
    with pytest.raises(font_uploads.FontUploadError, match="TTF or OTF"):
        font_uploads.save(db, "x", anton, ext=".zip", source="s", licence="l")
    with pytest.raises(font_uploads.FontUploadError, match="not a font"):
        font_uploads.save(db, "x", b"not a font at all", ext=".ttf", source="s", licence="l")
    with pytest.raises(font_uploads.FontUploadError, match="licence record"):
        font_uploads.save(db, "x", anton, ext=".ttf", source="s", licence=" ")
    row = font_uploads.save(db, "My Anton", anton, ext=".ttf", source="Google Fonts", licence="SIL OFL 1.1")
    fid = f"upload-{row['sha'][:10]}"
    assert fid in font_module.FONTS and font_module.FONTS[fid].ass_name == "Anton"
    assert font_module.pil(fid, 40).getlength("HELLO") > 0
    assert designs.normalize({"captions": {"font": fid}})["captions"]["font"] == fid
    assert (font_module.MERGED_DIR / f"{row['sha']}.ttf").exists() and (font_module.MERGED_DIR / "Anton-400.ttf").exists()
    templates.save_design(db, name="Uses it", design={"captions": {"font": fid}})
    with pytest.raises(font_uploads.FontUploadError, match="uses this font"):
        font_uploads.delete(db, row["id"])
    templates.delete(db, templates.list_templates(db)[0]["id"])
    font_uploads.delete(db, row["id"])
    assert fid not in font_module.FONTS and font_uploads.uploads(db) == []


# --- phase 3 finish: split, and the suggested webcam box ------------------------------------------------------------------

def test_split_is_a_layout_that_conflicts_with_the_other_framings_and_is_offered():
    assert "split" in designs.LAYOUTS and designs.normalize({"layout": "split"})["layout"] == "split"
    library = blueprints.load()
    assert "split" in library["stacked_webcam"].conflicts and "stacked_webcam" in library["split"].conflicts
    with pytest.raises(blueprints.BlueprintError):
        blueprints.resolve("two", {"framing": "split", "text": [], "brand": ["logo_corner"]}, {"words"}, forbid=set())


@needs_ffmpeg
def test_split_refuses_a_clip_without_two_people_instead_of_cutting_one_in_half(source):
    with pytest.raises(render.RenderError, match="needs two people in view"):
        render.speaker_centres(source, 0.2, 4.0, 1280)


def test_the_webcam_suggestion_finds_nothing_in_a_clip_without_a_face(source):
    from villomillo.formats import webcam

    assert webcam.suggest(source) is None


# --- captions: never two lines at once, and words that do not touch ---------------------------------------------------------

def fast_words():
    words, t = [], 0.0
    for i, w in enumerate("so what I really mean is that you could never have known this would happen to us".split()):
        words.append({"o0": t, "o1": t + 0.14, "t0": t, "t1": t + 0.14, "text": w})
        t += 0.16 + (0.5 if i == 6 else 0)
    return words


@pytest.mark.parametrize("style", sorted(captions.STYLES))
def test_two_caption_lines_are_never_on_screen_at_once_even_for_a_fast_talker(style):
    import re

    def seconds(stamp):
        h, m, s = stamp.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    ass = captions.build_ass(fast_words(), style, y=1345)
    spans = [(seconds(m.group(1)), seconds(m.group(2))) for m in re.finditer(r"Dialogue: 0,([\d:.]+),([\d:.]+),", ass)]
    assert spans and all(b0 >= a1 - 1e-6 for (_, a1), (b0, _) in zip(spans, spans[1:]))


@pytest.mark.parametrize("style", ["spotlight", "pop", "block", "neon", "marker", "burst", "underline"])
def test_outlined_and_glowing_styles_leave_extra_room_between_words(style):
    ass = captions.build_ass(fast_words(), style, y=1345)
    assert r"\fsp" in ass and captions.word_gap(captions.STYLES[style]) != " "
