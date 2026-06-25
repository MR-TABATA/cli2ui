"""MySQL engine — PyMySQL under the hood.

Phase 1 scope: browsing, ad-hoc queries (read-only enforced), the filter builder,
CSV import, streamed exports, EXPLAIN, the session/process list, table + column
DDL, indexes, databases and table sizes. Phase 2 adds the lock-wait graph
(list_blocking) and unused-index detection. Capabilities with no MySQL equivalent
(replication, server-config editor, role mutations, the planner what-if lab,
mysqldump backups, schema objects, bloat/vacuum health) raise a clear EngineError
or are declared UNSUPPORTED so the UI shows a "not applicable here" state.

Two failure shapes are kept distinct on purpose (see base.Engine.supports):
a feature that is *conceptually absent* (vacuum/bloat — InnoDB has no dead-tuple
model) returns empty and is flagged UNSUPPORTED, while one that *could* report a
problem but can't right now (lock waits when performance_schema is OFF) raises
EngineError. A safety signal like "is anything blocked?" must never degrade to a
false "nothing blocked".

MySQL has no schema-vs-database split: a schema *is* a database. The rest of the
app is written around (schema, table); the engine bridges that by reporting the
connection's database name as each table's schema and scoping catalog queries to
that one database. See mysql_sql.py.
"""
import contextlib
import csv
import io
import json
import os
import re
import subprocess  # nosec B404 — used only to run mysqldump/mysql with a fixed argv, no shell
import threading
import time
from dataclasses import dataclass
from datetime import datetime

import pymysql
from django.utils.translation import gettext as _

from .base import (
    Activity,
    Blocker,
    Column,
    ConnectionHeadroom,
    Database,
    Dump,
    Engine,
    EngineError,
    Index,
    LockWait,
    Preview,
    QueryResult,
    PlanNode,
    Role,
    Setting,
    Table,
    TableSize,
    UnusedIndex,
)
from .mysql_sql import (
    ACTIVITY_SQL,
    BLOCKING_SQL,
    LIST_COLUMNS_SQL,
    LIST_DATABASES_SQL,
    LIST_INDEXES_SQL,
    LIST_ROLES_SQL,
    LIST_TABLES_SQL,
    TABLE_COMMENT_SQL,
    TABLE_SIZES_SQL,
    UNUSED_INDEXES_SQL,
)

# Index access methods MySQL accepts in CREATE INDEX … USING. A fixed allow-list
# because the chosen method is spliced into the DDL as a bare keyword (it can't be
# bound), so it must never come straight from user input. FULLTEXT/SPATIAL use a
# different syntax (not USING), so they're left out of the plain-index path.
INDEX_METHODS = ("btree", "hash")

# Column types offered for ADD COLUMN / change type. Like INDEX_METHODS, the type
# is spliced as a bare keyword, so it must match this allow-list exactly — never
# raw user input. Parameterised types (varchar(n), decimal(p,s)) are left out to
# keep the splice injection-proof; text/int/decimal cover those needs.
COLUMN_TYPES = (
    "text", "int", "bigint", "smallint", "tinyint", "decimal", "double",
    "float", "date", "datetime", "timestamp", "time", "char", "varchar(255)",
    "json", "blob", "boolean",
)

# Filter-builder operators: stable key (from the UI <select>) → (SQL operator,
# takes a value?, optional LIKE wrapper). Column names go through _ident (backtick
# quoting) and values are bound as %s placeholders — nothing is interpolated.
# MySQL's default collations are case-insensitive, so plain LIKE matches ILIKE's
# behaviour on Postgres.
FILTER_OPS = {
    "eq":       ("=",            True,  None),
    "ne":       ("<>",           True,  None),
    "lt":       ("<",            True,  None),
    "le":       ("<=",           True,  None),
    "gt":       (">",            True,  None),
    "ge":       (">=",           True,  None),
    "contains": ("LIKE",         True,  "%{}%"),
    "starts":   ("LIKE",         True,  "{}%"),
    "null":     ("IS NULL",      False, None),
    "notnull":  ("IS NOT NULL",  False, None),
}

# Statements like CREATE/DROP/ALTER/TRUNCATE implicitly commit in MySQL and can't
# run in a rolled-back transaction, so the what-if lab can't be supported here.
_WHATIF_UNSUPPORTED = (
    "The planner what-if lab isn't available for MySQL — it relies on rolling "
    "back catalog edits, which MySQL DDL can't do (it commits implicitly).")

# How long a single mysqldump / restore may run before we give up (seconds).
MYSQLDUMP_TIMEOUT = 120
RESTORE_TIMEOUT = 300

# The server variables people actually reach for — connections, InnoDB memory,
# the binlog, logging — shown by default so the editor isn't a wall of ~600 vars.
MYSQL_COMMON_SETTINGS = [
    "max_connections", "max_allowed_packet", "wait_timeout",
    "innodb_buffer_pool_size", "innodb_log_file_size", "innodb_flush_log_at_trx_commit",
    "innodb_flush_method", "innodb_io_capacity", "innodb_lock_wait_timeout",
    "sync_binlog", "binlog_expire_logs_seconds", "slow_query_log",
    "long_query_time", "general_log", "log_output", "sql_mode",
    "character_set_server", "collation_server", "time_zone", "transaction_isolation",
]

# A real MySQL system-variable name is always [A-Za-z0-9_]; we whitelist against
# this before splicing a (catalog-verified) name into SET PERSIST, where it can't
# be bound as a parameter.
_VAR_NAME_RE = re.compile(r"\A[A-Za-z0-9_]+\Z")

# A numeric system variable (e.g. long_query_time) rejects a *quoted* value with
# "Incorrect argument type", so a digits-only value is spliced unquoted (safe —
# no SQL metacharacters can appear); everything else is bound as a parameter.
_NUMERIC_RE = re.compile(r"\A-?\d+(\.\d+)?\Z")


# MySQL replication is binlog/GTID-based, not WAL/slots, so it has its own shapes
# (rendered by partials/replication_mysql.html) rather than the Postgres ones.
@dataclass
class MysqlReplStatus:
    """The server's replication posture: am I a source, a replica, or neither;
    is binary logging on; where is the binlog; and (as a replica) are my threads
    running and how far behind am I?"""

    role: str                    # "source" | "replica" | "standalone"
    log_bin: bool
    server_id: int
    gtid_mode: str               # ON | OFF | ON_PERMISSIVE | …
    binlog_file: str | None
    binlog_pos: int | None
    source_host: str | None      # replica only
    io_running: bool | None      # replica only
    sql_running: bool | None     # replica only
    seconds_behind: int | None   # replica only

    @property
    def ready(self) -> bool:
        """Configured to act as a replication source: binary logging on and a
        non-zero server_id (every server in a topology needs a unique id)."""
        return self.log_bin and self.server_id != 0

    @property
    def healthy(self) -> bool:
        """Both replica threads running (only meaningful when role == replica)."""
        return bool(self.io_running and self.sql_running)


