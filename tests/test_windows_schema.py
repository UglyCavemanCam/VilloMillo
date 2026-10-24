"""The signal windows are held to a schema that matches their prompt, which
never asks for "trending": held to the chunk schema, Gemma 4 on OpenRouter
stopped at `"trending": ` and wrote spaces to its output limit
(analysis/highlights.py)."""

from analysis import highlights
from core.models import Segment


class SchemaLLM:
    supports_schema = True

    def __init__(self):
        self.schemas = []

    def generate(self, prompt, *, json_mode=False, schema=None):
        self.schemas.append(schema)
        return '{"clips": [{"start": 10, "end": 40, "score": 70, "engagement": 60, "hook": "h", "reason": "r"}]}'


def test_windows_are_asked_for_what_their_prompt_asks_for():
    llm = SchemaLLM()
    got = highlights.score_windows([Segment(start=10, end=12, text="hi", words=[])], llm, [(10.0, 40.0)])
    schema = llm.schemas[0]["properties"]["clips"]["items"]
    assert "trending" not in schema["properties"] and "trending" not in schema["required"]
    assert "hook" in schema["required"] and got[0].score == 70
    assert "trending" in highlights.CLIPS_SCHEMA["properties"]["clips"]["items"]["required"]   # chunks keep it
