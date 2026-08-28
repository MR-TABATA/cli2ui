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

## [1.6.0] - 2026-08-28

Press it and find out — that was the whole confirmation dialog. Now the button
says what it is about to do, before you press it.

### Added

- **The SQL a button is about to run, shown in its confirmation.** Not a second
  rendering of what we think it will do: `engine.preview()` runs the *same*
  method with the *same* arguments and captures what the executor composed,
  without sending it. A preview written separately from the executor drifts from
  it, and the first time you notice is when the button does something the
  preview did not show.

  The `SET lock_timeout` that guards every destructive DDL is shown too. It has
  always been there — bounding the wait so one clicked button cannot stall a
  table until the connection pool runs dry — but nothing in the UI ever said so.

- **What a TRUNCATE or DROP would take with it.** How many rows, and who points
  at this table.

  The row count comes from the planner's statistics, not `count(*)`: opening a
  confirmation dialog must not scan a billion-row table. It is labelled an
  estimate, with the date the statistics were last refreshed. A table that has
  never been analyzed reports **unknown**, not zero — "0 rows" would read as
  "empty, safe to drop".

  Foreign keys pointing at the table are listed, because a TRUNCATE fails
  without CASCADE while one exists, and finding that out after pressing is late.

- **What `DROP … CASCADE` would take with it.** Views and materialized views, by
  name. `DROP TABLE` defaults to RESTRICT and simply fails while something
  depends on it — that is the safe outcome. The dangerous one is reaching for
  CASCADE without knowing what is on the other end.

  Implicit dependencies (indexes, constraints, types) are left out: they go with
  the table by definition, and listing them buries the view you actually needed
  to see. Foreign keys are left out here too — they are counted in the line
  above, and the same object in two places reads as two casualties.

Previews that cannot be produced are not rendered at all. An empty SQL box or a
"0 rows" line would state something untrue.

## [1.5.0] - 2026-08-28

Declared, but not in force. Two constraints that are enforced with a hole in
them, both read from the catalog and both shown where you would actually notice
them — a database, not one table at a time.

### Added

- **Invalid indexes, listed DB-wide.** An index left `indisvalid = false` by a
  failed `CREATE INDEX CONCURRENTLY` is ignored by the planner outright: it
  costs disk and write time while answering nothing. The badge already existed
  on the table detail, but nobody opens 200 tables one by one.

  A build still running looks **exactly the same** in the catalog, so
  `pg_stat_progress_create_index` is read and "building" is reported as its own
  state rather than filtered out — dropping that one throws away work that is
  about to finish. A failed *concurrent drop* (`indislive = false`) gets its own
  state too: unusable for queries, still maintained on every write.

- **Composite UNIQUE with a NULL hole.** `UNIQUE (email, tenant_id)` does not
  stop two rows with the same email when `tenant_id` is NULL in both — NULLs are
  not equal to each other, so the index sees two different keys. The constraint
  is enforced; it has a hole the shape of its nullable columns.

  The list is catalog-only. Counting what actually slipped through is a separate,
  on-demand read: `GROUP BY` treats NULLs as the same group, which is precisely
  what the unique index does not do, and that difference is the duplicates.
  PostgreSQL 15's `NULLS NOT DISTINCT` closes the hole, so those indexes are not
  listed; the column itself only exists from 15, so the query is chosen by server
  version rather than referencing a column that would fail to parse on 14.

  Partial and expression indexes are excluded: a `WHERE`-qualified unique
  constraint declares uniqueness *for those rows*, so duplicates outside the
  predicate are the specification, not a slip.

Both are declared UNSUPPORTED on MySQL rather than answered with an empty card —
an empty card reads as "no problem here".

## [1.4.0] - 2026-08-23

### Fixed

- **The workspace no longer hangs while a table is locked** — `pg_total_relation_size()`
  and `pg_table_size()` open the relation they measure, so the size and bloat
  probes take `ACCESS SHARE` on every table and queued behind any `ACCESS
  EXCLUSIVE` holder. Both probes feed the overview and the Health panel, the
  pages on the way to the Locks panel, so an `ALTER TABLE` waiting on an idle
  transaction made the workspace unreachable at exactly the moment you needed it
  to explain the jam. Measured before the fix: `/locks` answered in 0.02s while
  the connection page never returned. Both probes now run under a 1s
  `lock_timeout` and report what happened; each degrades its own card, so Health
  keeps the cards a lock can't touch. (MySQL is unaffected: its sizes come from
  `information_schema.TABLES`, which reads the data dictionary — verified under a
  held `LOCK TABLES … WRITE`, where a plain `SELECT` blocked and the size query
  returned in 8ms.)
