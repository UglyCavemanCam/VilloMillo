"""A match's events as the person has them (sports/core/events_import.py):
read from what people paste, placed on the video by the score box's clock or
the list's own kick-offs, and merged with what was found. Neutral names only."""

import pytest

pytest.importorskip("yaml")

import sports
from sports.core import clips, detect
from sports.core import events_import as ei
from sports.soccer import scoreboard as sb

SPEC = sports.spec("soccer")


def _soccer(highlights="best", events=""):
    option = {"name": "soccer", "highlights": highlights}
    if events:
        option["events"] = events
    return sports.profile_for({"clips": {"sport": option}})


def test_a_club_apps_list_is_read_as_typed():
    text = """Full game using our new camera
09:22 Kick off
18:16 Goal! Player A
20:15 Goal! Player B
55:21 Half Time
1:00:40 Second Half
1:14.29: Goal!! Player B
1:51:29 Full Time. 11-0 Final Score"""
    read, unread = ei.parse(text, SPEC)
    assert [(e.kind, e.video_t, e.who) for e in read] == [
        ("kickoff", 562.0, ""), ("goal", 1096.0, "Player A"), ("goal", 1215.0, "Player B"),
        ("halftime", 3321.0, ""), ("kickoff_second", 3640.0, ""), ("goal", 4469.0, "Player B"),
        ("fulltime", 6689.0, "11 0 Final Score")]
    assert unread == ["Full game using our new camera"]
    assert ei.kickoffs(read) == {1: 562.0, 2: 3640.0}


def test_a_match_reports_minutes_are_read_too():
    read, unread = ei.parse("18' own goal Team A\n45+2' yellow card HOM\n67 Goal Player C\n"
                            "90'+3 red card\nminute,type,team\nno time here", SPEC)
    assert [(e.kind, e.minute, e.added, e.who) for e in read] == [
        ("own_goal", 18, 0, "Team A"), ("yellow_card", 45, 2, "HOM"), ("goal", 67, 0, "Player C"),
        ("red_card", 90, 3, "")]
    assert unread == ["no time here"]


def test_a_minute_is_placed_by_the_clock_or_the_kickoffs():
    # The clock runs 95 s behind the video in the first half, and the second
    # half's 45:00 is at 3100 s.
    readings = [sb.Reading(t=float(t), clock=t - 95, minute=(t - 95) // 60, visible=True)
                for t in range(100, 2900, 20)]
    readings += [sb.Reading(t=float(t), clock=2700 + t - 3100, minute=(2700 + t - 3100) // 60, visible=True)
                 for t in range(3100, 5000, 20)]
    board = sb.from_readings(readings)
    goal18 = ei.parse("18' goal", SPEC)[0][0]
    assert ei.place(goal18, board, {}) == (1115.0, 1174.0)           # 17:00-17:59 on the clock
    goal59 = ei.parse("59' goal", SPEC)[0][0]
    assert ei.place(goal59, board, {}) == (3880.0, 3939.0)           # 58:00 is 13 min into the second half
    # No clock: the list's kick-offs place it; neither, and it can't be.
    assert ei.place(goal18, None, {1: 562.0}) == (562.0 + 1020, 562.0 + 1079)
    assert ei.place(goal59, None, {1: 562.0}) is None
    assert ei.place(goal18, None, {}) is None


def test_listed_goals_become_certain_moments_where_nothing_was_found():
    text = "09:22 Kick off\n18:16 Goal! Player A\n1:00:40 Second Half\n1:06:51 Goal Player B"
    profile = _soccer("goals", text)
    found = detect.moments(profile, [], curves={"crowd": [0.0] * 7000}, board=None, video_end=7000.0,
                           min_len=10, max_len=60)
    goals = [e for e in found if e.type == "goal"]
    assert [(e.t, e.confidence, e.player, e.minute, e.period) for e in goals] == [
        (1096.0, 1.0, "Player A", 9, "first_half"), (4011.0, 1.0, "Player B", 52, "second_half")]
    # Nothing else marks it: from just before the tag to well after, since a
    # club app's goal tag can sit at the start of the attack.
    assert (goals[0].start, goals[0].end) == (1091.0, 1136.0)
    assert "from your match events" in goals[0].why()
    assert profile.listed_report == {"placed": 2, "unplaced": [], "unread": []}


def test_a_listed_goal_names_the_goal_the_score_box_found():
    # The score box found the goal and the crowd dated it to the second; the
    # list's "18'" names it without moving it.
    readings = [sb.Reading(t=float(t), score=(0, 0) if t < 1180 else (1, 0), teams=("HOM", "AWO"),
                           clock=t - 95, minute=(t - 95) // 60, visible=True) for t in range(100, 1400, 10)]
    board = sb.from_readings(readings, box=(0.1, 0.05, 0.3, 0.1))
    crowd = [0.0] * 3000
    for s in range(1150, 1156):
        crowd[s] = 0.9
    profile = _soccer("goals", "18' goal Player A")
    profile.board = board
    found = detect.moments(profile, [], curves={"crowd": crowd, "crowd_heard": crowd}, board=board,
                           video_end=3000.0, min_len=10, max_len=60)
    goals = [e for e in found if e.type == "goal"]
    assert len(goals) == 1
    assert goals[0].t == 1148.5 and goals[0].team == "HOM" and goals[0].player == "Player A"


def test_what_couldnt_be_read_or_placed_is_reported():
    profile = _soccer("goals", "18' goal\n1:00 Goal\nwhat a game")
    detect.moments(profile, [], curves={"crowd": [0.0] * 600}, board=None, video_end=600.0,
                   min_len=10, max_len=60)
    notes = clips.report(profile, [], [], {}, [])["notes"]
    assert any(n.startswith('Couldn\'t place "18\' goal"') for n in notes)
    assert any(n.startswith('Couldn\'t read "what a game"') for n in notes)
    assert "1 moment(s) from your match events" in notes


def test_the_option_keeps_the_list_and_refuses_one_with_nothing_readable():
    assert sports.clean({"name": "soccer", "events": "18:16 Goal"})["events"] == "18:16 Goal"
    with pytest.raises(ValueError, match="no line had both a time and a kind of moment"):
        sports.clean({"name": "soccer", "events": "what a match"})
    with pytest.raises(ValueError, match="at most"):
        sports.clean({"name": "soccer", "events": "18:16 Goal\n" * 500})
