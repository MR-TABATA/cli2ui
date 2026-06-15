"""Shared view helpers used across more than one panel module."""
from django.conf import settings as django_settings
from django.utils.translation import gettext as _

from ..engines import EngineError, get_engine
from ..models import Backup


def _mb(n):
    return f"{n / 1048576:.0f} MB" if n >= 1048576 else f"{n / 1024:.0f} kB"


def _auto_backup(connection, *, operation, kind, dbname, schema=None, table=None):
    """Take an automatic safety snapshot (custom-format pg_dump) just before a
    destructive op and store it if it's under the size limit. Returns a short
    notice for the UI; never raises — an oversized or failed snapshot must not
    block the operation, so the caller proceeds (warned)."""
    engine = get_engine(connection)
    try:
        if kind == Backup.KIND_TABLE:
            dump = engine.dump_table(schema, table, fmt="custom")
            target = f"{schema}.{table}"
        else:
            dump = engine.dump_database(dbname, fmt="custom")
            target = dbname
    except EngineError as exc:
        return _("⚠ No backup taken — the snapshot failed: %(err)s") % {"err": exc}
    limit = django_settings.CLI2UI_MAX_AUTO_BACKUP_BYTES
    if len(dump.data) > limit:
        return _("⚠ No backup taken — %(target)s is %(size)s, over the %(limit)s limit.") % {
            "target": target, "size": _mb(len(dump.data)), "limit": _mb(limit)}
    Backup.objects.create(
        connection=connection, operation=operation, kind=kind, target=target,
        dbname=dbname, data=dump.data, byte_size=len(dump.data))
    try:
        _prune_old_backups(connection)
    except Exception:  # noqa: BLE001  # nosec B110 — housekeeping; must never block the op
        pass
    return _("Backup saved before %(op)s — recover it from Backups.") % {"op": operation}


def _prune_old_backups(connection):
    """Keep this connection's auto-backups under the total-size budget by
    deleting the oldest first. The most recent snapshot is always kept (even if
    it alone exceeds the budget — the per-snapshot cap already bounds one), so
    a write always leaves at least its own undo point behind. Reads byte_size
    only, never the blobs."""
    limit = django_settings.CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES
    rows = connection.backups.order_by("-created_at", "-pk").values_list("pk", "byte_size")
    running, stale = 0, []
    for i, (pk, size) in enumerate(rows):
        running += size or 0
        if running > limit and i > 0:  # i == 0 is the newest — always keep it
            stale.append(pk)
    if stale:
        connection.backups.filter(pk__in=stale).delete()
