"""Phase 2 of the format builder: the new layout. The page's script lives in a file, the page carries every kind of element and the
data to add one, every setting of every element is on the page, and what the builder sends (a list of elements) is saved and comes
back in the same order with the same look."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient
from test_research import make_sv

from villomillo.formats import designs, elements, fonts, templates
from villomillo.web.app import create_app
from villomillo.web.pages import STATIC

SCRIPT = (STATIC / "formats-builder.js").read_text(encoding="utf-8")


@pytest.fixture
def sv(tmp_path, monkeypatch):
    return make_sv(tmp_path, monkeypatch)


@pytest.fixture
def client(sv):
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780")


def boot(client, query: str = "") -> dict:
    page = client.get("/formats" + query).text
    raw = page.split('<script id="boot" type="application/json">')[1].split("</script>")[0]
    return json.loads(raw.replace("\\u0027", "'").replace("\\u003c", "<").replace("\\u003e", ">").replace("\\u0026", "&"))


def save(client, name: str, els: list[dict], template_id: str | None = None) -> dict:
    r = client.post("/api/formats/save", json={"id": template_id, "name": name, "design": {"elements": els}, "categories": []})
    assert r.status_code == 200, r.text
    return r.json()


def saved_elements(client, template_id: str) -> list[dict]:
    return next(t for t in boot(client)["templates"] if t["id"] == template_id)["design"]["elements"]


def element(kind: str, **over) -> dict:
    e = elements.default_element(kind)
    for key, value in over.items():
        e[key] = value
    return e


# --- the page ----------------------------------------------------------------------------------------------------------

def test_the_builders_script_and_styles_are_files_and_the_page_has_no_script_of_its_own(client):
    page = client.get("/formats").text
    assert "/static/formats-builder.js" in page and "/static/formats-builder.css" in page
    assert "function renderStage" not in page and "fmt-phone{" not in page   # neither the script nor the styles are in the page any more
    assert client.get("/static/formats-builder.js").status_code == 200 and client.get("/static/formats-builder.css").status_code == 200


def test_the_page_carries_every_kind_of_element_and_what_a_new_one_starts_as(client):
    data = boot(client)
    assert [t["id"] for t in data["element_types"]] == list(elements.TYPES)
    assert set(data["element_defaults"]) == set(elements.TYPES)
    for kind, e in data["element_defaults"].items():
        e = dict(e, visible=False) if kind == "logo" else e    # a logo needs a picture before it can show
        assert elements.check([e])[0]["type"] == kind


def test_the_add_menu_offers_every_kind_and_the_layers_list_can_reorder_hide_lock_copy_and_delete():
    for kind in elements.TYPES:
        assert f"{kind}:" in SCRIPT.split("var BLURB = {")[1].split("};")[0], kind   # every kind is described in the add menu
    for word in ("Hide", "Show", "Lock", "Unlock", "Copy", "Delete", "Bring forward", "Send back", "dblclick", "dragstart", "drop"):
        assert word in SCRIPT
    assert "Effects" in SCRIPT and "Music" in SCRIPT   # the fixed layers for the format-wide settings


def _schema_blocks() -> dict[str, str]:
    """The script's list of settings, one block of text per kind of element (the shared look is written into each block)."""
    schema = SCRIPT.split("var X = N(")[1].split("function optionList")[0]
    shared = schema.split("var TEXT_BOX_LOOK = ")[1].split("var SCHEMA")[0]
    body = schema.split("var SCHEMA = {")[1]
    names = list(elements.TYPES)
    starts = {n: m.start() for n in names if (m := re.search(rf"\n    {n}: \{{", body))}
    assert set(starts) == set(names), "the script lists no settings for some kind of element"
    order = sorted(names, key=starts.get)
    return {n: body[starts[n]:starts[order[i + 1]] if i + 1 < len(order) else len(body)].replace("TEXT_BOX_LOOK", shared) for i, n in enumerate(order)}


