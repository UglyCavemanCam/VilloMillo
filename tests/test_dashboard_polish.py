"""Dashboard polish: failures look like the rest of the dashboard, and the shell carries the shortcuts and the font."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from villomillo import config
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app

PAGE = {"Accept": "text/html"}


@pytest.fixture
def app_and_db(tmp_path):
    db = Database(tmp_path / "polish.db")
    sv = Services(config.AppConfig(), config.load_profiles(), db, Quota(db, general_cap=9, search_cap=9))
    return create_app(sv), db


@pytest.fixture
def client(app_and_db):
    return TestClient(app_and_db[0], base_url="http://127.0.0.1:8780")


def test_a_browser_that_asks_for_a_missing_page_gets_a_page_inside_the_dashboard(client):
    r = client.get("/no-such-page?profile=solved", headers=PAGE)
    assert r.status_code == 404
    assert "That page is not here" in r.text and "Go to Home" in r.text
    assert 'class="sidebar"' in r.text  # still inside the shell


def test_an_unknown_channel_in_the_address_still_gets_a_page(client):
    r = client.get("/clips?profile=nobody", headers=PAGE)
    assert r.status_code == 404 and "Go to Home" in r.text


def test_scripts_and_the_api_keep_getting_json(client):
    assert client.get("/no-such-page?profile=solved").json() == {"detail": "Not Found"}
    assert client.get("/api/search?profile=nobody&q=ab", headers=PAGE).json()["detail"] == "no such channel profile"
    assert client.get("/clips/999/panel?profile=solved", headers={**PAGE, "X-Requested-With": "panel"}).json()["detail"] == "no such clip"


def test_a_crash_is_logged_and_shown_without_a_stack_trace(app_and_db):
    app, db = app_and_db

    @app.get("/boom")
    def boom():
        raise RuntimeError("disk full")

    client = TestClient(app, base_url="http://127.0.0.1:8780", raise_server_exceptions=False)
    r = client.get("/boom?profile=solved", headers=PAGE)
    assert r.status_code == 500 and "The dashboard hit an error" in r.text and "disk full" in r.text
    assert "Traceback" not in r.text
    assert client.get("/boom").json() == {"detail": "the dashboard hit an error; it is in the event log"}
    with db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM events WHERE component='dashboard' AND message LIKE '/boom%'").fetchone()["n"] == 2


def test_every_page_lists_the_shortcuts_and_loads_the_font(client):
    for path in ("/", "/clips", "/calendar", "/accounts", "/insights", "/settings"):
        html = client.get(f"{path}?profile=solved").text
        assert 'id="shortcuts"' in html and 'id="open-shortcuts"' in html, path
        assert "/static/fonts/Geist-Variable.woff2" in html, path
    assert client.get("/static/fonts/Geist-Variable.woff2").status_code == 200
    assert client.get("/static/fonts/Geist-OFL-LICENSE.txt").status_code == 200


def test_the_script_knows_every_page_shortcut(client):
    js = client.get("/static/app.js").text
    for key in ("h:", "c:", "p:", "a:", "i:", "s:"):
        assert key in js
