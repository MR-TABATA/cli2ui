"""Unit tests for the structured EXPLAIN diff + scale simulation core.

These exercise the pure, database-independent logic: parsing EXPLAIN JSON into a
PlanNode tree, the node-level diff (alignment, flip/add/remove classification,
row/cost factors), and (de)serialization. No PostgreSQL needed — SimpleTestCase,
no DB. The catalog-touching simulate_scale() itself is verified by hand against
the sample DB (see project notes); here we test the machinery it feeds.
"""
import contextlib
import io
import json
import os
import unittest
import unittest.mock
from types import SimpleNamespace

import psycopg2
from django.test import SimpleTestCase, TestCase, override_settings

from core.engines import EngineError, get_engine
from core.engines.base import (
    Activity, Column, ConnectionHeadroom, Index, PlanNode, Setting, Table,
)
from core.engines.postgres import (
    INDEX_METHODS,
    PostgresEngine,
    _clean,
    _parse_plan,
    _role,
    _setting,
    _tool_error,
    build_create_index_sql,
)
from core.forms import ConnectionForm
from core.models import Backup, Connection, PlanSnapshot
from core.plan_diff import diff_plans, node_from_dict, node_to_dict, to_text
from core.views._shared import _prune_old_backups


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


class ToolErrorTests(SimpleTestCase):
    def test_prefers_the_error_line_over_a_trailing_summary(self):
        stderr = (
            b"pg_restore: connecting to database for restore\n"
            b"pg_restore: error: could not execute query: ERROR:  relation "
            b'"orders" already exists\n'
            b"pg_restore: warning: errors ignored on restore: 1\n"
        )
        msg = _tool_error(stderr, "restore failed.")
        self.assertIn('relation "orders" already exists', msg)
        # The trailing "errors ignored" summary isn't the only thing shown.
        self.assertTrue(msg.startswith("pg_restore: error"))

    def test_falls_back_to_last_lines_when_nothing_flagged(self):
        msg = _tool_error(b"some noise\nmore noise\n", "x failed.")
        self.assertIn("noise", msg)

    def test_blank_stderr_uses_fallback(self):
        self.assertEqual(_tool_error(b"   \n", "pg_dump failed."), "pg_dump failed.")


class BuildCreateIndexSqlValidationTests(SimpleTestCase):
    """The shared spec→SQL core's validation — pure, no DB. (Rendering the
    composed statement needs a connection for identifier quoting, so the
    string-output checks live in the integration tests.) The index lab
    (planner_lab) reuses this builder with concurrently=False."""

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


class SessionConnectionReuseTests(SimpleTestCase):
    """session() must collapse many default-database probes onto one connection
    (the workspace overview's reason for existing), while still letting a
    different-dbname call open its own. No live DB — psycopg2.connect is mocked."""

    def _engine(self):
        conn = Connection(kind="postgres", host="h", port=5432,
                          dbname="d", user="u", password="p")
        return PostgresEngine(conn)

    @unittest.mock.patch("core.engines.postgres.psycopg2.connect")
    def test_session_reuses_one_connection_and_closes_it_once(self, connect):
        engine = self._engine()
        with engine.session():
            with engine._connect() as a:
                pass
            with engine._connect() as b:
                pass
        self.assertIs(a, b)
        self.assertEqual(connect.call_count, 1)
        connect.return_value.close.assert_called_once()

    @unittest.mock.patch("core.engines.postgres.psycopg2.connect")
    def test_without_session_each_connect_dials_fresh(self, connect):
        connect.side_effect = [unittest.mock.MagicMock(), unittest.mock.MagicMock()]
        engine = self._engine()
        with engine._connect():
            pass
        with engine._connect():
            pass
        self.assertEqual(connect.call_count, 2)

    @unittest.mock.patch("core.engines.postgres.psycopg2.connect")
    def test_session_routes_other_dbname_to_its_own_connection(self, connect):
        held, other = unittest.mock.MagicMock(), unittest.mock.MagicMock()
        connect.side_effect = [held, other]
        engine = self._engine()
        with engine.session():
            with engine._connect(dbname="postgres") as c:
                pass
        self.assertIs(c, other)
        self.assertEqual(connect.call_count, 2)


class BackupRetentionTests(TestCase):
    """The per-connection total-size cap on auto-backups: oldest deleted first,
    newest always kept. Management-DB only (SQLite) — no PostgreSQL needed."""

    def _conn(self):
        return Connection.objects.create(kind="postgres", dbname="d", user="u")

    def _backup(self, conn, size):
        return Backup.objects.create(
            connection=conn, operation="write query", kind=Backup.KIND_DATABASE,
            target="d", dbname="d", data=b"x" * size, byte_size=size)

    @override_settings(CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES=100)
    def test_prunes_oldest_until_under_budget(self):
        conn = self._conn()
        kept = [self._backup(conn, 40) for _ in range(4)]  # created oldest→newest
        _prune_old_backups(conn)
        # newest-first running total: 40, 80, 120(over), 160(over) → drop 2 oldest
        survivors = set(conn.backups.values_list("pk", flat=True))
        self.assertEqual(survivors, {kept[2].pk, kept[3].pk})

    @override_settings(CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES=10)
    def test_always_keeps_the_newest_even_if_it_alone_exceeds_budget(self):
        conn = self._conn()
        newest = self._backup(conn, 40)  # 40 > 10, but it's the only/newest one
        _prune_old_backups(conn)
        self.assertEqual(set(conn.backups.values_list("pk", flat=True)), {newest.pk})

    @override_settings(CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES=500 * 1024 * 1024)
    def test_under_budget_keeps_everything(self):
        conn = self._conn()
        for _ in range(3):
            self._backup(conn, 1024)
        _prune_old_backups(conn)
        self.assertEqual(conn.backups.count(), 3)

    @override_settings(CLI2UI_MAX_AUTO_BACKUP_TOTAL_BYTES=50)
    def test_other_connections_are_not_touched(self):
        a, b = self._conn(), self._conn()
        for _ in range(3):
            self._backup(a, 40)
        b_backup = self._backup(b, 40)
        _prune_old_backups(a)
        # b's backup survives regardless of a's pruning
        self.assertTrue(Backup.objects.filter(pk=b_backup.pk).exists())


