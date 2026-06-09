"""Engine interface shared by all database backends."""
from dataclasses import dataclass


class EngineError(Exception):
    """Raised when connecting to or querying the target database fails.

    Carries a message that is safe to show in the UI.
    """


@dataclass
class Table:
    schema: str
    name: str
    rows: int  # estimated live row count (approximate, from stats)

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    default: str | None


@dataclass
class Preview:
    columns: list[str]
    rows: list[tuple]


@dataclass
class Database:
    """One database on the server. The Web equivalent of a `\\l` row."""

    name: str
    owner: str
    encoding: str
    size: str | None  # pretty-printed, or None if we can't connect to size it


@dataclass
class Schema:
    """One schema. The Web equivalent of a `\\dn` row."""

    name: str
    owner: str


@dataclass
class Role:
    """One login/group role. The Web equivalent of a `\\du` row."""

    name: str
    attributes: list[str]  # human labels: "Superuser", "Create DB", …
    can_login: bool


@dataclass
class Setting:
    """One server configuration parameter (a row of pg_settings)."""

    name: str
    value: str           # current value, human form (e.g. "128MB", "on")
    unit: str | None
    category: str
    description: str
    vartype: str         # bool | integer | real | string | enum
    context: str         # internal | postmaster | sighup | user | ...
    enumvals: list[str] | None
    min_val: str | None
    max_val: str | None
    default: str | None  # boot value
    pending_restart: bool

    @property
    def requires_restart(self) -> bool:
        """Changing this needs a full server restart, not just a reload."""
        return self.context == "postmaster"

    @property
    def read_only(self) -> bool:
        return self.context == "internal"


class Engine:
    """Base class. One Engine wraps one saved Connection."""

    def __init__(self, connection):
        self.connection = connection

    def test(self) -> None:
        """Open a connection and fail loudly (EngineError) if it can't."""
        raise NotImplementedError

    def list_tables(self) -> list[Table]:
        """Return user tables. The Web equivalent of `\\dt` / `SHOW TABLES`."""
        raise NotImplementedError

    def list_columns(self, schema: str, table: str) -> list[Column]:
        """Column definitions for one table. The Web equivalent of `\\d table`."""
        raise NotImplementedError

    def preview_rows(self, schema: str, table: str, limit: int = 50) -> Preview:
        """First rows of a table. The Web equivalent of `SELECT * ... LIMIT n`."""
        raise NotImplementedError

    # --- catalog browsing (psql backslash commands) ------------------------

    def list_databases(self) -> list[Database]:
        """Databases on the server. The Web equivalent of `\\l`."""
        raise NotImplementedError

    def list_schemas(self) -> list[Schema]:
        """User schemas in the current database. The Web equivalent of `\\dn`."""
        raise NotImplementedError

    def list_roles(self) -> list[Role]:
        """Login/group roles. The Web equivalent of `\\du`."""
        raise NotImplementedError

    # --- catalog mutations (CREATE / DROP) ---------------------------------

    def create_schema(self, name: str) -> None:
        """Create a schema. The Web equivalent of `CREATE SCHEMA name`."""
        raise NotImplementedError

    def drop_schema(self, name: str, cascade: bool = False) -> None:
        """Drop a schema. The Web equivalent of `DROP SCHEMA name [CASCADE]`."""
        raise NotImplementedError

    def create_role(
        self,
        name: str,
        *,
        login: bool = False,
        password: str | None = None,
        superuser: bool = False,
        createdb: bool = False,
        createrole: bool = False,
    ) -> None:
        """Create a role. The Web equivalent of `CREATE ROLE name WITH …`."""
        raise NotImplementedError

    def drop_role(self, name: str) -> None:
        """Drop a role. The Web equivalent of `DROP ROLE name`."""
        raise NotImplementedError

    # --- server configuration (postgresql.conf, via SQL) -------------------

    def list_settings(self, names=None, category=None) -> list[Setting]:
        """Read configuration parameters. The Web equivalent of `SHOW ALL`."""
        raise NotImplementedError

    def list_setting_categories(self) -> list[str]:
        raise NotImplementedError

    def pending_restart_settings(self) -> list[Setting]:
        """Parameters changed via ALTER SYSTEM that await a server restart."""
        raise NotImplementedError

    def update_setting(self, name: str, value: str) -> Setting:
        """Set a parameter and reload. `ALTER SYSTEM SET` + `pg_reload_conf()`."""
        raise NotImplementedError

    def reset_setting(self, name: str) -> Setting:
        """Revert a parameter to its default. `ALTER SYSTEM RESET` + reload."""
        raise NotImplementedError
