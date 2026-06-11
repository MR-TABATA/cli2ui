"""Engine interface shared by all database backends."""
from dataclasses import dataclass
from datetime import datetime


class EngineError(Exception):
    """Raised when connecting to or querying the target database fails.

    Carries a message that is safe to show in the UI.
    """


@dataclass
class Table:
    schema: str
    name: str
    rows: int  # estimated live row count (approximate, from stats)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None


@dataclass
class Preview:
    columns: list[str]
    rows: list[tuple]


@dataclass
class Database:
    """One database on the server. The Web equivalent of a `\\l` row."""

    name: str
    owner: str
    encoding: str
    size: str | None  # pretty-printed, or None if we can't connect to size it


@dataclass
class Schema:
    """One schema. The Web equivalent of a `\\dn` row."""

    name: str
    owner: str


@dataclass
class Role:
    """One login/group role. The Web equivalent of a `\\du` row."""

    name: str
    attributes: list[str]  # human labels: "Superuser", "Create DB", …
    can_login: bool


@dataclass
class Index:
    """One index on a table. The Web equivalent of a `\\d table` index row."""

    name: str
    method: str          # access method: btree, hash, gin, …
    unique: bool
    primary: bool        # backs a PRIMARY KEY constraint
    definition: str      # the full CREATE INDEX … statement (pg_get_indexdef)
    size: str | None     # pretty-printed on-disk size
    valid: bool = True   # False = a failed CONCURRENTLY build left it unusable

    @property
    def columns_text(self) -> str:
        """The indexed columns/expressions, pulled from the definition's
        column list — e.g. 'customer_id, created_at' — for a compact display.

        Reads to the close paren that *matches* the first open paren, so a
        partial index's `WHERE (…)` or an expression index's nested parens
        (e.g. `(lower(name))`) don't leak into the display."""
        start = self.definition.find("(")
        if start == -1:
            return ""
        depth = 0
        for i in range(start, len(self.definition)):
            ch = self.definition[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return self.definition[start + 1:i].strip()
        return ""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    rowcount: int    # rows returned (after the cap)
    truncated: bool  # more rows existed beyond the cap
    duration_ms: int


@dataclass
class PlanNode:
    """One node of an EXPLAIN plan tree (parsed from FORMAT JSON).

    The structured form behind the raw plan text: it lets us diff plans node by
    node (Seq Scan → Index Scan, cost/row blow-ups) instead of as flat text, and
    it's the unit the scale simulation compares across row-count multipliers.
    """

    node_type: str            # "Seq Scan", "Hash Join", "Sort", …
    relation: str | None      # "Relation Name" — the table, if a scan
    index: str | None         # "Index Name" — if an index scan
    plan_rows: float          # estimated rows out of this node (the headline)
    total_cost: float         # estimated cumulative cost
    plan_width: int
    actual_rows: float | None  # ANALYZE only: real rows
    actual_ms: float | None    # ANALYZE only: real total time (ms, all loops)
    loops: int | None
    detail: str | None         # join type / strategy / scan direction hint
    children: list["PlanNode"]

    @property
    def summary(self) -> str:
        """The one-line label psql prints, e.g. 'Index Scan using pk on orders'."""
        s = self.node_type
        if self.index:
            s += f" using {self.index}"
        if self.relation:
            s += f" on {self.relation}"
        return s


@dataclass
class ScalePlan:
    """One EXPLAIN plan produced at a given row-count multiplier (what-if)."""

    factor: int        # 1 = real stats, 100 = "what if every table were 100× bigger"
    plan: PlanNode


@dataclass
class Activity:
    """One server session (a row of pg_stat_activity)."""

    pid: int
    user: str | None
    database: str | None
    app: str | None
    client: str | None
    state: str | None
    wait: str | None            # "Lock: relation" etc., or None
    blocked_by: list[int]       # pids blocking this one (pg_blocking_pids)
    query_secs: int | None      # how long the current query has run
    query: str
    is_self: bool = False       # this is cli2ui's own connection

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_by)


@dataclass
class Setting:
    """One server configuration parameter (a row of pg_settings)."""

    name: str
    value: str           # current value, human form (e.g. "128MB", "on")
    unit: str | None
    category: str
    description: str
    vartype: str         # bool | integer | real | string | enum
    context: str         # internal | postmaster | sighup | user | ...
    enumvals: list[str] | None
    min_val: str | None
    max_val: str | None
    default: str | None  # boot value
    pending_restart: bool

    @property
    def requires_restart(self) -> bool:
        """Changing this needs a full server restart, not just a reload."""
        return self.context == "postmaster"

    @property
    def read_only(self) -> bool:
        return self.context == "internal"


