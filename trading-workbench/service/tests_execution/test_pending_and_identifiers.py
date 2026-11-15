"""T03 §8·B1/B2：标识贯通、待确认状态与“永停工作态”的交易所事实收敛。

覆盖验收：
- round_id/cycle_id → business_key → client_order_id → exchange_order_id 全链落库；
- 同一意图重试保留原标识，超时后不生成新标识直接重发；
- 断线后按原 client_order_id 查询收敛（只查不重发）；
- 提交中/结果未知对外显示“待确认”，不因重试次数用尽改写成确定失败；
- 历史工作态永停行（T01 G4）按“不在 open 列表 + 按原标识查不到”收敛到明确终态。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, plan, submit  # noqa: E402

from workbench_service.execution.errors import OutboxUncertain  # noqa: E402
from workbench_service.risk.models import OrderPlan  # noqa: E402


def _seed_stale_protection_rows(case: ServiceCase) -> list[str]:
    """复刻 T01 基线 G4：4 条 2026-11-10 的保护单在 orders/outbox 永停 NEW。"""

    ledger = case.service.ledger
    rows = [
        ("wbp-st-1094bf0d8d082c6fd358", "STOP_MARKET", "1000000282224664", "2026-11-10T19:38:53Z"),
        ("wbp-ta-4357e546eba9a529dc2f", "TAKE_PROFIT_MARKET", "1000000282224667", "2026-11-10T19:38:53Z"),
        ("wbp-st-446f0cf9f2e3e0306a0b", "STOP_MARKET", "1000000282299671", "2026-11-10T21:09:27Z"),
        ("wbp-ta-862f25df05160489235d", "TAKE_PROFIT_MARKET", "1000000282299673", "2026-11-10T21:09:27Z"),
    ]
    for cid, order_type, exchange_id, created_at in rows:
        ledger.insert_order(
            {
                "client_order_id": cid,
                "exchange_order_id": exchange_id,
                "purpose": "PROTECTION",
                "symbol": "BTCUSDT",
                "side": "BUY",
                "position_side": "SHORT",
                "order_type": order_type,
                "qty_requested": "0.037",
                "qty_filled": "0",
                "status": "NEW",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        ledger.outbox_prepare(
            {
                "client_order_id": cid,
                "environment": "demo",
                "business_key": "PROTECTION:BTCUSDT:SHORT",
                "endpoint": "/fapi/v1/algoOrder",
                "request": {"symbol": "BTCUSDT", "type": order_type},
                "status": "NEW",
            }
        )
        ledger.execute(
            "UPDATE order_outbox SET status='NEW', created_at=?, updated_at=? "
            "WHERE client_order_id=?",
            (created_at, created_at, cid),
        )
    return [row[0] for row in rows]


class IdentifierChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_entry_order_persists_the_whole_identifier_chain(self) -> None:
        cycle_id = "cyc-20261114T010203Z-abc123"
        response = submit(self.client, approve(self.client, cycle_id=cycle_id))
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["intent_id"], cycle_id)
        self.assertTrue(str(body["business_key"]).startswith("ENTRY:BTCUSDT"))
        self.assertTrue(body["client_order_id"])
        self.assertTrue(body["exchange_order_id"])

        order = self.case.service.ledger.get_order(body["client_order_id"])
        self.assertIsNotNone(order)
        self.assertEqual(order["cycle_id"], cycle_id)
        self.assertEqual(str(order["exchange_order_id"]), str(body["exchange_order_id"]))

        payload = self.client.get("/api/orders").json()
        row = next(
            item
            for item in payload["orders"]
            if item["client_order_id"] == body["client_order_id"]
        )
        self.assertEqual(row["intent_id"], cycle_id)
        self.assertEqual(row["business_key"], body["business_key"])
        self.assertEqual(row["display"], "FILLED")
        self.assertFalse(row["pending_confirmation"])

    def test_same_intent_retry_reuses_identifiers_and_never_reposts(self) -> None:
        broker = self.case.service.broker
        order_plan = OrderPlan.from_payload(plan(), cycle_id="cyc-retry-intent-1")
        first = broker.open_market(
            order_plan, Decimal("0.02"), cycle_id="cyc-retry-intent-1"
        )
        self.assertFalse(first["idempotent_replay"])
        posts = len(self.case.fake.calls_for("/fapi/v1/order", "POST"))
        self.assertEqual(posts, 1)

        replay = broker.open_market(
            OrderPlan.from_payload(plan(), cycle_id="cyc-retry-intent-1"),
            Decimal("0.02"),
            cycle_id="cyc-retry-intent-1",
        )
        self.assertEqual(replay["client_order_id"], first["client_order_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            len(self.case.fake.calls_for("/fapi/v1/order", "POST")),
            1,
            "同一意图重试不得生成新标识再发一次",
        )

    def test_timeout_does_not_generate_a_new_identifier_for_a_repost(self) -> None:
        self.case.fake.timeout_orders = 1
        broker = self.case.service.broker
        order_plan = OrderPlan.from_payload(plan(), cycle_id="cyc-timeout-intent")
        with self.assertRaises(OutboxUncertain) as raised:
            broker.open_market(order_plan, Decimal("0.02"), cycle_id="cyc-timeout-intent")
        cid = raised.exception.client_order_id
        self.assertEqual(len(self.case.fake.calls_for("/fapi/v1/order", "POST")), 1)

        replay = broker.open_market(
            OrderPlan.from_payload(plan(), cycle_id="cyc-timeout-intent"),
            Decimal("0.02"),
            cycle_id="cyc-timeout-intent",
        )
        self.assertEqual(replay["client_order_id"], cid)
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            len(self.case.fake.calls_for("/fapi/v1/order", "POST")),
            1,
            "结果未知只能查询，不能换标识重发",
        )

    def test_query_by_original_id_converges_not_found_to_explicit_canceled(self) -> None:
        self.case.fake.timeout_orders = 1
        response = submit(self.client, approve(self.client, cycle_id="cyc-dropped"))
        self.assertEqual(response.status_code, 202, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_FILL_UNCONFIRMED")
        self.assertEqual(error["details"]["display"], "PENDING_CONFIRMATION")
        cid = error["details"]["client_order_id"]

        first = self.client.get(f"/api/execution/order/{cid}").json()
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertTrue(first["pending_confirmation"])
        self.assertFalse(first["converged"])
        self.assertGreaterEqual(first["query_confirmations"], 1)

        second = self.client.get(f"/api/execution/order/{cid}").json()
        self.assertEqual(second["status"], "CANCELED")
        self.assertTrue(second["converged"])
        self.assertEqual(second["converged_by"], "exchange_not_found")
        self.assertEqual(
            len(self.case.fake.calls_for("/fapi/v1/order", "POST")),
            1,
            "收敛过程中绝不能重发",
        )
        order = self.case.service.ledger.get_order(cid)
        self.assertEqual(order["status"], "CANCELED")
        self.assertIn("canceled_not_found_at_exchange", str(order["error_msg"] or ""))

    def test_query_endpoint_returns_404_for_unknown_local_id(self) -> None:
        response = self.client.get("/api/execution/order/nope-not-local")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_ORDER_NOT_FOUND")


class PendingConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def _unknown_entry(self) -> str:
        self.case.fake.timeout_orders = 1
        response = submit(self.client, approve(self.client, cycle_id="cyc-unknown"))
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()["error"]["details"]["client_order_id"]

    def test_unknown_order_is_displayed_as_pending_confirmation(self) -> None:
        cid = self._unknown_entry()

        status = self.client.get("/api/status").json()
        self.assertEqual(status["outbox"]["pending_confirmation_count"], 1)
        self.assertEqual(
            status["outbox"]["pending"][0]["display"], "PENDING_CONFIRMATION"
        )
        self.assertIn("ORDER_OUTBOX_UNKNOWN", status["readiness"]["reasons"])
        self.assertFalse(status["readiness"]["new_entry_allowed"])

        payload = self.client.get("/api/orders").json()
        self.assertEqual(payload["pending_confirmation_count"], 1)
        self.assertEqual(payload["pending_confirmation_ids"], [cid])
        row = next(
            item for item in payload["orders"] if item["client_order_id"] == cid
        )
        self.assertEqual(row["display"], "PENDING_CONFIRMATION")
        self.assertTrue(row["pending_confirmation"])
        self.assertFalse(row["terminal"])

    def test_repeated_reconcile_never_rewrites_pending_into_a_failure(self) -> None:
        cid = self._unknown_entry()
        for _ in range(3):
            self.client.post("/api/execution/reconcile")
        row = next(
            item
            for item in self.client.get("/api/orders").json()["orders"]
            if item["client_order_id"] == cid
        )
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertNotEqual(row["status"], "REJECTED")
        self.assertNotEqual(row["status"], "CANCELED")
        self.assertTrue(row["pending_confirmation"])
        self.assertGreaterEqual(int(row["outbox_attempts"]), 2)

    def test_recover_orders_endpoint_is_query_only(self) -> None:
        self._unknown_entry()
        response = self.client.post(
            "/api/execution/orders/recover", json={"request_id": "req-recover-orders"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["unresolved"], 1)
        self.assertFalse(body["converged"])
        self.assertEqual(body["operation_status"], "partial")
        self.assertEqual(len(self.case.fake.calls_for("/fapi/v1/order", "POST")), 1)
        operation = self.case.service.ledger.operations()[0]
        self.assertEqual(operation["action"], "recover_orders")
        self.assertEqual(operation["status"], "partial")


class StaleWorkingOrderConvergenceTests(unittest.TestCase):
    """T01 G4：交易所没有、账本却永停 NEW 的保护单必须收敛并可解释。"""

    def test_g4_rows_converge_to_explicit_terminal_on_startup(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                seeded = _seed_stale_protection_rows(case)
                before = client.get("/api/orders").json()
                self.assertEqual(
                    {row["status"] for row in before["orders"]}, {"NEW"}
                )
                self.assertEqual(before["pending_confirmation_count"], 0)

                startup = client.get("/api/status").json()["startup"]
                self.assertIsNotNone(startup.get("recovery"), startup)
                self.assertTrue(startup["recovery"]["converged"], startup["recovery"])
                self.assertTrue(startup["recovery"]["converged_orders"])

                after = client.get("/api/orders").json()
                statuses = {row["client_order_id"]: row for row in after["orders"]}
                for cid in seeded:
                    row = statuses[cid]
                    self.assertEqual(row["status"], "CANCELED", row)
                    self.assertEqual(row["display"], "TERMINAL")
                    self.assertEqual(
                        row["convergence_reason"], "canceled_not_found_at_exchange"
                    )
                    outbox_row = case.service.outbox.get(cid)
                    self.assertEqual(outbox_row["status"], "CANCELED")
                    self.assertEqual(
                        outbox_row["last_error"], "canceled_not_found_at_exchange"
                    )

                events = [
                    row["event_type"]
                    for row in case.service.ledger.list_risk_events(50)
                ]
                self.assertIn("order_converged_not_found", events)
                # 全过程只查询：没有任何下单请求
                self.assertEqual(
                    len(case.fake.calls_for("/fapi/v1/order", "POST")), 0
                )
                self.assertEqual(
                    len(case.fake.calls_for("/fapi/v1/algoOrder", "POST")), 0
                )
        finally:
            case.close()

    def test_open_protection_orders_are_never_converged(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                algo_id = case.fake.add_algo("BTCUSDT", "STOP_MARKET", "99000")
                cid = case.fake.algos[algo_id]["clientAlgoId"]
                case.service.ledger.insert_order(
                    {
                        "client_order_id": cid,
                        "exchange_order_id": str(algo_id),
                        "purpose": "PROTECTION",
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "order_type": "STOP_MARKET",
                        "qty_requested": "0.01",
                        "status": "NEW",
                        "created_at": "2026-11-10T19:38:53Z",
                        "updated_at": "2026-11-10T19:38:53Z",
                    }
                )
                result = case.service.broker.converge_stale_orders()
                self.assertEqual(result["converged"], [])
                self.assertEqual(case.service.ledger.get_order(cid)["status"], "NEW")
        finally:
            case.close()

    def test_query_failure_keeps_the_row_waiting(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                seeded = _seed_stale_protection_rows(case)
                case.fake.fail_algo_query = 50
                result = case.service.broker.converge_stale_orders()
                self.assertEqual(result["converged"], [])
                self.assertGreater(result["errors"], 0)
                for cid in seeded:
                    self.assertEqual(case.service.ledger.get_order(cid)["status"], "NEW")
        finally:
            case.close()


class BaselineCopyConvergenceTests(unittest.TestCase):
    """用 T01 采集副本库（data-t01-baseline）做一次真实历史行注入。"""

    BASELINE = Path(__file__).resolve().parents[2] / "data-t01-baseline"

    @unittest.skipUnless(BASELINE.is_dir(), "T01 baseline copy is not present")
    def test_real_baseline_rows_converge_without_touching_the_copy(self) -> None:
        import shutil
        import sqlite3
        import tempfile

        from fake_demo import FakeDemoExchange
        from support import make_service
        from workbench_service.config import Settings

        tmp = tempfile.TemporaryDirectory()
        target = Path(tmp.name) / "baseline"
        known_history = {
            "wbp-st-1094bf0d8d082c6fd358",
            "wbp-ta-4357e546eba9a529dc2f",
            "wbp-st-446f0cf9f2e3e0306a0b",
            "wbp-ta-862f25df05160489235d",
        }
        try:
            shutil.copytree(self.BASELINE, target)
            source = self.BASELINE / "workbench-v1.sqlite"
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                before_new = connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='NEW'"
                ).fetchone()[0]
                before_history = {
                    row[0]
                    for row in connection.execute(
                        "SELECT client_order_id FROM orders WHERE status='NEW' "
                        "AND created_at LIKE '2026-11-10%'"
                    )
                }
            finally:
                connection.close()
            self.assertTrue(
                known_history.issubset(before_history),
                f"副本缺少 2026-11-10 的 4 条历史保护单：{sorted(before_history)}",
            )

            settings = Settings.from_env(data_dir=target)
            service = make_service(settings, FakeDemoExchange())
            try:
                ledger = service.ledger
                stale = [
                    row
                    for row in ledger.list_active_orders()
                    if str(row.get("status")).upper() == "NEW"
                ]
                self.assertGreaterEqual(len(stale), len(known_history))
                result = service.broker.converge_stale_orders()
                self.assertGreaterEqual(len(result["converged"]), len(known_history))
                for row in stale:
                    self.assertEqual(ledger.get_order(row["client_order_id"])["status"], "CANCELED")
            finally:
                service.ledger.close()

            # 原始副本（数据目录）必须保持原样：只读计数不变
            connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            try:
                after_new = connection.execute(
                    "SELECT COUNT(*) FROM orders WHERE status='NEW'"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(
                after_new, before_new, "baseline data dir must not be modified"
            )
        finally:
            tmp.cleanup()



if __name__ == "__main__":
    unittest.main()
