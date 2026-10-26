"""Caption styles: twelve looks for the words as they are said, written as subtitle events (ASS) that ffmpeg burns in.

Each style is a row of data (font, size, how many words at a time, colours, and a kind that says how the spoken word is
shown). `build_ass` turns a clip's words into the subtitle file for one style. Everything is anchored to the clip's own
clock, so the same file works for a preview and for the finished clip.

Some looks are close cousins of the drawn previews on the plan page rather than exact copies, because subtitles cannot
draw everything a picture can: the boxed word (Block) is a thick coloured outline, the marker swipe (Marker) is an
underline, and Plate's corners are square.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import fonts

W = 1080
MAX_TEXT_W = 960      # a caption line never grows wider than this, whatever the words
INK = "#0E1217"


@dataclass(frozen=True)
class Style:
    id: str
    label: str
    blurb: str
    font: str           # the default font (see fonts.py)
    size: int
    group: int          # words on screen at a time
    upper: bool
    kind: str
    base: str           # the colour of the words
    accent: str         # the colour of the spoken word, or of the glow or box
    bord: int = 8


STYLES: dict[str, Style] = {s.id: s for s in (
    Style("spotlight", "Spotlight", "Three words at a time. The spoken word turns amber.", "montserrat-extrabold", 78, 3, True, "highlight", "#F4EBDD", "#FFB000", 9),
    Style("pop", "Pop", "Two words. The spoken word swells; key words stay amber.", "montserrat-black", 88, 2, True, "pop", "#F4EBDD", "#FFB000", 10),
    Style("block", "Block", "The spoken word sits in a yellow box.", "montserrat-extrabold", 76, 3, True, "block", "#FFFFFF", "#FFD400", 6),
    Style("dim", "Dim Light", "The whole line shows; words light up as they are said.", "poppins-semibold", 70, 4, False, "dim", "#FFFFFF", "#FFFFFF", 5),
    Style("slam", "Slam", "One word at a time, slamming in, alternating white and amber.", "anton", 150, 1, True, "slam", "#FFFFFF", "#FFB000", 10),
    Style("plate", "Plate", "Sentence case on a dark plate, fading in.", "inter-semibold", 62, 5, False, "plate", "#FFFFFF", "#FFFFFF", 18),
    Style("neon", "Neon Stream", "Cyan letters with a magenta glow.", "barlow-condensed", 100, 3, True, "neon", "#5CF0FF", "#FF2BD6", 4),
    Style("type", "Typewriter", "Letters appear one at a time.", "special-elite", 66, 4, False, "type", "#FFFFFF", "#FFFFFF", 5),
    Style("marker", "Marker", "Slightly tilted, the spoken word underlined in yellow.", "permanent-marker", 78, 3, False, "marker", "#FFFFFF", "#FFE135", 7),
    Style("build", "Build-up", "Words rise in one by one, then the line clears.", "poppins-bold", 76, 4, False, "build", "#FFFFFF", "#FFFFFF", 6),
    Style("underline", "Underline", "Sentence case, the spoken word underlined in amber.", "montserrat-bold", 76, 3, False, "underline", "#FFFFFF", "#FFB000", 6),
    Style("burst", "Burst", "Tilted, thick outline, the spoken word pulses.", "bangers", 110, 2, True, "burst", "#FFE135", "#FFFFFF", 10),
)}


def _bgr(hex_rgb: str) -> str:
    h = hex_rgb.lstrip("#")
    return h[4:6] + h[2:4] + h[0:2]


def _tag(hex_rgb: str) -> str:
    return f"&H{_bgr(hex_rgb)}&"


def _style_colour(hex_rgb: str, alpha: int = 0) -> str:
    return f"&H{alpha:02X}{_bgr(hex_rgb)}&"


def _stamp(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _clean(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


def groups_of(words: list[dict], size: int) -> list[dict]:
    """Caption groups of `size` words, never across a sentence end or a long gap."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) == size or w["o0"] - cur[-1]["o1"] > 0.5):
            groups.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith((".", "?", "!")):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    out = [{"words": g, "o0": g[0]["o0"], "o1": g[-1]["o1"] + 0.12} for g in groups]
    for this, following in zip(out, out[1:]):   # a line goes when the next one starts: two lines never share the screen
        this["o1"] = max(min(this["o1"], following["o0"]), this["words"][-1]["o1"])
    return out


def word_gap(st: Style) -> str:
    """What goes between two words: a space made wider by the outline or glow, which would otherwise make the words touch."""
    extra = int(st.bord * 1.5) + (8 if st.kind == "neon" else 0)
    return f"{{\\fsp{extra}}} {{\\fsp0}}" if extra else " "


def _fit(texts: list[str], font_id: str, size: int, room: float = 1.0) -> int:
    """The size that keeps the whole line inside the frame (`room` leaves space for a word that swells)."""
    width = fonts.pil(font_id, size).getlength(" ".join(texts)) * room
    return size if width <= MAX_TEXT_W else max(36, int(size * MAX_TEXT_W / width))


