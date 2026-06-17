"""Database engine abstraction.

The whole point of cli2ui is "CLI → Web UI". Different databases speak
different dialects (PostgreSQL `\\dt` vs MySQL `SHOW TABLES`), but the UI
should be one thing. So we hide the dialect behind an Engine and pick the
right one per connection. PostgreSQL ships first; MySQL slots in here later
without touching views or templates.
"""
from django.utils.translation import gettext as _

from .base import Engine, EngineError


def get_engine(connection) -> Engine:
    if connection.kind == "postgres":
        from .postgres import PostgresEngine

        return PostgresEngine(connection)
    if connection.kind == "mysql":
        from .mysql import MysqlEngine

        return MysqlEngine(connection)
    raise EngineError(_("Unsupported database kind: %(kind)s") % {"kind": connection.kind})


__all__ = ["Engine", "EngineError", "get_engine"]
