"""Terminal progress and logging helpers."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__

console = Console()


def configure_logging(
    log_directory: Path,
    level: int = logging.INFO,
    *,
    console_enabled: bool = True,
    file_enabled: bool = True,
) -> logging.Logger:
    """Configure console and rotating file logging without duplicate handlers."""
    log_directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("football_history")
    logger.setLevel(level)
    if not logger.handlers and (console_enabled or file_enabled):
        formatter = logging.Formatter(
            f"%(asctime)s %(levelname)s football-player-history v{__version__} %(message)s"
        )
        if console_enabled:
            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        if file_enabled:
            file_handler = RotatingFileHandler(
                log_directory / "football_history.log", maxBytes=2_000_000,
                backupCount=5, encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
    return logger


def show_status(players: list[dict[str, Any]]) -> None:
    """Display player status in a compact table."""
    table = Table(title="Football player history status")
    for column in ("Player", "Status", "Completed seasons", "Current season", "Fixtures"):
        table.add_column(column)
    for player in players:
        table.add_row(*(str(player.get(key, "-")) for key in
                        ("name", "status", "completed_seasons", "current_season", "fixtures")))
    console.print(table)


def progress_bar(description: str) -> Progress:
    """Create a Rich progress display with counts and elapsed time."""
    return Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    )
