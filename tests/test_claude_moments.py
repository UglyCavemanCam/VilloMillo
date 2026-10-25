"""Claude finding the moments: effort, usage counting, several chunks at once, bigger chunks, and a separate judge for the finalists."""

import json
import re
import threading
import time

import pytest
from test_llm_adapters import Response, backend, wire  # noqa: F401  (wire is a fixture)

from analysis import fusion, highlights
from core.models import Segment
from core.state import StateDB
from llm import usage
from llm.base import LLMBackend


def answer(text='{"clips": []}', usage_=None):
    body = {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}
    if usage_:
        body["usage"] = usage_
    return Response(200, body)


# ---- the Anthropic adapter ---------------------------------------------------------------------------------------------------


def test_claude_is_asked_to_think_little_and_the_usage_is_counted(wire, tmp_path):  # noqa: F811
    seen = []
    usage.set_sink(lambda *a: seen.append(a))
    try:
        calls = wire(answer(usage_={"input_tokens": 1200, "output_tokens": 300}))
        b = backend(tmp_path, "anthropic", "claude-haiku-5-5")
        b.effort = "low"
        assert b.generate("p", json_mode=True, schema={"type": "object"}) == '{"clips": []}'
    finally:
        usage.set_sink(None)
    assert calls[0]["json"]["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}, "effort": "low"}
    assert seen == [("anthropic", "claude-haiku-5-5", 1200, 300)]


def test_a_model_that_refuses_effort_is_asked_again_without_it(wire, tmp_path):  # noqa: F811
    refused = Response(400, {"error": {"type": "invalid_request_error", "message": "effort is not supported"}})
    calls = wire(refused, answer())
    b = backend(tmp_path, "anthropic", "claude-haiku-4-5")
    b.effort = "low"
    assert b.generate("p") == '{"clips": []}'
    assert "effort" in calls[0]["json"]["output_config"] and "output_config" not in calls[1]["json"]


def test_recording_never_breaks_a_call_and_does_nothing_without_a_sink():
    usage.set_sink(None)
    usage.record("anthropic", "m", {"input_tokens": 1})  # no sink: nothing happens
    usage.set_sink(lambda *a: 1 / 0)
    try:
        usage.record("anthropic", "m", {"input_tokens": 1})  # a failing sink is swallowed
        usage.record("anthropic", "m", None)
    finally:
        usage.set_sink(None)


def test_the_backend_is_built_with_low_effort_and_parallel_calls_from_the_config(tmp_path):
    from llm.registry import create_backend

    b = create_backend({"backend": "anthropic/claude-haiku-5-5", "data_dir": str(tmp_path)})
    assert b.effort == "low" and b.parallel == 4
    other = create_backend({"backend": "anthropic/claude-sonnet-5-5", "data_dir": str(tmp_path), "effort": "medium", "cloud_parallel": 8})
    assert other.effort == "medium" and other.parallel == 8
    assert create_backend({"backend": "ollama/gemma3:12b"}).parallel == 1


# ---- reading chunks ----------------------------------------------------------------------------------------------------------


def transcript(minutes):
    return [Segment(float(s), s + 9.0, f"Sentence {s} about a point worth hearing.") for s in range(0, minutes * 60, 10)]


class Counting(LLMBackend):
    """Answers each chunk with one candidate named after the chunk's first second; tracks how many calls overlap."""

    name = "test/counting"

    def __init__(self, parallel):
        self.parallel, self.live, self.peak, self.lock = parallel, 0, 0, threading.Lock()

    def generate(self, prompt, *, json_mode=False, schema=None):
        with self.lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(0.05)
        first = float(re.search(r"\[(\d+\.\d) - ", prompt).group(1))
        with self.lock:
            self.live -= 1
        return json.dumps({"clips": [{"start": first, "end": first + 30, "title": f"at {first:.0f}", "score": 80, "hook": "h", "reason": "r"}]})


def read(llm, minutes=25, **kw):
    clips, _ = highlights.find_highlights(transcript(minutes), llm, min_score=0, max_clips=999, max_overlap=1.1, max_text_similarity=1.1,
                                          max_segment_reuse=1.1, chunk_seconds=300, chunk_overlap_seconds=45, long_video_threshold_seconds=420, **kw)
    return clips


def test_a_cloud_model_reads_several_chunks_at_once_and_gets_the_same_answer_as_one_at_a_time():
    one, many = Counting(1), Counting(4)
    slow = read(one)
    fast = read(many)
    assert one.peak == 1 and many.peak > 1
    assert [round(c.start) for c in fast] == [round(c.start) for c in slow] and len(fast) >= 5


def test_a_chunk_that_fails_stops_the_read_like_before():
    class Boom(Counting):
        def generate(self, prompt, *, json_mode=False, schema=None):
            raise RuntimeError("the provider said no")

    with pytest.raises(RuntimeError, match="provider said no"):
        read(Boom(4))
    with pytest.raises(RuntimeError, match="provider said no"):
        read(Boom(1))


# ---- chunk size and the judge --------------------------------------------------------------------------------------------------


def test_a_cloud_model_gets_big_chunks_and_a_local_one_keeps_the_small_ones(monkeypatch):
    sizes = []
    monkeypatch.setattr(fusion.highlights, "find_highlights", lambda segments, llm, **kw: (sizes.append((kw["chunk_seconds"], kw["chunk_overlap_seconds"])) or [], []))
    base = {"clips": {"min_duration": 10, "max_duration": 60, "min_score": 55, "max_clips_per_video": 0},
            "analysis": {"chunk_seconds": 300, "chunk_overlap_seconds": 45, "long_video_threshold_seconds": 420}}
    for backend_spec in ("ollama/gemma3:12b", "anthropic/claude-haiku-5-5"):
        try:
            fusion.find_clips(__import__("pathlib").Path("x.mp4"), transcript(5), Counting(1), {**base, "llm": {"backend": backend_spec}},
                              signals=({}, {}), measure_reaction=False)
        except Exception:
            pass  # only the chunk arguments matter here
    assert sizes[:2] == [(300, 45), (1200, 60)]


def test_the_finalists_are_judged_by_the_rerank_model_when_one_is_set(tmp_path):
    main = Counting(1)
    assert fusion._rerank_backend({"llm": {"backend": "anthropic/claude-haiku-5-5"}}, main) is main  # none set: the same model
    assert fusion._rerank_backend({"llm": {"backend": "x/y", "rerank_backend": "x/y"}}, main) is main
    judge = fusion._rerank_backend({"llm": {"backend": "anthropic/claude-haiku-5-5", "rerank_backend": "anthropic/claude-sonnet-5-5", "data_dir": str(tmp_path)}}, main)
    assert judge.name == "anthropic/claude-sonnet-5-5" and judge.effort == "medium"
    broken = fusion._rerank_backend({"llm": {"backend": "a/b", "rerank_backend": "nobody/model"}}, main)
    assert broken is main  # a backend that cannot be made falls back to the main model


# ---- the engine's records ------------------------------------------------------------------------------------------------------


def test_usage_rows_are_stored_and_read_back_in_order(tmp_path):
    db = StateDB(tmp_path / "state.db")
    db.add_llm_usage("anthropic", "claude-haiku-5-5", 100, 10)
    db.add_llm_usage("anthropic", "claude-sonnet-5-5", 200, 20)
    rows = db.llm_usage_after(0)
    assert [(r["model"], r["input_tokens"]) for r in rows] == [("claude-haiku-5-5", 100), ("claude-sonnet-5-5", 200)]
    assert [r["id"] for r in db.llm_usage_after(rows[0]["id"])] == [rows[1]["id"]]
    db.close()


def test_the_engine_serves_usage_and_counts_what_a_cloud_answer_used(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import main as engine_main
    from server.api import create_app

    config = engine_main.load_config(engine_main.CONFIG_PATH)
    config["paths"]["data_dir"] = str(tmp_path)
    app_ = create_app(config, engine_main.CONFIG_PATH)
    try:
        client = TestClient(app_, base_url="http://127.0.0.1:8765")
        usage.record("anthropic", "claude-haiku-5-5", {"input_tokens": 500, "output_tokens": 50})
        body = client.get("/usage/llm?after=0").json()
        assert [(r["provider"], r["model"], r["input_tokens"], r["output_tokens"]) for r in body["rows"]] == [("anthropic", "claude-haiku-5-5", 500, 50)]
        assert client.get(f"/usage/llm?after={body['last_id']}").json() == {"rows": [], "last_id": body["last_id"]}
    finally:
        usage.set_sink(None)


def test_the_engine_reads_the_model_overrides_from_the_environment(monkeypatch):
    import main as engine_main

    monkeypatch.setenv("CLIPS_STUDIO_LLM_BACKEND", "anthropic/claude-haiku-5-5")
    monkeypatch.setenv("CLIPS_STUDIO_RERANK_BACKEND", "anthropic/claude-sonnet-5-5")
    config = engine_main.load_config(engine_main.CONFIG_PATH)
    assert config["llm"]["backend"] == "anthropic/claude-haiku-5-5" and config["llm"]["rerank_backend"] == "anthropic/claude-sonnet-5-5"
    monkeypatch.delenv("CLIPS_STUDIO_LLM_BACKEND")
    monkeypatch.delenv("CLIPS_STUDIO_RERANK_BACKEND")
    plain = engine_main.load_config(engine_main.CONFIG_PATH)
    assert plain["llm"]["backend"].startswith("ollama/") and "rerank_backend" not in plain["llm"]
