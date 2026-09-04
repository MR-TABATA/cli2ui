"""SQL the PostgreSQL engine runs — the catalog/stat queries, kept apart from
the engine logic in postgres.py so each file stays readable.

These are query *text* only. Identifier-quoting helpers, DDL builders and the
non-SQL constants (COLUMN_TYPES, INDEX_METHODS, DUMP_FORMATS, …) stay in
postgres.py, next to the code that uses them.
"""

# The Web equivalent of `\dt`: every user table plus an estimated row count
# (pg_stat lags reality but is free; an exact COUNT(*) per table would be slow).
# 行数は **`reltuples`（プランナ統計）** から取る。
#
# 以前は `pg_stat_user_tables.n_live_tup` を見ていたが、あれは *このサーバが
# 起動してから観測した書き込みの累積*で、**ダンプから復元しただけのテーブルでは
# 0 のまま**になる。実際 Airlines のデモ（`pg_restore` で入れたもの）では
# 236 万行の `ticket_flights` が「0」と表示されていた。
#
# `-1` は「まだ ANALYZE されていない」を表す PostgreSQL の値で、0 行とは違う。
# NULL にして返し、画面には「不明」と出す ── **0 と書くと「空だから消していい」に
# 読める**（TRUNCATE / DROP の確認では既にそう扱っている。同じ規則をここにも）。
LIST_TABLES_SQL = """
SELECT n.nspname AS schemaname,
       c.relname AS tablename,
       CASE WHEN c.reltuples < 0 THEN NULL
            ELSE c.reltuples::bigint END AS rows
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, c.relname;
"""

# The table's own COMMENT — the description psql prints at the foot of
# `\d+ table`. Looked up by name through pg_class/pg_namespace so it's pure `%s`
# binds (no format() `%%` collision like LIST_COLUMNS_SQL). Zero rows if the
# table is gone; a NULL comment column if it simply has no comment.
TABLE_COMMENT_SQL = """
SELECT obj_description(c.oid, 'pg_class') AS comment
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s;
"""

# The Web equivalent of `\d table`: column name, type, nullability, default,
# the column COMMENT, and whether it's a generated column. col_description and
# attgenerated both need the table oid + attnum, so we join pg_attribute (by
# name, skipping dropped columns so attnum stays correct); pg_attrdef carries the
# generation expression. attgenerated is '' for a plain column, 's' for STORED
# and 'v' for VIRTUAL (PostgreSQL 18+) — without it a generated column reads as a
# plain column with a NULL default. The CASE keeps pg_get_expr off ordinary
# columns (whose adbin is a default literal, not a generation expression).
# NOTE: the `%%I` are doubled on purpose — psycopg2 reads a lone `%` as a
# parameter marker and would collide with the `%s` binds below. `%%` emits a
# literal `%` for format()'s own placeholders. Do not "simplify" to `%I`.
LIST_COLUMNS_SQL = """
SELECT c.column_name, c.data_type, c.is_nullable, c.column_default,
       col_description(a.attrelid, a.attnum) AS comment,
       a.attgenerated AS generated,
       CASE WHEN a.attgenerated <> '' THEN pg_get_expr(ad.adbin, ad.adrelid) END AS gen_expr
FROM information_schema.columns c
LEFT JOIN pg_attribute a
       ON a.attrelid = format('%%I.%%I', c.table_schema, c.table_name)::regclass
      AND a.attname  = c.column_name
      AND NOT a.attisdropped
LEFT JOIN pg_attrdef ad
       ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
WHERE c.table_schema = %s AND c.table_name = %s
ORDER BY c.ordinal_position;
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

# The Web equivalent of `\dx`: extensions installed in this database, unioned
# with the ones the server has packaged and could install. pg_available_extensions
# already carries installed_version (NULL when not installed) and a one-line
# comment; the LEFT JOIN to pg_extension/pg_namespace adds the schema an installed
# one lives in. Installed extensions sort first (installed_version IS NULL → last).
EXTENSIONS_SQL = """
SELECT ae.name,
       ae.installed_version,
       ae.default_version,
       n.nspname AS schema,
       ae.comment