def build_ass(words: list[dict], style_id: str, *, y: int, font: str | None = None, size: int | None = None,
              upper: bool | None = None, base: str | None = None, accent: str | None = None,
              keys: frozenset[str] = frozenset()) -> str:
    """The subtitle file for a clip. `words` carry o0/o1 (seconds into the clip) and text; `y` is the caption's centre."""
    st = STYLES[style_id]
    font_id = font or st.font
    f = fonts.FONTS[font_id]
    size = size or st.size
    upper = st.upper if upper is None else upper
    base, accent = base or st.base, accent or st.accent
    base_t, accent_t, ink_t = _tag(base), _tag(accent), _tag(INK)

    outline_colour = {"neon": _style_colour(accent), "plate": _style_colour("#000000", 0x4D)}.get(st.kind, _style_colour(INK))
    secondary = _style_colour(base, 255 if st.kind == "type" else 0)  # typewriter letters stay invisible until their turn
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
        "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Cap,{f.ass_name},{size},{_style_colour(base)},{secondary},{outline_colour},{_style_colour('#000000', 128)},"
        f"{-1 if f.bold else 0},{-1 if f.italic else 0},0,0,100,100,0,0,{3 if st.kind == 'plate' else 1},{st.bord},0,5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    lines: list[str] = []
    slam_count = 0

    def event(start: float, end: float, text: str, fs: int, extra: str = "") -> None:
        end = max(end, start + 0.05)
        lines.append(f"Dialogue: 0,{_stamp(start)},{_stamp(end)},Cap,,0,0,0,,{{\\an5\\pos({W // 2},{y})\\fs{fs}{extra}}}{text}")

    groups = groups_of(words, st.group)
    for gi, g in enumerate(groups):
        gw = g["words"]
        texts = [_clean(w["text"].upper() if upper else w["text"]) for w in gw]
        room = 1.18 if st.kind in ("pop", "burst", "block") else 1.0
        fs = _fit(texts, font_id, size, room)
        starts = [w["o0"] for w in gw]
        ends = [starts[k + 1] if k + 1 < len(gw) else g["o1"] for k in range(len(gw))]

        if st.kind == "slam":
            for k, t in enumerate(texts):
                colour = accent_t if slam_count % 2 else base_t
                slam_count += 1
                last = gi + 1 == len(groups) and k + 1 == len(gw)
                end = min(max(gw[k]["o1"] + 0.12, starts[k] + 0.25), ends[k] + (0.3 if last else 0.0))   # never into the next word
                event(starts[k], end, f"{{\\1c{colour}\\fscx170\\fscy170\\t(0,90,\\fscx100\\fscy100)}}{t}", fs)
        elif st.kind == "plate":
            event(g["o0"], g["o1"], f"{{\\fad(120,0)}}{' '.join(texts)}", fs)
        elif st.kind == "type":
            chars = " ".join(texts)
            per = max(1, int((g["o1"] - g["o0"]) * 100 * 0.9 / max(len(chars), 1)))
            until = min(g["o1"] + 0.4, groups[gi + 1]["o0"]) if gi + 1 < len(groups) else g["o1"] + 0.4  # never over the next line
            event(g["o0"], until, "".join(f"{{\\k{per}}}{c}" for c in chars), fs)
        elif st.kind == "build":
            for k in range(len(texts)):
                before = word_gap(st).join(texts[:k])
                newest = f"{{\\alpha&HFF&\\t(0,140,\\alpha&H00&)}}{texts[k]}"
                event(starts[k], ends[k], (before + word_gap(st) if before else "") + newest, fs)
        else:  # the spoken word is marked inside a line that stays on screen
            tilt = {"marker": "\\frz-2.5", "burst": "\\frz-3"}.get(st.kind, "")
            apart = word_gap(st)
            for k in range(len(texts)):
                runs = []
                for j, t in enumerate(texts):
                    active = j == k
                    if st.kind == "highlight":
                        run = f"{{\\1c{accent_t if active else base_t}}}"
                    elif st.kind == "pop":
                        key = "".join(c for c in gw[j]["text"].lower() if c.isalnum()) in keys
                        run = (f"{{\\1c&HFFFFFF&\\fscx125\\fscy125\\t(0,120,\\fscx108\\fscy108)}}" if active
                               else f"{{\\1c{accent_t if key else base_t}\\fscx100\\fscy100}}")
                    elif st.kind == "block":
                        run = (f"{{\\1c&H111111&\\3c{accent_t}\\bord16}}" if active else f"{{\\1c{base_t}\\3c{ink_t}\\bord6}}")
                    elif st.kind == "dim":
                        run = "{\\1a&H00&\\3a&H00&}" if j <= k else "{\\1a&H99&\\3a&H99&}"
                    elif st.kind == "neon":
                        run = (f"{{\\1c&HFFFFFF&\\3c{accent_t}\\bord6\\blur8}}" if active else f"{{\\1c{base_t}\\3c{accent_t}\\bord4\\blur5}}")
                    elif st.kind in ("underline", "marker"):
                        run = f"{{\\1c{accent_t}\\u1}}" if active else f"{{\\1c{base_t}\\u0}}"
                    else:  # burst
                        run = (f"{{\\1c{accent_t}\\fscx118\\fscy118}}" if active else f"{{\\1c{base_t}\\fscx100\\fscy100}}")
                    runs.append(run + t)
                event(starts[k], ends[k], apart.join(runs), fs, tilt)
    return header + "\n".join(lines) + ("\n" if lines else "")
