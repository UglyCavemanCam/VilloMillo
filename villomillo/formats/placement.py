"""Where the logo and the call to action may sit. They must never cover the captions or the headline.

Each is a box on the 1080x1920 picture, shown for a stretch of the clip. Two boxes that share a stretch of time and overlap
are refused, with the reason in plain words: the caller either refuses the design (when saving) or flags the clip (when making it).
"""

from __future__ import annotations

from . import designs

W, H = 1080, 1920
MARGIN = 60
CAPTION_HALF_HEIGHT = 100      # a caption group is about 200 pixels tall, centred on its line (an estimate, checked on real clips)
CTA_HEIGHT = 90
CTA_WIDTH = 0.8                # the call to action is at most this share of the frame's width
HEADLINE_LINE = 1.25           # a headline line is about this many times its font size, plus its padding
HEADLINE_PAD = 30
SAVE_HEADLINE_END = 7.0        # when saving we do not know the clip, so assume the headline may show for its longest


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
    return problems(design, design["layout"], size, total=designs.DEMO_LENGTH, head_end=SAVE_HEADLINE_END)
