"""Clip formatting: give a clip the look its channel's recipe asks for.

    plan = blueprints.resolve(...)      which building blocks apply to this clip
    text = text.make(...)               the words the text layers show, written and checked
    render.render(...)                  frame the footage, paint the layers, encode

`format_clip` does all three for one clip from the engine's source video. `make_formatter` wraps it in the shape the
clip pipeline calls, so a failure never loses a clip: the pipeline keeps the engine's own render instead.
"""

from __future__ import annotations

import json
import pathlib

from .. import claude, secrets, spend, store
from ..config import ROOT, Profile, SpendBreaker
from ..db import Database
from . import blueprints, render, text
from .blueprints import BlueprintError, OriginalityError, Plan
from .render import ClipText, Job

DOWNLOADS = ROOT / "data" / "downloads"
TRANSCRIPTS = ROOT / "data" / "transcripts"

__all__ = ["BlueprintError", "ClipText", "FormatError", "OriginalityError", "Plan", "blueprints", "clip_words",
           "format_clip", "formatter_for", "make_formatter", "render", "text"]


class FormatError(RuntimeError):
    """The look could not be applied. The message says why; the clip keeps the engine's own render."""


def clip_words(video_id: str, start: float, end: float, *, directory: pathlib.Path | None = None) -> list[dict]:
    """The words spoken inside a clip, with their times, from the engine's transcript of the episode."""
    path = (directory or TRANSCRIPTS) / f"{video_id}.json"
    if not path.exists():
        raise FormatError(f"no word-timed transcript for {video_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [{"t0": w["start"], "t1": w["end"], "text": w["word"].strip()}
            for s in data["segments"] for w in s.get("words", [])
            if w["start"] >= start - 0.02 and w["end"] <= end + 0.1]


def format_clip(db: Database, profile: Profile, *, claude_client, jev, video_id: str, start_s: float, end_s: float,
                out_path: pathlib.Path, episode: str, breaker: SpendBreaker | None = None,
                source: pathlib.Path | None = None, words: list[dict] | None = None) -> dict:
    """Render one clip in the profile's recipe. Returns {"recipe", "dropped"}; raises FormatError if it cannot."""
    if not profile.format:
        raise FormatError("this channel has no format recipe set")
    source = source or DOWNLOADS / f"{video_id}.mp4"
    if not source.exists():
        raise FormatError(f"the source video is not on disk ({source.name})")
    words = words if words is not None else clip_words(video_id, start_s, end_s)
    if not words:
        raise FormatError("no words were found in the clip")
    if (pause := spend.current(db, breaker or SpendBreaker())):
        raise FormatError(pause.title.lower())
    try:
        clip_text, cost = text.make(claude_client, jev, channel=profile.display_name, niche=profile.niche.definition,
                                    episode=episode, words=words, start=start_s)
    except text.TextProblem as e:
        raise FormatError(f"no trustworthy on-screen text: {e}") from None
    with db.conn() as c:
        store.add_ai_usage(c, "claude", 0, 0, cost["claude"])
        store.add_ai_usage(c, "jev", cost["jev_tokens"], 0, cost["jev"])
    try:
        plan = blueprints.resolve(profile.format, clip_text.available(bool(words)), forbid=set(profile.format_forbid),
                                  overrides=profile.format_params)
    except BlueprintError as e:
        raise FormatError(str(e)) from None
    try:
        render.render(Job(source, start_s, end_s, words, clip_text, plan, profile.brand.handle, out_path))
    except render.RenderError as e:
        raise FormatError(str(e)) from None
    return {"recipe": plan.recipe, "dropped": plan.dropped, "text": clip_text}


def formatter_for(db: Database, profile: Profile, *, jev=None, breaker: SpendBreaker | None = None, recipe: str | None = None):
    """The clip pipeline's formatter for this channel, or None when the channel keeps the engine's own look.
    `recipe` is a look chosen for this video (the kind of video decides it); it replaces the channel's own."""
    if recipe:
        profile = profile.model_copy(update={"format": recipe})
    return make_formatter(db, profile, jev=jev, breaker=breaker) if profile.format else None


def make_formatter(db: Database, profile: Profile, *, jev=None, claude_client=None, breaker: SpendBreaker | None = None):
    """The callable the clip pipeline uses: formatter(clip, job, out_path). Clients are built when first needed."""

    def formatter(clip: dict, job: dict, out_path: pathlib.Path) -> dict:
        from ..jev import Jev

        with db.conn() as c:
            row = c.execute("SELECT title FROM videos WHERE profile_id=? AND video_id=?",
                            (profile.id, job["video_id"])).fetchone()
        return format_clip(db, profile, claude_client=claude_client or claude._client(),
                           jev=jev or Jev(secrets.get(secrets.JEV)), video_id=job["video_id"],
                           start_s=clip["start_s"], end_s=clip["end_s"], out_path=out_path,
                           episode=row["title"] if row else "the episode", breaker=breaker)

    return formatter
