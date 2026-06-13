"""Connection lifecycle and the workspace shell: landing page, connect, the
workspace frame and its home overview."""
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse

from ..engines import EngineError, get_engine
from ..forms import ConnectionForm
from ..models import Connection


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
    """DB-client view: table list in the sidebar, table detail in the main pane.
    The main pane starts on the bento overview, so the summaries are gathered
    here too (a static include — no extra round trip that could race a click)."""
    connection = get_object_or_404(Connection, pk=pk)
    try:
        tables = get_engine(connection).list_tables()
    except EngineError as exc:
        return render(
            request,
            "workspace.html",
            {"connection": connection, "tables": [], "error": str(exc),
             "connections": Connection.objects.all(),
             "summary": {}, "commands": 0},
        )
    return render(
        request,
        "workspace.html",
        {"connection": connection, "tables": tables,
         "connections": Connection.objects.all(),
         "summary": _overview_summary(connection),
         "commands": connection.commands.count()},
    )


def _overview_summary(connection):
    """At-a-glance operational stats for the bento overview. Each group is
    guarded on its own so one failing probe (or a locked-down DB) degrades that
    card to '—' rather than breaking the whole home page. Returns a dict whose
    values are None when their probe failed."""
    engine = get_engine(connection)
    s = {"tables": None, "activity": None, "health": None,
         "objects": None, "replication": None}
    try:
        tables = engine.list_tables()
        s["tables"] = {"count": len(tables), "rows": sum(t.rows for t in tables)}
    except EngineError:
        pass
    try:
        acts = [a for a in engine.list_activity() if not a.is_self]
        s["activity"] = {
            "sessions": len(acts),
            "active": sum(1 for a in acts if a.state == "active"),
            "blocked": sum(1 for a in acts if a.blocked),
        }
    except EngineError:
        pass
    try:
        sizes = engine.table_sizes(limit=1)
        s["health"] = {
            "largest": sizes[0] if sizes else None,
            "unused": len(engine.unused_indexes()),
            "dead": sum(v.dead for v in engine.vacuum_stats()),
        }
    except EngineError:
        pass
    try:
        s["objects"] = {
            "databases": len(engine.list_databases()),
            "schemas": len(engine.list_schemas()),
            "roles": len(engine.list_roles()),
        }
    except EngineError:
        pass
    try:
        s["replication"] = engine.replication_status()
    except EngineError:
        pass
    return s


def overview(request, pk, notice=None):
    """The workspace home: a bento of live operational summaries that drill down
    into each panel. `notice` is an optional info banner (e.g. the auto-backup
    result after a table drop)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(request, "partials/workspace_home.html",
                  {"connection": connection, "notice": notice,
                   "summary": _overview_summary(connection),
                   "commands": connection.commands.count()})
