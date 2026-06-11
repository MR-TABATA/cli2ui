"""Unit tests for the structured EXPLAIN diff + scale simulation core.

These exercise the pure, database-independent logic: parsing EXPLAIN JSON into a
PlanNode tree, the node-level diff (alignment, flip/add/remove classification,
row/cost factors), and (de)serialization. No PostgreSQL needed — SimpleTestCase,
no DB. The catalog-touching simulate_scale() itself is verified by hand against
the sample DB (see project notes); here we test the machinery it feeds.
"""
import os
import unittest
from types import SimpleNamespace

import psycopg2
from django.test import SimpleTestCase, TestCase

from core.engines import EngineError, get_engine
from core.engines.base import Activity, Index, PlanNode, Setting, Table
from core.engines.postgres import (
    INDEX_METHODS,
    _clean,
    _parse_plan,
    _relation_names,
    _role,
    _scale_error,
    _setting,
    build_create_index_sql,
)
from core.forms import ConnectionForm
from core.models import Connection, PlanSnapshot
from core.plan_diff import diff_plans, node_from_dict, node_to_dict, to_text


def node(node_type, *, relation=None, index=None, rows=0.0, cost=0.0,
         children=()):
    """Terse PlanNode builder for tests."""
    return PlanNode(
        node_type=node_type, relation=relation, index=index,
        plan_rows=rows, total_cost=cost, plan_width=0,
        actual_rows=None, actual_ms=None, loops=None, detail=None,
        children=list(children),
    )


# A realistic EXPLAIN (FORMAT JSON) payload: hash join over two seq scans,
# wrapped in an aggregate. Shaped like what psycopg2 hands back (already decoded).
SAMPLE_PAYLOAD = [{
    "Plan": {
        "Node Type": "Aggregate", "Strategy": "Hashed",
        "Total Cost": 8.32, "Plan Rows": 50, "Plan Width": 40,
        "Plans": [{
            "Node Type": "Hash Join", "Join Type": "Inner",
            "Hash Cond": "(o.customer_id = c.id)",
            "Total Cost": 6.69, "Plan Rows": 200, "Plan Width": 36,
            "Plans": [
                {"Node Type": "Seq Scan", "Relation Name": "orders",
                 "Total Cost": 4.0, "Plan Rows": 200, "Plan Width": 8},
                {"Node Type": "Hash", "Total Cost": 1.5, "Plan Rows": 50,
                 "Plan Width": 36, "Plans": [
                     {"Node Type": "Seq Scan", "Relation Name": "customers",
                      "Filter": "(id < 10)",
                      "Total Cost": 1.5, "Plan Rows": 50, "Plan Width": 36},
                 ]},
            ],
        }],
    },
}]


class ParsePlanTests(SimpleTestCase):
    def test_parses_tree_shape_and_fields(self):
        root = _parse_plan(SAMPLE_PAYLOAD)
        self.assertEqual(root.node_type, "Aggregate")
        self.assertEqual(root.plan_rows, 50)
        self.assertEqual(len(root.children), 1)
        join = root.children[0]
        self.assertEqual(join.node_type, "Hash Join")
        self.assertEqual(len(join.children), 2)
        self.assertEqual(join.children[0].relation, "orders")

    def test_accepts_raw_json_string(self):
        import json
        root = _parse_plan(json.dumps(SAMPLE_PAYLOAD))
        self.assertEqual(root.node_type, "Aggregate")

    def test_detail_captures_join_and_conditions(self):
        join = _parse_plan(SAMPLE_PAYLOAD).children[0]
        self.assertIn("Inner join", join.detail)
        self.assertIn("Hash Cond", join.detail)

    def test_summary_formats_scan_with_relation(self):
        orders = _parse_plan(SAMPLE_PAYLOAD).children[0].children[0]
        self.assertEqual(orders.summary, "Seq Scan on orders")

    def test_summary_formats_index_scan(self):
        n = node("Index Scan", relation="orders", index="ix_orders_cust")
        self.assertEqual(n.summary, "Index Scan using ix_orders_cust on orders")


