"""M1.4d clientOrderId \u552f\u4e00\u6027\u56de\u5f52\u3002

\u73b0\u573a\u4e8b\u6545\uff082026-11-10\uff09\uff1acycle_id \u5f62\u5982 ``cyc-20261110T161044Z-abc123``\uff0c
\u65e7\u5b9e\u73b0\u53d6\u524d 8 \u4e2a\u5b57\u7b26\u5f97\u5230\u6574\u65e5\u56fa\u5b9a\u7684 ``cyc20261``\uff0c\u4e8e\u662f\u540c\u4e00\u5929\u6bcf\u4e00\u8f6e\u90fd\u6d3e\u751f
\u540c\u4e00\u4e2a clientOrderId\uff1b\u7b2c\u4e8c\u8f6e\u8d77 P2 \u547d\u4e2d outbox \u5e42\u7b49\u5206\u652f\uff0c\u9759\u9ed8\u56de\u653e\u4e0a\u4e00\u7b14
\u8ba2\u5355\uff08\u754c\u9762\u663e\u793a\u201c\u6210\u4ea4\u201d\uff0c\u5b9e\u9645\u6ca1\u6709\u4e0b\u5355\uff09\u3002
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workbench_service.execution.broker import derive_client_order_id  # noqa: E402


class ClientOrderIdTests(unittest.TestCase):
    def test_same_day_cycles_get_distinct_ids(self) -> None:
        first = derive_client_order_id("cyc-20261110T161044Z-abc123", "BTCUSDT", "O")
        second = derive_client_order_id("cyc-20261110T171324Z-def456", "BTCUSDT", "O")
        self.assertNotEqual(first, second, "同一天不同轮次不能共享 clientOrderId")

    def test_same_cycle_is_deterministic(self) -> None:
        cycle = "cyc-20261110T161044Z-abc123"
        self.assertEqual(
            derive_client_order_id(cycle, "BTCUSDT", "O"),
            derive_client_order_id(cycle, "BTCUSDT", "O"),
        )

    def test_id_shape_and_length(self) -> None:
        cid = derive_client_order_id("cyc-20261110T161044Z-abc123", "BTCUSDT", "O", 1)
        self.assertTrue(cid.startswith("wb-"))
        self.assertIn("-BTC-O-1", cid)
        self.assertLessEqual(len(cid), 36, cid)
        self.assertRegex(cid, r"^[A-Za-z0-9-]+$")

    def test_short_cycle_ids_still_work(self) -> None:
        self.assertEqual(
            derive_client_order_id("r11", "BTCUSDT", "O"), "wb-r11-BTC-O-0"
        )

    def test_empty_cycle_id_falls_back_to_random(self) -> None:
        cid = derive_client_order_id("", "BTCUSDT", "O")
        self.assertTrue(cid.startswith("wb-m"))




class IntentIdentifierChainTests(unittest.TestCase):
    """T03 §8·B1：同一意图重试保留原 business_key/client_order_id。"""

    def setUp(self) -> None:
        import sys as _sys
        from pathlib import Path as _Path

        _sys.path.insert(0, str(_Path(__file__).resolve().parent))
        from support import ServiceCase, approve, submit

        self._approve = approve
        self._submit = submit
        self.case = ServiceCase(with_fake=True)
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_round_intent_and_exchange_ids_are_persisted(self) -> None:
        from decimal import Decimal

        from workbench_service.risk.models import OrderPlan

        cycle_id = "cyc-20261114T020304Z-chain1"
        response = self._submit(self.client, self._approve(self.client, cycle_id=cycle_id))
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["intent_id"], cycle_id)
        self.assertTrue(body["business_key"].startswith("ENTRY:BTCUSDT"))
        order = self.case.service.ledger.get_order(body["client_order_id"])
        self.assertEqual(order["cycle_id"], cycle_id)
        self.assertTrue(order["exchange_order_id"])
        self.assertEqual(
            Decimal(order["qty_filled"]), Decimal(str(body["qty_filled"]))
        )

    def test_same_intent_retry_never_generates_a_second_identifier(self) -> None:
        from decimal import Decimal

        from workbench_service.risk.models import OrderPlan

        broker = self.case.service.broker
        cycle_id = "cyc-20261114T020304Z-retry1"
        first = broker.open_market(
            OrderPlan.from_payload(
                {"symbol": "BTCUSDT", "side": "LONG", "leverage": 8, "qty": "0.02",
                 "sl": "99000", "tp": "102800"},
                cycle_id=cycle_id,
            ),
            Decimal("0.02"),
            cycle_id=cycle_id,
        )
        posts = len(self.case.fake.calls_for("/fapi/v1/order", "POST"))
        replay = broker.open_market(
            OrderPlan.from_payload(
                {"symbol": "BTCUSDT", "side": "LONG", "leverage": 8, "qty": "0.02",
                 "sl": "99000", "tp": "102800"},
                cycle_id=cycle_id,
            ),
            Decimal("0.02"),
            cycle_id=cycle_id,
        )
        self.assertEqual(replay["client_order_id"], first["client_order_id"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(
            len(self.case.fake.calls_for("/fapi/v1/order", "POST")),
            posts,
            "同一意图重试不得生成新标识再发一次",
        )

if __name__ == "__main__":
    unittest.main()