class ReplicationRecipeTests(SimpleTestCase):
    """The standby-setup recipe builder: pure string assembly from the live
    status + slots + the saved connection. No DB."""

    def _engine(self, host="db.example", port=5432, user="repl"):
        conn = Connection(kind="postgres", host=host, port=port, dbname="d", user=user)
        return PostgresEngine(conn)

    def _status(self, **kw):
        base = dict(wal_level="replica", max_wal_senders=10, max_replication_slots=10,
                    hot_standby="on", archive_mode="off", current_lsn="0/0",
                    is_standby=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_unready_primary_lists_each_conf_change(self):
        r = self._engine().replication_recipe(
            self._status(wal_level="minimal", max_wal_senders=0,
                         max_replication_slots=0, hot_standby="off"), [])
        self.assertFalse(r.ready)
        self.assertEqual(r.conf_changes, [
            ("wal_level", "replica"), ("max_wal_senders", "10"),
            ("max_replication_slots", "10"), ("hot_standby", "on")])

    def test_ready_primary_has_no_conf_changes(self):
        r = self._engine().replication_recipe(self._status(), [])
        self.assertTrue(r.ready)
        self.assertEqual(r.conf_changes, [])

    def test_fills_connection_values_into_commands(self):
        r = self._engine(host="pg.internal", port=5544, user="replica_user")\
            .replication_recipe(self._status(), [])
        self.assertIn("-h pg.internal", r.basebackup_cmd)
        self.assertIn("-p 5544", r.basebackup_cmd)
        self.assertIn("-U replica_user", r.basebackup_cmd)
        self.assertIn("--slot=standby_1", r.basebackup_cmd)
        self.assertEqual(r.primary_conninfo,
                         "host=pg.internal port=5544 user=replica_user")

    def test_reuses_an_existing_physical_slot(self):
        slots = [SimpleNamespace(slot_type="logical", name="log1"),
                 SimpleNamespace(slot_type="physical", name="my_slot")]
        r = self._engine().replication_recipe(self._status(), slots)
        self.assertTrue(r.slot_exists)
        self.assertEqual(r.slot_name, "my_slot")
        self.assertIn("--slot=my_slot", r.basebackup_cmd)

    def test_suggests_a_slot_when_none_exists(self):
        r = self._engine().replication_recipe(self._status(), [])
        self.assertFalse(r.slot_exists)
        self.assertEqual(r.slot_name, "standby_1")
        self.assertIn("standby_1", r.create_slot_sql)

    def test_never_embeds_the_password(self):
        eng = self._engine()
        eng.connection.password = "s3cret"
        r = eng.replication_recipe(self._status(), [])
        self.assertNotIn("s3cret", r.basebackup_cmd)
        self.assertNotIn("s3cret", r.primary_conninfo)
        self.assertNotIn("password", r.primary_conninfo.lower())


class IndexColumnsTextTests(SimpleTestCase):
    def _ix(self, definition):
        return Index(name="i", method="btree", unique=False, primary=False,
                     definition=definition, size="16 kB")

    def test_pulls_columns_from_definition(self):
        self.assertEqual(
            self._ix("CREATE INDEX i ON public.t USING btree (a, b)").columns_text,
            "a, b")

    def test_partial_index_stops_at_matching_paren(self):
        # The WHERE (…) clause must not leak into the column list.
        self.assertEqual(
            self._ix("CREATE INDEX i ON public.t USING btree (a) "
                     "WHERE (b > 0)").columns_text,
            "a")

    def test_expression_index_keeps_nested_parens(self):
        self.assertEqual(
            self._ix("CREATE INDEX i ON public.t USING btree "
                     "(lower(name))").columns_text,
            "lower(name)")

    def test_blank_when_no_parens(self):
        self.assertEqual(self._ix("weird").columns_text, "")


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

    def test_connection_headroom_pct_available_and_levels(self):
        h = ConnectionHeadroom(used=30, max=100, reserved=3)
        self.assertEqual(h.pct, 30)
        self.assertEqual(h.available, 70)
        self.assertEqual(h.level, "ok")
        self.assertEqual(ConnectionHeadroom(used=80, max=100).level, "warn")
        self.assertEqual(ConnectionHeadroom(used=95, max=100).level, "critical")

    def test_connection_headroom_handles_full_and_zero_limit(self):
        # At/over the limit there is no room left, never a negative count.
        self.assertEqual(ConnectionHeadroom(used=100, max=100).available, 0)
        # A zero/unknown limit must not divide by zero.
        self.assertEqual(ConnectionHeadroom(used=5, max=0).pct, 0)

    def test_column_comment_defaults_to_none(self):
        # comment is optional, so existing positional construction stays valid.
        c = Column(name="id", type="integer", nullable=False, default=None)
        self.assertIsNone(c.comment)
        self.assertEqual(
            Column("id", "integer", False, None, "the primary key").comment,
            "the primary key",
        )

    def test_column_generated_defaults_to_none(self):
        # generated/generation_expr are optional and trail comment, so existing
        # positional construction stays valid and a plain column isn't generated.
        c = Column(name="id", type="integer", nullable=False, default=None)
        self.assertIsNone(c.generated)
        self.assertFalse(c.is_generated)
        g = Column("area", "integer", True, None, generated="stored",
                   generation_expr="(w * h)")
        self.assertTrue(g.is_generated)
        self.assertEqual(g.generated, "stored")


# --- engine factory (no DB) -------------------------------------------------

class GetEngineTests(SimpleTestCase):
    def test_postgres_returns_engine(self):
        from core.engines.postgres import PostgresEngine
        conn = SimpleNamespace(kind="postgres")
        self.assertIsInstance(get_engine(conn), PostgresEngine)

    def test_mysql_returns_engine(self):
        from core.engines.mysql import MysqlEngine
        conn = SimpleNamespace(kind="mysql")
        self.assertIsInstance(get_engine(conn), MysqlEngine)

    def test_unsupported_kind_raises(self):
        with self.assertRaises(EngineError):
            get_engine(SimpleNamespace(kind="sqlite"))


# --- MySQL engine pure logic (no DB) ----------------------------------------

class MysqlPureLogicTests(SimpleTestCase):
    """The database-independent helpers of the MySQL engine: identifier quoting,
    size formatting, row→dataclass mappers, the EXPLAIN JSON parser and the
    error cleaner. No MySQL server — the catalog-touching methods are verified by
    hand against a sample DB (see project notes)."""

    def test_ident_quotes_and_doubles_backticks(self):
        from core.engines.mysql import _ident
        self.assertEqual(_ident("orders"), "`orders`")
        self.assertEqual(_ident("we`ird"), "`we``ird`")

    def test_generated_kind_maps_extra_flag(self):
        from core.engines.mysql import _generated_kind
        self.assertEqual(_generated_kind("VIRTUAL GENERATED"), "virtual")
        self.assertEqual(_generated_kind("STORED GENERATED"), "stored")
        # EXTRA may carry other flags; substring match still finds the kind.
        self.assertEqual(_generated_kind("DEFAULT_GENERATED on update CURRENT_TIMESTAMP"), None)
        self.assertIsNone(_generated_kind(""))
        self.assertIsNone(_generated_kind(None))

    def test_pretty_size_scales_units(self):
        from core.engines.mysql import _pretty_size
        self.assertIsNone(_pretty_size(None))
        self.assertEqual(_pretty_size(0), "0 B")
        self.assertEqual(_pretty_size(2048), "2 kB")
        self.assertEqual(_pretty_size(5 * 1048576), "5.0 MB")
        self.assertEqual(_pretty_size(3 * 1073741824), "3.0 GB")

    def test_clean_prefers_driver_message(self):
        import pymysql
        from core.engines.mysql import _clean
        self.assertEqual(_clean(pymysql.Error(1045, "Access denied")), "Access denied")
        self.assertEqual(_clean(pymysql.Error()), "Could not connect to MySQL.")

    def test_role_mapping(self):
        from core.engines.mysql import _role
        r = _role(("root", "localhost", "Y", "N"))
        self.assertEqual(r.name, "root@localhost")
        self.assertTrue(r.superuser)
        self.assertTrue(r.can_login)
        self.assertIn("Superuser", r.attributes)
        locked = _role(("app", "%", "N", "Y"))
        self.assertFalse(locked.can_login)
        self.assertIn("Locked", locked.attributes)

    def test_index_mapping_aggregates_columns(self):
        from core.engines.mysql import _index
        idx = _index(("PRIMARY", "BTREE", 0, "id"))
        self.assertTrue(idx.primary)
        self.assertTrue(idx.unique)
        self.assertEqual(idx.method, "btree")
        self.assertIsNone(idx.size)
        multi = _index(("idx_cust_date", "BTREE", 1, "customer_id, created_at"))
        self.assertFalse(multi.unique)
        self.assertFalse(multi.primary)
        # columns_text reads the parenthesised list out of the definition.
        self.assertEqual(multi.columns_text, "customer_id, created_at")

    def test_activity_mapping_maps_command_to_state(self):
        from core.engines.mysql import _activity
        running = _activity((7, "demo", "shop", "h:1", "Query", "executing",
                             3, "SELECT 1", 0))
        self.assertEqual(running.state, "active")
        self.assertEqual(running.query, "SELECT 1")
        self.assertFalse(running.is_self)
        idle = _activity((8, "demo", "shop", "h:2", "Sleep", None, 99, None, 1))
        self.assertEqual(idle.state, "sleep")
        self.assertEqual(idle.query, "")
        self.assertTrue(idle.is_self)

    def test_parse_plan_builds_tree(self):
        import json
        from core.engines.mysql import _parse_plan
        from core.plan_diff import to_text
        payload = {"query_block": {
            "cost_info": {"query_cost": "12.34"},
            "ordering_operation": {"nested_loop": [
                {"table": {"table_name": "orders", "access_type": "ALL",
                           "rows_produced_per_join": 1000,
                           "cost_info": {"prefix_cost": "5.0"},
                           "attached_condition": "(o.total > 1)"}},
                {"table": {"table_name": "customers", "access_type": "eq_ref",
                           "key": "PRIMARY", "rows_produced_per_join": 1,
                           "cost_info": {"prefix_cost": "10.0"}}},
            ]}}}
        node = _parse_plan(json.dumps(payload))
        self.assertEqual(node.node_type, "Sort")
        self.assertEqual(node.total_cost, 12.34)
        loop = node.children[0]
        self.assertEqual(loop.node_type, "Nested Loop")
        self.assertEqual(loop.children[0].relation, "orders")
        self.assertEqual(loop.children[0].node_type, "Full Table Scan")
        self.assertEqual(loop.children[1].node_type, "Index Scan")
        # The whole tree renders without error (used by snapshots / diffs).
        self.assertIn("orders", to_text(node))

    def test_unsupported_features_raise_engine_error(self):
        from core.engines import EngineError
        from core.engines.mysql import MysqlEngine
        conn = Connection(kind="mysql", host="h", port=3306,
                          dbname="shop", user="u", password="p")
        engine = MysqlEngine(conn)
        with self.assertRaises(EngineError):
            engine.rename_database("a", "b")
        with self.assertRaises(EngineError):
            engine.create_database("x", template="shop")
        with self.assertRaises(EngineError):
            engine.drop_index("shop", "idx")           # no table given
        with self.assertRaises(EngineError):
            engine.drop_database("shop")               # the connected DB
        with self.assertRaises(EngineError):
            engine.whatif_cursor()
        # Features with no InnoDB concept degrade to empty without touching a DB.
        self.assertEqual(engine.vacuum_stats(), [])
        self.assertEqual(engine.bloat_estimates(), [])
        self.assertEqual(engine.list_schemas(), [])

    def test_supports_flags_separate_na_from_empty(self):
        # vacuum/bloat/schemas are conceptually absent for MySQL, so panels show
        # "not applicable" rather than an empty card. Lock waits are NOT declared
        # unsupported — they're implemented and raise when undetectable, so they
        # must never be reported as a flat "supported but empty".
        from core.engines.mysql import MysqlEngine
        engine = MysqlEngine(Connection(kind="mysql", host="h", port=3306,
                                        dbname="shop", user="u", password="p"))
        self.assertFalse(engine.supports("vacuum"))
        self.assertFalse(engine.supports("bloat"))
        self.assertFalse(engine.supports("schemas"))
        self.assertTrue(engine.supports("locks"))

    def test_lock_waits_group_blockers_per_blocked_session(self):
        from core.engines.mysql import _lock_waits
        # PID 20 is blocked by two holders (51, 52); PID 30 by one (51). Each
        # (blocked, blocker) pair is one SQL row; the helper folds them.
        rows = [
            (20, "app", "UPDATE t SET x=1", 7, "RECORD", "X", "shop.t",
             51, "root", "Sleep", "BEGIN"),
            (20, "app", "UPDATE t SET x=1", 7, "RECORD", "X", "shop.t",
             52, "etl", "Query", "UPDATE t SET y=2"),
            (30, "web", "DELETE FROM t", 3, "RECORD", "X", "shop.t",
             51, "root", "Sleep", "BEGIN"),
        ]
        waits = _lock_waits(rows)
        self.assertEqual({w.blocked_pid for w in waits}, {20, 30})
        first = next(w for w in waits if w.blocked_pid == 20)
        self.assertEqual([b.pid for b in first.blockers], [51, 52])
        self.assertEqual(first.object, "shop.t")
        self.assertEqual(first.wait_secs, 7)
        # A blocker sitting in COMMAND='Sleep' is idle inside an open txn.
        self.assertEqual(first.blockers[0].state, "idle in transaction")
        self.assertEqual(first.blockers[1].state, "query")

    def test_lock_waits_empty_when_nothing_blocked(self):
        from core.engines.mysql import _lock_waits
        self.assertEqual(_lock_waits([]), [])

    def test_unused_index_mapping(self):
        from core.engines.mysql import _unused_index
        u = _unused_index(("shop", "orders", "idx_old"))
        self.assertEqual((u.schema, u.table, u.name), ("shop", "orders", "idx_old"))
        self.assertEqual(u.scans, 0)        # unused by definition
        self.assertIsNone(u.size)           # MySQL exposes no per-index size


class MysqlConnectionMockTests(SimpleTestCase):
    """The MySQL engine's connection-touching SQL: the lock-wait safety guard,
    the unused-index lookup, and the SQL the filter builder / DDL generate. No
    MySQL server — pymysql.connect is mocked and we assert what the engine *would*
    send, mirroring the Postgres connection tests (psycopg2.connect mocked)."""

    def _engine(self):
        from core.engines.mysql import MysqlEngine
        return MysqlEngine(Connection(kind="mysql", host="h", port=3306,
                                      dbname="shop", user="u", password="p"))

    @staticmethod
    def _cursor(connect):
        """Point mocked pymysql.connect at a cursor we control, so
        `with self._connect() as conn: with conn.cursor() as cur:` yields it."""
        cur = unittest.mock.MagicMock()
        connect.return_value.cursor.return_value.__enter__.return_value = cur
        return cur

    # --- list_blocking: the safety guard (must never answer a false "empty") --

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_blocking_raises_when_performance_schema_off(self, connect):
        from core.engines.mysql import BLOCKING_SQL
        cur = self._cursor(connect)
        cur.fetchone.return_value = (0,)            # @@performance_schema = OFF
        with self.assertRaises(EngineError):
            self._engine().list_blocking()
        # It must NOT have run the lock-wait query and returned its empty result —
        # that would be the false negative Phase 2 exists to prevent.
        ran = [c.args[0] for c in cur.execute.call_args_list]
        self.assertNotIn(BLOCKING_SQL, ran)
        cur.fetchall.assert_not_called()

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_blocking_raises_on_pre_8_0_server(self, connect):
        import pymysql
        cur = self._cursor(connect)
        cur.fetchone.return_value = (1,)            # PS on, but the query fails…
        # …because data_lock_waits is missing on the server (older than 8.0).
        cur.execute.side_effect = [None,
                                   pymysql.err.ProgrammingError(1146, "no such table")]
        with self.assertRaises(EngineError):
            self._engine().list_blocking()

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_blocking_returns_grouped_waits_when_enabled(self, connect):
        cur = self._cursor(connect)
        cur.fetchone.return_value = (1,)
        cur.fetchall.return_value = [
            (20, "app", "UPDATE t", 5, "RECORD", "X", "shop.t",
             51, "root", "Sleep", "BEGIN"),
        ]
        waits = self._engine().list_blocking()
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].blocked_pid, 20)
        self.assertEqual(waits[0].blockers[0].pid, 51)

    # --- unused_indexes: scoped to the connected database --------------------

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_unused_indexes_scopes_to_database_and_maps(self, connect):
        from core.engines.mysql import UNUSED_INDEXES_SQL
        cur = self._cursor(connect)
        cur.fetchall.return_value = [("shop", "orders", "idx_old")]
        result = self._engine().unused_indexes()
        cur.execute.assert_called_once_with(UNUSED_INDEXES_SQL, ("shop",))
        self.assertEqual(result[0].name, "idx_old")

    # --- filter builder / DDL: identifiers quoted, values bound --------------

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_filter_rows_quotes_identifiers_and_binds_values(self, connect):
        engine = self._engine()
        cur = self._cursor(connect)
        cur.fetchmany.return_value = []
        cur.description = [("status",)]
        cols = [SimpleNamespace(name="status"), SimpleNamespace(name="total")]
        with unittest.mock.patch.object(engine, "list_columns", return_value=cols):
            engine.filter_rows("shop", "orders", [
                {"column": "status", "op": "eq", "value": "paid"},
                {"column": "total", "op": "ge", "value": "100"},
            ], limit=50)
        query = next(c for c in cur.execute.call_args_list
                     if c.args[0].startswith("SELECT * FROM"))
        self.assertEqual(
            query.args[0],
            "SELECT * FROM `shop`.`orders` "
            "WHERE `status` = %s AND `total` >= %s LIMIT %s")
        self.assertEqual(query.args[1], ["paid", "100", 51])  # 51 = limit + 1 sentinel

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_filter_rows_rejects_unknown_column(self, connect):
        engine = self._engine()
        with unittest.mock.patch.object(
                engine, "list_columns",
                return_value=[SimpleNamespace(name="status")]):
            with self.assertRaises(EngineError):
                engine.filter_rows("shop", "orders",
                                   [{"column": "evil", "op": "eq", "value": "x"}])

    def test_create_index_builds_quoted_ddl(self):
        engine = self._engine()
        cols = [SimpleNamespace(name="customer_id"), SimpleNamespace(name="created_at")]
        with unittest.mock.patch.object(engine, "list_columns", return_value=cols), \
             unittest.mock.patch.object(engine, "_execute") as ex:
            engine.create_index("shop", "orders", ["customer_id", "created_at"],
                                method="btree", name="idx_cust_date")
        self.assertEqual(
            ex.call_args.args[0],
            "CREATE INDEX `idx_cust_date` ON `shop`.`orders` "
            "(`customer_id`, `created_at`) USING BTREE")

    def test_create_index_rejects_unknown_column_and_bad_method(self):
        engine = self._engine()
        with unittest.mock.patch.object(
                engine, "list_columns", return_value=[SimpleNamespace(name="id")]):
            with self.assertRaises(EngineError):
                engine.create_index("shop", "orders", ["nope"])      # unknown column
            with self.assertRaises(EngineError):
                engine.create_index("shop", "orders", ["id"], method="gist")  # bad method

    # --- backup: mysqldump / mysql, password via env, no shell ---------------

    @unittest.mock.patch("core.engines.mysql.subprocess.run")
    def test_dump_database_runs_mysqldump_with_password_in_env(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=b"-- dump", stderr=b"")
        dump = self._engine().dump_database("shop", fmt="custom")
        argv, kwargs = run.call_args.args[0], run.call_args.kwargs
        self.assertEqual(argv[0], "mysqldump")
        self.assertIn("shop", argv)
        self.assertIn("--single-transaction", argv)
        # The password goes through the environment, never the command line.
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], "p")
        self.assertNotIn("p", argv)
        self.assertTrue(dump.filename.endswith(".sql"))
        self.assertEqual(dump.data, b"-- dump")

    @unittest.mock.patch("core.engines.mysql.subprocess.run")
    def test_dump_table_passes_database_and_table(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=b"-- dump", stderr=b"")
        self._engine().dump_table("shop", "orders")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-2:], ["shop", "orders"])  # mysqldump <db> <table>

    @unittest.mock.patch("core.engines.mysql.subprocess.run")
    def test_dump_raises_when_tool_missing(self, run):
        run.side_effect = FileNotFoundError()
        with self.assertRaises(EngineError):
            self._engine().dump_database("shop")

    @unittest.mock.patch("core.engines.mysql.subprocess.run")
    def test_dump_raises_on_nonzero_exit(self, run):
        run.return_value = SimpleNamespace(
            returncode=2, stdout=b"", stderr=b"mysqldump: Error: access denied")
        with self.assertRaises(EngineError) as ctx:
            self._engine().dump_database("shop")
        self.assertIn("access denied", str(ctx.exception))

    @unittest.mock.patch("core.engines.mysql.subprocess.Popen")
    def test_restore_stream_pipes_into_mysql_client(self, popen):
        proc = popen.return_value
        proc.stderr.read.return_value = b""
        proc.wait.return_value = 0
        proc.returncode = 0
        self._engine().restore_stream("shop", io.BytesIO(b"-- sql"))
        argv, kwargs = popen.call_args.args[0], popen.call_args.kwargs
        self.assertEqual(argv[0], "mysql")
        self.assertEqual(argv[-1], "shop")
        self.assertEqual(kwargs["env"]["MYSQL_PWD"], "p")

    @unittest.mock.patch("core.engines.mysql.subprocess.Popen")
    def test_restore_stream_raises_on_nonzero_exit(self, popen):
        proc = popen.return_value
        proc.stderr.read.return_value = b""
        proc.wait.return_value = 1
        proc.returncode = 1
        with self.assertRaises(EngineError):
            self._engine().restore_stream("shop", io.BytesIO(b"-- sql"))

    def test_mysql_does_not_support_db_template(self):
        # Drives the restore-into-new-db path: MySQL has no CREATE DATABASE …
        # TEMPLATE, so the view must create the database empty instead.
        self.assertFalse(self._engine().supports("db_template"))

    # --- settings: SET PERSIST, name whitelisted, value bound ----------------

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_update_setting_splices_numeric_value_unquoted(self, connect):
        # A numeric variable rejects a quoted value ("Incorrect argument type"),
        # so a digits-only value is spliced directly (and is injection-safe).
        cur = self._cursor(connect)
        cur.fetchone.side_effect = [(1,), ("max_connections", "200")]
        setting = self._engine().update_setting("max_connections", "200")
        persist = next(c for c in cur.execute.call_args_list
                       if c.args[0].startswith("SET PERSIST"))
        self.assertEqual(persist.args[0], "SET PERSIST max_connections = 200")
        self.assertEqual(len(persist.args), 1)          # no bound params
        self.assertEqual(setting.value, "200")

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_update_setting_binds_non_numeric_value(self, connect):
        cur = self._cursor(connect)
        cur.fetchone.side_effect = [(1,), ("character_set_server", "utf8mb4")]
        self._engine().update_setting("character_set_server", "utf8mb4")
        persist = next(c for c in cur.execute.call_args_list
                       if c.args[0].startswith("SET PERSIST"))
        self.assertEqual(persist.args[0], "SET PERSIST character_set_server = %s")
        self.assertEqual(persist.args[1], ["utf8mb4"])  # string value bound, quoted

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_update_setting_rejects_unknown_variable(self, connect):
        cur = self._cursor(connect)
        cur.fetchone.return_value = None                # not in global_variables
        with self.assertRaises(EngineError):
            self._engine().update_setting("max_connections", "200")
        ran = [c.args[0] for c in cur.execute.call_args_list]
        self.assertFalse(any(s.startswith("SET PERSIST") for s in ran))

    def test_update_setting_rejects_malformed_name_without_db(self):
        # A name that isn't a valid system-variable identifier is refused before
        # it could ever reach SET PERSIST.
        with self.assertRaises(EngineError):
            self._engine().update_setting("max_connections; DROP", "1")

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_reset_setting_drops_persisted_and_reverts_runtime(self, connect):
        cur = self._cursor(connect)
        cur.fetchone.side_effect = [(1,), ("max_connections", "151")]
        self._engine().reset_setting("max_connections")
        ran = [c.args[0] for c in cur.execute.call_args_list]
        self.assertIn("RESET PERSIST IF EXISTS max_connections", ran)
        self.assertIn("SET GLOBAL max_connections = DEFAULT", ran)

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_list_settings_maps_and_detects_bool(self, connect):
        cur = self._cursor(connect)
        cur.fetchall.return_value = [("max_connections", "151"), ("autocommit", "ON")]
        rows = {s.name: s for s in self._engine().list_settings(names=["x"])}
        self.assertEqual(rows["max_connections"].vartype, "string")
        self.assertEqual(rows["autocommit"].vartype, "bool")
        self.assertEqual(rows["autocommit"].value, "on")   # normalised for the toggle

    def test_settings_categories_empty_and_common_nonempty(self):
        engine = self._engine()
        self.assertEqual(engine.list_setting_categories(), [])
        self.assertEqual(engine.pending_restart_settings(), [])
        self.assertIn("innodb_buffer_pool_size", engine.common_settings())

    # --- replication: binlog/GTID status, recipe, no slots -------------------

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_replication_status_reads_source_posture(self, connect):
        cur = self._cursor(connect)
        # 1: the @@global vars; 2: SHOW BINARY LOG STATUS; 3: SHOW REPLICA STATUS.
        cur.fetchone.side_effect = [
            (1, 5, "ON"),                       # log_bin, server_id, gtid_mode
            ("mysql-bin.000007", 154),          # binlog file, position
            None,                               # not a replica
        ]
        status = self._engine().replication_status()
        self.assertEqual(status.role, "source")
        self.assertTrue(status.log_bin)
        self.assertEqual(status.server_id, 5)
        self.assertEqual(status.binlog_file, "mysql-bin.000007")
        self.assertEqual(status.binlog_pos, 154)
        self.assertTrue(status.ready)

    @unittest.mock.patch("core.engines.mysql.pymysql.connect")
    def test_replication_status_detects_replica(self, connect):
        cur = self._cursor(connect)
        # SHOW REPLICA STATUS columns are mapped by name (zip with description).
        cur.description = [("Source_Host",), ("Replica_IO_Running",),
                           ("Replica_SQL_Running",), ("Seconds_Behind_Source",)]
        cur.fetchone.side_effect = [
            (1, 12, "ON"),                      # vars: log_bin, server_id, gtid_mode
            ("mysql-bin.000003", 99),           # binlog file, position
            ("10.0.0.9", "Yes", "Yes", 0),      # the replica-status row
        ]
        status = self._engine().replication_status()
        self.assertEqual(status.role, "replica")
        self.assertEqual(status.source_host, "10.0.0.9")
        self.assertTrue(status.io_running)
        self.assertTrue(status.healthy)
        self.assertEqual(status.seconds_behind, 0)

    def test_replication_recipe_lists_steps_when_not_ready(self):
        engine = self._engine()
        not_ready = SimpleNamespace(log_bin=False, server_id=0, gtid_mode="OFF")
        recipe = engine.replication_recipe(not_ready)
        self.assertFalse(recipe.ready)
        params = [p for p, _ in recipe.conf_changes]
        self.assertIn("log_bin", params)
        self.assertIn("server_id", params)
        self.assertIn("GRANT REPLICATION SLAVE", recipe.create_user_sql)
        self.assertIn("CHANGE REPLICATION SOURCE TO", recipe.change_source_sql)
        self.assertIn("SOURCE_HOST='h'", recipe.change_source_sql)
        self.assertEqual(recipe.start_replica_sql, "START REPLICA;")

    def test_replication_recipe_ready_when_configured(self):
        engine = self._engine()
        ready = SimpleNamespace(log_bin=True, server_id=1, gtid_mode="ON")
        self.assertTrue(engine.replication_recipe(ready).ready)

    def test_replication_slots_unsupported(self):
        engine = self._engine()
        self.assertEqual(engine.list_replication_slots(), [])
        self.assertFalse(engine.supports("replication_slots"))
        with self.assertRaises(EngineError):
            engine.create_replication_slot("s1")
        with self.assertRaises(EngineError):
            engine.drop_replication_slot("s1")