FROM pg_available_extensions ae
LEFT JOIN pg_extension e ON e.extname = ae.name
LEFT JOIN pg_namespace n ON n.oid = e.extnamespace
ORDER BY (ae.installed_version IS NULL), ae.name;
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

# Connection headroom: how many connections are in use against max_connections,
# the number behind "FATAL: too many connections". Counts client backends only
# (background workers/autovacuum don't draw on the user-facing pool the way a
# client does) and breaks them down by state for the panel. superuser_reserved
# slots are kept back from non-superusers, so they're surfaced alongside.
HEADROOM_SQL = """
SELECT count(*) FILTER (WHERE backend_type = 'client backend')                              AS used,
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'active')         AS active,
       count(*) FILTER (WHERE backend_type = 'client backend' AND state = 'idle')           AS idle,
       count(*) FILTER (WHERE backend_type = 'client backend'
                        AND state = 'idle in transaction')                                  AS idle_in_txn,
       current_setting('max_connections')::int                                              AS max_conn,
       current_setting('superuser_reserved_connections')::int                               AS reserved
FROM pg_stat_activity;
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
# trails what the primary has shipped it), plus write/flush/replay_lag as time.
# replay_lag is the one that matters for read-your-writes: how long a commit
# here takes to become visible on the standby — i.e. how long WAIT FOR LSN
# would block a read there. The lag columns are NULL until Postgres has a
# round-trip sample (an idle or just-attached standby), so they can be absent.
STANDBYS_SQL = """
SELECT pid, usename, application_name, client_addr::text, state, sync_state,
       sent_lsn::text, replay_lsn::text,
       pg_wal_lsn_diff(sent_lsn, replay_lsn)::bigint AS lag_bytes,
       EXTRACT(EPOCH FROM write_lag)::float8  AS write_lag_s,
       EXTRACT(EPOCH FROM flush_lag)::float8  AS flush_lag_s,
       EXTRACT(EPOCH FROM replay_lag)::float8 AS replay_lag_s
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

# Foreign-key edges: every FK as (constraint, child schema.table, parent
# schema.table, child columns). Read straight from pg_constraint (contype='f')
# so composite keys are one row; the child columns come from conkey joined back
# to pg_attribute, ordered as declared. User schemas only. Feeds the dependency
# graph (safe TRUNCATE order + cycle detection).
FK_EDGES_SQL = """
SELECT con.conname,
       ns.nspname  || '.' || cl.relname  AS child,
       fns.nspname || '.' || fcl.relname AS parent,
       (SELECT string_agg(att.attname, ', ' ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = u.attnum) AS columns
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cl      ON cl.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
JOIN pg_catalog.pg_class fcl     ON fcl.oid = con.confrelid
JOIN pg_catalog.pg_namespace fns ON fns.oid = fcl.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname  NOT IN ('pg_catalog', 'information_schema')
  AND fns.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY child, con.conname;
"""

# Health — foreign keys with no supporting index on the referencing side.
# Postgres does not auto-create one (unlike MySQL/InnoDB), so FK checks / cascade
# deletes / joins to the parent seq-scan. Flagged when the FK columns (con.conkey,
# in order) are not the leading key columns of ANY index on the child table. The
# leading slice is taken from indkey (an int2vector) via its text form to dodge
# 0-based vector indexing; `@>` over equal-length arrays = set equality, and the
# indnkeyatts guard keeps INCLUDE (non-key) columns out of the comparison.
# Only an index the planner can use for the FK's own lookup counts. A partial
# index (indpred) never qualifies: the referential-integrity probe that fires on
# a parent delete/update looks the child rows up by key alone, carrying no
# predicate, so the planner can never prove the index applies — the common
# soft-delete index `(fk_col) WHERE deleted_at IS NULL` leaves the FK
# seq-scanning. An index left invalid by a failed CREATE INDEX CONCURRENTLY is
# ignored by the planner outright. Counting either would report an FK as indexed
# while it still seq-scans.
FK_MISSING_INDEX_SQL = """
SELECT ns.nspname AS schema,
       cl.relname AS table,
       con.conname AS constraint,
       (SELECT string_agg(att.attname, ', ' ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = u.attnum) AS columns,
       fns.nspname || '.' || fcl.relname AS refs
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cl      ON cl.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
JOIN pg_catalog.pg_class fcl     ON fcl.oid = con.confrelid
JOIN pg_catalog.pg_namespace fns ON fns.oid = fcl.relnamespace
WHERE con.contype = 'f'
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
  AND NOT EXISTS (
    SELECT 1 FROM pg_catalog.pg_index idx
    WHERE idx.indrelid = con.conrelid
      AND idx.indpred IS NULL
      AND idx.indisvalid
      AND cardinality(con.conkey) <= idx.indnkeyatts
      AND (string_to_array(idx.indkey::text, ' ')::int2[])[1:cardinality(con.conkey)]
          @> con.conkey
  )
ORDER BY 1, 2, 3;
"""

