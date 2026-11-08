# Football Player History

A resumable Python pipeline for collecting detailed historical match data for selected players. The initial implementation uses the **official API-Football v3 API**; no website scraping, CAPTCHA bypass, login automation, or access-control circumvention is used.

## Introduction

Football Player History is an educational Python and SQLite project focused on collecting and organizing available football-player match information through API-Football. It began with an interest in Cristiano Ronaldo's match history and became a practical exercise in API integration, configuration, data storage, player identification, and reliable collection workflows.

The project does not claim complete career coverage or production readiness. Available data depends on the provider, subscription, and coverage.

## AI and human attribution

Shrouq Sabah directed the project's development and review. Perplexity assisted with research, brainstorming, requirements, and implementation prompts. GitHub Copilot assisted with interpreting prompts, generating and modifying code, debugging, and fixes. The project was developed with AI assistance; this attribution does not establish correctness or completed verification.

## Educational and experimental status

This project was created for education, experimentation, and learning. It may contain bugs, incomplete features, incorrect assumptions, or security and data-quality issues.

Its suitability for production use has not been established. It should not be relied upon for production systems, commercial decisions, betting, financial decisions, or other high-stakes uses without independent review, testing, security assessment, and any required permissions.

This statement describes the project’s intended use and maturity. It does not add restrictions to the permissions granted by the MIT License.

## Verification status

On 2026-11-08, the test suite under tests/ passed in the prepared Python 3.12.3 environment: 77 passed, 0 failed, 0 skipped, and 0 errors. Tests covering API requests used mocks. pytest-socket was enabled to block Python socket operations in the pytest process; the version-command tests also ran their authorized subprocesses, which were outside that protection. This was not OS-level network isolation.

This result does not verify live API compatibility, complete data coverage, production readiness, or licensing clearance. Historical verification records remain separate from this standalone test run.

## API access and data rights

This repository provides software demonstrating how to request and process data from API-Football. It is not a football dataset, does not provide an API subscription or API key, and does not grant access to data beyond the permissions and coverage of your own API-Football account.

Users must obtain their own API-Football account and subscription, as applicable, supply their own API key, and comply with the applicable subscription limits, rate limits, and API-Football Terms of Service. Available seasons, endpoints, and historical coverage depend on API-Football and the user's account/subscription. Availability can vary, and no coverage is guaranteed.

The project’s MIT License applies to project-owned source code and documentation. It does not grant rights to API-Football data, API responses, downloaded databases, player images, team or league logos, trademarks, or other third-party materials.

