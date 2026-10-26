"""The builder's preview: a short sample clip made by the real renderer from a video that is already downloaded.

Because it is the same renderer as the finished clips, what the preview shows is what a clip will look like. The words
spoken are real; the headline, label and takeaway are sample text, since writing them costs money and a preview is free.
"""

from __future__ import annotations

import functools
import hashlib
import json
import pathlib
import subprocess
import tempfile
import time

from . import FormatError, assets, blueprints, clip_words, designs, effects, music, placement, render
from .render import ClipText, Job

SAMPLE_LENGTH = designs.DEMO_LENGTH   # long enough for every timed choice: a logo can start at 30 seconds and a call to action sits at the end
KEEP_PREVIEWS = 12
STILL_AT = 2.0         # the frame the builder's stage pauses on, seconds into the sample
SEGMENT_SECONDS = 5.0  # the live preview is rendered in pieces this long, the one under the playhead first (see FORMAT-BUILDER-PLAN.md)
DRAFT_SCALE = 0.5      # and at this share of the full size, so an edit shows fast; `python -m villomillo formats-bench` measures both
BEST_SAMPLES = 3       # how many sample videos the builder offers first
SAMPLE_TEXT = ClipText(["YOUR HEADLINE GOES HERE", "IN TWO SHORT LINES"], [("A first point", 4.0)], "A short takeaway",
                       [("punch_in", 4.5, "a sample: the speaker's strongest point")])


class PreviewError(RuntimeError):
    """The message says why there is no preview, in plain words."""


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
        n = sum(1 for s in starts if t <= s < t + length)
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


def stage(db, video_id: str, out_dir: pathlib.Path, design: dict | None = None) -> dict:
    """What the builder's live stage needs for one sample video: a still frame framed the way the chosen layout frames a clip,
    and the spoken words of the sample, with times counted from the still. The stage draws the text itself, so changing the
    look is instant; only a change of layout, crop limit or webcam box needs a new frame. `warning` says why the layout
    could not frame this video, in which case the frame is the plain middle of the picture."""
    if video_id not in {v["id"] for v in sample_videos(db)}:
        raise PreviewError("that video is not available for a preview: pick another")
    design = designs.normalize(design)
    source = _source(video_id)
    start, end = sample_window(video_id)
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


def _job(db, profile, design: dict, video_id: str, start: float, end: float, out: pathlib.Path) -> Job:
    """The sample clip's job: the real renderer's input for these seconds of the video, with the sample text."""
    source = _source(video_id)
    words = clip_words(video_id, start, end)
    extra, logo = assets.for_design(db, design)
    try:
        plan = blueprints.resolve("preview", designs.layers_from(design), SAMPLE_TEXT.available(bool(words)) | extra,
                                  overrides=designs.overrides_from(design))
    except blueprints.BlueprintError as e:
        raise PreviewError(str(e)) from None
    if design["logo"]["enabled"]:
        found = placement.problems(design, design["layout"], placement.logo_size(logo, design), end - start,
                                   render.headline_end(SAMPLE_TEXT, plan))
        if found:
            raise PreviewError(found[0].capitalize() + ".")
    mix = None   # the preview lets you hear the music setting with the library's calmest track, when there is one
    if design["music"]["mode"] != "off":
        options = sorted(music.eligible(db, None, False), key=lambda t: (t["mood"] != "calm", t["id"]))
        if options:
            mix = {"path": music.file_path(options[0]), "volume": design["music"]["volume"], "track_id": options[0]["id"]}
    try:
        fx = effects.plan(SAMPLE_TEXT.proposed_effects, False, design["effects"], plan.framing().id, end - start,
                          render.headline_end(SAMPLE_TEXT, plan))
    except render.RenderError as e:
        raise PreviewError(str(e)) from None
    return Job(source, start, end, words, SAMPLE_TEXT, plan, profile.brand.handle or "@yourhandle", out, design=design, logo=logo,
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
