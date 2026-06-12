"""The SQL runner and planner workbench: ad-hoc queries, command history,
EXPLAIN, the scale simulation and the what-if index lab."""
import json

from django.shortcuts import get_object_or_404, render

from ..engines import EngineError, get_engine
from ..engines.postgres import INDEX_METHODS
from ..models import Backup, Command, Connection
from ..plan_diff import diff_plans, node_to_dict, to_text
from ._shared import _auto_backup


# Row-count multipliers for the scale simulation: now, 100×, 10000×. Enough
# spread to make the planner cross its Seq→Index / NestedLoop→Hash thresholds.
SCALE_FACTORS = (1, 100, 10000)


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
