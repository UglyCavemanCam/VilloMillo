"""AI Generate: three format designs made from a few answers, checked by the same rules as a hand-made design, each with a preview.

What the AI may choose comes from the playbook for the kind of clip (config/villomillo/format_playbooks.yaml, the reasons are in
docs/FORMAT-PLAYBOOKS.md). The AI is also shown what you liked and disliked before. Every option is checked three ways: the
settings, the playbook, and the preview picture itself (captions must not cover a face). Nothing is saved until one is picked.
"""

from __future__ import annotations

import json
import pathlib

import yaml

from ..config import ROOT
from . import blueprints, captions, designs, effects, fonts, placement, preview

PLAYBOOKS = ROOT / "config" / "villomillo" / "format_playbooks.yaml"
KINDS = {"podcast": "Podcasts", "talk": "Talks", "stream": "Stream clips", "other": "Other"}
FEELS = {"calm": "Calm: quiet captions, subtle effects only", "balanced": "Balanced: clear captions, moderate effects",
         "high": "High energy: bold captions, punchier effects"}
FEEL_EFFECT_CAP = {"calm": "subtle", "balanced": "balanced", "high": "punchy"}
LEVELS = ["off", "subtle", "balanced", "punchy"]
OPTIONS = 3
COST_LIMIT_USD = 0.25   # one generate may not cost more than this in AI requests
PREVIEW_TEXT_OK = {"words", "title", "points", "takeaway"}
FEEDBACK_SHOWN = 6      # how many liked and disliked looks the AI is shown
# caption styles that rely on a dark picture: they are refused where the caption sits over a bright part of the frame
DARK_FOOTAGE_ONLY = {"neon", "type", "marker"}
BRIGHT_BEHIND = 140      # the average brightness (0 to 255) behind a caption above which these styles stop being readable


class GenerateError(ValueError):
    """The message says what to do, in plain words."""


def playbooks(path: pathlib.Path | None = None) -> dict:
    return yaml.safe_load((path or PLAYBOOKS).read_text(encoding="utf-8"))


def playbook(kind: str, path: pathlib.Path | None = None) -> dict:
    return playbooks(path)["kinds"][kind]


def check_answers(answers: dict) -> dict:
    kind, feel = str(answers.get("kind", "")), str(answers.get("feel", ""))
    if kind not in KINDS:
        raise GenerateError("Pick the kind of clips this is for.")
    if feel not in FEELS:
        raise GenerateError("Pick how it should feel.")
    logo_id = answers.get("logo_id")
    note = str(answers.get("note") or "").strip()[:200]
    return {"kind": kind, "feel": feel, "campaign": bool(answers.get("campaign")),
            "logo_id": int(logo_id) if logo_id not in (None, "", 0, "0") else None, "note": note}


def allowed_styles(kind: str, feel: str, path: pathlib.Path | None = None) -> list[str]:
    """The caption styles that are both right for the kind of clip and right for the feel."""
    styles = playbook(kind, path)["caption_styles"]
    feel_ok = playbooks(path)["feel_styles"][feel]
    return [s for s in styles if s in feel_ok]


def catalog(kind: str, feel: str, path: pathlib.Path | None = None) -> dict:
    """What the AI may choose from for this kind of clip and feel. Anything outside it is refused by the checks."""
    book = playbook(kind, path)
    levels = LEVELS[: LEVELS.index(book["effects_max"]) + 1]
    levels = [lv for lv in levels if LEVELS.index(lv) <= LEVELS.index(FEEL_EFFECT_CAP[feel])]
    return {
        "caption_styles": [{"id": s, "label": captions.STYLES[s].label, "blurb": captions.STYLES[s].blurb}
                           for s in allowed_styles(kind, feel, path)],
        "fonts": [{"id": f.id, "label": f.label} for f in fonts.FONTS.values()],
        "layouts": book["layouts"],
        "caption_positions": book["positions"],
        "effect_levels": levels,
        "effect_kinds": book["effect_kinds"],
        "music_modes": ["off", "when_helps"],
        "headline_text_contrast_min": playbooks(path)["min_headline_contrast"],
    }


def recent_feedback(db, kind: str, limit: int = FEEDBACK_SHOWN) -> dict:
    """The looks the owner liked and disliked for this kind of clip, newest first, as short descriptions."""
    with db.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT verdict, design_json FROM format_feedback WHERE kind=? ORDER BY id DESC LIMIT ?",
                                           (kind, limit * 4))]
    out = {"liked": [], "disliked": []}
    for r in rows:
        bucket = "liked" if r["verdict"] == "more" else "disliked"
        if len(out[bucket]) < limit:
            d = json.loads(r["design_json"])
            out[bucket].append({"captions": d["captions"]["style"], "layout": d["layout"],
                                "effects": d["effects"]["intensity"], "headline_colour": d["headline"]["background"]})
    return out


