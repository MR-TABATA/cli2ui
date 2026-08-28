"""Live operations panels: activity, locks/blocking, replication and health."""
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _

from ..engines import EngineError, get_engine
from ..engines.base import build_block_forest
from ..models import Connection


# A readable version of pg_stat_activity for the "open in SQL" link.
ACTIVITY_SHOW_SQL = (
    "SELECT pid, usename, state, wait_event_type, query,\n"
    "       now() - query_start AS running_for, pg_blocking_pids(pid) AS blocked_by\n"
    "FROM pg_stat_activity\n"
    "WHERE backend_type = 'client backend'\n"
    "ORDER BY state = 'active' DESC, query_start;"
)


def activity(request, pk):
    """Running queries + connections from pg_stat_activity (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_activity(request, connection)


def activity_cancel(request, pk):
    """Cancel a session's query (pg_cancel_backend), then refresh the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    return _activity_signal(request, connection, "cancel")


def activity_kill(request, pk):
    """Force-close a session (pg_terminate_backend), then refresh the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    return _activity_signal(request, connection, "kill")


def _activity_signal(request, connection, action):
    pid = request.POST.get("pid")
    try:
        engine = get_engine(connection)
        if pid:
            if action == "kill":
                engine.terminate_backend(int(pid))
            else:
                engine.cancel_backend(int(pid))
    except (EngineError, ValueError) as exc:
        return _render_activity(request, connection, error=str(exc))
    return _render_activity(request, connection)


def _render_activity(request, connection, error=None):
    try:
        engine = get_engine(connection)
        sessions = engine.list_activity()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    # Headroom is a nice-to-have summary; a hiccup fetching it must not blank the
    # session list, so degrade to None and let the template omit the bar.
    try:
        headroom = engine.connection_headroom()
    except EngineError:
        headroom = None
    return render(
        request,
        "partials/activity.html",
        {"connection": connection, "sessions": sessions, "headroom": headroom,
         "query_sql": ACTIVITY_SHOW_SQL, "error": error},
    )


# Readable version of BLOCKING_SQL for the panel's "open in SQL" link.
BLOCKING_SHOW_SQL = (
    "SELECT a.pid, a.usename, a.query,\n"
    "       now() - a.query_start AS waiting_for,\n"
    "       l.locktype, l.mode, COALESCE(c.relname, l.locktype) AS object,\n"
    "       pg_blocking_pids(a.pid) AS blocked_by\n"
    "FROM pg_stat_activity a\n"
    "JOIN pg_locks l ON l.pid = a.pid AND NOT l.granted\n"
    "LEFT JOIN pg_class c ON c.oid = l.relation\n"
    "WHERE cardinality(pg_blocking_pids(a.pid)) > 0\n"
    "ORDER BY waiting_for DESC;"
)


def locks(request, pk):
    """Locks/blocking panel: who is waiting on a lock and who holds it."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_locks(request, connection)


def locks_cancel(request, pk):
    """Cancel the blocker's query (pg_cancel_backend), then refresh the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    return _locks_signal(request, connection, "cancel")


def locks_kill(request, pk):
    """Force-close the blocker (pg_terminate_backend), then refresh the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    return _locks_signal(request, connection, "kill")


def _locks_signal(request, connection, action):
    pid = request.POST.get("pid")
    try:
        engine = get_engine(connection)
        if pid:
            if action == "kill":
                engine.terminate_backend(int(pid))
            else:
                engine.cancel_backend(int(pid))
    except (EngineError, ValueError) as exc:
        return _render_locks(request, connection, error=str(exc))
    return _render_locks(request, connection)


def _render_locks(request, connection, error=None):
    try:
        waits = get_engine(connection).list_blocking()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    # Fold the flat blocked-session list into wait-for trees so the panel leads
    # with the head blocker (cancel it to free the whole chain) instead of only
    # the one-level "who's blocking me". Engine-agnostic — same LockWait input.
    trees = build_block_forest(waits)
    return render(
        request,
        "partials/locks.html",
        {"connection": connection, "trees": trees,
         "blocked_total": len(waits),
         "query_sql": BLOCKING_SHOW_SQL, "error": error},
    )


# Readable versions of the replication queries, for each table's "open in SQL".
STANDBYS_SHOW_SQL = (
    "SELECT pid, usename, application_name, client_addr, state, sync_state,\n"
    "       sent_lsn, replay_lsn,\n"
    "       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes,\n"
    "       write_lag, flush_lag, replay_lag\n"
    "FROM pg_stat_replication ORDER BY pid;"
)


