"""Finding a match's moments (sports/core/detect.py, sports/core/clips.py) and
reading its scoreboard (sports/soccer/scoreboard.py), on synthetic signals and
on the lines the OCR really read off a 2018 World Cup final's score bug. No
model runs here."""

import pytest

pytest.importorskip("yaml")

import sports
from core.models import ClipCandidate, Segment
from sports.core import clips, detect
from sports.soccer import scoreboard as sb


@pytest.fixture
def soccer():
    return sports.profile_for({"clips": {"sport": {"name": "soccer", "highlights": "best"}}})


def _profile(highlights="best", period="full"):
    return sports.profile_for({"clips": {"sport": {"name": "soccer", "highlights": highlights,
                                                   "period": period}}})


# ---- the scoreboard --------------------------------------------------------------


@pytest.mark.parametrize("line, score, teams, minute", [
    ("(> HOm  0 : 0AW0  16:47", (0, 0), ("HOM", "AWO"), 16),
    ("(HOM1:1AW037:37", (1, 1), ("HOM", "AWO"), 37),
    ("( HOM3 : 1AW0  60:07", (3, 1), ("HOM", "AWO"), 60),
    ("(HOM4·2|AW069:17", (4, 2), ("HOM", "AWO"), 69),
    ("(>HOM 4: 2 AW0  90:00 +2:", (4, 2), ("HOM", "AWO"), 90),
    ("HOM 0.0 AWO 8:27", (0, 0), ("HOM", "AWO"), 8),
    (">HOM | 1:O AW0  20:07", (1, 0), ("HOM", "AWO"), 20),     # a 0 read as the letter O
    ("HOM O-2 AWO 50:01", (0, 2), ("HOM", "AWO"), 50),
    ("1 - 0", (1, 0), None, None),
    ("23:27", None, None, 23),
])
def test_the_bug_is_read_as_the_ocr_really_reads_it(line, score, teams, minute):
    r = sb.parse([line])
    assert (r.score, r.teams, r.minute) == (score, teams, minute)


def _readings(scores):
    return [sb.Reading(t=float(t), score=s, teams=("HOM", "AWO") if s else None, visible=s is not None)
            for t, s in scores]


def test_a_goal_needs_two_readings_to_agree():
    board = sb.from_readings(_readings([(0, (0, 0)), (10, (0, 0)), (20, (1, 0)), (30, (1, 0))]))
    assert [(c.after, c.team, c.hi) for c in board.changes] == [((1, 0), "HOM", 20.0)]
    assert board.changes[0].lo <= 10 and board.changes[0].label() == "score 1-0 (HOM)"


def test_a_one_off_misread_is_not_a_goal():
    board = sb.from_readings(_readings([(0, (0, 0)), (10, (0, 0)), (20, (8, 0)), (30, (0, 0)),
                                        (40, (0, 0))]))
    assert board.changes == []


def test_two_goals_at_once_and_a_goal_taken_back_are_not_called():
    jump = sb.from_readings(_readings([(0, (0, 0)), (10, (0, 0)), (20, (2, 0)), (30, (2, 0))]))
    back = sb.from_readings(_readings([(0, (1, 0)), (10, (1, 0)), (20, (0, 0)), (30, (0, 0)),
                                       (40, (1, 0)), (50, (1, 0))]))
    assert jump.changes == []
    assert [c.after for c in back.changes] == [(1, 0)]


def test_the_halves_come_from_the_clock():
    readings = [sb.Reading(t=t, minute=m, visible=True) for t, m in
                [(100, 5), (1000, 20), (2800, 45), (3100, 46), (4000, 60)]]
    board = sb.from_readings(readings)
    assert board.halftime == 3100
    assert board.period_at(1000) == "first_half" and board.period_at(4000) == "second_half"
    assert sb.from_readings([]).period_at(1000) == ""


