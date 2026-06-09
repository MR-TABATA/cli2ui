"""PostgreSQL engine — psycopg2 under the hood."""
import contextlib

import psycopg2
from psycopg2 import sql

from .base import Column, Engine, EngineError, Preview, Setting, Table

# The Web equivalent of `\dt`: every user table plus an estimated row count
# (pg_stat lags reality but is free; an exact COUNT(*) per table would be slow).
LIST_TABLES_SQL = """
SELECT t.schemaname,
       t.tablename,
       COALESCE(s.n_live_tup, 0) AS rows
FROM pg_catalog.pg_tables t
LEFT JOIN pg_catalog.pg_stat_user_tables s
       ON s.schemaname = t.schemaname AND s.relname = t.tablename
WHERE t.schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY t.schemaname, t.tablename;
"""

# The Web equivalent of `\d table`: column name, type, nullability, default.
LIST_COLUMNS_SQL = """
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = %s AND table_name = %s
ORDER BY ordinal_position;
"""

# Configuration parameters. current_setting() gives the human form ("128MB",
# "on") rather than pg_settings.setting's raw units ("16384" in 8kB blocks).
SETTINGS_SELECT = """
SELECT name, current_setting(name) AS value, unit, category, short_desc,
       vartype, context, enumvals, min_val, max_val, boot_val, pending_restart
FROM pg_settings
"""


class PostgresEngine(Engine):
    @contextlib.contextmanager
    def _connect(self):
        c = self.connection
        try:
            conn = psycopg2.connect(
                host=c.host,
                port=c.port,
                dbname=c.dbname,
                user=c.user,
                password=c.password,
                connect_timeout=5,
            )
        except psycopg2.Error as exc:
            raise EngineError(_clean(exc)) from exc
        # autocommit so ALTER SYSTEM / pg_reload_conf run as standalone statements.
        conn.autocommit = True
        try:
            yield conn
        finally:
            conn.close()

    def test(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")

    def list_tables(self) -> list[Table]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_TABLES_SQL)
                return [
                    Table(schema=row[0], name=row[1], rows=row[2])
                    for row in cur.fetchall()
                ]

    def list_columns(self, schema: str, table: str) -> list[Column]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(LIST_COLUMNS_SQL, (schema, table))
                return [
                    Column(
                        name=row[0],
                        type=row[1],
                        nullable=(row[2] == "YES"),
                        default=row[3],
                    )
                    for row in cur.fetchall()
                ]

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        # Identifiers come from the schema, but compose them safely anyway so a
        # table named `users; DROP …` can never break out of the query.
        query = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (limit,))
                columns = [d.name for d in cur.description]
                rows = cur.fetchall()
        return Preview(columns=columns, rows=rows)


    # --- server configuration --------------------------------------------

    def list_settings(self, names=None, category=None) -> list[Setting]:
        where, params = "", []
        if names:
            where, params = "WHERE name = ANY(%s)", [list(names)]
        elif category:
            where, params = "WHERE category = %s", [category]
        sql_text = f"{SETTINGS_SELECT} {where} ORDER BY category, name"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(sql_text, params)
                return [_setting(row) for row in cur.fetchall()]

    def list_setting_categories(self) -> list[str]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT DISTINCT category FROM pg_settings ORDER BY category")
                return [row[0] for row in cur.fetchall()]

    def pending_restart_settings(self) -> list[Setting]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"{SETTINGS_SELECT} WHERE pending_restart ORDER BY name")
                return [_setting(row) for row in cur.fetchall()]

    def update_setting(self, name: str, value: str) -> Setting:
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("ALTER SYSTEM SET {} = {}").format(
                            sql.Identifier(name), sql.Literal(value)
                        )
                    )
                    cur.execute("SELECT pg_reload_conf()")
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    def reset_setting(self, name: str) -> Setting:
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        sql.SQL("ALTER SYSTEM RESET {}").format(sql.Identifier(name))
                    )
                    cur.execute("SELECT pg_reload_conf()")
                except psycopg2.Error as exc:
                    raise EngineError(_clean(exc)) from exc
                return self._one_setting(cur, name)

    @staticmethod
    def _one_setting(cur, name: str) -> Setting:
        cur.execute(f"{SETTINGS_SELECT} WHERE name = %s", [name])
        row = cur.fetchone()
        if row is None:
            raise EngineError(f"Unknown setting: {name}")
        return _setting(row)


def _setting(row) -> Setting:
    return Setting(
        name=row[0],
        value=row[1],
        unit=row[2],
        category=row[3],
        description=row[4],
        vartype=row[5],
        context=row[6],
        enumvals=row[7],
        min_val=row[8],
        max_val=row[9],
        default=row[10],
        pending_restart=row[11],
    )


def _clean(exc: psycopg2.Error) -> str:
    """psycopg2 errors are multi-line; keep the first meaningful line."""
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else "Could not connect to PostgreSQL."
