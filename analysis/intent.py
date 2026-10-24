"""A clip direction: what the person asked the clips to be about (#106).

"Make sure there's a clip of when I died to the boss, prioritize funny
moments, and more from the WoW part around 1:35." Typed into the app's bottom
box (the assistant passes it as `focus` with the job) or sent on the API, it
becomes a ClipIntent:

- targets: a topic that gets talked about, or a moment that happened, each
  with the words someone would SAY when it comes up. The model writes those
  once, which is how "WoW" also finds "Azeroth" without a search index;
- spans: time ranges ("1:35-1:55", "around 45 minutes", "near the end"),
  read by this code and never by the model;
- styles: funny, laughing, hype, reactions, arguments, emotional;
- counts: "at least two clips about ...".

It only ever ADDS: points for clips that match, candidate windows where a
target is talked about or inside an asked-for range, and a lift to the quality
bar for a must-have that was actually found. Nothing is lowered or removed and
the scoring prompts never see it, so a video queued without a direction is
processed exactly as before (Colin, 2026-09-28: "I don't want it to interfere
with current stuff if you just want to clip the stream"). "Avoid the intro"
is understood and reported as not applied.

Nothing is invented. A must-have whose words are never said is reported as not
found, and no clip is made for it.

Pure stdlib, so it tests on a CI runner without numpy.
"""

import json
import re
from dataclasses import dataclass, field

MAX_CHARS = 600

# Points a clip gets, by how strongly it was asked for. Beside the other
# additive layers (creator context +6, audience hype +8, the game +12) a
# direction weighs more, because the person said so for this video.
TOPIC_POINTS = {"must": 15, "strong": 15, "prefer": 8}
RANGE_POINTS = {"must": 12, "strong": 12, "prefer": 6}
STYLE_POINTS = {"strong": 10, "prefer": 6}
# All of it together, for one clip: enough to tip close calls and lift what was
# asked for, not so much that the scoring underneath stops meaning anything.
CAP = 25

# Windows added for one target or range. A word said all stream long would
# otherwise send the model hundreds of windows to score.
WINDOWS_PER_TARGET = 12
WINDOW_SECONDS = 30.0
# "Around 1:35" covers five minutes either side of it.
POINT_REACH = 300.0
# "The intro", "near the end": the first or last 15% of the video.
EDGE_SHARE = 0.15
# A word said in more than a quarter of the transcript marks nothing.
TOO_COMMON = 0.25

STYLES = ("funny", "laughing", "hype", "reactions", "arguments", "emotional")
STRENGTHS = ("must", "strong", "prefer")
STYLE_LABELS = {"funny": "funny moments", "laughing": "laughing", "hype": "hype moments",
                "reactions": "reactions", "arguments": "arguments", "emotional": "emotional moments"}

# Said all the time in any stream, whatever it is about. "WoW" written with its
# capitals still counts; "wow" the interjection does not.
_FILLER = {
    "wow", "ok", "okay", "yeah", "yes", "no", "lol", "omg", "bro", "dude", "man", "like", "so",
    "game", "games", "play", "playing", "stream", "video", "chat", "guys", "thing", "stuff",
}
_STOP = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "for", "from", "with",
    "about", "into", "around", "near", "this", "that", "these", "those", "it", "its", "is", "was",
    "are", "were", "be", "been", "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "them", "his", "her", "their", "what", "when", "where", "which", "who", "how", "there",
    "please", "make", "sure", "clip", "clips", "clipping", "want", "wanted", "would", "like",
    "create", "show", "pick", "choose", "grab", "cut",
    "some", "more", "most", "moments", "moment", "part", "parts", "segment", "section", "bit",
    "find", "give", "get", "include", "including", "focus", "prioritize", "prioritise",
    "priority", "weight", "higher", "lots", "lot", "few", "least", "much", "many", "really",
    "very", "also", "then", "than", "just", "only", "all", "any", "one", "two", "three", "four",
    "five", "talk", "talks", "talked", "talking", "said", "say", "says", "happened",
    "stream", "video", "vod", "minutes", "minute", "hours", "hour", "mark", "start", "end",
    "funny", "funniest", "laughing", "laugh", "laughs", "hype", "reactions", "reaction",
    "arguments", "argument", "emotional", "stuff", "things", "thing", "got", "did",
    "do", "does", "can", "could", "should", "will", "not", "don't", "dont", "avoid", "skip",
    "starts", "started", "starting", "long", "short", "good", "best", "boring", "big", "little",
    "whole", "found", "going", "back", "here", "able", "let", "lets", "let's", "try", "maybe",
    "use", "used", "using", "top", "great", "cool", "nice", "has", "have", "had", "being",
    "first", "last", "next", "ever", "every", "each", "other", "onto",
    "strong", "stronger", "weak", "huge", "discussion", "discussions", "conversation", "bits",
    "sections", "segments", "wants", "need", "needs", "put", "add", "keep",
}

