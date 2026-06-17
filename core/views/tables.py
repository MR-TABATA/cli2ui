"""Table and column operations, plus the table-detail panel (columns, indexes,
row preview) they re-render into."""
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from ..engines import EngineError, get_engine
from ..engines.postgres import COLUMN_TYPES, INDEX_METHODS
from ..models import Backup, Connection
from ._shared import _auto_backup
from .connection import overview


def table_detail(request, pk):
    """Columns + a row preview for one table (htmx partial into the main pane)."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.GET.get("schema", "")
    table = request.GET.get("table", "")
    return _render_detail(request, connection, schema, table)


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
    # Quote identifiers the way the target engine expects, so the prefilled
    # starter query is runnable as-is (MySQL uses backticks; double quotes are
    # string literals there unless ANSI_QUOTES is set).
    q = "`" if connection.kind == Connection.KIND_MYSQL else '"'
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
            "query_sql": f"SELECT * FROM {q}{schema}{q}.{q}{table}{q} LIMIT 100",  # nosec B608
            "error": error,
            "notice": notice,
        },
    )


def table_filter(request, pk):
    """Run the filter builder (column/operator/value rows, ANDed) as a read-only
    query and render just the result grid (htmx partial into the Data tab).
    Parallel POST arrays col/op/val line up by index; blank-column rows are
    dropped so an all-empty form acts as 'show everything'."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    cols = request.POST.getlist("col")
    ops = request.POST.getlist("op")
    vals = request.POST.getlist("val")
    filters = [
        {"column": c, "op": o, "value": v}
        for c, o, v in zip(cols, ops, vals) if c
    ]
    try:
        result = get_engine(connection).filter_rows(schema, table, filters)
    except EngineError as exc:
        return render(request, "partials/filter_result.html", {"error": str(exc)})
    rows = [["" if v is None else v for v in row] for row in result.rows]
    return render(request, "partials/filter_result.html",
                  {"result": result, "rows": rows})


def table_import(request, pk):
    """Import a CSV into the table (COPY, matched by header name). A safety
    snapshot is taken first — import only appends rows, but a snapshot lets you
    restore the table if the new data is wrong. Re-renders the detail panel with
    a row count, or an inline error if the file/types don't fit."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    upload = request.FILES.get("file")
    if not upload:
        return _render_detail(request, connection, schema, table,
                              error=_("Choose a CSV file to import."))
    notice = _auto_backup(connection, operation="CSV import", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        count = get_engine(connection).import_csv(schema, table, upload)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table,
                              error=str(exc), notice=notice)
    msg = _("Imported %(n)s row(s) from %(name)s.") % {"n": count, "name": upload.name}
    full = f"{msg} {notice}" if notice else msg
    response = _render_detail(request, connection, schema, table, notice=full)
    return _refresh_table_tree(response, request, connection)


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


def index_create(request, pk):
    """Create an index on a table (CREATE INDEX CONCURRENTLY), then re-render
    the table detail so the new index shows."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    columns = request.POST.getlist("columns")
    if not columns:
        return _render_detail(request, connection, schema, table,
                              error=_("Select at least one column to index."))
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
        get_engine(connection).drop_index(schema, request.POST.get("name", ""), table)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table)


def table_rename(request, pk):
    """Rename a table (ALTER TABLE … RENAME TO), then show the renamed table and
    refresh the sidebar tree."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    new_name = (request.POST.get("new_name") or "").strip()
    if not new_name:
        return _render_detail(request, connection, schema, table,
                              error=_("Enter a new table name."))
    notice = _auto_backup(connection, operation="rename table", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        get_engine(connection).rename_table(schema, table, new_name)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    response = _render_detail(request, connection, schema, new_name, notice=notice)
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
                              error=_("Enter a column name."))
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
                              error=_("Enter a new column name."))
    notice = _auto_backup(connection, operation="rename column", kind=Backup.KIND_TABLE,
                          dbname=connection.dbname, schema=schema, table=table)
    try:
        get_engine(connection).rename_column(
            schema, table, request.POST.get("column", ""), new_name)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table, notice=notice)


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
        # A type change can transform or lose data (the USING cast), so snapshot
        # the table first — unlike the no-op path below, which changes nothing.
        notice = _auto_backup(connection, operation="change column type",
                              kind=Backup.KIND_TABLE, dbname=connection.dbname,
                              schema=schema, table=table)
        try:
            get_engine(connection).alter_column_type(
                schema, table, request.POST.get("column", ""), new_type)
        except EngineError as exc:
            return _render_detail(request, connection, schema, table, error=str(exc))
        return _render_detail(request, connection, schema, table, notice=notice)
    return _render_detail(request, connection, schema, table)


def column_set_null(request, pk):
    """Add or drop a column's NOT NULL constraint."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    notice = _auto_backup(connection, operation="set column nullability",
                          kind=Backup.KIND_TABLE, dbname=connection.dbname,
                          schema=schema, table=table)
    try:
        get_engine(connection).set_column_null(
            schema, table, request.POST.get("column", ""),
            nullable=request.POST.get("nullable") == "1")
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table, notice=notice)


def column_set_default(request, pk):
    """Set or drop a column's DEFAULT (empty value drops it)."""
    connection = get_object_or_404(Connection, pk=pk)
    schema = request.POST.get("schema", "")
    table = request.POST.get("table", "")
    notice = _auto_backup(connection, operation="set column default",
                          kind=Backup.KIND_TABLE, dbname=connection.dbname,
                          schema=schema, table=table)
    try:
        get_engine(connection).set_column_default(
            schema, table, request.POST.get("column", ""),
            (request.POST.get("default") or "").strip() or None)
    except EngineError as exc:
        return _render_detail(request, connection, schema, table, error=str(exc))
    return _render_detail(request, connection, schema, table, notice=notice)
