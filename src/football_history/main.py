"""Command-line entry point for football history collection."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from rich.console import Console

from . import __version__
from .checkpoint import Checkpoint
from .config import Config, ConfigError
from .database import Database
from .pipeline import Pipeline
from .models import PlayerConfig
from .progress import configure_logging
from .resolution import PlayerResolver

console = Console()


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="football-history",
        description="Resumable football player match-history collection pipeline",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__version__,
        help="show the application version and exit",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Show the application name and version")
    subparsers.add_parser("run", help="Start or continue all configured crawls")
    subparsers.add_parser("resume", help="Resume all crawls from their checkpoints")
    subparsers.add_parser("status", help="Show crawl and request progress")
    subparsers.add_parser("requests", help="Show today's request-budget usage")
    seasons = subparsers.add_parser("seasons", help="Show a player's supported season progress")
    seasons.add_argument("--player", required=True)
    reset = subparsers.add_parser("reset", help="Remove checkpoint/progress for a player")
    reset.add_argument("--player", required=True)
    reset.add_argument("--delete-data", action="store_true",
                       help="Also delete the player's stored database records")
    subparsers.add_parser("validate-config", help="Validate project and player YAML configuration")
    show = subparsers.add_parser("show-player", help="Show one player's crawl settings and progress")
    show.add_argument("--name", required=True)
    subparsers.add_parser("resolve-players", help="Resolve pending players with sufficient filters")
    resolve = subparsers.add_parser("resolve-player", help="Resolve one configured player")
    resolve.add_argument("--name", required=True)
    resolution_status = subparsers.add_parser(
        "resolution-status", help="Show resumable identity-resolution progress"
    )
    resolution_status.add_argument("--name", required=True)
    candidates = subparsers.add_parser("candidates", help="List player identity candidates")
    candidates.add_argument("--name", required=True)
    candidates.add_argument("--season", type=int)
    retry = subparsers.add_parser(
        "retry-resolution", help="Reset and retry one player's season resolution"
    )
    retry.add_argument("--name", required=True)
    retry.add_argument("--season", type=int, required=True)
    select = subparsers.add_parser("select-player", help="Select a candidate player ID")
    select.add_argument("--name", required=True)
    select.add_argument("--candidate-id", type=int, required=True)
    add = subparsers.add_parser("add-player", help="Add a player to the crawl queue")
    add.add_argument("--name", required=True)
    add.add_argument("--team")
    add.add_argument("--league")
    add.add_argument("--league-id")
    add.add_argument("--season", type=int)
    clean = subparsers.add_parser("clean", help="Safely remove selected generated data")
    clean_group = clean.add_mutually_exclusive_group(required=True)
    for category in (
        "temp", "logs", "downloads", "json", "checkpoints", "database", "output", "all",
    ):
        clean_group.add_argument(f"--{category}", action="store_true")
    clean.add_argument("--yes", action="store_true")
    clean.add_argument("--drop-database", action="store_true")
    clean.add_argument("--include-player-config", action="store_true")
    reset_player = subparsers.add_parser("reset-player", help="Reset one player's runtime state")
    reset_player.add_argument("--name", required=True)
    reset_player.add_argument("--delete-data", action="store_true")
    reset_player.add_argument("--keep-candidates", action="store_true")
    reset_player.add_argument("--remove-from-config", action="store_true")
    reset_player.add_argument("--yes", action="store_true")
    reset_crawl = subparsers.add_parser("reset-crawl", help="Reset crawl progress for one player")
    reset_crawl.add_argument("--name", required=True)
    reset_resolution = subparsers.add_parser(
        "reset-resolution", help="Reset one player's identity-resolution state"
    )
    reset_resolution.add_argument("--name", required=True)
    reset_resolution.add_argument("--delete-data", action="store_true")
    reset_resolution.add_argument("--yes", action="store_true")
    reset_players = subparsers.add_parser(
        "reset-players", help="Reset runtime state for configured players"
    )
    reset_players.add_argument("--restore-defaults", action="store_true")
    reset_players.add_argument("--yes", action="store_true")
    reset_project = subparsers.add_parser(
        "reset-project", help="Restore default players and clear project runtime data"
    )
    reset_project.add_argument("--yes", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run a CLI command and return its process exit code."""
    args = build_parser().parse_args(argv)
    if args.command == "version":
        console.print(f"football-player-history {__version__}")
        return 0
    try:
        config = Config()
        logger = (
            configure_logging(
                config.paths.logs,
                getattr(logging, config.logs["level"].upper()),
                console_enabled=config.logs["console"],
                file_enabled=config.logs["file"],
            )
            if args.command in {"run", "resume"}
            else logging.getLogger("football_history")
        )
        if args.command == "validate-config":
            _validate(config)
            return 0
        database = Database(config.paths.database)
        if args.command in ("run", "resume"):
            config.api_key
            console.print(f"football-player-history v{__version__}")
            logger.info("football-player-history v%s", __version__)
            for player in config.players:
                console.print(f"Starting crawl for: {player.name}")
                logger.info("Starting crawl for: %s", player.name)
            summary = Pipeline(config, database=database, logger=logger).run(
                resume=args.command == "resume"
            )
            if summary["errors"]:
                return 1
        elif args.command == "status":
            _status(config, database)
        elif args.command == "requests":
            _show_requests(config, database)
        elif args.command == "seasons":
            _show_seasons(config, database, args.player)
        elif args.command == "reset":
            _reset(config, database, args.player, args.delete_data)
        elif args.command == "show-player":
            _show_player(config, database, args.name)
        elif args.command == "add-player":
            _add_player(
                config, database, args.name, args.team, args.league,
                args.league_id, args.season,
            )
        elif args.command == "resolve-player":
            _resolve_player(config, database, args.name, retry_terminal=True)
        elif args.command == "resolution-status":
            _show_resolution_status(config, database, args.name)
        elif args.command == "retry-resolution":
            _retry_resolution(config, database, args.name, args.season)
        elif args.command == "resolve-players":
            _resolve_players(config, database)
        elif args.command == "candidates":
            _show_candidates(database, args.name, args.season)
        elif args.command == "select-player":
            _select_player(config, database, args.name, args.candidate_id)
        elif args.command == "clean":
            _clean(
                config, database, _clean_categories(args),
                yes=args.yes, drop_database=args.drop_database,
                include_player_config=args.include_player_config,
            )
        elif args.command == "reset-player":
            _reset_player(
                config, database, args.name, delete_data=args.delete_data,
                keep_candidates=args.keep_candidates,
                remove_from_config=args.remove_from_config, yes=args.yes,
            )
        elif args.command == "reset-crawl":
            _reset_crawl(config, database, args.name)
        elif args.command == "reset-resolution":
            _reset_resolution(
                config, database, args.name,
                delete_data=args.delete_data, yes=args.yes,
            )
        elif args.command == "reset-players":
            _reset_players(
                config, database, restore_defaults=args.restore_defaults, yes=args.yes
            )
        elif args.command == "reset-project":
            _reset_project(config, database, yes=args.yes)
        return 0
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted. Saved progress can be resumed.[/yellow]")
        return 130
    except (ConfigError, OSError, ValueError, RuntimeError, LookupError) as exc:
        console.print(f"[red]Error: {exc}[/red]")
        return 1