# Ordinary words that are part of many names ("Ocarina of Time", "Call of
# Duty"): never a search word on their own.
_COMMON = {
    "time", "world", "war", "wars", "day", "days", "night", "life", "home", "way", "people",
    "year", "years", "new", "old", "great", "first", "last", "best", "high", "low", "hand",
    "head", "house", "city", "land", "king", "queen", "lord", "legend", "story", "call", "duty",
    "dark", "light", "star", "stars", "final", "fantasy", "grand", "auto", "theft", "league",
    "legends", "counter", "strike", "battle", "boss", "died", "death", "dead", "clip", "when",
}

_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "a couple of": 2, "a couple": 2, "a few": 3}

_MUST = re.compile(r"\b(make sure|must|need|needs|definitely|include|including|don'?t miss|"
                   r"don'?t forget|has to|have to|guarantee)\b", re.I)
_STRONG = re.compile(r"\b(focus|prioriti[sz]e|priority|weight|mostly|more clips|lots of|a lot|"
                     r"really want|higher|heavily|especially)\b", re.I)
# An exclusion. "Don't miss" and "don't forget" ask for something, not against it.
_NEGATED = re.compile(r"\b(avoid|skip|without|exclude|leave out|never|no more|"
                      r"don'?t (?!miss|forget)|do not (?!miss|forget)|not (?:the|any))\b", re.I)

_STYLE_WORDS = {
    "funny": r"\b(funn(?:y|iest|ier)|hilarious|jokes?|joking|comed(?:y|ic)|humou?r)\b",
    "laughing": r"\b(laugh\w*|giggl\w*|cracking up)\b",
    "hype": r"\b(hype|hyped|crazy|insane|epic|intense|chat went|chat (?:going|goes) (?:crazy|wild)|"
            r"clutch|exciting)\b",
    "reactions": r"\b(react\w*|surprised?|shock\w*)\b",
    "arguments": r"\b(argu\w*|fights?|fighting|debat\w*|drama|beef|disagree\w*|heated)\b",
    "emotional": r"\b(emotional|sad|cry\w*|tears|wholesome|heartfelt|touching)\b",
}

# What a clip's own words show, for the styles no signal measures on its own.
_LAUGH_TEXT = re.compile(r"\b(haha\w*|hehe\w*|lmao|lol|laugh\w*|hilarious)\b|\[laugh|\(laugh", re.I)
_ARGUE_TEXT = re.compile(r"\b(no way|you'?re wrong|shut up|argu\w*|disagree\w*|that'?s not true|"
                         r"are you serious|what are you talking about)\b", re.I)
_EMOTION_TEXT = re.compile(r"\b(cry\w*|tears|love you|miss (?:you|him|her|them)|proud of|"
                           r"thank you so much|emotional|means a lot)\b", re.I)