# 押す前に見せる — TRUNCATE / DROP の影響量。
#
# 行数は `count(*)` ではなく統計（reltuples）から出す。10 億行のテーブルで確認
# ダイアログを開くたびに全表走査するわけにはいかない。**代わりに推定だと明示する** ──
# `-1` は「まだ ANALYZE されていない」で、0 行とはまったく違う（PG14 以降。それ以前は
# 0 が同じ意味を持ってしまうので、last_analyze が無いことと併せて判定する）。
#
# 参照しているテーブルも返す。TRUNCATE は FK で参照されていると CASCADE 無しでは失敗し、
# DROP TABLE も依存があると失敗する ── 押してから知るのでは遅い種類の事実。
WRITE_IMPACT_SQL = """
SELECT cl.reltuples::bigint AS rows,
       st.last_analyze,
       st.last_autoanalyze,
       (SELECT coalesce(json_agg(json_build_object(
                   'table', child.qualified,
                   'constraint', child.conname,
                   'rows', child.rows) ORDER BY child.qualified), '[]'::json)
          FROM (
            SELECT cns.nspname || '.' || ccl.relname AS qualified,
                   con.conname,
                   ccl.reltuples::bigint AS rows
            FROM pg_catalog.pg_constraint con
            JOIN pg_catalog.pg_class ccl     ON ccl.oid = con.conrelid
            JOIN pg_catalog.pg_namespace cns ON cns.oid = ccl.relnamespace
            WHERE con.contype = 'f' AND con.confrelid = cl.oid
          ) child) AS referenced_by
FROM pg_catalog.pg_class cl
JOIN pg_catalog.pg_namespace ns ON ns.oid = cl.relnamespace
LEFT JOIN pg_catalog.pg_stat_user_tables st ON st.relid = cl.oid
WHERE ns.nspname = %s AND cl.relname = %s;
"""

# 押す前に見せる — DROP の巻き添え。
#
# `DROP TABLE` は既定で RESTRICT ＝ 依存しているものがあれば失敗する。CASCADE を
# 付けた瞬間に**それらも一緒に消える**ので、「何が付いてくるか」は押す前に見せる
# べき筆頭。pg_depend の deptype = 'n'（normal）が、CASCADE で連れて行かれる側。
#
# 索引・制約・型の暗黙依存（'a' auto / 'i' internal）は除く。テーブルと一緒に消えて
# 当たり前のもので、並べると本当に見たいもの（ビュー・関数・FK）が埋もれる。
# ただし FK は別枠で数えているので（WRITE_IMPACT_SQL の referenced_by）ここでは
# ビューとルールに絞る ── 同じものを 2 か所で数えると、合計が二重になる。
DROP_FALLOUT_SQL = """
SELECT DISTINCT
       CASE dependent.relkind
         WHEN 'v' THEN 'view'
         WHEN 'm' THEN 'materialized view'
         ELSE dependent.relkind::text
       END AS kind,
       dns.nspname || '.' || dependent.relname AS name
FROM pg_catalog.pg_class target
JOIN pg_catalog.pg_namespace ns   ON ns.oid = target.relnamespace
JOIN pg_catalog.pg_depend dep     ON dep.refobjid = target.oid
                                 AND dep.refclassid = 'pg_class'::regclass
                                 AND dep.deptype = 'n'
JOIN pg_catalog.pg_rewrite rw     ON rw.oid = dep.objid
JOIN pg_catalog.pg_class dependent ON dependent.oid = rw.ev_class
JOIN pg_catalog.pg_namespace dns  ON dns.oid = dependent.relnamespace
WHERE ns.nspname = %s AND target.relname = %s
  AND dependent.oid <> target.oid
  AND dependent.relkind IN ('v', 'm')
ORDER BY 1, 2;
"""