class RelationNamesTests(SimpleTestCase):
    def test_collects_every_scanned_table(self):
        self.assertEqual(
            _relation_names(_parse_plan(SAMPLE_PAYLOAD)),
            {"orders", "customers"},
        )

    def test_empty_when_no_relations(self):
        self.assertEqual(_relation_names(node("Result")), set())


class SerializationTests(SimpleTestCase):
    def test_round_trip_preserves_tree(self):
        root = _parse_plan(SAMPLE_PAYLOAD)
        clone = node_from_dict(node_to_dict(root))
        self.assertEqual(to_text(clone), to_text(root))


class ToTextTests(SimpleTestCase):
    def test_indents_by_depth(self):
        tree = node("Aggregate", rows=1, children=[
            node("Seq Scan", relation="orders", rows=200),
        ])
        lines = to_text(tree).splitlines()
        self.assertFalse(lines[0].startswith(" "))
        self.assertTrue(lines[1].lstrip().startswith("->"))
        self.assertIn("on orders", lines[1])


class DiffPlansTests(SimpleTestCase):
    def test_identical_plans_are_all_same(self):
        a = _parse_plan(SAMPLE_PAYLOAD)
        b = _parse_plan(SAMPLE_PAYLOAD)
        diff = diff_plans(a, b)
        self.assertTrue(diff.identical)
        self.assertFalse(diff.has_shape_change)
        self.assertTrue(all(r.kind == "same" for r in diff.rows))

    def test_scan_method_flip_is_one_changed_row(self):
        # Same table, Seq Scan -> Index Scan: should align into a single
        # 'changed' row (not a delete + insert pair).
        before = node("Seq Scan", relation="orders", rows=1, cost=4.5)
        after = node("Index Scan", relation="orders", index="ix", rows=166, cost=8.3)
        diff = diff_plans(before, after)
        self.assertEqual(len(diff.rows), 1)
        row = diff.rows[0]
        self.assertEqual(row.kind, "changed")
        self.assertEqual(row.summary_before, "Seq Scan on orders")
        self.assertEqual(row.summary, "Index Scan using ix on orders")
        self.assertTrue(diff.has_shape_change)

    def test_join_algorithm_flip_is_changed(self):
        before = node("Nested Loop", rows=10, children=[
            node("Seq Scan", relation="a", rows=10),
        ])
        after = node("Hash Join", rows=10, children=[
            node("Seq Scan", relation="a", rows=10),
        ])
        diff = diff_plans(before, after)
        top = diff.rows[0]
        self.assertEqual(top.kind, "changed")
        self.assertEqual(top.summary_before, "Nested Loop")
        self.assertEqual(top.summary, "Hash Join")

    def test_added_node_is_detected(self):
        before = node("Seq Scan", relation="orders", rows=200)
        after = node("Sort", rows=200, children=[
            node("Seq Scan", relation="orders", rows=200),
        ])
        kinds = {r.summary: r.kind for r in diff_plans(before, after).rows}
        self.assertEqual(kinds["Sort"], "added")
        self.assertEqual(kinds["Seq Scan on orders"], "same")

    def test_removed_node_is_detected(self):
        before = node("Sort", rows=200, children=[
            node("Seq Scan", relation="orders", rows=200),
        ])
        after = node("Seq Scan", relation="orders", rows=200)
        kinds = {r.summary: r.kind for r in diff_plans(before, after).rows}
        self.assertEqual(kinds["Sort"], "removed")

    def test_row_and_cost_factors(self):
        before = node("Seq Scan", relation="orders", rows=200, cost=4.0)
        after = node("Seq Scan", relation="orders", rows=20000, cost=200.0)
        row = diff_plans(before, after).rows[0]
        self.assertEqual(row.kind, "same")           # same node, just bigger
        self.assertAlmostEqual(row.rows_factor, 100.0)
        self.assertAlmostEqual(row.cost_factor, 50.0)

    def test_factors_none_when_before_is_zero(self):
        before = node("Seq Scan", relation="orders", rows=0, cost=0.0)
        after = node("Seq Scan", relation="orders", rows=20000, cost=200.0)
        row = diff_plans(before, after).rows[0]
        self.assertIsNone(row.rows_factor)
        self.assertIsNone(row.cost_factor)

    def test_scaled_plan_grows_but_keeps_shape(self):
        # The scale-simulation common case: identical shape, every row count ×N.
        small = _parse_plan(SAMPLE_PAYLOAD)
        big = node_from_dict(node_to_dict(small))
        _scale_rows(big, 100)
        diff = diff_plans(small, big)
        self.assertFalse(diff.has_shape_change)       # no flip
        self.assertFalse(diff.identical)              # but not identical (rows moved)
        self.assertTrue(all(r.kind == "same" for r in diff.rows))
        self.assertAlmostEqual(diff.rows[0].rows_factor, 100.0)


