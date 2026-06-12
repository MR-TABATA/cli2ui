import contextlib
import difflib
import io
import json

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from django.conf import settings as django_settings

from .engines import EngineError, get_engine
from .engines.postgres import COLUMN_TYPES, INDEX_METHODS
from .forms import ConnectionForm
from .models import Backup, Command, Connection, PlanSnapshot
from .plan_diff import diff_plans, node_from_dict, node_to_dict, to_text

# Row-count multipliers for the scale simulation: now, 100×, 10000×. Enough
# spread to make the planner cross its Seq→Index / NestedLoop→Hash thresholds.
SCALE_FACTORS = (1, 100, 10000)

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

SAMPLE_INITIAL = {
    "name": "Sample shop",
    "kind": Connection.KIND_POSTGRES,
    "host": "sampledb",
    "port": 5432,
    "dbname": "shop",
    "user": "demo",
    "password": "demo",  # nosec B105 — not a secret: prefill for the bundled demo DB
}


def index(request):
    """Landing page: new-connection form (pre-filled for the sample DB) plus
    links to any saved connections."""
    return render(
        request,
        "index.html",
        {
            "form": ConnectionForm(initial=SAMPLE_INITIAL),
            "connections": Connection.objects.all(),
        },
    )


def connect(request):
    """Validate + test a connection. On success, save it and send the client to
    its workspace; on failure, render the error inline and keep the form open."""
    if request.method != "POST":
        return index(request)

    form = ConnectionForm(request.POST)
    if not form.is_valid():
        return render(request, "partials/error.html", {"errors": form.errors})

    connection = form.save(commit=False)  # don't persist a connection we can't reach
    try:
        get_engine(connection).test()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})

    connection.save()
    response = HttpResponse(status=204)
    response["HX-Redirect"] = reverse("workspace", args=[connection.pk])
    return response


def workspace(request, pk):
    """DB-client view: table list in the sidebar, table detail in the main pane."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        tables = get_engine(connection).list_tables()
    except EngineError as exc:
        return render(
            request,
            "workspace.html",
            {"connection": connection, "tables": [], "error": str(exc),
             "connections": Connection.objects.all()},
        )
    return render(
        request,
        "workspace.html",
        {"connection": connection, "tables": tables,
         "connections": Connection.objects.all()},
    )


def table_detail(request, pk):
    """Columns + a row preview for one table (htmx partial into the main pane)."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.GET.get("schema", "")
    table = request.GET.get("table", "")
    return _render_detail(request, connection, schema, table)


def _mb(n):
    return f"{n / 1048576:.0f} MB" if n >= 1048576 else f"{n / 1024:.0f} kB"


def _auto_backup(connection, *, operation, kind, dbname, schema=None, table=None):
    """Take an automatic safety snapshot (custom-format pg_dump) just before a
    destructive op and store it if it's under the size limit. Returns a short
    notice for the UI; never raises — an oversized or failed snapshot must not
    block the operation, so the caller proceeds (warned)."""
    engine = get_engine(connection)
    try:
        if kind == Backup.KIND_TABLE:
            dump = engine.dump_table(schema, table, fmt="custom")
            target = f"{schema}.{table}"
        else:
            dump = engine.dump_database(dbname, fmt="custom")
            target = dbname
    except EngineError as exc:
        return f"⚠ No backup taken — the snapshot failed: {exc}"
    limit = django_settings.CLI2UI_MAX_AUTO_BACKUP_BYTES
    if len(dump.data) > limit:
        return (f"⚠ No backup taken — {target} is {_mb(len(dump.data))}, "
                f"over the {_mb(limit)} limit.")
    Backup.objects.create(
        connection=connection, operation=operation, kind=kind, target=target,
        dbname=dbname, data=dump.data, byte_size=len(dump.data))
    return f"Backup saved before {operation} — recover it from Backups."


