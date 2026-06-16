"""SQL the PostgreSQL engine runs — the catalog/stat queries, kept apart from
the engine logic in postgres.py so each file stays readable.

These are query *text* only. Identifier-quoting helpers, DDL builders and the
non-SQL constants (COLUMN_TYPES, INDEX_METHODS, DUMP_FORMATS, …) stay in
postgres.py, next to the code that uses them.
"""

# The Web equivalent of `\dt`: every user table plus an estimated row count
# (pg_stat lags reality but is free; an exact COUNT(*) per table would be slow).
LIST_TABLES_SQL = """
SELECT t.schemaname,
       t.tablename,
       COALESCE(s.n_live_tup, 0) AS rows
FROM pg_catalog.pg_tables t
LEFT JOIN pg_catalog.pg_stat_user_tables s
       ON s.schemaname = t.schemaname AND s.relname = t.tablename
WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY t.schemaname, t.tablename;
"""

# The Web equivalent of `\d table`: column name, type, nullability, default.
LIST_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position;
"""

# The Web equivalent of `\l`: every database with owner, encoding and size.
# pg_database_size() needs CONNECT, so guard it — shared/locked-down databases
# show a blank size rather than erroring the whole list.
LIST_DATABASES_SQL = """
SELECT d.datname,
       pg_catalog.pg_get_userbyid(d.datdba) AS owner,
       pg_catalog.pg_encoding_to_char(d.encoding) AS encoding,
       CASE WHEN pg_catalog.has_database_privilege(d.datname, 'CONNECT')
            THEN pg_catalog.pg_size_pretty(pg_catalog.pg_database_size(d.datname))
            END AS size
FROM pg_catalog.pg_database d
WHERE NOT d.datistemplate
ORDER BY d.datname;
"""

# The Web equivalent of `\dn`: user schemas (psql hides pg_* / information_schema).
LIST_SCHEMAS_SQL = """
SELECT n.nspname AS name,
       pg_catalog.pg_get_userbyid(n.nspowner) AS owner
FROM pg_catalog.pg_namespace n
WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'
ORDER BY n.nspname;
"""

# The Web equivalent of `\du`: roles, minus the internal pg_* ones.
LIST_ROLES_SQL = """
SELECT r.rolname, r.rolsuper, r.rolcreaterole, r.rolcreatedb,
       r.rolreplication, r.rolcanlogin, r.rolconnlimit
FROM pg_catalog.pg_roles r
WHERE r.rolname !~ '^pg_'
ORDER BY r.rolname;
"""

# The Web equivalent of querying pg_stat_activity: client sessions, what they're
# running, how long, and whether they're blocked. Includes our own connection
# (flagged is_self) so the list is never mysteriously empty; skips internal
# backends (autovacuum, walwriter, …).
ACTIVITY_SQL = """
SELECT pid, usename, datname, application_name, client_addr::text, state,
       NULLIF(concat_ws(': ', wait_event_type, wait_event), '') AS wait,
       pg_blocking_pids(pid) AS blocked_by,
       EXTRACT(EPOCH FROM (now() - query_start))::int AS query_secs,
       query,
       (pid = pg_backend_pid()) AS is_self
FROM pg_stat_activity
WHERE backend_type = 'client backend'
ORDER BY (pid = pg_backend_pid()) ASC, (state = 'active') DESC, query_start ASC NULLS LAST;
"""

# Blocked sessions: every backend stuck on a lock it can't get, plus how long
# it's waited and the contended object. pg_blocking_pids() yields the holders;
# the guard keeps only sessions actually blocked. A waiting backend has exactly
# one ungranted lock (the one it wants), so this is one row per blocked session.
BLOCKING_SQL = """
SELECT a.pid,
       a.usename,
       a.query,
       EXTRACT(EPOCH FROM (now() - a.query_start))::int AS wait_secs,
       l.locktype,
       l.mode,
       COALESCE(c.relname, l.locktype) AS object,
       pg_blocking_pids(a.pid) AS blocker_pids