def _scale_rows(n: PlanNode, factor: int) -> None:
    n.plan_rows *= factor
    for c in n.children:
        _scale_rows(c, factor)


# --- pure engine helpers (no DB) --------------------------------------------

class CleanErrorTests(SimpleTestCase):
    def test_keeps_only_first_line(self):
        exc = psycopg2.Error("relation does not exist\nLINE 1: ...\n  ^")
        self.assertEqual(_clean(exc), "relation does not exist")

    def test_blank_error_falls_back(self):
        self.assertEqual(_clean(psycopg2.Error("")), "Could not connect to PostgreSQL.")


class ScaleErrorTests(SimpleTestCase):
    def test_pg_class_permission_gets_friendly_hint(self):
        exc = psycopg2.Error("permission denied for table pg_class")
        self.assertIn("superuser", _scale_error(exc))

    def test_other_errors_pass_through(self):
        exc = psycopg2.Error("syntax error at or near \"SELCT\"")
        self.assertEqual(_scale_error(exc), 'syntax error at or near "SELCT"')


class BuildCreateIndexSqlValidationTests(SimpleTestCase):
    """The shared spec→SQL core's validation — pure, no DB. (Rendering the
    composed statement needs a connection for identifier quoting, so the
    string-output checks live in the integration tests.) The future index lab
    will reuse this builder with concurrently=False."""

    def test_unknown_method_rejected(self):
        # The method is the one piece spliced as raw SQL — must be allow-listed.
        with self.assertRaises(EngineError):
            build_create_index_sql("public", "t", ["a"],
                                   method="btree; DROP TABLE t")

    def test_methods_are_a_fixed_allow_list(self):
        self.assertIn("btree", INDEX_METHODS)
        self.assertNotIn("'; DROP", " ".join(INDEX_METHODS))

    def test_no_columns_rejected(self):
        with self.assertRaises(EngineError):
            build_create_index_sql("public", "t", [])


class IndexColumnsTextTests(SimpleTestCase):
    def test_pulls_columns_from_definition(self):
        ix = Index(name="i", method="btree", unique=False, primary=False,
                   definition='CREATE INDEX i ON public.t USING btree (a, b)',
                   size="16 kB")
        self.assertEqual(ix.columns_text, "a, b")

    def test_blank_when_no_parens(self):
        ix = Index(name="i", method="btree", unique=False, primary=False,
                   definition="weird", size=None)
        self.assertEqual(ix.columns_text, "")


class RoleMappingTests(SimpleTestCase):
    # Row order matches LIST_ROLES_SQL:
    # rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolcanlogin, rolconnlimit
    def test_superuser_login_role(self):
        role = _role(("postgres", True, True, True, True, True, -1))
        self.assertEqual(role.name, "postgres")
        self.assertTrue(role.can_login)
        self.assertIn("Superuser", role.attributes)
        self.assertNotIn("Cannot login", role.attributes)

    def test_nologin_group_role_with_conn_limit(self):
        role = _role(("readers", False, False, False, False, False, 5))
        self.assertFalse(role.can_login)
        self.assertIn("Cannot login", role.attributes)
        self.assertIn("5 connections", role.attributes)
        self.assertNotIn("Superuser", role.attributes)


