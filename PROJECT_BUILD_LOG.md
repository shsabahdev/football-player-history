# Project Build Log

## Original user request

Create a production-quality Python project that collects resumable historical football match data for configured players, beginning with Cristiano Ronaldo. Use SQLite, YAML configuration, checkpointing, progress output, and only authorized sources. Implement API-Football through its official API; do not scrape protected sites or bypass access controls. Include a CLI, tests, documentation, and a change log.

## Initial project design

- Use a `src/football_history` package with separated configuration, persistence, API/provider, checkpoint, pipeline, progress, and CLI modules.
- Keep static settings in YAML and secrets in `.env`/environment only.
- Persist complete checkpoint JSON in both a per-player atomic JSON file and SQLite.
- Use SQLite WAL, parameterized statements, uniqueness constraints, and idempotent upserts.
- Process players sequentially, seasons ascending, and persist each fixture stage before advancing.
- Keep provider behavior behind an abstract interface; implement API-Football v3 first.
- Test without live API calls using temporary directories and mocked responses.

## Files created

- Project metadata and configuration: `pyproject.toml`, `requirements.txt`, `.env.example`, `.gitignore`, `config/config.yaml`.
- Application package: `src/football_history/__init__.py`, `models.py`, `config.py`, `database.py`, `checkpoint.py`, `progress.py`, `api_client.py`, `pipeline.py`, `main.py`.
- Provider interface and implementation: `src/football_history/providers/__init__.py`, `base.py`, `api_football.py`.
- Tests: `tests/__init__.py`, `conftest.py`, `test_config.py`, `test_database.py`, `test_checkpoint.py`, `test_api_football_provider.py`, `test_pipeline.py`.
- Documentation: `README.md`, `PROJECT_BUILD_LOG.md`.
- Data and database directory placeholders: `.gitkeep` files; `data/.gitignore` protects the required `downloads/.gitkeep` from the workspace-level `downloads/` ignore rule.

## Major decisions

- API keys are read from `API_FOOTBALL_KEY` and never written to config or logs.
- Daily quota counts all outgoing attempts, including retries; SQLite enforces one shared daily quota across players, and the current player's usage is mirrored in its checkpoint.
- Request logs omit query strings so request parameters and credentials cannot leak into logs.
- `player_checkpoints` supplements the requested `crawl_progress` table so the full checkpoint can be recovered from SQLite.
- Each fixture stage is independently persisted, allowing recovery without losing previously fetched events/lineups/statistics.
- Raw API JSON is organized under a per-player directory so an explicit player-data reset can target only that player's raw files.
- Editable installation makes the requested `python -m football_history.main ...` invocation available from the project root.

## Change History

### 2026-11-08 — Clarify standalone attribution and documentation status
- **Requested change:** Update the standalone README, changelog, and Copilot workspace instructions with approved attribution, coverage, verification, and authorization disclosures.
- **Files changed:** `README.md`, `CHANGELOG.md`, `.github/copilot-instructions.md`, and `PROJECT_BUILD_LOG.md`.
- **Implementation notes:** Identified Shrouq Sabah as the intended copyright-holder name. Licensing authority over every included file remains unresolved. Removed the dated plan-coverage assertion, retained the provider-data-rights distinction and historical changelog entries, and added standalone-workspace authorization guidance.
- **Checks completed:** Static documentation checks only; tests were not run.
- **Correction (2026-11-08):** Restored a project introduction and the prior MIT-license/provider-data-rights paragraph in the README. This wording correction does not resolve licensing authority over every included file.
- **Correction (2026-11-08):** Replaced `tests/fixtures/{player,seasons,fixture,events,lineups,player_stats}.json` with minimal hand-authored examples using fabricated participant/team names and synthetic test-only IDs; updated dependent inputs and expectations in `tests/test_api_football_provider.py`. These replacements do not establish overall licensing clearance; tests were not run.
- **Verification update (2026-11-08):** Ran `env -u API_FOOTBALL_KEY PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -B -m pytest -q -p no:cacheprovider -p pytest_socket -p requests_mock.contrib._pytest_plugin --disable-socket tests` from the workspace using Python 3.12.3 in the dedicated `.venv`; 77 tests passed with exit status 0. pytest-socket blocked Python socket operations in the pytest process, but did not protect the authorized version-command subprocesses; this was not OS-level network isolation. No live API verification occurred, and the test run changed no application code or dependencies.
- **Documentation clarification (2026-11-09):** Clarified that users obtain their own API-Football account and subscription as applicable, supply their own API key, and comply with the API-Football Terms of Service.

