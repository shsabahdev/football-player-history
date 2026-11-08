"""Checkpoint persistence and recovery tests."""

from __future__ import annotations

from football_history.checkpoint import Checkpoint
from football_history.models import CheckpointState


def test_checkpoint_saves_loads_from_json_and_sqlite(config, database) -> None:
    player_id = database.upsert_player({"provider_player_id": "874", "name": "Cristiano Ronaldo"})
    checkpoint = Checkpoint(config.paths.checkpoints, database, player_id)
    state = CheckpointState(
        player_name="Cristiano Ronaldo", provider_player_id="874",
        current_season=2023, current_stage="fetch_lineups",
        completed_fixture_ids=["1"], active_fixture_id="2",
    )
    checkpoint.save(state)
    loaded = checkpoint.load("Cristiano Ronaldo")
    assert loaded.current_stage == "fetch_lineups"
    assert loaded.completed_fixture_ids == ["1"]
    assert database.load_checkpoint(player_id)["active_fixture_id"] == "2"


def test_checkpoint_recovers_from_database_when_json_missing(config, database) -> None:
    player_id = database.upsert_player({"provider_player_id": "874", "name": "Cristiano Ronaldo"})
    checkpoint = Checkpoint(config.paths.checkpoints, database, player_id)
    state = CheckpointState(player_name="Cristiano Ronaldo", completed_seasons=[2022])
    checkpoint.save(state)
    checkpoint.path_for(state.player_name).unlink()
    assert checkpoint.load(state.player_name).completed_seasons == [2022]
