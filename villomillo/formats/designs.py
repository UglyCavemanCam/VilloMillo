"""A template's design: every choice the builder offers, checked in plain words, and turned into the blocks a clip is made from.

A design is kept as the template's settings. `normalize` fills in what is missing and refuses what cannot work, with a
message that says what to change. `layers_from` turns the design into the building blocks (blueprints.yaml).
"""

from __future__ import annotations

import copy
import json
import re

from ..db import Database
from . import effects as effect_rules
from . import fonts
from .captions import STYLES

MUSIC_MODES = ("off", "when_helps", "always")
LAYOUTS = {"face_fill": "Face fill", "fit_blur": "Fit with blur", "stacked_webcam": "Stacked: webcam over the game",
           "split": "Split: left person on top, right person below"}
POSITIONS = {"lower": 1345, "middle": 1000, "upper": 640}   # a caption's centre; all inside Shorts' safe zone
ALIGNS = ("left", "center", "right")
HEADLINE_TOP = {"face_fill": 430, "fit_blur": 470, "stacked_webcam": 790, "split": 820}          # where the headline starts (the top fifth is left to Shorts' own title)
LOGO_Y = {"face_fill": 190, "fit_blur": 190, "stacked_webcam": 1010, "split": 1010}   # the logo's top edge: clear of the headline, captions and platform bars
CHIPS_Y = {"face_fill": 1175, "fit_blur": 395, "stacked_webcam": 1040, "split": 1040}
DELETED_KEY = "deleted_presets"
DEMO_LENGTH = 35.0   # seconds: the builder's sample clip, and the clip length the save-time placement check assumes.
                     # Longer than the latest second a logo can start (30), so every timed choice shows in the sample.
# Where the owner dragged an item on the stage, in the 1080x1920 picture. None means the layout's own spot above.
# y values are the same edge or centre the constants above give (see place_of); logo_x is the logo's left edge.
PLACE_LIMITS = {"headline_y": (0, 1920, "The headline's height"), "captions_y": (0, 1920, "The captions' height"),
                "labels_y": (0, 1920, "The labels' height"), "cta_y": (0, 1920, "The button's height"),
                "logo_x": (0, 1080, "The logo's left edge"), "logo_y": (0, 1920, "The logo's height")}


class DesignError(ValueError):
    """The message says what to change, in plain words."""


DEFAULT: dict = {
    "layout": "face_fill",
    "crop_limit": 0,
    "webcam": {"x": 0, "y": 11, "w": 21, "h": 27},   # where the webcam is, as % of the picture (stacked layout)
    "captions": {"enabled": True, "style": "spotlight", "font": None, "size": None, "uppercase": None,
                 "position": "lower", "text_color": None, "highlight_color": None},
    "headline": {"enabled": True, "font": "montserrat-extrabold", "size": 44, "align": "center", "color": "#17120E",
                 "background": "#FFB000", "radius": 14, "width_pct": 84, "shadow": False},
    "chips": True,
    "takeaway": True,
    "logo": {"enabled": False, "asset_id": None, "size": 14, "corner": "left", "from_s": 0},   # size: % of the frame's width
    "effects": {"intensity": "subtle", "kinds": list(effect_rules.KINDS)},   # off, subtle, balanced or punchy
    "music": {"mode": "when_helps", "volume": -24},   # off, when_helps or always; volume: dB under the voice
    "cta": {"enabled": False, "text": "Follow for more", "size": 40, "seconds": 3},
    "brand": {"watermark": True, "progress_bar": True},
    "style_mode": "fixed",
    "made_by": "you",   # "ai" for a template the AI Generate button made, so the list can say so
    "style_pool": list(STYLES),
    "place": {key: None for key in PLACE_LIMITS},   # dragged positions; None keeps the layout's spot
}


def place_of(design: dict, key: str, default: int) -> int:
    """Where an item sits: where the owner dragged it, or the layout's own spot when it was never moved."""
    value = (design.get("place") or {}).get(key)
    return default if value is None else value


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (over or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _hex(value, what: str, *, blank_ok: bool = False, none_ok: bool = False) -> str | None:
    if value is None and none_ok:
        return None
    if value == "" and blank_ok:
        return ""
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value.strip()):
        return value.strip().upper()
    raise DesignError(f"{what} must be a colour like #FFB000.")