_CLOCK = r"\d{1,2}:\d{2}(?::\d{2})?"
# Each unit ends on a word boundary: "5 more clips" is not five minutes.
_VERBAL = (r"\d+(?:\.\d+)?\s*(?:h|hr|hrs|hours?)\b(?:\s*(?:and\s*)?\d+\s*(?:m|mins?|minutes?)\b)?"
           r"|\d+\s*(?:m|mins?|minutes?)\b(?:\s*(?:and\s*)?\d+\s*(?:s|secs?|seconds?)\b)?")
_TIME = rf"(?:{_CLOCK}|{_VERBAL})"
_RANGE = re.compile(rf"({_TIME})\s*(?:-|–|—|to|until|till|through)\s*({_TIME})", re.I)
_POINT = re.compile(rf"(?<![\d:])({_TIME})(?![\d:])", re.I)
_FIRST_PART = re.compile(r"\b(?:the )?(?:intro|beginning|opening)\b|\b(?:at|near|from) the start\b|"
                         r"\bfirst (?:part|few minutes|bit)\b", re.I)
_LAST_PART = re.compile(r"\b(?:near|at|towards?) the end\b|\bthe (?:ending|outro|last part)\b|"
                        r"\bend of the (?:stream|video)\b|\blast (?:part|few minutes|bit)\b", re.I)
_COUNT = re.compile(r"\b(?:at least|minimum(?: of)?|min(?:imum)?\.?)?\s*"
                    r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|a couple(?: of)?|a few)"
                    r"\s+(?:more\s+|good\s+|funny\s+|short\s+)?clips?\b", re.I)

SCHEMA = {
    "type": "object",
    "properties": {
        "targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "what": {"type": "string"},
                    "kind": {"type": "string", "enum": ["topic", "moment"]},
                    "strength": {"type": "string", "enum": list(STRENGTHS)},
                    "terms": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["what", "kind", "strength", "terms"],
                "additionalProperties": False,
            },
        },
        "styles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "style": {"type": "string", "enum": list(STYLES)},
                    "strength": {"type": "string", "enum": ["strong", "prefer"]},
                },
                "required": ["style", "strength"],
                "additionalProperties": False,
            },
        },
        "avoid": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["targets", "styles", "avoid"],
    # Strict structured output (OpenAI, and OpenRouter models that take it)
    # refuses a schema without this on every object. A refusal falls back to
    # plain JSON and is remembered for the model, so the clip scoring's own
    # schema would be dropped with it for the rest of the session.
    "additionalProperties": False,
}

PROMPT = """Someone is using a video clipping app. They wrote what they want clipped from ONE video (a stream or recording of them). Turn it into JSON. Only use what they wrote: never add topics or moments of your own.

- targets: each thing they want clips of.
  - what: a short name for it, in their words.
  - kind: "moment" for one thing that happened (a death, a win, a reveal), "topic" for a subject they talk about.
  - strength: "must" when they say make sure / include / need; "strong" for focus on / prioritize / more of; otherwise "prefer".
  - terms: 3 to 10 words or short phrases someone would SAY out loud in the video when this comes up: the name, its abbreviation, nicknames, places, characters and closely related words. (For example, "Minecraft" would get "Minecraft", "creeper", "Nether", "diamonds", "Ender Dragon", "villager"; a cake disaster would get "cake", "oven", "burnt", "collapsed", "frosting".) Keep an abbreviation's capital letters.
- styles: which of funny, laughing, hype, reactions, arguments, emotional they asked for, each with strength "strong" or "prefer".
- avoid: anything they asked to leave out or avoid, in their words.

If they only gave a time or a style, "targets" is an empty list. Never copy the examples above into your answer.
Leave times ("1:35", "around 45 minutes") and numbers of clips out of everything: those are read separately.

Answer with only the JSON object: {"targets": [...], "styles": [...], "avoid": [...]}

What they wrote:
\"\"\"{text}\"\"\"
"""