# --- models + form (sqlite management DB) -----------------------------------

class BackupModelTests(SimpleTestCase):
    def test_pretty_size_scales_units(self):
        self.assertEqual(Backup(byte_size=512).pretty_size, "512 B")
        self.assertEqual(Backup(byte_size=2048).pretty_size, "2 kB")
        self.assertEqual(Backup(byte_size=5 * 1048576).pretty_size, "5.0 MB")


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


def _has_pg_dump() -> bool:
    import shutil
    return shutil.which("pg_dump") is not None


def _has_restore_tools() -> bool:
    import shutil
    return all(shutil.which(t) for t in ("pg_dump", "psql", "pg_restore"))


def _client_major():
    import re
    import subprocess  # nosec B404
    try:
        out = subprocess.run(["pg_dump", "--version"],  # nosec B603 B607
                             capture_output=True, text=True)
        m = re.search(r"(\d+)\.", out.stdout)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _server_major():
    try:
        with get_engine(_sampledb())._connect() as conn, conn.cursor() as cur:
            cur.execute("SHOW server_version_num")
            return int(cur.fetchone()[0]) // 10000
    except Exception:
        return None


def _restore_compatible() -> bool:
    # A clean round-trip restore needs the client (pg_dump/psql) major version to
    # be no newer than the server's — a newer client emits settings (e.g.
    # transaction_timeout in PG17) that an older server rejects. This is a real
    # pg constraint, not a code issue, so the round-trip tests gate on it.
    if not _has_restore_tools():
        return False
    c, s = _client_major(), _server_major()
    return c is not None and s is not None and c <= s


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

    def test_connection_headroom_reports_usage_against_limit(self):
        h = self.engine.connection_headroom()
        # Our own connection counts, so at least one is in use, under the limit.
        self.assertGreaterEqual(h.used, 1)
        self.assertGreater(h.max, h.used)
        self.assertEqual(h.available, h.max - h.used)
        self.assertEqual(set(h.by_state), {"active", "idle", "idle in transaction"})

    def test_stream_query_yields_header_then_all_rows(self):
        # The export path streams the FULL result, past run_query's display cap.
        gen = self.engine.stream_query("SELECT generate_series(1, 5000) AS n")
        columns = next(gen)
        self.assertEqual(columns, ["n"])
        rows = list(gen)
        self.assertEqual(len(rows), 5000)
        self.assertEqual(rows[0][0], 1)

    def test_stream_query_read_only_rejects_writes(self):
        # Read-only is enforced the same way as run_query; the error surfaces on
        # the first next() (which runs the query), before any rows stream.
        with self.assertRaises(EngineError):
            next(self.engine.stream_query("CREATE TEMP TABLE _cli2ui_probe (x int)"))

    def test_stream_table_yields_header_then_rows(self):
        # stream_table is SELECT * over a quoted identifier, same streaming path.
        gen = self.engine.stream_table("public", "orders")
        columns = next(gen)
        self.assertIn("customer_id", columns)
        self.assertGreater(len(list(gen)), 0)

    def test_stream_table_bad_table_raises_on_first_next(self):
        with self.assertRaises(EngineError):
            next(self.engine.stream_table("public", "no_such_table"))

    def test_filter_rows_applies_conditions(self):
        # total >= 0 matches every seeded order; an impossible id matches none.
        hit = self.engine.filter_rows(
            "public", "orders", [{"column": "total", "op": "ge", "value": "0"}])
        self.assertIn("customer_id", hit.columns)
        self.assertGreater(hit.rowcount, 0)
        miss = self.engine.filter_rows(
            "public", "orders", [{"column": "id", "op": "eq", "value": "-1"}])
        self.assertEqual(miss.rowcount, 0)

    def test_filter_rows_no_filters_returns_all_capped(self):
        res = self.engine.filter_rows("public", "orders", [])
        self.assertGreater(res.rowcount, 0)

    def test_filter_rows_bad_column_raises(self):
        with self.assertRaises(EngineError):
            self.engine.filter_rows(
                "public", "orders", [{"column": "nope", "op": "eq", "value": "1"}])

    def test_filter_rows_bad_operator_raises(self):
        with self.assertRaises(EngineError):
            self.engine.filter_rows(
                "public", "orders", [{"column": "id", "op": "drop", "value": "1"}])

    def test_filter_rows_is_null_needs_no_value(self):
        # A valueless operator must compose without binding a placeholder.
        res = self.engine.filter_rows(
            "public", "orders", [{"column": "total", "op": "notnull", "value": ""}])
        self.assertGreater(res.rowcount, 0)

    def _scratch_table(self, ddl):
        """Create a throwaway table for write tests; caller drops it in finally."""
        self.engine.run_query(ddl, read_only=False)

    def test_import_csv_appends_rows_matched_by_header(self):
        self._scratch_table("CREATE TABLE _cli2ui_imp (id int, label text)")
        try:
            # Header order reversed from table order — matched by name, not position.
            buf = io.BytesIO(b"label,id\nalice,1\nbob,2\n")
            count = self.engine.import_csv("public", "_cli2ui_imp", buf)
            self.assertEqual(count, 2)
            res = self.engine.filter_rows(
                "public", "_cli2ui_imp", [{"column": "id", "op": "eq", "value": "1"}])
            self.assertEqual(res.rows[0][res.columns.index("label")], "alice")
        finally:
            self.engine.run_query("DROP TABLE _cli2ui_imp", read_only=False)

    def test_import_csv_rolls_back_on_bad_data(self):
        self._scratch_table("CREATE TABLE _cli2ui_imp (id int, label text)")
        try:
            buf = io.BytesIO(b"id,label\n1,ok\nnotanint,bad\n")
            with self.assertRaises(EngineError):
                self.engine.import_csv("public", "_cli2ui_imp", buf)
            # The good row must not survive — the whole import is one transaction.
            res = self.engine.filter_rows("public", "_cli2ui_imp", [])
            self.assertEqual(res.rowcount, 0)
        finally:
            self.engine.run_query("DROP TABLE _cli2ui_imp", read_only=False)

    def test_import_csv_unknown_column_raises(self):
        self._scratch_table("CREATE TABLE _cli2ui_imp (id int)")
        try:
            buf = io.BytesIO(b"id,ghost\n1,x\n")
            with self.assertRaises(EngineError):
                self.engine.import_csv("public", "_cli2ui_imp", buf)
        finally:
            self.engine.run_query("DROP TABLE _cli2ui_imp", read_only=False)

    def test_explain_json_returns_a_tree(self):
        node = self.engine.explain_json("SELECT * FROM orders")
        self.assertTrue(node.node_type)
        self.assertIn("orders", node.summary)

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

    def test_list_indexes_marks_primary_key_and_validity(self):
        # orders has a primary key; its backing index should be flagged, and
        # every existing index should report as valid.
        idx = self.engine.list_indexes("public", "orders")
        self.assertTrue(any(i.primary for i in idx))
        self.assertTrue(all(i.valid for i in idx))

    def test_table_sizes_lists_sample_tables_with_pretty_sizes(self):
        sizes = self.engine.table_sizes()
        names = {s.name for s in sizes}
        self.assertIn("orders", names)
        orders = next(s for s in sizes if s.name == "orders")
        self.assertGreater(orders.total_bytes, 0)
        self.assertTrue(orders.total)  # pretty string like "X kB"
        # Sorted largest-first.
        self.assertEqual([s.total_bytes for s in sizes],
                         sorted((s.total_bytes for s in sizes), reverse=True))

    def test_unused_indexes_excludes_primary_and_reports_size(self):
        # Create a secondary index nobody queries → it shows up as unused;
        # the primary key never does (it backs a constraint).
        name = "cli2ui_test_unused_idx"
        self.engine.create_index("public", "orders", ["total"], name=name)
        try:
            unused = self.engine.unused_indexes()
            by_name = {u.name: u for u in unused}
            self.assertIn(name, by_name)
            self.assertEqual(by_name[name].scans, 0)
            self.assertNotIn("orders_pkey", by_name)  # primary excluded
        finally:
            self.engine.drop_index("public", name)

    def test_vacuum_stats_report_counts_and_ratio(self):
        stats = self.engine.vacuum_stats()
        by_name = {v.name: v for v in stats}
        self.assertIn("orders", by_name)
        orders = by_name["orders"]
        self.assertGreaterEqual(orders.live, 0)
        self.assertGreaterEqual(orders.dead, 0)
        self.assertTrue(0.0 <= orders.dead_ratio <= 1.0)
        # Sorted by dead tuples, most first.
        self.assertEqual([v.dead for v in stats],
                         sorted((v.dead for v in stats), reverse=True))

    def test_create_index_preserves_column_order(self):
        # Composite index column order is significant and must follow the
        # order we pass, not the table's column order.
        name = "cli2ui_test_order_idx"
        self.engine.create_index("public", "orders", ["total", "customer_id"],
                                 name=name)
        try:
            ix = next(i for i in self.engine.list_indexes("public", "orders")
                      if i.name == name)
            self.assertEqual(ix.columns_text, "total, customer_id")
        finally:
            self.engine.drop_index("public", name)

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

    def test_create_rename_drop_database_roundtrip(self):
        a, b = "cli2ui_test_db", "cli2ui_test_db_renamed"
        self._drop_db_if_exists(a)
        self._drop_db_if_exists(b)
        self.engine.create_database(a)
        try:
            self.assertIn(a, self._db_names())
            self.engine.rename_database(a, b)
            names = self._db_names()
            self.assertIn(b, names)
            self.assertNotIn(a, names)
        finally:
            self._drop_db_if_exists(a)
            self._drop_db_if_exists(b)
        self.assertNotIn(b, self._db_names())

    def test_clone_database_via_template(self):
        name = "cli2ui_test_clone"
        self._drop_db_if_exists(name)
        # template0 has no connections ever, so it's the reliable clone source.
        self.engine.create_database(name, template="template0")
        try:
            self.assertIn(name, self._db_names())
        finally:
            self._drop_db_if_exists(name)

    def test_cannot_drop_or_rename_connected_database(self):
        current = self.engine.connection.dbname
        with self.assertRaises(EngineError):
            self.engine.drop_database(current)
        with self.assertRaises(EngineError):
            self.engine.rename_database(current, "something_else")

    def test_rename_and_reown_schema(self):
        a, b = "cli2ui_test_sch", "cli2ui_test_sch2"
        for n in (a, b):
            if self._has_schema(n):
                self.engine.drop_schema(n, cascade=True)
        self.engine.create_schema(a)
        try:
            owner = self.engine.connection.user  # a role that exists
            self.engine.alter_schema_owner(a, owner)
            self.engine.rename_schema(a, b)
            names = {s.name for s in self.engine.list_schemas()}
            self.assertIn(b, names)
            self.assertNotIn(a, names)
            renamed = next(s for s in self.engine.list_schemas() if s.name == b)
            self.assertEqual(renamed.owner, owner)
        finally:
            for n in (a, b):
                if self._has_schema(n):
                    self.engine.drop_schema(n, cascade=True)

    def test_alter_and_rename_role(self):
        a, b = "cli2ui_test_role", "cli2ui_test_role2"
        for n in (a, b):
            if self._has_role(n):
                self.engine.drop_role(n)
        self.engine.create_role(a)
        try:
            self.engine.alter_role(a, login=True, superuser=False,
                                   createdb=True, createrole=False)
            role = next(r for r in self.engine.list_roles() if r.name == a)
            self.assertTrue(role.can_login)
            self.assertTrue(role.createdb)
            self.assertFalse(role.superuser)
            self.engine.rename_role(a, b)
            names = {r.name for r in self.engine.list_roles()}
            self.assertIn(b, names)
            self.assertNotIn(a, names)
        finally:
            for n in (a, b):
                if self._has_role(n):
                    self.engine.drop_role(n)

    def test_cannot_rename_connected_role(self):
        with self.assertRaises(EngineError):
            self.engine.rename_role(self.engine.connection.user, "someone_else")

    def test_rename_truncate_drop_table_roundtrip(self):
        a, b = "cli2ui_test_tbl", "cli2ui_test_tbl2"
        for n in (a, b):
            self._drop_table_if_exists(n)
        self._exec(f'CREATE TABLE public."{a}" (x int)')
        self._exec(f'INSERT INTO public."{a}" VALUES (1), (2)')
        try:
            # rename
            self.engine.rename_table("public", a, b)
            names = {t.name for t in self.engine.list_tables()}
            self.assertIn(b, names)
            self.assertNotIn(a, names)
            # truncate empties the (renamed) table
            self.assertEqual(self._rowcount(b), 2)
            self.engine.truncate_table("public", b)
            self.assertEqual(self._rowcount(b), 0)
            # drop removes it from the tree
            self.engine.drop_table("public", b)
            self.assertNotIn(b, {t.name for t in self.engine.list_tables()})
        finally:
            for n in (a, b):
                self._drop_table_if_exists(n)

    def test_drop_table_is_not_cascade(self):
        # A dependent view blocks the drop (non-CASCADE), surfaced as EngineError
        # rather than silently taking the view down too.
        t, v = "cli2ui_test_dep_tbl", "cli2ui_test_dep_view"
        self._exec(f'DROP VIEW IF EXISTS public."{v}"')
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        self._exec(f'CREATE VIEW public."{v}" AS SELECT x FROM public."{t}"')
        try:
            with self.assertRaises(EngineError):
                self.engine.drop_table("public", t)
        finally:
            self._exec(f'DROP VIEW IF EXISTS public."{v}"')
            self._drop_table_if_exists(t)

    def test_add_rename_drop_column_roundtrip(self):
        t = "cli2ui_test_col_tbl"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        try:
            self.engine.add_column("public", t, "note", "text")
            self.assertIn("note", self._col_names(t))
            self.engine.rename_column("public", t, "note", "memo")
            names = self._col_names(t)
            self.assertIn("memo", names)
            self.assertNotIn("note", names)
            self.engine.drop_column("public", t, "memo")
            self.assertNotIn("memo", self._col_names(t))
        finally:
            self._drop_table_if_exists(t)

    def test_add_column_not_null_with_default_on_populated_table(self):
        # NOT NULL needs a default to backfill existing rows; the literal default
        # is bound, then cast to the column type by the DB.
        t = "cli2ui_test_col_nn"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        self._exec(f'INSERT INTO public."{t}" VALUES (1), (2)')
        try:
            self.engine.add_column("public", t, "active", "boolean",
                                   nullable=False, default="false")
            col = next(c for c in self.engine.list_columns("public", t)
                       if c.name == "active")
            self.assertFalse(col.nullable)
        finally:
            self._drop_table_if_exists(t)

    def test_add_column_rejects_unknown_type(self):
        # The type allow-list catches a bogus type before any DDL runs.
        t = "cli2ui_test_col_bad"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        try:
            with self.assertRaises(EngineError):
                self.engine.add_column("public", t, "y", "text; DROP TABLE x")
        finally:
            self._drop_table_if_exists(t)

    def test_rename_and_drop_column_reject_unknown_column(self):
        with self.assertRaises(EngineError):
            self.engine.rename_column("public", "orders", "no_such_col", "y")
        with self.assertRaises(EngineError):
            self.engine.drop_column("public", "orders", "no_such_col")

    def test_column_comment_is_surfaced(self):
        # COMMENT ON COLUMN shows up on the right column; uncommented = None.
        t = "cli2ui_test_comment"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (id int, note text)')
        self._exec(f'COMMENT ON COLUMN public."{t}".id IS \'the primary key\'')
        try:
            cols = {c.name: c for c in self.engine.list_columns("public", t)}
            self.assertEqual(cols["id"].comment, "the primary key")
            self.assertIsNone(cols["note"].comment)
        finally:
            self._drop_table_if_exists(t)

    def test_generated_column_is_surfaced(self):
        # A generated column reports its kind + expression; a plain column reports
        # neither. STORED works on every supported PG (VIRTUAL is the PG18+
        # default but isn't needed to prove the column viewer no longer hides it).
        t = "cli2ui_test_generated"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" '
                   f'(w int, h int, area int GENERATED ALWAYS AS (w * h) STORED)')
        try:
            cols = {c.name: c for c in self.engine.list_columns("public", t)}
            self.assertEqual(cols["area"].generated, "stored")
            self.assertTrue(cols["area"].is_generated)
            self.assertIn("*", cols["area"].generation_expr or "")
            self.assertIsNone(cols["w"].generated)
            self.assertFalse(cols["w"].is_generated)
        finally:
            self._drop_table_if_exists(t)

    def test_table_comment_is_surfaced(self):
        # COMMENT ON TABLE shows up; an uncommented table reports None.
        t = "cli2ui_test_tcomment"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (id int)')
        try:
            self.assertIsNone(self.engine.table_comment("public", t))
            self._exec(f'COMMENT ON TABLE public."{t}" IS \'orders, one per line\'')
            self.assertEqual(self.engine.table_comment("public", t),
                             "orders, one per line")
        finally:
            self._drop_table_if_exists(t)

    def test_alter_column_type_casts_existing_values(self):
        # text→integer isn't an implicit cast; the generated USING x::integer
        # makes it work and converts the stored values.
        t = "cli2ui_test_retype"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (amount text)')
        self._exec(f"INSERT INTO public.\"{t}\" VALUES ('1'), ('2')")
        try:
            self.engine.alter_column_type("public", t, "amount", "integer")
            col = next(c for c in self.engine.list_columns("public", t)
                       if c.name == "amount")
            self.assertEqual(col.type, "integer")
        finally:
            self._drop_table_if_exists(t)

    def test_alter_column_type_rejects_unknown_type(self):
        with self.assertRaises(EngineError):
            self.engine.alter_column_type("public", "orders", "total",
                                          "money; DROP TABLE x")

    def test_set_and_drop_column_not_null(self):
        t = "cli2ui_test_null"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        try:
            self.engine.set_column_null("public", t, "x", nullable=False)
            self.assertFalse(self._col("x", t).nullable)
            self.engine.set_column_null("public", t, "x", nullable=True)
            self.assertTrue(self._col("x", t).nullable)
        finally:
            self._drop_table_if_exists(t)

    def test_set_and_drop_column_default(self):
        t = "cli2ui_test_default"
        self._drop_table_if_exists(t)
        self._exec(f'CREATE TABLE public."{t}" (x int)')
        try:
            self.engine.set_column_default("public", t, "x", "5")
            self.assertIn("5", self._col("x", t).default or "")
            self.engine.set_column_default("public", t, "x", None)
            self.assertIsNone(self._col("x", t).default)
        finally:
            self._drop_table_if_exists(t)

    @unittest.skipUnless(_has_pg_dump(), "pg_dump not installed")
    def test_dump_database_plain_is_restorable_sql(self):
        dump = self.engine.dump_database("shop", fmt="plain")
        self.assertEqual(dump.content_type, "application/sql")
        self.assertTrue(dump.filename.endswith(".sql"))
        self.assertIn(b"orders", dump.data)
        self.assertIn(b"CREATE TABLE", dump.data)

    @unittest.skipUnless(_has_pg_dump(), "pg_dump not installed")
    def test_dump_database_custom_is_a_binary_archive(self):
        dump = self.engine.dump_database("shop", fmt="custom")
        self.assertEqual(dump.content_type, "application/octet-stream")
        self.assertTrue(dump.filename.endswith(".dump"))
        # pg_dump custom archives start with the "PGDMP" magic marker.
        self.assertTrue(dump.data.startswith(b"PGDMP"))

    @unittest.skipUnless(_has_pg_dump(), "pg_dump not installed")
    def test_dump_table_is_scoped_to_one_table(self):
        dump = self.engine.dump_table("public", "orders", fmt="plain")
        self.assertTrue(dump.filename.startswith("public.orders-"))
        self.assertIn(b"CREATE TABLE", dump.data)
        self.assertIn(b"orders", dump.data)
        # -t orders must not pull in a different table's DDL.
        self.assertNotIn(b"CREATE TABLE public.order_items", dump.data)

    @unittest.skipUnless(_has_pg_dump(), "pg_dump not installed")
    def test_dump_rejects_unknown_format(self):
        with self.assertRaises(EngineError):
            self.engine.dump_database("shop", fmt="bogus")

    @unittest.skipUnless(_has_pg_dump(), "pg_dump not installed")
    def test_dump_nonexistent_database_errors(self):
        with self.assertRaises(EngineError):
            self.engine.dump_database("cli2ui_no_such_db", fmt="plain")

    @unittest.skipUnless(_restore_compatible(),
                         "restore round-trip needs pg client major <= server major")
    def test_restore_plain_dump_into_new_database(self):
        # Round-trip: dump shop as plain SQL, restore it into a fresh DB via
        # psql, and confirm the schema landed.
        name = "cli2ui_test_restore_plain"
        self._drop_db_if_exists(name)
        data = self.engine.dump_database("shop", fmt="plain").data
        self.engine.create_database(name, template="template0")
        try:
            self.engine.restore(name, data)
            self.assertIn("orders", self._tables_in(name))
        finally:
            self._drop_db_if_exists(name)

    @unittest.skipUnless(_restore_compatible(),
                         "restore round-trip needs pg client major <= server major")
    def test_restore_custom_dump_into_new_database(self):
        # The custom-format archive goes through pg_restore (detected by the
        # PGDMP marker), not psql.
        name = "cli2ui_test_restore_custom"
        self._drop_db_if_exists(name)
        data = self.engine.dump_database("shop", fmt="custom").data
        self.engine.create_database(name, template="template0")
        try:
            self.engine.restore(name, data)
            self.assertIn("orders", self._tables_in(name))
        finally:
            self._drop_db_if_exists(name)

    @unittest.skipUnless(_has_restore_tools(), "psql/pg_restore not installed")
    def test_restore_bad_dump_errors(self):
        name = "cli2ui_test_restore_bad"
        self._drop_db_if_exists(name)
        self.engine.create_database(name, template="template0")
        try:
            with self.assertRaises(EngineError):
                self.engine.restore(name, b"this is not a valid dump;\n")
        finally:
            self._drop_db_if_exists(name)

    # helpers
    def _col_names(self, table):
        return {c.name for c in self.engine.list_columns("public", table)}

    def _col(self, name, table):
        return next(c for c in self.engine.list_columns("public", table)
                    if c.name == name)

    def _exec(self, raw_sql):
        with self.engine._connect() as conn, conn.cursor() as cur:
            cur.execute(raw_sql)

    def _rowcount(self, table):
        with self.engine._connect() as conn, conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM public."{table}"')
            return cur.fetchone()[0]

    def _drop_table_if_exists(self, name):
        self._exec(f'DROP TABLE IF EXISTS public."{name}"')

    def _has_role(self, name):
        return any(r.name == name for r in self.engine.list_roles())

    def _db_names(self):
        return {d.name for d in self.engine.list_databases()}

    def _tables_in(self, dbname):
        conn = SimpleNamespace(kind="postgres", host="localhost", port=5433,
                               dbname=dbname, user="demo", password="demo")
        return {t.name for t in get_engine(conn).list_tables()}

    def _drop_db_if_exists(self, name):
        if name in self._db_names():
            self.engine.drop_database(name, force=True)

    def _has_schema(self, name):
        return any(s.name == name for s in self.engine.list_schemas())


