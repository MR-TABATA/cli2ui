# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versioning convention for this project:

- **`0.x`** — deepening the PostgreSQL ops console. The `0.x` minor number tracks
  maturity, not API stability; PostgreSQL features land here (`0.8.x` patches,
  `0.9` = feature-frozen / MySQL in progress).
- **`1.0`** — multi-database support (MySQL) lands. This is the first stable
  release: cli2ui becomes a multi-DB ops console.
- **`1.x` / `2.0`** — post-1.0, additive features bump the minor; only an actual
  backward-incompatible change bumps the major.

## [Unreleased]

### Added

- **Replication: replay lag in time, not just bytes** — the Standbys table now
  shows each replica's `replay_lag` alongside the byte lag: how long a commit on
  the primary takes to become visible on that standby (`ms` / `s` / `min`). Byte
  lag tells you *how far* a replica trails; replay lag tells you *how long* —
  which is the number that matters when you read your own writes from a replica.
  It's exactly the wait PostgreSQL 19's `WAIT FOR LSN` would incur for a read
  there. Read from `pg_stat_replication.replay_lag` (also `write_lag` /
  `flush_lag` in the panel's "open in SQL"); `NULL` until Postgres has a
  round-trip sample, shown as `—`.

- **Locks: head-blocker chains** — the Locks panel now folds the wait-for graph
  into a tree instead of a flat "who's blocking me" list. Each tree is rooted at
  the *head blocker* — the session holding a lock and waiting on nothing — with
  the blocked sessions nested beneath it, deeper for each step down the chain, so
  the highest-leverage action (cancel/kill the head to free everything under it)
  is at the top, labelled with how many it's blocking. Multi-level chains
  (A waits on B waits on C) are traced to their root and blocking cycles are
  detected and flagged. Engine-agnostic — the same folding runs for PostgreSQL
  and MySQL.
- **Health: orphan rows (unenforced references)** — a new card surfacing the two
  places a referenced row can go missing: foreign keys left `NOT VALID` (their
  pre-existing rows were never checked) and `<base>_id` columns with no foreign
  key that name-match a table's single-column primary key. A validated FK can't
  have orphans, so it's never listed. Each row offers an on-demand "count
  orphans" that runs a read-only `LEFT JOIN` anti-join under a statement timeout
  — it counts, it never validates or changes anything. A fact, not a rule.
  PostgreSQL; MySQL shows "not applicable" (InnoDB has no `NOT VALID` clause, so
  a declared FK is always enforced).
- **Extensions panel** — a read-only catalog view of what's installed in this
  database and what the server could install (the Web equivalent of `\dx`
  unioned with `pg_available_extensions`), with an update-available badge when
  an installed version lags the server's default. `CREATE EXTENSION` stays in
  the SQL runner. MySQL shows "not applicable" (plugins/components are a
  different, server-level concept).
- **JSON shape on `json`/`jsonb` columns** — an on-demand "shape" view in the
  table detail's Columns tab: sample up to 500 non-null values (read-only,
  statement-timeout capped) and show the observed top-level keys with value
  types and occurrence counts, root type mix, nesting depth, and whether a GIN
  index covers the column. Describes the rows examined, not every row — a fact,
  not a rule. PostgreSQL; MySQL shows "not applicable".
- **Opt-in Airlines demo database** — `seed/demo/fetch.sh` downloads the
  Postgres Pro "Airlines" dataset (~60MB; never committed) and
  `docker compose --profile demo up -d airlines` serves it on port 5434, loaded
  once into a named volume. Realistic volume (65k flights, 593k bookings, 829k
  tickets, `jsonb` columns) for the health, activity, and sampling features.

### Fixed

- **Health: unindexed foreign keys** no longer counts a partial index (its
  predicate can't be proven for the FK's own lookup) or an invalid index (left
  behind by a failed `CREATE INDEX CONCURRENTLY`) as covering a foreign key —
  both previously produced a false "indexed" verdict.

## [1.2.0] - 2026-07-05

### Added

- **Health: unindexed foreign keys & redundant indexes** — two new read-only,
  catalog-fact cards on the Health panel. "Unindexed foreign keys" flags FK
  columns that aren't the leading key of any index (PostgreSQL doesn't auto-create
  one, so FK checks / cascade deletes / joins to the parent seq-scan; MySQL/InnoDB
  auto-indexes FK columns, so the card shows "not applicable"). "Redundant
  indexes" flags a non-unique index whose columns are a leading prefix of, or
  identical to, another index on the same table. Neither advises — they only
  surface the fact. PostgreSQL and MySQL.

## [1.1.0] - 2026-07-04

### Added

- **Dependencies panel** — the foreign-key graph of the current database,
  topologically sorted (`graphlib.TopologicalSorter`) into a safe TRUNCATE/DELETE
  order and its reverse (the safe INSERT/load order). Foreign-key cycles are
  detected and named (no valid order exists), self-referential keys are flagged,
  and the edges are listed for inspection. Read-only — nothing is truncated; the
  order is only computed and shown. Works for both PostgreSQL (`pg_constraint`)
  and MySQL (`information_schema.KEY_COLUMN_USAGE`).

## [1.0.0] - 2026-06-20

First stable release: cli2ui becomes a multi-database ops console. A full MySQL
engine lands alongside the mature PostgreSQL one, behind the same safety-first,
local-only UI — where a feature has no MySQL equivalent the panel degrades to a
clear "not applicable" rather than a misleading empty card.

