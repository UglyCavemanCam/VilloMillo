"""The Assets tab: pictures you upload once and the formats use. Today that is your logo.

Files are kept under data/villomillo/assets/ named by their hash, so the same picture is stored once. They are only ever
read back as pictures; nothing in them is run. Assets are shared by every template, like the templates themselves.
"""

from __future__ import annotations

import hashlib
import io
import pathlib

from PIL import Image, UnidentifiedImageError

from .. import store
from ..config import ROOT
from . import designs

FOLDER = ROOT / "data" / "villomillo" / "assets"
MAX_BYTES = 2 * 1024 * 1024
MAX_SIDE = 4000
TYPES = {"PNG": "png", "WEBP": "webp", "GIF": "gif"}
MEDIA = {"png": "image/png", "webp": "image/webp", "gif": "image/gif"}


class AssetError(ValueError):
    """The message says what to change, in plain words."""


def save_logo(db, name: str, data: bytes, folder: pathlib.Path | None = None) -> dict:
    if not data:
        raise AssetError("That file is empty.")
    if len(data) > MAX_BYTES:
        raise AssetError("A logo must be 2 MB or smaller.")
    try:
        fmt = Image.open(io.BytesIO(data)).format
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise AssetError("That file is not a picture the app can read. Use a PNG, WebP or GIF.") from None
    if fmt not in TYPES:
        raise AssetError("A logo must be a PNG, WebP or GIF.")
    if max(img.size) > MAX_SIDE:
        raise AssetError(f"A logo can be at most {MAX_SIDE} pixels on its longest side.")
    ext, sha = TYPES[fmt], hashlib.sha256(data).hexdigest()
    folder = folder or FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / f"{sha}.{ext}").exists():
        (folder / f"{sha}.{ext}").write_bytes(data)
    label = pathlib.Path(name or "logo").name[:80] or "logo"
    with db.conn() as c:
        cur = c.execute("INSERT INTO assets (kind, name, sha, ext, created_at) VALUES ('logo', ?, ?, ?, ?)",
                        (label, sha, ext, store.iso()))
        row = c.execute("SELECT * FROM assets WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def logos(db) -> list[dict]:
    with db.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM assets WHERE kind='logo' ORDER BY id")]


def get(db, asset_id: int | None) -> dict | None:
    if asset_id is None:
        return None
    with db.conn() as c:
        row = c.execute("SELECT * FROM assets WHERE id=? AND kind='logo'", (asset_id,)).fetchone()
    return dict(row) if row else None


def file_path(asset: dict, folder: pathlib.Path | None = None) -> pathlib.Path:
    return (folder or FOLDER) / f"{asset['sha']}.{asset['ext']}"


def delete(db, asset_id: int) -> None:
    """A logo a template still uses cannot be deleted: change that template first, so no clip is left without its logo."""
    from . import templates

    for t in templates.list_templates(db):
        if (t["settings"].get("logo") or {}).get("asset_id") == asset_id:
            raise AssetError(f"The template {t['name']!r} uses this logo. Change that template first.")
    with db.conn() as c:
        c.execute("DELETE FROM assets WHERE id=? AND kind='logo'", (asset_id,))


def for_design(db, design: dict, folder: pathlib.Path | None = None) -> tuple[set[str], pathlib.Path | None]:
    """What a design brings beyond the clip's own words: the call to action, and the logo's file. A missing logo is a
    plain-words error, so the clip is flagged for review rather than made without a logo it was meant to have."""
    available = {"cta"} if design["cta"]["enabled"] else set()
    if not design["logo"]["enabled"]:
        return available, None
    asset = get(db, design["logo"]["asset_id"])
    path = file_path(asset, folder) if asset else None
    if path is None or not path.exists():
        raise designs.DesignError("The logo is missing: upload it again in Assets, or turn the logo off.")
    return available | {"logo"}, path