def _render_detail(request, connection, schema, table, error=None, notice=None):
    """Render the table-detail panel: columns, indexes and a row preview.

    A connection-level failure falls back to the error partial; a per-action
    failure (passed in as `error`) re-renders the panel with everything intact
    plus an inline message, so an index create/drop keeps the user in place."""
    try:
        engine = get_engine(connection)
        columns = engine.list_columns(schema, table)
        indexes = engine.list_indexes(schema, table)
        preview = engine.preview_rows(schema, table)
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})

    # Blank out NULLs for a cleaner preview grid.
    rows = [["" if v is None else v for v in row] for row in preview.rows]
    return render(
        request,
        "partials/detail.html",
        {
            "connection": connection,
            "schema": schema,
            "table": table,
            "columns": columns,
            "indexes": indexes,
            "index_methods": INDEX_METHODS,
            "column_types": COLUMN_TYPES,
            "preview_columns": preview.columns,
            "preview_rows": rows,
            # Display-only: prefilled into the SQL editor as a starting point,
            # never executed server-side. The run path (run_query) is read-only
            # enforced by the DB. nosec B608 — not a query we build and execute.
            "query_sql": f'SELECT * FROM "{schema}"."{table}" LIMIT 100',  # nosec B608
            "error": error,
            "notice": notice,
        },
    )


def index_create(request, pk):
    """Create an index on a table (CREATE INDEX CONCURRENTLY), then re-render
    the table detail so the new index shows."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    columns = request.POST.getlist("columns")
    if not columns:
        return _render_detail(request, connection, schema, table,
                              error="Select at least one column to index.")
    try:
        get_engine(connection).create_index(
            schema, table, columns,
            method=request.POST.get("method", "btree"),
            unique=request.POST.get("unique") == "on",
            name=(request.POST.get("name") or "").strip() or None,
        )
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def index_drop(request, pk):
    """Drop an index (DROP INDEX), then re-render the table detail."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    try:
        get_engine(connection).drop_index(schema, request.POST.get("name", ""))
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def _refresh_table_tree(response, request, connection):
    """Out-of-band swap the sidebar table list into an existing response, so a
    rename/truncate/drop updates the tree in the same round trip as the main
    pane. Skipped silently if the connection can't be listed (the response
    already carries the relevant error)."""
    try:
        tables = get_engine(connection).list_tables()
    except EngineError:
        return response
    response.content += render_to_string(
        "partials/table_list.html",
        {"connection": connection, "tables": tables, "oob": True},
        request=request,
    ).encode()
    return response


def table_rename(request, pk):
    """Rename a table (ALTER TABLE … RENAME TO), then show the renamed table and
    refresh the sidebar tree."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    new_name = (request.POST.get("new_name") or "").strip()
    if not new_name:
        return _render_detail(request, connection, schema, table,
                              error="Enter a new table name.")
    try:
        get_engine(connection).rename_table(schema, table, new_name)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    response = _render_detail(request, connection, schema, new_name)
    return _refresh_table_tree(response, request, connection)


def table_truncate(request, pk):
    """Truncate a table (delete every row), then re-render its detail with the
    now-empty preview and refresh the sidebar row count."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    notice = _auto_backup(connection, operation="truncate", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        get_engine(connection).truncate_table(schema, table)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    response = _render_detail(request, connection, schema, table, notice=notice)
    return _refresh_table_tree(response, request, connection)


