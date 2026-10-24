"""Scoring a gaming stream as a gaming stream (analysis/gaming.py,
analysis/chat_moments.py, the gaming profile in analysis/fusion.py).

In-game moments count even when the streamer says little: chat's reactions
and the streamer's sudden shouts mark them, the prompts are told what a
highlight is in this kind of game, and standard scoring is untouched.
"""

import json
import random

import pytest

from analysis import gaming
from core import modes
from sources.ytdlp_common import games_from_info

# ---- which game, which genre ------------------------------------------------------------


@pytest.mark.parametrize("name,genre", [
    ("Marvel Rivals", "shooter"), ("VALORANT", "shooter"), ("EA SPORTS FC 25", "sports"),
    ("Elden Ring first playthrough!", "soulslike"), ("Dark Souls III", "soulslike"),
    ("League of Legends", "moba"), ("Fortnite", "battle_royale"), ("Minecraft", "sandbox"),
    ("The Legend of Zelda: Breath of the Wild", "adventure"), ("any% speedrun WR attempts", "speedrun"),
    ("Just Chatting", "reaction"),
])
def test_a_game_names_its_genre(name, genre):
    assert gaming.genre_of(name) == genre


def test_short_game_names_only_match_whole_words():
    assert gaming.genre_of("trust issues with my team") is None     # not "rust"
    assert gaming.genre_of("a dark and stormy night") is None       # not "ark"


def test_the_game_played_longest_is_the_main_one():
    games = [{"name": "Just Chatting", "start": 0, "end": 600},
             {"name": "VALORANT", "start": 600, "end": 9000}]
    p = gaming.profile_for({"clips": {"gaming_scoring": True}}, games, "late night grind")
    assert p.game == "VALORANT" and p.genre == "shooter"


def test_the_title_names_the_game_when_the_platform_does_not():
    p = gaming.profile_for({"clips": {"gaming_scoring": True}}, [], "First time playing ELDEN RING")
    assert p.genre == "soulslike" and p.game == ""


def test_unknown_games_score_as_a_generic_game():
    assert gaming.profile_for({"clips": {"gaming_scoring": True}}, [], "chill stream").genre == "generic"


def test_a_gaming_stream_is_weighted_like_any_stream_and_the_game_adds_on_top():
    """A reaction to BlizzCon is talk: weighting a game channel at 40% left a
    two-hour reaction with 13 clips. The words weigh what they do anywhere."""
    split = gaming.profile_for({"clips": {"gaming": True}}, [], "")
    vertical = gaming.profile_for({"clips": {"gaming_scoring": True, "vertical_live": True}}, [], "")
    assert split.weights == vertical.weights == gaming.STANDARD_WEIGHTS
    assert "game" not in split.weights
    mine = {"text": 0.4, "visual": 0.2, "reaction": 0.1, "audio": 0.2, "engagement": 0.1}
    assert gaming.profile_for({"clips": {"gaming": True}, "scoring": {"weights": mine}}, [], "").weights == mine


def test_settings_can_set_the_gaming_weights():
    cfg = {"clips": {"gaming_scoring": True},
           "scoring": {"profiles": {"gaming": {"weights": {"text": 0.5, "engagement": 0, "audio": 0.1,
                                                           "visual": 0.1, "reaction": 0.1, "game": 0.2}}}}}
    assert gaming.profile_for(cfg, [], "").weights["text"] == 0.5


def test_the_guidance_names_the_game_and_counts_quiet_plays():
    text = gaming.profile_for({"clips": {"gaming_scoring": True}},
                              [{"name": "Marvel Rivals", "start": 0, "end": 10}], "").guidance()
    assert "Marvel Rivals (shooter)" in text
    assert "little is said" in text and "menus" in text
    assert "kills and multi-kills" in text
    reaction = gaming.GamingProfile(genre="reaction").guidance()
    assert "reaction stream" in reaction and "they're watching" in reaction
    # Talk is judged as on any stream; the game adds to it.
    assert "exactly as you would on any stream" in text and "off-topic" not in text
    assert len(gaming.GamingProfile().guidance("rerank")) < len(gaming.GamingProfile().guidance())


