# Database Documentation

## Overview

The database stores football-player match-history data and the runtime state needed to collect it. It supports player resolution, season and team fixture discovery, match details, resumable crawling, quota accounting, and request auditing. SQLite is used because the project is local and lightweight and does not require a database server. The database is initialized in WAL (write-ahead logging) mode.

Project-generated timestamps are stored as UTC ISO 8601 text, normally with an explicit `+00:00` offset (for example, `2026-10-23T08:15:30.123456+00:00`). This is the format produced by Python `datetime.isoformat()` in the current implementation; it is UTC-equivalent to a timestamp ending in `Z`, but is not literally formatted with `Z`. Date-only quota keys use `YYYY-MM-DD`. Provider-supplied kickoff values are retained as returned and may use their own ISO 8601 offset.

Columns named `provider_*` contain identifiers or values associated with the external provider (currently API-Football), not SQLite row IDs. SQLite `id` columns are internal keys unless noted otherwise.

## Database location

- Default path: `db/football_history.sqlite3`
- The path is configured under `database.path` in `config/project.yaml`.
- `Database` creates the parent directory and initializes the database automatically if it does not exist.
- WAL mode may create `db/football_history.sqlite3-wal` and `db/football_history.sqlite3-shm` beside the main file while the database is in use. These are SQLite-managed files; do not remove them while a connection or application process is using the database.

### Git and database files

The SQLite database and its `-wal`, `-shm`, and `-journal` sidecars are local generated runtime data. They must not be committed to Git; `.gitignore` excludes these files while `db/.gitkeep` remains tracked so the empty database directory exists in a checkout.

The `-wal` file may contain committed transactions that have not yet been checkpointed into the main `.sqlite3` database. Do not delete the `-wal` file while a database connection may be open. Do not back up only the main `.sqlite3` file while a `-wal` file exists, because the copy could omit committed data. A safe backup must either include the main database and all database sidecar files together, or be taken after all database connections close cleanly and SQLite has completed its checkpoint/cleanup.

## Design principles

- One row in `fixtures` represents one provider-identified football match.
- One row in `lineups` represents one selected player for one team in one fixture; both the starting XI and substitutes are stored.
- One row in `match_events` represents one discrete in-match incident returned by the provider.
- One row in `player_match_stats` represents one provider player’s performance in one fixture.
- One row in `player_candidates` represents one possible identity match produced by a player-resolution search.
- Runtime crawl progress lives in `crawl_progress`, `player_checkpoints`, and atomic checkpoint JSON files, not in `config/players.yaml`.
- Provider IDs are provider-specific. Do not compare IDs from different providers as if they shared a namespace.
- Upserts and unique constraints prevent repeated processing from creating duplicate fixture, event, lineup, statistic, candidate, and player-fixture records.
- Use parameterized SQL for values and transactions for logically related writes.
- A team-season fixture listing is only a set of candidate matches for a player. Do not treat it as proof that the player appeared in each match. Confirm participation using `player_fixtures.participated = 1`.

## Entity relationship overview

```text
players
 ├── seasons                         (one player to many seasons)
 ├── fixtures                        (legacy/primary player association)
 ├── player_fixtures                  (one player's candidate/confirmed fixtures)
 │    └── player_fixture_teams        (teams that yielded a candidate fixture)
 ├── crawl_progress
 ├── player_checkpoints
 ├── player_candidates                (resolution evidence; name-based, no FK)
 ├── request_log                      (request attribution; no FK constraint)
 └── daily_player_request_usage       (player name + UTC date; no FK)

fixtures
 ├── player_fixtures                  (player relationship to the shared fixture)
 ├── match_events
 ├── lineups
 └── player_match_stats

player_resolution_attempts            (resolution-attempt history; no FK)
player_resolution_searches            (cached search completion; no FK)
player_resolution_cache               (cached provider responses; no FK)
subscription_season_ranges             (provider-level subscription bounds)
daily_request_usage                    (global UTC-date quota counter)
```

`fixture_id` is the central internal link from a match in `fixtures` to its events, lineups, player statistics, and player associations. `player_fixtures` is the authoritative relationship for whether a discovered fixture belongs in an individual player's match history. `fixtures.player_id` is retained as the fixture's primary/legacy player association; because a provider fixture can be associated with more than one configured player, use `player_fixtures` when querying player-specific matches.

The current schema does not declare foreign keys from `request_log`, `player_candidates`, player-resolution tables, or `daily_player_request_usage` to `players`; these records can therefore retain audit or resolution history independently of a player row.

## Tables

Nullable values below follow the actual SQLite schema as initialized by `src/football_history/database.py`, including its additive compatibility alterations. `INTEGER PRIMARY KEY AUTOINCREMENT` is an automatically generated SQLite row ID even though `PRAGMA table_info` reports its `notnull` flag as zero.

### `players`

**Purpose:** Stores configured player identity and overall crawl/resolution status.

**Granularity:** One row per configured/resolved player. The configured name is used as a case-insensitive lookup key in application queries; the schema does not declare it unique.

**Relationships:** Parent of `seasons`, `fixtures`, `player_fixtures`, `crawl_progress`, and `player_checkpoints`. `request_log.player_id` refers to it by convention only, without a foreign-key constraint.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal player row ID. |
| `provider_player_id` | TEXT | Yes | Unique external provider player ID. |
| `provider` | TEXT | Yes | Provider name, such as `api_football`. |
| `resolution_status` | TEXT | No; default `pending` | Outcome/stage of player identity resolution. |
| `current_team_name` | TEXT | Yes | Configured team hint used for resolution. |
| `resolved_at` | TEXT | Yes | UTC timestamp when identity was resolved, when supplied. |
| `name` | TEXT | No | Configured/display name. |
| `status` | TEXT | No; default `pending` | Overall crawl/runtime status. |
| `crawl_from_season` | INTEGER | Yes | Earliest configured season to crawl. |
| `crawl_to_season` | TEXT | Yes | Configured final season; may be a year string or `current`. |
| `created_at` | TEXT | Yes | Row creation timestamp. |
| `updated_at` | TEXT | Yes | Last update timestamp. |

#### Important columns and rules

