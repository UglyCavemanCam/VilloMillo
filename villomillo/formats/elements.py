"""A format's look as a list of elements, instead of a fixed set of switches.

Every picture the renderer lays over the video is an element: a headline, point labels, a takeaway, a handle, free text, a
logo, a box, a button, the captions, a progress bar. A format has as many as you like (of any kind, more than one of a kind),
and each carries its own position, size, look, timing and rules.

    from_legacy(design)       today's switches -> the element list (a converted format looks exactly as before)
    check(list)               refuse what cannot work, in plain words, and fill in what is missing
    legacy_from(list)         the old sections (headline, captions, logo, ...) that the placement checks and the old
                              page still read; they are worked out from the elements, never the other way round
    window(timing, total, natural)   when an element is on screen
    for_campaign(list)        the elements a campaign clip keeps

The list is in drawing order: the first element is at the back and the last is in front. The captions and the progress bar
are burned in after every picture, so they sit on top whatever their place in the list.

Positions are in the 1080 by 1920 picture. `x` and `y` of a headline are its top edge (it is as wide as its box), of a logo
its left and top edge; for everything else they are the element's centre. `None` means "the layout's own spot".
"""

from __future__ import annotations

import copy
import re

from . import fonts
from .checks import DesignError, contrast, hex_colour, whole

AMBER, INK, BLACK, WHITE = "#FFB000", "#17120E", "#000000", "#FFFFFF"
MAX_ELEMENTS = 40
TIMING_MODES = ("spoken", "whole", "first", "last", "range")

TYPES = {"headline": "Headline", "captions": "Captions", "label": "Point label", "takeaway": "Takeaway", "handle": "Handle",
         "text": "Text", "logo": "Logo", "shape": "Box", "button": "Button", "progress_bar": "Progress bar"}
# the building block (blueprints.yaml) that gates each type; text and shapes need nothing from the clip
BLOCK = {"headline": "headline", "captions": "captions", "label": "point_chips", "takeaway": "takeaway", "handle": "watermark_center",
         "logo": "logo_corner", "button": "cta_end", "progress_bar": "progress_bar_bottom"}
SPOKEN_ONLY = {"label", "captions"}                    # these follow what is said
SPOKEN_OK = {"headline", "takeaway"} | SPOKEN_ONLY     # these may follow what is said; the rest use a fixed timing
BRAND = {"handle", "logo", "button", "progress_bar"}   # a campaign clip never carries these, whatever the format says
BOX_TYPES = {"label", "takeaway", "button", "text"}    # drawn as a line of text on an optional box
LEGACY_KEYS = ("captions", "headline", "chips", "takeaway", "logo", "cta", "brand", "place")   # the sections the elements replace


def _timing(mode="whole", seconds=3.0, start=0.0, end=None) -> dict:
    return {"mode": mode, "seconds": seconds, "from": start, "to": end}


def box_style(*, size=40, pad=26, color=INK, box=AMBER, font="montserrat-black", radius=None, uppercase=True, outline_w=0) -> dict:
    """The look of a line of text on a box. `radius` None is a pill (as round as the box is tall)."""
    return {"font": font, "size": size, "color": color, "box": box, "radius": radius, "pad_x": pad, "pad_y": pad,
            "outline_color": BLACK, "outline_w": outline_w, "shadow": False, "opacity": 100, "uppercase": uppercase}


