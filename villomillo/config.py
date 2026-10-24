"""Settings: one app file plus one profile file per channel, checked on load."""

from __future__ import annotations

import hashlib
import json
import pathlib
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config" / "villomillo"



class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in a YAML file is an error, not a silent no-op


class Dashboard(_Strict):
    host: str = "127.0.0.1"
    port: int = 8780

    @field_validator("host")
    @classmethod
    def _local_only(cls, v: str) -> str:
        if v not in ("127.0.0.1", "localhost"):
            raise ValueError("the dashboard only listens on this PC (127.0.0.1)")
        return v


class Budgets(_Strict):
    youtube_general_units_per_day: int = 8000
    youtube_search_calls_per_day: int = 80


class SpendBreaker(_Strict):
    """Work pauses when spending since your last approval reaches these amounts. There are no daily or monthly caps."""

    claude_usd: float = Field(25.0, gt=0)
    jev_usd: float = Field(5.0, gt=0)


class Scoring(_Strict):
    """The numbers behind a clip's viral score, estimated CPM and expected revenue (docs/AUTOPILOT-PLAN.md, section 6)."""

    viral_min: int = Field(75, ge=0, le=100)  # a clip needs at least this viral score
    engine_weight: float = Field(0.6, ge=0, le=1)  # the engine's rating counts this much; Jev's reading of the clip counts the rest
    cpm_base: float = Field(1.0, gt=0)  # estimated CPM of a clip that scores exactly viral_min in a factor-1 category
    cpm_floor: float = Field(1.0, ge=0)  # a pasted clip needs at least this estimated CPM
    predicted_views: int = Field(8000, ge=0)  # a rough guess per clip, replaced by real results later
    type_certainty_min: float = Field(0.75, ge=0, le=1)  # below this, the video's kind is unsure and the plain look is used
    category_factors: dict[str, float] = {"Podcasts": 1.0, "Stream clips": 1.0}  # all 1.0 until real results say otherwise
    default_factor: float = Field(1.0, gt=0)


class Schedule(_Strict):
    post_every_minutes: int = Field(15, ge=1)  # send posts that are due
    stats_every_minutes: int = Field(360, ge=30)  # read the real views of recent posts


class AppConfig(_Strict):
    database: str = "data/villomillo/villomillo.db"
    dashboard: Dashboard = Dashboard()
    budgets: Budgets = Budgets()
    spend_breaker: SpendBreaker = SpendBreaker()
    scoring: Scoring = Scoring()
    schedule: Schedule = Schedule()

    def database_path(self) -> pathlib.Path:
        p = pathlib.Path(self.database)
        return p if p.is_absolute() else ROOT / p


class Niche(_Strict):
    definition: str
    include: list[str] = []
    exclude: list[str] = []


class Campaign(_Strict):
    """The rules a clipping campaign sets that code can check on a finished clip."""
    name: str
    min_s: float = Field(20, ge=5)
    max_s: float = Field(60, ge=10)
    face_window_s: float = Field(5, gt=0)  # the campaign's face rule: the host on screen in the first N seconds
    require_captions: bool = True
    no_ad_reads: bool = True

    @model_validator(mode="after")
    def _ordered(self):
        if self.min_s >= self.max_s:
            raise ValueError("campaign min_s must be less than max_s")
        return self


class Posting(_Strict):
    """How approved clips go out. Every posting path starts as a dry run."""
    dry_run: bool = True  # write what would be posted to the outbox; send nothing
    platforms: dict[str, Literal["api", "manual"]] = {}  # platform -> how it goes out
    privacy: Literal["private", "unlisted", "public"] = "private"
    min_gap_minutes: int = Field(180, ge=0)
    daily_cap: int = Field(2, ge=1)  # per platform, per day (YouTube's own limit for a channel is 100)
    topics: dict[str, list[str]] = {}  # platform -> words that send a clip there
    tags: dict[str, str] = {}  # platform -> the campaign's required @mentions, as written


class Brand(_Strict):
    name: str = ""  # the channel's name as people see it, for the dashboard
    handle: str = ""  # the channel handle, shown as the faint watermark in the middle of every clip


class Profile(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,30}$")
    display_name: str
    category: str = ""  # what this channel posts, such as "Podcasts". One channel per category.
    autopilot: str = Field("review", pattern=r"^(off|review|auto)$")
    niche: Niche
    campaign: Campaign | None = None
    posting: Posting | None = None
    brand: Brand = Brand()
    format: str | None = None  # the recipe in config/villomillo/blueprints.yaml; None keeps the engine's own look
    format_forbid: list[str] = []  # blueprints this channel must never use (for example outside footage)
    format_params: dict[str, dict] = {}  # per-blueprint parameter overrides

    def digest(self) -> str:
        blob = json.dumps(self.model_dump(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def load_app(path: pathlib.Path | None = None) -> AppConfig:
    path = path or CONFIG_DIR / "app.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return AppConfig(**(data or {}))


def load_profiles(directory: pathlib.Path | None = None) -> dict[str, Profile]:
    directory = directory or CONFIG_DIR / "channels"
    profiles: dict[str, Profile] = {}
    for f in sorted(directory.glob("*.yaml")):
        profile = Profile(**yaml.safe_load(f.read_text(encoding="utf-8")))
        if profile.id in profiles:
            raise ValueError(f"two profiles use the id {profile.id!r}")
        profiles[profile.id] = profile
    return profiles