- `id` is an internal SQLite identifier. Use it for foreign-key joins, not as an API-Football ID.
- `provider_player_id` is nullable until identity is known and unique when present. The current unique constraint is global across providers, so the implementation assumes the configured provider IDs do not collide.
- `provider` identifies the provider namespace. It is nullable for compatibility but new resolved rows normally set it.
- `resolution_status` records identity resolution separately from crawl `status`. Supported configured resolution values include `pending`, `resolved`, `needs_team_or_league`, `needs_review`, `ambiguous`, `not_found`, `not_found_for_season`, `paused_quota_limit`, `failed`, `resolving_team`, `team_resolved`, `resolving_players_page`, `players_page_resolved`, and `matching_candidates`.
- `status` is operational state, commonly `pending`, `in_progress`, `paused`, `completed`, or `unsupported_for_subscription`; values are text and are not constrained by a SQL `CHECK`.
- `crawl_from_season` and `crawl_to_season` define the crawl window. `desired_historical_from_season` is a configuration input, not a column in this table.
- `created_at`, `updated_at`, and `resolved_at` are text timestamps; application-generated values use UTC ISO 8601.

**Constraints and indexes:** `id` is the primary key; `provider_player_id` has a unique constraint. `name` is `NOT NULL`, and `status` and `resolution_status` are `NOT NULL` with the defaults above.

### `seasons`

**Purpose:** Tracks crawl progress and outcome for each player-season.

**Granularity:** One row per player and season.

**Relationships:** `player_id` references `players.id`; unique on `(player_id, season)`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal season-progress row ID. |
| `player_id` | INTEGER | No | Parent player. |
| `season` | INTEGER | No | Season start year used by the provider. |
| `status` | TEXT | No; default `pending` | Crawl outcome/state for this season. |
| `fixtures_found` | INTEGER | Yes; default `0` | Candidate fixtures discovered. |
| `fixtures_processed` | INTEGER | Yes; default `0` | Candidate fixtures whose participation and configured processing completed. |
| `started_at` | TEXT | Yes | First crawl start timestamp. |
| `completed_at` | TEXT | Yes | Season completion timestamp or terminal no-team timestamp. |

#### Important columns and rules

- `(player_id, season)` identifies a season attempt and prevents duplicate progress rows.
- `status` values used by the pipeline include `pending`, `in_progress`, `paused`, `completed`, `unsupported_for_subscription`, and `no_team_data`.
- `fixtures_found` counts discovered team fixtures, not necessarily appearances by the player. `fixtures_processed` can include candidate fixtures ruled out by participation checking.
- `started_at` and `completed_at` are UTC ISO 8601 text when set.

**Constraints and indexes:** Primary key `id`; unique `(player_id, season)`; foreign key `player_id → players.id`; `player_id` and `season` are `NOT NULL`. Counter columns default to zero but are nullable under the declared schema.

### `fixtures`

**Purpose:** Stores match-level metadata returned by the provider.

**Granularity:** One row per provider fixture ID.

**Relationships:** `player_id` references `players.id`. Parent of `match_events`, `lineups`, `player_match_stats`, and `player_fixtures`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal fixture row ID. |
| `provider_fixture_id` | TEXT | Yes; unique | Provider's match identifier. |
| `player_id` | INTEGER | No | Primary/legacy player association for the fixture. |
| `season` | INTEGER | No | Season year associated with this fixture. |
| `competition_id` | TEXT | Yes | Provider competition identifier. |
| `competition_name` | TEXT | Yes | Competition display name. |
| `date` | TEXT | Yes | Kickoff date/time returned by the provider. |
| `status` | TEXT | Yes | Provider fixture status code. |
| `home_team_id` | TEXT | Yes | Provider ID of the home team. |
| `home_team_name` | TEXT | Yes | Home team name. |
| `away_team_id` | TEXT | Yes | Provider ID of the away team. |
| `away_team_name` | TEXT | Yes | Away team name. |
| `home_goals` | INTEGER | Yes | Home team's recorded score. |
| `away_goals` | INTEGER | Yes | Away team's recorded score. |
| `venue` | TEXT | Yes | Venue name, when returned. |
| `referee` | TEXT | Yes | Referee name, when returned. |
| `raw_json_path` | TEXT | Yes | Path to saved provider JSON, if enabled. |
| `created_at` | TEXT | Yes | Row creation timestamp. |
| `updated_at` | TEXT | Yes | Last metadata upsert timestamp. |

#### Important columns and rules

- `id` is used by child tables. `provider_fixture_id` is the external fixture identifier and is unique in the current schema.
- `player_id` identifies the player associated when the fixture row was first inserted. Since one fixture may be discovered for multiple players, `player_fixtures` is the player-specific association; joins for an individual's match log should use it.
- `season` is the competition season year, not necessarily the calendar year in which every match date falls.
- `competition_id` and team IDs are text because external identifiers should not be coerced into internal SQLite IDs.
- `status` is the provider's fixture/match status (for example, an API-Football short code such as `FT` for full-time), not the crawler's status.
- `date` is retained as provider data. Do not assume that it uses the project's UTC timestamp formatter.
- `raw_json_path` is nullable when raw-response saving is disabled or no raw payload is available.

**Constraints and indexes:** Primary key `id`; unique `provider_fixture_id`; foreign key `player_id → players.id`; `player_id` and `season` are `NOT NULL`. Indexes: `idx_fixtures_player_season(player_id, season)` and `idx_fixtures_provider_id(provider_fixture_id)`; uniqueness also creates an SQLite autoindex.

### `player_fixtures`

**Purpose:** Records a player's candidate fixture and whether the player was confirmed to participate.

**Granularity:** One row per player and provider fixture, regardless of how many associated teams discovered the same fixture.

