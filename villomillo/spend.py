"""The spend breaker. Work pauses when AI spending since your last approval reaches a limit, or when an account is out of funds.

There are no monthly or daily dollar caps. Instead the breaker counts what Claude and Jev have cost since you last pressed
Approve, and stops everything at the limit (default $25 Claude, $5 Jev). Approving starts a new count. A provider answer that
means "no balance left" also pauses, and names the account to top up; approving clears it once you have topped up.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import SpendBreaker
from .db import Database
from .store import iso

APPROVED_AT = "spend_approved_at"
APPROVED_ID = "spend_approved_id"  # the last spend_log row that was already counted when you approved
FUNDS_OUT = "funds_out:"  # followed by the engine name ("claude" or "jev")
ENGINES = ("claude", "jev")
NAMES = {"claude": "Claude", "jev": "Jev"}
NO_FUNDS_WORDS = ("credit balance", "insufficient", "out of credit", "payment required", "billing", "no balance",
                  "quota exceeded", "exceeded your current quota")


class SpendPaused(RuntimeError):
    """Raised before an AI call when the breaker is tripped or an account is out of funds."""


@dataclass(frozen=True)
class Pause:
    kind: str  # "breaker" | "funds"
    engine: str
    spent: float
    limit: float

    @property
    def title(self) -> str:
        name = NAMES[self.engine]
        return f"Paused: {name} is out of funds" if self.kind == "funds" else "Paused: spending reached the breaker"

    @property
    def text(self) -> str:
        name = NAMES[self.engine]
        if self.kind == "funds":
            return f"{name} said the account has no balance left. Top it up, then approve to continue."
        return (f"{name} has spent ${self.spent:.2f} since your last approval (limit ${self.limit:.2f}). "
                "Nothing new is being made or posted until you approve.")


def _meta_get(db: Database, key: str) -> str | None:
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(db: Database, key: str, value: str) -> None:
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, value))


def since_approval(db: Database, engine: str) -> float:
    """What this engine has cost since the last approval (or since the start, if you have never approved)."""
    after = int(_meta_get(db, APPROVED_ID) or 0)
    with db.conn() as c:
        row = c.execute("SELECT COALESCE(SUM(cost_usd), 0) AS total FROM spend_log WHERE engine=? AND id>?",
                        (engine, after)).fetchone()
    return float(row["total"])


def current(db: Database, breaker: SpendBreaker) -> Pause | None:
    """Why work is paused right now, or None when it may continue. A missing balance is reported before the breaker."""
    limits = {"claude": breaker.claude_usd, "jev": breaker.jev_usd}
    for engine in ENGINES:
        if _meta_get(db, FUNDS_OUT + engine):
            return Pause("funds", engine, since_approval(db, engine), limits[engine])
    for engine in ENGINES:
        spent = since_approval(db, engine)
        if spent >= limits[engine]:
            return Pause("breaker", engine, spent, limits[engine])
    return None


def guard(db: Database, breaker: SpendBreaker | None = None) -> None:
    """Call before any paid AI request. Raises SpendPaused when work must wait for you."""
    pause = current(db, breaker or SpendBreaker())
    if pause:
        raise SpendPaused(pause.text)


def approve(db: Database) -> None:
    """Start a new count from now and clear any 'out of funds' mark (you have topped up)."""
    with db.conn() as c:
        last = c.execute("SELECT COALESCE(MAX(id), 0) AS n FROM spend_log").fetchone()["n"]
    _meta_set(db, APPROVED_ID, str(last))
    _meta_set(db, APPROVED_AT, iso())
    with db.conn() as c:
        c.execute("DELETE FROM meta WHERE key LIKE ?", (FUNDS_OUT + "%",))


def note_failure(db: Database, engine: str, error: BaseException | str) -> bool:
    """If this provider error means the account has no balance, pause on that account. Returns True when it did."""
    text = str(error).lower()
    if engine not in ENGINES or not (any(w in text for w in NO_FUNDS_WORDS) or "http 402" in text):
        return False
    _meta_set(db, FUNDS_OUT + engine, iso())
    return True


def engine_of(error: BaseException) -> str | None:
    """Which provider an error came from, judged by its type; None when it is neither."""
    from .jev import JevError

    if isinstance(error, JevError):
        return "jev"
    module = type(error).__module__ or ""
    return "claude" if module.startswith("anthropic") else None
