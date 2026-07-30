"""Engine interface shared by all database backends."""
from dataclasses import dataclass, field
from datetime import datetime
from graphlib import CycleError, TopologicalSorter


class EngineError(Exception):
    """Raised when connecting to or querying the target database fails.

    Carries a message that is safe to show in the UI.
    """


def _human_seconds(secs: float | None) -> str | None:
    """A short, readable duration: '820 ms', '1.4 s', '3.2 min'. Sub-second
    values keep millisecond precision (replication lag is usually tiny).
    Returns None for None so callers can render a placeholder."""
    if secs is None:
        return None
    if secs < 1:
        return f"{secs * 1000:.0f} ms"
    if secs < 60:
        return f"{secs:.1f} s"
    return f"{secs / 60:.1f} min"


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
    comment: str | None = None  # COMMENT ON COLUMN / COLUMN_COMMENT, if any
    # Generated columns: a value computed from other columns rather than stored
    # directly. `generated` is "stored" or "virtual" (PostgreSQL 18+ defaults new
    # generated columns to VIRTUAL), or None for an ordinary column;
    # `generation_expr` is the expression it's computed from. Without these a
    # generated column reads as a plain column with no default — the `\d` view
    # would be quietly wrong.
    generated: str | None = None
    generation_expr: str | None = None

    @property
    def is_generated(self) -> bool:
        return self.generated is not None


@dataclass
class JsonbKey:
    """One top-level key seen in a sampled JSON/JSONB column: how many of the
    sampled object rows carried it, and the JSON type(s) its value took."""

    name: str
    count: int           # sampled object rows whose root had this key
    types: list[str]     # distinct jsonb_typeof values seen: object, array, string, …


@dataclass
class JsonbShape:
    """The observed shape of a JSON/JSONB column, read from a bounded, read-only
    sample of its non-null rows. A fact-finding aid — the top-level keys, how deep
    values nest, the mix of root types, and whether a GIN index backs the column —
    never a schema recommendation. Because it samples rather than scanning every
    row, it describes what was seen in the sample, not a guarantee about the whole
    table. A jsonb literal `'null'` is a real value (unlike SQL NULL, which is
    excluded), so `null` can legitimately appear among the root types."""

    column: str
    sampled: int                 # non-null rows examined
    root_types: dict[str, int]   # jsonb_typeof of each root value → count
    keys: list[JsonbKey]         # top-level keys of object roots, most common first
    max_depth: int               # deepest container nesting seen (0 = only scalars)
    gin_indexes: list[str]       # names of GIN indexes covering this column, if any

    @property
    def is_object(self) -> bool:
        """Whether any sampled root was a JSON object — i.e. the keys table
        carries meaning (arrays/scalars have no top-level keys)."""
        return self.root_types.get("object", 0) > 0

    @property
    def has_gin(self) -> bool:
        return bool(self.gin_indexes)


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
class ConnectionHeadroom:
    """How close the server is to refusing new connections: current connection
    count vs the server's limit. The number behind 'FATAL: too many connections'
    — the one operational signal a session list doesn't make obvious. Read-only."""

    used: int                              # connections counted against the limit
    max: int                               # the server's limit (max_connections)
    reserved: int = 0                      # slots held back for superusers; 0 if N/A
    by_state: dict[str, int] | None = None  # client sessions per state; None if N/A

    @property
    def available(self) -> int:
        return self.max - self.used if self.max > self.used else 0

    @property
    def pct(self) -> int:
        """Used as a percentage of the limit, 0..100 — the headline number."""
        return round(self.used / self.max * 100) if self.max else 0

    @property
    def level(self) -> str:
        """ok | warn | critical — drives the bar colour. Crossing ~75% is worth
        noticing; ~90% means new connections are about to start failing."""
        p = self.pct
        if p >= 90:
            return "critical"
        if p >= 75:
            return "warn"
        return "ok"


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
class BlockNode:
    """One session in a wait-for tree. `depth` places it under a head blocker:
    depth 0 is the head (the session at the root, holding a lock and not itself
    waiting), depth 1+ are the sessions blocked beneath it, deeper = further down
    the chain. The wait fields describe what *this* session is stuck on and are
    None for the head, which isn't waiting on anything. `in_cycle` marks a session
    caught in a blocking cycle (its wait leads back to one already on the path)."""

    pid: int
    user: str | None
    state: str | None
    query: str
    depth: int
    wait_secs: int | None       # how long this session has waited (None = head)
    lock_mode: str | None       # the lock this session waits for (None = head)
    object: str | None          # contended object (None = head)
    in_cycle: bool = False


