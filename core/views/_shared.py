"""Shared view helpers used across more than one panel module."""
from django.conf import settings as django_settings

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
        return f"⚠ No backup taken — the snapshot failed: {exc}"
    limit = django_settings.CLI2UI_MAX_AUTO_BACKUP_BYTES
    if len(dump.data) > limit:
        return (f"⚠ No backup taken — {target} is {_mb(len(dump.data))}, "
                f"over the {_mb(limit)} limit.")
    Backup.objects.create(
        connection=connection, operation=operation, kind=kind, target=target,
        dbname=dbname, data=dump.data, byte_size=len(dump.data))
    return f"Backup saved before {operation} — recover it from Backups."
