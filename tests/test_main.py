"""CLI request-budget reporting tests."""

from __future__ import annotations

from datetime import datetime, timezone

from football_history.checkpoint import Checkpoint
from football_history.main import (
    _show_requests, _show_resolution_status, _show_seasons, _status, build_parser,
)
from football_history.models import CheckpointState


def test_requests_command_is_registered() -> None:
    assert build_parser().parse_args(["requests"]).command == "requests"
    assert build_parser().parse_args([
        "seasons", "--player", "Cristiano Ronaldo"
    ]).command == "seasons"
    assert build_parser().parse_args([
        "add-player", "--name", "Lionel Messi"
    ]).command == "add-player"
    assert build_parser().parse_args([
        "resolution-status", "--name", "Lionel Messi"
    ]).command == "resolution-status"


def test_requests_command_reports_budget_and_player_usage(config, database, capsys) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    assert database.reserve_daily_request(today, 80, "Cristiano Ronaldo") == (1, 1)
    _show_requests(config, database)
    output = capsys.readouterr().out
    assert "Daily limit: 80" in output
    assert "Enforced limit: 80" in output
    assert "Used today: 1" in output
    assert "Remaining: 79" in output
    assert "Attributed: 1" in output
    assert "Cristiano Ronaldo (ID unknown): 1" in output
    assert "Budget resets at:" in output


def test_status_and_seasons_commands_report_cached_season_state(
    config, database, capsys
) -> None:
    player = config.players[0]
    player_id = database.upsert_player({
        "provider_player_id": player.provider_player_id, "name": player.name,
        "crawl_from_season": player.crawl_from_season, "crawl_to_season": player.crawl_to_season,
    })
    database.upsert_season(player_id, 2003, status="unsupported_for_subscription")
    database.upsert_season(player_id, 2022, status="completed")
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(
            player_name=player.name, provider_player_id=player.provider_player_id,
            supported_season_min=2022, supported_season_max=2024,
            unsupported_seasons=[2003], skipped_seasons=[2003],
            seasons_to_crawl=[2022, 2023, 2024], completed_seasons=[2022],
            current_season=2023, current_stage="fetch_events",
            total_fixtures_processed=2, total_fixtures_found=4,
        )
    )
    _status(config, database)
    status_output = capsys.readouterr().out
    assert "Provider player ID: 874" in status_output
    assert "Configured crawl period: 2022-2026" in status_output
    assert "Desired historical period: 2003-2026" in status_output
    assert "Supported subscription range: 2022-2024" in status_output
    assert "Skipped unsupported seasons: 2003" in status_output
    assert "Current stage: fetch_events" in status_output
    assert "Fixtures processed: 2/4" in status_output

    _show_seasons(config, database, player.name)
    seasons_output = capsys.readouterr().out
    assert "Configured seasons: 2022-2026" in seasons_output
    assert "Supported seasons: 2022-2024" in seasons_output
    assert "Unsupported seasons: 2003" in seasons_output
    assert "Completed seasons: 2022" in seasons_output
    assert "Pending seasons: 2023, 2024" in seasons_output


def test_resolution_status_reports_resume_page(config, database, capsys) -> None:
    player = config.add_player("Lionel Messi", team_name="Inter Miami")
    player_id = database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": None, "resolution_status": "pending",
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(
            player_name=player.name,
            current_season=2022,
            player_config_name=player.name,
            provider=player.provider,
            resolution_stage="resolving_players_page",
            current_team_name="Inter Miami",
            resolved_team_id="57",
            resolved_team_name="Inter Miami",
            resolution_season=2022,
            current_page=2,
            has_more_pages=True,
            last_processed_page=1,
        )
    )

    _show_resolution_status(config, database, player.name)
    output = capsys.readouterr().out
    assert "Resolution stage: resolving_players_page" in output
    assert "Resolved team: Inter Miami (57)" in output
    assert "Current page: 2" in output
    assert "Pages processed: 1" in output
    assert "Next action: fetch players page 2" in output
