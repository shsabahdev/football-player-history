"""Typed settings and checkpoint models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PathsConfig:
    project_root: Path
    base_data: Path
    downloads: Path
    output: Path
    logs: Path
    checkpoints: Path
    json: Path
    temp: Path
    database: Path


@dataclass(frozen=True)
class DatabaseConfig:
    name: str
    path: Path


@dataclass(frozen=True)
class APIConfig:
    provider: str
    base_url: str
    request_timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    requests_per_day: int
    daily_request_safety_margin: int


@dataclass(frozen=True)
class PlayerConfig:
    name: str
    provider_player_id: str | None
    crawl_from_season: int
    crawl_to_season: int | str
    desired_historical_from_season: int | None = None
    provider: str = "api_football"
    current_team_name: str | None = None
    league_name: str | None = None
    league_id: str | None = None
    resolution_status: str = "pending"
    resolution_season: int | None = None
    resolution_seasons: list[int] = field(default_factory=list)
    attempted_resolution_seasons: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class CrawlingConfig:
    batch_size: int
    delay_between_requests_seconds: float
    save_raw_json: bool
    overwrite_existing: bool


@dataclass(frozen=True)
class CheckpointConfig:
    resume_on_start: bool


@dataclass
class CheckpointState:
    player_name: str
    provider_player_id: str | None = None
    supported_season_min: int | None = None
    supported_season_max: int | None = None
    unsupported_seasons: list[int] = field(default_factory=list)
    skipped_seasons: list[int] = field(default_factory=list)
    current_season: int | None = None
    current_stage: str = "resolve_player"
    status: str = "in_progress"
    active_fixture_id: str | None = None
    active_fixture_player_stats: list[dict[str, Any]] | None = None
    active_fixture_player_stats_cached: bool = False
    active_fixture_player_stats_raw_path: str | None = None
    seasons_to_crawl: list[int] = field(default_factory=list)
    fixture_ids: list[str] = field(default_factory=list)
    completed_seasons: list[int] = field(default_factory=list)
    in_progress_seasons: list[int] = field(default_factory=list)
    last_processed_fixture_id: str | None = None
    completed_fixture_ids: list[str] = field(default_factory=list)
    discovered_team_ids: list[str] = field(default_factory=list)
    processed_team_ids: list[str] = field(default_factory=list)
    current_team_id: str | None = None
    current_fixture_page: int = 1
    discovered_fixture_ids: list[str] = field(default_factory=list)
    processed_fixture_ids: list[str] = field(default_factory=list)
    participation_checked_fixture_ids: list[str] = field(default_factory=list)
    player_participated_fixture_ids: list[str] = field(default_factory=list)
    season_status: str | None = None
    season_fixture_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    total_fixtures_found: int = 0
    total_fixtures_processed: int = 0
    requests_used_today: int = 0
    request_date: str | None = None
    player_config_name: str | None = None
    provider: str | None = None
    resolution_stage: str = "pending"
    current_team_name: str | None = None
    resolved_team_id: str | None = None
    resolved_team_name: str | None = None
    resolution_season: int | None = None
    current_page: int = 1
    has_more_pages: bool = False
    candidates_found: list[int] = field(default_factory=list)
    candidates_processed: int = 0
    last_processed_page: int | None = None
    error_message: str | None = None
    resolution_total_pages: int | None = None
    resolution_current_attempt: dict[str, Any] | None = None
    resolution_completed_attempts: list[dict[str, Any]] = field(default_factory=list)
    started_at: str | None = None
    last_updated_at: str | None = None
    errors: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CheckpointState":
        """Build a checkpoint from JSON-compatible data."""
        allowed = cls.__dataclass_fields__
        return cls(**{key: item for key, item in value.items() if key in allowed})

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-compatible checkpoint data."""
        return {key: value for key, value in vars(self).items()}