@dataclass
class Target:
    what: str
    kind: str = "topic"  # topic | moment
    strength: str = "prefer"  # must | strong | prefer
    terms: list[str] = field(default_factory=list)
    count: int = 0  # "at least N clips" of it; 0 when no number was given
    spots: list[float] = field(default_factory=list)  # where it's said, set by locate()
    patterns: list = field(default_factory=list, repr=False)

    def label(self) -> str:
        return self.what


@dataclass
class Span:
    start: float
    end: float
    strength: str = "prefer"
    count: int = 0
    words: str = ""  # what they wrote for it

    def label(self) -> str:
        return f"{clock(self.start)}-{clock(self.end)}"

    def holds(self, c) -> bool:
        return self.start <= (c.start + c.end) / 2 <= self.end


@dataclass
class ClipIntent:
    text: str
    targets: list[Target] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    styles: dict[str, str] = field(default_factory=dict)  # style -> strong | prefer
    not_applied: list[str] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    fallback: bool = False  # the model's answer was unusable; read from the words
    boosted: int = 0
    added: int = 0
    required: int = 0

    def understood(self) -> list[str]:
        lines = []
        for t in self.targets:
            head = "Must include" if t.strength == "must" or t.count else (
                "Focus on" if t.strength == "strong" else "More of")
            lines.append(f"{head}: {t.what}{_at_least(t.count)}")
        for s in self.spans:
            head = "Must include" if s.strength == "must" or s.count else (
                "Focus on" if s.strength == "strong" else "More from")
            lines.append(f"{head}: {s.label()}{_at_least(s.count)}")
        for style, strength in self.styles.items():
            lines.append(f"{'Focus on' if strength == 'strong' else 'More'} {STYLE_LABELS[style]}")
        return lines

    def report(self) -> dict:
        """What was understood and done, for the video's outcome and the UI."""
        return {
            "direction": self.text,
            "understood": self.understood(),
            "not_found": list(self.not_found),
            "not_applied": list(self.not_applied),
            "notes": list(self.notes),
            "boosted": self.boosted,
            "added_windows": self.added,
            "required": self.required,
        }


def _at_least(n: int) -> str:
    # One is what a must-have already means, so only a real count is shown.
    return f" (at least {n} clips)" if n > 1 else ""


def clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


# ---- reading the direction ---------------------------------------------------


def parse(text: str, llm, duration: float) -> ClipIntent | None:
    """The direction as a ClipIntent, or None when nothing was written.

    One model call, for what only a model can do (the targets and the words
    that mark them). Times, counts and exclusions are read here, and a model
    answer that comes back empty or broken falls back to the words themselves,
    so a direction always counts for something."""
    text = " ".join(str(text or "").split())[:MAX_CHARS]
    if not text:
        return None
    intent = ClipIntent(text=text)
    clauses = _clauses(text)
    overall = _strength(text)
    for clause in clauses:
        if _NEGATED.search(clause):
            intent.not_applied.append(clause)
            continue
        intent.spans += _spans(clause, duration, overall)
    data = _ask(llm, text)
    avoid = [a.lower() for a in (_words_of(x) for x in (data or {}).get("avoid", []) or []) if a]
    for raw in (data or {}).get("targets", []) or []:
        t = _target(raw)
        if t is None:
            continue
        # Something they asked to leave out is not a target, whatever the model did.
        if any(t.what.lower() in a or a in t.what.lower() for a in avoid):
            continue
        if any(t.what.lower() in c.lower() for c in intent.not_applied):
            continue
        # Only what they actually wrote. A small model can hand back a topic
        # from nowhere (gemma:7b answered "1:35 to 1:55" with a whole game), so
        # a target has to share a real word with the direction.
        if not _grounded(t, text):
            continue
        # "Must" only when their words say so: a model reads "clips about X" as
        # a must-have on one run and a preference on the next.
        if t.strength == "must" and not _MUST.search(text):
            t.strength = "strong"
        intent.targets.append(t)
    # Styles are the ones their words name. A model asked for styles tends to
    # tick every box going (all six, for "funny moments and laughing"), so it
    # only gets to say how strongly one they named was meant.
    said = {}
    for raw in (data or {}).get("styles", []) or []:
        if isinstance(raw, dict) and str(raw.get("style", "")).lower() in STYLES:
            said[str(raw["style"]).lower()] = str(raw.get("strength", "prefer"))
    for clause in clauses:
        if _NEGATED.search(clause):
            continue
        for style, pattern in _STYLE_WORDS.items():
            if re.search(pattern, clause, re.I) and style not in intent.styles:
                strong = _STRONG.search(clause) or said.get(style) == "strong"
                intent.styles[style] = "strong" if strong else "prefer"
    for a in avoid:
        if not any(a in c.lower() for c in intent.not_applied):
            intent.not_applied.append(a)
    if not intent.targets:
        # No usable answer (a small model's JSON can come back empty, or with
        # nothing that was really asked): each part of the direction that names
        # something still counts, read from its own words.
        for clause in clauses:
            if _NEGATED.search(clause):
                continue
            words = _content_words(clause, [])
            if words:
                intent.fallback = True
                named = _MUST.search(clause) or _STRONG.search(clause)
                intent.targets.append(Target(what=" ".join(words[:4]),
                                             strength=_strength(clause) if named else overall,
                                             terms=words))
    _counts(intent, clauses)
    return intent