class SettingMappingTests(SimpleTestCase):
    def _row(self, context):
        # matches SETTINGS_SELECT column order
        return ("work_mem", "4MB", "kB", "Resource Usage", "desc",
                "integer", context, None, "64", "2147483647", "4096", False)

    def test_maps_fields(self):
        s = _setting(self._row("user"))
        self.assertEqual(s.name, "work_mem")
        self.assertEqual(s.value, "4MB")
        self.assertEqual(s.default, "4096")

    def test_requires_restart_only_for_postmaster(self):
        self.assertTrue(_setting(self._row("postmaster")).requires_restart)
        self.assertFalse(_setting(self._row("user")).requires_restart)

    def test_read_only_only_for_internal(self):
        self.assertTrue(_setting(self._row("internal")).read_only)
        self.assertFalse(_setting(self._row("user")).read_only)


# --- dataclass behaviour (no DB) --------------------------------------------

class DataclassPropertyTests(SimpleTestCase):
    def test_table_qualified_name(self):
        self.assertEqual(Table(schema="public", name="orders", rows=5).qualified,
                         "public.orders")

    def test_activity_blocked_reflects_blockers(self):
        base = dict(pid=1, user="u", database="d", app="a", client=None,
                    state="active", wait=None, query_secs=1, query="...")
        self.assertTrue(Activity(blocked_by=[42], **base).blocked)
        self.assertFalse(Activity(blocked_by=[], **base).blocked)


# --- engine factory (no DB) -------------------------------------------------

class GetEngineTests(SimpleTestCase):
    def test_postgres_returns_engine(self):
        from core.engines.postgres import PostgresEngine
        conn = SimpleNamespace(kind="postgres")
        self.assertIsInstance(get_engine(conn), PostgresEngine)

    def test_unsupported_kind_raises(self):
        with self.assertRaises(EngineError):
            get_engine(SimpleNamespace(kind="mysql"))


# --- models + form (sqlite management DB) -----------------------------------

class ConnectionModelTests(TestCase):
    def test_display_name_prefers_label(self):
        c = Connection(name="Prod", dbname="shop", host="db", user="u")
        self.assertEqual(c.display_name, "Prod")

    def test_display_name_falls_back_to_db_at_host(self):
        c = Connection(name="", dbname="shop", host="db", user="u")
        self.assertEqual(c.display_name, "shop@db")

    def test_str_includes_kind_and_endpoint(self):
        c = Connection(name="Prod", kind="postgres", host="db", port=5432,
                       dbname="shop", user="u")
        self.assertEqual(str(c), "Prod (PostgreSQL @ db:5432)")


class PlanSnapshotModelTests(TestCase):
    def test_str_is_label(self):
        conn = Connection.objects.create(dbname="shop", user="u")
        snap = PlanSnapshot.objects.create(connection=conn, label="before idx",
                                           sql="SELECT 1", plan_text="x")
        self.assertEqual(str(snap), "before idx")

    def test_plan_json_defaults_blank(self):
        conn = Connection.objects.create(dbname="shop", user="u")
        snap = PlanSnapshot.objects.create(connection=conn, label="l",
                                           sql="SELECT 1", plan_text="x")
        self.assertEqual(snap.plan_json, "")


