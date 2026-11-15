"""§8·C 资金口径：四本账分离、对账三态、覆盖范围与 schema 版本一致性。

全部离线：临时库 + fake 上游，不访问网络、不动真实 data/。
"""

import dataclasses
import sys
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from support import ServiceCase, make_service

from workbench_service.config import Settings
from workbench_service.ledger.schema import LEDGER_SCHEMA_VERSION

DAY_MS = 24 * 3600 * 1000


def _flow(ref_id, kind, amount, at_ms, *, asset="USDT", income_type=None):
    return {
        "ref_id": ref_id,
        "kind": kind,
        "amount": amount,
        "at_ms": at_ms,
        "asset": asset,
        "income_type": income_type or kind,
    }


class IncomeIngestTests(unittest.TestCase):
    """income 流水落库：分类、幂等、完成标记与策略盈亏联动。"""

    def setUp(self):
        self.case = ServiceCase(with_fake=True)
        self.ledger = self.case.service.ledger
        self.now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def tearDown(self):
        self.case.close()

    def _income_rows(self):
        return [
            {
                "tranId": 900001,
                "time": self.now_ms - 3000,
                "incomeType": "FUNDING_FEE",
                "income": "-0.42",
                "asset": "USDT",
                "symbol": "BTCUSDT",
            },
            {
                "tranId": 900002,
                "time": self.now_ms - 2000,
                "incomeType": "TRANSFER",
                "income": "500",
                "asset": "USDT",
            },
            {
                "tranId": 900003,
                "time": self.now_ms - 1000,
                "incomeType": "TRANSFER",
                "income": "-120",
                "asset": "USDT",
            },
        ]

    def test_income_is_classified_and_idempotent(self):
        self.case.fake.income_rows = self._income_rows()
        first = self.case.service.sync_capital_flows(range_s=86400)
        self.assertTrue(first["ok"], first)
        self.assertEqual(first["inserted"], 3)
        second = self.case.service.sync_capital_flows(range_s=86400)
        self.assertEqual(second["inserted"], 0, "重复回填不得产生重复行")

        report = self.ledger.strategy_pnl(
            start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000
        )
        self.assertEqual(report["funding_pnl"], "-0.42")
        self.assertEqual(report["transfers"]["net_quote"], "380")
        self.assertTrue(report["funding_known"])
        # 无成交时净盈亏 = 资金费（划转不计入）
        self.assertEqual(report["net_strategy_pnl"], "-0.42")

    def test_sync_marks_income_complete_so_funding_is_known(self):
        self.case.fake.income_rows = []
        result = self.case.service.sync_capital_flows(range_s=86400)
        self.assertTrue(result["complete"])
        state = self.ledger.backfill_state("income")
        self.assertTrue(state["complete"])
        report = self.ledger.strategy_pnl(
            start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000
        )
        self.assertTrue(report["funding_known"])
        self.assertEqual(report["funding_pnl"], "0")

    def test_no_sync_keeps_funding_unknown(self):
        report = self.ledger.strategy_pnl(
            start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000
        )
        self.assertFalse(report["funding_known"])
        self.assertIsNone(report["net_strategy_pnl"])

    def test_sync_endpoint_is_operator_triggered_and_read_only(self):
        self.case.fake.income_rows = self._income_rows()
        client = self.case.client()
        with client:
            before = len(self.case.fake.signed_calls_for("/fapi/v1/order", "POST"))
            response = client.post("/api/sync-income?range=7d")
            after = len(self.case.fake.signed_calls_for("/fapi/v1/order", "POST"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(before, after, "同步资金流水不得下任何订单")
        body = response.json()
        self.assertTrue(body["demo_only"])
        self.assertGreaterEqual(body["inserted"], 1)


class FundCaliberTests(unittest.TestCase):
    def setUp(self):
        self.case = ServiceCase(with_fake=False)
        self.ledger = self.case.service.ledger
        self.now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    def tearDown(self):
        self.case.close()

    def _mark_backfill_complete(self):
        """把 fills/income 两条流标记为已回填完成。

        没有这一步时“资金费”只能算未知，net_strategy_pnl 必须是 None。
        """
        for stream in ("fills", "income"):
            self.ledger.set_backfill_state(
                stream, cursor_ms=self.now_ms, covered_from_ms=self.now_ms - DAY_MS, complete=True
            )

    def _fill(self, fill_id, *, realized, commission, asset="USDT", at_ms=None):
        self.ledger.insert_fill(
            {
                "fill_id": fill_id,
                "client_order_id": f"local-{fill_id}",
                "symbol": "BTCUSDT",
                "side": "SELL",
                "price": "100",
                "qty": "1",
                "realized_pnl": realized,
                "commission": commission,
                "commission_asset": asset,
                "trade_time_ms": self.now_ms if at_ms is None else at_ms,
            }
        )

    def test_fees_and_funding_are_separate_books_and_transfers_excluded(self):
        self._fill("f1", realized="10", commission="0.4")
        self._fill("f2", realized="-2", commission="0.1")
        self.ledger.upsert_capital_flows(
            [
                _flow("inc-1", "FUNDING_FEE", "-0.25", self.now_ms - 1000),
                _flow("inc-2", "TRANSFER_IN", "1000", self.now_ms - 2000),
                _flow("inc-3", "TRANSFER_OUT", "-300", self.now_ms - 3000),
            ]
        )
        self._mark_backfill_complete()
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertEqual(report["realized_pnl"], "8")
        self.assertEqual(report["commission_quote"], "0.5")
        self.assertEqual(report["funding_pnl"], "-0.25")
        # 策略净盈亏 = 已实现 - 手续费 + 资金费；划转不参与
        self.assertEqual(report["net_strategy_pnl"], "7.25")
        self.assertEqual(report["transfers"]["net_quote"], "700")
        self.assertIn("transfers", report["excludes"])

    def test_transfer_never_counts_as_profit(self):
        self.ledger.upsert_capital_flows(
            [_flow("inc-9", "TRANSFER_IN", "5000", self.now_ms - 1000)]
        )
        self._mark_backfill_complete()
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertEqual(report["realized_pnl"], "0")
        self.assertEqual(report["net_strategy_pnl"], "0")
        self.assertEqual(report["transfers"]["net_quote"], "5000")

    def test_non_quote_fee_marks_unknown_instead_of_zero_cost(self):
        self._fill("f3", realized="5", commission="0.01", asset="BNB")
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertIsNone(report["net_strategy_pnl"])
        self.assertTrue(any("BNB" in reason for reason in report["unknown"]), report["unknown"])

    def test_missing_commission_asset_is_unknown(self):
        self._fill("f4", realized="5", commission="0.01", asset=None)
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertIsNone(report["net_strategy_pnl"])
        self.assertTrue(report["unknown"])

    def test_unbackfilled_funding_is_unknown_not_zero(self):
        # 只有成交、从未回填资金费：不能凭空得到“净盈亏 = 已实现 - 手续费”。
        self._fill("f0", realized="10", commission="0.2")
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertFalse(report["funding_known"])
        self.assertIsNone(report["net_strategy_pnl"])
        self.assertTrue(any("资金费" in reason for reason in report["unknown"]), report["unknown"])

    def test_income_synced_realized_and_commission_are_not_transfers(self):
        """V12：income 同步会把 REALIZED_PNL / COMMISSION 写进 capital_flows。

        这两项已由 fills 计入“已实现”和“手续费”，若再被当成划转，expected
        会凭空多出（已实现−手续费），对账就会恒为 partial 并掩盖真实差额。
        夹具必须覆盖这个真实形态——旧夹具有 FUNDING_FEE/TRANSFER_*，所以 435
        条用例全绿也没抓到。
        """
        self._fill("v12a", realized="60", commission="0.5")
        self.ledger.upsert_capital_flows(
            [
                _flow("v12-funding", "FUNDING_FEE", "0.2", self.now_ms - 3000),
                _flow("v12-realized", "REALIZED_PNL", "60", self.now_ms - 2000, income_type="REALIZED_PNL"),
                _flow("v12-commission", "COMMISSION", "-0.5", self.now_ms - 1000, income_type="COMMISSION"),
            ]
        )
        self._mark_backfill_complete()
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertEqual(report["transfers"]["net_quote"], "0", "REALIZED_PNL/COMMISSION 不得被当成划转")
        self.assertEqual(report["transfers"]["count"], 0)
        self.assertEqual(report["funding_pnl"], "0.2")
        # 净盈亏 = 已实现 60 − 手续费 0.5 + 资金费 0.2
        self.assertEqual(report["net_strategy_pnl"], "59.7")
        # 交易所权益变动若与之相等（无划转），对账必须 matched
        result = self.ledger.reconcile_funds(
            start_ms=self.now_ms - DAY_MS,
            end_ms=self.now_ms + 1000,
            exchange_delta="59.7",
            tolerance="0.01",
        )
        self.assertEqual(result["status"], "matched", result["reasons"])
        self.assertEqual(result["diff"], "0")

    def test_unclassified_flow_type_is_reported_not_silently_filed(self):
        self.ledger.upsert_capital_flows(
            [_flow("v12-weird", "REFERRAL_KICKBACK", "1.5", self.now_ms - 500)]
        )
        self._mark_backfill_complete()
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertEqual(report["transfers"]["net_quote"], "0")
        self.assertTrue(
            any("未识别的资金流水类型" in reason for reason in report["unknown"]),
            report["unknown"],
        )

    def test_duplicate_backfill_is_idempotent(self):
        rows = [_flow("inc-dup", "FUNDING_FEE", "-1", self.now_ms - 500)]
        self.assertEqual(self.ledger.upsert_capital_flows(rows), 1)
        self.assertEqual(self.ledger.upsert_capital_flows(rows), 0)
        self.assertEqual(self.ledger.upsert_capital_flows(rows), 0)
        stored = self.ledger.list_capital_flows(start_ms=0, end_ms=self.now_ms + 1000)
        self.assertEqual(len(stored), 1)
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertEqual(report["funding_pnl"], "-1")

    def test_backfill_cursor_never_moves_backwards(self):
        self.ledger.set_backfill_state(
            "income", cursor_ms=self.now_ms, covered_from_ms=self.now_ms - DAY_MS, complete=True
        )
        self.ledger.set_backfill_state("income", cursor_ms=self.now_ms - DAY_MS)
        state = self.ledger.backfill_state("income")
        self.assertEqual(state["cursor_ms"], self.now_ms)
        self.assertTrue(state["complete"])

    def test_coverage_says_history_is_incomplete_not_zero(self):
        self._fill("f5", realized="1", commission="0.01")
        report = self.ledger.strategy_pnl(start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000)
        self.assertFalse(report["coverage"]["complete"])
        self.assertIsNone(report["coverage"]["covered_from_ms"])
        self.assertIn("不完整", report["coverage"]["note"])

    def test_reconcile_matched_within_tolerance(self):
        self._fill("f6", realized="10", commission="0")
        self._mark_backfill_complete()
        result = self.ledger.reconcile_funds(
            start_ms=self.now_ms - DAY_MS,
            end_ms=self.now_ms + 1000,
            exchange_delta="10.005",
            tolerance="0.01",
        )
        self.assertEqual(result["status"], "matched")
        self.assertIsNotNone(result["last_success_at"])

    def test_reconcile_partial_reports_diff_and_reason(self):
        self._fill("f7", realized="10", commission="0")
        self._mark_backfill_complete()
        result = self.ledger.reconcile_funds(
            start_ms=self.now_ms - DAY_MS,
            end_ms=self.now_ms + 1000,
            exchange_delta="13",
            tolerance="0.01",
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["diff"], "3")
        self.assertTrue(any("超出容差" in reason for reason in result["reasons"]), result["reasons"])

    def test_reconcile_unknown_when_inputs_missing(self):
        result = self.ledger.reconcile_funds(
            start_ms=self.now_ms - DAY_MS, end_ms=self.now_ms + 1000, exchange_delta=None
        )
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["diff"])
        self.assertTrue(result["reasons"])

    def test_unknown_equity_is_never_reported_as_zero(self):
        self.case.service.state.sync(None, equity_available=False)
        snapshot = self.case.service.state.snapshot()
        self.assertFalse(snapshot["available"])
        self.assertIsNone(snapshot["equity_session"])

    def test_risk_state_carries_equity_source_and_timezone(self):
        from decimal import Decimal

        self.case.service.state.sync(Decimal("1000"), equity_available=True)
        snapshot = self.case.service.state.snapshot()
        self.assertEqual(snapshot["day_start_equity_source"], "exchange")
        self.assertEqual(snapshot["day_start_equity_tz"], "UTC")
        self.assertIsNotNone(snapshot["day_start_equity_at"])
        self.assertEqual(snapshot["equity_caliber"], "exchange_wallet_plus_upnl")

    def test_water_level_is_not_reset_when_equity_unavailable(self):
        from decimal import Decimal

        self.case.service.state.sync(Decimal("1000"), equity_available=True)
        before = self.case.service.state.snapshot()
        # 查询失败：水位必须保持，不能因为“取不到”就把基线清零。
        self.case.service.state.sync(None, equity_available=False)
        after = self.case.service.state.snapshot()
        self.assertEqual(before["day_start_equity"], after["day_start_equity"])
        self.assertEqual(before["peak_equity"], after["peak_equity"])
        self.assertFalse(after["available"])

    def test_schema_report_flags_version_drift(self):
        report = self.ledger.schema_report()
        self.assertEqual(report["recorded_ledger_schema_version"], str(LEDGER_SCHEMA_VERSION))
        self.assertTrue(report["consistent"], report)
        # 冒充旧版本必须被识别，而不是静默通过。
        self.ledger.set_meta("ledger_schema_version", "1")
        drift = self.ledger.schema_report()
        self.assertFalse(drift["consistent"])
        self.assertIn("不一致", drift["reason"])

    def test_pnl_endpoint_returns_calibers_and_reconcile(self):
        self._fill("f8", realized="3", commission="0.05")
        client = self.case.client()
        with client:
            response = client.get("/api/pnl?range=1d")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIn("calibers", body)
        for key in ("wallet_balance", "unrealized_pnl", "margin_equity", "available_balance"):
            self.assertIn(key, body["calibers"])
        self.assertEqual(body["strategy"]["realized_pnl"], "3")
        self.assertEqual(body["strategy"]["commission_quote"], "0.05")
        self.assertIn(body["reconcile"]["status"], {"matched", "partial", "unknown"})
        self.assertIn("consistent", body["schema"])


class _AdvanceableClock:
    """可推进的假时钟：让“最小间隔/游标推进”可确定性断言，不靠 sleep。"""

    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class IncomeSyncScheduleTests(unittest.TestCase):
    """T18 §8·C2：资金费回填由服务端循环自己触发，不再依赖操作员点按钮。

    断言的是“实际发生了哪些交易所调用/账本写入”，不是函数返回码好看：
    间隔内不得重复打交易所、失败要落 last_error、游标只进不退、
    任何路径都不得产生签名写请求（POST）。
    """

    def setUp(self):
        self.clock = _AdvanceableClock()
        self.case = ServiceCase(with_fake=True, clock=self.clock)
        self.service = self.case.service
        self.ledger = self.service.ledger
        self.now_ms = int(self.clock.now * 1000)

    def tearDown(self):
        self.case.close()

    def _income_rows(self):
        return [
            {
                "tranId": 910001,
                "time": self.now_ms - 3000,
                "incomeType": "FUNDING_FEE",
                "income": "-0.42",
                "asset": "USDT",
                "symbol": "BTCUSDT",
            },
            {
                "tranId": 910002,
                "time": self.now_ms - 2000,
                "incomeType": "TRANSFER",
                "income": "500",
                "asset": "USDT",
            },
        ]

    def _income_calls(self) -> int:
        return len(self.case.fake.calls_for("/fapi/v1/income", "GET"))

    def _report(self):
        return self.ledger.strategy_pnl(
            start_ms=int(self.clock.now * 1000) - DAY_MS,
            end_ms=int(self.clock.now * 1000) + 1000,
        )

    def _assert_no_trading_write(self) -> None:
        self.assertEqual(self.case.fake.signed_calls_for("/fapi/v1/order", "POST"), [])
        self.assertEqual(self.case.fake.signed_calls_for("/fapi/v1/algoOrder", "POST"), [])
        posts = [call for call in self.case.fake.signed_calls if call["method"] == "POST"]
        self.assertEqual(posts, [], "资金费回填不得引入任何签名写请求")

    def test_service_cycle_turns_funding_known_and_pnl_computable(self):
        self.case.fake.income_rows = self._income_rows()
        before = self._report()
        self.assertFalse(before["funding_known"])
        self.assertIsNone(before["net_strategy_pnl"])

        result = self.service.maybe_sync_income()
        self.assertEqual(result["status"], "synced", result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["inserted"], 2)
        self.assertTrue(result["funding_known"])

        after = self._report()
        self.assertTrue(after["funding_known"])
        self.assertEqual(after["funding_pnl"], "-0.42")
        self.assertEqual(after["net_strategy_pnl"], "-0.42")

        # 同一事实在只读端点上可复现：/api/pnl 从 null 变成可计算值。
        client = self.case.client()
        with client:
            response = client.get("/api/pnl?range=1d")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["strategy"]["funding_known"])
        self.assertEqual(body["strategy"]["net_strategy_pnl"], "-0.42")
        self.assertIsNone(body["strategy"]["coverage"]["income"]["last_error"])
        self._assert_no_trading_write()

    def test_interval_throttles_exchange_hits(self):
        self.case.fake.income_rows = self._income_rows()
        settings = self.service.income_sync_settings()
        self.assertEqual(self.service.maybe_sync_income()["status"], "synced")
        calls_after_first = self._income_calls()
        self.assertEqual(calls_after_first, 1)

        throttled = self.service.maybe_sync_income()
        self.assertEqual(throttled["status"], "throttled")
        self.assertEqual(throttled["reason"], "interval_not_due")
        self.assertGreater(throttled["retry_after_s"], 0)
        self.assertIsNotNone(throttled["next_due_at"])
        self.assertEqual(self._income_calls(), calls_after_first, "间隔内不得再打交易所")

        # 到点后才允许下一次；这正是“不是每个请求都打交易所”的证据。
        self.clock.advance(settings["interval_s"] + 1)
        again = self.service.maybe_sync_income()
        self.assertEqual(again["status"], "synced", again)
        self.assertEqual(self._income_calls(), calls_after_first + 1)

    def test_window_continues_from_cursor_and_cursor_never_moves_backwards(self):
        self.case.fake.income_rows = self._income_rows()
        self.service.maybe_sync_income()
        first = self.ledger.backfill_state("income")
        self.assertEqual(first["cursor_ms"], self.now_ms)

        self.clock.advance(1000)
        later_ms = int(self.clock.now * 1000)
        result = self.service.maybe_sync_income()
        self.assertEqual(result["status"], "synced", result)

        query = dict(urllib.parse.parse_qsl(self.case.fake.calls_for("/fapi/v1/income", "GET")[-1]["query"]))
        overlap_ms = int(self.service.income_sync_settings()["overlap_s"] * 1000)
        self.assertEqual(int(query["startTime"]), first["cursor_ms"] - overlap_ms, "只拉未覆盖区间")
        self.assertEqual(int(query["endTime"]), later_ms)

        second = self.ledger.backfill_state("income")
        self.assertEqual(second["cursor_ms"], later_ms)
        self.assertGreater(second["cursor_ms"], first["cursor_ms"], "游标只进不退")
        self.assertLessEqual(second["covered_from_ms"], first["covered_from_ms"])

    def test_repeated_window_backfill_is_idempotent(self):
        self.case.fake.income_rows = self._income_rows()
        first = self.service.maybe_sync_income()
        self.assertEqual(first["inserted"], 2)
        # 模拟“重新回填同一窗口”：清掉游标后从同一个默认窗口再拉一遍。
        self.ledger.execute("DELETE FROM backfill_state WHERE stream='income'")
        self.clock.advance(self.service.income_sync_settings()["min_interval_s"] + 1)
        second = self.service.maybe_sync_income(force=True)
        self.assertEqual(second["status"], "synced", second)
        self.assertEqual(second["inserted"], 0, "同一窗口重复回填不得产生重复行")
        flows = self.ledger.list_capital_flows(
            start_ms=0, end_ms=int(self.clock.now * 1000) + 1000
        )
        self.assertEqual(len(flows), 2)
        self.assertEqual(self._report()["funding_pnl"], "-0.42")

    def test_failure_is_recorded_and_does_not_block_snapshot_or_control(self):
        control_before = self.ledger.load_control()
        signed_before = len(self.case.fake.signed_calls)
        original = self.service.broker.client.income

        def _boom(**kwargs):
            raise RuntimeError("income upstream down")

        self.service.broker.client.income = _boom
        try:
            result = self.service.maybe_sync_income(force=True)
        finally:
            self.service.broker.client.income = original

        self.assertEqual(result["status"], "failed", result)
        self.assertIn("income upstream down", result["error"])
        state = self.ledger.backfill_state("income") or {}
        self.assertIn("income_sync_failed", str(state.get("last_error")))
        self.assertFalse(bool(state.get("complete")), "失败不得把资金费标成已知")

        report = self._report()
        self.assertFalse(report["funding_known"])
        self.assertIsNone(report["net_strategy_pnl"])
        self.assertTrue(any("资金费" in reason for reason in report["unknown"]))

        # 可读：status 端点直接给出失败原因与状态，不用翻日志。
        status = self.service.status()
        self.assertEqual(status["income_sync"]["last_status"], "failed")
        self.assertIn("income_sync_failed", status["income_sync"]["last_error"])
        self.assertFalse(status["income_sync"]["funding_known"])
        self.assertTrue(status["exchange"]["reachable"], "回填失败不得让账户/行情读取一起变不可用")

        # 不阻塞权益快照：心跳照常落 equity 快照。
        self.service._heartbeat()
        self.assertIsNotNone(self.ledger.latest_equity_since(600))

        # 不改控制态、不产生下单路径。
        self.assertEqual(self.ledger.load_control(), control_before)
        posts = [
            call for call in self.case.fake.signed_calls[signed_before:] if call["method"] == "POST"
        ]
        self.assertEqual(posts, [])

    def test_defaults_are_configured_and_visible(self):
        settings = Settings.from_env(data_dir=self.case.data_dir)
        self.assertTrue(settings.income_sync_enabled)
        self.assertEqual(settings.income_sync_interval_s, 900.0)
        self.assertEqual(settings.income_sync_min_interval_s, 60.0)
        self.assertEqual(settings.income_sync_window_s, 604800.0)
        self.assertEqual(settings.describe()["income_sync"]["interval_s"], 900.0)
        status = self.service.income_sync_status()
        for key in ("enabled", "interval_s", "min_interval_s", "window_s", "manual_endpoint"):
            self.assertIn(key, status)
        self.assertEqual(status["manual_endpoint"], "POST /api/sync-income")
        self.assertFalse(status["funding_known"], "没有回填过时资金费必须是未知")

    def test_disabled_switch_falls_back_to_manual_and_is_not_silent(self):
        settings = dataclasses.replace(self.service.settings, income_sync_enabled=False)
        disabled = make_service(settings, self.case.fake, clock=self.clock)
        try:
            calls_before = self._income_calls()
            result = disabled.maybe_sync_income()
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(result["reason"], "income_sync_disabled")
            self.assertIn("/api/sync-income", result["note"])
            self.assertEqual(self._income_calls(), calls_before, "关闭时自动路径不得打交易所")

            status = disabled.status()["income_sync"]
            self.assertFalse(status["enabled"])
            self.assertIn("已关闭", status["note"])

            # 手动入口仍然可用：关闭开关是“回到手动”，不是静默失效。
            manual = disabled.sync_capital_flows(range_s=86400)
            self.assertTrue(manual["ok"])
            self.assertTrue(manual["complete"])
            self.assertEqual(self._income_calls(), calls_before + 1)
            self.assertTrue(disabled.income_sync_status()["funding_known"])
        finally:
            disabled.ledger.close()

    def test_http_polling_never_hits_the_exchange(self):
        """只读轮询（/api/status）不得成为回填触发器：节奏只归服务端循环。"""

        self.case.fake.income_rows = self._income_rows()
        client = self.case.client()
        with client:
            for _ in range(3):
                response = client.get("/api/status")
                self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._income_calls(), 0, "只读轮询不得触发交易所 income 请求")
        self.assertIsNone(self.ledger.backfill_state("income"))
        self.assertFalse(self.service.status()["income_sync"]["funding_known"])

    def test_status_endpoint_body_carries_income_sync_structure(self):
        """守卫 /api/status 的响应体：缺方法/缺字段会 500，这里必须 200 且结构完整。"""

        self.case.fake.income_rows = self._income_rows()
        client = self.case.client()
        with client:
            response = client.get("/api/status")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            self.assertEqual(body["venue"], "demo")
            self.assertTrue(body["demo_only"])
            self.assertIn("equity", body)
            self.assertIn("control", body)
            self.assertTrue(body["exchange"]["reachable"])
            sync = body["income_sync"]
            for key in (
                "enabled",
                "interval_s",
                "min_interval_s",
                "window_s",
                "overlap_s",
                "last_status",
                "last_error",
                "next_due_at",
                "backfill",
                "funding_known",
                "manual_endpoint",
                "note",
            ):
                self.assertIn(key, sync, f"status.income_sync 缺字段 {key}")
            self.assertFalse(sync["funding_known"])
            self.assertIsNone(sync["last_status"])

            self.assertEqual(self.service.maybe_sync_income()["status"], "synced")

            after = client.get("/api/status")
            self.assertEqual(after.status_code, 200, after.text)
            sync_after = after.json()["income_sync"]
            self.assertTrue(sync_after["funding_known"])
            self.assertEqual(sync_after["last_status"], "synced")
            self.assertTrue(sync_after["backfill"]["complete"])
            self.assertIsNotNone(sync_after["last_success_at"])
            self.assertIsNone(sync_after["last_error"])

    def test_supervisor_loop_is_what_triggers_the_backfill(self):
        """不需要任何外部请求：监督循环自己把资金费拉回来。"""

        self.case.fake.income_rows = self._income_rows()
        self.assertIsNone(self.ledger.backfill_state("income"))
        self.assertTrue(self.service.start_monitor(interval_s=1.0))
        try:
            deadline = time.time() + 10
            state = None
            while time.time() < deadline:
                state = self.ledger.backfill_state("income")
                if state and state.get("complete"):
                    break
                time.sleep(0.05)
            self.assertTrue(state and state.get("complete"), "监督循环应自行完成 income 回填")
            self.assertTrue(self._report()["funding_known"])
            self.assertEqual(self.service.income_sync_status()["last_status"], "synced")
            self._assert_no_trading_write()
        finally:
            self.service.stop_monitor()