FROM pg_stat_activity a
JOIN pg_locks l ON l.pid = a.pid AND NOT l.granted
LEFT JOIN pg_class c ON c.oid = l.relation
WHERE cardinality(pg_blocking_pids(a.pid)) > 0
ORDER BY wait_secs DESC NULLS LAST;
"""

# pid → (user, state, query) for every client backend, so we can describe the
# blocker sessions referenced by pg_blocking_pids without a second round-trip.
ACTIVITY_MAP_SQL = """
SELECT pid, usename, state, query
FROM pg_stat_activity
WHERE backend_type = 'client backend';
"""

# Replication posture in one row: the config knobs that decide whether a standby
# can attach, plus the current WAL position. pg_current_wal_lsn() errors while in
# recovery, so a standby reports its replay LSN instead.
REPLICATION_STATUS_SQL = """
SELECT current_setting('wal_level'),
       current_setting('max_wal_senders')::int,
       current_setting('max_replication_slots')::int,
       current_setting('hot_standby'),
       current_setting('archive_mode'),
       (CASE WHEN pg_is_in_recovery()
             THEN pg_last_wal_replay_lsn()
             ELSE pg_current_wal_lsn() END)::text,
       pg_is_in_recovery();
"""

# Connected replicas. lag is sent − replayed in bytes (how far the standby
# trails what the primary has shipped it).
STANDBYS_SQL = """
SELECT pid, usename, application_name, client_addr::text, state, sync_state,
       sent_lsn::text, replay_lsn::text,
       pg_wal_lsn_diff(sent_lsn, replay_lsn)::bigint AS lag_bytes
FROM pg_stat_replication
ORDER BY pid;
"""

# Replication slots. wal_status flags whether the WAL a slot needs is still
# kept ('reserved') or has been lost — the headline "is this slot a problem?".
SLOTS_SQL = """
SELECT slot_name, slot_type, database, active, restart_lsn::text, wal_status
FROM pg_replication_slots
ORDER BY slot_name;
"""

# Configuration parameters. current_setting() gives the human form ("128MB",
# "on") rather than pg_settings.setting's raw units ("16384" in 8kB blocks).
SETTINGS_SELECT = """
SELECT name, current_setting(name) AS value, unit, category, short_desc,
       vartype, context, enumvals, min_val, max_val, boot_val, pending_restart
FROM pg_settings
"""

# The Web equivalent of the index list in `\d table`: name, access method,
# uniqueness, whether it backs the primary key, the full definition and size.
LIST_INDEXES_SQL = """
SELECT i.relname AS name,
       am.amname AS method,
       ix.indisunique AS is_unique,
       ix.indisprimary AS is_primary,
       pg_catalog.pg_get_indexdef(ix.indexrelid) AS definition,
       pg_catalog.pg_size_pretty(pg_catalog.pg_relation_size(ix.indexrelid)) AS size,
       ix.indisvalid AS is_valid
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class i ON i.oid = ix.indexrelid
JOIN pg_catalog.pg_class t ON t.oid = ix.indrelid
JOIN pg_catalog.pg_namespace n ON n.oid = t.relnamespace
JOIN pg_catalog.pg_am am ON am.oid = i.relam
WHERE n.nspname = %s AND t.relname = %s
ORDER BY ix.indisprimary DESC, i.relname;
"""

# Health — largest tables by total on-disk size (heap + indexes + toast). The
# Web equivalent of `\dt+` sorted by size.
TABLE_SIZES_SQL = """
SELECT n.nspname AS schema,
       c.relname AS name,
       pg_total_relation_size(c.oid) AS total_bytes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_size_pretty(pg_table_size(c.oid))          AS table_size,
       pg_size_pretty(pg_indexes_size(c.oid))        AS index_size
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT %s;
"""

# Health — non-constraint indexes the planner has never used since the last
# stats reset (idx_scan = 0). Primary/unique indexes are excluded: they back
# constraints, so a zero scan count doesn't make them droppable.
UNUSED_INDEXES_SQL = """
SELECT s.schemaname AS schema,
       s.relname    AS table,
       s.indexrelname AS name,
       s.idx_scan   AS scans,
       pg_relation_size(s.indexrelid)             AS bytes,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_catalog.pg_stat_user_indexes s
