"""Phase 6 of the format builder: right-click menus, dragging, resizing and the keyboard. The edits are plain functions in formats-actions.js, so
each menu action, drag, resize and nudge runs here in node against the real element defaults, and the results are saved and rendered by the app."""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient
from test_formats_bench import library  # noqa: F401  (a fixture the next one uses)
from test_live_preview import clip_library, needs_ffmpeg  # noqa: F401  (clip_library is a fixture)
from test_research import make_sv

from villomillo.formats import designs, elements, preview, render
from villomillo.web import app as web_app
from villomillo.web.pages import STATIC

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
ACTIONS = STATIC / "formats-actions.js"
SCRIPT = (STATIC / "formats-builder.js").read_text(encoding="utf-8")
GEO = {"positions": designs.POSITIONS, "headline_top": designs.HEADLINE_TOP, "logo_y": designs.LOGO_Y, "chips_y": designs.CHIPS_Y}
DEFAULTS = {k: elements.default_element(k) for k in elements.TYPES}


def js(body: str, **given):
    """Run some JavaScript against formats-actions.js. `A`, `defaults`, `geo` and the keyword arguments are in scope; `out` is returned."""
    code = ("const A = require(process.argv[1]); const given = JSON.parse(require('fs').readFileSync(0, 'utf8')); "
            "const {defaults, geo} = given; let out; " + body + "; console.log(JSON.stringify(out));")
    done = subprocess.run(["node", "-e", code, str(ACTIONS)], input=json.dumps({"defaults": DEFAULTS, "geo": GEO, **given}), capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def make_list(*kinds) -> list[dict]:
    out = []
    for i, kind in enumerate(kinds):
        e = json.loads(json.dumps(DEFAULTS[kind]))
        e["id"] = f"{kind}-{i}"
        out.append(e)
    return out


# --- every menu action does what it says ----------------------------------------------------------------------------------------

def test_the_menu_for_an_element_offers_every_action_and_says_which_are_possible():
    items = js("out = A.menuFor(given.list, 'label-1')", list=make_list("headline", "label", "takeaway"))
    assert [i["label"] for i in items] == ["Edit", "Duplicate", "Hide", "Lock", "Bring forward", "Send back", "Reset position", "Delete"]
    enabled = {i["action"]: i["enabled"] for i in items}
    assert enabled["forward"] and enabled["back"] and enabled["delete"] and not enabled["reset"]    # nothing to reset: it is at its own spot
    ends = js("out = [A.menuFor(given.list, 'headline-0'), A.menuFor(given.list, 'takeaway-2')]", list=make_list("headline", "label", "takeaway"))
    assert not {i["action"]: i["enabled"] for i in ends[0]}["back"] and not {i["action"]: i["enabled"] for i in ends[1]}["forward"]   # the back and the front
    locked = make_list("headline", "label")
    locked[1].update(locked=True, x=300, y=900)
    got = {i["action"]: i for i in js("out = A.menuFor(given.list, 'label-1')", list=locked)}
    assert got["lock"]["label"] == "Unlock" and not got["delete"]["enabled"] and not got["reset"]["enabled"] and got["duplicate"]["enabled"] and got["edit"]["enabled"]


def test_duplicate_puts_a_copy_just_in_front_a_little_apart_and_selects_it():
    run = js("const r = A.run(given.list, 'label-1', 'duplicate', {layout: 'face_fill', geo}); out = {r, list: given.list}", list=make_list("headline", "label", "takeaway"))
    ids = [e["id"] for e in run["list"]]
    assert ids == ["headline-0", "label-1", "label", "takeaway-2"] and run["r"]["selected"] == "label" and run["r"]["changed"]
    copy = run["list"][2]
    assert copy["name"] == "Point label copy" and copy["locked"] is False
    assert (copy["x"], copy["y"]) == (540 + 40, designs.CHIPS_Y["face_fill"] + 60)       # a little down and across from the layout's own spot


def test_hide_and_show_lock_and_unlock_toggle_the_element():
    out = js("""const l = given.list; const seen = [];
                for (const a of ['hide', 'lock', 'hide', 'lock']) { A.run(l, 'label-1', a, {layout: 'face_fill', geo}); seen.push([l[1].visible, l[1].locked]); } out = seen""",
             list=make_list("headline", "label"))
    assert out == [[False, False], [False, True], [True, True], [True, False]]


def test_bring_forward_and_send_back_move_one_place_and_stop_at_the_ends():
    out = js("""const l = given.list; const order = () => l.map(e => e.type); const seen = [];
                A.run(l, 'headline-0', 'forward', {}); seen.push(order());
                A.run(l, 'headline-0', 'forward', {}); seen.push(order());
                const last = A.run(l, 'headline-0', 'forward', {}); seen.push(order(), last.changed);
                A.run(l, 'headline-0', 'back', {}); seen.push(order());
                const first = A.run(l, 'label-1', 'back', {}); A.run(l, 'label-1', 'back', {}); const stuck = A.run(l, 'label-1', 'back', {}); seen.push(order(), stuck.changed); out = seen""",
             list=make_list("headline", "label", "takeaway"))
    assert out[:3] == [["label", "headline", "takeaway"], ["label", "takeaway", "headline"], ["label", "takeaway", "headline"]] and out[3] is False
    assert out[4] == ["label", "headline", "takeaway"] and out[5] == ["label", "headline", "takeaway"] and out[6] is False


def test_reset_position_puts_the_element_back_at_the_layouts_own_spot_unless_it_is_locked():
    moved = make_list("headline", "label")
    moved[1].update(x=100, y=200)
    out = js("const l = given.list; const a = A.run(l, 'label-1', 'reset', {}); out = [a.changed, l[1].x, l[1].y]", list=moved)
    assert out == [True, None, None]
    moved[1].update(x=100, y=200, locked=True)
    out = js("const l = given.list; const a = A.run(l, 'label-1', 'reset', {}); out = [a.changed, l[1].x, l[1].y]", list=moved)
    assert out == [False, 100, 200]


def test_delete_removes_the_element_and_selects_its_neighbour_but_never_a_locked_one():
    out = js("const l = given.list; const r = A.run(l, 'label-1', 'delete', {}); out = [r, l.map(e => e.id)]", list=make_list("headline", "label", "takeaway"))
    assert out[0]["changed"] and out[0]["selected"] == "takeaway-2" and out[1] == ["headline-0", "takeaway-2"]
    last = js("const l = given.list; const r = A.run(l, 'takeaway-2', 'delete', {}); out = [r.selected, l.map(e => e.id)]", list=make_list("headline", "label", "takeaway"))
    assert last == ["label-1", ["headline-0", "label-1"]]
    locked = make_list("headline", "label")
    locked[1]["locked"] = True
    kept = js("const l = given.list; const r = A.run(l, 'label-1', 'delete', {}); out = [r.changed, l.length]", list=locked)
    assert kept == [False, 2]
    only = js("const l = given.list; const r = A.run(l, 'headline-0', 'delete', {}); out = [r.selected, l.length]", list=make_list("headline"))
    assert only == [None, 0]


def test_edit_opens_the_settings_and_changes_nothing():
    out = js("const l = given.list; const before = JSON.stringify(l); const r = A.run(l, 'label-1', 'edit', {}); out = [r, JSON.stringify(l) === before]", list=make_list("headline", "label"))
    assert out[0] == {"changed": False, "selected": "label-1", "edit": True} and out[1] is True


def test_right_click_on_empty_space_offers_every_kind_to_add_and_adds_it_where_clicked():
    types = [{"id": k, "label": v} for k, v in elements.TYPES.items()]
    menu = js("out = A.addMenu(given.list, given.types)", list=make_list("headline", "captions"), types=types)
    assert [m["kind"] for m in menu] == list(elements.TYPES) and not next(m for m in menu if m["kind"] == "captions")["enabled"]
    assert all(m["enabled"] for m in menu if m["kind"] != "captions")
    placed = js("""const l = given.list; const made = ['label', 'shape', 'headline', 'progress_bar', 'text'].map(k => A.addElement(l, defaults, k, {label: k, at: {x: 300, y: 700}}));
                   out = made.map(e => [e.type, e.id, e.x, e.y])""", list=make_list("headline"))
    assert placed == [["label", "label", 300, 700], ["shape", "shape", 300, 700], ["headline", "headline", None, 700], ["progress_bar", "progress_bar", None, 700], ["text", "text", 300, 700]]


def test_adding_says_in_plain_words_why_it_cannot():
    err = js("""let msg = []; for (const [k, o] of [['captions', {}], ['logo', {}]]) { try { A.addElement(given.list, defaults, k, o); msg.push('added'); } catch (e) { msg.push(e.message); } } out = msg""",
             list=make_list("captions"))
    assert err == ["A format has one set of captions. Change the ones it has.", "Upload your logo in the Assets tab first, then add it here."]
    assert js("out = A.addElement(given.list, defaults, 'logo', {logoId: 7}).options.asset_id", list=[]) == 7


# --- dragging, resizing and the keyboard --------------------------------------------------------------------------------------------

def test_dragging_moves_each_kind_of_element_by_the_distance_dragged_and_keeps_it_on_the_picture():
    out = js("""const run = (kind, g, dx, dy) => { const e = JSON.parse(JSON.stringify(defaults[kind])); A.dragTo(e, g, dx, dy); return [e.x, e.y]; };
                const at = {x: 540, y: 1000};
                out = {label: run('label', {at, w: 400, h: 100}, 120, -300), text: run('text', {at, w: 400, h: 100}, -9999, 9999),
                       headline: run('headline', {at: {x: 0, y: 430}, w: 900, h: 200}, 500, 100), logo: run('logo', {at: {x: 60, y: 190}, w: 150, h: 80}, 100, 50),
                       bar: run('progress_bar', {at: {x: 0, y: 1906}, w: 1080, h: 14}, 0, 500), captions: run('captions', {at: {x: 540, y: 1345}, w: 900, h: 200}, 0, -45)}""")
    assert out["label"] == [660, 700]                                     # the distance dragged, in picture units
    assert out["text"] == [200, 1870]                                     # dragged off the picture: kept where all of it still shows
    assert out["headline"] == [None, 530] and out["logo"] == [160, 240]   # a headline moves by its top edge, a logo by its corner
    assert out["bar"] == [None, 1906] and out["captions"] == [None, 1300]


def test_a_dragged_element_snaps_to_the_guide_lines_when_it_is_close():
    out = js("""const e = JSON.parse(JSON.stringify(defaults.label)); const seen = [];
                A.dragTo(e, {at: {x: 530, y: 940}, w: 300, h: 80}, 5, 12, (c, lines, axis) => { const hit = lines.find(v => Math.abs(c - v) <= 14); seen.push([axis, hit]); return hit === undefined ? c : hit; });
                out = [e.x, e.y, seen]""")
    assert out[0] == 540 and out[1] == 960 and out[2] == [["x", 540], ["y", 960]]


def test_resizing_sets_the_same_number_the_settings_show_for_each_kind_of_element():
    out = js("""const R = (kind, p, start, tweak) => { const e = JSON.parse(JSON.stringify(defaults[kind])); if (tweak) tweak(e); A.resizeTo(e, p, start); return e; };
                const h = R('headline', {x: 540 + 400, y: 0}, {at: {x: 0, y: 430}});
                const l = R('logo', {x: 60 + 216, y: 0}, {at: {x: 60, y: 190}});
                const s = R('shape', {x: 540 + 300, y: 960 + 100}, {at: {x: 540, y: 960}});
                const lab = R('label', {x: 540 + 300, y: 0}, {at: {x: 540, y: 1175}, size: 40, halfW: 200});
                const tk = R('takeaway', {x: 540 + 100, y: 0}, {at: {x: 540, y: 1175}, size: 46, halfW: 200});
                const hd = R('handle', {x: 540 + 90, y: 0}, {at: {x: 540, y: 960}, size: 26, halfW: 90});
                const bar = R('progress_bar', {x: 0, y: 1912 + 30}, {at: {x: 0, y: 1912}});
                const tiny = R('label', {x: 541, y: 0}, {at: {x: 540, y: 1175}, size: 40, halfW: 200});
                const cap = R('captions', {x: 900, y: 900}, {at: {x: 540, y: 1000}});
                out = {headline: h.style.width_pct, logo: l.style.size, shape: [s.w, s.h], label: lab.style.size, takeaway: tk.style.size, handle: hd.style.size, bar: bar.style.height, tiny: tiny.style.size, cap: cap.style.size, resizable: ['headline','logo','shape','label','takeaway','button','text','handle','progress_bar','captions'].map(k => A.resizable({type: k}))}""")
    assert out["headline"] == 74 and out["logo"] == 20 and out["shape"] == [600, 200]
    assert out["label"] == 60 and out["takeaway"] == 23 and out["handle"] == 26 and out["bar"] == 30
    assert out["tiny"] == 16 and out["cap"] is None                      # never smaller than the least the settings allow; captions have no grip
    assert out["resizable"] == [True] * 9 + [False]


def test_the_keyboard_deletes_and_nudges_by_one_or_by_ten_with_shift_and_not_a_locked_element():
    assert js("out = ['Delete', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'a'].map(k => A.keyEdit(k, false))") == [
        {"kind": "delete"}, {"kind": "nudge", "dx": -1, "dy": 0}, {"kind": "nudge", "dx": 1, "dy": 0}, {"kind": "nudge", "dx": 0, "dy": -1}, {"kind": "nudge", "dx": 0, "dy": 1}, None]
    assert js("out = [A.keyEdit('ArrowDown', true), A.keyEdit('ArrowLeft', true)]") == [{"kind": "nudge", "dx": 0, "dy": 10}, {"kind": "nudge", "dx": -10, "dy": 0}]
    out = js("""const e = JSON.parse(JSON.stringify(defaults.label)); const seen = [];
                A.nudge(e, 1, 0, 'face_fill', geo, {w: 300, h: 80}); seen.push([e.x, e.y]);
                A.nudge(e, 0, 10, 'face_fill', geo, {w: 300, h: 80}); seen.push([e.x, e.y]);
                e.locked = true; seen.push(A.nudge(e, 5, 5, 'face_fill', geo, {w: 300, h: 80}), [e.x, e.y]);
                const h = JSON.parse(JSON.stringify(defaults.headline)); A.nudge(h, 7, 3, 'face_fill', geo, {w: 900, h: 200}); seen.push([h.x, h.y]); out = seen""")
    assert out == [[541, 1175], [541, 1185], False, [541, 1185], [None, 433]]    # the first nudge starts from the layout's own spot; a headline has no across


def test_an_element_put_outside_the_safe_zone_is_told_so_in_plain_words_and_the_layouts_own_spots_never_are():
    box = {"x0": 100, "y0": 700, "x1": 400, "y1": 800}
    out = js("""const moved = JSON.parse(JSON.stringify(defaults.label)); moved.name = 'Point label'; moved.x = 250; moved.y = 750; const own = JSON.parse(JSON.stringify(defaults.label));
                const bar = JSON.parse(JSON.stringify(defaults.progress_bar)); bar.y = 1906;
                const edge = (b) => A.safeNote(moved, Object.assign({}, given.box, b));
                out = {inside: edge({}), left: edge({x0: 10}), right: edge({x1: 1070}), top: edge({y0: 20}), bottom: edge({y1: 1700}), own: A.safeNote(own, {x0: 0, y0: 0, x1: 1080, y1: 1920}), bar: A.safeNote(bar, {x0: 0, y0: 1900, x1: 1080, y1: 1920}), zone: A.SAFE}""", box=box)
    assert out["inside"] == "" and out["own"] == "" and out["bar"] == ""
    assert out["left"] == out["right"] == out["top"] == out["bottom"] == \
        "The point label sits outside the safe zone (the dashed lines): the app's own title and buttons can cover part of it."
    assert out["zone"] == {"left": 60, "right": 1020, "top": 150, "bottom": 1560}


# --- what is saved and what is rendered -------------------------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    return TestClient(web_app.create_app(make_sv(tmp_path, monkeypatch)), base_url="http://127.0.0.1:8780")


def test_the_numbers_from_a_drag_and_a_resize_are_the_numbers_that_are_saved(client):
    els = make_list("headline", "label", "shape", "handle")
    els[2].update(x=540, y=960, w=300, h=100)
    moved = js("""const l = given.list;
                  A.dragTo(l[1], {at: A.posOf(l[1], 'face_fill', geo), w: 400, h: 100}, -60, -250);
                  A.resizeTo(l[2], {x: 540 + 250, y: 960 + 70}, {at: {x: 540, y: 960}});
                  A.resizeTo(l[1], {x: 480 + 300, y: 0}, {at: {x: l[1].x, y: l[1].y}, size: 40, halfW: 200});
                  A.nudge(l[3], 3, -2, 'face_fill', geo, {w: 200, h: 40}); out = l""", list=els)
    label, shape, handle = moved[1], moved[2], moved[3]
    assert (label["x"], label["y"], label["style"]["size"]) == (480, 925, 60) and (shape["w"], shape["h"]) == (500, 140) and (handle["x"], handle["y"]) == (543, 958)
    saved = client.post("/api/formats/save", json={"name": "Dragged", "design": {"elements": moved}, "categories": []})
    assert saved.status_code == 200
    page = client.get("/formats").text
    got = json.loads(page.split('<script id="boot" type="application/json">')[1].split("</script>")[0].replace("\\u0027", "'").replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))
    kept = {e["id"]: e for e in got["templates"][0]["design"]["elements"]}
    assert (kept["label-1"]["x"], kept["label-1"]["y"], kept["label-1"]["style"]["size"]) == (480, 925, 60)
    assert (kept["shape-2"]["w"], kept["shape-2"]["h"]) == (500, 140) and (kept["handle-3"]["x"], kept["handle-3"]["y"]) == (543, 958)


