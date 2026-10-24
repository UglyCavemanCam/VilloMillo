"""A clip direction (issue #106): what the person asked the clips to be about.

It only adds: points for what matches, windows for what was asked for, a lift
to the bar for a must-have that was actually said. Without one, nothing about
the run changes. Nothing is invented: a must-have that was never said is
reported, and no clip is made for it.
"""

import json
import shutil
from pathlib import Path

import pytest

from analysis import intent

ROOT = Path(__file__).resolve().parent.parent
HOURS = 3 * 3600


class Says:
    """A model that answers with this, whatever it is asked."""

    def __init__(self, answer):
        self.answer = answer if isinstance(answer, str) else json.dumps(answer)

    def generate(self, *_a, **_k):
        return self.answer


def _answer(targets=(), styles=(), avoid=()):
    return {"targets": list(targets), "styles": list(styles), "avoid": list(avoid)}


WOW = {"what": "WoW", "kind": "topic", "strength": "prefer",
       "terms": ["WoW", "World of Warcraft", "Azeroth", "raid"]}


# ---- reading the direction ---------------------------------------------------


def test_nothing_written_is_no_direction():
    assert intent.parse("   ", Says(_answer()), HOURS) is None


def test_a_topic_gets_the_words_it_is_said_with():
    got = intent.parse("Create clips from the parts where I talk about WoW.", Says(_answer([WOW])), HOURS)
    assert [t.what for t in got.targets] == ["WoW"]
    assert "Azeroth" in got.targets[0].terms


def test_funny_is_a_style():
    got = intent.parse("Prioritize funny moments.", Says(_answer()), HOURS)
    assert got.styles == {"funny": "strong"}
    assert got.targets == []


def test_times_are_read_by_the_code_not_the_model():
    got = intent.parse("Make more clips from 1:35:00 to 1:55:00.",
                       Says(_answer([{**WOW, "terms": ["1:35", "WoW"]}])), HOURS)
    assert [(s.start, s.end) for s in got.spans] == [(5700, 6900)]
    assert all(":" not in term for t in got.targets for term in t.terms)


def test_a_short_clock_is_hours_in_a_long_stream_and_minutes_in_a_short_video():
    long = intent.parse("more clips from 1:35-1:55", Says(_answer()), HOURS)
    short = intent.parse("more clips from 12:00-18:00", Says(_answer()), 1500)
    assert [(s.start, s.end) for s in long.spans] == [(5700, 6900)]
    assert [(s.start, s.end) for s in short.spans] == [(720, 1080)]


def test_around_a_time_covers_five_minutes_either_side():
    got = intent.parse("the part around 45 minutes", Says(_answer()), HOURS)
    assert [(s.start, s.end) for s in got.spans] == [(2400, 3000)]


def test_near_the_end_is_the_last_part():
    got = intent.parse("There was a really funny segment near the end.", Says(_answer()), 1000)
    assert [(s.start, s.end) for s in got.spans] == [(850, 1000)]
    assert "funny" in got.styles


def test_make_sure_is_a_must_have():
    boss = {"what": "died to the boss", "kind": "moment", "strength": "must", "terms": ["boss", "died"]}
    got = intent.parse("Make sure there is a clip from when I died to the boss.", Says(_answer([boss])), HOURS)
    assert got.targets[0].strength == "must"


def test_a_must_have_needs_their_words_to_say_so():
    got = intent.parse("clips about WoW", Says(_answer([{**WOW, "strength": "must"}])), HOURS)
    assert got.targets[0].strength == "strong"


def test_avoid_is_understood_and_not_applied():
    got = intent.parse("Don't use the boring intro.", Says(_answer(avoid=["boring intro"])), HOURS)
    assert got.spans == [] and got.targets == []
    assert got.not_applied


def test_a_topic_the_model_brought_itself_is_dropped():
    # gemma:7b answered a time range with a whole game it was never told about.
    got = intent.parse("Make more clips from 1:35:00 to 1:55:00.", Says(_answer([
        {"what": "World of Warcraft", "kind": "topic", "strength": "strong", "terms": ["WoW"]}])), HOURS)
    assert got.targets == []
    assert len(got.spans) == 1


