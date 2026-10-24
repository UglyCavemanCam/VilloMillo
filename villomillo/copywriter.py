"""Titles and captions for approved clips.

Claude writes three options from the clip's own words. Code then enforces the rules a model can break (length,
filler openings, clickbait phrases, hashtags), builds the final caption itself so the campaign's required tags are
always there, and Jev screens each option twice: does it claim anything the speaker did not say, and would it stop a
scrolling viewer. The most accurate and attention-grabbing option is chosen; the others stay for you to pick.
"""

from __future__ import annotations

import json
import re

from . import claude, secrets, spend, store
from .config import Posting, Profile, SpendBreaker
from .db import Database

TITLE_TARGET = 60
TITLE_HARD = 70
LINE_MAX = 100
MISLEADING_LIMIT = 0.3  # Jev's probability that an option claims something the speaker did not say; strict on purpose
FILLER_OPENERS = {"now", "so", "and", "but", "well", "um", "uh", "like", "okay", "ok", "yeah"}
CLICKBAIT = ("you won't believe", "you wont believe", "shocking", "gone wrong", "not clickbait", "must watch",
             "will blow your mind", "100% real")
HASHTAG = re.compile(r"^#[A-Za-z][A-Za-z0-9_]{1,29}$")
PLATFORM_NAMES = {"youtube_shorts": "YouTube Shorts"}
STOP_CRITERIA = [
    "Generic or confusing; gives no reason to stop.",
    "Dull; it names a topic without a point.",
    "Clear but unremarkable.",
    "Specific and intriguing; it promises a point worth hearing.",
    "A specific tension or payoff that makes people stop and watch.",
]


def option_problems(option: dict, platform: str) -> list[str]:
    """Why an option from Claude cannot be used. Empty means it passes every rule code can check."""
    problems = []
    title, idea, question = option["title"], option["idea"], option["question"]
    if platform == "youtube_shorts":
        if not title:
            problems.append("no title")
        else:
            if len(title) > TITLE_HARD:
                problems.append(f"title is {len(title)} characters; the limit is {TITLE_HARD}")
            if title.rstrip().endswith(("…", "...")):
                problems.append("title is cut off")
            letters = [ch for ch in title if ch.isalpha()]
            if len(letters) >= 4 and sum(ch.isupper() for ch in letters) / len(letters) > 0.7:
                problems.append("title is in capitals")
            if title.split()[0].strip(",.").lower() in FILLER_OPENERS:
                problems.append("title opens with filler")
            if "#" in title:
                problems.append("title has a hashtag in it")
    for phrase in CLICKBAIT:
        if phrase in f"{title} {idea}".lower():
            problems.append(f"clickbait phrase: {phrase}")
    if not idea or len(idea) > LINE_MAX:
        problems.append(f"the first line must be 1 to {LINE_MAX} characters")
    if not question or len(question) > LINE_MAX or not question.endswith("?"):
        problems.append("the question must be short and end with a question mark")
    tags = option["hashtags"]
    if not 2 <= len(tags) <= 4 or not all(HASHTAG.match(t) for t in tags):
        problems.append("hashtags must be 2 to 4 single words starting with #")
    return problems


def assemble(posting: Posting, platform: str, episode: str, option: dict) -> dict:
    """The final title, caption and tags for one option. The campaign's required tags are always added here."""
    required = posting.tags.get(platform, "")
    seen, hashtags = set(), []
    for tag in (["#Shorts"] if platform == "youtube_shorts" else []) + option["hashtags"]:
        if tag.lower() not in seen:
            seen.add(tag.lower())
            hashtags.append(tag)
    hashtag_line = " ".join(hashtags[:5])
    if platform == "youtube_shorts":
        lines = [option["idea"], option["question"], "", f"Clip from: {episode}", required, "", hashtag_line]
        keywords, size = [], 0
        for keyword in option["keywords"][:8]:
            if keyword and len(keyword) <= 30 and size + len(keyword) <= 400 and keyword not in keywords:
                keywords.append(keyword)
                size += len(keyword)
        return {"title": option["title"], "caption": "\n".join(lines).strip()[:5000], "tags": keywords or ["Shorts"]}
    raise ValueError(f"no copy rules for {platform}")


def screen(jev, clip_text: str, niche: str, options: list[dict]) -> tuple[list[dict], dict]:
    """Add Jev's two judgments to each option. Returns (options, usage) with `misleading` and `stop` set."""
    state = {
        "niche": {"definition": niche},
        "clip_text": clip_text[:4000],
        "options": [{"option": i, "title": o["title"], "first_line": o["idea"], "question": o["question"]}
                    for i, o in enumerate(options)],
    }
    questions = {}
    for i in range(len(options)):
        questions[f"misleading_{i}"] = {
            "type": "noul",
            "instructions": (f"Does option {i} (its title or first line) claim, imply or quote anything the speaker "
                             "did not actually say or mean in the clip text? Yes if it exaggerates, changes the "
                             "meaning or invents a claim."),
        }
        questions[f"stop_{i}"] = {
            "type": "score",
            "instructions": f"How likely is option {i}'s title and first line to make someone scrolling stop and watch?",
            "criteria": STOP_CRITERIA,
        }
    result = jev.ask(state, questions)
    screened = [{**o, "misleading": float(result.answers[f"misleading_{i}"]["noul"]),
                 "stop": int(result.answers[f"stop_{i}"]["score"])} for i, o in enumerate(options)]
    return screened, {"input_tokens": result.usage.get("input_tokens") or 0, "cost": result.cost_usd}


