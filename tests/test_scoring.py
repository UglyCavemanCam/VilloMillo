"""The scoring brain: viral score, estimated CPM, expected revenue, and what kind of video it is. No network."""

from __future__ import annotations

import pytest

from villomillo import scoring, spend, store
from villomillo.config import Scoring, SpendBreaker
from villomillo.db import Database
from villomillo.jev import JevResult

RULES = Scoring()
TEXT = "Most people think discipline is about willpower. It is not. It is about designing your day so you never need it."


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "scoring.db")


class FakeJev:
    """Answers by question name; counts how often it is asked."""

    def __init__(self, hook=3.0, payoff=3.0, alone=0.9, kinds=None):
        self.calls = 0
        self.hook, self.payoff, self.alone = hook, payoff, alone
        self.kinds = kinds or {"podcast": 0.9, "stream": 0.05, "talk": 0.05}

    def ask(self, state, questions):
        self.calls += 1
        answers = {}
        for name in questions:
            if name == "hook":
                answers[name] = {"type": "score", "score": self.hook}
            elif name == "payoff":
                answers[name] = {"type": "score", "score": self.payoff}
            elif name == "stands_alone":
                answers[name] = {"type": "noul", "noul": self.alone}
            else:
                answers[name] = {"type": "noul", "noul": self.kinds[name]}
        return JevResult("jev-test", answers, {"input_tokens": 1000})


def moment(i=1, score=88, text=TEXT) -> dict:
    return {"id": i, "start": 100.0 * i, "end": 100.0 * i + 30, "score": score, "hook": f"hook {i}", "text": text}


PODCAST = scoring.KindReading("podcast", "podcast or interview", "Podcasts", 0.9, "podcast_talk", False, "x")


# ---- the formula -----------------------------------------------------------------------------------------------


def test_the_plan_example_a_viral_91_clip_is_about_one_dollar_twenty_one_and_a_68_is_not_made():
    good = scoring.score_moment(RULES, moment(score=91), None, PODCAST)
    assert good.viral == 91 and good.cpm == 1.21 and good.revenue == 9.68 and good.make
    bad = scoring.score_moment(RULES, moment(score=68), None, PODCAST)
    assert bad.cpm == 0.91 and not bad.make


def test_a_clip_scoring_exactly_the_minimum_is_made():
    m = scoring.score_moment(RULES, moment(score=75), None, PODCAST)
    assert m.cpm == 1.0 and m.make


def test_jevs_reading_is_blended_with_the_engines_rating():
    reading = scoring.Reading(hook=4, payoff=4, stands_alone=1.0)  # a perfect 100
    assert reading.score == 100
    viral, _ = scoring.viral_score(RULES, 80, reading)
    assert viral == round(0.6 * 80 + 0.4 * 100) == 88
    assert scoring.viral_score(RULES, 80, scoring.Reading(0, 0, 0.0))[0] == 48


def test_every_number_has_a_sentence_that_names_its_parts():
    m = scoring.score_moment(RULES, moment(score=88), scoring.Reading(3, 3, 0.9), PODCAST)
    viral, cpm, revenue, verdict = m.explain
    assert "engine rated the moment 88" in viral and "reading of its words was" in viral and "60% engine" in viral
    assert "$1.00 x the Podcasts factor 1 x (" in cpm and "estimate" in cpm
    assert "8,000 predicted views" in revenue and f"${m.revenue:.2f}" in revenue
    assert verdict.startswith("Will be made")


def test_a_skipped_clip_says_exactly_why():
    m = scoring.score_moment(RULES, moment(score=60), None, PODCAST)
    assert m.explain[-1] == "Will not be made: the viral score 60 is under 75 and the CPM $0.80 is under the $1.00 floor."


def test_the_way_a_video_arrived_changes_the_floor():
    m = moment(score=80)
    assert scoring.score_moment(RULES, m, None, PODCAST, source="research", research_bar=1.10).make is False  # CPM 1.07
    assert scoring.score_moment(RULES, m, None, PODCAST, source="research", research_bar=1.05).make is True
    assert scoring.score_moment(RULES, m, None, PODCAST, source="research", research_bar=0.5).make is True  # never below $1.00
    paid = scoring.score_moment(RULES, m, None, PODCAST, source="campaign", campaign_cpm=1.80)
    assert paid.cpm == 1.8 and not paid.cpm_estimated and paid.make and "campaign's stated pay" in paid.explain[1]
    assert not scoring.score_moment(RULES, m, None, PODCAST, source="campaign", campaign_cpm=0.80).make
    with pytest.raises(ValueError):
        scoring.score_moment(RULES, m, None, PODCAST, source="friend")