SLOTS_SHOW_SQL = (
    "SELECT slot_name, slot_type, database, active, restart_lsn, wal_status\n"
    "FROM pg_replication_slots ORDER BY slot_name;"
)


def replication(request, pk):
    """Replication panel: readiness + WAL position, connected standbys, slots."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_replication(request, connection)


def slot_create(request, pk):
    """Create a physical replication slot, then refresh the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_replication(request, connection, error=_("Slot name is required."))
    try:
        get_engine(connection).create_replication_slot(name)
    except EngineError as exc:
        return _render_replication(request, connection, error=str(exc))
    return _render_replication(request, connection)


def slot_drop(request, pk):
    """Drop a replication slot (frees the WAL it pinned), then refresh."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_replication(request, connection, error=_("Slot name is required."))
    try:
        get_engine(connection).drop_replication_slot(name)
    except EngineError as exc:
        return _render_replication(request, connection, error=str(exc))
    return _render_replication(request, connection)


def _render_replication(request, connection, error=None):
    try:
        engine = get_engine(connection)
        status = engine.replication_status()
        standbys = engine.list_standbys()
        slots = engine.list_replication_slots()
        recipe = engine.replication_recipe(status, slots)
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    # MySQL's replication model (binlog/GTID, no slots, CHANGE REPLICATION SOURCE
    # TO) is too different from Postgres' (WAL/slots/pg_basebackup) to share a
    # template, so each engine gets its own.
    template = ("partials/replication_mysql.html" if connection.kind == "mysql"
                else "partials/replication.html")
    return render(
        request,
        template,
        {"connection": connection, "status": status, "standbys": standbys,
         "slots": slots, "recipe": recipe, "standbys_sql": STANDBYS_SHOW_SQL,
         "slots_sql": SLOTS_SHOW_SQL, "error": error},
    )


# Readable versions of the health queries, for each card's "open in SQL" link.
SIZES_SHOW_SQL = (
    "SELECT n.nspname AS schema, c.relname AS name,\n"
    "       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,\n"
    "       pg_size_pretty(pg_table_size(c.oid))   AS table_size,\n"
    "       pg_size_pretty(pg_indexes_size(c.oid)) AS index_size\n"
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace\n"
    "WHERE c.relkind IN ('r','p')\n"
    "  AND n.nspname NOT IN ('pg_catalog','information_schema')\n"
    "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20;"
)


INVALID_INDEX_SHOW_SQL = (
    "SELECT ns.nspname AS schema, tbl.relname AS table, idx.relname AS index,\n"
    "       ix.indisready AS ready, ix.indislive AS live,\n"
    "       (prog.pid IS NOT NULL) AS building,\n"
    "       pg_size_pretty(pg_relation_size(idx.oid)) AS size\n"
    "FROM pg_index ix\n"
    "JOIN pg_class idx ON idx.oid = ix.indexrelid\n"
    "JOIN pg_class tbl ON tbl.oid = ix.indrelid\n"
    "JOIN pg_namespace ns ON ns.oid = idx.relnamespace\n"
    "LEFT JOIN pg_stat_progress_create_index prog ON prog.index_relid = idx.oid\n"
    "WHERE NOT ix.indisvalid\n"
    "  AND ns.nspname NOT IN ('pg_catalog', 'information_schema')\n"
    "ORDER BY pg_relation_size(idx.oid) DESC;"
)


UNUSED_SHOW_SQL = (
    "SELECT s.schemaname, s.relname AS table, s.indexrelname AS index,\n"
    "       s.idx_scan AS scans,\n"
    "       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size\n"
    "FROM pg_stat_user_indexes s\n"
    "JOIN pg_index i ON i.indexrelid = s.indexrelid\n"
    "WHERE s.idx_scan = 0 AND NOT i.indisprimary AND NOT i.indisunique\n"
    "ORDER BY pg_relation_size(s.indexrelid) DESC;"
)


VACUUM_SHOW_SQL = (
    "SELECT schemaname, relname, n_live_tup, n_dead_tup,\n"
    "       GREATEST(last_vacuum, last_autovacuum)   AS last_vacuum,\n"
    "       GREATEST(last_analyze, last_autoanalyze) AS last_analyze\n"
    "FROM pg_stat_user_tables\n"
    "ORDER BY n_dead_tup DESC;"
)


# Trimmed, runnable form of BLOAT_SQL (engine) for the card's "open in SQL".
BLOAT_SHOW_SQL = (
    "-- Estimated table bloat from pg_stats (no table scan; approximate).\n"
    "SELECT schemaname, tablename, pg_size_pretty(table_bytes) AS size,\n"
    "       pg_size_pretty(CASE WHEN relpages < otta THEN 0\n"
    "                      ELSE (bs*(relpages-otta))::bigint END) AS wasted,\n"
    "       CASE WHEN otta=0 THEN 1.0 ELSE round((relpages/otta)::numeric,2) END AS ratio\n"
    "FROM (\n"
    "  SELECT schemaname, tablename, cc.relpages, bs, pg_table_size(cc.oid) AS table_bytes,\n"
    "    ceil((cc.reltuples*((datahdr+ma-(CASE WHEN datahdr%ma=0 THEN ma ELSE datahdr%ma END))\n"
    "         +nullhdr2+4))/(bs-20::float)) AS otta\n"
    "  FROM (SELECT ma,bs,schemaname,tablename,\n"
    "          (datawidth+(hdr+ma-(CASE WHEN hdr%ma=0 THEN ma ELSE hdr%ma END)))::numeric AS datahdr,\n"
    "          (maxfracsum*(nullhdr+ma-(CASE WHEN nullhdr%ma=0 THEN ma ELSE nullhdr%ma END))) AS nullhdr2\n"
    "        FROM (SELECT schemaname,tablename,hdr,ma,bs,\n"
    "                SUM((1-null_frac)*avg_width) AS datawidth, MAX(null_frac) AS maxfracsum,\n"
    "                hdr+(SELECT 1+count(*)/8 FROM pg_stats s2 WHERE null_frac<>0\n"
    "                     AND s2.schemaname=s.schemaname AND s2.tablename=s.tablename) AS nullhdr\n"
    "              FROM pg_stats s,(SELECT current_setting('block_size')::numeric AS bs,23 AS hdr,8 AS ma) c\n"
    "              WHERE schemaname NOT IN ('pg_catalog','information_schema') GROUP BY 1,2,3,4,5) foo) rs\n"
    "  JOIN pg_class cc ON cc.relname=rs.tablename\n"
    "  JOIN pg_namespace nn ON cc.relnamespace=nn.oid AND nn.nspname=rs.schemaname\n"
    "  WHERE cc.relkind='r' AND cc.relpages>0) sml\n"
    "ORDER BY wasted DESC LIMIT 20;"
)


FK_EDGES_SHOW_SQL = (
    "SELECT con.conname,\n"
    "       ns.nspname  || '.' || cl.relname  AS child,\n"
    "       fns.nspname || '.' || fcl.relname AS parent\n"
    "FROM pg_constraint con\n"
    "JOIN pg_class cl      ON cl.oid = con.conrelid\n"
    "JOIN pg_namespace ns  ON ns.oid = cl.relnamespace\n"
    "JOIN pg_class fcl     ON fcl.oid = con.confrelid\n"
    "JOIN pg_namespace fns ON fns.oid = fcl.relnamespace\n"
    "WHERE con.contype = 'f'\n"
    "  AND ns.nspname NOT IN ('pg_catalog','information_schema')\n"
    "ORDER BY child, con.conname;"
)


def dependencies(request, pk):
    """Foreign-key dependency graph: a safe TRUNCATE/load order for the tables,
    and any FK cycle. Read-only — it only reads the catalog and computes."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        graph = get_engine(connection).dependency_graph()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/dependencies.html",
        {"connection": connection, "graph": graph,
         "fk_sql": FK_EDGES_SHOW_SQL},
    )


