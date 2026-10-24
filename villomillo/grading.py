"""The questions Jev answers about each video, and the code that sends them.

The video's title and description are written by strangers. They go in `state` as data and
Jev's answers are only ever used to rank, never to decide what the program does, so a video
that tries to talk the grader into something can at worst move itself up or down a list.
"""

from __future__ import annotations

import json

from . import store
from .config import Profile
from .db import Database
from .jev import Jev, JevError

QUESTION_SET_VERSION = 1

RISKS = {
    "risk_violence": "Does the title or description point to graphic violence or gore?",
    "risk_sexual": "Does the title or description point to sexually explicit content?",
    "risk_minors": "Does the title or description point to a child or minor in danger or being exploited?",
    "risk_tragedy": "Does the title or description exploit a real tragedy, death or disaster?",
    "risk_hate": "Does the title or description point to hate or harassment aimed at a group or a person?",
    "risk_misinfo": "Is this likely to spread dangerous medical or financial misinformation?",
}

MOMENTS = [
    "Mostly music, one continuous demonstration, a slideshow or a trailer. No moment stands on its own.",
    "One long continuous presentation. A few passages might stand alone but they lean on earlier context.",
    "Conversation or commentary with occasional passages that make sense on their own.",
    "Conversation with several clear standalone moments: a story, a strong opinion, a joke or a debate.",
    "A talk, interview or stream packed with standalone moments: stories, punchlines, reveals, hot takes.",
]
APPEAL = [
    "A narrow or dated topic that few people would care about.",
    "Of interest to a small niche only.",
    "Moderate interest to a defined audience.",
    "Broad interest; people are actively talking about it.",
    "Very broad or trending interest across many different audiences.",
]
HOOK = [
    "Nothing in the title or description suggests a striking opening or claim.",
    "A mild premise; a short clip from it would need to be found by luck.",
    "An interesting premise, but not obviously striking.",
    "A clear promise of something striking: a bold claim, a surprising guest, a conflict.",
    "An unmistakable hook: a shocking claim, a famous clash or an irresistible question.",
]


def build_questions() -> dict:
    q = {
        "fits_niche": {
            "type": "noul",
            "instructions": (
                "Is this video's main subject a good match for `niche.definition`, and clear of everything "
                "listed under `niche.exclude`?"
            ),
        },
        "needs_visuals": {
            "type": "noul",
            "instructions": (
                "Is this video's main appeal what you see rather than what is said (gameplay, a music "
                "performance, charts or slides), so that a vertical crop of a short moment would lose the point?"
            ),
        },
        "has_moments": {
            "type": "score",
            "instructions": "How likely is this video to contain several moments that make sense as a 15 to 60 "
                            "second clip on their own, without the rest of the video?",
            "criteria": MOMENTS,
        },
        "topic_appeal": {
            "type": "score",
            "instructions": "How broad and strong is the interest in short clips about this topic right now?",
            "criteria": APPEAL,
        },
        "hook_potential": {
            "type": "score",
            "instructions": "How strong an opening hook does this video's title and premise offer for a short clip?",
            "criteria": HOOK,
        },
    }
    for name, text in RISKS.items():
        q[name] = {"type": "noul", "instructions": text}
    return q


def build_state(video: dict, profile: Profile) -> dict:
    tags = json.loads(video["tags_json"]) if isinstance(video.get("tags_json"), str) else video.get("tags", [])
    return {
        "niche": {
            "definition": profile.niche.definition,
            "include": profile.niche.include,
            "exclude": profile.niche.exclude,
        },
        "video": {
            "title": video["title"],
            "channel": video["channel_title"],
            "length_minutes": round((video["duration_s"] or 0) / 60),
            "description": (video["description"] or "")[:1500],
            "tags": tags[:15],
        },
    }


def grade_one(db: Database, jev: Jev, profile: Profile, video: dict) -> dict:
    """Ask Jev about one video, save the raw answers, and return them."""
    result = jev.ask(build_state(video, profile), build_questions())
    with db.conn() as c:
        store.save_evaluation(
            c, profile.id, video["video_id"], "jev", result.model, QUESTION_SET_VERSION,
            result.answers, result.usage, result.cost_usd,
        )
        store.add_ai_usage(
            c, "jev", result.usage.get("input_tokens") or 0, result.usage.get("output_tokens") or 0, result.cost_usd
        )
    return result.answers


__all__ = ["QUESTION_SET_VERSION", "JevError", "build_questions", "build_state", "grade_one"]
