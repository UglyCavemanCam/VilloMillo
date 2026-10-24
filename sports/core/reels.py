"""Story reels from a match's clips, as Spiideo makes them: the match recap,
and a reel per team and per player, in match order.

Built from the clips already rendered (9:16, or 16:9 with Longform), so a
reel costs a join, not a render: the clips are joined with the concat
demuxer and no re-encode (they come out of the same renderer in the same
format), each one's end card trimmed off and one added at the end of the
reel. What each reel holds:
- the recap: every named moment, the goals and cards and saves, in order;
- a team's reel: the moments the scoreboard or the match events give to it;
- a player's reel: the moments the match events name them in, and for a
  name typed in Teams or players, the moments whose commentary says it.
A reel needs two moments or more: one is the clip itself.
"""

import re
import subprocess
import unicodedata
import zlib
from pathlib import Path
from typing import NamedTuple

KINDS = ("recap", "teams", "players")
MIN_PARTS = 2


class Reel(NamedTuple):
    kind: str       # recap, team or player
    subject: str    # the team or the player; "" for the recap
    title: str
    parts: list     # the clips, in match order


def _scores(clip) -> dict:
    return getattr(clip.candidate, "subscores", None) or {}


def _named(clip) -> bool:
    s = _scores(clip)
    return bool(s.get("sport_event")) and s.get("sport_event") != "big_moment" and not s.get("sport_replay")


def _at(clip) -> float:
    return _scores(clip).get("sport_t", clip.candidate.start)


