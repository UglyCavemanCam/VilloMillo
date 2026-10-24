"""The scoring brain: every possible clip gets a viral score, an estimated CPM and an expected revenue, and the video
gets a kind (which decides its category and its look). The formulas are in docs/AUTOPILOT-PLAN.md, section 6.

Nothing here is random. Jev's answers are stored by the text they were about, so the same video always gives the same
scores and the same text is never paid for twice. Every number comes with a plain sentence saying where it came from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import spend, store
from .config import Scoring, SpendBreaker
from .db import Database

# What a video can be. `category` is the channel category its clips belong to; `look` is a recipe in blueprints.yaml.
KINDS = {
    "podcast": {"label": "podcast or interview", "category": "Podcasts", "look": "podcast_talk",
                "ask": "Is this mainly a conversation, interview or panel between people, such as a podcast or talk show?"},
    "stream": {"label": "livestream", "category": "Stream clips", "look": "generic_safe",
               "ask": "Is this a recording of a livestream: a person playing a game, reacting, or chatting live with an audience?"},
    "talk": {"label": "talk or lecture", "category": "Talks", "look": "podcast_talk",
             "ask": "Is this mainly one person speaking to an audience, such as a lecture, speech, tutorial or presentation?"},
}
OTHER = {"label": "something else", "category": "Other", "look": "generic_safe"}
SAFE_LOOK = "generic_safe"
SOURCES = ("paste", "research", "campaign")

CLIP_CRITERIA = {
    "hook": ["Nothing in the opening suggests a striking opening or claim.", "A mild premise; the opening would need luck to hold a viewer.",
             "An interesting premise, but not obviously striking.", "A clear promise of something striking: a bold claim, a surprising guest, a conflict.",
             "An unmistakable hook: a shocking claim, a famous clash or an irresistible question."],
    "payoff": ["Trails off with no point", "Ends on a weak or unfinished thought", "Ends on a clear but small point",
               "Ends on a satisfying point", "Ends on a strong punchline or conclusion"],
}


@dataclass(frozen=True)
class Reading:
    """Jev's reading of one clip's words."""

    hook: float  # 0 to 4
    payoff: float  # 0 to 4
    stands_alone: float  # 0 to 1

    @property
    def score(self) -> int:
        """0 to 100: the average of the three, each on its own scale."""
        return round(100 * (self.hook / 4 + self.payoff / 4 + self.stands_alone) / 3)


@dataclass(frozen=True)
class KindReading:
    kind: str  # podcast | stream | talk | other
    label: str
    category: str
    certainty: float  # 0 to 1
    look: str  # the recipe that will be used (the safe one when unsure)
    unsure: bool
    explain: str


@dataclass
class MomentScore:
    id: int
    start: float
    end: float
    hook: str
    engine_score: int
    reading: Reading | None
    viral: int
    cpm: float  # estimated, or the campaign's stated pay
    cpm_estimated: bool
    views: int
    revenue: float
    make: bool
    explain: list[str] = field(default_factory=list)  # one plain sentence per number, then the verdict


@dataclass
class VideoScores:
    kind: KindReading
    moments: list[MomentScore]  # best expected revenue first

    @property
    def makeable(self) -> list[MomentScore]:
        return [m for m in self.moments if m.make]


# ---- Jev, with every answer kept --------------------------------------------------------------------------------


def _key(kind: str, payload: dict) -> str:
    return hashlib.sha256((kind + "\n" + json.dumps(payload, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()


def _ask(db: Database, jev, breaker: SpendBreaker | None, kind: str, state: dict, questions: dict) -> dict:
    """Jev's answers for this state, from the store when it has been asked before, otherwise asked once and stored."""
    key = _key(kind, {"state": state, "questions": questions})
    with db.conn() as c:
        row = c.execute("SELECT answers_json FROM readings WHERE key=?", (key,)).fetchone()
    if row:
        return json.loads(row["answers_json"])
    spend.guard(db, breaker)
    result = jev.ask(state, questions)
    with db.conn() as c:
        store.add_ai_usage(c, "jev", result.usage.get("input_tokens") or 0, 0, result.cost_usd)
        c.execute("INSERT OR REPLACE INTO readings (key, kind, answers_json, model, created_at) VALUES (?,?,?,?,?)",
                  (key, kind, json.dumps(result.answers), result.model, store.iso()))
    return result.answers


def read_clip(db: Database, jev, text: str, *, breaker: SpendBreaker | None = None) -> Reading | None:
    """Jev's reading of the words of one clip, or None when there are no words to read."""
    text = " ".join(text.split())[:4000]
    if len(text) < 20:
        return None
    questions = {
        "hook": {"type": "score", "instructions": "How strong is the opening of this clip as a hook?", "criteria": CLIP_CRITERIA["hook"]},
        "payoff": {"type": "score", "instructions": "Does the clip end on a payoff, or does it trail off?", "criteria": CLIP_CRITERIA["payoff"]},
        "stands_alone": {"type": "noul", "instructions": "Could a viewer who has not seen the rest of the video understand and enjoy this clip?"},
    }
    a = _ask(db, jev, breaker, "clip", {"clip_text": text}, questions)
    return Reading(hook=float(a["hook"]["score"]), payoff=float(a["payoff"]["score"]), stands_alone=float(a["stands_alone"]["noul"]))