def test_a_style_dressed_as_a_topic_is_not_a_topic():
    got = intent.parse("Find funny moments.", Says(_answer([
        {"what": "funny moments", "kind": "topic", "strength": "prefer", "terms": []}])), HOURS)
    assert got.targets == [] and "funny" in got.styles


def test_a_model_that_ticks_every_style_gets_only_the_ones_named():
    every = [{"style": s, "strength": "prefer"} for s in intent.STYLES]
    got = intent.parse("prioritize funny moments and laughing", Says(_answer(styles=every)), HOURS)
    assert set(got.styles) == {"funny", "laughing"}


def test_at_least_two_clips_is_a_count():
    oot = {"what": "Ocarina of Time", "kind": "topic", "strength": "prefer", "terms": ["Ocarina of Time"]}
    got = intent.parse("Give me at least two clips from the segment where I talked about Ocarina of Time.",
                       Says(_answer([oot])), HOURS)
    assert got.targets[0].count == 2
    assert "Ocarina" in got.targets[0].terms  # the name's own word counts too


def test_every_part_of_a_long_direction_is_kept():
    boss = {"what": "died to the boss", "kind": "moment", "strength": "must", "terms": ["boss", "died"]}
    text = ("I want at least one clip from when I died to the boss, prioritize funny moments and "
            "laughing, and give me more clips from the WoW discussion around 1:35-1:55. Avoid the boring intro.")
    got = intent.parse(text, Says(_answer([boss, {**WOW, "what": "WoW discussion"}])), HOURS)
    assert {t.what for t in got.targets} == {"died to the boss", "WoW discussion"}
    assert [(s.start, s.end) for s in got.spans] == [(5700, 6900)]
    assert set(got.styles) == {"funny", "laughing"}
    assert got.not_applied  # the intro


def test_an_unusable_answer_still_counts_each_part_from_its_words():
    text = ("I want at least one clip from when I died to the boss, prioritize funny moments, "
            "and more from the WoW discussion around 1:35-1:55.")
    got = intent.parse(text, Says("not json at all"), HOURS)
    assert got.fallback
    terms = {term for t in got.targets for term in t.terms}
    assert {"boss", "WoW"} <= terms
    boss = next(t for t in got.targets if "boss" in t.terms)
    assert boss.count == 1  # "at least one clip"


def test_a_model_that_fails_outright_does_not_stop_the_video():
    class Broken:
        def generate(self, *_a, **_k):
            raise TimeoutError("model went away")

    got = intent.parse("more clips about WoW", Broken(), HOURS)
    assert got is not None and got.fallback


# ---- finding it and scoring it -------------------------------------------------


def _segments():
    from core.models import Segment

    segs = [Segment(start=float(s), end=float(s + 5), text="just chatting about nothing much") for s in range(0, 600, 5)]
    for s in segs:
        if 450 <= s.start < 470:
            s.text = "we did the Azeroth raid last night and it was wild"
        if 300 <= s.start < 310:
            s.text = "haha that was hilarious, I can't stop laughing"
    return segs


def test_a_topic_is_found_across_the_whole_transcript():
    got = intent.parse("clips about WoW", Says(_answer([WOW])), 600)
    intent.locate(got, _segments())
    assert got.targets[0].spots and min(got.targets[0].spots) >= 450


def test_wow_the_word_is_not_wow_the_game():
    from core.models import Segment

    got = intent.parse("clips about WoW", Says(_answer([{**WOW, "terms": ["wow", "WoW"]}])), 600)
    intent.locate(got, [Segment(0, 5, "wow that was close"), Segment(5, 10, "back in WoW today")])
    assert got.targets[0].spots == [5]


