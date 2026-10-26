"""Rendering a clip in one ffmpeg pass.

Python decides what goes where: it follows the speaker's face, writes the captions as subtitles, and draws the headline,
labels and watermark once as transparent images. ffmpeg then does all the per-frame work in a single filter graph, so a
45-second clip takes seconds rather than minutes (Phase 0 measured 104 s the old way and 9 s this way).

The look comes from the plan (which blocks apply, see blueprints.py) and the design (fonts, colours, caption style, see
designs.py). Every word on screen is the speaker's own; the layers only arrange and label what was said.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

import cv2
import numpy as np

from core import binaries

from . import captions, designs, effects, fonts, overlays
from .blueprints import Plan

W, H = 1080, 1920
AMBER, INK = "#FFB000", "#17120E"
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000"

# YouTube compresses every upload again, and a clip that was already squeezed hard has little left to give: its text edges and
# faces turn mushy. So the file is made with plenty of detail (CRF 14, a low number is more detail) and told which colour standard
# it uses (BT.709, what every player and YouTube assume for HD), because raw frames carry no colour tag of their own.
FINAL_ARGS = ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-maxrate", "40M", "-bufsize", "80M", "-profile:v", "high"]
PREVIEW_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
COLOUR_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


class RenderError(RuntimeError):
    pass


class FaceProblem(RenderError):
    """The crop would leave the speaker out of the picture."""


@dataclass
class ClipText:
    """What the text layers say. Written by formats/text.py and checked against the transcript."""
    title_lines: list[str] = field(default_factory=list)
    points: list[tuple[str, float]] = field(default_factory=list)    # (label, seconds into the clip)
    takeaway: str = ""
    proposed_effects: list = field(default_factory=list)   # (kind, seconds into the clip, reason), as Claude proposed them
    serious: bool = False                                  # the subject is loss, injury or similar: no shake, flash or music
    music: tuple[str, str] | None = None                   # (mood, reason) Claude asked for, when the library offered moods

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
    design: dict = field(default_factory=dict)
    logo: pathlib.Path | None = None   # the uploaded logo's file, when the design shows one
    effects: list = field(default_factory=list)   # the effects this clip gets, from effects.plan()
    sounds: list = field(default_factory=list)    # {path, start, volume} sound effects laid under the picture effects
    music: dict | None = None                     # {path, volume}: the track to put under the voice, when the clip gets music


# --- following the speaker --------------------------------------------------------------------------------------

class Face:
    """Where the speaker's face is, sampled four times a second straight through the video and smoothed.
    With no face found it holds the middle. Positions are in source pixels, times in source seconds."""

    def __init__(self, source: pathlib.Path, start: float, end: float, hz: float = 4.0):
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise RenderError(f"cannot open the source video {source}")
        try:
            self._track(cap, start, end, hz)
        finally:
            cap.release()

    def _track(self, cap: cv2.VideoCapture, start: float, end: float, hz: float) -> None:
        front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        prof = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        sw, sh = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        first = max(start - 0.5, 0.0)
        cap.set(cv2.CAP_PROP_POS_MSEC, first * 1000)
        step = max(1, round(fps / hz))
        ts, xs, ys, ws, last = [], [], [], [], None
        for i in range(int((end - first + 0.5) * fps)):
            if i % step:
                if not cap.grab():
                    break
                continue
            ok, frame = cap.read()
            if not ok:
                break
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
            ts.append(first + i / fps), xs.append(pick[0]), ys.append(pick[1]), ws.append(pick[2] * sw / 960)
        self.size = (sw, sh)
        self.default = (sw / 2, sh * 0.3)
        self.t, self.raw_x = np.array(ts), np.array(xs)
        self.width = np.array(ws)   # the face's width in source pixels, at each check
        self.x, self.y = (self._smooth(self.raw_x), self._smooth(np.array(ys))) if ts else (None, None)

    @staticmethod
    def _smooth(v: np.ndarray, k: int = 2) -> np.ndarray:
        med = np.array([np.median(v[max(0, i - k): i + k + 1]) for i in range(len(v))])
        return np.convolve(np.pad(med, k, mode="edge"), np.ones(2 * k + 1) / (2 * k + 1), mode="valid")

    def centre_at(self, times: np.ndarray) -> np.ndarray:
        if self.x is None:
            return np.full(len(times), self.default[0])
        return np.interp(times, self.t, self.x)


def speaker_centres(source: pathlib.Path, start: float, end: float, sw: int) -> tuple[float, float]:
    """Where the two people sit in a wide shot, as their horizontal centres in source pixels (left one first).
    Found from the faces seen at several points in the clip. A clip with only one face in view cannot be split: refused."""
    cap = cv2.VideoCapture(str(source))
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    lefts, rights = [], []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
        for t in np.linspace(start + 0.5, max(end - 0.5, start + 0.6), 8):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (960, 540)), cv2.COLOR_BGR2GRAY)
            faces = sorted(front.detectMultiScale(gray, 1.1, 5, minSize=(40, 40)), key=lambda f: f[0])
            big = sorted(faces, key=lambda f: -f[2])[:2]
            if len(big) == 2:
                xs = sorted(((x + w / 2) * sw / 960 for x, _, w, _ in big))
                # two real people sit well apart and have faces of about the same size; one face twice is not two people
                similar = min(big[0][2], big[1][2]) >= 0.7 * max(big[0][2], big[1][2])
                if similar and xs[1] - xs[0] >= 0.35 * sw:
                    lefts.append(xs[0]), rights.append(xs[1])
    finally:
        cap.release()
    if len(lefts) < 3:
        raise RenderError("the split layout needs two people in view; two faces were not found in this clip")
    return float(np.median(lefts)), float(np.median(rights))


def check_window(face: Face, crop_w: float, left_limit: float, start: float) -> None:
    """Refuse a crop that would leave the speaker out of the picture, and say where."""
    if face.x is None:
        return
    sw = face.size[0]
    for t, x in zip(face.t, face.raw_x):
        x0 = min(max(x - crop_w / 2, left_limit), sw - crop_w)
        if not x0 <= x <= x0 + crop_w:
            at = max(t - start, 0.0)
            raise FaceProblem(f"the crop would cut off the speaker at about {int(at // 60)}:{int(at % 60):02d}; "
                              "the format keeps the picture clear of a part of the frame the speaker is in")


def _lerp_expr(grid: np.ndarray, values: np.ndarray) -> str:
    """A piecewise-linear ffmpeg expression in t through (grid[i], values[i])."""
    expr = f"{values[-1]:.1f}"
    for i in range(len(grid) - 2, -1, -1):
        t0, t1, v0, v1 = grid[i], grid[i + 1], values[i], values[i + 1]
        expr = f"if(lt(t,{t1:.1f}),{v0:.1f}+({v1 - v0:.2f})*(t-{t0:.1f})/{t1 - t0:.1f},{expr})"
    return expr


def key_words(text: ClipText) -> frozenset[str]:
    out = set()
    for line in [*text.title_lines, *(label for label, _ in text.points)]:
        out |= {w for w in "".join(c.lower() if c.isalnum() else " " for c in line).split() if len(w) >= 4}
    return frozenset(out)


# --- the render -------------------------------------------------------------------------------------------------

def _fps(path: pathlib.Path) -> float:
    cap = cv2.VideoCapture(str(path))
    try:
        return float(cap.get(cv2.CAP_PROP_FPS)) or 25.0
    finally:
        cap.release()


def _source_size(path: pathlib.Path) -> tuple[int, int]:
    cap = cv2.VideoCapture(str(path))
    try:
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        cap.release()
    if not w or not h:
        raise RenderError(f"cannot open the source video {path}")
    return w, h


def headline_end(text: ClipText, plan: Plan) -> float:
    """Seconds into the clip when the headline goes: it shows for as long as its words take, within its limits."""
    if not (plan.has("headline") and text.title_lines):
        return 0.0
    words = sum(len(line.split()) for line in text.title_lines)
    base, per, low, high = (plan.param("headline", k) for k in ("base_s", "per_word_s", "min_s", "max_s"))
    return min(high, max(low, base + per * words))


def _framing(job: Job, framing: str, sw: int, sh: int, label: str = "base") -> str:
    """The first part of the filter graph: the source framed into 1080x1920, labelled [label]."""
    if framing == "fit_blur":
        return (f"[0:v]split=2[a][b];"
                f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},gblur=sigma=40,eq=brightness=-0.2:saturation=0.85[bg];"
                f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease:flags=lanczos,unsharp=5:5:0.5[fg];"
                f"[bg][fg]overlay=(W-w)/2:(H-h)/2[{label}]")
    if framing == "split":   # two people on one wide shot: each band is cut around its own person, the left one on top
        left, right = speaker_centres(job.source, job.start, job.end, sw)
        band_h, band_w = H // 2, W
        crop_h = min(sh, int(sw * band_h / band_w) // 2 * 2)
        crop_w = int(crop_h * band_w / band_h) // 2 * 2
        def window(cx: float) -> str:
            return f"crop={crop_w}:{crop_h}:{int(min(max(cx - crop_w / 2, 0), sw - crop_w))}:{(sh - crop_h) // 2}"
        return (f"[0:v]split=2[l][r];"
                f"[l]{window(left)},scale={W}:{band_h}:flags=lanczos,unsharp=5:5:0.5[top];"
                f"[r]{window(right)},scale={W}:{band_h}:flags=lanczos,unsharp=5:5:0.5[bottom];"
                f"[top][bottom]vstack[{label}]")
    if framing == "stacked_webcam":   # a stream: the webcam band on top (cut from its box in the picture), the game below it
        d = designs.normalize(job.design)
        cam = d["webcam"]
        share = float(job.plan.framing_param("webcam_share", 0.38))
        band = int(H * share) // 2 * 2
        game = H - band
        return (f"[0:v]split=2[cam][game];"
                f"[cam]crop=iw*{cam['w'] / 100:.4f}:ih*{cam['h'] / 100:.4f}:iw*{cam['x'] / 100:.4f}:ih*{cam['y'] / 100:.4f},"
                f"scale={W}:{band}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{band},unsharp=5:5:0.5[top];"
                f"[game]scale=-2:{game}:flags=lanczos,crop={W}:{game},unsharp=5:5:0.5[bottom];"
                f"[top][bottom]vstack[{label}]")
    if sw * 16 >= sh * 9:   # wider than a phone: a full-height window the shape of the frame, following the face
        crop_w, crop_h = int(sh * 9 / 16) // 2 * 2, sh // 2 * 2
    else:                   # already narrower: the full width, as tall as the frame shape allows
        crop_w, crop_h = sw // 2 * 2, int(sw * 16 / 9) // 2 * 2
    limit = float(job.plan.framing_param("keep_right_of", 0.0)) * sw
    face = Face(job.source, job.start, job.end)
    check_window(face, crop_w, limit, job.start)
    total = job.end - job.start
    grid = np.arange(0.0, total + 1.0, 1.0)
    x0 = np.clip(face.centre_at(grid + job.start) - crop_w / 2, limit, sw - crop_w)
    base = f"[0:v]crop=w={crop_w}:h={crop_h}:x='{_lerp_expr(grid, x0)}':y={(sh - crop_h) // 2},"
    zoom = effects.zoom_factor(job.effects)
    if zoom == "1":
        return f"{base}scale={W}:{H}:flags=lanczos,unsharp=5:5:0.5[{label}]"
    # a punch-in or zoom-out: the picture is scaled up or down frame by frame, then the middle 1080x1920 kept. The crop
    # stays on the speaker's face and keeps the caption-free picture size (ffmpeg cannot resize a crop per frame).
    return (f"{base}scale=w='2*floor({W // 2}/{zoom})':h='2*floor({H // 2}/{zoom})':eval=frame:flags=lanczos,"
            f"crop={W}:{H}:(iw-{W})/2:(ih-{H})/2,unsharp=5:5:0.5[{label}]")


def _overlays(job: Job, d: dict, total: float, framing: str, work: pathlib.Path) -> list[dict]:
    """Every image to lay over the video: its file, where it goes and when it shows (None = the whole clip)."""
    plan, text, items = job.plan, job.text, []

    def add(name: str, image, x: str, y: str, window: tuple[float, float] | None) -> None:
        path = work / f"{name}.png"
        image.save(path)
        items.append({"file": path.name, "x": x, "y": y, "window": window})

    head_end = 0.0
    if plan.has("headline") and text.title_lines:
        words = sum(len(line.split()) for line in text.title_lines)
        base, per, low, high = (plan.param("headline", k) for k in ("base_s", "per_word_s", "min_s", "max_s"))
        head_end = min(high, max(low, base + per * words))
        add("headline", overlays.headline_image(text.title_lines, d["headline"]), "0", str(designs.HEADLINE_TOP[framing]), (0.0, head_end))
    row = designs.CHIPS_Y[framing]
    cta = d["cta"]["text"] if plan.has("cta_end") and total > 6.0 else None
    cta_start = total - float(d["cta"]["seconds"]) if cta else None
    take_end = cta_start - 0.3 if cta else total   # the call to action has the last seconds; the takeaway lands just before it
    take_start = take_end - 2.6 if plan.has("takeaway") and text.takeaway and take_end > 3.0 else None
    if plan.has("point_chips"):
        for i, (label, at) in enumerate(text.points):
            end = text.points[i + 1][1] - 0.3 if i + 1 < len(text.points) else total
            if take_start is not None:
                end = min(end, take_start - 0.1)
            start = max(at, head_end)
            if end - start >= 0.6:
                add(f"chip{i}", overlays.pill_image(f"{i + 1}  ·  {label.upper()}", 40, AMBER, INK), "(W-w)/2", f"{row}-h/2", (start, end))
    if take_start is not None:
        add("takeaway", overlays.pill_image(text.takeaway.upper(), 46, AMBER, INK, pad=34), "(W-w)/2", f"{row}-h/2", (take_start, take_end))
    if cta:
        add("cta", overlays.pill_image(cta.upper(), int(d["cta"]["size"]), AMBER, INK, pad=34), "(W-w)/2", f"{row}-h/2", (cta_start, total))
    if plan.has("logo_corner") and job.logo is not None:
        image = overlays.logo_image(job.logo, round(W * int(d["logo"]["size"]) / 100))
        margin = int(plan.param("logo_corner", "margin"))
        right = framing in ("stacked_webcam", "split") or d["logo"]["corner"] == "right"   # the top band is taken: the logo goes right
        x = f"W-w-{margin}" if right else str(margin)
        from_s = float(d["logo"]["from_s"])
        add("logo", image, x, str(designs.LOGO_Y[framing]), (from_s, total) if from_s > 0 else None)
    if plan.has("watermark_center") and job.handle:
        add("watermark", overlays.watermark_image(job.handle, int(plan.param("watermark_center", "alpha")),
                                                  int(plan.param("watermark_center", "size"))), "(W-w)/2", "(H-h)/2", None)
    return items


def render(job: Job, *, preview: bool = False) -> pathlib.Path:
    """Make the clip. `preview` trades quality for speed (a few seconds of video for the builder's preview)."""
    ffmpeg = binaries.ffmpeg()
    if not (pathlib.Path(ffmpeg).exists() or shutil.which(ffmpeg)):
        raise RenderError("ffmpeg was not found: install FFmpeg or put it on PATH")
    d = designs.normalize(job.design)
    total = job.end - job.start
    sw, sh = _source_size(job.source)
    framing = job.plan.framing().id
    job.out.parent.mkdir(parents=True, exist_ok=True)

    work = pathlib.Path(tempfile.mkdtemp(prefix="vm-render-"))
    try:
        for e in job.effects:
            if e["kind"] in ("punch_in", "zoom_out") and framing != "face_fill":
                raise RenderError(f"the {effects.KINDS[e['kind']]['label'].lower()} effect needs the face-fill layout")
        shake = effects.shake_offset(job.effects, 16)
        if shake == "0":
            parts = [_framing(job, framing, sw, sh)]
        else:   # a shake: a slightly tighter window that jitters over the picture, so no edge shows
            parts = [_framing(job, framing, sw, sh, label="base0"),
                     f"[base0]crop=w=1040:h=1848:x='20+{shake}':y='36+{effects.shake_offset(job.effects, 8)}',"
                     f"scale={W}:{H}[base]"]
        inputs = ["-ss", f"{job.start:.3f}", "-t", f"{total:.3f}", "-i", str(job.source)]
        chain = "base"
        fps = _fps(job.source)
        for k, (first, last) in enumerate(effects.freeze_frames(job.effects, fps)):
            parts.append(f"[{chain}]split=2[fz{k}a][fz{k}b];[fz{k}a][fz{k}b]freezeframes=first={first}:last={last}:replace={first}[fz{k}]")
            chain = f"fz{k}"
        colour = effects.colour_filters(job.effects)
        if colour:
            parts.append(f"[{chain}]{','.join(colour)}[fx]")
            chain = "fx"
        items = _overlays(job, d, total, framing, work)
        for n, item in enumerate(items, start=1):
            inputs += ["-loop", "1", "-framerate", "30", "-t", f"{total:.3f}", "-i", item["file"]]
            if item["window"] is None:
                parts.append(f"[{n}:v]format=rgba[o{n}]")
            else:
                a, b = item["window"]
                parts.append(f"[{n}:v]format=rgba,fade=t=in:st={a:.2f}:d=0.25:alpha=1,"
                             f"fade=t=out:st={max(a, b - 0.25):.2f}:d=0.25:alpha=1[o{n}]")
            parts.append(f"[{chain}][o{n}]overlay=x={item['x']}:y={item['y']}[s{n}]")
            chain = f"s{n}"

        c = d["captions"]
        if job.plan.has("captions") and job.words:
            rel = [{**w, "o0": w["t0"] - job.start, "o1": w["t1"] - job.start} for w in job.words]
            (work / "captions.ass").write_text(captions.build_ass(
                rel, c["style"], y=designs.POSITIONS[c["position"]], font=c["font"], size=c["size"], upper=c["uppercase"],
                base=c["text_color"], accent=c["highlight_color"], keys=key_words(job.text)), encoding="utf-8")
            parts.append(f"[{chain}]subtitles=captions.ass:fontsdir={fonts.fonts_dir_option()}[subs]")
            chain = "subs"

        tail = ""
        if job.plan.has("progress_bar_bottom"):
            bar = int(job.plan.param("progress_bar_bottom", "height"))
            tail = (f"drawbox=x=0:y={H - bar}:w={W}:h={bar}:color=0xF4EBDD@0.27:t=fill,"
                    f"drawbox=x=0:y={H - bar}:w='{W}*min(1,t/{total:.3f})':h={bar}:color=0xFFB000:t=fill,")
        parts.append(f"[{chain}]{tail}scale=out_color_matrix=bt709:out_range=tv,format=yuv420p[out]")

        audio = ["-map", "0:a:0?", "-af", LOUDNESS]
        hits = [h for h in job.sounds if pathlib.Path(h["path"]).exists()]
        if job.music or hits:
            # every sound in the clip's mix is levelled first, so the settings mean "dB under the voice"; the music is looped,
            # faded, and pushed down whenever the voice is speaking; each sound effect starts at its effect; then the usual
            # loudness check is applied to the whole mix
            branches = []
            if job.music:
                music_file = pathlib.Path(job.music["path"])
                if not music_file.exists():
                    raise RenderError(f"the music file is missing ({music_file.name}): add the track again on the Music tab")
                m = len(items) + 1
                inputs += ["-stream_loop", "-1", "-t", f"{total:.3f}", "-i", str(music_file)]
                parts += ["[0:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,asplit=2[va][vk]",
                          f"[{m}:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11,volume={int(job.music['volume'])}dB,"
                          f"afade=t=in:st=0:d=1,afade=t=out:st={max(total - 1.5, 0):.2f}:d=1.5[mus]",
                          "[mus][vk]sidechaincompress=threshold=0.02:ratio=10:attack=20:release=400[duck]"]
                branches += ["[va]", "[duck]"]
            else:
                parts.append("[0:a]aresample=48000,loudnorm=I=-16:TP=-1.5:LRA=11[va]")
                branches.append("[va]")
            for k, hit in enumerate(hits):
                n = len(items) + 1 + (1 if job.music else 0) + k
                inputs += ["-i", str(hit["path"])]
                ms = int(round(max(hit["start"], 0.0) * 1000))
                parts.append(f"[{n}:a]aresample=48000,volume={hit['volume']}dB,adelay={ms}|{ms}[x{k}]")
                branches.append(f"[x{k}]")
            parts.append(f"{''.join(branches)}amix=inputs={len(branches)}:duration=first:normalize=0,{LOUDNESS}[aout]")
            audio = ["-map", "[aout]"]
        cmd = [ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", ";".join(parts), "-map", "[out]", *audio,
               *(PREVIEW_ARGS if preview else FINAL_ARGS), *COLOUR_TAGS,
               "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest", str(job.out.resolve())]
        done = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True)
        if done.returncode:
            raise RenderError(f"ffmpeg failed: {done.stderr[-500:]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return job.out
