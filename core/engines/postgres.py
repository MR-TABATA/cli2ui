"""PostgreSQL engine — psycopg2 under the hood."""
import contextlib

import psycopg2

from .base import Engine, EngineError, Table

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


def _clean(exc: psycopg2.Error) -> str:
    """psycopg2 errors are multi-line; keep the first meaningful line."""
    msg = str(exc).strip()
    return msg.splitlines()[0] if msg else "Could not connect to PostgreSQL."