def _ask(llm, text: str) -> dict | None:
    from llm.base import generate_json

    try:
        raw = generate_json(llm, PROMPT.replace("{text}", text.replace('"""', "'")), SCHEMA)
    except Exception as e:  # a direction must never stop the video being clipped
        print(f"  Clip direction: the model couldn't read it ({e}); using its words")
        return None
    return _json_object(raw)


def _json_object(raw: str) -> dict | None:
    if not raw:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _target(raw) -> Target | None:
    if not isinstance(raw, dict):
        return None
    what = " ".join(str(raw.get("what", "")).split())[:80]
    if not what:
        return None
    terms = []
    for term in raw.get("terms") or []:
        term = " ".join(str(term).split())[:40]
        # A time copied into the terms would match every clock on screen, and
        # a word like "discussion" or "stream" (given for "the WoW discussion")
        # would match clips about anything.
        generic = " " not in term and term.lower() in _STOP and term == term.lower()
        if term and not re.search(r"\d:\d", term) and not generic and term not in terms:
            terms.append(term)
    if not terms:
        terms = _content_words(what, [])
    # "Ocarina of Time" is often just "Ocarina" when it's said: a name's own
    # distinctive word counts too. "Time" and "Call" on their own would match
    # half the stream, so common words never do.
    for phrase in [what]:
        words = phrase.split()
        if len(words) < 2:
            continue
        for w in words:
            w = w.strip(".,!?'\"")
            if (len(w) >= 4 and w[0].isupper() and w.lower() not in _STOP
                    and w.lower() not in _COMMON and w not in terms):
                terms.append(w)
    if not terms:
        return None  # a style ("funny moments") or a filler word, not something said
    strength = str(raw.get("strength", "prefer"))
    kind = str(raw.get("kind", "topic"))
    return Target(
        what=what,
        kind=kind if kind in ("topic", "moment") else "topic",
        strength=strength if strength in STRENGTHS else "prefer",
        terms=terms[:12],
    )


_STYLE_ONLY = re.compile("|".join(_STYLE_WORDS.values()) + r"|\bmoments?\b|\bparts?\b", re.I)


def _grounded(t: Target, text: str) -> bool:
    """Whether this target is something they wrote about, and not a style
    ("funny reactions") or a topic the model brought itself."""
    low = text.lower()
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z']+", t.what)
             if w.lower() not in _STOP and not _STYLE_ONLY.fullmatch(w)]
    if not words:
        return False
    # One of its words, or the start of one ("dying" for "died"): the model's
    # name for it may differ a little from theirs, but not in every word.
    return any(re.search(rf"(?<!\w){re.escape(w.lower()[:max(4, len(w) - 2)])}", low) for w in words)


