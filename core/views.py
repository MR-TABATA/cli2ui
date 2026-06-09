from django.shortcuts import get_object_or_404, render

from .engines import EngineError, get_engine
from .forms import ConnectionForm
from .models import Connection


def index(request):
    """Landing page: saved connections + a new-connection form.

    Form fields are pre-filled to point at the bundled sample DB, so a fresh
    `docker compose up` lets you click straight through to a table list.
    """
    initial = {
        "name": "Sample shop",
        "kind": Connection.KIND_POSTGRES,
        "host": "sampledb",
        "port": 5432,
        "dbname": "shop",
        "user": "demo",
        "password": "demo",
    }
    return render(
        request,
        "index.html",
        {
            "form": ConnectionForm(initial=initial),
            "connections": Connection.objects.all(),
        },
    )


def connect(request):
    """Save a connection, test it, and render its table list (htmx partial)."""
    if request.method != "POST":
        return index(request)

    form = ConnectionForm(request.POST)
    if not form.is_valid():
        return render(request, "partials/error.html", {"errors": form.errors})

    connection = form.save()
    return _render_tables(request, connection)


def tables(request, pk):
    connection = get_object_or_404(Connection, pk=pk)
    return _render_tables(request, connection)


def _render_tables(request, connection):
    try:
        engine = get_engine(connection)
        table_list = engine.list_tables()
    except EngineError as exc:
        return render(
            request,
            "partials/error.html",
            {"message": str(exc), "connection": connection},
        )

    template = "partials/tables.html" if request.headers.get("HX-Request") else "tables.html"
    return render(
        request,
        template,
        {"connection": connection, "tables": table_list},
    )
