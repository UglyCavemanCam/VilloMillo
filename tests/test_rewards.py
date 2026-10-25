"""Handing posted campaign clips in to Content Rewards by a browser signed in as you.

The queue and the outcomes are tested with a stand-in browser. The real browser code is tested against a small local web page that
behaves like the site (a Sign in button, a Submit clip dialog, the refusals it can give), when a browser program is available."""

from __future__ import annotations

import json
import os
import pathlib
import random
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from villomillo import campaigns, channels, config, posting, rewards, runner, store
from villomillo.db import Database
from villomillo.quota import Quota
from villomillo.rewards import Outcome
from villomillo.services import Services
from villomillo.web.app import create_app

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
SOLVED = campaigns.SOLVED_ID
RULES = config.Scoring()


@pytest.fixture
def sv(tmp_path):
    db = Database(tmp_path / "rewards.db")
    campaigns.seed_solved(db, RULES)
    return Services(config.AppConfig(), channels.load(db), db, Quota(db, general_cap=9, search_cap=9))


def campaign_post(sv, *, campaign=SOLVED, status="uploaded", url="https://youtube.com/shorts/abc", n=1) -> int:
    with sv.db.conn() as c:
        c.execute("INSERT INTO clips_ready (profile_id, engine_clip_id, video_id, start_s, end_s, engine_score, hook, file_path, validation_json, grade_json, "
                  "state, reason, created_at, campaign_id) VALUES ('solved', (SELECT COALESCE(MAX(engine_clip_id), 0) + 1 FROM clips_ready), 'vid00000001', 0, 30, 90, 'h', "
                  "'f.mp4', '{}', '{}', 'approved', 'r', ?, ?)", (store.iso(), campaign))
        clip_id = c.execute("SELECT MAX(id) AS id FROM clips_ready").fetchone()["id"]
        c.execute("INSERT INTO posts (profile_id, clip_id, platform, route, status, title, caption, tags_json, scheduled_at, url, posted_at, created_at) "
                  "VALUES ('solved', ?, 'youtube_shorts', 'api', ?, 't', 'c', '[]', ?, ?, ?, ?)", (clip_id, status, store.iso(NOW), url, store.iso(NOW), store.iso(NOW)))
        return c.execute("SELECT MAX(id) AS id FROM posts").fetchone()["id"]


class FakeDriver:
    """Stands in for the browser: says what the page would say."""

    def __init__(self, outcome=None, boom=None):
        self.outcome, self.boom, self.calls = outcome or Outcome("submitted", "Sent."), boom, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, campaign_url, post_url, *, dry_run):
        self.calls.append((campaign_url, post_url, dry_run))
        if self.boom:
            raise self.boom
        return self.outcome

    def signed_in(self, url=None):
        return True


def ready(sv, *, dry_run=False, signed_in=True):
    rewards.set_dry_run(sv.db, dry_run)
    rewards.set_signed_in(sv.db, signed_in)


def work(sv, post_id, driver, *, now=NOW, rng=None):
    return rewards.work(sv, post_id, driver_factory=lambda **k: driver, now=now, rng=rng or random.Random(1))


# ---- reading what the page says --------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,status", [
    ("You've already submitted this clip.", "duplicate"),
    ("This post is too old to submit.", "post_age"),
    ("Posted more than 24 hours ago", "post_age"),
    ("Link your YouTube account before you submit.", "not_linked"),
    ("This post does not use the required sound.", "wrong_sound"),
    ("This platform is not supported for this campaign.", "unsupported"),
    ("Drafts require pre-approval before posting.", "needs_draft"),
])
def test_each_refusal_the_page_can_give_is_recognised(text, status):
    assert rewards.classify("Submit a clip\n" + text + "\nCancel").status == status


def test_nothing_is_recognised_in_a_clean_form_and_success_is_told_apart():
    assert rewards.classify("Paste your post link\nSubmit\nCancel") is None
    assert rewards.looks_submitted("Clip submitted. It is now under review.") and not rewards.looks_submitted("Paste your post link")
    assert "too old" in rewards.classify("Check your link.\nThis post is too old.\nTry another.").detail


# ---- the queue -------------------------------------------------------------------------------------------------------


