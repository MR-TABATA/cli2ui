"""Live operations panels: activity, locks/blocking, replication and health."""
from django.shortcuts import get_object_or_404, render

from ..engines import EngineError, get_engine
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
        sessions = get_engine(connection).list_activity()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/activity.html",
        {"connection": connection, "sessions": sessions,
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
    return render(
        request,
        "partials/locks.html",
        {"connection": connection, "waits": waits,
         "query_sql": BLOCKING_SHOW_SQL, "error": error},
    )


# Readable versions of the replication queries, for each table's "open in SQL".
STANDBYS_SHOW_SQL = (
    "SELECT pid, usename, application_name, client_addr, state, sync_state,\n"
    "       sent_lsn, replay_lsn,\n"
    "       pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes\n"
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
        return _render_replication(request, connection, error="Slot name is required.")
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
        return _render_replication(request, connection, error="Slot name is required.")
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
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/replication.html",
        {"connection": connection, "status": status, "standbys": standbys,
         "slots": slots, "standbys_sql": STANDBYS_SHOW_SQL,
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


def health(request, pk):
    """Health panel: table sizes, unused indexes, dead-tuple/vacuum, bloat."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        engine = get_engine(connection)
        sizes = engine.table_sizes()
        unused = engine.unused_indexes()
        vacuum = engine.vacuum_stats()
        bloat = engine.bloat_estimates()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return render(
        request,
        "partials/health.html",
        {
            "connection": connection,
            "sizes": sizes,
            "max_bytes": max((s.total_bytes for s in sizes), default=0),
            "unused": unused,
            "vacuum": vacuum,
            "bloat": bloat,
            "sizes_sql": SIZES_SHOW_SQL,
            "unused_sql": UNUSED_SHOW_SQL,
            "vacuum_sql": VACUUM_SHOW_SQL,
            "bloat_sql": BLOAT_SHOW_SQL,
        },
    )
