"""Handing in posted clips to Content Rewards, the way you do by hand: a browser signed in as you opens the campaign, chooses
Submit clip, pastes the video's link and sends it.

Content Rewards has no way for a program to submit a clip (no API for creators), so this drives the website with the session you
signed in with once. Nothing here ever sees your password: you sign in yourself in a real browser window, and only the browser's
own saved session is reused (a private folder inside data/villomillo/, never committed).

The page is driven by what its buttons say ("Submit clip", a link field, "Submit"), not by hidden codes, so a small redesign is
survived. When a page does not look as expected the attempt is recorded as unknown with a screenshot, and nothing is marked handed
in, so no clip is ever wrongly counted as submitted.

Settings (kept in the database's meta table): `rewards_auto` (on by default), `rewards_dry_run` (on by default: the browser goes
through the form and stops before pressing Submit, saving a screenshot, so the first run can be checked), `rewards_signed_in`.
"""

from __future__ import annotations

import os
import random
import re
import threading
from dataclasses import dataclass
from datetime import timedelta

from . import activity, campaigns, contentrewards, store
from .config import ROOT
from .db import Database

PROFILE_DIR = ROOT / "data" / "villomillo" / "rewards-browser"
SHOTS_DIR = ROOT / "data" / "villomillo" / "rewards-shots"
SIGN_IN_PAGE = f"{contentrewards.BASE}/discover"
SITE_HOST = "contentrewards.com"
SITE_PREFIX = "/c"  # the creator pages; the sign-in page (/login) is outside it
LOGIN_URL = "https://contentrewards.com/login?next=%2Fc%2Fdiscover"
GAP_S = (20, 60)  # a random pause between two submissions, so they do not arrive in a burst
RETRY_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
LOGIN_WAIT_S = 600

AUTO, DRY, SIGNED_IN, LAST = "rewards_auto", "rewards_dry_run", "rewards_signed_in", "rewards_last_submit"

# status -> (plain text, badge kind, done: the link counts as handed in)
STATUS = {
    "submitted": ("Handed in", "good", True), "duplicate": ("Already handed in", "good", True),
    "dry_run": ("Checked, not sent", "", False), "queued": ("Waiting", "", False),
    "needs_login": ("Sign in needed", "warn", False), "not_installed": ("Browser tool not installed", "warn", False),
    "not_public": ("Video is not public", "warn", False), "off": ("Automatic handing in is off", "", False),
    "needs_draft": ("A draft needs approval first", "warn", False), "post_age": ("Post is too old", "bad", False),
    "not_linked": ("Account not linked", "bad", False), "wrong_sound": ("Required sound missing", "bad", False),
    "unsupported": ("Platform not supported", "bad", False), "not_joined": ("Campaign not joined", "bad", False),
    "unknown": ("Needs a look", "warn", False), "failed": ("Failed", "bad", False),
}
RETRYABLE = ("unknown", "failed")


class RewardsError(RuntimeError):
    """The message is safe to show."""


@dataclass
class Outcome:
    status: str
    detail: str = ""
    screenshot: str | None = None


# ---- reading what the page says --------------------------------------------------------------------------------------

_RULES = (  # (status, words that mean it): checked in order against the text the form shows
    ("duplicate", r"already (been )?(submitted|added)|duplicate|you('ve| have) already"),
    ("post_age", r"too old|older than|posted (more than|over)|post age|no longer eligible|must be (posted|published) (within|in the last)"),
    ("not_linked", r"link (your|an?|the)( \w+){0,2} (account|channel)|not linked|unlinked|connect (your|an?)( \w+){0,2} (account|channel)|isn.t linked|oauth|verify (your|that you own)"),
    ("wrong_sound", r"required sound|sound requirement|must use the (required )?sound"),
    ("unsupported", r"not supported|unsupported platform|can.?t submit .* from"),
    ("needs_draft", r"pre-?approval|submit a draft|draft (must|needs)|approved draft"),
)
_SUCCESS = re.compile(r"submitted|submission (received|sent|created)|under review|pending review|thank you|success|sent for review", re.I)


def classify(text: str) -> Outcome | None:
    """What a form's or page's words say went wrong, or None when nothing in them reads as a refusal."""
    for status, pattern in _RULES:
        m = re.search(pattern, text or "", re.I)
        if m:
            return Outcome(status, _sentence(text, m.start()))
    return None