JOIN pg_catalog.pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisprimary
  AND NOT i.indisunique
ORDER BY pg_relation_size(s.indexrelid) DESC;
"""

# Health — dead tuples + last (auto)vacuum/analyze per table. GREATEST ignores
# NULLs, so it yields the most recent of the manual/auto pair (or NULL if both).
VACUUM_STATS_SQL = """
SELECT schemaname, relname, n_live_tup, n_dead_tup,
       GREATEST(last_vacuum, last_autovacuum)   AS last_vacuum,
       GREATEST(last_analyze, last_autoanalyze) AS last_analyze
FROM pg_catalog.pg_stat_user_tables
ORDER BY n_dead_tup DESC, schemaname, relname;
"""

# Health — estimated table bloat from pg_stats alone (no table scan, so it's
# cheap but approximate). It compares each table's actual page count against the
# "ideal" page count its average row width implies; the gap is wasted space.
# Adapted from the long-standing PostgreSQL wiki bloat-estimation query, reduced
# to heap (table) bloat only. Needs ANALYZE to have populated pg_stats.
# NOTE: this query uses `%` (modulo) heavily, which collides with psycopg2's
# parameter expansion — so LIMIT is spliced in via .format(limit=) and the query
# is executed with no params. See the caller.
BLOAT_SQL = """
SELECT schemaname, tablename, table_bytes,
       CASE WHEN relpages < otta THEN 0
            ELSE (bs * (relpages - otta))::bigint END AS wasted_bytes,
       CASE WHEN otta = 0 THEN 1.0
            ELSE round((relpages / otta)::numeric, 2) END AS bloat_ratio
FROM (
  SELECT schemaname, tablename, cc.relpages, bs,
         pg_table_size(cc.oid) AS table_bytes,
         ceil((cc.reltuples * ((datahdr + ma -
              (CASE WHEN datahdr % ma = 0 THEN ma ELSE datahdr % ma END))
              + nullhdr2 + 4)) / (bs - 20::float)) AS otta
  FROM (
    SELECT ma, bs, schemaname, tablename,
           (datawidth + (hdr + ma -
              (CASE WHEN hdr % ma = 0 THEN ma ELSE hdr % ma END)))::numeric AS datahdr,
           (maxfracsum * (nullhdr + ma -
              (CASE WHEN nullhdr % ma = 0 THEN ma ELSE nullhdr % ma END))) AS nullhdr2
    FROM (
      SELECT schemaname, tablename, hdr, ma, bs,
             SUM((1 - null_frac) * avg_width) AS datawidth,
             MAX(null_frac) AS maxfracsum,
             hdr + (SELECT 1 + count(*) / 8 FROM pg_stats s2
                    WHERE null_frac <> 0 AND s2.schemaname = s.schemaname
                      AND s2.tablename = s.tablename) AS nullhdr
      FROM pg_stats s,
           (SELECT current_setting('block_size')::numeric AS bs, 23 AS hdr, 8 AS ma) AS constants
      WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
      GROUP BY 1, 2, 3, 4, 5
    ) AS foo
  ) AS rs
  JOIN pg_class cc ON cc.relname = rs.tablename
  JOIN pg_namespace nn ON cc.relnamespace = nn.oid
   AND nn.nspname = rs.schemaname
  WHERE cc.relkind = 'r' AND cc.relpages > 0
) AS sml
ORDER BY wasted_bytes DESC
LIMIT {limit};
"""