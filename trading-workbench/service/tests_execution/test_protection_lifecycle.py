"""T03 §8·B3/B4：部分成交后的保护覆盖、补挂前查重、保护失败处置。

覆盖验收：
- 部分成交按交易所确认的实际仓位判定保护；后续增量成交重新核对并按需补挂；
- 补挂前先查已有保护单，同一侧不会挂出第二张；
- 回执丢失按客户端标识查询收敛，不盲目重发；
- 保护确认建不起来时按已配置策略减少风险并记录（系统仓位减仓 / 外部仓位只阻断）；
- 查询失败（UNKNOWN）既不当作“没有保护”，也不自动平仓。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, submit  # noqa: E402


def _open_position(case: ServiceCase, client) -> dict:
    response = submit(client, approve(client, cycle_id="prot-lifecycle"))
    assert response.status_code == 201, response.text
    return response.json()


def _count_open_algos(fake, order_type: str) -> int:
    return len([row for row in fake._open_algos() if row["orderType"] == order_type])


def _cancel_exchange_protection(fake) -> None:
    for algo in fake.algos.values():
        algo["algoStatus"] = "CANCELED"
        algo["status"] = "CANCELED"


class PartialFillProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_protection_uses_exchange_confirmed_partial_qty(self) -> None:
        self.fake.partial_fill = True
        body = _open_position(self.case, self.client)
        self.assertEqual(body["status"], "PARTIALLY_FILLED")
        self.assertEqual(Decimal(body["qty_filled"]), Decimal("0.01"))
        self.assertEqual(body["protection"]["status"], "OK")
        row = self.case.service.ledger.get_position("BTCUSDT", "LONG")
        self.assertEqual(Decimal(row["qty"]), Decimal("0.01"))
        self.assertEqual(row["protection_status"], "OK")
        self.assertEqual(_count_open_algos(self.fake, "STOP_MARKET"), 1)
        self.assertEqual(_count_open_algos(self.fake, "TAKE_PROFIT_MARKET"), 1)

    def test_incremental_fill_rechecks_and_tops_up_without_duplicates(self) -> None:
        self.fake.partial_fill = True
        _open_position(self.case, self.client)
        # 后续增量成交：交易所确认仓位变成 0.02，且 TP 已在交易所消失
        self.fake.positions["BTCUSDT"]["qty"] = Decimal("0.02")
        for algo in self.fake.algos.values():
            if algo["orderType"] == "TAKE_PROFIT_MARKET":
                algo["algoStatus"] = "CANCELED"

        reconciled = self.service.broker.reconcile()
        self.assertIn("BTCUSDT", reconciled["incremental_fills"])
        self.assertIn(
            "protection_recheck_after_incremental_fill",
            [row["event_type"] for row in self.service.ledger.list_risk_events(50)],
        )

        repaired = self.service.broker.enforce_protection()
        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual(_count_open_algos(self.fake, "STOP_MARKET"), 1)
        self.assertEqual(_count_open_algos(self.fake, "TAKE_PROFIT_MARKET"), 1)
        row = self.service.ledger.get_position("BTCUSDT", "LONG")
        self.assertEqual(Decimal(row["qty"]), Decimal("0.02"))
        self.assertEqual(row["protection_status"], "OK")

    def test_repair_queries_existing_protection_before_placing(self) -> None:
        _open_position(self.case, self.client)
        posts = len(self.fake.calls_for("/fapi/v1/algoOrder", "POST"))
        self.assertEqual(posts, 2)
        # 任何新补挂都会被拒绝：覆盖已 OK 时根本不该再发 POST
        self.fake.protection_failures = 1000
        result = self.service.broker.repair_protection()
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            len(self.fake.calls_for("/fapi/v1/algoOrder", "POST")),
            posts,
            "已有保护时不得再挂同向保护单",
        )

    def test_lost_protection_receipt_is_converged_by_query(self) -> None:
        # 交易所收单但回执丢失（两次 POST 都在落地后超时）
        self.fake.algo_timeout_after_create = 2
        body = _open_position(self.case, self.client)
        self.assertEqual(body["protection"]["status"], "OK")
        self.assertEqual(_count_open_algos(self.fake, "STOP_MARKET"), 1)
        self.assertEqual(_count_open_algos(self.fake, "TAKE_PROFIT_MARKET"), 1)
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/algoOrder", "POST")), 2)
        for algo in self.fake._open_algos():
            outbox_row = self.case.service.outbox.get(algo["clientAlgoId"])
            self.assertNotEqual(
                outbox_row["status"], "UNKNOWN", "查询已证明存在的保护单不能永停 UNKNOWN"
            )


class ProtectionFailureDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_confirmed_failure_reduces_system_position_and_records(self) -> None:
        _open_position(self.case, self.client)
        _cancel_exchange_protection(self.fake)
        self.fake.protection_failures = 1000
        posts_before = len(self.fake.calls_for("/fapi/v1/algoOrder", "POST"))

        result = self.service.broker.enforce_protection()
        item = result["repaired"][0]
        self.assertEqual(item["disposition"], "reduce_position")
        self.assertTrue(item["closed"])
        self.assertEqual(self.fake.positions, {})
        # 有限补单：不会无限挂保护
        self.assertLessEqual(
            len(self.fake.calls_for("/fapi/v1/algoOrder", "POST")) - posts_before, 4
        )
        operation = self.service.ledger.operations()[0]
        self.assertEqual(operation["action"], "reduce_position")
        self.assertEqual(operation["status"], "succeeded")
        self.assertEqual(operation["summary"]["closed"], True)
        self.assertIn(
            "protection_reduce_position",
            [row["event_type"] for row in self.service.ledger.list_risk_events(50)],
        )

    def test_reduce_is_recorded_once_per_position_identity(self) -> None:
        _open_position(self.case, self.client)
        _cancel_exchange_protection(self.fake)
        self.fake.protection_failures = 1000
        with mock.patch.object(
            self.service.broker, "close_market", return_value={"closed": False}
        ) as close_mock:
            first = self.service.broker.enforce_protection()
            second = self.service.broker.enforce_protection()
        self.assertEqual(first["repaired"][0]["disposition"], "reduce_position_incomplete")
        self.assertEqual(second["repaired"][0]["disposition"], "reduce_already_recorded")
        self.assertEqual(close_mock.call_count, 1, "同一仓位不重复发减仓命令")

    def test_reduce_failure_blocks_new_risk_and_marks_position(self) -> None:
        _open_position(self.case, self.client)
        _cancel_exchange_protection(self.fake)
        self.fake.protection_failures = 1000
        with mock.patch.object(
            self.service.broker, "close_market", side_effect=RuntimeError("close failed")
        ):
            result = self.service._enforce_protection()
        item = result["repaired"][0]
        self.assertEqual(item["disposition"], "reduce_unknown")
        self.assertEqual(self.service.state.control().state, "close_only")
        events = [row["event_type"] for row in self.service.ledger.list_risk_events(50)]
        self.assertIn("protection_reduce_failed", events)
        row = self.service.ledger.get_position("BTCUSDT", "LONG")
        self.assertEqual(row["protection_status"], "PROTECTION_FAILED")
        self.assertEqual(len(self.fake.positions), 1)

    def test_external_position_is_never_auto_closed(self) -> None:
        self.fake.add_position("BTCUSDT", "LONG", "0.01", "100000")
        self.service.ensure_started()
        self.service.broker.reconcile()
        row = self.service.ledger.get_position("BTCUSDT", "LONG")
        self.assertEqual(row["origin"], "EXTERNAL")

        self.fake.protection_failures = 1000
        result = self.service.broker.enforce_protection()
        item = result["repaired"][0]
        self.assertEqual(item["disposition"], "block_new_entries")
        self.assertEqual(len(self.fake.positions), 1, "外部仓位不套用系统减仓策略")

        status = self.service.status()
        self.assertIn("PROTECTION_MISSING", status["readiness"]["reasons"])
        self.assertFalse(status["readiness"]["new_entry_allowed"])

    def test_unproven_coverage_blocks_new_risk_without_flatten(self) -> None:
        _open_position(self.case, self.client)
        self.fake.fail_algo_query = 100
        result = self.service._enforce_protection()
        item = result["repaired"][0]
        self.assertEqual(item["status"], "UNKNOWN")
        self.assertEqual(item["disposition"], "block_new_entries")
        self.assertEqual(len(self.fake.positions), 1, "查询失败不得平仓")
        # 保护未证明时新开仓必须被拒（具体错误码取决于同一快照下先命中的规则）。
        blocked = evaluate(self.client, cycle_id="after-unproven")
        self.assertEqual(blocked.status_code, 422, blocked.text)
        status = self.service.status()
        # 保护查询失败会连带交易所不可达/权益不可用，任一都必须阻断新开仓。
        self.assertTrue(
            {
                "PROTECTION_UNPROVEN",
                "E_EXCHANGE_UNAVAILABLE",
                "EQUITY_UNAVAILABLE",
            }
            & set(status["readiness"]["reasons"]),
            status["readiness"],
        )
        self.assertFalse(status["readiness"]["new_entry_allowed"])


if __name__ == "__main__":
    unittest.main()