def _fold(text: str) -> str:
    """Lower case with the accents off: "Joël" typed is "Joel" heard."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).casefold()


def names(typed: str) -> list[str]:
    """The names in Teams or players: split at commas, "and", "&", "/" or
    "vs" ("Player A, Player B & Player C")."""
    parts = re.split(r"[,;/&]|\s(?:and|vs?\.?)\s", typed or "", flags=re.IGNORECASE)
    out: list[str] = []
    for part in parts:
        name = " ".join(part.split())
        if len(name) >= 2 and _fold(name) not in {_fold(n) for n in out}:
            out.append(name)
    return out


def says(text: str, name: str) -> bool:
    """Whether the commentary says the name: all of it, or its last word
    when that's long enough to be the surname commentators use. Whole words
    only, accents and case aside."""
    words = _fold(name).split()
    if not words:
        return False
    heard = _fold(text)
    options = {" ".join(words)} | ({words[-1]} if len(words) > 1 and len(words[-1]) >= 4 else set())
    return any(re.search(rf"(?<!\w){re.escape(o)}(?!\w)", heard) for o in options)


def _a_team(name: str, codes: set[str]) -> bool:
    """Whether a typed name is a team the score box read: its code ("HOM"),
    or a name starting with it ("Home United")."""
    letters = re.sub(r"[^a-z]", "", _fold(name))
    return _fold(name) in codes or (len(letters) >= 3 and letters[:3] in codes)


def plan(clips: list, asked, score: str = "", typed: str = "", said=None) -> list[Reel]:
    """Each reel asked for that has enough moments. `clips` are the run's
    rendered clips (RenderedClip); `typed` is Teams or players, and
    `said(clip)` the commentary in a clip's window, when there is a
    transcript."""
    asked = set(asked or [])
    moments = sorted((c for c in clips if _named(c)), key=_at)
    out: list[Reel] = []
    if "recap" in asked and len(moments) >= MIN_PARTS:
        out.append(Reel("recap", "", f"{score}: match recap" if score else "Match recap", moments))
    if "teams" in asked:
        teams: dict[str, list] = {}
        for c in moments:
            if _scores(c).get("sport_team"):
                teams.setdefault(_scores(c)["sport_team"], []).append(c)
        for team, parts in teams.items():
            if len(parts) >= MIN_PARTS:
                out.append(Reel("team", team, f"{team}: the match", parts))
    if "players" in asked:
        players: dict[str, list] = {}
        for c in moments:
            if _scores(c).get("sport_player"):
                players.setdefault(_scores(c)["sport_player"], []).append(c)
        # A name typed in Teams or players: the moments whose commentary says
        # it. A team the score box read is left to its team reel.
        codes = {_fold(_scores(c)["sport_team"]) for c in moments if _scores(c).get("sport_team")}
        for name in names(typed) if said is not None else []:
            if _a_team(name, codes):
                continue
            heard = [c for c in moments if says(said(c), name)]
            if heard:
                key = next((k for k in players if _fold(k) == _fold(name)), name)
                have = players.setdefault(key, [])
                have.extend(c for c in heard if c not in have)
                have.sort(key=_at)
        most = max((len(p) for p in players.values()), default=0)
        for player, parts in players.items():
            if len(parts) >= MIN_PARTS:
                # The most moments, and more than anyone else: the stand-in
                # for a player of the match, and said to be only that.
                top = len(parts) == most and most >= 3 and sum(len(p) == most for p in players.values()) == 1
                out.append(Reel("player", player, f"{player}: {'the most moments' if top else 'the match'}", parts))
    return out


def file_name(reel: Reel) -> str:
    """One file per reel, whatever its length, so a re-run writes the same
    one: reel_recap.mp4, reel_team_hom_1a2b.mp4. The hash keeps two names
    that read alike apart; the short name keeps the path short."""
    if not reel.subject:
        return f"reel_{reel.kind}.mp4"
    slug = re.sub(r"[^a-z0-9]+", "-", _fold(reel.subject)).strip("-")[:16].strip("-")
    tag = format(zlib.crc32(reel.subject.encode("utf-8")) & 0xFFFF, "04x")
    return f"reel_{reel.kind}_{slug}_{tag}.mp4" if slug else f"reel_{reel.kind}_{tag}.mp4"


def chapters(parts: list, durations: list[float]) -> str:
    """"0:00 Goal 18' HOM" per moment, for the reel's description."""
    lines, at = [], 0.0
    for clip, dur in zip(parts, durations):
        s = _scores(clip)
        when = f" {s['sport_minute']}'" if s.get("sport_minute") is not None else ""
        who = s.get("sport_team") or s.get("sport_player") or ""
        lines.append(f"{int(at) // 60}:{int(at) % 60:02d} {s.get('sport_label', 'Moment')}{when}{(' ' + who) if who else ''}")
        at += dur
    return "\n".join(lines)


def duration(path) -> float:
    from core.binaries import ffprobe

    out = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of",
                          "default=nw=1:nk=1", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def lengths(paths: list, trims: list[float] | None = None) -> list[float]:
    """Each clip's length in a reel: its own, less the `trims[i]` seconds
    (its end card, when it has one) left off."""
    kept = []
    for p, trim in zip(paths, trims or [0.0] * len(paths)):
        whole = duration(p)
        kept.append(max(0.5, whole - trim) if trim else whole)
    return kept


def join(paths: list, out: Path, trims: list[float] | None = None) -> list[float]:
    """The clips joined into `out` without re-encoding, each with its last
    `trims[i]` seconds left off. Returns each part's length as it went in."""
    from core.binaries import ffmpeg
    from core.paths import discard

    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".txt")
    trims = trims or [0.0] * len(paths)
    kept = lengths(paths, trims)
    rows = []
    for p, trim, keep in zip(paths, trims, kept):
        rows.append(f"file '{Path(p).resolve().as_posix()}'")
        if trim:
            rows.append(f"outpoint {keep:.3f}")
    listing.write_text("\n".join(rows) + "\n", encoding="utf-8")
    try:
        r = subprocess.run([ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                            "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(out)],
                           capture_output=True, text=True)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(r.stderr.strip()[-300:] or "the join failed")
    finally:
        discard(listing)
    return kept