@dataclass
class TableSize:
    """On-disk footprint of one table: heap + indexes + toast. The Web
    equivalent of `\\dt+` / pg_total_relation_size()."""

    schema: str
    name: str
    total_bytes: int      # heap + indexes + toast (for sorting / the bar)
    total: str            # pretty: "12 MB"
    table: str            # pretty: heap only
    index: str            # pretty: indexes only

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class UnusedIndex:
    """A non-constraint index the planner has never used (idx_scan = 0 since the
    last stats reset) — a candidate to drop. The inverse of the index lab."""

    schema: str
    table: str
    name: str
    scans: int
    bytes: int
    size: str             # pretty


@dataclass
class VacuumStat:
    """Dead-tuple / vacuum health for one table (from pg_stat_user_tables). Dead
    tuples are the raw material of bloat; the last-vacuum times say whether
    (auto)vacuum is keeping up."""

    schema: str
    name: str
    live: int
    dead: int
    last_vacuum: datetime | None    # most recent manual OR auto vacuum
    last_analyze: datetime | None   # most recent manual OR auto analyze

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def dead_ratio(self) -> float:
        """Dead / (live + dead), 0..1 — the headline bloat signal."""
        total = self.live + self.dead
        return self.dead / total if total else 0.0

    @property
    def dead_pct(self) -> int:
        return round(self.dead_ratio * 100)


@dataclass
class IndexPreview:
    """The result of a 'what-if' index trial: the same query EXPLAIN ANALYZE'd
    without and then with a hypothetical index, which is created and immediately
    rolled back. Real measured timing, zero persistence."""

    ddl: str             # the CREATE INDEX you'd run for real (display)
    before: PlanNode     # plan + real timing without the index
    after: PlanNode      # plan + real timing with the hypothetical index
    used: bool           # did the planner actually choose the hypothetical index?

    @property
    def before_ms(self) -> float | None:
        return self.before.actual_ms

    @property
    def after_ms(self) -> float | None:
        return self.after.actual_ms

    @property
    def speedup(self) -> float | None:
        """before / after — >1 means the index made the query faster."""
        if self.before_ms and self.after_ms and self.after_ms > 0:
            return self.before_ms / self.after_ms
        return None


