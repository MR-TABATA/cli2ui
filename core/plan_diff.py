"""Structured diff between two EXPLAIN plan trees.

This is the shared core behind two features that look different but ask the same
question — "how did the plan change?":

  * snapshot A/B compare (before vs after an index), and
  * scale simulation (1× vs 100× vs 10000× row counts).

Both hand two ``PlanNode`` trees to ``diff_plans`` and render the result. It is
pure (no Django, no psycopg2): trees in, diff rows out. We align nodes with
stdlib ``difflib.SequenceMatcher`` over a *shape key* that deliberately ignores
the scan method and join algorithm — so a Seq Scan flipping to an Index Scan on
the same table shows up as one *changed* row (the thing you want to see), while a
genuinely new node (a Sort, a parallel Gather) shows up as added/removed.
"""
import difflib
from dataclasses import dataclass, field

from .engines.base import PlanNode

# Node types whose only interesting difference, when comparing the same query, is
# *which* access path the planner picked for a given table / which join it chose.
_SCANS = {
    "Seq Scan", "Index Scan", "Index Only Scan", "Bitmap Heap Scan",
    "Bitmap Index Scan", "Tid Scan", "Sample Scan",
}
_JOINS = {"Nested Loop", "Hash Join", "Merge Join"}


@dataclass
class DiffRow:
    kind: str               # "same" | "changed" | "added" | "removed"
    depth: int
    summary: str            # node label on the "after" side (or the lone side)
    summary_before: str | None  # set only when kind == "changed"
    rows_before: float | None
    rows_after: float | None
    cost_before: float | None
    cost_after: float | None

    @property
    def rows_factor(self) -> float | None:
        if self.rows_before and self.rows_after and self.rows_before > 0:
            return self.rows_after / self.rows_before
        return None

    @property
    def cost_factor(self) -> float | None:
        if self.cost_before and self.cost_after and self.cost_before > 0:
            return self.cost_after / self.cost_before
        return None


@dataclass
class PlanDiff:
    rows: list[DiffRow] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        """No shape change and no row/cost movement — the plans are the same."""
        for r in self.rows:
            if r.kind != "same":
                return False
            if r.rows_factor is not None and abs(r.rows_factor - 1.0) > 1e-9:
                return False
            if r.cost_factor is not None and abs(r.cost_factor - 1.0) > 1e-9:
                return False
        return True

    @property
    def has_shape_change(self) -> bool:
        """A scan/join method flipped or a node appeared/disappeared — i.e. the
        plan didn't just get more expensive, it changed strategy."""
        return any(r.kind != "same" for r in self.rows)


def diff_plans(before: PlanNode, after: PlanNode) -> PlanDiff:
    fa = _flatten(before)
    fb = _flatten(after)
    keys_a = [_shape_key(n) for _, n in fa]
    keys_b = [_shape_key(n) for _, n in fb]
    matcher = difflib.SequenceMatcher(a=keys_a, b=keys_b, autojunk=False)

    rows: list[DiffRow] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for di, dj in zip(range(i1, i2), range(j1, j2)):
                rows.append(_paired(fa[di], fb[dj]))
        elif tag == "delete":
            rows.extend(_removed(fa[di]) for di in range(i1, i2))
        elif tag == "insert":
            rows.extend(_added(fb[dj]) for dj in range(j1, j2))
        elif tag == "replace":
            rows.extend(_removed(fa[di]) for di in range(i1, i2))
            rows.extend(_added(fb[dj]) for dj in range(j1, j2))
    return PlanDiff(rows=rows)


def _paired(a_item, b_item) -> DiffRow:
    da, na = a_item
    db, nb = b_item
    changed = na.summary != nb.summary
    return DiffRow(
        kind="changed" if changed else "same",
        depth=db,
        summary=nb.summary,
        summary_before=na.summary if changed else None,
        rows_before=na.plan_rows, rows_after=nb.plan_rows,
        cost_before=na.total_cost, cost_after=nb.total_cost,
    )


def _removed(item) -> DiffRow:
    d, n = item
    return DiffRow(kind="removed", depth=d, summary=n.summary, summary_before=None,
                   rows_before=n.plan_rows, rows_after=None,
                   cost_before=n.total_cost, cost_after=None)


def _added(item) -> DiffRow:
    d, n = item
    return DiffRow(kind="added", depth=d, summary=n.summary, summary_before=None,
                   rows_before=None, rows_after=n.plan_rows,
                   cost_before=None, cost_after=n.total_cost)


def _flatten(node: PlanNode, depth: int = 0, out=None) -> list[tuple[int, PlanNode]]:
    out = [] if out is None else out
    out.append((depth, node))
    for child in node.children:
        _flatten(child, depth + 1, out)
    return out


def _shape_key(node: PlanNode):
    """Align nodes by structural role, blind to access method, so a method swap
    on the same table (Seq → Index on `orders`, Nested Loop → Hash Join) aligns
    into one 'changed' row instead of a remove+add pair. Depth is deliberately
    NOT part of the key: inserting a wrapper node (a Sort, a parallel Gather)
    shifts the depth of everything below it, and we don't want that to spuriously
    re-diff the whole subtree — difflib's ordering keeps the alignment sane."""
    nt = node.node_type
    if nt in _SCANS or nt.endswith("Scan"):
        return ("scan", node.relation)
    if nt in _JOINS:
        return ("join",)
    return (nt, node.relation)


# --- (de)serialization for storing a plan in a snapshot ---------------------

def node_to_dict(node: PlanNode) -> dict:
    return {
        "node_type": node.node_type,
        "relation": node.relation,
        "index": node.index,
        "plan_rows": node.plan_rows,
        "total_cost": node.total_cost,
        "plan_width": node.plan_width,
        "actual_rows": node.actual_rows,
        "actual_ms": node.actual_ms,
        "loops": node.loops,
        "detail": node.detail,
        "children": [node_to_dict(c) for c in node.children],
    }


def node_from_dict(d: dict) -> PlanNode:
    return PlanNode(
        node_type=d["node_type"], relation=d.get("relation"), index=d.get("index"),
        plan_rows=d.get("plan_rows", 0), total_cost=d.get("total_cost", 0.0),
        plan_width=d.get("plan_width", 0), actual_rows=d.get("actual_rows"),
        actual_ms=d.get("actual_ms"), loops=d.get("loops"), detail=d.get("detail"),
        children=[node_from_dict(c) for c in d.get("children", [])],
    )


def to_text(node: PlanNode) -> str:
    """An indented text rendering of the tree (close to psql's EXPLAIN output),
    kept for the plain-text snapshot view and text diff fallback."""
    lines: list[str] = []
    for depth, n in _flatten(node):
        indent = "  " * depth + ("-> " if depth else "")
        cost = f"cost={n.total_cost:.2f} rows={n.plan_rows:g}"
        if n.actual_rows is not None:
            cost += f" actual_rows={n.actual_rows:g}"
            if n.actual_ms is not None:
                cost += f" time={n.actual_ms:.2f}ms"
        detail = f"  [{n.detail}]" if n.detail else ""
        lines.append(f"{indent}{n.summary}{detail}  ({cost})")
    return "\n".join(lines)
