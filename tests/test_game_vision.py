"""The AI looks at a gaming stream's best candidates (analysis/game_vision.py).

A local model that takes images is shown a few frames of each and says what
happens: a clear moment raises a clip, a menu or a black screen lowers it,
by at most 10 points. A model that can't see, and any cloud model, is
skipped. The model and the video are stood in for.
"""

import json

import pytest

from analysis import game_vision, gaming
from analysis.game_vision import frame_times, verdict_delta


def test_the_verdict_moves_a_clip_by_at_most_ten():
    assert verdict_delta({"gameplay": False, "moment": "settings menu", "strength": 9}) == (-10, "settings menu")
    assert verdict_delta({"gameplay": "false", "strength": 3}) == (-10, "not gameplay")
    assert verdict_delta({"gameplay": True, "moment": "scores a goal", "strength": 8}) == (6, "scores a goal")
    assert verdict_delta({"gameplay": True, "moment": "runs", "strength": 5}) == (0, "runs")
    assert verdict_delta({"gameplay": True, "strength": 14})[0] == 10
    assert verdict_delta({"gameplay": True, "strength": "2"})[0] == -6
    assert verdict_delta({"gameplay": True, "strength": "lots"}) is None
    assert verdict_delta(None) is None


def test_the_frames_cover_the_setup_the_play_and_the_reaction():
    assert frame_times(100, 124) == [103.0, 109.0, 115.0, 121.0]


class _Seer:
    """A local model that sees: says what each clip is by its first frame's value."""
    name = "ollama/test-vision"

    def __init__(self, verdicts, colour="red"):
        self.verdicts = verdicts
        self.colour = colour
        self.prompts = []

    def sees_images(self):
        return True

    def look(self, prompt, images):
        if "single colour" in prompt:
            return json.dumps({"colour": self.colour})
        self.prompts.append((prompt, len(images)))
        return json.dumps(self.verdicts.pop(0))


def _clips(scores):
    from core.models import ClipCandidate

    return [ClipCandidate(start=float(100 * i), end=float(100 * i + 24), score=s, subscores={})
            for i, s in enumerate(scores)]


def test_the_best_candidates_are_looked_at_and_moved():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from core.models import Segment

    clips = _clips([60, 80, 70])
    llm = _Seer([{"gameplay": True, "moment": "scores a goal", "strength": 9},       # the 80
                 {"gameplay": False, "moment": "settings menu", "strength": 0}])      # the 70
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "Rocket League"}], "")
    segments = [Segment(start=101.0, end=104.0, text="WHAT A GOAL")]
    events = [(103.0, "GAME SOUND: an explosion")]
    looked = game_vision.look_at(clips, "v.mp4", llm, profile, segments, events,
                                 grab=lambda t: np.full((720, 1280, 3), 40, dtype=np.uint8), max_candidates=2)
    assert looked == 2
    first, goal, menu = clips
    assert goal.score == 88 and goal.subscores["seen"] == 8 and goal.subscores["seen_what"] == "scores a goal"
    assert goal.subscores["game_why"].startswith("SEEN: scores a goal")
    assert menu.score == 60 and menu.subscores["seen"] == -10
    assert first.score == 60 and "seen" not in first.subscores                  # not among the best 2
    prompt, n = llm.prompts[0]
    assert n == 4 and "Rocket League (sports game)" in prompt and "WHAT A GOAL" in prompt
    assert "3s: GAME SOUND: an explosion" in prompt


def test_what_the_picture_shows_only_takes_points_off_a_quiet_clip():
    """Just chatting, or a reaction with no gameplay in the picture, is
    judged on the talk: no gameplay costs a clip full of talk nothing, half
    as much when half of it is talk, and the full 10 when it is silent. A
    clear moment adds the same either way."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")

    clips = _clips([80, 70, 60, 50])
    talk = {0.0: 0.0, 100.0: 0.5, 200.0: 1.0, 300.0: 1.0}
    none = {"gameplay": False, "moment": "the streamer talking to chat", "strength": 0}
    llm = _Seer([none, none, none, {"gameplay": True, "moment": "wins the fight", "strength": 9}])
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "Apex Legends"}], "")
    game_vision.look_at(clips, "v.mp4", llm, profile, [], [], talk=lambda c: talk[c.start],
                        grab=lambda t: np.full((720, 1280, 3), 40, dtype=np.uint8), max_candidates=4)
    assert [c.subscores["seen"] for c in clips] == [-10, -5, 0, 8]
    assert [c.score for c in clips] == [70, 65, 60, 58]


def test_a_model_that_says_it_sees_but_does_not_is_found_out():
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    game_vision._can_see.clear()
    assert game_vision.can_see(_Seer([], colour="red"))
    blind = _Seer([], colour="a dark, abstract background")
    blind.name = "ollama/blind"
    assert not game_vision.can_see(blind)


def test_fusion_skips_a_model_that_cannot_see_and_drops_what_looking_sinks(monkeypatch, capsys):
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from analysis import fusion

    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "Apex Legends"}], "")
    clips = _clips([70, 70])
    fusion._look_at_game(clips, "v.mp4", None, profile, [], [])            # e.g. a cloud model
    assert "skipped" in capsys.readouterr().out and all("seen" not in c.subscores for c in clips)

    game_vision._can_see.clear()

    def fake_look(finalists, *_a, **_k):
        finalists[0].score -= 10
        finalists[0].subscores["seen"] = -10
        return 1
    monkeypatch.setattr(game_vision, "look_at", fake_look)
    fusion._look_at_game(clips, "v.mp4", _Seer([]), profile, [], [])
    assert "1 lowered" in capsys.readouterr().out and clips[0].score == 60


def test_a_clip_looking_sinks_under_the_bar_is_rejected(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from analysis import fusion, highlights
    from core.models import ClipCandidate, Segment

    monkeypatch.setattr(highlights, "find_highlights", lambda *_a, **_k: (
        [ClipCandidate(start=0, end=30, score=90, hook="h", reason="r"),
         ClipCandidate(start=200, end=230, score=90, hook="h", reason="r")], []))
    monkeypatch.setattr(highlights, "score_windows", lambda _s, _l, windows, **_k: [])
    monkeypatch.setattr(fusion, "reaction_for_window", lambda *_a, **_k: 0.5)

    def sink_the_second(finalists, *_a, **_k):
        target = next(c for c in finalists if c.start == 200)
        target.score = 0
        target.subscores["seen"] = -10
    monkeypatch.setattr(fusion, "_look_at_game", sink_the_second)
    n = 600
    audio = {"spike": np.ones(n, dtype=np.float32), "burst": np.zeros(n), "noisiness": np.zeros(n)}
    cfg = {"clips": {"min_duration": 10, "max_duration": 60, "min_score": 1, "max_clips_per_video": 0},
           "analysis": {"chunk_seconds": 600, "chunk_overlap_seconds": 30, "long_video_threshold_seconds": 3600,
                        "max_overlap": 0.3, "max_text_similarity": 0.8, "max_segment_reuse": 0.5},
           "scoring": {"rerank_pool": 0, "read_screen": False}, "tracking": {"detector": "yolov8n-pose.pt"}}
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "Apex Legends"}], "")
    kept, rejections = fusion.find_clips("v.mp4", [Segment(start=1.0, end=3.0, text="go")], None, cfg,
                                         signals=(audio, {"motion": np.zeros(n)}), gaming=profile)
    assert [c.start for c in kept] == [0]
    assert [(r.candidate.start, r.reason) for r in rejections] == [(200, "below_min_score")]
