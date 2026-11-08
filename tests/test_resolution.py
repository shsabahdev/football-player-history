"""Mocked tests for name-only player resolution."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from football_history.checkpoint import Checkpoint
from football_history.main import _add_player, _retry_resolution, _select_player
from football_history.models import CheckpointState
from football_history.pipeline import Pipeline
from football_history.resolution import PlayerResolver, name_similarity, normalize_name


def _team_payload(team_id: int = 57, name: str = "Inter Miami") -> dict:
    return {"response": [{"team": {"id": team_id, "name": name}}], "paging": {
        "current": 1, "total": 1,
    }}


def _players_payload(names: list[tuple[int, str]], team_id: int = 57) -> dict:
    return {
        "response": [
            {
                "player": {"id": player_id, "name": name},
                "statistics": [{"team": {"id": team_id, "name": "Inter Miami"}}],
            }
            for player_id, name in names
        ],
        "paging": {"current": 1, "total": 1},
    }


def _add_config_player(config, database, name="Lionel Messi", team="Inter Miami", season=2022):
    return config.add_player(name, team_name=team, resolution_season=season)


def _mock_resolution(config, requests_mock, names):
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    base = config.api.base_url
    requests_mock.get(f"{base}/teams", json=_team_payload())
    requests_mock.get(
        f"{base}/players",
        json=_players_payload(names),
    )


def _seed_previous_messi_attempt(config, database):
    """Create the saved 2022 checkpoint and candidate evidence from the live run."""
    player = config.add_player(
        "Lionel Messi", team_name="Inter Miami", resolution_season=2022
    )
    config.update_player_progress(
        player.name, resolution_status="pending",
        resolution_seasons=[2023, 2024],
        attempted_resolution_seasons=[2022],
        desired_historical_from_season=2004,
    )
    player = config.get_player(player.name)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": None, "resolution_status": "pending",
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    for candidate_id in range(1, 53):
        database.upsert_player_candidate({
            "player_config_name": player.name, "provider": player.provider,
            "provider_player_id": str(8000 + candidate_id),
            "provider_team_id": "9568", "provider_team_name": "Inter Miami",
            "season": 2022, "matched_name": f"Old candidate {candidate_id}",
            "name_similarity": 0.2, "status": "rejected",
            "source_page": (candidate_id % 3) + 1,
            "raw_json_path": f"data/json/lionel_messi/page-{candidate_id}.json",
        })
    player_row = database.get_player(player.name)
    Checkpoint(
        config.paths.checkpoints, database, int(player_row["id"])
    ).save(CheckpointState(
        player_name=player.name, player_config_name=player.name,
        provider=player.provider, resolution_season=2022, current_season=2022,
        resolved_team_id="9568", resolved_team_name="Inter Miami",
        current_team_name="Inter Miami", current_page=4,
        last_processed_page=3, has_more_pages=False,
        candidates_found=list(range(1, 53)), resolution_stage="not_found",
        status="not_found",
    ))
    return player


def test_add_player_name_only_and_with_team_is_local(config, database, capsys) -> None:
    _add_player(config, database, "Neymar")
    assert config.get_player("Neymar").resolution_status == "needs_team_or_league"
    _add_player(config, database, "Lionel Messi", "Inter Miami")
    assert config.get_player("Lionel Messi").resolution_status == "pending"
    assert database.get_player("Lionel Messi")["current_team_name"] == "Inter Miami"
    assert "2003 is historically desired" not in config.config_path.read_text(encoding="utf-8")
    assert database.get_daily_request_usage(datetime.now(timezone.utc).date().isoformat()) == 0
    assert "No API requests were made" in capsys.readouterr().out


def test_normalization_removes_accents_punctuation_and_whitespace() -> None:
    assert normalize_name("  Kylian   Mbappé! ") == "kylian mbappe"
    assert name_similarity("Kylian Mbappé", "kylian mbappe") == 1.0


def test_configured_provider_id_resolves_without_api_search(config, database) -> None:
    config.set_player_resolution(
        "Cristiano Ronaldo", "pending", provider_player_id="874"
    )
    player = config.get_player("Cristiano Ronaldo")
    assert player is not None

    assert PlayerResolver(config, database).resolve(player) == "resolved"
    assert database.table_count("request_log") == 0
    assert config.get_player(player.name).resolution_status == "resolved"


def test_team_resolution_exact_match_updates_config_database_and_checkpoint(
    config, database, requests_mock
) -> None:
    _mock_resolution(config, requests_mock, [(1001, "Lionel Messi")])
    player = _add_config_player(config, database)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    assert PlayerResolver(config, database).resolve(player) == "resolved"
    resolved = config.get_player(player.name)
    assert resolved.provider_player_id == "1001"
    assert resolved.resolution_status == "resolved"
    assert len(requests_mock.request_history) == 2
    assert requests_mock.request_history[0].qs == {"search": ["inter miami"]}
    assert database.get_resolution_page(
        player.name, player.provider, "57", 2022, 1
    ) is not None
    player_row = database.get_player(player.name)
    assert player_row["provider_player_id"] == "1001"
    assert player_row["resolution_status"] == "resolved"
    state = Checkpoint(config.paths.checkpoints, database, int(player_row["id"])).load(
        player.name
    )
    assert state.provider_player_id == "1001"
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT player_id,provider_player_id,season,stage,team_id,team_name,
            page_number,endpoint
            FROM request_log ORDER BY id"""
        ).fetchall()
    assert rows[0]["stage"] == "resolve_team"
    assert rows[0]["endpoint"] == "teams"
    assert rows[1]["provider_player_id"] is None
    assert rows[1]["season"] == 2022
    assert rows[1]["stage"] == "resolve_team_players"
    assert rows[1]["team_id"] == "57"
    assert rows[1]["team_name"] == "Inter Miami"
    assert rows[1]["page_number"] == 1