**Relationships:** `player_id → players.id`; `fixture_id → fixtures.id`; unique `(player_id, provider_fixture_id)`. Child of `player_fixture_teams`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal player-fixture relationship ID. |
| `player_id` | INTEGER | No | Configured player. |
| `fixture_id` | INTEGER | No | Internal match row. |
| `provider_player_id` | TEXT | No | External player ID used to check participation. |
| `provider_fixture_id` | TEXT | No | External fixture ID. |
| `season` | INTEGER | No | Season of the candidate fixture. |
| `provider_team_id` | TEXT | Yes | First associated provider team retained on this row. |
| `provider_team_name` | TEXT | Yes | Name of that associated team. |
| `participated` | INTEGER | Yes | `NULL` unchecked, `1` confirmed, `0` not found. |
| `participation_source` | TEXT | No; default `unknown` | Evidence source for the participation value. |
| `raw_json_path` | TEXT | Yes | Raw fixture-list response path, if saved. |
| `participation_raw_json_path` | TEXT | Yes | Raw fixture-player response path, if saved. |
| `created_at` | TEXT | Yes | Relationship creation timestamp. |
| `updated_at` | TEXT | Yes | Last relationship/participation update timestamp. |

#### Important columns and rules

- `participated` must remain `NULL` until a participation check has run. The current provider sets it to `1` or `0` only after reading `/fixtures/players?fixture={fixture_id}` and checking for the configured `provider_player_id`.
- `participation_source` currently starts as `unknown` and is set to `fixture_players_endpoint` by the pipeline after a check. The schema does not constrain source strings.
- `provider_team_id`/`provider_team_name` capture one team association; every additional association is stored in `player_fixture_teams`.
- The provider fixture ID is repeated here intentionally so the unique key is provider-facing and the relationship can be audited. Use the internal `fixture_id` for joins.
- Match-log queries must select only `participated = 1`. A team fixture by itself does not establish that the player appeared.

**Constraints and indexes:** Primary key `id`; unique `(player_id, provider_fixture_id)`; foreign keys to `players.id` and `fixtures.id`; the listed player, fixture, provider IDs, and season are `NOT NULL`. `participation_source` is `NOT NULL DEFAULT 'unknown'`. No SQL `CHECK` currently restricts `participated` to `NULL`, `0`, or `1`.

### `player_fixture_teams`

**Purpose:** Preserves all teams whose season fixture lists contributed to one player's candidate fixture.

**Granularity:** One row per `player_fixtures` relationship and provider team.

**Relationships:** `player_fixture_id → player_fixtures.id ON DELETE CASCADE`; composite primary key `(player_fixture_id, provider_team_id)`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `player_fixture_id` | INTEGER | No | Candidate player-fixture relationship. |
| `provider_team_id` | TEXT | No | Provider team identifier. |
| `provider_team_name` | TEXT | Yes | Team name as returned by the provider. |
| `created_at` | TEXT | Yes | Association creation timestamp. |

#### Important columns and rules

- A fixture may be returned for both sides of a match, or be found through multiple player team associations. This junction table retains those origins while `player_fixtures` deduplicates the player/fixture pair.
- Deleting a `player_fixtures` row cascades to its team associations. The parent player-fixture row is not automatically deleted when a `players` or `fixtures` row is deleted because those foreign keys use SQLite's default `NO ACTION`.

**Constraints and indexes:** Composite primary key `(player_fixture_id, provider_team_id)`; both columns are `NOT NULL`; foreign key to `player_fixtures.id` with `ON DELETE CASCADE`.

### `match_events`

**Purpose:** Stores incidents in a match timeline, such as goals, cards, and substitutions, as reported by the provider.

**Granularity:** One row per fixture and provider event identifier.

**Relationships:** `fixture_id → fixtures.id`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal event row ID. |
| `fixture_id` | INTEGER | No | Parent match. |
| `provider_event_id` | TEXT | Yes | Provider event ID or generated event hash. |
| `minute` | TEXT | Yes | Match minute, possibly including stoppage time. |
| `event_type` | TEXT | Yes | Provider category of incident. |
| `detail` | TEXT | Yes | Provider's event detail/description. |
| `team_id` | TEXT | Yes | Provider ID of the team associated with the event. |
| `team_name` | TEXT | Yes | Team associated with the event. |
| `player_id` | TEXT | Yes | Provider player ID attached to the event. |
| `player_name` | TEXT | Yes | Player attached to the event. |
| `assist_player_id` | TEXT | Yes | Provider player ID recorded as the assist provider. |
| `assist_player_name` | TEXT | Yes | Name of the assist provider. |
| `raw_json_path` | TEXT | Yes | Saved raw response path. |
| `created_at` | TEXT | Yes | Row creation timestamp. |

#### Important columns and rules

- `(fixture_id, provider_event_id)` is the event's deduplication key. If the provider supplies no ID, the application uses a deterministic hash of the event payload. Since `provider_event_id` is nullable at the schema level, SQLite permits multiple `NULL` values under the unique constraint; application code should continue supplying an ID/hash.
- `minute` is text because it may contain stoppage-time notation such as `90+3`.
- A provider event's player and assist fields refer to the event participants, not necessarily the configured player whose crawl discovered the fixture.
- `event_type` and `detail` contain provider vocabulary; the schema does not enumerate allowed values.

**Constraints and indexes:** Primary key `id`; unique `(fixture_id, provider_event_id)`; foreign key `fixture_id → fixtures.id`; `fixture_id` is `NOT NULL`; index `idx_events_fixture(fixture_id)`.

### `lineups`

**Purpose:** Stores the provider's team lineup for a fixture, including starters and substitutes.

**Granularity:** One row per fixture, team, and lineup player.

**Relationships:** `fixture_id → fixtures.id`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal lineup row ID. |
| `fixture_id` | INTEGER | No | Parent match. |
| `team_id` | TEXT | Yes | Provider team ID. |
| `team_name` | TEXT | Yes | Team name. |
| `formation` | TEXT | Yes | Team shape, such as `4-3-3`. |
| `coach_name` | TEXT | Yes | Coach name for the lineup. |
| `player_id` | TEXT | Yes | Provider ID for this lineup player. |
| `player_name` | TEXT | Yes | Player name. |
| `player_number` | TEXT | Yes | Shirt number as returned by the provider. |
| `position` | TEXT | Yes | Position code/label. |
| `grid` | TEXT | Yes | Provider lineup grid/location value. |
| `is_starter` | INTEGER | Yes | `1` for a starting XI entry, `0` for a substitute entry. |
| `is_captain` | INTEGER | Yes | `1` when marked captain, otherwise `0`. |
| `raw_json_path` | TEXT | Yes | Saved raw response path. |
| `created_at` | TEXT | Yes | Row creation timestamp. |

