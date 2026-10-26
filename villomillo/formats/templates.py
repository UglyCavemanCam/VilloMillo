"""Templates: saved formats, and which clip category uses each one. Kept in the database, not in code.

A template is a set of building blocks (its layers, see blueprints.py) plus its settings. Each category uses at most one
template. A category with none has no format, so its clips wait for you (see clipper.collect).
"""

from __future__ import annotations

import json
import re
import secrets

from .. import store
from ..db import Database
from . import blueprints, designs

NAME_MAX = 60


class TemplateError(ValueError):
    """A template cannot be saved or assigned. The message says why, in plain words."""


def category_key(category: str | None) -> str:
    return (category or "").strip().lower()


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "layers": json.loads(r["layers_json"]),
            "settings": json.loads(r["settings_json"]), "updated_at": r["updated_at"]}


def list_templates(db: Database) -> list[dict]:
    with db.conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM templates ORDER BY name, id")]


def get(db: Database, template_id: str) -> dict | None:
    with db.conn() as c:
        r = c.execute("SELECT * FROM templates WHERE id=?", (template_id,)).fetchone()
    return _row(r) if r else None


def save(db: Database, *, name: str, layers: dict, settings: dict | None = None, template_id: str | None = None) -> dict:
    """Save a new template, or change an existing one. Its layers are checked against the block file first."""
    name = (name or "").strip()
    if not name:
        raise TemplateError("give the template a name")
    if len(name) > NAME_MAX:
        raise TemplateError(f"the name is longer than {NAME_MAX} characters")
    problem = blueprints.layers_problem(layers, blueprints.load())
    if problem:
        raise TemplateError(problem)
    template_id = template_id or f"{re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')[:30] or 'template'}-{secrets.token_hex(3)}"
    now = store.iso()
    with db.conn() as c:
        exists = c.execute("SELECT 1 FROM templates WHERE id=?", (template_id,)).fetchone()
        if exists:
            c.execute("UPDATE templates SET name=?, layers_json=?, settings_json=?, updated_at=? WHERE id=?",
                      (name, json.dumps(layers), json.dumps(settings or {}), now, template_id))
        else:
            c.execute("INSERT INTO templates (id, name, layers_json, settings_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      (template_id, name, json.dumps(layers), json.dumps(settings or {}), now, now))
    return get(db, template_id)


def save_design(db: Database, *, name: str, design: dict, template_id: str | None = None) -> dict:
    """Save a template from the builder's choices: they are checked (designs.DesignError says what to change), then stored."""
    try:
        design = designs.normalize(design)
    except designs.DesignError as e:
        raise TemplateError(str(e)) from None
    return save(db, name=name, layers=designs.layers_from(design), settings=design, template_id=template_id)


def delete(db: Database, template_id: str) -> list[str]:
    """Delete a template for good. Returns the categories that used it, which now have no format."""
    with db.conn() as c:
        cats = [r["category"] for r in c.execute("SELECT category FROM template_assignments WHERE template_id=?", (template_id,))]
        c.execute("DELETE FROM template_assignments WHERE template_id=?", (template_id,))
        c.execute("DELETE FROM templates WHERE id=?", (template_id,))
    return cats


def assign(db: Database, category: str, template_id: str | None) -> None:
    """Use this template for the category, or (with None) give the category no format."""
    key = category_key(category)
    if not key:
        raise TemplateError("pick a category first")
    with db.conn() as c:
        if template_id is None:
            c.execute("DELETE FROM template_assignments WHERE category=?", (key,))
            return
        if not c.execute("SELECT 1 FROM templates WHERE id=?", (template_id,)).fetchone():
            raise TemplateError("there is no such template")
        c.execute("INSERT INTO template_assignments (category, template_id, updated_at) VALUES (?,?,?) "
                  "ON CONFLICT(category) DO UPDATE SET template_id=excluded.template_id, updated_at=excluded.updated_at",
                  (key, template_id, store.iso()))


def assignments(db: Database) -> dict[str, str]:
    """Category -> template id, for every category that has a format."""
    with db.conn() as c:
        return {r["category"]: r["template_id"] for r in c.execute("SELECT category, template_id FROM template_assignments")}


def for_category(db: Database, category: str | None) -> dict | None:
    """The template a category uses, or None when it has none."""
    key = category_key(category)
    if not key:
        return None
    with db.conn() as c:
        r = c.execute("SELECT t.* FROM template_assignments a JOIN templates t ON t.id = a.template_id WHERE a.category=?",
                      (key,)).fetchone()
    return _row(r) if r else None