def test_the_match_minute_is_counted_the_way_a_match_report_counts_it():
    board = sb.from_readings([sb.parse(["HOM 0-0 AWO 17:23"])])
    board.readings[0].t = 1100.0
    assert board.minute_at(1100) == 18             # 17:23 on the clock is the 18th minute
    assert board.minute_at(1150) == 19             # 18:13, from the same reading
    assert board.minute_at(1400) is None           # too far from any reading to say


def test_a_misread_clock_digit_doesnt_move_the_minute():
    # The clock is 95 s behind the video; two readings misread a digit by 5 s.
    readings = [sb.Reading(t=float(t), clock=t - 95, minute=(t - 95) // 60, visible=True)
                for t in range(4000, 4100, 5)]
    readings[9].clock -= 5
    readings[10].clock += 5
    board = sb.from_readings(readings)
    assert board.minute_at(4145.5) == 68           # 67:30.5 on the clock
    assert board.minute_at(4155) == 68             # 67:40
    assert board.minute_at(4175) == 69             # 68:00


def test_the_bug_hidden_is_a_replay_sign():
    board = sb.from_readings(_readings([(0, (1, 0)), (10, None), (20, None), (30, (1, 0))]))
    assert board.hidden(5, 25) and not board.hidden(25, 35)


def test_the_box_is_found_where_a_score_shows():
    np = pytest.importorskip("numpy")  # CI installs only the light dependencies
    pytest.importorskip("cv2")
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    def ocr(img):
        return [([[40, 10], [80, 10], [80, 30], [40, 30]], "HOM 1 0 AWO", 0.9)]

    box = sb.find_box(lambda t: frame, 5400, ocr)
    assert box is not None and box[1] < 0.2


def test_once_the_teams_are_known_a_soft_reading_still_gives_the_score():
    # Soft text (an upscaled or filmed screen) loses the separator and reads
    # 0 as a letter; the flag in front of a code reads as a letter too.
    readings = [sb.parse([text]) for text in
                ["HOM 0:0 AWO 16:00", "DHOM 0:0 AWO 16:05", "HOM 0-0 AWO 16:10",
                 "HOMOOAW016:15", "DHOM1OAW017:02", "HOM1·OAW017:10", "HOM 1 AWO"]]
    for i, r in enumerate(readings):
        r.t = float(i * 10)
    board = sb.from_readings(readings)
    assert [r.score for r in board.readings] == [(0, 0), (0, 0), (0, 0), (0, 0), (1, 0), (1, 0), None]
    assert {r.teams for r in board.readings if r.teams} == {("HOM", "AWO")}
    assert [(c.after, c.team) for c in board.changes] == [((1, 0), "HOM")]


def test_the_box_is_the_score_and_its_clock_not_the_banners_beside_it():
    # A portrait frame's band holds a tall strip of stadium: the score run
    # and the clock a little apart from it make the box; an ad board on the
    # same row, and a banner on another, don't.
    lines = [((0.12, 0.28, 0.24, 0.39), "HOM"), ((0.29, 0.27, 0.43, 0.41), "0:0"),
             ((0.49, 0.28, 0.60, 0.39), "AWO"), ((0.76, 0.28, 0.90, 0.40), "15:07"),
             ((0.10, 0.70, 0.60, 0.80), "WORLD CUP FINAL 2026")]
    run = sb._score_lines(lines, aspect=0.39)
    assert [t for _b, t in run] == ["HOM", "0:0", "AWO", "15:07"]
    assert sb._score_lines(lines[4:], aspect=0.39) == []


def test_no_score_bug_means_no_board():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    board = sb.read(lambda t: frame, 600, find_ocr=lambda img: [], rec=lambda img: "")
    assert board.box is None and board.changes == []


# ---- finding the moments -----------------------------------------------------------


def _curve(n, spans):
    c = [0.0] * n
    for lo, hi, v in spans:
        for i in range(lo, hi + 1):
            c[i] = v
    return c


def _moments(profile, segments, crowd, voice=None, whistle=None, board=None, **kw):
    n = len(crowd)
    return detect.moments(profile, segments, curves={"crowd": crowd, "whistle": whistle or [0.0] * n},
                          voice=voice or [0.0] * n, board=board, video_end=float(n),
                          min_len=10, max_len=60, **kw)


def test_crowd_voice_and_the_commentary_make_a_goal(soccer):
    segs = [Segment(1000, 1004, "and it's in! GOOOAL! what a goal")]
    found = _moments(soccer, segs, _curve(3000, [(1002, 1010, 0.9)]), voice=_curve(3000, [(1001, 1003, 0.8)]))
    assert [(e.type, e.t) for e in found] == [("goal", 1000.0)]
    assert found[0].confidence == 1.0 and "crowd roar" in found[0].why()
    assert found[0].start == 986 and found[0].end == 1010


def test_commentary_alone_is_not_a_moment(soccer):
    segs = [Segment(500, 505, "remember that goal he scored last season?")]
    assert _moments(soccer, segs, [0.0] * 2000) == []


def test_a_roar_alone_is_a_big_moment_dated_back_for_the_lag(soccer):
    found = _moments(soccer, [], _curve(2000, [(600, 606, 0.8)]))
    assert [(e.type, e.t) for e in found] == [("big_moment", 600.0)]
    assert found[0].start == 600 - 1.5 - 10


def test_a_short_roar_is_a_near_miss_not_a_moment(soccer):
    assert _moments(soccer, [], _curve(2000, [(600, 600, 0.9)])) == []


def test_with_the_score_read_only_the_score_makes_a_goal(soccer):
    # "They've scored four!", said over a roar with the score read and
    # unchanged: talk about a goal, not one.
    segs = [Segment(2000, 2004, "they have scored four in a final")]
    readings = _readings([(t, (1, 0)) for t in range(1900, 2300, 20)])
    found = _moments(soccer, segs, _curve(3000, [(2001, 2004, 0.8)]), board=sb.from_readings(readings))
    assert [e.type for e in found] == ["big_moment"]
    # Without the score read there, the commentary and the crowd decide.
    unread = sb.from_readings(_readings([(t, (1, 0)) for t in range(100, 400, 20)]))
    found = _moments(soccer, segs, _curve(3000, [(2001, 2004, 0.8)]), board=unread)
    assert [e.type for e in found] == ["goal"]


def test_the_goal_again_soon_after_is_its_replay(soccer):
    segs = [Segment(1000, 1004, "GOOOAL! what a goal"),
            Segment(1040, 1046, "let's see it again, what a goal that is")]
    crowd = _curve(3000, [(1002, 1010, 0.9), (1042, 1046, 0.7)])
    found = _moments(soccer, segs, crowd, voice=_curve(3000, [(1001, 1003, 0.8)]))
    assert [e.is_replay for e in found] == [False, True]
    assert found[0].group == found[1].group


def test_the_scoreboard_confirms_the_goal_and_names_the_side(soccer):
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1030, (0, 1)), (1040, (0, 1))]))
    board.readings[2].teams = ("HOM", "AWO")
    found = _moments(soccer, [], _curve(3000, [(1002, 1010, 0.9)]), board=board)
    goal = next(e for e in found if e.type == "goal")
    assert goal.team == "AWO" and goal.confidence == 1.0 and "score 0-1 (AWO)" in goal.why()