# Health — composite UNIQUE that NULL slips through.
#
# `UNIQUE (email, tenant_id)` は、tenant_id が NULL の行同士を**別物**として扱う。
# NULL 同士は等しくないので、同じ email が何行でも入る。宣言は効いているのに、
# 効いていない範囲がある ── 気づくのはたいてい重複が出たあと。
#
# 検出は 2 条件だけ: 複合（キー列が 2 本以上）で、キー列に NULL 許容が混ざること。
# PostgreSQL 15 の `NULLS NOT DISTINCT` で作った索引はすり抜けないので除く。
# **その列は 15 で増えたため、14 以前では SQL に書くと parse で落ちる。**
# だから 2 本用意して、サーバ版で選ぶ（14 以前は常に NULLS DISTINCT ＝ 全部対象）。
#
# 部分索引（indpred）は除く。WHERE 付きの一意制約は「その条件の行だけ」の宣言で、
# 範囲外の重複はすり抜けではなく仕様。式索引（indexprs）も列に還元できないので除く。
_NULL_SLIP_BASE = """
SELECT ns.nspname AS schema,
       tbl.relname AS table,
       idx.relname AS index,
       con.conname AS constraint,
       (SELECT string_agg(att.attname, ', ' ORDER BY k.ord)
          FROM unnest(ix.indkey[0:ix.indnkeyatts-1]) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = ix.indrelid AND att.attnum = k.attnum) AS columns,
       (SELECT string_agg(att.attname, ', ' ORDER BY k.ord)
          FROM unnest(ix.indkey[0:ix.indnkeyatts-1]) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = ix.indrelid AND att.attnum = k.attnum
         WHERE NOT att.attnotnull) AS nullable
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class idx     ON idx.oid = ix.indexrelid
JOIN pg_catalog.pg_class tbl     ON tbl.oid = ix.indrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = tbl.relnamespace
LEFT JOIN pg_catalog.pg_constraint con
       ON con.conindid = ix.indexrelid AND con.contype IN ('u', 'p')
WHERE ix.indisunique
  AND ix.indisvalid
  AND ix.indnkeyatts > 1
  AND ix.indpred IS NULL
  AND ix.indexprs IS NULL
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
  {extra}
  AND EXISTS (
    SELECT 1 FROM unnest(ix.indkey[0:ix.indnkeyatts-1]) AS k(attnum)
    JOIN pg_catalog.pg_attribute att
      ON att.attrelid = ix.indrelid AND att.attnum = k.attnum
    WHERE NOT att.attnotnull
  )
ORDER BY 1, 2, 3;
"""

# 15 以降: NULLS NOT DISTINCT で作られた索引はすり抜けないので落とす。
NULL_SLIP_SQL_PG15 = _NULL_SLIP_BASE.format(extra="AND NOT ix.indnullsnotdistinct")
# 14 以前: その概念自体が無い ＝ 一意索引はすべて NULLS DISTINCT。
NULL_SLIP_SQL_LEGACY = _NULL_SLIP_BASE.format(extra="")

# 数えるとき、列は要求ではなくカタログから引き直す（要求が名指しするのは索引だけ）。
# 複合・一意・部分索引でない、という条件をここでもう一度確かめる ── パネルを開いてから
# 索引が作り替えられていたら、数える対象が別物になっているため。
NULL_SLIP_RESOLVE_SQL = """
SELECT (SELECT array_agg(att.attname ORDER BY k.ord)
          FROM unnest(ix.indkey[0:ix.indnkeyatts-1]) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = ix.indrelid AND att.attnum = k.attnum) AS columns
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class idx     ON idx.oid = ix.indexrelid
JOIN pg_catalog.pg_class tbl     ON tbl.oid = ix.indrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = tbl.relnamespace
WHERE ns.nspname = %s AND tbl.relname = %s AND idx.relname = %s
  AND ix.indisunique AND ix.indisvalid AND ix.indnkeyatts > 1
  AND ix.indpred IS NULL AND ix.indexprs IS NULL;
"""

