"""Moving and resizing a layer on the layout editor's preview
(ui/src/renderer/src/lib/layerDrag.ts): it snaps to the Short's middle and to
other layers the way a design editor does, a facecam keeps its shape whichever
side it's pulled from, and Alt turns snapping off. Run under Node; skipped
without it."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "ui" / "src" / "renderer" / "src" / "lib"
TARGETS = {"xs": [0, 540, 1080], "ys": [0, 960, 1920]}
OPTS = {"aspect": None, "canvas": [1080, 1920], "minW": 130, "minH": 38, "targets": TARGETS, "tol": 22}


def _drag(tmp_path, calls, fn="dragLayer"):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node isn't available")
    (tmp_path / "layerDrag.ts").write_text((LIB / "layerDrag.ts").read_text(encoding="utf-8"), encoding="utf-8")
    script = (f"const m = await import({json.dumps((tmp_path / 'layerDrag.ts').as_uri())});"
              f"const calls = {json.dumps(calls)};"
              f"console.log(JSON.stringify(calls.map(c => m.{fn}(...c))));")
    r = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0 and "strip-types" in r.stderr:
        pytest.skip("this Node can't run TypeScript directly")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_layer_moved_near_the_middle_snaps_to_it_and_shows_the_line(tmp_path):
    (got,) = _drag(tmp_path, [[[100, 300, 400, 400], "move", 250, 0, OPTS]])     # its middle lands at 550
    assert got["box"][0] + got["box"][2] / 2 == 540 and got["guides"]["xs"] == [540]


def test_alt_places_it_freely(tmp_path):
    (got,) = _drag(tmp_path, [[[100, 300, 400, 400], "move", 250, 0, {**OPTS, "targets": None}]])
    assert got["box"][0] == 350 and got["guides"] == {"xs": [], "ys": []}


def test_it_lines_up_with_another_layer(tmp_path):
    opts = {**OPTS, "targets": {"xs": [0, 540, 1080, 700], "ys": [0, 960, 1920, 250]}}
    (got,) = _drag(tmp_path, [[[100, 300, 400, 400], "move", 0, -40, opts]])
    assert got["box"][1] == 250 and got["guides"]["ys"] == [250]


@pytest.mark.parametrize("handle,dx,dy", [("w", -100, 0), ("e", 100, 0), ("n", 0, -100), ("s", 0, 100),
                                          ("nw", -100, -100), ("se", 100, 100), ("ne", 100, -100), ("sw", -100, 100)])
def test_a_round_facecam_pulled_from_any_side_keeps_its_shape(tmp_path, handle, dx, dy):
    start = [300, 400, 400, 400]
    (got,) = _drag(tmp_path, [[start, handle, dx, dy, {**OPTS, "aspect": 1.0, "targets": None}]])
    x, y, w, h = got["box"]
    assert w == h == 500                                                   # grew, still round
    if "w" in handle:
        assert x + w == 700                                                # the right side stayed put
    if "n" in handle:
        assert y + h == 800
    if handle in ("e", "w"):
        assert y + h / 2 == 600                                            # grows about its middle
    if handle in ("n", "s"):
        assert x + w / 2 == 500


def test_a_small_facecam_keeps_its_16_by_10_shape(tmp_path):
    (got,) = _drag(tmp_path, [[[300, 400, 480, 300], "e", 160, 0, {**OPTS, "aspect": 1.6, "targets": None}]])
    assert got["box"][2:] == [640, 400]


def test_a_free_box_snaps_each_edge_it_moves(tmp_path):
    (got,) = _drag(tmp_path, [[[100, 300, 400, 100], "e", 30, 0, OPTS]])
    assert got["box"] == [100, 300, 440, 100] and got["guides"]["xs"] == [540]


def test_a_layer_never_leaves_the_short_or_shrinks_past_the_minimum(tmp_path):
    moved, shrunk = _drag(tmp_path, [[[100, 300, 400, 400], "move", 2000, 3000, {**OPTS, "targets": None}],
                                     [[100, 300, 400, 400], "se", -1000, -1000,
                                      {**OPTS, "aspect": 1.0, "targets": None}]])
    assert moved["box"] == [680, 1520, 400, 400]
    assert shrunk["box"][2] == 130


# ---- the game box on the video frame keeps clear of the webcam ------------------------

FRAME = [960, 540]
WALL = [600, 300, 360, 240]          # a webcam bottom right, grown a little past its border


def test_a_game_box_moved_into_the_webcam_stops_at_its_edge(tmp_path):
    start = [0, 0, 500, 540]
    (got,) = _drag(tmp_path, [[start, "move", [150, 0, 500, 540], [WALL], FRAME]], fn="keepOut")
    assert got["box"] == [100, 0, 500, 540] and got["guides"]["xs"] == [600]


def test_a_game_box_pulled_into_the_webcam_stops_at_its_edge(tmp_path):
    start = [0, 0, 500, 540]
    (edge, corner) = _drag(tmp_path, [[start, "e", [0, 0, 700, 540], [WALL], FRAME],
                                      [[0, 0, 500, 250], "se", [0, 0, 700, 400], [WALL], FRAME]], fn="keepOut")
    assert edge["box"] == [0, 0, 600, 540] and edge["guides"]["xs"] == [600]
    assert corner["box"] == [0, 0, 600, 400]            # stopped on the side that keeps the most of it


def test_a_game_box_already_over_the_webcam_is_not_held(tmp_path):
    (got,) = _drag(tmp_path, [[[0, 0, 960, 540], "move", [0, 0, 960, 540], [WALL], FRAME]], fn="keepOut")
    assert got["box"] == [0, 0, 960, 540] and got["guides"] == {"xs": [], "ys": []}


def test_a_game_box_that_misses_the_webcam_goes_where_it_was_put(tmp_path):
    (got,) = _drag(tmp_path, [[[0, 0, 400, 250], "move", [100, 20, 400, 250], [WALL], FRAME]], fn="keepOut")
    assert got["box"] == [100, 20, 400, 250]


def test_a_game_box_pulled_into_the_webcam_keeps_its_shape(tmp_path):
    """Zoomed to fill, the game box keeps its space's shape (here 1:1): cut
    short by the webcam on one side, the other follows."""
    wall, frame = [600, 0, 400, 300], [1000, 1000]         # a webcam top right
    (got,) = _drag(tmp_path, [[[0, 100, 400, 400], "e", [0, 0, 700, 700], [wall], frame, 1.0]], fn="keepOut")
    assert got["box"] == [0, 50, 600, 600]              # stopped at the webcam, still square, same middle


# ---- the game box takes the shape of its space on the Short ---------------------------


def test_a_game_box_takes_a_new_shape_with_its_middle_and_size_kept(tmp_path):
    box = [0.3, 0.2, 0.4, 0.3]                      # 0.12 of the frame, middle at (0.5, 0.35)
    (tall, back) = _drag(tmp_path, [[box, 0.5, [1, 1]], [[0.3, 0.2, 0.4, 0.3], 0.4 / 0.3, [1, 1]]], fn="reshape")
    x, y, w, h = tall
    assert abs(w / h - 0.5) < 1e-9 and abs(w * h - 0.12) < 1e-9
    assert abs(x + w / 2 - 0.5) < 1e-9 and abs(y + h / 2 - 0.35) < 1e-9
    assert back == box                              # already that shape: untouched


def test_a_reshaped_game_box_stays_inside_the_frame(tmp_path):
    (got,) = _drag(tmp_path, [[[0.0, 0.0, 0.9, 0.9], 0.3, [1, 1]]], fn="reshape")
    x, y, w, h = got
    assert x >= 0 and y >= 0 and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9 and abs(w / h - 0.3) < 1e-9