def looks_submitted(text: str) -> bool:
    return bool(_SUCCESS.search(text or ""))


def _sentence(text: str, at: int) -> str:
    start = max(text.rfind(".", 0, at), text.rfind("\n", 0, at)) + 1
    end = min([i for i in (text.find(".", at), text.find("\n", at)) if i != -1] or [len(text)])
    return " ".join(text[start:end + 1].split())[:200]


# ---- settings --------------------------------------------------------------------------------------------------------


def _get(db: Database, key: str) -> str | None:
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _put(db: Database, key: str, value: str) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def settings(db: Database) -> dict:
    signed = _get(db, SIGNED_IN)
    return {"auto": _get(db, AUTO) != "0", "dry_run": _get(db, DRY) != "0", "signed_in": None if signed is None else signed == "1",
            "installed": missing() is None, "missing": missing()}


def set_auto(db: Database, on: bool) -> None:
    _put(db, AUTO, "1" if on else "0")
    activity.log(db, "campaign", "Handing in clips to Content Rewards is " + ("on." if on else "off."))


def set_dry_run(db: Database, on: bool) -> None:
    _put(db, DRY, "1" if on else "0")
    activity.log(db, "campaign", "Handing in clips: " + ("trial mode on, the form is filled in but not sent." if on else "trial mode off, clips are sent for real."), level="warn" if not on else "info")


def set_signed_in(db: Database, value: bool) -> None:
    _put(db, SIGNED_IN, "1" if value else "0")


