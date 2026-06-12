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


def overview(request, pk, notice=None):
    """The workspace home: what each section is and where to start. `notice` is
    an optional info banner (e.g. the auto-backup result after a table drop)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(request, "partials/workspace_home.html",
                  {"connection": connection, "notice": notice})