def test_a_goal_only_the_scoreboard_saw_is_still_a_goal(soccer):
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1030, (1, 0)), (1040, (1, 0))]))
    found = _moments(soccer, [], [0.0] * 3000, board=board)
    assert [(e.type, e.t) for e in found] == [("goal", 1000.0)]


def test_the_crowd_dates_a_goal_the_scoreboard_confirms(soccer):
    # The roar lasts two seconds, too short for a moment of its own, and the
    # new score shows 40 s later: the goal is where the roar starts, less
    # the crowd's lag, and the clip holds the build-up and the celebration.
    board = sb.from_readings(_readings([(0, (0, 0)), (1000, (0, 0)), (1100, (1, 0)), (1110, (1, 0))]))
    found = _moments(soccer, [], _curve(3000, [(1060, 1061, 0.8)]), board=board)
    assert [(e.type, e.t) for e in found] == [("goal", 1058.5)]
    assert (found[0].start, found[0].end) == (1044.5, 1068.5)


def test_the_goal_is_the_loudest_roar_not_an_earlier_chance(soccer):
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1100, (1, 0)), (1110, (1, 0))]))
    # A chance's "ooh" is short; the goal's roar is louder and goes on.
    crowd = _curve(3000, [(950, 953, 0.6), (1060, 1070, 0.9)])
    found = _moments(soccer, [], crowd, board=board)
    assert [(e.type, e.t) for e in found] == [("big_moment", 950.0), ("goal", 1058.5)]


