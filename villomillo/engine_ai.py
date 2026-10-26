"""Which AI finds the moments in a video, and what it costs.

The clipping engine reads a video's transcript with an AI model to find the moments worth clipping, and by default that is a free
local model on this PC (slow: minutes per chunk, hours for a long video). With the switch on, Claude does it: Haiku reads the
chunks (fast and cheap) and Sonnet orders the finalists (better judgement, a tiny input). The engine is started with the model
names in its environment, so its settings file is untouched and running it by hand still uses the local model.

Your Claude key is copied into the engine's own key store (the engine keeps its keys separately), and what the engine's Claude
calls use is read back from the engine and counted toward the spending limit, like VilloMillo's own Claude calls.
"""

from __future__ import annotations

import json

from . import claude, secrets, store
from .db import Database

KEY = "engine_ai"
LAST_ID_KEY = "engine_usage_id"
FIND_MODEL = "claude-haiku-5-5"
JUDGE_MODEL = "claude-sonnet-5-5"
# Anthropic's list prices in dollars per million tokens (input, output). A model not listed is priced like the dearest, so the
# spending limit errs on the safe side. (Haiku costs more above 100,000 tokens in one prompt; chunks are far smaller than that.)
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5": (2.00, 10.00),
          "claude-opus-5": (5.00, 25.00), "claude-haiku-4-5": (1.00, 5.00), "claude-sonnet-4-6": (3.00, 15.00)}
DEAREST = (10.00, 50.00)


def _allowed(saved: dict, name: str, default: str) -> str:
    """The saved model if the app may use it, else the default: a model above Sonnet 5.5 never reaches the engine."""
    value = saved.get(name, default)
    return value if value in claude.ALLOWED_MODELS else default


def settings(db: Database) -> dict:
    with db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    saved = json.loads(row["value"]) if row else {}
    return {"use_claude": bool(saved.get("use_claude", True)), "find": _allowed(saved, "find", FIND_MODEL),
            "judge": _allowed(saved, "judge", JUDGE_MODEL)}


def save(db: Database, *, use_claude: bool | None = None, find: str | None = None, judge: str | None = None) -> None:
    now = settings(db)
    if use_claude is not None:
        now["use_claude"] = bool(use_claude)
    if find:
        now["find"] = claude.check_model(find)   # refused above Sonnet 5.5
    if judge:
        now["judge"] = claude.check_model(judge)
    with db.conn() as c:
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (KEY, json.dumps(now)))
    store.event(db, "info", "engine", "Claude finds the moments" if now["use_claude"] else "The engine's own model finds the moments")


def key_stored() -> bool:
    try:
        return secrets.is_stored(secrets.CLAUDE)
    except Exception:  # a locked or missing keychain
        return False


def active(db: Database) -> bool:
    """Whether the engine will use Claude: you want it and a Claude key is stored."""
    return settings(db)["use_claude"] and key_stored()


def env(db: Database) -> dict[str, str]:
    """What to add to the engine's environment: nothing when the engine uses its own model."""
    if not active(db):
        return {}
    s = settings(db)
    return {"CLIPS_STUDIO_LLM_BACKEND": f"anthropic/{s['find']}", "CLIPS_STUDIO_RERANK_BACKEND": f"anthropic/{s['judge']}"}


def engine_data_dir():
    """Where the engine keeps its data, exactly as the engine works it out."""
    import main as engine_main

    return engine_main.load_config(engine_main.CONFIG_PATH)["paths"]["data_dir"]


def sync_key() -> bool:
    """Copy the Claude key into the engine's key store so the engine can use it. True when the engine has a key afterwards."""
    from llm.providers import keys

    if not key_stored():
        return False
    data_dir = engine_data_dir()
    mine = secrets.get(secrets.CLAUDE)
    if keys.load_key(data_dir, "anthropic") != mine:
        keys.save_key(data_dir, "anthropic", mine)
    return True


def cost(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = PRICES.get(model, DEAREST)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def poll_usage(sv, engine=None) -> list[str]:
    """Read what the engine's cloud calls used since the last look and count it toward the Claude spending limit."""
    from . import clipper

    engine = engine or clipper.Engine()
    with sv.db.conn() as c:
        row = c.execute("SELECT value FROM meta WHERE key=?", (LAST_ID_KEY,)).fetchone()
    after = int(row["value"]) if row else 0
    try:
        data = engine.llm_usage(after)
    except clipper.EngineUnavailable:
        return []
    rows = [r for r in data.get("rows", []) if r.get("provider") == "anthropic"]
    spent = 0.0
    with sv.db.conn() as c:
        for r in rows:
            dollars = cost(r["model"], r["input_tokens"], r["output_tokens"])
            store.add_ai_usage(c, "claude", r["input_tokens"], r["output_tokens"], dollars)
            spent += dollars
        c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (LAST_ID_KEY, str(data.get("last_id", after))))
    return [f"Counted {len(rows)} Claude call{'s' if len(rows) != 1 else ''} from the engine: ${spent:.4f}"] if rows else []