def default_element(kind: str, **over) -> dict:
    """A new element of this kind with its usual look. `over` changes any top-level field."""
    if kind not in TYPES:
        raise DesignError(f"There is no element called {kind}.")
    e = {"id": kind, "type": kind, "name": TYPES[kind], "visible": True, "locked": False, "x": None, "y": None, "w": None, "h": None,
         "timing": _timing("spoken" if kind in SPOKEN_OK else "whole"), "rules": {"never_on_campaign": kind in BRAND},
         "style": {}, "options": {}, "text": {"source": "fixed", "default": ""}}
    if kind == "headline":
        e["style"] = {"font": "montserrat-extrabold", "size": 44, "align": "center", "color": INK, "box": AMBER, "radius": 14,
                      "width_pct": 84, "shadow": False, "outline_color": BLACK, "outline_w": None, "opacity": 100}
        e["text"] = {"source": "ai_headline", "default": "YOUR HEADLINE GOES HERE\nIN TWO SHORT LINES"}
    elif kind == "captions":
        e["style"] = {"font": None, "size": None, "uppercase": None, "text_color": None, "highlight_color": None}
        e["options"] = {"style": "spotlight", "position": "lower"}
    elif kind == "label":
        e["style"] = box_style()
        e["options"] = {"numbered": True}
        e["text"] = {"source": "ai_point", "default": "A first point"}
    elif kind == "takeaway":
        e["style"] = box_style(size=46, pad=34)
        e["text"] = {"source": "ai_takeaway", "default": "A short takeaway"}
    elif kind == "handle":
        e["style"] = {"font": "montserrat-bold", "size": None, "color": WHITE, "opacity": None, "outline_color": BLACK, "outline_w": 1,
                      "box": "", "radius": None, "pad_x": 8, "pad_y": 8}
        e["text"] = {"source": "handle", "default": "@yourhandle"}
    elif kind == "text":
        e["style"] = box_style(size=48, color=WHITE, box="", outline_w=4)
        e["text"] = {"source": "fixed", "default": "Your text"}
    elif kind == "logo":
        e["style"] = {"size": 14, "opacity": 100}
        e["options"] = {"asset_id": None, "corner": "left", "margin": None}
    elif kind == "shape":
        e["style"] = {"color": AMBER, "radius": 20, "opacity": 100, "outline_color": BLACK, "outline_w": 0}
        e["w"], e["h"], e["x"], e["y"] = 600, 120, 540, 960
    elif kind == "button":
        e["style"] = box_style(size=40, pad=34)
        e["text"] = {"source": "fixed", "default": "Follow for more"}
        e["timing"] = _timing("last", 3.0)
    elif kind == "progress_bar":
        e["style"] = {"color": AMBER, "track_color": "#F4EBDD", "track_opacity": 27, "height": None}
    for key, value in over.items():
        e[key] = value
    return e


# --- today's switches <-> elements ----------------------------------------------------------------------------------

def _from_legacy(kind: str, d: dict) -> dict:
    """One element out of a normalized legacy design, switched on or off as the design had it."""
    e = default_element(kind)
    p = d["place"]
    if kind == "headline":
        h = d["headline"]
        e["visible"], e["y"] = h["enabled"], p["headline_y"]
        e["style"].update(font=h["font"], size=h["size"], align=h["align"], color=h["color"], box=h["background"], radius=h["radius"],
                          width_pct=h["width_pct"], shadow=h["shadow"], outline_w=0 if h["background"] else 4)
    elif kind == "captions":
        c = d["captions"]
        e["visible"], e["y"] = c["enabled"], p["captions_y"]
        e["style"].update(font=c["font"], size=c["size"], uppercase=c["uppercase"], text_color=c["text_color"], highlight_color=c["highlight_color"])
        e["options"].update(style=c["style"], position=c["position"])
    elif kind == "label":
        e["visible"], e["y"] = d["chips"], p["labels_y"]
    elif kind == "takeaway":
        e["visible"], e["y"] = d["takeaway"], p["labels_y"]
    elif kind == "handle":
        e["visible"] = d["brand"]["watermark"]
    elif kind == "logo":
        lg = d["logo"]
        e["visible"], e["x"], e["y"] = lg["enabled"], p["logo_x"], p["logo_y"]
        e["style"]["size"] = lg["size"]
        e["options"].update(asset_id=lg["asset_id"], corner=lg["corner"])
        e["timing"] = _timing("range", start=float(lg["from_s"]))
    elif kind == "button":
        cta = d["cta"]
        e["visible"], e["y"] = cta["enabled"], p["cta_y"]
        e["style"]["size"] = cta["size"]
        e["text"]["default"] = cta["text"]
        e["timing"] = _timing("last", float(cta["seconds"]))
    elif kind == "progress_bar":
        e["visible"] = d["brand"]["progress_bar"]
    return e


LEGACY_ORDER = ("headline", "label", "takeaway", "button", "logo", "handle", "captions", "progress_bar")   # the order they were drawn in


def from_legacy(design: dict) -> list[dict]:
    """The element list for a normalized design made of the old switches. An element that is switched off and was never
    changed is left out; one that is off but was set up is kept, hidden, so switching it back on loses nothing."""
    from . import designs

    out = []
    for kind in LEGACY_ORDER:
        e = _from_legacy(kind, design)
        if not e["visible"]:
            plain = _from_legacy(kind, designs.DEFAULT)
            plain["visible"] = False
            if e == plain:
                continue
        out.append(e)
    return out


def _first(els: list[dict], kind: str) -> dict | None:
    of_kind = [e for e in els if e["type"] == kind]
    return next((e for e in of_kind if e["visible"]), of_kind[0] if of_kind else None)


