"""The DDL rehearsal: run the real statement inside a transaction that is always
rolled back, under a budget, and report what came back.

`ALTER` is the one destructive operation that can be genuinely rehearsed —
PostgreSQL can roll back DDL — so it is the only member of the dry-run family
that lands on the "try it and throw it away" side rather than the "show it
before you press" side (v1.6.0).

**Three outcomes, and all three are answers.**

- ``completed``   — it went through. ``ran_ms`` is how long the statement took,
  which is how long ACCESS EXCLUSIVE is held: the real button sends this as one
  autocommit statement, so the statement's duration *is* the hold.
- ``over_budget`` — it did not finish inside the budget. This is not a failure.
  "It does not fit in 3 s on this table" is the thing you came to find out, and
  treating the timeout as a result rather than an error is the point of the
  design. ``limit`` says which bound stopped it: ``statement`` (the work itself
  was too slow) or ``lock`` (it never got the table — somebody else holds it).
- ``error``       — it would not run at all: existing NULLs, a column that is
  not there. Found in seconds, at no cost, instead of in production.

**What the rehearsal costs, said out loud.** It is not a simulation: it takes a
real ACCESS EXCLUSIVE lock on the table for as long as it runs, so concurrent
readers queue behind it exactly as they would for the real change. That is the
price of a real measurement, and the UI says so before you press.

**Why only SET NOT NULL.** ``ALTER COLUMN TYPE`` rewrites the whole table, and a
rewrite burns WAL and temporary disk for real even when the transaction is
rolled back. It needs a size gatekeeper first; until then this module refuses
to be pointed at anything but SET NOT NULL.
"""
import time
from dataclasses import dataclass

from django.utils.translation import gettext as _

from core.engines import EngineError
# Shared with core: the same builder backs the real (committed) change, so the
# rehearsal cannot drift into measuring a statement the button would not send.
from core.engines.postgres import build_set_null_sql

# The budget. Deliberately short: the question is "is this instant or not",
# and every millisecond of the answer is a millisecond of held lock.
REHEARSAL_BUDGET_MS = 3000
# The wait for the lock, matching the real DDL path (DDL_LOCK_TIMEOUT).
REHEARSAL_LOCK_TIMEOUT = "2s"

# SQLSTATEs that mean "the bound stopped it", not "the statement was wrong".
QUERY_CANCELED = "57014"      # statement_timeout fired
LOCK_NOT_AVAILABLE = "55P03"  # lock_timeout fired


@dataclass
class Rehearsal:
    """One rehearsal's result. `ddl` is the statement that was actually run —
    the same one the real button sends."""

    ddl: str
    outcome: str                  # "completed" | "over_budget" | "error"
    budget_ms: int
    lock_timeout: str
    ran_ms: float | None = None   # completed only: the measured hold
    limit: str | None = None      # over_budget only: "statement" | "lock"
    message: str | None = None    # error only: the cleaned driver message

    @property
    def completed(self) -> bool:
        return self.outcome == "completed"

    @property
    def over_budget(self) -> bool:
        return self.outcome == "over_budget"


def rehearse_set_not_null(engine, schema: str, table: str, column: str, *,
                          budget_ms: int = REHEARSAL_BUDGET_MS,
                          lock_timeout: str = REHEARSAL_LOCK_TIMEOUT) -> Rehearsal:
    """Run `ALTER TABLE ... SET NOT NULL` for real inside the always-rolled-back
    transaction and report which of the three answers came back.

    Raises EngineError only for a question that cannot be asked (no such column,
    or an engine with no rehearsal at all). A statement that fails *as a
    statement* is an outcome, not an exception — that is the whole feature.
    """
    if column not in {c.name for c in engine.list_columns(schema, table)}:
        raise EngineError(_("No such column: %(name)s") % {"name": column})
    statement = build_set_null_sql(schema, table, column, nullable=False)
    ddl = ""
    try:
        with engine.whatif_cursor(timeout_ms=budget_ms,
                                  lock_timeout=lock_timeout) as cur:
            ddl = statement.as_string(cur)
            started = time.perf_counter()
            cur.execute(statement)
            ran_ms = (time.perf_counter() - started) * 1000
    except EngineError as exc:
        return _from_error(exc, ddl, budget_ms, lock_timeout)
    return Rehearsal(ddl=ddl, outcome="completed", ran_ms=ran_ms,
                     budget_ms=budget_ms, lock_timeout=lock_timeout)


def _from_error(exc: EngineError, ddl: str, budget_ms: int,
                lock_timeout: str) -> Rehearsal:
    """Turn the driver's failure into the right outcome. Keyed on SQLSTATE, not
    on the message: the server may be speaking a language we don't parse."""
    if exc.sqlstate in (QUERY_CANCELED, LOCK_NOT_AVAILABLE):
        return Rehearsal(
            ddl=ddl, outcome="over_budget", budget_ms=budget_ms,
            lock_timeout=lock_timeout,
            limit="lock" if exc.sqlstate == LOCK_NOT_AVAILABLE else "statement")
    return Rehearsal(ddl=ddl, outcome="error", budget_ms=budget_ms,
                     lock_timeout=lock_timeout, message=str(exc))
