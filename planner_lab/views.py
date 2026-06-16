"""The planner workbench panels: the scale simulation and the what-if index lab.
Split out of core so the whole feature is one removable app."""
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _

from core.engines import EngineError, get_engine
from core.engines.postgres import INDEX_METHODS
from core.models import Connection
from core.plan_diff import diff_plans

from .whatif import preview_index, simulate_scale

# Row-count multipliers for the scale simulation: now, 100×, 10000×. Enough
# spread to make the planner cross its Seq→Index / NestedLoop→Hash thresholds.
SCALE_FACTORS = (1, 100, 10000)


def scale_run(request, pk):
    """Scale simulation: EXPLAIN the editor's query at 1× / 100× / 10000× the
    real row counts, then structurally diff adjacent plans so you can see where
    the plan shape breaks as the data grows."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    if not sql_text:
        return render(request, "partials/query_result.html", {"empty": True})
    try:
        plans = simulate_scale(get_engine(connection), sql_text, factors=SCALE_FACTORS)
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
        schema, _sep, table = qualified.partition(".")
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
                      {"error": _("Pick a target query and at least one column.")})
    try:
        preview = preview_index(
            get_engine(connection), sql_text, schema, table, columns,
            method=method, unique=unique)
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