def _clamp(value, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(round(value))))


def legacy_from(els: list[dict]) -> dict:
    """The old sections that the elements stand for (the first element of each kind), for the checks and the old page that still
    read them. Anything the old sections cannot say is left out; the elements stay the truth."""
    from . import designs

    base = copy.deepcopy(designs.DEFAULT)
    out = {k: base[k] for k in ("captions", "headline", "logo", "cta")}
    out["chips"] = out["takeaway"] = False
    out["brand"] = {"watermark": False, "progress_bar": False}
    out["place"] = dict(base["place"])
    for key in ("captions", "headline", "logo", "cta"):
        out[key]["enabled"] = False
    if (e := _first(els, "headline")):
        s = e["style"]
        out["headline"].update(enabled=e["visible"], font=s["font"], size=s["size"], align=s["align"], color=s["color"], background=s["box"],
                               radius=s["radius"], width_pct=s["width_pct"], shadow=s["shadow"])
        out["place"]["headline_y"] = e["y"]
    if (e := _first(els, "captions")):
        s = e["style"]
        out["captions"].update(enabled=e["visible"], style=e["options"]["style"], font=s["font"], size=s["size"], uppercase=s["uppercase"],
                               position=e["options"]["position"], text_color=s["text_color"], highlight_color=s["highlight_color"])
        out["place"]["captions_y"] = e["y"]
    labels, takeaway = _first(els, "label"), _first(els, "takeaway")
    out["chips"] = bool(labels and labels["visible"])
    out["takeaway"] = bool(takeaway and takeaway["visible"])
    row = next((e["y"] for e in (labels, takeaway) if e is not None and e["y"] is not None), None)
    out["place"]["labels_y"] = row
    if (e := _first(els, "logo")):
        t = e["timing"]
        out["logo"].update(enabled=e["visible"], asset_id=e["options"]["asset_id"], size=e["style"]["size"], corner=e["options"]["corner"],
                           from_s=_clamp(t["from"], 0, 30) if t["mode"] == "range" else 0)
        out["place"]["logo_x"], out["place"]["logo_y"] = e["x"], e["y"]
    if (e := _first(els, "button")):
        t = e["timing"]
        out["cta"].update(enabled=e["visible"], text=e["text"]["default"], size=e["style"]["size"],
                          seconds=_clamp(t["seconds"], 1, 6) if t["mode"] in ("first", "last") else 3)
        out["place"]["cta_y"] = e["y"]
    out["brand"] = {"watermark": any(e["type"] == "handle" and e["visible"] for e in els),
                    "progress_bar": any(e["type"] == "progress_bar" and e["visible"] for e in els)}
    return out


# --- checking a list of elements ------------------------------------------------------------------------------------

def _merge(base: dict, over) -> dict:
    out = copy.deepcopy(base)
    if not isinstance(over, dict):
        return out
    for key, value in over.items():
        out[key] = _merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
    return out


def _box(e: dict, who: str) -> None:
    s = e["style"]
    if s["font"] not in fonts.FONTS:
        raise DesignError(f"That font is not available ({who}).")
    s["size"] = whole(s["size"], 16, 160, f"The size of {who}")
    s["color"] = hex_colour(s["color"], f"The text colour of {who}")
    s["box"] = hex_colour(s["box"], f"The box colour of {who}", blank_ok=True)
    s["radius"] = whole(s["radius"], 0, 100, f"The corner radius of {who}", none_ok=True)
    s["pad_x"] = whole(s["pad_x"], 0, 120, f"The side padding of {who}")
    s["pad_y"] = whole(s["pad_y"], 0, 120, f"The top and bottom padding of {who}")
    s["outline_color"] = hex_colour(s["outline_color"], f"The outline colour of {who}")
    s["outline_w"] = whole(s["outline_w"], 0, 12, f"The outline width of {who}")
    s["shadow"], s["uppercase"] = bool(s["shadow"]), bool(s["uppercase"])
    s["opacity"] = whole(s["opacity"], 10, 100, f"The opacity of {who}")
    if s["box"] and contrast(s["color"], s["box"]) < 3:
        raise DesignError(f"The text of {who} and its box are too close in colour to read. Pick colours that contrast more.")


