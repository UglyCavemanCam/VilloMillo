"""A model's clip reply is parsed tolerantly (analysis/highlights.py): a
malformed entry is dropped and the rest kept, whatever its shape. A model
once answered a batch of signal windows with a string among the entries,
and the AttributeError failed a whole 1h42m video."""

from analysis import highlights


def test_an_entry_that_is_not_an_object_is_dropped_not_a_crash():
    raw = '{"clips": ["10-40 a goal", {"start": 10, "end": 40, "score": 70, "hook": "h"}, 7, null, []]}'
    got = highlights._parse_clips_json(raw)
    assert [(c.start, c.end, c.score, c.hook) for c in got] == [(10.0, 40.0, 70, "h")]


def test_a_reply_of_nothing_but_strings_is_no_clips():
    assert highlights._parse_clips_json('{"clips": ["one", "two"]}') == []