def _words_of(raw) -> str:
    """A model's list item as text: some answer {"text": "..."} for a string."""
    if isinstance(raw, dict):
        raw = next((v for v in raw.values() if isinstance(v, str)), "")
    return " ".join(str(raw or "").split())


def _clauses(text: str) -> list[str]:
    # "between 1:35 and 1:55" is one range, not two clauses.
    text = re.sub(rf"between\s+({_TIME})\s+and\s+({_TIME})", r"\1-\2", text, flags=re.I)
    parts = re.split(r"[.;!?\n]+|,\s*(?:and|but|then)?\s*|\s+(?:and|but|then|also)\s+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _strength(text: str) -> str:
    if _MUST.search(text):
        return "must"
    return "strong" if _STRONG.search(text) else "prefer"


def _seconds(token: str, duration: float, hours_first: bool | None = None) -> float | None:
    token = token.strip().lower()
    if ":" in token:
        parts = [int(p) for p in token.split(":")]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        a, b = parts
        if hours_first is None:
            # "1:35" in a three-hour stream is an hour and 35 minutes; in a
            # twenty-minute video it is a minute and 35 seconds.
            hours_first = duration > 3600 and a < 10 and a * 3600 + b * 60 <= duration + 60
        return a * 3600 + b * 60 if hours_first else a * 60 + b
    total = 0.0
    found = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|mins?|minutes?|s|secs?|seconds?)", token):
        found = True
        n = float(value)
        total += n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
    return total if found else None


def _spans(clause: str, duration: float, overall: str) -> list[Span]:
    strength = _strength(clause) if (_MUST.search(clause) or _STRONG.search(clause)) else (
        "strong" if re.search(r"\bmore\b", clause, re.I) else overall)
    end_of = duration if duration > 0 else None
    spans: list[Span] = []
    taken = clause
    for m in _RANGE.finditer(clause):
        a_tok, b_tok = m.group(1), m.group(2)
        hours_first = None
        if ":" in a_tok and a_tok.count(":") == 1:
            a_parts = [int(p) for p in a_tok.split(":")]
            hours_first = duration > 3600 and a_parts[0] < 10 and a_parts[0] * 3600 + a_parts[1] * 60 <= duration + 60
        a = _seconds(a_tok, duration, hours_first)
        b = _seconds(b_tok, duration, hours_first)
        if a is None or b is None or b <= a:
            continue
        if end_of is not None:
            b = min(b, end_of)
        if end_of is None or a < end_of:
            spans.append(Span(a, b, strength, words=m.group(0)))
        taken = taken.replace(m.group(0), " ")
    for m in _POINT.finditer(taken):
        t = _seconds(m.group(1), duration)
        if t is None or (end_of is not None and t > end_of):
            continue
        lo = max(0.0, t - POINT_REACH)
        hi = t + POINT_REACH if end_of is None else min(end_of, t + POINT_REACH)
        spans.append(Span(lo, hi, strength, words=m.group(1)))
    if end_of:
        if _FIRST_PART.search(clause):
            spans.append(Span(0.0, end_of * EDGE_SHARE, strength, words="the start"))
        if _LAST_PART.search(clause):
            spans.append(Span(end_of * (1 - EDGE_SHARE), end_of, strength, words="the end"))
    return spans


def _counts(intent: ClipIntent, clauses: list[str]) -> None:
    """"At least two clips from the WoW part": the number goes to the range or
    target named in the same clause."""
    for clause in clauses:
        m = _COUNT.search(clause)
        if not m or _NEGATED.search(clause):
            continue
        word = m.group(1).lower()
        n = int(word) if word.isdigit() else _NUMBERS.get(word, 0)
        if n <= 0:
            continue
        n = min(n, 20)
        span = next((s for s in intent.spans if s.words and s.words in clause), None)
        if span is not None:
            span.count = max(span.count, n)
            continue
        low = clause.lower()
        target = next((t for t in intent.targets
                       if any(w in low for w in t.what.lower().split() if w not in _STOP)
                       or any(term.lower() in low for term in t.terms)), None)
        if target is None and len(intent.targets) == 1:
            target = intent.targets[0]
        if target is not None:
            target.count = max(target.count, n)


