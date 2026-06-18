"""Engine interface shared by all database backends."""
from dataclasses import dataclass
from datetime import datetime


class EngineError(Exception):
    """Raised when connecting to or querying the target database fails.

    Carries a message that is safe to show in the UI.
    """


@dataclass
class Dump:
    """A pg_dump result, ready to hand to the browser as a download."""
    filename: str
    content_type: str
    data: bytes


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
    superuser: bool = False   # raw flags, for prefilling the edit form
    createdb: bool = False
    createrole: bool = False


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
class Blocker:
    """One session holding a lock that another session is waiting on."""

    pid: int
    user: str | None
    state: str | None
    query: str


@dataclass
class LockWait:
    """One blocked session: the lock it's stuck waiting for and who holds it.
    The Web equivalent of joining pg_locks against pg_stat_activity to answer
    "what is my query waiting on, and who do I cancel to free it?"."""

    blocked_pid: int
    blocked_user: str | None
    blocked_query: str
    wait_secs: int | None       # how long it has been waiting
    lock_type: str              # pg_locks.locktype (relation, transactionid, …)
    lock_mode: str              # requested mode (AccessExclusiveLock, …)
    object: str                 # contended relation name, or the lock type
    blockers: list[Blocker]     # sessions that must release before this proceeds


@dataclass
class ReplicationStatus:
    """The server's replication posture: am I a primary or a standby, where is
    my WAL, and am I configured to accept a replica? The settings come straight
    from pg_settings so this doubles as a 'ready to attach a standby?' check."""

    wal_level: str              # minimal | replica | logical
    max_wal_senders: int        # 0 means no standby can connect
    max_replication_slots: int
    hot_standby: str            # on | off
    archive_mode: str           # on | off | always
    current_lsn: str            # write LSN (primary) or replay LSN (standby)
    is_standby: bool            # pg_is_in_recovery()

    @property
    def ready(self) -> bool:
        """Configured to accept a physical standby: WAL detailed enough and at
        least one sender slot available."""
        return self.wal_level in ("replica", "logical") and self.max_wal_senders > 0


@dataclass
class Standby:
    """One connected replica (a row of pg_stat_replication)."""

    pid: int
    user: str | None
    app: str | None
    client: str | None
    state: str | None           # streaming | catchup | …
    sync_state: str | None      # async | sync | quorum
    sent_lsn: str | None
    replay_lsn: str | None
    lag_bytes: int | None       # sent − replayed, in bytes


@dataclass
class ReplicationSlot:
    """One replication slot (a row of pg_replication_slots). An inactive slot
    keeps WAL pinned, so the panel surfaces active/inactive prominently."""

    name: str
    slot_type: str              # physical | logical
    database: str | None        # set for logical slots only
    active: bool
    restart_lsn: str | None
    wal_status: str | None      # reserved | extended | unreserved | lost


@dataclass
class ReplicationRecipe:
    """A copy-paste walkthrough for attaching a physical standby to this server,
    with the current connection + server values already filled in. Pure string
    assembly — no commands are run; the user copies and runs them themselves."""

    primary_host: str
    primary_port: int
    primary_user: str
    slot_name: str                 # an existing physical slot, or a suggested name
    slot_exists: bool              # True if slot_name already exists on the server
    # (param, recommended value) the primary still needs to accept a standby;
    # empty when it's already ready. Each needs a restart (postmaster context).
    conf_changes: list[tuple[str, str]]
    create_slot_sql: str           # SELECT pg_create_physical_replication_slot('…');
    basebackup_cmd: str            # pg_basebackup … -R -X stream --slot=…
    primary_conninfo: str          # what `pg_basebackup -R` writes into the standby
    standby_datadir: str           # placeholder path for the new standby's data dir

    @property
    def ready(self) -> bool:
        return not self.conf_changes


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
class BloatEstimate:
    """Estimated table bloat — wasted space beyond what the rows actually need,
    from a statistics-only query (no table scan). Approximate by design: it
    relies on pg_stats, so it's directional, not exact. The neighbour of the
    dead-rows card: dead tuples are *why* a table bloats, this is *how much*."""

    schema: str
    name: str
    table_bytes: int        # current on-disk heap size (context / the bar)
    wasted_bytes: int       # estimated reclaimable space
    bloat_ratio: float      # actual pages / ideal pages; 1.0 = no bloat

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def wasted_pct(self) -> int:
        """Wasted / current size, 0..100 — the headline number."""
        return round(self.wasted_bytes / self.table_bytes * 100) if self.table_bytes else 0