def test_a_goal_is_dated_after_the_score_before_it(soccer):
    # Two goals two minutes apart: the second's search stops at the first's
    # new score, so the first goal's louder roar can't be taken for it.
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1010, (1, 0)), (1020, (1, 0)),
                                        (1130, (2, 0)), (1140, (2, 0))]))
    crowd = _curve(3000, [(1000, 1010, 1.0), (1100, 1104, 0.5)])
    goals = [e for e in _moments(soccer, [], crowd, board=board) if e.type == "goal"]
    assert [e.t for e in goals] == [998.5, 1098.5]


def test_a_roar_soon_after_a_goal_is_its_celebration(soccer):
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1030, (1, 0)), (1040, (1, 0))]))
    crowd = _curve(3000, [(1000, 1008, 0.9), (1025, 1030, 0.8)])
    found = _moments(soccer, [], crowd, board=board)
    assert [(e.type, e.is_replay) for e in found] == [("goal", False), ("big_moment", True)]
    assert found[0].group == found[1].group


def test_goals_and_celebrations_keep_more_after(soccer):
    segs = [Segment(1000, 1004, "GOOOAL!")]
    found = _moments(soccer, segs, _curve(3000, [(1002, 1010, 0.9)]),
                     extra_after=12, extra_types={"goal"})
    assert found[0].end == 1000 + 10 + 12


# ---- from moments to clips -----------------------------------------------------------


def _cand(start, end, score):
    return ClipCandidate(start=start, end=end, score=score, subscores={})


def test_one_clip_per_moment_and_the_bonus_by_worth(soccer):
    segs = [Segment(1000, 1004, "GOOOAL! what a goal"), Segment(1040, 1046, "let's see it again")]
    found = _moments(soccer, segs, _curve(3000, [(1002, 1010, 0.9), (1042, 1046, 0.7)]),
                     voice=_curve(3000, [(1001, 1003, 0.8)]))
    goal_clip, replay_clip, other = _cand(990, 1015, 70), _cand(1035, 1060, 75), _cand(2000, 2030, 60)
    attached = clips.attach(found, [goal_clip, replay_clip, other])
    assert clips.bonus(attached[id(goal_clip)]) == 20 and clips.bonus(attached[id(replay_clip)]) == 0
    kept, dropped, _notes = clips.choose(soccer, [goal_clip, replay_clip, other], attached,
                                         min_score=55, max_len=60)
    assert goal_clip in kept and other in kept
    assert (replay_clip, "same_moment") in dropped


