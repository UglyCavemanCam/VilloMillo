"""Rendering a plan: read the source video, frame it, paint the text and brand layers, encode with the audio.

The look comes entirely from the plan (see blueprints.py and config/villomillo/blueprints.yaml). Every word on screen
is the speaker's own; the layers only arrange and label what was said.
"""

from __future__ import annotations

import functools
import pathlib
import shutil
import subprocess
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .blueprints import Plan

W, H = 1080, 1920
FONTS = pathlib.Path("C:/Windows/Fonts")
BLACK, BOLD = "ariblk.ttf", "segoeuib.ttf"
INK, AMBER, BONE = (23, 18, 14), (255, 176, 0), (244, 235, 221)   # the brand palette until real colours are chosen
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"


class RenderError(RuntimeError):
    pass


@dataclass
class ClipText:
    """What the text layers say. Written by formats/text.py and checked against the transcript."""
    title_lines: list[str] = field(default_factory=list)
    points: list[tuple[str, float]] = field(default_factory=list)    # (label, seconds into the clip)
    takeaway: str = ""

    def available(self, has_words: bool) -> set[str]:
        have = {"words"} if has_words else set()
        if self.title_lines:
            have.add("title")
        if self.points:
            have.add("points")
        if self.takeaway:
            have.add("takeaway")
        return have


@dataclass
class Job:
    source: pathlib.Path
    start: float
    end: float
    words: list[dict]                  # {"t0", "t1", "text"} in episode seconds
    text: ClipText
    plan: Plan
    handle: str
    out: pathlib.Path


@functools.lru_cache(maxsize=256)
def font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:  # not Windows, or the font is missing: a plain font still renders
        try:
            return ImageFont.load_default(size)
        except TypeError:
            return ImageFont.load_default()


def ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def window(t: float, a: float, b: float, fade: float = 0.25) -> float:
    """0..1 visibility of something shown from a to b, fading in and out."""
    return ease((t - a) / fade) * ease((b - t) / fade)


def title_seconds(lines: list[str], base: float, per_word: float, low: float, high: float) -> float:
    """A second to notice the title plus a little per word to read it, within limits."""
    words = sum(len(line.split()) for line in lines)
    return min(high, max(low, base + per_word * words))


# --- following the speaker --------------------------------------------------------------------------------------

class Face:
    """Where the speaker's face is, smoothed, sampled across the clip. With no face found it holds the middle."""

    def __init__(self, cap: cv2.VideoCapture, start: float, end: float):
        front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        prof = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        sw, sh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        ts, xs, ys, last = [], [], [], None
        for i in range(int((end - start + 1.0) * 8)):
            t = start - 0.5 + i / 8
            cap.set(cv2.CAP_PROP_POS_MSEC, max(t, 0) * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (960, 540)), cv2.COLOR_BGR2GRAY)
            faces = list(front.detectMultiScale(gray, 1.1, 5, minSize=(60, 60)))
            if not faces:
                faces = list(prof.detectMultiScale(gray, 1.1, 4, minSize=(60, 60)))
            if not faces:
                continue
            found = [((x + w / 2) * sw / 960, (y + h / 2) * sh / 540, w) for x, y, w, h in faces]
            pick = (min(found, key=lambda c: abs(c[0] - last[0]) + abs(c[1] - last[1])) if last
                    else max(found, key=lambda c: c[2]))
            last = pick
            ts.append(t), xs.append(pick[0]), ys.append(pick[1])
        self.default = (sw / 2, sh * 0.3)
        self.t = np.array(ts)
        self.x, self.y = (self._smooth(np.array(xs)), self._smooth(np.array(ys))) if ts else (None, None)

    @staticmethod
    def _smooth(v: np.ndarray) -> np.ndarray:
        med = np.array([np.median(v[max(0, i - 4): i + 5]) for i in range(len(v))])
        return np.convolve(np.pad(med, 4, mode="edge"), np.ones(9) / 9, mode="valid")

    def at(self, t: float) -> tuple[float, float]:
        if self.x is None:
            return self.default
        return float(np.interp(t, self.t, self.x)), float(np.interp(t, self.t, self.y))


# --- framing ----------------------------------------------------------------------------------------------------

def crop_to(frame: np.ndarray, cx: float, face_y: float, out_w: int, out_h: int, crop_w: float, face_at: float,
            left_limit: float = 0.0) -> np.ndarray:
    """A window of the source with the output's shape, the face `face_at` of the way down, never left of `left_limit`."""
    sh, sw = frame.shape[:2]
    crop_h = crop_w * out_h / out_w
    if crop_h > sh:
        crop_h, crop_w = sh, sh * out_w / out_h
    if crop_w > sw:
        crop_w, crop_h = sw, sw * out_h / out_w
    x0 = int(round(min(max(cx - crop_w / 2, left_limit), sw - crop_w)))
    y0 = int(round(min(max(face_y - face_at * crop_h, 0), sh - crop_h)))
    piece = frame[y0: y0 + int(crop_h), x0: x0 + int(crop_w)]
    return cv2.resize(piece, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)