@unittest.skipUnless(_sampledb_reachable() and _has_pg_dump(),
                     "needs the sample DB and pg_dump")
class DumpViewTests(TestCase):
    """The backup download endpoints: GET → pg_dump → attachment response, with
    a plain-text error (502) when the dump fails."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="dump", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")

    def test_database_dump_downloads_sql_attachment(self):
        url = self.reverse("database_dump", args=[self.conn.pk])
        resp = self.client.get(url, {"name": "shop", "format": "plain"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".sql", resp["Content-Disposition"])
        self.assertIn(b"orders", resp.content)

    def test_table_dump_downloads_attachment(self):
        url = self.reverse("table_dump", args=[self.conn.pk])
        resp = self.client.get(
            url, {"schema": "public", "table": "orders", "format": "plain"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(b"orders", resp.content)

    def test_database_dump_failure_returns_error_text(self):
        url = self.reverse("database_dump", args=[self.conn.pk])
        resp = self.client.get(url, {"name": "cli2ui_no_such_db", "format": "plain"})
        self.assertEqual(resp.status_code, 502)


@unittest.skipUnless(_sampledb_reachable(), "needs the sample DB")
class QueryExportViewTests(TestCase):
    """The query-result export endpoint: POST SQL → streamed CSV/JSON attachment,
    full result set, with a plain-text error when the query is bad."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="export", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")

    def _get(self, fmt):
        url = self.reverse("query_export", args=[self.conn.pk])
        resp = self.client.post(
            url, {"sql": "SELECT generate_series(1, 3) AS n", "format": fmt})
        # StreamingHttpResponse: join the streamed chunks to inspect the body.
        body = b"".join(resp.streaming_content)
        return resp, body

    def test_csv_export_streams_full_result_as_attachment(self):
        resp, body = self._get("csv")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn(".csv", resp["Content-Disposition"])
        text = body.decode("utf-8-sig")  # tolerate the leading BOM
        self.assertIn("n", text.splitlines()[0])
        self.assertEqual(text.strip().splitlines()[-1], "3")

    def test_json_export_streams_array_of_objects(self):
        resp, body = self._get("json")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(".json", resp["Content-Disposition"])
        data = json.loads(body)
        self.assertEqual([r["n"] for r in data], [1, 2, 3])

    def test_bad_query_returns_error_not_a_download(self):
        url = self.reverse("query_export", args=[self.conn.pk])
        resp = self.client.post(url, {"sql": "SELECT * FROM no_such_table", "format": "csv"})
        self.assertEqual(resp.status_code, 502)

    def test_empty_sql_is_rejected(self):
        url = self.reverse("query_export", args=[self.conn.pk])
        resp = self.client.post(url, {"sql": "  ", "format": "csv"})
        self.assertEqual(resp.status_code, 400)


