"""After a video is re-rendered, the engine keeps its clip ids; refresh_files copies the new files over."""

from __future__ import annotations

import json

import pytest
import solved_profile

from villomillo import clipper, store, validate
from villomillo.db import Database


class FakeEngine:
    def __init__(self, files):
        self.files = files

    def media(self, clip_id):
        return self.files[clip_id]


@pytest.fixture
def held(tmp_path, monkeypatch):
    db = Database(tmp_path / "refresh.db")
    paths = {}
    for clip_id in (1, 2):
        path = tmp_path / f"{clip_id}.mp4"
        path.write_bytes(b"old-%d" % clip_id)
        paths[clip_id] = path
        with db.conn() as c:
            c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, "
                      "hook, file_path, validation_json, grade_json, state, reason, created_at) "
                      "VALUES ('solved', ?, 'vid1', 0, 30, 80, 'h', ?, '{}', '{}', 'approved', 'r', ?)",
                      (clip_id, str(path), store.iso()))
    return db, paths, solved_profile.solved()


def test_a_changed_file_that_passes_its_checks_replaces_ours(held, monkeypatch):
    db, paths, profile = held
    monkeypatch.setattr(validate, "check", lambda p: {"ok": True, "problems": [], "facts": {"duration_s": 30}})
    lines = clipper.refresh_files(db, FakeEngine({1: b"new-1", 2: b"old-2"}), profile, "vid1")
    assert lines[0].startswith("clip 1: replaced") and lines[1] == "clip 2: unchanged"
    assert paths[1].read_bytes() == b"new-1" and paths[2].read_bytes() == b"old-2"
    assert not list(paths[1].parent.glob("*.new.mp4"))
    with db.conn() as c:
        assert json.loads(c.execute("SELECT validation_json FROM clips_ready WHERE engine_clip_id=1").fetchone()[0])["ok"]


def test_a_new_file_that_fails_its_checks_is_discarded_and_the_old_one_kept(held, monkeypatch):
    db, paths, profile = held
    monkeypatch.setattr(validate, "check", lambda p: {"ok": False, "problems": ["no audio stream"], "facts": {}})
    lines = clipper.refresh_files(db, FakeEngine({1: b"broken", 2: b"old-2"}), profile, "vid1")
    assert "kept the old one: no audio stream" in lines[0]
    assert paths[1].read_bytes() == b"old-1" and not list(paths[1].parent.glob("*.new.mp4"))
