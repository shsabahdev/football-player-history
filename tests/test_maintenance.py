"""Configuration separation, cleanup, and reset safety tests."""

from __future__ import annotations

import yaml

from football_history.checkpoint import Checkpoint
from football_history.config import AppConfig, ConfigError, PlayersConfig, ProjectConfig
from football_history.main import (
    _clean, _reset_crawl, _reset_player, _reset_project, _reset_resolution,
)
from football_history.models import CheckpointState


def _add_runtime_player(config, database, name: str):
    player = config.get_player(name)
    if player is None:
        player = config.add_player(name, team_name="Inter Miami")
    player_id = database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    return player, player_id


def test_project_and_players_configs_load_separately(config) -> None:
    assert isinstance(config.project_config, ProjectConfig)
    assert isinstance(config.players_config, PlayersConfig)
    assert isinstance(config, AppConfig)
    assert config.project_config_path.name == "project.yaml"
    assert config.config_path.name == "players.yaml"
    assert config.paths.downloads == config.project_root / "data" / "downloads"
    assert config.paths.database == config.project_root / "db" / "football_history.sqlite3"
    assert config.paths.logs.is_dir()


def test_invalid_player_configuration_reports_filename(project_root) -> None:
    path = project_root / "config" / "players.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["players"]["to_crawl"][0].pop("crawl_from_season")
    path.write_text(yaml.safe_dump(value), encoding="utf-8")
    try:
        AppConfig(project_root)
    except ConfigError as exc:
        assert "crawl_from_season" in str(exc)
    else:
        raise AssertionError("Invalid players.yaml was accepted.")


def test_player_input_updates_never_write_runtime_fields(config) -> None:
    player = config.add_player("Temporary Player")
    config.update_player(player.name, {"current_team_name": "Inter Miami"})
    assert config.get_player(player.name).current_team_name == "Inter Miami"
    try:
        config.update_player(player.name, {"current_page": 4})
    except ConfigError as exc:
        assert "Runtime" in str(exc)
    else:
        raise AssertionError("Runtime fields were accepted as player inputs.")
    assert "current_page" not in config.config_path.read_text(encoding="utf-8")


def test_player_can_be_removed_and_completed_player_is_not_crawled(config) -> None:
    player = config.add_player("Temporary Player")
    config.mark_player_completed(player.name)
    assert config.get_player(player.name) is None
    assert config.list_completed_players()[0]["name"] == player.name
    assert player.name not in {item.name for item in config.list_pending_players()}
    config.remove_player(player.name)
    assert all(item["name"] != player.name for item in config.list_completed_players())


def test_clean_temp_removes_only_temp_and_preserves_gitkeep(config, database) -> None:
    temporary = config.paths.temp / "scratch.tmp"
    unrelated = config.paths.json / "keep.json"
    gitkeep = config.paths.temp / ".gitkeep"
    temporary.write_text("temp", encoding="utf-8")
    unrelated.write_text("json", encoding="utf-8")
    gitkeep.write_text("", encoding="utf-8")

    assert _clean(config, database, ["temp"], yes=True)
    assert not temporary.exists()
    assert unrelated.is_file()
    assert gitkeep.is_file()
    assert config.paths.temp.is_dir()


