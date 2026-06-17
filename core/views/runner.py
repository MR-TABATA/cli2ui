"""The SQL runner: ad-hoc queries, result export, command history, and EXPLAIN.
The planner what-if tools (scale simulation, index lab) live in the planner_lab
app."""
import csv
import json

from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.translation import gettext as _

from ..engines import EngineError, get_engine
from ..models import Backup, Command, Connection
from ..plan_diff import node_to_dict, to_text
from ._shared import _auto_backup


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
        {"connection": connection, "sql": sql_text, "result": result,
         "rows": rows, "notice": notice, "wrote": write},
    )


class _Echo:
    """A write-only file that just returns what it's given, so csv.writer can
    hand each formatted row straight to the streaming response."""

    def write(self, value):
        return value


def _csv_stream(columns, rows):
    """Yield a CSV file row by row. The leading BOM makes Excel open it as UTF-8
    (the common 'CSV → Excel' handoff) without mangling non-ASCII."""
    writer = csv.writer(_Echo())
    yield "﻿"  # UTF-8 BOM, so Excel detects the encoding
    if columns:
        yield writer.writerow(columns)
    for row in rows:
        yield writer.writerow(["" if v is None else v for v in row])


def _json_stream(columns, rows):
    """Yield a JSON array of {column: value} objects, one row at a time. default=str
    serialises dates/Decimals/etc.; ensure_ascii=False keeps text human-readable."""
    yield "["
    first = True
    for row in rows:
        obj = dict(zip(columns, row))
        yield ("" if first else ",") + json.dumps(obj, default=str, ensure_ascii=False)
        first = False
    yield "]"


def query_export(request, pk):
    """Stream the read-only query's *full* result (not the 1000-row display cap)
    as a CSV or JSON download. A POST so the arbitrary, possibly long SQL rides
    in the body; the browser saves the streamed response as a file."""
    connection = get_object_or_404(Connection, pk=pk)
    sql_text = (request.POST.get("sql") or "").strip()
    fmt = request.POST.get("format", "csv")
    if not sql_text:
        return HttpResponse(_("Nothing to export — run a query first."),
                            content_type="text/plain", status=400)

    source = get_engine(connection).stream_query(sql_text)
    # Pull the header first: this runs the query, so a bad query surfaces here as
    # a clean error response. Once streaming starts we're committed to a 200.
    try:
        columns = next(source)
    except EngineError as exc:
        return HttpResponse(str(exc), content_type="text/plain", status=502)

    stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
    if fmt == "json":
        body, ctype, ext = _json_stream(columns, source), "application/json", "json"
    else:
        body, ctype, ext = _csv_stream(columns, source), "text/csv; charset=utf-8", "csv"
    response = StreamingHttpResponse(body, content_type=ctype)
    response["Content-Disposition"] = f'attachment; filename="query-{stamp}.{ext}"'
    return response


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
    except Exception:  # noqa: BLE001  # nosec B110 — logging is best-effort; a failed history write must never break the user's query
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
