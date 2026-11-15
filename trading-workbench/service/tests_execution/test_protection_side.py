"""M1.4d 保护单 \u4f4d\u7f6e\u65b9\u5411\u56de\u5f52\uff1a``positionSide`` \u53ea\u80fd\u5728 HEDGE \u6a21\u5f0f\u4e0b\u53d1\u9001\u3002

\u73b0\u573a\u4e8b\u6545\uff082026-11-10T16:13Z\uff09\uff1aENTRY \u5e02\u4ef7\u5355\u5728 one-way \u8d26\u6237\u4e0a\u6ca1\u6709
``positionSide`` \u5b57\u6bb5\uff08\u6210\u4ea4\u6b63\u5e38\uff09\uff0c\u4f46\u4fdd\u62a4\u5355\u603b\u662f\u5e26 ``positionSide=SHORT``\uff0c
\u4ea4\u6613\u6240\u56de ``-4061 Order's position side does not match user's setting``\u3002
\u7ed3\u679c\uff1a\u521a\u5f00\u4ed3\u7684\u4ed3\u4f4d\u56e0\u201c\u4fdd\u62a4\u4e0d\u5b8c\u6574\u201d\u88ab\u5f3a\u5236\u5e73\u4ed3\uff0c\u6c38\u8fdc\u6301\u4ed3\u4e0d\u4e86\u3002
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workbench_service.execution.errors import DemoApiError  # noqa: E402
from workbench_service.execution.protection import (  # noqa: E402
    ProtectionFailurePolicy,
    ProtectionManager,
)


class _StubOutbox:
    def __init__(self) -> None:
        self.prepared: list[dict] = []
        self.acked: list[str] = []
        self.rejected: list[tuple[str, str]] = []
        self.unknown: list[tuple[str, str]] = []

    def prepare(self, **kwargs):
        self.prepared.append(kwargs)
        return None

    def acknowledge(self, client_order_id: str, response):
        self.acked.append(client_order_id)
        return "ACKNOWLEDGED"

    def reject(self, client_order_id: str, exc: BaseException) -> None:
        self.rejected.append((client_order_id, str(exc)))

    def mark_unknown(self, client_order_id: str, exc: BaseException) -> None:
        self.unknown.append((client_order_id, str(exc)))


class _StubClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.params: dict | None = None

    def submit_algo_order(self, params: dict):
        self.params = dict(params)
        if self.error is not None:
            raise self.error
        return {"code": "200", "algoId": 1, "status": "NEW"}


def _place(mode: str | None, error: Exception | None = None):
    client = _StubClient(error)
    outbox = _StubOutbox()
    manager = ProtectionManager(client, outbox)
    manager.place(
        symbol="BTCUSDT",
        close_side="BUY",
        kind="STOP_MARKET",
        trigger="73825.0",
        client_algo_id="wbp-st-test",
        position_side="SHORT",
        position_mode=mode,
    )
    return client, outbox


class ProtectionPositionSideTests(unittest.TestCase):
    def test_one_way_account_does_not_send_position_side(self) -> None:
        client, outbox = _place("ONE_WAY")
        self.assertNotIn("positionSide", client.params or {})
        self.assertEqual(outbox.acked, ["wbp-st-test"])

    def test_both_mode_does_not_send_position_side(self) -> None:
        client, _ = _place("BOTH")
        self.assertNotIn("positionSide", client.params or {})

    def test_hedge_account_sends_position_side(self) -> None:
        client, _ = _place("HEDGE")
        self.assertEqual((client.params or {}).get("positionSide"), "SHORT")

    def test_unknown_mode_defaults_to_no_position_side(self) -> None:
        client, _ = _place(None)
        self.assertNotIn("positionSide", client.params or {})

    def test_rejection_marks_outbox_rejected_instead_of_leaving_submitting(self) -> None:
        error = DemoApiError(
            "-4061 position side mismatch",
            path="/fapi/v1/algoOrder",
            status_code=400,
            code=-4061,
            kind="rejected",
        )
        with self.assertRaises(DemoApiError):
            _place("ONE_WAY", error=error)
        client = _StubClient(error)
        outbox = _StubOutbox()
        manager = ProtectionManager(client, outbox)
        with self.assertRaises(DemoApiError):
            manager.place(
                symbol="BTCUSDT",
                close_side="BUY",
                kind="STOP_MARKET",
                trigger="73825.0",
                client_algo_id="wbp-st-reject",
                position_side="SHORT",
                position_mode="ONE_WAY",
            )
        self.assertEqual([row[0] for row in outbox.rejected], ["wbp-st-reject"])
        self.assertEqual(outbox.acked, [])

    def test_transport_error_marks_unknown(self) -> None:
        error = DemoApiError(
            "timeout",
            path="/fapi/v1/algoOrder",
            kind="unknown",
        )
        client = _StubClient(error)
        outbox = _StubOutbox()
        manager = ProtectionManager(client, outbox)
        with self.assertRaises(DemoApiError):
            manager.place(
                symbol="BTCUSDT",
                close_side="BUY",
                kind="STOP_MARKET",
                trigger="73825.0",
                client_algo_id="wbp-st-unknown",
                position_side="SHORT",
                position_mode="ONE_WAY",
            )
        self.assertEqual([row[0] for row in outbox.unknown], ["wbp-st-unknown"])
        self.assertEqual(outbox.rejected, [])


class AlgoStatusConvergenceTests(unittest.TestCase):
    """平仓后仍显示“在途保护单”的根因：algos 的 FINISHED 被当成未知状态。

    现场（2026-11-14）：平掉 SHORT 0.028 后，账本仍有 7 张 PROTECTION 订单停在 NEW，
    而交易所返回 algoStatus=FINISHED + actualOrderId（已触发并产生实际订单）。
    旧实现把 FINISHED 归到 ACKNOWLEDGED（活动态），于是永久显示“在途”。
    """

    def test_finished_algo_with_actual_order_is_terminal(self) -> None:
        from workbench_service.execution.outbox import TERMINAL_STATUSES, OrderOutbox

        status = OrderOutbox.status_from_response(
            {"algoStatus": "FINISHED", "actualOrderId": "2867916792", "quantity": "0.0280"}
        )
        self.assertEqual(status, "TRIGGERED")
        self.assertIn(status, TERMINAL_STATUSES, "触发完成必须落终态，不能留在活动态")

    def test_unrecognised_algo_status_still_falls_back_conservatively(self) -> None:
        from workbench_service.execution.outbox import OrderOutbox

        # 未识别的奇形状态仍按原来的保守做法，不臆造终态。
        self.assertEqual(
            OrderOutbox.status_from_response({"algoStatus": "WEIRD_STATE"}), "ACKNOWLEDGED"
        )


if __name__ == "__main__":
    unittest.main()


class _LostReceiptClient:
    """交易所已收单但回执丢失：POST 必定超时，openAlgoOrders 能看到已建立的那一张。"""

    def __init__(self) -> None:
        self.orders: list[dict] = []

    def submit_algo_order(self, params: dict):
        self.orders.append(dict(params))
        raise DemoApiError("timeout", path="/fapi/v1/algoOrder", kind="unknown")

    def open_algo_orders(self) -> list[dict]:
        if not self.orders:
            return []
        first = self.orders[0]
        return [
            {
                "algoId": 1,
                "clientAlgoId": first["clientAlgoId"],
                "symbol": first["symbol"],
                "side": first["side"],
                "orderType": first["type"],
                "triggerPrice": first["triggerPrice"],
                "algoStatus": "NEW",
            }
        ]


class ProtectionRetryIdentityTests(unittest.TestCase):
    """T03 §8·B3：补挂前先查已有保护，同一侧不会挂出第二张；重试保留原标识。"""

    def _manager(self, client) -> ProtectionManager:
        return ProtectionManager(
            client, _StubOutbox(), policy=ProtectionFailurePolicy(recheck_attempts=1)
        )

    def test_lost_receipt_is_proven_by_query_without_a_second_order(self) -> None:
        client = _LostReceiptClient()
        manager = self._manager(client)
        outcome = manager._place_verified(
            symbol="BTCUSDT",
            close_side="SELL",
            kind="STOP_MARKET",
            trigger="99000",
            position_side="LONG",
            position_mode="ONE_WAY",
            retry=1,
        )
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["proven_by"], "query")
        self.assertEqual(len(client.orders), 1, "回执丢失后先查询，不重复补挂")
        self.assertEqual(manager.outbox.acked, [client.orders[0]["clientAlgoId"]])
        self.assertTrue(client.orders[0]["clientAlgoId"].startswith("wbp-st-"))

    def test_retry_reuses_the_same_deterministic_identifier(self) -> None:
        first = _LostReceiptClient()
        second = _LostReceiptClient()
        for client in (first, second):
            self._manager(client)._place_verified(
                symbol="BTCUSDT",
                close_side="SELL",
                kind="STOP_MARKET",
                trigger="99000",
                position_side="LONG",
                position_mode="ONE_WAY",
                retry=1,
            )
        self.assertEqual(
            first.orders[0]["clientAlgoId"],
            second.orders[0]["clientAlgoId"],
            "同一意图重试必须复用原 clientAlgoId",
        )