def _whole(value, lo: int, hi: int, what: str, *, none_ok: bool = False) -> int | None:
    if value is None and none_ok:
        return None
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        raise DesignError(f"{what} must be a whole number from {lo} to {hi}.") from None
    if not lo <= n <= hi:
        raise DesignError(f"{what} must be a whole number from {lo} to {hi}.")
    return n


def _luminance(hex_rgb: str) -> float:
    channels = []
    for i in (1, 3, 5):
        c = int(hex_rgb[i:i + 2], 16) / 255
        channels.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def normalize(design: dict | None) -> dict:
    """The design with every missing choice filled in, or a DesignError that says what to change."""
    d = _merge(DEFAULT, design or {})
    if d["layout"] not in LAYOUTS:
        raise DesignError("Pick a layout: Face fill or Fit with blur.")

    d["crop_limit"] = _whole(d["crop_limit"], 0, 60, "The crop limit")
    cam = d["webcam"]
    for key, what in (("x", "left"), ("y", "top"), ("w", "width"), ("h", "height")):
        _whole(cam.get(key), 0, 100, f"The webcam box {what}")
    if cam["w"] < 5 or cam["h"] < 5 or cam["x"] + cam["w"] > 100 or cam["y"] + cam["h"] > 100:
        raise DesignError("The webcam box must fit inside the picture and be at least 5% wide and tall.")

    c = d["captions"]
    c["enabled"] = bool(c["enabled"])
    if c["style"] not in STYLES:
        raise DesignError(f"There is no caption style called {c['style']}.")
    if c["font"] is not None and c["font"] not in fonts.FONTS:
        raise DesignError("That caption font is not available.")
    c["size"] = _whole(c["size"], 40, 160, "The caption size", none_ok=True)
    c["uppercase"] = None if c["uppercase"] is None else bool(c["uppercase"])
    if c["position"] not in POSITIONS:
        raise DesignError("Caption position must be lower, middle or upper.")
    c["text_color"] = _hex(c["text_color"], "The caption text colour", none_ok=True)
    c["highlight_color"] = _hex(c["highlight_color"], "The caption highlight colour", none_ok=True)

    h = d["headline"]
    h["enabled"] = bool(h["enabled"])
    if h["font"] not in fonts.FONTS:
        raise DesignError("That headline font is not available.")
    h["size"] = _whole(h["size"], 24, 90, "The headline size")
    if h["align"] not in ALIGNS:
        raise DesignError("Headline alignment must be left, centre or right.")
    h["color"] = _hex(h["color"], "The headline text colour")
    h["background"] = _hex(h["background"], "The headline box colour", blank_ok=True)
    h["radius"] = _whole(h["radius"], 0, 40, "The headline corner radius")
    h["width_pct"] = _whole(h["width_pct"], 40, 100, "The headline width")
    h["shadow"] = bool(h["shadow"])
    if h["background"] and contrast(h["color"], h["background"]) < 3:
        raise DesignError("The headline text and its box are too close in colour to read. Pick colours that contrast more.")

    lg = d["logo"]
    lg["enabled"] = bool(lg["enabled"])
    lg["asset_id"] = _whole(lg["asset_id"], 1, 10 ** 9, "The logo", none_ok=True)
    lg["size"] = _whole(lg["size"], 6, 30, "The logo size")
    if lg["enabled"] and lg["asset_id"] is None:
        raise DesignError("Pick a logo to show, or turn the logo off.")
    cta = d["cta"]
    cta["enabled"] = bool(cta["enabled"])
    cta["text"] = str(cta["text"] or "").strip()
    if len(cta["text"]) > 40:
        raise DesignError("The call to action must be 40 characters or fewer.")
    if cta["enabled"] and not cta["text"]:
        raise DesignError("Type the call to action, or turn it off.")
    cta["size"] = _whole(cta["size"], 24, 60, "The call to action size")
    cta["seconds"] = _whole(cta["seconds"], 1, 6, "The call to action length")
    if lg["corner"] not in ("left", "right"):
        raise DesignError("The logo corner must be left or right.")
    lg["from_s"] = _whole(lg["from_s"], 0, 30, "The second the logo first shows")
    fx = d["effects"]
    if fx["intensity"] not in effect_rules.INTENSITIES:
        raise DesignError("Effects must be off, subtle, balanced or punchy.")
    if not isinstance(fx["kinds"], list) or any(k not in effect_rules.KINDS for k in fx["kinds"]):
        raise DesignError("One of the effects is not known.")
    fx["kinds"] = [k for k in effect_rules.KINDS if k in fx["kinds"]]
    mu = d["music"]
    if mu["mode"] not in MUSIC_MODES:
        raise DesignError("Music must be off, only when it helps, or always.")
    mu["volume"] = _whole(mu["volume"], -30, -16, "The music volume")
    d["chips"], d["takeaway"] = bool(d["chips"]), bool(d["takeaway"])
    d["brand"] = {"watermark": bool(d["brand"]["watermark"]), "progress_bar": bool(d["brand"]["progress_bar"])}
    if d["style_mode"] not in ("fixed", "ai"):
        raise DesignError("Caption style must be fixed or picked by the AI.")
    pool = [s for s in d["style_pool"] if s in STYLES] if isinstance(d["style_pool"], list) else []
    if not pool:
        raise DesignError("Pick at least one caption style for the AI to choose from.")
    d["style_pool"] = pool
    if not (h["enabled"] or d["chips"] or d["takeaway"]):
        raise DesignError("A format needs the headline, the point labels or the takeaway: captions on their own are not enough.")
    given = d.get("place") or {}
    d["place"] = {key: _whole(given.get(key), lo, hi, what, none_ok=True) for key, (lo, hi, what) in PLACE_LIMITS.items()}
    if h["enabled"] and c["enabled"] and c["position"] == "upper":
        raise DesignError("The headline and the captions would overlap at the top. Move the captions lower.")
    return d