### 2026-10-30 — Clarify open-source licensing and external data rights
- **Requested change:** Disclose AI-assisted development and experimental status; distinguish project licensing from API-Football data rights; apply the MIT License to project-owned source and documentation.
- **Files changed:** `README.md`, `LICENSE`, `pyproject.toml`, `CHANGELOG.md`, and `PROJECT_BUILD_LOG.md`.
- **Implementation notes:** Added the standard MIT License with the confirmed copyright holder, disclosed AI-assisted development and educational/experimental status, and clarified API-Football data rights and restrictions. Preserved existing documentation and third-party notices; no application behavior or local data was changed.
- **Checks completed:** `pytest -q` passed (83 tests, no live API requests); the package wheel built with the existing setuptools 68.1.2 backend and contains MIT metadata plus the LICENSE; TOML, README wording, and `git diff --check` validations passed.
- **Publication review:** No tracked credentials, database files/sidecars, response exports, logos, or image assets were found. Test fixtures and the default player configuration include the real Cristiano Ronaldo name and API-Football player ID `874` alongside synthetic fixture values; review those references against provider terms before publishing. No existing third-party license notices were found.

### 2026-10-23 — Ignore local SQLite runtime files
- Added Git ignore rules for SQLite database files and journal/WAL/SHM sidecars.
- Kept `db/.gitkeep` tracked so the database directory remains in the repository.
- Documented the local-only status, WAL safety, and backup requirements in `DATABASE.md`.

### 2026-10-23 — Fixed player fixture discovery for API-Football
- Removed invalid `/fixtures?player={id}&season={season}&page={page}` request.
- Added team-based fixture discovery using `/players?id={id}&season={season}` followed by `/fixtures?team={team_id}&season={season}`.
- Added player participation verification using `/fixtures/players?fixture={fixture_id}`.
- Added `player_fixtures` table to separate discovered fixtures from confirmed player participation.
- Updated checkpointing for team discovery, fixture pagination, and participation checks.
- Updated request attribution and daily quota enforcement.
- Added tests for team-based discovery, participation verification, deduplication, and resume behavior.

### 2026-10-22 — Initial project creation
- Created Python + SQLite football player history collection project.
- Added YAML configuration reader.
- Added SQLite schema for players, seasons, fixtures, events, lineups, player statistics, crawl progress, and request logs.
- Added checkpoint and resume mechanism.
- Added API-Football provider with daily request tracking.
- Added CLI commands for run, resume, status, reset, validate-config, and show-player.
- Added tests, requirements.txt, README.md, and this build log.

Future project modifications should be recorded here with their date and a short description.

### 2026-10-22 — Full-project review
- Reviewed the generated source, configuration, tests, fixtures, and documentation.
- Confirmed the CLI configuration validation and all tests pass without live API requests.
- Updated the README and build-log description to reflect the shared SQLite daily request counter.

### 2026-10-22 — Fix player resolution and enforce request budget
- **Date:** 2026-10-22
- **Requested change:** Resolve configured API-Football player IDs without search, validate the returned ID, and enforce a strict global daily request budget with a safety margin, paused/resumable behavior, and a `requests` CLI command.
- **Files changed:** `PROJECT_BUILD_LOG.md`, `config/config.yaml`, `src/football_history/providers/api_football.py`, `src/football_history/providers/base.py` (if interface changes are needed), `src/football_history/api_client.py`, `src/football_history/checkpoint.py`, `src/football_history/database.py`, `src/football_history/pipeline.py`, `src/football_history/main.py`, `tests/test_api_football_provider.py`, `tests/test_pipeline.py`, `tests/test_config.py`, and `README.md`.
- **Implementation notes:** Preserve the existing configured daily limit of 80. Add a non-negative `api.daily_request_safety_margin` with a default of zero and enforce the effective limit (`requests_per_day - safety_margin`) atomically before every outgoing request, including retries. Persist global daily usage and per-player attribution in SQLite, mirror usage in checkpoints, pause cleanly at the limit, and report the next UTC reset. Keep API tests mocked and verify that ID-based resolution does not call player search.
- **Additional implementation files:** `src/football_history/models.py`, `tests/test_database.py`, and `tests/test_main.py`.