@unittest.skipUnless(_sampledb_reachable(), "needs the sample DB")
class TableExportViewTests(TestCase):
    """The table export endpoint: GET schema/table → streamed CSV/JSON of every
    row, with a plain-text error for a missing/bad table."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="texport", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")

    def test_csv_export_streams_every_row_as_attachment(self):
        url = self.reverse("table_export", args=[self.conn.pk])
        resp = self.client.get(url, {"schema": "public", "table": "orders", "format": "csv"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("orders", resp["Content-Disposition"])
        text = b"".join(resp.streaming_content).decode("utf-8-sig")
        self.assertIn("customer_id", text.splitlines()[0])

    def test_json_export_streams_array_of_objects(self):
        url = self.reverse("table_export", args=[self.conn.pk])
        resp = self.client.get(url, {"schema": "public", "table": "orders", "format": "json"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(".json", resp["Content-Disposition"])
        data = json.loads(b"".join(resp.streaming_content))
        self.assertIn("customer_id", data[0])

    def test_missing_table_param_is_rejected(self):
        url = self.reverse("table_export", args=[self.conn.pk])
        resp = self.client.get(url, {"schema": "public", "format": "csv"})
        self.assertEqual(resp.status_code, 400)

    def test_bad_table_returns_error_not_a_download(self):
        url = self.reverse("table_export", args=[self.conn.pk])
        resp = self.client.get(url, {"schema": "public", "table": "no_such_table", "format": "csv"})
        self.assertEqual(resp.status_code, 502)


@unittest.skipUnless(_sampledb_reachable(), "needs the sample DB")
class TableFilterViewTests(TestCase):
    """The filter builder endpoint: parallel col/op/val arrays → read-only
    SELECT, rendered as the result grid partial."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="tfilter", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")

    def test_filter_renders_matching_rows(self):
        url = self.reverse("table_filter", args=[self.conn.pk])
        resp = self.client.post(url, {
            "schema": "public", "table": "orders",
            "col": "total", "op": "ge", "val": "0"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_id")

    def test_blank_column_rows_are_dropped(self):
        # An all-blank form is "show everything" — not an error.
        url = self.reverse("table_filter", args=[self.conn.pk])
        resp = self.client.post(url, {
            "schema": "public", "table": "orders", "col": "", "op": "eq", "val": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "customer_id")

    def test_bad_column_renders_error_partial(self):
        url = self.reverse("table_filter", args=[self.conn.pk])
        resp = self.client.post(url, {
            "schema": "public", "table": "orders",
            "col": "nope", "op": "eq", "val": "1"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Filter error")


@unittest.skipUnless(_sampledb_reachable(), "needs the sample DB")
class TableImportViewTests(TestCase):
    """The CSV import endpoint: an uploaded CSV is COPYed into a scratch table
    and the detail panel re-renders with a row count; a bad file errors inline."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse
        self.reverse = reverse
        self.upload = SimpleUploadedFile
        self.conn = Connection.objects.create(
            name="timport", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")
        self.engine = get_engine(self.conn)
        self.engine.run_query(
            "CREATE TABLE _cli2ui_imp_v (id int, label text)", read_only=False)

    def tearDown(self):
        self.engine.run_query("DROP TABLE IF EXISTS _cli2ui_imp_v", read_only=False)

    def test_import_loads_rows_and_reports_count(self):
        csv = self.upload("data.csv", b"id,label\n1,a\n2,b\n3,c\n", content_type="text/csv")
        resp = self.client.post(self.reverse("table_import", args=[self.conn.pk]), {
            "schema": "public", "table": "_cli2ui_imp_v", "file": csv})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Imported 3")
        res = self.engine.filter_rows("public", "_cli2ui_imp_v", [])
        self.assertEqual(res.rowcount, 3)

    def test_missing_file_is_rejected_inline(self):
        resp = self.client.post(self.reverse("table_import", args=[self.conn.pk]), {
            "schema": "public", "table": "_cli2ui_imp_v"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Choose a CSV file")


@unittest.skipUnless(_sampledb_reachable() and _has_restore_tools(),
                     "needs the sample DB and psql/pg_restore")
class RestoreViewTests(TestCase):
    """Restore upload flow: an uploaded dump creates a new database and populates
    it; a bad dump leaves nothing behind (the new database is rolled back)."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="restore", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")
        self.engine = get_engine(self.conn)
        self.created = []

    def tearDown(self):
        for n in self.created:
            with contextlib.suppress(EngineError):
                self.engine.drop_database(n, force=True)

    def _db_names(self):
        return {d.name for d in self.engine.list_databases()}

    @unittest.skipUnless(_restore_compatible(),
                         "restore round-trip needs pg client major <= server major")
    def test_upload_creates_and_populates_new_database(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        name = "cli2ui_view_restore"
        self.created.append(name)
        if name in self._db_names():
            self.engine.drop_database(name, force=True)
        data = self.engine.dump_database("shop", fmt="plain").data
        upload = SimpleUploadedFile(f"{name}.sql", data,
                                    content_type="application/sql")
        resp = self.client.post(self.reverse("database_restore", args=[self.conn.pk]),
                                {"name": name, "dump": upload})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(name, self._db_names())

    def test_bad_upload_rolls_back_new_database(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        name = "cli2ui_view_restore_bad"
        self.created.append(name)
        if name in self._db_names():
            self.engine.drop_database(name, force=True)
        upload = SimpleUploadedFile(f"{name}.sql", b"NOT A VALID DUMP;\n")
        resp = self.client.post(self.reverse("database_restore", args=[self.conn.pk]),
                                {"name": name, "dump": upload})
        self.assertEqual(resp.status_code, 200)  # objects panel re-renders w/ error
        self.assertNotIn(name, self._db_names())  # half-made DB was dropped


@unittest.skipUnless(_sampledb_reachable() and _has_pg_dump(),
                     "needs the sample DB and pg_dump")
class AutoBackupTests(TestCase):
    """A destructive op takes an automatic safety snapshot first; an oversized
    snapshot is skipped (with a warning) but never blocks the operation."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="ab", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")
        self.engine = get_engine(self.conn)
        self.tbl = "cli2ui_autobackup_tbl"
        self._exec(f'DROP TABLE IF EXISTS public."{self.tbl}"')
        self._exec(f'CREATE TABLE public."{self.tbl}" (x int)')
        self._exec(f'INSERT INTO public."{self.tbl}" VALUES (1), (2)')

    def tearDown(self):
        self._exec(f'DROP TABLE IF EXISTS public."{self.tbl}"')

    def _exec(self, sql):
        with self.engine._connect() as c, c.cursor() as cur:
            cur.execute(sql)

    def _tables(self):
        return {t.name for t in self.engine.list_tables()}

    def test_drop_table_snapshots_first_then_drops(self):
        resp = self.client.post(self.reverse("table_drop", args=[self.conn.pk]),
                                {"schema": "public", "table": self.tbl})
        self.assertEqual(resp.status_code, 200)
        b = Backup.objects.get(connection=self.conn)
        self.assertEqual(b.kind, Backup.KIND_TABLE)
        self.assertEqual(b.target, f"public.{self.tbl}")
        self.assertGreater(b.byte_size, 0)
        self.assertTrue(bytes(b.data).startswith(b"PGDMP"))  # custom-format archive
        self.assertNotIn(self.tbl, self._tables())           # actually dropped

    @override_settings(CLI2UI_MAX_AUTO_BACKUP_BYTES=1)
    def test_oversized_snapshot_is_skipped_but_op_proceeds(self):
        resp = self.client.post(self.reverse("table_drop", args=[self.conn.pk]),
                                {"schema": "public", "table": self.tbl})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Backup.objects.count(), 0)   # too big → not stored
        self.assertNotIn(self.tbl, self._tables())    # dropped anyway


@unittest.skipUnless(_sampledb_reachable() and _has_pg_dump(),
                     "needs the sample DB and pg_dump")
class BackupPanelTests(TestCase):
    """The Backups panel: list, download, delete, and restore-into-a-new-DB."""

    def setUp(self):
        from django.urls import reverse
        self.reverse = reverse
        self.conn = Connection.objects.create(
            name="bp", kind="postgres", host="localhost", port=5433,
            dbname="shop", user="demo", password="demo")
        self.engine = get_engine(self.conn)
        data = self.engine.dump_table("public", "orders", fmt="custom").data
        self.backup = Backup.objects.create(
            connection=self.conn, operation="drop table", kind=Backup.KIND_TABLE,
            target="public.orders", dbname="shop", data=data, byte_size=len(data))

    def test_list_shows_the_snapshot(self):
        resp = self.client.get(self.reverse("backups", args=[self.conn.pk]))
        self.assertContains(resp, "public.orders")

    def test_download_returns_the_archive(self):
        resp = self.client.get(self.reverse("backup_download", args=[self.conn.pk]),
                               {"id": self.backup.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertTrue(resp.content.startswith(b"PGDMP"))

    def test_delete_removes_the_snapshot(self):
        resp = self.client.post(self.reverse("backup_delete", args=[self.conn.pk]),
                                {"id": self.backup.pk})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Backup.objects.count(), 0)

    @unittest.skipUnless(_restore_compatible(),
                         "restore round-trip needs pg client major <= server major")
    def test_restore_snapshot_into_new_database(self):
        name = "cli2ui_backup_restore"
        if name in {d.name for d in self.engine.list_databases()}:
            self.engine.drop_database(name, force=True)
        try:
            resp = self.client.post(self.reverse("backup_restore", args=[self.conn.pk]),
                                    {"id": self.backup.pk, "name": name})
            self.assertEqual(resp.status_code, 200)
            self.assertIn(name, {d.name for d in self.engine.list_databases()})
            conn = SimpleNamespace(kind="postgres", host="localhost", port=5433,
                                   dbname=name, user="demo", password="demo")
            self.assertIn("orders", {t.name for t in get_engine(conn).list_tables()})
        finally:
            with contextlib.suppress(EngineError):
                self.engine.drop_database(name, force=True)


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


class _BrowserE2E(LiveServerTestCase):
    """Shared scaffolding for the Playwright smoke tests: launch chromium once,
    seed a connection to the sample DB, and hand each test a fresh page. No test
    methods of its own, so it's never collected as a suite."""

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

    def open_section(self, name):
        """Navigate to a section through the overview hover menu (the bento)."""
        self.page.get_by_role("button", name="overview").hover()
        self.page.locator("#nav-menu").get_by_role("button", name=name).click()


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class ExplainDiffSmokeE2E(_BrowserE2E):
    """One end-to-end pass of the headline feature: EXPLAIN a query, save it,
    EXPLAIN a different one, save it, and diff the two — expecting the structured
    node diff with its 'plan changed' badge to render in the page."""

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
        self.open_section("SQL runner")
        page.locator("textarea[name=sql]:not(.hidden)").wait_for()

        # Two queries whose plans differ in shape (the ORDER BY adds a Sort node).
        self._explain_and_save("SELECT * FROM orders", "plain",
                               plan_token="Seq Scan on orders")
        self._explain_and_save("SELECT * FROM orders ORDER BY total", "sorted",
                               plan_token="Sort")

        self.open_section("Snapshots")
        page.locator("select[name=a]").wait_for()
        page.locator("select[name=a]").select_option(label="plain")
        page.locator("select[name=b]").select_option(label="sorted")
        page.get_by_role("button", name="Diff", exact=True).click()

        diff = page.locator("#snapshot-diff")
        expect(diff.get_by_text("plan changed")).to_be_visible()
        expect(diff).to_contain_text("Seq Scan on orders")
        expect(diff).to_contain_text("Sort")


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class IndexManagementSmokeE2E(_BrowserE2E):
    """One end-to-end pass of index management: open a table, create an index
    from the column picker, see it listed, then drop it — driving the real htmx
    swaps and the hx-confirm dialog on drop."""

    IDX = "e2e_orders_cust_idx"

    def tearDown(self):
        # Leave the sample DB clean even if an assertion fails mid-flow.
        try:
            get_engine(self.conn).drop_index("public", self.IDX)
        except EngineError:
            pass
        super().tearDown()

    def test_create_then_drop_index(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")

        # Open the orders table detail (the trailing match avoids order_items).
        page.locator('button[hx-get$="table=orders"]').click()
        page.locator("#detail h2").wait_for()

        # Indexes subtab → open the Create index drawer.
        page.get_by_role("button", name="Indexes", exact=True).click()
        page.get_by_role("button", name="+ Create index").first.click()
        create = page.locator('form[hx-post*="indexes/create"]')
        create.wait_for(state="visible")

        # Pick two columns in the REVERSE of their table order (orders is
        # …, customer_id, total, …) to prove the ordered picker controls the
        # composite index's column order — not the DOM/checkbox order.
        create.get_by_role("button", name="total", exact=True).click()
        create.get_by_role("button", name="customer_id", exact=True).click()
        create.locator('input[name=name]').fill(self.IDX)
        create.get_by_role("button", name="+ Create index").click()

        # The swap resets the panel to Columns; reopen Indexes to see the row,
        # with its columns in the chosen order.
        page.get_by_role("button", name="Indexes", exact=True).click()
        row = page.locator("#detail tr", has_text=self.IDX)
        expect(row).to_contain_text("total, customer_id")

        # Drop it (htmx hx-confirm fires window.confirm — auto-accept).
        page.on("dialog", lambda d: d.accept())
        row.get_by_role("button", name="drop").click()
        expect(page.locator("#detail")).not_to_contain_text(self.IDX)


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class IndexLabSmokeE2E(_BrowserE2E):
    """One end-to-end pass of the what-if index lab: open it, pick a table and a
    column, preview a hypothetical index, and see the real before/after timing
    and plan diff render — without creating anything."""

    def test_preview_hypothetical_index(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        self.open_section("Index lab")

        # Choose the orders table; its columns load (htmx swaps #lab).
        page.locator("#lab select[name=qualified]").wait_for()
        page.locator("#lab select[name=qualified]").select_option("public.orders")

        # Pick a column to index, then preview against the prefilled query.
        form = page.locator('form[hx-post*="lab/preview"]')
        form.get_by_role("button", name="customer_id", exact=True).click()
        form.locator("textarea[name=sql]").fill(
            "SELECT * FROM orders WHERE customer_id = 1")
        form.get_by_role("button", name="Preview index ▸").click()

        # Real timing + the create-for-real affordance show up; nothing created.
        result = page.locator("#lab-result")
        expect(result).to_contain_text("with index")
        expect(result).to_contain_text("ms")
        expect(result.get_by_role("button", name="Create for real ▸")).to_be_visible()


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class DatabaseManagementSmokeE2E(_BrowserE2E):
    """Create a database from the Objects panel, see it listed, then drop it."""

    DB = "cli2ui_e2e_db"

    def tearDown(self):
        try:
            get_engine(self.conn).drop_database(self.DB, force=True)
        except EngineError:
            pass
        super().tearDown()

    def test_create_then_drop_database(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        self.open_section("Objects")

        page.get_by_role("button", name="+ New database").click()
        create = page.locator('form[hx-post*="databases/create"]')
        create.wait_for(state="visible")
        create.locator("input[name=name]").fill(self.DB)
        create.get_by_role("button", name="+ Create database").click()

        row = page.locator("#detail tr", has_text=self.DB)
        expect(row).to_be_visible()

        page.on("dialog", lambda d: d.accept())
        row.get_by_role("button", name="drop").click()
        expect(page.locator("#detail")).not_to_contain_text(self.DB)


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class SchemaAlterSmokeE2E(_BrowserE2E):
    """Rename a schema through the Objects panel's edit drawer."""

    SCH, SCH2 = "cli2ui_e2e_sch", "cli2ui_e2e_sch2"

    def setUp(self):
        super().setUp()
        get_engine(self.conn).create_schema(self.SCH)

    def tearDown(self):
        eng = get_engine(self.conn)
        for n in (self.SCH, self.SCH2):
            try:
                eng.drop_schema(n, cascade=True)
            except EngineError:
                pass
        super().tearDown()

    def test_rename_schema_inline(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        self.open_section("Objects")
        page.get_by_role("button", name="Schemas", exact=True).click()

        row = page.locator("#detail tr", has_text=self.SCH)
        row.get_by_role("button", name="edit").click()
        edit = page.locator('form[hx-post*="schemas/alter"]')
        edit.locator("input[name=new]").fill(self.SCH2)
        edit.get_by_role("button", name="Save").click()

        objects = page.locator("#detail")
        expect(objects).to_contain_text(self.SCH2)
        expect(objects.get_by_text(self.SCH, exact=True)).to_have_count(0)


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class TableManagementSmokeE2E(_BrowserE2E):
    """Open a table, rename it from the header drawer (main pane AND sidebar tree
    update in one round trip), then drop it from the Danger subtab — driving the
    real htmx swaps, the rename drawer, and the hx-confirm dialog on drop."""

    TBL, TBL2 = "cli2ui_e2e_tbl", "cli2ui_e2e_tbl2"

    def setUp(self):
        super().setUp()
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE TABLE public."{self.TBL}" (x int)')

    def tearDown(self):
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            for n in (self.TBL, self.TBL2):
                cur.execute(f'DROP TABLE IF EXISTS public."{n}"')
        super().tearDown()

    def test_rename_then_drop_table(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")

        # Open the new table from the sidebar tree.
        page.locator(f'button[hx-get$="table={self.TBL}"]').click()
        expect(page.locator("#detail h2")).to_have_text(f"public.{self.TBL}")

        # Rename it via the header drawer; both panes update from one response.
        page.locator("#detail").get_by_role("button", name="rename", exact=True).click()
        form = page.locator('form[hx-post*="table/rename"]')
        form.locator("input[name=new_name]").fill(self.TBL2)
        form.get_by_role("button", name="Rename").click()

        expect(page.locator("#detail h2")).to_have_text(f"public.{self.TBL2}")
        tree = page.locator("#table-list")
        expect(tree.locator(f'button[hx-get$="table={self.TBL2}"]')).to_have_count(1)
        expect(tree.locator(f'button[hx-get$="table={self.TBL}"]')).to_have_count(0)

        # Drop it from the Danger subtab (hx-confirm → window.confirm, accepted).
        page.on("dialog", lambda d: d.accept())
        page.get_by_role("button", name="Danger", exact=True).click()
        page.get_by_role("button", name="Drop table", exact=True).click()
        expect(tree.locator(f'button[hx-get$="table={self.TBL2}"]')).to_have_count(0)


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class ColumnManagementSmokeE2E(_BrowserE2E):
    """Open a table, add a column from the drawer, rename it from the edit
    drawer, then drop it — driving the real htmx swaps and the hx-confirm
    dialog on drop."""

    TBL = "cli2ui_e2e_col_tbl"

    def setUp(self):
        super().setUp()
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE TABLE public."{self.TBL}" (x int)')

    def tearDown(self):
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS public."{self.TBL}"')
        super().tearDown()

    def test_add_rename_drop_column(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        page.locator(f'button[hx-get$="table={self.TBL}"]').click()
        page.locator("#detail h2").wait_for()

        # Add a column via the drawer.
        page.get_by_role("button", name="+ Add column").first.click()
        add = page.locator('form[hx-post*="columns/add"]')
        add.wait_for(state="visible")
        add.locator("input[name=name]").fill("note")
        add.locator("select[name=type]").select_option("text")
        add.get_by_role("button", name="+ Add column").click()
        row = page.locator("#detail tbody tr", has_text="note")
        expect(row).to_be_visible()

        # Rename it via the edit drawer.
        row.get_by_role("button", name="edit").click()
        rename = page.locator('form[hx-post*="columns/rename"]')
        rename.locator("input[name=new_name]").fill("memo")
        rename.get_by_role("button", name="Rename").click()
        memo = page.locator("#detail tbody tr", has_text="memo")
        expect(memo).to_be_visible()

        # Drop it from the grid (hx-confirm → window.confirm, auto-accepted).
        page.on("dialog", lambda d: d.accept())
        memo.get_by_role("button", name="drop").click()
        expect(page.locator("#detail tbody tr", has_text="memo")).to_have_count(0)


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class ColumnAlterSmokeE2E(_BrowserE2E):
    """Open a table, open a column's edit drawer, change its type and flip its
    nullability — checking the Columns grid reflects each ALTER."""

    TBL = "cli2ui_e2e_alter_tbl"

    def setUp(self):
        super().setUp()
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'CREATE TABLE public."{self.TBL}" (amount text)')

    def tearDown(self):
        with get_engine(self.conn)._connect() as conn, conn.cursor() as cur:
            cur.execute(f'DROP TABLE IF EXISTS public."{self.TBL}"')
        super().tearDown()

    def _amount_row(self):
        return self.page.locator("#detail tbody tr", has_text="amount")

    def test_change_type_then_set_not_null(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        page.locator(f'button[hx-get$="table={self.TBL}"]').click()
        page.locator("#detail h2").wait_for()

        # Open the edit drawer and change text → integer.
        self._amount_row().get_by_role("button", name="edit").click()
        retype = page.locator('form[hx-post*="columns/type"]')
        retype.locator("select[name=type]").select_option("integer")
        retype.get_by_role("button", name="Change").click()
        # Type cell (2nd column) reflects the change.
        expect(self._amount_row().locator("td").nth(1)).to_have_text("integer")

        # Re-open the edit drawer and add a NOT NULL constraint.
        self._amount_row().get_by_role("button", name="edit").click()
        setnull = page.locator('form[hx-post*="columns/null"]')
        setnull.get_by_role("button", name="set NOT NULL").click()
        # Nullable cell (3rd column) flips to "no".
        expect(self._amount_row().locator("td").nth(2)).to_have_text("no")


@unittest.skipUnless(_HAS_PLAYWRIGHT and _sampledb_reachable(),
                     "needs playwright + chromium and a reachable sample DB")
class HealthSmokeE2E(_BrowserE2E):
    """The health panel renders its two cards against the sample DB."""

    def test_health_panel_shows_sizes(self):
        page = self.page
        page.goto(f"{self.live_server_url}/c/{self.conn.pk}/")
        self.open_section("Health")
        detail = page.locator("#detail")
        expect(detail).to_contain_text("Table sizes")
        expect(detail).to_contain_text("Unused indexes")
        expect(detail).to_contain_text("Dead rows")
        expect(detail).to_contain_text("public.orders")
