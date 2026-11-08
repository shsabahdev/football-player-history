"""Shared project fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from football_history.config import Config
from football_history.database import Database


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Create an isolated project directory with valid default configuration."""
    source = Path(__file__).parents[1]
    shutil.copytree(source / "config", tmp_path / "config")
    config_path = tmp_path / "config" / "players.yaml"
    settings = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    settings["players"]["to_crawl"] = [
        player for player in settings["players"]["to_crawl"]
        if player.get("name") != "Lionel Messi"
    ]
    config_path.write_text(
        yaml.safe_dump(settings, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / ".env").write_text("API_FOOTBALL_KEY=test-key\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def config(project_root: Path) -> Config:
    """Return validated isolated settings."""
    return Config(project_root)


@pytest.fixture
def database(config: Config) -> Database:
    """Return an initialized isolated database."""
    return Database(config.paths.database)
