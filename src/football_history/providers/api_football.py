"""API-Football v3 provider using its official API."""

from __future__ import annotations

import re
from typing import Any

from ..api_client import APIClient
from .base import PlayerDataProvider


class SubscriptionSeasonError(RuntimeError):
    """The API subscription explicitly rejects a requested season."""

    def __init__(self, season: int, minimum: int, maximum: int) -> None:
        self.season = season
        self.minimum = minimum
        self.maximum = maximum
        super().__init__(
            f"Season {season} is outside the API-Football subscription range "
            f"{minimum}-{maximum}."
        )


def _response(payload: dict[str, Any]) -> list[dict[str, Any]]:
    errors = payload.get("errors")
    if errors and errors != {}:
        raise RuntimeError(f"API-Football reported an API error: {errors}")
    result = payload.get("response", [])
    if not isinstance(result, list):
        raise ValueError("API-Football returned a non-list response field.")
    if any(not isinstance(item, dict) for item in result):
        raise ValueError("API-Football returned an invalid response item.")
    return result


class APIFootballProvider(PlayerDataProvider):
    """Parse API-Football v3 responses into provider-neutral records."""

    def __init__(self, client: APIClient) -> None:
        self.client = client
        self._fixture_player_stats_cache: dict[str, list[dict[str, Any]]] = {}
        super().__init__()

    def resolve_player(
        self,
        name: str,
        provider_player_id: str | None,
        season: int | None = None,
        *,
        team_id: str | None = None,
        league_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve by configured ID first; filtered search is an explicit fallback."""
        if provider_player_id is not None:
            player_id = str(provider_player_id).strip()
            if not player_id.isdigit() or int(player_id) < 1:
                raise ValueError(
                    f"Invalid API-Football player ID '{provider_player_id}'. "
                    "Set provider_player_id to a positive numeric API-Football ID."
                )
            if (
                not isinstance(season, int) or isinstance(season, bool) or season < 1
            ):
                raise ValueError(
                    f"A valid crawl season is required to resolve API-Football player ID "
                    f"{player_id}; configure crawl_from_season."
                )
            try:
                rows = _response(self.client.get("players", {"id": player_id, "season": season}))
            except RuntimeError as exc:
                raise LookupError(
                    f"API-Football could not resolve player ID {player_id} for season {season}. "
                    "Verify the ID, season, and API subscription access."
                ) from exc
            match = next(
                (item for item in rows if self._player_id(item) == player_id),
                None,
            )
            if match is None:
                raise LookupError(
                    f"API-Football returned no player matching configured ID {player_id} "
                    f"for season {season}. Verify provider_player_id and crawl_from_season."
                )
            return self._player(match)

        if team_id is None and league_id is None:
            raise ValueError(
                f"Player ID is missing for {name}. Set provider_player_id in config/players.yaml. "
                "Name search is only supported when a team_id or league_id filter is provided."
            )
        params: dict[str, Any] = {"search": name}
        if team_id is not None:
            params["team"] = team_id
        if league_id is not None:
            params["league"] = league_id
        rows = _response(self.client.get("players", params))
        match = next((item for item in rows if self._same_person(item, name)), None)
        if match is None:
            raise LookupError(
                f"Player {name} was not found with the supplied API-Football team/league filter."
            )
        return self._player(match)

    def get_available_seasons(self, provider_player_id: str) -> list[int]:
        cached = getattr(self, "_available_seasons_cache", None)
        if cached is not None:
            return list(cached)
        payload = self.client.get("players/seasons", {"player": provider_player_id})
        errors = payload.get("errors")
        if errors and errors != {}:
            raise RuntimeError(f"API-Football reported an API error: {errors}")
        values = payload.get("response", [])
        if not isinstance(values, list):
            raise ValueError("API-Football returned an invalid seasons response.")
        seasons: set[int] = set()
        for value in values:
            item = value.get("season", value) if isinstance(value, dict) else value
            if isinstance(item, int):
                seasons.add(item)
            elif isinstance(item, str) and item.isdigit():
                seasons.add(int(item))
        self._available_seasons_cache = sorted(seasons)
        return list(self._available_seasons_cache)

    def search_teams(self, team_name: str) -> list[dict[str, Any]]:
        """Find teams by name; the teams endpoint does not accept a page parameter."""
        return _response(self.client.get("teams", {"search": team_name}))

    def search_leagues(self, league_name: str) -> list[dict[str, Any]]:
        """Find leagues by name using paginated API-Football league search."""
        return self._get_all_pages("leagues", {"search": league_name})

    def get_players_by_team(
        self, team_id: str, season: int, *, league_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Fetch players from a team or league and season, following pagination."""
        params: dict[str, Any] = {"team": team_id, "season": season}
        if league_id is not None:
            params["league"] = league_id
        return self._get_all_pages("players", params)

    def get_players_by_team_page(
        self, team_id: str, season: int, page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch one team-season page and return rows, current page, and page count."""
        return self._get_one_page("players", {"team": team_id, "season": season}, page)

    def get_players_by_league(self, league_id: str, season: int) -> list[dict[str, Any]]:
        """Fetch league player entries for a season, following pagination."""
        return self._get_all_pages("players", {"league": league_id, "season": season})

    def get_players_by_league_page(
        self, league_id: str, season: int, page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch one league-season page and return rows, current page, and page count."""
        return self._get_one_page(
            "players", {"league": league_id, "season": season}, page
        )

    def _get_all_pages(
        self, endpoint: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            rows, current, total_pages = self._get_one_page(endpoint, params, page)
            result.extend(rows)
            page = current + 1
        return result

    def _get_one_page(
        self, endpoint: str, params: dict[str, Any], page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch and validate one API response page."""
        if page < 1:
            raise ValueError("API-Football page number must be positive.")
        payload = self.client.get(endpoint, {**params, "page": page})
        rows = _response(payload)
        paging = payload.get("paging") or {}
        if not isinstance(paging, dict):
            raise ValueError(f"API-Football returned invalid {endpoint} pagination metadata.")
        current = paging.get("current", page)
        total_pages = paging.get("total", current)
        if (
            not isinstance(current, int) or isinstance(current, bool) or current != page
            or not isinstance(total_pages, int) or isinstance(total_pages, bool)
            or total_pages < current
        ):
            raise ValueError(f"API-Football returned invalid {endpoint} pagination metadata.")
        return rows, current, total_pages

    def get_supported_season_range(self, provider_player_id: str) -> tuple[int, int]:
        """Validate the requested historical bound against subscription plan limits."""
        seasons = self.get_available_seasons(provider_player_id)
        self._available_seasons_cache = list(seasons)
        if not seasons:
            raise LookupError(f"No API-Football seasons found for player ID {provider_player_id}.")
        current = getattr(self.client, "request_context", {})
        requested_start = current.get("season")
        probe_min = requested_start if isinstance(requested_start, int) else min(seasons)
        set_context = getattr(self.client, "set_request_context", None)
        if set_context is not None:
            set_context(
                player_id=current.get("player_id"),
                provider_player_id=provider_player_id,
                season=probe_min,
                fixture_id=None,
                stage="validate_season_range",
            )
        try:
            _response(self.client.get("players", {"id": provider_player_id, "season": probe_min}))
        except RuntimeError as exc:
            bounds = self._season_bounds(str(exc))
            if bounds:
                return bounds
            raise
        return min(probe_min, min(seasons)), max(seasons)

    def get_player_team_seasons(
        self, provider_player_id: str, season: int
    ) -> list[dict[str, Any]]:
        """Find every distinct team associated with a player in a season."""
        try:
            rows = _response(
                self.client.get("players", {"id": provider_player_id, "season": season})
            )
        except RuntimeError as exc:
            bounds = self._season_bounds(str(exc))
            if bounds:
                raise SubscriptionSeasonError(season, *bounds) from exc
            raise
        teams: dict[str, dict[str, Any]] = {}
        for item in rows:
            associations: list[Any] = []
            if isinstance(item.get("team"), dict):
                associations.append(item["team"])
            statistics = item.get("statistics", [])
            if isinstance(statistics, list):
                associations.extend(
                    entry.get("team")
                    for entry in statistics
                    if isinstance(entry, dict) and isinstance(entry.get("team"), dict)
                )
            listed_teams = item.get("teams", [])
            if isinstance(listed_teams, list):
                associations.extend(
                    entry.get("team", entry)
                    for entry in listed_teams
                    if isinstance(entry, dict)
                )
            for team in associations:
                team_id = team.get("id")
                if team_id is not None:
                    teams.setdefault(str(team_id), dict(team))
        return list(teams.values())

    def get_team_fixtures(self, team_id: str, season: int) -> list[dict[str, Any]]:
        """Fetch every team fixture page, following only advertised pagination."""
        fixtures: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            rows, current, total_pages = self.get_team_fixtures_page(team_id, season, page)
            fixtures.extend(rows)
            page = current + 1
        return fixtures

    def get_team_fixtures_page(
        self, team_id: str, season: int, page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Fetch a team fixture page without sending page unless pagination exists."""
        if page < 1:
            raise ValueError("API-Football page number must be positive.")
        params: dict[str, Any] = {"team": team_id, "season": season}
        if page > 1:
            params["page"] = page
        try:
            payload = self.client.get("fixtures", params)
        except RuntimeError as exc:
            bounds = self._season_bounds(str(exc))
            if bounds:
                raise SubscriptionSeasonError(season, *bounds) from exc
            raise
        rows = _response(payload)
        paging = payload.get("paging")
        if paging is None:
            return rows, page, page
        if not isinstance(paging, dict):
            raise ValueError("API-Football returned invalid fixture pagination metadata.")
        current = paging.get("current", page)
        total_pages = paging.get("total", current)
        if (
            not isinstance(current, int) or isinstance(current, bool) or current != page
            or not isinstance(total_pages, int) or isinstance(total_pages, bool)
            or total_pages < current
        ):
            raise ValueError("API-Football returned invalid fixture pagination metadata.")
        return rows, current, total_pages

    def get_fixture_player_participation(
        self, fixture_id: str, provider_player_id: str
    ) -> bool:
        """Check fixture player data and cache it for the player-stats stage."""
        rows = _response(self.client.get("fixtures/players", {"fixture": fixture_id}))
        self._fixture_player_stats_cache[fixture_id] = rows
        for team_block in rows:
            players = team_block.get("players", [])
            if not isinstance(players, list):
                continue
            for entry in players:
                if not isinstance(entry, dict):
                    continue
                player = entry.get("player")
                if isinstance(player, dict) and str(player.get("id")) == provider_player_id:
                    return True
        return False

    @staticmethod
    def _season_bounds(message: str) -> tuple[int, int] | None:
        match = re.search(r"\bfrom\s+(\d{4})\s+to\s+(\d{4})\b", message, re.IGNORECASE)
        if not match:
            return None
        minimum, maximum = int(match.group(1)), int(match.group(2))
        return (minimum, maximum) if minimum <= maximum else None

    def get_fixture_details(self, fixture_id: str) -> dict[str, Any]:
        rows = _response(self.client.get("fixtures", {"id": fixture_id}))
        return rows[0] if rows else {}

    def get_fixture_events(self, fixture_id: str) -> list[dict[str, Any]]:
        return _response(self.client.get("fixtures/events", {"fixture": fixture_id}))

    def get_fixture_lineups(self, fixture_id: str) -> list[dict[str, Any]]:
        return _response(self.client.get("fixtures/lineups", {"fixture": fixture_id}))

    def get_fixture_player_stats(self, fixture_id: str) -> list[dict[str, Any]]:
        cached = self._fixture_player_stats_cache.pop(fixture_id, None)
        if cached is not None:
            return cached
        return _response(self.client.get("fixtures/players", {"fixture": fixture_id}))

    @staticmethod
    def _player(item: dict[str, Any]) -> dict[str, Any]:
        player = item.get("player", item)
        if not isinstance(player, dict):
            raise ValueError("API-Football returned an invalid player object.")
        player_id = player.get("id")
        player_name = player.get("name")
        if player_id is None or not player_name:
            raise ValueError("API-Football returned a player without an id or name.")
        return {"provider_player_id": str(player_id), "name": str(player_name)}

    @staticmethod
    def _player_id(item: dict[str, Any]) -> str | None:
        player = item.get("player", item)
        if not isinstance(player, dict) or player.get("id") is None:
            return None
        return str(player["id"])

    @staticmethod
    def _same_person(item: dict[str, Any], expected: str) -> bool:
        player = item.get("player", item)
        if not isinstance(player, dict):
            return False
        return str(player.get("name", "")).casefold() == expected.casefold()