class Engine:
    """Base class. One Engine wraps one saved Connection."""

    # Maintenance/introspection features this engine cannot answer *at all*
    # because the concept does not exist for it (e.g. InnoDB has no vacuum or
    # dead-tuple model). Panels use this to show a "not applicable to this
    # engine" state instead of an empty-data state, so a structural absence is
    # never misread as "nothing to report". A feature that *could* return rows
    # but is simply not implemented yet must NOT live here — it should raise
    # EngineError so the caller surfaces "couldn't determine", never a false
    # empty. Keys are stable strings: "vacuum", "bloat", "schemas".
    UNSUPPORTED: frozenset = frozenset()

    def __init__(self, connection):
        self.connection = connection

    def supports(self, feature: str) -> bool:
        """Whether this engine can answer `feature` at all (see UNSUPPORTED)."""
        return feature not in self.UNSUPPORTED

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

    def filter_rows(self, schema: str, table: str, filters: list[dict], *,
                    limit: int = 1000, timeout_ms: int = 15000) -> QueryResult:
        """Run a read-only `SELECT * ... WHERE <conds>` built from the filter
        builder (column/operator/value rows, ANDed). Columns are validated and
        composed safely; values are bound, not interpolated."""
        raise NotImplementedError

    def import_csv(self, schema: str, table: str, fileobj, *,
                   encoding: str = "utf-8-sig") -> int:
        """Append rows from a CSV (with header) into an existing table, matching
        columns by header name, in one all-or-nothing transaction. Returns the
        number of rows imported."""
        raise NotImplementedError

    def stream_query(self, sql: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Run read-only SQL and stream the full result for a file export: yield
        the column-name list, then one row tuple at a time, without buffering the
        whole result in memory. Read-only and time-limited like run_query."""
        raise NotImplementedError

    def stream_table(self, schema: str, table: str, *, timeout_ms: int = 60000,
                     max_rows: int = 1_000_000):
        """Stream a whole table's rows for a CSV/JSON export — the same as
        stream_query over `SELECT * FROM <table>`, with the identifiers quoted
        safely. Yields the column-name list, then one row tuple at a time."""
        raise NotImplementedError

    def whatif_cursor(self, *, timeout_ms: int = 15000, lock_timeout: str = "2s"):
        """Context manager yielding a cursor in a transaction that is ALWAYS
        rolled back — the primitive the planner what-if tools (scale simulation,
        index lab, in their own app) run their catalog/DDL edits + EXPLAIN
        through, so nothing is ever persisted. Driver errors surface as
        EngineError."""
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

    def list_blocking(self) -> list[LockWait]:
        """Sessions blocked waiting on a lock, paired with whoever holds it.
        The Web equivalent of joining `pg_locks` to `pg_stat_activity`."""
        raise NotImplementedError

    # --- replication (pg_stat_replication / pg_replication_slots) -----------

    def replication_status(self) -> ReplicationStatus:
        """WAL position, primary/standby role, and config readiness."""
        raise NotImplementedError

    def list_standbys(self) -> list[Standby]:
        """Connected replicas. The Web equivalent of `pg_stat_replication`."""
        raise NotImplementedError

    def list_replication_slots(self) -> list[ReplicationSlot]:
        """Replication slots. The Web equivalent of `pg_replication_slots`."""
        raise NotImplementedError

    def create_replication_slot(self, name: str) -> None:
        """Create a physical slot. `pg_create_physical_replication_slot(name)`."""
        raise NotImplementedError

    def drop_replication_slot(self, name: str) -> None:
        """Drop a slot, freeing the WAL it pinned. `pg_drop_replication_slot`."""
        raise NotImplementedError

    def replication_recipe(self, status, slots) -> ReplicationRecipe:
        """Build the copy-paste standby-setup walkthrough from the already-fetched
        status + slots (no extra round trip), with current values filled in."""
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

    # --- catalog alterations (ALTER) ---------------------------------------

    def rename_schema(self, old: str, new: str) -> None:
        """`ALTER SCHEMA old RENAME TO new`."""
        raise NotImplementedError

    def alter_schema_owner(self, name: str, owner: str) -> None:
        """`ALTER SCHEMA name OWNER TO owner`."""
        raise NotImplementedError

    def rename_role(self, old: str, new: str) -> None:
        """`ALTER ROLE old RENAME TO new`."""
        raise NotImplementedError

    def alter_role(self, name: str, *, login: bool, superuser: bool,
                   createdb: bool, createrole: bool,
                   password: str | None = None) -> None:
        """Set a role's attributes (`ALTER ROLE name WITH …`). The booleans are
        the desired final state; password is set only when provided."""
        raise NotImplementedError

    def create_database(self, name: str, *, template: str | None = None,
                        owner: str | None = None,
                        encoding: str | None = None) -> None:
        """Create a database, optionally copying an existing one as a TEMPLATE
        (the Web equivalent of `createdb` / `CREATE DATABASE … TEMPLATE src`)."""
        raise NotImplementedError

    def drop_database(self, name: str, *, force: bool = False) -> None:
        """Drop a database (`DROP DATABASE name [WITH (FORCE)]`). FORCE
        disconnects other sessions first (PostgreSQL 13+)."""
        raise NotImplementedError

    def rename_database(self, old: str, new: str) -> None:
        """Rename a database (`ALTER DATABASE old RENAME TO new`)."""
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

    def drop_index(self, schema: str, name: str, table: str | None = None) -> None:
        """Drop an index. The Web equivalent of `DROP INDEX name`. `table` is
        optional for engines that don't need it (PostgreSQL) but required by
        those that do (MySQL's `DROP INDEX name ON table`)."""
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

    def bloat_estimates(self, limit: int = 20) -> list[BloatEstimate]:
        """Estimated table bloat from pg_stats (no table scan). Approximate."""
        raise NotImplementedError

    # --- server configuration (postgresql.conf, via SQL) -------------------

    def common_settings(self) -> list[str]:
        """The parameters shown by default in the settings editor (so it isn't a
        wall of obscure tunables). Engine-specific; empty if unsupported."""
        return []

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