class VenueIdentityTests(unittest.TestCase):
    """§6 M2：环境在启动时固定；错目录/错身份必须拒绝，不得静默改标签。"""

    def test_unknown_venue_fails_closed(self):
        import os
        from workbench_service.config import Settings

        previous = os.environ.get("WORKBENCH_VENUE")
        os.environ["WORKBENCH_VENUE"] = "testnet"
        try:
            with self.assertRaises(ValueError):
                Settings.from_env()
        finally:
            if previous is None:
                os.environ.pop("WORKBENCH_VENUE", None)
            else:
                os.environ["WORKBENCH_VENUE"] = previous

    def test_venue_selects_base_url_and_live_data_dir(self):
        import os
        from workbench_service.config import Settings

        previous = os.environ.get("WORKBENCH_VENUE")
        os.environ["WORKBENCH_VENUE"] = "live"
        try:
            settings = Settings.from_env()
        finally:
            if previous is None:
                os.environ.pop("WORKBENCH_VENUE", None)
            else:
                os.environ["WORKBENCH_VENUE"] = previous
        self.assertEqual(settings.venue, "live")
        self.assertEqual(settings.base_url, "https://fapi.binance.com")
        self.assertEqual(settings.venue_host, "fapi.binance.com")
        self.assertEqual(settings.data_dir.name, "live")

    def test_guard_rejects_cross_venue_hosts(self):
        from workbench_service.guard import evaluate_guard

        self.assertTrue(evaluate_guard("https://demo-fapi.binance.com", "demo").ok)
        self.assertTrue(evaluate_guard("https://fapi.binance.com", "live").ok)
        # Demo 基址在 live 环境下必须被拒，反之亦然。
        self.assertFalse(evaluate_guard("https://demo-fapi.binance.com", "live").ok)
        self.assertFalse(evaluate_guard("https://fapi.binance.com", "demo").ok)
        self.assertFalse(evaluate_guard("https://fapi.binance.com", "testnet").ok)

    def test_ledger_venue_binding_refuses_mismatch(self):
        import tempfile
        from pathlib import Path

        from workbench_service.ledger.store import Ledger

        directory = Path(tempfile.mkdtemp())
        first = Ledger(directory)
        self.assertTrue(first.bind_venue("demo")["changed"])
        first.close()
        second = Ledger(directory)
        self.assertFalse(second.bind_venue("demo")["changed"])
        with self.assertRaises(ValueError):
            second.bind_venue("live")
        second.close()

    def test_live_credentials_never_fall_back_to_demo(self):
        from workbench_service.execution.credentials import load_live_credentials

        demo_only = {"BINANCE_DEMO_API_KEY": "k", "BINANCE_DEMO_API_SECRET": "s"}
        self.assertIsNone(load_live_credentials(".", env=demo_only))
        pair = {"BINANCE_LIVE_API_KEY": "k", "BINANCE_LIVE_API_SECRET": "s"}
        creds = load_live_credentials(".", env=pair)
        self.assertIsNotNone(creds)
        self.assertEqual(creds.source, "env")
        self.assertEqual(len(creds.fingerprint()), 8)