def _content_words(text: str, excluded: list[str]) -> list[str]:
    for clause in excluded:
        text = text.replace(clause, " ")
    text = re.sub(rf"{_TIME}", " ", text, flags=re.I)
    words = []
    for w in re.findall(r"[A-Za-z][A-Za-z'+-]*[A-Za-z]|[A-Za-z]", text):
        low = w.lower()
        # "WoW" and "GTA" are names, whatever "wow" the word is.
        named = any(ch.isupper() for ch in w[1:])
        if not named and (len(w) < 3 or low in _STOP or low in _FILLER):
            continue
        if w not in words:
            words.append(w)
    return words


# ---- finding it in the video ---------------------------------------------------


def _pattern(term: str):
    term = term.strip()
    if len(term) < 2:
        return None
    # "WoW", "GTA", "OoT" keep their capitals, so "wow" the word is not them.
    exact = any(ch.isupper() for ch in term[1:])
    if not exact and term.lower() in _FILLER:
        return None
    body = r"\s+".join(re.escape(w) for w in term.split())
    return re.compile(rf"(?<!\w){body}(?:'s|s|es|ed|ing)?(?!\w)", 0 if exact else re.IGNORECASE)


def locate(intent: ClipIntent, segments) -> None:
    """Where each target is said, across the whole transcript: every chunk of a
    long stream, not only the ones a model happened to be shown."""
    n = len(segments)
    for t in intent.targets:
        patterns = []
        for term in t.terms:
            p = _pattern(term)
            if p is None:
                continue
            hits = sum(1 for s in segments if p.search(s.text))
            if n >= 20 and hits > TOO_COMMON * n:
                continue  # said all stream long: it marks nothing
            patterns.append(p)
        t.patterns = patterns
        t.spots = [s.start for s in segments if any(p.search(s.text) for p in patterns)]


def mentions(t: Target, text: str) -> bool:
    return any(p.search(text) for p in t.patterns)


def windows(intent: ClipIntent, existing, duration: float) -> list[tuple[float, float]]:
    """Candidate windows for what was asked, where no candidate covers it yet.

    The chunk pass proposes only so many moments; a topic it passed over could
    otherwise never be picked however many points it was offered. Each is a
    plain window here: fusion fits it to sentences and has it scored exactly
    as a signal peak is."""
    taken = [(c.start, c.end) for c in existing]
    out: list[tuple[float, float]] = []

    def free(a: float, b: float) -> bool:
        for s, e in taken + out:
            overlap = min(b, e) - max(a, s)
            if overlap > 0.5 * min(b - a, e - s):
                return False
        return True

    for t in intent.targets:
        added = 0
        for first, last, _hits in _clusters(t.spots):
            if added >= WINDOWS_PER_TARGET:
                break
            if any(s <= first <= e for s, e in taken):
                continue  # a candidate already has it: the points will reach it
            a = max(0.0, first - 5.0)
            b = max(a + WINDOW_SECONDS, min(last + 10.0, a + 60.0))
            if duration > 0:
                b = min(b, duration)
            if b - a >= 5.0 and free(a, b):
                out.append((a, b))
                added += 1
    for span in intent.spans:
        tiles = []
        a = span.start
        while a + 10.0 <= span.end:
            b = min(a + WINDOW_SECONDS, span.end)
            if free(a, b):
                tiles.append((a, b))
            a += WINDOW_SECONDS + 10.0
        if len(tiles) > WINDOWS_PER_TARGET:
            step = len(tiles) / WINDOWS_PER_TARGET
            tiles = [tiles[int(i * step)] for i in range(WINDOWS_PER_TARGET)]
        out += tiles
    return out