def backdrop(frame: np.ndarray, cx: float, fy: float) -> np.ndarray:
    """A dark, heavily blurred copy of the shot to fill the frame behind the picture (BGR)."""
    tiny = crop_to(frame, cx, fy, 54, 96, frame.shape[0] * 9 / 16, 0.40)
    back = cv2.resize(cv2.GaussianBlur(tiny, (0, 0), 2), (W, H), interpolation=cv2.INTER_CUBIC).astype(np.float32)
    return (back * 0.42 + np.array(INK[::-1], np.float32) * 0.58).astype(np.uint8)


def frame_clean(frame, cx, fy, zoom, plan: Plan):
    y, h = plan.framing_param("window_y"), plan.framing_param("window_h")
    back = backdrop(frame, cx, fy)
    back[y: y + h] = crop_to(frame, cx, fy, W, h, plan.framing_param("crop_w") / zoom, plan.framing_param("face_at"),
                             plan.framing_param("keep_right_of", 0.0) * frame.shape[1])
    return back[:, :, ::-1].copy(), [y - 3, y + h - 1]


def frame_full(frame, cx, fy, zoom, plan: Plan):
    view = crop_to(frame, cx, fy, W, H, plan.framing_param("crop_w") / zoom, plan.framing_param("face_at"),
                   plan.framing_param("keep_right_of", 0.0) * frame.shape[1])
    return view[:, :, ::-1].copy(), []


def frame_blur(frame, cx, fy, zoom, plan: Plan):
    sh, sw = frame.shape[:2]
    back = backdrop(frame, sw / 2, sh * 0.3)
    h = min(H, int(W * sh / sw))
    zw, zh = sw / zoom, sh / zoom
    x0, y0 = int((sw - zw) / 2), int((sh - zh) / 2)
    back[(H - h) // 2: (H - h) // 2 + h] = cv2.resize(frame[y0: y0 + int(zh), x0: x0 + int(zw)], (W, h),
                                                     interpolation=cv2.INTER_AREA)
    return back[:, :, ::-1].copy(), []


FRAMING = {"clean_frame": frame_clean, "full_bleed_tracked": frame_full, "blur_fit": frame_blur}


def zoom_amount(plan: Plan, t: float, total: float, pulses: list[float]) -> float:
    if plan.has("slow_push"):
        return plan.param("slow_push", "amount") * t / total
    if plan.has("punch_ins"):
        length = plan.param("punch_ins", "length_s")
        return plan.param("punch_ins", "amount") * max([window(t, p, p + length, 0.35) for p in pulses] or [0.0])
    return 0.0


# --- text and brand layers --------------------------------------------------------------------------------------