# Health — invalid indexes: what a failed CREATE INDEX CONCURRENTLY leaves behind.
# The planner ignores such an index outright, so it costs disk and write time
# while answering nothing. The badge already exists on the table detail; a DB is
# where you actually notice them, because nobody opens 200 tables one by one.
#
# **A build in progress looks exactly the same in pg_index** (indisvalid = false
# until it finishes). Reporting a running CIC as wreckage would send someone to
# drop an index that is about to become useful, so the state is read from
# pg_stat_progress_create_index (PG 12+) and reported as its own column rather
# than filtered out — "I cannot tell yet" is a different answer from "it failed".
#
# indislive = false is the other half of the family: a failed *concurrent drop*.
# The index is already unusable for queries but still maintained on write, which
# is the worst of both, so it is called out separately.
INVALID_INDEXES_SQL = """
SELECT ns.nspname AS schema,
       tbl.relname AS table,
       idx.relname AS index,
       ix.indisready AS ready,
       ix.indislive AS live,
       (prog.pid IS NOT NULL) AS building,
       pg_relation_size(idx.oid) AS bytes,
       pg_size_pretty(pg_relation_size(idx.oid)) AS size
FROM pg_catalog.pg_index ix
JOIN pg_catalog.pg_class idx     ON idx.oid = ix.indexrelid
JOIN pg_catalog.pg_class tbl     ON tbl.oid = ix.indrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = idx.relnamespace
LEFT JOIN pg_catalog.pg_stat_progress_create_index prog ON prog.index_relid = idx.oid
WHERE NOT ix.indisvalid
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_relation_size(idx.oid) DESC, 1, 2, 3;
"""

# Health — redundant indexes: a non-unique index whose leading key columns are a
# prefix of (or identical to) another index on the same table + access method.
# Partial (indpred) and expression (indexprs) indexes are excluded — their keycol
# list would misrepresent them. keycols is the leading key columns as attnums
# (indkey text form, sliced to indnkeyatts); the prefix test is exact and ordered.
# A unique index is never reported as the redundant one (it enforces uniqueness);
# for an exact-duplicate pair the indexrelid tie-break lists it once.
DUPLICATE_INDEX_SQL = """
WITH idx AS (
  SELECT i.indexrelid, i.indrelid, i.indisunique,
         n.nspname AS sch, c.relname AS tbl, ic.relname AS name,
         am.amname AS method,
         (string_to_array(i.indkey::text, ' ')::int2[])[1:i.indnkeyatts] AS keycols,
         (SELECT string_agg(att.attname, ', ' ORDER BY k.ord)
            FROM unnest((string_to_array(i.indkey::text, ' ')::int2[])[1:i.indnkeyatts])
                 WITH ORDINALITY AS k(attnum, ord)
            JOIN pg_catalog.pg_attribute att
              ON att.attrelid = i.indrelid AND att.attnum = k.attnum) AS colnames,
         pg_relation_size(i.indexrelid) AS bytes
  FROM pg_catalog.pg_index i
  JOIN pg_catalog.pg_class c   ON c.oid = i.indrelid
  JOIN pg_catalog.pg_class ic  ON ic.oid = i.indexrelid
  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_catalog.pg_am am      ON am.oid = ic.relam
  WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    AND i.indpred IS NULL
    AND i.indexprs IS NULL
)
SELECT a.sch, a.tbl,
       a.name AS redundant, a.colnames AS cols,
       b.name AS covered_by, b.colnames AS covered_by_cols,
       (cardinality(a.keycols) = cardinality(b.keycols)) AS identical,
       pg_size_pretty(a.bytes) AS size
FROM idx a
JOIN idx b
  ON a.indrelid = b.indrelid
 AND a.indexrelid <> b.indexrelid
 AND a.method = b.method
 AND NOT a.indisunique
 AND b.keycols[1:cardinality(a.keycols)] = a.keycols
 AND (cardinality(a.keycols) < cardinality(b.keycols)
      OR a.indexrelid < b.indexrelid)
ORDER BY 1, 2, 3;
"""

