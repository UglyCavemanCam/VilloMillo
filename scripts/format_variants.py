"""Variations of Option A (the Editorial Clip) for clip 2, rendered from the 1440p source. A design tool only.

Every variation shares these fixes: no watermark, a taller progress bar flush with the bottom edge, and a title that
stays up for as long as its length needs. Each then improves one thing:

  A1  Pop captions   caption-led: two-word captions that pop, key words always in amber, punch-ins on key words
  A2  Clean frame    layout-led: speaker in a window over a blurred backdrop; nothing is written over his face
  A3  Checklist      structure-led: both skills shown up front as a row that lights up as he names each

Run from the project root:  .venv\\Scripts\\python.exe scripts\\format_variants.py
"""

from __future__ import annotations

import functools
import re
import sys
from dataclasses import dataclass

import cv2
import numpy as np
from format_preview import (
    AMBER,
    BLACK,
    BONE,
    E0,
    FPS,
    HANDLE,
    INK,
    OUT,
    S0,
    SRC,
    Face,
    H,
    W,
    composite,
    crop_to,
    ease,
    encode,
    font,
    load_words,
    pill,
    read_frames,
    window,
)

TITLE = ["FEEL LIKE DOORS STAY CLOSED?", "BUILD THESE TWO SKILLS"]
TAKEAWAY = "SKILLS CAN EXPAND YOUR LUCK"
SKILLS = [("SOCIAL SKILLS", 9.3), ("AN OPEN MIND", 24.8)]    # named by him at these seconds into the clip
KEYWORDS = {"luck", "skills", "skill", "doors", "closed", "open", "mind", "research", "social", "people", "expand"}
BAR_HEIGHT = 14
WATERMARK_ALPHA = 38                  # of 255 (about 15%): faint, but still just readable on light and dark shots
CAPTION_Y = 1345                      # above the bottom 480 px that Shorts and Reels cover


@functools.lru_cache(maxsize=256)
def cached_font(name: str, size: int):
    return font(name, size)


def title_seconds(*lines: str) -> float:
    """How long a title stays up: a second to notice it, plus about 0.4 s per word to read it."""
    words = sum(len(line.split()) for line in lines)
    return min(7.0, max(2.5, 1.0 + 0.4 * words))


def clean(word: str) -> str:
    return re.sub(r"[^a-z']", "", word.lower())


# --- drawing ---------------------------------------------------------------------------------------------

def draw_title(d, t: float, show_s: float, y0: int) -> None:
    v = window(t, -0.3, show_s, 0.3)
    if v <= 0:
        return
    a = int(255 * v)
    d.rounded_rectangle((60, y0, W - 60, y0 + 170), radius=30, fill=(*INK, int(225 * v)))
    d.rectangle((60, y0, 74, y0 + 170), fill=(*AMBER, a))
    d.text((100, y0 + 45), TITLE[0], font=cached_font(BLACK, 43), fill=(*BONE, a), anchor="lm")
    d.text((100, y0 + 122), TITLE[1], font=cached_font(BLACK, 43), fill=(*AMBER, a), anchor="lm")


