"""Configuration reader tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from football_history.config import Config, ConfigError


def test_config_loads_and_resolves_paths(config: Config) -> None:
    assert config.project_root.is_absolute()
    assert config.get_player("cristiano ronaldo").provider_player_id == "874"
    assert config.get_player("cristiano ronaldo").desired_historical_from_season == 2003
    assert config.get_player("cristiano ronaldo").crawl_from_season == 2022
    assert config.paths.database == config.project_root / "db" / "football_history.sqlite3"
    assert config.paths.checkpoints.is_dir()
    assert config.paths.logs.is_dir()


def test_missing_api_key_is_clear(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project_root / ".env").unlink()
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    with pytest.raises(ConfigError, match="API_FOOTBALL_KEY"):
        _ = Config(project_root).api_key


def test_config_updates_save_atomically(config: Config) -> None:
    player = config.players[0]
    config.add_or_update_completed_player(player)
    reloaded = Config(config.project_root)
    assert reloaded.get_player(player.name) is None
    assert reloaded.completed_players[0]["name"] == player.name
    assert reloaded.completed_players[0]["desired_historical_from_season"] == 2003


def test_invalid_configuration_type_raises_clear_error(project_root: Path) -> None:
    config_file = project_root / "config" / "project.yaml"
    text = config_file.read_text(encoding="utf-8").replace("max_retries: 3", "max_retries: many")
    config_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="api.max_retries"):
        Config(project_root)


def test_safety_margin_cannot_exceed_daily_request_limit(project_root: Path) -> None:
    config_file = project_root / "config" / "project.yaml"
    text = config_file.read_text(encoding="utf-8").replace(
        "daily_request_safety_margin: 0", "daily_request_safety_margin: 7510"
    )
    config_file.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="cannot exceed api.requests_per_day"):
        Config(project_root)


def test_safety_margin_is_optional_and_defaults_to_zero(project_root: Path) -> None:
    config_file = project_root / "config" / "project.yaml"
    text = config_file.read_text(encoding="utf-8").replace(
        "  daily_request_safety_margin: 0\n", ""
    )
    config_file.write_text(text, encoding="utf-8")
    assert Config(project_root).api.daily_request_safety_margin == 0