# Readable version of EXTENSIONS_SQL for the panel's "open in SQL" link.
EXTENSIONS_SHOW_SQL = (
    "SELECT ae.name, ae.installed_version, ae.default_version,\n"
    "       n.nspname AS schema, ae.comment\n"
    "FROM pg_available_extensions ae\n"
    "LEFT JOIN pg_extension e ON e.extname = ae.name\n"
    "LEFT JOIN pg_namespace n ON n.oid = e.extnamespace\n"
    "ORDER BY (ae.installed_version IS NULL), ae.name;"
)


def extensions(request, pk):
    """Extensions panel: what's installed in this database (\\dx) and what the
    server could install. Read-only — it only reads the catalog."""
    connection = get_object_or_404(Connection, pk=pk)
    engine = get_engine(connection)
    if not engine.supports("extensions"):
        return render(request, "partials/extensions.html",
                      {"connection": connection, "supported": False,
                       "installed": [], "available": []})
    try:
        exts = engine.list_extensions()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/extensions.html",
        {"connection": connection, "supported": True,
         "installed": [e for e in exts if e.installed],
         "available": [e for e in exts if not e.installed],
         "extensions_sql": EXTENSIONS_SHOW_SQL},
    )


FK_MISSING_INDEX_SHOW_SQL = (
    "SELECT con.conrelid::regclass AS table, con.conname AS constraint,\n"
    "       pg_get_constraintdef(con.oid) AS definition\n"
    "FROM pg_constraint con\n"
    "WHERE con.contype = 'f'\n"
    "  AND NOT EXISTS (\n"
    "    SELECT 1 FROM pg_index idx\n"
    "    WHERE idx.indrelid = con.conrelid\n"
    "      AND cardinality(con.conkey) <= idx.indnkeyatts\n"
    "      AND (string_to_array(idx.indkey::text, ' ')::int2[])"
    "[1:cardinality(con.conkey)] @> con.conkey\n"
    "  )\n"
    "ORDER BY 1, 2;"
)


