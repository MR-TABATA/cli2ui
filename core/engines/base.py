"""Engine interface shared by all database backends."""
from dataclasses import dataclass


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
