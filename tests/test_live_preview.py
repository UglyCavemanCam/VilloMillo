"""Phase 3 of the format builder: the live preview. The sample is made in short pieces by the real renderer, nearest the playhead
first; a piece is made again only when something that shows in it changed; a newer edit replaces an older one; the page plays the
pieces one after the other, always for the design as it is now."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_formats_bench import library, needs_ffmpeg, write_transcript  # noqa: F401  (library is a fixture)
from test_research import make_sv

from villomillo.formats import designs, elements, live, preview, render
from villomillo.web import app as web_app
from villomillo.web.pages import STATIC

SCRIPT = (STATIC / "formats-builder.js").read_text(encoding="utf-8")


# --- the queue: which piece is made first, what a newer edit replaces, what is reused ----------------------------------------

def prepared(prefix: str, count: int = 7):
    return preview.Prepared(None, preview.spans()[:count], [f"{prefix}{i}" for i in range(count)])


class Maker:
    """A stand-in for the renderer: writes a file for the piece, and can be told to hold the pieces whose key starts with `hold`."""

    def __init__(self, hold: str = ""):
        self.calls, self.hold, self.release = [], hold, threading.Event()
        self.fail: set[str] = set()

    def __call__(self, prep, index, folder, cancel):
        key = prep.keys[index]
        self.calls.append(key)
        if key.startswith(self.hold) and self.hold:
            while not (cancel.is_set() or self.release.is_set()):
                time.sleep(0.005)
            if cancel.is_set():
                raise render.RenderCancelled("replaced")
        if key in self.fail:
            raise preview.PreviewError("this piece cannot be made")
        folder.mkdir(parents=True, exist_ok=True)
        preview.segment_path(folder, key).write_bytes(b"piece")


def until(check, seconds: float = 5.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.01)
    return False


def test_the_piece_under_the_playhead_is_made_first_then_its_neighbours_then_the_rest():
    assert live.order(7, 3) == [3, 4, 2, 5, 1, 6, 0]
    assert live.order(7, 0) == [0, 1, 2, 3, 4, 5, 6] and live.order(7, 6) == [6, 5, 4, 3, 2, 1, 0]


def test_dragging_the_timeline_asks_for_the_right_piece_first(tmp_path):
    maker = Maker()
    queue = live.LivePreview(tmp_path, maker)
    status = queue.submit(prepared("t"), playhead_seconds=31.2)   # the last piece is 30 to 35
    assert until(lambda: queue.status(status["round"])["done"])
    assert maker.calls == ["t6", "t5", "t4", "t3", "t2", "t1", "t0"]
    maker2 = Maker()
    queue2 = live.LivePreview(tmp_path / "other", maker2)
    queue2.submit(prepared("u"), playhead_seconds=12.0)
    assert until(lambda: queue2._current is not None and all(s == "ready" for s in queue2._current.state))
    assert maker2.calls[:3] == ["u2", "u3", "u1"]


def test_a_piece_that_is_already_made_is_reused_not_made_again(tmp_path):
    maker = Maker()
    queue = live.LivePreview(tmp_path, maker)
    first = queue.submit(prepared("same"))
    assert until(lambda: queue.status(first["round"])["done"]) and len(maker.calls) == 7
    again = queue.submit(prepared("same"), playhead_seconds=20)
    assert again["done"] and all(s["state"] == "ready" and s["url"] for s in again["segments"])   # ready at once, from the files
    time.sleep(0.1)
    assert len(maker.calls) == 7


def test_only_the_pieces_whose_fingerprint_changed_are_made_again(tmp_path):
    maker = Maker()
    queue = live.LivePreview(tmp_path, maker)
    keys = [f"k{i}" for i in range(7)]
    first = queue.submit(preview.Prepared(None, preview.spans(), keys))
    assert until(lambda: queue.status(first["round"])["done"])
    changed = list(keys)
    changed[5], changed[6] = "k5b", "k6b"
    second = queue.submit(preview.Prepared(None, preview.spans(), changed), playhead_seconds=32)
    assert until(lambda: queue.status(second["round"])["done"])
    assert maker.calls[7:] == ["k6b", "k5b"]


def test_two_quick_edits_only_finish_the_last_one(tmp_path):
    maker = Maker(hold="a")   # the first edit's pieces are slow
    queue = live.LivePreview(tmp_path, maker)
    one = queue.submit(prepared("a"))
    assert until(lambda: maker.calls == ["a0"])   # the first piece of the first edit is being made
    two = queue.submit(prepared("b"))
    assert until(lambda: queue.status(two["round"])["done"])
    assert maker.calls.count("a0") == 1 and not any(c.startswith("a") and c != "a0" for c in maker.calls)   # the rest of the first edit was never started
    assert sorted(c for c in maker.calls if c.startswith("b")) == [f"b{i}" for i in range(7)]
    assert queue.status(one["round"])["superseded"] is True
    assert not preview.segment_path(tmp_path, "a0").exists()


def test_a_piece_that_the_newer_edit_still_wants_next_is_not_stopped(tmp_path):
    maker = Maker(hold="w")
    queue = live.LivePreview(tmp_path, maker)
    queue.submit(prepared("w"))
    assert until(lambda: maker.calls == ["w0"])
    queue.submit(prepared("w"), playhead_seconds=1.0)    # the same design, the playhead still in the first piece
    time.sleep(0.15)
    assert maker.calls == ["w0"]                          # it carried on, it was not started over
    maker.release.set()
    assert until(lambda: queue._current is not None and all(s == "ready" for s in queue._current.state))
    assert maker.calls.count("w0") == 1


def test_a_piece_that_cannot_be_made_is_reported_and_the_others_carry_on(tmp_path):
    maker = Maker()
    maker.fail = {"f3"}
    queue = live.LivePreview(tmp_path, maker)
    status = queue.submit(prepared("f"))
    assert until(lambda: queue.status(status["round"])["done"])
    final = queue.status(status["round"])
    assert [s["state"] for s in final["segments"]] == ["ready"] * 3 + ["failed"] + ["ready"] * 3
    assert final["segments"][3]["error"] == "this piece cannot be made"


# --- which pieces an edit changes ---------------------------------------------------------------------------------------------

def keys_for(design: dict, prints: list, *, scale: float = 1.0, text=None) -> list[str]:
    job = SimpleNamespace(start=100.0, end=135.0, text=text or render.ClipText(["A"], [("p", 4.0)], "t"), handle="@me")
    saved = render.overlay_prints
    render.overlay_prints = lambda j: (prints, "", {})
    try:
        return preview.segment_keys(job, designs.normalize(design), "vid", scale)
    finally:
        render.overlay_prints = saved


def differing(a: list[str], b: list[str]) -> list[int]:
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


def test_a_piece_is_made_again_only_when_a_picture_that_shows_in_it_changed():
    design = {}
    button = ("button-a", "(W-w)/2", "100", (32.0, 35.0))
    label = ("label-a", "(W-w)/2", "900", (4.0, 32.3))
    base = keys_for(design, [label, button])
    assert len(base) == 7 and len(set(base)) == 7
    assert differing(base, keys_for(design, [label, button])) == []                                       # nothing changed: nothing is made again
    assert differing(base, keys_for(design, [label, ("button-b", *button[1:])])) == [6]                    # the button's colour: the last piece only
    assert differing(base, keys_for(design, [label, button, ("box", "0", "0", (10.0, 20.0))])) == [2, 3]    # a box from 10 to 20 s: the pieces it is in
    assert differing(base, keys_for(design, [label, button, ("always", "0", "0", None)])) == list(range(7))   # a picture on the whole clip: every piece


def test_a_picture_that_ends_sooner_changes_only_the_pieces_where_it_ended():
    design = {}
    button = ("button-a", "0", "0", (32.0, 35.0))
    long_label = ("label-a", "0", "0", (4.0, 32.3))
    short_label = ("label-a", "0", "0", (4.0, 29.0))    # a button now starts, so the label ends sooner
    assert differing(keys_for(design, [long_label]), keys_for(design, [short_label])) == [5, 6]


def test_a_format_wide_setting_changes_every_piece_and_so_does_the_size():
    base = keys_for({}, [])
    assert differing(base, keys_for({"layout": "fit_blur"}, [])) == list(range(7))
    assert differing(base, keys_for({"music": {"mode": "always"}}, [])) == list(range(7))
    assert differing(base, keys_for({}, [], scale=0.5)) == list(range(7))
    assert differing(base, keys_for({}, [], text=render.ClipText(["B"], [("p", 4.0)], "t"))) == list(range(7))


def test_the_example_text_of_the_designs_own_elements_is_what_the_preview_says():
    design = designs.normalize({})
    for e in design["elements"]:
        if e["type"] == "headline":
            e["text"]["default"] = "MY OWN TITLE\nSECOND LINE\nTHIRD"
        if e["type"] == "takeaway":
            e["text"]["default"] = "Remember this"
    said = preview.sample_text(design)
    assert said.title_lines == ["MY OWN TITLE", "SECOND LINE"] and said.takeaway == "Remember this"
    assert preview.sample_text(designs.normalize({})).title_lines == preview.SAMPLE_TEXT.title_lines   # the default is the sample's own text


def test_the_sample_is_cut_into_pieces_of_the_set_length():
    got = preview.spans()
    assert got[0] == (0.0, preview.SEGMENT_SECONDS) and got[-1][1] == preview.SAMPLE_LENGTH
    assert all(b - a <= preview.SEGMENT_SECONDS + 1e-6 for a, b in got) and [a for a, _ in got[1:]] == [b for _, b in got[:-1]]


# --- the pieces, made by the real renderer --------------------------------------------------------------------------------------

@pytest.fixture
def clip_library(tmp_path, monkeypatch, library):  # noqa: F811
    downloads, transcripts = library
    write_transcript(transcripts, "talkyvideo1", [31 + 0.4 * i for i in range(90)])
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=75",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=75", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(downloads / "talkyvideo1.mp4")], check=True, timeout=180)
    sv = make_sv(tmp_path, monkeypatch)
    return sv, next(iter(sv.profiles.values()))


def frame_at(video, seconds: float, out) -> Image.Image:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{seconds}", "-i", str(video), "-frames:v", "1", str(out)], check=True, timeout=60)
    return Image.open(out).convert("RGB")


def length_of(video) -> float:
    done = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=60)
    return float(done.stdout.strip())


@needs_ffmpeg
@pytest.mark.skipif(shutil.which("ffprobe") is None, reason="ffprobe is not installed")
def test_a_piece_renders_only_the_asked_stretch_and_keeps_the_whole_clips_timing(clip_library, tmp_path):
    sv, profile = clip_library
    headline = elements.default_element("headline")
    box = elements.default_element("shape", name="Magenta", timing={"mode": "range", "seconds": 3.0, "from": 12.0, "to": 14.0})
    box["style"].update(color="#FF00FF", radius=0, outline_w=0)
    box["w"] = box["h"] = 400
    design = {"elements": [headline, box], "effects": {"intensity": "off", "kinds": []}}
    prep = preview.prepare(sv.db, profile, design, "talkyvideo1")
    assert len(prep.keys) == 7 and prep.job.end - prep.job.start == preview.SAMPLE_LENGTH
    out = tmp_path / "pieces"
    piece = preview.make_segment(prep, 2, out)                   # the piece from 10 s to 15 s
    assert piece.exists() and piece.name == f"format-seg-{prep.keys[2]}.mp4" and not list(out.glob("*.part.mp4"))
    assert length_of(piece) == pytest.approx(5.0, abs=0.25)      # only that stretch, not the whole sample
    inside = frame_at(piece, 3.0, tmp_path / "in.png").getpixel((540, 960))   # 13 s into the sample: the box is on
    outside = frame_at(piece, 1.0, tmp_path / "out.png").getpixel((540, 960))  # 11 s: not yet
    assert inside[0] > 200 and inside[2] > 200 and inside[1] < 80
    assert not (outside[0] > 200 and outside[2] > 200 and outside[1] < 80)
    again = preview.make_segment(prep, 2, out)                   # an identical piece is found, not made again
    assert again == piece


@needs_ffmpeg
def test_a_piece_in_a_bad_design_is_refused_in_plain_words_before_anything_is_made(clip_library):
    sv, profile = clip_library
    with pytest.raises(designs.DesignError, match="Pick a logo"):
        preview.prepare(sv.db, profile, {"elements": [elements.default_element("headline"), elements.default_element("logo")]}, "talkyvideo1")
    with pytest.raises(preview.PreviewError, match="not available|not downloaded"):
        preview.prepare(sv.db, profile, {}, "nosuchvideo")


# --- the page and its endpoints -------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    sv = make_sv(tmp_path, monkeypatch)
    previews = tmp_path / "previews"
    monkeypatch.setattr(web_app, "PREVIEWS", previews)
    return TestClient(web_app.create_app(sv), base_url="http://127.0.0.1:8780"), previews, monkeypatch


def test_the_start_endpoint_makes_the_pieces_and_the_status_endpoint_says_which_are_ready(client):
    http, previews, monkeypatch = client
    maker = Maker()
    monkeypatch.setattr(preview, "prepare", lambda db, profile, design, video_id, draft=False, clip=None: prepared("abcdef00000"))
    monkeypatch.setattr(preview, "make_segment", maker)
    started = http.post("/api/formats/preview/start", json={"design": {}, "video_id": "x", "playhead": 12.0}).json()
    assert started["ok"] and started["length"] == preview.SAMPLE_LENGTH and len(started["segments"]) == 7
    assert until(lambda: http.get(f"/api/formats/preview/status?round={started['round']}").json()["done"])
    done = http.get(f"/api/formats/preview/status?round={started['round']}").json()
    assert all(s["state"] == "ready" for s in done["segments"]) and maker.calls[0] == "abcdef000002"      # the playhead's piece first
    served = http.get(done["segments"][0]["url"])
    assert served.status_code == 200 and served.headers["content-type"] == "video/mp4" and served.content == b"piece"


def test_a_newer_round_replaces_the_older_one_and_the_older_says_so(client):
    http, previews, monkeypatch = client
    monkeypatch.setattr(preview, "prepare", lambda db, profile, design, video_id, draft=False, clip=None: prepared(design.get("tag", "p")))
    monkeypatch.setattr(preview, "make_segment", Maker())
    one = http.post("/api/formats/preview/start", json={"design": {"tag": "p"}, "video_id": "x"}).json()
    two = http.post("/api/formats/preview/start", json={"design": {"tag": "q"}, "video_id": "x"}).json()
    assert two["round"] > one["round"]
    old = http.get(f"/api/formats/preview/status?round={one['round']}").json()
    assert old["superseded"] is True and old["current"] == two["round"]
    assert http.get("/api/formats/preview/status?round=9999").json()["superseded"] is True


def test_the_start_endpoint_refuses_in_plain_words(client):
    http, _, _ = client
    r = http.post("/api/formats/preview/start", json={"design": {}, "video_id": "nosuchvideo"})
    assert r.status_code == 422 and r.json()["error"]
    r = http.post("/api/formats/preview/start", json={"design": {"elements": [{"type": "nonsense"}]}, "video_id": "nosuchvideo"})
    assert r.status_code == 422 and "not a kind the app knows" in r.json()["error"]
    r = http.post("/api/formats/preview/start", json={"design": {}, "video_id": "x", "playhead": "soon"})
    assert r.status_code == 422


def test_only_finished_pieces_can_be_fetched(client):
    http, previews, _ = client
    previews.mkdir(parents=True)
    (previews / "format-seg-0123456789ab.mp4").write_bytes(b"ok")
    (previews / "format-seg-0123456789ab.part.mp4").write_bytes(b"half")
    assert http.get("/formats/preview/format-seg-0123456789ab.mp4").status_code == 200
    for name in ("format-seg-0123456789ab.part.mp4", "format-seg-../../x.mp4", "format-seg-zzzzzzzzzzzz.mp4", "format-seg-ffffffffffff.mp4"):
        assert http.get(f"/formats/preview/{name}").status_code == 404


# --- what the page does ------------------------------------------------------------------------------------------------------------

def test_the_old_check_with_real_render_box_is_gone_and_the_player_and_timeline_are_there(client):
    http, _, _ = client
    page = http.get("/formats").text
    for gone in ("v_btn", "v_player", "Check with real render"):
        assert gone not in page and gone not in SCRIPT
    for present in ('id="pv-play"', 'id="pv-seek"', 'id="pv-a"', 'id="pv-b"', 'id="pv-badge"', 'id="v_video"', 'id="pv-bars"', 'id="pv-draft"', 'Updating'):
        assert present in page, present


def test_the_script_asks_for_a_new_round_by_itself_after_every_change_and_plays_two_videos_in_turn():
    assert "/api/formats/preview/start" in SCRIPT and "/api/formats/preview/status" in SCRIPT
    assert "function onEdit() { renderStage(); checkPlace(false); renderBars(); previewDirty(); }" in SCRIPT      # every edit asks again, a moment later
    assert "setTimeout(startRound, force ? 0 : 450)" in SCRIPT                                              # after you stop typing or dragging
    assert 'vids = [$("pv-a"), $("pv-b")]' in SCRIPT and "pv.live = 1 - pv.live" in SCRIPT                  # two players, swapped at each seam
    assert "Waiting for this part to finish being made" in SCRIPT                  # pressing play on a piece being made says so
    assert "pv.fresh = pv.fresh.map" in SCRIPT                                                              # old pictures are not played as current


def test_the_sample_dropdown_lists_the_best_sample_first(client):
    http, _, _ = client
    data = json.loads(http.get("/formats").text.split('<script id="boot" type="application/json">')[1].split("</script>")[0])
    assert isinstance(data["videos"], list)   # best-first order is tested with the samples; the page opens on the first of them