Before collecting, storing, publishing, redistributing, or commercially using API data, review [API-Football’s current terms](https://www.api-football.com/terms) and obtain any necessary permissions from the provider and relevant rights holders. Paying for API access does not automatically grant all publication or commercial rights.

API-Football’s current terms prohibit direct resale of provider data without permission. Accounts must not be shared, and users must not create multiple accounts to increase free-plan quotas. Follow applicable rate limits. Additional rights may be needed to publish or use data and visual assets. Review the current terms for details and changes.

## No affiliation

This is an independent project. It is not affiliated with, sponsored by, or endorsed by API-Football/API-Sports, Perplexity, GitHub, Microsoft, or any football club, league, federation, or player. Product and organization names are used only to identify the services and tools involved.

## Features

- Python 3.11+, SQLite, and API-Football v3.
- Team-based fixture discovery per player and season, with events, lineups, and player statistics collected only for confirmed appearances.
- Multi-team fixture deduplication and explicit participation tracking.
- Atomic JSON and SQLite checkpoints to resume after interruption.
- Daily request limits, retry/backoff for transient API failures, raw response storage, and progress reporting.
- Parameterized SQL and idempotent upserts for safe reruns.
- A provider interface ready for additional authorized data sources.

## Architecture

```text
CLI (run / resume / status / reset)
             |
          Pipeline ---- Checkpoint (atomic JSON + SQLite)
             |                      |
      Provider interface            +---- Config (YAML + .env)
             |
  API-Football v3 client ---- request log / quota / raw JSON
             |
       SQLite database
 players / seasons / fixtures / player_fixtures / events / lineups / player statistics
```

## Installation

From this project directory, create and activate a virtual environment, then install dependencies and the project in editable mode:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install -e .
```

## API key setup

Copy `.env.example` to `.env` and put your API-Football key in the environment file:

```text
API_FOOTBALL_KEY=your_key_here
```

Alternatively, export `API_FOOTBALL_KEY` in the process environment. The key is not stored in YAML, source code, request logs, or raw API files. Do not commit `.env`.

## Configuration and adding players

Configuration is separated by purpose:

- `config/project.yaml` contains stable application settings: paths, database, API limits, crawling defaults, resolution defaults, and logging.
- `config/players.yaml` contains current player crawl inputs and the configured completed-player list. Add, change, or remove a player here (or use the CLI).
- `config/default_players.yaml` is the factory player list used only by `reset-project` or `reset-players --restore-defaults`.
- Runtime state is not configuration: checkpoints and active pagination state are in SQLite and `data/checkpoints/*.checkpoint.json`; request usage, candidates, resolution attempts, and crawl progress are stored in SQLite.

### Migration from `config/config.yaml`

The former combined file is no longer loaded. Move its stable project settings into `config/project.yaml` and the `players.to_crawl` / `players.completed_players` values into `config/players.yaml`. `config/default_players.yaml` is the factory reset source, not the active input file. Existing SQLite data and checkpoint JSON remain in place because their configured paths are unchanged; runtime progress must not be copied into either player YAML file.

All configured paths are resolved relative to the project root. API keys are read only from `.env` or the environment and must never be placed in YAML. `crawl_to_season: current` means the current calendar year. Each player entry requires a name, `crawl_from_season`, and `crawl_to_season`. Set `provider_player_id` to the API-Football ID; when set, resolution calls `/players?id=...&season=...` directly and never uses name search. Search fallback is only used when a team or league filter is explicitly supplied.

Add a player under `players.to_crawl` in `config/players.yaml`, or use the CLI:

```yaml
- name: Example Player
  provider_player_id: "12345"
  desired_historical_from_season: 2003
  crawl_from_season: 2018
  crawl_to_season: current
```

Completed players are moved to `players.completed_players` only after successful completion.

`desired_historical_from_season` records the eventual history target. `crawl_from_season` is the earliest season to attempt; actual availability depends on the provider and account/subscription and is checked when crawling.

### Adding and resolving players by name

`provider_player_id` is optional for new players. Add a name-only player locally with:

```bash
python -m football_history.main add-player --name "Lionel Messi"
```

Without an ID or a team/league filter, the player is saved as `needs_team_or_league` and is not crawled. Add a current team to make the player eligible for safe lookup:

```bash
python -m football_history.main add-player --name "Lionel Messi" --team "Inter Miami"
python -m football_history.main resolve-player --name "Lionel Messi"
```

Alternatively, use `--league "League name" --season 2024` or a configured `league_id` and season. API-Football does not allow an unfiltered player-name search, so resolution first searches `/teams?search=...` or `/leagues?search=...`, then searches players with the resulting team/league ID and a supported season. The resolver prefers the most recent cached supported season (or the configured crawl start until a supported range is known), follows API pagination, normalizes accents and punctuation, and applies `resolution.minimum_name_similarity` from `config/project.yaml` (default `0.85`).

One qualifying high-confidence result is cached and marked `resolved`. Multiple qualifying results are saved as candidates and marked `needs_review`; no candidate is guessed or crawled. Inspect saved pagination state and candidates without making API requests:

```bash
python -m football_history.main resolution-status --name "Lionel Messi"
python -m football_history.main candidates --name "Lionel Messi"
```

Review and select explicitly:

```bash
python -m football_history.main candidates --name "Lionel Messi"
python -m football_history.main select-player --name "Lionel Messi" --candidate-id 12
```

No-match results are cached as `not_found` and are not searched again automatically. Add a better team/league filter or a provider ID before retrying with `resolve-player`. Resolution searches use the same daily quota and request log as crawling, record player/team-or-league/season/stage/page attribution, and cache each player page separately. The atomic checkpoint stores the resolved team, season, next page, processed page count, candidate IDs, and request usage. On quota pause or interruption, the next run resumes at the next unprocessed page without repeating the team search or completed player pages. `crawling.max_resolution_pages` in `config/project.yaml` caps pages read per resolution run (default 20). Only players marked `resolved` with a provider ID enter the crawl queue.

For players configured with `resolution_seasons`, each season is an independent attempt with its own page state and candidates. A season with no match is recorded as `not_found_for_season`; the resolver advances only through the configured season list and sets the overall result to `not_found` or `needs_review` after the list is exhausted. Successful team resolution is reused across seasons, while each new season starts on page 1. Completed attempt summaries are retained in the checkpoint and SQLite, and candidates are separated by season and attempt ID. Inspect a specific season or safely reset only that season with:

```bash
python -m football_history.main candidates --name "Lionel Messi" --season 2022
python -m football_history.main retry-resolution --name "Lionel Messi" --season 2023
```

`resolution-status` shows attempted and pending seasons, each recorded outcome, and the current season/page. Retrying a season clears only its candidate/page cache and attempt record; it does not alter other seasons' candidates or progress.

Before crawling, the provider loads the player's available seasons and validates the configured historical target against the API. If the API returns a plan-limit message describing a supported season range, the minimum and maximum supported seasons are parsed. Otherwise, the validated target and the player's available-season bounds are cached in the checkpoint. The pipeline marks requested seasons outside that range `unsupported_for_subscription`, saves the checkpoint, logs a warning, and skips them without retrying. Temporary failures (timeouts, HTTP 429, and 5xx) still use normal retry/backoff. If no configured crawl seasons overlap the discovered range, the crawl stops gracefully and recommends changing `crawl_from_season` or upgrading the subscription. After changing subscriptions, use `reset-crawl --name "Cristiano Ronaldo"` to clear crawl progress and rediscover supported seasons.

## Commands

```bash
python -m football_history.main validate-config
python -m football_history.main run
python -m football_history.main resume
python -m football_history.main status
python -m football_history.main requests
python -m football_history.main seasons --player "Cristiano Ronaldo"
python -m football_history.main show-player --name "Cristiano Ronaldo"
python -m football_history.main reset-player --name "Cristiano Ronaldo"
python -m football_history.main add-player --name "Example Player" --team "Example FC"
python -m football_history.main resolve-players
python -m football_history.main resolve-player --name "Example Player"
python -m football_history.main resolution-status --name "Example Player"
python -m football_history.main candidates --name "Example Player" --season 2024
python -m football_history.main retry-resolution --name "Example Player" --season 2024
python -m football_history.main select-player --name "Example Player" --candidate-id 12
```

`reset` removes checkpoint and crawl-progress state, but preserves downloaded match records and raw JSON by default. Pass `--delete-data` only when you also want the player's stored database records and that player's raw response files deleted.

### Cleaning generated data

Use one cleanup selector per command:

```bash
python -m football_history.main clean --temp
python -m football_history.main clean --logs
python -m football_history.main clean --downloads
python -m football_history.main clean --json
python -m football_history.main clean --checkpoints
python -m football_history.main clean --database
python -m football_history.main clean --database --drop-database
python -m football_history.main clean --output
python -m football_history.main clean --all
```

Cleanup prints the affected paths or database action before proceeding. Database and `--all` cleanup require confirmation; pass `--yes` only when the deletion is intended and safe for automation. `clean --database` clears application rows but retains schema; `--drop-database` removes the SQLite file and its WAL/SHM sidecars so the schema is recreated on the next run. Cleanup preserves `.gitkeep` files and recreates required data directories. It never removes source code, `.env`, `config/project.yaml`, or `config/default_players.yaml`; `--include-player-config` with `--all` restores player inputs from the defaults.

### Resetting runtime state

```bash
python -m football_history.main reset-player --name "Lionel Messi"
python -m football_history.main reset-player --name "Lionel Messi" --keep-candidates
python -m football_history.main reset-player --name "Lionel Messi" --delete-data
python -m football_history.main reset-player --name "Lionel Messi" --remove-from-config
python -m football_history.main reset-crawl --name "Cristiano Ronaldo"
python -m football_history.main reset-resolution --name "Lionel Messi"
python -m football_history.main reset-players
python -m football_history.main reset-players --restore-defaults
python -m football_history.main reset-project
```

`reset-player` clears one player's checkpoint, progress, resolution attempts, and candidates by default; `--keep-candidates` preserves resolution evidence. `--delete-data` also removes that player's database records and generated files, and requires confirmation. `--remove-from-config` additionally removes the player input. `reset-crawl` clears crawl progress and resets seasons while keeping player identity and resolution. `reset-resolution` clears resolution state and candidates while retaining crawl results unless `--delete-data` is specified. `reset-players` clears runtime data for all configured players without changing `players.yaml`, unless `--restore-defaults` is explicitly supplied. `reset-project` requires confirmation and restores the factory player list while clearing generated data. These operations preserve source code, `.env`, `project.yaml`, and `default_players.yaml`.

For scripted cleanup or resets that prompt for confirmation, use `--yes` only after verifying the target project directory and intended data loss.

## Database

The SQLite database is `db/football_history.sqlite3`, uses WAL mode and foreign keys, and contains `players`, `seasons`, `fixtures`, `player_fixtures`, `player_fixture_teams`, `match_events`, `lineups`, `player_match_stats`, `crawl_progress`, `request_log`, `daily_request_usage`, `daily_player_request_usage`, `player_checkpoints`, `player_candidates`, and `player_resolution_attempts`. `fixtures` stores team-season candidates; `player_fixtures` stores a player's relationship to each fixture and leaves participation unknown until confirmed by `/fixtures/players`. The related team table preserves every team association when multiple teams produce the same fixture. Use `Database.list_confirmed_player_fixtures` or filter `player_fixtures.participated=1` for the player's final match log. Candidate rows include their season, source page, raw-response path, and resolution attempt ID. The request log records player ID, provider player ID, season, fixture, team, page, crawl stage, endpoint, URL (without query parameters), HTTP status, outcome, error, and request timestamp. Requests made before player resolution are marked with null player fields. Existing aggregate player counters are retained; older request-log rows without detailed metadata remain visible as legacy attribution because their original per-request context cannot be reconstructed. The daily request counter is shared across players and persisted in SQLite. Season/fixture identity and related records are upserted to make reruns safe.

## Checkpoint and request-limit behavior

Each player has a JSON checkpoint in `data/checkpoints/` and a corresponding SQLite checkpoint. State tracks the supported season bounds, unsupported/skipped seasons, current stage and season, active fixture, completed fixture IDs/seasons, API requests used for the UTC calendar day, timestamps, and errors. For each season it also stores discovered and processed team IDs, current team/page, discovered and processed fixtures, participation checks, and confirmed player fixtures. The workflow first calls `/players?id=...&season=...`, then discovers fixtures for each returned team with `/fixtures?team=...&season=...`; it sends a `page` parameter only when the fixtures response advertises pagination. Each team page and participation result is persisted before advancing. Before match details are collected, `/fixtures/players?fixture=...` must include the configured provider player ID. A fixture without that confirmed participant remains stored as a candidate but is excluded from the player's match log. Seasons with no team data are recorded as `no_team_data` and skipped without failing the crawl. A restart skips completed team discoveries, fixture pages, participation checks, and processing stages and resumes at the next outstanding operation.

The daily request limit is global across players and counts every outgoing API attempt, including player resolution, season discovery, range validation, player-team lookup, each team fixture page, participation checks, fixture details, events, lineups, and player statistics. `api.daily_request_safety_margin` reserves requests from `api.requests_per_day`; the enforced cap is the configured limit minus the margin. Usage is atomically reserved before each request and every request attempt is written to `request_log` with its current player/season/team/page/fixture/stage context where known. When the effective cap is reached, the pipeline pauses the player and current season/stage, saves its checkpoint, reports the current player/season/fixture/stage and UTC reset time, and makes no further requests. The next run resumes from that checkpoint without repeating completed team pages, participation checks, fixture stages, or known unsupported seasons.

Inspect today's global and per-player usage with:

```bash
python -m football_history.main requests
```

It reports total, attributed and unattributed requests today, counts by player/stage/endpoint, daily limit, remaining quota, and the next UTC reset time. The API key is never included in logs. Retryable failures include HTTP 429/5xx, timeouts, and connection errors; 401/403/404 are not retried.

`status` displays each player's configured and desired periods, cached supported range, completed/skipped seasons, current stage, fixture progress, and today's player request usage. `seasons --player "Cristiano Ronaldo"` summarizes configured, supported, unsupported, completed, and pending seasons without making an API request.

Raw successful JSON is optional (`crawling.save_raw_json`) and stored in per-player subdirectories under `data/json/`. Logs are written to `data/output/logs/`.

## Testing

```bash
pytest -v
```

Tests use temporary SQLite databases and mocked HTTP responses. They do not make live API requests.

## Versioning and releases

The project uses Semantic Versioning (`MAJOR.MINOR.PATCH`) and remains on `0.x.y` versions before 1.0:

- **PATCH** increments for bug fixes and backward-compatible internal corrections.
- **MINOR** increments for backward-compatible features, commands, configuration options, database tables, endpoints, or provider support.
- **MAJOR** increments for breaking changes such as incompatible CLI/config changes, breaking database migrations, or removed public functionality.
- Optional pre-release labels (for example `0.2.0-beta.1`) are used only when needed.

The runtime version is defined once in `src/football_history/_version.py`. Keep the PEP 621 package version in `pyproject.toml` synchronized with it. Update `CHANGELOG.md` for every release. Before release, run:

```bash
pytest -v
python -m football_history.main validate-config
```

The CLI version is available through `python -m football_history.main version` and `python -m football_history.main --version`.

## Release checklist

1. Update the version in `src/football_history/_version.py`.
2. Update the version in `pyproject.toml`.
3. Update `CHANGELOG.md`.
4. Update `PROJECT_BUILD_LOG.md` with the release entry.
5. Run `pytest -v`.
6. Run `python -m football_history.main validate-config`.
7. Commit the release changes.
8. Create a Git tag using the format `v0.2.0`.

## Legal and data-source notice

Use only data and API access that you are authorized to use, and comply with API-Football's current terms, quotas, and applicable law. This project calls the official API and does not scrape Transfermarkt or bypass CAPTCHAs, WAFs, rate limits, login protections, or other access controls. Verify your subscription provides the requested historical endpoints before running a large crawl.

## License

Project-owned source code and documentation are licensed under the MIT License. See [LICENSE](LICENSE) for the full terms, including warranty and liability disclaimers.

Third-party components retain their respective licenses. External API data, images, logos, and other third-party content are not licensed by this repository’s MIT License.

## Troubleshooting

- **`API_FOOTBALL_KEY is not set`**: create `.env` from `.env.example` or export the variable in the same shell.
- **401/403**: verify the key and API subscription/permissions. These errors are intentionally not retried.
- **429 or daily limit reached**: the pipeline stops and saves state; wait until quota resets or adjust the configured quota to match your actual plan.
- **No player found**: check spelling and provider player ID; the ID must match the API-Football search result.
- **Resume appears to repeat a request**: the request may have completed just before a crash but before its response was committed; safe upserts prevent duplicate records.
- **Paths**: relative paths in YAML resolve from this project directory. `validate-config` prints the resolved locations.
