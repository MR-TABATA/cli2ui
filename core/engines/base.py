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
