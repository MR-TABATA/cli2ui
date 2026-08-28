"""PostgreSQL engine — psycopg2 under the hood."""
import contextlib
import csv
import io
import json
import os
import re
import subprocess  # nosec B404 — used only to run pg_dump with a fixed argv, no shell
import threading
import time
from datetime import datetime

import psycopg2
from django.utils.translation import gettext as _
from psycopg2 import errors as pg_errors
from psycopg2 import sql

from .base import (
    Activity,
    BloatEstimate,
    Blocker,
    Column,
    ConnectionHeadroom,
    Database,
    Dump,
    DuplicateIndex,
    Engine,
    EngineError,
    Extension,
    FKMissingIndex,
    ForeignKeyEdge,
    Index,
    InvalidIndex,
    JsonbKey,
    JsonbShape,
    LockWait,
    NullSlip,
    NullSlipCount,
    OrphanCandidate,
    OrphanCount,
    PlanNode,
    Preview,
    QueryResult,
    ReplicationRecipe,
    ReplicationSlot,
    ReplicationStatus,
    Role,
    Schema,
    Setting,
    SqlPreview,
    Standby,
    Table,
    TableSize,
    UnusedIndex,
    VacuumStat,
    WriteImpact,
)

# Catalog / stat query text lives in pg_sql.py; the engine methods below
# reference these by bare name exactly as before.
from .pg_sql import (
    ACTIVITY_MAP_SQL,
    ACTIVITY_SQL,
    BLOAT_SQL,
    BLOCKING_SQL,
    DUPLICATE_INDEX_SQL,
    EXTENSIONS_SQL,
    FK_EDGES_SQL,
    FK_MISSING_INDEX_SQL,
    FK_RESOLVE_SQL,
    HEADROOM_SQL,
    INFERRED_FK_SQL,
    INFERRED_RESOLVE_SQL,
    LIST_COLUMNS_SQL,
    LIST_DATABASES_SQL,
    LIST_INDEXES_SQL,
    LIST_ROLES_SQL,
    LIST_SCHEMAS_SQL,
    LIST_TABLES_SQL,
    NOT_VALID_FK_SQL,
    REPLICATION_STATUS_SQL,
    SETTINGS_SELECT,
    SLOTS_SQL,
    STANDBYS_SQL,
    TABLE_COMMENT_SQL,
    TABLE_SIZES_SQL,
    INVALID_INDEXES_SQL,
    NULL_SLIP_RESOLVE_SQL,
    WRITE_IMPACT_SQL,
    NULL_SLIP_SQL_LEGACY,
    NULL_SLIP_SQL_PG15,
    UNUSED_INDEXES_SQL,
    VACUUM_STATS_SQL,
)

# Index access methods we let the UI offer. A fixed allow-list because the
# chosen method is interpolated into the DDL as a bare keyword (it can't be a
# bound parameter), so it must never come straight from user input.
INDEX_METHODS = ("btree", "hash", "gin", "gist", "spgist", "brin")

# Column types offered for ADD COLUMN. Like INDEX_METHODS, the chosen type is
# spliced into the DDL as a bare keyword (it can't be bound), so it must match
# this allow-list exactly — never raw user input. Parameterised types (varchar(n),
# numeric(p,s)) are deliberately left out to keep the splice point injection-proof;
# text/numeric cover those needs.
COLUMN_TYPES = (
    "text", "integer", "bigint", "smallint", "boolean", "numeric",
    "real", "double precision", "date", "timestamptz", "timestamp",
    "time", "uuid", "jsonb", "json", "bytea", "inet",
)

# pg_dump output formats offered for backup: flag, file extension, MIME type.
# 'plain' is restorable with psql and human-readable; 'custom' is compressed and
# restorable selectively with pg_restore.
DUMP_FORMATS = {
    "plain": ("-Fp", "sql", "application/sql"),
    "custom": ("-Fc", "dump", "application/octet-stream"),
}

# How long a single pg_dump / restore may run before we give up (seconds).
DUMP_TIMEOUT = 120
RESTORE_TIMEOUT = 300

# How long a DDL statement may wait for its lock before giving up. Short on
# purpose — see _execute() for why waiting is the dangerous part.
DDL_LOCK_TIMEOUT = "2s"

# How long the table-size probe may wait for a lock. Shorter than the DDL guard:
# a size is informational, and this probe runs on the overview that leads to the
# Locks panel — see table_sizes() for why a read-only query queues at all.
SIZES_LOCK_TIMEOUT = "1s"

# The parameters people actually reach for — connections, memory, WAL, logging.
# Shown by default so the editor isn't a wall of 350 obscure GUCs.
COMMON_SETTINGS = [
    "max_connections", "shared_buffers", "effective_cache_size", "work_mem",
    "maintenance_work_mem", "wal_buffers", "min_wal_size", "max_wal_size",
    "checkpoint_completion_target", "random_page_cost", "effective_io_concurrency",
    "default_statistics_target", "log_min_duration_statement", "log_statement",
    "log_connections", "log_disconnections", "log_lock_waits",
    "idle_in_transaction_session_timeout", "statement_timeout", "timezone",
]

# Filter-builder operators. Maps a stable key (sent by the UI <select>) to the
# SQL operator, whether it takes a value, and an optional pattern wrapper for
# the value (LIKE patterns). The query is always composed with sql.Identifier /
# sql.Placeholder, so neither column names nor values are ever interpolated.
FILTER_OPS = {
    "eq":       ("=",            True,  None),
    "ne":       ("<>",           True,  None),
    "lt":       ("<",            True,  None),
    "le":       ("<=",           True,  None),
    "gt":       (">",            True,  None),
    "ge":       (">=",           True,  None),
    "contains": ("ILIKE",        True,  "%{}%"),
    "starts":   ("ILIKE",        True,  "{}%"),
    "null":     ("IS NULL",      False, None),
    "notnull":  ("IS NOT NULL",  False, None),
}


def build_create_index_sql(schema, table, columns, *, method="btree",
                           unique=False, name=None, concurrently=False):
    """Compose a CREATE INDEX statement from a spec. The shared core for both
    real index creation (CONCURRENTLY, outside a transaction) and — later — the
    "what-if" index lab (plain, inside a rolled-back transaction): same spec, the
    only difference is the CONCURRENTLY flag.

    Identifiers go through sql.Identifier (safe quoting); the method is checked
    against INDEX_METHODS so the one piece interpolated as raw SQL can't inject.
    """
    if method not in INDEX_METHODS:
        raise EngineError(_("Unsupported index method: %(method)s") % {"method": method})
    if not columns:
        raise EngineError(_("Select at least one column to index."))
    parts = [sql.SQL("CREATE")]
    if unique:
        parts.append(sql.SQL("UNIQUE"))
    parts.append(sql.SQL("INDEX"))
    if concurrently:
        parts.append(sql.SQL("CONCURRENTLY"))
    if name:
        parts.append(sql.Identifier(name))
    parts.append(sql.SQL("ON {}.{}").format(
        sql.Identifier(schema), sql.Identifier(table)))
    parts.append(sql.SQL("USING ") + sql.SQL(method))
    parts.append(sql.SQL("({})").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in columns)))
    return sql.SQL(" ").join(parts)


def _json_type(v) -> str:
    """The jsonb_typeof label for an already-parsed Python value. bool is checked
    before int/float because in Python `True` is also an `int`."""
    if isinstance(v, dict):
        return "object"
    if isinstance(v, list):
        return "array"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if v is None:
        return "null"   # the jsonb literal 'null', not SQL NULL (that's filtered out)
    return "string"


def _json_depth(v) -> int:
    """Deepest container nesting in a parsed JSON value: 0 for a bare scalar, 1
    for a flat object/array, 2 for one level of nested container, and so on.
    Iterative (an explicit stack) so a pathologically deep document can't blow
    Python's recursion limit."""
    max_d = 0
    stack = [(v, 1)]
    while stack:
        cur, d = stack.pop()
        if isinstance(cur, dict):
            if d > max_d:
                max_d = d
            for x in cur.values():
                if isinstance(x, (dict, list)):
                    stack.append((x, d + 1))
        elif isinstance(cur, list):
            if d > max_d:
                max_d = d
            for x in cur:
                if isinstance(x, (dict, list)):
                    stack.append((x, d + 1))
    return max_d


