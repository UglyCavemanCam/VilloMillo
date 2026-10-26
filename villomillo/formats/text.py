"""The words the text layers put on screen: written by Claude, checked by code, screened by Jev.

Code checks every rule a model can break (length, filler openers, points that are not really in the transcript).
Jev then asks of each piece whether it claims anything the speaker did not say. A title that fails is rewritten up to twice;
a point or takeaway that fails is simply left out. If no title survives, no look is applied (the caller falls back).
"""

from __future__ import annotations

import re

from .. import claude
from . import effects
from .render import ClipText

TITLE_LINE_MAX = 30
LABEL_MAX = 22
TAKEAWAY_MAX = 34
MISLEADING_LIMIT = 0.3          # Jev's probability that a piece claims something the speaker did not say
FILLER = {"now", "so", "and", "but", "well", "um", "uh", "like", "okay", "ok", "yeah"}


class TextProblem(ValueError):
    pass


def norm(word: str) -> str:
    return re.sub(r"[^a-z0-9']", "", word.lower())


def find_phrase(words: list[dict], phrase: str, start: float) -> float | None:
    """Seconds into the clip where `phrase` (consecutive words) is first said, or None if it is not in the clip."""
    wanted = [norm(w) for w in phrase.split() if norm(w)]
    heard = [norm(w["text"]) for w in words]
    for i in range(len(heard) - len(wanted) + 1):
        if wanted and heard[i: i + len(wanted)] == wanted:
            return max(words[i]["t0"] - start, 0.0)
    return None


def check(raw: dict, words: list[dict], start: float, music_moods: tuple[str, ...] = ()) -> ClipText:
    """Turn Claude's reply into a ClipText, or say why it cannot be used."""
    lines = [str(x).strip() for x in (raw.get("title_lines") or []) if str(x).strip()][:2]
    if not lines:
        raise TextProblem("no title")
    for line in lines:
        if len(line) > TITLE_LINE_MAX or "#" in line:
            raise TextProblem(f"title line is too long or has a hashtag: {line!r}")
    if lines[0].split()[0].strip(",.").lower() in FILLER:
        raise TextProblem("the title opens with filler")
    points = []
    for p in (raw.get("points") or [])[:3]:
        label, at = str(p.get("label") or "").strip(), str(p.get("at") or "").strip()
        when = find_phrase(words, at, start) if label and at else None
        if label and len(label) <= LABEL_MAX and when is not None:
            points.append((label, when))
    points.sort(key=lambda p: p[1])
    takeaway = str(raw.get("takeaway") or "").strip()
    if len(takeaway) > TAKEAWAY_MAX:
        takeaway = ""
    proposed = []
    for e in [x for x in (raw.get("effects") or []) if isinstance(x, dict)][:effects.MAX_PROPOSED]:
        kind, at, reason = str(e.get("kind") or "").strip(), str(e.get("at") or "").strip(), str(e.get("reason") or "").strip()
        when = find_phrase(words, at, start) if at else None
        if kind in effects.KINDS and when is not None and reason and len(reason) <= effects.REASON_MAX:
            proposed.append((kind, when, reason))
    music = None
    if music_moods and isinstance(raw.get("music"), dict):
        mood, why = str(raw["music"].get("mood") or "").strip().lower(), str(raw["music"].get("reason") or "").strip()
        if mood in music_moods and why and len(why) <= effects.REASON_MAX:
            music = (mood, why)
    return ClipText(lines, points, takeaway, proposed, bool(raw.get("serious")), music)