def test_a_submitted_clip_is_recorded_and_counts_as_handed_in(sv):
    ready(sv)
    post = campaign_post(sv)
    driver = FakeDriver()
    line = work(sv, post, driver)
    assert "handed in" in line
    assert driver.calls == [(campaigns.get(sv.db, SOLVED)["url"] or f"https://contentrewards.com/c/campaigns/{SOLVED}", "https://youtube.com/shorts/abc", False)]
    assert rewards.state_of(sv.db, post)["status"] == "submitted"
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM campaign_submissions WHERE post_id=?", (post,)).fetchone()["n"] == 1
    assert "already handed in" in work(sv, post, FakeDriver(), now=NOW + timedelta(hours=1))
    assert campaigns.ready_to_submit(sv.db) == []


def test_trial_mode_fills_the_form_but_hands_nothing_in(sv):
    ready(sv, dry_run=True)
    post = campaign_post(sv)
    driver = FakeDriver(Outcome("dry_run", "stopped before Submit", "/tmp/shot.png"))
    work(sv, post, driver)
    assert driver.calls[0][2] is True
    state = rewards.state_of(sv.db, post)
    assert state["status"] == "dry_run" and state["screenshot"] == "/tmp/shot.png"
    assert len(campaigns.ready_to_submit(sv.db)) == 1  # still to hand in


@pytest.mark.parametrize("status", ["duplicate"])
def test_an_already_handed_in_clip_counts_as_done(sv, status):
    ready(sv)
    post = campaign_post(sv)
    work(sv, post, FakeDriver(Outcome(status, "already submitted")))
    assert campaigns.ready_to_submit(sv.db) == []


@pytest.mark.parametrize("status", ["post_age", "not_linked", "needs_draft", "wrong_sound", "unsupported", "not_joined"])
def test_a_refusal_is_recorded_with_its_reason_and_is_not_counted_as_handed_in(sv, status):
    ready(sv)
    post = campaign_post(sv)
    work(sv, post, FakeDriver(Outcome(status, "the page said so")))
    assert rewards.state_of(sv.db, post)["status"] == status and len(campaigns.ready_to_submit(sv.db)) == 1
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='submit_clip'").fetchone()["n"] == 0  # not worth trying again by itself


def test_an_unclear_page_or_a_crash_is_tried_again_later_three_times_then_left_for_you(sv):
    ready(sv)
    post = campaign_post(sv)
    for attempt, now in enumerate((NOW, NOW + timedelta(hours=1), NOW + timedelta(hours=2)), 1):
        driver = FakeDriver(boom=RuntimeError("browser died")) if attempt == 1 else FakeDriver(Outcome("unknown", "unclear"))
        work(sv, post, driver, now=now)
        with sv.db.conn() as c:
            queued = c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='submit_clip' AND dedupe_key LIKE '%retry'").fetchone()["n"]
        assert queued == (attempt if attempt < rewards.MAX_ATTEMPTS else attempt - 1)
    state = rewards.state_of(sv.db, post)
    assert state["status"] == "unknown" and state["attempts"] == 3 and len(campaigns.ready_to_submit(sv.db)) == 1


def test_nothing_is_attempted_when_signed_out_off_not_public_or_not_on_youtube(sv):
    post = campaign_post(sv)
    driver = FakeDriver()
    rewards.set_signed_in(sv.db, False)
    assert "sign in needed" in work(sv, post, driver)
    ready(sv)
    rewards.set_auto(sv.db, False)
    assert "off" in work(sv, post, driver)
    rewards.set_auto(sv.db, True)
    sv.profiles["solved"] = sv.profiles["solved"].model_copy(update={"posting": sv.profiles["solved"].posting.model_copy(update={"privacy": "private"})})
    assert "not public" in work(sv, post, driver) or "is not public" in work(sv, post, driver)
    sv.profiles["solved"] = sv.profiles["solved"].model_copy(update={"posting": sv.profiles["solved"].posting.model_copy(update={"privacy": "public"})})
    waiting = campaign_post(sv, status="queued", url="")
    assert "not on YouTube yet" in work(sv, waiting, driver)
    assert driver.calls == []