def _validate(config: Config) -> None:
    console.print("Configuration is valid.")
    console.print(f"Project settings: {config.project_config_path}")
    console.print(f"Player inputs: {config.config_path}")
    for name, path in (
        ("Project root", config.project_root), ("Downloads", config.paths.downloads),
        ("Output", config.paths.output), ("Checkpoints", config.paths.checkpoints),
        ("JSON", config.paths.json), ("Temporary files", config.paths.temp),
        ("Logs", config.paths.logs), ("Database", config.paths.database),
    ):
        console.print(f"{name}: {path}")
    console.print(f"Players configured: {len(config.players)}")


def _status(config: Config, database: Database) -> None:
    for settings in _all_player_settings(config):
        name = str(_setting(settings, "name"))
        player = database.get_player(name)
        player_id = int(player["id"]) if player else None
        checkpoint = Checkpoint(config.paths.checkpoints, database, player_id).load(
            name, _setting(settings, "provider_player_id")
        )
        statuses = database.get_season_statuses(player_id) if player_id is not None else {}
        completed = sorted(
            season for season, status in statuses.items() if status == "completed"
        )
        unsupported = sorted(set(checkpoint.unsupported_seasons) | {
            season for season, status in statuses.items()
            if status == "unsupported_for_subscription"
        })
        configured_end = _season_end(settings)
        support_range = _format_range(
            checkpoint.supported_season_min, checkpoint.supported_season_max
        )
        used = (
            database.get_daily_request_usage_for_player(
                datetime.now(timezone.utc).date().isoformat(), name
            )
            if player else checkpoint.requests_used_today
        )
        console.print(f"Player: {name}")
        console.print(f"  Provider player ID: {_setting(settings, 'provider_player_id') or '-'}")
        console.print(
            f"  Configured crawl period: {_setting(settings, 'crawl_from_season')}-{configured_end}"
        )
        console.print(
            f"  Desired historical period: "
            f"{_setting(settings, 'desired_historical_from_season') or _setting(settings, 'crawl_from_season')}"
            f"-{configured_end}"
        )
        console.print(f"  Supported subscription range: {support_range}")
        console.print(f"  Completed seasons: {', '.join(map(str, completed)) or '-'}")
        console.print(f"  Skipped unsupported seasons: {', '.join(map(str, unsupported)) or '-'}")
        console.print(f"  Current season: {checkpoint.current_season or '-'}")
        console.print(f"  Current stage: {checkpoint.current_stage}")
        console.print(
            f"  Fixtures processed: {checkpoint.total_fixtures_processed}/"
            f"{checkpoint.total_fixtures_found}"
        )
        console.print(f"  Requests used today: {used}")


