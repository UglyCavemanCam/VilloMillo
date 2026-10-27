"""The builder's preview: a short sample clip made by the real renderer from a video that is already downloaded.

Because it is the same renderer as the finished clips, what the preview shows is what a clip will look like. The words
spoken are real; the headline, label and takeaway are sample text, since writing them costs money and a preview is free.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import hashlib
import json
import pathlib
import subprocess
import tempfile
import time

from . import CAMPAIGN_FORBIDS, FormatError, assets, blueprints, clip_words, designs, effects, elements, music, placement, render
from .render import ClipText, Job

SAMPLE_LENGTH = designs.DEMO_LENGTH   # long enough for every timed choice: a logo can start at 30 seconds and a call to action sits at the end
KEEP_PREVIEWS = 12
STILL_AT = 2.0         # the frame the builder's stage pauses on, seconds into the sample
SEGMENT_SECONDS = 5.0  # the live preview is rendered in pieces this long, the one under the playhead first (see FORMAT-BUILDER-PLAN.md)
DRAFT_SCALE = 0.5      # and at this share of the full size, so an edit shows fast; `python -m villomillo formats-bench` measures both
BEST_SAMPLES = 3       # how many sample videos the builder offers first
SEGMENT_SCALE = 1.0    # the live preview's size: full, because on the measured PC half size saved nothing (see the plan's "Measured")
FADE = 0.3            # seconds a picture takes to fade, with a little to spare
KEEP_SEGMENTS = 240    # finished pieces kept for reuse (about 40 MB)
SAMPLE_TEXT = ClipText(["YOUR HEADLINE GOES HERE", "IN TWO SHORT LINES"], [("A first point", 4.0)], "A short takeaway",
                       [("punch_in", 4.5, "a sample: the speaker's strongest point")])


class PreviewError(RuntimeError):
    """The message says why there is no preview, in plain words."""


@dataclasses.dataclass
class RealClip:
    """A clip that was really made, as the builder previews it when Edit look opens: its own stretch of its own video, its own words
    and (when they were kept) its own headline, labels and takeaway, instead of a sample."""
    video_id: str
    start: float
    end: float
    text: ClipText | None = None   # None for a clip made before its text was kept: the design's example text is shown instead
    campaign: bool = False         # a campaign clip never carries the handle, logo, button or progress bar

    @classmethod
    def from_row(cls, row) -> "RealClip":
        import json as _json

        kept = (_json.loads(row["grade_json"] or "{}")).get("look_text")
        return cls(row["video_id"], float(row["start_s"]), float(row["end_s"]), ClipText.restore(kept) if kept else None, bool(row["campaign_id"]))


def _compute_densest(video_id: str, length: float) -> tuple[float, int]:
    try:
        starts = sorted(w["t0"] for w in clip_words(video_id, 0.0, 1e9))
    except FormatError as e:
        raise PreviewError(str(e)) from None
    if not starts:
        raise PreviewError("that video has no word-timed transcript")
    best, best_n = starts[0], 0
    for t in starts:
        if t < 30 and starts[-1] > 60:
            continue
        n = bisect.bisect_left(starts, t + length) - bisect.bisect_left(starts, t)   # the words in [t, t + length)
        if n > best_n:
            best, best_n = t, n
    return best, best_n


@functools.lru_cache(maxsize=256)
def _cached_densest(path: str, stamp: int, length: float) -> tuple[float, int]:
    return _compute_densest(pathlib.Path(path).stem, length)


def _densest(video_id: str, length: float) -> tuple[float, int]:
    """(start, words) of the stretch of `length` seconds with the most speech, after the first half minute of a long video.
    The answer is kept until the transcript file changes, because the page asks for every sample video each time it opens."""
    from . import TRANSCRIPTS

    path = TRANSCRIPTS / f"{video_id}.json"
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return _compute_densest(video_id, length)
    return _cached_densest(str(path), stamp, length)


def sample_videos(db) -> list[dict]:
    """Videos that are downloaded and have a word-timed transcript, which is what a preview needs, the one with the most speech
    in a sample-length stretch first: that one is the builder's default sample."""
    from core.paths import cached_source

    from . import DOWNLOADS, TRANSCRIPTS

    out = []
    with db.conn() as c:
        titles = {r["video_id"]: r["title"] for r in c.execute("SELECT video_id, title FROM videos")}
    for transcript in sorted(TRANSCRIPTS.glob("*.json")):
        video_id = transcript.stem
        if cached_source(DOWNLOADS, video_id):
            try:
                words = _densest(video_id, SAMPLE_LENGTH)[1]
            except (PreviewError, OSError, ValueError, KeyError):
                words = 0
            out.append({"id": video_id, "title": titles.get(video_id) or video_id, "words": words})
    return sorted(out, key=lambda v: (-v["words"], v["id"]))


