"""A match's story reels (sports/core/reels.py): which clips each reel holds,
in match order, and the join that makes them. Neutral names only."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("yaml")

import sports
from sports.core import reels


def _clip(t, label="Goal", event="goal", team="", player="", minute=None, replay=False, path="x.mp4"):
    scores = {"sport_event": event, "sport_label": label, "sport_t": float(t)}
    if team:
        scores["sport_team"] = team
    if player:
        scores["sport_player"] = player
    if minute is not None:
        scores["sport_minute"] = minute
    if replay:
        scores["sport_replay"] = True
    return SimpleNamespace(candidate=SimpleNamespace(start=float(t) - 14, subscores=scores), path=Path(path))


def test_the_recap_is_every_named_moment_in_match_order():
    clips = [_clip(3000, team="AWO"), _clip(1000, team="HOM"), _clip(2000, "Big moment", "big_moment"),
             _clip(2500, "Yellow card", "yellow_card"), _clip(1100, replay=True)]
    planned = reels.plan(clips, ["recap"], "HOM 1-1 AWO")
    assert [(r.kind, r.subject, r.title) for r in planned] == [("recap", "", "HOM 1-1 AWO: match recap")]
    assert [c.candidate.subscores["sport_t"] for c in planned[0].parts] == [1000.0, 2500.0, 3000.0]


def test_a_reel_per_team_and_per_player_with_two_moments_or_more():
    clips = [_clip(100, team="HOM", player="Player A"), _clip(200, team="HOM", player="Player A"),
             _clip(300, team="HOM", player="Player A"), _clip(400, team="AWO", player="Player B"),
             _clip(500, team="HOM", player="Player C"), _clip(600, team="HOM", player="Player C")]
    planned = reels.plan(clips, ["teams", "players"])
    assert [(r.kind, r.subject, r.title, len(r.parts)) for r in planned] == [
        ("team", "HOM", "HOM: the match", 5),
        ("player", "Player A", "Player A: the most moments", 3),
        ("player", "Player C", "Player C: the match", 2)]
    # One file each, whatever its length; two names that read alike stay apart.
    assert [reels.file_name(r)[:-9] for r in planned] == ["reel_team_hom", "reel_player_player-a", "reel_player_player-c"]
    assert reels.file_name(reels.Reel("player", "Player-A", "", [])) != reels.file_name(planned[1])
    # Nothing asked, nothing made; one moment isn't a reel.
    assert reels.plan(clips, []) == []
    assert reels.plan(clips[:1], ["recap"]) == []


def test_the_names_typed_and_how_the_commentary_says_them():
    assert reels.names("Player A, Player B & Player C and Player D / HOM vs AWO, player a") == [
        "Player A", "Player B", "Player C", "Player D", "HOM", "AWO"]
    assert reels.says("What a finish from Player Joel!", "Player Joël")  # accents aside
    assert reels.says("Surname again!", "First Surname")                 # the surname alone
    assert not reels.says("Kaneda runs", "Kane")                         # whole words only
    assert not reels.says("First runs", "First Surname")                 # a first name isn't enough


def test_a_player_typed_in_teams_or_players_gets_the_moments_the_commentary_names():
    clips = [_clip(100, team="HOM"), _clip(200, "Yellow card", "yellow_card"), _clip(300, team="AWO"),
             _clip(400, team="HOM", player="Player B"), _clip(500, "Big moment", "big_moment")]
    commentary = {100: "and Surname scores for HOM", 200: "a booking for surname",
                  300: "HOM concede", 400: "Player B, and Surname in the box", 500: "Surname again"}

    def said(c):
        return commentary[int(c.candidate.subscores["sport_t"])]

    commentary[300] = "Home United concede"
    planned = reels.plan(clips, ["players"], typed="First Surname, HOM, Home United, Player B", said=said)
    # Surname: named in three moments (a big moment isn't one), and more than
    # anyone else. HOM is a team the score box read, by its code or its name:
    # its team reel covers it. Player B: the match events' moment, and the
    # commentary's none besides.
    assert [(r.title, [c.candidate.subscores["sport_t"] for c in r.parts]) for r in planned] == [
        ("First Surname: the most moments", [100.0, 200.0, 400.0])]
    # Without a transcript, only the match events name players.
    assert reels.plan(clips, ["players"], typed="First Surname", said=None) == []


def test_the_description_lists_each_moment_where_it_starts():
    parts = [_clip(100, team="HOM", minute=18), _clip(200, "Yellow card", "yellow_card", player="Player B")]
    assert reels.chapters(parts, [24.0, 21.5]) == "0:00 Goal 18' HOM\n0:24 Yellow card Player B"


def test_the_option_takes_the_reels_it_knows():
    assert sports.clean({"name": "soccer", "reels": ["players", "recap"]})["reels"] == ["recap", "players"]
    assert sports.clean({"name": "soccer", "reels": "recap, teams"})["reels"] == ["recap", "teams"]
    assert "reels" not in sports.clean({"name": "soccer", "reels": []})
    with pytest.raises(ValueError, match="reels must be some of"):
        sports.clean({"name": "soccer", "reels": ["bloopers"]})


def _ffmpeg():
    try:
        from core.binaries import ffmpeg

        exe = ffmpeg()
    except Exception:
        exe = None
    if not exe or not (Path(exe).exists() or shutil.which(exe)):
        pytest.skip("ffmpeg isn't installed here")
    return exe


def test_clips_are_joined_without_their_end_cards(tmp_path):
    exe = _ffmpeg()
    parts = []
    for i in range(2):
        p = tmp_path / f"part{i}.mp4"
        subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=108x192:rate=30:duration=4",
                        "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(p)], check=True)
        parts.append(p)
    out = tmp_path / "reel.mp4"
    # The first has a 1 s card to leave off; the second has none, and keeps it all.
    lengths = reels.join(parts, out, [1.0, 0.0])
    assert out.exists() and abs(lengths[0] - 3.0) < 0.2 and abs(lengths[1] - 4.0) < 0.2
    assert abs(reels.duration(out) - 7.0) < 0.6


def test_a_reel_is_made_again_into_its_own_row(tmp_path, db, monkeypatch):
    """The reels are joined from every clip of the run, the re-rendered ones
    too. A reel is known by what it is, not by its length: a re-run makes it
    again into its own row and file, with one end card, and a reel as long
    as another never takes its row."""
    exe = _ffmpeg()
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from core import pipeline
    from core.models import ClipCandidate, Segment
    from video import outro

    monkeypatch.setattr(outro, "DURATION", 1.0)
    cards = []

    def finish(src, dst, _config):
        cards.append(Path(dst).name)
        shutil.copyfile(src, dst)
        Path(src).unlink()
        return True

    monkeypatch.setattr(outro, "finish", finish)
    db.upsert_video("vid", title="A match")
    clips = []
    for i, t in enumerate((1000, 2000)):
        p = tmp_path / f"clip{i}.mp4"
        # 3 s of window and a 1 s card: the file is 4 s long.
        subprocess.run([exe, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=108x192:rate=30:duration=4",
                        "-f", "lavfi", "-i", "sine=duration=4", "-c:v", "libx264", "-g", "30", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(p)], check=True)
        scores = {"sport_event": "goal", "sport_label": "Goal", "sport_t": float(t), "sport_team": "HOM"}
        clips.append(SimpleNamespace(candidate=ClipCandidate(start=t - 2.0, end=t + 1.0, score=80, hook="",
                                                             subscores=scores), path=p))
    recap = SimpleNamespace(option={"reels": ["recap"]}, report_data={"score": "HOM 2-0 AWO"})

    def rows():
        return {Path(r["path"]).relative_to(tmp_path).as_posix(): r for r in db.clips_for_video("vid")}

    made = pipeline._sport_reels(db, "vid", recap, clips, tmp_path, {})
    assert [m.path.name for m in made] == ["reel_recap.mp4"] and abs(made[0].candidate.end - 6.0) < 0.2
    assert cards == ["reel_recap.mp4"] and not list(tmp_path.glob("*.pre-card.mp4"))
    assert abs(reels.duration(made[0].path) - 6.0) < 0.6

    # The same clips again: made again into its row and file, one more card.
    db.set_clip(rows()["reel_recap.mp4"]["id"], title="My title")
    assert pipeline._sport_reels(db, "vid", recap, clips, tmp_path, {}) == []
    assert list(rows()) == ["reel_recap.mp4"] and rows()["reel_recap.mp4"]["title"] == "My title"
    assert cards == ["reel_recap.mp4"] * 2

    # Longform's 16:9 reel: a row of its own, its profile marking it Longform.
    wide = pipeline._sport_reels(db, "vid", recap, clips, tmp_path / "Longform", {},
                                 start=0.011, opts={"profile": "short_clips"})
    assert len(wide) == 1 and wide[0].candidate.start == 0.011
    assert json.loads(rows()["Longform/reel_recap.mp4"]["render_opts"]) == {"profile": "short_clips", "reel": "recap"}

    # A name typed in Teams or players, said in both clips' commentary (a
    # segment overlapping the window counts; one after it doesn't). Its reel
    # is as long as the recap, and gets a row of its own beside it.
    said = [Segment(start=995.0, end=999.5, text="Surname with the ball"),
            Segment(start=2000.5, end=2004.0, text="and SURNAME scores"),
            Segment(start=2002.0, end=2010.0, text="Player C, after the whistle")]
    named = SimpleNamespace(option={"reels": ["players"], "teams": "Surname, Player C"}, report_data={})
    made = pipeline._sport_reels(db, "vid", named, clips, tmp_path, {}, said)
    assert [m.candidate.hook for m in made] == ["Surname: the match"]
    player = rows()[made[0].path.name]
    assert json.loads(player["render_opts"]) == {"reel": "player", "of": "Surname"}
    assert len(player["description"].splitlines()) == 2    # both moments, as chapters
    assert rows()["reel_recap.mp4"]["title"] == "My title" and len(rows()) == 3
    assert player["end_s"] != rows()["reel_recap.mp4"]["end_s"]