def test_league_resolution_attributes_league_and_season(config, database, requests_mock) -> None:
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    database.set_supported_season_range(config.api.provider, 2022, 2024)
    player = config.add_player("Lionel Messi", league_id="39", resolution_season=2022)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    requests_mock.get(
        f"{config.api.base_url}/players",
        json=_players_payload([(1001, "Lionel Messi")]),
    )

    assert PlayerResolver(config, database).resolve(player) == "resolved"
    with database.connect() as connection:
        row = connection.execute(
            "SELECT player_id,season,stage,team_id,league_id,endpoint FROM request_log"
        ).fetchone()
    assert row["player_id"] is not None
    assert row["season"] == 2024
    assert row["stage"] == "resolve_league_players"
    assert row["team_id"] is None
    assert row["league_id"] == "39"
    assert row["endpoint"] == "players"


def test_exact_match_stops_player_pagination(config, database, requests_mock) -> None:
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    requests_mock.get(f"{config.api.base_url}/teams", json=_team_payload())
    requests_mock.get(
        f"{config.api.base_url}/players?team=57&season=2022&page=1",
        json={
            **_players_payload([(1001, "Lionel Messi")]),
            "paging": {"current": 1, "total": 3},
        },
    )
    player = _add_config_player(config, database)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })

    assert PlayerResolver(config, database).resolve(player) == "resolved"
    assert len(requests_mock.request_history) == 2
    assert requests_mock.request_history[-1].qs["page"] == ["1"]


def test_multiple_exact_matches_require_manual_review(config, database, requests_mock) -> None:
    _mock_resolution(config, requests_mock, [(1001, "Lionel Messi"), (1002, "Lionel Messi")])
    player = _add_config_player(config, database)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    assert PlayerResolver(config, database).resolve(player) == "needs_review"
    assert config.get_player(player.name).provider_player_id is None
    candidates = database.list_player_candidates(player.name)
    assert len(candidates) == 2
    assert {row["status"] for row in candidates} == {"ambiguous"}

    _select_player(config, database, player.name, int(candidates[0]["id"]))
    assert config.get_player(player.name).resolution_status == "resolved"
    assert config.get_player(player.name).provider_player_id in {"1001", "1002"}