- **"Cancel" is no longer offered where it does nothing** — cancel stops a
  *running query*, so on a session that isn't running one it reports success and
  releases nothing. That session is the usual head blocker (`idle in
  transaction`), so the button appeared exactly where it couldn't help.
  Cancel now renders disabled there, with a note that only kill ends the
  transaction; sessions waiting on a lock keep it, since a lock wait means a
  statement is in flight.

### Changed

- **`docker compose up` now pulls a published image instead of building one** —
  the first screen used to be preceded by apt and pip on a laptop that has no
  reason to compile anything. Compose points at `jiniie/cli2ui:latest` (amd64 and
  arm64, pushed on `v*` tags), so starting the app is a pull; `CLI2UI_IMAGE` in
  `.env` pins a version. Building from source moves to
  `docker-compose.override.yml.example` — copy it and you get `build: .` plus the
  working-tree mount, i.e. the previous behaviour with live reload.
- **Upgrading an existing checkout moves the management database.** Dropping the
  `.:/app` mount from the default path meant saved connections and auto-backups
  would have lived inside the container and vanished on the next `up`, so
  `CLI2UI_DB_PATH` now points at `/data` on a named volume. A checkout that had
  connections saved in the project root will therefore start empty: either copy
  the existing `db.sqlite3` into the volume, or use the override, which puts the
  file back in the project root where the host can inspect or delete it.

## [1.3.0] - 2026-07-31

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

### Changed

- **Structural changes now give up instead of piling up** — every `ALTER` /
  `DROP` / `TRUNCATE` / rename the UI issues runs with a short `lock_timeout`
  (2s; `lock_wait_timeout` on MySQL, whose own default is a full year). A DDL
  statement needs an exclusive lock, so it waits behind any open transaction on
  the table — and every statement that arrives after it, plain `SELECT`s
  included, then waits behind the DDL. That is how one clicked button used to be
  able to stall a whole table until the connection pool ran dry. Now the wait is
  bounded: the change fails in seconds with a message saying nothing was changed
  and pointing at the Locks panel, and no queue ever forms. It bounds the *wait*,
  not the hold — a statement that rewrites the table (`ALTER COLUMN … TYPE`)
  still holds its lock for as long as the rewrite runs. `CREATE INDEX
  CONCURRENTLY` / `DROP INDEX CONCURRENTLY` are deliberately exempt: they block
  nobody, and timing them out is what leaves an invalid index behind.

- **Write mode offers the same lock guard** — hand-written DDL can park a table
  exactly like the buttons can, so write mode in the SQL runner runs under the
  same 2s `lock_timeout`, shown in the header line and turned off with a
  checkbox. Opt-out rather than mandatory: this is SQL you wrote yourself, and
  sometimes waiting for the lock is the point. Read-only runs are untouched — a
  waiting `SELECT` holds no lock, so nothing can queue behind it, and
  `statement_timeout` already bounds it.

### Fixed

- **Write mode never reached the server** — the write-mode and (new) lock-guard
  toggles sit outside the runner's `<form>`, and htmx only serializes a form's
  own descendants, so `write=1` was silently dropped from every request. The
  panel showed write mode as armed while the server ran the statement read-only:
  writes came back as "cannot execute … in a read-only transaction" and the
  safety snapshot that precedes a write never ran. The form now pulls the
  toggles in explicitly (`hx-include`), and a test asserts it keeps doing so.


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

[Unreleased]: https://github.com/MR-TABATA/cli2ui/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/MR-TABATA/cli2ui/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/MR-TABATA/cli2ui/compare/v0.9.0...v1.0.0
[0.9.0]: https://github.com/MR-TABATA/cli2ui/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/MR-TABATA/cli2ui/releases/tag/v0.8.0
