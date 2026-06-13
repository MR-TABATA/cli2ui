"""Catalog objects and server config: databases, schemas, roles, backups/restore
and the postgresql.conf settings editor."""
import contextlib
import io

from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.template.loader import render_to_string
from django.utils.translation import gettext as _

from ..engines import EngineError, get_engine
from ..models import Backup, Connection
from ._shared import _auto_backup


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


def objects(request, pk):
    """Catalog browser: databases (\\l), schemas (\\dn), roles (\\du) — htmx partial."""
    connection = get_object_or_404(Connection, pk=pk)
    return _render_objects(request, connection)


def database_create(request, pk):
    """Create a database, optionally cloning one via TEMPLATE, then re-render."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error=_("Database name is required."))
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
        return _render_objects(request, connection, error=_("Database name is required."))
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
        return _render_objects(request, connection, error=_("Both names are required."))
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
        return _("Provide a new database name.")
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
        return _("Restore failed — database not created. %(err)s") % {"err": exc}
    return None


def _restore_into_existing_db(connection, name, stream):
    """Restore the dump (streamed) into an existing database, without creating or
    dropping anything. The caller is responsible for the type-gate confirmation —
    this overwrites/merges into a live database. Returns an error or None."""
    if not name:
        return _("Provide the database name.")
    try:
        get_engine(connection).restore_stream(name, stream)
    except EngineError as exc:
        return _("Restore failed. %(err)s") % {"err": exc}
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
            error=_("Provide a database name and a dump file to restore."))
    if target == "existing":
        # Type-gate: the user must type the database name to confirm overwriting
        # a live database (there's no auto-snapshot here — it's their own dump).
        if (request.POST.get("confirm") or "").strip() != name:
            return _render_objects(
                request, connection,
                error=_("To restore into the existing “%(name)s”, type its name to confirm.") % {"name": name})
        err = _restore_into_existing_db(connection, name, upload)
        notice = _("Restored into existing database “%(name)s”.") % {"name": name}
    else:
        err = _restore_into_new_db(connection, name, upload)
        notice = _("Restored into new database “%(name)s”.") % {"name": name}
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
        notice=_("Restored “%(target)s” into new database “%(name)s”.") % {"target": backup.target, "name": name})


def schema_create(request, pk):
    """Create a schema (CREATE SCHEMA), then re-render the objects panel."""
    connection = get_object_or_404(Connection, pk=pk)
    name = (request.POST.get("name") or "").strip()
    if not name:
        return _render_objects(request, connection, error=_("Schema name is required."))
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
        return _render_objects(request, connection, error=_("Schema name is required."))
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
        return _render_objects(request, connection, error=_("Schema name is required."))
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
        return _render_objects(request, connection, error=_("Role name is required."))
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
        return _render_objects(request, connection, error=_("Role name is required."))
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
        return _render_objects(request, connection, error=_("Role name is required."))
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