@dataclass
class BlockTree:
    """A wait-for tree rooted at one head blocker: the session to cancel/kill to
    free the most, and the chain of sessions stuck beneath it. `nodes` is the tree
    flattened in display order (head first, then descendants depth-first), so a
    template renders it by indenting on `depth`. `has_cycle` means the chain forms
    a loop (a deadlock-shaped wait), in which case the root is an arbitrary member
    rather than a true unblocked head. Read-only: it's computed from the lock-wait
    snapshot, nothing is signalled here."""

    nodes: list[BlockNode]      # head at index 0; descendants follow, by depth
    has_cycle: bool = False

    @property
    def head(self) -> BlockNode:
        return self.nodes[0]

    @property
    def blocked_count(self) -> int:
        """Sessions freed if the head releases — everything below depth 0."""
        return sum(1 for n in self.nodes if n.depth > 0)


def build_block_forest(waits: list[LockWait]) -> list[BlockTree]:
    """Fold the flat lock-wait list into wait-for trees rooted at head blockers.

    Engine-agnostic: both engines already return one LockWait per blocked session
    with its direct blockers, and every mid-chain session is itself a blocked row,
    so the flat list holds every edge of the wait-for graph — the head blocker is
    the one session that blocks others yet appears in no blocked row. We build the
    reverse edges (blocker → the sessions it blocks) and walk down from each head,
    guarding against cycles by tracking the pids on the current path. A pure
    cycle with no unblocked head (A⇄B) is surfaced as its own tree flagged
    has_cycle so a deadlock-shaped wait is never silently dropped."""
    blocked = {w.blocked_pid: w for w in waits}
    info: dict[int, Blocker] = {}       # pid → description (from blocker side)
    children: dict[int, list[int]] = {}  # blocker pid → pids it blocks
    for w in waits:
        for b in w.blockers:
            children.setdefault(b.pid, []).append(w.blocked_pid)
            info.setdefault(b.pid, b)

    def node(pid: int, depth: int, in_cycle: bool) -> BlockNode:
        w = blocked.get(pid)
        desc = info.get(pid)
        if w is not None:   # a blocked session — full wait info from its row
            return BlockNode(
                pid=pid, user=w.blocked_user,
                state=desc.state if desc else None,
                query=w.blocked_query, depth=depth, wait_secs=w.wait_secs,
                lock_mode=w.lock_mode, object=w.object, in_cycle=in_cycle)
        # a head blocker — not waiting, so described only from the blocker side
        return BlockNode(
            pid=pid, user=desc.user if desc else None,
            state=desc.state if desc else None,
            query=desc.query if desc else "", depth=depth,
            wait_secs=None, lock_mode=None, object=None, in_cycle=in_cycle)

    def walk(pid: int, depth: int, path: frozenset[int]) -> list[BlockNode]:
        cyc = pid in path
        out = [node(pid, depth, cyc)]
        if cyc:
            return out          # already on this path — stop, don't recurse
        for child in sorted(children.get(pid, [])):
            out += walk(child, depth + 1, path | {pid})
        return out

    trees: list[BlockTree] = []
    reached: set[int] = set()
    heads = sorted(p for p in children if p not in blocked)
    for head in heads:
        nodes = walk(head, 0, frozenset())
        reached.update(n.pid for n in nodes)
        trees.append(BlockTree(nodes=nodes,
                               has_cycle=any(n.in_cycle for n in nodes)))

    # Any blocked session not reached from a head is in a pure cycle (no unblocked
    # root); surface each such component starting from its lowest pid.
    remaining = sorted(p for p in blocked if p not in reached)
    for start in remaining:
        if start in reached:
            continue
        nodes = walk(start, 0, frozenset())
        reached.update(n.pid for n in nodes)
        trees.append(BlockTree(nodes=nodes, has_cycle=True))

    # Highest-leverage first: most sessions freed, then longest-waiting.
    trees.sort(key=lambda t: (t.blocked_count,
                              max((n.wait_secs or 0) for n in t.nodes)),
               reverse=True)
    return trees


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
    # Time lag, in seconds. replay_lag is the read-your-writes metric: how long
    # a commit takes to become visible here (how long WAIT FOR LSN would wait).
    # NULL until Postgres has a round-trip sample, so all three can be None.
    write_lag_s: float | None = None
    flush_lag_s: float | None = None
    replay_lag_s: float | None = None

    @property
    def replay_lag_human(self) -> str | None:
        """replay_lag as a compact human string (ms / s / min), or None when
        Postgres hasn't sampled it yet."""
        return _human_seconds(self.replay_lag_s)


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


