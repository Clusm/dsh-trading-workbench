"""Offline strategy authorization/version/state-machine regression tests."""
import copy
import sqlite3
import tempfile
import unittest
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from workbench_service.errors import ServiceError
from workbench_service.strategies import StrategyStore


RULES = {
    "symbols": ["BTCUSDT"], "timeframes": ["15m"],
    "entry": {"kind": "mark_threshold", "side": "long", "operator": "gt", "value": 90000},
    "exit": {"stop_loss_pct": 0.01, "take_profit_pct": 0.028},
    "invalidation": {"kind": "stop_loss"}, "position_constraints": {"max_positions": 1},
    "required_data": ["mark_price"], "requires_ai": False,
}


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workbench-v1.sqlite"
        self.store = StrategyStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self, rules=None):
        s = self.store.create("测试策略")
        return self.store.save_draft(s["strategy_id"], {"revision": 0, "source_text": "突破后交易", "rules": copy.deepcopy(rules or RULES)})

    def mount(self, s, request="req"):
        return self.store.mount(s["strategy_id"], {"revision": s["draft"]["revision"], "content_hash": s["draft"]["content_hash"], "request_id": request, "confirmed": True})

    def test_revision_conflict_does_not_overwrite_and_restart_recovers(self):
        s = self.draft()
        with self.assertRaises(ServiceError):
            self.store.save_draft(s["strategy_id"], {"revision": 0, "source_text": "旧分析"})
        self.assertEqual(StrategyStore(self.path).get(s["strategy_id"])["draft"], s["draft"])

    def test_mount_idempotency_and_immutable_version(self):
        s = self.draft()
        first, second = self.mount(s), self.mount(s)
        self.assertEqual(first["version"], second["version"])
        self.assertTrue(second["replayed"])
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE strategy_versions SET content_hash='tampered'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM strategy_versions")

    def test_unanswered_question_blocks_mount(self):
        s = self.draft()
        s = self.store.save_draft(s["strategy_id"], {**s["draft"], "questions": [{"id": "stop", "question": "确认止损？"}]})
        with self.assertRaises(ServiceError) as err:
            self.mount(s)
        self.assertEqual(err.exception.code, "E_STRATEGY_INCOMPLETE")

    def test_rename_draft_and_disable_preserve_historical_management(self):
        s = self.mount(self.draft())["strategy"]
        sid = s["strategy_id"]
        enabled = self.store.authorize(sid, enabled=True, generation=s["generation"])
        renamed = self.store.rename(sid, "独立名称")
        self.assertEqual(renamed["generation"], enabled["generation"])
        newer = self.store.save_draft(sid, {"revision": 1, "source_text": "未确认的新内容"})
        self.assertEqual(newer["current_version"], 1)
        self.store.validate_authorization(sid, 1, enabled["generation"])
        self.store.authorize(sid, enabled=False, generation=enabled["generation"])
        with self.assertRaises(ServiceError):
            self.store.validate_authorization(sid, 1, enabled["generation"])
        self.assertTrue(self.store.validate_authorization(sid, 1, enabled["generation"], purpose="manage", position_strategy_id=sid, position_strategy_version=1)["authorized"])
        with self.assertRaises(ServiceError):
            self.store.validate_authorization(sid, 1, 0, purpose="manage", position_strategy_id="other", position_strategy_version=1)

    def test_enabled_limit_atomic_across_store_instances(self):
        mounted = [self.mount(self.draft(), f"r{i}")["strategy"] for i in range(6)]
        def enable(s):
            try:
                StrategyStore(self.path).authorize(s["strategy_id"], enabled=True, generation=s["generation"])
                return True
            except ServiceError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(enable, mounted))
        self.assertEqual(results.count(True), 5)
        self.assertIn("E_STRATEGY_ENABLED_LIMIT", results)

    def test_unsupported_may_mount_but_never_enable(self):
        rules = {**RULES, "entry": "趋势健康时入场", "requires_ai": True}
        s = self.mount(self.draft(rules))["strategy"]
        self.assertFalse(s["confirmed_version"]["validation"]["execution_ready"])
        with self.assertRaises(ServiceError) as err:
            self.store.authorize(s["strategy_id"], enabled=True, generation=s["generation"])
        self.assertEqual(err.exception.code, "E_STRATEGY_UNSUPPORTED")

    def test_extra_conditions_never_silently_disappear_from_execution(self):
        rules = {**RULES, "cooldown_after_loss": "等待一小时"}
        s = self.mount(self.draft(rules))["strategy"]
        self.assertFalse(s["confirmed_version"]["validation"]["execution_ready"])
        with self.assertRaises(ServiceError):
            self.store.authorize(s["strategy_id"], enabled=True, generation=s["generation"])

    def test_mode_cas_invalidates_prior_generation(self):
        s = self.mount(self.draft())["strategy"]
        s = self.store.authorize(s["strategy_id"], enabled=True, generation=s["generation"])
        self.assertEqual(self.store.set_config("mounted", 0)["revision"], 1)
        with self.assertRaises(ServiceError):
            self.store.validate_authorization(s["strategy_id"], 1, s["generation"])
        with self.assertRaises(ServiceError):
            self.store.set_config("native", 0)

    def test_schema_backup_contains_existing_data_and_repeated_init_safe(self):
        path = Path(self.tmp.name) / "old.sqlite"
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE evidence(value TEXT)")
            conn.execute("INSERT INTO evidence VALUES ('preserved')")
            conn.commit()
        StrategyStore(path)
        StrategyStore(path)
        backups = list((path.parent / "backups").glob("before-strategies-*.sqlite"))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as conn:
            self.assertEqual(conn.execute("SELECT value FROM evidence").fetchone()[0], "preserved")


class StrategyExecutionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from support import ServiceCase
        self.case = ServiceCase(with_fake=True)
        self.client = self.case.client().__enter__()
        self.store = self.case.service.strategies
        s = self.store.create("确定性策略")
        s = self.store.save_draft(s["strategy_id"], {"revision": 0, "source_text": "价格高于90000，止损1%，止盈2.8%", "rules": RULES})
        s = self.store.mount(s["strategy_id"], {"revision": 1, "content_hash": s["draft"]["content_hash"], "request_id": "initial", "confirmed": True})["strategy"]
        self.store.set_config("mounted", 0)
        s = self.store.get(s["strategy_id"])
        s = self.store.authorize(s["strategy_id"], enabled=True, generation=s["generation"])
        self.context = {"strategy_id": s["strategy_id"], "version": 1, "generation": s["generation"], "mode_revision": 1, "signal_key": f"15m:{int(self.case.clock()*1000)//900000}"}

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.case.close()

    def evaluate(self, context=None, cycle="strategy-cycle", plan_override=None):
        from support import PLAN, seed_evidence
        seed_evidence(self.case.service, cycle, age_ms=1000)
        body = {"cycle_id": cycle, "plan": {**PLAN, **(plan_override or {})}, "strategy_context": self.context if context is None else context}
        return self.client.post("/api/risk/evaluate", json=body)

    def test_mounted_without_context_blocks_native_fallback(self):
        from support import evaluate
        response = evaluate(self.client)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_STRATEGY_CONTEXT_REQUIRED")

    def test_p1_compiles_exit_and_p2_records_immutable_attribution(self):
        approved = self.evaluate(plan_override={"sl": "98000", "tp": "105000"})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["plan"]["sl"], "99000")
        result = self.client.post("/api/execution/order", json={"approval_token": approved.json()["approval"]["token"], "strategy_context": self.context})
        self.assertIn(result.status_code, (200, 201), result.text)
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_signals").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT strategy_id,version FROM strategy_position_ownership").fetchone()[0], self.context["strategy_id"])

    def test_strategy_deadline_restores_original_intent_and_never_adopts_external(self):
        from workbench_service.strategies.execution import restore_position_metadata
        approved = self.evaluate()
        self.assertEqual(approved.status_code, 200, approved.text)
        result = self.client.post("/api/execution/order", json={"approval_token": approved.json()["approval"]["token"], "strategy_context": self.context})
        self.assertIn(result.status_code, (200, 201), result.text)
        with self.store.transaction(write=True) as conn:
            original = dict(conn.execute("SELECT * FROM positions").fetchone())
            self.assertIsNone(original["deadline_at"])
            self.assertIn(";strategy:", original["time_stop_source"])
            conn.execute("UPDATE positions SET opened_at=NULL,deadline_at=NULL,time_stop_source=NULL,origin='EXTERNAL'")
            conn.execute("DELETE FROM strategy_position_ownership")
        restore_position_metadata(self.store)
        with self.store.transaction(write=True) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_position_ownership").fetchone()[0], 0)
            conn.execute("UPDATE positions SET origin='OPENED_BY_WORKBENCH'")
        restore_position_metadata(self.store)
        with self.store.transaction() as conn:
            restored = dict(conn.execute("SELECT * FROM positions").fetchone())
            self.assertEqual(restored["opened_at"], original["opened_at"])
            self.assertEqual(restored["time_stop_source"], original["time_stop_source"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_position_ownership").fetchone()[0], 1)

    def test_time_stop_comes_from_confirmed_version(self):
        from support import PLAN
        from workbench_service.risk.models import OrderPlan
        plan = OrderPlan.from_payload(PLAN, cycle_id="deadline")
        from dataclasses import replace
        self.case.service.settings = replace(self.case.service.settings, auto_time_stop_hours=8)
        request = self.case.service._time_stop_request(plan, strategy_context=self.context)
        self.assertEqual(request["hours"], 0)
        self.assertIsNone(request["deadline_at"])
        sid = self.context["strategy_id"]
        draft = self.store.get(sid)["draft"]
        rules = copy.deepcopy(RULES)
        rules["exit"]["time_stop_hours"] = 2
        updated = self.store.save_draft(sid, {"revision": draft["revision"], "source_text": "两小时退出", "rules": rules})
        self.store.mount(sid, {"revision": updated["draft"]["revision"], "content_hash": updated["draft"]["content_hash"], "request_id": "deadline-v2", "confirmed": True})
        request = self.case.service._time_stop_request(plan, strategy_context={**self.context, "version": 2})
        self.assertEqual(request["hours"], 2)
        from datetime import datetime
        self.assertEqual((datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00")) - datetime.fromisoformat(request["opened_at"].replace("Z", "+00:00"))).total_seconds(), 7200)

    def test_p2_rejects_disabled_or_tampered_context(self):
        approved = self.evaluate()
        self.assertEqual(approved.status_code, 200, approved.text)
        self.store.authorize(self.context["strategy_id"], enabled=False, generation=self.context["generation"])
        result = self.client.post("/api/execution/order", json={"approval_token": approved.json()["approval"]["token"], "strategy_context": self.context})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_NOT_AUTHORIZED")
        self.assertEqual(len(self.case.fake.orders), 0)

    def test_wrong_side_stale_signal_and_forged_manage_are_rejected(self):
        result = self.evaluate(plan_override={"side": "SHORT"})
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_PLAN_MISMATCH")
        result = self.evaluate({**self.context, "signal_key": "arbitrary"})
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_SIGNAL_EXPIRED")
        result = self.client.post("/api/strategies/validate-authorization", json={"strategy_id": self.context["strategy_id"], "version": 1, "generation": self.context["generation"], "purpose": "manage"})
        self.assertEqual(result.status_code, 409)

    def test_mode_change_invalidates_native_approval(self):
        from support import approve
        self.store.set_config("native", 1)
        approved = approve(self.client)
        self.store.set_config("mounted", 2)
        result = self.client.post("/api/execution/order", json={"approval_token": approved["approval"]["token"]})
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_MODE_CHANGED")


if __name__ == "__main__":
    unittest.main()