def _show_requests(config: Config, database: Database) -> None:
    """Display global and per-player request usage and the next UTC reset."""
    today = datetime.now(timezone.utc).date()
    request_date = today.isoformat()
    configured_limit = config.api.requests_per_day
    effective_limit = max(0, configured_limit - config.api.daily_request_safety_margin)
    reset_date = today.fromordinal(today.toordinal() + 1)
    reset_at = datetime.combine(reset_date, datetime.min.time(), tzinfo=timezone.utc)
    report = database.get_request_report(request_date)
    console.print(f"API request report — {request_date}")
    console.print(f"Daily limit: {configured_limit}")
    console.print(f"Enforced limit: {effective_limit}")
    console.print(f"Used today: {report['total']}")
    console.print(f"Remaining: {max(0, effective_limit - report['total'])}")
    console.print(f"Attributed: {report['attributed']}")
    console.print(f"Unattributed: {report['unattributed']}")
    console.print(f"Safety margin: {config.api.daily_request_safety_margin}")
    console.print(f"Budget resets at: {reset_at.isoformat()}")
    console.print("By player:")
    _print_counts(report["by_player"])
    console.print("By stage:")
    _print_counts(report["by_stage"])
    console.print("By endpoint:")
    _print_counts(report["by_endpoint"])


def _show_seasons(config: Config, database: Database, name: str) -> None:
    settings = next(
        (item for item in _all_player_settings(config)
         if str(_setting(item, "name")).casefold() == name.casefold()),
        None,
    )
    if settings is None:
        raise ConfigError(f"Player is not present in configuration: {name}")
    player = database.get_player(name)
    player_id = int(player["id"]) if player else None
    state = Checkpoint(config.paths.checkpoints, database, player_id).load(
        name, _setting(settings, "provider_player_id")
    )
    end = _season_end(settings)
    start = int(_setting(settings, "crawl_from_season"))
    minimum, maximum = state.supported_season_min, state.supported_season_max
    supported_start = max(start, minimum) if minimum is not None else None
    supported_end = min(end, maximum) if maximum is not None else None
    statuses = database.get_season_statuses(player_id) if player_id is not None else {}
    unsupported = sorted(set(state.unsupported_seasons) | {
        season for season, status in statuses.items()
        if status == "unsupported_for_subscription"
    })
    completed = sorted(
        season for season, status in statuses.items() if status == "completed"
    )
    pending = sorted(
        season for season in state.seasons_to_crawl
        if season not in completed and season not in unsupported
    )
    console.print(f"Player: {name}")
    console.print(f"Configured seasons: {start}-{end}")
    supported_range = (
        _format_range(supported_start, supported_end)
        if supported_start is not None and supported_end is not None
        and supported_start <= supported_end
        else "none in configured period"
        if minimum is not None and maximum is not None
        else "not discovered"
    )
    console.print(
        f"Supported seasons: {supported_range}"
    )
    console.print(f"Unsupported seasons: {', '.join(map(str, unsupported)) or '-'}")
    console.print(f"Completed seasons: {', '.join(map(str, completed)) or '-'}")
    console.print(f"Pending seasons: {', '.join(map(str, pending)) or '-'}")


def _all_player_settings(config: Config) -> list[PlayerConfig | dict[str, Any]]:
    return [*config.players, *config.completed_players]


def _setting(settings: PlayerConfig | dict[str, Any], key: str) -> Any:
    if isinstance(settings, dict):
        return settings.get(key)
    return getattr(settings, key, None)


def _season_end(settings: PlayerConfig | dict[str, Any]) -> int:
    value = _setting(settings, "crawl_to_season")
    return datetime.now().year if value == "current" else int(value)


def _format_range(minimum: int | None, maximum: int | None) -> str:
    if minimum is None or maximum is None:
        return "not discovered"
    return f"{minimum}-{maximum}"


def _print_counts(counts: object) -> None:
    if not isinstance(counts, dict) or not counts:
        console.print("  - None")
        return
    for key, count in counts.items():
        console.print(f"  - {key}: {count}")


