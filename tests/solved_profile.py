"""The Podcasts channel as it was set up for the Solved campaign: two posts a day, three hours apart, with the
campaign's required tags. Several tests exercise those rules, so they build the channel this way on purpose; the
shipped starting channel no longer has them."""

from __future__ import annotations

from villomillo import config


def solved() -> config.Profile:
    shipped = config.load_profiles()["solved"]
    old = config.Posting(
        dry_run=True, platforms={"youtube_shorts": "manual"}, privacy="private", min_gap_minutes=180, daily_cap=2,
        topics={"youtube_shorts": ["values", "purpose", "meaning", "growth", "discipline", "life", "self", "hard truth"]},
        tags={"youtube_shorts": "@solvedclips @iammarkmanson"})
    return shipped.model_copy(update={"posting": old})


def profiles() -> dict[str, config.Profile]:
    return {"solved": solved()}