class Engine:
    """Base class. One Engine wraps one saved Connection."""

    def __init__(self, connection):
        self.connection = connection

    def test(self) -> None:
        """Open a connection and fail loudly (EngineError) if it can't."""
        raise NotImplementedError

    def list_tables(self) -> list[Table]:
        """Return user tables. The Web equivalent of `\\dt` / `SHOW TABLES`."""
        raise NotImplementedError

    def list_columns(self, schema: str, table: str) -> list[Column]:
        """Column definitions for one table. The Web equivalent of `\\d table`."""
        raise NotImplementedError

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        """First rows of a table. The Web equivalent of `SELECT * ... LIMIT n`."""
        raise NotImplementedError

    def run_query(self, sql: str, *, max_rows: int = 1000,
                  timeout_ms: int = 15000, read_only: bool = True) -> QueryResult:
        """Run ad-hoc SQL. Read-only by default; the DB enforces it, and the
        result is capped + time-limited so a stray query can't take anything down."""
        raise NotImplementedError

    def explain(self, sql: str, *, analyze: bool = False,
                timeout_ms: int = 15000) -> str:
        """Return the query plan as text. ANALYZE runs the query for real
        timings (still inside a read-only transaction, so writes are rejected)."""
        raise NotImplementedError

    def explain_json(self, sql: str, *, analyze: bool = False,
                     timeout_ms: int = 15000) -> "PlanNode":
        """Return the query plan as a parsed tree (EXPLAIN FORMAT JSON), so it
        can be diffed structurally instead of as text. Same read-only safety."""
        raise NotImplementedError

    def simulate_scale(self, sql: str, *, factors=(1, 100, 10000),
                       timeout_ms: int = 15000) -> "list[ScalePlan]":
        """What-if planning: temporarily scale the involved tables' row-count
        stats by each factor and EXPLAIN, so you can see *where the plan shape
        breaks* as data grows — without any real data. The catalog edit is made
        inside a transaction that is always rolled back (never committed, and
        invisible to other sessions via MVCC). Plan shape only, not real time;
        requires a superuser connection."""
        raise NotImplementedError

    # --- activity / sessions (pg_stat_activity) ----------------------------

    def list_activity(self) -> list[Activity]:
        """Running queries and connections. The Web equivalent of querying
        `pg_stat_activity` / `SHOW PROCESSLIST`."""
        raise NotImplementedError

    def cancel_backend(self, pid: int) -> bool:
        """Cancel the running query in a session. `pg_cancel_backend(pid)`."""
        raise NotImplementedError

    def terminate_backend(self, pid: int) -> bool:
        """Force-close a session. `pg_terminate_backend(pid)`."""
        raise NotImplementedError

    # --- catalog browsing (psql backslash commands) ------------------------

    def list_databases(self) -> list[Database]:
        """Databases on the server. The Web equivalent of `\\l`."""
        raise NotImplementedError

    def list_schemas(self) -> list[Schema]:
        """User schemas in the current database. The Web equivalent of `\\dn`."""
        raise NotImplementedError

    def list_roles(self) -> list[Role]:
        """Login/group roles. The Web equivalent of `\\du`."""
        raise NotImplementedError

    # --- catalog mutations (CREATE / DROP) ---------------------------------

    def create_schema(self, name: str) -> None:
        """Create a schema. The Web equivalent of `CREATE SCHEMA name`."""
        raise NotImplementedError

    def drop_schema(self, name: str, cascade: bool = False) -> None:
        """Drop a schema. The Web equivalent of `DROP SCHEMA name [CASCADE]`."""
        raise NotImplementedError

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
        """Create a role. The Web equivalent of `CREATE ROLE name WITH …`."""
        raise NotImplementedError

    def drop_role(self, name: str) -> None:
        """Drop a role. The Web equivalent of `DROP ROLE name`."""
        raise NotImplementedError

    # --- indexes (CREATE / DROP INDEX) -------------------------------------

    def list_indexes(self, schema: str, table: str) -> list[Index]:
        """Indexes on one table. The Web equivalent of `\\d table`'s index list."""
        raise NotImplementedError

    def create_index(self, schema: str, table: str, columns: list[str], *,
                     method: str = "btree", unique: bool = False,
                     name: str | None = None) -> None:
        """Create an index. The Web equivalent of `CREATE INDEX … ON table (…)`.
        Built CONCURRENTLY so it doesn't lock out writes on a live table."""
        raise NotImplementedError

    def drop_index(self, schema: str, name: str) -> None:
        """Drop an index. The Web equivalent of `DROP INDEX name`."""
        raise NotImplementedError

    def preview_index(self, sql: str, schema: str, table: str,
                      columns: list[str], *, method: str = "btree",
                      unique: bool = False,
                      timeout_ms: int = 15000) -> "IndexPreview":
        """What-if index trial: EXPLAIN ANALYZE the query without, then with, a
        hypothetical index built inside a transaction that is always rolled back.
        Real timing, zero persistence — the index (and any side effects of
        running the query under ANALYZE) are never committed and are invisible to
        other sessions. Reuses the same CREATE INDEX builder as create_index, but
        non-concurrent so it can live inside the transaction."""
        raise NotImplementedError

    # --- health (sizes, unused indexes) ------------------------------------

    def table_sizes(self, limit: int = 20) -> list[TableSize]:
        """Largest tables by total on-disk size (heap + indexes + toast)."""
        raise NotImplementedError

    def unused_indexes(self) -> list[UnusedIndex]:
        """Non-constraint indexes the planner has never used — drop candidates."""
        raise NotImplementedError

    def vacuum_stats(self) -> list[VacuumStat]:
        """Dead-tuple counts and last (auto)vacuum/analyze times per table."""
        raise NotImplementedError

    # --- server configuration (postgresql.conf, via SQL) -------------------

    def list_settings(self, names=None, category=None) -> list[Setting]:
        """Read configuration parameters. The Web equivalent of `SHOW ALL`."""
        raise NotImplementedError

    def list_setting_categories(self) -> list[str]:
        raise NotImplementedError

    def pending_restart_settings(self) -> list[Setting]:
        """Parameters changed via ALTER SYSTEM that await a server restart."""
        raise NotImplementedError

    def update_setting(self, name: str, value: str) -> Setting:
        """Set a parameter and reload. `ALTER SYSTEM SET` + `pg_reload_conf()`."""
        raise NotImplementedError

    def reset_setting(self, name: str) -> Setting:
        """Revert a parameter to its default. `ALTER SYSTEM RESET` + reload."""
        raise NotImplementedError