def test_gaming_scoring_is_opt_in_and_implied_by_the_split_layout():
    assert not modes.gaming_scoring({"clips": {}})
    assert not modes.gaming_scoring({"clips": {"vertical_live": True}})
    assert modes.gaming_scoring({"clips": {"gaming": True}})
    assert modes.gaming_scoring({"clips": {"gaming_scoring": True, "vertical_live": True}})


# ---- what the platform says was played --------------------------------------------------


def test_twitch_game_chapters_become_games():
    info = {"duration": 5000, "chapters": [{"start_time": 0, "end_time": 900, "title": "Just Chatting"},
                                           {"start_time": 900, "end_time": 5000, "title": "Elden Ring"}]}
    assert games_from_info(info, "twitch") == [{"name": "Just Chatting", "start": 0.0, "end": 900.0},
                                               {"name": "Elden Ring", "start": 900.0, "end": 5000.0}]


def test_kick_category_and_youtube_gaming_tags():
    assert games_from_info({"duration": 60, "categories": ["VALORANT"]}, "kick")[0]["name"] == "VALORANT"
    yt = games_from_info({"duration": 60, "categories": ["Gaming"], "tags": ["marvel rivals", "gorr"]},
                         "youtube")
    assert yt == [{"name": "", "start": 0.0, "end": 60.0, "hint": "marvel rivals gorr"}]
    assert games_from_info({"duration": 60, "categories": ["Education"], "tags": ["x"]}, "youtube") == []
    # A YouTube video's chapters are the creator's own titles, not games.
    assert games_from_info({"duration": 60, "chapters": [{"title": "Intro"}]}, "youtube") == []


def test_the_games_are_kept_for_a_rerun(db):
    db.upsert_video("tw_1", title="t")
    db.set_video_games("tw_1", [{"name": "Rust", "start": 0, "end": 10}])
    db.set_video_games("tw_1", [])                       # an empty list never overwrites
    assert db.video_games("tw_1") == [{"name": "Rust", "start": 0, "end": 10}]
    assert db.video_games("missing") == []


# ---- chat's reactions ----------------------------------------------------------------------


def _chat(duration=600, bursts=(), seed=1):
    rng = random.Random(seed)
    msgs = [(t + rng.random(), f"u{rng.randint(0, 50)}", rng.choice(["hi", "nice", "lol ok", "how are you"]))
            for t in range(duration) if rng.random() < 0.8]
    for at, word, count in bursts:
        msgs += [(at + i * 5.0 / count, f"b{i}", word) for i in range(count)]
    return sorted(msgs)


def _signal(msgs, duration=600):
    np = pytest.importorskip("numpy")
    from analysis.chat_moments import chat_signal

    k = gaming.knowledge()
    return np, chat_signal(msgs, duration, k["chat_classes"], k["chat_lag_seconds"])


def test_a_chat_burst_marks_the_moment_before_it_and_says_what_kind():
    np, sig = _signal(_chat(bursts=[(200, "KEKW KEKW", 40), (400, "POGGERS NO WAY", 40)]))
    events = dict(sig.events)
    laugh = [t for t, d in sig.events if "laughing" in d]
    hype = [t for t, d in sig.events if "hype" in d]
    assert laugh and abs(laugh[0] - 194) <= 2                   # dated back by chat's lag
    assert hype and abs(hype[0] - 394) <= 2
    assert "KEKW" in events[laugh[0]]
    assert sig.curve[int(laugh[0]):int(laugh[0]) + 6].max() > 0.9 and float(np.median(sig.curve)) < 0.05
    assert len(sig.events) == 2                                # nothing where chat was normal


def test_normal_chat_marks_nothing_and_too_little_chat_says_nothing():
    _np, sig = _signal(_chat())
    assert sig.events == []
    _np, none = _signal([(10.0, "a", "hi"), (20.0, "b", "hey")])
    assert none is None


