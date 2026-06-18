"""SQL the MySQL engine runs — the catalog/stat queries, kept apart from the
engine logic in mysql.py so each file stays readable (mirrors pg_sql.py).

These are query *text* only. Identifier-quoting helpers, DDL builders and the
non-SQL constants stay in mysql.py, next to the code that uses them.

MySQL has no schema-vs-database split: a "schema" is a database. So every query
here is scoped to a single database name (the connection's dbname), passed as a
bound parameter, and the engine reports that database name as the table's schema
so the rest of the app — which is written around (schema, table) — keeps working.
"""

# The Web equivalent of `SHOW TABLES`: base tables in one database plus an
# estimated row count. TABLE_ROWS is an estimate for InnoDB (like Postgres'
# n_live_tup — cheap, lags reality; an exact COUNT(*) per table would be slow).
LIST_TABLES_SQL = """
SELECT TABLE_SCHEMA, TABLE_NAME, COALESCE(TABLE_ROWS, 0) AS row_estimate
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME;
"""

# The Web equivalent of `DESCRIBE table`: column name, full type, nullability,
# default. COLUMN_TYPE carries the precise type ("varchar(255)", "int unsigned"),
# richer than DATA_TYPE alone.
LIST_COLUMNS_SQL = """
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION;
"""

# The Web equivalent of `SHOW DATABASES`: every database with its default
# charset and on-disk size (summed data + index length across its tables).
# Owner has no MySQL equivalent (databases aren't owned by a role), so it's blank.
# Size is returned as raw bytes and pretty-printed in Python (no pg_size_pretty).
LIST_DATABASES_SQL = """
SELECT s.SCHEMA_NAME AS name,
       s.DEFAULT_CHARACTER_SET_NAME AS encoding,
       (SELECT SUM(t.DATA_LENGTH + t.INDEX_LENGTH)
        FROM information_schema.TABLES t
        WHERE t.TABLE_SCHEMA = s.SCHEMA_NAME) AS size_bytes
FROM information_schema.SCHEMATA s
ORDER BY s.SCHEMA_NAME;
"""

# The Web equivalent of `SELECT User, Host FROM mysql.user`: login accounts.
# MySQL identifies an account as user@host; Super_priv is the closest analogue to
# a superuser flag (it predates 8.0's dynamic privileges but is still present),
# and account_locked says whether the account can currently log in.
LIST_ROLES_SQL = """
SELECT User, Host, Super_priv, account_locked
FROM mysql.user
ORDER BY User, Host;
"""

# The Web equivalent of `SHOW PROCESSLIST`: client sessions, what they're
# running, how long, and which session is our own (flagged is_self so the list is
# never mysteriously empty). MySQL has no separate "internal backends" to skip.
ACTIVITY_SQL = """
SELECT ID, USER, DB, HOST, COMMAND, STATE, TIME, INFO,
       (ID = CONNECTION_ID()) AS is_self
FROM information_schema.PROCESSLIST
ORDER BY (ID = CONNECTION_ID()) ASC, (COMMAND = 'Query') DESC, TIME DESC;
"""

# The Web equivalent of `SHOW INDEX FROM table`, aggregated to one row per index.
# information_schema.STATISTICS lists one row per indexed column; GROUP_CONCAT
# rebuilds the ordered column list. Per-index on-disk size isn't exposed here, so
# the engine reports it as unknown.
LIST_INDEXES_SQL = """
SELECT INDEX_NAME,
       MAX(INDEX_TYPE)   AS method,
       MAX(NON_UNIQUE)   AS non_unique,
       GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX SEPARATOR ', ') AS index_columns
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
GROUP BY INDEX_NAME
ORDER BY (INDEX_NAME = 'PRIMARY') DESC, INDEX_NAME;
"""

# Health — largest tables in one database by total on-disk size (data + index).
# DATA_LENGTH/INDEX_LENGTH are bytes; pretty-printing happens in Python.
TABLE_SIZES_SQL = """
SELECT TABLE_SCHEMA, TABLE_NAME,
       COALESCE(DATA_LENGTH, 0) + COALESCE(INDEX_LENGTH, 0) AS total_bytes,
       COALESCE(DATA_LENGTH, 0)  AS data_bytes,
       COALESCE(INDEX_LENGTH, 0) AS index_bytes
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
ORDER BY total_bytes DESC
LIMIT %s;
"""

# Lock-wait graph (MySQL 8.0+): who is stuck waiting on a row/table lock and who
# holds it. performance_schema.data_lock_waits pairs a *requesting* lock with the
# *blocking* lock; we map both back to their InnoDB transaction and, through
# trx_mysql_thread_id, to the processlist id — the same id `KILL` (cancel/kill)
# takes, so the panel's buttons line up with list_activity. One row per
# (blocked, blocker) pair; the engine groups them per blocked session.
# Returns no rows when nothing is blocked; the engine checks @@performance_schema
# separately so "disabled" is never silently reported as "nothing blocked".
BLOCKING_SQL = """
SELECT rt.trx_mysql_thread_id                          AS blocked_pid,
       rp.USER                                         AS blocked_user,
       COALESCE(rt.trx_query, '')                      AS blocked_query,
       TIMESTAMPDIFF(SECOND, rt.trx_wait_started, NOW()) AS wait_secs,
       rl.LOCK_TYPE                                     AS lock_type,
       rl.LOCK_MODE                                     AS lock_mode,
       CONCAT_WS('.', rl.OBJECT_SCHEMA, rl.OBJECT_NAME) AS object,
       bt.trx_mysql_thread_id                          AS blocker_pid,
       bp.USER                                         AS blocker_user,
       bp.COMMAND                                      AS blocker_command,
       COALESCE(bt.trx_query, '')                      AS blocker_query
FROM performance_schema.data_lock_waits w
JOIN performance_schema.data_locks rl ON w.REQUESTING_ENGINE_LOCK_ID = rl.ENGINE_LOCK_ID
JOIN performance_schema.data_locks bl ON w.BLOCKING_ENGINE_LOCK_ID  = bl.ENGINE_LOCK_ID
JOIN information_schema.INNODB_TRX rt ON w.REQUESTING_ENGINE_TRANSACTION_ID = rt.trx_id
JOIN information_schema.INNODB_TRX bt ON w.BLOCKING_ENGINE_TRANSACTION_ID  = bt.trx_id
LEFT JOIN information_schema.PROCESSLIST rp ON rp.ID = rt.trx_mysql_thread_id
LEFT JOIN information_schema.PROCESSLIST bp ON bp.ID = bt.trx_mysql_thread_id
ORDER BY blocked_pid, blocker_pid;
"""

# Indexes the server has never read since the last stats reset — drop candidates.
# sys.schema_unused_indexes is a view over performance_schema index-io summaries;
# it already excludes primary keys. Scoped to the connected database. MySQL does
# not expose a cheap per-index on-disk size, so the engine reports size unknown.
# Needs performance_schema ON (and the sys schema, default on 8.0); with it off
# the view is empty — a best-effort optimisation hint, not a safety signal.
UNUSED_INDEXES_SQL = """
SELECT object_schema, object_name, index_name
FROM sys.schema_unused_indexes
WHERE object_schema = %s
ORDER BY object_name, index_name;
"""