def best_samples(db, count: int = BEST_SAMPLES) -> list[dict]:
    """The sample videos the builder opens on: the most speech first, with the stretch each one is previewed on."""
    out = []
    for v in sample_videos(db)[:count]:
        start, end = sample_window(v["id"])
        out.append({**v, "start": round(start, 2), "end": round(end, 2)})
    return out


def sample_window(video_id: str, length: float = SAMPLE_LENGTH) -> tuple[float, float]:
    """The stretch of the video with the most speech in it, after the first half minute."""
    start, _ = _densest(video_id, length)
    return start, start + length


def _source(video_id: str) -> pathlib.Path:
    from core.paths import cached_source

    from . import DOWNLOADS

    source = cached_source(DOWNLOADS, video_id) or DOWNLOADS / f"{video_id}.mp4"
    if not source.exists():
        raise PreviewError("that video is not downloaded any more: pick another")
    return source


def _plain_still(source: pathlib.Path, start: float, out: pathlib.Path) -> pathlib.Path:
    """The middle of the frame cut to the 9:16 shape: what the stage shows when the chosen layout cannot frame this video."""
    from core import binaries

    cmd = [binaries.ffmpeg(), "-y", "-v", "error", "-ss", f"{start + STILL_AT:.3f}", "-i", str(source), "-frames:v", "1",
           "-vf", "crop='min(iw,ih*9/16)':ih,scale=540:960", str(out)]
    try:
        subprocess.run(cmd, check=True, timeout=60, capture_output=True)
    except (subprocess.SubprocessError, OSError):
        raise PreviewError("the still frame could not be made from that video") from None
    return out