@pytest.mark.parametrize("kind", list(elements.TYPES))
def test_every_setting_of_every_kind_of_element_is_on_the_page(kind):
    """The settings are made from a list in the script: every style and option an element has, its position, size and timing."""
    block = _schema_blocks()[kind]
    shown = set(re.findall(r'"((?:style|options|text)\.\w+)"', block))
    e = elements.default_element(kind)
    wanted = {f"style.{k}" for k in e["style"]} | {f"options.{k}" for k in e["options"]}
    if kind in ("headline", "label", "takeaway", "handle", "text", "button"):
        wanted.add("text.default")
    assert wanted <= shown, f"{kind}: nothing on the page changes {sorted(wanted - shown)}"
    # position and size: every kind has a spot to set (a shape also its size), and timing is built for all of them
    assert re.search(r'pos: \[[^\]]*(Y|N\("y")', block), f"{kind} has no vertical position setting"
    if kind == "shape":
        assert 'N("w"' in block and 'N("h"' in block
    assert "t.mode = mode.value" in SCRIPT and all(f'"timing.{k}"' in SCRIPT for k in ("seconds", "from", "to"))   # the timing section, for every kind that has one


def test_the_old_look_switches_are_gone_from_the_page_script():
    for old in ("c_enabled", "h_enabled", "x_chips", "b_wm", "selectPane", "PANE_OF", "MOVE_KEY"):
        assert old not in SCRIPT


def test_a_saved_old_style_template_reaches_the_builder_as_elements(sv, client):
    old = {"captions": {"style": "slam"}, "headline": {"size": 52}, "chips": True, "takeaway": True, "brand": {"watermark": True, "progress_bar": False}}
    templates.save(sv.db, name="Old", layers=designs.layers_from(designs.normalize(old)), settings=old)   # stored the way it was before elements
    got = boot(client)["templates"][0]["design"]["elements"]
    kinds = [e["type"] for e in got]
    assert {"headline", "captions", "label", "takeaway", "handle"} <= set(kinds) and "progress_bar" not in kinds
    assert next(e for e in got if e["type"] == "headline")["style"]["size"] == 52


# --- adding, ordering and deleting elements change what is saved -----------------------------------------------------------

def test_what_the_builder_adds_is_saved_in_its_order_with_its_names_and_look(client):
    made = [
        element("headline", name="Big title"), element("label", id="label", name="Tag"),
        element("label", id="label-2", name="Number tag", y=900, style={**elements.default_element("label")["style"], "box": "#FFFFFF", "color": "#000000", "size": 52}),
        element("text", id="text", name="Sticker", text={"source": "fixed", "default": "Part one"}, x=300, y=700),
        element("shape", id="shape", name="Band", x=540, y=1500, w=900, h=90),
        element("handle"), element("progress_bar", id="bar-2", y=40), element("captions"),
    ]
    tid = save(client, "Many", made)["id"]
    got = saved_elements(client, tid)
    assert [e["id"] for e in got] == ["headline", "label", "label-2", "text", "shape", "handle", "bar-2", "captions"]
    assert [e["name"] for e in got][:5] == ["Big title", "Tag", "Number tag", "Sticker", "Band"]
    assert got[2]["style"]["box"] == "#FFFFFF" and got[2]["style"]["size"] == 52
    assert (got[3]["x"], got[3]["y"], got[3]["text"]["default"]) == (300, 700, "Part one")
    assert (got[4]["w"], got[4]["h"]) == (900, 90) and got[6]["y"] == 40


def test_deleting_reordering_hiding_and_locking_change_the_saved_format(client):
    first = [element("headline"), element("label"), element("takeaway"), element("handle"), element("captions")]
    tid = save(client, "Edit me", first)["id"]
    after = saved_elements(client, tid)
    after = [after[3], after[0], after[4], after[1], after[2]]                       # reordered
    after = [e for e in after if e["type"] != "takeaway"]                              # one deleted
    after[0]["visible"], after[1]["locked"] = False, True                              # one hidden, one locked
    save(client, "Edit me", after, tid)
    got = saved_elements(client, tid)
    assert [e["type"] for e in got] == ["handle", "headline", "captions", "label"]
    assert got[0]["visible"] is False and got[1]["locked"] is True


