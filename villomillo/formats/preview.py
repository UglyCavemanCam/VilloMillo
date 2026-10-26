"""The builder's preview: a short sample clip made by the real renderer from a video that is already downloaded.

Because it is the same renderer as the finished clips, what the preview shows is what a clip will look like. The words
spoken are real; the headline, label and takeaway are sample text, since writing them costs money and a preview is free.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

from . import assets, blueprints, clip_words, designs, effects, music, placement, render
from .render import ClipText, Job

SAMPLE_LENGTH = 8.0
KEEP_PREVIEWS = 12
SAMPLE_TEXT = ClipText(["YOUR HEADLINE GOES HERE", "IN TWO SHORT LINES"], [("A first point", 4.0)], "A short takeaway",
                       [("punch_in", 4.5, "a sample: the speaker's strongest point")])


class PreviewError(RuntimeError):
    """The message says why there is no preview, in plain words."""


def sample_videos(db) -> list[dict]:
    """Videos that are downloaded and have a word-timed transcript, which is what a preview needs."""
    from core.paths import cached_source

    from . import DOWNLOADS, TRANSCRIPTS

    out = []
    with db.conn() as c:
        titles = {r["video_id"]: r["title"] for r in c.execute("SELECT video_id, title FROM videos")}
    for transcript in sorted(TRANSCRIPTS.glob("*.json")):
        video_id = transcript.stem
        if cached_source(DOWNLOADS, video_id):
            out.append({"id": video_id, "title": titles.get(video_id) or video_id})
    return out


def sample_window(video_id: str, length: float = SAMPLE_LENGTH) -> tuple[float, float]:
    """The stretch of the video with the most speech in it, after the first half minute."""
    starts = sorted(w["t0"] for w in clip_words(video_id, 0.0, 1e9))
    if not starts:
        raise PreviewError("that video has no word-timed transcript")
    best, best_n = starts[0], 0
    for t in starts:
        if t < 30 and starts[-1] > 60:
            continue
        n = sum(1 for s in starts if t <= s < t + length)
        if n > best_n:
            best, best_n = t, n
    return best, best + length


def make_preview(db, profile, design: dict, video_id: str, out_dir: pathlib.Path) -> pathlib.Path:
    """Render the sample clip for a design. `designs.DesignError` says what to change when a choice cannot work."""
    from core.paths import cached_source

    from . import DOWNLOADS

    design = designs.normalize(design)
    source = cached_source(DOWNLOADS, video_id) or DOWNLOADS / f"{video_id}.mp4"
    if not source.exists():
        raise PreviewError("that video is not downloaded any more: pick another")
    start, end = sample_window(video_id)
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
    out_dir.mkdir(parents=True, exist_ok=True)
    mix = None   # the preview lets you hear the music setting with the library's calmest track, when there is one
    if design["music"]["mode"] != "off":
        options = sorted(music.eligible(db, None, False), key=lambda t: (t["mood"] != "calm", t["id"]))
        if options:
            mix = {"path": music.file_path(options[0]), "volume": design["music"]["volume"], "track_id": options[0]["id"]}
    out = out_dir / f"format-preview-{hashlib.sha1(json.dumps([design, video_id, mix and mix['track_id']], sort_keys=True).encode()).hexdigest()[:12]}.mp4"
    if not out.exists():
        try:
            fx = effects.plan(SAMPLE_TEXT.proposed_effects, False, design["effects"], plan.framing().id, end - start,
                              render.headline_end(SAMPLE_TEXT, plan))
            render.render(Job(source, start, end, words, SAMPLE_TEXT, plan, profile.brand.handle or "@yourhandle", out, design=design,
                              logo=logo, effects=fx, music=mix), preview=True)
        except render.RenderError as e:
            raise PreviewError(str(e)) from None
    for old in sorted(out_dir.glob("format-preview-*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)[KEEP_PREVIEWS:]:
        old.unlink(missing_ok=True)
    return out
