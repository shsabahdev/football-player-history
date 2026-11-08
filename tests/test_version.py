"""Application version and CLI output tests."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from football_history import __version__


PROJECT_ROOT = Path(__file__).parents[1]
SEMVER_PATTERN = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _run_cli(*arguments: str) -> str:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-m", "football_history.main", *arguments],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_package_version_is_valid_semver() -> None:
    assert SEMVER_PATTERN.fullmatch(__version__)


def test_source_and_package_versions_match() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    assert metadata["project"]["version"] == __version__
    version_source = (PROJECT_ROOT / "src/football_history/_version.py").read_text(
        encoding="utf-8"
    )
    assert version_source.count("__version__") == 1


def test_version_subcommand_prints_application_name_and_version() -> None:
    assert _run_cli("version") == f"football-player-history {__version__}"


def test_global_version_flag_prints_only_version_number() -> None:
    assert _run_cli("--version") == __version__
