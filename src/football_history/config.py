"""Configuration loading, validation, and safe persistence."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .models import (
    APIConfig,
    CheckpointConfig,
    CrawlingConfig,
    DatabaseConfig,
    PathsConfig,
    PlayerConfig,
)


class ConfigError(ValueError):
    """Raised when project configuration is missing or invalid."""


def _read_yaml(path: Path, label: str) -> dict[str, Any]:
    """Read a YAML mapping and report file-specific configuration errors."""
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except OSError as exc:
        raise ConfigError(f"Cannot read {label}: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"{label.capitalize()} root must be a YAML mapping: {path}")
    return value


class ProjectConfig:
    """Load stable application settings from config/project.yaml."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.path = project_root / "config" / "project.yaml"
        self.raw = _read_yaml(self.path, "project configuration")
        allowed_top_level = {
            "project", "paths", "database", "api", "crawling", "resolution", "logging",
        }
        unexpected = self.raw.keys() - allowed_top_level
        if unexpected:
            raise ConfigError(
                "project.yaml contains unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        project = self.raw.get("project")
        if (
            not isinstance(project, dict)
            or not isinstance(project.get("name"), str)
            or not project["name"].strip()
            or not isinstance(project.get("version"), str)
        ):
            raise ConfigError("project.yaml requires non-empty project.name and project.version.")
        for section in ("paths", "database", "api", "crawling", "resolution", "logging"):
            if not isinstance(self.raw.get(section), dict):
                raise ConfigError(f"project.yaml requires a '{section}' mapping.")
        required_paths = (
            "base_data_path", "downloads_dir", "output_dir", "checkpoints_dir",
            "json_dir", "temp_dir", "logs_dir",
        )
        missing_paths = [key for key in required_paths if key not in self.raw["paths"]]
        if missing_paths:
            raise ConfigError(
                f"Missing project.paths fields: {', '.join(missing_paths)}"
            )
        if not {"name", "path"} <= self.raw["database"].keys():
            raise ConfigError("project.yaml database requires name and path.")
        for section in ("paths", "database"):
            for key, value in self.raw[section].items():
                if not isinstance(value, str) or not value.strip():
                    raise ConfigError(f"project.yaml {section}.{key} must be a non-empty string.")
        for key in (
            "provider", "base_url", "request_timeout_seconds", "max_retries",
            "retry_backoff_seconds", "requests_per_day",
        ):
            if key not in self.raw["api"]:
                raise ConfigError(f"Missing project.api field: {key}")
        for key in (
            "batch_size", "delay_between_requests_seconds", "save_raw_json",
            "overwrite_existing", "max_resolution_pages",
        ):
            if key not in self.raw["crawling"]:
                raise ConfigError(f"Missing project.crawling field: {key}")
        for key in ("minimum_name_similarity", "prefer_exact_match"):
            if key not in self.raw["resolution"]:
                raise ConfigError(f"Missing project.resolution field: {key}")
        for key in ("level", "console", "file"):
            if key not in self.raw["logging"]:
                raise ConfigError(f"Missing project.logging field: {key}")
        logging_settings = self.raw["logging"]
        if not isinstance(logging_settings["level"], str) or any(
            not isinstance(logging_settings[key], bool) for key in ("console", "file")
        ):
            raise ConfigError("project.logging requires a level string and boolean console/file.")
        if logging_settings["level"].upper() not in {
            "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL",
        }:
            raise ConfigError("project.logging.level must be DEBUG, INFO, WARNING, ERROR, or CRITICAL.")
        paths = self.raw["paths"]
        resolve = lambda value: (
            Path(value) if Path(value).is_absolute()
            else project_root / Path(value)
        ).resolve()
        self.name = project["name"]
        self.version = project["version"]
        self.paths = PathsConfig(
            project_root=project_root,
            base_data=resolve(paths["base_data_path"]),
            downloads=resolve(paths["downloads_dir"]),
            output=resolve(paths["output_dir"]),
            logs=resolve(paths["logs_dir"]),
            checkpoints=resolve(paths["checkpoints_dir"]),
            json=resolve(paths["json_dir"]),
            temp=resolve(paths["temp_dir"]),
            database=resolve(self.raw["database"]["path"]),
        )
        self.database = DatabaseConfig(
            name=self.raw["database"]["name"],
            path=self.paths.database,
        )
        api_settings = dict(self.raw["api"])
        api_settings.setdefault("daily_request_safety_margin", 0)
        try:
            self.api = APIConfig(**api_settings)
            self.crawling = CrawlingConfig(**{
                key: self.raw["crawling"][key] for key in (
                    "batch_size", "delay_between_requests_seconds",
                    "save_raw_json", "overwrite_existing",
                )
            })
        except TypeError as exc:
            raise ConfigError(f"Invalid project.yaml settings: {exc}") from exc
        self.resolution = dict(self.raw["resolution"])
        self.logging = dict(logging_settings)

    @property
    def paths_settings(self) -> dict[str, Any]:
        """Return the raw stable path declarations."""
        return self.raw["paths"]


class PlayersConfig:
    """Load and atomically persist player inputs from config/players.yaml."""

    def __init__(self, project_root: Path) -> None:
        self.path = project_root / "config" / "players.yaml"
        self.raw = _read_yaml(self.path, "player input configuration")
        if self.raw.keys() != {"players"}:
            raise ConfigError("players.yaml may contain only the 'players' input mapping.")
        players = self.raw.get("players")
        if not isinstance(players, dict):
            raise ConfigError("players.yaml requires a 'players' mapping.")
        for key in ("to_crawl", "completed_players"):
            if not isinstance(players.get(key), list):
                raise ConfigError(f"players.{key} must be a list.")
            for item in players[key]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ConfigError(f"Every players.{key} entry requires a name.")

    @property
    def to_crawl(self) -> list[dict[str, Any]]:
        return self.raw["players"]["to_crawl"]

    @property
    def completed_players(self) -> list[dict[str, Any]]:
        return self.raw["players"]["completed_players"]

    def save(self) -> None:
        """Atomically write only player input fields."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                yaml.safe_dump(self.raw, stream, sort_keys=False, allow_unicode=True)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()


class Config:
    """Combine validated project settings and player run inputs."""

    def __init__(self, project_root: Path | str | None = None) -> None:
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.project_config = ProjectConfig(self.project_root)
        self.players_config = PlayersConfig(self.project_root)
        self.project_config_path = self.project_config.path
        self.config_path = self.players_config.path
        load_dotenv(self.project_root / ".env")
        project = self.project_config.raw
        paths = project["paths"]
        raw: dict[str, Any] = {
            "project_name": project["project"]["name"],
            "version": project["project"].get("version", "0.1.0"),
            "base_data_path": paths["base_data_path"],
            "downloads_dir": paths["downloads_dir"],
            "output_dir": paths["output_dir"],
            "checkpoints_dir": paths["checkpoints_dir"],
            "json_dir": paths["json_dir"],
            "temp_dir": paths["temp_dir"],
            "logs_dir": paths["logs_dir"],
            "database": project["database"],
            "api": project["api"],
            "crawling": {
                key: project["crawling"][key] for key in (
                    "batch_size", "delay_between_requests_seconds",
                    "save_raw_json", "overwrite_existing",
                )
            },
            "checkpoint": {"resume_on_start": True},
            "player_resolution": {
                "minimum_similarity": project["resolution"]["minimum_name_similarity"],
                "max_pages": project["crawling"]["max_resolution_pages"],
                "prefer_exact_match": project["resolution"]["prefer_exact_match"],
            },
            "players": self.players_config.raw["players"],
        }
        self._raw: dict[str, Any] = raw
        self._validate()
        self.project_name: str = raw["project_name"]
        self.paths = self._build_paths()
        self.database = DatabaseConfig(
            name=raw["database"]["name"],
            path=self._resolve(raw["database"]["path"]),
        )
        api_settings = dict(raw["api"])
        api_settings.setdefault("daily_request_safety_margin", 0)
        self.api = APIConfig(**api_settings)
        self.crawling = CrawlingConfig(**raw["crawling"])
        self.checkpoint = CheckpointConfig(**raw["checkpoint"])
        self.players = [self._player(item) for item in raw["players"]["to_crawl"]]
        self.completed_players: list[dict[str, Any]] = raw["players"]["completed_players"]
        resolution = raw.get("player_resolution", {})
        self.player_resolution_min_similarity = float(
            resolution.get("minimum_similarity", 0.85)
        )
        self.player_resolution_max_pages = int(resolution.get("max_pages", 20))
        for directory in (
            self.paths.base_data,
            self.paths.downloads,
            self.paths.output,
            self.paths.logs,
            self.paths.checkpoints,
            self.paths.json,
            self.paths.temp,
            self.database.path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @property
    def logs(self) -> dict[str, Any]:
        """Return logging settings from project.yaml."""
        return self.project_config.raw["logging"]

    @property
    def api_key(self) -> str:
        """Return the API key or fail clearly without exposing its value."""
        key = os.getenv("API_FOOTBALL_KEY", "").strip()
        if not key:
            raise ConfigError(
                "API_FOOTBALL_KEY is not set. Add it to the project .env file "
                "or export it in the environment."
            )
        return key

    def get_player(self, name: str) -> PlayerConfig | None:
        """Find a configured player by case-insensitive name."""
        return next((player for player in self.players if player.name.casefold() == name.casefold()), None)

    def list_pending_players(self) -> list[PlayerConfig]:
        """Return configured players eligible for crawl/resolution."""
        return list(self.players)

    def list_completed_players(self) -> list[dict[str, Any]]:
        """Return the configured completed-player entries."""
        return list(self.completed_players)

    def list_players_needing_resolution(self) -> list[PlayerConfig]:
        """Return players without a resolved provider identity."""
        return [
            player for player in self.players
            if player.resolution_status != "resolved" or not player.provider_player_id
        ]

    def update_player(self, name: str, updates: dict[str, Any]) -> PlayerConfig:
        """Update only approved user-input fields for a configured player."""
        allowed = {
            "provider", "provider_player_id", "current_team_name", "league_name",
            "league_id", "resolution_status", "resolution_season", "resolution_seasons",
            "attempted_resolution_seasons", "desired_historical_from_season",
            "crawl_from_season", "crawl_to_season",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise ConfigError(
                "Runtime or unsupported player fields cannot be written to players.yaml: "
                + ", ".join(sorted(invalid))
            )
        self.update_player_progress(name, **updates)
        player = self.get_player(name)
        if player is None:
            raise ConfigError(f"Player is not in the crawl list: {name}")
        return player

    def remove_player(self, name: str) -> None:
        """Remove one player input from both configured lists."""
        players = self._raw["players"]
        players["to_crawl"] = [
            item for item in players["to_crawl"]
            if item.get("name", "").casefold() != name.casefold()
        ]
        players["completed_players"] = [
            item for item in players["completed_players"]
            if item.get("name", "").casefold() != name.casefold()
        ]
        self.players = [self._player(item) for item in players["to_crawl"]]
        self.completed_players = players["completed_players"]
        self.save()

    def mark_player_completed(self, name: str) -> None:
        """Move a configured player input to the completed list."""
        player = self.get_player(name)
        if player is None:
            raise ConfigError(f"Player is not in the crawl list: {name}")
        self.add_or_update_completed_player(player)

    def reset_players_to_defaults(self) -> None:
        """Atomically replace players.yaml with the factory default player list."""
        defaults = _read_yaml(
            self.project_root / "config" / "default_players.yaml",
            "default player configuration",
        )
        if not isinstance(defaults.get("players"), dict):
            raise ConfigError("default_players.yaml requires a 'players' mapping.")
        self.players_config.raw = defaults
        self._raw["players"] = defaults["players"]
        self.players = [self._player(item) for item in defaults["players"]["to_crawl"]]
        self.completed_players = defaults["players"]["completed_players"]
        self.save()

    def add_or_update_completed_player(self, player: PlayerConfig | dict[str, Any]) -> None:
        """Move a player to completed_players and persist configuration."""
        item = player if isinstance(player, dict) else {
            "name": player.name,
            "provider": player.provider,
            "provider_player_id": player.provider_player_id,
            "current_team_name": player.current_team_name,
            "league_name": player.league_name,
            "league_id": player.league_id,
            "resolution_status": player.resolution_status,
            "resolution_season": player.resolution_season,
            "resolution_seasons": player.resolution_seasons,
            "attempted_resolution_seasons": player.attempted_resolution_seasons,
            "desired_historical_from_season": player.desired_historical_from_season,
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        }
        self._raw["players"]["completed_players"] = [
            old for old in self._raw["players"]["completed_players"]
            if old.get("name", "").casefold() != item["name"].casefold()
        ] + [item]
        self._raw["players"]["to_crawl"] = [
            old for old in self._raw["players"]["to_crawl"]
            if old.get("name", "").casefold() != item["name"].casefold()
        ]
        self.completed_players = self._raw["players"]["completed_players"]
        self.players = [self._player(value) for value in self._raw["players"]["to_crawl"]]
        self.save()

    def add_player_to_crawl(self, player: PlayerConfig | dict[str, Any]) -> None:
        """Move a completed player back to the crawl queue and persist."""
        item = player if isinstance(player, dict) else {
            "name": player.name,
            "provider": player.provider,
            "provider_player_id": player.provider_player_id,
            "current_team_name": player.current_team_name,
            "league_name": player.league_name,
            "league_id": player.league_id,
            "resolution_status": player.resolution_status,
            "resolution_season": player.resolution_season,
            "resolution_seasons": player.resolution_seasons,
            "attempted_resolution_seasons": player.attempted_resolution_seasons,
            "desired_historical_from_season": player.desired_historical_from_season,
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        }
        self._raw["players"]["completed_players"] = [
            old for old in self._raw["players"]["completed_players"]
            if old.get("name", "").casefold() != item["name"].casefold()
        ]
        self._raw["players"]["to_crawl"] = [
            old for old in self._raw["players"]["to_crawl"]
            if old.get("name", "").casefold() != item["name"].casefold()
        ] + [item]
        self.completed_players = self._raw["players"]["completed_players"]
        self.players = [self._player(value) for value in self._raw["players"]["to_crawl"]]
        self.save()

    def update_player_progress(self, name: str, **updates: Any) -> None:
        """Update approved user inputs, never runtime crawl progress."""
        allowed = {
            "provider", "provider_player_id", "current_team_name", "league_name",
            "league_id", "resolution_status", "resolution_season", "resolution_seasons",
            "attempted_resolution_seasons", "desired_historical_from_season",
            "crawl_from_season", "crawl_to_season",
        }
        invalid = set(updates) - allowed
        if invalid:
            raise ConfigError(
                "Runtime or unsupported player fields cannot be written to players.yaml: "
                + ", ".join(sorted(invalid))
            )
        for item in self._raw["players"]["to_crawl"] + self._raw["players"]["completed_players"]:
            if item.get("name", "").casefold() == name.casefold():
                item.update(updates)
                self.players = [
                    self._player(value) for value in self._raw["players"]["to_crawl"]
                ]
                self.completed_players = self._raw["players"]["completed_players"]
                self.save()
                return
        raise ConfigError(f"Player is not configured: {name}")

    def add_player(
        self,
        name: str | dict[str, Any],
        *,
        team_name: str | None = None,
        league_name: str | None = None,
        league_id: str | None = None,
        resolution_season: int | None = None,
    ) -> PlayerConfig:
        """Add a name-based player to the crawl queue without making API calls."""
        if isinstance(name, dict):
            player_input = dict(name)
            name = str(player_input.get("name", ""))
            team_name = player_input.get("current_team_name", team_name)
            league_name = player_input.get("league_name", league_name)
            league_id = player_input.get("league_id", league_id)
            resolution_season = player_input.get("resolution_season", resolution_season)
        else:
            player_input = {}
        normalized_name = name.strip()
        if not normalized_name:
            raise ConfigError("Player name cannot be empty.")
        if any(player.name.casefold() == normalized_name.casefold() for player in self.players):
            raise ConfigError(f"Player is already in the crawl queue: {normalized_name}")
        if any(item.get("name", "").casefold() == normalized_name.casefold()
               for item in self.completed_players):
            raise ConfigError(f"Player is already completed: {normalized_name}")
        existing = self.players[0] if self.players else None
        item: dict[str, Any] = {
            "name": normalized_name,
            "provider": self.api.provider,
            "provider_player_id": None,
            "current_team_name": team_name.strip() if team_name else None,
            "league_name": league_name.strip() if league_name else None,
            "league_id": str(league_id) if league_id else None,
            "resolution_status": "pending" if (team_name or league_name or league_id)
            else "needs_team_or_league",
            "desired_historical_from_season": None,
            "crawl_from_season": existing.crawl_from_season if existing else 2022,
            "crawl_to_season": existing.crawl_to_season if existing else "current",
        }
        item.update(player_input)
        item.setdefault("provider", self.api.provider)
        item.setdefault("provider_player_id", None)
        item.setdefault("resolution_status", "pending" if (
            item.get("current_team_name") or item.get("league_name") or item.get("league_id")
        ) else "needs_team_or_league")
        item.setdefault("desired_historical_from_season", None)
        item.setdefault("crawl_from_season", existing.crawl_from_season if existing else 2022)
        item.setdefault("crawl_to_season", existing.crawl_to_season if existing else "current")
        if resolution_season is not None:
            item["resolution_season"] = resolution_season
        self._raw["players"]["to_crawl"].append(item)
        self.players.append(self._player(item))
        self.save()
        return self.players[-1]

    def set_player_resolution(
        self,
        name: str,
        status: str,
        *,
        provider_player_id: str | None = None,
        current_team_name: str | None = None,
        resolution_season: int | None = None,
    ) -> None:
        """Persist resolution outcome in config without losing other player settings."""
        for item in self._raw["players"]["to_crawl"]:
            if item.get("name", "").casefold() == name.casefold():
                item["resolution_status"] = status
                if provider_player_id is not None:
                    item["provider_player_id"] = str(provider_player_id)
                if current_team_name is not None:
                    item["current_team_name"] = current_team_name
                if resolution_season is not None:
                    item["resolution_season"] = resolution_season
                self.players = [
                    self._player(value) for value in self._raw["players"]["to_crawl"]
                ]
                self.save()
                return
        raise ConfigError(f"Player is not in the crawl queue: {name}")

    def save(self) -> None:
        """Persist only user-declared player inputs."""
        for item in self._raw["players"]["to_crawl"] + self._raw["players"]["completed_players"]:
            forbidden = {
                "current_page", "current_season", "completed_fixture_ids",
                "requests_used_today", "error_message", "last_updated_at",
            } & item.keys()
            if forbidden:
                raise ConfigError(
                    "Runtime state cannot be written to players.yaml: "
                    + ", ".join(sorted(forbidden))
                )
        self.players_config.raw["players"] = self._raw["players"]
        self.players_config.save()

    def _validate(self) -> None:
        required = ("project_name", "base_data_path", "downloads_dir", "output_dir",
                    "checkpoints_dir", "json_dir", "temp_dir", "database", "api",
                    "players", "crawling", "checkpoint")
        missing = [key for key in required if key not in self._raw]
        if missing:
            raise ConfigError(f"Missing required configuration fields: {', '.join(missing)}")
        for section, keys in {
            "database": ("name", "path"),
            "api": ("provider", "base_url", "request_timeout_seconds", "max_retries",
                    "retry_backoff_seconds", "requests_per_day"),
            "players": ("to_crawl", "completed_players"),
            "crawling": ("batch_size", "delay_between_requests_seconds",
                         "save_raw_json", "overwrite_existing"),
            "checkpoint": ("resume_on_start",),
        }.items():
            if not isinstance(self._raw[section], dict):
                raise ConfigError(f"'{section}' must be a mapping.")
            absent = [key for key in keys if key not in self._raw[section]]
            if absent:
                raise ConfigError(f"Missing {section} fields: {', '.join(absent)}")
        if not isinstance(self._raw["players"]["to_crawl"], list) or not isinstance(
            self._raw["players"]["completed_players"], list
        ):
            raise ConfigError("players.to_crawl and players.completed_players must be lists.")
        for player in self._raw["players"]["to_crawl"]:
            self._player(player)
        for player in self._raw["players"]["completed_players"]:
            self._player(player)
        for section, keys in (("database", ("name", "path")),
                              ("api", ("provider", "base_url"))):
            for key in keys:
                if not isinstance(self._raw[section][key], str) or not self._raw[section][key]:
                    raise ConfigError(f"{section}.{key} must be a non-empty string.")
        api = self._raw["api"]
        for key in ("request_timeout_seconds", "retry_backoff_seconds"):
            if (not isinstance(api[key], (int, float)) or isinstance(api[key], bool)
                    or api[key] <= 0):
                raise ConfigError(f"api.{key} must be a positive number.")
        safety_margin = api.get("daily_request_safety_margin", 0)
        for key, value in (
            ("max_retries", api["max_retries"]),
            ("requests_per_day", api["requests_per_day"]),
            ("daily_request_safety_margin", safety_margin),
        ):
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < (1 if key == "requests_per_day" else 0)):
                raise ConfigError(f"api.{key} has an invalid value.")
        if safety_margin > api["requests_per_day"]:
            raise ConfigError(
                "api.daily_request_safety_margin cannot exceed api.requests_per_day."
            )
        crawl = self._raw["crawling"]
        if (not isinstance(crawl["batch_size"], int) or isinstance(crawl["batch_size"], bool)
                or crawl["batch_size"] < 1):
            raise ConfigError("crawling.batch_size must be a positive integer.")
        if (not isinstance(crawl["delay_between_requests_seconds"], (int, float))
                or isinstance(crawl["delay_between_requests_seconds"], bool)
                or crawl["delay_between_requests_seconds"] < 0):
            raise ConfigError("crawling.delay_between_requests_seconds must be non-negative.")
        for key in ("save_raw_json", "overwrite_existing"):
            if not isinstance(crawl[key], bool):
                raise ConfigError(f"crawling.{key} must be boolean.")
        if not isinstance(self._raw["checkpoint"]["resume_on_start"], bool):
            raise ConfigError("checkpoint.resume_on_start must be boolean.")
        resolution = self._raw.get("player_resolution", {})
        if not isinstance(resolution, dict):
            raise ConfigError("'player_resolution' must be a mapping.")
        threshold = resolution.get("minimum_similarity", 0.85)
        if (
            not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
            or not 0 <= threshold <= 1
        ):
            raise ConfigError("player_resolution.minimum_similarity must be between 0 and 1.")
        max_pages = resolution.get("max_pages", 20)
        if not isinstance(max_pages, int) or isinstance(max_pages, bool) or max_pages < 1:
            raise ConfigError("player_resolution.max_pages must be a positive integer.")
        for key in ("project_name", "base_data_path", "downloads_dir", "output_dir",
                    "checkpoints_dir", "json_dir", "temp_dir", "logs_dir"):
            if not isinstance(self._raw[key], str) or not self._raw[key]:
                raise ConfigError(f"{key} must be a non-empty string.")

    def _player(self, item: Any) -> PlayerConfig:
        if not isinstance(item, dict):
            raise ConfigError("Each player entry must be a mapping.")
        allowed = {
            "name", "provider", "provider_player_id", "current_team_name",
            "league_name", "league_id", "resolution_status", "resolution_season",
            "resolution_seasons", "attempted_resolution_seasons",
            "desired_historical_from_season", "crawl_from_season", "crawl_to_season",
        }
        unexpected = item.keys() - allowed
        if unexpected:
            raise ConfigError(
                "Runtime or unsupported fields are not allowed in players.yaml: "
                + ", ".join(sorted(unexpected))
            )
        required = ("name", "crawl_from_season", "crawl_to_season")
        if any(key not in item for key in required):
            raise ConfigError(f"Player entries require {', '.join(required)}.")
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise ConfigError("Player name must be a non-empty string.")
        if not isinstance(item["crawl_from_season"], int) or isinstance(
            item["crawl_from_season"], bool
        ):
            raise ConfigError(f"crawl_from_season must be an integer for {item['name']}.")
        desired_start = item.get("desired_historical_from_season")
        if desired_start is not None and (
            not isinstance(desired_start, int) or isinstance(desired_start, bool)
        ):
            raise ConfigError(
                f"desired_historical_from_season must be an integer for {item['name']}."
            )
        resolution_seasons = item.get("resolution_seasons", [])
        attempted_seasons = item.get("attempted_resolution_seasons", [])
        for field_name, seasons in (
            ("resolution_seasons", resolution_seasons),
            ("attempted_resolution_seasons", attempted_seasons),
        ):
            if (
                not isinstance(seasons, list)
                or any(not isinstance(season, int) or isinstance(season, bool) or season < 1
                       for season in seasons)
                or len(set(seasons)) != len(seasons)
            ):
                raise ConfigError(f"{field_name} must be a list of distinct positive integers.")
        end = item["crawl_to_season"]
        if not (
            (isinstance(end, int) and not isinstance(end, bool))
            or end == "current"
        ):
            raise ConfigError(f"crawl_to_season must be an integer or 'current' for {item['name']}.")
        player_id = item.get("provider_player_id")
        resolution_season = item.get("resolution_season")
        if resolution_season is not None and (
            not isinstance(resolution_season, int) or isinstance(resolution_season, bool)
            or resolution_season < 1
        ):
            raise ConfigError(f"resolution_season must be a positive integer for {item['name']}.")
        provider = item.get("provider", "api_football")
        if not isinstance(provider, str) or not provider.strip():
            raise ConfigError(f"provider must be a non-empty string for {item['name']}.")
        resolution_status = item.get(
            "resolution_status", "resolved" if player_id else "pending"
        )
        valid_statuses = {
            "pending", "resolved", "needs_team_or_league", "needs_review",
            "ambiguous", "not_found", "not_found_for_season",
            "paused_quota_limit", "failed", "resolving_team", "team_resolved",
            "resolving_players_page", "players_page_resolved", "matching_candidates",
        }
        if resolution_status not in valid_statuses:
            raise ConfigError(
                f"Invalid resolution_status '{resolution_status}' for {item['name']}."
            )
        for key in ("current_team_name", "league_name"):
            if item.get(key) is not None and not isinstance(item[key], str):
                raise ConfigError(f"{key} must be a string for {item['name']}.")
        if item.get("league_id") is not None and (
            not isinstance(item["league_id"], (str, int))
            or isinstance(item["league_id"], bool)
        ):
            raise ConfigError(f"league_id must be a string or integer for {item['name']}.")
        return PlayerConfig(item["name"], str(player_id) if player_id is not None else None,
                            item["crawl_from_season"], end,
                            desired_start,
                            str(provider),
                            item.get("current_team_name"),
                            item.get("league_name"),
                            str(item["league_id"]) if item.get("league_id") is not None else None,
                            resolution_status,
                            resolution_season,
                            resolution_seasons,
                            attempted_seasons)

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return (path if path.is_absolute() else self.project_root / path).resolve()

    def _build_paths(self) -> PathsConfig:
        base = self._resolve(self._raw["base_data_path"])
        return PathsConfig(
            project_root=self.project_root,
            base_data=base,
            downloads=self._resolve(self._raw["downloads_dir"]),
            output=self._resolve(self._raw["output_dir"]),
            logs=self._resolve(self._raw["logs_dir"]),
            checkpoints=self._resolve(self._raw["checkpoints_dir"]),
            json=self._resolve(self._raw["json_dir"]),
            temp=self._resolve(self._raw["temp_dir"]),
            database=self._resolve(self._raw["database"]["path"]),
        )


class AppConfig(Config):
    """Validated composition of project settings and player inputs."""


Config = AppConfig