def _clusters(spots: list[float], gap: float = 30.0) -> list[tuple[float, float, int]]:
    """Mentions close together are one stretch of talk; the longest first."""
    groups: list[list[float]] = []
    for t in sorted(spots):
        if groups and t - groups[-1][-1] <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    return sorted(((g[0], g[-1], len(g)) for g in groups), key=lambda x: -x[2])


# ---- what a clip gets for it -----------------------------------------------------


def bonus(intent: ClipIntent, c, clip_text: str) -> tuple[int, list[str]]:
    """Points this clip gets from the direction, and why. Never negative."""
    points = 0
    why: list[str] = []
    for t in intent.targets:
        if mentions(t, clip_text):
            points += TOPIC_POINTS.get(t.strength, 8)
            why.append(f"about {t.what}")
    for span in intent.spans:
        if span.holds(c):
            points += RANGE_POINTS.get(span.strength, 6)
            why.append(f"in {span.label()}")
    s = c.subscores or {}
    for style, strength in intent.styles.items():
        share = _style_share(style, s, clip_text, bool(getattr(c, "trending", False)))
        got = round(STYLE_POINTS.get(strength, 6) * share)
        if got > 0:
            points += got
            why.append(style)
    return min(CAP, max(0, points)), why


def _style_share(style: str, s: dict, text: str, trending: bool) -> float:
    """How much a clip shows a style, 0..1, from signals the scorer already
    measured. A style nothing measures gets nothing rather than a guess."""
    audio = s.get("audio", 0) / 100.0
    if style in ("funny", "laughing"):
        # Audio excitement is bursts and noisiness: laughter and applause.
        share = max(0.0, (audio - 0.5) / 0.5)
        if _LAUGH_TEXT.search(text):
            share = max(share, 0.6)
        return min(1.0, share)
    if style == "hype":
        share = max(0.0, (max(audio, s.get("engagement", 0) / 100.0) - 0.6) / 0.4)
        if s.get("hype"):
            share = max(share, 0.8)
        return min(1.0, share)
    if style == "reactions":
        if not s.get("reaction_measured"):
            return 0.0
        return min(1.0, max(0.0, (s.get("reaction", 0) / 100.0 - 0.55) / 0.45))
    if style == "arguments":
        return 0.8 if trending else (0.6 if _ARGUE_TEXT.search(text) else 0.0)
    if style == "emotional":
        return 0.6 if _EMOTION_TEXT.search(text) else 0.0
    return 0.0


def require(intent: ClipIntent, candidates, text_of, min_score: int, max_overlap: float) -> None:
    """A must-have, or "at least N", that was found is kept.

    The best-scoring clips that match it are marked required and lifted to the
    quality bar, so neither the bar nor a clip cap drops them; overlap rules
    still apply. One that was never said is reported, and nothing is forced."""
    goals: list[tuple[str, int, object]] = []
    for t in intent.targets:
        n = max(t.count, 1 if t.strength == "must" else 0)
        if n:
            goals.append((t.what, n, lambda c, t=t: mentions(t, text_of(c))))
    for span in intent.spans:
        n = max(span.count, 1 if span.strength == "must" else 0)
        if n:
            goals.append((span.label(), n, span.holds))
    for label, n, matches in goals:
        chosen = []
        for c in sorted((c for c in candidates if matches(c)), key=lambda c: c.score, reverse=True):
            if len(chosen) >= n:
                break
            if any(c.overlap_ratio(k) > max_overlap for k in chosen):
                continue
            chosen.append(c)
        if not chosen:
            intent.not_found.append(label)
            continue
        if len(chosen) < n:
            intent.notes.append(f"Only {len(chosen)} of {n} clips found for {label}")
        for c in chosen:
            s = c.subscores if c.subscores is not None else {}
            c.subscores = s
            if not s.get("required"):
                intent.required += 1
            s["required"] = label
            if c.score < min_score:
                s["intent_lift"] = min_score - c.score
                c.score = min_score


def is_required(c) -> bool:
    return bool((c.subscores or {}).get("required"))
