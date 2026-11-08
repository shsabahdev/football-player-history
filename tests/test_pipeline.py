"""Pipeline behavior tests using a local provider."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from football_history.api_client import DailyLimitReached
from football_history.checkpoint import Checkpoint
from football_history.models import CheckpointState
from football_history.pipeline import Pipeline
from football_history.providers.base import PlayerDataProvider


class EmptyProvider(PlayerDataProvider):
    def resolve_player(self, name, provider_player_id, season=None, *, team_id=None, league_id=None):
        return {"name": name, "provider_player_id": provider_player_id or "999"}

    def get_available_seasons(self, provider_player_id):
        return [2022]

    def get_player_team_seasons(self, provider_player_id, season):
        return []

    def get_team_fixtures(self, team_id, season):
        return []

    def get_team_fixtures_page(self, team_id, season, page):
        return [], page, page

    def get_fixture_player_participation(self, fixture_id, provider_player_id):
        return False

    def get_fixture_details(self, fixture_id):
        return {}

    def get_fixture_events(self, fixture_id):
        return []

    def get_fixture_lineups(self, fixture_id):
        return []

    def get_fixture_player_stats(self, fixture_id):
        return []


class MatchProvider(EmptyProvider):
    def __init__(self, fail_lineups_once=False):
        self.lineup_calls = 0
        self.fail_lineups_once = fail_lineups_once
        self.fixture_detail_calls = 0
        self.event_calls = 0

    def get_player_team_seasons(self, provider_player_id, season):
        return [{"id": 1, "name": "Home"}]

    def get_team_fixtures(self, team_id, season):
        return [{"fixture": {"id": 100}, "league": {"id": 39, "name": "League"}}]

    def get_team_fixtures_page(self, team_id, season, page):
        return self.get_team_fixtures(team_id, season), page, page

    def get_fixture_player_participation(self, fixture_id, provider_player_id):
        return True

    def get_fixture_details(self, fixture_id):
        self.fixture_detail_calls += 1
        return {
            "fixture": {"id": 100, "date": "2023-01-01", "status": {"short": "FT"}},
            "league": {"id": 39, "name": "League"},
            "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
            "goals": {"home": 1, "away": 0},
        }

    def get_fixture_events(self, fixture_id):
        self.event_calls += 1
        return [{"time": {"elapsed": 10}, "type": "Goal",
                 "player": {"id": 999, "name": "Example Player"}}]

    def get_fixture_lineups(self, fixture_id):
        self.lineup_calls += 1
        if self.fail_lineups_once and self.lineup_calls == 1:
            raise RuntimeError("simulated interruption")
        return [{
            "team": {"id": 1, "name": "Home"}, "formation": "4-3-3",
            "startXI": [{"player": {"id": 999, "name": "Example Player", "number": 9}}],
        }]

    def get_fixture_player_stats(self, fixture_id):
        return [{
            "team": {"id": 1, "name": "Home"},
            "players": [{
                "player": {"id": 999, "name": "Example Player"},
                "statistics": [{"games": {"minutes": 90}, "goals": {"total": 1}}],
            }],
        }]


class NeverCalledProvider(EmptyProvider):
    def _unexpected(self):
        raise AssertionError("Provider should not be called for completed work.")

    resolve_player = _unexpected
    get_available_seasons = _unexpected
    get_player_team_seasons = _unexpected
    get_team_fixtures = _unexpected
    get_team_fixtures_page = _unexpected
    get_fixture_player_participation = _unexpected
    get_fixture_details = _unexpected
    get_fixture_events = _unexpected
    get_fixture_lineups = _unexpected
    get_fixture_player_stats = _unexpected


class SubscriptionRangeProvider(EmptyProvider):
    def __init__(self):
        self.team_lookup_seasons = []

    def get_available_seasons(self, provider_player_id):
        return [2003, 2022, 2024]

    def get_supported_season_range(self, provider_player_id):
        return 2022, 2024

    def get_player_team_seasons(self, provider_player_id, season):
        self.team_lookup_seasons.append(season)
        return []


def test_pipeline_skips_season_without_team_data(config, database) -> None:
    player_name = config.players[0].name
    config._raw["players"]["to_crawl"][0]["crawl_from_season"] = 2022
    config._raw["players"]["to_crawl"][0]["crawl_to_season"] = 2022
    config._raw["api"]["requests_per_day"] = 10
    summary = Pipeline(config, database=database, provider=EmptyProvider()).run()
    assert summary["seasons_completed"] == 0
    row = database.get_player("Cristiano Ronaldo")
    assert row["status"] == "completed"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT status FROM seasons WHERE season=2022"
        ).fetchone()[0] == "no_team_data"
    assert config.completed_players[0]["name"] == "Cristiano Ronaldo"
    state = Checkpoint(
        config.paths.checkpoints, database, int(row["id"])
    ).load(player_name)
    assert state.season_status == "no_team_data"


def test_pipeline_skips_unsupported_seasons_and_resumes_without_refetch(
    config, database
) -> None:
    player = config.players[0]
    config.update_player_progress(player.name, crawl_from_season=2022, crawl_to_season=2024)
    provider = SubscriptionRangeProvider()
    first = Pipeline(config, database=database, provider=provider).run()
    assert first["errors"] == []
    assert provider.team_lookup_seasons == [2022, 2024]
    player_id = int(database.get_player(player.name)["id"])
    statuses = database.get_season_statuses(player_id)
    assert statuses[2003] == "unsupported_for_subscription"
    assert statuses[2022] == "no_team_data"
    assert statuses[2024] == "no_team_data"
    state = Checkpoint(config.paths.checkpoints, database, player_id).load(player.name)
    assert (state.supported_season_min, state.supported_season_max) == (2022, 2024)
    assert 2003 in state.unsupported_seasons

    config.add_player_to_crawl(config.completed_players[0])
    second = Pipeline(config, database=database, provider=provider).run(resume=True)
    assert second["errors"] == []
    assert provider.team_lookup_seasons == [2022, 2024]


def test_pipeline_resumes_at_last_incomplete_fixture_stage(config, database) -> None:
    player = config.players[0]
    config.update_player_progress(
        player.name, crawl_from_season=2022, crawl_to_season=2022
    )
    provider = MatchProvider(fail_lineups_once=True)
    first = Pipeline(config, database=database, provider=provider).run()
    assert first["errors"]
    player_id = database.get_player(player.name)["id"]
    state = Checkpoint(config.paths.checkpoints, database, player_id).load(player.name)
    assert state.current_stage == "fetch_lineups"
    assert database.table_count("match_events") == 1

    second = Pipeline(config, database=database, provider=provider).run(resume=True)
    assert second["errors"] == []
    assert database.table_count("fixtures") == 1
    assert database.table_count("match_events") == 1
    assert database.table_count("lineups") == 1
    assert database.table_count("player_match_stats") == 1
    assert provider.fixture_detail_calls == 1
    assert provider.event_calls == 1


def test_pipeline_deduplicates_fixtures_across_player_teams(config, database) -> None:
    config.update_player_progress(
        config.players[0].name, crawl_from_season=2022, crawl_to_season=2022
    )

    class MultiTeamProvider(MatchProvider):
        def get_player_team_seasons(self, provider_player_id, season):
            return [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]

    provider = MultiTeamProvider()
    summary = Pipeline(config, database=database, provider=provider).run()

    assert summary["errors"] == []
    assert summary["matches"] == 1
    assert database.table_count("fixtures") == 1
    assert database.table_count("player_fixtures") == 1
    assert database.table_count("player_fixture_teams") == 2
    assert provider.fixture_detail_calls == 1


def test_pipeline_excludes_fixtures_without_confirmed_player_participation(
    config, database
) -> None:
    config.update_player_progress(
        config.players[0].name, crawl_from_season=2022, crawl_to_season=2022
    )

    class NonParticipantProvider(MatchProvider):
        def get_fixture_player_participation(self, fixture_id, provider_player_id):
            return False

    provider = NonParticipantProvider()
    player_name = config.players[0].name
    summary = Pipeline(config, database=database, provider=provider).run()

    assert summary["errors"] == []
    assert summary["matches"] == 0
    assert database.table_count("fixtures") == 1
    assert database.table_count("player_fixtures") == 1
    assert database.list_confirmed_player_fixtures(
        int(database.get_player(player_name)["id"])
    ) == []
    assert provider.fixture_detail_calls == 0
    assert database.table_count("match_events") == 0


def test_pipeline_resumes_after_team_fixture_page_quota_pause(config, database) -> None:
    config.update_player_progress(
        config.players[0].name, crawl_from_season=2022, crawl_to_season=2022
    )

    class PaginatedProvider(EmptyProvider):
        def __init__(self):
            self.team_lookups = 0
            self.page_attempts = []
            self.successful_pages = []
            self.pause_once = True

        def get_player_team_seasons(self, provider_player_id, season):
            self.team_lookups += 1
            return [{"id": 57, "name": "Team"}]

        def get_team_fixtures_page(self, team_id, season, page):
            self.page_attempts.append(page)
            if page == 2 and self.pause_once:
                self.pause_once = False
                raise DailyLimitReached("quota")
            self.successful_pages.append(page)
            return ([{"fixture": {"id": 100 + page}}], page, 2)

    provider = PaginatedProvider()
    first = Pipeline(config, database=database, provider=provider).run()
    assert first["paused"] is True
    state = Checkpoint(
        config.paths.checkpoints, database, int(database.get_player(
            config.players[0].name
        )["id"])
    ).load(config.players[0].name)
    assert state.current_stage == "discover_team_fixtures"
    assert state.current_team_id == "57"
    assert state.current_fixture_page == 2
    assert state.discovered_fixture_ids == ["101"]

    second = Pipeline(config, database=database, provider=provider).run(resume=True)
    assert second["errors"] == []
    assert provider.team_lookups == 1
    assert provider.successful_pages == [1, 2]
    assert provider.page_attempts == [1, 2, 2]


def test_pipeline_resumes_after_participation_check_without_rechecking(
    config, database
) -> None:
    config.update_player_progress(
        config.players[0].name, crawl_from_season=2022, crawl_to_season=2022
    )

    class PausingProvider(MatchProvider):
        def __init__(self):
            super().__init__()
            self.participation_calls = 0
            self.stats_calls = 0
            self.pause_details_once = True

        def get_fixture_player_participation(self, fixture_id, provider_player_id):
            self.participation_calls += 1
            return True

        def get_fixture_player_stats(self, fixture_id):
            self.stats_calls += 1
            return super().get_fixture_player_stats(fixture_id)

        def get_fixture_details(self, fixture_id):
            if self.pause_details_once:
                self.pause_details_once = False
                raise DailyLimitReached("quota")
            return super().get_fixture_details(fixture_id)

    provider = PausingProvider()
    first = Pipeline(config, database=database, provider=provider).run()
    assert first["paused"] is True
    state = Checkpoint(
        config.paths.checkpoints,
        database,
        int(database.get_player(config.players[0].name)["id"]),
    ).load(config.players[0].name)
    assert state.active_fixture_player_stats_cached is True
    assert state.participation_checked_fixture_ids == ["100"]

    second = Pipeline(config, database=database, provider=provider).run(resume=True)
    assert second["errors"] == []
    assert provider.participation_calls == 1
    assert provider.stats_calls == 1
    assert provider.fixture_detail_calls == 1


def test_pipeline_skips_a_completed_player(config, database) -> None:
    config.add_or_update_completed_player(config.players[0])
    summary = Pipeline(config, database=database, provider=MatchProvider()).run()
    assert summary["players_completed"] == 0
    assert database.table_count("players") == 0


def test_pipeline_skips_completed_seasons(config, database) -> None:
    player = config.players[0]
    player_id = database.upsert_player({
        "provider_player_id": player.provider_player_id, "name": player.name,
        "crawl_from_season": 2022, "crawl_to_season": 2022,
    })
    database.upsert_season(player_id, 2022, status="completed")
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(
            player_name=player.name, provider_player_id=player.provider_player_id,
            current_stage="complete_player", seasons_to_crawl=[2022], completed_seasons=[2022],
            supported_season_min=2022, supported_season_max=2024,
        )
    )
    summary = Pipeline(config, database=database, provider=NeverCalledProvider()).run(resume=True)
    assert summary["seasons_completed"] == 1
    assert config.completed_players[0]["name"] == player.name


def test_pipeline_skips_completed_fixtures(config, database) -> None:
    player = config.players[0]
    player_id = database.upsert_player({
        "provider_player_id": player.provider_player_id, "name": player.name,
        "crawl_from_season": 2022, "crawl_to_season": 2022,
    })
    fixture_db_id = database.upsert_fixture({
        "provider_fixture_id": "100", "player_id": player_id, "season": 2022,
    })
    database.record_player_fixture(
        player_id=player_id,
        fixture_id=fixture_db_id,
        provider_player_id=player.provider_player_id,
        provider_fixture_id="100",
        season=2022,
        provider_team_id="1",
        provider_team_name="Home",
        raw_json_path=None,
    )
    database.set_player_fixture_participation(player_id, "100", True)
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(
            player_name=player.name, provider_player_id=player.provider_player_id,
            current_season=2022, current_stage="fetch_fixture", seasons_to_crawl=[2022],
            fixture_ids=["100"], completed_fixture_ids=["100"],
            season_fixture_state={
                "2022": {
                    "fixture_discovery_completed": True,
                    "discovered_fixture_ids": ["100"],
                    "processed_fixture_ids": ["100"],
                    "participation_checked_fixture_ids": ["100"],
                    "player_participated_fixture_ids": ["100"],
                }
            },
            total_fixtures_found=1, total_fixtures_processed=1,
        )
    )
    summary = Pipeline(config, database=database, provider=NeverCalledProvider()).run(resume=True)
    assert summary["matches"] == 1
    assert database.table_count("fixtures") == 1


def test_daily_budget_pauses_before_fixture_details_and_resumes_without_rediscovery(
    config, database, requests_mock
) -> None:
    player = config.players[0]
    config.update_player_progress(player.name, crawl_from_season=2022, crawl_to_season=2022)
    config.api = replace(
        config.api, requests_per_day=7, daily_request_safety_margin=1, max_retries=0
    )
    config.crawling = replace(
        config.crawling, delay_between_requests_seconds=0, save_raw_json=False
    )
    base = config.api.base_url
    no_team = {"response": [{"player": {"id": 874, "name": player.name}}]}
    player_team = {
        "response": [{
            "player": {"id": 874, "name": player.name},
            "statistics": [{"team": {"id": 1, "name": "Home"}}],
        }]
    }
    requests_mock.get(
        f"{base}/players",
        [
            {"json": no_team},
            {"json": no_team},
            {"json": player_team},
        ],
    )
    requests_mock.get(f"{base}/players/seasons", json={"response": [2022]})
    fixture_metadata = {
        "fixture": {"id": 1001, "date": "2022-01-01", "status": {"short": "FT"}},
        "league": {"id": 39, "name": "League"},
        "teams": {"home": {"id": 1, "name": "Home"}, "away": {"id": 2, "name": "Away"}},
        "goals": {"home": 1, "away": 0},
    }
    requests_mock.get(
        f"{base}/fixtures?team=1&season=2022",
        json={"response": [fixture_metadata]},
    )
    requests_mock.get(
        f"{base}/fixtures?id=1001", json={"response": [fixture_metadata]}
    )
    player_fixture_stats = {
        "response": [{
            "team": {"id": 1, "name": "Home"},
            "players": [{
                "player": {"id": 874, "name": player.name},
                "statistics": [{"games": {"minutes": 90}}],
            }],
        }]
    }
    requests_mock.get(
        f"{base}/fixtures/players?fixture=1001",
        json=player_fixture_stats,
    )

    first_summary = Pipeline(config, database=database).run()
    assert first_summary["paused"] is True
    assert first_summary["errors"] == []
    assert "requests used" in first_summary["pause_message"]
    assert "fetch_fixture" in first_summary["pause_message"]
    assert "resets at" in first_summary["pause_message"]
    player_row = database.get_player(player.name)
    assert player_row["status"] == "paused"
    today = datetime.now(timezone.utc).date().isoformat()
    assert database.get_daily_request_usage(today) == 6
    assert database.get_player_daily_request_usage(today) == [(player.name, 6)]
    with database.connect() as connection:
        request_rows = connection.execute(
            """SELECT player_id,provider_player_id,season,fixture_id,stage,team_id,
            page_number FROM request_log
            ORDER BY id"""
        ).fetchall()
    assert len(request_rows) == 6
    assert all(row["player_id"] == player_row["id"] for row in request_rows)
    assert all(row["provider_player_id"] == "874" for row in request_rows)
    assert {row["stage"] for row in request_rows} == {
        "resolve_player", "discover_seasons", "validate_season_range",
        "discover_player_teams", "discover_team_fixtures", "verify_participation",
    }
    team_request = next(row for row in request_rows if row["stage"] == "discover_team_fixtures")
    assert (team_request["team_id"], team_request["page_number"]) == ("1", 1)
    participation_request = next(
        row for row in request_rows if row["stage"] == "verify_participation"
    )
    assert (
        participation_request["team_id"], participation_request["fixture_id"]
    ) == ("1", "1001")
    state = Checkpoint(config.paths.checkpoints, database, int(player_row["id"])).load(player.name)
    assert state.status == "paused"
    assert state.current_stage == "fetch_fixture"
    assert state.active_fixture_id == "1001"
    with database.connect() as connection:
        assert connection.execute("SELECT status FROM seasons").fetchone()[0] == "paused"
        assert connection.execute(
            "SELECT status FROM crawl_progress WHERE stage='fetch_fixture'"
        ).fetchone()[0] == "paused"

    config.api = replace(config.api, requests_per_day=10)
    requests_mock.get(
        f"{base}/fixtures/events", json={"response": []}
    )
    requests_mock.get(
        f"{base}/fixtures/lineups", json={"response": []}
    )
    requests_mock.get(
        f"{base}/fixtures/players", json={"response": []}
    )
    second_summary = Pipeline(config, database=database).run(resume=True)
    assert second_summary["paused"] is False
    assert second_summary["errors"] == []
    fixture_requests = [
        request for request in requests_mock.request_history
        if request.path_url.startswith("/fixtures?")
    ]
    assert len(fixture_requests) == 2
    fixture_queries = [request.qs for request in fixture_requests]
    assert {"team": ["1"], "season": ["2022"]} in fixture_queries
    assert {"id": ["1001"]} in fixture_queries
    assert all("player" not in query for query in fixture_queries)
    participation_requests = [
        request for request in requests_mock.request_history
        if request.path_url.startswith("/fixtures/players?")
    ]
    assert len(participation_requests) == 1
    assert database.table_count("fixtures") == 1