## 2026-10-22 — Added subscription-aware season handling and request attribution
- Changed Cristiano Ronaldo crawl_from_season from 2003 to 2022.
- Added desired_historical_from_season to preserve the eventual historical target.
- Added subscription-supported season detection and caching.
- Added unsupported-season handling to prevent repeated rejected API requests.
- Improved request attribution for player, season, fixture, and crawl stage.
- Improved request report to distinguish attributed and unattributed requests.
- Updated checkpoint behavior to save supported range, unsupported seasons, and request usage.
- Updated CLI status, requests, and seasons commands.
- Added tests for season validation, request attribution, budget enforcement, and resume behavior.

## 2026-10-22 — Added versioning and release workflow
- Added Semantic Versioning using MAJOR.MINOR.PATCH.
- Added single-source version file at src/football_history/_version.py.
- Added pyproject.toml with PEP 621 metadata.
- Added CLI version command and --version flag.
- Added CHANGELOG.md.
- Added release checklist and versioning documentation to README.md.
- Added tests for version consistency and CLI version output.

## 2026-10-22 — Added name-only player resolution
- Added ability to add players using only a name.
- Added optional current_team_name to improve resolution accuracy.
- Added API-Football team-search and team-based player lookup workflow.
- Added player_candidates table and resolution statuses.
- Added conservative name normalization and similarity matching.
- Added CLI commands for adding, resolving, reviewing, and selecting players.
- Integrated resolution with request budget, checkpointing, and crawl queue.
- Added tests for resolution, ambiguity, caching, and request accounting.

## 2026-10-22 — Added resumable paginated player resolution
- Preserved successful team lookup for Lionel Messi and Inter Miami.
- Added pagination-aware player resolution for API-Football.
- Added resumable resolution state for team ID, season, page number, and candidates.
- Prevented repeated team and player-page requests after interruption or quota pause.
- Persisted player candidates across pagination and resolution runs.
- Improved quota-guard behavior to pause resolution at the exact next page.
- Added CLI resolution-status command and improved resolve-player output.
- Added tests for pagination, resume, candidate persistence, and quota pauses.

## 2026-10-22 — Added multi-season player resolution
- Corrected resolution status semantics to distinguish not_found_for_season from global not_found.
- Preserved the completed 2022 Inter Miami resolution attempt and its 52 candidate records.
- Added multi-season resolution attempts with separate pagination state per season.
- Added resolution_seasons and attempted_resolution_seasons configuration fields.
- Added candidate separation by resolution season and attempt.
- Added CLI support for resolution status, candidate inspection, and retrying a specific season.
- Updated checkpoint behavior to preserve completed attempts and start new seasons at page 1.
- Added tests for multi-season resolution, candidate isolation, quota pauses, and resume behavior.

## 2026-10-23 — Separated project configuration from run inputs and added cleanup/reset commands
- Split config/config.yaml into project.yaml, default_players.yaml, and players.yaml.
- Moved stable paths, database, API, crawling, resolution, and logging settings into project.yaml.
- Moved player crawl inputs and completed-player list into players.yaml.
- Kept runtime crawl progress, checkpoints, request counts, and resolution pagination state in SQLite and checkpoint files.
- Added Config, ProjectConfig, PlayersConfig, and AppConfig classes.
- Added clean commands for temp, logs, downloads, JSON, checkpoints, database, output, and all generated data.
- Added reset commands for player, crawl, resolution, players, and project state.
- Added safety confirmations and protection for source code, .env, project.yaml, and default_players.yaml.
- Added tests for configuration separation, cleanup, reset, and safety behavior.
- Updated documentation and changelog.
