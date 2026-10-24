"""Preview renders of one clip in two candidate formats, from the 1440p source. A throwaway design tool:
nothing here is used by the pipeline yet. Run from the project root:

    .venv\\Scripts\\python.exe scripts\\format_preview.py

Writes clip2_optionA.mp4 and clip2_optionB.mp4 to data/villomillo/previews/.

Option A (Editorial Clip): the full-screen face-tracked crop, plus an editorial layer on top of the footage.
Option B (Story Cut): a cold open, pauses trimmed, jump cuts hidden by alternating zooms, and a stacked layout
with a persistent headline and step cards above the speaker.
Everything on screen comes from the episode: the words are Mark's own, cards only name what he names.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "downloads" / "UDO8FWVDR5g.mp4"
TRANSCRIPT = ROOT / "data" / "transcripts" / "UDO8FWVDR5g.json"
OUT = ROOT / "data" / "villomillo" / "previews"
FONTS = pathlib.Path("C:/Windows/Fonts")

FPS = 24
W, H = 1080, 1920
S0, E0 = 593.45, 622.45          # the clip, in the episode
SHOW_TEXT_EDGE = 1040            # the show's own on-screen text ends left of this x in the 1440p source
COLD = (602.85, 606.30)          # the cold open: "developing good social skills can expand your earth luck."
HANDLE = "@cavemancam"
HEADLINE = ["FEEL LIKE DOORS", "STAY CLOSED?"]
HEADLINE_SUB = "BUILD THESE TWO SKILLS"
TAKEAWAY = "SKILLS CAN EXPAND YOUR LUCK"
# Placeholder brand colours until real ones are chosen.
INK, AMBER, BONE = (23, 18, 14), (255, 176, 0), (244, 235, 221)


def font(name: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / name), size)


BLACK = "ariblk.ttf"
BOLD = "segoeuib.ttf"


def ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def window(t: float, a: float, b: float, fade: float = 0.25) -> float:
    """0..1 visibility of something shown from a to b, fading in and out."""
    return ease((t - a) / fade) * ease((b - t) / fade)


# --- transcript and face track -------------------------------------------------------------------------

def load_words() -> list[dict]:
    data = json.loads(TRANSCRIPT.read_text(encoding="utf-8"))
    words = [w for s in data["segments"] for w in s["words"] if w["start"] >= S0 - 0.02 and w["end"] <= E0 + 0.1]
    return [{"t0": w["start"], "t1": w["end"], "text": w["word"].strip()} for w in words]


def track_face() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Smoothed face centre (x, y) in source pixels, sampled at the source frame rate across the clip."""
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    prof = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
    cap = cv2.VideoCapture(str(SRC))
    ts, xs, ys = [], [], []
    last = None
    n = int((E0 - S0 + 1.0) * 8)
    for i in range(n):
        t = S0 - 0.5 + i / 8
        cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
        ok, frame = cap.read()
        if not ok:
            continue
        gray = cv2.cvtColor(cv2.resize(frame, (960, 540)), cv2.COLOR_BGR2GRAY)
        faces = list(front.detectMultiScale(gray, 1.1, 5, minSize=(60, 60)))
        if not faces:
            faces = list(prof.detectMultiScale(gray, 1.1, 4, minSize=(60, 60)))
        if not faces:
            continue
        centres = [((x + w / 2) * 2560 / 960, (y + h / 2) * 1440 / 540, w) for x, y, w, h in faces]
        pick = min(centres, key=lambda c: abs(c[0] - last[0]) + abs(c[1] - last[1])) if last else max(centres, key=lambda c: c[2])
        last = pick
        ts.append(t), xs.append(pick[0]), ys.append(pick[1])
    cap.release()
    ts, xs, ys = np.array(ts), np.array(xs), np.array(ys)

    def smooth(v: np.ndarray) -> np.ndarray:
        med = np.array([np.median(v[max(0, i - 4): i + 5]) for i in range(len(v))])
        k = np.ones(9) / 9
        pad = np.pad(med, 4, mode="edge")
        return np.convolve(pad, k, mode="valid")

    return ts, smooth(xs), smooth(ys)


class Face:
    def __init__(self):
        self.t, self.x, self.y = track_face()

    def at(self, t: float) -> tuple[float, float]:
        return float(np.interp(t, self.t, self.x)), float(np.interp(t, self.t, self.y))


# --- layout geometry ------------------------------------------------------------------------------------