@pytest.fixture
def fused(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from analysis import fusion, highlights
    from core.models import ClipCandidate

    asked: list = []
    picks = [ClipCandidate(start=s, end=s + 30, score=62, hook="h", reason="r") for s in (0, 100, 200)]
    state = {"window_score": 50}

    def score_windows(_segments, _llm, windows, **_k):
        asked.extend(windows)
        return [ClipCandidate(start=a, end=b, score=state["window_score"], hook="w", source="signal")
                for a, b in windows]

    monkeypatch.setattr(highlights, "find_highlights", lambda *_a, **_k: ([ClipCandidate(
        start=p.start, end=p.end, score=p.score, hook=p.hook, reason=p.reason) for p in picks], []))
    monkeypatch.setattr(highlights, "score_windows", score_windows)
    monkeypatch.setattr(fusion, "reaction_for_window", lambda *_a, **_k: 0.5)

    def run(direction=None, min_score=40, answer=None, window_score=50):
        state["window_score"] = window_score
        asked.clear()
        config = {
            "clips": {"min_duration": 10, "max_duration": 60, "min_score": min_score, "max_clips_per_video": 0},
            "analysis": {"chunk_seconds": 600, "chunk_overlap_seconds": 30,
                         "long_video_threshold_seconds": 3600, "max_overlap": 0.3,
                         "max_text_similarity": 0.8, "max_segment_reuse": 0.5},
            "scoring": {"rerank_pool": 0},
            "tracking": {"detector": "yolov8n-pose.pt"},
        }
        audio = np.zeros(600)
        audio[300:310] = 1.0  # the laughing
        signals = ({"spike": audio}, {"motion": np.zeros(600)})
        got = intent.parse(direction, Says(answer or _answer()), 600) if direction else None
        kept, rejected = fusion.find_clips("vod.mp4", _segments(), Says("{}"), config,
                                           signals=signals, measure_reaction=False,
                                           **({"intent": got} if got is not None else {}))
        return kept, rejected, got, list(asked)

    return run


def _key(clips):
    return sorted((round(c.start, 1), round(c.end, 1), c.score) for c in clips)


def test_no_direction_changes_nothing(fused, monkeypatch):
    before, _r, _i, asked_before = fused()
    monkeypatch.setattr(intent, "parse", lambda *_a, **_k: pytest.fail("no direction, no intent code"))
    again, _r, _i, asked_again = fused()
    assert _key(before) == _key(again) and asked_before == asked_again


def test_a_topic_late_in_the_video_gets_a_window_and_wins(fused):
    kept, _r, got, asked = fused("focus on the WoW talk", answer=_answer([{**WOW, "strength": "strong"}]))
    assert any(a <= 455 <= b for a, b in asked)
    wow = [c for c in kept if (c.subscores or {}).get("intent_why", "").startswith("about WoW")]
    assert wow and got.added >= 1


def test_a_must_have_that_was_said_is_kept_even_below_the_bar(fused):
    boss = {"what": "the Azeroth raid", "kind": "moment", "strength": "must", "terms": ["Azeroth", "raid"]}
    kept, _r, got, _a = fused("make sure you include the Azeroth raid", min_score=55,
                              answer=_answer([boss]), window_score=20)
    required = [c for c in kept if (c.subscores or {}).get("required")]
    assert required and required[0].score >= 55
    assert got.not_found == []


def test_a_must_have_that_was_never_said_is_reported_not_invented(fused):
    win = {"what": "the tournament win", "kind": "moment", "strength": "must", "terms": ["tournament"]}
    kept, _r, got, _a = fused("make sure you include when I won the tournament", answer=_answer([win]))
    assert got.not_found == ["the tournament win"]
    assert not any((c.subscores or {}).get("required") for c in kept)
    assert got.report()["not_found"] == ["the tournament win"]


def test_a_direction_never_lowers_a_score(fused):
    before, _r, _i, _a = fused()
    after, _r, _i, _a = fused("prioritize funny moments and laughing")
    was = {(round(c.start), round(c.end)): c.score for c in before}
    for c in after:
        if (round(c.start), round(c.end)) in was:
            assert c.score >= was[(round(c.start), round(c.end))]


def test_the_laughing_gets_the_funny_points(fused):
    kept, rejected, _i, _a = fused("prioritize funny moments and laughing")
    everything = kept + [r.candidate for r in rejected]
    laughing = [c for c in everything if c.start <= 305 <= c.end]
    assert laughing and all("funny" in c.subscores.get("intent_why", "") for c in laughing)


def test_funny_does_not_make_duplicates(fused):
    kept, _r, _i, _a = fused("prioritize funny moments and laughing")
    for i, a in enumerate(kept):
        for b in kept[i + 1:]:
            assert a.overlap_ratio(b) <= 0.3


def test_a_time_range_gets_windows_and_points(fused):
    _k, _r, _i, signal_peaks = fused()
    kept, _r, got, asked = fused("more clips from 8:00-9:50")
    added = [w for w in asked if w not in signal_peaks]
    assert added and all(480 <= a and b <= 590 for a, b in added)
    assert any("in 8:00-9:50" in (c.subscores or {}).get("intent_why", "") for c in kept)


# ---- the job option ------------------------------------------------------------


def test_no_focus_means_the_intent_code_never_runs(monkeypatch):
    pipeline = pytest.importorskip("core.pipeline")  # CI installs only the light dependencies
    monkeypatch.setattr(intent, "parse", lambda *_a, **_k: pytest.fail("called without a direction"))
    assert pipeline.clip_direction({"clips": {}}, Says("{}"), 600) is None
    assert pipeline.clip_direction({"clips": {"focus": "  "}}, Says("{}"), 600) is None


def test_the_option_reaches_the_payload_trimmed():
    pytest.importorskip("fastapi")
    from server.api import JobIn, JobPatch, LocalVideoIn, _process_options

    assert _process_options(JobIn(url="u", focus="  more   WoW  "))["focus"] == "more WoW"
    assert _process_options(JobPatch(focus="x" * 900))["focus"] == "x" * intent.MAX_CHARS
    assert _process_options(LocalVideoIn(path="p.mp4", focus="funny"))["focus"] == "funny"
    assert "focus" not in _process_options(JobIn(url="u"))


def test_the_assistant_tool_passes_the_direction(monkeypatch):
    from server import mcp

    sent = {}
    monkeypatch.setattr(mcp, "_request", lambda _m, path, body=None, **_k: sent.update({path: body}) or {"job_id": 7})
    mcp._queue_video({"url": "https://youtu.be/abcdefghijk", "focus": "funny moments about WoW"})
    mcp._queue_local_file({"path": "C:/v.mp4", "focus": "near the end"})
    assert sent["/jobs"]["focus"] == "funny moments about WoW"
    assert sent["/videos/local"]["focus"] == "near the end"
    names = {t["name"]: t for t in mcp.TOOLS}
    assert "focus" in names["queue_video"]["inputSchema"]["properties"]
    assert "focus" in names["queue_local_file"]["inputSchema"]["properties"]


def test_batch_and_patch_carry_it(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("yaml")
    pytest.importorskip("yt_dlp")
    from fastapi.testclient import TestClient

    from main import BUNDLED_CONFIG, load_config
    from server.api import create_app

    settings = tmp_path / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings)
    config = load_config(BUNDLED_CONFIG)
    config["paths"]["data_dir"] = str(tmp_path / "data")
    client = TestClient(create_app(config, settings), base_url="http://127.0.0.1")
    made = client.post("/jobs/batch", json={"items": [
        {"url": "https://www.youtube.com/watch?v=abcdefghijk", "focus": "the WoW part"}]}).json()
    job_id = made["created"][0]["job_id"]
    assert json.loads(client.get(f"/jobs/{job_id}").json()["payload"])["focus"] == "the WoW part"
    assert client.patch(f"/jobs/{job_id}", json={"focus": "funny moments"}).status_code == 200
    assert json.loads(client.get(f"/jobs/{job_id}").json()["payload"])["focus"] == "funny moments"


def test_the_schema_is_one_strict_structured_output_accepts():
    # OpenAI's strict mode (and OpenRouter models that take it) refuses a schema
    # unless every object lists all its properties as required and closes
    # itself. A refusal is remembered per model and would drop the clip
    # scoring's own schema too.
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                assert set(node.get("required", [])) == set(node.get("properties", {}))
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(intent.SCHEMA)


def test_a_generic_word_is_not_a_search_word():
    # OpenRouter's Gemma 4 gave "discussion" for "the WoW discussion".
    wow = {**WOW, "what": "WoW discussion", "terms": ["WoW", "discussion", "World of Warcraft"]}
    got = intent.parse("more clips from the WoW discussion", Says(_answer([wow])), HOURS)
    assert got.targets[0].terms == ["WoW", "World of Warcraft"]
