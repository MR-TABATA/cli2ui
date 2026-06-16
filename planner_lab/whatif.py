"""The planner what-if engine logic: scale simulation and the index lab.

These used to be PostgresEngine methods; they live here now so the whole feature
is one removable unit. They drive the database only through the engine's public
primitives — `whatif_cursor()` (a transaction that is always rolled back) and
`explain_json()` — plus shared plan helpers, so this app never imports psycopg2.
"""
from dataclasses import dataclass

from django.utils.translation import gettext as _

from core.engines import EngineError
from core.engines.base import PlanNode
# Shared with core: _parse_plan also backs explain_json; build_create_index_sql
# also backs the real (committed) index creation.
from core.engines.postgres import _parse_plan, build_create_index_sql

# The throwaway index we actually build inside the rolled-back transaction, named
# so we can detect whether the planner chose it.
HYPO_INDEX_NAME = "_cli2ui_hypothetical_idx"

# Scale-simulation what-if: multiply the planner's row-count estimate for the
# named tables (and their indexes) by a factor. We scale ONLY reltuples, not
# relpages: the planner derives a tuple *density* (reltuples/relpages) and
# multiplies it by the table's *actual* page count, so scaling both by N cancels
# out. Run only inside a transaction that is always rolled back.
SCALE_PGCLASS_SQL = """
UPDATE pg_class
   SET reltuples = reltuples * %s
 WHERE oid IN (
   SELECT oid FROM pg_class WHERE relname = ANY(%s) AND relkind IN ('r', 'p')
   UNION
   SELECT indexrelid FROM pg_index
    WHERE indrelid IN (SELECT oid FROM pg_class
                        WHERE relname = ANY(%s) AND relkind IN ('r', 'p'))
 )
"""


@dataclass
class ScalePlan:
    """One EXPLAIN plan produced at a given row-count multiplier (what-if)."""

    factor: int        # 1 = real stats, 100 = "what if every table were 100× bigger"
    plan: PlanNode


@dataclass
class IndexPreview:
    """The result of a 'what-if' index trial: the same query EXPLAIN ANALYZE'd
    without and then with a hypothetical index, which is created and immediately
    rolled back. Real measured timing, zero persistence."""

    ddl: str             # the CREATE INDEX you'd run for real (display)
    before: PlanNode     # plan + real timing without the index
    after: PlanNode      # plan + real timing with the hypothetical index
    used: bool           # did the planner actually choose the hypothetical index?

    @property
    def before_ms(self) -> float | None:
        return self.before.actual_ms

    @property
    def after_ms(self) -> float | None:
        return self.after.actual_ms

    @property
    def speedup(self) -> float | None:
        """before / after — >1 means the index made the query faster."""
        if self.before_ms and self.after_ms and self.after_ms > 0:
            return self.before_ms / self.after_ms
        return None


def simulate_scale(engine, sql_text: str, *, factors=(1, 100, 10000),
                   timeout_ms: int = 15000) -> list[ScalePlan]:
    """What-if planning: EXPLAIN the query at each row-count multiplier. The
    factor-1 plan is the real one; it also tells us which tables the query
    touches, so we scale exactly those (not the whole database)."""
    base = engine.explain_json(sql_text, timeout_ms=timeout_ms)
    relnames = sorted(_relation_names(base))
    plans = [ScalePlan(factor=1, plan=base)]
    for n in factors:
        if n == 1:
            continue
        plans.append(ScalePlan(
            factor=n,
            plan=_explain_scaled(engine, sql_text, n, relnames, timeout_ms),
        ))
    return plans


def _explain_scaled(engine, sql_text: str, factor: int,
                    relnames: list[str], timeout_ms: int) -> PlanNode:
    # Plain EXPLAIN (no ANALYZE) never runs the user's query; the pg_class edit
    # lives only inside whatif_cursor's always-rolled-back transaction.
    try:
        with engine.whatif_cursor(timeout_ms=timeout_ms) as cur:
            cur.execute(SCALE_PGCLASS_SQL, [factor, relnames, relnames])
            cur.execute("EXPLAIN (FORMAT JSON) " + sql_text)
            payload = cur.fetchone()[0]
    except EngineError as exc:
        raise _scale_error(exc) from exc
    return _parse_plan(payload)


def preview_index(engine, sql_text: str, schema: str, table: str,
                  columns: list[str], *, method: str = "btree",
                  unique: bool = False, timeout_ms: int = 15000) -> IndexPreview:
    """EXPLAIN ANALYZE the query without, then with, a hypothetical index built
    inside the always-rolled-back transaction. Real timing, zero persistence."""
    valid = {c.name for c in engine.list_columns(schema, table)}
    unknown = [c for c in columns if c not in valid]
    if unknown:
        raise EngineError(_("No such column(s): %(cols)s") % {"cols": ', '.join(unknown)})
    # The throwaway index we build (plain, not CONCURRENTLY, so it can live inside
    # the transaction); and the statement the user would run for real (displayed).
    hypo = build_create_index_sql(schema, table, columns, method=method,
                                  unique=unique, name=HYPO_INDEX_NAME,
                                  concurrently=False)
    real = build_create_index_sql(schema, table, columns, method=method,
                                  unique=unique, concurrently=True)
    explain = "EXPLAIN (ANALYZE, FORMAT JSON) " + sql_text
    with engine.whatif_cursor(timeout_ms=timeout_ms) as cur:
        cur.execute(explain)
        before = _parse_plan(cur.fetchone()[0])
        cur.execute(hypo)  # visible to the next EXPLAIN in this tx
        cur.execute(explain)
        after = _parse_plan(cur.fetchone()[0])
        ddl = real.as_string(cur)
    return IndexPreview(ddl=ddl, before=before, after=after,
                        used=_uses_index(after, HYPO_INDEX_NAME))


def _relation_names(node: PlanNode, acc: set[str] | None = None) -> set[str]:
    """Every table the plan reads — what the scale simulation grows."""
    acc = set() if acc is None else acc
    if node.relation:
        acc.add(node.relation)
    for child in node.children:
        _relation_names(child, acc)
    return acc


def _uses_index(node: PlanNode, name: str) -> bool:
    """Whether any scan in the plan tree uses the named index — i.e. did the
    planner actually pick the hypothetical index we offered it?"""
    if node.index == name:
        return True
    return any(_uses_index(c, name) for c in node.children)


def _scale_error(exc: EngineError) -> EngineError:
    """Add a superuser hint when the pg_class edit was refused. Operates on the
    already-cleaned EngineError message, so this app never touches psycopg2."""
    msg = str(exc)
    if "pg_class" in msg and "permission" in msg.lower():
        return EngineError(
            _("Scale simulation needs a superuser connection (it edits pg_class)."))
    return exc
