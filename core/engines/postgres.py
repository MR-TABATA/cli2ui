"""PostgreSQL engine — psycopg2 under the hood."""
import contextlib
import json
import time

import psycopg2
from psycopg2 import sql

from .base import (
    Activity,
    Column,
    Database,
    Engine,
    EngineError,
    Index,
    IndexPreview,
    PlanNode,
    Preview,
    QueryResult,
    Role,
    ScalePlan,
    Schema,
    Setting,
    Table,
    TableSize,
    UnusedIndex,
    VacuumStat,
)

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

# Index access methods we let the UI offer. A fixed allow-list because the
# chosen method is interpolated into the DDL as a bare keyword (it can't be a
# bound parameter), so it must never come straight from user input.
INDEX_METHODS = ("btree", "hash", "gin", "gist", "spgist", "brin")

# Name given to the throwaway index created during a what-if trial, so we can
# tell whether the planner actually chose it (it's rolled back regardless).
HYPO_INDEX_NAME = "_cli2ui_hypothetical_idx"


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
        raise EngineError(f"Unsupported index method: {method}")
    if not columns:
        raise EngineError("Select at least one column to index.")
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


# Scale-simulation what-if: multiply the planner's row-count estimate for the
# named tables (and their indexes) by a factor. We scale ONLY reltuples, not
# relpages: the planner derives a tuple *density* (reltuples/relpages) and
# multiplies it by the table's *actual* page count, so scaling both by N cancels
# out and changes nothing — scaling reltuples alone is what makes it believe the
# table grew. (Page-based I/O cost can't be inflated via the catalog this way;
# this models row-count growth, which is what drives plan *shape*.) Run only
# inside a transaction that is always rolled back.
SCALE_PGCLASS_SQL = """
UPDATE pg_class
   SET reltuples = reltuples * %s
 WHERE oid IN (
   SELECT oid FROM pg_class WHERE relname = ANY(%s) AND relkind IN ('r', 'p')
   UNION
   SELECT indexrelid FROM pg_index
    WHERE indrelid IN (SELECT oid FROM pg_class
                        WHERE relname = ANY(%s) AND relkind IN ('r', 'p'))
 )
"""


