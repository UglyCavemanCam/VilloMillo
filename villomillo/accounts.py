"""Channel accounts: which route a platform really uses, and the safety stops that pause an account.

Two switches, both per channel and platform:

- The audit. A platform's API only posts for real once its audit has passed (an unaudited YouTube project's uploads
  are locked private). Until you record that it passed, posts for that platform stay manual. You record it by hand.
- The account. An account is paused by itself when posting keeps failing, when its sign-in stops working, or when
  the platform says something that looks like a strike or a suspension. A paused account sends nothing; its posts
  wait, and the reason is shown. Only you resume it.
"""

from __future__ import annotations

import re
from datetime import datetime

from .config import Profile
from .db import Database
from .store import iso, now

MAX_FAILURES = 3          # in a row, for ordinary errors
POLICY = re.compile(r"suspend|terminat|banned|account disabled|channel disabled|strike|community guidelines|"
                    r"policy violation", re.I)
AUTH = re.compile(r"sign-in|signed in|expired|invalid_grant|unauthori[sz]ed|not connected|no youtube account|reconnect|access token|"
                  r"permission", re.I)
QUOTA = re.compile(r"quota|rate limit|too many|limit exceeded|daily posting limit", re.I)


# --- audits and the route a platform really uses ---------------------------------------------------------------

def set_audit(db: Database, profile_id: str, platform: str, passed: bool, note: str = "") -> None:
    with db.conn() as c:
        c.execute("INSERT INTO platform_audits (profile_id, platform, passed, note, set_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(profile_id, platform) DO UPDATE SET passed=excluded.passed, note=excluded.note, "
                  "set_at=excluded.set_at", (profile_id, platform, int(passed), note, iso()))


def audit_passed(db: Database, profile_id: str, platform: str) -> bool:
    with db.conn() as c:
        row = c.execute("SELECT passed FROM platform_audits WHERE profile_id=? AND platform=?",
                        (profile_id, platform)).fetchone()
    return bool(row and row["passed"])


def effective_route(db: Database, profile: Profile, platform: str) -> tuple[str, str]:
    """(route, why). The route is 'api' or 'manual'. The API is only used for real once the audit has passed;
    a dry run may rehearse it earlier because nothing is sent."""
    posting = profile.posting
    if posting is None or posting.platforms.get(platform, "manual") != "api":
        return "manual", "set to manual in the channel's settings"
    if posting.dry_run:
        return "api", "dry run: the API path is rehearsed and nothing is sent"
    if audit_passed(db, profile.id, platform):
        return "api", "the platform's audit has passed"
    return "manual", "the platform's audit has not passed yet"


# --- account state and the safety stops ------------------------------------------------------------------------

def state(db: Database, profile_id: str, platform: str) -> dict:
    with db.conn() as c:
        row = c.execute("SELECT status, reason, failures FROM account_state WHERE profile_id=? AND platform=?",
                        (profile_id, platform)).fetchone()
    return dict(row) if row else {"status": "active", "reason": None, "failures": 0}


def is_paused(db: Database, profile_id: str, platform: str) -> bool:
    return state(db, profile_id, platform)["status"] == "paused"


def _write(db: Database, profile_id: str, platform: str, status: str, reason: str | None, failures: int) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO account_state (profile_id, platform, status, reason, failures, changed_at) "
                  "VALUES (?,?,?,?,?,?) ON CONFLICT(profile_id, platform) DO UPDATE SET status=excluded.status, "
                  "reason=excluded.reason, failures=excluded.failures, changed_at=excluded.changed_at",
                  (profile_id, platform, status, reason, failures, iso()))


def pause(db: Database, profile_id: str, platform: str, reason: str) -> None:
    _write(db, profile_id, platform, "paused", reason[:300], state(db, profile_id, platform)["failures"])


def resume(db: Database, profile_id: str, platform: str) -> None:
    _write(db, profile_id, platform, "active", None, 0)


def classify_failure(error: str) -> str:
    """policy (a strike or suspension), auth (sign-in broke), quota (try again later) or other."""
    if QUOTA.search(error) and not POLICY.search(error):
        return "quota"
    if POLICY.search(error):
        return "policy"
    if AUTH.search(error):
        return "auth"
    return "other"


def record_success(db: Database, profile_id: str, platform: str) -> None:
    current = state(db, profile_id, platform)
    if current["failures"]:
        _write(db, profile_id, platform, current["status"], current["reason"], 0)


def record_failure(db: Database, profile_id: str, platform: str, error: str) -> str | None:
    """Count a failed post. Returns the reason if this failure paused the account."""
    kind = classify_failure(error)
    current = state(db, profile_id, platform)
    if kind == "quota":
        return None  # nothing wrong with the account: the platform's daily allowance ran out
    failures = current["failures"] + 1
    reason = None
    if kind == "policy":
        reason = f"the platform's reply looks like a strike or suspension: {error[:160]}"
    elif kind == "auth":
        reason = f"the sign-in no longer works; reconnect the account: {error[:160]}"
    elif failures >= MAX_FAILURES:
        reason = f"{failures} posts in a row failed; the last said: {error[:160]}"
    _write(db, profile_id, platform, "paused" if reason else current["status"], reason or current["reason"], failures)
    return reason


def views_dropped(recent: list[int], baseline: list[int], *, factor: float = 0.3, min_posts: int = 5) -> bool:
    """True when the latest posts get far fewer views than the account's usual: the median of the last
    `min_posts` falls below `factor` of the median of the baseline. Needs real view counts (Phase 6 collects them)."""
    if len(recent) < min_posts or len(baseline) < min_posts:
        return False
    ordered = sorted(baseline)
    usual = ordered[len(ordered) // 2]
    latest = sorted(recent[-min_posts:])[min_posts // 2]
    return usual > 0 and latest < usual * factor


# --- the overview ----------------------------------------------------------------------------------------------

def overview(db: Database, profiles: dict[str, Profile], at: datetime | None = None) -> list[dict]:
    """One row per channel and platform, for a channel's page and the command line."""
    rows = []
    for profile in profiles.values():
        if profile.posting is None:
            continue
        for platform, configured in profile.posting.platforms.items():
            route, why = effective_route(db, profile, platform)
            current = state(db, profile.id, platform)
            with db.conn() as c:
                waiting = c.execute("SELECT COUNT(*) AS n FROM posts WHERE profile_id=? AND platform=? AND status IN "
                                    "('queued','manual_ready')", (profile.id, platform)).fetchone()["n"]
            rows.append({"profile": profile.id, "name": profile.display_name, "platform": platform,
                         "configured": configured, "route": route, "why": why,
                         "audit": audit_passed(db, profile.id, platform), "status": current["status"],
                         "reason": current["reason"], "failures": current["failures"], "waiting": waiting,
                         "dry_run": profile.posting.dry_run, "at": (at or now()).isoformat(timespec="seconds")})
    return rows
