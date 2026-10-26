"""The clean slate: the preview numbers, a checked backup, and a wipe that runs only when the numbers, the backup and the
engine all agree. Everything runs in a temporary folder with a fake engine; the real data is never touched."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from villomillo import cleanslate, clipper, store
from villomillo.db import Database


class FakeEngine:
    def __init__(self, clips_by_video=None, offline=False):
        self.clips_by_video, self.offline, self.deleted = clips_by_video or {}, offline, []

    def clips(self, video_id):
        if self.offline:
            raise clipper.EngineUnavailable("the clipping engine is not running at http://127.0.0.1:8765")
        return self.clips_by_video.get(video_id, [])

    def delete_clip(self, clip_id):
        self.deleted.append(clip_id)
        return True


def seed(db: Database) -> None:
    now = store.iso()
    with db.conn() as c:
        for n, video in ((1, "vid1"), (2, "vid2")):
            c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, validation_json, grade_json, "
                      "state, reason, created_at, format_applied) VALUES ('solved', ?, ?, 0, 30, '{}', '{}', 'approved', 'x', ?, 1)",
                      (n, video, now))
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, created_at) "
                  "VALUES ('solved', 1, 'youtube_shorts', 'api', 'uploaded', 't', 'c', '[]', ?, ?)", (now, now))
        c.execute("INSERT INTO post_stats (post_id, taken_at, views) VALUES (1, ?, 900)", (now,))
        c.execute("INSERT INTO copy_options (post_id, title, caption, tags_json, source, created_at) VALUES (1, 't', 'c', '[]', 'x', ?)", (now,))
        c.execute("INSERT INTO rewards_submissions (post_id, campaign_id, status, updated_at) VALUES (1, 'c1', 'handed', ?)", (now,))
        c.execute("INSERT INTO campaign_submissions (post_id, campaign_id, submitted_at) VALUES (1, 'c1', ?)", (now,))
        c.execute("INSERT INTO clip_jobs (profile_id, video_id, engine_job_id, status, created_at, updated_at) "
                  "VALUES ('solved', 'vid1', 1, 'done', ?, ?)", (now, now))
        c.execute("INSERT INTO runs (url, platform, video_id, profile_id, state, created_at, updated_at) "
                  "VALUES ('u', 'youtube', 'vid1', 'solved', 'finished', ?, ?)", (now, now))
        c.execute("INSERT INTO campaigns (id, title, url, state, first_seen_at, fetched_at) VALUES ('c1', 'Campaign', 'u', 'joined', ?, ?)",
                  (now, now))
        c.execute("INSERT INTO campaign_queue (campaign_id, video_id, url, created_at) VALUES ('c1', 'vid9', 'u', ?)", (now,))
        c.execute("INSERT INTO research_candidates (video_id, platform, url, via, state, found_at) VALUES ('r1', 'youtube', 'u', 'watch', 'queued', ?)", (now,))
        c.execute("INSERT INTO research_candidates (video_id, platform, url, via, state, found_at) VALUES ('r2', 'youtube', 'u', 'watch', 'skipped', ?)", (now,))
        c.execute("INSERT INTO tasks (kind, payload_json, dedupe_key, status, run_after, created_at) VALUES ('copy', '{}', 'k1', 'queued', ?, ?)", (now, now))
        c.execute("INSERT INTO tasks (kind, payload_json, dedupe_key, status, run_after, created_at) VALUES ('old', '{}', 'k2', 'done', ?, ?)", (now, now))


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = Database(tmp_path / "db" / "villomillo.db")
    seed(db)
    outbox, previews, thumbs, engine_clips = (tmp_path / d for d in ("outbox", "previews", "thumbs", "engine-clips"))
    for folder in (outbox / "solved", previews, thumbs, engine_clips / "vid1"):
        folder.mkdir(parents=True)
        (folder / "clip.mp4").write_bytes(b"video")
    monkeypatch.setattr(cleanslate, "APP_FILE_DIRS", [outbox, previews, thumbs])
    monkeypatch.setattr(cleanslate, "ENGINE_CLIP_DIR", engine_clips)
    monkeypatch.setattr(cleanslate, "BACKUP_FILE_DIRS", [outbox, previews, thumbs, engine_clips])
    return db, tmp_path / "backups"


def test_the_preview_counts_what_would_go_and_its_digest_follows_the_numbers(world):
    db, _ = world
    report = cleanslate.preview(db)
    assert report["counts"]["clips"] == 2 and report["counts"]["posts"] == 1
    assert report["counts"]["campaign_hand_ins"] == 1 and report["counts"]["content_rewards_attempts"] == 1
    assert report["counts"]["research_waiting"] == 1 and report["counts"]["tasks_waiting"] == 1
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, validation_json, grade_json, "
                  "state, reason, created_at) VALUES ('solved', 3, 'vid3', 0, 30, '{}', '{}', 'review', 'x', ?)", (store.iso(),))
    assert cleanslate.preview(db)["digest"] != report["digest"]


def test_a_backup_is_checked_and_holds_the_database_and_the_clip_files(world):
    db, root = world
    folder = cleanslate.backup(db, root=root, when=datetime(2026, 10, 25, 14, 0, 0))
    manifest = json.loads((folder / cleanslate.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["verified"] is True and manifest["digest"] == cleanslate.preview(db)["digest"]
    assert manifest["files_in_zip"] == 4 and (folder / "villomillo.db").exists() and (folder / "clip-files.zip").exists()


def test_a_wipe_refuses_numbers_that_have_changed_or_no_backup(world):
    db, root = world
    digest = cleanslate.preview(db)["digest"]
    with pytest.raises(cleanslate.CleanSlateError, match="numbers have changed"):
        cleanslate.wipe(db, "0000000000", root=root, engine=FakeEngine())
    with pytest.raises(cleanslate.CleanSlateError, match="no checked backup"):
        cleanslate.wipe(db, digest, root=root, engine=FakeEngine())


def test_a_wipe_refuses_while_the_clipping_engine_is_off(world):
    db, root = world
    digest = cleanslate.preview(db)["digest"]
    cleanslate.backup(db, root=root)
    with pytest.raises(cleanslate.CleanSlateError, match="Start the clipping engine"):
        cleanslate.wipe(db, digest, root=root, engine=FakeEngine(offline=True))
    assert cleanslate.preview(db)["counts"]["clips"] == 2  # nothing was removed


def test_a_wipe_removes_clips_posts_and_queues_and_keeps_the_rest(world):
    db, root = world
    digest = cleanslate.preview(db)["digest"]
    cleanslate.backup(db, root=root)
    engine = FakeEngine({"vid1": [{"id": 1}], "vid2": [{"id": 2}]})
    result = cleanslate.wipe(db, digest, root=root, engine=engine)

    assert sorted(engine.deleted) == [1, 2] and result["engine_clips_removed"] == 2
    after = cleanslate.preview(db)["counts"]
    assert all(after[k] == 0 for k in ("clips", "posts", "clip_jobs", "runs", "campaign_queue", "campaign_hand_ins",
                                       "content_rewards_attempts", "research_waiting", "tasks_waiting"))
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1          # kept
        assert c.execute("SELECT COUNT(*) FROM research_candidates WHERE state='skipped'").fetchone()[0] == 1  # history kept
        assert c.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0] == 1               # history kept
        assert c.execute("SELECT COUNT(*) FROM post_stats").fetchone()[0] == 0
    assert all(not any(p.iterdir()) for p in cleanslate.APP_FILE_DIRS)