def table_drop(request, pk):
    """Drop a table. Nothing left to show, so land on the overview and drop it
    out of the sidebar tree."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    notice = _auto_backup(connection, operation="drop table", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        get_engine(connection).drop_table(schema, table)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    response = overview(request, pk, notice=notice)
    return _refresh_table_tree(response, request, connection)


def column_add(request, pk):
    """Add a column (ALTER TABLE … ADD COLUMN), then re-render the table detail."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_detail(request, connection, schema, table,
                              error="Enter a column name.")
    try:
        get_engine(connection).add_column(
            schema, table, name, request.POST.get("type", ""),
            nullable=request.POST.get("notnull") != "on",
            default=(request.POST.get("default") or "").strip() or None,
        )
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def column_rename(request, pk):
    """Rename a column (ALTER TABLE … RENAME COLUMN), then re-render detail."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    new_name = (request.POST.get("new_name") or "").strip()
    if not new_name:
        return _render_detail(request, connection, schema, table,
                              error="Enter a new column name.")
    try:
        get_engine(connection).rename_column(
            schema, table, request.POST.get("column", ""), new_name)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def column_drop(request, pk):
    """Drop a column (ALTER TABLE … DROP COLUMN), then re-render detail."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    notice = _auto_backup(connection, operation="drop column", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        get_engine(connection).drop_column(
            schema, table, request.POST.get("column", ""))
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table, notice=notice)


def column_retype(request, pk):
    """Change a column's type (ALTER COLUMN … TYPE, with a generated USING cast).
    A no-op if the chosen type matches the current one — avoids a needless and
    expensive table rewrite."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    new_type = request.POST.get("type", "")
    if new_type and new_type != request.POST.get("cur_type", ""):
        try:
            get_engine(connection).alter_column_type(
                schema, table, request.POST.get("column", ""), new_type)
        except EngineError as exc:
            return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def column_set_null(request, pk):
    """Add or drop a column's NOT NULL constraint."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    try:
        get_engine(connection).set_column_null(
            schema, table, request.POST.get("column", ""),
            nullable=request.POST.get("nullable") == "1")
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def column_set_default(request, pk):
    """Set or drop a column's DEFAULT (empty value drops it)."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    try:
        get_engine(connection).set_column_default(
            schema, table, request.POST.get("column", ""),
            (request.POST.get("default") or "").strip() or None)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def index_lab(request, pk):
    """The what-if index lab panel. Optionally prefilled (schema/table/sql) from
    an entry point — a table's "Try an index", the SQL runner, or empty from the
    nav. When a table is chosen its columns load so you can pick what to index."""
    connection = get_object_or_404(Connection, pk=pk)
    # The table dropdown sends one "schema.table" value; everything else carries
    # schema/table separately. partition() keeps the first dot as the boundary.
    qualified = request.GET.get("qualified")
    if qualified:
        schema, _, table = qualified.partition(".")
    else:
        schema = request.GET.get("schema", "")
        table = request.GET.get("table", "")
    sql_text = request.GET.get("sql", "")
    try:
        engine = get_engine(connection)
        tables = engine.list_tables()
        columns = engine.list_columns(schema, table) if schema and table else []
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    if not sql_text and schema and table:
        # Display-only starter query for the lab's editor (the user edits/runs
        # it); not a server-executed query. nosec B608 — same as table_detail.
        sql_text = f'SELECT * FROM "{schema}"."{table}"'  # nosec B608
    return render(
        request,
        "partials/index_lab.html",
        {
            "connection": connection, "tables": tables,
            "schema": schema, "table": table, "columns": columns,
            "index_methods": INDEX_METHODS, "sql": sql_text,
        },
    )


def index_lab_preview(request, pk):
    """Run the what-if trial and render the before/after timing + plan diff."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    sql_text = (request.POST.get("sql") or "").strip()
    columns = request.POST.getlist("columns")
    method = request.POST.get("method", "btree")
    unique = request.POST.get("unique") == "on"
    if not sql_text or not columns:
        return render(request, "partials/index_lab_result.html",
                      {"error": "Pick a target query and at least one column."})
    try:
        preview = get_engine(connection).preview_index(
            sql_text, schema, table, columns, method=method, unique=unique)
    except EngineError as exc:
        return render(request, "partials/index_lab_result.html",
                      {"error": str(exc)})
    diff = diff_plans(preview.before, preview.after)
    return render(
        request,
        "partials/index_lab_result.html",
        {
            "connection": connection, "preview": preview, "diff": diff,
            "verdict": _index_verdict(preview),
            "schema": schema, "table": table, "columns": columns,
            "method": method, "unique": unique,
        },
    )


