"""Format blueprints (F1): the resolver's rules, the on-screen text checks, the brand marks, a real render from a
synthetic video, and the clip pipeline's fallback. Claude and Jev are fakes."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from villomillo import clipper, config, formats, store
from villomillo.db import Database
from villomillo.formats import blueprints, render, text
from villomillo.jev import JevResult

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                                  reason="ffmpeg is not installed")
WORDS = [{"t0": 0.5 + i * 0.25, "t1": 0.7 + i * 0.25, "text": w} for i, w in enumerate(
    "Luck is something you can train. Social skills open doors. Another skill is an open mind.".split())]


# --- blueprints and the resolver -------------------------------------------------------------------------------

def test_the_shipped_blueprint_file_is_valid_and_has_the_recipes():
    library, recipes = blueprints.load()
    assert {"podcast_talk", "generic_safe"} <= set(recipes)
    assert library["clean_frame"].layer == "framing" and library["hook_title"].added_value


def test_a_full_clip_gets_every_blueprint_of_the_recipe():
    plan = blueprints.resolve("podcast_talk", {"words", "title", "points", "takeaway"})
    assert plan.ids() == ["clean_frame", "hook_title", "captions_plain", "point_chips", "takeaway", "slow_push",
                          "progress_bar_bottom", "watermark_center"]
    assert plan.dropped == []


def test_blueprints_whose_needs_are_missing_are_dropped_and_reported():
    plan = blueprints.resolve("podcast_talk", {"words", "title"})
    assert not plan.has("point_chips") and not plan.has("takeaway")
    assert ("point_chips", "the clip has no points") in plan.dropped


def test_a_plan_with_nothing_but_a_crop_and_captions_is_refused():
    with pytest.raises(blueprints.OriginalityError, match="nothing but a crop and captions"):
        blueprints.resolve("podcast_talk", {"words"})
    with pytest.raises(blueprints.OriginalityError):
        blueprints.resolve("generic_safe", {"words", "title"}, forbid={"hook_title"})


def test_a_channel_can_forbid_a_blueprint():
    plan = blueprints.resolve("podcast_talk", {"words", "title"}, forbid={"watermark_center"})
    assert not plan.has("watermark_center") and ("watermark_center", "not allowed for this channel") in plan.dropped


def test_channel_overrides_change_a_blueprints_parameters():
    plan = blueprints.resolve("podcast_talk", {"words", "title"}, overrides={"clean_frame": {"keep_right_of": 0.4}})
    assert plan.framing_param("keep_right_of") == 0.4 and plan.framing_param("window_h") == 1000


def write_file(tmp_path, data) -> Path:
    path = tmp_path / "bp.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


BASE = {"blueprints": {
    "a": {"layer": "framing", "conflicts": ["b"]}, "b": {"layer": "framing", "conflicts": ["a"]},
    "t": {"layer": "text", "added_value": True}}}


@pytest.mark.parametrize(
    ("recipe", "message"),
    [
        ({"framing": "a", "text": ["zzz"]}, "unknown blueprint"),
        ({"framing": "t"}, "not framing"),
        ({"text": ["t"]}, "exactly one framing"),
        ({"framing": "a", "motion": ["t"]}, "is a text blueprint, not motion"),
    ],
)
def test_a_broken_recipe_is_refused_when_loaded(tmp_path, recipe, message):
    with pytest.raises(blueprints.BlueprintError, match=message):
        blueprints.load(write_file(tmp_path, {**BASE, "recipes": {"r": recipe}}))


def test_a_recipe_cannot_stack_blueprints_that_conflict(tmp_path):
    data = {"blueprints": {**BASE["blueprints"], "c": {"layer": "text", "conflicts": ["t"]}},
            "recipes": {"r": {"framing": "a", "text": ["t", "c"]}}}
    with pytest.raises(blueprints.BlueprintError, match="conflicts with"):
        blueprints.load(write_file(tmp_path, data))


# --- the title stays up long enough to read ---------------------------------------------------------------------

def test_a_title_stays_up_a_second_plus_a_little_per_word_within_limits():
    nine = ["FEEL LIKE DOORS STAY CLOSED?", "BUILD THESE TWO SKILLS"]
    assert render.title_seconds(nine, 1.0, 0.4, 2.5, 7.0) == pytest.approx(4.6)
    assert render.title_seconds(["HI"], 1.0, 0.4, 2.5, 7.0) == 2.5
    assert render.title_seconds(["WORD " * 40], 1.0, 0.4, 2.5, 7.0) == 7.0


# --- the on-screen text -----------------------------------------------------------------------------------------

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
    assert clip_text.takeaway and cost["claude"] > 0 and cost["jev_tokens"] == 500


def test_a_title_that_may_misstate_the_speaker_is_rewritten_twice_then_refused():
    claude_client = FakeClaude(good_raw(), good_raw(), good_raw())
    with pytest.raises(text.TextProblem, match="may misstate"):
        make(claude_client, FakeJev(title=0.8))
    assert claude_client.calls == 3


def test_a_point_or_takeaway_that_may_misstate_the_speaker_is_left_out_not_fatal():
    clip_text, _ = make(FakeClaude(good_raw()), FakeJev(point_0=0.9, takeaway=0.9))
    assert [label for label, _ in clip_text.points] == ["AN OPEN MIND"] and clip_text.takeaway == ""


# --- the brand marks --------------------------------------------------------------------------------------------

def test_the_watermark_is_faint_and_sits_in_the_middle():
    plan = blueprints.resolve("podcast_talk", {"words", "title"})
    black = np.zeros((render.H, render.W, 3), np.uint8)
    out = render.composite(black, lambda d: render.paint_brand(d, plan, 0.0, 1.0, "@cavemancam"))
    rows, cols = np.nonzero(out.max(axis=2))
    assert out.max() <= 40, "faint: at most about 15% white over black"
    assert abs(rows.mean() - render.H / 2) < 40 and abs(cols.mean() - render.W / 2) < 40
    assert cols.max() - cols.min() < 400, "small"


def test_a_long_label_shrinks_to_fit_inside_the_frame():
    black = np.zeros((render.H, render.W, 3), np.uint8)
    out = render.composite(black, lambda d: render.pill(
        d, render.W // 2, 400, "SOME LUCK CAN BE GROWN WITH SKILLS AND A LONG TAIL", 46, render.AMBER, render.INK))
    cols = np.nonzero(out.max(axis=2).any(axis=0))[0]
    assert cols.min() >= 20 and cols.max() <= render.W - 20


def test_the_progress_bar_is_flush_with_the_bottom_and_fills_with_time():
    plan = blueprints.resolve("podcast_talk", {"words", "title"})
    black = np.zeros((render.H, render.W, 3), np.uint8)
    out = render.composite(black, lambda d: render.paint_bar(d, plan, 5.0, 10.0))
    assert out[render.H - 1, 10].tolist() != [0, 0, 0] and out[render.H - 15, 10].tolist() == [0, 0, 0]
    filled = int((out[render.H - 5].astype(int) == np.array(render.AMBER)).all(axis=1).sum())
    assert abs(filled - render.W // 2) < 4


# --- a real render ----------------------------------------------------------------------------------------------

def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=size=1280x720:rate=24:duration=5", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
                   check=True, timeout=120)
    return path


@needs_ffmpeg
@pytest.mark.parametrize("recipe", ["podcast_talk", "generic_safe"])
def test_a_clip_is_rendered_in_a_recipe_as_vertical_video_with_sound(source, tmp_path, recipe):
    clip_text = render.ClipText(["LUCK CAN BE TRAINED", "TWO WAYS"], [("SOCIAL SKILLS", 1.0)], "SKILLS EXPAND LUCK")
    plan = blueprints.resolve(recipe, clip_text.available(True))
    out = render.render(render.Job(source, 0.2, 3.2, WORDS, clip_text, plan, "@cavemancam", tmp_path / "out.mp4"))
    streams = {s["codec_type"]: s for s in probe(out)}
    assert (streams["video"]["width"], streams["video"]["height"]) == (1080, 1920)
    assert abs(float(streams["video"]["duration"]) - 3.0) < 0.2 and "audio" in streams


# --- the clip pipeline never loses a clip over a look -----------------------------------------------------------

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
    profile = config.Profile(id="test", display_name="Test", niche={"definition": "talks"}, format="podcast_talk")
    with db.conn() as c:
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES ('test', 'abcdefghijk', 1, 'submitted', ?, ?)", (store.iso(), store.iso()))
        job = dict(c.execute("SELECT * FROM clip_jobs").fetchone())
    return db, profile, job, make_clip(tmp_path / "engine.mp4").read_bytes(), tmp_path


@needs_ffmpeg
def test_a_formatted_clip_replaces_the_engines_render_and_records_the_recipe(collected):
    db, profile, job, engine_media, tmp_path = collected
    styled = make_clip(tmp_path / "styled.mp4").read_bytes() + b"\0"   # different bytes, still a valid clip

    def formatter(clip, job, out_path):
        out_path.write_bytes(styled)
        return {"recipe": "podcast_talk"}

    stub = Stub(engine_media)
    assert clipper.collect(db, stub, stub, profile, job, formatter).startswith("1 clips made")
    path = clipper.OUTBOX / "test" / "1.mp4"
    assert path.read_bytes() == styled and (clipper.OUTBOX / "test" / "1.engine.mp4").read_bytes() == engine_media
    with db.conn() as c:
        row = c.execute("SELECT state, grade_json FROM clips_ready").fetchone()
    assert row["state"] == "review" and json.loads(row["grade_json"])["look"] == "recipe podcast_talk"


@needs_ffmpeg
def test_a_look_that_fails_keeps_the_engines_render_and_says_so(collected):
    db, profile, job, engine_media, _ = collected

    def formatter(clip, job, out_path):
        raise formats.FormatError("the source video is not on disk (abcdefghijk.mp4)")

    stub = Stub(engine_media)
    clipper.collect(db, stub, stub, profile, job, formatter)
    assert (clipper.OUTBOX / "test" / "1.mp4").read_bytes() == engine_media
    with db.conn() as c:
        row = c.execute("SELECT state, reason FROM clips_ready").fetchone()
    assert row["state"] == "review" and "look not applied: the source video is not on disk" in row["reason"]


def test_a_channel_without_a_recipe_gets_no_formatter(tmp_path):
    db = Database(tmp_path / "f.db")
    plain = config.Profile(id="plain", display_name="Plain", niche={"definition": "x"})
    assert formats.formatter_for(db, plain) is None
    assert formats.formatter_for(db, plain.model_copy(update={"format": "podcast_talk"})) is not None


def test_formatting_stops_cleanly_without_a_source_or_words(tmp_path):
    db = Database(tmp_path / "f.db")
    profile = config.Profile(id="tt", display_name="T", niche={"definition": "x"}, format="podcast_talk")
    kwargs = {"claude_client": None, "jev": None, "video_id": "v", "start_s": 0.0, "end_s": 10.0,
              "out_path": tmp_path / "o.mp4", "episode": "E"}
    with pytest.raises(formats.FormatError, match="not on disk"):
        formats.format_clip(db, profile, source=tmp_path / "missing.mp4", words=WORDS, **kwargs)
    (tmp_path / "s.mp4").write_bytes(b"x")
    with pytest.raises(formats.FormatError, match="no words"):
        formats.format_clip(db, profile, source=tmp_path / "s.mp4", words=[], **kwargs)
