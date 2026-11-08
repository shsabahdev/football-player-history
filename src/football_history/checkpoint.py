"""Crash-safe JSON and SQLite checkpoints."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .database import Database
from .models import CheckpointState

STAGES = (
    "resolve_player", "discover_seasons", "discover_fixtures", "discover_player_teams",
    "discover_team_fixtures", "verify_participation", "fetch_fixture", "fetch_events",
    "fetch_lineups", "fetch_player_stats", "finalize_season", "complete_player",
)


class Checkpoint:
    """Persist resumable player state after each meaningful operation."""

    def __init__(self, directory: Path, database: Database, player_id: int | None = None) -> None:
        self.directory = directory
        self.database = database
        self.player_id = player_id

    @staticmethod
    def filename(player_name: str) -> str:
        """Create a filesystem-safe, stable checkpoint filename."""
        slug = re.sub(r"[^a-z0-9]+", "_", player_name.casefold()).strip("_")
        return f"{slug or 'player'}.checkpoint.json"

    def path_for(self, player_name: str) -> Path:
        """Return the checkpoint file path for a player."""
        return self.directory / self.filename(player_name)

    def load(self, player_name: str, provider_player_id: str | None = None) -> CheckpointState:
        """Load state from JSON first, then recover from SQLite."""
        path = self.path_for(player_name)
        load_error: Exception | None = None
        if path.is_file():
            try:
                state = CheckpointState.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if state.player_name.casefold() == player_name.casefold():
                    self._refresh_request_date(state)
                    return state
                load_error = ValueError(f"Checkpoint player name does not match {player_name}.")
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                load_error = exc
        if self.player_id is not None:
            stored = self.database.load_checkpoint(self.player_id)
            if stored:
                state = CheckpointState.from_dict(stored)
                self._refresh_request_date(state)
                return state
        if load_error is not None:
            raise ValueError(
                f"Checkpoint for {player_name} is unreadable and no SQLite recovery copy exists."
            ) from load_error
        now = datetime.now(timezone.utc).isoformat()
        return CheckpointState(
            player_name=player_name, provider_player_id=provider_player_id,
            request_date=datetime.now(timezone.utc).date().isoformat(),
            started_at=now, last_updated_at=now,
        )

    def save(self, state: CheckpointState) -> None:
        """Write JSON atomically and persist the same state in SQLite."""
        self.directory.mkdir(parents=True, exist_ok=True)
        state.last_updated_at = datetime.now(timezone.utc).isoformat()
        destination = self.path_for(state.player_name)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.directory, prefix=f".{destination.name}.",
                suffix=".tmp", delete=False,
            ) as stream:
                temporary = Path(stream.name)
                json.dump(state.to_dict(), stream, indent=2, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(destination)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        if self.player_id is not None:
            self.database.save_checkpoint(self.player_id, state.to_dict())

    def bind_player(self, player_id: int) -> None:
        """Associate future saves and restores with a SQLite player record."""
        self.player_id = player_id

    def record_error(self, state: CheckpointState, error: Exception | str) -> None:
        """Append an error and persist it immediately."""
        state.errors.append(str(error))
        self.save(state)

    @staticmethod
    def _refresh_request_date(state: CheckpointState) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        if state.request_date != today:
            state.request_date = today
            state.requests_used_today = 0
