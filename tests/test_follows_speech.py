"""Phase 5 of the format builder: the preview follows what is said. A label is on screen while its point is being made, every other
element is on screen exactly in the time it was given, the timeline is drawn from what the renderer will really do, and the page
shows every element as an outline when paused and the real picture while playing."""

from __future__ import annotations

import subprocess

import pytest
from PIL import Image
from test_formats_bench import library  # noqa: F401  (a fixture the next one uses)
from test_live_preview import clip_library, frame_at, needs_ffmpeg  # noqa: F401  (clip_library is a fixture)

from villomillo.formats import designs, elements, live, preview, render
from villomillo.web.pages import STATIC

SCRIPT = (STATIC / "formats-builder.js").read_text(encoding="utf-8")
CSS = (STATIC / "formats-builder.css").read_text(encoding="utf-8")
POINTS = [("Point one", 6.0), ("Point two", 11.0), ("Point three", 15.0)]
SAID = render.ClipText(["A TITLE", "IN TWO LINES"], POINTS, "The takeaway")


def words_every(gap: float, count: int, start: float = 100.0) -> list[dict]:
    return [{"t0": start + i * gap, "t1": start + i * gap + gap * 0.8, "text": f"w{i}" if i % 9 else f"Word{i}."} for i in range(count)]


# --- the points of a sample are found in what is said -------------------------------------------------------------------------------

def test_a_sample_gets_points_at_words_that_are_really_spoken():
    words = words_every(0.25, 140)                                          # 35 seconds of speech
    points = preview.sample_points(words, 100.0, 35.0, "A first point")
    assert len(points) == 3 and points[0][0] == "A first point"            # the first is the label's own example text
    times = [t for _, t in points]
    assert all(b - a >= 4.0 for a, b in zip(times, times[1:])) and times[0] >= 3.5 and times[-1] <= 28
    spoken = {round(w["t0"] - 100.0, 2) for w in words}
    assert all(round(t, 2) in spoken for t in times)                       # each point starts on a word that is spoken
    assert points[1][0].startswith(("W", "w")) and "w" in points[1][0].lower()   # and is named by the words that begin it


def test_a_sample_with_little_speech_gets_fewer_points_and_none_when_there_is_none():
    assert preview.sample_points([], 0.0, 35.0, "First") == []
    few = preview.sample_points(words_every(1.0, 8, 0.0), 0.0, 35.0, "First")
    assert 0 < len(few) < 3 and few[0][0] == "First"


def test_points_prefer_the_start_of_a_sentence_to_the_middle_of_one():
    words = words_every(0.25, 140)
    for w in words:
        w["text"] = "so"
    words[55]["text"] = "Done."      # a sentence ends here, so the next word starts one, at 14.0 s: close to the second point's aim (14.7 s)
    words[56]["text"] = "Next"
    points = preview.sample_points(words, 100.0, 35.0, "First")
    assert any(abs(t - 14.0) < 0.01 for _, t in points)   # the start of the sentence is chosen over a word in the middle of one that is nearer the aim


# --- when each element is on screen, from what the renderer will really do ---------------------------------------------------------

@pytest.fixture
def prepared_clip(clip_library):  # noqa: F811
    sv, profile = clip_library

    def make(design: dict, text=SAID):
        return preview.prepare(sv.db, profile, design, "talkyvideo1", clip=preview.RealClip("talkyvideo1", 40.0, 60.0, text, False))
    return make


def timed(kind: str, name: str, **timing) -> dict:
    e = elements.default_element(kind, name=name, id=f"{kind}-{name.lower()}")
    e["timing"] = {"mode": "whole", "seconds": 3.0, "from": 0.0, "to": None, **timing}
    return e


@needs_ffmpeg
def test_each_label_is_on_screen_from_its_point_until_just_before_the_next(prepared_clip):
    prep = prepared_clip({"elements": [elements.default_element("headline"), elements.default_element("label"), elements.default_element("takeaway")]})
    first, second, third = prep.windows["label"]
    assert first == pytest.approx((6.0, 10.7), abs=0.05) and second == pytest.approx((11.0, 14.7), abs=0.05)
    assert third[0] == pytest.approx(15.0, abs=0.05) and 17.0 < third[1] <= 17.4     # the last one gives way to the takeaway
    assert prep.windows["takeaway"][0][1] == pytest.approx(20.0, abs=0.05)


