"""The worker's own record of how far a running job has got.

An integration's dock may connect in the middle of a two-hour job and never
see the progress events that went out before. What it reads must agree with
the percentage and time left the app itself is showing.
"""

import time
from pathlib import Path

import pytest

try:
    from server.jobs import Worker
except ImportError as e:  # CI installs only the light dependencies
    pytest.skip(f"worker imports unavailable: {e}", allow_module_level=True)

JOB_PROGRESS = Path(__file__).resolve().parent.parent / "ui/src/renderer/src/lib/jobProgress.ts"


@pytest.fixture
def worker(tmp_path):
    w = Worker({"paths": {"data_dir": str(tmp_path)}})
    w._progress[7] = {"started": time.time() - 100, "fraction": 0.0, "stage": "", "label": "Starting"}
    return w


def test_stage_weights_match_the_app(worker):
    worker._record_progress(7, {"stage": "transcribe", "fraction": 0.5})
    snap = worker.progress_snapshot(7)
    assert snap["percent"] == round((0.15 + 0.25 * 0.5) * 100)
    assert snap["label"] == "Transcribing speech"


def test_progress_never_moves_backwards(worker):
    worker._record_progress(7, {"stage": "analyze", "current": 3, "total": 4})
    before = worker.progress_snapshot(7)["percent"]
    worker._record_progress(7, {"stage": "download", "fraction": 0.2})
    assert worker.progress_snapshot(7)["percent"] == before


def test_time_left_is_extrapolated_from_elapsed_time(worker):
    worker._record_progress(7, {"stage": "analyze", "fraction": 0.5})  # 55%
    eta = worker.progress_snapshot(7)["eta_seconds"]
    assert 70 <= eta <= 90  # 100 s elapsed * 45 / 55


def test_no_time_left_is_claimed_this_early(worker):
    worker._record_progress(7, {"stage": "download", "fraction": 0.1})  # 1.5%
    assert worker.progress_snapshot(7)["eta_seconds"] is None


def test_render_names_the_clip(worker):
    worker._record_progress(7, {"stage": "render", "clip": 3, "total": 12})
    assert worker.progress_snapshot(7)["label"] == "Rendering clip 3/12"


def test_a_job_that_is_not_running_has_no_progress(worker):
    assert worker.progress_snapshot(99) is None


def test_moment_runs_name_the_plugin_in_the_label(worker):
    worker._record_progress(7, {"stage": "ranking", "fraction": 0.5, "plugin": "Quarkbloom Rater"})
    snap = worker.progress_snapshot(7)
    assert snap["label"] == "Rating moments with Quarkbloom Rater"
    assert snap["percent"] == round((0.65 + 0.05 * 0.5) * 100)
    worker._record_progress(7, {"stage": "understand", "fraction": 0.5, "plugin": "Quarkbloom Notes"})
    assert worker.progress_snapshot(7)["label"] == "Understanding moments with Quarkbloom Notes"
    # VilloMillo's own ranking carries no plugin, and keeps its label.
    worker._record_progress(7, {"stage": "ranking", "current": 2, "total": 3})
    assert worker.progress_snapshot(7)["label"] == "Ranking the best moments"
    worker._record_progress(7, {"stage": "understand", "fraction": 0.2})
    assert worker.progress_snapshot(7)["label"] == "Understanding the moments"
    # A plugin name on another stage changes nothing.
    worker._record_progress(7, {"stage": "analyze", "fraction": 0.5, "plugin": "Quarkbloom Finder"})
    assert worker.progress_snapshot(7)["label"] == "Finding the best moments"


def test_an_edit_run_names_the_plugin_and_the_bar_never_goes_back(worker):
    worker._record_progress(7, {"stage": "understand", "fraction": 1.0, "plugin": "Quarkbloom Notes"})
    worker._record_progress(7, {"stage": "edit", "fraction": 0.5, "plugin": "Quarkbloom Trimmer"})
    snap = worker.progress_snapshot(7)
    assert snap["label"] == "Suggesting edits with Quarkbloom Trimmer"
    assert snap["percent"] == round((0.70 + 0.08 * 0.5) * 100)
    worker._record_progress(7, {"stage": "edit", "fraction": 0.2})
    assert worker.progress_snapshot(7)["label"] == "Suggesting edits"


