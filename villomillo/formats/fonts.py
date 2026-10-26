"""The fonts a format can use. All are open-licence fonts kept in fonts/ (their licences are in fonts/licences/):
SIL Open Font License for Montserrat, Anton, Poppins, Inter, Barlow Condensed and Bangers; Apache 2.0 for Special Elite
and Permanent Marker. The same files draw the headline images and are handed to the caption engine."""

from __future__ import annotations

import functools
import pathlib
import shutil
from dataclasses import dataclass

from PIL import ImageFont

FONTS_DIR = pathlib.Path(__file__).parent / "fonts"


USER_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "villomillo" / "fonts"          # uploaded fonts
MERGED_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data" / "villomillo" / "fonts-all"      # bundled + uploaded, for captions


@dataclass(frozen=True)
class Font:
    id: str
    label: str
    file: str
    ass_name: str          # the family name the caption engine finds the file by
    bold: bool = False
    italic: bool = False
    folder: str | None = None   # None: bundled with the app; otherwise the folder an uploaded font lives in


FONTS: dict[str, Font] = {f.id: f for f in (
    Font("montserrat-bold", "Montserrat Bold", "Montserrat-700.ttf", "Montserrat", bold=True),
    Font("montserrat-extrabold", "Montserrat ExtraBold", "Montserrat-800.ttf", "Montserrat ExtraBold"),
    Font("montserrat-black", "Montserrat Black", "Montserrat-900.ttf", "Montserrat Black"),
    Font("anton", "Anton", "Anton-400.ttf", "Anton"),
    Font("poppins-semibold", "Poppins SemiBold", "Poppins-600.ttf", "Poppins SemiBold"),
    Font("poppins-bold", "Poppins Bold", "Poppins-700.ttf", "Poppins", bold=True),
    Font("inter-semibold", "Inter SemiBold", "Inter-600.ttf", "Inter SemiBold"),
    Font("barlow-condensed", "Barlow Condensed ExtraBold Italic", "BarlowCondensed-800-italic.ttf", "Barlow Condensed ExtraBold", italic=True),
    Font("special-elite", "Special Elite", "SpecialElite-400.ttf", "Special Elite"),
    Font("permanent-marker", "Permanent Marker", "PermanentMarker-400.ttf", "Permanent Marker"),
    Font("bangers", "Bangers", "Bangers-400.ttf", "Bangers"),
)}


def sync_merged() -> pathlib.Path:
    """One folder with every font (bundled and uploaded), which the caption engine is pointed at."""
    MERGED_DIR.mkdir(parents=True, exist_ok=True)
    for source in [*FONTS_DIR.glob("*.ttf"), *FONTS_DIR.glob("*.otf"), *USER_DIR.glob("*.ttf"), *USER_DIR.glob("*.otf")]:
        target = MERGED_DIR / source.name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copyfile(source, target)
    return MERGED_DIR


def register_uploads(rows: list[dict]) -> None:
    """Make uploaded fonts available to the picker and the renderer. Called whenever the list may have changed."""
    for key in [k for k, f in FONTS.items() if f.folder]:
        del FONTS[key]
    for row in rows:
        fid = f"upload-{row['sha'][:10]}"
        FONTS[fid] = Font(fid, row["label"], f"{row['sha']}.{row['ext']}", row["family"], folder=str(USER_DIR))
    pil.cache_clear()


def fonts_dir_option() -> str:
    """The folder as the caption filter wants it written (forward slashes, the drive colon escaped, in quotes)."""
    folder = sync_merged()
    return "'" + str(folder).replace("\\", "/").replace(":", "\\:") + "'"


@functools.lru_cache(maxsize=64)
def pil(font_id: str, size: int) -> ImageFont.FreeTypeFont:
    font = FONTS[font_id]
    return ImageFont.truetype(str(pathlib.Path(font.folder or FONTS_DIR) / font.file), size)