class ClientGuardAndProxyTests(unittest.TestCase):
    """§6 M2：客户端守卫按环境放行；代理环境变量里的非法条目不能炸掉构造。"""

    def _creds(self):
        from workbench_service.execution.credentials import DemoCredentials

        return DemoCredentials(api_key="k", api_secret="s", source="test")

    def test_client_accepts_the_venue_it_is_told_about(self):
        from workbench_service.execution.client import DemoBinanceClient

        demo = DemoBinanceClient("https://demo-fapi.binance.com", self._creds())
        self.assertEqual(demo.host, "demo-fapi.binance.com")
        live = DemoBinanceClient(
            "https://fapi.binance.com", self._creds(), allowed_host="fapi.binance.com"
        )
        self.assertEqual(live.host, "fapi.binance.com")

    def test_client_still_rejects_a_host_it_was_not_told_about(self):
        from workbench_service.execution.client import DemoBinanceClient
        from workbench_service.execution.errors import DemoGuardViolation

        with self.assertRaises(DemoGuardViolation):
            DemoBinanceClient("https://fapi.binance.com", self._creds())

    def test_invalid_no_proxy_entries_are_dropped_before_building_the_client(self):
        import os

        from workbench_service.execution.client import DemoBinanceClient

        previous = os.environ.get("NO_PROXY")
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1,[::1]"
        try:
            # 旧行为：httpx.Client() 解析 NO_PROXY 抛 InvalidURL，构造即失败。
            client = DemoBinanceClient("https://demo-fapi.binance.com", self._creds())
            self.assertEqual(client.host, "demo-fapi.binance.com")
            self.assertNotIn("[::1]", os.environ.get("NO_PROXY", ""))
        finally:
            if previous is None:
                os.environ.pop("NO_PROXY", None)
            else:
                os.environ["NO_PROXY"] = previous


if __name__ == "__main__":
    unittest.main()