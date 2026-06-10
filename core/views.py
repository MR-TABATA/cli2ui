from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.urls import reverse

from .engines import EngineError, get_engine
from .forms import ConnectionForm
from .models import Connection

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
    "password": "demo",
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
    try:
        engine = get_engine(connection)
        columns = engine.list_columns(schema, table)
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
            "preview_columns": preview.columns,
            "preview_rows": rows,
            "query_sql": f'SELECT * FROM "{schema}"."{table}" LIMIT 100',
        },
    )


def query(request, pk):
    """SQL runner: render the read-only editor panel (htmx partial)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(
        request,
        "partials/query.html",
        {"connection": connection, "sql": request.GET.get("sql", "")},
    )


def query_run(request, pk):
    """Execute ad-hoc SQL read-only and render the result grid."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    if not sql_text:
        return render(request, "partials/query_result.html", {"empty": True})
    try:
        result = get_engine(connection).run_query(sql_text)
    except EngineError as exc:
        return render(request, "partials/query_result.html", {"error": str(exc)})

    rows = [["" if v is None else v for v in row] for row in result.rows]
    return render(
        request,
        "partials/query_result.html",
        {"result": result, "rows": rows},
    )


def objects(request, pk):
    """Catalog browser: databases (\\l), schemas (\\dn), roles (\\du) — htmx partial."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_objects(request, connection)


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


def schema_delete(request, pk):
    """Drop a schema (DROP SCHEMA), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = request.POST.get("name", "")
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


def role_delete(request, pk):
    """Drop a role (DROP ROLE), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = request.POST.get("name", "")
    try:
        get_engine(connection).drop_role(name)
    except EngineError as exc:
        return _render_objects(request, connection, error=str(exc))
    return _render_objects(request, connection)


def _render_objects(request, connection, error=None):
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