@dataclass
class FKMissingIndex:
    """A foreign key whose referencing (child) columns are not the leading columns
    of any index. PostgreSQL does not auto-create one, so FK validation, cascade
    deletes, and joins back to the parent fall back to a sequential scan. A
    catalog fact (the FK columns aren't any index's leading key), not advice."""

    schema: str
    table: str
    constraint: str
    columns: str          # the FK (child) columns, comma-joined
    references: str       # parent qualified name, for context

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass
class DuplicateIndex:
    """A non-unique index whose key columns are a leading prefix of (or identical
    to) another index on the same table — so the wider index already serves its
    lookups and it is redundant. A catalog fact; whether to drop it is the user's
    call (this never advises)."""

    schema: str
    table: str
    name: str                 # the redundant index
    columns: str              # its key columns
    covered_by: str           # the wider/identical index that already covers it
    covered_by_columns: str
    identical: bool           # True = exactly the same columns (a pure duplicate)
    size: str | None          # pretty on-disk size of the redundant index, if known

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"


@dataclass
class OrphanCandidate:
    """A referential relationship whose child rows might not resolve to a parent
    — a place orphan rows can hide, worth an on-demand count. Two kinds:

    - "unvalidated": a real foreign key added `NOT VALID` and never validated, so
      the rows that existed when it was added were never checked
      (pg_constraint.convalidated = false). The relationship is declared; only its
      back-catalogue is unverified.
    - "inferred": a column named `<base>_id` with *no* foreign key at all, whose
      values look like they should reference a table called `<base>`/`<base>s`
      (that table has a single-column primary key of the same type). A guess from
      naming, not a declared relationship — flagged as inferred so it is never
      mistaken for one.

    A validated foreign key can't have orphans by construction, so it is never a
    candidate. Read-only and factual: this says where orphans *could* exist, never
    that you should add or validate a constraint."""

    kind: str                 # "unvalidated" | "inferred"
    schema: str
    table: str
    columns: str              # child column(s), comma-joined
    references: str           # parent qualified "schema.table"
    ref_columns: str          # parent column(s), comma-joined
    constraint: str | None    # FK name when kind == "unvalidated"; None if inferred

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def inferred(self) -> bool:
        return self.kind == "inferred"


@dataclass
class OrphanCount:
    """The result of an on-demand orphan check: how many child rows hold a
    non-null reference that matches no parent row. Computed read-only with a
    LEFT JOIN anti-join — the same shape PostgreSQL's own `VALIDATE CONSTRAINT`
    uses — without validating or changing anything. `checked` is the denominator:
    child rows whose reference columns are all non-null (a NULL reference is not
    an orphan under MATCH SIMPLE, so it isn't counted either way)."""

    orphans: int              # child rows whose non-null reference has no parent
    checked: int              # child rows with a non-null reference (denominator)

    @property
    def clean(self) -> bool:
        return self.orphans == 0


