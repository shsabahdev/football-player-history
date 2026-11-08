"""Provider interface for authorized football data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PlayerDataProvider(ABC):
    """Abstract interface implemented by each permitted data provider."""

    def __init__(self) -> None:
        self._available_seasons_cache: list[int] | None = None

    @abstractmethod
    def resolve_player(
        self,
        name: str,
        provider_player_id: str | None,
        season: int | None = None,
        *,
        team_id: str | None = None,
        league_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a provider ID, or search by name with team/league filters."""

    @abstractmethod
    def get_available_seasons(self, provider_player_id: str) -> list[int]:
        """Return seasons available for the player."""

    def get_supported_season_range(self, provider_player_id: str) -> tuple[int, int]:
        """Return the range of seasons exposed for a player by the provider."""
        seasons = self.get_available_seasons(provider_player_id)
        if not seasons:
            raise LookupError(f"No seasons are available for player ID {provider_player_id}.")
        self._available_seasons_cache = list(seasons)
        return min(seasons), max(seasons)

    def get_cached_available_seasons(self) -> list[int] | None:
        """Return seasons already fetched during range discovery, if any."""
        cached = getattr(self, "_available_seasons_cache", None)
        return (
            list(cached)
            if cached is not None
            else None
        )

    @abstractmethod
    def get_player_team_seasons(
        self, provider_player_id: str, season: int
    ) -> list[dict[str, Any]]:
        """Return teams associated with a player during a season."""

    @abstractmethod
    def get_team_fixtures(self, team_id: str, season: int) -> list[dict[str, Any]]:
        """Return all fixtures for one team and season."""

    @abstractmethod
    def get_team_fixtures_page(
        self, team_id: str, season: int, page: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        """Return one team fixture page and its current and total page numbers."""

    @abstractmethod
    def get_fixture_player_participation(
        self, fixture_id: str, provider_player_id: str
    ) -> bool:
        """Confirm whether the configured player appears in fixture player data."""

    @abstractmethod
    def get_fixture_details(self, fixture_id: str) -> dict[str, Any]:
        """Return full fixture metadata."""

    @abstractmethod
    def get_fixture_events(self, fixture_id: str) -> list[dict[str, Any]]:
        """Return fixture events."""

    @abstractmethod
    def get_fixture_lineups(self, fixture_id: str) -> list[dict[str, Any]]:
        """Return fixture lineups."""

    @abstractmethod
    def get_fixture_player_stats(self, fixture_id: str) -> list[dict[str, Any]]:
        """Return per-player fixture statistics."""