def missing() -> str | None:
    """Why the browser tool cannot run, or None."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "The browser tool is not installed. Close the app and start it again with run-app.bat: it installs it."
    return None


# ---- the browser -----------------------------------------------------------------------------------------------------


class BrowserDriver:
    """Drives a real browser that keeps its session in PROFILE_DIR. Use as a context manager."""

    def __init__(self, *, headless: bool = True):
        self.headless = headless
        self._pw = self._ctx = self.page = None

    def __enter__(self) -> BrowserDriver:
        if (why := missing()):
            raise RewardsError(why)
        from playwright.sync_api import sync_playwright

        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs = {"user_data_dir": str(PROFILE_DIR), "headless": self.headless, "viewport": {"width": 1280, "height": 900},
                  "ignore_default_args": ["--enable-automation"], "args": ["--disable-blink-features=AutomationControlled"]}
        exe = os.environ.get("VILLOMILLO_BROWSER")  # a browser program to use instead (Edge, Brave, a tester's Chromium)
        if exe:
            self._ctx = self._pw.chromium.launch_persistent_context(executable_path=exe, **kwargs)
        else:
            try:  # the Chrome already on this computer signs in most reliably; the browser Playwright brings is the fallback
                self._ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
            except Exception:
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self.page.set_default_timeout(20000)
        return self

    def __exit__(self, *exc) -> None:
        for closer in (lambda: self._ctx.close(), lambda: self._pw.stop()):
            try:
                closer()
            except Exception:  # closing must never hide the real result
                pass

    def goto(self, url: str) -> None:
        self.page.goto(url, wait_until="domcontentloaded")
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:  # a page that never goes quiet is still usable
            pass

    def _asks_to_sign_in(self) -> bool:
        """The page offers a way to sign in: a link to the login page, or a button saying so."""
        names = re.compile(r"^\s*(sign in|log in|login|continue with \w+|log in with \w+)\s*$", re.I)
        if self.page.locator("a[href^='/login'], a[href*='contentrewards.com/login']").count():
            return True
        return any(self.page.get_by_role(role, name=names).count() for role in ("button", "link"))

    def signed_in(self, url: str | None = None) -> bool:
        """True when the page offers no way to sign in: the session is accepted."""
        self.goto(url or SIGN_IN_PAGE)
        return not self._asks_to_sign_in()

    def seems_signed_in(self) -> bool:
        """Look at the page you are on, without opening another: True only on a Content Rewards page that offers no Sign in button.
        Used while you sign in, so the window is never pulled away from the sign-in pages (Whop's included)."""
        from urllib.parse import urlparse

        try:
            where = urlparse(self.page.url)
            host = where.hostname or ""
            if not (host == SITE_HOST or host.endswith("." + SITE_HOST)) or not where.path.startswith(SITE_PREFIX):
                return False  # not a creator page (the sign-in page, or another site's): leave it alone
            return not self._asks_to_sign_in()
        except Exception:  # the page is changing under us: look again next time
            return False

    def text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def shot(self, label: str) -> str:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        path = SHOTS_DIR / f"{store.now():%Y%m%d-%H%M%S}-{re.sub(r'[^a-z0-9]+', '-', label.lower())[:30]}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)

    def submit(self, campaign_url: str, post_url: str, *, dry_run: bool) -> Outcome:
        """Open the campaign, choose Submit clip, paste the link and (unless dry_run) send it."""
        page = self.page
        self.goto(campaign_url)
        if not self.signed_in(campaign_url):
            return Outcome("needs_login", "Content Rewards asks you to sign in.", self.shot("needs-login"))
        opener = page.get_by_role("button", name=re.compile(r"submit (a )?clip", re.I)).or_(page.get_by_role("link", name=re.compile(r"submit (a )?clip", re.I)))
        if not opener.count():
            page_text = self.text()
            if page.get_by_role("button", name=re.compile(r"join campaign|apply", re.I)).count():
                return Outcome("not_joined", "The page offers Join Campaign, not Submit clip.", self.shot("not-joined"))
            return classify(page_text) or Outcome("unknown", "There is no Submit clip button on the campaign page.", self.shot("no-button"))
        opener.first.click()
        page.wait_for_timeout(1200)
        scope = page.get_by_role("dialog")
        scope = scope if scope.count() else page
        field = scope.locator("input[type=url], input[placeholder*='link' i], input[placeholder*='url' i], input[name*='url' i], input[type=text]").first
        if not field.count():
            return Outcome("unknown", "The submit form has no place to paste a link.", self.shot("no-field"))
        field.fill(post_url)
        page.wait_for_timeout(2500)  # the page checks the link as soon as it is pasted
        shown = scope.inner_text() if scope is not page else self.text()
        if (problem := classify(shown)):
            problem.screenshot = self.shot(problem.status)
            return problem
        if dry_run:
            return Outcome("dry_run", "The form was filled in and stopped before Submit.", self.shot("dry-run"))
        send = scope.get_by_role("button", name=re.compile(r"^\s*(submit|send|confirm)\b", re.I))
        if not send.count() or send.last.is_disabled():
            return Outcome("unknown", "The Submit button is missing or cannot be pressed.", self.shot("no-submit"))
        send.last.click()
        page.wait_for_timeout(4000)
        after = self.text()
        if (problem := classify(scope.inner_text() if scope is not page and scope.count() else after) or classify(after)):
            problem.screenshot = self.shot(problem.status)
            return problem
        if looks_submitted(after) or (scope is not page and not scope.count()):
            return Outcome("submitted", "Sent.", self.shot("submitted"))
        return Outcome("unknown", "The page did not say whether the clip was accepted.", self.shot("unclear"))


# ---- signing in ------------------------------------------------------------------------------------------------------

LOGIN = {"state": "idle", "message": ""}  # idle | waiting | done | error: what the dashboard shows while the window is open
_login_lock = threading.Lock()


def login(db: Database, *, driver_factory=BrowserDriver, wait_s: int = LOGIN_WAIT_S, poll_s: float = 3.0, sleep=None) -> bool:
    """Open a visible browser on Content Rewards and wait for you to sign in. Returns whether you did."""
    import time

    sleep = sleep or time.sleep
    LOGIN.update(state="waiting", message="A browser window opened on this computer. Sign in to Content Rewards there; it closes by itself.")
    try:
        with driver_factory(headless=False) as driver:
            driver.goto(LOGIN_URL)  # straight to the sign-in page, so there is nothing to click first
            waited = 0.0
            while waited < wait_s:
                if driver.seems_signed_in() and driver.signed_in():  # look in place first; open a page only to confirm
                    set_signed_in(db, True)
                    LOGIN.update(state="done", message="Signed in.")
                    activity.log(db, "campaign", "Signed in to Content Rewards: clips can be handed in automatically.")
                    return True
                sleep(poll_s)
                waited += poll_s
        LOGIN.update(state="error", message="The sign-in window was not used for 10 minutes and was closed. Press Sign in to try again.")
    except RewardsError as e:
        LOGIN.update(state="error", message=str(e))
    except Exception as e:  # a browser that would not open must not take the app down
        LOGIN.update(state="error", message=f"The browser could not be opened: {type(e).__name__}: {str(e)[:160]}")
    return False


def start_login(db: Database, *, target=None) -> bool:
    """Run the sign-in window in the background so the dashboard stays usable. False when one is already open."""
    if not _login_lock.acquire(blocking=False):
        return False

    def run():
        try:
            (target or login)(db)
        finally:
            _login_lock.release()

    threading.Thread(target=run, name="rewards-login", daemon=True).start()
    return True


def check_signed_in(db: Database, *, driver_factory=BrowserDriver) -> bool | None:
    """Look, without a window, whether the saved session is still accepted. None when the browser tool cannot run."""
    try:
        with driver_factory(headless=True) as driver:
            ok = driver.signed_in()
    except RewardsError:
        return None
    except Exception as e:
        activity.log(db, "campaign", f"Could not check the Content Rewards sign-in: {type(e).__name__}: {str(e)[:120]}", level="warn")
        return None
    set_signed_in(db, ok)
    return ok


# ---- the queue of submissions ----------------------------------------------------------------------------------------


def record(db: Database, post_id: int, campaign_id: str, outcome: Outcome, *, attempts: int | None = None) -> None:
    done = STATUS.get(outcome.status, ("", "", False))[2]
    with db.conn() as c:
        old = c.execute("SELECT attempts FROM rewards_submissions WHERE post_id=?", (post_id,)).fetchone()
        n = attempts if attempts is not None else (old["attempts"] if old else 0)
        c.execute("INSERT INTO rewards_submissions (post_id, campaign_id, status, detail, screenshot, attempts, updated_at) VALUES (?,?,?,?,?,?,?) "
                  "ON CONFLICT(post_id) DO UPDATE SET status=excluded.status, detail=excluded.detail, screenshot=excluded.screenshot, "
                  "attempts=excluded.attempts, updated_at=excluded.updated_at",
                  (post_id, campaign_id, outcome.status, outcome.detail[:300], outcome.screenshot, n, store.iso()))
        if done:
            c.execute("INSERT OR IGNORE INTO campaign_submissions (post_id, campaign_id, submitted_at) VALUES (?,?,?)", (post_id, campaign_id, store.iso()))


def states(db: Database, limit: int = 12) -> list[dict]:
    """The latest result for each clip, newest first, as the Campaigns page lists them."""
    with db.conn() as c:
        rows = c.execute("SELECT rs.*, p.url, cm.title AS campaign_title FROM rewards_submissions rs LEFT JOIN posts p ON p.id=rs.post_id "
                         "LEFT JOIN campaigns cm ON cm.id=rs.campaign_id ORDER BY rs.updated_at DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "text": STATUS.get(r["status"], (r["status"], "", False))[0], "kind": STATUS.get(r["status"], ("", "", False))[1]} for r in rows]


def state_of(db: Database, post_id: int) -> dict | None:
    with db.conn() as c:
        row = c.execute("SELECT * FROM rewards_submissions WHERE post_id=?", (post_id,)).fetchone()
    return dict(row) if row else None


def counts(db: Database) -> dict:
    with db.conn() as c:
        rows = c.execute("SELECT status, COUNT(*) AS n FROM rewards_submissions GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def queue(db: Database, post_id: int, *, delay_s: float = 0) -> int | None:
    """Ask for this post's link to be handed in. Only a post that went out for a campaign is queued. Returns the task id."""
    from . import tasks

    with db.conn() as c:
        row = c.execute("SELECT p.id, p.status, cr.campaign_id FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id WHERE p.id=?", (post_id,)).fetchone()
    if row is None or not row["campaign_id"] or row["status"] not in ("uploaded", "posted"):
        return None
    if _get(db, AUTO) == "0":
        return None
    when = store.now() + timedelta(seconds=delay_s) if delay_s else None
    return tasks.enqueue(db, "submit_clip", {"post_id": post_id}, dedupe_key=f"submit:{post_id}:{int(store.now().timestamp() // 20)}:{int(delay_s)}", run_after=when, priority=50)


def queue_waiting(db: Database) -> int:
    """Queue every posted campaign clip whose link has not been handed in, spread out by the usual pauses. Returns how many."""
    waiting = list(campaigns.ready_to_submit(db))
    wait = 0.0
    for r in reversed(waiting):  # oldest first
        queue(db, r["post_id"], delay_s=wait)
        wait += random.uniform(*GAP_S)
    return len(waiting)


def work(sv, post_id: int, *, driver_factory=BrowserDriver, rng: random.Random | None = None, now=None) -> str:
    """Hand in one posted clip. Returns a short line for the activity log. Never raises for what the page says: that becomes a status."""
    from . import tasks

    db, rng, now = sv.db, rng or random, now or store.now()
    with db.conn() as c:
        row = c.execute("SELECT p.id, p.url, p.status, p.profile_id, cr.campaign_id FROM posts p JOIN clips_ready cr ON cr.id=p.clip_id WHERE p.id=?", (post_id,)).fetchone()
        done = c.execute("SELECT 1 FROM campaign_submissions WHERE post_id=?", (post_id,)).fetchone()
    if row is None or not row["campaign_id"]:
        return f"post {post_id}: not a campaign clip"
    if done:
        return f"post {post_id}: already handed in"
    campaign_id = row["campaign_id"]
    if row["status"] not in ("uploaded", "posted") or not row["url"]:
        return f"post {post_id}: not on YouTube yet"
    opts = settings(db)

    def stop(status: str, detail: str) -> str:
        record(db, post_id, campaign_id, Outcome(status, detail))
        return f"post {post_id}: {STATUS[status][0].lower()}. {detail}"

    if not opts["auto"]:
        return stop("off", "Turn it on in Campaigns.")
    if opts["missing"]:
        return stop("not_installed", opts["missing"])
    if opts["signed_in"] is False or opts["signed_in"] is None:
        return stop("needs_login", "Press Sign in to Content Rewards on the Campaigns page.")
    poster = sv.profiles.get(row["profile_id"])
    if poster and poster.posting and poster.posting.privacy != "public":
        return stop("not_public", f"The channel posts as {poster.posting.privacy}; Content Rewards cannot see a video that is not public.")
    last = _get(db, LAST)
    if last:  # one at a time with a random pause, so submissions do not arrive in a burst
        gap = timedelta(seconds=rng.uniform(*GAP_S))
        if now < store.parse_time(last) + gap:
            tasks.enqueue(db, "submit_clip", {"post_id": post_id}, dedupe_key=f"submit:{post_id}:{int(now.timestamp())}:wait", run_after=store.parse_time(last) + gap, priority=50)
            return f"post {post_id}: waiting a moment after the last one"
    campaign = campaigns.get(db, campaign_id)
    url = (campaign or {}).get("url") or contentrewards.campaign_url(campaign_id)
    try:
        with driver_factory(headless=True) as driver:
            outcome = driver.submit(url, row["url"], dry_run=opts["dry_run"])
    except RewardsError as e:
        outcome = Outcome("not_installed", str(e))
    except Exception as e:  # the browser died or the page broke: try again later, never lose the clip
        outcome = Outcome("failed", f"{type(e).__name__}: {str(e)[:200]}")
    attempts = (state_of(db, post_id) or {}).get("attempts", 0) + 1
    record(db, post_id, campaign_id, outcome, attempts=attempts)
    _put(db, LAST, store.iso(now))
    if outcome.status == "needs_login":
        set_signed_in(db, False)
    elif outcome.status in ("submitted", "duplicate", "dry_run"):
        set_signed_in(db, True)
    if outcome.status in RETRYABLE and attempts < MAX_ATTEMPTS:
        tasks.enqueue(db, "submit_clip", {"post_id": post_id}, dedupe_key=f"submit:{post_id}:{int(now.timestamp())}:retry", run_after=now + RETRY_AFTER, priority=50)
    return f"post {post_id}: {STATUS.get(outcome.status, (outcome.status,))[0].lower()}. {outcome.detail}".strip()


def retry_failed(db: Database) -> int:
    """After signing in (or fixing the cause), queue again every clip that was stopped for a reason that can be fixed."""
    with db.conn() as c:
        ids = [r["post_id"] for r in c.execute(
            "SELECT post_id FROM rewards_submissions WHERE status IN ('needs_login','not_installed','not_public','unknown','failed','dry_run','off','not_joined') "
            "AND post_id NOT IN (SELECT post_id FROM campaign_submissions) ORDER BY post_id")]
    wait = 0.0
    for post_id in ids:
        queue(db, post_id, delay_s=wait)
        wait += random.uniform(*GAP_S)
    return len(ids)
