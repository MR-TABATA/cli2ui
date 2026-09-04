"""Tests for the planner what-if app: the scale simulation, the index lab, the
DDL rehearsal, the engine whatif_cursor primitive all three ride on, and the
feature flag."""
import unittest

from django.test import SimpleTestCase

from core.engines import EngineError, get_engine
from core.engines.postgres import _parse_plan
from core.features import enabled
# Reuse the core test fixtures (plan helpers + the sample DB).
# expect() only exists when playwright is installed; the E2E class below is
# skipped without it, so a stub keeps the module importable either way.
try:
    from playwright.sync_api import expect
except ImportError:  # pragma: no cover - mirrors core.tests
    expect = None
from core.tests import (
    SAMPLE_PAYLOAD,
    _BrowserE2E,
    _HAS_PLAYWRIGHT,
    _sampledb,
    _sampledb_reachable,
    node,
)
from planner_lab.rehearsal import (
    LOCK_NOT_AVAILABLE,
    QUERY_CANCELED,
    _from_error,
    rehearse_set_not_null,
)
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


class RehearsalOutcomeTests(SimpleTestCase):
    """The mapping that makes a timeout an answer. Keyed on SQLSTATE, so these
    hold on a server that reports its errors in another language."""

    def _mapped(self, exc):
        return _from_error(exc, "ALTER TABLE public.t ALTER COLUMN x SET NOT NULL",
                           3000, "2s")

    def test_statement_timeout_is_an_answer_not_a_failure(self):
        r = self._mapped(EngineError("canceling statement due to statement timeout",
                                     sqlstate=QUERY_CANCELED))
        self.assertEqual(r.outcome, "over_budget")
        self.assertEqual(r.limit, "statement")
        self.assertIsNone(r.message)

    def test_lock_timeout_says_which_bound_stopped_it(self):
        # Same outcome, different cause: the work was never even reached.
        r = self._mapped(EngineError("could not obtain lock on relation",
                                     sqlstate=LOCK_NOT_AVAILABLE))
        self.assertEqual(r.outcome, "over_budget")
        self.assertEqual(r.limit, "lock")

    def test_a_statement_that_cannot_run_stays_an_error(self):
        r = self._mapped(EngineError('column "x" contains null values',
                                     sqlstate="23502"))
        self.assertEqual(r.outcome, "error")
        self.assertIn("null values", r.message)

    def test_no_sqlstate_is_an_error(self):
        # MySQL has no rehearsal at all — whatif_cursor refuses without a code.
        r = self._mapped(EngineError("What-if tools need PostgreSQL."))
        self.assertEqual(r.outcome, "error")


@unittest.skipUnless(_sampledb_reachable(), "sample DB not reachable on localhost:5433")
class DdlRehearsalIntegrationTests(SimpleTestCase):
    """The rehearsal against a real server: all three outcomes, and the promise
    that nothing survives it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = get_engine(_sampledb())

    def _exec(self, sql_text):
        self.engine.run_query(sql_text, read_only=False)

    def _drop(self, table):
        self._exec(f'DROP TABLE IF EXISTS public."{table}"')

    def _nullable(self, table, column):
        col = next(c for c in self.engine.list_columns("public", table)
                   if c.name == column)
        return col.nullable

    def test_clean_column_completes_and_leaves_no_trace(self):
        t = "cli2ui_rehearse_ok"
        self._drop(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        self._exec(f'INSERT INTO public."{t}" VALUES (1), (2)')
        try:
            r = rehearse_set_not_null(self.engine, "public", t, "x")
            self.assertEqual(r.outcome, "completed")
            self.assertIsNotNone(r.ran_ms)
            self.assertIn("SET NOT NULL", r.ddl)
            # The measurement was real, but the column is untouched.
            self.assertTrue(self._nullable(t, "x"))
        finally:
            self._drop(t)

    def test_existing_nulls_come_back_as_would_not_run(self):
        t = "cli2ui_rehearse_nulls"
        self._drop(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        self._exec(f'INSERT INTO public."{t}" VALUES (1), (NULL)')
        try:
            r = rehearse_set_not_null(self.engine, "public", t, "x")
            self.assertEqual(r.outcome, "error")
            self.assertIn("null", r.message.lower())
        finally:
            self._drop(t)

    def test_lock_held_elsewhere_is_over_budget_not_a_crash(self):
        # Somebody else's open transaction is exactly what the real change would
        # hit. The rehearsal must report that, not blow up.
        t = "cli2ui_rehearse_locked"
        self._drop(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        try:
            with self.engine._connect() as blocker:
                blocker.autocommit = False       # hold the lock, never commit
                with blocker.cursor() as cur:
                    cur.execute(f'SELECT count(*) FROM public."{t}"')
                    cur.fetchone()
                r = rehearse_set_not_null(self.engine, "public", t, "x",
                                          lock_timeout="200ms")
            self.assertEqual(r.outcome, "over_budget")
            self.assertEqual(r.limit, "lock")
        finally:
            self._drop(t)

    def test_unknown_column_is_a_question_that_cannot_be_asked(self):
        with self.assertRaises(EngineError):
            rehearse_set_not_null(self.engine, "public", "orders", "no_such_column")


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class DdlRehearsalSmokeE2E(_BrowserE2E):
    """The button as a user meets it: open a nullable column's edit drawer,
    press Rehearse, read the verdict — and find the column still nullable."""

    TBL = "cli2ui_e2e_rehearse"

    def setUp(self):
        super().setUp()
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE TABLE public."{self.TBL}" (x int)')
            cur.execute(f'INSERT INTO public."{self.TBL}" VALUES (1), (2)')

    def tearDown(self):
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS public."{self.TBL}"')
        super().tearDown()

    def test_rehearse_reports_a_verdict_and_changes_nothing(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        page.locator(f'button[hx-get$="table={self.TBL}"]').click()
        page.locator("#detail h2").wait_for()
        # 既定タブは Data（テーブル名を押した人が見たいのは中身）。
        # 列を触るテストは、まず Columns へ移る。
        page.get_by_role("button", name="Columns", exact=True).click()

        page.locator("#detail tbody tr", has_text="x").get_by_role(
            "button", name="edit").click()
        rehearse = page.locator('form[hx-post*="rehearse"]')
        rehearse.wait_for(state="visible")
        rehearse.get_by_role("button", name="Rehearse it").click()

        result = page.locator("#ddl-rehearsal")
        expect(result).to_contain_text("ACCESS EXCLUSIVE")
        expect(result).to_contain_text("SET NOT NULL")
        expect(result).to_contain_text("Rolled back")
        # The measurement was real; the column is not.
        col = next(c for c in get_engine(self.conn).list_columns("public", self.TBL)
                   if c.name == "x")
        self.assertTrue(col.nullable)