#### Important columns and rules

- The unique key `(fixture_id, team_id, player_id)` supports idempotent lineup upserts. The player ID here identifies any participant in either team, not only the configured player.
- `is_starter` distinguishes the starting XI from the substitutes list; the schema stores this as an integer flag and does not add a `CHECK` constraint.
- `position`, `formation`, `grid`, and shirt number are provider-reported text and may be null or provider-specific.

**Constraints and indexes:** Primary key `id`; unique `(fixture_id, team_id, player_id)`; foreign key `fixture_id → fixtures.id`; `fixture_id` is `NOT NULL`; index `idx_lineups_fixture(fixture_id)`.

### `player_match_stats`

**Purpose:** Stores per-player performance statistics for a fixture.

**Granularity:** One row per fixture and provider player ID.

**Relationships:** `fixture_id → fixtures.id`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal statistic row ID. |
| `fixture_id` | INTEGER | No | Parent match. |
| `provider_player_id` | TEXT | Yes | Provider ID for the player. |
| `player_name` | TEXT | Yes | Player name. |
| `team_id` | TEXT | Yes | Provider team ID for the player's match stats. |
| `team_name` | TEXT | Yes | Player's team name for this fixture. |
| `minutes_played` | INTEGER | Yes | Minutes played. |
| `rating` | TEXT | Yes | Provider rating; text preserves decimal formatting. |
| `goals` | INTEGER | Yes | Goals scored by this player in the fixture. |
| `assists` | INTEGER | Yes | Assists recorded for this player. |
| `shots_total` | INTEGER | Yes | Total shots. |
| `shots_on_goal` | INTEGER | Yes | Shots on target. |
| `passes_total` | INTEGER | Yes | Total passes. |
| `passes_accuracy` | TEXT | Yes | Provider pass-accuracy value, often formatted as a percentage. |
| `duels_total` | INTEGER | Yes | Total duels contested. |
| `duels_won` | INTEGER | Yes | Duels won. |
| `raw_json_path` | TEXT | Yes | Saved raw response path. |
| `created_at` | TEXT | Yes | Row creation timestamp. |

#### Important columns and rules

- `(fixture_id, provider_player_id)` is the upsert key. `provider_player_id` is nullable in the schema; SQLite's unique constraint permits multiple null IDs, so provider data should include IDs for reliable deduplication.
- `rating` and `passes_accuracy` are text to preserve provider formatting and precision.
- `goals` and `assists` are the stats endpoint's per-player totals; event-level goal/assist attribution is separately represented in `match_events`.
- This table's foreign key is to the fixture, not to `players`. Use the provider player ID to identify the athlete.

**Constraints and indexes:** Primary key `id`; unique `(fixture_id, provider_player_id)`; foreign key `fixture_id → fixtures.id`; `fixture_id` is `NOT NULL`; index `idx_stats_fixture(fixture_id)`.

### `crawl_progress`

**Purpose:** Stores a per-player, per-season, per-stage summary of crawl progress.

**Granularity:** One row per player, nullable season, and stage.

**Relationships:** `player_id → players.id`; unique `(player_id, season, stage)`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal progress row ID. |
| `player_id` | INTEGER | No | Parent player. |
| `season` | INTEGER | Yes | Season for this progress record; null for player-level stages. |
| `stage` | TEXT | No | Pipeline stage. |
| `status` | TEXT | No | Progress status. |
| `completed_items` | INTEGER | Yes; default `0` | Number of completed items at this stage. |
| `total_items` | INTEGER | Yes; default `0` | Known total for this stage. |
| `last_processed_id` | TEXT | Yes | Last processed fixture/item identifier. |
| `error_message` | TEXT | Yes | Latest recorded error, if any. |
| `updated_at` | TEXT | Yes | Last progress update timestamp. |

#### Important columns and rules

- A null `season` is used for player-wide work such as identity or season discovery.
- `stage` records where the pipeline is or was operating; current crawl stages include season discovery, player team discovery, team fixture discovery, participation verification, fixture detail retrieval, events, lineups, and player statistics.
- This row is a summary only. Detailed resumable lists and page state are stored in `player_checkpoints.checkpoint_json` and checkpoint JSON files.
- `status` commonly uses `pending`, `in_progress`, `paused`, or `completed`; stage-specific and error states may also be written as text.

**Constraints and indexes:** Primary key `id`; unique `(player_id, season, stage)`; foreign key `player_id → players.id`; `player_id`, `stage`, and `status` are `NOT NULL`; counts default to zero but are nullable. Index `idx_progress_player_season_stage(player_id, season, stage)`.

### `request_log`

**Purpose:** Audits each outgoing provider API attempt, including its attribution and result.

**Granularity:** One row per network attempt, including retries.

**Relationships:** `player_id` is an application-level reference to `players.id`; no foreign-key constraint is declared.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal request row ID. |
| `provider` | TEXT | Yes | Provider name. |
| `player_id` | INTEGER | Yes | Internal player attribution. |
| `provider_player_id` | TEXT | Yes | External player ID attribution. |
| `season` | INTEGER | Yes | Season associated with the request. |
| `fixture_id` | TEXT | Yes | Provider fixture ID associated with the request. |
| `stage` | TEXT | Yes | Pipeline/resolution stage. |
| `team_id` | TEXT | Yes | Provider team ID, when relevant. |
| `team_name` | TEXT | Yes | Team name, when relevant. |
| `league_id` | TEXT | Yes | Provider league ID, when relevant. |
| `league_name` | TEXT | Yes | League name, when relevant. |
| `endpoint` | TEXT | Yes | API endpoint path. |
| `url` | TEXT | Yes | URL without query parameters. |
| `status_code` | INTEGER | Yes | HTTP response status code. |
| `success` | INTEGER | Yes | `1` for success, `0` for failed/in-progress attempt. |
| `error_message` | TEXT | Yes | Error or in-progress marker. |
| `requested_at` | TEXT | Yes | UTC attempt timestamp. |
| `page_number` | INTEGER | Yes | Page associated with paginated work. |