class PostgresEngine(Engine):
    @contextlib.contextmanager
    def _connect(self, dbname=None):
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
                    )
                    for row in cur.fetchall()
                ]

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
                  timeout_ms: int = 15000, read_only: bool = True) -> QueryResult:
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
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            # Read-only: never persist, even for a statement that slipped through.
            conn.rollback()
        return QueryResult(
            columns=columns, rows=rows, rowcount=rowcount,
            truncated=truncated, duration_ms=duration_ms,
        )

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

    def simulate_scale(self, sql_text: str, *, factors=(1, 100, 10000),
                       timeout_ms: int = 15000) -> list[ScalePlan]:
        # The factor-1 plan is the real one; it also tells us which tables the
        # query touches, so we scale exactly those (not the whole database).
        base = self.explain_json(sql_text, timeout_ms=timeout_ms)
        relnames = sorted(_relation_names(base))
        plans = [ScalePlan(factor=1, plan=base)]
        for n in factors:
            if n == 1:
                continue
            plans.append(ScalePlan(
                factor=n,
                plan=self._explain_scaled(sql_text, n, relnames, timeout_ms),
            ))
        return plans

    def _explain_scaled(self, sql_text: str, factor: int,
                        relnames: list[str], timeout_ms: int) -> PlanNode:
        with self._connect() as conn:
            # NOT read-only: we UPDATE pg_class. But plain EXPLAIN (no ANALYZE)
            # never runs the user's query, and we ROLLBACK unconditionally — the
            # catalog edit is never committed and is invisible to other sessions.
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute("SET LOCAL lock_timeout = '2s'")  # don't hang on catalog locks
                    cur.execute(SCALE_PGCLASS_SQL, [factor, relnames, relnames])
                    cur.execute("EXPLAIN (FORMAT JSON) " + sql_text)
                    payload = cur.fetchone()[0]
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_scale_error(exc)) from exc
            conn.rollback()  # never persist the what-if catalog edit
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
        inside a transaction; _connect is autocommit, so each runs standalone."""
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
            raise EngineError(f"No such column(s): {', '.join(unknown)}")
        # CONCURRENTLY can't run inside a transaction — fine here, _connect()
        # is autocommit, so _execute() runs it as a standalone statement.
        self._execute(build_create_index_sql(
            schema, table, columns, method=method, unique=unique,
            name=name, concurrently=True,
        ))

    def drop_index(self, schema: str, name: str) -> None:
        self._execute(sql.SQL("DROP INDEX CONCURRENTLY {}.{}").format(
            sql.Identifier(schema), sql.Identifier(name)))

    def preview_index(self, sql_text: str, schema: str, table: str,
                      columns: list[str], *, method: str = "btree",
                      unique: bool = False,
                      timeout_ms: int = 15000) -> IndexPreview:
        valid = {c.name for c in self.list_columns(schema, table)}
        unknown = [c for c in columns if c not in valid]
        if unknown:
            raise EngineError(f"No such column(s): {', '.join(unknown)}")
        # The throwaway index we actually build (named so we can detect use);
        # plain (not CONCURRENTLY) so it can live inside the transaction.
        hypo = build_create_index_sql(schema, table, columns, method=method,
                                      unique=unique, name=HYPO_INDEX_NAME,
                                      concurrently=False)
        # The statement the user would run for real (what we display).
        real = build_create_index_sql(schema, table, columns, method=method,
                                      unique=unique, concurrently=True)
        explain = "EXPLAIN (ANALYZE, FORMAT JSON) " + sql_text
        with self._connect() as conn:
            # NOT read-only: we CREATE INDEX. Safety is the unconditional
            # ROLLBACK — the index, and any side effects of running the query
            # under ANALYZE, are never committed and are invisible to others.
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL statement_timeout = %s", [timeout_ms])
                    cur.execute("SET LOCAL lock_timeout = '2s'")
                    cur.execute(explain)
                    before = _parse_plan(cur.fetchone()[0])
                    cur.execute(hypo)  # visible to the next EXPLAIN in this tx
                    cur.execute(explain)
                    after = _parse_plan(cur.fetchone()[0])
                    ddl = real.as_string(cur)
            except psycopg2.Error as exc:
                conn.rollback()
                raise EngineError(_clean(exc)) from exc
            conn.rollback()  # never persist the hypothetical index
        return IndexPreview(ddl=ddl, before=before, after=after,
                            used=_uses_index(after, HYPO_INDEX_NAME))

    def _execute(self, statement) -> None:
        """Run a composed DDL statement, mapping driver errors to EngineError."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(statement)
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc

    # --- health ----------------------------------------------------------

    def table_sizes(self, limit: int = 20) -> list[TableSize]:
        with self._connect() as conn:
            with conn.cursor() as cur:
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

    def vacuum_stats(self) -> list[VacuumStat]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(VACUUM_STATS_SQL)
                return [
                    VacuumStat(schema=row[0], name=row[1], live=row[2],
                               dead=row[3], last_vacuum=row[4], last_analyze=row[5])
                    for row in cur.fetchall()
                ]

    # --- server configuration --------------------------------------------

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
            raise EngineError(f"Unknown setting: {name}")
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
    return Role(name=row[0], attributes=attrs, can_login=canlogin)


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


def _relation_names(node: PlanNode, acc: set[str] | None = None) -> set[str]:
    """Every table the plan reads — what the scale simulation grows."""
    acc = set() if acc is None else acc
    if node.relation:
        acc.add(node.relation)
    for child in node.children:
        _relation_names(child, acc)
    return acc


def _uses_index(node: PlanNode, name: str) -> bool:
    """Whether any scan in the plan tree uses the named index — i.e. did the
    planner actually pick the hypothetical index we offered it?"""
    if node.index == name:
        return True
    return any(_uses_index(c, name) for c in node.children)


def _scale_error(exc: psycopg2.Error) -> str:
    msg = _clean(exc)
    if "pg_class" in msg and "permission" in msg.lower():
        return "Scale simulation needs a superuser connection (it edits pg_class)."
    return msg


def _clean(exc: psycopg2.Error) -> str:
    """psycopg2 errors are multi-line; keep the first meaningful line."""
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else "Could not connect to PostgreSQL."