def test_no_match_is_cached_and_not_retried_automatically(
    config, database, requests_mock
) -> None:
    _mock_resolution(config, requests_mock, [(1001, "Different Player")])
    player = _add_config_player(config, database)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    resolver = PlayerResolver(config, database)
    assert resolver.resolve(player) == "not_found"
    first_request_count = len(requests_mock.request_history)
    assert config.get_player(player.name).resolution_status == "not_found"
    assert resolver.resolve(config.get_player(player.name)) == "not_found"
    assert len(requests_mock.request_history) == first_request_count


def test_resolution_resumes_from_next_page_after_quota_pause(
    config, database, requests_mock
) -> None:
    config.api = replace(config.api, requests_per_day=2, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    requests_mock.get(f"{config.api.base_url}/teams", json=_team_payload())
    requests_mock.get(
        f"{config.api.base_url}/players?team=57&season=2022&page=1",
        json={
            **_players_payload([(1001, "A Different Player")]),
            "paging": {"current": 1, "total": 2},
        },
    )
    player = _add_config_player(config, database)
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    resolver = PlayerResolver(config, database)
    assert resolver.resolve(player) == "paused_quota_limit"
    assert len(requests_mock.request_history) == 2
    player_row = database.get_player(player.name)
    checkpoint = Checkpoint(config.paths.checkpoints, database, int(player_row["id"]))
    paused = checkpoint.load(player.name)
    assert paused.status == "paused"
    assert paused.resolution_stage == "paused_quota_limit"
    assert paused.resolved_team_id == "57"
    assert paused.resolved_team_name == "Inter Miami"
    assert paused.resolution_season == 2022
    assert paused.current_page == 2
    assert paused.last_processed_page == 1
    assert paused.has_more_pages is True
    assert len(database.list_player_candidates(player.name)) == 1
    assert database.list_player_candidates(player.name)[0]["source_page"] == 1
    assert database.get_daily_request_usage(datetime.now(timezone.utc).date().isoformat()) == 2

    config.api = replace(config.api, requests_per_day=10, max_retries=0)
    requests_mock.get(
        f"{config.api.base_url}/players?team=57&season=2022&page=2",
        json={
            **_players_payload([(1002, "Lionel Messi")]),
            "paging": {"current": 2, "total": 2},
        },
    )
    assert resolver.resolve(config.get_player(player.name)) == "resolved"
    assert len(requests_mock.request_history) == 3
    assert requests_mock.request_history[0].path == "/teams"
    assert [request.qs.get("page") for request in requests_mock.request_history[1:]] == [
        ["1"], ["2"],
    ]
    candidates = database.list_player_candidates(player.name)
    assert len(candidates) == 2
    assert {row["source_page"] for row in candidates} == {1, 2}
    assert config.get_player(player.name).provider_player_id == "1002"


def test_resolution_status_recovers_successful_saved_page_without_api(
    config, database
) -> None:
    player = config.add_player("Lionel Messi", team_name="Inter Miami")
    player_id = database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": None, "resolution_status": "pending",
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    request_id = database.begin_request(
        player.provider, "players", "https://example.test/players",
        player_id=player_id, season=2022, stage="resolve_team_players", team_id="57",
    )
    database.finish_request(request_id, 200, True, None)
    database.set_resolution_cache(
        player.name, player.provider, "teams:inter miami",
        _team_payload()["response"],
    )
    raw_dir = config.paths.json / "lionel_messi"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_page = raw_dir / "players_page_1.json"
    raw_page.write_text(
        json.dumps({
            "get": "players",
            "parameters": {"team": "57", "season": "2022", "page": 1},
            "errors": [],
            "paging": {"current": 1, "total": 2},
            "response": _players_payload([(1001, "A Different Player")])["response"],
        }),
        encoding="utf-8",
    )
    Checkpoint(config.paths.checkpoints, database, player_id).save(
        CheckpointState(player_name=player.name, current_season=2022)
    )

    state = PlayerResolver(config, database).prepare_resolution_status(player)
    assert state.resolved_team_id == "57"
    assert state.resolved_team_name == "Inter Miami"
    assert state.resolution_season == 2022
    assert state.current_page == 2
    assert state.last_processed_page == 1
    assert state.resolution_stage == "resolving_players_page"
    assert len(database.list_player_candidates(player.name)) == 1
    assert database.list_player_candidates(player.name)[0]["raw_json_path"] == str(raw_page)
    with database.connect() as connection:
        assert connection.execute(
            "SELECT page_number FROM request_log WHERE id=?", (request_id,)
        ).fetchone()["page_number"] == 1


def test_unresolved_and_needs_review_players_are_not_crawled(config, database) -> None:
    player = _add_config_player(config, database)
    config.set_player_resolution("Cristiano Ronaldo", "needs_review")
    database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    config.set_player_resolution(player.name, "needs_review")

    class NoCallProvider:
        def __getattr__(self, name):
            raise AssertionError(f"Provider called unexpectedly: {name}")

    summary = Pipeline(config, database=database, provider=NoCallProvider()).run()
    assert summary["errors"] == []
    assert summary["players_completed"] == 0
    assert database.table_count("request_log") == 0


def test_multiseason_resume_preserves_2022_and_resolves_from_2023_page_one(
    config, database, requests_mock
) -> None:
    player = _seed_previous_messi_attempt(config, database)
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    requests_mock.get(
        f"{config.api.base_url}/players?team=9568&season=2023&page=1",
        json=_players_payload([(1001, "Lionel Messi")], team_id=9568),
    )

    state = PlayerResolver(config, database).prepare_resolution_status(player)
    assert state.resolution_season == 2023
    assert state.current_page == 1
    assert state.resolved_team_id == "9568"
    assert state.resolution_completed_attempts[0]["season"] == 2022
    assert state.resolution_completed_attempts[0]["status"] == "not_found_for_season"
    assert len(database.list_resolution_candidates(player.name, 2022)) == 52

    assert PlayerResolver(config, database).resolve(config.get_player(player.name)) == "resolved"
    assert len(requests_mock.request_history) == 1
    assert requests_mock.request_history[0].qs == {
        "team": ["9568"], "season": ["2023"], "page": ["1"],
    }
    assert config.get_player(player.name).provider_player_id == "1001"
    assert config.get_player(player.name).resolution_seasons == []
    assert len(database.list_resolution_candidates(player.name, 2022)) == 52
    assert len(database.list_resolution_candidates(player.name, 2023)) == 1


def test_not_found_season_advances_to_next_configured_season(
    config, database, requests_mock
) -> None:
    player = _seed_previous_messi_attempt(config, database)
    config.api = replace(config.api, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    empty = {"response": [], "paging": {"current": 1, "total": 1}}
    requests_mock.get(
        f"{config.api.base_url}/players?team=9568&season=2023&page=1", json=empty
    )
    requests_mock.get(
        f"{config.api.base_url}/players?team=9568&season=2024&page=1", json=empty
    )

    assert PlayerResolver(config, database).resolve(player) == "not_found"
    assert [request.qs["season"][0] for request in requests_mock.request_history] == [
        "2023", "2024",
    ]
    saved = Checkpoint(
        config.paths.checkpoints, database, int(database.get_player(player.name)["id"])
    ).load(player.name)
    outcomes = {
        item["season"]: item["status"]
        for item in saved.resolution_completed_attempts
    }
    assert outcomes == {
        2022: "not_found_for_season",
        2023: "not_found_for_season",
        2024: "not_found_for_season",
    }
    assert config.get_player(player.name).attempted_resolution_seasons == [
        2022, 2023, 2024,
    ]


def test_quota_pause_and_resume_are_isolated_to_active_season(
    config, database, requests_mock
) -> None:
    player = _seed_previous_messi_attempt(config, database)
    config.api = replace(config.api, requests_per_day=1, max_retries=0)
    config.crawling = replace(config.crawling, delay_between_requests_seconds=0)
    requests_mock.get(
        f"{config.api.base_url}/players?team=9568&season=2023&page=1",
        json={
            **_players_payload([(2001, "Another Player")], team_id=9568),
            "paging": {"current": 1, "total": 2},
        },
    )
    player_row = database.get_player(player.name)
    assert PlayerResolver(config, database).resolve(player) == "paused_quota_limit"
    paused = Checkpoint(
        config.paths.checkpoints, database, int(player_row["id"])
    ).load(player.name)
    assert paused.resolution_season == 2023
    assert paused.current_page == 2
    assert paused.resolution_current_attempt["stage"] == "paused_quota_limit"
    assert len(database.list_resolution_candidates(player.name, 2022)) == 52

    config.api = replace(config.api, requests_per_day=10, max_retries=0)
    requests_mock.get(
        f"{config.api.base_url}/players?team=9568&season=2023&page=2",
        json={
            **_players_payload([(2002, "Lionel Messi")], team_id=9568),
            "paging": {"current": 2, "total": 2},
        },
    )
    assert PlayerResolver(config, database).resolve(config.get_player(player.name)) == "resolved"
    assert [request.qs["page"][0] for request in requests_mock.request_history] == ["1", "2"]
    assert [request.qs["season"][0] for request in requests_mock.request_history] == [
        "2023", "2023",
    ]
    assert len(database.list_resolution_candidates(player.name, 2022)) == 52


def test_retry_resolution_resets_only_requested_season(config, database) -> None:
    player = _seed_previous_messi_attempt(config, database)
    state = PlayerResolver(config, database).prepare_resolution_status(player)
    player_row = database.get_player(player.name)
    database.upsert_player_candidate({
        "player_config_name": player.name, "provider": player.provider,
        "provider_player_id": "93001", "provider_team_id": "9568",
        "provider_team_name": "Inter Miami", "season": 2023,
        "matched_name": "A different player", "name_similarity": 0.1,
        "status": "rejected", "source_page": 1,
    })
    database.set_resolution_page(
        player.name, player.provider, "9568", 2022, 1,
        {"response": [], "paging": {"current": 1, "total": 3}},
    )
    database.set_resolution_page(
        player.name, player.provider, "9568", 2023, 1,
        {"response": [], "paging": {"current": 1, "total": 1}},
    )

    _retry_resolution(config, database, player.name, 2023)

    reset = Checkpoint(
        config.paths.checkpoints, database, int(player_row["id"])
    ).load(player.name)
    assert reset.resolution_season == 2023
    assert reset.current_page == 1
    assert [item["season"] for item in reset.resolution_completed_attempts] == [2022]
    assert len(database.list_resolution_candidates(player.name, 2022)) == 52
    assert database.list_resolution_candidates(player.name, 2023) == []
    assert database.get_resolution_page(player.name, player.provider, "9568", 2022, 1)
    assert database.get_resolution_page(player.name, player.provider, "9568", 2023, 1) is None


def test_candidate_upsert_deduplicates_provider_id_within_attempt(config, database) -> None:
    attempt_id = "Lionel Messi|api_football|2023|9568"
    candidate = {
        "player_config_name": "Lionel Messi", "provider": "api_football",
        "provider_player_id": "9001", "provider_team_id": "9568",
        "provider_team_name": "Inter Miami", "season": 2023,
        "matched_name": "Lionel Messi", "name_similarity": 1.0,
        "attempt_id": attempt_id,
    }
    first = database.upsert_player_candidate(candidate)
    second = database.upsert_player_candidate({
        **candidate, "provider_team_id": "9569", "provider_team_name": "Other Team",
        "source_page": 2,
    })
    saved = database.list_resolution_candidates("Lionel Messi", 2023)
    assert first == second
    assert len(saved) == 1
    assert saved[0]["attempt_id"] == attempt_id