#### Important columns and rules

- A row is created before network I/O; it may temporarily have `success = 0` and `error_message = 'request_in_progress'`. The outcome is updated after the attempt.
- The request URL intentionally omits its query string, preventing parameters and credentials from being persisted there. Do not add API keys to this table.
- Requests that occur before player resolution may have null player attribution.
- Each retry is a separate logged request and consumes quota.

**Constraints and indexes:** Primary key `id`; no foreign keys, unique constraints, or explicit indexes are declared for this table. All data columns are nullable in the current schema.

### `daily_request_usage`

**Purpose:** Tracks the global API request count for each UTC date.

**Granularity:** One row per request date.

**Relationships:** No foreign keys.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `request_date` | TEXT | No (PK) | UTC date key in `YYYY-MM-DD` form. |
| `requests_used` | INTEGER | No; default `0` | Number of globally reserved API attempts. |

#### Important columns and rules

- A request is atomically reserved before sending it, so the count includes retries and attempted requests.
- The table tracks usage independently of request-log rows to ensure quota reservations remain authoritative even if a request is interrupted.

**Constraints and indexes:** Primary key `request_date`; `requests_used INTEGER NOT NULL DEFAULT 0`.

### `daily_player_request_usage`

**Purpose:** Tracks API request usage attributed to a configured player by UTC date.

**Granularity:** One row per player name and request date.

**Relationships:** No foreign keys; player attribution is stored by name.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `request_date` | TEXT | No; composite PK | UTC date key. |
| `player_name` | TEXT | No; composite PK | Configured player name. |
| `requests_used` | INTEGER | No; default `0` | Requests reserved for this player/date. |

#### Important columns and rules

- Composite key `(request_date, player_name)` prevents duplicate daily counters for the same name.
- The player name is not constrained to a row in `players`; historical counters can remain after a player record is removed.

**Constraints and indexes:** Composite primary key `(request_date, player_name)`; `requests_used INTEGER NOT NULL DEFAULT 0`.

### `player_checkpoints`

**Purpose:** Stores the latest complete JSON checkpoint as a recoverable SQLite copy.

**Granularity:** One checkpoint per player.

**Relationships:** `player_id → players.id`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `player_id` | INTEGER | No (PK) | Parent player and checkpoint key. |
| `checkpoint_json` | TEXT | No | Serialized `CheckpointState`. |
| `updated_at` | TEXT | Yes | Last checkpoint save timestamp. |

#### Important columns and rules

- `checkpoint_json` includes resumable state such as active season/stage, discovered and processed fixture IDs, team/page progress, participation checks, quota date/count, and errors.
- The same state is saved as an atomic per-player JSON file. SQLite provides recovery if the file is unavailable.
- JSON contents evolve with the application and are not a normalized relational record. Use checkpoint/model code to interpret it.

**Constraints and indexes:** `player_id` primary key and foreign key to `players.id`; `checkpoint_json TEXT NOT NULL`.

### `player_candidates`

**Purpose:** Preserves possible provider identities found during configured player resolution.

**Granularity:** One candidate provider identity within a player/provider/attempt context.

**Relationships:** No foreign keys; `player_config_name` is a text association to configuration.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal candidate row ID. |
| `player_config_name` | TEXT | No | Configured person being resolved. |
| `provider` | TEXT | No | Provider namespace. |
| `provider_player_id` | TEXT | Yes | Candidate external player ID. |
| `provider_team_id` | TEXT | Yes | Team used to find the candidate. |
| `provider_team_name` | TEXT | Yes | Team name. |
| `season` | INTEGER | Yes | Candidate search season. |
| `matched_name` | TEXT | Yes | Provider-returned player name. |
| `name_similarity` | REAL | Yes | Similarity score calculated by the resolver. |
| `status` | TEXT | No; default `candidate` | Candidate decision state. |
| `raw_json_path` | TEXT | Yes | Saved source response path. |
| `source_page` | INTEGER | Yes | Player-search page where candidate was found. |
| `attempt_id` | TEXT | Yes | Resolution attempt identifier. |
| `created_at` | TEXT | Yes | Row creation timestamp. |
| `updated_at` | TEXT | Yes | Last candidate update timestamp. |

#### Important columns and rules

- The actual column is `attempt_id`, not `resolution_attempt_id`.
- Candidate deduplication is maintained by expression index `idx_player_candidate_identity` over configured name, provider, and coalesced candidate player ID, team ID, and season. Partial unique index `idx_player_candidate_attempt_identity` additionally deduplicates non-null player IDs within an attempt.
- Candidate statuses used by resolution include `candidate`, `selected`, `rejected`, and `ambiguous`; no `CHECK` constraint restricts the text.
- Similarity is resolver evidence, not a provider field or proof of identity. A candidate must not be treated as resolved until the configured decision rules or explicit user selection mark it so.

**Constraints and indexes:** Primary key `id`; `player_config_name`, `provider` are `NOT NULL`; `status TEXT NOT NULL DEFAULT 'candidate'`; the two unique indexes described above.

### `player_resolution_attempts`

**Purpose:** Stores season-specific player-resolution attempt state and outcomes.

**Granularity:** One row per unique attempt ID.

**Relationships:** No foreign keys; associated with the configured player by `player_config_name`.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `attempt_id` | TEXT | No (PK) | Unique attempt key. |
| `player_config_name` | TEXT | No | Configured player name. |
| `provider` | TEXT | No | Provider namespace. |
| `season` | INTEGER | No | Season attempted. |
| `team_id` | TEXT | Yes | Resolved team ID, when team-based. |
| `team_name` | TEXT | Yes | Resolved team name. |
| `status` | TEXT | No | Attempt outcome/stage. |
| `state_json` | TEXT | No | Serialized attempt-specific pagination/candidate state. |
| `created_at` | TEXT | No | Attempt creation timestamp. |
| `updated_at` | TEXT | No | Last attempt update timestamp. |

#### Important columns and rules

- `state_json` preserves attempt-specific details so trying another season does not reuse the prior season's page state.
- `status` is text and may contain resolution outcomes such as `pending`, `not_found_for_season`, `needs_review`, `resolved`, `paused_quota_limit`, or `failed`.
- `attempt_id` is the primary key. Attempt rows do not have a relational FK to `players`; use the configured name and application logic.