def stage(db, video_id: str, out_dir: pathlib.Path, design: dict | None = None, clip: RealClip | None = None) -> dict:
    """What the builder's live stage needs for one sample video: a still frame framed the way the chosen layout frames a clip,
    and the spoken words of the sample, with times counted from the still. The stage draws the text itself, so changing the
    look is instant; only a change of layout, crop limit or webcam box needs a new frame. `warning` says why the layout
    could not frame this video, in which case the frame is the plain middle of the picture."""
    if clip is not None:
        video_id = clip.video_id
    elif video_id not in {v["id"] for v in sample_videos(db)}:
        raise PreviewError("that video is not available for a preview: pick another")
    design = designs.normalize(design)
    source = _source(video_id)
    start, end = (clip.start, clip.end) if clip is not None else sample_window(video_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    key = json.dumps([video_id, round(start, 2), design["layout"], design["crop_limit"], design["webcam"]], sort_keys=True)
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    out, warning = out_dir / f"format-still-{digest}.jpg", None
    if not out.exists():
        try:
            plan = blueprints.resolve("stage", {"framing": design["layout"], "text": ["headline"]}, SAMPLE_TEXT.available(True),
                                      overrides=designs.overrides_from(design))
            render.framed_still(Job(source, start, start + STILL_AT + 2.0, [], SAMPLE_TEXT, plan, "", out, design=design), out, STILL_AT)
        except (render.RenderError, blueprints.BlueprintError) as e:
            out.unlink(missing_ok=True)
            warning = str(e)
            out = out_dir / f"format-still-{hashlib.sha1((key + 'plain').encode()).hexdigest()[:12]}.jpg"
            if not out.exists():
                _plain_still(source, start, out)
    words = [{"t0": round(w["t0"] - start, 2), "t1": round(w["t1"] - start, 2), "text": w["text"]}
             for w in clip_words(video_id, start, end)]
    return {"still": f"/formats/still/{out.name}", "words": words, "length": round(end - start, 2), "at": STILL_AT, "warning": warning}


def sample_text(design: dict) -> ClipText:
    """The sample's headline, label and takeaway: the example text of the design's own elements (what the stage shows before play),
    so the real picture says what the stage says. The points keep the sample's times."""
    shown = [e for e in design["elements"] if e["visible"]]
    first = lambda kind: next((e for e in shown if e["type"] == kind), None)   # noqa: E731
    out = SAMPLE_TEXT
    if (e := first("headline")) and (lines := [x.strip() for x in e["text"]["default"].split("\n") if x.strip()][:2]):
        out = dataclasses.replace(out, title_lines=lines)
    if (e := first("label")) and e["text"]["default"].strip():
        out = dataclasses.replace(out, points=[(e["text"]["default"].strip(), SAMPLE_TEXT.points[0][1])])
    if (e := first("takeaway")) and e["text"]["default"].strip():
        out = dataclasses.replace(out, takeaway=e["text"]["default"].strip())
    return out


def sample_points(words: list[dict], start: float, length: float, first: str, count: int = 3) -> list[tuple[str, float]]:
    """Point labels for a sample, found in what is really said in it: the first (kept as the label's own example text) near 4 seconds,
    the others spread through the rest at the start of a sentence or a pause, each named by the words that begin it. Times are seconds
    into the clip. A real clip has its own points; this is only for samples and for clips made before theirs were kept."""
    spoken = [(w["t0"] - start, str(w.get("text", "")).strip()) for w in words]
    spoken = [(t, x) for t, x in spoken if x and 0 <= t <= length]
    stops = {i for i, (t, x) in enumerate(spoken) if i == 0 or t - spoken[i - 1][0] >= 0.3 or spoken[i - 1][1][-1:] in ".?!"}
    usable = [i for i in range(len(spoken)) if 3.5 <= spoken[i][0] <= length - 7]
    out: list[tuple[str, float]] = []
    for k, aim in enumerate([4.0, length * 0.42, length * 0.72][:count]):
        # the start of a sentence or a pause is where a point begins; when there is none within a second and a half, the nearest word will do
        spot = min((i for i in usable if not out or spoken[i][0] >= out[-1][1] + 4.0),
                   key=lambda i: abs(spoken[i][0] - aim) + (0.0 if i in stops else 1.5), default=None)
        if spot is None:
            break
        said = " ".join("".join(ch for ch in w if ch.isalnum() or ch in "'-") for w in (x for _, x in spoken[spot:spot + 4])).strip()
        out.append((first if k == 0 else (said[:1].upper() + said[1:] or "Another point"), round(spoken[spot][0], 2)))
    return out


def _job(db, profile, design: dict, video_id: str, start: float, end: float, out: pathlib.Path, *, text: ClipText | None = None,
         campaign: bool = False) -> Job:
    """The clip's job: the real renderer's input for these seconds of the video, with the sample text (or the clip's own)."""
    source = _source(video_id)
    sampled = text is None   # no text of its own: the sample's, with points found in the words
    text = text or sample_text(design)
    forbid = set(design.get("forbid", []))
    if campaign:   # as format_clip does it: no handle, logo, button or progress bar, and nothing the format keeps off campaign clips
        kept, gone = elements.for_campaign(design["elements"])
        design = designs.normalize({**design, "elements": kept})
        forbid |= gone | CAMPAIGN_FORBIDS
    words = clip_words(video_id, start, end)
    if sampled and text.points:
        found = sample_points(words, start, end - start, text.points[0][0])
        text = dataclasses.replace(text, points=found or text.points)
    extra, logo = assets.for_design(db, design)
    try:
        plan = blueprints.resolve("preview", designs.layers_from(design), text.available(bool(words)) | extra,
                                  overrides=designs.overrides_from(design), forbid=forbid)
    except blueprints.BlueprintError as e:
        raise PreviewError(str(e)) from None
    if design["logo"]["enabled"]:
        found = placement.problems(design, design["layout"], placement.logo_size(logo, design), end - start,
                                   render.headline_end(text, plan))
        if found:
            raise PreviewError(found[0].capitalize() + ".")
    mix = None   # the preview lets you hear the music setting with the library's calmest track, when there is one
    if design["music"]["mode"] != "off":
        options = sorted(music.eligible(db, None, False), key=lambda t: (t["mood"] != "calm", t["id"]))
        if options:
            mix = {"path": music.file_path(options[0]), "volume": design["music"]["volume"], "track_id": options[0]["id"],
                   "duration_s": options[0]["duration_s"]}
    try:
        fx = effects.plan(text.proposed_effects, False, design["effects"], plan.framing().id, end - start,
                          render.headline_end(text, plan))
    except render.RenderError as e:
        raise PreviewError(str(e)) from None
    return Job(source, start, end, words, text, plan, profile.brand.handle or "@yourhandle", out, design=design, logo=logo,
               effects=fx, music=mix)


def make_preview(db, profile, design: dict, video_id: str, out_dir: pathlib.Path) -> pathlib.Path:
    """Render the sample clip for a design. `designs.DesignError` says what to change when a choice cannot work."""
    design = designs.normalize(design)
    _source(video_id)   # the video comes first: "not downloaded" is the better thing to be told
    start, end = sample_window(video_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    job = _job(db, profile, design, video_id, start, end, out_dir / "pending.mp4")
    track = job.music and job.music["track_id"]
    out = out_dir / f"format-preview-{hashlib.sha1(json.dumps([design, video_id, track], sort_keys=True).encode()).hexdigest()[:12]}.mp4"
    if not out.exists():
        job.out = out
        try:
            render.render(job, preview=True)
        except render.RenderError as e:
            raise PreviewError(str(e)) from None
    for old in sorted(out_dir.glob("format-preview-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_PREVIEWS:]:
        old.unlink(missing_ok=True)
    return out


def bench(db, profile, video_id: str | None = None, seconds: float = SEGMENT_SECONDS) -> dict:
    """How long the real renderer takes on this computer for `seconds` of one sample video, in each way a preview could be made.
    Nothing is kept. Returns the video, the stretch, and a row per way with the seconds it took."""
    chosen = video_id or (sample_videos(db) or [{}])[0].get("id")
    if not chosen:
        raise PreviewError("there is no downloaded video with a word-timed transcript to measure on: make a clip first")
    start, _ = sample_window(chosen)
    design = designs.normalize({})
    rows = []
    with tempfile.TemporaryDirectory(prefix="vm-bench-") as folder:
        job = _job(db, profile, design, chosen, start, start + seconds, pathlib.Path(folder) / "bench.mp4")
        for label, preview, scale in (("draft: half size", True, DRAFT_SCALE), ("preview: full size", True, 1.0), ("final quality: full size", False, 1.0)):
            began = time.monotonic()
            try:
                render.render(job, preview=preview, scale=scale)
            except render.RenderError as e:
                raise PreviewError(str(e)) from None
            rows.append({"way": label, "seconds": round(time.monotonic() - began, 2)})
    return {"video": chosen, "start": round(start, 2), "length": seconds, "rows": rows}


# --- the live preview: a sample clip made in pieces ------------------------------------------------------------------------

def spans(length: float = SAMPLE_LENGTH, seconds: float = SEGMENT_SECONDS) -> list[tuple[float, float]]:
    """The pieces the sample is cut into, in seconds into the sample: [(0, 5), (5, 10), ..., (30, 35)]."""
    count = int(-(-length // seconds))
    return [(round(i * seconds, 3), round(min((i + 1) * seconds, length), 3)) for i in range(count)]


def segment_keys(job: Job, design: dict, video_id: str, scale: float, track_id=None, printed=None) -> list[str]:
    """One fingerprint per piece. A piece is made again only when something that shows in it changed: a format-wide setting changes
    every piece, and a picture (a label, a button, a box...) only the pieces it is on screen in, found from what the renderer would
    really draw and when."""
    length = job.end - job.start
    prints, bars, _ = printed or render.overlay_prints(job)
    captions = [e for e in design["elements"] if e["visible"] and e["type"] == "captions"]
    common = [video_id, round(job.start, 2), scale, design["layout"], design["crop_limit"], design["webcam"], design["music"], design["effects"],
              track_id, dataclasses.asdict(job.text), job.handle, captions, bars]
    out = []
    for span in spans(length):
        near = (span[0] - FADE, span[1] + FADE)   # a picture fades in and out over a quarter of a second: only what happens in reach of the piece matters
        here = [(f, x, y, None if w is None else (max(w[0], near[0]), min(w[1], near[1]))) for f, x, y, w in prints
                if w is None or (w[0] < span[1] and w[1] > span[0])]
        out.append(hashlib.sha1(json.dumps([common, span, here], sort_keys=True, default=str).encode()).hexdigest()[:12])
    return out


@dataclasses.dataclass
class Prepared:
    """Everything a live preview of one design needs, worked out once: the real renderer's job for the whole sample, the pieces
    and a fingerprint for each."""
    job: Job | None
    spans: list[tuple[float, float]]
    keys: list[str]
    scale: float = 1.0
    windows: dict | None = None   # when each element is on screen, from what the renderer will really draw (see render.overlay_prints)


def prepare(db, profile, design: dict, video_id: str, *, draft: bool = False, clip: RealClip | None = None) -> Prepared:
    """Check a design and the sample (or the real clip), and make ready to render it in pieces. `designs.DesignError` and `PreviewError`
    say what to change."""
    design = designs.normalize(design)
    if clip is not None:
        video_id = clip.video_id
    _source(video_id)   # the video comes first: "not downloaded" is the better thing to be told
    start, end = (clip.start, clip.end) if clip is not None else sample_window(video_id)
    job = _job(db, profile, design, video_id, start, end, pathlib.Path("pending.mp4"), text=clip.text if clip else None,
               campaign=bool(clip and clip.campaign))
    scale = DRAFT_SCALE if draft else SEGMENT_SCALE
    track = job.music and job.music["track_id"]
    printed = render.overlay_prints(job)
    return Prepared(job, spans(end - start), segment_keys(job, job.design, video_id, scale, track, printed), scale, printed[2])


def segment_path(out_dir: pathlib.Path, key: str) -> pathlib.Path:
    return out_dir / f"format-seg-{key}.mp4"


def make_segment(prepared: Prepared, index: int, out_dir: pathlib.Path, cancel=None) -> pathlib.Path:
    """Render one piece of the sample (unless an identical one is already there) with the real renderer. The piece is made under a
    temporary name and renamed when whole, so a stopped render leaves nothing that could be mistaken for a finished piece."""
    out = segment_path(out_dir, prepared.keys[index])
    if out.exists():
        return out
    out_dir.mkdir(parents=True, exist_ok=True)
    part = out.with_name(out.stem + ".part.mp4")
    job = dataclasses.replace(prepared.job, segment=prepared.spans[index], out=part)
    try:
        render.render(job, preview=True, scale=prepared.scale, cancel=cancel)
    except render.RenderCancelled:
        part.unlink(missing_ok=True)
        raise
    except render.RenderError as e:
        part.unlink(missing_ok=True)
        raise PreviewError(str(e)) from None
    part.replace(out)
    return out


def prune_segments(out_dir: pathlib.Path, keep: set[str]) -> None:
    """Forget the oldest finished pieces, never one the current preview uses."""
    files = sorted((f for f in out_dir.glob("format-seg-*.mp4") if not f.name.endswith(".part.mp4")), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in files[KEEP_SEGMENTS:]:
        if old.stem.removeprefix("format-seg-") not in keep:
            old.unlink(missing_ok=True)
    for stale in out_dir.glob("format-seg-*.part.mp4"):
        if time.time() - stale.stat().st_mtime > 600:
            stale.unlink(missing_ok=True)