def test_a_page_that_asks_to_sign_in_marks_the_session_as_signed_out(sv):
    ready(sv)
    post = campaign_post(sv)
    work(sv, post, FakeDriver(Outcome("needs_login", "asks you to sign in")))
    assert rewards.settings(sv.db)["signed_in"] is False and rewards.state_of(sv.db, post)["status"] == "needs_login"


def test_submissions_are_one_at_a_time_with_a_pause_between(sv):
    ready(sv)
    first, second = campaign_post(sv, url="https://youtube.com/shorts/one"), campaign_post(sv, url="https://youtube.com/shorts/two")
    work(sv, first, FakeDriver())
    driver = FakeDriver()
    line = work(sv, second, driver, now=NOW + timedelta(seconds=5))
    assert "waiting a moment" in line and driver.calls == []
    with sv.db.conn() as c:
        later = c.execute("SELECT run_after FROM tasks WHERE kind='submit_clip' ORDER BY id DESC").fetchone()["run_after"]
    assert NOW + timedelta(seconds=rewards.GAP_S[0] - 1) <= store.parse_time(later) <= NOW + timedelta(seconds=rewards.GAP_S[1] + 1)
    assert "handed in" in work(sv, second, driver, now=NOW + timedelta(seconds=rewards.GAP_S[1] + 2))


def test_a_campaign_clip_is_queued_the_moment_it_is_posted_and_other_clips_are_not(sv, monkeypatch):
    posted = campaign_post(sv)
    plain = campaign_post(sv, campaign=None)
    assert rewards.queue(sv.db, posted) is not None and rewards.queue(sv.db, plain) is None
    rewards.set_auto(sv.db, False)
    assert rewards.queue(sv.db, campaign_post(sv)) is None
    # the upload itself queues it
    rewards.set_auto(sv.db, True)
    monkeypatch.setattr(posting, "default_adapters", lambda profile: {"youtube_shorts": type("A", (), {"post": lambda self, **k: {"url": "https://youtube.com/shorts/new", "notes": [], "remote_id": "new"}})()})
    new = campaign_post(sv, status="manual_ready", url="")
    with sv.db.conn() as c:
        c.execute("DELETE FROM tasks")
    p = sv.profiles["solved"]
    sv.profiles["solved"] = p.model_copy(update={"posting": p.posting.model_copy(update={"dry_run": False, "platforms": {"youtube_shorts": "api"}})})
    from villomillo import accounts

    accounts.set_audit(sv.db, "solved", "youtube_shorts", True, "t")
    posting.resend_waiting(sv.db, sv.profiles["solved"], "youtube_shorts", now=NOW)
    posting.run_due(sv.db, sv.profiles["solved"], now=NOW + timedelta(hours=1), adapters=posting.default_adapters(sv.profiles["solved"]))
    with sv.db.conn() as c:
        assert c.execute("SELECT status FROM posts WHERE id=?", (new,)).fetchone()["status"] == "uploaded"
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='submit_clip'").fetchone()["n"] >= 1


def test_waiting_clips_are_queued_oldest_first_and_after_signing_in_the_stuck_ones_go_again(sv):
    ready(sv)
    ids = [campaign_post(sv, url=f"https://youtube.com/shorts/{i}") for i in range(3)]
    assert rewards.queue_waiting(sv.db) == 3
    with sv.db.conn() as c:
        rows = c.execute("SELECT payload_json, run_after FROM tasks WHERE kind='submit_clip' ORDER BY id").fetchall()
    assert [json.loads(r["payload_json"])["post_id"] for r in rows] == ids and len(rows) == 3
    rewards.record(sv.db, ids[0], SOLVED, Outcome("needs_login", "x"))
    with sv.db.conn() as c:
        c.execute("DELETE FROM tasks")
    assert rewards.retry_failed(sv.db) == 1


def test_the_worker_runs_a_submit_task_and_a_sign_in_check(sv, monkeypatch):
    ready(sv)
    post = campaign_post(sv)
    done = []
    monkeypatch.setattr(rewards, "work", lambda sv_, pid, **k: done.append(pid) or f"post {pid}: handed in")
    runner.do_submit_clip(sv, {"post_id": post})
    assert done == [post]
    seen = []
    monkeypatch.setattr(rewards, "check_signed_in", lambda db, **k: seen.append(1) or True)
    runner.do_rewards_check(sv, {})
    assert seen == [1] and {"submit_clip", "rewards_check"} <= set(runner.HANDLERS)