def _add_player(
    config: Config,
    database: Database,
    name: str,
    team: str | None = None,
    league: str | None = None,
    league_id: str | None = None,
    season: int | None = None,
) -> None:
    """Add a player to YAML and SQLite without contacting the provider."""
    player = config.add_player(
        name, team_name=team, league_name=league, league_id=league_id,
        resolution_season=season,
    )
    database.upsert_player({
        "name": player.name,
        "provider": player.provider,
        "provider_player_id": player.provider_player_id,
        "resolution_status": player.resolution_status,
        "current_team_name": player.current_team_name,
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    console.print(
        f"Added {player.name}; resolution status: {player.resolution_status}. "
        "No API requests were made."
    )


def _resolve_player(
    config: Config, database: Database, name: str, *, retry_terminal: bool
) -> None:
    """Resolve one configured player using the budgeted API client."""
    player = config.get_player(name)
    if player is None:
        raise ConfigError(f"Player is not in the crawl queue: {name}")
    state = PlayerResolver(config, database).prepare_resolution_status(player)
    console.print(
        f"Resolution starting: stage {state.resolution_stage}, "
        f"team {state.resolved_team_name or player.current_team_name or '-'} "
        f"({state.resolved_team_id or '-'}), season {state.resolution_season or '-'}, "
        f"page {state.current_page}, candidates {len(state.candidates_found)}."
    )
    status = PlayerResolver(config, database).resolve(
        player, retry_terminal=retry_terminal
    )
    console.print(f"{player.name}: resolution status {status}.")
    if status == "paused_quota_limit":
        state = PlayerResolver(config, database).prepare_resolution_status(player)
        today = datetime.now(timezone.utc).date().isoformat()
        used = database.get_daily_request_usage(today)
        console.print("Resolution paused: daily API request limit reached.")
        console.print(f"Player: {player.name}")
        console.print(
            f"Team: {state.resolved_team_name or player.current_team_name or '-'} "
            f"({state.resolved_team_id or '-'})"
        )
        console.print(f"Season: {state.resolution_season or '-'}")
        action = (
            f"fetch players page {state.current_page}"
            if state.resolved_team_id else "resolve team"
        )
        console.print(f"Next action: {action}")
        console.print(f"Requests used today: {used}/{config.api.requests_per_day}")
    elif status == "pending":
        _show_resolution_status(config, database, name)
    elif status == "needs_team_or_league":
        console.print(
            "Provide --team, --league with --season, or a provider_player_id."
        )
    elif status == "needs_review":
        console.print(f"Review candidates with: python -m football_history.main candidates --name \"{player.name}\"")
    elif status == "not_found":
        console.print(
            "No sufficiently similar match was found. Add a team/league filter or provider_player_id."
        )


def _show_resolution_status(config: Config, database: Database, name: str) -> None:
    """Display page-level resolution progress without making API requests."""
    player = config.get_player(name)
    if player is None:
        raise ConfigError(f"Player is not in the crawl queue: {name}")
    state = PlayerResolver(config, database).prepare_resolution_status(player)
    today = datetime.now(timezone.utc).date().isoformat()
    used = database.get_daily_request_usage_for_player(today, player.name)
    pages_processed = state.last_processed_page or 0
    if state.resolution_stage == "resolved":
        next_action = "none; player is resolved"
    elif state.has_more_pages:
        next_action = f"fetch players page {state.current_page}"
    elif state.resolution_stage == "needs_team_or_league":
        next_action = "provide a team, league, or provider player ID"
    else:
        next_action = "resume player resolution"
    console.print(f"Player: {player.name}")
    console.print(f"Resolution status: {player.resolution_status}")
    console.print(
        f"Attempted seasons: {', '.join(map(str, player.attempted_resolution_seasons)) or '-'}"
    )
    console.print(
        f"Pending seasons: {', '.join(map(str, player.resolution_seasons)) or '-'}"
    )
    console.print(
        f"Resolved team: {state.resolved_team_name or player.current_team_name or '-'} "
        f"({state.resolved_team_id or '-'})"
    )
    console.print(f"Resolution season: {state.resolution_season or '-'}")
    console.print(f"Resolution stage: {state.resolution_stage}")
    console.print(f"Current page: {state.current_page}")
    console.print(f"Pages processed: {pages_processed}")
    console.print(f"Candidates found: {len(state.candidates_found)}")
    for attempt in state.resolution_completed_attempts:
        console.print(
            f"Season {attempt.get('season')}: {attempt.get('status')}; "
            f"{attempt.get('candidates_found', 0)} candidates; "
            f"pages {', '.join(map(str, attempt.get('pages_processed', []))) or '-'}"
        )
    console.print(f"Next action: {next_action}")
    console.print(f"Requests used today: {used}/{config.api.requests_per_day}")


def _resolve_players(config: Config, database: Database) -> None:
    """Resolve all pending players; unresolved players remain out of the crawl."""
    resolver = PlayerResolver(config, database)
    for player in list(config.players):
        if player.resolution_status not in {"pending", "resolved"}:
            continue
        if player.resolution_status == "resolved" and player.provider_player_id:
            continue
        status = resolver.resolve(player)
        console.print(f"{player.name}: resolution status {status}.")
        if status == "paused_quota_limit":
            break


def _show_candidates(
    database: Database, name: str, season: int | None = None
) -> None:
    """List stored candidate IDs and matching evidence."""
    candidates = database.list_resolution_candidates(name, season)
    if not candidates:
        qualifier = f" in season {season}" if season is not None else ""
        console.print(f"No resolution candidates stored for {name}{qualifier}.")
        return
    qualifier = f" in season {season}" if season is not None else ""
    console.print(f"Candidates for {name}{qualifier}:")
    for candidate in candidates:
        if candidate["provider_player_id"] is None:
            continue
        console.print(
            f"  ID {candidate['id']}: {candidate['matched_name']} "
            f"(player ID {candidate['provider_player_id']}), "
            f"team {candidate['provider_team_name'] or '-'} "
            f"({candidate['provider_team_id'] or '-'}), season {candidate['season'] or '-'}, "
            f"page {candidate['source_page'] or '-'}, "
            f"similarity {candidate['name_similarity']:.3f}, status {candidate['status']}"
        )
    console.print(
        f"Select with: python -m football_history.main select-player --name \"{name}\" "
        "--candidate-id <ID>"
    )


def _retry_resolution(
    config: Config, database: Database, name: str, season: int
) -> None:
    """Reset one season's candidate and pagination state for a safe retry."""
    player = config.get_player(name)
    if player is None:
        raise ConfigError(f"Player is not in the crawl queue: {name}")
    database.delete_resolution_attempt(name, player.provider, season)
    seasons = [value for value in player.resolution_seasons if value != season]
    seasons.insert(0, season)
    attempted = [
        value for value in player.attempted_resolution_seasons if value != season
    ]
    config.update_player_progress(
        name,
        resolution_status="pending",
        resolution_seasons=seasons,
        attempted_resolution_seasons=attempted,
    )
    row = database.get_player(name)
    if row is None:
        player_id = database.upsert_player({
            "name": player.name, "provider": player.provider,
            "provider_player_id": player.provider_player_id,
            "resolution_status": "pending",
            "current_team_name": player.current_team_name,
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        })
    else:
        player_id = int(row["id"])
    checkpoint = Checkpoint(config.paths.checkpoints, database, player_id)
    state = checkpoint.load(name, player.provider_player_id)
    if state.resolution_current_attempt and state.resolution_current_attempt.get("season") == season:
        state.resolution_current_attempt = None
    state.resolution_season = season
    state.current_season = season
    state.current_page = 1
    state.last_processed_page = None
    state.has_more_pages = False
    state.candidates_found = []
    state.candidates_processed = 0
    state.resolution_stage = "pending"
    state.status = "in_progress"
    state.resolution_completed_attempts = [
        item for item in state.resolution_completed_attempts
        if item.get("season") != season
    ]
    checkpoint.save(state)
    console.print(
        f"Reset {name} season {season} only. Inspect or resume with "
        f"resolve-player --name \"{name}\"."
    )


def _select_player(
    config: Config, database: Database, name: str, candidate_id: int
) -> None:
    """Select a sufficiently similar candidate and persist the resolved identity."""
    player = config.get_player(name)
    if player is None:
        raise ConfigError(f"Player is not in the crawl queue: {name}")
    candidate = next(
        (row for row in database.list_player_candidates(name)
         if int(row["id"]) == candidate_id),
        None,
    )
    if candidate is None or candidate["provider_player_id"] is None:
        raise ConfigError(f"Candidate {candidate_id} does not belong to {name}.")
    score = candidate["name_similarity"]
    if score is None or float(score) < config.player_resolution_min_similarity:
        raise ConfigError(
            f"Candidate similarity is below the configured threshold "
            f"{config.player_resolution_min_similarity:.2f}; refusing to select it."
        )
    selected = database.select_player_candidate(candidate_id)
    if selected is None:
        raise ConfigError(f"Candidate {candidate_id} no longer exists.")
    provider_player_id = str(selected["provider_player_id"])
    team_name = selected["provider_team_name"]
    season = int(selected["season"]) if selected["season"] is not None else None
    config.set_player_resolution(
        name, "resolved", provider_player_id=provider_player_id,
        current_team_name=team_name, resolution_season=season,
    )
    player_row_id = database.upsert_player({
        "name": player.name, "provider": player.provider,
        "provider_player_id": provider_player_id,
        "resolution_status": "resolved", "current_team_name": team_name,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "crawl_from_season": player.crawl_from_season,
        "crawl_to_season": player.crawl_to_season,
    })
    database.update_player_resolution(
        player_row_id, "resolved", provider_player_id=provider_player_id,
        current_team_name=team_name, resolved_at=datetime.now(timezone.utc).isoformat(),
    )
    console.print(f"Selected {selected['matched_name']} (player ID {provider_player_id}) for {name}.")


def _show_player(config: Config, database: Database, name: str) -> None:
    player = config.get_player(name)
    completed = next((item for item in config.completed_players
                      if item.get("name", "").casefold() == name.casefold()), None)
    settings = player or completed
    if settings is None:
        raise ConfigError(f"Player is not present in configuration: {name}")
    row = database.get_player(name)
    console.print(f"Name: {name}")
    console.print(f"Crawl from: {settings.crawl_from_season if player else settings.get('crawl_from_season')}")
    end = settings.crawl_to_season if player else settings.get("crawl_to_season")
    console.print(f"Crawl to: {end}")
    console.print(f"Database status: {row['status'] if row else 'pending'}")
    if row:
        state = Checkpoint(config.paths.checkpoints, database, int(row["id"])).load(name)
        effective_limit = max(
            0, config.api.requests_per_day - config.api.daily_request_safety_margin
        )
        console.print(f"Completed seasons: {', '.join(map(str, state.completed_seasons)) or '-'}")
        console.print(f"Current season: {state.current_season or '-'}")
        console.print(f"Progress: {state.total_fixtures_processed}/{state.total_fixtures_found} fixtures")
        console.print(f"Requests today: {state.requests_used_today}/{effective_limit}")
        console.print(f"Current stage: {state.current_stage}")
    console.print(f"Configured provider player id: {settings.provider_player_id if player else settings.get('provider_player_id')}")


def _reset(config: Config, database: Database, name: str, delete_data: bool) -> None:
    row = database.get_player(name)
    configured = config.get_player(name)
    completed = next((item for item in config.completed_players
                      if item.get("name", "").casefold() == name.casefold()), None)
    checkpoint_path = Checkpoint(config.paths.checkpoints, database).path_for(name)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    if row:
        player_id = int(row["id"])
        database.delete_player_progress(player_id)
        if delete_data:
            database.delete_player_data(player_id)
    if configured:
        config.add_player_to_crawl(configured)
    elif completed:
        config.add_player_to_crawl(completed)
    elif row:
        config.add_player_to_crawl({
            "name": name,
            "provider_player_id": row["provider_player_id"],
            "crawl_from_season": row["crawl_from_season"],
            "crawl_to_season": row["crawl_to_season"],
        })
    if delete_data:
        slug = Checkpoint.filename(name).removesuffix(".checkpoint.json")
        raw_directory = config.paths.json / slug
        if raw_directory.is_dir():
            for path in raw_directory.iterdir():
                if path.is_file():
                    path.unlink()
            raw_directory.rmdir()
    console.print(f"Checkpoint/progress reset for {name}.")
    if delete_data:
        console.print("The player's database records and matching raw JSON files were deleted.")


def _clean_categories(args: argparse.Namespace) -> list[str]:
    if args.all:
        return ["all", "temp", "downloads", "json", "checkpoints", "output", "database"]
    return [
        name for name in
        ("temp", "logs", "downloads", "json", "checkpoints", "database", "output")
        if getattr(args, name)
    ]


def _confirm(message: str, *, yes: bool) -> bool:
    if yes:
        return True
    return console.input(f"{message} [y/N] ").strip().casefold() in {"y", "yes"}


def _safe_project_path(config: Config, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(config.project_root):
        raise ConfigError(f"Refusing to remove a path outside the project: {resolved}")
    return resolved


def _remove_contents(directory: Path) -> None:
    """Remove generated contents while retaining directories and .gitkeep files."""
    directory.mkdir(parents=True, exist_ok=True)
    for child in directory.iterdir():
        if child.name == ".gitkeep":
            continue
        if child.is_symlink() or child.is_file():
            child.unlink()
        elif child.is_dir():
            _remove_contents(child)
            if not any(child.iterdir()):
                child.rmdir()


def _existing_files(paths: list[Path]) -> list[Path]:
    found: set[Path] = set()
    for path in paths:
        if path.is_file() or path.is_symlink():
            found.add(path)
        elif path.is_dir():
            found.update(
                item for item in path.rglob("*")
                if item.name != ".gitkeep" and (item.is_file() or item.is_symlink())
            )
    return sorted(found)


def _clean(
    config: Config,
    database: Database,
    categories: list[str],
    *,
    yes: bool = False,
    drop_database: bool = False,
    include_player_config: bool = False,
) -> bool:
    """Remove selected generated data without crossing project boundaries."""
    if include_player_config and "all" not in categories:
        raise ConfigError("--include-player-config is supported only with --all.")
    if drop_database and "database" not in categories:
        raise ConfigError("--drop-database requires --database or --all.")
    paths = {
        "temp": config.paths.temp,
        "logs": config.paths.logs,
        "downloads": config.paths.downloads,
        "json": config.paths.json,
        "checkpoints": config.paths.checkpoints,
        "output": config.paths.output,
    }
    clear_all_contents = "all" in categories
    dirs = list(dict.fromkeys(
        _safe_project_path(config, paths[name])
        for name in categories
        if name in paths and (
            clear_all_contents or name in {"temp", "logs", "downloads", "output"}
        )
    ))
    targeted: list[Path] = []
    if not clear_all_contents:
        if "json" in categories:
            targeted.extend(config.paths.json.rglob("*.json"))
        if "checkpoints" in categories:
            targeted.extend(config.paths.checkpoints.rglob("*.checkpoint.json"))
        targeted = [
            _safe_project_path(config, path) for path in targeted
            if path.name != ".gitkeep"
        ]
    database_path = _safe_project_path(config, config.paths.database)
    candidates = list(set(_existing_files(dirs)) | set(targeted))
    if "database" in categories:
        if drop_database:
            candidates.extend(
                path for path in (
                    database_path,
                    Path(f"{database_path}-wal"),
                    Path(f"{database_path}-shm"),
                ) if path.exists()
            )
        else:
            console.print(f"Database rows to clear (schema preserved): {database_path}")
    if include_player_config:
        console.print(
            f"Player configuration to restore from defaults: {config.config_path}"
        )
    console.print("Cleanup plan:")
    for directory in dirs:
        console.print(f"  Clear generated contents under: {directory}")
    for category, directory in (
        ("json", config.paths.json), ("checkpoints", config.paths.checkpoints),
    ):
        if category in categories and not clear_all_contents:
            pattern = "*.json" if category == "json" else "*.checkpoint.json"
            console.print(f"  Delete {pattern} files under: {directory}")
    for path in sorted(set(candidates)):
        console.print(f"  Delete: {path}")
    if not candidates and not dirs and "database" not in categories:
        console.print("  Nothing to delete.")
    requires_confirmation = "all" in categories or "database" in categories
    if requires_confirmation and not _confirm("Proceed with cleanup?", yes=yes):
        console.print("Cleanup cancelled.")
        return False

    if "database" in categories:
        if drop_database:
            for path in (
                database_path, Path(f"{database_path}-wal"), Path(f"{database_path}-shm")
            ):
                if path.is_file():
                    path.unlink()
        else:
            database.clear_all_data()
    for directory in dirs:
        _remove_contents(directory)
    if targeted:
        for path in set(targeted):
            if path.is_file() or path.is_symlink():
                path.unlink()
        for directory in (
            config.paths.json if "json" in categories else None,
            config.paths.checkpoints if "checkpoints" in categories else None,
        ):
            if directory is not None:
                _remove_empty_directories(directory)
    if include_player_config:
        config.reset_players_to_defaults()
    for directory in (
        config.paths.base_data, config.paths.downloads, config.paths.output,
        config.paths.logs, config.paths.checkpoints, config.paths.json,
        config.paths.temp,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    console.print("Cleanup complete.")
    return True


def _remove_empty_directories(directory: Path) -> None:
    for child in directory.iterdir():
        if child.is_dir() and not child.is_symlink():
            _remove_empty_directories(child)
            if not any(child.iterdir()):
                child.rmdir()


def _reset_player(
    config: Config,
    database: Database,
    name: str,
    *,
    delete_data: bool,
    keep_candidates: bool,
    remove_from_config: bool,
    yes: bool,
) -> None:
    """Reset one player's crawl and resolution data."""
    row = database.get_player(name)
    player = config.get_player(name)
    if row is None and player is None:
        raise ConfigError(f"Player is not configured: {name}")
    checkpoint_path = Checkpoint(config.paths.checkpoints, database).path_for(name)
    console.print(f"Reset plan for {name}: checkpoint, crawl progress")
    if not keep_candidates:
        console.print("  Resolution attempts and candidates")
    if delete_data:
        console.print(f"  Player database records: {name}")
        generated_paths = _player_generated_paths(config, name)
        for path in _existing_files(generated_paths):
            console.print(f"  Delete: {path}")
    if delete_data and not _confirm(
        "Proceed with player data deletion?", yes=yes
    ):
        console.print("Reset cancelled.")
        return
    if row is not None:
        player_id = int(row["id"])
        database.delete_player_progress(player_id)
        if not keep_candidates:
            database.clear_player_resolution(player_id, name)
        else:
            database.reset_player_identity(player_id)
        if delete_data:
            database.delete_player_data(player_id, keep_resolution=keep_candidates)
    if checkpoint_path.exists():
        checkpoint_path.unlink()
    player = config.get_player(name)
    if player is None and not remove_from_config:
        completed = next(
            (
                item for item in config.completed_players
                if item.get("name", "").casefold() == name.casefold()
            ),
            None,
        )
        if completed is not None:
            config.add_player_to_crawl(completed)
            player = config.get_player(name)
    if player is not None:
        config.update_player(
            name, {
                "resolution_status": "pending",
                "provider_player_id": None,
                "attempted_resolution_seasons": [],
            },
        )
    if delete_data:
        _remove_player_generated_files(config, name)
    if remove_from_config:
        config.remove_player(name)
    console.print(f"Reset complete for {name}.")


def _remove_player_generated_files(config: Config, name: str) -> None:
    slug = Checkpoint.filename(name).removesuffix(".checkpoint.json")
    for target in _player_generated_paths(config, name)[:2]:
        target = _safe_project_path(config, target)
        if target.is_dir():
            _remove_contents(target)
            if not any(target.iterdir()):
                target.rmdir()
    output = _safe_project_path(config, config.paths.output)
    for path in output.glob(f"*{slug}*"):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            _remove_contents(path)
            if not any(path.iterdir()):
                path.rmdir()


def _player_generated_paths(config: Config, name: str) -> list[Path]:
    slug = Checkpoint.filename(name).removesuffix(".checkpoint.json")
    paths = [
        _safe_project_path(config, config.paths.json / slug),
        _safe_project_path(config, config.paths.downloads / slug),
    ]
    output = _safe_project_path(config, config.paths.output)
    paths.extend(
        _safe_project_path(config, path)
        for path in output.glob(f"*{slug}*")
    )
    return paths


def _reset_crawl(config: Config, database: Database, name: str) -> None:
    """Clear crawl progress while preserving identity-resolution state."""
    row = database.get_player(name)
    if row is None:
        player = config.get_player(name)
        if player is None:
            raise ConfigError(f"Player is not configured: {name}")
        player_id = database.upsert_player({
            "name": player.name, "provider": player.provider,
            "provider_player_id": player.provider_player_id,
            "resolution_status": player.resolution_status,
            "current_team_name": player.current_team_name,
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        })
        row = database.get_player(name)
        if row is None:
            raise RuntimeError(f"Failed to initialize player runtime state: {name}")
        row_id = player_id
    else:
        row_id = int(row["id"])
    player_id = row_id
    checkpoint = Checkpoint(config.paths.checkpoints, database, player_id)
    state = checkpoint.load(name, row["provider_player_id"])
    database.reset_player_crawl(player_id)
    state.provider_player_id = (
        state.provider_player_id
        or (str(row["provider_player_id"]) if row["provider_player_id"] else None)
    )
    state.current_season = None
    state.current_stage = (
        "discover_seasons" if state.provider_player_id else "resolve_player"
    )
    state.status = "pending"
    state.active_fixture_id = None
    state.active_fixture_player_stats = None
    state.active_fixture_player_stats_cached = False
    state.active_fixture_player_stats_raw_path = None
    state.seasons_to_crawl = []
    state.fixture_ids = []
    state.completed_seasons = []
    state.in_progress_seasons = []
    state.last_processed_fixture_id = None
    state.completed_fixture_ids = []
    state.discovered_team_ids = []
    state.processed_team_ids = []
    state.current_team_id = None
    state.current_fixture_page = 1
    state.discovered_fixture_ids = []
    state.processed_fixture_ids = []
    state.participation_checked_fixture_ids = []
    state.player_participated_fixture_ids = []
    state.season_status = None
    state.season_fixture_state = {}
    state.total_fixtures_found = 0
    state.total_fixtures_processed = 0
    checkpoint.save(state)
    console.print(f"Crawl progress reset for {name}; player identity was preserved.")


def _reset_resolution(
    config: Config,
    database: Database,
    name: str,
    *,
    delete_data: bool,
    yes: bool,
) -> None:
    """Clear identity resolution state without discarding crawl results by default."""
    console.print(f"Reset resolution plan for {name}: candidates, attempts, resolution checkpoint.")
    if delete_data:
        console.print(f"  Also delete database crawl records and generated files for {name}.")
        for path in _existing_files(_player_generated_paths(config, name)):
            console.print(f"  Delete: {path}")
    if delete_data and not _confirm(
        f"Also delete crawl data for {name}?", yes=yes
    ):
        console.print("Reset cancelled.")
        return
    row = database.get_player(name)
    if row is None:
        player = config.get_player(name)
        if player is None:
            raise ConfigError(f"Player is not configured: {name}")
        player_id = database.upsert_player({
            "name": player.name, "provider": player.provider,
            "provider_player_id": player.provider_player_id,
            "resolution_status": player.resolution_status,
            "current_team_name": player.current_team_name,
            "crawl_from_season": player.crawl_from_season,
            "crawl_to_season": player.crawl_to_season,
        })
        row = database.get_player(name)
        if row is None:
            raise RuntimeError(f"Failed to initialize player runtime state: {name}")
    else:
        player_id = int(row["id"])
    checkpoint = Checkpoint(config.paths.checkpoints, database, player_id)
    state = checkpoint.load(name, row["provider_player_id"])
    database.clear_player_resolution(player_id, name)
    if delete_data:
        database.delete_player_data(player_id)
    state.provider_player_id = None
    state.resolution_stage = "pending"
    state.resolution_season = None
    state.resolution_current_attempt = None
    state.resolution_completed_attempts = []
    state.resolved_team_id = None
    state.resolved_team_name = None
    state.current_page = 1
    state.last_processed_page = None
    state.has_more_pages = False
    state.candidates_found = []
    state.candidates_processed = 0
    state.error_message = None
    if delete_data:
        checkpoint_path = checkpoint.path_for(name)
        if checkpoint_path.exists():
            checkpoint_path.unlink()
        _remove_player_generated_files(config, name)
    else:
        checkpoint.save(state)
    player = config.get_player(name)
    is_configured = player is not None or any(
        item.get("name", "").casefold() == name.casefold()
        for item in config.completed_players
    )
    if is_configured:
        config.update_player(name, {
            "provider_player_id": None, "resolution_status": "pending",
            "attempted_resolution_seasons": [],
            "resolution_season": None,
        })
    console.print(f"Resolution state reset for {name}.")


def _reset_players(
    config: Config, database: Database, *, restore_defaults: bool, yes: bool
) -> None:
    """Clear runtime data for all players without changing inputs by default."""
    console.print("Reset all-player runtime plan:")
    console.print(f"  Clear generated data under {config.paths.base_data}")
    console.print(f"  Clear database rows under {config.paths.database}")
    if restore_defaults:
        console.print(f"  Restore player inputs from {config.project_root / 'config/default_players.yaml'}")
    if not _confirm("Proceed with reset?", yes=yes):
        console.print("Reset cancelled.")
        return
    categories = ["temp", "downloads", "json", "checkpoints", "output", "database"]
    if not _clean(config, database, categories, yes=True):
        return
    if restore_defaults:
        config.reset_players_to_defaults()
    console.print("Runtime state reset for all players.")


def _reset_project(config: Config, database: Database, *, yes: bool) -> None:
    """Restore factory players and clear generated project data."""
    console.print("Reset project plan:")
    for directory in (
        config.paths.temp, config.paths.downloads, config.paths.json,
        config.paths.checkpoints, config.paths.output,
    ):
        safe_directory = _safe_project_path(config, directory)
        console.print(f"  Clear generated contents under: {safe_directory}")
        for path in _existing_files([safe_directory]):
            console.print(f"  Delete: {path}")
    console.print(f"  Clear database rows (preserve schema): {config.paths.database}")
    console.print(f"  Restore player inputs from defaults: {config.config_path}")
    console.print("  Preserve source code, .env, project.yaml, and default_players.yaml.")
    if not _confirm("Restore default players and clear all generated data?", yes=yes):
        console.print("Reset cancelled.")
        return
    config.reset_players_to_defaults()
    _clean(
        config, database,
        ["temp", "downloads", "json", "checkpoints", "output", "database"],
        yes=True,
    )


if __name__ == "__main__":
    sys.exit(main())
