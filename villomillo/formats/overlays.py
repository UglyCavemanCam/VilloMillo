"""The pictures laid over the video: the headline, text on a box, boxes, the logo and the handle.

Each is drawn once as a transparent image with the bundled fonts; ffmpeg places it and fades it in and out at the right
times (see render.py), so nothing is drawn frame by frame.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

from . import fonts

W = 1080


def rgb(hex_rgb: str) -> tuple[int, int, int]:
    h = hex_rgb.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def faded(img: Image.Image, opacity: int | None) -> Image.Image:
    """The picture at this opacity (percent). Fully opaque is returned untouched."""
    if opacity is None or opacity >= 100:
        return img
    img = img.copy()
    img.putalpha(img.getchannel("A").point(lambda v: round(v * opacity / 100)))
    return img


def headline_image(lines: list[str], d: dict) -> Image.Image:
    """The headline, one rounded box per line behind the words. `d` is the design's headline section."""
    texts = [t.upper() for t in lines[:2] if t.strip()]
    size, max_w = d["size"], int(W * d["width_pct"] / 100)
    while True:
        font = fonts.pil(d["font"], size)
        pad_x = int(size * 0.45)
        if max(font.getlength(t) for t in texts) + 2 * pad_x <= max_w or size <= 24:
            break
        size -= 2
    ascent, descent = font.getmetrics()
    pad_y, gap = int(size * 0.22), int(size * 0.18)
    box_h = ascent + descent + 2 * pad_y
    img = Image.new("RGBA", (W, len(texts) * box_h + (len(texts) - 1) * gap + 12), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    fg, bg = rgb(d["color"]), (rgb(d["background"]) if d["background"] else None)
    stroke = d.get("outline_w")
    stroke = (0 if bg else 4) if stroke is None else stroke   # unset: an outline only when there is no box behind the words
    stroke_fill = (*rgb(d.get("outline_color") or "#000000"), 255)
    left = (W - max_w) // 2
    for i, text in enumerate(texts):
        box_w = int(font.getlength(text) + 2 * pad_x)
        x = left if d["align"] == "left" else (W - box_w) // 2 if d["align"] == "center" else W - left - box_w
        y = i * (box_h + gap)
        if bg:
            draw.rounded_rectangle((x, y, x + box_w, y + box_h), radius=min(d["radius"], box_h // 2), fill=(*bg, 255))
        if d["shadow"]:
            draw.text((x + pad_x + 4, y + pad_y + 4), text, font=font, fill=(0, 0, 0, 170))
        draw.text((x + pad_x, y + pad_y), text, font=font, fill=(*fg, 255),
                  stroke_width=stroke, stroke_fill=stroke_fill)
    return faded(img, d.get("opacity"))


def box_image(text: str, st: dict) -> Image.Image:
    """A line of text on a rounded box, never wider than the frame. `st` is an element's style (elements.box_style): font, size,
    colours, corner radius (None is a pill), padding, outline, shadow and opacity. A style with no box draws the words alone."""
    size, pad_x, pad_y = st["size"], st["pad_x"], st["pad_y"]
    font = fonts.pil(st["font"], size)
    while font.getlength(text) + pad_x * 2 > W - 80 and size > 24:  # shrink to fit
        size -= 2
        font = fonts.pil(st["font"], size)
    w, h = int(font.getlength(text) + pad_x * 2), size + pad_y
    room = st["outline_w"] + (4 if st["shadow"] else 0)   # so an outline or a shadow is not cut off at the edge
    img = Image.new("RGBA", (w + 2 * room, h + 2 * room), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if st["box"]:
        radius = h // 2 if st["radius"] is None else min(st["radius"], h // 2)
        draw.rounded_rectangle((room, room, room + w - 1, room + h - 1), radius=radius, fill=(*rgb(st["box"]), 255))
    cx, cy = room + w / 2, room + h / 2 + 2
    if st["shadow"]:
        draw.text((cx + 4, cy + 4), text, font=font, fill=(0, 0, 0, 170), anchor="mm")
    draw.text((cx, cy), text, font=font, fill=(*rgb(st["color"]), 255), anchor="mm", stroke_width=st["outline_w"],
              stroke_fill=(*rgb(st["outline_color"]), 255))
    return faded(img, st["opacity"])


def pill_image(text: str, size: int, fill: str, ink: str, font_id: str = "montserrat-black", pad: int = 26) -> Image.Image:
    """A label on a rounded pill, never wider than the frame."""
    return box_image(text, {"font": font_id, "size": size, "color": ink, "box": fill, "radius": None, "pad_x": pad, "pad_y": pad,
                            "outline_color": "#000000", "outline_w": 0, "shadow": False, "opacity": 100})


def shape_image(w: int, h: int, st: dict) -> Image.Image:
    """A plain box: a colour, rounded corners and an outline."""
    room = st["outline_w"]
    img = Image.new("RGBA", (w + 2 * room, h + 2 * room), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((room, room, room + w - 1, room + h - 1), radius=min(st["radius"], h // 2, w // 2),
                                          fill=(*rgb(st["color"]), 255), outline=(*rgb(st["outline_color"]), 255) if room else None,
                                          width=room or 1)
    return faded(img, st["opacity"])


def logo_image(path: pathlib.Path, width: int, max_h: int = 260, opacity: int | None = None) -> Image.Image:
    """The logo at the width asked for, or no taller than max_h. An animated GIF shows its first frame."""
    img = Image.open(path).convert("RGBA")
    if round(img.height * width / img.width) > max_h:
        width = round(img.width * max_h / img.height)
    return faded(img.resize((width, round(img.height * width / img.width)), Image.LANCZOS), opacity)


def handle_image(handle: str, st: dict, alpha: int | None = None, size: int | None = None) -> Image.Image:
    """The channel handle, small and faint by default, with a hairline edge so it shows on light and dark shots. `st` is the
    handle element's style; `alpha` and `size` fill in what it leaves unset (the block's own defaults)."""
    alpha = round(st["opacity"] * 255 / 100) if st["opacity"] is not None else 38 if alpha is None else alpha
    font = fonts.pil(st["font"], st["size"] or size or 30)
    box = font.getbbox(handle)
    pad_x, pad_y = st["pad_x"], st["pad_y"]
    img = Image.new("RGBA", (box[2] - box[0] + 2 * pad_x, box[3] - box[1] + 2 * pad_y), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if st["box"]:
        radius = img.height // 2 if st["radius"] is None else min(st["radius"], img.height // 2)
        draw.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius, fill=(*rgb(st["box"]), alpha))
    draw.text((pad_x - box[0], pad_y - box[1]), handle, font=font, fill=(*rgb(st["color"]), alpha),
              stroke_width=st["outline_w"], stroke_fill=(*rgb(st["outline_color"]), alpha // 2))
    return img


def watermark_image(handle: str, alpha: int, size: int) -> Image.Image:
    """The channel handle, small and faint, with a hairline edge so it shows on light and dark shots."""
    return handle_image(handle, {"font": "montserrat-bold", "size": size, "color": "#FFFFFF", "opacity": None, "outline_color": "#000000",
                                 "outline_w": 1, "box": "", "radius": None, "pad_x": 8, "pad_y": 8}, alpha)