def detect_kind(db: Database, jev, rules: Scoring, *, title: str, channel: str, excerpts: list[str],
                breaker: SpendBreaker | None = None) -> KindReading:
    """What kind of video this is. Jev weighs each kind on its own; the certainty is the winner's share of the weight."""
    state = {"title": title[:300], "channel": channel[:120], "excerpts": [" ".join(e.split())[:600] for e in excerpts[:3]]}
    questions = {k: {"type": "noul", "instructions": v["ask"]} for k, v in KINDS.items()}
    answers = _ask(db, jev, breaker, "kind", state, questions)
    weights = {k: max(0.0, float(answers[k]["noul"])) for k in KINDS}
    top = max(weights, key=lambda k: (weights[k], k))
    if weights[top] < 0.5:
        info, kind, certainty = OTHER, "other", 1 - weights[top]
        why = f"Jev found none of the known kinds a good match (best {weights[top]:.0%}), so this is treated as {info['label']}."
    else:
        info, kind, certainty = KINDS[top], top, weights[top] / sum(weights.values())
        why = f"Jev put {certainty:.0%} of the weight on {info['label']}."
    unsure = certainty < rules.type_certainty_min
    look = SAFE_LOOK if unsure else info["look"]
    note = (f" That is under the {rules.type_certainty_min:.0%} needed, so the plain look is used." if unsure
            else f" That clears {rules.type_certainty_min:.0%}, so the {look} look is used.")
    return KindReading(kind, info["label"], info["category"], round(certainty, 4), look, unsure, why + note)


# ---- the formula ---------------------------------------------------------------------------------------------------


def factor_for(rules: Scoring, category: str) -> float:
    for name, value in rules.category_factors.items():
        if name.strip().lower() == category.strip().lower():
            return value
    return rules.default_factor


def viral_score(rules: Scoring, engine_score: int, reading: Reading | None) -> tuple[int, str]:
    """The 0 to 100 score, and the sentence that explains it."""
    if reading is None:
        return engine_score, f"Viral {engine_score}: there were no words to read, so the engine's rating of {engine_score} is used alone."
    w = rules.engine_weight
    viral = round(w * engine_score + (1 - w) * reading.score)
    return viral, (f"Viral {viral}: the engine rated the moment {engine_score} and Jev's reading of its words was {reading.score} "
                   f"(hook {reading.hook:g} of 4, payoff {reading.payoff:g} of 4, stands alone {reading.stands_alone:.0%}), "
                   f"counted {w:.0%} engine and {1 - w:.0%} reading.")


def estimate_cpm(rules: Scoring, category: str, viral: int) -> tuple[float, str]:
    factor = factor_for(rules, category)
    cpm = round(rules.cpm_base * factor * viral / rules.viral_min, 2)
    return cpm, (f"Estimated CPM ${cpm:.2f} = ${rules.cpm_base:.2f} x the {category} factor {factor:g} x ({viral} / {rules.viral_min}); "
                 "it is an estimate until real results come back.")


def score_moment(rules: Scoring, moment: dict, reading: Reading | None, kind: KindReading, *, source: str = "paste",
                 research_bar: float | None = None, campaign_cpm: float | None = None) -> MomentScore:
    """Score one previewed moment (a dict from the engine's preview) for the way the video arrived."""
    if source not in SOURCES:
        raise ValueError(f"source must be one of {SOURCES}")
    viral, viral_why = viral_score(rules, int(moment["score"]), reading)
    if campaign_cpm is not None:
        cpm, cpm_why, estimated = round(campaign_cpm, 2), f"CPM ${campaign_cpm:.2f}: the campaign's stated pay per 1,000 views.", False
    else:
        cpm, cpm_why = estimate_cpm(rules, kind.category, viral)
        estimated = True
    revenue = round(rules.predicted_views * cpm / 1000, 2)
    rev_why = f"Expected revenue ${revenue:.2f} = {rules.predicted_views:,} predicted views x ${cpm:.2f} / 1,000."
    floor = {"paste": rules.cpm_floor, "research": max(rules.cpm_floor, research_bar or 0), "campaign": rules.cpm_floor}[source]
    problems = []
    if viral < rules.viral_min:
        problems.append(f"the viral score {viral} is under {rules.viral_min}")
    if cpm < floor:
        problems.append(f"the CPM ${cpm:.2f} is under the ${floor:.2f} floor")
    verdict = ("Will be made: " + f"viral {viral} and CPM ${cpm:.2f} clear {rules.viral_min} and ${floor:.2f}." if not problems
               else "Will not be made: " + " and ".join(problems) + ".")
    return MomentScore(
        id=int(moment["id"]), start=float(moment["start"]), end=float(moment["end"]), hook=moment.get("hook", ""),
        engine_score=int(moment["score"]), reading=reading, viral=viral, cpm=cpm, cpm_estimated=estimated,
        views=rules.predicted_views, revenue=revenue, make=not problems, explain=[viral_why, cpm_why, rev_why, verdict])


def score_video(db: Database, jev, rules: Scoring, preview: dict, *, source: str = "paste", research_bar: float | None = None,
                campaign_cpm: float | None = None, breaker: SpendBreaker | None = None) -> VideoScores:
    """Score a whole preview (the engine's {"video", "moments"}): the video's kind, then every moment, best revenue first."""
    video, moments = preview.get("video") or {}, preview.get("moments") or []
    excerpts = [m.get("text", "") for m in moments if m.get("text")]
    kind = detect_kind(db, jev, rules, title=video.get("title", ""), channel=video.get("channel", ""), excerpts=excerpts, breaker=breaker)
    scored = [score_moment(rules, m, read_clip(db, jev, m.get("text", ""), breaker=breaker), kind, source=source,
                           research_bar=research_bar, campaign_cpm=campaign_cpm) for m in moments]
    scored.sort(key=lambda s: (-s.revenue, -s.viral, s.id))
    return VideoScores(kind, scored)