# ---- the page and the buttons ----------------------------------------------------------------------------------------


@pytest.fixture
def client(sv):
    return TestClient(create_app(sv), base_url="http://127.0.0.1:8780")


def test_the_campaigns_page_shows_the_sign_in_status_and_the_buttons(sv, client, monkeypatch):
    campaign_post(sv)
    page = client.get("/campaigns").text
    assert "Handing in clips to Content Rewards" in page and "Sign in to Content Rewards" in page and "Hand in waiting clips" in page
    assert "Not signed in yet" in page or "Sign in needed" in page or "browser tool is not installed" in page
    rewards.set_signed_in(sv.db, True)
    monkeypatch.setattr(rewards, "missing", lambda: None)
    page = client.get("/campaigns").text
    assert "Signed in" in page and "Trial mode" in page and "<b>On</b>" in page


def test_the_buttons_start_the_sign_in_queue_the_check_and_save_the_switches(sv, client, monkeypatch):
    started = []
    monkeypatch.setattr(rewards, "start_login", lambda db: started.append(1) or True)
    assert client.post("/campaigns/rewards/login", follow_redirects=False).headers["location"].endswith("rewards=opened")
    assert started == [1]
    assert client.post("/campaigns/rewards/check", follow_redirects=False).status_code == 303
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM tasks WHERE kind='rewards_check'").fetchone()["n"] == 1
    client.post("/campaigns/rewards/settings", data={"dry_run": "0"})
    client.post("/campaigns/rewards/settings", data={"auto": "0"})
    state = rewards.settings(sv.db)
    assert state["dry_run"] is False and state["auto"] is False
    rewards.set_auto(sv.db, True)
    ready(sv)
    campaign_post(sv)
    r = client.post("/campaigns/rewards/submit-waiting", follow_redirects=False)
    assert "queued" in r.headers["location"]


def test_the_defaults_are_automatic_on_and_trial_mode_on(sv):
    state = rewards.settings(sv.db)
    assert state["auto"] is True and state["dry_run"] is True and state["signed_in"] is None


def test_only_one_sign_in_window_opens_at_a_time(sv):
    gate = threading.Event()
    assert rewards.start_login(sv.db, target=lambda db: gate.wait(5)) is True
    assert rewards.start_login(sv.db, target=lambda db: None) is False
    gate.set()


def test_signing_in_waits_for_the_page_to_stop_asking_and_gives_up_after_the_wait(sv):
    class Waiting(FakeDriver):
        def __init__(self, after, **k):
            super().__init__()
            self.after, self.n = after, 0

        def goto(self, url):
            pass

        def signed_in(self, url=None):
            self.n += 1
            return self.n > self.after

    assert rewards.login(sv.db, driver_factory=lambda **k: Waiting(2), wait_s=30, poll_s=3, sleep=lambda s: None) is True
    assert rewards.settings(sv.db)["signed_in"] is True and rewards.LOGIN["state"] == "done"
    rewards.set_signed_in(sv.db, False)
    assert rewards.login(sv.db, driver_factory=lambda **k: Waiting(10 ** 6), wait_s=9, poll_s=3, sleep=lambda s: None) is False
    assert rewards.LOGIN["state"] == "error" and rewards.settings(sv.db)["signed_in"] is False


# ---- the real browser, against a page that behaves like the site -------------------------------------------------------


def _find_browser() -> str | None:
    for path in (os.environ.get("VILLOMILLO_BROWSER"), "/opt/pw-browsers/chromium"):
        if path and pathlib.Path(path).exists():
            return path
    return None


PAGE = """<!doctype html><html><body>
{header}
<h1>Campaign</h1>
{body}
<script>
const dlg = document.getElementById('dlg');
const open = document.getElementById('open');
if (open) open.onclick = () => dlg.style.display = 'block';
const input = document.getElementById('link');
const note = document.getElementById('note');
if (input) input.oninput = () => {{
  const v = input.value;
  note.textContent = v.includes('dup') ? "You've already submitted this clip." : v.includes('old') ? 'This post is too old to submit.' : v.includes('unlinked') ? 'Link your YouTube account first.' : '';
}};
const go = document.getElementById('go');
if (go) go.onclick = () => {{ dlg.style.display = 'none'; document.getElementById('toast').textContent = 'Clip submitted. It is now under review.'; fetch('/sent?u=' + encodeURIComponent(input.value)); }};
</script></body></html>"""
SIGNED_OUT = '<nav><a href="/signin" role="button">Sign in</a></nav>'
SIGNED_IN = '<nav><span>Creator menu</span></nav>'
FORM = """<button id="open">Submit clip</button><div id="toast"></div>
<div id="dlg" role="dialog" style="display:none"><input id="link" placeholder="Paste your post link"><p id="note"></p><button id="go">Submit</button><button>Cancel</button></div>"""