def pill(d, cx: int, cy: int, text: str, size: int, fill, ink, alpha: float = 1.0, pad: int = 26) -> None:
    f = font(BLACK, size)
    while f.getlength(text) + pad * 2 > W - 80 and size > 24:  # never wider than the frame
        size -= 2
        f = font(BLACK, size)
    w, h = f.getlength(text) + pad * 2, size + pad
    a = int(255 * alpha)
    d.rounded_rectangle((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), radius=h // 2, fill=(*fill, a))
    d.text((cx, cy + 2), text, font=f, fill=(*ink, a), anchor="mm")


def groups_of(words: list[dict], size: int) -> list[dict]:
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


def key_words(text: ClipText) -> set[str]:
    out = set()
    for line in [*text.title_lines, *(label for label, _ in text.points)]:
        out |= {w for w in "".join(c.lower() if c.isalnum() else " " for c in line).split() if len(w) >= 4}
    return out


def paint_captions(d, groups, t, y, size, pop: bool, keys: set[str]) -> None:
    group = next((g for g in groups if g["o0"] - 0.02 <= t <= g["o1"]), None)
    if group is None:
        return
    texts = [w["text"].upper() for w in group["words"]]
    widths = [font(BLACK, size).getlength(s + " ") for s in texts]
    while sum(widths) > 980 and size > 48:
        size -= 4
        widths = [font(BLACK, size).getlength(s + " ") for s in texts]
    space = font(BLACK, size).getlength(" ")
    x = (W - (sum(widths) - space)) / 2
    for w, s, wd in zip(group["words"], texts, widths):
        active = w["o0"] - 0.02 <= t <= w["o1"] + 0.04
        key = "".join(c for c in w["text"].lower() if c.isalnum()) in keys
        if pop:
            grow = 1.06 + 0.12 * max(0.0, 1 - (t - w["o0"]) / 0.15) if active else 1.0
            colour = AMBER if key else ((255, 255, 255) if active else BONE)
            f = font(BLACK, int(size * grow))
        else:
            colour, f = (AMBER if active else BONE), font(BLACK, size)
        d.text((x + (wd - space) / 2, y), s, font=f, fill=(*colour, 255), anchor="mm",
               stroke_width=10 if pop else 9, stroke_fill=(*INK, 255))
        x += wd


def paint_title(d, t, show_s, y0, lines) -> None:
    v = window(t, -0.3, show_s, 0.3)
    if v <= 0:
        return
    a = int(255 * v)
    box = 170 if len(lines) > 1 else 105
    d.rounded_rectangle((60, y0, W - 60, y0 + box), radius=30, fill=(*INK, int(225 * v)))
    d.rectangle((60, y0, 74, y0 + box), fill=(*AMBER, a))
    for k, line in enumerate(lines[:2]):
        d.text((100, y0 + (45 if len(lines) > 1 else 52) + k * 77), line.upper(), font=font(BLACK, 43),
               fill=(*(BONE if k == 0 else AMBER), a), anchor="lm")


def paint_chips(d, t, total, y, points) -> None:
    for i, (label, start) in enumerate(points):
        end = points[i + 1][1] - 0.3 if i + 1 < len(points) else total
        v = window(t, start, end, 0.3)
        if v > 0:
            pill(d, W // 2, y, f"{i + 1}  ·  {label.upper()}", 40, AMBER, INK, v)


def paint_brand(d, plan: Plan, t: float, total: float, handle: str) -> None:
    if plan.has("watermark_center") and handle:
        alpha = int(plan.param("watermark_center", "alpha"))
        d.text((W // 2, H // 2), handle, font=font(BOLD, int(plan.param("watermark_center", "size"))),
               fill=(255, 255, 255, alpha), anchor="mm", stroke_width=1, stroke_fill=(0, 0, 0, alpha // 2))


def paint_bar(d, plan: Plan, t: float, total: float) -> None:
    if plan.has("progress_bar_bottom"):
        h = int(plan.param("progress_bar_bottom", "height"))
        d.rectangle((0, H - h, W, H), fill=(*BONE, 70))
        d.rectangle((0, H - h, int(W * min(1.0, t / total)), H), fill=(*AMBER, 255))


def composite(frame_rgb: np.ndarray, paint) -> np.ndarray:
    base = Image.fromarray(frame_rgb).convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer))
    return np.array(Image.alpha_composite(base, layer).convert("RGB"))


# --- the render -------------------------------------------------------------------------------------------------

def render(job: Job) -> pathlib.Path:
    plan, text = job.plan, job.text
    cap = cv2.VideoCapture(str(job.source))
    if not cap.isOpened():
        raise RenderError(f"cannot open the source video {job.source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    n = int(round((job.end - job.start) * fps))
    total = n / fps
    framing = FRAMING[plan.framing().id]
    face = Face(cap, job.start, job.end) if plan.framing().id != "blur_fit" else None

    rel = [{**w, "o0": w["t0"] - job.start, "o1": w["t1"] - job.start} for w in job.words]
    captions = "captions_pop" if plan.has("captions_pop") else "captions_plain" if plan.has("captions_plain") else None
    groups = groups_of(rel, plan.param(captions, "words_per_group")) if captions else []
    keys = key_words(text)
    show_s = (title_seconds(text.title_lines, *(plan.param("hook_title", k) for k in
                                                ("base_s", "per_word_s", "min_s", "max_s")))
              if plan.has("hook_title") else 0.0)
    pulses = [1.3] + [at for _, at in text.points]
    title_y, row_y, caption_y = (plan.framing_param(k) for k in ("title_y", "row_y", "caption_y"))

    def paint(d, t, edges):
        paint_brand(d, plan, t, total, job.handle)
        for y in edges:
            d.rectangle((0, y, W, y + 4), fill=(*AMBER, 255))
        if plan.has("hook_title"):
            paint_title(d, t, show_s, title_y, text.title_lines)
        tk = window(t, total - 2.6, total, 0.3) if plan.has("takeaway") and total > show_s + 3 else 0.0
        if tk > 0:
            pill(d, W // 2, row_y, text.takeaway.upper(), 46, AMBER, INK, tk, pad=34)
        elif plan.has("point_chips") and t > show_s - 0.2:
            paint_chips(d, t, total, row_y, text.points)
        if captions:
            paint_captions(d, groups, t, caption_y, plan.param(captions, "size"), captions == "captions_pop", keys)
        paint_bar(d, plan, t, total)

    def frames():
        cap.set(cv2.CAP_PROP_POS_MSEC, job.start * 1000)
        for i in range(n):
            ok, frame = cap.read()
            if not ok:
                return
            t = i / fps
            cx, fy = face.at(job.start + t) if face else (0.0, 0.0)
            rgb, edges = framing(frame, cx, fy, 1 + zoom_amount(plan, t, total, pulses), plan)
            yield composite(rgb, lambda d, t=t, edges=edges: paint(d, t, edges))

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RenderError("ffmpeg is not on PATH")
    job.out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", f"{fps}",
           "-i", "pipe:0", "-ss", f"{job.start:.3f}", "-t", f"{total:.3f}", "-i", str(job.source),
           "-map", "0:v", "-map", "1:a:0?", "-af", LOUDNESS,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(job.out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for frame in frames():
            proc.stdin.write(frame.tobytes())
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()
        stderr = proc.stderr.read().decode(errors="replace")
        proc.wait()
        cap.release()
    if proc.returncode:
        raise RenderError(f"ffmpeg failed: {stderr[-400:]}")
    return job.out
