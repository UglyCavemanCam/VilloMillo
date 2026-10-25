"""The clip engine's status and supervision, the health checks, the doctor command and the Settings panel. No real process or network."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from villomillo import channels, cli, config, engine_manager, health, runner, store
from villomillo.db import Database
from villomillo.engine_manager import EngineManager
from villomillo.quota import Quota
from villomillo.services import Services
from villomillo.web.app import create_app


class FakeProc:
    def __init__(self):
        self.code, self.pid, self.ended = None, 4242, []

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0
        self.ended.append("terminate")

    def kill(self):
        self.code = -9

    def wait(self, timeout=None):
        return self.code


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(tmp_path, up=False):
    state = {"up": up, "procs": []}
    clock = Clock()

    def popen(cmd, **kw):
        p = FakeProc()
        state["procs"].append((cmd, p))
        return p

    m = EngineManager(url="http://127.0.0.1:8765", command=["engine", "serve"], log_path=tmp_path / "engine.log", popen=popen,
                      probe=lambda: state["up"], clock=clock)
    return m, state, clock


def test_an_answering_engine_is_on_and_one_failed_check_does_not_turn_the_light_off(tmp_path):
    m, state, clock = make(tmp_path, up=True)
    assert m.status()["state"] == "on"
    state["up"] = False
    clock.t += 5
    assert m.status()["state"] == "on"  # one miss: a busy engine answers slowly
    clock.t += 5
    assert m.status()["state"] == "not_responding"  # two in a row, and it had been up
    state["up"] = True
    clock.t += 5
    assert m.status()["state"] == "on" and m.status()["uptime_s"] is not None


def test_an_engine_that_never_answered_is_off_and_the_dashboard_alone_cannot_start_it(tmp_path):
    m, state, clock = make(tmp_path)
    assert m.status()["state"] == "off" and not m.up
    assert "run-app.bat" in m.start() and not state["procs"]
    assert "run-app.bat" in m.restart()


def test_running_the_app_starts_the_engine_and_a_first_start_is_starting_not_off(tmp_path):
    m, state, clock = make(tmp_path)
    assert "Starting" in m.enable()
    cmd, proc = state["procs"][0]
    assert cmd == ["engine", "serve"] and "engine starting" in (tmp_path / "engine.log").read_text()
    assert m.status()["state"] == "starting" and m.status()["managed"]
    clock.t += 400  # models load slowly, but not forever
    assert m.status()["state"] == "not_responding"
    state["up"] = True
    clock.t += 5
    assert m.status()["state"] == "on"


def test_an_engine_that_already_answers_is_left_alone(tmp_path):
    m, state, clock = make(tmp_path, up=True)
    assert "already running" in m.enable() and not state["procs"]
    assert "outside VilloMillo" in m.restart() and not state["procs"]


def test_an_engine_that_exits_is_started_again_after_a_short_wait(tmp_path):
    m, state, clock = make(tmp_path)
    m.enable()
    state["up"] = True
    clock.t += 5
    m.tick()
    assert m.status()["state"] == "on"
    state["procs"][0][1].code = 1
    (tmp_path / "engine.log").write_text("x\nModuleNotFoundError: No module named 'faster_whisper'\n")
    state["up"] = False
    m.tick()
    assert len(state["procs"]) == 1  # waits first
    clock.t += 2.5
    m.tick()
    assert len(state["procs"]) == 2 and m.restarts == 1


def test_repeated_crashes_stop_the_retrying_and_show_the_last_error_until_you_press_start(tmp_path):
    m, state, clock = make(tmp_path)
    m.enable()
    for _ in range(3):
        state["procs"][-1][1].code = 1
        (tmp_path / "engine.log").write_text("--- engine starting ---\nModuleNotFoundError: No module named 'faster_whisper'\n")
        m.tick()
        clock.t += 20
        m.tick()
    st = m.status()
    assert st["state"] == "error" and st["gave_up"] and "faster_whisper" in st["last_error"]
    count = len(state["procs"])
    clock.t += 100
    m.tick()
    assert len(state["procs"]) == count  # it does not keep trying
    assert "Starting" in m.start() and len(state["procs"]) == count + 1 and m.status()["state"] == "starting"


def test_restart_stops_what_it_started_and_starts_it_again_and_stop_ends_the_engine(tmp_path):
    m, state, clock = make(tmp_path)
    m.enable()
    first = state["procs"][0][1]
    state["up"] = True
    clock.t += 5
    assert m.status()["state"] == "on"
    assert m.restart() == "Restarting the engine." and first.ended == ["terminate"] and len(state["procs"]) == 2
    m.stop()
    assert state["procs"][1][1].ended == ["terminate"] and not m.supervised


def test_a_library_that_will_not_load_raises_an_error_instead_of_opening_a_box(tmp_path):
    calls = []
    fake = type("K", (), {"SetErrorMode": staticmethod(lambda mode: calls.append(mode))})()
    assert engine_manager.quiet_dialogs(fake) is True and calls == [0x8001]
    assert engine_manager.quiet_dialogs() in (True, False)  # on Linux it does nothing; on Windows it sets the same mode
    m, state, clock = make(tmp_path)
    m.enable()  # starting the engine sets it first, so the engine inherits it
    assert state["procs"]


def test_the_last_error_is_the_telling_line_of_the_log(tmp_path):
    log = tmp_path / "engine.log"
    log.write_text("starting\nTraceback (most recent call last):\n  File x\nOSError: [Errno 98] Address already in use\nbye\n")
    assert engine_manager.last_error(log) == "OSError: [Errno 98] Address already in use"
    log.write_text("--- engine starting ---\nloading models\n")
    assert engine_manager.last_error(log) == "loading models"
    assert engine_manager.last_error(tmp_path / "none.log") is None
    assert "loading models" in engine_manager.tail(log)


# ---- health, doctor and the pages ------------------------------------------------------------------------------------------


@pytest.fixture
def sv(tmp_path, monkeypatch):
    m, state, clock = make(tmp_path)
    monkeypatch.setattr(engine_manager, "manager", m)
    monkeypatch.setattr(health.secrets, "is_stored", lambda name: name in ("youtube_api_key", "typesafe_api_key"))
    database = Database(tmp_path / "h.db")
    s = Services(config.AppConfig(), channels.load(database), database, Quota(database, general_cap=9, search_cap=9), None, None)
    return s, m, state, clock


def by_key(items):
    return {i["key"]: i for i in items}


def test_the_worker_proves_it_is_alive_and_a_missing_one_is_reported(sv):
    s, m, state, clock = sv
    assert health.worker(s.db)["state"] == "off"
    assert by_key(health.checks(s))["worker"]["state"] == "bad" and "run-app.bat" in by_key(health.checks(s))["worker"]["fix"]
    health.beat(s.db, "research")
    assert health.worker(s.db) == {"state": "running", "age_s": 0, "task": "research"} or health.worker(s.db)["state"] == "running"
    with s.db.conn() as c:
        c.execute("UPDATE meta SET value=? WHERE key='worker_beat'", (store.iso(store.now().replace(year=2020)),))
    assert health.worker(s.db)["state"] == "stuck"


def test_the_running_worker_beats_around_every_task(sv):
    s, m, state, clock = sv
    runner.run_one(s)  # nothing to do
    from villomillo import tasks

    tasks.enqueue(s.db, "research", {"profile": None})
    assert runner.run_one(s) and health.worker(s.db)["state"] == "running" and health.worker(s.db)["task"] == ""


def test_the_checks_name_what_is_missing_and_what_to_do(sv):
    s, m, state, clock = sv
    got = by_key(health.checks(s))
    assert got["engine"]["state"] == "bad" and got["key:anthropic_api_key"]["state"] == "bad" and "key claude" in got["key:anthropic_api_key"]["fix"]
    assert got["key:twitch_client_id"]["state"] == "warn" and got["key:youtube_api_key"]["state"] == "ok"
    assert got["yt:solved"]["state"] == "warn" and got["post:solved"]["state"] == "warn" and "Dry run" in got["post:solved"]["detail"]
    assert "to fix" in health.summary(list(got.values()))
    state["up"] = True
    clock.t += 5
    assert by_key(health.checks(s))["engine"]["state"] == "ok"


def test_the_doctor_command_lists_everything_and_fails_when_something_must_be_fixed(sv, monkeypatch, capsys):
    s, m, state, clock = sv
    monkeypatch.setattr(cli, "_services", lambda with_apis=True: s)
    with pytest.raises(SystemExit) as stop:
        cli.cmd_doctor(None)
    out = capsys.readouterr().out
    assert stop.value.code == 1 and "[FIX ] Clip engine" in out and "-> " in out and "to fix" in out


def test_the_settings_panel_shows_state_buttons_log_and_health(sv):
    s, m, state, clock = sv
    client = TestClient(create_app(s), base_url="http://127.0.0.1:8780")
    page = client.get("/settings").text
    assert "Clip engine" in page and "Open log" in page and "Health" in page and "watching only" in page and "to fix" in page
    assert "Clip engine is off" in page  # the sidebar light, with the same words
    m.enable()
    page = client.get("/settings").text
    assert "Starting" in page and "watching only" not in page
    state["up"] = True
    clock.t += 5
    assert "Clip engine is on" in client.get("/settings").text
    done = client.post("/api/engine/restart", follow_redirects=False)
    assert done.status_code == 303 and "Restarting" in done.headers["location"].replace("%20", " ")
    assert client.post("/api/engine/explode").status_code == 404
    m.log_path.write_text("hello from the engine")
    assert "hello from the engine" in client.get("/engine/log").text


def test_the_sidebar_light_links_to_the_panel(sv):
    s, m, state, clock = sv
    assert 'href="/settings?profile=solved#engine"' in TestClient(create_app(s), base_url="http://127.0.0.1:8780").get("/").text