def test_clean_json_removes_only_raw_json_files(config, database) -> None:
    raw_json = config.paths.json / "player" / "response.json"
    unrelated = config.paths.json / "player" / "notes.txt"
    checkpoint = config.paths.checkpoints / "player.checkpoint.json"
    raw_json.parent.mkdir(parents=True)
    raw_json.write_text("{}", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    checkpoint.write_text("{}", encoding="utf-8")

    _clean(config, database, ["json"], yes=True)

    assert not raw_json.exists()
    assert unrelated.is_file()
    assert checkpoint.exists()


def test_clean_checkpoints_removes_only_checkpoint_json(config, database) -> None:
    checkpoint = config.paths.checkpoints / "player.checkpoint.json"
    unrelated = config.paths.checkpoints / "notes.txt"
    checkpoint.write_text("{}", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    _clean(config, database, ["checkpoints"], yes=True)

    assert not checkpoint.exists()
    assert unrelated.is_file()


def test_clean_database_clears_rows_and_preserves_schema(config, database) -> None:
    _add_runtime_player(config, database, "Cristiano Ronaldo")
    assert database.table_count("players") == 1

    assert _clean(config, database, ["database"], yes=True)

    assert database.table_count("players") == 0
    with database.connect() as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='players'"
        ).fetchone() is not None


def test_clean_drop_database_removes_database_and_sqlite_sidecars(
    config, database
) -> None:
    database_path = config.paths.database
    wal_path = database_path.with_name(f"{database_path.name}-wal")
    shm_path = database_path.with_name(f"{database_path.name}-shm")
    wal_path.write_text("", encoding="utf-8")
    shm_path.write_text("", encoding="utf-8")

    assert _clean(
        config, database, ["database"], yes=True, drop_database=True
    )

    assert not database_path.exists()
    assert not wal_path.exists()
    assert not shm_path.exists()
    assert database_path.parent.is_dir()


def test_clean_all_requires_confirmation(config, database, monkeypatch) -> None:
    scratch = config.paths.temp / "must-remain.tmp"
    scratch.write_text("keep", encoding="utf-8")
    monkeypatch.setattr("football_history.main.console.input", lambda _: "no")

    assert not _clean(
        config, database,
        ["all", "temp", "downloads", "json", "checkpoints", "output", "database"],
    )
    assert scratch.is_file()


def test_reset_player_clears_one_players_state(config, database) -> None:
    player, player_id = _add_runtime_player(config, database, "Cristiano Ronaldo")
    other, other_id = _add_runtime_player(config, database, "Temporary Player")
    database.upsert_season(player_id, 2022, status="completed")
    database.upsert_season(other_id, 2022, status="completed")
    database.upsert_player_candidate({
        "player_config_name": player.name, "provider": player.provider,
        "provider_player_id": "999", "season": 2022, "matched_name": "Other",
        "name_similarity": 0.1,
    })
    _reset_player(
        config, database, player.name, delete_data=False,
        keep_candidates=False, remove_from_config=False, yes=True,
    )
    assert database.get_player(player.name)["provider_player_id"] is None
    assert database.get_season_statuses(player_id)[2022] == "pending"
    assert database.get_season_statuses(other_id)[2022] == "completed"
    assert database.list_player_candidates(player.name) == []
    assert config.get_player(player.name).resolution_status == "pending"


def test_reset_resolution_preserves_crawl_data(config, database) -> None:
    player, player_id = _add_runtime_player(config, database, "Cristiano Ronaldo")
    database.upsert_season(player_id, 2022, status="completed")
    database.upsert_player_candidate({
        "player_config_name": player.name, "provider": player.provider,
        "provider_player_id": "999", "season": 2022, "matched_name": "Other",
        "name_similarity": 0.1,
    })
    _reset_resolution(config, database, player.name, delete_data=False, yes=True)
    assert database.get_season_statuses(player_id)[2022] == "completed"
    assert database.list_player_candidates(player.name) == []
    assert config.get_player(player.name).resolution_status == "pending"


def test_reset_resolution_delete_data_removes_player_artifacts(config, database) -> None:
    player, player_id = _add_runtime_player(config, database, "Cristiano Ronaldo")
    raw = config.paths.json / "cristiano_ronaldo" / "response.json"
    download = config.paths.downloads / "cristiano_ronaldo" / "response.json"
    raw.parent.mkdir(parents=True)
    download.parent.mkdir(parents=True)
    raw.write_text("{}", encoding="utf-8")
    download.write_text("{}", encoding="utf-8")

    _reset_resolution(
        config, database, player.name, delete_data=True, yes=True
    )

    assert database.get_player(player.name) is None
    assert not raw.exists()
    assert not download.exists()
    assert config.get_player(player.name).resolution_status == "pending"


def test_reset_crawl_preserves_player_identity(config, database) -> None:
    player, player_id = _add_runtime_player(config, database, "Cristiano Ronaldo")
    database.upsert_season(player_id, 2022, status="completed")
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(
            player_name=player.name, provider_player_id="874",
            current_season=2022, completed_seasons=[2022],
            resolution_stage="resolved",
        )
    )
    _reset_crawl(config, database, player.name)
    assert database.get_player(player.name)["provider_player_id"] == "874"
    assert database.get_season_statuses(player_id)[2022] == "pending"
    state = Checkpoint(config.paths.checkpoints, database, player_id).load(player.name)
    assert state.current_season is None
    assert state.current_stage == "discover_seasons"
    assert state.resolution_stage == "resolved"


def test_reset_project_restores_defaults_and_protects_configuration(
    config, database
) -> None:
    config_path = config.project_config_path
    defaults_path = config.project_root / "config" / "default_players.yaml"
    env_path = config.project_root / ".env"
    source_file = config.project_root / "src" / "sentinel.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("protected", encoding="utf-8")
    env_contents = env_path.read_text(encoding="utf-8")

    _reset_project(config, database, yes=True)

    assert [player.name for player in config.players] == ["Cristiano Ronaldo"]
    assert config_path.is_file()
    assert defaults_path.is_file()
    assert env_path.read_text(encoding="utf-8") == env_contents
    assert source_file.read_text(encoding="utf-8") == "protected"
    assert database.table_count("players") == 0
