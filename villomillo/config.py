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

WEIGHT_NAMES = ("outlier", "momentum", "engagement", "niche", "moments", "appeal", "hook")


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
    claude_escalations_per_day: int = Field(10, ge=0)
    transcript_jev_usd_per_video: float = 0.01  # a single video's transcript pass should never cost more than this


class SpendBreaker(_Strict):
    """Work pauses when spending since your last approval reaches these amounts. There are no daily or monthly caps."""

    claude_usd: float = Field(25.0, gt=0)
    jev_usd: float = Field(5.0, gt=0)


class Schedule(_Strict):
    watch_every_minutes: int = Field(30, ge=5)
    topics_every_minutes: int = Field(720, ge=30)
    snapshots_every_minutes: int = Field(60, ge=10)
    grade_every_minutes: int = Field(15, ge=1)
    grade_batch: int = Field(40, ge=1)
    review_every_minutes: int = Field(10080, ge=60)  # weekly
    post_every_minutes: int = Field(15, ge=1)  # send posts that are due


class AppConfig(_Strict):
    database: str = "data/villomillo/villomillo.db"
    dashboard: Dashboard = Dashboard()
    budgets: Budgets = Budgets()
    spend_breaker: SpendBreaker = SpendBreaker()
    schedule: Schedule = Schedule()

    def database_path(self) -> pathlib.Path:
        p = pathlib.Path(self.database)
        return p if p.is_absolute() else ROOT / p


class WatchChannel(_Strict):
    handle: str | None = None
    channel_id: str | None = None
    note: str = ""

    @model_validator(mode="after")
    def _one_way_to_name_it(self) -> WatchChannel:
        if bool(self.handle) == bool(self.channel_id):
            raise ValueError("give either handle or channel_id, not both and not neither")
        return self


class Niche(_Strict):
    definition: str
    include: list[str] = []
    exclude: list[str] = []


class Trending(_Strict):
    regions: list[str] = ["US"]
    youtube_category: int | None = None


class Research(_Strict):
    watch_channels: list[WatchChannel] = []
    topics: list[str] = []
    max_searches_per_run: int = Field(8, ge=0, le=50)
    topic_search_days: int = Field(7, ge=1, le=60)
    trending: Trending | None = None
    freshness_hours: int = Field(336, ge=1)
    languages: list[str] = ["en"]
    min_duration_min: float = 8
    max_duration_min: float = 360
    blocked_title_words: list[str] = []
    blocked_channel_ids: list[str] = []


class Grading(_Strict):
    min_score: float = Field(70, ge=0, le=100)
    min_certainty: float = Field(0.6, ge=0, le=1)
    escalate_band: float = Field(5, ge=0, le=50)
    weights: dict[str, float] = {
        "outlier": 0.20,
        "momentum": 0.10,
        "engagement": 0.10,
        "niche": 0.20,
        "moments": 0.15,
        "appeal": 0.15,
        "hook": 0.10,
    }

    @field_validator("weights")
    @classmethod
    def _known_weights(cls, v: dict[str, float]) -> dict[str, float]:
        unknown = set(v) - set(WEIGHT_NAMES)
        if unknown:
            raise ValueError(f"unknown weights {sorted(unknown)}; allowed: {list(WEIGHT_NAMES)}")
        if any(x < 0 for x in v.values()) or sum(v.values()) <= 0:
            raise ValueError("weights must be zero or more, and not all zero")
        return v


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
    daily_cap: int = Field(2, ge=1)  # per platform, per UTC day
    topics: dict[str, list[str]] = {}  # platform -> words that send a clip there
    tags: dict[str, str] = {}  # platform -> the campaign's required @mentions, as written


class Brand(_Strict):
    name: str = ""  # the channel's name as people see it, for the dashboard
    handle: str = ""  # the channel handle, shown as the faint watermark in the middle of every clip


class Profile(_Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,30}$")
    display_name: str
    autopilot: str = Field("review", pattern=r"^(off|review|auto)$")
    niche: Niche
    research: Research = Research()
    grading: Grading = Grading()
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