def _check_one(raw: dict, kind: str, who: str) -> dict:
    e = _merge(default_element(kind), raw)
    s, o, t = e["style"], e["options"], e["text"]
    e["name"] = str(e["name"] or TYPES[kind]).strip()[:40] or TYPES[kind]
    e["visible"], e["locked"] = bool(e["visible"]), bool(e["locked"])
    for axis, hi in (("x", 1080), ("y", 1920)):
        e[axis] = whole(e[axis], 0, hi, f"The {axis} position of {who}", none_ok=True)
    tm = e["timing"]
    if tm["mode"] not in TIMING_MODES:
        raise DesignError(f"The timing of {who} must be spoken, the whole clip, the first seconds, the last seconds or a time range.")
    if tm["mode"] == "spoken" and kind not in SPOKEN_OK:
        raise DesignError(f"{who[0].upper() + who[1:]} cannot follow the spoken words: pick the whole clip, the first or last seconds, or a time range.")
    if tm["mode"] != "spoken" and kind in SPOKEN_ONLY:
        raise DesignError(f"{who[0].upper() + who[1:]} follows what is said, so its timing cannot be changed.")
    try:
        tm["seconds"] = float(tm["seconds"])
    except (TypeError, ValueError):
        tm["seconds"] = -1.0
    if not 0.5 <= tm["seconds"] <= 120:
        raise DesignError(f"The seconds of {who} must be from 0.5 to 120.")
    tm["from"] = float(whole(tm["from"], 0, 3600, f"The start time of {who}"))
    if tm["to"] is not None:
        tm["to"] = float(whole(tm["to"], 1, 3600, f"The end time of {who}"))
        if tm["to"] <= tm["from"]:
            raise DesignError(f"The end time of {who} must come after its start time.")
    e["rules"] = {"never_on_campaign": True if kind in BRAND else bool(e["rules"].get("never_on_campaign"))}
    t["source"], t["default"] = str(t["source"]), str(t["default"] or "")[:200]
    if kind in BOX_TYPES:
        _box(e, who)
        if kind in ("button", "text") and not t["default"].strip():
            raise DesignError(f"Type the words for {who}, or remove it.")
    elif kind == "headline":
        if s["font"] not in fonts.FONTS:
            raise DesignError("That headline font is not available.")
        s["size"] = whole(s["size"], 24, 90, "The headline size")
        if s["align"] not in ("left", "center", "right"):
            raise DesignError("Headline alignment must be left, centre or right.")
        s["color"] = hex_colour(s["color"], "The headline text colour")
        s["box"] = hex_colour(s["box"], "The headline box colour", blank_ok=True)
        s["radius"] = whole(s["radius"], 0, 40, "The headline corner radius")
        s["width_pct"] = whole(s["width_pct"], 40, 100, "The headline width")
        s["shadow"] = bool(s["shadow"])
        s["outline_color"] = hex_colour(s["outline_color"], "The headline outline colour")
        s["outline_w"] = whole(s["outline_w"], 0, 12, "The headline outline width", none_ok=True)
        s["opacity"] = whole(s["opacity"], 10, 100, "The headline opacity")
        if s["box"] and contrast(s["color"], s["box"]) < 3:
            raise DesignError("The headline text and its box are too close in colour to read. Pick colours that contrast more.")
    elif kind == "captions":
        from .captions import STYLES

        if o["style"] not in STYLES:
            raise DesignError(f"There is no caption style called {o['style']}.")
        if s["font"] is not None and s["font"] not in fonts.FONTS:
            raise DesignError("That caption font is not available.")
        s["size"] = whole(s["size"], 40, 160, "The caption size", none_ok=True)
        s["uppercase"] = None if s["uppercase"] is None else bool(s["uppercase"])
        if o["position"] not in ("lower", "middle", "upper"):
            raise DesignError("Caption position must be lower, middle or upper.")
        s["text_color"] = hex_colour(s["text_color"], "The caption text colour", none_ok=True)
        s["highlight_color"] = hex_colour(s["highlight_color"], "The caption highlight colour", none_ok=True)
    elif kind == "handle":
        if s["font"] not in fonts.FONTS:
            raise DesignError(f"That font is not available ({who}).")
        s["size"] = whole(s["size"], 12, 120, f"The size of {who}", none_ok=True)
        s["color"] = hex_colour(s["color"], f"The text colour of {who}")
        s["opacity"] = whole(s["opacity"], 5, 100, f"The opacity of {who}", none_ok=True)
        s["outline_color"] = hex_colour(s["outline_color"], f"The outline colour of {who}")
        s["outline_w"] = whole(s["outline_w"], 0, 12, f"The outline width of {who}")
        s["box"] = hex_colour(s["box"], f"The box colour of {who}", blank_ok=True)
        s["radius"] = whole(s["radius"], 0, 100, f"The corner radius of {who}", none_ok=True)
        s["pad_x"] = whole(s["pad_x"], 0, 120, f"The side padding of {who}")
        s["pad_y"] = whole(s["pad_y"], 0, 120, f"The top and bottom padding of {who}")
    elif kind == "logo":
        s["size"] = whole(s["size"], 6, 30, "The logo size")
        s["opacity"] = whole(s["opacity"], 10, 100, "The logo opacity")
        o["asset_id"] = whole(o["asset_id"], 1, 10 ** 9, "The logo", none_ok=True)
        if o["corner"] not in ("left", "right"):
            raise DesignError("The logo corner must be left or right.")
        o["margin"] = whole(o["margin"], 0, 300, "The logo margin", none_ok=True)
        if e["visible"] and o["asset_id"] is None:
            raise DesignError("Pick a logo to show, or turn the logo off.")
    elif kind == "shape":
        e["w"], e["h"] = whole(e["w"], 4, 1080, f"The width of {who}"), whole(e["h"], 4, 1920, f"The height of {who}")
        s["color"] = hex_colour(s["color"], f"The colour of {who}")
        s["radius"] = whole(s["radius"], 0, 960, f"The corner radius of {who}")
        s["opacity"] = whole(s["opacity"], 5, 100, f"The opacity of {who}")
        s["outline_color"] = hex_colour(s["outline_color"], f"The outline colour of {who}")
        s["outline_w"] = whole(s["outline_w"], 0, 40, f"The outline width of {who}")
    elif kind == "progress_bar":
        s["color"] = hex_colour(s["color"], "The progress bar colour")
        s["track_color"] = hex_colour(s["track_color"], "The progress bar track colour")
        s["track_opacity"] = whole(s["track_opacity"], 0, 100, "The progress bar track opacity")
        s["height"] = whole(s["height"], 2, 80, "The progress bar height", none_ok=True)
    if kind == "button":
        text = t["default"].strip()
        if len(text) > 40:
            raise DesignError("The call to action must be 40 characters or fewer.")
        t["default"] = text
    return e