def test_a_format_needs_something_to_say_and_is_told_so_in_plain_words(client):
    for els in ([], [element("captions")], [element("handle"), element("progress_bar")]):
        r = client.post("/api/formats/save", json={"name": "Bare", "design": {"elements": els}, "categories": []})
        assert r.status_code == 422 and "A format needs the headline, the point labels or the takeaway" in r.json()["error"]


def test_a_format_can_have_several_of_one_kind_and_each_keeps_its_own_look(client):
    tid = save(client, "Bars", [element("headline"), element("progress_bar"), element("progress_bar", id="bar-2", y=60, style={**elements.default_element("progress_bar")["style"], "color": "#FF0000"})])["id"]
    got = saved_elements(client, tid)
    assert [e["id"] for e in got] == ["headline", "progress_bar", "bar-2"] and got[2]["style"]["color"] == "#FF0000" and got[1]["style"]["color"] == "#FFB000"


# --- labels and the handle show and save every style option -----------------------------------------------------------------

OTHER_FONT = next(f for f in fonts.FONTS if f != "montserrat-black")
CHANGES = {
    "label": {"font": OTHER_FONT, "size": 52, "color": "#000000", "box": "#FFFFFF", "radius": 12, "pad_x": 33, "pad_y": 11, "outline_color": "#112233",
              "outline_w": 3, "shadow": True, "uppercase": False, "opacity": 80},
    "handle": {"font": OTHER_FONT, "size": 31, "color": "#00FF00", "opacity": 70, "outline_color": "#112233", "outline_w": 2, "box": "#000000", "radius": 14,
               "pad_x": 21, "pad_y": 9},
}


@pytest.mark.parametrize("kind", ["label", "handle"])
def test_labels_and_the_handle_save_every_style_option(client, kind):
    assert set(CHANGES[kind]) == set(elements.default_element(kind)["style"]), "a style option was added: add it to this test"
    for key, value in CHANGES[kind].items():
        e = element(kind)
        e["style"][key] = value
        tid = save(client, f"{kind} {key}", [element("headline"), e])["id"]
        assert next(x for x in saved_elements(client, tid) if x["type"] == kind)["style"][key] == value, f"{kind} {key}"


@pytest.mark.parametrize("kind", ["label", "handle"])
def test_labels_and_the_handle_save_their_position_timing_and_text(client, kind):
    e = element(kind, x=200, y=900, text={"source": "fixed", "default": "Hello"}, name="Mine")
    if kind == "handle":
        e["timing"] = {"mode": "first", "seconds": 4.0, "from": 0.0, "to": None}
    tid = save(client, kind, [element("headline"), e])["id"]
    got = next(x for x in saved_elements(client, tid) if x["type"] == kind)
    assert (got["x"], got["y"], got["text"]["default"], got["name"]) == (200, 900, "Hello", "Mine")
    if kind == "handle":
        assert got["timing"]["mode"] == "first" and got["timing"]["seconds"] == 4.0


def test_a_label_can_be_numbered_or_plain_and_a_second_label_is_its_own(client):
    plain = element("label", options={"numbered": False})
    tid = save(client, "Labels", [element("headline"), plain, element("label", id="label-2", y=900)])["id"]
    got = saved_elements(client, tid)
    assert [e["options"]["numbered"] for e in got if e["type"] == "label"] == [False, True]


# --- plain words when something cannot work ---------------------------------------------------------------------------------

@pytest.mark.parametrize("change,message", [
    ({"style": {"color": "#FFB100", "box": "#FFB000"}}, "too close in colour to read"),
    ({"style": {"font": "no-such-font"}}, "That font is not available"),
    ({"style": {"size": 400}}, "must be a whole number from 16 to 160"),
])
def test_a_label_that_cannot_work_is_refused_in_plain_words(client, change, message):
    e = element("label")
    e["style"] = {**e["style"], **change["style"]}
    r = client.post("/api/formats/save", json={"name": "Bad", "design": {"elements": [e]}, "categories": []})
    assert r.status_code == 422 and message in r.json()["error"]


def test_a_text_element_with_no_words_is_refused(client):
    r = client.post("/api/formats/save", json={"name": "Bad", "design": {"elements": [element("text", text={"source": "fixed", "default": " "})]}, "categories": []})
    assert r.status_code == 422 and "Type the words" in r.json()["error"]
