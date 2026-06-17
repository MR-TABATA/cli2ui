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

[Unreleased]: https://github.com/MR-TABATA/cli2ui/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/MR-TABATA/cli2ui/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/MR-TABATA/cli2ui/releases/tag/v0.8.0