def test_all_goals_keeps_every_confirmed_goal_with_its_build_up():
    profile = _profile("goals")
    segs = [Segment(1000, 1004, "GOOOAL!"), Segment(2000, 2004, "what a save!")]
    found = _moments(profile, segs, _curve(3000, [(1002, 1010, 0.9), (2002, 2008, 0.8)]),
                     voice=_curve(3000, [(1001, 1003, 0.8), (2001, 2003, 0.8)]))
    late_goal, save = _cand(999, 1020, 40), _cand(1995, 2015, 90)
    attached = clips.attach(found, [late_goal, save])
    kept, dropped, _ = clips.choose(profile, [late_goal, save], attached, min_score=55, max_len=60)
    assert kept == [late_goal] and (save, "not_in_highlights") in dropped
    assert late_goal.score == 55 and late_goal.subscores["required"] == "Goal"
    assert (late_goal.start, late_goal.end) == (986, 1010)


def test_best_moments_keeps_a_goal_the_scoreboard_confirmed(soccer):
    # The words alone rated it 10; the score changed for it, so it's kept.
    board = sb.from_readings(_readings([(0, (0, 0)), (900, (0, 0)), (1030, (1, 0)), (1040, (1, 0))]))
    soccer.board = board
    found = _moments(soccer, [], _curve(3000, [(1002, 1010, 0.9)]), board=board)
    quiet_goal, other = _cand(986, 1010, 10), _cand(2000, 2030, 40)
    attached = clips.attach(found, [quiet_goal, other])
    kept, _dropped, _ = clips.choose(soccer, [quiet_goal, other], attached, min_score=55, max_len=60)
    assert quiet_goal in kept and other in kept
    assert quiet_goal.score == 55 and quiet_goal.subscores["required"] == "Goal"
    assert other.score == 40                      # a clip with no confirmed goal is left to its score


def test_the_footage_is_a_broadcast_when_a_score_box_is_on_screen(soccer):
    board = sb.from_readings(_readings([(0, (0, 0)), (10, (0, 0))]), box=(0.1, 0.05, 0.3, 0.1))
    assert soccer.resolve_footage(board) == "broadcast"
    assert soccer.resolve_footage(None) == "sideline"
    assert soccer.resolve_footage(sb.Scoreboard()) == "sideline"
    chosen = _profile("best")
    chosen.option["footage"] = "broadcast"
    assert chosen.resolve_footage(None) == "broadcast"


def test_club_footage_that_confirms_no_goal_gets_its_best_moments():
    # All goals, on footage with no score box and no commentary: rather than
    # nothing, the best moments, and a note that says why.
    profile = _profile("goals")
    profile.resolve_footage(None)
    found = _moments(profile, [], _curve(3000, [(1000, 1004, 0.9)]))
    moment, other = _cand(990, 1015, 70), _cand(2000, 2030, 60)
    attached = clips.attach(found, [moment, other])
    kept, dropped, notes = clips.choose(profile, [moment, other], attached, min_score=55, max_len=60)
    assert kept == [moment, other] and dropped == []
    assert notes and "best moments instead" in notes[0]
    assert clips.report(profile, found, kept, attached, notes)["footage"] == "sideline"
    # On a broadcast, a choice nothing matched still keeps nothing else.
    broadcast = _profile("goals")
    broadcast.resolve_footage(sb.from_readings([], box=(0.1, 0.05, 0.3, 0.1)))
    kept, _dropped, _notes = clips.choose(broadcast, [moment, other], attached, min_score=55, max_len=60)
    assert kept == []


def test_a_half_filter_keeps_what_it_cannot_place():
    profile = _profile("best", "second_half")
    profile.board = sb.from_readings([sb.Reading(t=t, minute=m, visible=True)
                                      for t, m in [(100, 5), (1000, 20), (2800, 45), (3100, 46), (4000, 60)]])
    first, second = _cand(1000, 1030, 70), _cand(4000, 4030, 70)
    kept, dropped, _ = clips.choose(profile, [first, second], {}, min_score=55, max_len=60)
    assert kept == [second] and (first, "other_period") in dropped
    blind = _profile("best", "second_half")
    kept, _, notes = clips.choose(blind, [first], {}, min_score=55, max_len=60)
    assert kept == [first] and notes


