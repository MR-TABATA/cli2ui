"""Connection lifecycle and the workspace shell: landing page, connect, the
workspace frame and its home overview."""
import os

from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone

from ..engines import EngineError, get_engine
from ..forms import ConnectionForm
from ..models import Connection


# Every field is overridable via CLI2UI_SAMPLE_* so a deployment can point the
# prefilled form at its own bundled/demo database without forking this file.
# Unset -> identical to the historical hardcoded defaults (the bundled `shop`
# sample used by `docker compose up`).
SAMPLE_INITIAL = {
    "name": os.environ.get("CLI2UI_SAMPLE_NAME", "Sample shop"),
    "kind": Connection.KIND_POSTGRES,
    "host": os.environ.get("CLI2UI_SAMPLE_HOST", "sampledb"),
    "port": int(os.environ.get("CLI2UI_SAMPLE_PORT", "5432")),
    "dbname": os.environ.get("CLI2UI_SAMPLE_DBNAME", "shop"),
    "user": os.environ.get("CLI2UI_SAMPLE_USER", "demo"),
    "password": os.environ.get("CLI2UI_SAMPLE_PASSWORD", "demo"),  # nosec B105 — not a secret: prefill for the bundled demo DB
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


def delete_connection(request, pk):
    """Forget one saved connection (only the local row — never touches the
    target database). Used to prune the switcher list, which otherwise only
    grows every time `connect` succeeds. `return_to` lets the caller land back
    on the workspace it was already in (deleting some *other* connection from
    the switcher shouldn't kick you out of the one you're looking at)."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    connection = get_object_or_404(Connection, pk=pk)
    connection.delete()
    response = HttpResponse(status=204)
    response["HX-Redirect"] = request.POST.get("return_to") or reverse("index")
    return response


def clear_connections(request):
    """Forget every saved connection at once — the list-declutter escape
    hatch for when `connect` has piled up a long trail of one-off/test rows.
    Local rows only; no target database is touched.

    `keep` (optional) is the pk of the connection the request was made from
    (the workspace switcher passes its own `connection.pk`). Per-row delete in
    that same switcher already never targets the connection you're currently
    looking at — this matches that: clearing the list from inside a workspace
    must not also end the session you're in. Only the index page (no "current"
    connection to protect) clears truly everything."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    keep_pk = request.POST.get("keep")
    qs = Connection.objects.all()
    if keep_pk:
        qs = qs.exclude(pk=keep_pk)
    qs.delete()
    response = HttpResponse(status=204)
    if keep_pk and Connection.objects.filter(pk=keep_pk).exists():
        response["HX-Redirect"] = reverse("workspace", args=[keep_pk])
    else:
        response["HX-Redirect"] = reverse("index")
    return response


def workspace(request, pk):
    """DB-client view: table list in the sidebar, table detail in the main pane.
    The main pane starts on the bento overview, so the summaries are gathered
    here too (a static include — no extra round trip that could race a click)."""
    connection = get_object_or_404(Connection, pk=pk)
    # 切り替えメニューの並び順（-last_used_at）が「最後に使った順」になるよう、
    # ここを踏むたびに繰り上げる。.update() で 1 行だけ叩く（フルの model.save() で
    # 他のフィールドを巻き込まない・シグナルも起こさない）。
    Connection.objects.filter(pk=pk).update(last_used_at=timezone.now())
    try:
        tables = get_engine(connection).list_tables()
    except EngineError as exc:
        return render(
            request,
            "workspace.html",
            {"connection": connection, "tables": [], "error": str(exc),
             "connections": Connection.objects.all(),
             "summary": {}, "commands": 0,
             "snapshots_count": 0, "backups_count": 0},
        )
    return render(
        request,
        "workspace.html",
        {"connection": connection, "tables": tables,
         "connections": Connection.objects.all(),
         **_overview_context(connection)},
    )


def _overview_summary(connection):
    """At-a-glance operational stats for the bento overview. Each group is
    guarded on its own so one failing probe (or a locked-down DB) degrades that
    card to '—' rather than breaking the whole home page. Returns a dict whose
    values are None when their probe failed.

    All probes run over one shared connection (engine.session()) instead of
    reconnecting per probe. The per-probe guard is broad on purpose: a probe
    can fail with a raw driver error (e.g. permission denied on
    pg_stat_replication), not just EngineError, and that must still degrade
    only its own card."""
    engine = get_engine(connection)
    s = {"tables": None, "activity": None, "health": None,
         "objects": None, "replication": None}

    def _tables():
        ts = engine.list_tables()
        # 統計の無いテーブルは足さない ── 不明を 0 として合算すると、
        # 合計だけがそれらしい顔で嘘をつく。
        return {"count": len(ts), "rows": sum(t.rows for t in ts if t.rows is not None)}

    def _activity():
        acts = [a for a in engine.list_activity() if not a.is_self]
        return {
            "sessions": len(acts),
            "active": sum(1 for a in acts if a.state == "active"),
            "blocked": sum(1 for a in acts if a.blocked),
        }

    def _health():
        sizes = engine.table_sizes(limit=1)
        return {
            "largest": sizes[0] if sizes else None,
            "unused": len(engine.unused_indexes()),
            "dead": sum(v.dead for v in engine.vacuum_stats()),
        }

    def _objects():
        return {
            "databases": len(engine.list_databases()),
            "schemas": len(engine.list_schemas()),
            "roles": len(engine.list_roles()),
        }

    def _probe(key, fn):
        # Broad on purpose: a probe can fail with a raw driver error (e.g.
        # permission denied on pg_stat_replication), not just EngineError, and
        # that must degrade only its own card — never the whole page.
        try:
            s[key] = fn()
        except Exception:  # noqa: BLE001  # nosec B110 — best-effort card; see comment above
            pass

    try:
        with engine.session():
            _probe("tables", _tables)
            _probe("activity", _activity)
            _probe("health", _health)
            _probe("objects", _objects)
            _probe("replication", engine.replication_status)
    except EngineError:
        pass  # couldn't even connect — every card stays '—'
    return s


def _overview_context(connection):
    """Everything the bento home renders: the live engine summary plus the cheap
    local-model counts (commands / snapshots / backups)."""
    return {
        "summary": _overview_summary(connection),
        "commands": connection.commands.count(),
        "snapshots_count": connection.snapshots.count(),
        "backups_count": connection.backups.count(),
    }


def overview(request, pk, notice=None):
    """The workspace home (#detail landing): the section bento. The same grid
    backs the overview hover menu, so clicking ⌂ overview lands here and hovering
    shows it as a quick menu. `notice` is an optional info banner (e.g. the
    auto-backup result after a table drop)."""
    connection = get_object_or_404(Connection, pk=pk)
    return render(request, "partials/workspace_home.html",
                  {"connection": connection, "notice": notice,
                   **_overview_context(connection)})