**Constraints and indexes:** Primary key `attempt_id`; `player_config_name`, `provider`, `season`, `status`, `state_json`, `created_at`, and `updated_at` are `NOT NULL`.

### `player_resolution_searches`

**Purpose:** Caches whether a player/provider/team-or-league/season search has completed.

**Granularity:** One row per configured player, provider, team filter, league filter, and season.

**Relationships:** No foreign keys.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `id` | INTEGER | No (generated PK) | Internal search-cache row ID. |
| `player_config_name` | TEXT | No | Configured player name. |
| `provider` | TEXT | No | Provider namespace. |
| `team_id` | TEXT | Yes | Team search filter. |
| `league_id` | TEXT | Yes | League search filter. |
| `season` | INTEGER | No | Search season. |
| `completed` | INTEGER | No; default `0` | Whether the search is complete. |
| `created_at` | TEXT | Yes | Cache row creation timestamp. |
| `updated_at` | TEXT | Yes | Last cache update timestamp. |

#### Important columns and rules

- The table-level unique constraint covers `(player_config_name, provider, team_id, league_id, season)`.
- Expression unique index `idx_resolution_search_identity` also coalesces nullable team/league filters to empty strings. This prevents duplicate logical searches when optional filters are null.
- `completed` is represented as an integer flag; there is no SQL `CHECK` constraint.

**Constraints and indexes:** Primary key `id`; unique constraint and expression unique index described above; `player_config_name`, `provider`, `season` are `NOT NULL`; `completed INTEGER NOT NULL DEFAULT 0`.

### `player_resolution_cache`

**Purpose:** Caches provider response payloads used during player resolution.

**Granularity:** One row per configured player, provider, and cache key.

**Relationships:** No foreign keys.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `player_config_name` | TEXT | No; composite PK | Configured player name. |
| `provider` | TEXT | No; composite PK | Provider namespace. |
| `cache_key` | TEXT | No; composite PK | Search/page-specific cache key. |
| `payload_json` | TEXT | No | Serialized provider response. |
| `updated_at` | TEXT | No | Last cache update timestamp. |

#### Important columns and rules

- Composite primary key `(player_config_name, provider, cache_key)` isolates cached data across players/providers and distinct searches/pages.
- Payloads are provider data, not authoritative user identity decisions. The resolver interprets them alongside checkpoint and candidate records.

**Constraints and indexes:** Composite primary key `(player_config_name, provider, cache_key)`; all columns are `NOT NULL`.

### `subscription_season_ranges`

**Purpose:** Caches the season interval currently available under a provider subscription.

**Granularity:** One row per provider.

**Relationships:** No foreign keys.

**Brief column summary:**

| Column | Type | Nullable | Purpose |
|---|---|---:|---|
| `provider` | TEXT | No (PK) | Provider key. |
| `minimum_season` | INTEGER | No | Earliest supported season. |
| `maximum_season` | INTEGER | No | Latest supported season. |
| `updated_at` | TEXT | No | Last range update timestamp. |

#### Important columns and rules

- The range is subscription/provider metadata, not proof that each individual season has player or team data.
- Application code should ensure `minimum_season <= maximum_season`; the current schema has no `CHECK` constraint enforcing the comparison.

**Constraints and indexes:** Primary key `provider`; all columns are `NOT NULL`.

## Data dictionary

| Term or convention | Meaning |
|---|---|
| Internal ID | SQLite key such as `players.id` or `fixtures.id`; used for relational joins. |
| `provider_*` ID | Identifier in a provider's namespace. It is not interchangeable with the SQLite key or another provider's ID. |
| `fixture_id` | Usually the internal `fixtures.id` in child tables. In `request_log`, it is text and stores the provider fixture identifier. |
| Season | The competition season start year, for example `2022`; it is not a matchday or a date. |
| Status | A text value whose meaning is table-specific: crawl status, resolution outcome, season outcome, fixture status, or candidate decision. Most status columns have no SQL enum/check constraint. |
| Nullable | `NULL` means the value is unknown, not returned, not applicable, or not yet checked, depending on the column. It should not be silently replaced with a false value. |
| Raw JSON path | Local path to saved provider payload when `crawling.save_raw_json` is enabled. It is not the payload itself. |
| UTC timestamp | Project-generated timestamp as ISO 8601 text with `+00:00`; quota dates are date-only UTC keys. |

`desired_historical_from_season` is configured in `config/players.yaml` and used by the crawl workflow, but is not persisted as a column in the current SQLite schema. Similarly, detailed team/page/fixture pagination is part of checkpoint JSON, not separate normalized SQL columns.

## Football terminology

