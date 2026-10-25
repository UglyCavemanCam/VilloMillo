"""Campaign rules on finished clips: length, face in the first seconds, subtitles, no ad read. No engine, no Jev."""

from __future__ import annotations

import json
import pathlib

import pytest
from pydantic import ValidationError

from villomillo import campaign
from villomillo.config import Campaign
from villomillo.db import Database

RULES = Campaign(name="test", min_s=20, max_s=60, face_window_s=5)
CLIP = pathlib.Path("clip.mp4")


def run(**over):
    args = {"duration_s": 40, "caption_text": "some words", "ad_read": 0.02, "face": lambda p, w: 1.0}
    args.update(over)
    return campaign.check(RULES, path=CLIP, **args)


def test_a_clip_that_meets_every_rule_passes_with_a_reminder_to_confirm_the_host():
    result = run()
    assert result["ok"] and result["problems"] == []
    assert any("confirm it is Mark" in w for w in result["warnings"])


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"duration_s": 12}, "12 s; the campaign wants 20 to 60 s"),
        ({"duration_s": 75}, "75 s; the campaign wants 20 to 60 s"),
        ({"face": lambda p, w: 0.2}, "no clear face in the first 5 s"),
        ({"caption_text": "   "}, "no subtitle text"),
        ({"ad_read": 0.9}, "looks like an ad read"),
        ({"ad_read": None}, "the ad-read check has not run"),
    ],
)
def test_each_rule_has_its_own_problem(over, expected):
    result = run(**over)
    assert not result["ok"]
    assert any(expected in p for p in result["problems"]), result["problems"]


def test_the_rules_can_be_switched_off_per_campaign():
    lax = RULES.model_copy(update={"require_captions": False, "no_ad_reads": False})
    result = campaign.check(lax, path=CLIP, duration_s=40, caption_text="", ad_read=None, face=lambda p, w: 1.0)
    assert result["ok"]


def test_the_campaign_block_must_have_a_sensible_length_range():
    with pytest.raises(ValidationError):
        Campaign(name="bad", min_s=60, max_s=20)


def test_the_solved_campaign_carries_the_brief_rules():
    from villomillo import campaigns

    rules = Campaign(name="Solved", **campaigns.SOLVED_RULES)
    assert (rules.min_s, rules.max_s, rules.face_window_s) == (20, 60, 5)
    assert rules.require_captions and rules.no_ad_reads


def test_face_share_reads_frames_from_the_first_window(tmp_path):
    """Uses a generated video with no people in it: the share must come back as 0, not raise."""
    import cv2
    import numpy as np

    path = tmp_path / "blank.mp4"
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 480))
    for _ in range(60):
        writer.write(np.full((480, 320, 3), 128, dtype=np.uint8))
    writer.release()
    assert campaign.face_share(path, 5.0) == 0.0


def test_the_rule_result_is_kept_on_the_clip(tmp_path):
    db = Database(tmp_path / "rules.db")
    with db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, "
                  "file_path, validation_json, grade_json, state, reason, created_at) "
                  "VALUES ('solved', 9, 'v', 0, 30, 80, 'h', 'f.mp4', '{}', ?, 'review', 'r', '2026-10-24T00:00:00+00:00')",
                  (json.dumps({"self_contained": 0.9}),))
        clip_id = c.execute("SELECT id FROM clips_ready").fetchone()["id"]
    campaign.record(db, "solved", clip_id, run())
    with db.conn() as c:
        grade = json.loads(c.execute("SELECT grade_json FROM clips_ready WHERE id=?", (clip_id,)).fetchone()["grade_json"])
    assert grade["self_contained"] == 0.9
    assert grade["campaign"]["ok"] is True and "checked_at" in grade["campaign"]


def test_the_face_rule_runs_only_when_the_campaign_requires_it():
    no_face_needed = RULES.model_copy(update={"require_face": False})
    asked = []

    def face(path, window):
        asked.append(window)
        return 0.0

    result = campaign.check(no_face_needed, path=CLIP, duration_s=40, caption_text="some words", ad_read=0.02, face=face)
    assert result["ok"] and asked == [] and result["facts"]["face_share"] is None
    assert not any("confirm it is Mark" in w for w in result["warnings"])
    required = campaign.check(RULES, path=CLIP, duration_s=40, caption_text="some words", ad_read=0.02, face=face)
    assert not required["ok"] and asked == [5]


def test_rules_saved_before_the_switch_existed_still_require_a_face():
    assert Campaign(name="old", min_s=20, max_s=60, face_window_s=5).require_face is True
