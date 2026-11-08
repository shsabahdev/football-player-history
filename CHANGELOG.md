# Changelog

## Standalone repository note

This changelog was retained from the private development project. Its version labels and dates have not been independently verified during preparation of this standalone repository.

All notable changes to this project are documented in this file.

## [Unreleased]

### Added
- Added an AI-assisted-development disclosure and an educational/experimental-use warning to the README.
- Clarified the distinction between the software license and external API data rights, including provider-term reminders.
- Added the MIT License for project-owned source code and documentation.
## [0.2.0] - 2026-10-23

### Added
- Split stable project settings, factory player defaults, and active player inputs into separate YAML files.
- Added confirmation-protected cleanup and targeted reset command families.
- Added migration guidance for the new configuration layout.

### Changed
- Player input updates now write only to `config/players.yaml`; runtime progress remains in SQLite and checkpoint files.

### Fixed
- Replaced invalid player-scoped `/fixtures` requests with player-season team discovery and team-filtered fixture pagination.
- Added verified player participation tracking so team fixtures are not treated as player appearances without `/fixtures/players` confirmation.
- Persisted team/page/fixture/participation state for safe quota pause and resume.

### Migration
- `config/config.yaml` is replaced by `config/project.yaml`, `config/default_players.yaml`, and `config/players.yaml`.
- Move existing player entries into `config/players.yaml`; stable settings belong in `config/project.yaml`.
- Runtime progress remains in SQLite and `data/checkpoints/*.checkpoint.json`.

## [0.1.0] - 2026-10-22

### Added
- Initial football player history collection pipeline.
- YAML-based configuration reader.
- SQLite storage for players, seasons, fixtures, events, lineups, and player statistics.
- Checkpoint and resume mechanism.
- API-Football provider.
- CLI commands for run, resume, status, reset, validate-config, show-player, and requests.
- Request-budget enforcement.
- Subscription-aware season handling and request attribution.
- Name-only player resolution with team/league-filtered lookups and candidate review.
- Resumable API-Football player-page pagination with persisted candidate evidence.
- Independent multi-season player resolution attempts with isolated pagination and candidates.
- Versioning support.

### Changed
- Added `version` and `--version` CLI output.
- Added supported-season discovery and unsupported-season skipping.
- Added `add-player`, `resolve-player`, `resolve-players`, `candidates`, and `select-player` commands.
- Added `resolution-status` and configurable maximum resolution page count.
- Added season-filtered candidate inspection and targeted `retry-resolution`.

### Fixed
- Preserved historical season targets separately from the active subscription crawl period.
- Prevented unresolved or ambiguous players from entering the crawl queue.
- Preserved the exact next player page when resolution pauses at the daily quota.
- Distinguished a season-level `not_found_for_season` result from global `not_found`.
- Preserved completed season attempts and advanced only through configured resolution seasons.

### Removed
- None.

### Security
- API keys remain environment-only and are excluded from logs and request URLs.