@needs_ffmpeg
def test_every_other_element_is_on_screen_exactly_in_the_time_it_was_given(prepared_clip):
    prep = prepared_clip({"elements": [elements.default_element("headline"), timed("shape", "A", mode="first", seconds=3.0),
                                       timed("shape", "B", mode="last", seconds=4.0), timed("shape", "C", mode="range", **{"from": 8.0, "to": 12.0}),
                                       timed("text", "D"), timed("progress_bar", "E", mode="range", **{"from": 2.0, "to": 5.0})]})
    got = prep.windows
    assert got["shape-a"] == [(0.0, 3.0)] and got["shape-b"] == [(16.0, 20.0)] and got["shape-c"] == [(8.0, 12.0)]
    assert got["text-d"] == [None] and got["progress_bar-e"] == [(2.0, 5.0)]   # None is the whole clip


def test_the_live_status_carries_the_windows_for_the_timeline(tmp_path):
    prep = preview.Prepared(None, preview.spans()[:2], ["k0", "k1"], 1.0, {"label": [(4.2, 14.4), (14.7, 25.0)], "captions": [None]})
    queue = live.LivePreview(tmp_path, lambda *a: None)
    status = queue.submit(prep)
    assert status["elements"] == {"label": [(4.2, 14.4), (14.7, 25.0)], "captions": [None]}


# --- the real picture: the label that is up is the point being spoken ---------------------------------------------------------------

def magenta(frame: Image.Image, y: int) -> int:
    band = frame.crop((0, max(y - 70, 0), 1080, y + 70)).convert("RGB")
    return sum(1 for r, g, b in band.getdata() if r > 200 and b > 200 and g < 70)


@needs_ffmpeg
def test_in_the_real_picture_the_label_up_at_each_moment_is_the_point_being_spoken(prepared_clip, tmp_path):
    label = elements.default_element("label")
    label["style"].update(box="#FF00FF", color="#000000")
    row = designs.CHIPS_Y["face_fill"]
    prep = prepared_clip({"elements": [elements.default_element("headline"), label, elements.default_element("takeaway")],
                          "effects": {"intensity": "off", "kinds": []}})
    pieces = {i: preview.make_segment(prep, i, tmp_path / "pieces") for i in (1, 2, 3)}
    # (moment in the clip, is a label up?) around the windows (6 to 10.7), (11 to 14.7), (15 to 17.3)
    expect = [(5.5, False), (7.0, True), (10.0, True), (10.95, False), (12.0, True), (14.0, True), (14.85, False), (16.0, True), (17.9, False)]
    for moment, up in expect:
        piece = pieces[int(moment // 5)]
        found = magenta(frame_at(piece, moment - 5 * int(moment // 5), tmp_path / f"f{moment}.png"), row)
        assert (found > 300) == up, f"at {moment} s a label should {'' if up else 'not '}be up (found {found} pixels)"


# --- the page: outlines when paused, the real picture while playing ---------------------------------------------------------------

def test_the_page_shows_every_element_as_an_outline_when_paused_and_the_real_picture_while_playing():
    assert "Ghost: every element" in SCRIPT and "Live: the real picture" in SCRIPT
    assert ".fmt-phone:not(.live) .st-item{outline:1px dashed" in CSS                      # ghost: each element outlined, so each can be picked
    assert ".fmt-phone.live #stage-items,.fmt-phone.live>#stage-still{visibility:hidden}" in CSS   # live: the outlines are gone
    assert 'vids.some(function (v) { return !v.hidden; })' in SCRIPT                      # the mode follows whether a video is on screen
    assert "function pause()" in SCRIPT and "v.hidden = true" in SCRIPT.split("function pause()")[1].split('$("pv-play")')[0]   # pause: back to the outlines
    assert 'if (!pv.scrub && !pv.playing) { v.hidden = true; }' in SCRIPT                  # but not while playing: the last picture holds


def test_the_timeline_bars_come_from_the_windows_the_server_reports():
    assert "pv.windows = s.elements" in SCRIPT and "pv.windows[e.id]" in SCRIPT
    assert 'real.forEach(function (w) { marks.push(w ? [w[0], w[1], ""] : [0, pv.length, ""]); })' in SCRIPT   # a label gets one bar per point
    subprocess.run(["node", "--check", str(STATIC / "formats-builder.js")], check=True, timeout=60)