def _index_verdict(preview):
    """A plain-language headline for a what-if trial, based on the real timing
    and whether the planner actually used the hypothetical index."""
    if not preview.used:
        return {"label": "index not used — wouldn't help this query", "tone": "muted"}
    s = preview.speedup
    if s and s >= 1.5:
        return {"label": f"▼ {s:.1f}× faster", "tone": "good"}
    if s and s <= 0.67:
        return {"label": f"▲ {1 / s:.1f}× slower", "tone": "bad"}
    return {"label": "≈ no measurable change", "tone": "muted"}


def overview(request, pk, notice=None):
    """The workspace home: what each section is and where to start. `notice` is
    an optional info banner (e.g. the auto-backup result after a table drop)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(request, "partials/workspace_home.html",
                  {"connection": connection, "notice": notice})


def query(request, pk):
    """SQL runner: render the read-only editor panel (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(
        request,
        "partials/query.html",
        {"connection": connection, "sql": request.GET.get("sql", "")},
    )


def query_run(request, pk):
    """Execute ad-hoc SQL and render the result grid. Read-only by default; in
    write mode the statement is committed, after an automatic safety snapshot."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    write = bool(request.POST.get("write"))
    if not sql_text:
        return render(request, "partials/query_result.html", {"empty": True})

    # Safety net: a whole-database snapshot before any arbitrary write, so the
    # change can be undone from Backups (same mechanism as drop/truncate).
    notice = None
    if write:
        notice = _auto_backup(connection, operation="write query",
                              kind=Backup.KIND_DATABASE, dbname=connection.dbname)

    try:
        result = get_engine(connection).run_query(sql_text, read_only=not write)
    except EngineError as exc:
        _log_command(connection, sql_text, read_only=not write, error=str(exc))
        return render(request, "partials/query_result.html",
                      {"error": str(exc), "notice": notice})

    _log_command(connection, sql_text, read_only=not write, result=result)
    rows = [["" if v is None else v for v in row] for row in result.rows]
    return render(
        request,
        "partials/query_result.html",
        {"result": result, "rows": rows, "notice": notice, "wrote": write},
    )


def _log_command(connection, sql_text, *, read_only, result=None, error=None):
    """Record one runner execution in the management DB. Never raises — a
    logging failure must not break the query the user actually ran."""
    try:
        Command.objects.create(
            connection=connection, sql=sql_text, read_only=read_only,
            status=Command.STATUS_ERROR if error else Command.STATUS_OK,
            rowcount=result.rowcount if result else None,
            duration_ms=result.duration_ms if result else None,
            error=error or "",
        )
    except Exception:  # noqa: BLE001 — logging is best-effort
        pass


def history(request, pk):
    """Command history: SQL run through the runner, newest first (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_history(request, connection)


def history_clear(request, pk):
    """Delete this connection's command history, then re-render the panel."""
    connection = get_object_or_404(Connection, pk=pk)
    connection.commands.all().delete()
    return _render_history(request, connection)


def _render_history(request, connection):
    return render(
        request,
        "partials/history.html",
        {"connection": connection, "commands": connection.commands.all()[:200]},
    )


def explain_run(request, pk):
    """Run EXPLAIN on the editor's SQL and show the plan with a save affordance."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    analyze = request.POST.get("analyze") == "1"
    if not sql_text:
        return render(request, "partials/query_result.html", {"empty": True})
    try:
        node = get_engine(connection).explain_json(sql_text, analyze=analyze)
    except EngineError as exc:
        return render(request, "partials/query_result.html", {"error": str(exc)})

    # Render the plan from the parsed tree (so the saved snapshot carries the
    # structured form for node-level diffs, no second EXPLAIN at save time).
    return render(
        request,
        "partials/explain_result.html",
        {
            "connection": connection, "sql": sql_text,
            "plan": to_text(node),
            "plan_json": json.dumps(node_to_dict(node)),
            "analyzed": analyze,
        },
    )


def snapshot_save(request, pk):
    """Persist an EXPLAIN plan as a named snapshot, then confirm inline."""
    connection = get_object_or_404(Connection, pk=pk)
    label = (request.POST.get("label") or "").strip()
    if not label:
        label = timezone.now().strftime("plan %Y-%m-%d %H:%M:%S")
    snapshot = PlanSnapshot.objects.create(
        connection=connection,
        label=label,
        sql=request.POST.get("sql", ""),
        plan_text=request.POST.get("plan_text", ""),
        plan_json=request.POST.get("plan_json", ""),
        analyzed=request.POST.get("analyzed") == "1",
    )
    return render(request, "partials/snapshot_saved.html",
                  {"connection": connection, "snapshot": snapshot})


def snapshots(request, pk):
    """List saved plan snapshots with an A/B compare bar (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(
        request,
        "partials/snapshots.html",
        {"connection": connection, "snapshots": connection.snapshots.all()},
    )


def snapshot_plan(request, pk):
    """Show the exact plan text that was saved (not a re-run)."""
    connection = get_object_or_404(Connection, pk=pk)
    snap = connection.snapshots.filter(pk=request.GET.get("id")).first()
    if not snap:
        return HttpResponse("")
    return render(request, "partials/snapshot_plan.html", {"snapshot": snap})


def snapshot_delete(request, pk):
    """Delete one snapshot, then re-render the snapshots panel."""
    connection = get_object_or_404(Connection, pk=pk)
    connection.snapshots.filter(pk=request.POST.get("id")).delete()
    return render(
        request,
        "partials/snapshots.html",
        {"connection": connection, "snapshots": connection.snapshots.all()},
    )


def snapshot_diff(request, pk):
    """Diff two saved plans (A → B). Structured node-level diff when both plans
    have the JSON form; falls back to a text diff for older snapshots."""
    connection = get_object_or_404(Connection, pk=pk)
    a = connection.snapshots.filter(pk=request.GET.get("a")).first()
    b = connection.snapshots.filter(pk=request.GET.get("b")).first()
    if not a or not b:
        return render(request, "partials/snapshot_diff.html", {"missing": True})

    if a.plan_json and b.plan_json:
        diff = diff_plans(node_from_dict(json.loads(a.plan_json)),
                          node_from_dict(json.loads(b.plan_json)))
        return render(
            request,
            "partials/snapshot_diff.html",
            {"connection": connection, "a": a, "b": b,
             "structured": diff, "identical": diff.identical},
        )

    lines = []
    for line in difflib.unified_diff(
        a.plan_text.splitlines(), b.plan_text.splitlines(),
        lineterm="", n=3,
    ):
        if line.startswith(("---", "+++")):
            continue  # drop file headers; we show labels ourselves
        if line.startswith("@@"):
            kind = "hunk"
        elif line.startswith("+"):
            kind = "add"
        elif line.startswith("-"):
            kind = "del"
        else:
            kind = "ctx"
        lines.append({"kind": kind, "text": line})

    return render(
        request,
        "partials/snapshot_diff.html",
        {"connection": connection, "a": a, "b": b, "lines": lines,
         "identical": not lines},
    )


def scale_run(request, pk):
    """Scale simulation: EXPLAIN the editor's query at 1× / 100× / 10000× the
    real row counts, then structurally diff adjacent plans so you can see where
    the plan shape breaks as the data grows."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    if not sql_text:
        return render(request, "partials/query_result.html", {"empty": True})
    try:
        plans = get_engine(connection).simulate_scale(sql_text, factors=SCALE_FACTORS)
    except EngineError as exc:
        return render(request, "partials/query_result.html", {"error": str(exc)})

    # Diff each factor against the previous one — the jump that flips a scan or
    # join is the headline.
    steps = [
        {"a": prev, "b": cur, "diff": diff_plans(prev.plan, cur.plan)}
        for prev, cur in zip(plans, plans[1:])
    ]
    return render(
        request,
        "partials/scale_result.html",
        {"connection": connection, "sql": sql_text, "plans": plans, "steps": steps},
    )


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


def objects(request, pk):
    """Catalog browser: databases (\\l), schemas (\\dn), roles (\\du) — htmx partial."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_objects(request, connection)


def database_create(request, pk):
    """Create a database, optionally cloning one via TEMPLATE, then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Database name is required.")
    template = (request.POST.get("template") or "").strip() or None
    try:
        get_engine(connection).create_database(name, template=template)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def database_drop(request, pk):
    """Drop a database (DROP DATABASE [WITH FORCE]), then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Database name is required.")
    notice = _auto_backup(connection, operation="drop database",
                          kind=Backup.KIND_DATABASE, dbname=name)
    try:
        get_engine(connection).drop_database(
            name, force=request.POST.get("force") == "on")
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection, notice=notice)


def database_rename(request, pk):
    """Rename a database (ALTER DATABASE … RENAME TO), then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    old = (request.POST.get("old") or "").strip()
    new = (request.POST.get("new") or "").strip()
    if not old or not new:
        return _render_objects(request, connection, error="Both names are required.")
    try:
        get_engine(connection).rename_database(old, new)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def _dump_download(dump):
    """Wrap a Dump as an attachment download."""
    response = HttpResponse(dump.data, content_type=dump.content_type)
    response["Content-Disposition"] = f'attachment; filename="{dump.filename}"'
    return response


def database_dump(request, pk):
    """Stream a pg_dump of a database to the browser as a download. A GET so a
    plain link/anchor (target=_blank) triggers it; the dump only reads the DB.
    On failure, return plain text — the link opens in a new tab, so the
    workspace stays intact."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        dump = get_engine(connection).dump_database(
            request.GET.get("name", ""),
            fmt=request.GET.get("format", "plain"))
    except EngineError as exc:
        return HttpResponse(str(exc), content_type="text/plain", status=502)
    return _dump_download(dump)


def _restore_into_new_db(connection, name, stream):
    """Create a brand-new database (from the pristine template0) and restore the
    dump (a file-like object, streamed) into it. If the restore fails, drop the
    just-made database so a failed attempt leaves nothing behind. Returns an
    error string, or None on success."""
    if not name:
        return "Provide a new database name."
    engine = get_engine(connection)
    try:
        engine.create_database(name, template="template0")
    except EngineError as exc:
        return str(exc)
    try:
        engine.restore_stream(name, stream)
    except EngineError as exc:
        with contextlib.suppress(EngineError):
            engine.drop_database(name, force=True)
        return f"Restore failed — database not created. {exc}"
    return None


def _restore_into_existing_db(connection, name, stream):
    """Restore the dump (streamed) into an existing database, without creating or
    dropping anything. The caller is responsible for the type-gate confirmation —
    this overwrites/merges into a live database. Returns an error or None."""
    if not name:
        return "Provide the database name."
    try:
        get_engine(connection).restore_stream(name, stream)
    except EngineError as exc:
        return f"Restore failed. {exc}"
    return None


def database_restore(request, pk):
    """Restore an uploaded dump into a new database (default) or, with a type-gate
    confirmation, into an existing one. The upload is streamed to the client tool,
    not read wholly into memory."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    target = request.POST.get("target", "new")
    upload = request.FILES.get("dump")
    if not name or not upload:
        return _render_objects(
            request, connection,
            error="Provide a database name and a dump file to restore.")
    if target == "existing":
        # Type-gate: the user must type the database name to confirm overwriting
        # a live database (there's no auto-snapshot here — it's their own dump).
        if (request.POST.get("confirm") or "").strip() != name:
            return _render_objects(
                request, connection,
                error=f"To restore into the existing “{name}”, type its name to confirm.")
        err = _restore_into_existing_db(connection, name, upload)
        notice = f"Restored into existing database “{name}”."
    else:
        err = _restore_into_new_db(connection, name, upload)
        notice = f"Restored into new database “{name}”."
    if err:
        return _render_objects(request, connection, error=err)
    return _render_objects(request, connection, notice=notice)


def table_dump(request, pk):
    """pg_dump a single table (-t) as a download. GET, like database_dump."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        dump = get_engine(connection).dump_table(
            request.GET.get("schema", ""), request.GET.get("table", ""),
            fmt=request.GET.get("format", "plain"))
    except EngineError as exc:
        return HttpResponse(str(exc), content_type="text/plain", status=502)
    return _dump_download(dump)


