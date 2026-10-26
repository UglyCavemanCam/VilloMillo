"""Effects: small picture moves on the words that earn them.

Claude proposes them in the same request that writes the title (claude.py), each tied to words the speaker said. This file
is the code that keeps the last word: it caps how many a template may use, drops any that do not fit the layout, the
template's allowed kinds, a serious subject, or a spot under the headline or the last second, and spaces the rest. The
renderer (render.py) then applies what is left as crop changes and timed filters. Nothing here touches pixels.
"""

from __future__ import annotations

KINDS = {
    "punch_in": {"label": "Punch-in", "blurb": "A quick zoom toward the speaker on a strong statement.", "dur": 1.2,
                 "layouts": ("face_fill",), "serious_ok": True},
    "zoom_out": {"label": "Zoom-out reveal", "blurb": "Starts tight and pulls back, for a reveal or a hook.", "dur": 1.6,
                 "layouts": ("face_fill",), "serious_ok": True},
    "shake": {"label": "Shake", "blurb": "A short camera jolt on an impact word. Never on a serious subject.", "dur": 0.6,
              "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": False},
    "flash": {"label": "Flash", "blurb": "A brief bright pop on a sudden turn. Never on a serious subject.", "dur": 0.15,
              "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": False},
    "pop": {"label": "Colour pop", "blurb": "Richer colour on an emotional peak.", "dur": 1.5,
            "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": True},
    "glitch": {"label": "Glitch", "blurb": "A brief colour split, for gaming or comedy moments. Never on a serious subject.",
               "dur": 0.25, "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": False},
    "blur": {"label": "Soft focus", "blurb": "The picture softens for a moment, to hold on a key line.", "dur": 0.8,
             "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": True},
    "freeze": {"label": "Freeze frame", "blurb": "The picture holds for half a second on a punchline. The audio keeps going.",
               "dur": 0.5, "layouts": ("face_fill", "fit_blur", "stacked_webcam"), "serious_ok": True},
}
INTENSITIES = ("off", "subtle", "balanced", "punchy")
GAP = {"subtle": 8.0, "balanced": 5.0, "punchy": 3.0}   # seconds of clip per effect allowed
SPACING = 2.5          # the least time between two effects
ZOOM = 0.12            # a punch-in or zoom-out changes the crop by this much at its tightest
SHAKE_HZ = 13.0
REASON_MAX = 90
MAX_PROPOSED = 3


def settings_default() -> dict:
    return {"intensity": "subtle", "kinds": list(KINDS)}


def plan(proposed: list[tuple[str, float, str]], serious: bool, settings: dict, framing: str, total: float,
         headline_end: float) -> list[dict]:
    """The effects a clip gets: [{kind, start, end, reason}], seconds into the clip. Off means none."""
    intensity = settings["intensity"]
    if intensity == "off":
        return []
    allowed = set(settings["kinds"])
    cap = max(1, int(total // GAP[intensity]))
    kept: list[dict] = []
    for kind, when, reason in sorted(proposed, key=lambda p: p[1]):
        if len(kept) >= cap:
            break
        if kind not in KINDS or kind not in allowed or framing not in KINDS[kind]["layouts"]:
            continue
        if serious and not KINDS[kind]["serious_ok"]:
            continue
        end = when + KINDS[kind]["dur"]
        if when < headline_end or end > total - 1.0:   # not under the headline, and not in the last second
            continue
        if kept and when - kept[-1]["start"] < SPACING:
            continue
        kept.append({"kind": kind, "label": KINDS[kind]["label"], "start": round(when, 2), "end": round(end, 2), "reason": reason})
    return kept


def _window(e: dict) -> str:
    return f"between(t,{e['start']:.2f},{e['end']:.2f})"


def zoom_factor(effects: list[dict]) -> str:
    """How big the face crop is at each moment, as a multiple of its normal size: 1 means normal, 0.88 is a tight punch."""
    terms = []
    for e in effects:
        a, d = e["start"], e["end"] - e["start"]
        if e["kind"] == "punch_in":
            term = f"-{ZOOM}*sin(PI*(t-{a:.2f})/{d:.2f})"
        elif e["kind"] == "zoom_out":
            term = f"-{ZOOM}*(1-(t-{a:.2f})/{d:.2f})"
        else:
            continue
        terms.append(f"if({_window(e)},{term},0)")
    return f"(1+{'+'.join(terms)})" if terms else "1"


def shake_offset(effects: list[dict], amount: float) -> str:
    """A sideways or upward jitter, in pixels, that dies away over each shake."""
    terms = [f"if({_window(e)},{amount}*sin(2*PI*{SHAKE_HZ}*(t-{e['start']:.2f}))*(1-(t-{e['start']:.2f})/{e['end'] - e['start']:.2f}),0)"
             for e in effects if e["kind"] == "shake"]
    return f"({'+'.join(terms)})" if terms else "0"


def colour_filters(effects: list[dict]) -> list[str]:
    """Timed filters for flashes, colour pops, glitches and soft focus, applied to the picture under the words."""
    out = []
    for e in effects:
        if e["kind"] == "flash":
            out.append(f"eq=brightness=0.25:enable='{_window(e)}'")
        elif e["kind"] == "pop":
            out.append(f"eq=saturation=1.35:enable='{_window(e)}'")
        elif e["kind"] == "glitch":
            out.append(f"rgbashift=rh=8:bh=-8:enable='{_window(e)}'")
        elif e["kind"] == "blur":
            out.append(f"gblur=sigma=5:enable='{_window(e)}'")
    return out


def freeze_frames(effects: list[dict], fps: float) -> list[tuple[int, int]]:
    """The (first, last) frame numbers each freeze holds, at the clip's frame rate."""
    return [(round(e["start"] * fps), round(e["end"] * fps)) for e in effects if e["kind"] == "freeze"]