def overrides_from(design: dict) -> dict:
    """Block settings a design carries: how far from the left edge a face-fill crop must stay (0 = no limit)."""
    return {"face_fill": {"keep_right_of": design.get("crop_limit", 0) / 100}}


def layers_from(design: dict) -> dict:
    """The building blocks a normalized design stands for."""
    text = [name for name, on in (("captions", design["captions"]["enabled"]), ("headline", design["headline"]["enabled"]),
                                  ("point_chips", design["chips"]), ("takeaway", design["takeaway"])) if on]
    brand = [name for name, on in (("watermark_center", design["brand"]["watermark"]),
                                   ("progress_bar_bottom", design["brand"]["progress_bar"]),
                                   ("logo_corner", design["logo"]["enabled"]), ("cta_end", design["cta"]["enabled"])) if on]
    return {"framing": design["layout"], "text": text, "brand": brand}


# --- quick presets: whole formats that ship with the app ---------------------------------------------------------

PRESETS: list[dict] = [
    {"id": "podcast", "name": "Podcast",
     "blurb": "The speaker fills the screen. Spotlight captions, an amber headline box, point labels and a takeaway, "
              "a faint handle in the middle and a thin progress bar.",
     "design": normalize({})},
    {"id": "stream", "name": "Stream reaction",
     "blurb": "The webcam sits in a band on top and the game fills the band below it. Slam captions, one word at a time, "
              "a headline, point labels and a takeaway. Built for a Twitch or Kick VOD where the webcam sits in a corner.",
     "design": normalize({"layout": "stacked_webcam", "captions": {"style": "slam"}})},
]


def _deleted(db: Database) -> list[str]:
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (DELETED_KEY,)).fetchone()
    return json.loads(row["value"]) if row else []


def presets(db: Database) -> list[dict]:
    """The quick presets that have not been deleted. A deleted preset never comes back."""
    gone = set(_deleted(db))
    return [p for p in PRESETS if p["id"] not in gone]


def preset(db: Database, preset_id: str) -> dict | None:
    return next((p for p in presets(db) if p["id"] == preset_id), None)


def delete_preset(db: Database, preset_id: str) -> None:
    if not any(p["id"] == preset_id for p in PRESETS):
        raise DesignError("There is no such preset.")
    gone = sorted({*_deleted(db), preset_id})
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (DELETED_KEY, json.dumps(gone)))