def test_words_count_only_as_written():
    pytest.importorskip("numpy")
    from analysis.chat_moments import _matchers, classify

    m = _matchers(gaming.knowledge()["chat_classes"])
    assert "hype" in classify("HOW", m) and "hype" not in classify("how do you do that", m)
    assert "hype" in classify("no way that happened", m)
    assert "funny" in classify("😂😂", m) and "clip" in classify("someone clip that", m)
    assert classify("hello there", m) == {}


# ---- the fused score --------------------------------------------------------------------


@pytest.fixture
def fusing(monkeypatch):
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from analysis import fusion, highlights
    from core.models import ClipCandidate

    seen = {"prompts": [], "windows": []}

    def fake_find(*_a, **k):
        seen["guidance"] = k.get("guidance", "")
        seen["title"] = k.get("events_title")
        return [ClipCandidate(start=0, end=30, score=60, hook="h", reason="r")], []

    def fake_windows(_segments, _llm, windows, **k):
        seen["windows"] = list(windows)
        seen["windows_guidance"] = k.get("guidance", "")
        return [ClipCandidate(start=s, end=e, score=60, hook="h", source="signal") for s, e in windows]

    monkeypatch.setattr(highlights, "find_highlights", fake_find)
    monkeypatch.setattr(highlights, "score_windows", fake_windows)
    monkeypatch.setattr(fusion, "reaction_for_window", lambda *_a, **_k: 0.5)
    return np, fusion, seen


CFG = {
    "clips": {"min_duration": 10, "max_duration": 60, "min_score": 0, "max_clips_per_video": 0},
    "analysis": {"chunk_seconds": 600, "chunk_overlap_seconds": 30, "long_video_threshold_seconds": 3600,
                 "max_overlap": 0.3, "max_text_similarity": 0.8, "max_segment_reuse": 0.5},
    "scoring": {"rerank_pool": 0},
    "tracking": {"detector": "yolov8n-pose.pt"},
}


def _quiet_stream(np, n=600, shout_at=None):
    """A quiet game stream: almost nothing said, one loud moment. When there
    is a shout, the streamer talked normally half a minute before it, which
    is what the shout is measured against."""
    from core.models import Segment

    spike = np.ones(n, dtype=np.float32)
    burst = np.zeros(n, dtype=np.float32)
    loudness = np.full(n, 0.02, dtype=np.float32)
    segments = []
    if shout_at is not None:
        spike[shout_at:shout_at + 3] = 4.5
        burst[shout_at:shout_at + 3] = 8
        loudness[shout_at - 40:shout_at - 25] = 0.1
        loudness[shout_at:shout_at + 3] = 0.4
        segments.append(Segment(start=float(shout_at - 40), end=float(shout_at - 25), text="ok let's go in",
                                words=[{"start": float(shout_at - 40), "end": float(shout_at - 25), "word": "ok"}]))
    segments.append(Segment(start=float(shout_at or 5), end=float((shout_at or 5) + 3), text="NO WAY",
                            words=[{"start": float(shout_at or 5), "end": float((shout_at or 5) + 2), "word": "NO WAY"}]))
    audio = {"spike": spike, "burst": burst, "noisiness": np.zeros(n), "loudness": loudness}
    visual = {"motion": np.zeros(n)}
    return segments, (audio, visual)


class _NoLLM:
    def generate(self, *_a, **_k):
        raise RuntimeError("no model in this test")


def test_a_quiet_play_that_chat_reacts_to_becomes_a_clip_with_a_game_bonus(fusing):
    np, fusion, seen = fusing
    from analysis.chat_moments import chat_signal

    segments, signals = _quiet_stream(np, shout_at=300)
    k = gaming.knowledge()
    chat = chat_signal(_chat(bursts=[(306, "POGGERS", 40)]), 600, k["chat_classes"], k["chat_lag_seconds"])
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "VALORANT"}], "")
    kept, _ = fusion.find_clips("v.mp4", segments, _NoLLM(), CFG, signals=signals, gaming=profile, chat=chat)
    moment = [c for c in kept if c.start <= 300 <= c.end]
    assert moment, [(c.start, c.end) for c in kept]
    c = moment[0]
    assert c.start <= 297 and c.end - c.start >= 10                # starts before the play
    assert c.subscores["game"] >= 60 and 5 <= c.subscores.get("game_bonus", 0) <= 12
    assert "CHAT: hype" in c.subscores["game_why"]
    assert "VALORANT (shooter)" in seen["guidance"] and "VALORANT" in seen["windows_guidance"]
    assert seen["title"].startswith("GAME / CHAT")


