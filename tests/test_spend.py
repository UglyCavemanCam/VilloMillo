"""The spend breaker: pause at the limits since the last approval, pause when an account is out of funds, approve to go on."""

from __future__ import annotations

import pytest

from villomillo import runner, spend, store, tasks
from villomillo.config import SpendBreaker
from villomillo.db import Database
from villomillo.jev import JevError

BREAKER = SpendBreaker()


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "spend.db")


def spent(db, engine, usd):
    with db.conn() as c:
        store.add_ai_usage(c, engine, 0, 0, usd)


def test_the_defaults_are_25_dollars_claude_and_5_dollars_jev():
    assert (BREAKER.claude_usd, BREAKER.jev_usd) == (25.0, 5.0)


def test_nothing_is_paused_below_the_limits(db):
    spent(db, "claude", 24.99)
    spent(db, "jev", 4.99)
    assert spend.current(db, BREAKER) is None
    spend.guard(db, BREAKER)  # does not raise


def test_reaching_either_limit_pauses_and_names_the_account(db):
    spent(db, "jev", 5.0)
    pause = spend.current(db, BREAKER)
    assert pause and pause.kind == "breaker" and pause.engine == "jev"
    assert "Jev" in pause.text and "$5.00" in pause.text
    with pytest.raises(spend.SpendPaused):
        spend.guard(db, BREAKER)


def test_approving_starts_a_new_count_and_old_spend_no_longer_counts(db):
    spent(db, "claude", 26.0)
    assert spend.current(db, BREAKER)
    spend.approve(db)
    assert spend.current(db, BREAKER) is None and spend.since_approval(db, "claude") == 0.0
    spent(db, "claude", 3.0)
    assert spend.since_approval(db, "claude") == pytest.approx(3.0)


def test_a_no_balance_answer_pauses_until_you_approve(db):
    assert spend.note_failure(db, "claude", "Your credit balance is too low to access the API") is True
    pause = spend.current(db, BREAKER)
    assert pause and pause.kind == "funds" and pause.engine == "claude" and "Top it up" in pause.text
    spend.approve(db)
    assert spend.current(db, BREAKER) is None


def test_other_errors_do_not_pause(db):
    assert spend.note_failure(db, "jev", "HTTP 500 server error") is False
    assert spend.note_failure(db, "jev", JevError("HTTP 402 payment required")) is True
    assert spend.note_failure(db, "other", "credit balance") is False


def test_errors_are_attributed_to_their_provider():
    assert spend.engine_of(JevError("x")) == "jev"
    assert spend.engine_of(ValueError("x")) is None


def test_the_worker_claims_nothing_while_paused(db):
    class Sv:
        pass

    from villomillo.config import AppConfig

    sv = Sv()
    sv.db, sv.app = db, AppConfig()
    tasks.enqueue(db, "watch", {"profile": "x"}, dedupe_key="k")
    spent(db, "claude", 30.0)
    assert runner.run_one(sv) is False
    with db.conn() as c:
        assert c.execute("SELECT status FROM tasks").fetchone()["status"] == "queued"
