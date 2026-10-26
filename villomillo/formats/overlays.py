"""The pictures laid over the video: the headline, the point labels, the takeaway and the watermark.

Each is drawn once as a transparent image with the bundled fonts; ffmpeg places it and fades it in and out at the right
times (see render.py), so nothing is drawn frame by frame.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from . import fonts

W = 1080


def rgb(hex_rgb: str) -> tuple[int, int, int]:
    h = hex_rgb.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


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
                  stroke_width=0 if bg else 4, stroke_fill=(0, 0, 0, 255))
    return img


def pill_image(text: str, size: int, fill: str, ink: str, font_id: str = "montserrat-black", pad: int = 26) -> Image.Image:
    """A label on a rounded pill, never wider than the frame."""
    font = fonts.pil(font_id, size)
    while font.getlength(text) + pad * 2 > W - 80 and size > 24:  # shrink to fit
        size -= 2
        font = fonts.pil(font_id, size)
    w, h = int(font.getlength(text) + pad * 2), size + pad
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(*rgb(fill), 255))
    draw.text((w / 2, h / 2 + 2), text, font=font, fill=(*rgb(ink), 255), anchor="mm")
    return img


def logo_image(path: pathlib.Path, width: int, max_h: int = 260) -> Image.Image:
    """The logo at the width asked for, or no taller than max_h. An animated GIF shows its first frame."""
    img = Image.open(path).convert("RGBA")
    if round(img.height * width / img.width) > max_h:
        width = round(img.width * max_h / img.height)
    return img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)


def watermark_image(handle: str, alpha: int, size: int) -> Image.Image:
    """The channel handle, small and faint, with a hairline edge so it shows on light and dark shots."""
    font = fonts.pil("montserrat-bold", size)
    box = font.getbbox(handle)
    img = Image.new("RGBA", (box[2] - box[0] + 16, box[3] - box[1] + 16), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((8 - box[0], 8 - box[1]), handle, font=font, fill=(255, 255, 255, alpha),
                             stroke_width=1, stroke_fill=(0, 0, 0, alpha // 2))
    return img