def screen(jev, clip_words: str, text: ClipText) -> tuple[dict[str, float], dict]:
    """Jev's probability, per piece, that it claims or implies something the speaker did not say, and its usage."""
    questions = {"title": " ".join(text.title_lines)}
    for i, (label, _) in enumerate(text.points):
        questions[f"point_{i}"] = label
    if text.takeaway:
        questions["takeaway"] = text.takeaway
    ask = {name: {"type": "noul", "instructions": (
        f"Does the on-screen text for '{name}' claim, imply or quote anything the speaker did not actually say or "
        "mean in the clip transcript? Yes if it exaggerates, changes the meaning or invents a claim.")}
        for name in questions}
    result = jev.ask({"clip_transcript": clip_words[:4000], "on_screen_text": questions}, ask)
    usage = {"input_tokens": result.usage.get("input_tokens") or 0, "cost": result.cost_usd}
    return {name: float(result.answers[name]["noul"]) for name in questions}, usage


SERIOUS_LIMIT = 0.5             # Jev's probability that the subject is loss, injury, violence or tragedy
MOMENT_LIMIT = 0.5              # Jev's probability that a proposed effect really lands on an emphatic moment


def screen_moments(jev, clip_words: str, text: ClipText) -> tuple[list[bool], float, dict]:
    """Jev's answers on the proposed effects (is each one a real emphatic moment the speaker makes?) and on the subject
    (is it serious?). Returns (keep flags for the effects, the serious probability, usage)."""
    moments = {f"moment_{i}": f"{kind}: {why}" for i, (kind, _, why) in enumerate(text.proposed_effects)}
    ask = {name: {"type": "noul", "instructions": (
        f"Is this a real, emphatic moment (a strong claim, punchline or surprise) that the speaker actually makes at this point "
        f"in the clip transcript? Answer no if it is flat, invented, or the description does not match what was said. Described as: '{why}'")}
        for name, why in moments.items()}
    ask["serious"] = {"type": "noul", "instructions": (
        "Is the subject of this clip serious: loss, injury, illness, violence, a death or tragedy? Yes if so.")}
    result = jev.ask({"clip_transcript": clip_words[:4000], "moments": moments}, ask)
    keep = [float(result.answers[f"moment_{i}"]["noul"]) >= MOMENT_LIMIT for i in range(len(text.proposed_effects))]
    usage = {"input_tokens": result.usage.get("input_tokens") or 0, "cost": result.cost_usd}
    return keep, float(result.answers["serious"]["noul"]), usage


def make(claude_client, jev, *, channel: str, niche: str, episode: str, words: list[dict],
         start: float, music_moods: tuple[str, ...] = ()) -> tuple[ClipText, dict]:
    """The checked text for one clip, and what it cost: {"claude": usd, "jev": usd, "jev_tokens": n}.
    Raises TextProblem if no title can be trusted."""
    transcript = " ".join(w["text"] for w in words)
    facts = {"channel": channel, "niche": niche, "episode_title": episode, "transcript": transcript[:4000]}
    if music_moods:
        facts["music_moods"] = list(music_moods)
    cost, last = {"claude": 0.0, "jev": 0.0, "jev_tokens": 0}, "no reply"
    for _ in range(3):  # a title that fails is rewritten up to twice
        raw, spent = claude.clip_overlay_text(claude_client, facts)
        cost["claude"] += spent
        try:
            text = check(raw, words, start, music_moods)
        except TextProblem as e:
            last = str(e)
            continue
        odds, usage = screen(jev, transcript, text)
        cost["jev"] += usage["cost"]
        cost["jev_tokens"] += usage["input_tokens"]
        if odds["title"] >= MISLEADING_LIMIT:
            last = f"the title may misstate the speaker ({odds['title']:.0%})"
            continue
        text.points = [p for i, p in enumerate(text.points) if odds.get(f"point_{i}", 0) < MISLEADING_LIMIT]
        if odds.get("takeaway", 0) >= MISLEADING_LIMIT:
            text.takeaway = ""
        keep, serious_odds, moment_usage = screen_moments(jev, transcript, text)
        cost["jev"] += moment_usage["cost"]
        cost["jev_tokens"] += moment_usage["input_tokens"]
        text.proposed_effects = [e for e, ok in zip(text.proposed_effects, keep) if ok]
        text.serious = text.serious or serious_odds >= SERIOUS_LIMIT
        return text, cost
    raise TextProblem(last)