class Site(BaseHTTPRequestHandler):
    sent: ClassVar[list] = []
    joined = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        signed = "s=1" in (self.headers.get("Cookie") or "")
        if self.path.startswith("/signin"):
            self.send_response(302)
            self.send_header("Set-Cookie", "s=1; Path=/; Max-Age=86400")
            self.send_header("Location", "/discover")
            self.end_headers()
            return
        if self.path.startswith("/sent"):
            Site.sent.append(self.path)
            self.send_response(204)
            self.end_headers()
            return
        body = FORM if self.path.startswith("/campaign") and Site.joined else '<button>Join Campaign</button>' if self.path.startswith("/campaign") else ""
        html = PAGE.format(header=SIGNED_IN if signed else SIGNED_OUT, body=body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())


@pytest.fixture
def site():
    pytest.importorskip("playwright")
    if not _find_browser():
        pytest.skip("no browser program available for the real-browser test")
    Site.sent, Site.joined = [], True
    server = HTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def driver(tmp_path, monkeypatch, site):
    monkeypatch.setenv("VILLOMILLO_BROWSER", _find_browser())
    monkeypatch.setattr(rewards, "PROFILE_DIR", tmp_path / "profile")
    monkeypatch.setattr(rewards, "SHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(rewards, "SIGN_IN_PAGE", f"{site}/discover")
    return rewards.BrowserDriver(headless=True)


def test_the_real_browser_sees_it_is_signed_out_then_in_and_keeps_the_session(driver, site):
    with driver as d:
        assert d.signed_in() is False
        d.goto(f"{site}/signin")
        assert d.signed_in() is True
    with rewards.BrowserDriver(headless=True) as again:  # a new browser, the same saved session
        assert again.signed_in() is True


def test_the_real_browser_asks_to_sign_in_when_signed_out(driver, site):
    with driver as d:
        out = d.submit(f"{site}/campaign/1", "https://youtube.com/shorts/abc", dry_run=False)
    assert out.status == "needs_login" and pathlib.Path(out.screenshot).exists()


def test_the_real_browser_fills_the_form_and_in_trial_mode_stops_before_submit(driver, site):
    with driver as d:
        d.goto(f"{site}/signin")
        out = d.submit(f"{site}/campaign/1", "https://youtube.com/shorts/abc", dry_run=True)
    assert out.status == "dry_run" and pathlib.Path(out.screenshot).exists() and Site.sent == []


def test_the_real_browser_submits_for_real_and_reads_the_result(driver, site):
    with driver as d:
        d.goto(f"{site}/signin")
        out = d.submit(f"{site}/campaign/1", "https://youtube.com/shorts/abc", dry_run=False)
    assert out.status == "submitted" and len(Site.sent) == 1 and "abc" in Site.sent[0]


@pytest.mark.parametrize("link,status", [("https://youtube.com/shorts/dup1", "duplicate"), ("https://youtube.com/shorts/old1", "post_age"),
                                         ("https://youtube.com/shorts/unlinked1", "not_linked")])
def test_the_real_browser_reports_each_refusal_the_form_shows_and_sends_nothing(driver, site, link, status):
    with driver as d:
        d.goto(f"{site}/signin")
        out = d.submit(f"{site}/campaign/1", link, dry_run=False)
    assert out.status == status and Site.sent == []


def test_the_real_browser_says_when_the_campaign_has_not_been_joined(driver, site):
    Site.joined = False
    with driver as d:
        d.goto(f"{site}/signin")
        out = d.submit(f"{site}/campaign/1", "https://youtube.com/shorts/abc", dry_run=False)
    assert out.status == "not_joined"