def _render_backups(request, connection, error=None, notice=None):
    return render(request, "partials/backups.html",
                  {"connection": connection, "backups": connection.backups.all(),
                   "error": error, "notice": notice})


def backups(request, pk):
    """The automatic safety snapshots taken before destructive operations."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_backups(request, connection)


def backup_download(request, pk):
    """Download a stored snapshot as a custom-format .dump file."""
    connection = get_object_or_404(Connection, pk=pk)
    backup = get_object_or_404(Backup, pk=request.GET.get("id"), connection=connection)
    stamp = backup.created_at.strftime("%Y%m%d-%H%M%S")
    response = HttpResponse(bytes(backup.data), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{backup.target}-{stamp}.dump"'
    return response


def backup_delete(request, pk):
    """Delete a stored snapshot, then re-render the list."""
    connection = get_object_or_404(Connection, pk=pk)
    Backup.objects.filter(pk=request.POST.get("id"), connection=connection).delete()
    return _render_backups(request, connection)


def backup_restore(request, pk):
    """Restore a stored snapshot into a brand-new database (never overwriting an
    existing one). A table snapshot lands in a fresh DB containing just that
    table, so you can recover its data without touching the original."""
    connection = get_object_or_404(Connection, pk=pk)
    backup = get_object_or_404(Backup, pk=request.POST.get("id"), connection=connection)
    name = (request.POST.get("name") or "").strip()
    err = _restore_into_new_db(connection, name, io.BytesIO(bytes(backup.data)))
    if err:
        return _render_backups(request, connection, error=err)
    return _render_backups(
        request, connection,
        notice=f"Restored “{backup.target}” into new database “{name}”.")


def schema_create(request, pk):
    """Create a schema (CREATE SCHEMA), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Schema name is required.")
    try:
        get_engine(connection).create_schema(name)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def schema_alter(request, pk):
    """Rename a schema and/or change its owner (ALTER SCHEMA), then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    old = (request.POST.get("old") or "").strip()
    new = (request.POST.get("new") or "").strip()
    owner = (request.POST.get("owner") or "").strip()
    cur_owner = request.POST.get("cur_owner", "")
    if not old:
        return _render_objects(request, connection, error="Schema name is required.")
    try:
        engine = get_engine(connection)
        if owner and owner != cur_owner:
            engine.alter_schema_owner(old, owner)   # on the current name
        if new and new != old:
            engine.rename_schema(old, new)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def schema_delete(request, pk):
    """Drop a schema (DROP SCHEMA), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Schema name is required.")
    try:
        get_engine(connection).drop_schema(name)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def role_create(request, pk):
    """Create a role (CREATE ROLE), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Role name is required.")
    try:
        get_engine(connection).create_role(
            name,
            login=request.POST.get("login") == "on",
            password=(request.POST.get("password") or "").strip() or None,
            superuser=request.POST.get("superuser") == "on",
            createdb=request.POST.get("createdb") == "on",
            createrole=request.POST.get("createrole") == "on",
        )
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def role_alter(request, pk):
    """Change a role's attributes and/or rename it (ALTER ROLE), then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    old = (request.POST.get("old") or "").strip()
    new = (request.POST.get("new") or "").strip()
    if not old:
        return _render_objects(request, connection, error="Role name is required.")
    try:
        engine = get_engine(connection)
        engine.alter_role(
            old,
            login=request.POST.get("login") == "on",
            superuser=request.POST.get("superuser") == "on",
            createdb=request.POST.get("createdb") == "on",
            createrole=request.POST.get("createrole") == "on",
            password=(request.POST.get("password") or "").strip() or None,
        )
        if new and new != old:
            engine.rename_role(old, new)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def role_delete(request, pk):
    """Drop a role (DROP ROLE), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error="Role name is required.")
    try:
        get_engine(connection).drop_role(name)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def _render_objects(request, connection, error=None, notice=None):
    """Gather databases / schemas / roles and render the objects panel.

    A connection-level failure falls back to the error partial; a per-action
    failure (passed in as `error`) re-renders the panel with the list intact
    plus an inline message, so the user keeps their place."""
    try:
        engine = get_engine(connection)
        databases = engine.list_databases()
        schemas = engine.list_schemas()
        roles = engine.list_roles()
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})

    return render(
        request,
        "partials/objects.html",
        {
            "connection": connection,
            "databases": databases,
            "schemas": schemas,
            "roles": roles,
            "error": error,
            "notice": notice,
        },
    )


def settings(request, pk):
    """postgresql.conf editor: read parameters via pg_settings (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    category = request.GET.get("category") or ""
    try:
        engine = get_engine(connection)
        if category:
            rows = engine.list_settings(category=category)
        else:
            rows = engine.list_settings(names=COMMON_SETTINGS)
        categories = engine.list_setting_categories()
        pending = [s.name for s in engine.pending_restart_settings()]
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})

    return render(
        request,
        "partials/settings.html",
        {
            "connection": connection,
            "settings": rows,
            "categories": categories,
            "category": category,
            "pending": pending,
        },
    )


