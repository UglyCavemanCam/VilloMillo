"""The pieces everything else needs, built once from the settings files and stored keys."""

from __future__ import annotations

from dataclasses import dataclass

from . import channels, secrets
from .config import AppConfig, Profile, load_app
from .db import Database
from .jev import Jev
from .quota import Quota
from .twitch import Twitch
from .youtube import YouTube


@dataclass
class Services:
    app: AppConfig
    profiles: dict[str, Profile]
    db: Database
    quota: Quota
    yt: YouTube | None = None
    jev: Jev | None = None
    twitch: Twitch | None = None


def build(*, with_apis: bool = True) -> Services:
    app = load_app()
    db = Database(app.database_path())
    profiles = channels.load(db)
    quota = Quota(
        db,
        general_cap=app.budgets.youtube_general_units_per_day,
        search_cap=app.budgets.youtube_search_calls_per_day,
    )
    services = Services(app, profiles, db, quota)
    if with_apis:
        services.yt = YouTube(secrets.get(secrets.YOUTUBE), quota)
        services.jev = Jev(secrets.get(secrets.JEV))
        if secrets.is_stored(secrets.TWITCH_ID) and secrets.is_stored(secrets.TWITCH_SECRET):
            services.twitch = Twitch(secrets.get(secrets.TWITCH_ID), secrets.get(secrets.TWITCH_SECRET))
    return services