def _load(db: Database, profile_id: str, post_id: int):
    with db.conn() as c:
        post = c.execute("SELECT * FROM posts WHERE profile_id=? AND id=?", (profile_id, post_id)).fetchone()
        if post is None:
            raise LookupError("no such post")
        clip = c.execute("SELECT * FROM clips_ready WHERE id=?", (post["clip_id"],)).fetchone()
        video = c.execute("SELECT title FROM videos WHERE profile_id=? AND video_id=?",
                          (clip["profile_id"], clip["video_id"])).fetchone()
        has_options = c.execute("SELECT 1 FROM copy_options WHERE post_id=?", (post_id,)).fetchone() is not None
    return post, clip, (video["title"] if video else "the podcast"), has_options


def generate(db: Database, claude_client, jev, profile: Profile, post_id: int, *, breaker: SpendBreaker | None = None,
             force: bool = False) -> str:
    """Write, check and store the options for one post, and put the best one on it. Returns a short status."""
    post, clip, episode, has_options = _load(db, profile.id, post_id)
    if post["status"] in ("uploaded", "posted"):
        return "skipped: this post is already out"
    if post["status"] == "blocked":
        return "skipped: this post is blocked"
    if has_options and not force:
        return "skipped: options are already written"
    if (pause := spend.current(db, breaker or SpendBreaker())):
        return f"skipped: {pause.title.lower()}"

    platform = post["platform"]
    grade = json.loads(clip["grade_json"])
    facts = {
        "platform": PLATFORM_NAMES.get(platform, platform.replace("_", " ").title()),
        "channel": profile.display_name, "niche": profile.niche.definition, "episode_title": episode,
        "required_tags": profile.posting.tags.get(platform, ""),
        "clip_text": (grade.get("caption_text") or clip["hook"] or "")[:4000],
    }
    survivors, rejected = [], []
    for _ in range(2):  # one retry if nothing usable comes back
        options, cost = claude.copy_options(claude_client, facts)
        with db.conn() as c:
            store.add_ai_usage(c, "claude", 0, 0, cost)
        usable = []
        for option in options:
            problems = option_problems(option, platform)
            if problems:
                rejected.extend(problems)
            else:
                usable.append(option)
        if not usable:
            continue
        screened, usage = screen(jev, facts["clip_text"], profile.niche.definition, usable)
        with db.conn() as c:
            store.add_ai_usage(c, "jev", usage["input_tokens"], 0, usage["cost"])
        survivors = [o for o in screened if o["misleading"] < MISLEADING_LIMIT]
        rejected.extend(f"may misstate the speaker ({o['misleading']:.0%})" for o in screened
                        if o["misleading"] >= MISLEADING_LIMIT)
        if survivors:
            break
    if rejected:
        store.event(db, "info", "copy", f"post {post_id}: options dropped", reasons=rejected[:8])
    if not survivors:
        store.event(db, "warn", "copy", f"post {post_id}: kept the basic copy", reasons=rejected[:6])
        return "kept the basic copy: " + "; ".join(rejected[:3] or ["no options came back"])

    survivors.sort(key=lambda o: (-o["stop"], o["misleading"]))
    built = [{**assemble(profile.posting, platform, episode, o), "stop": o["stop"], "misleading": o["misleading"]}
             for o in survivors]
    with db.conn() as c:
        if force:
            c.execute("DELETE FROM copy_options WHERE post_id=?", (post_id,))
        for i, b in enumerate(built):
            c.execute("INSERT INTO copy_options (post_id, title, caption, tags_json, stop_score, misleading, chosen, "
                      "source, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (post_id, b["title"], b["caption"], json.dumps(b["tags"]), b["stop"], b["misleading"],
                       1 if i == 0 else 0, claude.MODEL, store.iso()))
        c.execute("UPDATE posts SET title=?, caption=?, tags_json=? WHERE id=?",
                  (built[0]["title"], built[0]["caption"], json.dumps(built[0]["tags"]), post_id))
    return f"{len(built)} options written; chose the one with hook score {built[0]['stop']}/5"


def choose(db: Database, profile_id: str, post_id: int, option_id: int) -> None:
    """Use a different option for a post that has not gone out yet."""
    with db.conn() as c:
        post = c.execute("SELECT status FROM posts WHERE profile_id=? AND id=?", (profile_id, post_id)).fetchone()
        option = c.execute("SELECT * FROM copy_options WHERE id=? AND post_id=?", (option_id, post_id)).fetchone()
        if post is None or option is None:
            raise LookupError("no such post or option")
        if post["status"] in ("uploaded", "posted"):
            raise ValueError("this post is already out; its copy can no longer change")
        c.execute("UPDATE copy_options SET chosen=(id=?) WHERE post_id=?", (option_id, post_id))
        c.execute("UPDATE posts SET title=?, caption=?, tags_json=? WHERE id=?",
                  (option["title"], option["caption"], option["tags_json"], post_id))


def generate_with_keys(db: Database, profile: Profile, post_id: int, *, jev=None, force: bool = False,
                       breaker: SpendBreaker | None = None) -> str:
    """`generate` with the Claude and Jev clients built from the keychain, for the CLI and the dashboard."""
    from .jev import Jev

    return generate(db, claude._client(), jev or Jev(secrets.get(secrets.JEV)), profile, post_id,
                    breaker=breaker, force=force)
