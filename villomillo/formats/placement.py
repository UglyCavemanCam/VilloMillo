"""Where the logo and the call to action may sit. They must never cover the captions or the headline.

Each is a box on the 1080x1920 picture, shown for a stretch of the clip. Two boxes that share a stretch of time and overlap
are refused, with the reason in plain words: the caller either refuses the design (when saving) or flags the clip (when making it).
"""

from __future__ import annotations

from . import designs, elements

W, H = 1080, 1920
MARGIN = 60
CAPTION_HALF_HEIGHT = 100      # a caption group is about 200 pixels tall, centred on its line (an estimate, checked on real clips)
CTA_HEIGHT = 90
CTA_WIDTH = 0.8                # the call to action is at most this share of the frame's width
HEADLINE_LINE = 1.25           # a headline line is about this many times its font size, plus its padding
HEADLINE_PAD = 30
SAVE_HEADLINE_END = 7.0        # when saving we do not know the clip, so assume the headline may show for its longest
MIN_TEXT = 28                  # a line of text smaller than this (in the 1080 wide picture) is hard to read on a phone
MIN_HANDLE = 20                # the faint handle is allowed to be a little smaller
BUTTONS = (960, 900, W, 1660)  # where the app's own buttons (like, comment, share) sit on the right edge: x0, y0, x1, y1
TITLE_TOP, TITLE_BOTTOM = 110, 1640   # above and below these lines the app's own bars and the clip's title and description cover the picture
OVERLAP_SHARE = 0.25           # two elements count as on top of each other when this much of the smaller one is covered
TEXT_KINDS = ("headline", "captions", "label", "takeaway", "button", "text", "handle", "logo")
LEGACY_KINDS = ("headline", "captions", "logo", "button")   # the pairs of these four are already judged by `problems`


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _boxes_touch(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def problems(design: dict, framing: str, logo_size: tuple[int, int] | None, total: float, head_end: float) -> list[str]:
    """Every way the chosen placement covers something it must not. An empty list means it is safe."""
    found: list[str] = []
    boxes = []   # (name, (x0, y0, x1, y1), (t0, t1))

    if design["captions"]["enabled"]:
        centre = designs.place_of(design, "captions_y", designs.POSITIONS[design["captions"]["position"]])
        boxes.append(("the captions", (0, centre - CAPTION_HALF_HEIGHT, W, centre + CAPTION_HALF_HEIGHT), (0.0, total)))
    if design["headline"]["enabled"] and head_end > 0:
        width = int(W * design["headline"]["width_pct"] / 100)
        size = int(design["headline"]["size"])
        height = int(2 * (size * HEADLINE_LINE + HEADLINE_PAD))
        top = designs.place_of(design, "headline_y", designs.HEADLINE_TOP[framing])
        boxes.append(("the headline", ((W - width) // 2, top, (W + width) // 2, top + height), (0.0, head_end)))

    logo = design["logo"]
    if logo["enabled"] and logo_size is not None:
        w, h = logo_size
        right = framing in ("stacked_webcam", "split") or logo["corner"] == "right"
        x0 = designs.place_of(design, "logo_x", W - w - MARGIN if right else MARGIN)
        y0 = designs.place_of(design, "logo_y", designs.LOGO_Y[framing])
        boxes.append(("the logo", (x0, y0, x0 + w, y0 + h), (float(logo["from_s"]), total)))

    cta = design["cta"]
    if cta["enabled"]:
        seconds = float(cta["seconds"])
        cw = int(W * CTA_WIDTH)
        cy = designs.place_of(design, "cta_y", designs.CHIPS_Y[framing])
        boxes.append(("the call to action", ((W - cw) // 2, cy - CTA_HEIGHT // 2, (W + cw) // 2, cy + CTA_HEIGHT // 2),
                      (max(total - seconds, 0.0), total)))

    for i, (name_a, box_a, time_a) in enumerate(boxes):
        for name_b, box_b, time_b in boxes[i + 1:]:
            if _overlap(time_a, time_b) and _boxes_touch(box_a, box_b):
                mover, other = (name_a, name_b) if "logo" in name_a or "call" in name_a else (name_b, name_a)
                advice = ("make the logo smaller or choose the other corner" if "logo" in mover
                          else "move the call to action or the captions")
                found.append(f"{mover[0].upper() + mover[1:]} would cover {other}: {advice}")
    return found


def logo_size(logo_path, design: dict) -> tuple[int, int]:
    """The logo's box on the picture, as the renderer draws it (the width from the setting, the height capped)."""
    from PIL import Image

    img = Image.open(logo_path)
    w = round(W * int(design["logo"]["size"]) / 100)
    h = round(img.height * w / img.width)
    if h > 260:
        w, h = round(img.width * 260 / img.height), 260
    return w, h


def check_design(db, design: dict, folder=None) -> None:
    """Refuse a design whose placement covers something, before it is saved. Raises designs.DesignError with the reason."""
    found = problems_for(db, design, folder)
    if found:
        raise designs.DesignError(found[0].capitalize() + ".")


def problems_for(db, design: dict, folder=None) -> list[str]:
    """Every way the design's placement covers something, for the builder to show while things are dragged."""
    from . import assets

    size = None
    if design["logo"]["enabled"]:
        asset = assets.get(db, design["logo"]["asset_id"])
        path = assets.file_path(asset, folder) if asset else None
        if path is not None and path.exists():
            from PIL import Image

            img = Image.open(path)
            w = round(W * int(design["logo"]["size"]) / 100)
            h = round(img.height * w / img.width)
            size = (w, min(h, 260))
    found = problems(design, design["layout"], size, total=designs.DEMO_LENGTH, head_end=SAVE_HEADLINE_END)
    return found + element_problems(design, design["layout"], size, designs.DEMO_LENGTH, SAVE_HEADLINE_END)


# --- every element, in plain words -------------------------------------------------------------------------------------------------

def _box(e: dict, framing: str, logo: tuple[int, int] | None) -> tuple[int, int, int, int] | None:
    """Where an element sits on the picture as (x0, y0, x1, y1), estimated from its settings the way the renderer lays it out. A line of text
    is about 0.6 of its size wide per character; the check allows for that being a guess."""
    s, kind = e["style"], e["type"]
    if kind == "progress_bar":
        return None
    if kind == "captions":
        centre = e["y"] if e["y"] is not None else designs.POSITIONS[e["options"]["position"]]
        return (0, centre - CAPTION_HALF_HEIGHT, W, centre + CAPTION_HALF_HEIGHT)
    if kind == "headline":
        width = int(W * s["width_pct"] / 100)
        top = e["y"] if e["y"] is not None else designs.HEADLINE_TOP[framing]
        return ((W - width) // 2, top, (W + width) // 2, top + int(2 * (s["size"] * HEADLINE_LINE + HEADLINE_PAD)))
    if kind == "logo":
        if logo is None:
            return None
        right = framing in ("stacked_webcam", "split") or e["options"]["corner"] == "right"
        margin = e["options"]["margin"] if e["options"]["margin"] is not None else MARGIN
        x0 = e["x"] if e["x"] is not None else (W - logo[0] - margin if right else margin)
        y0 = e["y"] if e["y"] is not None else designs.LOGO_Y[framing]
        return (x0, y0, x0 + logo[0], y0 + logo[1])
    cx = e["x"] if e["x"] is not None else W // 2
    if kind == "shape":
        cy = e["y"] if e["y"] is not None else H // 2
        return (cx - e["w"] // 2, cy - e["h"] // 2, cx + e["w"] // 2, cy + e["h"] // 2)
    cy = e["y"] if e["y"] is not None else (designs.CHIPS_Y[framing] if kind in ("label", "takeaway", "button") else H // 2)
    size = s["size"] or 26
    words = e["text"]["default"] or ""
    wide = (0.62 if s.get("uppercase") else 0.55) * size * (len(words) + (6 if kind == "label" and e["options"].get("numbered") else 0))
    width = min(int(wide + 2 * (s.get("pad_x") or 0)), int(W * 0.96))
    height = int(size * 1.3 + 2 * (s.get("pad_y") or 0))
    return (cx - width // 2, cy - height // 2, cx + width // 2, cy + height // 2)


def _when(e: dict, total: float, head_end: float) -> tuple[float, float]:
    """When an element can be on screen, in seconds. What follows the speech is taken at its widest."""
    if e["timing"]["mode"] == "spoken":
        return {"headline": (0.0, head_end), "label": (head_end, max(total - 3.0, head_end)), "takeaway": (max(total - 3.0, 0.0), total)}.get(e["type"], (0.0, total))
    return elements.window(e["timing"], total) or (0.0, total)


def element_problems(design: dict, framing: str, logo_size: tuple[int, int] | None, total: float, head_end: float) -> list[str]:
    """What is wrong with the elements' sizes and places, in plain words: text too small to read on a phone, an element put where the app's own
    buttons or title would hide it, and two elements on top of each other at the same time. An element that is still at the layout's own spot is
    never judged for where it is (those spots are chosen to be clear); the progress bar and boxes behind other things are left alone. The same
    caption, headline, logo and button clashes that `problems` reports are not said twice."""
    shown = [e for e in (design.get("elements") or elements.from_legacy(design)) if e["visible"] and e["type"] in TEXT_KINDS]   # AI Generate checks the old sections
    found: list[str] = []
    for e in shown:
        name = e["name"].lower()
        size = e["style"].get("size") if e["type"] not in ("captions", "logo") else None
        floor = MIN_HANDLE if e["type"] == "handle" else MIN_TEXT
        if size is not None and size < floor:
            found.append(f"The {name} is too small to read on a phone: its text is {size} pixels tall, so make it at least {floor}")
    boxes = {e["id"]: _box(e, framing, logo_size) for e in shown}
    for e in shown:
        box = boxes[e["id"]]
        if box is None or (e["x"] is None and e["y"] is None):
            continue
        name = e["name"].lower()
        if _boxes_touch(box, BUTTONS):
            found.append(f"The {name} would be hidden behind the app's buttons on the right: move it left or up")
        elif box[3] > TITLE_BOTTOM:
            found.append(f"The {name} would be hidden behind the clip's title and description at the bottom: move it up")
        elif box[1] < TITLE_TOP:
            found.append(f"The {name} would be hidden behind the app's top bar: move it down")
    first = {k: next((e["id"] for e in shown if e["type"] == k), None) for k in LEGACY_KINDS}
    judged = {i for i in first.values() if i}
    solid = [e for e in shown if e["type"] != "handle"]   # the handle is a faint mark that is meant to sit under other things
    for i, a in enumerate(solid):
        for b in solid[i + 1:]:
            if a["id"] in judged and b["id"] in judged:
                continue   # `problems` already says it
            box_a, box_b = boxes[a["id"]], boxes[b["id"]]
            if box_a is None or box_b is None:
                continue
            ta, tb = _when(a, total, head_end), _when(b, total, head_end)
            if not _overlap(ta, tb) or not _boxes_touch(box_a, box_b):
                continue
            covered = (min(box_a[2], box_b[2]) - max(box_a[0], box_b[0])) * (min(box_a[3], box_b[3]) - max(box_a[1], box_b[1]))
            smaller = min((box_a[2] - box_a[0]) * (box_a[3] - box_a[1]), (box_b[2] - box_b[0]) * (box_b[3] - box_b[1]))
            if covered / max(smaller, 1) >= OVERLAP_SHARE:
                start, end = max(ta[0], tb[0]), min(ta[1], tb[1])
                who = f"The two {a['name'].lower()} elements" if a["name"].lower() == b["name"].lower() else f"The {a['name'].lower()} and the {b['name'].lower()}"
                found.append(f"{who} would be on top of each other from {start:.0f} to {end:.0f} seconds: move one of them")
    return found
