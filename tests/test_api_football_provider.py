"""Provider and API client tests with mocked HTTP responses."""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path

import pytest
import requests

from football_history.api_client import APIClient, DailyLimitReached
from football_history.models import CheckpointState
from football_history.providers.api_football import APIFootballProvider


class StubClient:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append((endpoint, params))
        return self.payloads[endpoint]


def response_fixture(name):
    # Hand-authored payloads use synthetic test-only IDs, not verified provider identities.
    return json.loads((Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8"))


TEST_PLAYER_NAME = "Aster Vale"
TEST_PLAYER_ID = "900001"
TEST_TEAM_ID = "900011"
TEST_FIXTURE_ID = "900101"


def test_provider_parses_player_seasons_fixtures_and_detail_shapes() -> None:
    client = StubClient({
        "players": response_fixture("player.json"),
        "players/seasons": response_fixture("seasons.json"),
        "fixtures": response_fixture("fixture.json"),
        "fixtures/events": response_fixture("events.json"),
        "fixtures/lineups": response_fixture("lineups.json"),
        "fixtures/players": response_fixture("player_stats.json"),
    })
    provider = APIFootballProvider(client)
    assert provider.resolve_player(
        TEST_PLAYER_NAME, TEST_PLAYER_ID, season=2098
    )["provider_player_id"] == TEST_PLAYER_ID
    assert client.calls[0] == ("players", {"id": TEST_PLAYER_ID, "season": 2098})
    assert provider.get_available_seasons(TEST_PLAYER_ID) == [2098, 2099]
    assert (
        provider.get_team_fixtures(TEST_TEAM_ID, 2099)[0]["fixture"]["id"]
        == int(TEST_FIXTURE_ID)
    )
    assert (
        provider.get_fixture_details(TEST_FIXTURE_ID)["fixture"]["id"]
        == int(TEST_FIXTURE_ID)
    )
    assert provider.get_fixture_events(TEST_FIXTURE_ID)[0]["type"] == "Goal"
    assert (
        provider.get_fixture_lineups(TEST_FIXTURE_ID)[0]["team"]["id"]
        == int(TEST_TEAM_ID)
    )
    assert provider.get_fixture_player_participation(TEST_FIXTURE_ID, TEST_PLAYER_ID) is True
    assert (
        provider.get_fixture_player_stats(TEST_FIXTURE_ID)[0]["players"][0]["player"]["id"]
        == int(TEST_PLAYER_ID)
    )


def test_configured_player_id_never_uses_search() -> None:
    client = StubClient({"players": response_fixture("player.json")})
    player = APIFootballProvider(client).resolve_player(
        TEST_PLAYER_NAME, TEST_PLAYER_ID, season=2003
    )
    assert player["provider_player_id"] == TEST_PLAYER_ID
    assert client.calls == [("players", {"id": TEST_PLAYER_ID, "season": 2003})]


def test_missing_or_malformed_player_id_has_actionable_error() -> None:
    client = StubClient({"players": response_fixture("player.json")})
    provider = APIFootballProvider(client)
    with pytest.raises(ValueError, match="Set provider_player_id"):
        provider.resolve_player("Cristiano Ronaldo", None)
    with pytest.raises(ValueError, match="positive numeric"):
        provider.resolve_player("Cristiano Ronaldo", "not-an-id", season=2003)
    assert client.calls == []


def test_provider_rejects_response_with_wrong_configured_id() -> None:
    client = StubClient({"players": {"response": [{"player": {"id": 999, "name": "Other"}}]}})
    with pytest.raises(LookupError, match="no player matching configured ID 874"):
        APIFootballProvider(client).resolve_player("Cristiano Ronaldo", "874", season=2003)
    assert client.calls == [("players", {"id": "874", "season": 2003})]


def test_search_fallback_requires_and_uses_team_or_league_filter() -> None:
    client = StubClient({"players": response_fixture("player.json")})
    provider = APIFootballProvider(client)
    assert provider.resolve_player(
        TEST_PLAYER_NAME, None, league_id="39"
    )["provider_player_id"] == TEST_PLAYER_ID
    assert client.calls == [("players", {"search": TEST_PLAYER_NAME, "league": "39"})]


def test_provider_fetches_all_fixture_pages() -> None:
    class PagedClient:
        def __init__(self):
            self.pages = []

        def get(self, endpoint, params=None):
            page = params.get("page", 1)
            self.pages.append((endpoint, params))
            return {
                "response": [{"fixture": {"id": page}}],
                "paging": {"current": page, "total": 2},
            }

    client = PagedClient()
    fixtures = APIFootballProvider(client).get_team_fixtures("57", 2023)
    assert [item["fixture"]["id"] for item in fixtures] == [1, 2]
    assert client.pages == [
        ("fixtures", {"team": "57", "season": 2023}),
        ("fixtures", {"team": "57", "season": 2023, "page": 2}),
    ]


def test_provider_discovers_and_deduplicates_player_teams() -> None:
    class TeamClient(StubClient):
        def get(self, endpoint, params=None):
            self.calls.append((endpoint, params))
            return {
                "response": [{
                    "player": {"id": 874, "name": "Player"},
                    "statistics": [
                        {"team": {"id": 1, "name": "First"}},
                        {"team": {"id": 2, "name": "Second"}},
                        {"team": {"id": 1, "name": "First"}},
                    ],
                }]
            }

    client = TeamClient({})
    teams = APIFootballProvider(client).get_player_team_seasons("874", 2023)
    assert teams == [{"id": 1, "name": "First"}, {"id": 2, "name": "Second"}]
    assert client.calls == [("players", {"id": "874", "season": 2023})]


def test_provider_does_not_send_page_when_fixture_paging_is_absent() -> None:
    client = StubClient({"fixtures": {"response": [{"fixture": {"id": 1001}}]}})
    assert APIFootballProvider(client).get_team_fixtures("57", 2023) == [
        {"fixture": {"id": 1001}}
    ]
    assert client.calls == [("fixtures", {"team": "57", "season": 2023})]


def test_provider_confirms_only_matching_fixture_player() -> None:
    client = StubClient({
        "fixtures/players": {
            "response": [{
                "team": {"id": 1},
                "players": [{"player": {"id": 874, "name": "Player"}}],
            }]
        }
    })
    provider = APIFootballProvider(client)
    assert provider.get_fixture_player_participation("1001", "874")
    assert not provider.get_fixture_player_participation("1001", "999")
    assert client.calls == [
        ("fixtures/players", {"fixture": "1001"}),
        ("fixtures/players", {"fixture": "1001"}),
    ]


def test_provider_fetches_one_player_page_with_pagination_metadata() -> None:
    class PageClient:
        def __init__(self):
            self.calls = []

        def get(self, endpoint, params=None):
            self.calls.append((endpoint, params))
            return {
                "response": [{"player": {"id": 1000 + params["page"], "name": "Player"}}],
                "paging": {"current": params["page"], "total": 3},
            }

    client = PageClient()
    rows, current, total = APIFootballProvider(client).get_players_by_team_page(
        "57", 2022, 2
    )
    assert rows[0]["player"]["id"] == 1002
    assert (current, total) == (2, 3)
    assert client.calls == [
        ("players", {"team": "57", "season": 2022, "page": 2})
    ]


def test_provider_detects_subscription_range_from_plan_error() -> None:
    class RangeClient(StubClient):
        def get(self, endpoint, params=None):
            self.calls.append((endpoint, params))
            if endpoint == "players/seasons":
                return {"response": [2003, 2004, 2022, 2024]}
            return {
                "errors": {
                    "plan": "Free plans do not have access to this season, try from 2022 to 2024."
                },
                "response": [],
            }

    client = RangeClient({})
    provider = APIFootballProvider(client)
    assert provider.get_supported_season_range("874") == (2022, 2024)
    assert client.calls == [
        ("players/seasons", {"player": "874"}),
        ("players", {"id": "874", "season": 2003}),
    ]


def test_client_retries_transient_error(config, database, requests_mock) -> None:
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    config.api = replace(config.api, retry_backoff_seconds=0)
    state = CheckpointState(player_name="Test")
    requests_mock.get(
        "https://v3.football.api-sports.io/players/seasons",
        [{"status_code": 503, "json": {}}, {"status_code": 200, "json": {"response": [2023]}}],
    )
    client = APIClient(config, database, state, lambda _: None, session=requests.Session())
    assert client.get("players/seasons", {"player": "1"})["response"] == [2023]
    assert state.requests_used_today == 2
    assert database.table_count("request_log") == 2
    today = datetime.now(timezone.utc).date().isoformat()
    assert database.get_daily_request_usage(today) == 2
    assert database.get_player_daily_request_usage(today) == [("Test", 2)]


def test_client_stops_at_daily_limit(config, database) -> None:
    config.api = replace(config.api, requests_per_day=2, daily_request_safety_margin=1)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    today = datetime.now(timezone.utc).date().isoformat()
    assert database.reserve_daily_request(today, 1, "Test") == (1, 1)
    state = CheckpointState(
        player_name="Test", requests_used_today=1,
        request_date=today,
    )
    saved = []
    client = APIClient(config, database, state, lambda value: saved.append(value.to_dict()))
    with pytest.raises(DailyLimitReached):
        client.get("players/seasons")
    assert database.get_daily_request_usage(today) == 1
    assert saved[-1]["status"] == "paused"


def test_client_resets_checkpoint_usage_after_calendar_day_changes(
    config, database, requests_mock
) -> None:
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    requests_mock.get(
        "https://v3.football.api-sports.io/players/seasons",
        json={"response": [2023]},
    )
    state = CheckpointState(
        player_name="Cristiano Ronaldo",
        request_date="2000-01-01",
        requests_used_today=99,
    )
    yesterday = (datetime.now(timezone.utc).date()).fromordinal(
        datetime.now(timezone.utc).date().toordinal() - 1
    ).isoformat()
    assert database.reserve_daily_request(yesterday, 100, "Cristiano Ronaldo") == (1, 1)
    saved = []
    client = APIClient(config, database, state, lambda value: saved.append(value.to_dict()),
                       session=requests.Session())
    client.get("players/seasons", {"player": "874"})
    assert state.request_date == datetime.now(timezone.utc).date().isoformat()
    assert state.requests_used_today == 1
    assert saved
    assert database.get_player_daily_request_usage(state.request_date) == [
        ("Cristiano Ronaldo", 1)
    ]


def test_client_logs_request_attribution(config, database, requests_mock) -> None:
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    player_id = database.upsert_player({
        "provider_player_id": "874", "name": "Cristiano Ronaldo",
        "crawl_from_season": 2022, "crawl_to_season": 2024,
    })
    requests_mock.get(
        "https://v3.football.api-sports.io/fixtures/events",
        json={"response": []},
    )
    state = CheckpointState(
        player_name="Cristiano Ronaldo", provider_player_id="874", current_season=2024,
        current_stage="fetch_events", active_fixture_id="123",
    )
    client = APIClient(config, database, state, lambda _: None, session=requests.Session())
    client.set_request_context(
        player_id=player_id, provider_player_id="874", season=2024,
        fixture_id="123", stage="fetch_events",
    )
    client.get("fixtures/events", {"fixture": "123"})
    with database.connect() as connection:
        row = connection.execute(
            """SELECT player_id,provider_player_id,season,fixture_id,stage,endpoint,url,
            status_code,success,error_message,requested_at FROM request_log"""
        ).fetchone()
    assert row["player_id"] == player_id
    assert row["provider_player_id"] == "874"
    assert row["season"] == 2024
    assert row["fixture_id"] == "123"
    assert row["stage"] == "fetch_events"
    assert row["endpoint"] == "fixtures/events"
    assert "?" not in row["url"]
    assert row["status_code"] == 200
    assert row["success"] == 1
    assert row["requested_at"]
