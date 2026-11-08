"""Conservative, checkpointed player identity resolution."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .api_client import APIClient, DailyLimitReached
from .checkpoint import Checkpoint
from .config import Config
from .database import Database, utc_now
from .models import CheckpointState, PlayerConfig
from .providers.api_football import APIFootballProvider


def normalize_name(value: str) -> str:
    """Normalize accents, punctuation, and whitespace for conservative comparison."""
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join("".join(char if char.isalnum() else " " for char in ascii_text).split())


def name_similarity(left: str, right: str) -> float:
    """Return normalized-name similarity in the inclusive range zero to one."""
    return SequenceMatcher(None, normalize_name(left), normalize_name(right)).ratio()


class PlayerResolver:
    """Resolve configured names with team/league filters, caching, and review."""

    def __init__(self, config: Config, database: Database) -> None:
        self.config = config
        self.database = database
        self._client: APIClient | None = None
        self._force_refresh = False

    def resolve(self, player: PlayerConfig, *, retry_terminal: bool = False) -> str:
        """Resolve one player and return its resulting resolution status."""
        existing = self.database.get_player(player.name)
        self._force_refresh = retry_terminal and player.resolution_status in {
            "not_found", "needs_review", "ambiguous",
        }
        player_id = self.database.upsert_player(self._database_values(player))
        checkpoint = Checkpoint(
            self.config.paths.checkpoints, self.database, player_id
        )
        state = checkpoint.load(player.name, player.provider_player_id)
        state.player_config_name = player.name
        state.provider = player.provider
        state.current_team_name = player.current_team_name
        self._prepare_season_attempt(player, state)
        if player.provider_player_id:
            self._save_resolution(
                player, player_id, checkpoint, state, "resolved",
                provider_player_id=player.provider_player_id,
            )
            self.database.update_player_resolution(
                player_id, "resolved", provider_player_id=player.provider_player_id,
                current_team_name=player.current_team_name, resolved_at=utc_now(),
            )
            self.config.set_player_resolution(
                player.name, "resolved", provider_player_id=player.provider_player_id
            )
            return "resolved"
        if existing and existing["provider_player_id"]:
            self._save_resolution(
                player, player_id, checkpoint, state, "resolved",
                provider_player_id=str(existing["provider_player_id"]),
            )
            self.database.update_player_resolution(
                player_id, "resolved", provider_player_id=str(existing["provider_player_id"]),
                current_team_name=player.current_team_name, resolved_at=utc_now(),
            )
            self.config.set_player_resolution(
                player.name, "resolved", provider_player_id=str(existing["provider_player_id"])
            )
            return "resolved"
        if player.resolution_status in {"needs_team_or_league", "needs_review", "ambiguous"}:
            if not retry_terminal:
                return player.resolution_status
        if player.resolution_status == "not_found" and not retry_terminal:
            return "not_found"
        if not (player.current_team_name or player.league_id or player.league_name):
            self._save_resolution(player, player_id, checkpoint, state, "needs_team_or_league")
            self.config.set_player_resolution(player.name, "needs_team_or_league")
            return "needs_team_or_league"

        state.current_stage = "resolve_player"
        state.status = "in_progress"
        season = state.resolution_season or self._resolution_season(player)
        state.resolution_season = season
        state.current_season = season
        if state.resolution_stage in {"pending", "paused_quota_limit"}:
            state.resolution_stage = "resolving_team" if player.current_team_name else "resolving_players_page"
        state.error_message = None
        checkpoint.save(state)
        client = APIClient(
            self.config, self.database, state, checkpoint.save,
        )
        self._client = client
        provider = APIFootballProvider(client)
        self._recover_saved_pages(player, checkpoint, state, player_id)
        self._sync_candidate_ids(player, state)
        try:
            if player.current_team_name:
                self._resolve_team_pages(
                    player, player_id, season, provider, client, checkpoint, state
                )
            else:
                self._resolve_league_pages(
                    player, player_id, season, provider, client, checkpoint, state
                )
        except DailyLimitReached as exc:
            state.resolution_stage = "paused_quota_limit"
            state.error_message = str(exc)
            state.status = "paused"
            self._sync_candidate_ids(player, state)
            self._save_attempt_state(player, state)
            checkpoint.save(state)
            self.config.set_player_resolution(player.name, "pending")
            return "paused_quota_limit"
        except Exception as exc:
            state.error_message = str(exc)
            checkpoint.save(state)
            raise

        if state.resolution_stage == "resolved":
            return "resolved"
        if state.status == "paused" and state.has_more_pages:
            return "pending"
        selected_pool = self._qualified_candidates(player, season)
        exact = [candidate for candidate in selected_pool if candidate["exact"]]
        selected_pool = exact or selected_pool
        if len(selected_pool) == 1:
            candidate = selected_pool[0]
            return self._finish_resolution(
                player, player_id, checkpoint, state, candidate, season
            )
        if selected_pool:
            self.database.update_player_resolution(player_id, "needs_review")
            self.database.mark_candidates_ambiguous(player.name, season=season)
            state.resolution_stage = "needs_review"
            return self._complete_season(
                player, player_id, checkpoint, state, season, "needs_review"
            )
        return self._complete_season(
            player, player_id, checkpoint, state, season, "not_found_for_season"
        )

    def prepare_resolution_status(self, player: PlayerConfig) -> CheckpointState:
        """Recover local page/checkpoint state without making API requests."""
        player_id = self.database.upsert_player(self._database_values(player))
        checkpoint = Checkpoint(self.config.paths.checkpoints, self.database, player_id)
        state = checkpoint.load(player.name, player.provider_player_id)
        state.player_config_name = player.name
        state.provider = player.provider
        state.current_team_name = player.current_team_name
        self._prepare_season_attempt(player, state)
        self._recover_saved_pages(player, checkpoint, state, player_id)
        self._sync_candidate_ids(player, state)
        checkpoint.save(state)
        return state

    def _resolve_team_pages(
        self,
        player: PlayerConfig,
        player_id: int,
        season: int,
        provider: APIFootballProvider,
        client: APIClient,
        checkpoint: Checkpoint,
        state: CheckpointState,
    ) -> None:
        if not state.resolved_team_id:
            state.resolution_stage = "resolving_team"
            state.current_page = 1
            state.has_more_pages = False
            checkpoint.save(state)
            client.set_request_context(
                player_id=player_id, season=season, stage="resolve_team",
                team_name=player.current_team_name,
            )
            teams = self._cached_call(
                player.name, player.provider,
                f"teams:{normalize_name(player.current_team_name or '')}",
                lambda: provider.search_teams(player.current_team_name or ""),
            )
            matches = self._match_teams(teams, player.current_team_name or "")
            if not matches:
                state.resolution_stage = "not_found"
                return
            team = matches[0].get("team", {})
            state.resolved_team_id = str(team.get("id", "")) or None
            state.resolved_team_name = str(team.get("name", "")) or player.current_team_name
            state.resolution_stage = "team_resolved"
            checkpoint.save(state)
        team_id = state.resolved_team_id
        if not team_id:
            state.resolution_stage = "not_found"
            return
        team_name = state.resolved_team_name or player.current_team_name or ""
        self._process_pages(
            player, player_id, season, team_id, team_name, provider, client,
            checkpoint, state, league=False,
        )

    def _resolve_league_pages(
        self,
        player: PlayerConfig,
        player_id: int,
        season: int,
        provider: APIFootballProvider,
        client: APIClient,
        checkpoint: Checkpoint,
        state: CheckpointState,
    ) -> None:
        league_id = player.league_id
        league_name = player.league_name or ""
        if not league_id:
            state.resolution_stage = "resolving_team"
            checkpoint.save(state)
            client.set_request_context(
                player_id=player_id, season=season, stage="resolve_league",
                league_name=league_name,
            )
            leagues = self._cached_call(
                player.name, player.provider,
                f"leagues:{normalize_name(league_name)}",
                lambda: provider.search_leagues(league_name),
            )
            matches = self._match_teams(leagues, league_name, entity_key="league")
            if len(matches) != 1:
                state.resolution_stage = "needs_review" if matches else "not_found"
                return
            league = matches[0].get("league", {})
            league_id = str(league.get("id", ""))
            state.resolved_team_id = league_id or None
            state.resolved_team_name = str(league.get("name", "")) or league_name
        else:
            state.resolved_team_id = str(league_id)
            state.resolved_team_name = league_name
        state.resolution_stage = "team_resolved"
        checkpoint.save(state)
        if state.resolved_team_id:
            self._process_pages(
                player, player_id, season, state.resolved_team_id,
                state.resolved_team_name or league_name, provider, client,
                checkpoint, state, league=True,
            )

    def _process_pages(
        self,
        player: PlayerConfig,
        player_id: int,
        season: int,
        entity_id: str,
        entity_name: str,
        provider: APIFootballProvider,
        client: APIClient,
        checkpoint: Checkpoint,
        state: CheckpointState,
        *,
        league: bool,
    ) -> None:
        page = max(state.current_page, (state.last_processed_page or 0) + 1)
        while page <= self.config.player_resolution_max_pages:
            state.current_page = page
            state.has_more_pages = True
            state.resolution_stage = "resolving_players_page"
            state.status = "in_progress"
            state.error_message = None
            self._save_attempt_state(player, state)
            checkpoint.save(state)
            page_payload = self.database.get_resolution_page(
                player.name, player.provider, entity_id, season, page,
                scope="league" if league else "team",
            )
            raw_path: str | None = None
            if page_payload is None:
                recovered = self._read_raw_player_page(entity_id, season, page)
                if recovered is not None:
                    rows, current, total, raw_path = recovered
                    page_payload = {
                        "response": rows, "paging": {"current": current, "total": total},
                        "raw_json_path": raw_path,
                    }
                    self.database.set_resolution_page(
                        player.name, player.provider, entity_id, season, page, page_payload,
                        scope="league" if league else "team",
                    )
            if page_payload is None:
                client.set_request_context(
                    player_id=player_id, season=season,
                    stage="resolve_league_players" if league else "resolve_team_players",
                    team_id=None if league else entity_id,
                    team_name=None if league else entity_name,
                    league_id=entity_id if league else None,
                    league_name=entity_name if league else None,
                    page_number=page,
                )
                if league:
                    rows, current, total = provider.get_players_by_league_page(
                        entity_id, season, page
                    )
                else:
                    rows, current, total = provider.get_players_by_team_page(
                        entity_id, season, page
                    )
                raw_path = (
                    str(client.last_raw_json_path) if client.last_raw_json_path else None
                )
                page_payload = {
                    "response": rows, "paging": {"current": current, "total": total},
                    "raw_json_path": raw_path,
                }
                self.database.set_resolution_page(
                    player.name, player.provider, entity_id, season, page, page_payload,
                    scope="league" if league else "team",
                )
            rows = page_payload.get("response", [])
            paging = page_payload.get("paging", {})
            current = int(paging.get("current", page))
            total = int(paging.get("total", current))
            self._collect_candidates(
                player, rows, season, entity_id, entity_name, {},
                source_page=current, raw_json_path=page_payload.get("raw_json_path"),
            )
            state.last_processed_page = current
            state.current_page = current + 1
            state.resolution_total_pages = total
            state.has_more_pages = current < total
            state.resolution_stage = "players_page_resolved"
            state.candidates_processed += len(rows)
            self._sync_candidate_ids(player, state)
            self._save_attempt_state(player, state)
            checkpoint.save(state)
            exact = [
                candidate for candidate in self._qualified_candidates(player, season)
                if candidate["exact"]
            ]
            if len(exact) == 1:
                self._finish_resolution(
                    player, player_id, checkpoint, state, exact[0], season
                )
                return
            if current >= total:
                self.database.mark_resolution_search_complete(
                    player.name, player.provider, season,
                    league_id=entity_id if league else None,
                    team_id=None if league else entity_id,
                )
                state.has_more_pages = False
                state.resolution_stage = "matching_candidates"
                checkpoint.save(state)
                return
            page = current + 1
        state.current_page = page
        state.has_more_pages = True
        state.resolution_stage = "resolving_players_page"
        state.error_message = (
            f"Resolution stopped at configured maximum of "
            f"{self.config.player_resolution_max_pages} pages."
        )
        state.status = "paused"
        self._sync_candidate_ids(player, state)
        self._save_attempt_state(player, state)
        checkpoint.save(state)

    def _recover_saved_pages(
        self,
        player: PlayerConfig,
        checkpoint: Checkpoint,
        state: CheckpointState,
        player_id: int,
    ) -> None:
        """Import successful raw pages left by a pre-pagination resolver run."""
        team_cache = self.database.get_resolution_cache(
            player.name, player.provider,
            f"teams:{normalize_name(player.current_team_name or '')}",
        )
        if not state.resolved_team_id and team_cache and player.current_team_name:
            matches = self._match_teams(team_cache, player.current_team_name)
            if len(matches) == 1:
                team = matches[0].get("team", {})
                state.resolved_team_id = str(team.get("id", "")) or None
                state.resolved_team_name = str(team.get("name", "")) or player.current_team_name
                state.resolution_stage = "team_resolved"
        if not state.resolved_team_id or state.resolution_season is None:
            return
        raw_paths = self._raw_player_files(player.name)
        raw_paths.sort(key=self._raw_page_number)
        for raw_path in raw_paths:
            try:
                payload = json.loads(raw_path.read_text(encoding="utf-8"))
                params = payload.get("parameters", {})
                if (
                    payload.get("get") != "players"
                    or payload.get("errors")
                    or str(params.get("team")) != state.resolved_team_id
                    or int(params.get("season", -1)) != state.resolution_season
                ):
                    continue
                page = int(params.get("page", 1))
                self.database.backfill_resolution_request_page(
                    player_id, state.resolved_team_id, state.resolution_season, page
                )
                if self.database.get_resolution_page(
                    player.name, player.provider, state.resolved_team_id,
                    state.resolution_season, page,
                ):
                    continue
                paging = payload.get("paging", {})
                cached = {
                    "response": payload.get("response", []),
                    "paging": paging,
                    "raw_json_path": str(raw_path),
                }
                self.database.set_resolution_page(
                    player.name, player.provider, state.resolved_team_id,
                    state.resolution_season, page, cached,
                )
                self._collect_candidates(
                    player, cached["response"], state.resolution_season,
                    state.resolved_team_id, state.resolved_team_name or "",
                    {}, source_page=page, raw_json_path=str(raw_path),
                )
                state.candidates_processed += len(cached["response"])
                current = int(paging.get("current", page))
                total = int(paging.get("total", current))
                state.last_processed_page = max(state.last_processed_page or 0, current)
                state.current_page = max(state.current_page, current + 1)
                state.resolution_total_pages = total
                state.has_more_pages = state.current_page <= total
                state.resolution_stage = (
                    "resolving_players_page" if state.has_more_pages
                    else "players_page_resolved"
                )
                state.status = "paused" if state.has_more_pages else "in_progress"
                state.current_season = state.resolution_season
                state.requests_used_today = self.database.get_daily_request_usage_for_player(
                    datetime.now(timezone.utc).date().isoformat(), player.name
                )
                state.request_date = datetime.now(timezone.utc).date().isoformat()
                self._sync_candidate_ids(player, state)
                checkpoint.save(state)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue

    def _prepare_season_attempt(
        self, player: PlayerConfig, state: CheckpointState
    ) -> None:
        """Migrate legacy page state and select only the next configured season."""
        completed = state.resolution_completed_attempts
        attempted = list(player.attempted_resolution_seasons)
        previous_season = state.resolution_season
        if (
            previous_season in attempted
            and previous_season not in [item.get("season") for item in completed]
            and state.last_processed_page is not None
        ):
            outcome = "not_found_for_season"
            if state.resolution_stage in {"needs_review", "ambiguous"}:
                outcome = "needs_review"
            record = {
                "season": previous_season, "team_id": state.resolved_team_id,
                "team_name": state.resolved_team_name or player.current_team_name,
                "status": outcome,
                "pages_processed": list(range(1, state.last_processed_page + 1)),
                "candidates_found": self._candidate_count(
                    player, previous_season, state.resolved_team_id
                ),
                "high_confidence_match": False, "completed_at": utc_now(),
            }
            completed.append(record)
            self.database.save_resolution_attempt(
                self._attempt_id(player, previous_season, state.resolved_team_id),
                player.name, player.provider, previous_season,
                state.resolved_team_id, state.resolved_team_name, outcome, record,
            )
            state.resolution_completed_attempts = completed
        for record in state.resolution_completed_attempts:
            record["candidates_found"] = self._candidate_count(
                player, int(record["season"]), record.get("team_id")
            )
            self.database.save_resolution_attempt(
                self._attempt_id(player, int(record["season"]), record.get("team_id")),
                player.name, player.provider, int(record["season"]),
                record.get("team_id"), record.get("team_name"),
                str(record.get("status", "pending")), record,
            )

        pending = list(player.resolution_seasons)
        if pending:
            season = pending[0]
            current = state.resolution_current_attempt or {}
            if current.get("season") != season:
                saved = self.database.get_resolution_attempt(
                    player.name, player.provider, season
                )
                if saved is not None:
                    current = json.loads(saved["state_json"])
                else:
                    current = {
                        "season": season, "team_id": state.resolved_team_id,
                        "team_name": state.resolved_team_name or player.current_team_name,
                        "stage": "pending", "current_page": 1, "pages_processed": [],
                        "has_more_pages": None, "candidates_found": 0,
                        "last_processed_page": None,
                    }
            state.resolution_current_attempt = current
            state.resolution_season = season
            state.current_season = season
            state.resolved_team_id = current.get("team_id") or state.resolved_team_id
            state.resolved_team_name = current.get("team_name") or state.resolved_team_name
            state.current_page = int(current.get("current_page", 1))
            state.last_processed_page = current.get("last_processed_page")
            state.has_more_pages = bool(current.get("has_more_pages"))
            state.candidates_processed = 0
            state.candidates_found = []
            state.resolution_stage = str(current.get("stage", "pending"))
            state.status = (
                "paused" if state.resolution_stage == "paused_quota_limit"
                else "in_progress"
            )
        elif state.resolution_season is None:
            state.resolution_season = self._resolution_season(player)
            state.current_season = state.resolution_season

    def _save_attempt_state(
        self, player: PlayerConfig, state: CheckpointState
    ) -> None:
        """Persist the active season's independent pagination state."""
        season = state.resolution_season
        if season is None:
            return
        current = state.resolution_current_attempt or {}
        current.update({
            "season": season, "team_id": state.resolved_team_id,
            "team_name": state.resolved_team_name or player.current_team_name,
            "stage": state.resolution_stage, "current_page": state.current_page,
            "pages_processed": list(range(1, (state.last_processed_page or 0) + 1)),
            "has_more_pages": state.has_more_pages,
            "candidates_found": len(state.candidates_found),
            "last_processed_page": state.last_processed_page,
            "requests_used_today": state.requests_used_today,
            "request_date": state.request_date, "error_message": state.error_message,
        })
        state.resolution_current_attempt = current
        self.database.save_resolution_attempt(
            self._attempt_id(player, season, state.resolved_team_id),
            player.name, player.provider, season, state.resolved_team_id,
            state.resolved_team_name, state.resolution_stage, current,
        )

    def _complete_season(
        self, player: PlayerConfig, player_id: int, checkpoint: Checkpoint,
        state: CheckpointState, season: int, outcome: str,
    ) -> str:
        """Record one season outcome and continue only through configured seasons."""
        record = {
            "season": season, "team_id": state.resolved_team_id,
            "team_name": state.resolved_team_name or player.current_team_name,
            "status": outcome,
            "pages_processed": list(range(1, (state.last_processed_page or 0) + 1)),
            "candidates_found": len(state.candidates_found),
            "high_confidence_match": False, "completed_at": utc_now(),
        }
        state.resolution_completed_attempts = [
            item for item in state.resolution_completed_attempts
            if item.get("season") != season
        ] + [record]
        state.resolution_current_attempt = None
        self.database.save_resolution_attempt(
            self._attempt_id(player, season, state.resolved_team_id),
            player.name, player.provider, season, state.resolved_team_id,
            state.resolved_team_name, outcome, record,
        )
        remaining = [value for value in player.resolution_seasons if value != season]
        attempted = list(dict.fromkeys([*player.attempted_resolution_seasons, season]))
        self.config.update_player_progress(
            player.name, resolution_seasons=remaining,
            attempted_resolution_seasons=attempted,
        )
        if remaining and outcome != "resolved":
            state.resolution_stage = "pending"
            state.current_page = 1
            state.last_processed_page = None
            state.has_more_pages = False
            state.candidates_found = []
            state.candidates_processed = 0
            state.resolution_season = remaining[0]
            state.current_season = remaining[0]
            state.status = "in_progress"
            checkpoint.save(state)
            self.config.set_player_resolution(player.name, "pending")
            next_player = self.config.get_player(player.name)
            if next_player is None:
                raise RuntimeError(f"Player disappeared from configuration: {player.name}")
            return self.resolve(next_player, retry_terminal=True)

        if outcome == "resolved":
            return "resolved"
        plausible = any(
            item["status"] in {"needs_review", "ambiguous"}
            for item in state.resolution_completed_attempts
        )
        global_status = "needs_review" if plausible else "not_found"
        state.resolution_stage = global_status
        self._save_resolution(player, player_id, checkpoint, state, global_status)
        self.database.update_player_resolution(player_id, global_status)
        self.config.set_player_resolution(player.name, global_status)
        return global_status

    @staticmethod
    def _attempt_id(
        player: PlayerConfig, season: int, team_id: str | None
    ) -> str:
        return f"{player.name}|{player.provider}|{season}|{team_id or ''}"

    def _raw_player_files(self, player_name: str) -> list[Path]:
        slug = re.sub(r"[^a-z0-9]+", "_", player_name.casefold()).strip("_") or "player"
        return list((self.config.paths.json / slug).glob("players_*.json"))

    @staticmethod
    def _raw_page_number(path: Path) -> int:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return int(payload.get("parameters", {}).get("page", 1))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return 0

    def _read_raw_player_page(
        self, team_id: str, season: int, page: int
    ) -> tuple[list[dict[str, Any]], int, int, str] | None:
        for path in self._raw_player_files(self._client.checkpoint.player_name if self._client else ""):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                params = payload.get("parameters", {})
                if (
                    payload.get("get") == "players"
                    and not payload.get("errors")
                    and str(params.get("team")) == team_id
                    and int(params.get("season", -1)) == season
                    and int(params.get("page", 1)) == page
                ):
                    paging = payload.get("paging", {})
                    return (
                        payload.get("response", []),
                        int(paging.get("current", page)),
                        int(paging.get("total", page)),
                        str(path),
                    )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return None

    def _sync_candidate_ids(self, player: PlayerConfig, state: CheckpointState) -> None:
        state.candidates_found = [
            int(row["id"]) for row in self.database.list_resolution_candidates(
                player.name, state.resolution_season
            )
            if row["provider_player_id"] is not None
        ]

    def _candidate_count(
        self, player: PlayerConfig, season: int, team_id: str | None
    ) -> int:
        """Count saved candidate evidence for one team-season attempt."""
        return sum(
            1 for row in self.database.list_resolution_candidates(player.name, season)
            if row["provider_player_id"] is not None
            and (team_id is None or str(row["provider_team_id"]) == str(team_id))
        )

    def _qualified_candidates(
        self, player: PlayerConfig, season: int | None = None
    ) -> list[dict[str, Any]]:
        candidates = []
        for row in self.database.list_resolution_candidates(player.name, season):
            if row["provider_player_id"] is None or row["name_similarity"] is None:
                continue
            if float(row["name_similarity"]) < self.config.player_resolution_min_similarity:
                continue
            value = dict(row)
            value["exact"] = normalize_name(player.name) == normalize_name(
                str(value.get("matched_name") or "")
            )
            candidates.append(value)
        exact = [item for item in candidates if item["exact"]]
        return exact or candidates

    def _finish_resolution(
        self,
        player: PlayerConfig,
        player_id: int,
        checkpoint: Checkpoint,
        state: CheckpointState,
        candidate: dict[str, Any],
        season: int,
    ) -> str:
        self.database.upsert_player_candidate({**candidate, "status": "selected"})
        with self.database.transaction() as connection:
            connection.execute(
                """UPDATE player_candidates SET status=CASE
                WHEN provider_player_id=? THEN 'selected' ELSE 'rejected' END,
                updated_at=? WHERE player_config_name=? COLLATE NOCASE AND season=?""",
                (
                    candidate["provider_player_id"], utc_now(), player.name, season,
                ),
            )
        state.resolution_stage = "resolved"
        state.error_message = None
        self._save_resolution(
            player, player_id, checkpoint, state, "resolved",
            provider_player_id=candidate["provider_player_id"],
            current_team_name=candidate["provider_team_name"],
            resolution_season=season,
        )
        self.database.upsert_player({
            **self._database_values(player),
            "provider_player_id": candidate["provider_player_id"],
            "resolution_status": "resolved",
            "current_team_name": candidate["provider_team_name"],
            "resolved_at": utc_now(),
        })
        self.database.update_player_resolution(
            player_id, "resolved", provider_player_id=candidate["provider_player_id"],
            current_team_name=candidate["provider_team_name"], resolved_at=utc_now(),
        )
        self.config.set_player_resolution(
            player.name, "resolved",
            provider_player_id=candidate["provider_player_id"],
            current_team_name=candidate["provider_team_name"],
            resolution_season=season,
        )
        completed = state.resolution_completed_attempts
        completed.append({
            "season": season, "team_id": state.resolved_team_id,
            "team_name": state.resolved_team_name or player.current_team_name,
            "status": "resolved",
            "pages_processed": list(range(1, (state.last_processed_page or 0) + 1)),
            "candidates_found": len(state.candidates_found),
            "high_confidence_match": True, "completed_at": utc_now(),
        })
        state.resolution_completed_attempts = completed
        state.resolution_current_attempt = None
        self.config.update_player_progress(
            player.name,
            resolution_seasons=[],
            attempted_resolution_seasons=list(dict.fromkeys(
                [*player.attempted_resolution_seasons, season]
            )),
        )
        self.database.save_resolution_attempt(
            self._attempt_id(player, season, state.resolved_team_id),
            player.name, player.provider, season, state.resolved_team_id,
            state.resolved_team_name, "resolved",
            state.resolution_completed_attempts[-1],
        )
        checkpoint.save(state)
        return "resolved"

    def _resolution_season(self, player: PlayerConfig) -> int:
        cached = self.database.get_latest_supported_season()
        if cached is not None:
            return cached
        if player.resolution_season is not None:
            return player.resolution_season
        if player.crawl_to_season != "current":
            return min(int(player.crawl_to_season), max(player.crawl_from_season, 2024))
        return player.crawl_from_season

    def _cached_call(
        self, player_name: str, provider: str, cache_key: str, call: Any
    ) -> list[dict[str, Any]]:
        cached = (
            None if self._force_refresh
            else self.database.get_resolution_cache(player_name, provider, cache_key)
        )
        if cached is not None:
            return cached
        value = call()
        self.database.set_resolution_cache(player_name, provider, cache_key, value)
        return value

    def _match_teams(
        self, rows: list[dict[str, Any]], expected: str, *, entity_key: str = "team"
    ) -> list[dict[str, Any]]:
        expected_normalized = normalize_name(expected)
        exact = [
            row for row in rows
            if normalize_name(str((row.get(entity_key) or {}).get("name", ""))) == expected_normalized
        ]
        if exact:
            return exact
        return [
            row for row in rows
            if name_similarity(str((row.get(entity_key) or {}).get("name", "")), expected)
            >= self.config.player_resolution_min_similarity
        ]

    def _collect_candidates(
        self,
        player: PlayerConfig,
        rows: list[dict[str, Any]],
        season: int,
        team_id: str,
        team_name: str,
        output: dict[tuple[str, str], dict[str, Any]],
        *,
        source_page: int | None = None,
        raw_json_path: str | None = None,
    ) -> None:
        for row in rows:
            player_data = row.get("player", row)
            if not isinstance(player_data, dict) or player_data.get("id") is None:
                continue
            matched_name = str(player_data.get("name", ""))
            score = name_similarity(player.name, matched_name)
            stats = row.get("statistics") or []
            actual_team = {}
            if stats and isinstance(stats[0], dict):
                actual_team = stats[0].get("team") or {}
            candidate_team_id = str(actual_team.get("id", team_id))
            candidate_team_name = str(actual_team.get("name", team_name))
            if player.current_team_name and candidate_team_name and (
                name_similarity(candidate_team_name, player.current_team_name) < 0.85
            ):
                continue
            candidate = {
                "player_config_name": player.name,
                "provider": player.provider,
                "provider_player_id": str(player_data["id"]),
                "provider_team_id": candidate_team_id,
                "provider_team_name": candidate_team_name,
                "season": season,
                "source_page": source_page,
                "attempt_id": self._attempt_id(player, season, team_id),
                "matched_name": matched_name,
                "name_similarity": score,
                "status": (
                    "candidate" if score >= self.config.player_resolution_min_similarity
                    else "rejected"
                ),
                "raw_json_path": raw_json_path or (
                    str(self._client.last_raw_json_path)
                    if self._client and self._client.last_raw_json_path else None
                ),
                "exact": normalize_name(player.name) == normalize_name(matched_name),
            }
            key = (candidate["provider_player_id"], candidate_team_id)
            prior = output.get(key)
            if prior is None or score > prior["name_similarity"]:
                output[key] = candidate
            self.database.upsert_player_candidate(candidate)

    @staticmethod
    def _database_values(player: PlayerConfig) -> dict[str, Any]:
        return {
            "provider_player_id": player.provider_player_id,
            "provider": player.provider,
            "resolution_status": player.resolution_status,
            "current_team_name": player.current_team_name,
            "name": player.name,
            "status": "pending",
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        }

    @staticmethod
    def _save_resolution(
        player: PlayerConfig,
        player_id: int,
        checkpoint: Checkpoint,
        state: CheckpointState,
        status: str,
        *,
        provider_player_id: str | None = None,
        current_team_name: str | None = None,
        resolution_season: int | None = None,
    ) -> None:
        state.provider_player_id = provider_player_id or state.provider_player_id
        state.status = status
        state.resolution_stage = status
        state.current_stage = "resolve_player"
        state.current_season = resolution_season or state.current_season
        state.resolution_season = resolution_season or state.resolution_season
        checkpoint.bind_player(player_id)
        checkpoint.save(state)