def _gunfire(np, n=600, at=298):
    """What the tagger heard: a burst of gunfire at `at`, nothing else."""
    from analysis.game_audio import sound_signal

    k = gaming.knowledge()
    heard = {g: np.full(n, 0.01, dtype=np.float32) for g in k["sound_groups"]}
    heard["gunfire"][at:at + 5] = 0.6
    return sound_signal(heard, k["sound_groups"], "shooter", k["genre_sounds"])


def test_the_game_sound_and_the_streamer_agreeing_is_a_moment_without_chat(fusing):
    np, fusion, _seen = fusing
    segments, signals = _quiet_stream(np, shout_at=300)
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "VALORANT"}], "")
    kept, _ = fusion.find_clips("v.mp4", segments, _NoLLM(), CFG, signals=signals, gaming=profile,
                                sounds=_gunfire(np))
    c = next(c for c in kept if c.start <= 300 <= c.end)
    assert 5 <= c.subscores.get("game_bonus", 0) <= 12
    assert "GAME SOUND: gunfire" in c.subscores["game_why"] and "STREAMER" in c.subscores["game_why"]


def test_the_game_sound_alone_is_a_candidate_but_not_a_bonus(fusing):
    np, fusion, _seen = fusing
    segments, signals = _quiet_stream(np)                  # nothing loud, nothing said near it
    profile = gaming.profile_for({"clips": {"gaming_scoring": True}}, [{"name": "VALORANT"}], "")
    kept, _ = fusion.find_clips("v.mp4", segments, _NoLLM(), CFG, signals=signals, gaming=profile,
                                sounds=_gunfire(np))
    moment = [c for c in kept if c.start <= 298 <= c.end]
    assert moment and moment[0].start <= 295                # the fight, from just before it
    # The play itself adds a little; nothing agreeing with it, so no more.
    assert moment[0].subscores["game"] >= 40 and moment[0].subscores.get("game_bonus", 0) < 5


def test_standard_scoring_is_untouched_by_the_gaming_profile(fusing):
    np, fusion, seen = fusing
    segments, signals = _quiet_stream(np, shout_at=300)
    kept, _ = fusion.find_clips("v.mp4", segments, _NoLLM(), CFG, signals=signals)
    assert seen["guidance"] == "" and seen["title"] is None
    assert all("game" not in c.subscores for c in kept)


def test_little_talking_moves_weight_to_the_game_not_the_screen():
    pytest.importorskip("numpy")
    from analysis import fusion
    from core.models import ClipCandidate

    c = ClipCandidate(start=0, end=20, score=0)
    c.subscores = {"text": 0, "engagement": 0, "audio": 50, "visual": 0, "game": 100}
    w = gaming.STANDARD_WEIGHTS
    quiet = fusion._fuse(c, w, reaction=0.0, speech_ratio=0.0, game=True)
    talky = fusion._fuse(c, w, reaction=0.0, speech_ratio=1.0, game=True)
    assert quiet > talky                          # silence over a big play isn't penalised
    # With talking, a gaming stream's clip scores exactly as on any stream.
    c.subscores = {"text": 85, "engagement": 80, "audio": 50, "visual": 40, "game": 10}
    assert fusion._fuse(c, w, 0.5, 1.0, game=True) == fusion._fuse(c, w, 0.5, 1.0)
    c.subscores = {"text": 0, "engagement": 0, "audio": 50, "visual": 0, "game": 100}
    standard = {"text": 0.30, "visual": 0.20, "reaction": 0.20, "audio": 0.20, "engagement": 0.10}
    c.subscores["visual"] = 100
    assert fusion._fuse(c, standard, 0.0, 0.0) == pytest.approx(0.20 * 2.0 + 0.20 * 0.5)