@dataclass
class Extension:
    """One PostgreSQL extension, installed or merely available to install. The
    Web equivalent of `\\dx` joined with `pg_available_extensions` — a read-only
    catalog fact answering "what's loaded in this database, and what could be?".
    `installed_version` is None when the extension is available but not created."""

    name: str
    installed_version: str | None   # None = available on the server but not installed
    default_version: str | None     # the version a fresh CREATE/ALTER would install
    schema: str | None              # namespace it lives in, when installed
    comment: str | None             # one-line description (pg_available_extensions)

    @property
    def installed(self) -> bool:
        return self.installed_version is not None

    @property
    def update_available(self) -> bool:
        """Installed at an older version than the server now offers — an
        `ALTER EXTENSION … UPDATE` would move it forward. A fact, not advice."""
        return (self.installed_version is not None
                and self.default_version is not None
                and self.installed_version != self.default_version)


@dataclass
class ForeignKeyEdge:
    """One foreign-key relationship: `child` references `parent`. An edge of the
    dependency graph used to order safe TRUNCATE/load and to spot FK cycles.
    A self-referential FK (child == parent) is flagged rather than dropped
    silently — it doesn't constrain table-level order (a single-table TRUNCATE
    or full-table DELETE clears every row at once and satisfies it), but it does
    constrain row-level deletes, so it's worth surfacing."""

    constraint: str
    child: str          # qualified "schema.table" that holds the FK
    parent: str         # qualified "schema.table" it references
    columns: str        # child column(s), comma-joined, for display

    @property
    def self_ref(self) -> bool:
        return self.child == self.parent


@dataclass
class DependencyGraph:
    """The foreign-key graph of one database, topologically sorted.

    `order` is a safe TRUNCATE/DELETE order — children (referencing tables)
    before parents (referenced tables) — so you never delete a row another table
    still points at. `load_order` is the reverse: the safe INSERT/restore order.
    When the FKs form a cycle no such order exists, so both lists are empty and
    `cycle` names the tables caught in it (the footgun this surfaces for free).
    Read-only: nothing is truncated, the order is only computed and shown."""

    edges: list[ForeignKeyEdge]
    order: list[str]              # safe TRUNCATE order: children first; [] if cyclic
    load_order: list[str]         # safe INSERT order: parents first; [] if cyclic
    self_refs: list[str]          # tables with a self-referential FK
    cycle: list[str]              # tables forming a dependency cycle, or []
    node_count: int               # user tables considered

    @property
    def has_cycle(self) -> bool:
        return bool(self.cycle)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


