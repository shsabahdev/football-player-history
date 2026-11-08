"""Resumable player/season/fixture collection pipeline."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from rich.console import Console

from .api_client import APIClient, DailyLimitReached
from .checkpoint import Checkpoint
from .config import Config
from .database import Database, utc_now
from .models import CheckpointState, PlayerConfig
from .providers.api_football import APIFootballProvider, SubscriptionSeasonError
from .providers.base import PlayerDataProvider

console = Console()
FIXTURE_STAGES = ("fetch_fixture", "fetch_events", "fetch_lineups", "fetch_player_stats")


class Pipeline:
    """Coordinate checkpointed collection from an authorized provider."""

    def __init__(
        self, config: Config, database: Database | None = None,
        provider: PlayerDataProvider | None = None, logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.database = database or Database(config.paths.database)
        self.logger = logger or logging.getLogger("football_history")
        self.provider = provider
        self.checkpoint: Checkpoint | None = None
        self.state: CheckpointState | None = None
        self.player_id: int | None = None
        self._event_rows: list[dict[str, Any]] = []
        self._lineup_rows: list[dict[str, Any]] = []
        self._stats_rows: list[dict[str, Any]] = []

    def run(self, resume: bool = False, only_player: str | None = None) -> dict[str, Any]:
        """Run all configured players and return a final summary."""
        summary: dict[str, Any] = {
            "players_completed": 0, "seasons_completed": 0, "matches": 0,
            "requests_used": 0, "errors": [], "paused": False,
        }
        should_resume = resume or self.config.checkpoint.resume_on_start
        completed_names = {item.get("name", "").casefold() for item in self.config.completed_players}
        for player in self.config.players:
            if only_player and player.name.casefold() != only_player.casefold():
                continue
            if player.resolution_status != "resolved" or not player.provider_player_id:
                message = (
                    f"Skipping {player.name}: resolution status is "
                    f"{player.resolution_status}; set provider_player_id or resolve the name "
                    "with team/league information before crawling."
                )
                self.logger.warning(message)
                continue
            if player.name.casefold() in completed_names:
                self.logger.info("Skipping completed player: %s", player.name)
                continue
            try:
                player_completed = self._run_player(player, should_resume)
                if player_completed:
                    summary["players_completed"] += 1
                self._add_player_totals(summary)
            except DailyLimitReached as exc:
                self.logger.warning("%s", exc)
                if self.state and self.checkpoint:
                    self.state.status = "paused"
                    self.checkpoint.save(self.state)
                    if self.player_id is not None:
                        self.database.mark_player_paused(
                            self.player_id, self.state.current_season, self.state.current_stage
                        )
                summary["paused"] = True
                summary["pause_message"] = str(exc)
                console.print(f"[yellow]{exc}[/yellow]")
                self._add_player_totals(summary)
                break
            except KeyboardInterrupt:
                if self.state and self.checkpoint:
                    self.checkpoint.save(self.state)
                self.logger.info("Interrupted; checkpoint saved.")
                raise
            except Exception as exc:
                self.logger.exception("Player crawl failed for %s", player.name)
                summary["errors"].append(f"{player.name}: {exc}")
                if self.state and self.checkpoint:
                    self.checkpoint.record_error(self.state, exc)
                self._add_player_totals(summary)
        today = datetime.now(timezone.utc).date().isoformat()
        summary["requests_used"] = self.database.get_daily_request_usage(today)
        summary["database_path"] = str(self.config.paths.database)
        console.print(
            f"Finished: {summary['matches']} matches, {summary['seasons_completed']} seasons, "
            f"{summary['requests_used']} requests today, {len(summary['errors'])} errors."
        )
        for error in summary["errors"]:
            console.print(f"[yellow]Warning: {error}[/yellow]")
        console.print(f"Database: {summary['database_path']}")
        return summary

    def _run_player(self, player: PlayerConfig, resume: bool) -> bool:
        self.checkpoint = Checkpoint(self.config.paths.checkpoints, self.database)
        self.player_id = None
        existing = self.database.get_player(player.name)
        if existing:
            self.player_id = int(existing["id"])
            self.checkpoint.bind_player(self.player_id)
        state = (
            self.checkpoint.load(player.name, player.provider_player_id)
            if resume else CheckpointState(
                player_name=player.name, provider_player_id=player.provider_player_id
            )
        )
        self.state = state
        if self.provider is None:
            if self.config.api.provider != "api_football":
                raise ValueError(f"Unsupported configured provider: {self.config.api.provider}")
            if not player.provider_player_id:
                raise ValueError(
                    f"Player ID is missing for {player.name}. Add provider_player_id to "
                    "config/players.yaml. API-Football name search requires a team or league filter."
                )
        if (
            player.provider_player_id is not None
            and state.provider_player_id is not None
            and str(player.provider_player_id) != state.provider_player_id
        ):
            raise ValueError(
                f"Configured player ID for {player.name} does not match its saved checkpoint. "
                f"Run reset-player --name \"{player.name}\" before crawling the new ID."
            )
        state.status = "in_progress"
        if player.provider_player_id:
            self.player_id = self.database.upsert_player({
                "provider_player_id": player.provider_player_id,
                "provider": player.provider,
                "resolution_status": player.resolution_status,
                "current_team_name": player.current_team_name,
                "name": player.name,
                "status": "in_progress",
                "crawl_from_season": player.crawl_from_season,
                "crawl_to_season": player.crawl_to_season,
            })
            self.checkpoint.bind_player(self.player_id)
        if resume and state.current_stage != "resolve_player":
            self.logger.info("Resuming %s at %s.", player.name, state.current_stage)

        # Resolve the provider identity before binding SQLite checkpoint storage.
        provider = self.provider
        if provider is None:
            client = APIClient(self.config, self.database, state, self.checkpoint.save, self.logger)
            provider = APIFootballProvider(client)
            self.provider = provider
        if state.current_stage == "resolve_player" or not state.provider_player_id:
            state.current_stage = "resolve_player"
            state.current_season = player.crawl_from_season
            self._save()
            self._set_request_context("resolve_player", player.crawl_from_season)
            resolved = provider.resolve_player(
                player.name, player.provider_player_id, season=player.crawl_from_season
            )
            state.provider_player_id = str(resolved["provider_player_id"])
            self.player_id = self.database.upsert_player({
                "provider_player_id": state.provider_player_id, "name": resolved.get("name") or player.name,
                "provider": player.provider,
                "resolution_status": "resolved",
                "current_team_name": player.current_team_name,
                "status": "in_progress", "crawl_from_season": player.crawl_from_season,
                "crawl_to_season": player.crawl_to_season,
            })
            self.checkpoint.bind_player(self.player_id)
            self._save()
            self._set_request_context("discover_seasons")
        else:
            self.player_id = self.database.upsert_player({
                "provider_player_id": state.provider_player_id, "name": player.name,
                "provider": player.provider,
                "resolution_status": player.resolution_status,
                "current_team_name": player.current_team_name,
                "status": "in_progress", "crawl_from_season": player.crawl_from_season,
                "crawl_to_season": player.crawl_to_season,
            })
            self.checkpoint.bind_player(self.player_id)

        end_season = (
            datetime.now().year if player.crawl_to_season == "current"
            else int(player.crawl_to_season)
        )
        target_start = player.desired_historical_from_season or player.crawl_from_season
        requested_start = min(target_start, player.crawl_from_season)
        if state.supported_season_min is None or state.supported_season_max is None:
            resume_season = state.current_season
            resume_stage = state.current_stage
            state.current_stage = "validate_season_range"
            self._save()
            self._set_request_context("discover_seasons", requested_start)
            minimum, maximum = provider.get_supported_season_range(state.provider_player_id)
            if minimum > maximum:
                raise ValueError(
                    f"Provider returned an invalid supported season range: {minimum}-{maximum}."
                )
            state.supported_season_min = minimum
            state.supported_season_max = maximum
            self.database.set_supported_season_range(self.config.api.provider, minimum, maximum)
            if resume_stage in FIXTURE_STAGES or resume_stage in {
                "discover_player_teams", "discover_team_fixtures", "verify_participation"
            }:
                state.current_season = resume_season
                state.current_stage = resume_stage
            else:
                state.current_stage = "discover_seasons"
            self._save()

        minimum = state.supported_season_min
        maximum = state.supported_season_max
        assert minimum is not None and maximum is not None
        stored_statuses = self.database.get_season_statuses(self.player_id)
        for season in range(requested_start, end_season + 1):
            if stored_statuses.get(season) == "completed":
                if season not in state.completed_seasons:
                    state.completed_seasons.append(season)
                continue
            if season < minimum or season > maximum:
                if season not in state.unsupported_seasons:
                    state.unsupported_seasons.append(season)
                if season not in state.skipped_seasons:
                    state.skipped_seasons.append(season)
                self.database.mark_season_unsupported(self.player_id, season)
                self.logger.warning(
                    "Skipping %s season %s: outside API-Football subscription range %s-%s.",
                    player.name, season, minimum, maximum,
                )
                self._save()
            elif stored_statuses.get(season) == "unsupported_for_subscription":
                if season not in state.unsupported_seasons:
                    state.unsupported_seasons.append(season)
                if season not in state.skipped_seasons:
                    state.skipped_seasons.append(season)

        configured_supported = max(player.crawl_from_season, minimum) <= min(end_season, maximum)
        if not configured_supported:
            message = (
                f"No configured seasons are supported for {player.name}: requested "
                f"{player.crawl_from_season}-{end_season}, subscription supports "
                f"{minimum}-{maximum}. Change crawl_from_season or upgrade the API subscription."
            )
            self.logger.warning(message)
            console.print(f"[yellow]{message}[/yellow]")
            state.status = "unsupported_for_subscription"
            self._save()
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE players SET status='unsupported_for_subscription',updated_at=? WHERE id=?",
                    (utc_now(), self.player_id),
                )
            return False

        if not state.seasons_to_crawl:
            state.current_stage = "discover_seasons"
            self._save()
            self._set_request_context("discover_seasons")
            available = provider.get_cached_available_seasons()
            if available is None:
                available = provider.get_available_seasons(state.provider_player_id)
            state.seasons_to_crawl = sorted(
                season for season in set(available)
                if player.crawl_from_season <= season <= end_season
                and minimum <= season <= maximum
            )
            self._save()

        for season in state.seasons_to_crawl:
            if season in state.completed_seasons:
                continue
            if stored_statuses.get(season) == "no_team_data":
                continue
            if season in state.unsupported_seasons or stored_statuses.get(season) == (
                "unsupported_for_subscription"
            ):
                continue
            if season < minimum or season > maximum:
                if season not in state.unsupported_seasons:
                    state.unsupported_seasons.append(season)
                if season not in state.skipped_seasons:
                    state.skipped_seasons.append(season)
                self.database.mark_season_unsupported(self.player_id, season)
                self.logger.warning(
                    "Skipping %s season %s: outside API-Football subscription range %s-%s.",
                    player.name, season, minimum, maximum,
                )
                state.current_season = season
                state.current_stage = "unsupported_for_subscription"
                self._save()
                continue
            state.current_season = season
            if state.current_stage == "finalize_season":
                self._finalize_season(season)
                continue
            if season not in state.in_progress_seasons:
                state.in_progress_seasons.append(season)
            self.database.upsert_season(self.player_id, season, status="in_progress", started_at=utc_now())
            season_work = self._season_work(season)
            if not season_work["fixture_discovery_completed"]:
                try:
                    has_team_data = self._discover_season_fixtures(provider, season, season_work)
                except SubscriptionSeasonError as exc:
                    state.supported_season_min = exc.minimum
                    state.supported_season_max = exc.maximum
                    if season not in state.unsupported_seasons:
                        state.unsupported_seasons.append(season)
                    if season not in state.skipped_seasons:
                        state.skipped_seasons.append(season)
                    self.database.mark_season_unsupported(self.player_id, season)
                    state.current_stage = "unsupported_for_subscription"
                    self.logger.warning("%s", exc)
                    self._save()
                    continue
                if not has_team_data:
                    continue
            state.fixture_ids = list(season_work["discovered_fixture_ids"])
            for fixture_id in state.fixture_ids:
                if fixture_id in season_work["processed_fixture_ids"]:
                    continue
                resume_stage = (
                    state.current_stage
                    if state.active_fixture_id == fixture_id and state.current_stage in FIXTURE_STAGES
                    else None
                )
                state.active_fixture_id = fixture_id
                participated = self.database.get_player_fixture_participation(
                    self.player_id, fixture_id
                )
                if participated is None:
                    state.current_stage = "verify_participation"
                    self._save()
                    team_id, team_name = self._fixture_team_context(season, fixture_id)
                    self._set_request_context(
                        "verify_participation", season, fixture_id,
                        team_id=team_id, team_name=team_name,
                    )
                    participated = provider.get_fixture_player_participation(
                        fixture_id, state.provider_player_id or ""
                    )
                    self.database.set_player_fixture_participation(
                        self.player_id, fixture_id, participated, self._raw_path(provider)
                    )
                if fixture_id not in season_work["participation_checked_fixture_ids"]:
                    season_work["participation_checked_fixture_ids"].append(fixture_id)
                if participated:
                    if fixture_id not in season_work["player_participated_fixture_ids"]:
                        season_work["player_participated_fixture_ids"].append(fixture_id)
                    if not state.active_fixture_player_stats_cached:
                        state.active_fixture_player_stats = provider.get_fixture_player_stats(
                            fixture_id
                        )
                        state.active_fixture_player_stats_cached = True
                        state.active_fixture_player_stats_raw_path = self._raw_path(provider)
                    state.current_stage = resume_stage or "fetch_fixture"
                    self._sync_season_checkpoint(season, season_work)
                    self._save()
                    self._process_fixture(provider, fixture_id, season)
                else:
                    self._sync_season_checkpoint(season, season_work)
                    self._save()
                if fixture_id not in season_work["processed_fixture_ids"]:
                    season_work["processed_fixture_ids"].append(fixture_id)
                for team_id in season_work["discovered_team_ids"]:
                    if fixture_id in season_work["team_fixture_ids"].get(team_id, []):
                        processed_for_team = season_work[
                            "processed_team_fixture_ids"
                        ].setdefault(team_id, [])
                        if fixture_id not in processed_for_team:
                            processed_for_team.append(fixture_id)
                if fixture_id not in state.completed_fixture_ids:
                    state.completed_fixture_ids.append(fixture_id)
                    state.total_fixtures_processed += 1
                state.last_processed_fixture_id = fixture_id
                state.active_fixture_id = None
                state.current_stage = "fetch_fixture"
                self._sync_season_checkpoint(season, season_work)
                self.database.upsert_season(
                    self.player_id, season, status="in_progress",
                    fixtures_found=len(state.fixture_ids),
                    fixtures_processed=len(season_work["processed_fixture_ids"]),
                    started_at=utc_now(),
                )
                self._save()
                self._print_progress(player.name, season)

            state.current_stage = "finalize_season"
            self._save()
            self._finalize_season(season)

        state.current_stage = "complete_player"
        self._save()
        with self.database.transaction() as connection:
            connection.execute("UPDATE players SET status='completed',updated_at=? WHERE id=?",
                               (utc_now(), self.player_id))
        self.config.add_or_update_completed_player(player)
        return True

    def _season_work(self, season: int) -> dict[str, Any]:
        """Return initialized resumable fixture-discovery state for a season."""
        assert self.state is not None
        key = str(season)
        work = self.state.season_fixture_state.setdefault(key, {})
        defaults: dict[str, Any] = {
            "player_team_discovery_completed": False,
            "fixture_discovery_completed": False,
            "discovered_team_ids": [],
            "processed_team_ids": [],
            "current_team_id": None,
            "current_fixture_page": 1,
            "discovered_fixture_ids": [],
            "processed_fixture_ids": [],
            "participation_checked_fixture_ids": [],
            "player_participated_fixture_ids": [],
            "team_fixture_pages": {},
            "team_fixture_page_totals": {},
            "team_fixture_ids": {},
            "processed_team_fixture_ids": {},
            "team_info": {},
            "fixture_count_recorded": False,
            "season_status": None,
        }
        for key_name, default in defaults.items():
            work.setdefault(key_name, default)
        self._sync_season_checkpoint(season, work)
        return work

    def _sync_season_checkpoint(self, season: int, work: dict[str, Any]) -> None:
        """Mirror the active season's detailed state into top-level checkpoint fields."""
        assert self.state is not None
        self.state.discovered_team_ids = list(work["discovered_team_ids"])
        self.state.processed_team_ids = list(work["processed_team_ids"])
        self.state.current_team_id = work["current_team_id"]
        self.state.current_fixture_page = int(work["current_fixture_page"])
        self.state.discovered_fixture_ids = list(work["discovered_fixture_ids"])
        self.state.processed_fixture_ids = list(work["processed_fixture_ids"])
        self.state.participation_checked_fixture_ids = list(
            work["participation_checked_fixture_ids"]
        )
        self.state.player_participated_fixture_ids = list(
            work["player_participated_fixture_ids"]
        )
        self.state.season_status = work["season_status"]
        self.state.current_season = season

    def _discover_season_fixtures(
        self,
        provider: PlayerDataProvider,
        season: int,
        work: dict[str, Any],
    ) -> bool:
        """Discover candidate fixtures through the player's season teams."""
        assert self.state is not None and self.player_id is not None
        state = self.state
        if not work["player_team_discovery_completed"]:
            state.current_stage = "discover_player_teams"
            self._sync_season_checkpoint(season, work)
            self._save()
            self._set_request_context("discover_player_teams", season)
            teams = provider.get_player_team_seasons(
                state.provider_player_id or "", season
            )
            team_info: dict[str, dict[str, Any]] = {}
            for value in teams:
                team = value.get("team", value)
                team_id = team.get("id") if isinstance(team, dict) else None
                if team_id is None:
                    continue
                team_id = str(team_id)
                team_info[team_id] = {
                    "id": team_id,
                    "name": str(team.get("name") or team_id),
                }
            work["team_info"] = team_info
            work["discovered_team_ids"] = list(team_info)
            work["player_team_discovery_completed"] = True
            self._sync_season_checkpoint(season, work)
            self._save()
            if not work["discovered_team_ids"]:
                work["season_status"] = "no_team_data"
                work["fixture_discovery_completed"] = True
                state.current_stage = "discover_fixtures"
                self.database.upsert_season(
                    self.player_id, season, status="no_team_data",
                    fixtures_found=0, fixtures_processed=0, completed_at=utc_now(),
                )
                if season not in state.skipped_seasons:
                    state.skipped_seasons.append(season)
                self._sync_season_checkpoint(season, work)
                self._save()
                return False

        for team_id in work["discovered_team_ids"]:
            if team_id in work["processed_team_ids"]:
                continue
            work["current_team_id"] = team_id
            pages = work["team_fixture_pages"].setdefault(team_id, [])
            page = max(pages, default=0) + 1
            total_pages = int(work["team_fixture_page_totals"].get(team_id, 1))
            while page <= total_pages:
                work["current_fixture_page"] = page
                state.current_stage = "discover_team_fixtures"
                self._sync_season_checkpoint(season, work)
                self._save()
                team = work["team_info"][team_id]
                self._set_request_context(
                    "discover_team_fixtures", season, team_id=team_id,
                    team_name=team["name"], page_number=page,
                )
                fixtures, current, total_pages = provider.get_team_fixtures_page(
                    team_id, season, page
                )
                if current != page or total_pages < current:
                    raise ValueError("Provider returned invalid team fixture pagination.")
                raw_path = self._raw_path(provider)
                team_fixture_ids = work["team_fixture_ids"].setdefault(team_id, [])
                for item in fixtures:
                    fixture_id = self._fixture_id(item)
                    if not fixture_id:
                        self.logger.warning(
                            "Ignoring fixture without provider id in season %s", season
                        )
                        continue
                    record = self._fixture_record(
                        item, player_id=self.player_id, season=season,
                        raw_json_path=raw_path,
                    )
                    fixture_db_id = self.database.upsert_fixture(record)
                    self.database.record_player_fixture(
                        player_id=self.player_id,
                        fixture_id=fixture_db_id,
                        provider_player_id=state.provider_player_id or "",
                        provider_fixture_id=fixture_id,
                        season=season,
                        provider_team_id=team_id,
                        provider_team_name=team["name"],
                        raw_json_path=raw_path,
                    )
                    if fixture_id not in team_fixture_ids:
                        team_fixture_ids.append(fixture_id)
                    if fixture_id not in work["discovered_fixture_ids"]:
                        work["discovered_fixture_ids"].append(fixture_id)
                if current not in pages:
                    pages.append(current)
                work["team_fixture_page_totals"][team_id] = total_pages
                page = current + 1
                work["current_fixture_page"] = page if page <= total_pages else current
                state.fixture_ids = list(work["discovered_fixture_ids"])
                self._sync_season_checkpoint(season, work)
                self.database.upsert_season(
                    self.player_id, season, status="in_progress",
                    fixtures_found=len(work["discovered_fixture_ids"]),
                    fixtures_processed=len(work["processed_fixture_ids"]),
                    started_at=utc_now(),
                )
                self._save()

            if team_id not in work["processed_team_ids"]:
                work["processed_team_ids"].append(team_id)
            work["current_team_id"] = None
            work["current_fixture_page"] = 1
            self._sync_season_checkpoint(season, work)
            self._save()

        work["fixture_discovery_completed"] = True
        work["season_status"] = "in_progress"
        state.fixture_ids = list(work["discovered_fixture_ids"])
        if not work["fixture_count_recorded"]:
            state.total_fixtures_found += len(work["discovered_fixture_ids"])
            work["fixture_count_recorded"] = True
        state.current_stage = "verify_participation"
        self._sync_season_checkpoint(season, work)
        self.database.upsert_season(
            self.player_id, season, status="in_progress",
            fixtures_found=len(work["discovered_fixture_ids"]),
            fixtures_processed=len(work["processed_fixture_ids"]),
            started_at=utc_now(),
        )
        self._save()
        return True

    def _process_fixture(self, provider: PlayerDataProvider, fixture_id: str, season: int) -> None:
        assert self.state is not None and self.player_id is not None
        state = self.state
        stage = state.current_stage if state.current_stage in FIXTURE_STAGES else FIXTURE_STAGES[0]
        start = FIXTURE_STAGES.index(stage)
        fixture_db_id: int | None = None
        for current in FIXTURE_STAGES[start:]:
            state.current_stage = current
            state.active_fixture_id = fixture_id
            self._save()
            team_id, team_name = self._fixture_team_context(season, fixture_id)
            self._set_request_context(
                current, season, fixture_id, team_id=team_id, team_name=team_name
            )
            if current == "fetch_fixture":
                details = provider.get_fixture_details(fixture_id)
                if details:
                    fixture_db_id = self.database.upsert_fixture(
                        self._fixture_record(
                            details, self.player_id, season, self._raw_path(provider)
                        )
                    )
                else:
                    fixture_db_id = self._fixture_database_id(fixture_id)
            elif current == "fetch_events":
                event_data = provider.get_fixture_events(fixture_id)
                raw_path = self._raw_path(provider)
                self._event_rows = [
                    self._event_record(item, raw_path) for item in event_data
                ]
                self.database.save_fixture_records(fixture_db_id or self._fixture_database_id(fixture_id),
                                                   self._event_rows, [], [])
            elif current == "fetch_lineups":
                lineup_data = provider.get_fixture_lineups(fixture_id)
                raw_path = self._raw_path(provider)
                self._lineup_rows = [
                    {**entry, "raw_json_path": raw_path}
                    for item in lineup_data
                    for entry in self._lineup_record(item)
                ]
                self.database.save_fixture_records(fixture_db_id or self._fixture_database_id(fixture_id),
                                                   [], self._lineup_rows, [])
            elif current == "fetch_player_stats":
                if state.active_fixture_player_stats_cached:
                    stat_data = state.active_fixture_player_stats or []
                    raw_path = state.active_fixture_player_stats_raw_path
                else:
                    stat_data = provider.get_fixture_player_stats(fixture_id)
                    raw_path = self._raw_path(provider)
                self._stats_rows = [
                    {**self._stats_record(team_block, player_entry), "raw_json_path": raw_path}
                    for team_block in stat_data
                    for player_entry in team_block.get("players", [])
                    if isinstance(player_entry, dict)
                ]
                self.database.save_fixture_records(fixture_db_id or self._fixture_database_id(fixture_id),
                                                   [], [], self._stats_rows)
                state.active_fixture_player_stats = None
                state.active_fixture_player_stats_cached = False
                state.active_fixture_player_stats_raw_path = None
            next_index = FIXTURE_STAGES.index(current) + 1
            if next_index < len(FIXTURE_STAGES):
                state.current_stage = FIXTURE_STAGES[next_index]
            self._save()
        if fixture_db_id is None:
            fixture_db_id = self._fixture_database_id(fixture_id)
        self._event_rows, self._lineup_rows, self._stats_rows = [], [], []

    def _fixture_database_id(self, provider_fixture_id: str) -> int:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT id FROM fixtures WHERE provider_fixture_id=?", (provider_fixture_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise RuntimeError(f"Fixture metadata missing for {provider_fixture_id}.")
        return int(row["id"])

    @staticmethod
    def _fixture_id(item: dict[str, Any]) -> str | None:
        fixture = item.get("fixture", item)
        value = fixture.get("id") if isinstance(fixture, dict) else None
        return str(value) if value is not None else None

    @staticmethod
    def _fixture_record(
        item: dict[str, Any], player_id: int, season: int, raw_json_path: str | None = None
    ) -> dict[str, Any]:
        fixture = item.get("fixture", item)
        league = item.get("league", {})
        teams = item.get("teams", {})
        goals = item.get("goals", {})
        venue = fixture.get("venue") or {}
        status = fixture.get("status") or {}
        home_team = teams.get("home") or {}
        away_team = teams.get("away") or {}
        return {
            "provider_fixture_id": str(fixture.get("id")), "player_id": player_id, "season": season,
            "competition_id": league.get("id"), "competition_name": league.get("name"),
            "date": fixture.get("date"), "status": status.get("short") if isinstance(status, dict) else status,
            "home_team_id": home_team.get("id"), "home_team_name": home_team.get("name"),
            "away_team_id": away_team.get("id"), "away_team_name": away_team.get("name"),
            "home_goals": goals.get("home"), "away_goals": goals.get("away"),
            "venue": venue.get("name") if isinstance(venue, dict) else venue,
            "referee": fixture.get("referee"), "raw_json_path": raw_json_path,
        }

    @staticmethod
    def _event_record(item: dict[str, Any], raw_json_path: str | None = None) -> dict[str, Any]:
        team, player, assist = item.get("team") or {}, item.get("player") or {}, item.get("assist") or {}
        event_time = item.get("time") or {}
        elapsed = event_time.get("elapsed")
        extra = event_time.get("extra")
        minute = f"{elapsed}+{extra}" if elapsed is not None and extra is not None else elapsed
        event_id = item.get("id")
        if event_id is None:
            event_id = hashlib.sha256(
                json.dumps(item, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
        return {
            "provider_event_id": str(event_id), "minute": str(minute or ""),
            "event_type": item.get("type"), "detail": item.get("detail"),
            "team_id": team.get("id"), "team_name": team.get("name"),
            "player_id": player.get("id"), "player_name": player.get("name"),
            "assist_player_id": assist.get("id"), "assist_player_name": assist.get("name"),
            "raw_json_path": raw_json_path,
        }

    @staticmethod
    def _lineup_record(item: dict[str, Any]) -> list[dict[str, Any]]:
        team = item.get("team") or {}
        coach = item.get("coach") or {}
        result: list[dict[str, Any]] = []
        for key in ("startXI", "substitutes"):
            for entry in item.get(key, []) or []:
                player = entry.get("player") or {}
                result.append({
                    "team_id": team.get("id"), "team_name": team.get("name"),
                    "formation": item.get("formation"), "coach_name": coach.get("name"),
                    "player_id": player.get("id"), "player_name": player.get("name"),
                    "player_number": player.get("number"), "position": player.get("pos"),
                    "grid": player.get("grid"), "is_starter": int(key == "startXI"),
                    "is_captain": int(bool(player.get("captain"))),
                })
        return result

    @staticmethod
    def _stats_record(team_block: dict[str, Any], player_entry: dict[str, Any]) -> dict[str, Any]:
        player = player_entry.get("player") or {}
        statistics = (player_entry.get("statistics") or [{}])[0]
        def metric(group: str, key: str) -> Any:
            section = statistics.get(group) or {}
            return section.get(key) if isinstance(section, dict) else None
        return {
            "provider_player_id": str(player.get("id")) if player.get("id") is not None else None,
            "player_name": player.get("name"), "team_id": (team_block.get("team") or {}).get("id"),
            "team_name": (team_block.get("team") or {}).get("name"),
            "minutes_played": metric("games", "minutes"), "rating": metric("games", "rating"),
            "goals": metric("goals", "total"), "assists": metric("goals", "assists"),
            "shots_total": metric("shots", "total"), "shots_on_goal": metric("shots", "on"),
            "passes_total": metric("passes", "total"), "passes_accuracy": metric("passes", "accuracy"),
            "duels_total": metric("duels", "total"), "duels_won": metric("duels", "won"),
        }

    def _save(self) -> None:
        if self.checkpoint and self.state:
            self.checkpoint.save(self.state)

    def _fixture_team_context(self, season: int, fixture_id: str) -> tuple[str | None, str | None]:
        """Return the first recorded team association for request attribution."""
        assert self.state is not None
        work = self.state.season_fixture_state.get(str(season), {})
        fixture_teams = work.get("team_fixture_ids", {})
        team_info = work.get("team_info", {})
        for team_id in work.get("discovered_team_ids", []):
            if fixture_id in fixture_teams.get(team_id, []):
                team = team_info.get(team_id, {})
                return team_id, team.get("name")
        return None, None

    def _set_request_context(
        self,
        stage: str,
        season: int | None = None,
        fixture_id: str | None = None,
        *,
        team_id: str | None = None,
        team_name: str | None = None,
        page_number: int | None = None,
    ) -> None:
        if self.state is None:
            return
        client = getattr(self.provider, "client", None)
        if client is None:
            return
        setter = getattr(client, "set_request_context", None)
        if setter is not None:
            setter(
                player_id=self.player_id,
                provider_player_id=self.state.provider_player_id,
                season=season,
                fixture_id=fixture_id,
                stage=stage,
                team_id=team_id,
                team_name=team_name,
                page_number=page_number,
            )

    @staticmethod
    def _raw_path(provider: PlayerDataProvider) -> str | None:
        """Return the most recent saved response path for the API provider."""
        client = getattr(provider, "client", None)
        path = getattr(client, "last_raw_json_path", None)
        return str(path) if path else None

    def _finalize_season(self, season: int) -> None:
        assert self.state is not None and self.player_id is not None
        state = self.state
        work = self._season_work(season)
        work["season_status"] = "completed"
        self._sync_season_checkpoint(season, work)
        self.database.upsert_season(
            self.player_id, season, status="completed", fixtures_found=len(state.fixture_ids),
            fixtures_processed=sum(1 for fixture in state.fixture_ids
                                   if fixture in state.completed_fixture_ids),
            completed_at=utc_now(),
        )
        if season not in state.completed_seasons:
            state.completed_seasons.append(season)
        state.in_progress_seasons = [value for value in state.in_progress_seasons if value != season]
        state.fixture_ids = []
        state.current_stage = "discover_fixtures"
        self._save()

    def _add_player_totals(self, summary: dict[str, Any]) -> None:
        """Accumulate stored totals for the most recently processed player."""
        if self.player_id is None:
            return
        connection = self.database.connect()
        try:
            fixtures = connection.execute(
                """SELECT COUNT(*) FROM player_fixtures
                WHERE player_id=? AND participated=1""",
                (self.player_id,),
            ).fetchone()[0]
            seasons = connection.execute(
                "SELECT COUNT(*) FROM seasons WHERE player_id=? AND status='completed'",
                (self.player_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        summary["matches"] += int(fixtures)
        summary["seasons_completed"] += int(seasons)

    def _print_progress(self, player: str, season: int) -> None:
        assert self.state is not None
        effective_limit = max(
            0, self.config.api.requests_per_day - self.config.api.daily_request_safety_margin
        )
        console.print(
            f"Player: {player} | Season: {season} | Fixtures: "
            f"{sum(1 for value in self.state.fixture_ids if value in self.state.completed_fixture_ids)}/"
            f"{len(self.state.fixture_ids)} | Requests today: {self.state.requests_used_today}/"
            f"{effective_limit} | Current stage: {self.state.current_stage}"
        )
