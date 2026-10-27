"""Fonts you upload for captions and headlines. Each must be a real TTF or OTF file, with its source and licence recorded.

They are stored under data/villomillo/fonts/ by hash, copied into the merged folder the caption engine reads, and offered in the
font menus next to the bundled fonts.
"""

from __future__ import annotations

import hashlib
import pathlib
import tempfile

from PIL import ImageFont

from .. import store
from . import fonts

MAX_BYTES = 3 * 1024 * 1024
TYPES = (".ttf", ".otf")


class FontUploadError(ValueError):
    """The message says what to change, in plain words."""


def uploads(db) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM font_uploads ORDER BY id")]


def refresh(db) -> None:
    """Re-register the uploaded fonts so the pickers and the renderer know them."""
    fonts.register_uploads(uploads(db))


def save(db, name: str, data: bytes, *, ext: str, source: str, licence: str, folder: pathlib.Path | None = None) -> dict:
    ext = ext.lower()
    if ext not in TYPES:
        raise FontUploadError("A font must be a TTF or OTF file.")
    if not data:
        raise FontUploadError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise FontUploadError("A font must be 3 MB or smaller.")
    if not source.strip() or not licence.strip():
        raise FontUploadError("Say where the font came from and its licence: every font needs a licence record.")
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / f"font{ext}"
        path.write_bytes(data)
        try:
            family = ImageFont.truetype(str(path), 24).getname()[0]
        except OSError:
            raise FontUploadError("That file is not a font the app can read.") from None
    sha = hashlib.sha256(data).hexdigest()
    target = folder or fonts.USER_DIR
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{sha}{ext}").write_bytes(data)
    fonts.sync_merged()   # the caption engine reads one folder with every font in it
    label = (name or family).strip()[:60] or family
    with db.conn() as c:
        c.execute("INSERT OR IGNORE INTO font_uploads (label, family, sha, ext, source, licence, added_at) VALUES (?,?,?,?,?,?,?)",
                  (label, family, sha, ext.lstrip("."), source.strip(), licence.strip(), store.iso()))
        row = c.execute("SELECT * FROM font_uploads WHERE sha=?", (sha,)).fetchone()
    refresh(db)
    return dict(row)


def delete(db, font_id: int, folder: pathlib.Path | None = None) -> None:
    """A font a template still uses cannot be deleted: change that template first."""
    from . import templates

    with db.conn() as c:
        row = c.execute("SELECT * FROM font_uploads WHERE id=?", (font_id,)).fetchone()
    if row is None:
        return
    key = f"upload-{row['sha'][:10]}"
    for t in templates.list_templates(db):
        s = t["settings"]
        used = {s.get("captions", {}).get("font"), s.get("headline", {}).get("font")}   # a format saved before elements
        used |= {e.get("style", {}).get("font") for e in s.get("elements") or []}       # every kind of element can have its own font now
        if key in used:
            raise FontUploadError(f"The template {t['name']!r} uses this font. Change that template first.")
    with db.conn() as c:
        c.execute("DELETE FROM font_uploads WHERE id=?", (font_id,))
    (folder or fonts.USER_DIR).joinpath(f"{row['sha']}.{row['ext']}").unlink(missing_ok=True)
    (fonts.MERGED_DIR / f"{row['sha']}.{row['ext']}").unlink(missing_ok=True)
    refresh(db)