def _summarise_json(docs) -> tuple[dict, list, int]:
    """Reduce a sample of parsed JSON documents to (root_types, keys, max_depth).
    Pure Python over already-parsed values, so it's unit-testable without a DB.
    Only object roots contribute top-level keys; each key records how many sampled
    objects carried it and the distinct value types it took."""
    root_types: dict[str, int] = {}
    key_count: dict[str, int] = {}
    key_types: dict[str, set] = {}
    max_depth = 0
    for doc in docs:
        root_types[_json_type(doc)] = root_types.get(_json_type(doc), 0) + 1
        d = _json_depth(doc)
        if d > max_depth:
            max_depth = d
        if isinstance(doc, dict):
            for k, val in doc.items():
                key_count[k] = key_count.get(k, 0) + 1
                key_types.setdefault(k, set()).add(_json_type(val))
    keys = [JsonbKey(name=k, count=key_count[k], types=sorted(key_types[k]))
            for k in key_count]
    # Most common keys first; name breaks ties for a stable order.
    keys.sort(key=lambda jk: (-jk.count, jk.name))
    return root_types, keys, max_depth


def _gin_indexes_for(indexes, column: str) -> list[str]:
    """Names of the GIN indexes whose column list references `column` — a plain
    `gin(col)`, an operator-class `gin(col jsonb_path_ops)`, or an expression
    `gin((col -> 'k'))` all mention the column as a whole word. A best-effort
    catalog fact for the shape panel."""
    pat = re.compile(r"\b" + re.escape(column) + r"\b")
    return [ix.name for ix in indexes
            if ix.method == "gin" and pat.search(ix.columns_text)]


