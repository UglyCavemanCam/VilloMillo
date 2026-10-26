"""Which AI finds the moments (Claude Haiku reads, Sonnet judges, or the engine's own model), its key, its cost and its spending count."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_research import FakeYT, make_sv

from villomillo import claude, clipper, engine_ai, engine_manager, health, runner, secrets, spend, tasks
from villomillo.web.app import create_app


@pytest.fixture
def sv(tmp_path, monkeypatch):
    s = make_sv(tmp_path, monkeypatch, yt=FakeYT())
    stored = {"claude": True}
    monkeypatch.setattr(secrets, "is_stored", lambda name: stored.get("claude", False) if name == secrets.CLAUDE else name == secrets.YOUTUBE)
    s.stored = stored
    return s


def test_claude_is_the_default_when_a_key_is_stored_and_the_engines_own_model_when_not(sv):
    assert engine_ai.settings(sv.db) == {"use_claude": True, "find": "claude-haiku-5-5", "judge": "claude-sonnet-5-5"}
    assert engine_ai.active(sv.db) and engine_ai.env(sv.db) == {"CLIPS_STUDIO_LLM_BACKEND": "anthropic/claude-haiku-5-5",
                                                                 "CLIPS_STUDIO_RERANK_BACKEND": "anthropic/claude-sonnet-5-5"}
    sv.stored["claude"] = False
    assert not engine_ai.active(sv.db) and engine_ai.env(sv.db) == {}  # no key: the engine keeps its own model
    sv.stored["claude"] = True
    engine_ai.save(sv.db, use_claude=False)
    assert not engine_ai.active(sv.db) and engine_ai.env(sv.db) == {}
    engine_ai.save(sv.db, use_claude=True)
    with pytest.raises(claude.ClaudeError, match="not allowed"):   # nothing above Sonnet 5.5 can be chosen
        engine_ai.save(sv.db, use_claude=True, judge="claude-opus-5-5")
    assert engine_ai.env(sv.db)["CLIPS_STUDIO_RERANK_BACKEND"] == "anthropic/claude-sonnet-5-5"


def test_the_app_never_sends_a_claude_call_to_a_model_above_sonnet():
    sent = []

    class Client:
        class messages:
            @staticmethod
            def create(**kwargs):
                sent.append(kwargs["model"])
                raise AssertionError("should not be reached for a refused model")

    assert claude.MODEL == "claude-sonnet-5-5" and claude.check_model("claude-haiku-5-5") == "claude-haiku-5-5"
    with pytest.raises(claude.ClaudeError, match="not allowed"):
        claude._create(Client(), model="claude-opus-5-5", max_tokens=10, system="s", messages=[])
    with pytest.raises(claude.ClaudeError, match="not allowed"):
        claude._create(Client(), model="claude-fable-5-1", max_tokens=10, system="s", messages=[])
    assert sent == []   # refused before anything left the app


def test_a_saved_model_above_sonnet_is_replaced_by_the_default(sv):
    with sv.db.conn() as c:   # as if an older version had saved it
        c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (engine_ai.KEY, '{"use_claude": true, "find": "claude-opus-5-5", "judge": "claude-fable-5-1"}'))
    assert engine_ai.settings(sv.db)["find"] == "claude-haiku-5-5" and engine_ai.settings(sv.db)["judge"] == "claude-sonnet-5-5"
    assert "opus" not in str(engine_ai.env(sv.db)) and "fable" not in str(engine_ai.env(sv.db))


def test_what_a_call_costs_uses_the_list_prices_and_an_unknown_model_counts_as_the_dearest():
    assert engine_ai.cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert engine_ai.cost("claude-sonnet-5-5", 100_000, 10_000) == pytest.approx(0.30)
    assert engine_ai.cost("claude-mystery-9", 1_000_000, 0) == pytest.approx(10.0)
    # a 4-hour video: about 70K tokens read and 15K written by Haiku, a tiny Sonnet judging step
    assert engine_ai.cost("claude-haiku-5-5", 70_000, 15_000) + engine_ai.cost("claude-sonnet-5-5", 4_000, 600) < 0.05


class FakeUsageEngine:
    def __init__(self, rows, last_id=None, down=False):
        self.rows, self.last, self.down, self.asked = rows, last_id, down, []

    def llm_usage(self, after=0):
        self.asked.append(after)
        if self.down:
            raise clipper.EngineUnavailable("off")
        rows = [r for r in self.rows if r["id"] > after]
        return {"rows": rows, "last_id": rows[-1]["id"] if rows else after}


def row(i, model="claude-haiku-5-5", a=100_000, b=10_000, provider="anthropic"):
    return {"id": i, "provider": provider, "model": model, "input_tokens": a, "output_tokens": b}


def test_the_engines_claude_calls_are_counted_toward_the_spending_limit_once(sv):
    before = spend.since_approval(sv.db, "claude")
    engine = FakeUsageEngine([row(1), row(2, "claude-sonnet-5-5", 10_000, 1_000), row(3, provider="openai")])
    lines = engine_ai.poll_usage(sv, engine)
    assert lines == ["Counted 2 Claude calls from the engine: $0.0400"] or lines[0].startswith("Counted 2 Claude calls")
    assert spend.since_approval(sv.db, "claude") == pytest.approx(before + 0.015 + 0.03)
    assert engine_ai.poll_usage(sv, engine) == [] and engine.asked == [0, 3]  # the second look starts after the last row seen
    with sv.db.conn() as c:
        assert c.execute("SELECT requests FROM ai_usage WHERE engine='claude'").fetchone()["requests"] == 2


def test_an_engine_that_is_off_is_skipped_without_losing_its_place(sv):
    assert engine_ai.poll_usage(sv, FakeUsageEngine([row(1)], down=True)) == []
    assert engine_ai.poll_usage(sv, FakeUsageEngine([row(1)])) != []  # nothing was marked as seen


def test_enough_claude_use_trips_the_spend_breaker_like_any_other_claude_call(sv):
    engine = FakeUsageEngine([row(1, "claude-opus-5-5", 5_000_000, 1_000_000)])  # about $40 at Opus prices
    engine_ai.poll_usage(sv, engine)
    assert spend.current(sv.db, sv.app.spend_breaker) is not None


def test_your_claude_key_is_copied_into_the_engines_key_store_once(sv, tmp_path, monkeypatch):
    from llm.providers import keys

    monkeypatch.setattr(engine_ai, "engine_data_dir", lambda: str(tmp_path / "engine"))
    monkeypatch.setattr(secrets, "get", lambda name: "sk-ant-api03-" + "Zz9" * 16)
    assert engine_ai.sync_key() is True
    assert keys.load_key(tmp_path / "engine", "anthropic") == "sk-ant-api03-" + "Zz9" * 16
    assert engine_ai.sync_key() is True  # nothing changes the second time
    sv.stored["claude"] = False
    assert engine_ai.sync_key() is False


def test_the_engine_is_started_with_the_models_in_its_environment(tmp_path, sv):
    seen = []

    class Proc:
        pid = 1

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    def popen(cmd, **kw):
        seen.append(kw["env"])
        return Proc()

    m = engine_manager.EngineManager(url="http://127.0.0.1:8765", command=["engine"], log_path=tmp_path / "e.log", popen=popen, probe=lambda: False)
    m.env_for = lambda: engine_ai.env(sv.db)
    m.enable()
    assert seen[0]["CLIPS_STUDIO_LLM_BACKEND"] == "anthropic/claude-haiku-5-5" and "PATH" in seen[0]  # the rest of the environment is kept
    engine_ai.save(sv.db, use_claude=False)
    m.restart()
    assert "CLIPS_STUDIO_LLM_BACKEND" not in seen[1]


def test_the_usage_job_is_scheduled_and_counts_what_the_engine_used(sv, monkeypatch):
    tasks.ensure_schedules(sv.db, sv.app, sv.profiles)
    with sv.db.conn() as c:
        row_ = c.execute("SELECT * FROM schedules WHERE name='usage'").fetchone()
    assert row_["kind"] == "usage" and row_["profile_id"] is None and row_["interval_s"] == 120
    monkeypatch.setattr(clipper, "Engine", lambda: FakeUsageEngine([row(1)]))
    runner.HANDLERS["usage"](sv, {"profile": None})
    with sv.db.conn() as c:
        assert c.execute("SELECT COUNT(*) AS n FROM events WHERE component='spend' AND message LIKE 'Counted 1 Claude call%'").fetchone()["n"] == 1


def test_the_health_check_says_which_ai_finds_the_moments(sv):
    by = {i["key"]: i for i in health.checks(sv)}
    assert by["moments_ai"]["state"] == "ok" and "claude-haiku-5-5 reads the video, claude-sonnet-5-5 judges" in by["moments_ai"]["detail"]
    sv.stored["claude"] = False
    by = {i["key"]: i for i in health.checks(sv)}
    assert by["moments_ai"]["state"] == "warn" and "no Claude key" in by["moments_ai"]["detail"] and "key claude" in by["moments_ai"]["fix"]
    engine_ai.save(sv.db, use_claude=False)
    by = {i["key"]: i for i in health.checks(sv)}
    assert by["moments_ai"]["state"] == "warn" and "Settings" in by["moments_ai"]["fix"]


def test_the_settings_card_explains_and_switches_it_and_restarts_the_engine(sv, monkeypatch):
    client = TestClient(create_app(sv), base_url="http://127.0.0.1:8780")
    page = client.get("/settings").text
    assert "Finding moments" in page and "Claude is doing it now" in page and "claude-haiku-5-5 reads" in page and "Use the engine's own model instead" in page
    restarted = []
    monkeypatch.setattr(engine_manager.manager, "supervised", True)
    monkeypatch.setattr(engine_manager.manager, "restart", lambda: restarted.append(1) or "Restarting the engine.")
    answer = client.post("/settings/moment-ai", data={"use_claude": "0"}, follow_redirects=True)
    assert restarted == [1] and not engine_ai.settings(sv.db)["use_claude"]
    assert "The engine's own model on this PC is doing it" in answer.text and "Restarting the engine." in answer.text
    client.post("/settings/moment-ai", data={"use_claude": "1"})
    assert engine_ai.settings(sv.db)["use_claude"] and len(restarted) == 2
    sv.stored["claude"] = False
    assert "no Claude key is stored" in client.get("/settings").text
