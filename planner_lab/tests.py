"""Tests for the planner what-if app: the moved scale-simulation and index-lab
logic, the engine whatif_cursor primitive it rides on, and the feature flag."""
import unittest

from django.test import SimpleTestCase

from core.engines import EngineError, get_engine
from core.engines.postgres import _parse_plan
from core.features import enabled
# Reuse the core test fixtures (plan helpers + the sample DB).
from core.tests import SAMPLE_PAYLOAD, _sampledb, _sampledb_reachable, node
from planner_lab.whatif import (
    HYPO_INDEX_NAME,
    _relation_names,
    _scale_error,
    preview_index,
    simulate_scale,
)


def _max_rows(n) -> float:
    return max([n.plan_rows] + [_max_rows(c) for c in n.children])


class RelationNamesTests(SimpleTestCase):
    def test_collects_every_scanned_table(self):
        self.assertEqual(
            _relation_names(_parse_plan(SAMPLE_PAYLOAD)),
            {"orders", "customers"},
        )

    def test_empty_when_no_relations(self):
        self.assertEqual(_relation_names(node("Result")), set())


class ScaleErrorTests(SimpleTestCase):
    def test_pg_class_permission_gets_friendly_hint(self):
        # Operates on the already-cleaned EngineError message (no psycopg2 here).
        hinted = _scale_error(EngineError("permission denied for table pg_class"))
        self.assertIn("superuser", str(hinted))

    def test_other_errors_pass_through_unchanged(self):
        exc = EngineError('syntax error at or near "SELCT"')
        self.assertIs(_scale_error(exc), exc)


class FeatureFlagTests(SimpleTestCase):
    def test_planner_lab_is_registered(self):
        # The AppConfig.ready() hook registered the feature key on startup; the
        # nav templates and URLconf key off this.
        self.assertIn("planner_lab", enabled())


@unittest.skipUnless(_sampledb_reachable(), "sample DB not reachable on localhost:5433")
class PlannerWhatifIntegrationTests(SimpleTestCase):
    """The what-if logic against the sample DB — same guarantees as before the
    move (real measurements, and the catalog/index edits are always rolled back),
    now driving the engine only through whatif_cursor()."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = get_engine(_sampledb())

    def _reltuples(self, relname):
        with self.engine._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [relname])
            return cur.fetchone()[0]

    def test_whatif_cursor_rolls_back_its_work(self):
        # The engine primitive: a real (non-temp) DDL inside it is never persisted.
        with self.engine.whatif_cursor() as cur:
            cur.execute("CREATE TABLE _cli2ui_whatif_probe (x int)")
        with self.engine._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public._cli2ui_whatif_probe')")
            self.assertIsNone(cur.fetchone()[0])

    def test_simulate_scale_scales_and_leaves_no_trace(self):
        before = self._reltuples("orders")
        plans = simulate_scale(
            self.engine,
            "SELECT customer_id, count(*) FROM orders GROUP BY customer_id",
            factors=(1, 100))
        scaled = next(p for p in plans if p.factor == 100)
        base = next(p for p in plans if p.factor == 1)
        # The 100× plan estimates more rows out of the scan than the 1× plan...
        self.assertGreater(_max_rows(scaled.plan), _max_rows(base.plan))
        # ...but the catalog is untouched afterwards (the what-if was rolled back).
        self.assertEqual(self._reltuples("orders"), before)

    def test_preview_index_measures_and_leaves_no_trace(self):
        before_idx = {i.name for i in self.engine.list_indexes("public", "orders")}
        preview = preview_index(
            self.engine, "SELECT * FROM orders WHERE customer_id = 1",
            "public", "orders", ["customer_id"])
        self.assertIsNotNone(preview.before.actual_ms)
        self.assertIsNotNone(preview.after.actual_ms)
        self.assertIn("CONCURRENTLY", preview.ddl)
        after_idx = {i.name for i in self.engine.list_indexes("public", "orders")}
        self.assertEqual(before_idx, after_idx)
        self.assertNotIn(HYPO_INDEX_NAME, after_idx)

    def test_preview_index_rejects_unknown_column(self):
        with self.assertRaises(EngineError):
            preview_index(self.engine, "SELECT * FROM orders", "public",
                          "orders", ["no_such_col"])