class ConnectionFormTests(TestCase):
    BASE = {"name": "S", "kind": "postgres", "host": "localhost",
            "port": 5432, "dbname": "shop", "user": "demo", "password": "demo"}

    def test_valid(self):
        self.assertTrue(ConnectionForm(self.BASE).is_valid())

    def test_dbname_required(self):
        form = ConnectionForm({**self.BASE, "dbname": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("dbname", form.errors)


# --- integration: PostgresEngine vs the sample DB (skipped if unreachable) ---

def _sampledb():
    return SimpleNamespace(kind="postgres", host="localhost", port=5433,
                           dbname="shop", user="demo", password="demo")


def _sampledb_reachable() -> bool:
    try:
        get_engine(_sampledb()).test()
        return True
    except Exception:
        return False


@unittest.skipUnless(_sampledb_reachable(),
                     "sample DB not reachable on localhost:5433")
class PostgresEngineIntegrationTests(SimpleTestCase):
    """The safety guarantees that make this tool safe to point at a real DB.
    Runs against the bundled docker sampledb; skipped when it isn't up."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.engine = get_engine(_sampledb())

    def test_list_tables_includes_sample_data(self):
        names = {t.name for t in self.engine.list_tables()}
        self.assertIn("orders", names)

    def test_run_query_caps_rows_and_flags_truncation(self):
        res = self.engine.run_query("SELECT generate_series(1, 5000)", max_rows=10)
        self.assertEqual(res.rowcount, 10)
        self.assertTrue(res.truncated)

    def test_run_query_read_only_rejects_writes(self):
        # The DB itself rejects the write inside the read-only transaction —
        # no fragile SQL scanning on our side.
        with self.assertRaises(EngineError):
            self.engine.run_query("CREATE TEMP TABLE _cli2ui_probe (x int)")

    def test_explain_json_returns_a_tree(self):
        node = self.engine.explain_json("SELECT * FROM orders")
        self.assertTrue(node.node_type)
        self.assertIn("orders", _relation_names(node))

    def test_simulate_scale_scales_and_leaves_no_trace(self):
        before = self._reltuples("orders")
        plans = self.engine.simulate_scale(
            "SELECT customer_id, count(*) FROM orders GROUP BY customer_id",
            factors=(1, 100))
        scaled = next(p for p in plans if p.factor == 100)
        base = next(p for p in plans if p.factor == 1)
        # The 100× plan estimates more rows out of the scan than the 1× plan...
        self.assertGreater(_max_rows(scaled.plan), _max_rows(base.plan))
        # ...but the catalog is untouched afterwards (the what-if was rolled back).
        self.assertEqual(self._reltuples("orders"), before)

    def test_create_and_drop_schema_roundtrip(self):
        name = "cli2ui_test_schema"
        self.engine.drop_schema(name, cascade=True) if self._has_schema(name) else None
        self.engine.create_schema(name)
        try:
            self.assertTrue(self._has_schema(name))
        finally:
            self.engine.drop_schema(name)
        self.assertFalse(self._has_schema(name))

    def test_create_and_drop_index_roundtrip(self):
        name = "cli2ui_test_idx"
        self.engine.create_index("public", "orders", ["customer_id"], name=name)
        try:
            names = {i.name for i in self.engine.list_indexes("public", "orders")}
            self.assertIn(name, names)
        finally:
            self.engine.drop_index("public", name)
        after = {i.name for i in self.engine.list_indexes("public", "orders")}
        self.assertNotIn(name, after)

    def test_create_index_rejects_unknown_column(self):
        # Whitelisting catches a bad column before any DDL runs.
        with self.assertRaises(EngineError):
            self.engine.create_index("public", "orders", ["no_such_col"])

    def test_list_indexes_marks_primary_key(self):
        # orders has a primary key; its backing index should be flagged.
        idx = self.engine.list_indexes("public", "orders")
        self.assertTrue(any(i.primary for i in idx))

    def test_build_create_index_sql_renders(self):
        # Identifier quoting needs a real connection; assert the composed DDL
        # (keyword order, CONCURRENTLY/UNIQUE placement, quoted identifiers).
        with self.engine._connect() as conn:
            simple = build_create_index_sql(
                "public", "orders", ["customer_id"]).as_string(conn)
            self.assertEqual(
                simple,
                'CREATE INDEX ON "public"."orders" USING btree ("customer_id")')
            full = build_create_index_sql(
                "public", "orders", ["a", "b"], unique=True, method="gin",
                name="orders_ab_idx", concurrently=True).as_string(conn)
            self.assertEqual(
                full,
                'CREATE UNIQUE INDEX CONCURRENTLY "orders_ab_idx" '
                'ON "public"."orders" USING gin ("a", "b")')
            # A quote in an identifier is escaped, never an injection point.
            quoted = build_create_index_sql(
                "public", "t", ['weird"name']).as_string(conn)
            self.assertIn('"weird""name"', quoted)

    # helpers
    def _has_schema(self, name):
        return any(s.name == name for s in self.engine.list_schemas())

    def _reltuples(self, relname):
        with self.engine._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT reltuples FROM pg_class WHERE relname = %s", [relname])
            return cur.fetchone()[0]


def _max_rows(node: PlanNode) -> float:
    return max([node.plan_rows] + [_max_rows(c) for c in node.children])


# --- smoke E2E: the Explain → save → structured diff flow in a real browser ---
# Drives the actual htmx wiring (hx-post/target swaps, CSRF via body hx-headers)
# end to end. Needs the sample DB up AND the dev-only `playwright` package with a
# chromium browser (see requirements-dev.txt); skipped otherwise, so the default
# `manage.py test` still passes on a runtime-only install.

try:
    from playwright.sync_api import expect, sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

from django.test import LiveServerTestCase  # noqa: E402


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class ExplainDiffSmokeE2E(LiveServerTestCase):
    """One end-to-end pass of the headline feature: EXPLAIN a query, save it,
    EXPLAIN a different one, save it, and diff the two — expecting the structured
    node diff with its 'plan changed' badge to render in the page."""

    @classmethod
    def setUpClass(cls):
        # Playwright's sync API runs its own asyncio loop, which trips Django's
        # "no DB calls from an async context" guard during the test DB teardown.
        # This env var is the documented escape hatch for sync Playwright + Django.
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "1"
        super().setUpClass()
        cls._pw = sync_playwright().start()
        cls.browser = cls._pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def setUp(self):
        self.conn = Connection.objects.create(
            name="e2e", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")
        self.page = self.browser.new_page()

    def tearDown(self):
        self.page.close()

    def _explain_and_save(self, sql, label, plan_token):
        # plan_token is a string unique to THIS query's plan; waiting for it
        # guarantees the new EXPLAIN result has swapped in (htmx) before we touch
        # the label field — otherwise we'd race the previous result's stale input.
        self.page.locator("textarea[name=sql]:not(.hidden)").fill(sql)
        self.page.get_by_role("button", name="Explain", exact=True).click()
        expect(self.page.locator("#query-result")).to_contain_text(plan_token)
        self.page.locator("input[name=label]").fill(label)
        self.page.get_by_role("button", name="Save snapshot").click()
        expect(self.page.get_by_text(f"saved “{label}”")).to_be_visible()

    def test_explain_save_and_structured_diff(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        page.get_by_role("button", name="▷ SQL", exact=True).click()
        page.locator("textarea[name=sql]:not(.hidden)").wait_for()

        # Two queries whose plans differ in shape (the ORDER BY adds a Sort node).
        self._explain_and_save("SELECT * FROM orders", "plain",
                               plan_token="Seq Scan on orders")
        self._explain_and_save("SELECT * FROM orders ORDER BY total", "sorted",
                               plan_token="Sort")

        page.get_by_role("button", name="◫ snapshots").click()
        page.locator("select[name=a]").wait_for()
        page.locator("select[name=a]").select_option(label="plain")
        page.locator("select[name=b]").select_option(label="sorted")
        page.get_by_role("button", name="Diff", exact=True).click()

        diff = page.locator("#snapshot-diff")
        expect(diff.get_by_text("plan changed")).to_be_visible()
        expect(diff).to_contain_text("Seq Scan on orders")
        expect(diff).to_contain_text("Sort")
