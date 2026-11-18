"""Offline strategy authorization, Phase-2 evaluator, attribution, circuit-breaker & P1/P2 tests."""
import copy
import sqlite3
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workbench_service.errors import ServiceError
from workbench_service.market.config import MarketSettings, WHITELIST
from workbench_service.market.klines import Bar
from workbench_service.market.service import MarketService
from workbench_service.market.transport import MarketUpstreamError
from workbench_service.strategies import StrategyStore
from workbench_service.strategies.attribution import check_and_trip_circuits, list_attribution
from workbench_service.strategies.evaluator import describe_rules, evaluate_entry
from workbench_service.strategies.validation import validate


RULES = {
    "symbols": ["BTCUSDT"],
    "timeframes": ["15m"],
    "entry": {"kind": "mark_threshold", "side": "long", "operator": "gt", "value": 90000},
    "exit": {"stop_loss_pct": 0.01, "take_profit_pct": 0.028},
    "invalidation": {"kind": "stop_loss"},
    "position_constraints": {"max_positions": 1},
    "required_data": ["mark_price"],
    "requires_ai": False,
}


def _make_bars(closes: list[float], *, end_open_ms: int, step_ms: int = 900_000) -> list[Bar]:
    count = len(closes)
    first_open = end_open_ms - (count - 1) * step_ms
    bars: list[Bar] = []
    for idx, c in enumerate(closes):
        open_ms = first_open + idx * step_ms
        bars.append(
            Bar(
                open_time_ms=open_ms,
                close_time_ms=open_ms + step_ms - 1,
                open=c,
                high=c * 1.005,
                low=c * 0.995,
                close=c,
                volume=100.0,
                quote_volume=100.0 * c,
                trades=10,
                taker_buy_base=50.0,
                taker_buy_quote=50.0 * c,
                final=True,
                source="test",
            )
        )
    return bars


