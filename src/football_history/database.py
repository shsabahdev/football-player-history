"""SQLite persistence layer for crawl and match data."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    """Return a timezone-aware ISO timestamp."""
    return datetime.now(timezone.utc).isoformat()


class Database:
    """Manage the SQLite database and idempotent data writes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit all writes together, rolling back on failure."""
        connection = self.connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create tables and indexes if they do not already exist."""
        wal_connection = sqlite3.connect(self.path, timeout=30)
        try:
            wal_connection.execute("PRAGMA journal_mode = WAL")
        finally:
            wal_connection.close()
        statements = [
            """CREATE TABLE IF NOT EXISTS players (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider_player_id TEXT UNIQUE,
                provider TEXT, resolution_status TEXT NOT NULL DEFAULT 'pending',
                current_team_name TEXT, resolved_at TEXT,
                name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                crawl_from_season INTEGER, crawl_to_season TEXT, created_at TEXT, updated_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL, season INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', fixtures_found INTEGER DEFAULT 0,
                fixtures_processed INTEGER DEFAULT 0, started_at TEXT, completed_at TEXT,
                UNIQUE(player_id, season), FOREIGN KEY(player_id) REFERENCES players(id))""",
            """CREATE TABLE IF NOT EXISTS fixtures (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider_fixture_id TEXT UNIQUE,
                player_id INTEGER NOT NULL, season INTEGER NOT NULL, competition_id TEXT,
                competition_name TEXT, date TEXT, status TEXT, home_team_id TEXT, home_team_name TEXT,
                away_team_id TEXT, away_team_name TEXT, home_goals INTEGER, away_goals INTEGER,
                venue TEXT, referee TEXT, raw_json_path TEXT, created_at TEXT, updated_at TEXT,
                FOREIGN KEY(player_id) REFERENCES players(id))""",
            """CREATE TABLE IF NOT EXISTS player_fixtures (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL,
                fixture_id INTEGER NOT NULL, provider_player_id TEXT NOT NULL,
                provider_fixture_id TEXT NOT NULL, season INTEGER NOT NULL,
                provider_team_id TEXT, provider_team_name TEXT, participated INTEGER,
                participation_source TEXT NOT NULL DEFAULT 'unknown', raw_json_path TEXT,
                participation_raw_json_path TEXT,
                created_at TEXT, updated_at TEXT,
                UNIQUE(player_id, provider_fixture_id),
                FOREIGN KEY(player_id) REFERENCES players(id),
                FOREIGN KEY(fixture_id) REFERENCES fixtures(id))""",
            """CREATE TABLE IF NOT EXISTS player_fixture_teams (
                player_fixture_id INTEGER NOT NULL, provider_team_id TEXT NOT NULL,
                provider_team_name TEXT, created_at TEXT,
                PRIMARY KEY(player_fixture_id, provider_team_id),
                FOREIGN KEY(player_fixture_id) REFERENCES player_fixtures(id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS match_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, provider_event_id TEXT,
                minute TEXT, event_type TEXT, detail TEXT, team_id TEXT, team_name TEXT, player_id TEXT,
                player_name TEXT, assist_player_id TEXT, assist_player_name TEXT, raw_json_path TEXT,
                created_at TEXT, UNIQUE(fixture_id, provider_event_id),
                FOREIGN KEY(fixture_id) REFERENCES fixtures(id))""",
            """CREATE TABLE IF NOT EXISTS lineups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, team_id TEXT,
                team_name TEXT, formation TEXT, coach_name TEXT, player_id TEXT, player_name TEXT,
                player_number TEXT, position TEXT, grid TEXT, is_starter INTEGER, is_captain INTEGER,
                raw_json_path TEXT, created_at TEXT, UNIQUE(fixture_id, team_id, player_id),
                FOREIGN KEY(fixture_id) REFERENCES fixtures(id))""",
            """CREATE TABLE IF NOT EXISTS player_match_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id INTEGER NOT NULL, provider_player_id TEXT,
                player_name TEXT, team_id TEXT, team_name TEXT, minutes_played INTEGER, rating TEXT,
                goals INTEGER, assists INTEGER, shots_total INTEGER, shots_on_goal INTEGER,
                passes_total INTEGER, passes_accuracy TEXT, duels_total INTEGER, duels_won INTEGER,
                raw_json_path TEXT, created_at TEXT, UNIQUE(fixture_id, provider_player_id),
                FOREIGN KEY(fixture_id) REFERENCES fixtures(id))""",
            """CREATE TABLE IF NOT EXISTS crawl_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_id INTEGER NOT NULL, season INTEGER,
                stage TEXT NOT NULL, status TEXT NOT NULL, completed_items INTEGER DEFAULT 0,
                total_items INTEGER DEFAULT 0, last_processed_id TEXT, error_message TEXT, updated_at TEXT,
                UNIQUE(player_id, season, stage), FOREIGN KEY(player_id) REFERENCES players(id))""",
            """CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, player_id INTEGER,
                provider_player_id TEXT, season INTEGER, fixture_id TEXT, stage TEXT,
                team_id TEXT, team_name TEXT, league_id TEXT, league_name TEXT,
                endpoint TEXT, url TEXT,
                status_code INTEGER, success INTEGER, error_message TEXT, requested_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS daily_request_usage (
                request_date TEXT PRIMARY KEY, requests_used INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS daily_player_request_usage (
                request_date TEXT NOT NULL, player_name TEXT NOT NULL,
                requests_used INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(request_date, player_name))""",
            """CREATE TABLE IF NOT EXISTS player_checkpoints (
                player_id INTEGER PRIMARY KEY, checkpoint_json TEXT NOT NULL, updated_at TEXT,
                FOREIGN KEY(player_id) REFERENCES players(id))""",
            """CREATE TABLE IF NOT EXISTS player_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_config_name TEXT NOT NULL,
                provider TEXT NOT NULL, provider_player_id TEXT, provider_team_id TEXT,
                provider_team_name TEXT, season INTEGER, matched_name TEXT,
                name_similarity REAL, status TEXT NOT NULL DEFAULT 'candidate',
                raw_json_path TEXT, source_page INTEGER, attempt_id TEXT,
                created_at TEXT, updated_at TEXT)""",
            """CREATE TABLE IF NOT EXISTS player_resolution_attempts (
                attempt_id TEXT PRIMARY KEY, player_config_name TEXT NOT NULL,
                provider TEXT NOT NULL, season INTEGER NOT NULL, team_id TEXT,
                team_name TEXT, status TEXT NOT NULL, state_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS player_resolution_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT, player_config_name TEXT NOT NULL,
                provider TEXT NOT NULL, team_id TEXT, league_id TEXT, season INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT,
                UNIQUE(player_config_name,provider,team_id,league_id,season))""",
            """CREATE TABLE IF NOT EXISTS player_resolution_cache (
                player_config_name TEXT NOT NULL, provider TEXT NOT NULL, cache_key TEXT NOT NULL,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(player_config_name,provider,cache_key))""",
            """CREATE TABLE IF NOT EXISTS subscription_season_ranges (
                provider TEXT PRIMARY KEY, minimum_season INTEGER NOT NULL,
                maximum_season INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_fixtures_player_season ON fixtures(player_id, season)",
            "CREATE INDEX IF NOT EXISTS idx_fixtures_provider_id ON fixtures(provider_fixture_id)",
            "CREATE INDEX IF NOT EXISTS idx_events_fixture ON match_events(fixture_id)",
            "CREATE INDEX IF NOT EXISTS idx_lineups_fixture ON lineups(fixture_id)",
            "CREATE INDEX IF NOT EXISTS idx_stats_fixture ON player_match_stats(fixture_id)",
            "CREATE INDEX IF NOT EXISTS idx_progress_player_season_stage ON crawl_progress(player_id, season, stage)",
        ]
        with self.transaction() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            for table, columns in {
                "players": (
                    ("provider", "TEXT"),
                    ("resolution_status", "TEXT NOT NULL DEFAULT 'pending'"),
                    ("current_team_name", "TEXT"),
                    ("resolved_at", "TEXT"),
                ),
                "request_log": (
                    ("team_id", "TEXT"),
                    ("team_name", "TEXT"),
                    ("league_id", "TEXT"),
                    ("league_name", "TEXT"),
                    ("page_number", "INTEGER"),
                ),
                "player_candidates": (
                    ("source_page", "INTEGER"), ("attempt_id", "TEXT"),
                ),
                "player_fixtures": (
                    ("participation_raw_json_path", "TEXT"),
                ),
            }.items():
                existing = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for column, declaration in columns:
                    if column not in existing:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
                        )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_player_candidate_identity
                ON player_candidates(player_config_name,provider,
                COALESCE(provider_player_id,''),COALESCE(provider_team_id,''),COALESCE(season,0))"""
            )
            connection.execute(
                """UPDATE player_candidates SET attempt_id=
                player_config_name || '|' || provider || '|' || season || '|' ||
                COALESCE(provider_team_id,'') WHERE attempt_id IS NULL AND season IS NOT NULL"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_player_candidate_attempt_identity
                ON player_candidates(player_config_name,provider,attempt_id,provider_player_id)
                WHERE attempt_id IS NOT NULL AND provider_player_id IS NOT NULL"""
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_resolution_search_identity
                ON player_resolution_searches(
                player_config_name,provider,COALESCE(team_id,''),COALESCE(league_id,''),season)"""
            )
            request_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(request_log)").fetchall()
            }
            for column, declaration in (
                ("player_id", "INTEGER"),
                ("provider_player_id", "TEXT"),
                ("season", "INTEGER"),
                ("fixture_id", "TEXT"),
                ("stage", "TEXT"),
            ):
                if column not in request_columns:
                    connection.execute(f"ALTER TABLE request_log ADD COLUMN {column} {declaration}")

    def upsert_player(self, player: dict[str, Any]) -> int:
        """Insert or update a player and return its database id."""
        now = utc_now()
        provider_player_id = player.get("provider_player_id")
        with self.transaction() as connection:
            row = None
            if provider_player_id is not None:
                row = connection.execute(
                    "SELECT id FROM players WHERE provider_player_id=?",
                    (provider_player_id,),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT id FROM players WHERE name=? COLLATE NOCASE",
                    (player["name"],),
                ).fetchone()
            if row:
                player_id = int(row["id"])
                connection.execute(
                    """UPDATE players SET provider_player_id=COALESCE(?,provider_player_id),
                    provider=COALESCE(?,provider),resolution_status=?,current_team_name=?,
                    resolved_at=COALESCE(?,resolved_at),name=?,status=?,
                    crawl_from_season=?,crawl_to_season=?,updated_at=? WHERE id=?""",
                    (
                        provider_player_id, player.get("provider"),
                        player.get("resolution_status", "resolved" if provider_player_id else "pending"),
                        player.get("current_team_name"), player.get("resolved_at"),
                        player["name"], player.get("status", "pending"),
                        player.get("crawl_from_season"),
                        str(player.get("crawl_to_season", "")), now, player_id,
                    ),
                )
                return player_id
            connection.execute(
                """INSERT INTO players(
                provider_player_id,provider,resolution_status,current_team_name,resolved_at,
                name,status,crawl_from_season,crawl_to_season,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    provider_player_id, player.get("provider"),
                    player.get("resolution_status", "resolved" if provider_player_id else "pending"),
                    player.get("current_team_name"), player.get("resolved_at"),
                    player["name"], player.get("status", "pending"),
                    player.get("crawl_from_season"), str(player.get("crawl_to_season", "")),
                    now, now,
                ),
            )
            row = connection.execute(
                "SELECT id FROM players WHERE name=? COLLATE NOCASE", (player["name"],)
            ).fetchone()
            if row is None:
                raise RuntimeError("Player upsert did not return an id.")
            return int(row["id"])

    def upsert_season(self, player_id: int, season: int, **values: Any) -> None:
        """Insert or update a player's season progress."""
        fields = {"status": values.get("status", "pending"),
                  "fixtures_found": values.get("fixtures_found", 0),
                  "fixtures_processed": values.get("fixtures_processed", 0),
                  "started_at": values.get("started_at"), "completed_at": values.get("completed_at")}
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO seasons(player_id,season,status,fixtures_found,fixtures_processed,started_at,completed_at)
                VALUES(:player_id,:season,:status,:fixtures_found,:fixtures_processed,:started_at,:completed_at)
                ON CONFLICT(player_id,season) DO UPDATE SET status=excluded.status,
                fixtures_found=excluded.fixtures_found,fixtures_processed=excluded.fixtures_processed,
                started_at=COALESCE(seasons.started_at,excluded.started_at),completed_at=excluded.completed_at""",
                {"player_id": player_id, "season": season, **fields},
            )

    def upsert_fixture(self, fixture: dict[str, Any]) -> int:
        """Insert or update fixture metadata without creating duplicate rows."""
        return self.upsert_fixtures([fixture])[0]

    def upsert_fixtures(self, fixtures: list[dict[str, Any]]) -> list[int]:
        """Upsert a batch of fixture metadata in one transaction."""
        now = utc_now()
        keys = ("provider_fixture_id", "player_id", "season", "competition_id", "competition_name",
                "date", "status", "home_team_id", "home_team_name", "away_team_id", "away_team_name",
                "home_goals", "away_goals", "venue", "referee", "raw_json_path")
        ids: list[int] = []
        with self.transaction() as connection:
            for fixture in fixtures:
                values = [fixture.get(key) for key in keys]
                connection.execute(
                    f"""INSERT INTO fixtures({','.join(keys)},created_at,updated_at)
                    VALUES({','.join('?' for _ in keys)},?,?)
                    ON CONFLICT(provider_fixture_id) DO UPDATE SET
                    {','.join(
                        f'{key}=excluded.{key}' for key in keys
                        if key not in {'provider_fixture_id', 'player_id'}
                    )},
                    updated_at=excluded.updated_at""",
                    (*values, now, now),
                )
                row = connection.execute(
                    "SELECT id FROM fixtures WHERE provider_fixture_id = ?",
                    (fixture["provider_fixture_id"],),
                ).fetchone()
                if row is None:
                    raise RuntimeError("Fixture upsert did not return an id.")
                ids.append(int(row["id"]))
        return ids

    def record_player_fixture(
        self,
        *,
        player_id: int,
        fixture_id: int,
        provider_player_id: str,
        provider_fixture_id: str,
        season: int,
        provider_team_id: str | None,
        provider_team_name: str | None,
        raw_json_path: str | None,
    ) -> None:
        """Upsert a discovered fixture and preserve every associated team."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_fixtures(
                player_id,fixture_id,provider_player_id,provider_fixture_id,season,
                provider_team_id,provider_team_name,participated,participation_source,
                raw_json_path,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,NULL,'unknown',?,?,?)
                ON CONFLICT(player_id,provider_fixture_id) DO UPDATE SET
                fixture_id=excluded.fixture_id,season=excluded.season,
                raw_json_path=COALESCE(excluded.raw_json_path,player_fixtures.raw_json_path),
                updated_at=excluded.updated_at""",
                (
                    player_id, fixture_id, provider_player_id, provider_fixture_id, season,
                    provider_team_id, provider_team_name, raw_json_path, now, now,
                ),
            )
            row = connection.execute(
                """SELECT id FROM player_fixtures
                WHERE player_id=? AND provider_fixture_id=?""",
                (player_id, provider_fixture_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("Player fixture upsert did not return an id.")
            if provider_team_id is not None:
                connection.execute(
                    """INSERT INTO player_fixture_teams(
                    player_fixture_id,provider_team_id,provider_team_name,created_at)
                    VALUES(?,?,?,?) ON CONFLICT(player_fixture_id,provider_team_id)
                    DO UPDATE SET provider_team_name=excluded.provider_team_name""",
                    (row["id"], provider_team_id, provider_team_name, now),
                )

    def set_player_fixture_participation(
        self, player_id: int, provider_fixture_id: str, participated: bool,
        raw_json_path: str | None = None,
    ) -> None:
        """Persist verified participation from API-Football fixture player data."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE player_fixtures SET participated=?,participation_source=?,
                participation_raw_json_path=COALESCE(?,participation_raw_json_path),
                updated_at=?
                WHERE player_id=? AND provider_fixture_id=?""",
                (
                    int(participated), "fixture_players_endpoint", raw_json_path, utc_now(),
                    player_id, provider_fixture_id,
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    f"Discovered player fixture {provider_fixture_id} was not persisted."
                )

    def get_player_fixture_participation(
        self, player_id: int, provider_fixture_id: str
    ) -> bool | None:
        """Return persisted participation, or None when it has not been checked."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT participated FROM player_fixtures
                WHERE player_id=? AND provider_fixture_id=?""",
                (player_id, provider_fixture_id),
            ).fetchone()
        if row is None or row["participated"] is None:
            return None
        return bool(row["participated"])

    def list_confirmed_player_fixtures(self, player_id: int) -> list[sqlite3.Row]:
        """Return only fixtures with confirmed participation for the player."""
        with closing(self.connect()) as connection:
            return list(connection.execute(
                """SELECT f.*,pf.provider_player_id,pf.provider_team_id,
                pf.provider_team_name,pf.participation_source
                FROM player_fixtures pf JOIN fixtures f ON f.id=pf.fixture_id
                WHERE pf.player_id=? AND pf.participated=1
                ORDER BY pf.season,f.date,f.provider_fixture_id""",
                (player_id,),
            ))

    def save_checkpoint(self, player_id: int, checkpoint: dict[str, Any]) -> None:
        """Persist complete checkpoint state and its progress summary."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_checkpoints(player_id,checkpoint_json,updated_at) VALUES(?,?,?)
                ON CONFLICT(player_id) DO UPDATE SET checkpoint_json=excluded.checkpoint_json,
                updated_at=excluded.updated_at""",
                (player_id, json.dumps(checkpoint), now),
            )
            season = checkpoint.get("current_season")
            stage = checkpoint.get("current_stage", "unknown")
            progress_values = (
                checkpoint.get("status", "in_progress"),
                checkpoint.get("total_fixtures_processed", 0),
                checkpoint.get("total_fixtures_found", 0),
                checkpoint.get("last_processed_fixture_id"),
                checkpoint.get("errors", [])[-1] if checkpoint.get("errors") else None,
                now,
            )
            if season is None:
                cursor = connection.execute(
                    """UPDATE crawl_progress SET status=?,completed_items=?,total_items=?,
                    last_processed_id=?,error_message=?,updated_at=?
                    WHERE player_id=? AND season IS NULL AND stage=?""",
                    (*progress_values, player_id, stage),
                )
                if cursor.rowcount == 0:
                    connection.execute(
                        """INSERT INTO crawl_progress(player_id,season,stage,status,completed_items,
                        total_items,last_processed_id,error_message,updated_at)
                        VALUES(?,NULL,?,?,?,?,?,?,?)""",
                        (player_id, stage, *progress_values),
                    )
            else:
                connection.execute(
                    """INSERT INTO crawl_progress(player_id,season,stage,status,completed_items,total_items,
                    last_processed_id,error_message,updated_at) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(player_id,season,stage) DO UPDATE SET status=excluded.status,
                    completed_items=excluded.completed_items,total_items=excluded.total_items,
                    last_processed_id=excluded.last_processed_id,error_message=excluded.error_message,
                    updated_at=excluded.updated_at""",
                    (player_id, season, stage, *progress_values),
                )

    def save_fixture_records(
        self, fixture_id: int, events: list[dict[str, Any]], lineups: list[dict[str, Any]],
        stats: list[dict[str, Any]],
    ) -> None:
        """Upsert events, lineup entries, and player statistics atomically."""
        now = utc_now()
        with self.transaction() as connection:
            for event in events:
                connection.execute(
                    """INSERT INTO match_events(fixture_id,provider_event_id,minute,event_type,detail,
                    team_id,team_name,player_id,player_name,assist_player_id,assist_player_name,raw_json_path,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fixture_id,provider_event_id) DO UPDATE SET
                    minute=excluded.minute,event_type=excluded.event_type,detail=excluded.detail,
                    team_id=excluded.team_id,team_name=excluded.team_name,player_id=excluded.player_id,
                    player_name=excluded.player_name,assist_player_id=excluded.assist_player_id,
                    assist_player_name=excluded.assist_player_name,raw_json_path=excluded.raw_json_path""",
                    (fixture_id, event.get("provider_event_id"), event.get("minute"), event.get("event_type"),
                     event.get("detail"), event.get("team_id"), event.get("team_name"), event.get("player_id"),
                     event.get("player_name"), event.get("assist_player_id"), event.get("assist_player_name"),
                     event.get("raw_json_path"), now),
                )
            for lineup in lineups:
                connection.execute(
                    """INSERT INTO lineups(fixture_id,team_id,team_name,formation,coach_name,player_id,
                    player_name,player_number,position,grid,is_starter,is_captain,raw_json_path,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fixture_id,team_id,player_id) DO UPDATE SET
                    team_name=excluded.team_name,formation=excluded.formation,coach_name=excluded.coach_name,
                    player_name=excluded.player_name,player_number=excluded.player_number,
                    position=excluded.position,grid=excluded.grid,is_starter=excluded.is_starter,
                    is_captain=excluded.is_captain,raw_json_path=excluded.raw_json_path""",
                    (fixture_id, lineup.get("team_id"), lineup.get("team_name"), lineup.get("formation"),
                     lineup.get("coach_name"), lineup.get("player_id"), lineup.get("player_name"),
                     lineup.get("player_number"), lineup.get("position"), lineup.get("grid"),
                     lineup.get("is_starter"), lineup.get("is_captain"), lineup.get("raw_json_path"), now),
                )
            for stat in stats:
                connection.execute(
                    """INSERT INTO player_match_stats(fixture_id,provider_player_id,player_name,team_id,
                    team_name,minutes_played,rating,goals,assists,shots_total,shots_on_goal,passes_total,
                    passes_accuracy,duels_total,duels_won,raw_json_path,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fixture_id,provider_player_id)
                    DO UPDATE SET player_name=excluded.player_name,team_id=excluded.team_id,
                    team_name=excluded.team_name,minutes_played=excluded.minutes_played,rating=excluded.rating,
                    goals=excluded.goals,assists=excluded.assists,shots_total=excluded.shots_total,
                    shots_on_goal=excluded.shots_on_goal,passes_total=excluded.passes_total,
                    passes_accuracy=excluded.passes_accuracy,duels_total=excluded.duels_total,
                    duels_won=excluded.duels_won,raw_json_path=excluded.raw_json_path""",
                    (fixture_id, stat.get("provider_player_id"), stat.get("player_name"),
                     stat.get("team_id"), stat.get("team_name"), stat.get("minutes_played"),
                     stat.get("rating"), stat.get("goals"), stat.get("assists"),
                     stat.get("shots_total"), stat.get("shots_on_goal"), stat.get("passes_total"),
                     stat.get("passes_accuracy"), stat.get("duels_total"), stat.get("duels_won"),
                     stat.get("raw_json_path"), now),
                )

    def load_checkpoint(self, player_id: int) -> dict[str, Any] | None:
        """Load the latest complete checkpoint for a player."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT checkpoint_json FROM player_checkpoints WHERE player_id=?", (player_id,)
            ).fetchone()
        return json.loads(row["checkpoint_json"]) if row else None

    def begin_request(
        self,
        provider: str,
        endpoint: str,
        url: str,
        *,
        player_id: int | None = None,
        provider_player_id: str | None = None,
        season: int | None = None,
        fixture_id: str | None = None,
        stage: str | None = None,
        team_id: str | None = None,
        team_name: str | None = None,
        league_id: str | None = None,
        league_name: str | None = None,
        page_number: int | None = None,
    ) -> int:
        """Log an outgoing request before network I/O so interruptions are counted."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """INSERT INTO request_log(
                provider,player_id,provider_player_id,season,fixture_id,stage,team_id,team_name,
                league_id,league_name,page_number,
                endpoint,url,status_code,success,error_message,requested_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,0,'request_in_progress',?)""",
                (provider, player_id, provider_player_id, season, fixture_id, stage,
                 team_id, team_name, league_id, league_name, page_number, endpoint,
                 url.split("?", 1)[0], utc_now()),
            )
            return int(cursor.lastrowid)

    def finish_request(
        self, request_id: int, status_code: int | None, success: bool, error_message: str | None
    ) -> None:
        """Update the outcome of a previously logged request."""
        with self.transaction() as connection:
            connection.execute(
                "UPDATE request_log SET status_code=?,success=?,error_message=? WHERE id=?",
                (status_code, int(success), error_message, request_id),
            )

    def backfill_resolution_request_page(
        self, player_id: int, team_id: str, season: int, page: int
    ) -> None:
        """Attach a recovered raw response page to its older successful request log row."""
        with self.transaction() as connection:
            connection.execute(
                """UPDATE request_log SET page_number=? WHERE id=(
                SELECT id FROM request_log WHERE player_id=? AND team_id=? AND season=?
                AND endpoint='players' AND stage='resolve_team_players'
                AND success=1 AND page_number IS NULL ORDER BY id LIMIT 1)""",
                (page, player_id, team_id, season),
            )

    def reserve_daily_request(
        self, request_date: str, limit: int, player_name: str
    ) -> tuple[int, int] | None:
        """Atomically reserve one API attempt; return global and per-player totals."""
        with self.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO daily_request_usage(request_date,requests_used) VALUES(?,0)",
                (request_date,),
            )
            row = connection.execute(
                "SELECT requests_used FROM daily_request_usage WHERE request_date=?", (request_date,)
            ).fetchone()
            if row is None:
                raise RuntimeError("Could not read daily request usage.")
            if int(row["requests_used"]) >= limit:
                return None
            connection.execute(
                "UPDATE daily_request_usage SET requests_used=requests_used+1 WHERE request_date=?",
                (request_date,),
            )
            updated = connection.execute(
                "SELECT requests_used FROM daily_request_usage WHERE request_date=?", (request_date,)
            ).fetchone()
            connection.execute(
                """INSERT INTO daily_player_request_usage(request_date,player_name,requests_used)
                VALUES(?,?,1) ON CONFLICT(request_date,player_name) DO UPDATE SET
                requests_used=daily_player_request_usage.requests_used+1""",
                (request_date, player_name),
            )
            player_row = connection.execute(
                """SELECT requests_used FROM daily_player_request_usage
                WHERE request_date=? AND player_name=?""",
                (request_date, player_name),
            ).fetchone()
            if player_row is None:
                raise RuntimeError("Could not read per-player daily request usage.")
            return int(updated["requests_used"]), int(player_row["requests_used"])

    def get_daily_request_usage(self, request_date: str) -> int:
        """Return globally tracked API attempts for a UTC date."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT requests_used FROM daily_request_usage WHERE request_date=?", (request_date,)
            ).fetchone()
        return int(row["requests_used"]) if row else 0

    def get_player_daily_request_usage(self, request_date: str) -> list[tuple[str, int]]:
        """Return today's request count for each player."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT player_name,requests_used FROM daily_player_request_usage
                WHERE request_date=? ORDER BY player_name""",
                (request_date,),
            ).fetchall()
        return [(str(row["player_name"]), int(row["requests_used"])) for row in rows]

    def get_daily_request_usage_for_player(self, request_date: str, player_name: str) -> int:
        """Return a single player's API attempts for a UTC date."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT requests_used FROM daily_player_request_usage
                WHERE request_date=? AND player_name=?""",
                (request_date, player_name),
            ).fetchone()
        return int(row["requests_used"]) if row else 0

    def get_request_report(self, request_date: str) -> dict[str, Any]:
        """Return daily request counts grouped by player, stage, and endpoint."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT player_id,provider_player_id,season,fixture_id,stage,endpoint,
                requested_at FROM request_log WHERE substr(requested_at,1,10)=?""",
                (request_date,),
            ).fetchall()
            player_names = {
                int(row["id"]): str(row["name"])
                for row in connection.execute("SELECT id,name FROM players").fetchall()
            }
            player_ids = {
                str(row["name"]): str(row["provider_player_id"])
                for row in connection.execute(
                    "SELECT name,provider_player_id FROM players WHERE provider_player_id IS NOT NULL"
                ).fetchall()
            }
        player_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        endpoint_counts: dict[str, int] = {}
        attributed = 0
        for row in rows:
            player_id = row["player_id"]
            provider_player_id = row["provider_player_id"]
            if player_id is not None or provider_player_id is not None:
                attributed += 1
                name = player_names.get(int(player_id), "Unknown player") if player_id else "Unknown player"
                player_label = f"{name} ({provider_player_id or 'ID unknown'})"
                player_counts[player_label] = player_counts.get(player_label, 0) + 1
            stage = str(row["stage"] or "unattributed")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            endpoint = str(row["endpoint"] or "unknown")
            endpoint_counts[endpoint] = endpoint_counts.get(endpoint, 0) + 1
        used = self.get_daily_request_usage(request_date)
        logged = len(rows)
        total = max(used, logged)
        daily_players = self.get_player_daily_request_usage(request_date)
        legacy_player_total = sum(count for _, count in daily_players)
        legacy_attributed = min(
            max(0, legacy_player_total - attributed), max(0, total - attributed)
        )
        remaining_legacy = legacy_attributed
        for player_name, count in daily_players:
            logged_for_player = sum(
                1 for row in rows
                if row["player_id"] is not None
                and player_names.get(int(row["player_id"])) == player_name
            )
            legacy_count = min(max(0, count - logged_for_player), remaining_legacy)
            if legacy_count:
                label = f"{player_name} ({player_ids.get(player_name, 'ID unknown')})"
                player_counts[label] = player_counts.get(label, 0) + legacy_count
                remaining_legacy -= legacy_count
        if legacy_attributed:
            legacy_to_remove = legacy_attributed
            existing_unattributed = stage_counts.get("unattributed", 0)
            stage_counts["unattributed"] = max(0, existing_unattributed - legacy_to_remove)
            existing_unattributed = endpoint_counts.get("unattributed", 0)
            endpoint_counts["unattributed"] = max(0, existing_unattributed - legacy_to_remove)
            stage_counts["legacy_attributed"] = (
                stage_counts.get("legacy_attributed", 0) + legacy_attributed
            )
            endpoint_counts["legacy_attributed"] = (
                endpoint_counts.get("legacy_attributed", 0) + legacy_attributed
            )
        unattributed = max(0, total - attributed - legacy_attributed)
        unlogged = max(0, total - logged)
        if unlogged:
            stage_counts["unattributed"] = stage_counts.get("unattributed", 0) + unlogged
            endpoint_counts["unattributed"] = endpoint_counts.get("unattributed", 0) + unlogged
        if not stage_counts.get("unattributed"):
            stage_counts.pop("unattributed", None)
        if not endpoint_counts.get("unattributed"):
            endpoint_counts.pop("unattributed", None)
        return {
            "total": total,
            "attributed": attributed + legacy_attributed,
            "unattributed": unattributed,
            "by_player": player_counts,
            "by_stage": stage_counts,
            "by_endpoint": endpoint_counts,
        }

    def get_season_statuses(self, player_id: int) -> dict[int, str]:
        """Return persisted status by season for a player."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT season,status FROM seasons WHERE player_id=? ORDER BY season",
                (player_id,),
            ).fetchall()
        return {int(row["season"]): str(row["status"]) for row in rows}

    def mark_season_unsupported(self, player_id: int, season: int) -> None:
        """Persist a season rejected by the subscription without retrying it."""
        self.upsert_season(player_id, season, status="unsupported_for_subscription")

    def upsert_player_candidate(self, candidate: dict[str, Any]) -> int:
        """Insert or update a candidate without duplicating a prior search result."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_candidates(
                player_config_name,provider,provider_player_id,provider_team_id,
                provider_team_name,season,matched_name,name_similarity,status,raw_json_path,
                source_page,attempt_id,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO UPDATE SET matched_name=excluded.matched_name,
                name_similarity=excluded.name_similarity,status=excluded.status,
                provider_team_id=excluded.provider_team_id,
                provider_team_name=excluded.provider_team_name,
                raw_json_path=COALESCE(player_candidates.raw_json_path,excluded.raw_json_path),
                source_page=COALESCE(player_candidates.source_page,excluded.source_page),
                attempt_id=COALESCE(player_candidates.attempt_id,excluded.attempt_id),
                updated_at=excluded.updated_at""",
                (
                    candidate["player_config_name"], candidate["provider"],
                    candidate.get("provider_player_id"), candidate.get("provider_team_id"),
                    candidate.get("provider_team_name"), candidate.get("season"),
                    candidate.get("matched_name"), candidate.get("name_similarity"),
                    candidate.get("status", "candidate"), candidate.get("raw_json_path"),
                    candidate.get("source_page"), candidate.get("attempt_id"), now, now,
                ),
            )
            if candidate.get("attempt_id") and candidate.get("provider_player_id"):
                row = connection.execute(
                    """SELECT id FROM player_candidates WHERE player_config_name=?
                    AND provider=? AND attempt_id=? AND provider_player_id=?""",
                    (candidate["player_config_name"], candidate["provider"],
                     candidate["attempt_id"], candidate["provider_player_id"]),
                ).fetchone()
            else:
                row = connection.execute(
                    """SELECT id FROM player_candidates WHERE player_config_name=? AND provider=?
                    AND COALESCE(provider_player_id,'')=COALESCE(?,'')
                    AND COALESCE(provider_team_id,'')=COALESCE(?,'')
                    AND COALESCE(season,0)=COALESCE(?,0)""",
                    (
                        candidate["player_config_name"], candidate["provider"],
                        candidate.get("provider_player_id"), candidate.get("provider_team_id"),
                        candidate.get("season"),
                    ),
                ).fetchone()
            if row is None:
                raise RuntimeError("Candidate upsert did not return an id.")
            return int(row["id"])

    def list_player_candidates(self, player_name: str) -> list[sqlite3.Row]:
        """List candidate records for a configured player."""
        with closing(self.connect()) as connection:
            return list(connection.execute(
                """SELECT * FROM player_candidates WHERE player_config_name=? COLLATE NOCASE
                ORDER BY name_similarity DESC, provider_team_name, provider_player_id""",
                (player_name,),
            ))

    def list_resolution_candidates(
        self, player_name: str, season: int | None = None
    ) -> list[sqlite3.Row]:
        """List all saved candidates or isolate one season's attempt."""
        with closing(self.connect()) as connection:
            if season is None:
                rows = connection.execute(
                    """SELECT * FROM player_candidates
                    WHERE player_config_name=? COLLATE NOCASE
                    ORDER BY season, name_similarity DESC, provider_team_name""",
                    (player_name,),
                )
            else:
                rows = connection.execute(
                    """SELECT * FROM player_candidates
                    WHERE player_config_name=? COLLATE NOCASE AND season=?
                    ORDER BY name_similarity DESC, provider_team_name""",
                    (player_name, season),
                )
            return list(rows)

    def save_resolution_attempt(
        self, attempt_id: str, player_name: str, provider: str, season: int,
        team_id: str | None, team_name: str | None, status: str,
        state: dict[str, Any],
    ) -> None:
        """Upsert season-scoped page progress for durable inspection and recovery."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_resolution_attempts(
                attempt_id,player_config_name,provider,season,team_id,team_name,status,
                state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(attempt_id) DO UPDATE SET team_id=excluded.team_id,
                team_name=excluded.team_name,status=excluded.status,
                state_json=excluded.state_json,updated_at=excluded.updated_at""",
                (attempt_id, player_name, provider, season, team_id, team_name, status,
                 json.dumps(state), now, now),
            )

    def list_resolution_attempts(self, player_name: str) -> list[sqlite3.Row]:
        """Return season-scoped resolution attempts in season order."""
        with closing(self.connect()) as connection:
            return list(connection.execute(
                """SELECT * FROM player_resolution_attempts
                WHERE player_config_name=? COLLATE NOCASE ORDER BY season""",
                (player_name,),
            ))

    def get_resolution_attempt(
        self, player_name: str, provider: str, season: int
    ) -> sqlite3.Row | None:
        """Load the durable state for one player's season attempt."""
        with closing(self.connect()) as connection:
            return connection.execute(
                """SELECT * FROM player_resolution_attempts
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND season=?
                ORDER BY updated_at DESC LIMIT 1""",
                (player_name, provider, season),
            ).fetchone()

    def delete_resolution_attempt(
        self, player_name: str, provider: str, season: int
    ) -> None:
        """Remove one attempt's metadata, pages, and candidates without touching other seasons."""
        with self.transaction() as connection:
            attempts = connection.execute(
                """SELECT attempt_id,team_id FROM player_resolution_attempts
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND season=?""",
                (player_name, provider, season),
            ).fetchall()
            connection.execute(
                """DELETE FROM player_resolution_cache
                WHERE player_config_name=? COLLATE NOCASE AND provider=?
                AND cache_key LIKE ?""",
                (player_name, provider, f"players:%:season:{season}:page:%"),
            )
            for attempt in attempts:
                if attempt["team_id"]:
                    for scope in ("team", "league"):
                        page_prefix = (
                            f"players:{scope}:{attempt['team_id']}:season:{season}:page:%"
                        )
                        connection.execute(
                            """DELETE FROM player_resolution_cache
                            WHERE player_config_name=? COLLATE NOCASE AND provider=?
                            AND cache_key LIKE ?""",
                            (player_name, provider, page_prefix),
                        )
            connection.execute(
                """DELETE FROM player_candidates
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND season=?""",
                (player_name, provider, season),
            )
            connection.execute(
                """DELETE FROM player_resolution_attempts
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND season=?""",
                (player_name, provider, season),
            )

    def select_player_candidate(self, candidate_id: int) -> sqlite3.Row | None:
        """Mark one candidate selected and return it."""
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM player_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """UPDATE player_candidates SET status=CASE WHEN id=? THEN 'selected' ELSE 'rejected' END,
                updated_at=? WHERE player_config_name=? COLLATE NOCASE""",
                (candidate_id, utc_now(), row["player_config_name"]),
            )
            return connection.execute(
                "SELECT * FROM player_candidates WHERE id=?", (candidate_id,)
            ).fetchone()

    def resolution_search_complete(
        self,
        player_name: str,
        provider: str,
        season: int,
        *,
        team_id: str | None = None,
        league_id: str | None = None,
    ) -> bool:
        """Check whether this exact team/league and season search was completed."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT completed FROM player_resolution_searches
                WHERE player_config_name=? COLLATE NOCASE AND provider=?
                AND team_id IS ? AND league_id IS ? AND season=?""",
                (player_name, provider, team_id, league_id, season),
            ).fetchone()
        return bool(row["completed"]) if row else False

    def get_resolution_cache(
        self, player_name: str, provider: str, cache_key: str
    ) -> list[dict[str, Any]] | None:
        """Load cached API response records for a resolution lookup."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT payload_json FROM player_resolution_cache
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND cache_key=?""",
                (player_name, provider, cache_key),
            ).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def set_resolution_cache(
        self, player_name: str, provider: str, cache_key: str,
        payload: list[dict[str, Any]],
    ) -> None:
        """Persist team, league, or player search results for safe resumption."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_resolution_cache(
                player_config_name,provider,cache_key,payload_json,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(player_config_name,provider,cache_key)
                DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (player_name, provider, cache_key, json.dumps(payload), utc_now()),
            )

    def get_resolution_page(
        self, player_name: str, provider: str, team_id: str, season: int, page: int,
        *, scope: str = "team",
    ) -> dict[str, Any] | None:
        """Load one cached player search page and its pagination metadata."""
        key = f"players:{scope}:{team_id}:season:{season}:page:{page}"
        with closing(self.connect()) as connection:
            row = connection.execute(
                """SELECT payload_json FROM player_resolution_cache
                WHERE player_config_name=? COLLATE NOCASE AND provider=? AND cache_key=?""",
                (player_name, provider, key),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload_json"])
        return payload if isinstance(payload, dict) else None

    def set_resolution_page(
        self,
        player_name: str,
        provider: str,
        team_id: str,
        season: int,
        page: int,
        payload: dict[str, Any],
        *,
        scope: str = "team",
    ) -> None:
        """Persist a single player page before processing or advancing."""
        key = f"players:{scope}:{team_id}:season:{season}:page:{page}"
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_resolution_cache(
                player_config_name,provider,cache_key,payload_json,updated_at)
                VALUES(?,?,?,?,?) ON CONFLICT(player_config_name,provider,cache_key)
                DO UPDATE SET payload_json=excluded.payload_json,updated_at=excluded.updated_at""",
                (player_name, provider, key, json.dumps(payload), utc_now()),
            )

    def get_latest_supported_season(self) -> int | None:
        """Return the highest cached subscription season across player checkpoints."""
        with closing(self.connect()) as connection:
            ranges = connection.execute(
                "SELECT maximum_season FROM subscription_season_ranges"
            ).fetchall()
            checkpoints = connection.execute(
                "SELECT checkpoint_json FROM player_checkpoints"
            ).fetchall()
        values = [int(row["maximum_season"]) for row in ranges]
        for row in checkpoints:
            state = json.loads(row["checkpoint_json"])
            maximum = state.get("supported_season_max")
            if isinstance(maximum, int) and not isinstance(maximum, bool):
                values.append(maximum)
        return max(values) if values else None

    def set_supported_season_range(
        self, provider: str, minimum: int, maximum: int
    ) -> None:
        """Cache subscription limits for future name-based resolution."""
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO subscription_season_ranges(
                provider,minimum_season,maximum_season,updated_at) VALUES(?,?,?,?)
                ON CONFLICT(provider) DO UPDATE SET minimum_season=excluded.minimum_season,
                maximum_season=excluded.maximum_season,updated_at=excluded.updated_at""",
                (provider, minimum, maximum, utc_now()),
            )

    def update_player_resolution(
        self,
        player_id: int,
        status: str,
        *,
        provider_player_id: str | None = None,
        current_team_name: str | None = None,
        resolved_at: str | None = None,
    ) -> None:
        """Persist resolution outcome for an existing player record."""
        with self.transaction() as connection:
            connection.execute(
                """UPDATE players SET resolution_status=?,provider_player_id=COALESCE(?,provider_player_id),
                current_team_name=COALESCE(?,current_team_name),resolved_at=COALESCE(?,resolved_at),
                updated_at=? WHERE id=?""",
                (status, provider_player_id, current_team_name, resolved_at, utc_now(), player_id),
            )

    def mark_candidates_ambiguous(
        self, player_name: str, *, season: int | None = None
    ) -> None:
        """Mark matching alternatives for manual review."""
        with self.transaction() as connection:
            query = """UPDATE player_candidates SET status='ambiguous',updated_at=?
                WHERE player_config_name=? COLLATE NOCASE AND status='candidate'"""
            params: tuple[Any, ...] = (utc_now(), player_name)
            if season is not None:
                query += " AND season=?"
                params += (season,)
            connection.execute(query, params)

    def mark_resolution_search_complete(
        self,
        player_name: str,
        provider: str,
        season: int,
        *,
        team_id: str | None = None,
        league_id: str | None = None,
    ) -> None:
        """Persist completion of one bounded player lookup."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO player_resolution_searches(
                player_config_name,provider,team_id,league_id,season,completed,created_at,updated_at)
                VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT DO UPDATE SET completed=1,updated_at=excluded.updated_at""",
                (player_name, provider, team_id, league_id, season, now, now),
            )

    def mark_player_paused(self, player_id: int, season: int | None, stage: str) -> None:
        """Mark a player and the current crawl stage as paused."""
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE players SET status='paused',updated_at=? WHERE id=?",
                (now, player_id),
            )
            if season is not None:
                connection.execute(
                    "UPDATE seasons SET status='paused' WHERE player_id=? AND season=?",
                    (player_id, season),
                )
            cursor = connection.execute(
                """UPDATE crawl_progress SET status='paused',updated_at=?
                WHERE player_id=? AND season IS ? AND stage=?""",
                (now, player_id, season, stage),
            )
            if cursor.rowcount == 0:
                connection.execute(
                    """INSERT INTO crawl_progress(
                    player_id,season,stage,status,completed_items,total_items,last_processed_id,
                    error_message,updated_at) VALUES(?,?,?,'paused',0,0,NULL,NULL,?)""",
                    (player_id, season, stage, now),
                )

    def delete_player_progress(self, player_id: int) -> None:
        """Remove checkpoint and crawl progress for a player."""
        with self.transaction() as connection:
            connection.execute("DELETE FROM crawl_progress WHERE player_id=?", (player_id,))
            connection.execute("DELETE FROM player_checkpoints WHERE player_id=?", (player_id,))
            connection.execute(
                """UPDATE seasons SET status='pending',fixtures_found=0,
                fixtures_processed=0,started_at=NULL,completed_at=NULL
                WHERE player_id=?""",
                (player_id,),
            )
            connection.execute(
                "UPDATE players SET status='pending',updated_at=? WHERE id=?",
                (utc_now(), player_id),
            )

    def reset_player_identity(self, player_id: int) -> None:
        """Clear the stored provider identity while retaining candidate evidence."""
        with self.transaction() as connection:
            connection.execute(
                """UPDATE players SET provider_player_id=NULL,
                resolution_status='pending',status='pending',resolved_at=NULL,
                updated_at=? WHERE id=?""",
                (utc_now(), player_id),
            )

    def reset_player_crawl(self, player_id: int) -> None:
        """Clear one player's crawl progress while retaining identity resolution."""
        with self.transaction() as connection:
            connection.execute(
                """DELETE FROM player_fixture_teams WHERE player_fixture_id IN (
                SELECT id FROM player_fixtures WHERE player_id=?)""",
                (player_id,),
            )
            connection.execute("DELETE FROM player_fixtures WHERE player_id=?", (player_id,))
            connection.execute(
                """UPDATE fixtures SET player_id=(
                SELECT pf.player_id FROM player_fixtures pf
                WHERE pf.fixture_id=fixtures.id ORDER BY pf.player_id LIMIT 1)
                WHERE player_id=? AND EXISTS(
                SELECT 1 FROM player_fixtures pf WHERE pf.fixture_id=fixtures.id)""",
                (player_id,),
            )
            connection.execute("DELETE FROM crawl_progress WHERE player_id=?", (player_id,))
            connection.execute(
                """UPDATE seasons SET status='pending',fixtures_found=0,
                fixtures_processed=0,started_at=NULL,completed_at=NULL
                WHERE player_id=?""",
                (player_id,),
            )
            connection.execute(
                "UPDATE players SET status='pending',updated_at=? WHERE id=?",
                (utc_now(), player_id),
            )

    def clear_player_resolution(self, player_id: int, player_name: str) -> None:
        """Remove only identity resolution data for one player."""
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM player_candidates WHERE player_config_name=? COLLATE NOCASE",
                (player_name,),
            )
            connection.execute(
                "DELETE FROM player_resolution_attempts WHERE player_config_name=? COLLATE NOCASE",
                (player_name,),
            )
            connection.execute(
                """DELETE FROM player_resolution_searches
                WHERE player_config_name=? COLLATE NOCASE""",
                (player_name,),
            )
            connection.execute(
                """DELETE FROM player_resolution_cache
                WHERE player_config_name=? COLLATE NOCASE""",
                (player_name,),
            )
            connection.execute(
                """UPDATE players SET provider_player_id=NULL,resolution_status='pending',
                status='pending',resolved_at=NULL,updated_at=? WHERE id=?""",
                (utc_now(), player_id),
            )

    def clear_all_data(self) -> None:
        """Delete runtime and crawl data while preserving the database schema."""
        tables = (
            "player_fixture_teams", "player_fixtures", "match_events", "lineups",
            "player_match_stats", "fixtures", "seasons",
            "crawl_progress", "player_checkpoints", "player_candidates",
            "player_resolution_attempts", "player_resolution_searches",
            "player_resolution_cache", "daily_player_request_usage",
            "daily_request_usage", "request_log", "subscription_season_ranges", "players",
        )
        with self.transaction() as connection:
            connection.execute("PRAGMA defer_foreign_keys = ON")
            for table in tables:
                connection.execute(f"DELETE FROM {table}")

    def clear_player_request_usage(self, player_name: str) -> None:
        """Clear one player's attributable request count rows."""
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM daily_player_request_usage WHERE player_name=? COLLATE NOCASE",
                (player_name,),
            )

    def get_player(self, name: str) -> sqlite3.Row | None:
        """Return a player row by case-insensitive name."""
        with closing(self.connect()) as connection:
            return connection.execute("SELECT * FROM players WHERE name=? COLLATE NOCASE", (name,)).fetchone()

    def list_players(self) -> list[sqlite3.Row]:
        """Return players and their core status."""
        with closing(self.connect()) as connection:
            return list(connection.execute("SELECT * FROM players ORDER BY name"))

    def table_count(self, table: str) -> int:
        """Return count for a known project table."""
        allowed = {"players", "seasons", "fixtures", "match_events", "lineups",
                   "player_match_stats", "crawl_progress", "request_log",
                   "daily_request_usage", "daily_player_request_usage",
                   "player_candidates", "player_resolution_searches",
                   "player_resolution_attempts", "player_resolution_cache",
                   "player_fixtures", "player_fixture_teams"}
        if table not in allowed:
            raise ValueError(f"Unknown table: {table}")
        with closing(self.connect()) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def delete_player_data(self, player_id: int, *, keep_resolution: bool = False) -> None:
        """Delete a player's relational records in foreign-key-safe order."""
        with self.transaction() as connection:
            player = connection.execute(
                "SELECT name FROM players WHERE id=?", (player_id,)
            ).fetchone()
            player_name = str(player["name"]) if player else None
            fixture_ids = [
                int(row["id"])
                for row in connection.execute(
                    """SELECT DISTINCT f.id FROM fixtures f
                    LEFT JOIN player_fixtures pf ON pf.fixture_id=f.id
                    WHERE f.player_id=? OR pf.player_id=?""",
                    (player_id, player_id),
                )
            ]
            connection.execute(
                """DELETE FROM player_fixture_teams WHERE player_fixture_id IN (
                SELECT id FROM player_fixtures WHERE player_id=?)""",
                (player_id,),
            )
            connection.execute("DELETE FROM player_fixtures WHERE player_id=?", (player_id,))
            orphan_fixture_ids = [
                fixture_id for fixture_id in fixture_ids
                if connection.execute(
                    "SELECT 1 FROM player_fixtures WHERE fixture_id=? LIMIT 1",
                    (fixture_id,),
                ).fetchone() is None
            ]
            if orphan_fixture_ids:
                placeholders = ",".join("?" for _ in orphan_fixture_ids)
                for table in ("match_events", "lineups", "player_match_stats"):
                    connection.execute(
                        f"DELETE FROM {table} WHERE fixture_id IN ({placeholders})",
                        orphan_fixture_ids,
                    )
                connection.execute(
                    f"DELETE FROM fixtures WHERE id IN ({placeholders})",
                    orphan_fixture_ids,
                )
            connection.execute("DELETE FROM seasons WHERE player_id=?", (player_id,))
            connection.execute("DELETE FROM crawl_progress WHERE player_id=?", (player_id,))
            connection.execute("DELETE FROM player_checkpoints WHERE player_id=?", (player_id,))
            connection.execute("DELETE FROM request_log WHERE player_id=?", (player_id,))
            if player_name is not None and not keep_resolution:
                for table in (
                    "player_candidates", "player_resolution_attempts",
                    "player_resolution_searches", "player_resolution_cache",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE player_config_name=? COLLATE NOCASE",
                        (player_name,),
                    )
                connection.execute(
                    "DELETE FROM daily_player_request_usage WHERE player_name=? COLLATE NOCASE",
                    (player_name,),
                )
            connection.execute("DELETE FROM players WHERE id=?", (player_id,))