| Term | Plain-English definition | Database representation / example |
|---|---|---|
| Fixture | A scheduled or played match between two teams. | One `fixtures` row; for example, `provider_fixture_id = '12345'`. |
| Season | A competition's yearly playing cycle. | `seasons.season` / `fixtures.season`, such as `2022`. |
| Competition | A league or tournament in which teams play. | `fixtures.competition_id` and `competition_name`. |
| Matchday | A numbered round of fixtures in a competition. | Not stored in a dedicated column in the current schema. |
| Home team | The team designated as playing at its home venue. | `fixtures.home_team_name` and `home_team_id`. |
| Away team | The team playing away from home. | `fixtures.away_team_name` and `away_team_id`. |
| Kickoff | The scheduled start time of a match. | `fixtures.date`; provider timestamp is retained. |
| Full-time | The end of normal playing time, including stoppage time. | Usually fixture status `FT`; stored in `fixtures.status`. |
| Score | The goals recorded for the two teams. | `home_goals` and `away_goals`, for example `2`–`1`. |
| Goal | A score credited to a player/team. | Usually a `match_events` row; player totals also appear in `player_match_stats.goals`. |
| Assist | A pass or action credited with setting up a goal. | Event `assist_player_id` / `assist_player_name`, and `player_match_stats.assists`. |
| Own goal | A goal accidentally scored into a player's own team's goal. | Provider event `detail`; the exact text is provider-specific. |
| Penalty | A direct shot from the penalty mark awarded after a foul. | Provider event `event_type` / `detail`, or provider-specific match event. |
| Missed penalty | A penalty attempt that does not result in a goal. | Provider event `detail`; not a separate column. |
| Yellow card | A caution shown to a player. | `match_events.event_type` / `detail`. |
| Second yellow card | A second caution in one match, normally followed by dismissal. | Provider event type/detail; may be represented with a red-card event as well. |
| Red card | A sending-off that removes a player from the match. | `match_events.event_type` / `detail`. |
| Substitution | One player replaces another during a match. | `match_events`; outgoing/incoming players follow provider event fields. |
| Starting XI | The eleven players who begin the match for a team. | `lineups.is_starter = 1`. |
| Substitute | A player named to enter from the bench, whether or not used. | `lineups.is_starter = 0`; match participation stats may show minutes. |
| Bench | The group of substitutes available to enter the match. | Usually represented by `lineups` substitute entries. |
| Formation | The team's tactical arrangement, such as `4-3-3`. | `lineups.formation`. |
| Captain | The player designated to lead the team on the field. | `lineups.is_captain = 1` when returned by the provider. |
| Position | A player's on-field role or lineup location. | `lineups.position`; provider value may be a code such as `M`. |
| Goalkeeper | The player primarily responsible for preventing goals and guarding the goal. | Provider position value, commonly `G`/`GK`, in `lineups.position`. |
| Defender | A player whose primary role is to protect the team's goal. | Provider position value, commonly `D`, in `lineups.position`. |
| Midfielder | A player who generally links defence and attack. | Provider position value, commonly `M`, in `lineups.position`. |
| Forward | A player whose primary role is to create or score attacking chances. | Provider position value, commonly `F`/`A`, in `lineups.position`. |
| Minutes played | Time a player took part in the match, as reported by the provider. | `player_match_stats.minutes_played`; may include added-time notation in some source data. |
| Shot | An attempt to score. | `player_match_stats.shots_total`. |
| Shot on target | A shot that would enter the goal but for a save or goal-line intervention, or that scores. | `player_match_stats.shots_on_goal`. |
| Pass | An attempt to move the ball to a teammate. | `player_match_stats.passes_total`. |
| Pass accuracy | Percentage of completed passes according to provider rules. | `player_match_stats.passes_accuracy`, stored as text. |
| Duel | A contest between players for possession or position. | `player_match_stats.duels_total` and `duels_won`. |
| Rating | Provider-calculated performance score. | `player_match_stats.rating`, stored as text to preserve formatting. |
| Clean sheet | A goalkeeper/team performance with no goals conceded while the player is eligible under provider rules. | No dedicated column currently; infer cautiously from fixture score and appearance/minutes. |
| Extra time | Additional playing periods used to decide some knockout matches. | Often reflected in fixture status such as `AET` or provider events/minutes. |
| Penalty shootout | A series of penalty kicks used to decide a tied match after extra time. | Often reflected in fixture status such as `PEN`; not a separate score column. |
| VAR | Video Assistant Referee review of a match decision. | May appear in `match_events.event_type` / `detail`; no dedicated column. |

Provider event labels and position codes are not normalized to a closed vocabulary. Preserve the provider-returned value and interpret it in the context of that provider.

## Data integrity rules

- **Primary keys:** Every table has a declared primary key. Integer `id` keys use `AUTOINCREMENT`; lookup/cache tables use natural or composite keys where defined.
- **Foreign keys:** Connections enforce `PRAGMA foreign_keys = ON` for each application connection. Child fixture details reference `fixtures.id`; season/progress/player-fixture/checkpoint rows reference `players.id`; team associations cascade when their `player_fixtures` parent is explicitly deleted.
- **Unique constraints:** The schema enforces provider player/fixture IDs, player-season progress, player-fixture relationships, per-fixture event/lineup/stat identities, player-stage progress, resolution cache/search keys, daily counters, and other listed unique identities. Nullable members can weaken uniqueness under SQLite's `NULL` semantics.
- **NOT NULL and defaults:** Follow the table definitions above. Most football/provider data remains nullable because the provider may omit it. Status/counter defaults do not imply that every status value is validated by SQL.
- **Expected status values:** `players.status`, `seasons.status`, `resolution_status`, fixture status, and candidate status are distinct concepts. The code writes common states described in their table entries, but these columns are generally plain `TEXT` without `CHECK` constraints; readers should tolerate new values.
- **Timestamps:** Project-created timestamp text is UTC ISO 8601 with `+00:00`; quota keys use `YYYY-MM-DD`. Provider fixture dates may retain a provider offset. Never compare differently formatted timestamps as raw local time.
- **Upserts:** Database methods use `INSERT ... ON CONFLICT` for stable identities. Upserts allow a rerun to refresh returned metadata without duplicating its logical record.
- **Duplicate prevention:** Duplicate matches/events/statistics inflate totals and timelines; unique keys and upserts make retries and resume safe. Keep their identity fields stable.
- **Provider identity:** A provider ID belongs to a provider namespace and may differ from internal IDs and other providers' IDs. Do not join external IDs to internal IDs.
- **Player participation:** A team-season fixture listing only shows that a team played the fixture. Set `player_fixtures.participated` to true only after the provider's fixture-player data contains that player's configured provider ID. Candidate fixtures with unknown or false participation must not be presented as confirmed appearances.
- **Parameterization and transactions:** Bind user/API values as SQL parameters. Multi-record changes should use `Database.transaction()` so partial writes are not left behind on an error.

## Common queries

These examples are read-only. Replace named parameters at execution time; do not interpolate values into SQL strings.

### List all players and their crawl status

```sql
SELECT id, name, provider, provider_player_id, resolution_status, status
FROM players
ORDER BY name;
```

### List all seasons for Cristiano Ronaldo

```sql
SELECT s.season, s.status, s.fixtures_found, s.fixtures_processed,
       s.started_at, s.completed_at
FROM seasons AS s
JOIN players AS p ON p.id = s.player_id
WHERE p.name = :player_name COLLATE NOCASE
ORDER BY s.season;
```

Bind `:player_name` to `Cristiano Ronaldo`.

### List completed fixtures for a player and season

