"""Templates and categories: saving and checking a template, assigning it to a category, deleting it for good, and the rule
that a clip with no format can never be approved, scheduled or posted."""

from __future__ import annotations

import pytest

from villomillo import clipper, config, store
from villomillo.db import Database
from villomillo.formats import templates
from villomillo import posting

LAYERS = {"framing": "face_fill", "text": ["captions", "takeaway"], "brand": ["progress_bar_bottom"]}


def test_a_template_is_saved_and_read_back(tmp_path):
    db = Database(tmp_path / "t.db")
    t = templates.save(db, name="Podcast punchy", layers=LAYERS, settings={"effects": {"intensity": "subtle"}})
    assert t["name"] == "Podcast punchy" and t["layers"] == LAYERS and t["settings"]["effects"]["intensity"] == "subtle"
    assert templates.get(db, t["id"])["id"] == t["id"]
    assert [x["id"] for x in templates.list_templates(db)] == [t["id"]]


def test_saving_again_changes_the_same_template(tmp_path):
    db = Database(tmp_path / "t.db")
    first = templates.save(db, name="Talk", layers=LAYERS)
    again = templates.save(db, name="Renamed", layers=LAYERS, template_id=first["id"])
    assert again["id"] == first["id"] and again["name"] == "Renamed" and len(templates.list_templates(db)) == 1


@pytest.mark.parametrize(
    ("name", "layers", "message"),
    [
        ("", LAYERS, "give the template a name"),
        ("x" * 61, LAYERS, "longer than 60"),
        ("Bad", {"framing": "nope"}, "no block called nope"),
        ("Bad", {"framing": "takeaway"}, "not framing"),
    ],
)
def test_a_template_that_cannot_work_is_refused(tmp_path, name, layers, message):
    db = Database(tmp_path / "t.db")
    with pytest.raises(templates.TemplateError, match=message):
        templates.save(db, name=name, layers=layers)
    assert templates.list_templates(db) == []


def test_a_category_uses_one_template_and_the_match_ignores_case(tmp_path):
    db = Database(tmp_path / "t.db")
    t = templates.save(db, name="Talk", layers=LAYERS)
    templates.assign(db, "Podcasts", t["id"])
    assert templates.for_category(db, "podcasts")["id"] == t["id"]
    assert templates.for_category(db, "Talks") is None
    assert templates.assignments(db) == {"podcasts": t["id"]}


def test_a_category_can_be_given_no_format_again(tmp_path):
    db = Database(tmp_path / "t.db")
    t = templates.save(db, name="Talk", layers=LAYERS)
    templates.assign(db, "Podcasts", t["id"])
    templates.assign(db, "Podcasts", None)
    assert templates.for_category(db, "Podcasts") is None


def test_assigning_a_missing_template_or_no_category_is_refused(tmp_path):
    db = Database(tmp_path / "t.db")
    with pytest.raises(templates.TemplateError, match="no such template"):
        templates.assign(db, "Podcasts", "missing")
    with pytest.raises(templates.TemplateError, match="pick a category"):
        templates.assign(db, "  ", None)


def test_deleting_a_template_is_permanent_and_says_which_categories_lost_it(tmp_path):
    db = Database(tmp_path / "t.db")
    t = templates.save(db, name="Talk", layers=LAYERS)
    templates.assign(db, "Podcasts", t["id"])
    templates.assign(db, "Talks", t["id"])
    assert sorted(templates.delete(db, t["id"])) == ["podcasts", "talks"]
    assert templates.get(db, t["id"]) is None and templates.for_category(db, "Podcasts") is None


def test_the_clips_table_has_the_format_columns_on_a_new_database(tmp_path):
    db = Database(tmp_path / "t.db")
    with db.conn() as c:
        columns = {r[1] for r in c.execute("PRAGMA table_info(clips_ready)")}
    assert {"template_id", "format_applied"} <= columns


def insert_clip(db: Database, *, state: str, format_applied: int) -> int:
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, validation_json, grade_json, "
                  "state, reason, created_at, category, format_applied) VALUES ('solved', 7, 'abcdefghijk', 0, 30, '{}', '{}', "
                  "?, 'waiting', ?, 'Podcasts', ?)", (state, store.iso(), format_applied))
        return c.execute("SELECT id FROM clips_ready").fetchone()["id"]


def test_a_clip_with_no_format_cannot_be_approved(tmp_path):
    db = Database(tmp_path / "t.db")
    clip_id = insert_clip(db, state="review", format_applied=0)
    with pytest.raises(ValueError, match="no format yet"):
        clipper.decide(db, "solved", clip_id, "approve")
    clipper.decide(db, "solved", clip_id, "reject")  # rejecting is still allowed


def test_a_clip_with_no_format_cannot_be_planned_for_posting(tmp_path):
    db = Database(tmp_path / "t.db")
    clip_id = insert_clip(db, state="approved", format_applied=0)
    channel = config.Profile(id="solved", display_name="Podcasts", niche={"definition": "x"}, category="Podcasts",
                             posting={"platforms": {"youtube_shorts": "manual"}})
    with pytest.raises(ValueError, match="no format yet"):
        posting.plan(db, channel, clip_id)
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
