"""SQLite schema and upsert tests."""

from __future__ import annotations
from datetime import datetime, timezone

def test_schema_and_fixture_upsert(database) -> None:
    player_id = database.upsert_player({
        "provider_player_id": "874", "name": "Cristiano Ronaldo",
        "crawl_from_season": 2003, "crawl_to_season": "current",
    })
    fixture = {
        "provider_fixture_id": "100", "player_id": player_id, "season": 2023,
        "competition_name": "League", "home_team_name": "Home", "away_team_name": "Away",
    }
    first = database.upsert_fixture(fixture)
    second = database.upsert_fixture({**fixture, "home_goals": 2})
    assert first == second
    assert database.table_count("fixtures") == 1
    with database.connect() as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert {"players", "seasons", "fixtures", "match_events", "lineups",
                "player_match_stats", "crawl_progress", "request_log",
                "player_checkpoints", "daily_request_usage",
                "daily_player_request_usage"} <= tables
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_daily_budget_reserves_global_and_per_player_usage(database) -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    assert database.reserve_daily_request(today, 2, "Ronaldo") == (1, 1)
    assert database.reserve_daily_request(today, 2, "Messi") == (2, 1)
    assert database.reserve_daily_request(today, 2, "Ronaldo") is None
    assert database.get_daily_request_usage(today) == 2
    assert database.get_player_daily_request_usage(today) == [("Messi", 1), ("Ronaldo", 1)]


def test_request_report_counts_attributed_and_unattributed_rows(database) -> None:
    player_id = database.upsert_player({
        "provider_player_id": "874", "name": "Cristiano Ronaldo",
        "crawl_from_season": 2022, "crawl_to_season": 2024,
    })
    today = datetime.now(timezone.utc).date().isoformat()
    database.reserve_daily_request(today, 80, "Cristiano Ronaldo")
    attributed_id = database.begin_request(
        "api_football", "fixtures/events", "https://example.test/fixtures/events",
        player_id=player_id, provider_player_id="874", season=2024, fixture_id="123",
        stage="fetch_events",
    )
    database.finish_request(attributed_id, 200, True, None)
    unattributed_id = database.begin_request(
        "api_football", "players", "https://example.test/players"
    )
    database.finish_request(unattributed_id, 200, True, None)

    report = database.get_request_report(today)
    assert report["total"] == 2
    assert report["attributed"] == 1
    assert report["unattributed"] == 1
    assert report["by_player"] == {"Cristiano Ronaldo (874)": 1}
    assert report["by_stage"] == {"fetch_events": 1, "unattributed": 1}
    assert report["by_endpoint"] == {"fixtures/events": 1, "players": 1}


def test_reset_progress_reopens_subscription_unsupported_seasons(database) -> None:
    player_id = database.upsert_player({
        "provider_player_id": "874", "name": "Cristiano Ronaldo",
        "crawl_from_season": 2003, "crawl_to_season": 2024,
    })
    database.mark_season_unsupported(player_id, 2003)
    database.delete_player_progress(player_id)
    assert database.get_season_statuses(player_id)[2003] == "pending"