# Integrity — foreign keys added NOT VALID and never validated. `convalidated`
# is false only for such constraints (a plain FK is validated on creation), so
# these are exactly the FKs whose pre-existing rows were never checked against
# the parent: the one place a *declared* FK can still hide orphan rows. Child and
# parent column lists come from conkey/confkey in ordinal order.
NOT_VALID_FK_SQL = """
SELECT con.conname,
       ns.nspname AS schema,
       cl.relname AS table,
       (SELECT string_agg(att.attname, ', ' ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = u.attnum) AS child_cols,
       fns.nspname || '.' || fcl.relname AS parent,
       (SELECT string_agg(att.attname, ', ' ORDER BY u.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = u.attnum) AS parent_cols
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cl      ON cl.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
JOIN pg_catalog.pg_class fcl     ON fcl.oid = con.confrelid
JOIN pg_catalog.pg_namespace fns ON fns.oid = fcl.relnamespace
WHERE con.contype = 'f'
  AND NOT con.convalidated
  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY 2, 3, 1;
"""

# Integrity — columns named `<base>_id` with NO foreign key, whose values look
# like they should reference a table. A naming-based guess, kept deliberately
# conservative so it never becomes design advice:
#   • the column is `%_id` (a bare `id` — the table's own PK convention — can't
#     match, it has no leading `<base>`);
#   • it is not already part of any FK (those are declared, not inferred);
#   • a table named exactly `<base>` or `<base>s` exists with a single-column
#     primary key of the *same* type (atttypid);
#   • that match is unique (HAVING count(*) = 1) — if `<base>` and `<base>s` both
#     qualify, or two schemas do, we don't guess.
# `left(col, -3)` drops the trailing `_id`. min() over a one-row group returns
# that single parent. The parent name/schema/column are what an orphan count
# re-derives; nothing here scans table data.
INFERRED_FK_SQL = """
WITH child_col AS (
  SELECT ns.nspname AS sch, cl.relname AS tbl, att.attname AS col,
         att.atttypid AS typ
  FROM pg_catalog.pg_attribute att
  JOIN pg_catalog.pg_class cl      ON cl.oid = att.attrelid AND cl.relkind = 'r'
  JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
  WHERE att.attnum > 0 AND NOT att.attisdropped
    AND att.attname LIKE '%\\_id'
    AND ns.nspname NOT IN ('pg_catalog', 'information_schema')
    AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_constraint c
      WHERE c.conrelid = cl.oid AND c.contype = 'f'
        AND att.attnum = ANY (c.conkey))
),
pk AS (
  SELECT n.nspname AS sch, cl.relname AS tbl, a.attname AS col, a.atttypid AS typ
  FROM pg_catalog.pg_constraint c
  JOIN pg_catalog.pg_class cl      ON cl.oid = c.conrelid AND cl.relkind = 'r'
  JOIN pg_catalog.pg_namespace n   ON n.oid = cl.relnamespace
  JOIN pg_catalog.pg_attribute a   ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
  WHERE c.contype = 'p' AND cardinality(c.conkey) = 1
)
SELECT cc.sch, cc.tbl, cc.col,
       min(pk.sch || '.' || pk.tbl) AS parent,
       min(pk.col) AS parent_col
FROM child_col cc
JOIN pk ON pk.typ = cc.typ
       AND pk.tbl IN (left(cc.col, -3), left(cc.col, -3) || 's')
GROUP BY cc.sch, cc.tbl, cc.col
HAVING count(*) = 1
ORDER BY 1, 2, 3;
"""