def draw_watermark(d) -> None:
    """A small mark in the middle of the frame, faint enough not to spoil the clip, hard to crop out."""
    d.text((W // 2, H // 2), HANDLE, font=cached_font("segoeuib.ttf", 30), fill=(255, 255, 255, WATERMARK_ALPHA),
           anchor="mm", stroke_width=1, stroke_fill=(0, 0, 0, WATERMARK_ALPHA // 2))


def progress_bar(d, t: float, total: float) -> None:
    y = H - BAR_HEIGHT
    d.rectangle((0, y, W, H), fill=(*BONE, 70))
    d.rectangle((0, y, int(W * min(1.0, t / total)), H), fill=(*AMBER, 255))


def make_groups(words: list[dict], size: int) -> list[dict]:
    """Caption groups of `size` words, never across a sentence end or a long gap."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) == size or w["o0"] - cur[-1]["o1"] > 0.5):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith((".", "?", "!")):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [{"words": g, "o0": g[0]["o0"], "o1": g[-1]["o1"] + 0.12} for g in groups]


def draw_captions(d, groups, t: float, *, pop: bool) -> None:
    group = next((g for g in groups if g["o0"] - 0.02 <= t <= g["o1"]), None)
    if group is None:
        return
    size = 88 if pop else 78
    texts = [w["text"].upper() for w in group["words"]]
    widths = [cached_font(BLACK, size).getlength(s + " ") for s in texts]
    while sum(widths) > 980 and size > 48:
        size -= 4
        widths = [cached_font(BLACK, size).getlength(s + " ") for s in texts]
    base = cached_font(BLACK, size)
    space = base.getlength(" ")
    x = (W - (sum(widths) - space)) / 2
    for w, s, wd in zip(group["words"], texts, widths):
        active = w["o0"] - 0.02 <= t <= w["o1"] + 0.04
        key = clean(w["text"]) in KEYWORDS
        if pop:
            grow = 1.06 + 0.12 * max(0.0, 1 - (t - w["o0"]) / 0.15) if active else 1.0
            colour = AMBER if key else ((255, 255, 255) if active else BONE)
            f = cached_font(BLACK, int(size * grow))
        else:
            colour = AMBER if active else BONE
            f = base
        d.text((x + (wd - space) / 2, CAPTION_Y), s, font=f, fill=(*colour, 255), anchor="mm",
               stroke_width=10 if pop else 9, stroke_fill=(*INK, 255))
        x += wd


def draw_chips(d, t: float, total: float, y: int) -> None:
    spans = [(SKILLS[0][1], SKILLS[1][1] - 0.3, f"SKILL 1  ·  {SKILLS[0][0]}"),
             (SKILLS[1][1], total, f"SKILL 2  ·  {SKILLS[1][0]}")]
    for a0, a1, label in spans:
        v = window(t, a0, a1, 0.3)
        if v > 0:
            pill(d, W // 2, y, label, 40, AMBER, INK, v)


def draw_checklist(d, t: float, y: int, start: float) -> None:
    """Both skills shown up front, dim, and lit amber as he names each."""
    v = ease((t - start) / 0.4)
    if v <= 0:
        return
    f = cached_font(BLACK, 34)
    labels = [f"{n + 1}  {name}" for n, (name, _) in enumerate(SKILLS)]
    widths = [f.getlength(s) + 60 for s in labels]
    x = (W - (sum(widths) + 22)) / 2
    for (label, (_, when)), wd in zip(zip(labels, SKILLS), widths):
        named = ease((t - when) / 0.3) if t >= when else 0.0
        d.rounded_rectangle((x, y - 36, x + wd, y + 36), radius=36, fill=(*INK, int(210 * v)),
                            outline=(*BONE, int(130 * v)), width=3)
        if named > 0:
            d.rounded_rectangle((x, y - 36, x + wd, y + 36), radius=36, fill=(*AMBER, int(255 * named)))
        d.text((x + wd / 2, y + 2), label, font=f, fill=(*(INK if named > 0.5 else BONE), int(255 * max(v, 0.0))), anchor="mm")
        x += wd + 22


# --- the variations --------------------------------------------------------------------------------------

@dataclass
class Style:
    name: str
    layout: str = "full"        # full-screen crop, or a window over a blurred backdrop
    pop: bool = False           # two-word pop captions with key words in amber
    zoom: str = "pulses"        # pulses on the emphasis points, "keywords", or one "slow" push-in
    row: str = "chips"          # skill chips, or the checklist row
    watermark: bool = False     # the faint centre mark


STYLES = [
    Style("clip2_A_fixed"),
    Style("clip2_A1_pop_captions", pop=True, zoom="keywords"),
    Style("clip2_A2_clean_frame", layout="framed", zoom="slow"),
    Style("clip2_A3_checklist", row="checklist"),
    Style("clip2_A2_clean_frame_wm", layout="framed", zoom="slow", watermark=True),
]
WIN_Y, WIN_H = 500, 1000


def zoom_amount(style: Style, t: float, total: float, words: list[dict]) -> float:
    if style.zoom == "slow":
        return 0.07 * t / total
    if style.zoom == "keywords":
        times, last = [], -9.0
        for w in words:
            if clean(w["text"]) in {"luck", "skills", "doors", "open", "mind"} and w["o0"] - last >= 2.2 and w["o0"] > 0.8:
                times.append(w["o0"])
                last = w["o0"]
        return 0.05 * max([window(t, p - 0.1, p + 1.0, 0.25) for p in times] or [0.0])
    return 0.06 * max(window(t, p, p + 2.2, 0.35) for p in (1.3, 9.3, 17.5, 24.8))


def framed_view(frame: np.ndarray, fx: float, fy: float, zoom: float) -> np.ndarray:
    """The speaker in a window over a dark, blurred copy of the same shot. Returns RGB."""
    tiny = crop_to(frame, fx, fy, 54, 96, 810, 0.40)
    back = cv2.resize(cv2.GaussianBlur(tiny, (0, 0), 2), (W, H), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    back = (back * 0.42 + np.array(INK[::-1], np.float32) * 0.58).astype(np.uint8)
    back[WIN_Y: WIN_Y + WIN_H] = crop_to(frame, fx, fy, W, WIN_H, 1000 / zoom, 0.47)
    return back[:, :, ::-1].copy()


def render(face: Face, words: list[dict], style: Style):
    total = E0 - S0
    n = int(round(total * FPS))
    out = [{**w, "o0": w["t0"] - S0, "o1": w["t1"] - S0} for w in words]
    groups = make_groups(out, 2 if style.pop else 3)
    show_s = title_seconds(*TITLE)
    framed = style.layout == "framed"
    title_y, row_y = (290, 375) if framed else (1085, 1170)
    cap = cv2.VideoCapture(str(SRC))

    def paint(d, t):
        if framed:
            d.rectangle((0, WIN_Y - 3, W, WIN_Y + 1), fill=(*AMBER, 255))
            d.rectangle((0, WIN_Y + WIN_H - 1, W, WIN_Y + WIN_H + 3), fill=(*AMBER, 255))
        if style.watermark:
            draw_watermark(d)
        draw_title(d, t, show_s, title_y)
        tk = window(t, total - 2.6, total, 0.3)
        if tk > 0:
            pill(d, W // 2, row_y, TAKEAWAY, 46, AMBER, INK, tk, pad=34)
        elif style.row == "checklist":
            draw_checklist(d, t, row_y, show_s - 0.2)
        elif t > show_s - 0.2:
            draw_chips(d, t, total, row_y)
        draw_captions(d, groups, t, pop=style.pop)
        progress_bar(d, t, total)

    def frames():
        for i, frame in enumerate(read_frames(cap, S0, n)):
            t = i / FPS
            zoom = 1 + zoom_amount(style, t, total, out)
            fx, fy = face.at(S0 + t)
            if framed:
                rgb = framed_view(frame, fx, fy, zoom)
            else:
                rgb = crop_to(frame, fx, fy, W, H, 810 / zoom, 0.40)[:, :, ::-1].copy()
            yield composite(rgb, lambda d, t=t: paint(d, t))

    path = OUT / f"{style.name}.mp4"
    encode(frames(), [(S0, n / FPS)], path)
    cap.release()
    return path


def main() -> None:
    words = load_words()
    face = Face()
    print("title stays up for", round(title_seconds(*TITLE), 1), "s")
    wanted = sys.argv[1:]  # optionally name the variations to render
    for style in STYLES:
        if not wanted or style.name in wanted:
            print(render(face, words, style))


if __name__ == "__main__":
    sys.exit(main())