def brief(answers: dict, db=None, path: pathlib.Path | None = None) -> dict:
    """What the AI is told. design_shape is the exact layout of a design: a reply copies it, with only the keys it changes."""
    kind, feel = answers["kind"], answers["feel"]
    book = playbook(kind, path)
    return {"clip_kind": KINDS[kind], "feel": FEELS[feel], "campaign_clips": answers["campaign"],
            "logo": answers["logo_id"] is not None, "owner_note": answers["note"],
            "playbook_rules": book["rules"], "catalog": catalog(kind, feel, path),
            "looks_the_owner_liked": recent_feedback(db, kind)["liked"] if db is not None else [],
            "looks_the_owner_disliked": recent_feedback(db, kind)["disliked"] if db is not None else [],
            "design_shape": designs.DEFAULT}


def _unknown_settings(part, base, path: str = "") -> None:
    """A setting name the app does not have is refused, not quietly ignored: the look would not be what the AI described."""
    if not isinstance(part, dict):
        raise designs.DesignError(f"'{path or 'the design'}' must be a set of settings")
    for key, value in part.items():
        if key not in base:
            raise designs.DesignError(f"'{path + key}' is not a setting the app has")
        if isinstance(base[key], dict):
            _unknown_settings(value, base[key], path + key + ".")


def _luminance(hex_rgb: str) -> float:
    def channel(v: int) -> float:
        v = v / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    h = hex_rgb.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def checked(partial: dict, answers: dict, db, folder=None, path: pathlib.Path | None = None) -> dict:
    """One design, made safe for these answers and this kind's playbook. Raises designs.DesignError with the reason."""
    partial = {k: v for k, v in partial.items() if k != "elements"} if isinstance(partial, dict) else partial   # the AI works in the old sections; the elements are worked out from them again at the end
    _unknown_settings(partial, designs.DEFAULT)
    d = designs.normalize(partial)
    d.pop("elements")
    kind, feel = answers["kind"], answers["feel"]
    book, rules = playbook(kind, path), playbooks(path)
    styles = allowed_styles(kind, feel, path)
    if d["captions"]["style"] not in styles:
        raise designs.DesignError(f"the caption style {d['captions']['style']} does not suit {KINDS[kind].lower()} "
                                  f"at this feel; use one of: {', '.join(styles)}")
    if d["layout"] not in book["layouts"]:
        raise designs.DesignError(f"the layout {d['layout']} does not suit {KINDS[kind].lower()}; use one of: {', '.join(book['layouts'])}")
    if d["captions"]["position"] not in book["positions"]:
        raise designs.DesignError(f"captions in the {d['captions']['position']} place do not suit {KINDS[kind].lower()}")
    if d["headline"]["enabled"] and d["headline"]["background"]:
        ratio = contrast(d["headline"]["color"], d["headline"]["background"])
        if ratio < rules["min_headline_contrast"]:
            raise designs.DesignError(f"the headline text is only {ratio:.2f} to 1 against its box; it needs at least "
                                      f"{rules['min_headline_contrast']} to 1 to be readable")
    cap = min(LEVELS.index(book["effects_max"]), LEVELS.index(FEEL_EFFECT_CAP[feel]))
    if LEVELS.index(d["effects"]["intensity"]) > cap:
        d["effects"]["intensity"] = LEVELS[cap]   # too strong for this kind or feel: brought down, never left as it was
    d["effects"]["kinds"] = [k for k in d["effects"]["kinds"] if k in book["effect_kinds"]]
    if d["music"]["mode"] == "always":   # the AI never turns "always" on by itself: it only ever suggests "only when it helps"
        d["music"]["mode"] = "when_helps"
    if answers["campaign"]:
        d["brand"] = {"watermark": False, "progress_bar": False}   # campaign clips never carry these
        d["logo"]["enabled"], d["cta"]["enabled"] = False, False
    elif answers["logo_id"] is not None:
        d["logo"]["enabled"], d["logo"]["asset_id"] = True, answers["logo_id"]
    else:
        d["logo"]["enabled"], d["logo"]["asset_id"] = False, None
    placement.check_design(db, d, folder)
    try:
        blueprints.resolve("generated", designs.layers_from(d), PREVIEW_TEXT_OK, overrides=designs.overrides_from(d))
    except blueprints.OriginalityError as e:
        raise designs.DesignError(f"a design must add more than a crop and captions: {e}") from None
    except blueprints.BlueprintError as e:
        raise designs.DesignError(str(e)) from None
    d["made_by"] = "ai"
    return designs.normalize(d)