def build_dependency_graph(tables: list[str], edges: list[ForeignKeyEdge]) -> DependencyGraph:
    """Topologically sort the FK graph. Engine-agnostic: the engines supply the
    table list and edges (from their catalogs); the ordering is pure Python.

    `graphlib.TopologicalSorter` does the work — including cycle detection, which
    it raises as `CycleError` (with the offending nodes) rather than making us
    hand-roll a visited/back-edge walk. Self-referential FKs are excluded from
    the sort (a node can't be its own predecessor without tripping a false cycle)
    and reported separately."""
    self_refs = sorted({e.child for e in edges if e.self_ref})
    sorter: TopologicalSorter = TopologicalSorter()
    # Add every table first so isolated ones (no FK in or out) still appear.
    for t in tables:
        sorter.add(t)
    for e in edges:
        if e.self_ref:
            continue  # not a table-level ordering constraint; tracked in self_refs
        # child depends on parent → static_order() yields the parent first.
        sorter.add(e.child, e.parent)
    try:
        load_order = list(sorter.static_order())
        cycle: list[str] = []
    except CycleError as exc:
        # CycleError.args == (message, [n1, n2, …, n1]); the second item is the
        # node list of the cycle, first == last.
        load_order = []
        cycle = list(exc.args[1]) if len(exc.args) > 1 else []
    return DependencyGraph(
        edges=edges,
        order=list(reversed(load_order)),  # children before parents
        load_order=load_order,             # parents before children
        self_refs=self_refs,
        cycle=cycle,
        node_count=len(tables),
    )


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

    def table_comment(self, schema: str, table: str) -> str | None:
        """The table's own COMMENT, if any — the description psql shows at the
        foot of `\\d+ table`. None when the table has no comment."""
        raise NotImplementedError

    def jsonb_shape(self, schema: str, table: str, column: str, *,
                    sample: int = 500, timeout_ms: int = 15000) -> "JsonbShape":
        """Sample a JSON/JSONB column (read-only) and report its observed shape:
        top-level keys, root-type mix, nesting depth, and any GIN index backing
        it. A fact-finding aid, not schema advice. Engines with no JSON shape
        concept declare "jsonb_shape" UNSUPPORTED rather than returning a false
        empty; the caller checks supports() first."""
        raise NotImplementedError

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        """First rows of a table. The Web equivalent of `SELECT * ... LIMIT n`."""
        raise NotImplementedError

    def run_query(self, sql: str, *, max_rows: int = 1000,
                  timeout_ms: int = 15000, read_only: bool = True,
                  lock_timeout: str | None = None) -> QueryResult:
        """Run ad-hoc SQL. Read-only by default; the DB enforces it, and the
        result is capped + time-limited so a stray query can't take anything down.

        lock_timeout (engine-native units) bounds how long the statement waits
        for a lock, for hand-written DDL in write mode — the same guard the DDL
        buttons get. Off by default: this is SQL the user typed, so waiting is
        opt-out here rather than mandatory."""
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

    def connection_headroom(self) -> "ConnectionHeadroom":
        """Current connection count vs the server's max_connections — how much
        room is left before it refuses new connections. The Web equivalent of
        `count(*) FROM pg_stat_activity` vs `max_connections` (PostgreSQL) /
        `Threads_connected` vs `max_connections` (MySQL). Read-only."""
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

    def list_extensions(self) -> list["Extension"]:
        """Extensions installed in the current database, plus those available to
        install. The Web equivalent of `\\dx` unioned with
        `pg_available_extensions`. Read-only. Engines with no extension concept
        declare "extensions" UNSUPPORTED rather than returning a false empty."""
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

    def fk_missing_indexes(self) -> list[FKMissingIndex]:
        """Foreign keys whose referencing columns have no supporting index — the
        FK columns aren't the leading key of any index on the child table. A
        catalog fact. (Some engines auto-index FK columns and declare this
        UNSUPPORTED, e.g. MySQL/InnoDB.)"""
        raise NotImplementedError

    def duplicate_indexes(self) -> list[DuplicateIndex]:
        """Non-unique indexes made redundant by another index on the same table
        (identical columns, or a leading prefix of a wider one). A catalog fact."""
        raise NotImplementedError

    def orphan_candidates(self) -> list[OrphanCandidate]:
        """Referential relationships where orphan rows could exist: foreign keys
        left `NOT VALID`, plus `<base>_id` columns with no foreign key that name-
        match a table's single-column primary key. Catalog-only — no table data is
        scanned. A fact, not advice. (Engines without a NOT VALID concept and no
        need for the inference declare "orphans" UNSUPPORTED.)"""
        raise NotImplementedError

    def orphan_count(self, schema: str, table: str, *,
                     constraint: str | None = None,
                     column: str | None = None) -> OrphanCount:
        """Count child rows whose non-null reference resolves to no parent row.
        On-demand and read-only: a LEFT JOIN anti-join under a statement timeout;
        nothing is validated or written. Identify the relationship by `constraint`
        (a NOT VALID FK, re-read from the catalog) or by `column` (an inferred
        `<base>_id`, whose parent is re-derived by the same rule) — the parent is
        never taken from the caller, so the check can't be pointed at an arbitrary
        table."""
        raise NotImplementedError

    # --- foreign-key dependency graph --------------------------------------

    def foreign_keys(self) -> list[ForeignKeyEdge]:
        """Every foreign-key relationship in the current database, as child →
        parent edges. The raw material for the dependency graph."""
        raise NotImplementedError

    def dependency_graph(self) -> DependencyGraph:
        """Order the tables by their foreign keys: a safe TRUNCATE order (and its
        reverse, the safe load order), plus any FK cycle. Read-only — it only
        reads the catalog and computes; nothing is truncated. The ordering logic
        is shared (build_dependency_graph); engines only supply foreign_keys()
        and the table list, so this works the same for any FK-aware engine."""
        tables = [t.qualified for t in self.list_tables()]
        return build_dependency_graph(tables, self.foreign_keys())

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