```sql
SELECT f.provider_fixture_id, f.date, f.competition_name,
       f.home_team_name, f.home_goals, f.away_goals, f.away_team_name,
       pf.participated
FROM player_fixtures AS pf
JOIN players AS p ON p.id = pf.player_id
JOIN fixtures AS f ON f.id = pf.fixture_id
WHERE p.name = :player_name COLLATE NOCASE
  AND pf.season = :season
  AND pf.participated = 1
  AND f.status IN ('FT', 'AET', 'PEN')
ORDER BY f.date;
```

The status codes shown are common API-Football examples; confirm the configured provider's status vocabulary before adapting the filter.

### List goals and assists for a player

```sql
SELECT f.date, f.competition_name, f.home_team_name, f.away_team_name,
       s.goals, s.assists
FROM player_match_stats AS s
JOIN fixtures AS f ON f.id = s.fixture_id
WHERE s.provider_player_id = :provider_player_id
ORDER BY f.date;
```

### Get a full match timeline for one fixture

```sql
SELECT minute, event_type, detail, team_name,
       player_name, assist_player_name
FROM match_events
WHERE fixture_id = :fixture_id
ORDER BY CAST(COALESCE(NULLIF(minute, ''), '0') AS INTEGER), id;
```

For stoppage-time strings such as `90+3`, the simple numeric sort groups by the base minute; use application-side parsing if exact stoppage ordering is required.

### Get a starting XI for one fixture and team

```sql
SELECT player_number, player_name, position, grid, is_captain
FROM lineups
WHERE fixture_id = :fixture_id
  AND team_id = :provider_team_id
  AND is_starter = 1
ORDER BY grid, player_number;
```

### Get Cristiano Ronaldo's statistics for one fixture

```sql
SELECT f.provider_fixture_id, f.date, s.player_name, s.team_name,
       s.minutes_played, s.goals, s.assists, s.shots_total, s.shots_on_goal,
       s.passes_total, s.passes_accuracy, s.duels_total, s.duels_won, s.rating
FROM player_match_stats AS s
JOIN fixtures AS f ON f.id = s.fixture_id
WHERE s.provider_player_id = :provider_player_id
  AND f.provider_fixture_id = :provider_fixture_id;
```

### Find fixtures where a player participated

```sql
SELECT pf.season, f.provider_fixture_id, f.date,
       f.home_team_name, f.home_goals, f.away_goals, f.away_team_name,
       pf.provider_team_name, pf.participation_source
FROM player_fixtures AS pf
JOIN players AS p ON p.id = pf.player_id
JOIN fixtures AS f ON f.id = pf.fixture_id
WHERE p.name = :player_name COLLATE NOCASE
  AND pf.participated = 1
ORDER BY pf.season, f.date;
```

### Count requests used today (UTC)

```sql
SELECT request_date, requests_used
FROM daily_request_usage
WHERE request_date = :utc_date;
```

### Find failed API requests

```sql
SELECT requested_at, provider, endpoint, player_id, provider_player_id,
       season, fixture_id, stage, status_code, error_message
FROM request_log
WHERE success = 0
  AND error_message IS NOT NULL
  AND error_message <> 'request_in_progress'
ORDER BY requested_at DESC;
```

### Find seasons marked `unsupported_for_subscription`

```sql
SELECT p.name, s.season, s.status
FROM seasons AS s
JOIN players AS p ON p.id = s.player_id
WHERE s.status = 'unsupported_for_subscription'
ORDER BY p.name, s.season;
```

### Find players that still need resolution

```sql
SELECT name, provider, provider_player_id, resolution_status
FROM players
WHERE resolution_status <> 'resolved'
   OR provider_player_id IS NULL
ORDER BY name;
```

## Maintenance rules

- Inspect a table's columns and defaults:

  ```sql
  PRAGMA table_info(table_name);
  ```

- Inspect indexes and their uniqueness:

  ```sql
  PRAGMA index_list(table_name);
  ```

- Inspect declared foreign keys:

  ```sql
  PRAGMA foreign_key_list(table_name);
  ```

- List all application tables:

  ```sql
  SELECT name
  FROM sqlite_master
  WHERE type = 'table'
    AND name NOT LIKE 'sqlite_%'
  ORDER BY name;
  ```

- Add an index when a real query pattern filters or joins on columns without a useful existing index; verify with `EXPLAIN QUERY PLAN` and tests rather than adding speculative indexes.
- Add a migration whenever changing a schema may encounter an existing database. `CREATE TABLE IF NOT EXISTS` alone does not add columns to an existing table; current initialization includes explicit compatibility `ALTER TABLE ... ADD COLUMN` steps for selected columns.
- Test fresh initialization and migration from a prior schema, verify constraints and foreign keys, and preserve existing data unless the change explicitly requires otherwise.
- Update `DATABASE.md` for every schema change. Update `CHANGELOG.md` for user-facing schema changes and `PROJECT_BUILD_LOG.md` for project-change requests.
- Back up the database before manual schema/data maintenance. Do not delete WAL/SHM files while SQLite is open; use the project's cleanup command only after confirming its target and intended data loss.

## Schema change procedure

1. Update the SQLite schema in `src/football_history/database.py`.
2. Add or update a migration if needed.
3. Add or update tests.
4. Update `DATABASE.md`.
5. Update `CHANGELOG.md` if the change affects users.
6. Update `PROJECT_BUILD_LOG.md`.
7. Run `pytest -v`.
8. Run `python -m football_history.main validate-config`.
9. Verify the actual database schema using `PRAGMA` commands.
10. Do not automatically commit, tag, or push.

## Documentation update rule

DATABASE.md is part of the project documentation. Whenever the SQLite schema changes—including adding, removing, renaming, or changing a table, column, constraint, index, enum value, or relationship—Copilot must update DATABASE.md in the same change.

The update must include:

- The changed table
- Changed columns
- Purpose of the change
- Impact on existing data
- Migration steps, if any
- Updated entity relationship diagram
- Updated common queries if affected
- Updated football terminology if new football concepts were introduced

Do not treat Git operations such as commit, branch, push, merge, or pull as schema changes. Those do not require a DATABASE.md update.