def test_category_factors_scale_the_estimate_and_unknown_categories_use_the_default():
    rules = Scoring(category_factors={"Podcasts": 1.5}, default_factor=0.5)
    assert scoring.estimate_cpm(rules, "podcasts", 75)[0] == 1.5
    assert scoring.estimate_cpm(rules, "Cooking", 75)[0] == 0.5


# ---- Jev's readings are kept ---------------------------------------------------------------------------------------


def test_the_same_text_is_read_once_and_always_scores_the_same(db):
    jev = FakeJev()
    first = scoring.read_clip(db, jev, TEXT)
    again = scoring.read_clip(db, FakeJev(hook=0, payoff=0, alone=0), TEXT)  # a different Jev would disagree, but is never asked
    assert first == again and jev.calls == 1
    assert scoring.read_clip(db, jev, TEXT + " More.") is not None and jev.calls == 2


def test_a_clip_with_no_words_is_not_read_and_nothing_is_paid(db):
    jev = FakeJev()
    assert scoring.read_clip(db, jev, "  ") is None and scoring.read_clip(db, jev, "too short") is None
    assert jev.calls == 0


def test_a_tripped_spend_breaker_stops_a_new_reading_but_not_one_already_kept(db):
    kept = scoring.read_clip(db, FakeJev(), TEXT)
    with db.conn() as c:
        store.add_ai_usage(c, "jev", 0, 0, 6.0)
    jev = FakeJev()
    with pytest.raises(spend.SpendPaused):
        scoring.read_clip(db, jev, TEXT + " A different clip.", breaker=SpendBreaker())
    assert jev.calls == 0
    assert scoring.read_clip(db, jev, TEXT, breaker=SpendBreaker()) == kept and jev.calls == 0


def test_a_reading_is_paid_for_in_the_spend_log(db):
    scoring.read_clip(db, FakeJev(), TEXT)
    assert spend.since_approval(db, "jev") > 0


# ---- what kind of video it is -------------------------------------------------------------------------------------


def kind(db, **weights):
    jev = FakeJev(kinds={"podcast": 0.0, "stream": 0.0, "talk": 0.0, **weights})
    return scoring.detect_kind(db, jev, RULES, title="A talk", channel="Chan", excerpts=[TEXT])


def test_a_clear_podcast_is_a_podcast_with_its_look(db):
    k = kind(db, podcast=0.9, stream=0.05, talk=0.05)
    assert (k.kind, k.category, k.look, k.unsure) == ("podcast", "Podcasts", "podcast_talk", False)
    assert k.certainty == 0.9 and "90%" in k.explain and "podcast_talk" in k.explain


def test_two_plausible_kinds_make_it_unsure_and_the_plain_look_is_used(db):
    k = kind(db, podcast=0.7, talk=0.6)
    assert k.kind == "podcast" and k.certainty == pytest.approx(0.5385, abs=1e-4)
    assert k.unsure and k.look == "generic_safe" and "under the 75%" in k.explain
    assert k.category == "Podcasts"  # the best guess still decides the category


def test_a_stream_is_a_stream_clips_video(db):
    k = kind(db, stream=0.95)
    assert (k.category, k.look) == ("Stream clips", "generic_safe") and not k.unsure


def test_a_video_matching_nothing_is_other_and_gets_the_plain_look(db):
    k = kind(db, podcast=0.2, stream=0.1, talk=0.1)
    assert (k.kind, k.category, k.look) == ("other", "Other", "generic_safe") and "none of the known kinds" in k.explain


# ---- a whole video ----------------------------------------------------------------------------------------------


PREVIEW = {"video": {"title": "Episode 12", "channel": "A Show", "duration": 3600},
           "moments": [moment(1, 60, TEXT + " One."), moment(2, 91, TEXT + " Two."), moment(3, 88, "")]}


def test_a_video_is_scored_best_expected_revenue_first_and_is_repeatable(db):
    jev = FakeJev()
    first = scoring.score_video(db, jev, RULES, PREVIEW)
    calls = jev.calls
    assert calls == 3  # the kind, and two clips that have words
    second = scoring.score_video(db, FakeJev(hook=0), RULES, PREVIEW)  # never asked: everything is kept
    assert [(m.id, m.viral, m.cpm, m.revenue) for m in first.moments] == [(m.id, m.viral, m.cpm, m.revenue) for m in second.moments]
    assert [m.id for m in first.moments] == [3, 2, 1]  # 3 has no words, so its 88 stands; 2 blends 91 with a reading of 80
    assert [m.make for m in first.moments] == [True, True, False]
    assert first.kind.kind == "podcast" and len(first.makeable) == 2


def test_a_video_with_no_moments_scores_nothing(db):
    assert scoring.score_video(db, FakeJev(), RULES, {"video": None, "moments": []}).moments == []
