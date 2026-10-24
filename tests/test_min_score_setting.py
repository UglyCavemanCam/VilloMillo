"""Minimum score (clips.min_score) is set from Settings (issue #111): the "no
clips" explanation sends people there to lower it, and it wasn't there."""

import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def api(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("yaml")
    from fastapi.testclient import TestClient

    from main import BUNDLED_CONFIG, load_config
    from server.api import create_app

    settings = tmp_path / "settings.yaml"
    shutil.copy(ROOT / "config" / "settings.yaml", settings)
    config = load_config(BUNDLED_CONFIG)
    config["paths"]["data_dir"] = str(tmp_path / "data")
    app = create_app(config, settings)
    # No `with`: startup never runs, so no worker thread starts.
    return TestClient(app, base_url="http://127.0.0.1"), config, settings


def _line(settings: Path) -> str:
    return next(line for line in settings.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("min_score:"))


def test_minimum_score_is_read_and_saved_with_its_comment_kept(api):
    client, config, settings = api
    assert client.get("/settings").json()["min_score"] == config["clips"]["min_score"]
    before = _line(settings)
    assert client.patch("/settings", json={"min_score": 40}).status_code == 200
    after = _line(settings)
    assert after.split("#")[1] == before.split("#")[1] and after.split("#")[0].split()[-1] == "40"
    assert config["clips"]["min_score"] == 40                 # the next job uses it, no restart
    assert client.get("/settings").json()["min_score"] == 40


def test_minimum_score_outside_0_to_100_is_refused(api):
    client, config, settings = api
    before = settings.read_text(encoding="utf-8")
    assert client.patch("/settings", json={"min_score": 101}).status_code == 400
    assert settings.read_text(encoding="utf-8") == before


def test_a_settings_file_without_the_line_gets_one_under_clips(api):
    import yaml

    client, config, settings = api
    settings.write_text("model: gemma:7b\nclips:\n  vertical: true\n", encoding="utf-8")
    assert client.patch("/settings", json={"min_score": 35}).status_code == 200
    assert yaml.safe_load(settings.read_text(encoding="utf-8"))["clips"]["min_score"] == 35