def test_the_streamer_getting_loud_while_talking_is_a_voice_jump():
    np = pytest.importorskip("numpy")
    from analysis import fusion
    from core.models import Segment

    loud = np.full(100, 0.02, dtype=np.float32)       # a quiet stream between lines
    loud[10:30] = 0.1                                   # the streamer talking
    loud[20:23] = 0.4        # 4x their own voice: a shout
    loud[60:63] = 0.4        # loud with nobody talking (the game)
    segs = [Segment(start=10.0, end=30.0, text="oh my god", words=[{"start": 10.0, "end": 29.0, "word": "god"}])]
    jump, events = fusion._voice_jump({"loudness": loud}, segs)
    assert jump[21] == pytest.approx(1.0) and jump[61] == 0
    # Just talking, 5x the quiet stream around it, is not a shout.
    assert jump[12] == 0
    assert [t for t, _d in events] == [20.0] and "4.0x their usual voice" in events[0][1]


def test_the_prompts_carry_the_guidance_only_for_a_gaming_stream(monkeypatch):
    pytest.importorskip("numpy")
    from analysis import highlights
    from core.models import Segment

    prompts = []

    class Echo:
        name = "echo"

        def generate(self, prompt, **_k):
            prompts.append(prompt)
            return json.dumps({"clips": []})

    segs = [Segment(start=0.0, end=20.0, text="we got him")]
    highlights.find_highlights(segs, Echo(), min_duration=10, max_duration=60)
    highlights.find_highlights(segs, Echo(), min_duration=10, max_duration=60,
                               guidance="THIS IS A GAMING STREAM")
    highlights.score_windows(segs, Echo(), [(0.0, 20.0)], guidance="THIS IS A GAMING STREAM")
    assert all("{mode_guidance}" not in p for p in prompts)
    assert "THIS IS A GAMING STREAM" not in prompts[0]
    assert "weigh the events seriously.\n" in prompts[0]           # the standard prompt, unchanged
    assert "weigh the events seriously.\n\nTHIS IS A GAMING STREAM" in prompts[1]
    assert "THIS IS A GAMING STREAM" in prompts[2]


# ---- the option ------------------------------------------------------------------------------


def test_gaming_scoring_goes_with_vertical_live_but_not_podcast():
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from server.api import _process_options

    class Body:
        def __init__(self, **k):
            self.__dict__.update(k)

    assert _process_options(Body(vertical_live=True, gaming_scoring=True)) == {
        "vertical_live": True, "gaming_scoring": True}
    with pytest.raises(HTTPException):
        _process_options(Body(podcast=True, gaming_scoring=True))


def test_a_gaming_stream_scores_its_windows_in_batches_matched_by_time():
    from analysis import highlights
    from core.models import Segment

    calls = []

    class Model:
        def generate(self, prompt, *, json_mode=False):
            import re
            starts = [float(s) for s in re.findall(r"WINDOW \d+ \[([\d.]+)s", prompt)]
            calls.append(starts)
            # Answers out of order and skips one: matched by start, not position.
            answers = [{"start": s, "end": s + 20, "score": 70 + int(s) % 10, "engagement": 60,
                        "hook": f"at {s:.0f}", "reason": "r"} for s in reversed(starts[1:])]
            return json.dumps({"clips": answers})

    windows = [(float(s), float(s) + 20) for s in range(0, 200, 10)]       # 20 windows
    got = highlights.score_windows([Segment(start=0, end=1, text="x")], Model(), windows, batch=8)
    assert [len(c) for c in calls] == [8, 8, 4]
    assert len(got) == 20 and [c.start for c in got] == [s for s, _ in windows]
    for batch_start in (0, 80, 160):                                       # the skipped one: neutral
        c = next(c for c in got if c.start == batch_start)
        assert c.score == 50 and c.hook == "High-energy moment"
    c = next(c for c in got if c.start == 30)
    assert c.hook == "at 30" and c.score == 70