def test_the_report_counts_what_the_match_gave(soccer):
    segs = [Segment(1000, 1004, "GOOOAL!")]
    found = _moments(soccer, segs, _curve(3000, [(1002, 1010, 0.9), (2002, 2008, 0.8)]),
                     voice=_curve(3000, [(1001, 1003, 0.8)]))
    rep = clips.report(soccer, found, [], {}, [])
    assert rep["found"] == {"Goal": 1} and rep["big_moments"] == 1 and rep["sport"] == "Soccer"


# ---- framing a match for 9:16 ---------------------------------------------------------


def _frames(n, per=None):
    """n samples at 5 per second; per[i] = dict of what sample i saw."""
    per = per or {}
    return [{"t": i / 5, "cut": False, "balls": [], "people": [], **per.get(i, {})} for i in range(n)]


def _plan(samples, crop=0.316):
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from sports.soccer.ball import plan

    return plan(samples, crop)


def test_the_crop_follows_the_ball():
    samples = _frames(40, {i: {"balls": [(0.3 + 0.01 * i, 0.5, 0.6)]} for i in range(40)})
    path, led = _plan(samples)
    assert path[0][1] == pytest.approx(0.3, abs=0.01)
    assert path[-1][1] > 0.6 and led["ball"] == 40


def test_a_ball_that_couldnt_get_there_is_ignored():
    samples = _frames(10, {i: {"balls": [(0.3, 0.5, 0.6)]} for i in range(5)})
    samples[5]["balls"] = [(0.95, 0.5, 0.3)]        # a boot, a head, a crowd ball
    path, _ = _plan(samples)
    assert all(x < 0.5 for _t, x in path)


def test_a_weak_detection_follows_a_ball_but_never_starts_one():
    # A steward's vest at 0.2 on its own isn't the ball...
    lone = _frames(10, {i: {"balls": [(0.8, 0.5, 0.2)]} for i in range(10)})
    _path, led = _plan(lone)
    assert led["ball"] == 0
    # ...but a blurred 0.2 right where the ball just was is.
    followed = _frames(10, {0: {"balls": [(0.4, 0.5, 0.7)]}})
    for i in range(1, 10):
        followed[i]["balls"] = [(0.4 + 0.01 * i, 0.5, 0.2)]
    _path, led = _plan(followed)
    assert led["ball"] == 10


def test_without_the_ball_the_crop_frames_the_play_near_where_it_was():
    samples = _frames(30, {0: {"balls": [(0.7, 0.5, 0.6)]}})
    for s in samples[1:]:
        s["people"] = [(0.72, 0.5, 0.03, 0.1), (0.68, 0.5, 0.03, 0.1), (0.1, 0.5, 0.03, 0.1)]
    path, led = _plan(samples)
    assert path[-1][1] == pytest.approx(0.7, abs=0.05) and led["players"] > 0


def test_a_close_up_frames_the_player():
    samples = _frames(20, {i: {"balls": [(0.2, 0.5, 0.6)],
                                 "people": [(0.75, 0.5, 0.2, 0.8)]} for i in range(20)})
    path, led = _plan(samples)
    assert path[-1][1] > 0.6 and led["close-up"] == 20


def test_a_cut_snaps_instead_of_panning():
    samples = _frames(20, {i: {"balls": [(0.2, 0.5, 0.6)]} for i in range(10)})
    for i in range(10, 20):
        samples[i]["balls"] = [(0.8, 0.5, 0.6)]
    samples[10]["cut"] = True
    path, _ = _plan(samples)
    assert path[9][1] < 0.3 and path[10][1] > 0.7


def test_the_crop_stays_inside_the_frame():
    samples = _frames(10, {i: {"balls": [(0.01, 0.5, 0.6)]} for i in range(10)})
    path, _ = _plan(samples, crop=0.316)
    assert all(x >= 0.158 for _t, x in path)
