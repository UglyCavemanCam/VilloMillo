"""Saved caption lines follow a clip whose start or end moved (#120).

A clip can carry its caption lines in its render options: the text as the
user corrected it, timed from the clip's start. Made ten seconds longer, such
a clip was burned with the same lines again, so the added seconds had no
captions, and no re-render, font change or second request could bring them:
every one of them read the same saved lines. A clip with nothing saved was
fine, which is why it looked like one cursed clip.
"""

import json
import re

import pytest

from core.models import ClipCandidate, Segment
from video.captions import build_caption_lines, refit_caption_lines


def _segments() -> list[Segment]:
    """A word every half second for a minute: w0 at 0.0-0.5, w1 at 0.5-1.0, ..."""
    words = [{"start": i * 0.5, "end": i * 0.5 + 0.5, "word": f"w{i}"} for i in range(120)]
    return [Segment(start=0.0, end=60.0, text=" ".join(w["word"] for w in words), words=words)]


def _saved(start: float, end: float) -> list[dict]:
    return build_caption_lines(_segments(), ClipCandidate(start=start, end=end, score=0), 3)


def _said(lines: list[dict]) -> list[int]:
    """Every transcript word the lines show, in order."""
    return [int(n) for line in lines for n in re.findall(r"w(\d+)", line["text"])]


def test_the_added_seconds_get_captions_and_the_corrected_text_stays():
    saved = _saved(10.0, 20.0)
    saved[1]["text"] = "what they really said"

    lines = refit_caption_lines(saved, _segments(), old_start=10.0, new_start=10.0, new_end=30.0)

    assert lines[: len(saved)] == saved                       # nothing saved was touched
    assert lines[-1]["end"] == pytest.approx(20.0)            # captions to the new end
    assert _said(lines[len(saved):]) == list(range(40, 60))   # the ten added seconds, each word once


def test_a_word_cut_by_the_old_end_is_not_said_twice():
    """The old clip ended mid-word: that word is already in the last saved line."""
    saved = _saved(10.0, 20.25)
    assert 40 in _said(saved)

    lines = refit_caption_lines(saved, _segments(), 10.0, 10.0, 30.0)

    assert _said(lines) == list(range(20, 60))


def test_an_earlier_start_shifts_the_saved_lines_and_captions_the_head():
    saved = _saved(10.0, 20.0)
    saved[0]["text"] = "fixed opening"

    lines = refit_caption_lines(saved, _segments(), old_start=10.0, new_start=5.0, new_end=20.0)

    moved = next(l for l in lines if l["text"] == "fixed opening")
    assert moved["start"] == pytest.approx(saved[0]["start"] + 5.0)   # still on the same words
    assert _said([l for l in lines if l["end"] <= 5.0]) == list(range(10, 20))
    assert lines == sorted(lines, key=lambda l: l["start"])


def test_a_shorter_clip_drops_what_it_no_longer_holds():
    saved = _saved(10.0, 20.0)

    lines = refit_caption_lines(saved, _segments(), 10.0, 10.0, 15.2)

    assert max(l["end"] for l in lines) <= 5.2 + 1e-9         # a line across the new end is cut at it
    assert all(l["start"] < 5.2 for l in lines)
    assert len(lines) < len(saved)


def test_a_line_blanked_on_purpose_stays_blank():
    saved = _saved(10.0, 20.0)
    saved[-1]["text"] = ""                                   # the user deleted that caption

    lines = refit_caption_lines(saved, _segments(), 10.0, 10.0, 30.0)

    assert lines[len(saved) - 1]["text"] == ""
    assert _said(lines[len(saved):]) == list(range(40, 60))   # and nothing fills its place


def test_a_clip_that_grew_before_the_fix_gets_its_captions_back():
    """The clip is already 10-30 in the library with lines saved for 10-20:
    its next re-render, with no change of start or end, fills the rest."""
    saved = _saved(10.0, 20.0)

    lines = refit_caption_lines(saved, _segments(), old_start=10.0, new_start=10.0, new_end=30.0)

    assert _said(lines) == list(range(20, 60))


def test_a_clip_moved_clear_of_its_saved_lines_is_captioned_afresh():
    lines = refit_caption_lines(_saved(10.0, 20.0), _segments(), 10.0, 40.0, 50.0)

    assert lines == _saved(40.0, 50.0)


def test_an_unchanged_clip_keeps_its_saved_lines_exactly():
    saved = _saved(10.0, 20.0)
    saved[2]["text"] = "corrected"

    assert refit_caption_lines(saved, _segments(), 10.0, 10.0, 20.0) == saved


def test_a_rerender_with_a_later_end_burns_captions_into_the_added_seconds(db, tmp_path, monkeypatch):
    """The whole path: the render job hands the render the refitted lines."""
    pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from types import SimpleNamespace

    from core import pipeline
    from server.jobs import Worker

    db.conn.execute(
        "INSERT INTO videos (video_id, title, status, created_at, updated_at) "
        "VALUES ('v1', 't', 'done', '2026-01-01', '2026-01-01')"
    )
    db.conn.execute(
        "INSERT INTO clips (video_id, start_s, end_s, score, render_opts, created_at) "
        "VALUES ('v1', 10.0, 20.0, 90, ?, '2026-01-01')",
        (json.dumps({"caption_lines": _saved(10.0, 20.0)}),),
    )
    db.conn.commit()
    clip_id = db.conn.execute("SELECT id FROM clips").fetchone()["id"]
    (tmp_path / "downloads").mkdir()
    (tmp_path / "downloads" / "v1.mp4").write_bytes(b"source")
    (tmp_path / "transcripts").mkdir()
    seg = _segments()[0]
    (tmp_path / "transcripts" / "v1.json").write_text(
        json.dumps({"segments": [{"start": seg.start, "end": seg.end, "text": seg.text, "words": seg.words}]}),
        encoding="utf-8",
    )

    seen: dict = {}

    def capture(_source, candidate, _segments, _clip_dir, _config, render_opts, _lang):
        seen["window"] = (candidate.start, candidate.end)
        seen["lines"] = render_opts["caption_lines"]
        raise RuntimeError("stop before any file is made")

    monkeypatch.setattr(pipeline, "_render_files", capture)
    job = SimpleNamespace(config={"paths": {"data_dir": str(tmp_path)}})
    with pytest.raises(RuntimeError, match="stop before"):
        Worker._rerender_clip(job, db, {"clip_id": clip_id, "end": 30.0})

    assert seen["window"] == (10.0, 30.0)
    assert _said(seen["lines"]) == list(range(20, 60))