def health(request, pk):
    """Health panel: table sizes, unused/redundant indexes, FK indexes,
    dead-tuple/vacuum, bloat."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        engine = get_engine(connection)
        # Sizes and bloat are the probes that measure on-disk footprint, so they
        # open user tables and a DDL lock can stop them (see
        # PostgresEngine._size_probe_cursor). Everything else here reads catalog
        # and stats views, so let those two degrade their own card rather than
        # blanking a panel whose remaining cards would have loaded fine.
        try:
            sizes, sizes_error = engine.table_sizes(), None
        except EngineError as exc:
            sizes, sizes_error = [], str(exc)
        unused = engine.unused_indexes()
        invalid = engine.invalid_indexes() if engine.supports("invalid_index") else []
        fk_missing = engine.fk_missing_indexes() if engine.supports("fk_index") else []
        duplicates = engine.duplicate_indexes()
        orphans = engine.orphan_candidates() if engine.supports("orphans") else []
        vacuum = engine.vacuum_stats()
        try:
            bloat, bloat_error = engine.bloat_estimates(), None
        except EngineError as exc:
            bloat, bloat_error = [], str(exc)
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/health.html",
        {
            "connection": connection,
            "sizes": sizes,
            "sizes_error": sizes_error,
            "max_bytes": max((s.total_bytes for s in sizes), default=0),
            "unused": unused,
            "invalid": invalid,
            "fk_missing": fk_missing,
            "duplicates": duplicates,
            "orphan_candidates": orphans,
            "vacuum": vacuum,
            "bloat": bloat,
            "bloat_error": bloat_error,
            # Some engines have no vacuum/bloat concept (e.g. MySQL/InnoDB), and
            # MySQL auto-indexes FK columns so "FK missing index" can't apply: the
            # panel shows a "not applicable here" note instead of an empty card.
            "supports_vacuum": engine.supports("vacuum"),
            "supports_bloat": engine.supports("bloat"),
            "supports_fk_index": engine.supports("fk_index"),
            "supports_orphans": engine.supports("orphans"),
            "supports_invalid_index": engine.supports("invalid_index"),
            "sizes_sql": SIZES_SHOW_SQL,
            "unused_sql": UNUSED_SHOW_SQL,
            "invalid_index_sql": INVALID_INDEX_SHOW_SQL,
            "fk_index_sql": FK_MISSING_INDEX_SHOW_SQL,
            "vacuum_sql": VACUUM_SHOW_SQL,
            "bloat_sql": BLOAT_SHOW_SQL,
        },
    )


def orphan_count(request, pk):
    """On-demand orphan count for one referential relationship, rendered as a
    small inline partial back into the Health card. Read-only: it runs a LEFT JOIN
    anti-join under a statement timeout, never validating or writing. The request
    names *which* relationship (a NOT VALID FK by `constraint`, or an inferred
    `<base>_id` by `column`); the engine re-derives the parent itself."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.GET.get("schema", "")
    table = request.GET.get("table", "")
    constraint = request.GET.get("constraint") or None
    column = request.GET.get("column") or None
    ctx = {"connection": connection, "schema": schema, "table": table}
    try:
        engine = get_engine(connection)
        if not engine.supports("orphans"):
            return render(request, "partials/orphan_count.html",
                          {**ctx, "unsupported": True})
        ctx["result"] = engine.orphan_count(
            schema, table, constraint=constraint, column=column)
    except EngineError as exc:
        return render(request, "partials/orphan_count.html",
                      {**ctx, "error": str(exc)})
    return render(request, "partials/orphan_count.html", ctx)