def picture_problems(path: pathlib.Path, design: dict, times=(1.0, 3.0, 5.0, 7.0)) -> list[str]:
    """Look at the preview itself: a caption must not sit over a face. Checked on the frames at a few moments of the clip."""
    import cv2

    from ..formats import designs as design_rules

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        return []
    front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    centre = design_rules.POSITIONS[design["captions"]["position"]]
    top, bottom = (centre - placement.CAPTION_HALF_HEIGHT) / 2, (centre + placement.CAPTION_HALF_HEIGHT) / 2
    hits, bright = 0, []
    try:
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(cv2.resize(frame, (540, 960)), cv2.COLOR_BGR2GRAY)
            bright.append(float(gray[int(top):int(bottom)].mean()))
            for x, y, w, h in front.detectMultiScale(gray, 1.1, 5, minSize=(50, 50)):
                if y < bottom and y + h > top:
                    hits += 1
                    break
    finally:
        cap.release()
    problems = []
    if hits >= 2:
        problems.append("a caption would sit over the speaker's face: move the captions, or pick a layout that keeps the face clear")
    if bright and design["captions"]["style"] in DARK_FOOTAGE_ONLY and sum(bright) / len(bright) > BRIGHT_BEHIND:
        problems.append(f"the {design['captions']['style']} captions would be hard to read over this bright part of the picture; "
                        "pick a style with an outline")
    return problems


def preview_video(db, kind: str, videos: list[dict], webcam=None) -> tuple[dict, str]:
    """The downloaded video to preview on: for a stream, one with a webcam in it; otherwise the first one. Returns (video, note)."""
    if kind != "stream":
        return videos[0], ""
    webcam = webcam or suggest_webcam
    for video in videos[:4]:
        if webcam(preview_source(video["id"])) is not None:
            return video, ""
    return videos[0], "No downloaded stream video has a webcam in it, so the stream previews use \"" + videos[0]["title"] + "\"."


def preview_source(video_id: str):
    from core.paths import cached_source

    from . import DOWNLOADS

    return cached_source(DOWNLOADS, video_id) or DOWNLOADS / f"{video_id}.mp4"


def suggest_webcam(source):
    from . import webcam

    return webcam.suggest(source)


def run(db, claude_client, answers: dict, profile, out_dir, *, ask=None) -> dict:
    """The three options: [{name, summary, design, preview}], plus any that were dropped and why."""
    answers = check_answers(answers)
    videos = preview.sample_videos(db)
    if not videos:
        raise GenerateError("Download a video first: a preview needs one of your downloaded videos to be made from.")
    video, note = preview_video(db, answers["kind"], videos)
    if ask is None:
        from .. import claude

        def ask(facts):
            return claude.generate_formats(claude_client, facts)
    reply, cost = ask(brief(answers, db))
    if cost > COST_LIMIT_USD:
        raise GenerateError(f"That request cost more than the {COST_LIMIT_USD * 100:.0f} cent limit, so nothing was kept. Try again.")
    raw = [o for o in (reply.get("options") or []) if isinstance(o, dict)][:OPTIONS]
    options, dropped, seen = [], [], set()
    for item in raw:
        name = str(item.get("name") or "").strip()[:40] or "Untitled format"
        try:
            design = checked(item.get("design") or {}, answers, db)
            key = json.dumps(design, sort_keys=True)
            if key in seen:
                raise designs.DesignError("it is the same as another option")
            seen.add(key)
            picture = preview.make_preview(db, profile, design, video["id"], out_dir)
            problems = picture_problems(picture, design)
            if problems:
                raise designs.DesignError(problems[0])
        except Exception as e:   # one bad option is left out with its reason; the others still come back
            dropped.append(f"{name}: {e}")
            continue
        options.append({"name": name, "summary": str(item.get("summary") or "").strip()[:160], "design": design,
                        "preview": f"/formats/preview/{picture.name}"})
    if not options:
        raise GenerateError("None of the options passed the checks. " + (dropped[0] if dropped else "Try again."))
    return {"options": options, "dropped": dropped, "cost": round(cost, 4), "note": note}


def record_feedback(db, answers: dict, design: dict, verdict: str) -> None:
    """Remember a look the owner liked ("more") or did not ("less"), so the next generate leans the right way."""
    from .. import store

    if verdict not in ("more", "less"):
        raise GenerateError("The feedback must be More like this or Not this.")
    whole = designs.normalize(design)   # stored whole, so the next request can read every part of it
    with db.conn() as c:
        c.execute("INSERT INTO format_feedback (kind, feel, verdict, design_json, created_at) VALUES (?,?,?,?,?)",
                  (answers["kind"], answers["feel"], verdict, json.dumps(whole, sort_keys=True), store.iso()))


def save(db, answers: dict, name: str, design: dict, categories: list[str]) -> dict:
    """Save one chosen option as a template, after checking it again."""
    from . import templates

    answers = check_answers(answers)
    final = checked(design, answers, db)
    template = templates.save_design(db, name=(name or "").strip()[:40] or "AI format", design=final)
    for category in categories:
        templates.assign(db, category.strip().lower(), template["id"])
    return template
