"""Clip formatting: give a clip the look its category's template asks for.

    plan = blueprints.resolve(...)      which building blocks apply to this clip
    text = text.make(...)               the words the text layers show, written and checked
    render.render(...)                  frame the footage, paint the layers, encode

`format_clip` does all three for one clip from the engine's source video. `make_formatter` wraps it in the shape the
clip pipeline calls. A clip whose category has no template gets no formatter at all: it is kept as the engine made it,
flagged for review, and cannot be scheduled or posted until a format is applied.
"""

from __future__ import annotations

import json
import pathlib

from .. import claude, secrets, spend, store
from ..config import ROOT, Profile, SpendBreaker
from ..db import Database
from . import assets, blueprints, designs, effects, font_uploads, music, placement, render, sounds, templates, text
from .blueprints import BlueprintError, OriginalityError, Plan
from .render import ClipText, Job

DOWNLOADS = ROOT / "data" / "downloads"
TRANSCRIPTS = ROOT / "data" / "transcripts"
CAMPAIGN_FORBIDS = {"watermark_center", "progress_bar_bottom", "logo_corner", "cta_end"}   # brand marks a campaign clip never carries

__all__ = ["BlueprintError", "ClipText", "FormatError", "OriginalityError", "Plan", "blueprints", "clip_words", "designs",
           "format_clip", "formatter_for", "make_formatter", "render", "templates", "text"]


class FormatError(RuntimeError):
    """The format could not be applied. The message says why; the clip waits for review with that reason."""


def clip_words(video_id: str, start: float, end: float, *, directory: pathlib.Path | None = None) -> list[dict]:
    """The words spoken inside a clip, with their times, from the engine's transcript of the episode."""
    path = (directory or TRANSCRIPTS) / f"{video_id}.json"
    if not path.exists():
        raise FormatError(f"no word-timed transcript for {video_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [{"t0": w["start"], "t1": w["end"], "text": w["word"].strip()}
            for s in data["segments"] for w in s.get("words", [])
            if w["start"] >= start - 0.02 and w["end"] <= end + 0.1]


def format_clip(db: Database, profile: Profile, template: dict, *, claude_client, jev, video_id: str, start_s: float,
                end_s: float, out_path: pathlib.Path, episode: str, breaker: SpendBreaker | None = None,
                source: pathlib.Path | None = None, words: list[dict] | None = None, campaign: bool = False,
                category: str | None = None) -> dict:
    """Render one clip in a template. Returns {"template", "dropped", "text"}; raises FormatError if it cannot.
    A campaign clip never gets the watermark or the progress bar, whatever the template says."""
    from core.paths import cached_source

    source = source or cached_source(DOWNLOADS, video_id) or DOWNLOADS / f"{video_id}.mp4"  # the download may be .mp4, .mkv or .webm
    if not source.exists():
        raise FormatError(f"the source video is not on disk ({source.name})")
    words = words if words is not None else clip_words(video_id, start_s, end_s)
    if not words:
        raise FormatError("no words were found in the clip")
    if (pause := spend.current(db, breaker or SpendBreaker())):
        raise FormatError(pause.title.lower())
    font_uploads.refresh(db)
    settings = designs.normalize(template["settings"])
    moods: list[str] = []   # the moods the library can offer this clip; the AI decides nothing about music when there are none
    if settings["music"]["mode"] != "off":
        heard = music.source_has_music(source, start_s, end_s)
        if heard is False or (heard is None and settings["layout"] != "stacked_webcam"):   # a stream's sound is usually the game's
            moods = music.available_moods(db, category, campaign)
    try:
        clip_text, cost = text.make(claude_client, jev, channel=profile.display_name, niche=profile.niche.definition,
                                    episode=episode, words=words, start=start_s, music_moods=tuple(moods))
    except text.TextProblem as e:
        raise FormatError(f"no trustworthy on-screen text: {e}") from None
    with db.conn() as c:
        store.add_ai_usage(c, "claude", 0, 0, cost["claude"])
        store.add_ai_usage(c, "jev", cost["jev_tokens"], 0, cost["jev"])
    chosen, track = None, None
    if moods and not clip_text.serious:   # a serious subject never gets music
        if clip_text.music:
            chosen = clip_text.music
        elif settings["music"]["mode"] == "always":
            chosen = (moods[0], "this template always has music")
        if chosen:
            track = music.pick(db, category, campaign, chosen[0])
            if track is None:
                raise FormatError(f"music was chosen ({chosen[0]}) but the library has no {chosen[0]} track for this clip")
    try:
        extra, logo = assets.for_design(db, settings)
    except designs.DesignError as e:
        raise FormatError(str(e)) from None
    forbid = set(settings.get("forbid", [])) | (CAMPAIGN_FORBIDS if campaign else set())
    try:
        plan = blueprints.resolve(template["name"], template["layers"], clip_text.available(bool(words)) | extra, forbid=forbid,
                                  overrides=designs.overrides_from(settings))
    except BlueprintError as e:
        raise FormatError(str(e)) from None
    fx = effects.plan(clip_text.proposed_effects, clip_text.serious, settings["effects"], plan.framing().id, end_s - start_s,
                      render.headline_end(clip_text, plan))
    if settings["logo"]["enabled"]:
        found = placement.problems(settings, plan.framing().id, placement.logo_size(logo, settings), end_s - start_s,
                                   render.headline_end(clip_text, plan))
        if found:
            raise FormatError(found[0].capitalize() + ".")
    try:
        hit_sounds = sounds.hits(db, fx)
        render.render(Job(source, start_s, end_s, words, clip_text, plan, profile.brand.handle, out_path, design=settings, logo=logo,
                          effects=fx, sounds=hit_sounds,
                          music={"path": music.file_path(track), "volume": settings["music"]["volume"]} if track else None))
    except render.RenderError as e:
        raise FormatError(str(e)) from None
    used = None
    if track:
        music.mark_used(db, track["id"])
        used = {"mood": chosen[0], "reason": chosen[1], "track_id": track["id"], "title": track["title"], "artist": track["artist"]}
    return {"template": template["id"], "dropped": plan.dropped, "text": clip_text, "effects": fx, "music": used,
            "sounds": [h["name"] for h in hit_sounds]}


def formatter_for(db: Database, profile: Profile, *, category: str | None, jev=None, breaker: SpendBreaker | None = None):
    """The clip pipeline's formatter for a category, or None when the category has no template."""
    template = templates.for_category(db, category)
    return make_formatter(db, profile, template, jev=jev, breaker=breaker) if template else None


def make_formatter(db: Database, profile: Profile, template: dict, *, jev=None, claude_client=None,
                   breaker: SpendBreaker | None = None):
    """The callable the clip pipeline uses: formatter(clip, job, out_path). Clients are built when first needed."""

    def formatter(clip: dict, job: dict, out_path: pathlib.Path) -> dict:
        from ..jev import Jev

        with db.conn() as c:
            row = c.execute("SELECT title FROM videos WHERE profile_id=? AND video_id=?",
                            (profile.id, job["video_id"])).fetchone()
        return format_clip(db, profile, template, claude_client=claude_client or claude._client(),
                           jev=jev or Jev(secrets.get(secrets.JEV)), video_id=job["video_id"],
                           start_s=clip["start_s"], end_s=clip["end_s"], out_path=out_path,
                           episode=row["title"] if row else "the episode", breaker=breaker,
                           campaign=bool(job.get("campaign_id")), category=job.get("category"))

    return formatter