@needs_ffmpeg
def test_a_deleted_element_is_not_in_the_next_render(clip_library):
    sv, profile = clip_library
    text = render.ClipText(["A TITLE", "IN TWO LINES"], [("Point one", 6.0), ("Point two", 11.0)], "The takeaway")
    els = make_list("headline", "label", "takeaway", "shape")
    els[3].update(x=540, y=700, w=300, h=100)
    clip = preview.RealClip("talkyvideo1", 40.0, 60.0, text, False)
    before = preview.prepare(sv.db, profile, {"elements": els}, "x", clip=clip)
    assert set(before.windows) >= {"headline-0", "label-1", "takeaway-2", "shape-3"}
    after_list = js("const l = given.list; A.run(l, 'shape-3', 'delete', {}); A.run(l, 'label-1', 'delete', {}); out = l", list=els)
    after = preview.prepare(sv.db, profile, {"elements": after_list}, "x", clip=clip)
    assert "shape-3" not in after.windows and "label-1" not in after.windows and {"headline-0", "takeaway-2"} <= set(after.windows)
    assert len(render.overlay_prints(after.job)[0]) < len(render.overlay_prints(before.job)[0])    # fewer pictures to lay over the clip
    assert before.keys != after.keys                                                                # so the pieces are made again


# --- the page is wired to it ------------------------------------------------------------------------------------------------------------

def test_the_page_loads_the_actions_first_and_blocks_the_browsers_menu_over_the_picture(client):
    page = client.get("/formats").text
    assert page.index("/static/formats-actions.js") < page.index("/static/formats-builder.js")
    assert client.get("/static/formats-actions.js").status_code == 200
    assert '$("stage").addEventListener("contextmenu"' in SCRIPT and "ev.preventDefault();" in SCRIPT.split('"contextmenu"')[1][:200]
    assert "Add element here" in SCRIPT and 'document.addEventListener("keydown"' in SCRIPT and "A.keyEdit(ev.key, ev.shiftKey)" in SCRIPT
    assert "if (ev.button !== 0) return;" in SCRIPT                                    # a right-click is not the start of a drag
    assert 'tabindex="0"' in page.split('id="stage"')[1][:80]
    for old in ("function moveItem(g, e, dx, dy) { A.dragTo", "function resize(g, e, ev) { A.resizeTo"):
        assert old in SCRIPT