class _FakeMarketTransport:
    def __init__(self, *, mark: float = 100_000.0, funding: float = 0.0003, fail: bool = False):
        self.mark = mark
        self.funding = funding
        self.fail = fail
        self.kline_closes: list[float] = [99_000.0 + i * 50.0 for i in range(60)]

    def get_json(self, path, params=None, **kwargs):
        if self.fail:
            raise MarketUpstreamError(503, "UNAVAILABLE", "market offline")
        now_ms = int(time.time() * 1000)
        if path == "/fapi/v1/time":
            return {"serverTime": now_ms}
        if path == "/fapi/v1/premiumIndex":
            if not params:
                return [
                    {
                        "symbol": sym,
                        "markPrice": str(self.mark),
                        "indexPrice": str(self.mark),
                        "lastFundingRate": str(self.funding),
                        "time": now_ms - 200,
                    }
                    for sym in WHITELIST
                ]
            sym = str(params.get("symbol") or "BTCUSDT").upper()
            return {
                "symbol": sym,
                "markPrice": str(self.mark),
                "indexPrice": str(self.mark),
                "lastFundingRate": str(self.funding),
                "time": now_ms - 200,
            }
        if path == "/fapi/v1/klines":
            step = 900_000
            cur_bucket = (now_ms // step) * step
            closes = self.kline_closes
            first = cur_bucket - len(closes) * step
            rows = []
            for idx, c in enumerate(closes):
                o_ms = first + idx * step
                rows.append(
                    [
                        o_ms,
                        str(c),
                        str(c * 1.005),
                        str(c * 0.995),
                        str(c),
                        "100",
                        o_ms + step - 1,
                        str(c * 100),
                        10,
                        "50",
                        str(c * 50),
                        "0",
                    ]
                )
            return rows
        return {}

    def close(self):
        return None


class StrategyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "workbench-v1.sqlite"
        self.store = StrategyStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def draft(self, rules=None):
        s = self.store.create("测试策略")
        return self.store.save_draft(
            s["strategy_id"],
            {"revision": 0, "source_text": "突破后交易", "rules": copy.deepcopy(rules or RULES)},
        )

    def mount(self, s, request="req"):
        return self.store.mount(
            s["strategy_id"],
            {
                "revision": s["draft"]["revision"],
                "content_hash": s["draft"]["content_hash"],
                "request_id": request,
                "confirmed": True,
            },
        )

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
        s = self.store.save_draft(
            s["strategy_id"], {**s["draft"], "questions": [{"id": "stop", "question": "确认止损？"}]}
        )
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
        self.assertTrue(
            self.store.validate_authorization(
                sid,
                1,
                enabled["generation"],
                purpose="manage",
                position_strategy_id=sid,
                position_strategy_version=1,
            )["authorized"]
        )
        with self.assertRaises(ServiceError):
            self.store.validate_authorization(
                sid, 1, 0, purpose="manage", position_strategy_id="other", position_strategy_version=1
            )

    def test_enabled_limit_atomic_across_store_instances(self):
        mounted = [self.mount(self.draft(), f"r{i}")["strategy"] for i in range(6)]

        def enable(s):
            try:
                StrategyStore(self.path).authorize(
                    s["strategy_id"], enabled=True, generation=s["generation"]
                )
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

    def test_whitelist_20_symbols_and_r09_r10_exit_validation(self):
        # DOGEUSDT & SUIUSDT in 20-symbol whitelist -> valid & execution_ready
        good = validate(
            {
                "rules": {
                    **RULES,
                    "symbols": ["DOGEUSDT", "SUIUSDT", "LINKUSDT"],
                    "exit": {"stop_loss_pct": 0.02, "take_profit_pct": 0.024},
                    "execution_mode": "rule_direct",
                    "circuit_breaker": {
                        "max_consecutive_losses": 3,
                        "max_cumulative_loss_usdt": 150.0,
                    },
                },
                "questions": [],
                "answers": {},
            }
        )
        self.assertTrue(good["valid"], good)
        self.assertTrue(good["execution_ready"], good)

        # Unlisted symbol PEPEUSDT -> valid=False in issues
        unlisted = validate(
            {"rules": {**RULES, "symbols": ["BTCUSDT", "PEPEUSDT"]}, "questions": [], "answers": {}}
        )
        self.assertFalse(unlisted["valid"])
        self.assertTrue(any("PEPEUSDT" in msg for msg in unlisted["issues"]))

        # R09 SL out of [0.002, 0.05] -> valid=False
        tight_sl = validate(
            {
                "rules": {**RULES, "exit": {"stop_loss_pct": 0.001, "take_profit_pct": 0.02}},
                "questions": [],
                "answers": {},
            }
        )
        self.assertFalse(tight_sl["valid"])
        wide_sl = validate(
            {
                "rules": {**RULES, "exit": {"stop_loss_pct": 0.06, "take_profit_pct": 0.12}},
                "questions": [],
                "answers": {},
            }
        )
        self.assertFalse(wide_sl["valid"])

        # R10 RR < 1.2 -> valid=False, blocks mount
        low_rr_draft = self.draft({**RULES, "exit": {"stop_loss_pct": 0.02, "take_profit_pct": 0.02}})
        self.assertFalse(low_rr_draft["draft"]["validation"]["valid"])
        with self.assertRaises(ServiceError) as err:
            self.mount(low_rr_draft, "bad-rr")
        self.assertEqual(err.exception.code, "E_STRATEGY_INCOMPLETE")

    def test_mount_downgrades_enabled_when_symbol_budget_exceeded(self):
        # Strategy A enabled with 8 symbols
        s1 = self.mount(self.draft({**RULES, "symbols": list(WHITELIST[:8])}), "m1")["strategy"]
        self.store.authorize(s1["strategy_id"], enabled=True, generation=s1["generation"])
        # Strategy B enabled with 2 symbols (total = 10)
        s2 = self.mount(self.draft({**RULES, "symbols": list(WHITELIST[8:10])}), "m2")["strategy"]
        s2 = self.store.authorize(s2["strategy_id"], enabled=True, generation=s2["generation"])
        self.assertTrue(s2["enabled"])

        # Update Strategy B draft to 4 symbols (8 + 4 = 12 > 10) and mount:
        s2_draft = self.store.save_draft(
            s2["strategy_id"],
            {
                "revision": s2["draft"]["revision"],
                "source_text": "扩充监控池",
                "rules": {**RULES, "symbols": list(WHITELIST[8:12])},
            },
        )
        mounted = self.mount(s2_draft, "m2-v2")
        self.assertTrue(mounted["budget_downgraded"])
        self.assertFalse(mounted["strategy"]["enabled"])
        self.assertEqual(mounted["strategy"]["current_version"], 2)


class Phase2EvaluatorUnitTests(unittest.TestCase):
    def test_phase2_primitives_validation_and_deterministic_evaluation(self):
        now_ms = 1_800_000_100_000
        step = 900_000
        last_open_ms = (now_ms // step - 1) * step  # latest closed 15m bar

        # 1) candle_close
        closes = [100.0 + i for i in range(30)]  # last close = 129.0
        bars = _make_bars(closes, end_open_ms=last_open_ms)
        r_cc = {
            **RULES,
            "entry": {"kind": "candle_close", "side": "long", "operator": "gte", "value": 129.0},
            "required_data": ["klines"],
        }
        self.assertTrue(validate({"rules": r_cc, "questions": [], "answers": {}})["execution_ready"])
        res_cc = evaluate_entry(r_cc, "BTCUSDT", {"mark": 129.5, "stale": False}, bars=bars, now_ms=now_ms)
        self.assertTrue(res_cc["matched"])
        self.assertEqual(res_cc["signal_key"], f"15m:{last_open_ms // step}")

        # 2) ema_cross (bullish cross on latest bar: flat 100s then sharp jump on final bar)
        cross_closes = [100.0] * 25 + [125.0]
        cross_bars = _make_bars(cross_closes, end_open_ms=last_open_ms)
        r_cross = {
            **RULES,
            "entry": {"kind": "ema_cross", "side": "long", "fast": 5, "slow": 20, "direction": "bullish"},
            "required_data": ["mark_price", "klines"],
        }
        self.assertTrue(validate({"rules": r_cross, "questions": [], "answers": {}})["execution_ready"])
        res_cross = evaluate_entry(
            r_cross, "BTCUSDT", {"mark": 125.0, "stale": False}, bars=cross_bars, now_ms=now_ms
        )
        self.assertTrue(res_cross["matched"], res_cross)
        self.assertGreater(res_cross["facts"]["ema_fast"], res_cross["facts"]["ema_slow"])
        self.assertLessEqual(res_cross["facts"]["prev_ema_fast"], res_cross["facts"]["prev_ema_slow"])

        # Insufficient bars check
        short_res = evaluate_entry(
            r_cross, "BTCUSDT", {"mark": 125.0, "stale": False}, bars=cross_bars[-5:], now_ms=now_ms
        )
        self.assertFalse(short_res["matched"])
        self.assertEqual(short_res["reason_code"], "INSUFFICIENT_BARS")

        # Stale klines check
        stale_bars = _make_bars(cross_closes, end_open_ms=last_open_ms - 10 * step)
        stale_res = evaluate_entry(
            r_cross, "BTCUSDT", {"mark": 125.0, "stale": False}, bars=stale_bars, now_ms=now_ms
        )
        self.assertFalse(stale_res["matched"])
        self.assertEqual(stale_res["reason_code"], "KLINE_STALE")

        # 3) ema_compare (fast=1 means close vs EMA(slow))
        r_cmp = {
            **RULES,
            "entry": {"kind": "ema_compare", "side": "long", "fast": 1, "slow": 10, "operator": "gt"},
            "required_data": ["klines"],
        }
        self.assertTrue(validate({"rules": r_cmp, "questions": [], "answers": {}})["execution_ready"])
        res_cmp = evaluate_entry(r_cmp, "BTCUSDT", {"mark": 130.0, "stale": False}, bars=bars, now_ms=now_ms)
        self.assertTrue(res_cmp["matched"])

        # 4) rsi_threshold + 5) funding_threshold + 6) all composite
        r_all = {
            **RULES,
            "entry": {
                "kind": "all",
                "side": "long",
                "conditions": [
                    {"kind": "rsi_threshold", "period": 14, "operator": "gt", "value": 70},
                    {"kind": "ema_compare", "fast": 5, "slow": 20, "operator": "gt"},
                    {"kind": "funding_threshold", "operator": "lte", "value": 0.001},
                ],
            },
            "required_data": ["mark_price", "klines", "funding_rate"],
        }
        self.assertTrue(validate({"rules": r_all, "questions": [], "answers": {}})["execution_ready"])
        res_all = evaluate_entry(
            r_all,
            "BTCUSDT",
            {"mark": 130.0, "stale": False, "funding_rate": 0.0002},
            bars=bars,
            now_ms=now_ms,
        )
        self.assertTrue(res_all["matched"], res_all)
        self.assertEqual(res_all["reason_code"], "OK")
        summary = describe_rules(r_all)
        self.assertIn("RSI(14)", summary["zh"])
        self.assertIn("RSI(14)", summary["en"])

        # Missing funding_rate -> FUNDING_UNAVAILABLE
        no_fund = evaluate_entry(
            r_all, "BTCUSDT", {"mark": 130.0, "stale": False}, bars=bars, now_ms=now_ms
        )
        self.assertFalse(no_fund["matched"])
        self.assertEqual(no_fund["reason_code"], "FUNDING_UNAVAILABLE")


class StrategyExecutionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).parent))
        from support import ServiceCase

        self.clock_val = [1_800_000_450.0]  # 450s into a 15m bucket (900s)
        self.case = ServiceCase(with_fake=True, clock=lambda: self.clock_val[0])
        self.fake_market_transport = _FakeMarketTransport(mark=100_000.0, funding=0.0002)
        self.market_service = MarketService(
            MarketSettings(
                base_url="https://demo-fapi.binance.com",
                data_dir=self.case.data_dir,
            ),
            transport=self.fake_market_transport,
        )
        self.case.app.state.market = self.market_service
        self.case.service.market_service = self.market_service

        self.client = self.case.client().__enter__()
        self.store = self.case.service.strategies
        s = self.store.create("确定性策略")
        s = self.store.save_draft(
            s["strategy_id"],
            {"revision": 0, "source_text": "价格高于90000，止损1%，止盈2.8%", "rules": RULES},
        )
        s = self.store.mount(
            s["strategy_id"],
            {
                "revision": 1,
                "content_hash": s["draft"]["content_hash"],
                "request_id": "initial",
                "confirmed": True,
            },
        )["strategy"]
        self.store.set_config("mounted", 0)
        s = self.store.get(s["strategy_id"])
        s = self.store.authorize(s["strategy_id"], enabled=True, generation=s["generation"])
        self.context = {
            "strategy_id": s["strategy_id"],
            "version": 1,
            "generation": s["generation"],
            "mode_revision": 1,
            "signal_key": f"15m:{int(self.case.clock() * 1000) // 900000}",
        }

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.case.close()

    def evaluate(self, context=None, cycle="strategy-cycle", plan_override=None):
        from support import PLAN, seed_evidence

        seed_evidence(self.case.service, cycle, age_ms=1000)
        body = {
            "cycle_id": cycle,
            "plan": {**PLAN, **(plan_override or {})},
            "strategy_context": self.context if context is None else context,
        }
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
        result = self.client.post(
            "/api/execution/order",
            json={
                "approval_token": approved.json()["approval"]["token"],
                "strategy_context": self.context,
            },
        )
        self.assertIn(result.status_code, (200, 201), result.text)
        with self.store.transaction() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM strategy_signals").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT strategy_id,version FROM strategy_position_ownership").fetchone()[0],
                self.context["strategy_id"],
            )
            self.assertEqual(
                conn.execute("SELECT outcome FROM strategy_decisions").fetchone()[0],
                "executed",
            )

    def test_strategy_deadline_restores_original_intent_and_never_adopts_external(self):
        from workbench_service.strategies.execution import restore_position_metadata

        approved = self.evaluate()
        self.assertEqual(approved.status_code, 200, approved.text)
        result = self.client.post(
            "/api/execution/order",
            json={
                "approval_token": approved.json()["approval"]["token"],
                "strategy_context": self.context,
            },
        )
        self.assertIn(result.status_code, (200, 201), result.text)
        with self.store.transaction(write=True) as conn:
            original = dict(conn.execute("SELECT * FROM positions").fetchone())
            self.assertIsNone(original["deadline_at"])
            self.assertIn(";strategy:", original["time_stop_source"])
            conn.execute(
                "UPDATE positions SET opened_at=NULL,deadline_at=NULL,time_stop_source=NULL,origin='EXTERNAL'"
            )
            conn.execute("DELETE FROM strategy_position_ownership")
        restore_position_metadata(self.store)
        with self.store.transaction(write=True) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM strategy_position_ownership").fetchone()[0], 0
            )
            conn.execute("UPDATE positions SET origin='OPENED_BY_WORKBENCH'")
        restore_position_metadata(self.store)
        with self.store.transaction() as conn:
            restored = dict(conn.execute("SELECT * FROM positions").fetchone())
            self.assertEqual(restored["opened_at"], original["opened_at"])
            self.assertEqual(restored["time_stop_source"], original["time_stop_source"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM strategy_position_ownership").fetchone()[0], 1
            )

    def test_time_stop_comes_from_confirmed_version(self):
        from dataclasses import replace
        from datetime import datetime
        from support import PLAN
        from workbench_service.risk.models import OrderPlan

        plan = OrderPlan.from_payload(PLAN, cycle_id="deadline")
        self.case.service.settings = replace(self.case.service.settings, auto_time_stop_hours=8)
        request = self.case.service._time_stop_request(plan, strategy_context=self.context)
        self.assertEqual(request["hours"], 0)
        self.assertIsNone(request["deadline_at"])
        sid = self.context["strategy_id"]
        draft = self.store.get(sid)["draft"]
        rules = copy.deepcopy(RULES)
        rules["exit"]["time_stop_hours"] = 2
        updated = self.store.save_draft(
            sid, {"revision": draft["revision"], "source_text": "两小时退出", "rules": rules}
        )
        self.store.mount(
            sid,
            {
                "revision": updated["draft"]["revision"],
                "content_hash": updated["draft"]["content_hash"],
                "request_id": "deadline-v2",
                "confirmed": True,
            },
        )
        request = self.case.service._time_stop_request(
            plan, strategy_context={**self.context, "version": 2}
        )
        self.assertEqual(request["hours"], 2)
        self.assertEqual(
            (
                datetime.fromisoformat(request["deadline_at"].replace("Z", "+00:00"))
                - datetime.fromisoformat(request["opened_at"].replace("Z", "+00:00"))
            ).total_seconds(),
            7200,
        )

    def test_p2_rejects_disabled_or_tampered_context(self):
        approved = self.evaluate()
        self.assertEqual(approved.status_code, 200, approved.text)
        self.store.authorize(
            self.context["strategy_id"], enabled=False, generation=self.context["generation"]
        )
        result = self.client.post(
            "/api/execution/order",
            json={
                "approval_token": approved.json()["approval"]["token"],
                "strategy_context": self.context,
            },
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_NOT_AUTHORIZED")
        self.assertEqual(len(self.case.fake.orders), 0)

    def test_wrong_side_stale_signal_and_forged_manage_are_rejected(self):
        result = self.evaluate(plan_override={"side": "SHORT"})
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_PLAN_MISMATCH")
        result = self.evaluate({**self.context, "signal_key": "arbitrary"})
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_SIGNAL_EXPIRED")
        result = self.client.post(
            "/api/strategies/validate-authorization",
            json={
                "strategy_id": self.context["strategy_id"],
                "version": 1,
                "generation": self.context["generation"],
                "purpose": "manage",
            },
        )
        self.assertEqual(result.status_code, 409)

    def test_mode_change_invalidates_native_approval(self):
        from support import approve

        self.store.set_config("native", 1)
        approved = approve(self.client)
        self.store.set_config("mounted", 2)
        result = self.client.post(
            "/api/execution/order", json={"approval_token": approved["approval"]["token"]}
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "E_STRATEGY_MODE_CHANGED")

    def test_p1_deduplicates_consumed_signal_and_allows_15m_boundary_rollover(self):
        # Set clock 390s into new 15m bucket; candidate evaluated 400s ago in previous bucket:
        bucket = int(1_800_000_900_000 // 900_000)  # exact 15m boundary
        prev_key = f"15m:{bucket - 1}"
        self.clock_val[0] = (bucket * 900) + 390.0  # 390s into current bucket (within 400s window)
        ctx = {**self.context, "signal_key": prev_key}

        approved = self.evaluate(context=ctx, cycle="rollover-cyc-1")
        self.assertEqual(approved.status_code, 200, approved.text)

        # Advance 12s across the 400s mark (within 15s P2 snapshot TTL, now 402s into bucket!):
        # P2 must NOT fail on wall-clock rollover because P1 token bound prev_key.
        self.clock_val[0] += 12.0
        submitted = self.client.post(
            "/api/execution/order",
            json={
                "approval_token": approved.json()["approval"]["token"],
                "strategy_context": ctx,
            },
        )
        self.assertIn(submitted.status_code, (200, 201), submitted.text)

        # Close position so R02/position limit does not mask P1 deduplication:
        closed = self.client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
        self.assertEqual(closed.status_code, 200, closed.text)

        # Re-submitting same signal_key at P1 must fail fast with 409 E_STRATEGY_SIGNAL_DUPLICATE:
        self.clock_val[0] = (bucket * 900) + 100.0
        dup = self.evaluate(context=ctx, cycle="rollover-cyc-2")
        self.assertEqual(dup.status_code, 409, dup.text)
        self.assertEqual(dup.json()["error"]["code"], "E_STRATEGY_SIGNAL_DUPLICATE")

    def test_p2_records_definite_rejection_in_strategy_signals(self):
        approved = self.evaluate(cycle="rej-cyc-1")
        self.assertEqual(approved.status_code, 200, approved.text)
        self.case.fake.reject_orders = 1
        rejected = self.client.post(
            "/api/execution/order",
            json={
                "approval_token": approved.json()["approval"]["token"],
                "strategy_context": self.context,
            },
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        with self.store.transaction() as conn:
            sig = dict(
                conn.execute(
                    "SELECT state, result_json FROM strategy_signals WHERE cycle_id='rej-cyc-1'"
                ).fetchone()
            )
        self.assertEqual(sig["state"], "rejected")
        self.assertIsNotNone(sig["result_json"])
        self.assertIn("E_EXCHANGE_REJECT", sig["result_json"])

    def test_circuit_breaker_auto_trips_on_consecutive_losses_and_resets_on_reenable(self):
        sid = self.context["strategy_id"]
        cur = self.store.get(sid)
        cb_rules = copy.deepcopy(RULES)
        cb_rules["circuit_breaker"] = {
            "max_consecutive_losses": 2,
            "max_cumulative_loss_usdt": 500,
        }
        updated = self.store.save_draft(
            sid,
            {
                "revision": cur["draft"]["revision"],
                "source_text": "连亏2笔自动熔断",
                "rules": cb_rules,
            },
        )
        mounted = self.store.mount(
            sid,
            {
                "revision": updated["draft"]["revision"],
                "content_hash": updated["draft"]["content_hash"],
                "request_id": "cb-v2",
                "confirmed": True,
            },
        )["strategy"]
        self.assertTrue(mounted["enabled"])

        # Execute 2 losing trades across 2 15m buckets:
        for i in range(2):
            self.clock_val[0] += 900.0
            self.case.fake.mark_price = Decimal("100000.0")
            self.case.fake.index_price = Decimal("100000.0")
            self.case.fake.bid_price = Decimal("99995.0")
            self.case.fake.ask_price = Decimal("100005.0")
            s_now = self.store.get(sid)
            ctx = {
                "strategy_id": sid,
                "version": s_now["current_version"],
                "generation": s_now["generation"],
                "mode_revision": self.store.config()["revision"],
                "signal_key": f"15m:{int(self.clock_val[0] * 1000) // 900000}",
            }
            p1 = self.evaluate(context=ctx, cycle=f"loss-cyc-{i}")
            self.assertEqual(p1.status_code, 200, p1.text)
            p2 = self.client.post(
                "/api/execution/order",
                json={"approval_token": p1.json()["approval"]["token"], "strategy_context": ctx},
            )
            self.assertIn(p2.status_code, (200, 201), p2.text)

            # Price drops 1000 USDT -> close at a loss!
            self.clock_val[0] += 5.0
            self.case.fake.mark_price = Decimal("99000.0")
            self.case.fake.index_price = Decimal("99000.0")
            self.case.fake.bid_price = Decimal("98995.0")
            self.case.fake.ask_price = Decimal("99005.0")
            closed = self.client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
            self.assertEqual(closed.status_code, 200, closed.text)

        # Strategy must now be auto-tripped & disabled!
        tripped = self.client.get(f"/api/strategies/{sid}").json()["strategy"]
        self.assertFalse(tripped["enabled"])
        self.assertEqual(tripped["circuit"]["state"], "tripped")
        self.assertEqual(tripped["stats"]["closed_trades"], 2)
        self.assertEqual(tripped["stats"]["losing_trades"], 2)
        self.assertEqual(tripped["stats"]["consecutive_losses"], 2)
        self.assertEqual(tripped["stats"]["win_rate"], 0.0)
        self.assertLess(Decimal(tripped["stats"]["realized_pnl_usdt"]), Decimal("0"))

        # Re-enabling resets circuit_state='ok' and resets consecutive_losses post-reset window!
        reenabled = self.client.post(
            f"/api/strategies/{sid}/authorization",
            json={"enabled": True, "generation": tripped["generation"]},
        )
        self.assertEqual(reenabled.status_code, 200, reenabled.text)
        body = reenabled.json()["strategy"]
        self.assertTrue(body["enabled"])
        self.assertEqual(body["circuit"]["state"], "ok")
        self.assertIsNotNone(body["circuit"]["reset_at"])
        self.assertEqual(body["stats"]["consecutive_losses"], 0)
        self.assertEqual(body["stats"]["closed_trades"], 2)  # lifetime history preserved!

        # Subsequent circuit check must NOT immediately re-trip before new post-reset losses:
        check_and_trip_circuits(self.store)
        self.assertTrue(self.store.get(sid)["enabled"])

    def test_evaluate_candidates_preview_veto_counterfactuals_and_health_capability(self):
        sid = self.context["strategy_id"]
        now_ms = int(self.clock_val[0] * 1000)

        # 1) /evaluate-candidates finds our enabled strategy
        eval_resp = self.client.post("/api/strategies/evaluate-candidates", json={"now_ms": now_ms})
        self.assertEqual(eval_resp.status_code, 200, eval_resp.text)
        payload = eval_resp.json()
        self.assertEqual(payload["mode"], "mounted")
        self.assertEqual(len(payload["candidates"]), 1)
        cand = payload["candidates"][0]
        self.assertEqual(cand["strategy_id"], sid)
        self.assertEqual(cand["symbol"], "BTCUSDT")
        self.assertEqual(cand["side"], "long")
        self.assertEqual(cand["execution_mode"], "ai_assisted")
        self.assertIn("BTCUSDT", payload["prices"])

        # 2) /preview returns live evaluation and graceful fallback when market is offline
        prev_ok = self.client.get(f"/api/strategies/{sid}/preview")
        self.assertEqual(prev_ok.status_code, 200, prev_ok.text)
        prev_json = prev_ok.json()
        self.assertTrue(prev_json["market_available"])
        self.assertTrue(prev_json["execution_ready"])
        self.assertTrue(prev_json["triggered_now"])
        self.assertEqual(prev_json["symbols"][0]["symbol"], "BTCUSDT")
        self.assertTrue(prev_json["symbols"][0]["matched"])

        self.fake_market_transport.fail = True
        self.market_service._marks.clear()
        prev_offline = self.client.get(f"/api/strategies/{sid}/preview")
        self.assertEqual(prev_offline.status_code, 200, prev_offline.text)
        self.assertFalse(prev_offline.json()["market_available"])
        self.fake_market_transport.fail = False
        self.market_service._mark_retry.clear()

        # 3) Record 3 AI veto decisions and verify counterfactual SL/TP outcomes via cached klines
        base_ms = 1_800_100_000_000
        # Seed cached 15m bars in market_service.klines._series:
        from workbench_service.market.klines import _Series

        bars = [
            # Bar 1 at base_ms + 900_000: drops to 98500 -> hits veto1 SL (99000) -> saved_sl!
            Bar(base_ms + 900_000, base_ms + 1_799_999, 100000, 100200, 98500, 98800, 10, 1000, 1, 5, 500, True, "test"),
            # Bar 2 at base_ms + 2_700_000: rallies to 103500 -> hits veto2 TP (102800) -> missed_tp!
            Bar(base_ms + 2_700_000, base_ms + 3_599_999, 100000, 103500, 99500, 103200, 10, 1000, 1, 5, 500, True, "test"),
        ]
        self.market_service.klines._series[("BTCUSDT", "15m")] = _Series(
            bars=bars, received_at=time.time(), live_bar_present=False
        )

        # Veto 1 at base_ms (before Bar 1 -> hits SL 99000 first)
        r1 = self.client.post(
            f"/api/strategies/{sid}/decisions",
            json={
                "cycle_id": "veto-cyc-1",
                "version": 1,
                "symbol": "BTCUSDT",
                "side": "long",
                "signal_key": "15m:2000111",
                "outcome": "ai_veto",
                "reason_code": "RED_TEAM_RISK",
                "entry_mark": "100000",
                "details": {"evaluated_at_ms": base_ms},
            },
        )
        self.assertEqual(r1.status_code, 200, r1.text)
        # Idempotent replay check:
        r1_dup = self.client.post(
            f"/api/strategies/{sid}/decisions",
            json={
                "cycle_id": "veto-cyc-1",
                "version": 1,
                "symbol": "BTCUSDT",
                "side": "long",
                "signal_key": "15m:2000111",
                "outcome": "ai_veto",
                "entry_mark": "100000",
                "details": {"evaluated_at_ms": base_ms},
            },
        )
        self.assertTrue(r1_dup.json()["decision"]["replayed"])

        # Veto 2 at base_ms + 2_000_000 (after Bar 1, before Bar 2 -> hits TP 102800)
        self.client.post(
            f"/api/strategies/{sid}/decisions",
            json={
                "cycle_id": "veto-cyc-2",
                "version": 1,
                "symbol": "BTCUSDT",
                "side": "long",
                "signal_key": "15m:2000113",
                "outcome": "ai_veto",
                "entry_mark": "100000",
                "details": {"evaluated_at_ms": base_ms + 2_000_000},
            },
        )
        # Veto 3 at base_ms + 5_000_000 (after Bar 2 -> no subsequent bars -> pending)
        self.client.post(
            f"/api/strategies/{sid}/decisions",
            json={
                "cycle_id": "veto-cyc-3",
                "version": 1,
                "symbol": "BTCUSDT",
                "side": "long",
                "signal_key": "15m:2000116",
                "outcome": "ai_veto",
                "entry_mark": "100000",
                "details": {"evaluated_at_ms": base_ms + 5_000_000},
            },
        )

        attr = self.client.get("/api/strategies/attribution")
        self.assertEqual(attr.status_code, 200, attr.text)
        row = next(item for item in attr.json()["strategies"] if item["strategy_id"] == sid)
        self.assertEqual(row["ai_veto_count"], 3)
        self.assertEqual(row["veto_saved_sl"], 1)
        self.assertEqual(row["veto_missed_tp"], 1)
        self.assertEqual(row["veto_pending"], 1)

        # 4) P2-11: /health capabilities.strategies reflects actual strategy_store availability
        self.assertTrue(self.client.get("/health").json()["capabilities"]["strategies"])
        saved_store = self.case.app.state.strategy_store
        try:
            self.case.app.state.strategy_store = None
            self.assertFalse(self.client.get("/health").json()["capabilities"]["strategies"])
            unavail = self.client.get("/api/strategies")
            self.assertEqual(unavail.status_code, 503)
            self.assertEqual(unavail.json()["error"]["code"], "E_DB_UNAVAILABLE")
        finally:
            self.case.app.state.strategy_store = saved_store

    def test_phase2_kline_strategy_end_to_end_p1_p2(self):
        # Create and enable a Phase-2 kline + funding composite strategy
        kline_rules = {
            **RULES,
            "entry": {
                "kind": "all",
                "side": "long",
                "conditions": [
                    {"kind": "ema_compare", "fast": 5, "slow": 20, "operator": "gt"},
                    {"kind": "rsi_threshold", "period": 14, "operator": "gt", "value": 60},
                    {"kind": "funding_threshold", "operator": "lte", "value": 0.001},
                ],
            },
            "required_data": ["mark_price", "klines", "funding_rate"],
            "execution_mode": "rule_direct",
        }
        created = self.store.create("K线直通策略")
        sid = created["strategy_id"]
        drafted = self.store.save_draft(
            sid, {"revision": 0, "source_text": "EMA5>EMA20 且 RSI>60", "rules": kline_rules}
        )
        mounted = self.store.mount(
            sid,
            {
                "revision": drafted["draft"]["revision"],
                "content_hash": drafted["draft"]["content_hash"],
                "request_id": "kline-m1",
                "confirmed": True,
            },
        )["strategy"]
        enabled = self.store.authorize(sid, enabled=True, generation=mounted["generation"])
        self.assertTrue(enabled["enabled"])

        # Evaluate candidates via API:
        now_ms = int(time.time() * 1000)
        self.clock_val[0] = now_ms / 1000.0
        cand_resp = self.client.post("/api/strategies/evaluate-candidates", json={"now_ms": now_ms})
        self.assertEqual(cand_resp.status_code, 200, cand_resp.text)
        cand = next(
            c for c in cand_resp.json()["candidates"] if c["strategy_id"] == sid
        )
        self.assertEqual(cand["execution_mode"], "rule_direct")
        self.assertIn("ema_fast", cand["indicator_facts"])
        self.assertIn("rsi", cand["indicator_facts"])

        ctx = {
            "strategy_id": sid,
            "version": cand["version"],
            "generation": cand["generation"],
            "mode_revision": cand_resp.json()["mode_revision"],
            "signal_key": cand["signal_key"],
        }
        p1 = self.evaluate(context=ctx, cycle="kline-e2e-1")
        self.assertEqual(p1.status_code, 200, p1.text)
        p2 = self.client.post(
            "/api/execution/order",
            json={"approval_token": p1.json()["approval"]["token"], "strategy_context": ctx},
        )
        self.assertIn(p2.status_code, (200, 201), p2.text)

        # Re-evaluating candidates skips it as ALREADY_SIGNALED:
        cand_resp_2 = self.client.post("/api/strategies/evaluate-candidates", json={"now_ms": now_ms})
        skipped_codes = {
            item["reason_code"]
            for item in cand_resp_2.json()["skipped"]
            if item["strategy_id"] == sid
        }
        self.assertIn("ALREADY_SIGNALED", skipped_codes)


if __name__ == "__main__":
    unittest.main()