# Re-derive one NOT VALID FK's parent + column lists by name (schema, table,
# conname). Returns parent schema/table and the ordered child/parent column
# arrays the orphan anti-join composes. Parametrised — never trusts a caller's
# idea of the parent.
FK_RESOLVE_SQL = """
SELECT fns.nspname AS parent_schema,
       fcl.relname AS parent_table,
       (SELECT array_agg(att.attname ORDER BY u.ord)
          FROM unnest(con.conkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.conrelid AND att.attnum = u.attnum) AS child_cols,
       (SELECT array_agg(att.attname ORDER BY u.ord)
          FROM unnest(con.confkey) WITH ORDINALITY AS u(attnum, ord)
          JOIN pg_catalog.pg_attribute att
            ON att.attrelid = con.confrelid AND att.attnum = u.attnum) AS parent_cols
FROM pg_catalog.pg_constraint con
JOIN pg_catalog.pg_class cl      ON cl.oid = con.conrelid
JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
JOIN pg_catalog.pg_class fcl     ON fcl.oid = con.confrelid
JOIN pg_catalog.pg_namespace fns ON fns.oid = fcl.relnamespace
WHERE con.contype = 'f' AND NOT con.convalidated
  AND ns.nspname = %s AND cl.relname = %s AND con.conname = %s;
"""

# Re-derive one inferred relationship by (schema, table, column), applying the
# same unique-match rule as INFERRED_FK_SQL. Returns the parent schema, table and
# PK column; no row when the column isn't an unambiguous inferred reference.
INFERRED_RESOLVE_SQL = """
WITH child_col AS (
  SELECT att.atttypid AS typ, att.attname AS col
  FROM pg_catalog.pg_attribute att
  JOIN pg_catalog.pg_class cl      ON cl.oid = att.attrelid AND cl.relkind = 'r'
  JOIN pg_catalog.pg_namespace ns  ON ns.oid = cl.relnamespace
  WHERE ns.nspname = %s AND cl.relname = %s AND att.attname = %s
    AND att.attnum > 0 AND NOT att.attisdropped
    AND att.attname LIKE '%%\\_id'
    AND NOT EXISTS (
      SELECT 1 FROM pg_catalog.pg_constraint c
      WHERE c.conrelid = cl.oid AND c.contype = 'f'
        AND att.attnum = ANY (c.conkey))
),
pk AS (
  SELECT n.nspname AS sch, cl.relname AS tbl, a.attname AS col, a.atttypid AS typ
  FROM pg_catalog.pg_constraint c
  JOIN pg_catalog.pg_class cl      ON cl.oid = c.conrelid AND cl.relkind = 'r'
  JOIN pg_catalog.pg_namespace n   ON n.oid = cl.relnamespace
  JOIN pg_catalog.pg_attribute a   ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
  WHERE c.contype = 'p' AND cardinality(c.conkey) = 1
)
SELECT min(pk.sch) AS parent_schema, min(pk.tbl) AS parent_table,
       min(pk.col) AS parent_col
FROM child_col cc
JOIN pk ON pk.typ = cc.typ
       AND pk.tbl IN (left(cc.col, -3), left(cc.col, -3) || 's')
HAVING count(*) = 1;
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

# 主キーの列を定義順で。ページ送りの並び順に使う ── 順序を指定しないと
# PostgreSQL は行の順序を約束せず、ページをめくると重複や欠落が起きる。
PRIMARY_KEY_COLUMNS_SQL = """
SELECT a.attname
FROM pg_catalog.pg_index i
JOIN pg_catalog.pg_class c      ON c.oid = i.indrelid
JOIN pg_catalog.pg_namespace n  ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a  ON a.attrelid = c.oid
                               AND a.attnum = ANY(i.indkey)
WHERE n.nspname = %s AND c.relname = %s AND i.indisprimary
ORDER BY array_position(i.indkey, a.attnum);
"""

# 1 テーブルの推定行数。count(*) は全走査になるので使わない。
# -1 は「まだ ANALYZE していない」で、0 行とは違うので NULL にして返す。
ESTIMATED_ROWS_SQL = """
SELECT CASE WHEN c.reltuples < 0 THEN NULL ELSE c.reltuples::bigint END
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = %s AND c.relname = %s;
"""