@dataclass
class MysqlReplica:
    """One connected replica, from SHOW REPLICAS on the source."""

    server_id: int
    host: str | None
    port: int | None


@dataclass
class MysqlReplRecipe:
    """A copy-paste walkthrough for attaching a replica to this server, with the
    current connection values filled in. Pure string assembly — nothing is run."""

    source_host: str
    source_port: int
    repl_user: str
    # (param, recommended value) the source still needs; empty when already ready.
    conf_changes: list
    create_user_sql: str         # run on the source
    change_source_sql: str       # run on the replica
    start_replica_sql: str       # run on the replica

    @property
    def ready(self) -> bool:
        return not self.conf_changes


def _ident(name: str) -> str:
    """Backtick-quote an identifier, doubling any embedded backtick — MySQL's
    rule for a quoted identifier. The one place identifiers reach SQL as text."""
    return "`" + str(name).replace("`", "``") + "`"


class MysqlEngine(Engine):
    # Capabilities this engine doesn't have. Health features with no InnoDB
    # equivalent (no vacuum / dead-tuple model → "vacuum"/"bloat"), no schema
    # distinct from a database ("schemas"), no replication-slot concept
    # ("replication_slots"), and no CREATE DATABASE … TEMPLATE ("db_template").
    # Panels read this to show "not applicable to MySQL" rather than an empty
    # card, and callers gate engine-specific behaviour on it. Lock waits are NOT
    # here — they're implemented (list_blocking) and raise when they can't be
    # determined, never silently report "nothing".
    UNSUPPORTED = frozenset({"vacuum", "bloat", "schemas",
                             "replication_slots", "db_template"})

    # When inside session(), the one open connection reused for every probe;
    # otherwise None and each _connect() dials its own. Mirrors PostgresEngine.
    _held = None

    @contextlib.contextmanager
    def _connect(self, dbname=None):
        # Inside session() reuse the single held connection (the overview fires
        # ~10 read-only probes and would otherwise connect ~10 times). A call to
        # a *different* dbname always opens its own.
        if dbname is None and self._held is not None:
            yield self._held
            return
        c = self.connection
        try:
            conn = pymysql.connect(
                host=c.host,
                port=c.port,
                user=c.user,
                password=c.password,
                database=dbname or c.dbname,
                connect_timeout=5,
                charset="utf8mb4",
                autocommit=True,
            )
        except pymysql.Error as exc:
            raise EngineError(_clean(exc)) from exc
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def session(self):
        """Open one connection and reuse it for every read-only probe in the
        block (see PostgresEngine.session). Each probe runs on its own autocommit
        statement. Do not call methods that open an explicit transaction
        (run_query / explain / stream_*) inside a session."""
        with self._connect() as conn:
            self._held = conn
            try:
                yield self
            finally:
                self._held = None

    def whatif_cursor(self, *, timeout_ms: int = 15000, lock_timeout: str = "2s"):
        raise EngineError(_(_WHATIF_UNSUPPORTED))

    def test(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    # --- browsing --------------------------------------------------------

    def list_tables(self) -> list[Table]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_TABLES_SQL, (self.connection.dbname,))
                return [
                    Table(schema=row[0], name=row[1], rows=int(row[2] or 0))
                    for row in cur.fetchall()
                ]

    def list_columns(self, schema: str, table: str) -> list[Column]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_COLUMNS_SQL, (schema, table))
                return [
                    Column(
                        name=row[0],
                        type=row[1],
                        nullable=(row[2] == "YES"),
                        default=row[3],
                        # MySQL returns '' (not NULL) for no comment; normalise.
                        comment=row[4] or None,
                    )
                    for row in cur.fetchall()
                ]

    def table_comment(self, schema: str, table: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(TABLE_COMMENT_SQL, (schema, table))
                row = cur.fetchone()
                # MySQL returns '' (not NULL) for no comment; normalise.
                return (row[0] or None) if row else None

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        query = f"SELECT * FROM {_ident(schema)}.{_ident(table)} LIMIT %s"  # nosec B608 — identifiers go through _ident (backtick-quoted); limit is bound as %s
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (limit,))
                columns = [d[0] for d in cur.description]
                rows = cur.fetchall()
        return Preview(columns=columns, rows=list(rows))

    # --- ad-hoc queries --------------------------------------------------

    def run_query(self, sql_text: str, *, max_rows: int = 1000,
                  timeout_ms: int = 15000, read_only: bool = True) -> QueryResult:
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    # max_execution_time bounds SELECTs (the only thing it limits
                    # in MySQL); START TRANSACTION READ ONLY makes the server
                    # itself reject any write — no fragile SQL scanning.
                    cur.execute("SET SESSION max_execution_time = %s", [timeout_ms])
                    cur.execute("START TRANSACTION READ ONLY" if read_only
                                else "START TRANSACTION")
                    t0 = time.perf_counter()
                    cur.execute(sql_text)
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    if cur.description is not None:
                        fetched = cur.fetchmany(max_rows + 1)
                        truncated = len(fetched) > max_rows
                        rows = [tuple(r) for r in fetched[:max_rows]]
                        columns = [d[0] for d in cur.description]
                        rowcount = len(rows)
                    else:
                        columns, rows, truncated, rowcount = [], [], False, cur.rowcount
            except pymysql.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            if read_only:
                conn.rollback()   # never persist, even a write that slipped past
            else:
                conn.commit()
        return QueryResult(
            columns=columns, rows=rows, rowcount=rowcount,
            truncated=truncated, duration_ms=duration_ms,
        )

    def filter_rows(self, schema: str, table: str, filters: list[dict], *,
                    limit: int = 1000, timeout_ms: int = 15000) -> QueryResult:
        """Read-only `SELECT * … WHERE <conds>` from the filter builder. Columns
        are validated against the real table and backtick-quoted; values are bound
        as placeholders — nothing is interpolated, so this is a guided query."""
        valid = {c.name for c in self.list_columns(schema, table)}
        conds, params = [], []
        for f in filters:
            col = f.get("column")
            op = f.get("op")
            if col not in valid:
                raise EngineError(_("No such column: %(name)s") % {"name": col})
            if op not in FILTER_OPS:
                raise EngineError(_("Unknown filter operator: %(op)s") % {"op": op})
            sql_op, needs_value, wrap = FILTER_OPS[op]
            if needs_value:
                value = f.get("value", "")
                params.append(wrap.format(value) if wrap else value)
                conds.append(f"{_ident(col)} {sql_op} %s")
            else:
                conds.append(f"{_ident(col)} {sql_op}")
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        query = f"SELECT * FROM {_ident(schema)}.{_ident(table)}{where} LIMIT %s"  # nosec B608 — identifiers via _ident; operators from FILTER_OPS allow-list; values bound as %s
        params.append(limit + 1)  # one extra row tells us the result was capped

        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET SESSION max_execution_time = %s", [timeout_ms])
                    cur.execute("START TRANSACTION READ ONLY")
                    t0 = time.perf_counter()
                    cur.execute(query, params)
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    fetched = cur.fetchmany(limit + 1)
                    truncated = len(fetched) > limit
                    rows = [tuple(r) for r in fetched[:limit]]
                    columns = [d[0] for d in cur.description]
            except pymysql.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()
        return QueryResult(columns=columns, rows=rows, rowcount=len(rows),
                           truncated=truncated, duration_ms=duration_ms)

    def import_csv(self, schema: str, table: str, fileobj, *,
                   encoding: str = "utf-8-sig") -> int:
        """Append rows from a CSV (with a header row) into an existing table,
        matched by header name and run as one all-or-nothing transaction (a bad
        row rolls all of it back, so the table is never left half-loaded). Rows go
        through a bound, batched INSERT — not LOAD DATA, which needs server-side
        file access or local_infile. An empty field is treated as NULL, matching
        Postgres' COPY default; quoting can't be recovered from csv, so a column
        that should hold an empty string receives NULL instead. Returns the count.
        """
        data = fileobj.read()
        if isinstance(data, bytes):
            data = data.decode(encoding, errors="replace")
        if not data.strip():
            raise EngineError(_("The CSV file is empty."))
        reader = csv.reader(data.splitlines())
        try:
            header = [c.strip() for c in next(reader)]
        except StopIteration as exc:
            raise EngineError(_("The CSV file is empty.")) from exc

        valid = {c.name for c in self.list_columns(schema, table)}
        unknown = [c for c in header if c not in valid]
        if unknown:
            raise EngineError(_("CSV header has columns not in %(table)s: %(cols)s") % {
                "table": table, "cols": ", ".join(unknown)})

        rows = [
            [v if v != "" else None for v in row]
            for row in reader if row  # skip blank trailing lines
        ]
        if not rows:
            return 0
        cols_sql = ", ".join(_ident(c) for c in header)
        placeholders = ", ".join(["%s"] * len(header))
        insert = (f"INSERT INTO {_ident(schema)}.{_ident(table)} "  # nosec B608 — schema/table/columns via _ident; row values bound as %s placeholders
                  f"({cols_sql}) VALUES ({placeholders})")
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("START TRANSACTION")
                    cur.executemany(insert, rows)
                    count = cur.rowcount
                conn.commit()
            except pymysql.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
        return count

    def stream_query(self, sql_text: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Run read-only SQL and stream the full result for a file export. Yields
        the column-name list first, then one row tuple at a time, pulled through
        an unbuffered server-side cursor (SSCursor) so a large result is never
        buffered whole in memory. Read-only and time-limited like run_query; only
        the first next() (which runs the query) raises EngineError."""
        return self._stream(sql_text, timeout_ms=timeout_ms, max_rows=max_rows)

    def stream_table(self, schema: str, table: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Stream a whole table for a CSV/JSON export — identifiers backtick-quoted,
        then streamed through the same read-only server-side cursor path."""
        query = f"SELECT * FROM {_ident(schema)}.{_ident(table)}"  # nosec B608 — identifiers via _ident; no user values in this statement
        return self._stream(query, timeout_ms=timeout_ms, max_rows=max_rows)

    def _stream(self, query: str, *, timeout_ms: int, max_rows: int):
        with self._connect() as conn:
            try:
                with conn.cursor() as setup:
                    setup.execute("SET SESSION max_execution_time = %s", [timeout_ms])
                    setup.execute("START TRANSACTION READ ONLY")
                # SSCursor streams rows from the server instead of buffering them.
                with conn.cursor(pymysql.cursors.SSCursor) as cur:
                    cur.execute(query)
                    if cur.description is None:
                        yield []          # not a row-returning statement
                        return
                    yield [d[0] for d in cur.description]   # header first
                    sent = 0
                    for row in cur:
                        yield tuple(row)
                        sent += 1
                        if sent >= max_rows:
                            return
            except pymysql.Error as exc:
                raise EngineError(_clean(exc)) from exc
            finally:
                conn.rollback()           # read-only: never persist anything

    # --- EXPLAIN ---------------------------------------------------------

    def explain(self, sql_text: str, *, analyze: bool = False,
                timeout_ms: int = 15000) -> str:
        # FORMAT=TREE is MySQL's closest analogue to psql's text plan (8.0.16+);
        # EXPLAIN ANALYZE runs the query for real timings (8.0.18+).
        prefix = "EXPLAIN ANALYZE " if analyze else "EXPLAIN FORMAT=TREE "
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET SESSION max_execution_time = %s", [timeout_ms])
                    # Read-only even for ANALYZE: it executes the query, so the
                    # read-only transaction is what keeps a write safe.
                    cur.execute("START TRANSACTION READ ONLY")
                    cur.execute(prefix + sql_text)
                    lines = [str(row[0]) for row in cur.fetchall()]
            except pymysql.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()
        return "\n".join(lines)

    def explain_json(self, sql_text: str, *, analyze: bool = False,
                     timeout_ms: int = 15000) -> PlanNode:
        # MySQL only emits the structured tree via FORMAT=JSON, which carries
        # estimates only — EXPLAIN ANALYZE returns text (TREE), not JSON. So the
        # structured plan (used for snapshots / diffs) is always estimate-based;
        # `analyze` is accepted for interface parity but doesn't add real timings.
        with self._connect() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SET SESSION max_execution_time = %s", [timeout_ms])
                    cur.execute("START TRANSACTION READ ONLY")
                    cur.execute("EXPLAIN FORMAT=JSON " + sql_text)
                    payload = cur.fetchone()[0]
            except pymysql.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()
        return _parse_plan(payload)

    # --- activity / sessions ---------------------------------------------

    def list_activity(self) -> list[Activity]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(ACTIVITY_SQL)
                return [_activity(row) for row in cur.fetchall()]

    def connection_headroom(self) -> ConnectionHeadroom:
        # Threads_connected is the live count; max_connections the ceiling.
        # MySQL has no superuser-reserved pool and no clean idle/active split
        # per connection (Command varies), so reserved=0 and by_state stays None.
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("SHOW STATUS LIKE 'Threads_connected'")
                    used = int(cur.fetchone()[1])
                    cur.execute("SHOW VARIABLES LIKE 'max_connections'")
                    max_conn = int(cur.fetchone()[1])
                except pymysql.Error as exc:
                    raise EngineError(_clean(exc)) from exc
        return ConnectionHeadroom(used=used, max=max_conn)

    def cancel_backend(self, pid: int) -> bool:
        return self._kill(pid, "QUERY")

    def terminate_backend(self, pid: int) -> bool:
        return self._kill(pid, "CONNECTION")

    def _kill(self, pid: int, what: str) -> bool:
        # pid is an int (the view casts it); KILL takes no placeholders, so the
        # validated int is spliced. `what` is a fixed literal, never user input.
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(f"KILL {what} {int(pid)}")
                    return True
                except pymysql.Error as exc:
                    raise EngineError(_clean(exc)) from exc

    def list_blocking(self):
        """The lock-wait graph: who is stuck on a lock and who holds it.

        Safety-critical, so it must never answer a false "nothing blocked". The
        data lives in performance_schema.data_lock_waits (MySQL 8.0+); when that
        instrumentation is OFF the table is simply empty, indistinguishable from
        "no waits", so we check @@performance_schema first and raise rather than
        return an empty list. Older servers without data_lock_waits raise too."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT @@performance_schema")
                if not cur.fetchone()[0]:
                    raise EngineError(_(
                        "Can't check for lock waits: the MySQL performance_schema "
                        "is disabled on this server, so blocking can't be detected. "
                        "Enable it (performance_schema=ON) to use this panel."))
                try:
                    cur.execute(BLOCKING_SQL)
                    rows = cur.fetchall()
                except pymysql.Error as exc:
                    # data_lock_waits / data_locks are MySQL 8.0+; older servers
                    # lack them. Surface that instead of a misleading empty panel.
                    raise EngineError(_(
                        "Can't check for lock waits: this needs MySQL 8.0+ "
                        "(performance_schema.data_lock_waits is unavailable here).")
                    ) from exc
        return _lock_waits(rows)

    # --- catalog browsing ------------------------------------------------

    def list_databases(self) -> list[Database]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_DATABASES_SQL)
                return [
                    Database(name=row[0], owner="", encoding=row[1] or "",
                             size=_pretty_size(row[2]))
                    for row in cur.fetchall()
                ]

    def list_schemas(self):
        # MySQL has no schemas distinct from databases — they're the same object.
        # The objects panel lists databases instead; schemas stays empty.
        return []

    def list_roles(self) -> list[Role]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_ROLES_SQL)
                return [_role(row) for row in cur.fetchall()]

    # --- catalog mutations (databases) -----------------------------------

    def create_database(self, name: str, *, template: str | None = None,
                        owner: str | None = None,
                        encoding: str | None = None) -> None:
        if template:
            raise EngineError(_(
                "MySQL can't create a database from a template (no TEMPLATE/"
                "CREATE DATABASE … LIKE). Create it empty, then load a dump."))
        stmt = f"CREATE DATABASE {_ident(name)}"
        if encoding:
            # encoding is a charset name; bound as a literal so it can't inject.
            stmt += " CHARACTER SET %s"
            self._execute(stmt, (encoding,))
        else:
            self._execute(stmt)

    def drop_database(self, name: str, *, force: bool = False) -> None:
        if name == self.connection.dbname:
            raise EngineError(_(
                "Can't drop the database this connection is using — connect to "
                "another database first."))
        self._execute(f"DROP DATABASE {_ident(name)}")

    def rename_database(self, old: str, new: str) -> None:
        raise EngineError(_(
            "MySQL doesn't support renaming a database. Create the new one and "
            "move the tables (RENAME TABLE), or dump and reload."))

    # --- indexes ---------------------------------------------------------

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_INDEXES_SQL, (schema, table))
                return [_index(row) for row in cur.fetchall()]

    def create_index(self, schema: str, table: str, columns: list[str], *,
                     method: str = "btree", unique: bool = False,
                     name: str | None = None) -> None:
        if method not in INDEX_METHODS:
            raise EngineError(_("Unsupported index method: %(method)s") % {"method": method})
        if not columns:
            raise EngineError(_("Select at least one column to index."))
        # Whitelist requested columns against the real table: names are
        # identifiers (can't be bound), so verify they exist rather than splice an
        # unknown name into DDL.
        valid = {c.name for c in self.list_columns(schema, table)}
        unknown = [c for c in columns if c not in valid]
        if unknown:
            raise EngineError(_("No such column(s): %(cols)s") % {"cols": ', '.join(unknown)})
        # Auto-name like Postgres' default if the user left it blank.
        index_name = name or f"{table}_{'_'.join(columns)}_idx"
        cols_sql = ", ".join(_ident(c) for c in columns)
        unique_sql = "UNIQUE " if unique else ""
        self._execute(
            f"CREATE {unique_sql}INDEX {_ident(index_name)} "
            f"ON {_ident(schema)}.{_ident(table)} ({cols_sql}) USING {method.upper()}")

    def drop_index(self, schema: str, name: str, table: str | None = None) -> None:
        # MySQL indexes are scoped to a table (an index name isn't unique within a
        # database), so DROP INDEX needs it; the detail panel posts the table.
        if not table:
            raise EngineError(_("Dropping an index needs the table it's on."))
        self._execute(
            f"DROP INDEX {_ident(name)} ON {_ident(schema)}.{_ident(table)}")

    # --- table-level operations ------------------------------------------

    def rename_table(self, schema: str, table: str, new_name: str) -> None:
        self._execute(
            f"RENAME TABLE {_ident(schema)}.{_ident(table)} "
            f"TO {_ident(schema)}.{_ident(new_name)}")

    def truncate_table(self, schema: str, table: str) -> None:
        self._execute(f"TRUNCATE TABLE {_ident(schema)}.{_ident(table)}")

    def drop_table(self, schema: str, table: str) -> None:
        self._execute(f"DROP TABLE {_ident(schema)}.{_ident(table)}")

    # --- column-level operations (ALTER TABLE) ---------------------------

    def add_column(self, schema: str, table: str, name: str, col_type: str, *,
                   nullable: bool = True, default: str | None = None) -> None:
        if col_type not in COLUMN_TYPES:
            raise EngineError(_(
                "Unsupported column type: %(t)s. Pick one of the listed types.")
                % {"t": col_type})
        stmt = (f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
                f"ADD COLUMN {_ident(name)} {col_type}")
        params: list = []
        if default not in (None, ""):
            stmt += " DEFAULT %s"
            params.append(default)
        if not nullable:
            stmt += " NOT NULL"
        self._execute(stmt, params or None)

    def rename_column(self, schema: str, table: str, old: str, new: str) -> None:
        # RENAME COLUMN is 8.0+; it keeps the existing type/attributes (unlike the
        # old CHANGE syntax, which needs them respecified).
        self._require_column(schema, table, old)
        self._execute(
            f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
            f"RENAME COLUMN {_ident(old)} TO {_ident(new)}")

    def drop_column(self, schema: str, table: str, name: str) -> None:
        self._require_column(schema, table, name)
        self._execute(
            f"ALTER TABLE {_ident(schema)}.{_ident(table)} DROP COLUMN {_ident(name)}")

    def alter_column_type(self, schema: str, table: str, name: str,
                          new_type: str) -> None:
        if new_type not in COLUMN_TYPES:
            raise EngineError(_(
                "Unsupported column type: %(t)s. Pick one of the listed types.")
                % {"t": new_type})
        # MySQL's MODIFY re-states the whole column, so any attribute left off is
        # dropped. Preserve NOT NULL (the safety-critical one — silently allowing
        # NULLs into a previously NOT NULL column would be surprising). A DEFAULT
        # can be an expression we can't safely re-quote, so it's not carried over;
        # the user can re-set it from the column menu.
        col = self._column(schema, table, name)
        null_sql = "" if col.nullable else " NOT NULL"
        self._execute(
            f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
            f"MODIFY COLUMN {_ident(name)} {new_type}{null_sql}")

    def set_column_null(self, schema: str, table: str, name: str, *,
                        nullable: bool) -> None:
        # MySQL has no "ALTER COLUMN … SET/DROP NOT NULL"; toggling nullability
        # means re-stating the column's type via MODIFY. Re-derive the type from
        # the catalog so the change is type-preserving.
        col = self._column(schema, table, name)
        null_sql = "NULL" if nullable else "NOT NULL"
        self._execute(
            f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
            f"MODIFY COLUMN {_ident(name)} {col.type} {null_sql}")

    def set_column_default(self, schema: str, table: str, name: str,
                           default: str | None) -> None:
        self._require_column(schema, table, name)
        if default in (None, ""):
            self._execute(
                f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
                f"ALTER COLUMN {_ident(name)} DROP DEFAULT")
        else:
            self._execute(
                f"ALTER TABLE {_ident(schema)}.{_ident(table)} "
                f"ALTER COLUMN {_ident(name)} SET DEFAULT %s", (default,))

    def _require_column(self, schema: str, table: str, name: str) -> None:
        if name not in {c.name for c in self.list_columns(schema, table)}:
            raise EngineError(_("No such column: %(name)s") % {"name": name})

    def _column(self, schema: str, table: str, name: str) -> Column:
        for c in self.list_columns(schema, table):
            if c.name == name:
                return c
        raise EngineError(_("No such column: %(name)s") % {"name": name})

    # --- health ----------------------------------------------------------

    def table_sizes(self, limit: int = 20) -> list[TableSize]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(TABLE_SIZES_SQL, (self.connection.dbname, limit))
                return [
                    TableSize(
                        schema=row[0], name=row[1], total_bytes=int(row[2] or 0),
                        total=_pretty_size(row[2]) or "0 B",
                        table=_pretty_size(row[3]) or "0 B",
                        index=_pretty_size(row[4]) or "0 B",
                    )
                    for row in cur.fetchall()
                ]

    def unused_indexes(self) -> list[UnusedIndex]:
        """Drop-candidate indexes (never read) for the connected database, via
        sys.schema_unused_indexes. Unlike list_blocking this is an optimisation
        hint, not a safety signal, so when performance_schema is off the view is
        simply empty (best-effort) rather than an error — and it feeds an
        aggregate count card that must keep working."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(UNUSED_INDEXES_SQL, (self.connection.dbname,))
                return [_unused_index(row) for row in cur.fetchall()]

    def vacuum_stats(self):
        return []   # "vacuum" not applicable — InnoDB has no dead-tuple model

    def bloat_estimates(self, limit: int = 20):
        return []   # "bloat" not applicable — no MySQL pg_stats-style estimate

    # --- backup (mysqldump / mysql) --------------------------------------

    def dump_database(self, dbname: str, *, fmt: str = "plain") -> Dump:
        """Dump a whole database with mysqldump, returned as a downloadable blob.
        MySQL has only one dump shape (SQL text), so `fmt` is accepted (the
        auto-snapshot path asks for "custom") but always yields SQL."""
        return self._run_mysqldump([dbname], base=dbname)

    def dump_table(self, schema: str, table: str, *, fmt: str = "plain") -> Dump:
        """Dump a single table. MySQL has no schema-vs-database split, so `schema`
        is the database; mysqldump takes `<database> <table>` positionally."""
        return self._run_mysqldump([schema, table], base=f"{schema}.{table}")

    def _run_mysqldump(self, scope: list[str], *, base: str) -> Dump:
        conn = self.connection
        argv = [
            "mysqldump",
            "-h", conn.host, "-P", str(conn.port), "-u", conn.user,
            # A consistent InnoDB snapshot without locking; skip tablespaces so
            # the dump doesn't need the PROCESS privilege (mysqldump 8.0).
            "--single-transaction", "--no-tablespaces", *scope,
        ]
        env = {**os.environ, "MYSQL_PWD": conn.password or ""}
        try:
            # No shell, fixed argv, password via env (never on the command line).
            proc = subprocess.run(  # nosec B603 B607
                argv, capture_output=True, env=env, timeout=MYSQLDUMP_TIMEOUT)
        except FileNotFoundError as exc:
            raise EngineError(
                "mysqldump not found — install the mysql client package "
                "(it ships in the Docker image).") from exc
        except subprocess.TimeoutExpired as exc:
            raise EngineError(_("mysqldump timed out.")) from exc
        if proc.returncode != 0:
            raise EngineError(_tool_error(proc.stderr, "mysqldump failed."))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Dump(filename=f"{base}-{stamp}.sql",
                    content_type="application/sql", data=proc.stdout)

    def restore(self, dbname: str, data: bytes) -> None:
        """Restore dump bytes into an existing database (in-memory convenience)."""
        self.restore_stream(dbname, io.BytesIO(data))

    def restore_stream(self, dbname: str, fileobj) -> None:
        """Restore a mysqldump SQL dump (a file-like object) into an existing
        database by streaming it to the `mysql` client's stdin in chunks rather
        than loading the whole dump into memory. The client runs in batch mode,
        which stops and exits non-zero on the first failed statement."""
        conn = self.connection
        argv = ["mysql", "-h", conn.host, "-P", str(conn.port),
                "-u", conn.user, dbname]
        env = {**os.environ, "MYSQL_PWD": conn.password or ""}
        try:
            # No shell; the dump is fed on stdin in chunks, never written to disk.
            proc = subprocess.Popen(  # nosec B603 B607
                argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=env)
        except FileNotFoundError as exc:
            raise EngineError(
                "mysql not found — install the mysql client package "
                "(it ships in the Docker image).") from exc
        # Drain stderr on a thread so a chatty client can't fill the pipe and
        # deadlock us while we're busy writing stdin.
        err_chunks: list[bytes] = []
        drainer = threading.Thread(
            target=lambda: err_chunks.extend(iter(lambda: proc.stderr.read(8192), b"")),
            daemon=True)
        drainer.start()
        try:
            with proc.stdin as stdin:
                for chunk in iter(lambda: fileobj.read(65536), b""):
                    stdin.write(chunk)
            proc.wait(timeout=RESTORE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise EngineError(_("restore timed out.")) from exc
        except BrokenPipeError:
            # The client exited early (e.g. an error mid-stream); fall through to
            # report it from the captured stderr below.
            proc.wait()
        finally:
            drainer.join(timeout=5)
        if proc.returncode != 0:
            raise EngineError(_tool_error(b"".join(err_chunks), "restore failed."))

    # --- replication (binlog / GTID) -------------------------------------

    def replication_status(self) -> MysqlReplStatus:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT @@global.log_bin, @@global.server_id, @@global.gtid_mode")
                log_bin, server_id, gtid_mode = cur.fetchone()
                binlog_file, binlog_pos = self._binlog_position(cur)
                repl = self._replica_row(cur)
        is_replica = repl is not None
        role = "replica" if is_replica else ("source" if log_bin else "standalone")
        repl = repl or {}
        return MysqlReplStatus(
            role=role, log_bin=bool(log_bin), server_id=int(server_id or 0),
            gtid_mode=gtid_mode or "OFF",
            binlog_file=binlog_file, binlog_pos=binlog_pos,
            source_host=repl.get("source_host"), io_running=repl.get("io_running"),
            sql_running=repl.get("sql_running"),
            seconds_behind=repl.get("seconds_behind"),
        )

    @staticmethod
    def _binlog_position(cur):
        # SHOW BINARY LOG STATUS (MySQL 8.2+) → SHOW MASTER STATUS (8.0). One
        # returns (File, Position, …); neither returns a row if binlog is off.
        for stmt in ("SHOW BINARY LOG STATUS", "SHOW MASTER STATUS"):
            try:
                cur.execute(stmt)
            except pymysql.Error:
                continue
            row = cur.fetchone()
            return (row[0], int(row[1])) if row else (None, None)
        return None, None

    @staticmethod
    def _replica_row(cur):
        # SHOW REPLICA STATUS (8.0.22+) → SHOW SLAVE STATUS (older). ~50 columns;
        # map by name and read the few we show, tolerating both spellings.
        for stmt in ("SHOW REPLICA STATUS", "SHOW SLAVE STATUS"):
            try:
                cur.execute(stmt)
            except pymysql.Error:
                continue
            row = cur.fetchone()
            if not row:
                return None
            d = {desc[0]: val for desc, val in zip(cur.description, row)}

            def pick(*keys):
                return next((d[k] for k in keys if k in d), None)

            return {
                "source_host": pick("Source_Host", "Master_Host"),
                "io_running": pick("Replica_IO_Running", "Slave_IO_Running") == "Yes",
                "sql_running": pick("Replica_SQL_Running", "Slave_SQL_Running") == "Yes",
                "seconds_behind": pick("Seconds_Behind_Source", "Seconds_Behind_Master"),
            }
        return None

    def list_standbys(self) -> list:
        """Connected replicas on a source — SHOW REPLICAS (8.0.22+) → SHOW SLAVE
        HOSTS (older). Empty unless a replica is attached (or we lack the priv)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                for stmt in ("SHOW REPLICAS", "SHOW SLAVE HOSTS"):
                    try:
                        cur.execute(stmt)
                    except pymysql.Error:
                        continue
                    cols = [d[0] for d in cur.description]
                    return [_replica(dict(zip(cols, r))) for r in cur.fetchall()]
        return []

    def list_replication_slots(self) -> list:
        return []   # MySQL has no replication-slot concept (UNSUPPORTED)

    def create_replication_slot(self, name: str) -> None:
        raise EngineError(_(
            "MySQL has no replication slots — it streams the binary log directly, "
            "so there's nothing to create."))

    def drop_replication_slot(self, name: str) -> None:
        raise EngineError(_("MySQL has no replication slots — nothing to drop."))

    def replication_recipe(self, status, slots=None) -> MysqlReplRecipe:
        c = self.connection
        conf = []
        if not status.log_bin:
            conf.append(("log_bin", "ON  (--log-bin in my.cnf; needs a restart)"))
        if status.server_id == 0:
            conf.append(("server_id", "1  (unique per server; needs a restart)"))
        if status.gtid_mode != "ON":
            conf.append(("gtid_mode / enforce_gtid_consistency",
                         "ON  (recommended — enables SOURCE_AUTO_POSITION)"))
        repl_user = "repl"
        create_user = (
            f"CREATE USER '{repl_user}'@'%' IDENTIFIED BY '<password>';\n"
            f"GRANT REPLICATION SLAVE ON *.* TO '{repl_user}'@'%';")
        change_source = (
            "CHANGE REPLICATION SOURCE TO\n"
            f"  SOURCE_HOST='{c.host}', SOURCE_PORT={c.port},\n"
            f"  SOURCE_USER='{repl_user}', SOURCE_PASSWORD='<password>',\n"
            "  SOURCE_AUTO_POSITION=1;")
        return MysqlReplRecipe(
            source_host=c.host, source_port=c.port, repl_user=repl_user,
            conf_changes=conf, create_user_sql=create_user,
            change_source_sql=change_source, start_replica_sql="START REPLICA;",
        )

    # --- server configuration (global variables, via SQL) ----------------

    def common_settings(self) -> list[str]:
        return MYSQL_COMMON_SETTINGS

    def list_settings(self, names=None, category=None) -> list[Setting]:
        # MySQL has no setting categories (list_setting_categories is empty), so
        # `category` is ignored; the panel filters client-side instead.
        where, params = "", []
        if names:
            placeholders = ", ".join(["%s"] * len(names))
            where = f"WHERE VARIABLE_NAME IN ({placeholders})"
            params = list(names)
        sql_text = ("SELECT VARIABLE_NAME, VARIABLE_VALUE "  # nosec B608 — `where` holds only %s placeholders; names are bound, nothing interpolated
                    f"FROM performance_schema.global_variables {where} "
                    "ORDER BY VARIABLE_NAME")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_text, params)
                return [_setting(name, value) for name, value in cur.fetchall()]

    def list_setting_categories(self) -> list[str]:
        return []   # MySQL system variables aren't grouped into categories

    def pending_restart_settings(self) -> list[Setting]:
        return []   # no clean MySQL equivalent of pg_settings.pending_restart

    def update_setting(self, name: str, value: str) -> Setting:
        """Persist a global variable with SET PERSIST (writes mysqld-auto.cnf, so
        it survives a restart — the closest match to Postgres' ALTER SYSTEM)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._require_var(cur, name)
                try:
                    # name is catalog-verified + charset-checked (can't be bound).
                    if _NUMERIC_RE.match(value):
                        # A numeric variable rejects a quoted value; the value is
                        # digits only here, so splicing it is injection-safe.
                        cur.execute(f"SET PERSIST {name} = {value}")  # nosec B608 — name whitelisted; value is digits-only
                    else:
                        cur.execute(f"SET PERSIST {name} = %s", [value])  # nosec B608 — name whitelisted; value bound
                except pymysql.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    def reset_setting(self, name: str) -> Setting:
        """Drop the persisted value (RESET PERSIST) and revert the running value
        to the server default (SET GLOBAL … = DEFAULT)."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                self._require_var(cur, name)
                try:
                    cur.execute(f"RESET PERSIST IF EXISTS {name}")  # nosec B608 — name whitelisted via _require_var
                    cur.execute(f"SET GLOBAL {name} = DEFAULT")     # nosec B608 — name whitelisted via _require_var
                except pymysql.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    @staticmethod
    def _require_var(cur, name: str) -> None:
        """Guard a variable name before it's spliced into SET PERSIST: it must
        match the system-variable charset and actually exist on the server."""
        if not _VAR_NAME_RE.match(name):
            raise EngineError(_("Unknown setting: %(name)s") % {"name": name})
        cur.execute(
            "SELECT 1 FROM performance_schema.global_variables "
            "WHERE VARIABLE_NAME = %s", [name])
        if cur.fetchone() is None:
            raise EngineError(_("Unknown setting: %(name)s") % {"name": name})

    @staticmethod
    def _one_setting(cur, name: str) -> Setting:
        cur.execute(
            "SELECT VARIABLE_NAME, VARIABLE_VALUE "
            "FROM performance_schema.global_variables WHERE VARIABLE_NAME = %s",
            [name])
        row = cur.fetchone()
        if row is None:
            raise EngineError(_("Unknown setting: %(name)s") % {"name": name})
        return _setting(row[0], row[1])

    # --- shared DDL runner -----------------------------------------------

    def _execute(self, statement: str, params=None) -> None:
        """Run a DDL/DML statement (autocommit connection — MySQL DDL commits
        implicitly anyway), mapping driver errors to EngineError."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(statement, params)
                except pymysql.Error as exc:
                    raise EngineError(_clean(exc)) from exc


# --- row → dataclass helpers ---------------------------------------------

def _activity(row) -> Activity:
    (pid, user, db, host, command, state, secs, info, is_self) = row
    cmd = command or ""
    return Activity(
        pid=pid,
        user=user,
        database=db,
        app=None,                       # MySQL has no per-session application name
        client=host,
        # Map MySQL's COMMAND onto the active/idle the overview counts: a running
        # query is COMMAND='Query'; everything else (Sleep, …) is shown verbatim.
        state="active" if cmd == "Query" else cmd.lower() or None,
        wait=state or None,             # MySQL's detailed STATE string
        blocked_by=[],                  # per-session blockers aren't joined here; the dedicated locks panel (list_blocking) shows the full wait graph
        query_secs=secs,
        query=info or "",
        is_self=bool(is_self),
    )


def _role(row) -> Role:
    """Turn a mysql.user row into the attribute labels the objects panel shows."""
    user, host, super_priv, locked = row
    can_login = (locked or "").upper() != "Y"
    is_super = (super_priv or "").upper() == "Y"
    attrs = []
    if is_super:
        attrs.append("Superuser")
    if not can_login:
        attrs.append("Locked")
    return Role(name=f"{user}@{host}", attributes=attrs, can_login=can_login,
                superuser=is_super)


def _setting(name: str, value) -> Setting:
    """Map a global_variables row to the shared Setting shape. MySQL exposes far
    less metadata than pg_settings (no unit/category/range/default/restart flag),
    so those are left empty; ON/OFF values render as a bool toggle. All variables
    are shown editable — MySQL reports read-only ones only when SET PERSIST is
    attempted, and update_setting surfaces that error inline."""
    val = "" if value is None else str(value)
    is_bool = val in ("ON", "OFF")
    return Setting(
        name=name,
        value=val.lower() if is_bool else val,
        unit=None, category="", description="",
        vartype="bool" if is_bool else "string",
        context="user", enumvals=None, min_val=None, max_val=None,
        default=None, pending_restart=False,
    )


def _replica(d) -> MysqlReplica:
    """Map a SHOW REPLICAS / SHOW SLAVE HOSTS row (already a name→value dict) to a
    MysqlReplica, tolerating the two column-name spellings."""
    def pick(*keys):
        return next((d[k] for k in keys if k in d), None)
    port = pick("Port")
    return MysqlReplica(
        server_id=int(pick("Server_Id", "Server_id") or 0),
        host=pick("Host"),
        port=int(port) if port else None,
    )


def _index(row) -> Index:
    name, method, non_unique, columns = row
    unique = (int(non_unique) == 0)
    primary = (name == "PRIMARY")
    cols = columns or ""
    kind = "UNIQUE INDEX" if unique else "INDEX"
    definition = f"{kind} {name} ({cols}) USING {method}"
    # Per-index on-disk size isn't exposed by information_schema, so it's unknown.
    return Index(name=name, method=(method or "").lower(), unique=unique,
                 primary=primary, definition=definition, size=None, valid=True)


def _lock_waits(rows) -> list[LockWait]:
    """Fold the flat (blocked, blocker) pairs from BLOCKING_SQL into one LockWait
    per blocked session, collecting its blockers. The blocked-side columns repeat
    across a session's rows; we read them once and append each distinct blocker.

    A blocker whose processlist COMMAND is 'Sleep' is holding the lock while
    sitting idle inside an open transaction — the classic MySQL stall — so we
    label it 'idle in transaction' to reuse the locks panel's amber badge."""
    waits: dict[int, LockWait] = {}
    seen_blockers: set[tuple[int, int]] = set()
    for row in rows:
        (blocked_pid, blocked_user, blocked_query, wait_secs, lock_type,
         lock_mode, obj, blocker_pid, blocker_user, blocker_command,
         blocker_query) = row
        wait = waits.get(blocked_pid)
        if wait is None:
            wait = LockWait(
                blocked_pid=blocked_pid, blocked_user=blocked_user,
                blocked_query=blocked_query or "", wait_secs=wait_secs,
                lock_type=lock_type or "", lock_mode=lock_mode or "",
                object=obj or (lock_type or ""), blockers=[],
            )
            waits[blocked_pid] = wait
        if blocker_pid is not None and (blocked_pid, blocker_pid) not in seen_blockers:
            seen_blockers.add((blocked_pid, blocker_pid))
            state = ("idle in transaction"
                     if (blocker_command or "") == "Sleep"
                     else (blocker_command or "").lower() or None)
            wait.blockers.append(Blocker(
                pid=blocker_pid, user=blocker_user, state=state,
                query=blocker_query or "",
            ))
    return list(waits.values())


def _unused_index(row) -> UnusedIndex:
    """Map a sys.schema_unused_indexes row to UnusedIndex. By definition these had
    zero reads (scans=0); MySQL exposes no cheap per-index byte size, so it's
    reported as unknown — same as list_indexes."""
    schema, table, name = row
    return UnusedIndex(schema=schema, table=table, name=name,
                       scans=0, bytes=0, size=None)


def _pretty_size(n) -> str | None:
    """Pretty-print a byte count like pg_size_pretty; None passes through (a
    database with no tables reports NULL for its summed size)."""
    if n is None:
        return None
    n = int(n)
    if n >= 1073741824:
        return f"{n / 1073741824:.1f} GB"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} kB"
    return f"{n} B"


def _clean(exc: pymysql.Error) -> str:
    """PyMySQL errors carry (errno, message); surface the message, falling back
    to the string form's first line."""
    args = getattr(exc, "args", None)
    if args and len(args) >= 2 and isinstance(args[1], str):
        return args[1]
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else "Could not connect to MySQL."


def _tool_error(stderr: bytes, fallback: str) -> str:
    """Pull the useful cause out of a mysqldump/mysql stderr dump. These tools
    interleave notices with the real failure, so prefer lines carrying an error
    marker (`mysqldump: Error:`, `ERROR 1045 (28000):`), keeping the first few
    for context; fall back to the closing lines, then a generic message."""
    lines = [ln.strip() for ln in stderr.decode(errors="replace").splitlines()
             if ln.strip()]
    if not lines:
        return fallback
    flagged = [ln for ln in lines if "error" in ln.lower()]
    chosen = flagged or lines[-3:]
    return " · ".join(chosen[:3])


# --- EXPLAIN FORMAT=JSON → PlanNode --------------------------------------

# MySQL access_type → a readable node label. Labels that describe a table access
# end in "Scan" so plan_diff aligns a method swap on the same table into one
# 'changed' row (its _shape_key keys scans by relation).
_ACCESS_LABELS = {
    "ALL": "Full Table Scan",
    "index": "Index Scan",
    "range": "Index Range Scan",
    "ref": "Index Scan",
    "eq_ref": "Index Scan",
    "ref_or_null": "Index Scan",
    "const": "Const Scan",
    "system": "Const Scan",
    "fulltext": "Fulltext Scan",
    "index_merge": "Index Merge Scan",
    "unique_subquery": "Index Scan",
    "index_subquery": "Index Scan",
}

# query_block keys that wrap an inner operation → the node label to show for them.
_WRAP_OPS = {
    "ordering_operation": "Sort",
    "grouping_operation": "Aggregate",
    "duplicates_removal": "Distinct",
    "buffer_result": "Materialize",
}


def _parse_plan(payload) -> PlanNode:
    """Turn EXPLAIN FORMAT=JSON output into a PlanNode tree. PyMySQL returns it as
    a JSON string; accept an already-decoded dict too."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return _mysql_node(payload["query_block"])


def _f(value) -> float:
    """MySQL reports costs/rows as strings in the JSON; coerce, defaulting to 0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _mysql_node(d: dict) -> PlanNode:
    """Recursively map one level of MySQL's EXPLAIN JSON to a PlanNode.

    A level is either a wrapper operation (ordering/grouping/…), a join
    (`nested_loop`), or a leaf table access (`table`). Unknown shapes degrade to a
    generic node so an unexpected plan never crashes the EXPLAIN view."""
    cost = _f((d.get("cost_info") or {}).get("query_cost"))

    for key, label in _WRAP_OPS.items():
        if key in d:
            child = _mysql_node(d[key])
            return PlanNode(
                node_type=label, relation=None, index=None,
                plan_rows=child.plan_rows, total_cost=cost or child.total_cost,
                plan_width=0, actual_rows=None, actual_ms=None, loops=None,
                detail=None, children=[child])

    if "nested_loop" in d:
        children = [_mysql_node(item) for item in d["nested_loop"]]
        rows = children[-1].plan_rows if children else 0.0
        return PlanNode(
            node_type="Nested Loop", relation=None, index=None,
            plan_rows=rows, total_cost=cost, plan_width=0,
            actual_rows=None, actual_ms=None, loops=None, detail=None,
            children=children)

    if "table" in d:
        return _mysql_table(d["table"])

    return PlanNode(
        node_type="Result", relation=None, index=None, plan_rows=0.0,
        total_cost=cost, plan_width=0, actual_rows=None, actual_ms=None,
        loops=None, detail=None, children=[])


def _mysql_table(t: dict) -> PlanNode:
    access = t.get("access_type", "")
    label = _ACCESS_LABELS.get(access, "Table Scan")
    rows = t.get("rows_produced_per_join")
    if rows is None:
        rows = t.get("rows_examined_per_scan", 0)
    cost_info = t.get("cost_info") or {}
    cost = _f(cost_info.get("prefix_cost") or cost_info.get("read_cost"))
    children = []
    sub = t.get("materialized_from_subquery")
    if isinstance(sub, dict) and "query_block" in sub:
        children.append(_mysql_node(sub["query_block"]))
    return PlanNode(
        node_type=label, relation=t.get("table_name"), index=t.get("key"),
        plan_rows=_f(rows), total_cost=cost, plan_width=0,
        actual_rows=None, actual_ms=None, loops=None,
        detail=t.get("attached_condition"), children=children)