def settings_update(request, pk):
    """Apply one parameter change (ALTER SYSTEM SET + reload), re-render its row."""
    connection = get_object_or_404(Connection, pk=pk)
    name = request.POST.get("name", "")
    value = request.POST.get("value", "")
    engine = get_engine(connection)
    error = None
    try:
        setting = engine.update_setting(name, value)
    except EngineError as exc:
        error = str(exc)
        try:
            found = engine.list_settings(names=[name])
        except EngineError:
            found = []
        if not found:
            return render(request, "partials/error.html", {"message": error})
        setting = found[0]

    return _row_with_banner(request, connection, engine, setting, error is None, error)


def settings_reset(request, pk):
    """Revert one parameter to its default (ALTER SYSTEM RESET + reload)."""
    connection = get_object_or_404(Connection, pk=pk)
    name = request.POST.get("name", "")
    engine = get_engine(connection)
    try:
        setting = engine.reset_setting(name)
    except EngineError as exc:
        return render(request, "partials/error.html", {"message": str(exc)})
    return _row_with_banner(request, connection, engine, setting, True, None)


def _row_with_banner(request, connection, engine, setting, saved, error):
    """Return the updated setting row plus an out-of-band refresh of the
    restart banner, so staging a restart-only change updates both at once."""
    row = render_to_string(
        "partials/settings_row.html",
        {"connection": connection, "s": setting, "saved": saved, "error": error},
        request=request,
    )
    try:
        pending = [s.name for s in engine.pending_restart_settings()]
    except EngineError:
        pending = []
    banner = render_to_string(
        "partials/restart_banner.html",
        {"connection": connection, "pending": pending, "oob": True},
        request=request,
    )
    return HttpResponse(row + banner)