class PostgresEngine(Engine):
    # When inside session(), the one open connection for the default database;
    # otherwise None and every _connect() dials its own.
    _held = None

    @contextlib.contextmanager
    def _connect(self, dbname=None):
        # Inside session() reuse the single held connection for the default
        # database, so a caller firing many small probes (the workspace
        # overview) pays one connect instead of one per probe. A call to a
        # *different* dbname (admin ops) always opens its own.
        if dbname is None and self._held is not None:
            yield self._held
            return
        c = self.connection
        try:
            conn = psycopg2.connect(
                host=c.host,
                port=c.port,
                dbname=dbname or c.dbname,
                user=c.user,
                password=c.password,
                connect_timeout=5,
            )
        except psycopg2.Error as exc:
            raise EngineError(_clean(exc)) from exc
        # autocommit so ALTER SYSTEM / pg_reload_conf run as standalone statements.
        conn.autocommit = True
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def session(self):
        """Open one connection and reuse it for every default-database query in
        the block — the workspace overview fires ~10 read-only probes and would
        otherwise connect ~10 times. Each probe runs on its own autocommit
        statement, so one failing probe doesn't poison the rest. Do not call
        methods that flip autocommit (run_query / explain) inside a session."""
        with self._connect() as conn:
            self._held = conn
            try:
                yield self
            finally:
                self._held = None

    @contextlib.contextmanager
    def whatif_cursor(self, *, timeout_ms: int = 15000, lock_timeout: str = "2s"):
        """A cursor inside a transaction that is ALWAYS rolled back — the engine
        primitive behind the planner what-if tools (scale simulation, index lab),
        which live in their own app. Whatever the caller runs through it (a
        pg_class stats edit, a hypothetical CREATE INDEX, EXPLAIN) is never
        committed and is invisible to other sessions.

        NOT read-only: the caller deliberately edits the catalog / builds an
        index — safety is the unconditional rollback, not the transaction mode.
        statement_timeout + lock_timeout are pre-set so a what-if can't hang or
        pin catalog locks. Driver errors surface as EngineError (cleaned to one
        line); the caller never touches psycopg2."""
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute("SET LOCAL lock_timeout = %s", [lock_timeout])
                    yield cur
            except psycopg2.Error as exc:
                raise EngineError(_clean(exc)) from exc
            finally:
                conn.rollback()  # the what-if is never persisted

    @contextlib.contextmanager
    def _size_probe_cursor(self, conn):
        """A cursor for the read-only probes that measure on-disk size.

        pg_total_relation_size() / pg_table_size() open the relation they
        measure, so a query that only reads sizes still takes ACCESS SHARE on
        every table it touches — and queues behind whoever holds ACCESS
        EXCLUSIVE. That makes these probes stallable by somebody else's DDL,
        which would be tolerable if they lived anywhere else: they feed the
        workspace overview and the Health panel, the pages you pass through on
        the way to the Locks panel. Unguarded, the one moment you need to see
        who is blocking whom is the moment these pages stop loading.

        So bound the wait and report it. The caller degrades its own card
        (Health keeps its other cards; the overview falls back to '—') rather
        than showing an empty list, which would read as "nothing to see"."""
        with conn.cursor() as cur:
            try:
                cur.execute("SET lock_timeout = %s", [SIZES_LOCK_TIMEOUT])
                yield cur
            except pg_errors.LockNotAvailable as exc:
                raise EngineError(_sizes_lock_error(SIZES_LOCK_TIMEOUT)) from exc
            finally:
                # Session-wide (SET LOCAL would need a transaction and _connect
                # is autocommit), so undo it: session() holds this connection
                # open for the probes that follow.
                with contextlib.suppress(psycopg2.Error):
                    cur.execute("RESET lock_timeout")

    def test(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    def list_tables(self) -> list[Table]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_TABLES_SQL)
                return [
                    Table(schema=row[0], name=row[1], rows=row[2])
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
                        comment=row[4],
                        # attgenerated: '' plain, 's' STORED, 'v' VIRTUAL (PG18+)
                        generated={"s": "stored", "v": "virtual"}.get(row[5] or ""),
                        generation_expr=row[6],
                    )
                    for row in cur.fetchall()
                ]

    def table_comment(self, schema: str, table: str) -> str | None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(TABLE_COMMENT_SQL, (schema, table))
                row = cur.fetchone()
                return row[0] if row else None

    def jsonb_shape(self, schema: str, table: str, column: str, *,
                    sample: int = 500, timeout_ms: int = 15000) -> JsonbShape:
        # Validate the column exists and is actually JSON — the name is an
        # identifier (can't be bound), so check it against the real table before
        # composing it into the query rather than trust the request.
        cols = {c.name: c for c in self.list_columns(schema, table)}
        col = cols.get(column)
        if col is None:
            raise EngineError(_("No such column: %(name)s") % {"name": column})
        if col.type not in ("jsonb", "json"):
            raise EngineError(
                _("%(name)s is not a JSON column.") % {"name": column})
        query = sql.SQL(
            "SELECT {col} FROM {sch}.{tbl} WHERE {col} IS NOT NULL LIMIT %s"
        ).format(col=sql.Identifier(column),
                 sch=sql.Identifier(schema), tbl=sql.Identifier(table))
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute(query, (sample,))
                    # psycopg2 parses jsonb/json into Python objects for us; guard
                    # the case where an adapter left it as text.
                    docs = [json.loads(r[0]) if isinstance(r[0], str) else r[0]
                            for r in cur.fetchall()]
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()  # read-only: never persist, even the SET statements
        root_types, keys, max_depth = _summarise_json(docs)
        gin = _gin_indexes_for(self.list_indexes(schema, table), column)
        return JsonbShape(column=column, sampled=len(docs), root_types=root_types,
                          keys=keys, max_depth=max_depth, gin_indexes=gin)

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        # Identifiers come from the schema, but compose them safely anyway so a
        # table named `users; DROP …` can never break out of the query.
        query = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (limit,))
                columns = [d.name for d in cur.description]
                rows = cur.fetchall()
        return Preview(columns=columns, rows=rows)

    def run_query(self, sql_text: str, *, max_rows: int = 1000,
                  timeout_ms: int = 15000, read_only: bool = True,
                  lock_timeout: str | None = None) -> QueryResult:
        with self._connect() as conn:
            # Need a real transaction to scope READ ONLY + statement_timeout.
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    if read_only:
                        # Must be the first statement in the transaction. The DB
                        # then rejects any write itself — no fragile SQL scanning.
                        cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    if lock_timeout is not None:
                        # Hand-written DDL queues the same way the buttons do
                        # (see _execute). SET LOCAL, so it dies with this
                        # transaction — nothing to reset afterwards.
                        cur.execute("SET LOCAL lock_timeout = %s", [lock_timeout])
                    t0 = time.perf_counter()
                    cur.execute(sql_text)
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    if cur.description is not None:
                        # Fetch one extra to know whether more rows existed.
                        fetched = cur.fetchmany(max_rows + 1)
                        truncated = len(fetched) > max_rows
                        rows = fetched[:max_rows]
                        columns = [d.name for d in cur.description]
                        rowcount = len(rows)
                    else:
                        columns, rows, truncated, rowcount = [], [], False, cur.rowcount
            except pg_errors.LockNotAvailable as exc:
                conn.rollback()
                raise EngineError(_lock_error(lock_timeout)) from exc
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            if read_only:
                # Never persist — even a statement that slipped past READ ONLY.
                conn.rollback()
            else:
                # Write mode: the statement succeeded, so make it durable.
                conn.commit()
        return QueryResult(
            columns=columns, rows=rows, rowcount=rowcount,
            truncated=truncated, duration_ms=duration_ms,
        )

    def filter_rows(self, schema: str, table: str, filters: list[dict], *,
                    limit: int = 1000, timeout_ms: int = 15000) -> QueryResult:
        """Run a read-only `SELECT * ... WHERE <conds>` built from the filter
        builder. Each filter is {column, op, value}; conditions are ANDed.
        Columns are validated against the real table and composed with
        sql.Identifier, values bound as placeholders — nothing is interpolated,
        so this is just a guided, parameterised query."""
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
                conds.append(sql.SQL("{} {} {}").format(
                    sql.Identifier(col), sql.SQL(sql_op), sql.Placeholder()))
            else:
                conds.append(sql.SQL("{} {}").format(
                    sql.Identifier(col), sql.SQL(sql_op)))
        where = sql.SQL("")
        if conds:
            where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conds)
        query = sql.SQL("SELECT * FROM {}.{}{} LIMIT %s").format(
            sql.Identifier(schema), sql.Identifier(table), where)
        params.append(limit + 1)  # one extra row tells us the result was capped

        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    t0 = time.perf_counter()
                    cur.execute(query, params)
                    duration_ms = int((time.perf_counter() - t0) * 1000)
                    fetched = cur.fetchmany(limit + 1)
                    truncated = len(fetched) > limit
                    rows = fetched[:limit]
                    columns = [d.name for d in cur.description]
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()  # read-only: never persist
        return QueryResult(columns=columns, rows=rows, rowcount=len(rows),
                           truncated=truncated, duration_ms=duration_ms)

    def import_csv(self, schema: str, table: str, fileobj, *,
                   encoding: str = "utf-8-sig") -> int:
        """Append rows from a CSV (with a header row) into an existing table via
        COPY. Columns are matched by *header name* — validated against the table,
        so the file's column order or a subset is fine. The whole import runs in
        one transaction: a bad row (type/constraint) rolls all of it back, so the
        table is never left half-loaded. Returns the number of rows imported.

        COPY's CSV defaults apply: an unquoted empty field is NULL, a quoted ""
        is an empty string. The header line is skipped by COPY itself."""
        head = fileobj.readline()
        fileobj.seek(0)
        if not head:
            raise EngineError(_("The CSV file is empty."))
        if isinstance(head, bytes):
            head = head.decode(encoding, errors="replace")
        csv_cols = [c.strip() for c in next(csv.reader([head]))]
        valid = {c.name for c in self.list_columns(schema, table)}
        unknown = [c for c in csv_cols if c not in valid]
        if unknown:
            raise EngineError(_("CSV header has columns not in %(table)s: %(cols)s") % {
                "table": table, "cols": ", ".join(unknown)})

        copy_sql = sql.SQL(
            "COPY {}.{} ({}) FROM STDIN WITH (FORMAT csv, HEADER true)").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.SQL(", ").join(sql.Identifier(c) for c in csv_cols))
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET client_encoding TO 'UTF8'")
                    cur.copy_expert(copy_sql, fileobj)
                    count = cur.rowcount
                conn.commit()
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
        return count

    def stream_query(self, sql_text: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Run read-only SQL and stream the *full* result for a file export
        (unlike run_query's 1000-row display cap). Yields the column-name list
        first, then one row tuple at a time, pulled through a server-side named
        cursor so a large result is fetched in batches — never buffered whole in
        memory, the half of "ad-hoc SQL but safe" that matters for exports.

        Read-only is enforced the same way as run_query (the DB rejects writes,
        no SQL scanning) and statement_timeout still applies. The connection is
        held open for the life of the generator — Django pulls rows as it writes
        the response body — then rolled back and closed when iteration ends.
        Because the caller may be partway through writing an HTTP response, an
        error mid-stream can't become a clean error page; only the first
        next() (which runs the query) raises EngineError for the view to catch.
        """
        return self._stream(sql_text, timeout_ms=timeout_ms, max_rows=max_rows)

    def stream_table(self, schema: str, table: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Stream a whole table for a CSV/JSON export. Identifiers are composed
        with sql.Identifier (safe quoting), then streamed through the same
        read-only server-side cursor path as stream_query."""
        query = sql.SQL("SELECT * FROM {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table))
        return self._stream(query, timeout_ms=timeout_ms, max_rows=max_rows)

    def _stream(self, query, *, timeout_ms: int, max_rows: int):
        """Shared generator behind stream_query / stream_table: yield the header
        column list, then row tuples, from a read-only server-side cursor.
        `query` is a str or a psycopg2 Composed."""
        with self._connect() as conn:
            # A named (server-side) cursor needs a real transaction, which also
            # scopes READ ONLY + statement_timeout.
            conn.autocommit = False
            try:
                with conn.cursor() as setup:
                    setup.execute("SET TRANSACTION READ ONLY")
                    setup.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                # Named cursor: rows stream from the backend in itersize batches.
                with conn.cursor(name="cli2ui_export") as cur:
                    cur.itersize = 2000
                    cur.execute(query)
                    # A server-side cursor only populates .description after the
                    # first fetch, so pull a batch before emitting the header.
                    batch = cur.fetchmany(cur.itersize)
                    if cur.description is None:
                        yield []          # not a row-returning statement
                        return
                    yield [d.name for d in cur.description]   # header first
                    sent = 0
                    while batch:
                        for row in batch:
                            yield row
                            sent += 1
                            if sent >= max_rows:
                                return
                        batch = cur.fetchmany(cur.itersize)
            except psycopg2.Error as exc:
                raise EngineError(_clean(exc)) from exc
            finally:
                conn.rollback()           # read-only: never persist anything

    def explain(self, sql_text: str, *, analyze: bool = False,
                timeout_ms: int = 15000) -> str:
        opts = "ANALYZE, " if analyze else ""
        prefix = f"EXPLAIN ({opts}FORMAT TEXT) "
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    # Read-only even for ANALYZE: explaining a write executes it,
                    # so the read-only transaction is what keeps ANALYZE safe.
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute(prefix + sql_text)
                    lines = [row[0] for row in cur.fetchall()]
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()
        return "\n".join(lines)

    def explain_json(self, sql_text: str, *, analyze: bool = False,
                     timeout_ms: int = 15000) -> PlanNode:
        opts = "ANALYZE, " if analyze else ""
        prefix = f"EXPLAIN ({opts}FORMAT JSON) "
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    # Same read-only guard as explain(): ANALYZE would run the
                    # query, so the read-only transaction is what keeps it safe.
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute(prefix + sql_text)
                    payload = cur.fetchone()[0]
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()
        return _parse_plan(payload)

    # --- activity / sessions ---------------------------------------------

    def list_activity(self) -> list[Activity]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(ACTIVITY_SQL)
                return [
                    Activity(
                        pid=row[0], user=row[1], database=row[2], app=row[3],
                        client=row[4], state=row[5], wait=row[6],
                        blocked_by=row[7] or [], query_secs=row[8], query=row[9],
                        is_self=row[10],
                    )
                    for row in cur.fetchall()
                ]

    def connection_headroom(self) -> ConnectionHeadroom:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(HEADROOM_SQL)
                used, active, idle, idle_in_txn, max_conn, reserved = cur.fetchone()
        return ConnectionHeadroom(
            used=used, max=max_conn, reserved=reserved,
            by_state={
                "active": active,
                "idle": idle,
                "idle in transaction": idle_in_txn,
            },
        )

    def cancel_backend(self, pid: int) -> bool:
        return self._signal("pg_cancel_backend", pid)

    def terminate_backend(self, pid: int) -> bool:
        return self._signal("pg_terminate_backend", pid)

    def _signal(self, fn: str, pid: int) -> bool:
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("SELECT {}(%s)").format(sql.Identifier(fn)), [pid]
                    )
                    return bool(cur.fetchone()[0])
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc

    def list_blocking(self) -> list[LockWait]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(BLOCKING_SQL)
                rows = cur.fetchall()
                cur.execute(ACTIVITY_MAP_SQL)
                info = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
        waits = []
        for row in rows:
            blockers = [
                Blocker(
                    pid=p,
                    user=info.get(p, (None, None, ""))[0],
                    state=info.get(p, (None, None, ""))[1],
                    query=info.get(p, (None, None, ""))[2],
                )
                for p in (row[7] or [])
            ]
            waits.append(
                LockWait(
                    blocked_pid=row[0], blocked_user=row[1], blocked_query=row[2],
                    wait_secs=row[3], lock_type=row[4], lock_mode=row[5],
                    object=row[6], blockers=blockers,
                )
            )
        return waits

    # --- replication -----------------------------------------------------

    def replication_status(self) -> ReplicationStatus:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(REPLICATION_STATUS_SQL)
                r = cur.fetchone()
        return ReplicationStatus(
            wal_level=r[0], max_wal_senders=r[1], max_replication_slots=r[2],
            hot_standby=r[3], archive_mode=r[4], current_lsn=r[5], is_standby=r[6],
        )

    def list_standbys(self) -> list[Standby]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(STANDBYS_SQL)
                return [
                    Standby(
                        pid=row[0], user=row[1], app=row[2], client=row[3],
                        state=row[4], sync_state=row[5], sent_lsn=row[6],
                        replay_lsn=row[7], lag_bytes=row[8],
                        write_lag_s=row[9], flush_lag_s=row[10],
                        replay_lag_s=row[11],
                    )
                    for row in cur.fetchall()
                ]

    def list_replication_slots(self) -> list[ReplicationSlot]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SLOTS_SQL)
                return [
                    ReplicationSlot(
                        name=row[0], slot_type=row[1], database=row[2],
                        active=row[3], restart_lsn=row[4], wal_status=row[5],
                    )
                    for row in cur.fetchall()
                ]

    def create_replication_slot(self, name: str) -> None:
        # The slot name is a bound value (not an identifier), so this is
        # injection-safe; Postgres rejects an ill-formed name with a clear error.
        self._call("pg_create_physical_replication_slot", name)

    def drop_replication_slot(self, name: str) -> None:
        self._call("pg_drop_replication_slot", name)

    def replication_recipe(self, status, slots) -> ReplicationRecipe:
        c = self.connection
        # What the primary still needs to accept a physical standby. Order
        # matters for readability; each is a postmaster (restart) setting.
        conf = []
        if status.wal_level == "minimal":
            conf.append(("wal_level", "replica"))
        if status.max_wal_senders == 0:
            conf.append(("max_wal_senders", "10"))
        if status.max_replication_slots == 0:
            conf.append(("max_replication_slots", "10"))
        if status.hot_standby == "off":
            conf.append(("hot_standby", "on"))

        # Reuse an existing physical slot if there's one; otherwise suggest a name.
        physical = [s for s in slots if s.slot_type == "physical"]
        if physical:
            slot_name, slot_exists = physical[0].name, True
        else:
            slot_name, slot_exists = "standby_1", False

        datadir = "/path/to/standby/datadir"
        # primary_conninfo carries no password — the standby should use .pgpass or
        # PGPASSWORD so the secret never lands in a config file.
        conninfo = f"host={c.host} port={c.port} user={c.user}"
        basebackup = (
            f"pg_basebackup -h {c.host} -p {c.port} -U {c.user} "
            f"-D {datadir} -R -X stream --slot={slot_name}"
        )
        create_slot_sql = (
            f"SELECT pg_create_physical_replication_slot('{slot_name}');"
        )
        return ReplicationRecipe(
            primary_host=c.host, primary_port=c.port, primary_user=c.user,
            slot_name=slot_name, slot_exists=slot_exists, conf_changes=conf,
            create_slot_sql=create_slot_sql, basebackup_cmd=basebackup,
            primary_conninfo=conninfo, standby_datadir=datadir,
        )

    def _call(self, fn: str, *args) -> None:
        """Run SELECT fn(%s, …) for a side-effecting function, mapping driver
        errors to EngineError. Args are bound values, never spliced."""
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(args))
        stmt = sql.SQL("SELECT {}({})").format(sql.Identifier(fn), placeholders)
        if getattr(self, "_preview", None) is not None:
            # 値はバインドで渡すので、見せる側でも同じ形に組んでから文字列にする。
            with self._connect() as conn:
                with conn.cursor() as cur:
                    self._preview.statements.append(
                        cur.mogrify(stmt, args).decode(conn.encoding or "utf-8"))
            return

        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(stmt, args)
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc

    # --- catalog browsing ------------------------------------------------

    def list_databases(self) -> list[Database]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_DATABASES_SQL)
                return [
                    Database(name=row[0], owner=row[1], encoding=row[2], size=row[3])
                    for row in cur.fetchall()
                ]

    def list_schemas(self) -> list[Schema]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_SCHEMAS_SQL)
                return [Schema(name=row[0], owner=row[1]) for row in cur.fetchall()]

    def list_roles(self) -> list[Role]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_ROLES_SQL)
                return [_role(row) for row in cur.fetchall()]

    def list_extensions(self) -> list[Extension]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(EXTENSIONS_SQL)
                return [
                    Extension(name=row[0], installed_version=row[1],
                              default_version=row[2], schema=row[3], comment=row[4])
                    for row in cur.fetchall()
                ]

    # --- catalog mutations -----------------------------------------------

    def create_schema(self, name: str) -> None:
        self._execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))

    def drop_schema(self, name: str, cascade: bool = False) -> None:
        stmt = sql.SQL("DROP SCHEMA {}{}").format(
            sql.Identifier(name),
            sql.SQL(" CASCADE") if cascade else sql.SQL(""),
        )
        self._execute(stmt)

    def create_role(
        self,
        name: str,
        *,
        login: bool = False,
        password: str | None = None,
        superuser: bool = False,
        createdb: bool = False,
        createrole: bool = False,
    ) -> None:
        opts = [sql.SQL("LOGIN") if login else sql.SQL("NOLOGIN")]
        if superuser:
            opts.append(sql.SQL("SUPERUSER"))
        if createdb:
            opts.append(sql.SQL("CREATEDB"))
        if createrole:
            opts.append(sql.SQL("CREATEROLE"))
        if password:
            opts.append(sql.SQL("PASSWORD {}").format(sql.Literal(password)))
        stmt = sql.SQL("CREATE ROLE {} WITH {}").format(
            sql.Identifier(name), sql.SQL(" ").join(opts)
        )
        self._execute(stmt)

    def drop_role(self, name: str) -> None:
        self._execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))

    # --- catalog alterations ---------------------------------------------

    def rename_schema(self, old: str, new: str) -> None:
        self._execute(sql.SQL("ALTER SCHEMA {} RENAME TO {}").format(
            sql.Identifier(old), sql.Identifier(new)))

    def alter_schema_owner(self, name: str, owner: str) -> None:
        self._execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
            sql.Identifier(name), sql.Identifier(owner)))

    def rename_role(self, old: str, new: str) -> None:
        if old == self.connection.user:
            raise EngineError(
                "Can't rename the role this connection is logged in as.")
        self._execute(sql.SQL("ALTER ROLE {} RENAME TO {}").format(
            sql.Identifier(old), sql.Identifier(new)))

    def alter_role(self, name: str, *, login: bool, superuser: bool,
                   createdb: bool, createrole: bool,
                   password: str | None = None) -> None:
        opts = [
            sql.SQL("LOGIN") if login else sql.SQL("NOLOGIN"),
            sql.SQL("SUPERUSER") if superuser else sql.SQL("NOSUPERUSER"),
            sql.SQL("CREATEDB") if createdb else sql.SQL("NOCREATEDB"),
            sql.SQL("CREATEROLE") if createrole else sql.SQL("NOCREATEROLE"),
        ]
        if password:
            opts.append(sql.SQL("PASSWORD {}").format(sql.Literal(password)))
        self._execute(sql.SQL("ALTER ROLE {} WITH {}").format(
            sql.Identifier(name), sql.SQL(" ").join(opts)))

    # --- databases -------------------------------------------------------

    def create_database(self, name: str, *, template: str | None = None,
                        owner: str | None = None,
                        encoding: str | None = None) -> None:
        opts = []
        if owner:
            opts.append(sql.SQL("OWNER {}").format(sql.Identifier(owner)))
        if template:
            opts.append(sql.SQL("TEMPLATE {}").format(sql.Identifier(template)))
        if encoding:
            opts.append(sql.SQL("ENCODING {}").format(sql.Literal(encoding)))
        stmt = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        if opts:
            stmt = stmt + sql.SQL(" WITH ") + sql.SQL(" ").join(opts)
        self._execute_admin(stmt)

    def drop_database(self, name: str, *, force: bool = False) -> None:
        if name == self.connection.dbname:
            raise EngineError(
                "Can't drop the database this connection is using — "
                "connect to another database first.")
        stmt = sql.SQL("DROP DATABASE {}{}").format(
            sql.Identifier(name),
            sql.SQL(" WITH (FORCE)") if force else sql.SQL(""),
        )
        self._execute_admin(stmt)

    def rename_database(self, old: str, new: str) -> None:
        if old == self.connection.dbname:
            raise EngineError(
                "Can't rename the database this connection is using — "
                "connect to another database first.")
        self._execute_admin(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(
            sql.Identifier(old), sql.Identifier(new)))

    def _maintenance_dbname(self) -> str:
        """A database to connect to for database-level admin: never the target,
        so CREATE/DROP/ALTER DATABASE (and TEMPLATE copies) aren't blocked by our
        own connection. 'postgres' exists on virtually every server; template1 is
        the fallback."""
        for db in ("postgres", "template1"):
            try:
                with self._connect(dbname=db):
                    return db
            except EngineError:
                continue
        raise EngineError(
            "Cannot reach a maintenance database (postgres / template1) to "
            "run database-level commands.")

    def _execute_admin(self, statement) -> None:
        """Run a database-level statement from a maintenance DB. These can't run
        inside a transaction; _connect is autocommit, so each runs standalone.

        No lock_timeout, unlike _execute: CREATE / DROP / ALTER DATABASE take no
        table lock, so they can't build a queue in front of other queries — they
        just fail outright when someone else is connected."""
        with self._connect(dbname=self._maintenance_dbname()) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(statement)
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc

    # --- indexes ---------------------------------------------------------

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_INDEXES_SQL, (schema, table))
                return [
                    Index(name=row[0], method=row[1], unique=row[2],
                          primary=row[3], definition=row[4], size=row[5],
                          valid=row[6])
                    for row in cur.fetchall()
                ]

    def create_index(self, schema: str, table: str, columns: list[str], *,
                     method: str = "btree", unique: bool = False,
                     name: str | None = None) -> None:
        # Whitelist the requested columns against the table's real columns:
        # column names are identifiers (can't be bound), so verify they exist
        # rather than splice an unknown name into DDL.
        valid = {c.name for c in self.list_columns(schema, table)}
        unknown = [c for c in columns if c not in valid]
        if unknown:
            raise EngineError(_("No such column(s): %(cols)s") % {"cols": ', '.join(unknown)})
        # CONCURRENTLY can't run inside a transaction — fine here, _connect()
        # is autocommit, so _execute() runs it as a standalone statement. No
        # lock_timeout: the build blocks nobody, and cutting its wait short is
        # exactly what leaves an invalid index behind (see _execute).
        self._execute(build_create_index_sql(
            schema, table, columns, method=method, unique=unique,
            name=name, concurrently=True,
        ), lock_timeout=None)

    def drop_index(self, schema: str, name: str, table: str | None = None) -> None:
        # `table` is unused: Postgres indexes live in a schema, so name+schema is
        # enough. It's in the signature only for parity with MySQL (see base).
        # lock_timeout=None for the same reason as create_index: an interrupted
        # concurrent drop can leave the index behind, marked invalid.
        self._execute(sql.SQL("DROP INDEX CONCURRENTLY {}.{}").format(
            sql.Identifier(schema), sql.Identifier(name)), lock_timeout=None)

    # --- table-level operations ------------------------------------------

    def rename_table(self, schema: str, table: str, new_name: str) -> None:
        self._execute(sql.SQL("ALTER TABLE {}.{} RENAME TO {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.Identifier(new_name)))

    def truncate_table(self, schema: str, table: str) -> None:
        self._execute(sql.SQL("TRUNCATE TABLE {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)))

    def drop_table(self, schema: str, table: str) -> None:
        # Non-CASCADE on purpose: if anything depends on the table (views,
        # foreign keys) the drop fails and we surface why, rather than quietly
        # taking dependents down with it — same low-risk stance as DROP SCHEMA.
        self._execute(sql.SQL("DROP TABLE {}.{}").format(
            sql.Identifier(schema), sql.Identifier(table)))

    # --- column-level operations (ALTER TABLE) ---------------------------

    def add_column(self, schema: str, table: str, name: str, col_type: str, *,
                   nullable: bool = True, default: str | None = None) -> None:
        # The type is the one bare keyword we splice, so it must match the
        # allow-list exactly (same rule as index methods); everything else is a
        # quoted identifier or a bound literal.
        if col_type not in COLUMN_TYPES:
            raise EngineError(
                f"Unsupported column type: {col_type}. Pick one of the listed types.")
        stmt = sql.SQL("ALTER TABLE {}.{} ADD COLUMN {} {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.Identifier(name), sql.SQL(col_type))
        if default not in (None, ""):
            # A literal default (e.g. 0, '', false) — bound, not spliced. The DB
            # casts it to the column type; an expression like now() isn't supported.
            stmt = stmt + sql.SQL(" DEFAULT {}").format(sql.Literal(default))
        if not nullable:
            stmt = stmt + sql.SQL(" NOT NULL")
        self._execute(stmt)

    def rename_column(self, schema: str, table: str, old: str, new: str) -> None:
        self._require_column(schema, table, old)
        self._execute(sql.SQL("ALTER TABLE {}.{} RENAME COLUMN {} TO {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.Identifier(old), sql.Identifier(new)))

    def drop_column(self, schema: str, table: str, name: str) -> None:
        # Non-CASCADE, matching drop_table: dependents block the drop loudly.
        self._require_column(schema, table, name)
        self._execute(sql.SQL("ALTER TABLE {}.{} DROP COLUMN {}").format(
            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(name)))

    def alter_column_type(self, schema: str, table: str, name: str,
                          new_type: str) -> None:
        if new_type not in COLUMN_TYPES:
            raise EngineError(
                f"Unsupported column type: {new_type}. Pick one of the listed types.")
        self._require_column(schema, table, name)
        # Build an explicit USING cast from safe parts (identifier + allow-listed
        # type) so casts that aren't implicit — e.g. text→integer — still work
        # without accepting a free-text USING expression.
        self._execute(sql.SQL(
            "ALTER TABLE {}.{} ALTER COLUMN {} TYPE {} USING {}::{}").format(
            sql.Identifier(schema), sql.Identifier(table), sql.Identifier(name),
            sql.SQL(new_type), sql.Identifier(name), sql.SQL(new_type)))

    def set_column_null(self, schema: str, table: str, name: str, *,
                        nullable: bool) -> None:
        self._require_column(schema, table, name)
        action = sql.SQL("DROP NOT NULL") if nullable else sql.SQL("SET NOT NULL")
        self._execute(sql.SQL("ALTER TABLE {}.{} ALTER COLUMN {} {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.Identifier(name), action))

    def set_column_default(self, schema: str, table: str, name: str,
                           default: str | None) -> None:
        self._require_column(schema, table, name)
        if default in (None, ""):
            action = sql.SQL("DROP DEFAULT")
        else:
            # Bound literal, not spliced; the DB casts it to the column type.
            action = sql.SQL("SET DEFAULT {}").format(sql.Literal(default))
        self._execute(sql.SQL("ALTER TABLE {}.{} ALTER COLUMN {} {}").format(
            sql.Identifier(schema), sql.Identifier(table),
            sql.Identifier(name), action))

    def _require_column(self, schema: str, table: str, name: str) -> None:
        if name not in {c.name for c in self.list_columns(schema, table)}:
            raise EngineError(_("No such column: %(name)s") % {"name": name})

    # --- backup (pg_dump) ------------------------------------------------

    def dump_database(self, dbname: str, *, fmt: str = "plain") -> Dump:
        """Dump a whole database with pg_dump, returned as a downloadable blob."""
        return self._run_pg_dump(["-d", dbname], fmt, base=dbname)

    def dump_table(self, schema: str, table: str, *, fmt: str = "plain") -> Dump:
        """Dump a single table (-t) from the connection's current database."""
        pattern = f'{self._dump_ident(schema)}.{self._dump_ident(table)}'
        return self._run_pg_dump(
            ["-d", self.connection.dbname, "-t", pattern], fmt,
            base=f"{schema}.{table}")

    @staticmethod
    def _dump_ident(name: str) -> str:
        # pg_dump -t takes a pattern; double-quote so the identifier is matched
        # literally (no case-folding / wildcard interpretation), doubling any
        # embedded quote — same rule as a quoted SQL identifier.
        return '"' + name.replace('"', '""') + '"'

    def _run_pg_dump(self, scope: list[str], fmt: str, *, base: str) -> Dump:
        if fmt not in DUMP_FORMATS:
            raise EngineError(_("Unknown dump format: %(fmt)s") % {"fmt": fmt})
        flag, ext, ctype = DUMP_FORMATS[fmt]
        conn = self.connection
        argv = [
            "pg_dump",
            "-h", conn.host, "-p", str(conn.port), "-U", conn.user,
            "--no-password", flag, *scope,
        ]
        env = {**os.environ, "PGPASSWORD": conn.password or ""}
        try:
            # No shell, fixed argv, password via env (never on the command line).
            proc = subprocess.run(  # nosec B603 B607
                argv, capture_output=True, env=env, timeout=DUMP_TIMEOUT)
        except FileNotFoundError as exc:
            raise EngineError(
                "pg_dump not found — install the postgresql-client package "
                "(it ships in the Docker image).") from exc
        except subprocess.TimeoutExpired as exc:
            raise EngineError(_("pg_dump timed out.")) from exc
        if proc.returncode != 0:
            raise EngineError(_tool_error(proc.stderr, "pg_dump failed."))
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return Dump(filename=f"{base}-{stamp}.{ext}",
                    content_type=ctype, data=proc.stdout)

    def restore(self, dbname: str, data: bytes) -> None:
        """Restore dump bytes into an existing database (convenience wrapper —
        streams from an in-memory buffer)."""
        self.restore_stream(dbname, io.BytesIO(data))

    def restore_stream(self, dbname: str, fileobj) -> None:
        """Restore a dump read from a file-like object into an existing database,
        streaming it to the client tool's stdin in chunks instead of loading the
        whole dump into memory. The format is detected from the leading bytes —
        a custom-format archive starts with the 'PGDMP' marker and goes through
        pg_restore; anything else is treated as plain SQL piped through psql
        (stopping on the first error)."""
        head = fileobj.read(5)
        is_custom = head[:5] == b"PGDMP"
        conn = self.connection
        common = ["-h", conn.host, "-p", str(conn.port), "-U", conn.user,
                  "--no-password", "-d", dbname]
        if is_custom:
            # --exit-on-error makes pg_restore stop and fail on the first error
            # instead of limping on and reporting a count, matching psql below.
            argv = ["pg_restore", "--exit-on-error", *common]
            tool = "pg_restore"
        else:
            # ON_ERROR_STOP makes psql exit non-zero on the first failed
            # statement; --single-transaction wraps the whole restore in one
            # transaction so a failure leaves the database untouched (all or
            # nothing), on top of the new-database drop the caller does.
            argv = ["psql", *common, "-v", "ON_ERROR_STOP=1",
                    "--single-transaction"]
            tool = "psql"
        env = {**os.environ, "PGPASSWORD": conn.password or ""}
        try:
            # No shell; the dump is fed on stdin in chunks, never written to disk.
            proc = subprocess.Popen(  # nosec B603 B607
                argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, env=env)
        except FileNotFoundError as exc:
            raise EngineError(
                f"{tool} not found — install the postgresql-client package "
                "(it ships in the Docker image).") from exc
        # Drain stderr on a thread so a chatty tool can't fill the pipe and
        # deadlock us while we're busy writing stdin.
        err_chunks: list[bytes] = []
        drainer = threading.Thread(
            target=lambda: err_chunks.extend(iter(lambda: proc.stderr.read(8192), b"")),
            daemon=True)
        drainer.start()
        try:
            with proc.stdin as stdin:
                if head:
                    stdin.write(head)
                for chunk in iter(lambda: fileobj.read(65536), b""):
                    stdin.write(chunk)
            proc.wait(timeout=RESTORE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise EngineError(_("restore timed out.")) from exc
        except BrokenPipeError:
            # The tool exited early (e.g. an error mid-stream); fall through to
            # report it from the captured stderr below.
            proc.wait()
        finally:
            drainer.join(timeout=5)
        if proc.returncode != 0:
            raise EngineError(_tool_error(b"".join(err_chunks), "restore failed."))

    def _execute(self, statement, *,
                 lock_timeout: str | None = DDL_LOCK_TIMEOUT) -> None:
        """Run a composed DDL statement, mapping driver errors to EngineError.

        Every ALTER / DROP / TRUNCATE routed through here takes an ACCESS
        EXCLUSIVE lock, and a DDL statement *waiting* for that lock is the
        classic self-inflicted outage: it queues behind whatever transaction is
        already on the table, and then every later statement — plain SELECTs
        included — queues behind the waiting DDL. One clicked button can stall a
        whole table until the connection pool runs dry. So we bound the wait: if
        the lock isn't free within lock_timeout the statement gives up, nothing
        was changed, and no queue ever forms.

        It bounds the *wait*, not the hold — a statement that rewrites the table
        (ALTER COLUMN TYPE) still keeps the lock for as long as the rewrite runs.

        Pass lock_timeout=None for CONCURRENTLY index work: it takes a weak lock
        that blocks nobody, but legitimately waits on other transactions, and
        timing it out is what leaves behind the invalid index it exists to avoid.

        Inside engine.preview() nothing is sent: the composed statement (and the
        lock guard that would precede it) is rendered and recorded instead. The
        rendering needs a connection — identifier quoting is the server's
        business — but no statement is executed on it."""
        if getattr(self, "_preview", None) is not None:
            with self._connect() as conn:
                if lock_timeout is not None:
                    self._preview.statements.append(
                        f"SET lock_timeout = '{lock_timeout}'")
                self._preview.statements.append(statement.as_string(conn))
            return

        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    if lock_timeout is not None:
                        cur.execute("SET lock_timeout = %s", [lock_timeout])
                    cur.execute(statement)
                except pg_errors.LockNotAvailable as exc:
                    raise EngineError(_lock_error(lock_timeout)) from exc
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                finally:
                    if lock_timeout is not None:
                        # SET LOCAL would need a transaction and _connect is
                        # autocommit, so the setting is session-wide: undo it in
                        # case this is session()'s held connection, which outlives
                        # the statement.
                        with contextlib.suppress(psycopg2.Error):
                            cur.execute("RESET lock_timeout")

    # --- health ----------------------------------------------------------

    def table_sizes(self, limit: int = 20) -> list[TableSize]:
        """Largest tables by total on-disk footprint (\\dt+). Size-probing, so
        it runs lock-guarded — see _size_probe_cursor."""
        with self._connect() as conn:
            with self._size_probe_cursor(conn) as cur:
                cur.execute(TABLE_SIZES_SQL, [limit])
                return [
                    TableSize(schema=row[0], name=row[1], total_bytes=row[2],
                              total=row[3], table=row[4], index=row[5])
                    for row in cur.fetchall()
                ]

    def unused_indexes(self) -> list[UnusedIndex]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(UNUSED_INDEXES_SQL)
                return [
                    UnusedIndex(schema=row[0], table=row[1], name=row[2],
                                scans=row[3], bytes=row[4], size=row[5])
                    for row in cur.fetchall()
                ]

    def invalid_indexes(self) -> list[InvalidIndex]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(INVALID_INDEXES_SQL)
                return [
                    InvalidIndex(schema=row[0], table=row[1], name=row[2],
                                 ready=row[3], live=row[4], building=row[5],
                                 bytes=row[6], size=row[7])
                    for row in cur.fetchall()
                ]

    def write_impact(self, schema: str, table: str) -> WriteImpact:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(WRITE_IMPACT_SQL, (schema, table))
                row = cur.fetchone()
        if row is None:
            raise EngineError(_("That table no longer exists."))
        rows, last_analyze, last_autoanalyze, referenced_by = row
        analyzed = max(filter(None, (last_analyze, last_autoanalyze)), default=None)
        # reltuples = -1 は「まだ ANALYZE していない」（PG14+）。それ以前は 0 が
        # 同じ意味を持ちうるので、統計時刻が無いことと併せて「不明」にする。
        unknown = rows is None or rows < 0 or (rows == 0 and analyzed is None)
        return WriteImpact(
            rows=None if unknown else rows,
            estimated=True,
            analyzed=analyzed.date().isoformat() if analyzed else None,
            referenced_by=referenced_by or [],
        )

    def null_slips(self) -> list[NullSlip]:
        with self._connect() as conn:
            # NULLS NOT DISTINCT は 15 で入った。14 以前は列そのものが無く、SQL に
            # 書くと parse で落ちるので、版でクエリを選ぶ（14 以前は全件が対象）。
            query = (NULL_SLIP_SQL_PG15 if conn.server_version >= 150000
                     else NULL_SLIP_SQL_LEGACY)
            with conn.cursor() as cur:
                cur.execute(query)
                return [
                    NullSlip(schema=row[0], table=row[1], index=row[2],
                             constraint=row[3], columns=row[4] or "",
                             nullable=row[5] or "")
                    for row in cur.fetchall()
                ]

    def null_slip_count(self, schema: str, table: str, index: str,
                        timeout_ms: int = 15000) -> NullSlipCount:
        # 列は要求を信じずカタログから引き直す（要求が名指しするのは索引だけ）。
        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute(NULL_SLIP_RESOLVE_SQL, (schema, table, index))
                    row = cur.fetchone()
                    if row is None or not row[0]:
                        conn.rollback()
                        raise EngineError(_(
                            "This index no longer looks like a composite UNIQUE "
                            "with a nullable column — it may have changed since "
                            "the panel loaded."))
                    cols = row[0]

                    # GROUP BY は NULL 同士を同じ組として扱う ── 一意索引がしない
                    # 扱い方で、その差がまさに漏れている重複。キーに NULL を 1 つも
                    # 含まない行は索引が止めているので、母数から外す。
                    keys = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
                    any_null = sql.SQL(" OR ").join(
                        sql.SQL("{} IS NULL").format(sql.Identifier(c)) for c in cols)
                    query = sql.SQL(
                        "SELECT count(*) AS groups, "
                        "       coalesce(sum(n), 0) - count(*) AS extra, "
                        "       coalesce((SELECT count(*) FROM {tbl} WHERE {any_null}), 0) AS checked "
                        "FROM (SELECT count(*) AS n FROM {tbl} WHERE {any_null} "
                        "      GROUP BY {keys} HAVING count(*) > 1) g"
                    ).format(tbl=sql.Identifier(schema, table),
                             any_null=any_null, keys=keys)
                    cur.execute(query)
                    groups, extra, checked = cur.fetchone()
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()   # 読むだけ。SET も含めて残さない
        return NullSlipCount(groups=groups, extra=extra, checked=checked)

    def vacuum_stats(self) -> list[VacuumStat]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(VACUUM_STATS_SQL)
                return [
                    VacuumStat(schema=row[0], name=row[1], live=row[2],
                               dead=row[3], last_vacuum=row[4], last_analyze=row[5])
                    for row in cur.fetchall()
                ]

    def bloat_estimates(self, limit: int = 20) -> list[BloatEstimate]:
        # The query is full of literal % (modulo operators), so it can't carry
        # bound params without psycopg2 misreading them as placeholders. limit is
        # a trusted int, so splice it directly and execute with no params.
        sql_text = BLOAT_SQL.format(limit=int(limit))
        with self._connect() as conn:
            # pg_table_size() in the estimate opens each table — same lock
            # exposure as table_sizes, same guard.
            with self._size_probe_cursor(conn) as cur:
                cur.execute(sql_text)
                return [
                    BloatEstimate(schema=row[0], name=row[1], table_bytes=row[2],
                                  wasted_bytes=row[3], bloat_ratio=float(row[4]))
                    for row in cur.fetchall()
                ]

    def fk_missing_indexes(self) -> list[FKMissingIndex]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(FK_MISSING_INDEX_SQL)
                return [
                    FKMissingIndex(schema=row[0], table=row[1], constraint=row[2],
                                   columns=row[3] or "", references=row[4])
                    for row in cur.fetchall()
                ]

    def duplicate_indexes(self) -> list[DuplicateIndex]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(DUPLICATE_INDEX_SQL)
                return [
                    DuplicateIndex(schema=row[0], table=row[1], name=row[2],
                                   columns=row[3] or "", covered_by=row[4],
                                   covered_by_columns=row[5] or "",
                                   identical=row[6], size=row[7])
                    for row in cur.fetchall()
                ]

    # --- referential integrity (orphan rows) -----------------------------

    def orphan_candidates(self) -> list[OrphanCandidate]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(NOT_VALID_FK_SQL)
                out = [
                    OrphanCandidate(
                        kind="unvalidated", schema=row[1], table=row[2],
                        columns=row[3] or "", references=row[4],
                        ref_columns=row[5] or "", constraint=row[0])
                    for row in cur.fetchall()
                ]
                cur.execute(INFERRED_FK_SQL)
                out += [
                    OrphanCandidate(
                        kind="inferred", schema=row[0], table=row[1],
                        columns=row[2], references=row[3],
                        ref_columns=row[4], constraint=None)
                    for row in cur.fetchall()
                ]
        return out

    def orphan_count(self, schema: str, table: str, *,
                     constraint: str | None = None,
                     column: str | None = None,
                     timeout_ms: int = 15000) -> OrphanCount:
        # Re-derive the parent + column lists from the catalog rather than trust
        # the caller: the request only names *which* relationship to check.
        if constraint is not None:
            resolve, params = FK_RESOLVE_SQL, (schema, table, constraint)
        elif column is not None:
            resolve, params = INFERRED_RESOLVE_SQL, (schema, table, column)
        else:
            raise EngineError(_("No relationship to check was given."))

        with self._connect() as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET TRANSACTION READ ONLY")
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute(resolve, params)
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        conn.rollback()
                        raise EngineError(_(
                            "This reference no longer resolves to a single "
                            "parent — it may have changed since the panel loaded."))
                    if constraint is not None:
                        pschema, ptable, child_cols, parent_cols = row
                    else:
                        pschema, ptable, pcol = row
                        child_cols, parent_cols = [column], [pcol]

                    # child ch LEFT JOIN parent pa ON ch.c = pa.r ... — a row is an
                    # orphan when every child ref column is non-null (a NULL ref is
                    # satisfied under MATCH SIMPLE) yet no parent row matched. The
                    # first parent column is non-null on any match, so its being
                    # NULL after the join means "unmatched".
                    join = sql.SQL(" AND ").join(
                        sql.SQL("ch.{} = pa.{}").format(
                            sql.Identifier(c), sql.Identifier(r))
                        for c, r in zip(child_cols, parent_cols))
                    nonnull = sql.SQL(" AND ").join(
                        sql.SQL("ch.{} IS NOT NULL").format(sql.Identifier(c))
                        for c in child_cols)
                    query = sql.SQL(
                        "SELECT count(*) FILTER (WHERE pa.{first} IS NULL) AS orphans, "
                        "       count(*) AS checked "
                        "FROM {child} ch LEFT JOIN {parent} pa ON {join} "
                        "WHERE {nonnull}"
                    ).format(
                        first=sql.Identifier(parent_cols[0]),
                        child=sql.Identifier(schema, table),
                        parent=sql.Identifier(pschema, ptable),
                        join=join, nonnull=nonnull)
                    cur.execute(query)
                    orphans, checked = cur.fetchone()
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()  # read-only: never persist, even the SET statements
        return OrphanCount(orphans=orphans, checked=checked)

    # --- foreign-key dependency graph ------------------------------------

    def foreign_keys(self) -> list[ForeignKeyEdge]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(FK_EDGES_SQL)
                return [
                    ForeignKeyEdge(constraint=row[0], child=row[1],
                                   parent=row[2], columns=row[3] or "")
                    for row in cur.fetchall()
                ]

    # --- server configuration --------------------------------------------

    def common_settings(self) -> list[str]:
        return COMMON_SETTINGS

    def list_settings(self, names=None, category=None) -> list[Setting]:
        where, params = "", []
        if names:
            where, params = "WHERE name = ANY(%s)", [list(names)]
        elif category:
            where, params = "WHERE category = %s", [category]
        sql_text = f"{SETTINGS_SELECT} {where} ORDER BY category, name"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_text, params)
                return [_setting(row) for row in cur.fetchall()]

    def list_setting_categories(self) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT category FROM pg_settings ORDER BY category")
                return [row[0] for row in cur.fetchall()]

    def pending_restart_settings(self) -> list[Setting]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"{SETTINGS_SELECT} WHERE pending_restart ORDER BY name")
                return [_setting(row) for row in cur.fetchall()]

    def update_setting(self, name: str, value: str) -> Setting:
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("ALTER SYSTEM SET {} = {}").format(
                            sql.Identifier(name), sql.Literal(value)
                        )
                    )
                    cur.execute("SELECT pg_reload_conf()")
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    def reset_setting(self, name: str) -> Setting:
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name))
                    )
                    cur.execute("SELECT pg_reload_conf()")
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    @staticmethod
    def _one_setting(cur, name: str) -> Setting:
        cur.execute(f"{SETTINGS_SELECT} WHERE name = %s", [name])
        row = cur.fetchone()
        if row is None:
            raise EngineError(_("Unknown setting: %(name)s") % {"name": name})
        return _setting(row)


def _setting(row) -> Setting:
    return Setting(
        name=row[0],
        value=row[1],
        unit=row[2],
        category=row[3],
        description=row[4],
        vartype=row[5],
        context=row[6],
        enumvals=row[7],
        min_val=row[8],
        max_val=row[9],
        default=row[10],
        pending_restart=row[11],
    )


def _role(row) -> Role:
    """Turn pg_roles flags into the attribute labels psql prints for `\\du`."""
    super_, createrole, createdb, replication, canlogin, connlimit = row[1:]
    attrs = []
    if super_:
        attrs.append("Superuser")
    if createrole:
        attrs.append("Create role")
    if createdb:
        attrs.append("Create DB")
    if replication:
        attrs.append("Replication")
    if not canlogin:
        attrs.append("Cannot login")
    if connlimit >= 0:
        attrs.append(f"{connlimit} connections")
    return Role(name=row[0], attributes=attrs, can_login=canlogin,
                superuser=super_, createdb=createdb, createrole=createrole)


def _parse_plan(payload) -> PlanNode:
    """Turn EXPLAIN (FORMAT JSON) output into a PlanNode tree. psycopg2 decodes
    json columns to Python objects, but accept a raw string too, just in case."""
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)
    return _plan_node(payload[0]["Plan"])


def _plan_node(d: dict) -> PlanNode:
    return PlanNode(
        node_type=d.get("Node Type", "?"),
        relation=d.get("Relation Name"),
        index=d.get("Index Name"),
        plan_rows=d.get("Plan Rows", 0),
        total_cost=d.get("Total Cost", 0.0),
        plan_width=d.get("Plan Width", 0),
        actual_rows=d.get("Actual Rows"),
        actual_ms=d.get("Actual Total Time"),
        loops=d.get("Actual Loops"),
        detail=_plan_detail(d),
        children=[_plan_node(c) for c in d.get("Plans", [])],
    )


def _plan_detail(d: dict) -> str | None:
    """The context psql prints under a node: join kind, aggregate strategy, and
    the conditions/filters. Display-only — the diff aligns on node summaries, not
    this — so it's safe to make it rich."""
    bits = []
    if d.get("Join Type"):
        bits.append(f"{d['Join Type']} join")
    if d.get("Strategy"):
        bits.append(d["Strategy"])
    if d.get("Parallel Aware"):
        bits.append("parallel")
    for key in ("Index Cond", "Recheck Cond", "Hash Cond", "Merge Cond", "Filter"):
        if d.get(key):
            bits.append(f"{key}: {d[key]}")
    return ", ".join(bits) or None


def _lock_error(timeout: str) -> str:
    """What to say when lock_timeout fired: the statement never ran, so the
    reassurance ("nothing was changed") matters as much as the cause."""
    return _("Gave up after waiting %(timeout)s for a lock on this object — "
             "another transaction is holding it. Nothing was changed. The Locks "
             "panel shows who; retry when it clears.") % {"timeout": timeout}


def _sizes_lock_error(timeout: str) -> str:
    """A read-only probe that timed out needs a different reassurance from a
    DDL one: nothing was attempted on the table at all — we only failed to
    measure it while someone else holds it."""
    return _("Gave up after waiting %(timeout)s to measure table sizes — another "
             "transaction holds a lock on one of these tables. Nothing was "
             "changed; the Locks panel shows who is holding it.") % {"timeout": timeout}


def _clean(exc: psycopg2.Error) -> str:
    """psycopg2 errors are multi-line; keep the first meaningful line."""
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else "Could not connect to PostgreSQL."


def _tool_error(stderr: bytes, fallback: str) -> str:
    """Pull the useful cause out of a pg_dump/psql/pg_restore stderr dump.

    These tools print progress and notices alongside the real failure, and the
    last line is often a summary ("errors ignored…"), not the cause. Prefer the
    lines that actually carry the error — those with an error:/fatal:/panic:
    marker (covers `ERROR:`, `pg_restore: error:`, `psql:…: ERROR:`) — keeping
    the first few so the context shows; fall back to the closing lines, then to
    a generic message."""
    lines = [ln.strip() for ln in stderr.decode(errors="replace").splitlines()
             if ln.strip()]
    if not lines:
        return fallback
    flagged = [ln for ln in lines
               if any(k in ln.lower() for k in ("error:", "fatal:", "panic:"))]
    chosen = flagged or lines[-3:]
    return " · ".join(chosen[:3])