def check(given) -> list[dict]:
    """The list with every missing choice filled in, or a DesignError that says what to change."""
    if not isinstance(given, list):
        raise DesignError("The elements must be a list.")
    if len(given) > MAX_ELEMENTS:
        raise DesignError(f"A format can have at most {MAX_ELEMENTS} elements.")
    out, seen, count = [], set(), {}
    for raw in given:
        if not isinstance(raw, dict) or raw.get("type") not in TYPES:
            raise DesignError("One of the elements is not a kind the app knows.")
        kind = raw["type"]
        count[kind] = count.get(kind, 0) + 1
        who = str(raw.get("name") or TYPES[kind]).strip()[:40] or TYPES[kind]
        e = _check_one(raw, kind, f"the {who.lower()}")
        ident = re.sub(r"[^a-z0-9_-]+", "-", str(raw.get("id") or "").lower()).strip("-")[:40]
        if not ident:
            ident = kind if count[kind] == 1 else f"{kind}-{count[kind]}"
        if ident in seen:
            raise DesignError(f"Two elements share the id {ident}.")
        seen.add(ident)
        e["id"] = ident
        out.append(e)
    return out


# --- when an element is on screen -----------------------------------------------------------------------------------

def window(timing: dict, total: float, natural: tuple[float, float] | None = None) -> tuple[float, float] | None:
    """Seconds into the clip when the element shows, or None for the whole clip. `natural` is the element's own timing when
    it follows what is said."""
    mode = timing["mode"]
    if mode == "spoken":
        return natural
    if mode == "whole":
        return None
    if mode == "first":
        return 0.0, min(timing["seconds"], total)
    if mode == "last":
        return max(total - timing["seconds"], 0.0), total
    start, end = timing["from"], timing["to"]
    if start <= 0 and end is None:
        return None
    return start, min(end, total) if end is not None else total


def active(els: list[dict]) -> list[dict]:
    return [e for e in els if e["visible"]]


# --- campaign clips -------------------------------------------------------------------------------------------------

def for_campaign(els: list[dict]) -> tuple[list[dict], set[str]]:
    """The elements a campaign clip keeps, and the building blocks that must be left out of its plan. A campaign never carries
    a handle, progress bar, logo or button, and an element can also be marked to stay off campaign clips."""
    kept = [e for e in els if not e["rules"]["never_on_campaign"]]
    gone = {BLOCK[e["type"]] for e in els if e["rules"]["never_on_campaign"] and e["type"] in BLOCK}
    return kept, gone