def crop_to(frame: np.ndarray, cx: float, face_y: float, out_w: int, out_h: int, crop_w: float, face_at: float) -> np.ndarray:
    """Crop a window of the source, width crop_w, aspect of the output, with the face `face_at` of the way down."""
    crop_h = crop_w * out_h / out_w
    sh, sw = frame.shape[:2]
    if crop_h > sh:
        crop_h = sh
        crop_w = crop_h * out_w / out_h
    x0 = int(round(min(max(cx - crop_w / 2, SHOW_TEXT_EDGE), sw - crop_w)))
    y0 = int(round(min(max(face_y - face_at * crop_h, 0), sh - crop_h)))
    piece = frame[y0: y0 + int(crop_h), x0: x0 + int(crop_w)]
    return cv2.resize(piece, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)


# --- overlay drawing ------------------------------------------------------------------------------------

def caption_groups(words: list[dict]) -> list[dict]:
    """Group output-timeline words, three at a time, never across a sentence end or a long gap."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) == 3 or w["o0"] - cur[-1]["o1"] > 0.5):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith((".", "?", "!")):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return [{"words": g, "o0": g[0]["o0"], "o1": g[-1]["o1"] + 0.12} for g in groups]


def draw_captions(layer: ImageDraw.ImageDraw, groups: list[dict], t: float, y: int = 1290) -> None:
    group = next((g for g in groups if g["o0"] - 0.02 <= t <= g["o1"]), None)
    if group is None:
        return
    size = 78
    f = font(BLACK, size)
    texts = [w["text"].upper() for w in group["words"]]
    widths = [f.getlength(s + " ") for s in texts]
    while sum(widths) > 960 and size > 50:
        size -= 4
        f = font(BLACK, size)
        widths = [f.getlength(s + " ") for s in texts]
    x = (W - (sum(widths) - f.getlength(" "))) / 2
    for w, s, wd in zip(group["words"], texts, widths):
        active = w["o0"] - 0.02 <= t <= w["o1"] + 0.04
        layer.text((x, y), s, font=f, fill=(*AMBER, 255) if active else (*BONE, 255), anchor="lm",
                   stroke_width=9, stroke_fill=(*INK, 255))
        x += wd


def pill(layer: ImageDraw.ImageDraw, cx: int, cy: int, text: str, size: int, fill, ink, alpha: float = 1.0, pad: int = 26) -> None:
    f = font(BLACK, size)
    w = f.getlength(text) + pad * 2
    h = size + pad
    a = int(255 * alpha)
    layer.rounded_rectangle((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), radius=h // 2, fill=(*fill, a))
    layer.text((cx, cy + 2), text, font=f, fill=(*ink, a), anchor="mm")


def handle_and_bar(layer: ImageDraw.ImageDraw, t: float, total: float, bar_y: int, handle_y: int = 262) -> None:
    layer.text((54, handle_y), HANDLE, font=font(BOLD, 36), fill=(*BONE, 190), anchor="lm",
               stroke_width=4, stroke_fill=(*INK, 150))
    layer.rectangle((0, bar_y, W, bar_y + 8), fill=(*BONE, 60))
    layer.rectangle((0, bar_y, int(W * min(1.0, t / total)), bar_y + 8), fill=(*AMBER, 255))


def composite(frame_rgb: np.ndarray, paint) -> np.ndarray:
    base = Image.fromarray(frame_rgb).convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    paint(ImageDraw.Draw(layer))
    return np.array(Image.alpha_composite(base, layer).convert("RGB"))


# --- encoding -------------------------------------------------------------------------------------------

def encode(frames_iter, audio_pieces: list[tuple[float, float] | float], out_path: pathlib.Path) -> None:
    """Pipe frames to FFmpeg with the audio pieces joined in order. A piece is (source start, length) or a gap (seconds)."""
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "pipe:0"]
    chains, labels = [], []
    k = 0
    for piece in audio_pieces:
        if isinstance(piece, tuple):
            start, length = piece
            cmd += ["-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", str(SRC)]
            chains.append(f"[{k + 1}:a]aresample=48000,aformat=channel_layouts=stereo,"
                          f"afade=t=in:d=0.008,afade=t=out:st={max(length - 0.008, 0):.3f}:d=0.008[a{k}]")
        else:
            cmd += ["-f", "lavfi", "-t", f"{piece:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            chains.append(f"[{k + 1}:a]aformat=channel_layouts=stereo[a{k}]")
        labels.append(f"[a{k}]")
        k += 1
    graph = ";".join(chains) + f";{''.join(labels)}concat=n={len(labels)}:v=0:a=1[aout]"
    cmd += ["-filter_complex", graph, "-map", "0:v", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for frame in frames_iter:
            proc.stdin.write(frame.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()
    if proc.returncode:
        raise RuntimeError("ffmpeg failed")


def read_frames(cap: cv2.VideoCapture, start: float, n: int):
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    for _ in range(n):
        ok, frame = cap.read()
        if not ok:
            return
        yield frame


# --- option A -------------------------------------------------------------------------------------------

def render_a(face: Face, words: list[dict]) -> pathlib.Path:
    total = E0 - S0
    n = int(round(total * FPS))
    out = [{**w, "o0": w["t0"] - S0, "o1": w["t1"] - S0} for w in words]
    groups = caption_groups(out)
    pulses = [1.3, 9.3, 17.5, 24.8]            # punch-ins on the emphasis points
    chips = [(9.3, 24.5, "SKILL 1  ·  SOCIAL SKILLS"), (24.8, total, "SKILL 2  ·  AN OPEN MIND")]
    cap = cv2.VideoCapture(str(SRC))

    def frames():
        for i, frame in enumerate(read_frames(cap, S0, n)):
            t = i / FPS
            zoom = 1 + 0.06 * max([window(t, p, p + 2.2, 0.35) for p in pulses])
            fx, fy = face.at(S0 + t)
            view = crop_to(frame, fx, fy, W, H, 810 / zoom, 0.40)
            rgb = view[:, :, ::-1].copy()

            def paint(d, t=t):
                hook = window(t, -0.3, 2.7, 0.3)
                if hook > 0:
                    a = int(255 * hook)
                    d.rounded_rectangle((60, 1085, W - 60, 1255), radius=30, fill=(*INK, int(225 * hook)))
                    d.rectangle((60, 1085, 74, 1255), fill=(*AMBER, a))
                    d.text((100, 1130), " ".join(HEADLINE), font=font(BLACK, 43), fill=(*BONE, a), anchor="lm")
                    d.text((100, 1207), HEADLINE_SUB, font=font(BLACK, 43), fill=(*AMBER, a), anchor="lm")
                for a0, a1, label in chips:
                    v = window(t, a0, a1, 0.3)
                    if v > 0 and t > 2.9:
                        pill(d, W // 2, 1170, label, 40, AMBER, INK, v)
                tk = window(t, total - 2.6, total, 0.3)
                if tk > 0:
                    pill(d, W // 2, 1170, TAKEAWAY, 46, AMBER, INK, tk, pad=34)
                draw_captions(d, groups, t, y=1345)
                handle_and_bar(d, t, total, 1424)

            yield composite(rgb, paint)

    out_path = OUT / "clip2_optionA.mp4"
    encode(frames(), [(S0, n / FPS)], out_path)
    cap.release()
    return out_path


# --- option B -------------------------------------------------------------------------------------------

def plan_pieces(words: list[dict]) -> list[dict]:
    """Cold open, a rewind beat, then the clip with pauses over 0.4 s cut out (cuts only fall in gaps)."""
    pieces = [{"kind": "cold", "a": COLD[0], "b": COLD[1]}, {"kind": "rewind", "dur": 0.5}]
    spans = [[w["t0"] - 0.06, w["t1"] + 0.10] for w in words]
    merged = [spans[0]]
    for s in spans[1:]:
        if s[0] - merged[-1][1] <= 0.40:
            merged[-1][1] = max(merged[-1][1], s[1])
        else:
            merged.append(s)
    for k, (a, b) in enumerate(merged):
        pieces.append({"kind": "main", "a": a, "b": b, "k": k})
    out = 0.0
    for p in pieces:
        p["n"] = int(round((p["dur"] if p["kind"] == "rewind" else p["b"] - p["a"]) * FPS))
        p["o"] = out
        out += p["n"] / FPS
    return pieces


def render_b(face: Face, words: list[dict]) -> pathlib.Path:
    pieces = plan_pieces(words)
    total = sum(p["n"] for p in pieces) / FPS
    # words on the output timeline (cold open words and main-body words)
    out = []
    for p in pieces:
        if p["kind"] == "rewind":
            continue
        for w in words:
            if w["t0"] >= p["a"] - 0.01 and w["t1"] <= p["b"] + 0.01:
                out.append({**w, "o0": p["o"] + (w["t0"] - p["a"]), "o1": p["o"] + (w["t1"] - p["a"])})
    groups = caption_groups(out)

    def main_time(text: str) -> float:
        for p in pieces:
            if p["kind"] != "main":
                continue
            for w in words:
                if w["text"].lower().startswith(text) and p["a"] - 0.01 <= w["t0"] <= p["b"]:
                    return p["o"] + (w["t0"] - p["a"])
        raise ValueError(text)

    cards = [(main_time("developing"), "Social skills"), (main_time("another"), "An open mind")]
    cap = cv2.VideoCapture(str(SRC))
    first = None

    first = next(read_frames(cap, pieces[2]["a"], 1))  # the rewind beat holds the first frame of the clip
    bounds = [w["t0"] for prev, w in zip(words, words[1:]) if prev["text"].endswith((".", "?", "!"))]
    bounds = [b for b in bounds if b > S0 + 0.5]
    ctx = (total, cards, groups, face, bounds)

    def frames():
        for p in pieces:
            if p["kind"] == "rewind":
                for i in range(p["n"]):
                    yield paint_b(p["o"] + i / FPS, first, S0, p, i, ctx, rewind=True)
                continue
            for i, frame in enumerate(read_frames(cap, p["a"], p["n"])):
                yield paint_b(p["o"] + i / FPS, frame, p["a"] + i / FPS, p, i, ctx)

    out_path = OUT / "clip2_optionB.mp4"
    audio = [(p["a"], p["n"] / FPS) if p["kind"] != "rewind" else p["n"] / FPS for p in pieces]
    encode(frames(), audio, out_path)
    cap.release()
    return out_path


def paint_b(t, frame, src_t, piece, i, ctx, rewind=False):
    total, cards, groups, face, bounds = ctx
    # Punch-in cuts: the zoom flips at every sentence and at every trimmed pause, so cuts never look like jumps.
    flips = sum(src_t >= b for b in bounds) + piece.get("k", 0)
    zoom = 1.0 if flips % 2 == 0 else 1.10
    if piece["kind"] == "cold":
        zoom = 1.06
    fx, fy = face.at(min(max(src_t, S0), E0)) if not rewind else face.at(S0)
    win_h = 1020
    view = crop_to(frame, fx, fy, W, win_h, 1050 / zoom, 0.30)
    canvas = np.zeros((H, W, 3), np.uint8)
    canvas[: H - win_h] = np.linspace(INK, (36, 27, 19), H - win_h).astype(np.uint8)[:, None, :]
    canvas[H - win_h:] = view[:, :, ::-1]

    def paint(d):
        d.text((54, 262), HANDLE, font=font(BOLD, 36), fill=(*BONE, 200), anchor="lm")
        tk = window(t, total - 2.6, total, 0.3)
        if tk > 0:
            pill(d, W // 2, 440, TAKEAWAY, 40, AMBER, INK, tk, pad=34)
        elif piece["kind"] == "cold" or rewind:
            label = "THE SHORT VERSION" if piece["kind"] == "cold" else "NOW FROM THE START"
            pill(d, W // 2, 440, label, 52, AMBER, INK)
        else:
            d.rectangle((60, 330, 76, 470), fill=(*AMBER, 255))
            d.text((100, 360), "2 SKILLS THAT", font=font(BLACK, 62), fill=(*BONE, 255), anchor="lm")
            d.text((100, 440), "EXPAND YOUR LUCK", font=font(BLACK, 62), fill=(*AMBER, 255), anchor="lm")
        # step cards: slots show from the start, fill in when he names each skill
        for n, (when, label) in enumerate(cards):
            y = 580 + n * 150
            shown = ease((t - when) / 0.35) if piece["kind"] == "main" and t >= when else 0.0
            d.rounded_rectangle((60, y, W - 60, y + 120), radius=28, fill=(255, 255, 255, int(24 + 30 * shown)),
                                outline=(*BONE, int(70 + 150 * shown)), width=3)
            d.ellipse((84, y + 20, 164, y + 100), fill=(*AMBER, int(90 + 165 * shown)))
            d.text((124, y + 61), str(n + 1), font=font(BLACK, 46), fill=(*INK, 255), anchor="mm")
            if shown > 0:
                d.text((200, y + 62), label, font=font(BLACK, 54), fill=(*BONE, int(255 * shown)), anchor="lm")
        draw_captions(d, groups, t, y=1300)
        d.rectangle((0, H - win_h - 4, W, H - win_h + 4), fill=(*BONE, 50))
        d.rectangle((0, H - win_h - 4, int(W * min(1.0, t / total)), H - win_h + 4), fill=(*AMBER, 255))

    out = composite(canvas, paint)
    if rewind:
        flash = max(0.0, 1 - i / 6) * 0.8
        out = (out * (1 - flash) + 255 * flash).astype(np.uint8)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    words = load_words()
    face = Face()
    print("words:", len(words), "| face samples:", len(face.t))
    print("A:", render_a(face, words))
    print("B:", render_b(face, words))


if __name__ == "__main__":
    sys.exit(main())