### Added
- MySQL support (phase 1) — a `MysqlEngine` (PyMySQL) wired into the engine
  factory, so a connection of kind "mysql" now works for: connecting, the table
  list / column detail / row preview, the read-only ad-hoc query runner (the
  server enforces read-only via `START TRANSACTION READ ONLY`) with write mode,
  the filter builder, CSV import, streamed CSV/JSON exports, `EXPLAIN`
  (`FORMAT=JSON` parsed into the shared plan tree, so snapshots/diffs work),
  the session/process list (`SHOW PROCESSLIST`) with cancel/kill, index list +
  create/drop, table rename/truncate/drop, column add/rename/drop/retype/
  nullability/default, database create/drop, the user list, and table sizes.
  The connection form defaults the port to 3306 for MySQL and the detail panel's
  starter query uses backtick quoting.
- `cryptography` dependency — PyMySQL needs it for MySQL 8's default
  `caching_sha2_password` auth over a non-TLS connection.
- MySQL support (phase 2) — the locks panel now shows a real lock-wait graph for
  MySQL (`performance_schema.data_lock_waits`, MySQL 8.0+), and the health panel
  lists unused indexes (`sys.schema_unused_indexes`). When the server can't
  answer "is anything blocked?" — `performance_schema` is off, or the server is
  older than 8.0 — the panel raises a clear message instead of reporting a false
  "nothing blocked".
- `Engine.supports()` / `UNSUPPORTED` — engines can declare a feature
  *conceptually absent* (e.g. InnoDB has no vacuum or bloat model, and MySQL has
  no schema separate from a database). Panels then show "not applicable to this
  engine" rather than an empty card, so a structural absence is never confused
  with "no data".
- MySQL support (phase 3) — the three remaining ops surfaces now work for MySQL:
  - **Backup** via `mysqldump` / `mysql` (fixed argv, no shell, password through
    the `MYSQL_PWD` environment, never the command line). This also fixes a
    latent crash: destructive operations snapshot first via `_auto_backup`, which
    had no MySQL dump to call and 500'd before the operation ran.
  - **Settings editor** reads `performance_schema.global_variables` and persists
    changes with `SET PERSIST` (writes `mysqld-auto.cnf`, surviving a restart —
    the closest match to Postgres' `ALTER SYSTEM`). Variable names are whitelisted
    against the server catalog before use; values are bound.
  - **Replication** shows the binlog/GTID posture (role, `log_bin`, `server_id`,
    `gtid_mode`, binlog position, and replica thread/lag health), connected
    replicas, and a copy-paste recipe to attach one (`GRANT REPLICATION SLAVE` →
    `CHANGE REPLICATION SOURCE TO … SOURCE_AUTO_POSITION=1` → `START REPLICA`).
    MySQL has no replication slots, so that part is flagged not-applicable.

### Notes
- MySQL has no schema-vs-database split, so the engine reports the connection's
  database as each table's schema and scopes catalog queries to it. The remaining
  capabilities with no MySQL equivalent — role mutations, the planner what-if lab
  (MySQL DDL commits implicitly, so it can't be rolled back), replication slots,
  and vacuum/bloat/schema health — raise a clear message or are flagged "not
  applicable", so panels degrade rather than break. The distinction is
  deliberate: a feature that *could* report a problem but can't right now (lock
  waits with `performance_schema` off) raises, while one that is conceptually
  absent returns empty and is flagged — a safety signal like "is anything
  blocked?" must never degrade to a false negative.

## [0.9.0] - 2026-06-17

PostgreSQL feature-freeze milestone: the explicit PostgreSQL backlog is now
complete. Next stop is `1.0` with multi-database (MySQL) support.

### Added
- Table-level CSV/JSON export: a per-table "export" control on the table detail
  panel streams every row (read-only `SELECT *`, full table — not the preview's
  row cap) as a download, reusing the query exporter's streaming path.
- Filter builder on the table Data tab: stack column / operator (=, ≠, <, ≤, >,
  ≥, contains, starts with, is null, is not null) / value rows, ANDed, run as a
  read-only `SELECT * … WHERE …` (columns validated, values bound — never
  interpolated) and rendered in place.
- CSV import: append rows from an uploaded CSV into an existing table via `COPY`,
  matched by header name, in one all-or-nothing transaction (a bad row rolls the
  whole import back), with an automatic safety snapshot taken first.
- `CLI2UI_DB_PATH` environment variable to relocate the management SQLite file
  (e.g. onto a named Docker volume) — see README.NETWORKING.md.

## [0.8.0] - 2026-06-17

First versioned release. The project was already public at
[cli2ui.com](https://cli2ui.com); this tags the current, mature PostgreSQL-only
state as the baseline (`0.8` reflecting how much is already built, with `1.0`
reserved for multi-DB support).

### Added
- Read-only and write-mode SQL runner, with query result export to CSV/JSON
  (streamed, full result set).
- Object browser: databases, schemas, tables, columns, indexes, constraints.
- Table operations: create/rename/drop, column add/rename/type/null/default
  changes, with automatic snapshot before destructive changes.
- Database operations: create, clone, rename, drop, and ALTER schema/role.
- Backup & restore via `pg_dump`/`psql` with streaming restore, restore into a
  new or existing database, and an auto-backup total-size retention cap.
- Activity, Locks/blocking, Health (sizes, unused indexes, dead rows, bloat
  estimate), and Replication (readiness, WAL position, slots, standby setup
  recipe) panels.
- Command history.
- Optional `planner_lab` app (scale simulation + index lab), decoupled behind a
  feature flag.
- Internationalisation (English / Japanese).
- Workspace overview dashboard and unified UI (design system).

[Unreleased]: https://github.com/MR-TABATA/cli2ui/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/MR-TABATA/cli2ui/compare/v0.9.0...v1.0.0
[0.9.0]: https://github.com/MR-TABATA/cli2ui/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/MR-TABATA/cli2ui/releases/tag/v0.8.0
