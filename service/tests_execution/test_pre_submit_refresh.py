"""T03/A·提交前硬刷新：价格与可用余额必须绕过缓存/老化后重算，失败即阻断。

背景（队长补充的 A 项执行侧落点）：risk 侧只有 R14 ≤15s 老化判断，而执行层
broker 的快照缓存 TTL=2s；P2 复算若吃缓存，可用余额最多陈旧 2 秒。这里验证
真正提交前一定重新读账户/余额与即时行情，并重跑限额与 sizing。
"""

from __future__ import annotations

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, submit  # noqa: E402


class PreSubmitRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_cached_available_balance_cannot_be_used_for_submit(self) -> None:
        approval = approve(self.client, cycle_id="refresh-balance")
        cached = Decimal(self.service.broker._cache.account["availableBalance"])
        self.assertGreater(cached, Decimal("1000"), "P1 之后缓存里应是旧的高余额")
        # 可用余额在缓存有效期内下降：不得用 2 秒前的缓存值作为提交依据
        self.fake.available_override = Decimal("50")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(Decimal(body["pre_submit_refresh"]["available_margin"]), Decimal("50"))
        self.assertEqual(Decimal(body["qty_requested"]), Decimal("0.004"))
        self.assertEqual(
            self.fake.positions["BTCUSDT"]["qty"], Decimal("0.004"),
            "实际下单数量必须来自刷新后的可用余额",
        )

    def test_p2_refresh_uses_the_live_price_not_the_aged_token_price(self) -> None:
        approval = approve(self.client, cycle_id="refresh-price")
        # 价格在 P1 之后跌破止损：token 里的 100000 已过期，刷新后必须拒绝
        self.fake.mark_price = Decimal("98000")
        self.fake.index_price = Decimal("98000")
        self.fake.bid_price = Decimal("97995")
        self.fake.ask_price = Decimal("98005")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 422, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_RISK_SLTP")
        self.assertEqual(error["details"]["stage"], "P2_REFRESH")
        self.assertEqual(Decimal(error["details"]["refreshed_mark"]), Decimal("98000"))
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 0)

    def test_refresh_failure_blocks_the_entry(self) -> None:
        approval = approve(self.client, cycle_id="refresh-account-down")
        self.fake.fail_account = 10
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 503, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], "E_EXECUTION_REFRESH_FAILED")
        self.assertEqual(error["details"]["reason"], "exchange_unavailable")
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 0)
        # 刷新发生在 outbox prepare 之前：不留 SUBMITTING 孤儿行
        self.assertEqual(self.service.outbox.counts().get("submitting", 0), 0)
        self.assertEqual(self.service.ledger.list_orders(limit=10), [])

    def test_market_refresh_failure_blocks_the_entry(self) -> None:
        approval = approve(self.client, cycle_id="refresh-market-down")
        self.fake.fail_market_facts = 10
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_MARKET_UNAVAILABLE")
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 0)

    def test_refresh_reruns_limits_and_reduces_qty(self) -> None:
        approval = approve(self.client, cycle_id="refresh-limits")
        self.assertEqual(Decimal(approval["approval"]["approved_qty"]), Decimal("0.02"))
        self.fake.available_override = Decimal("120")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["pre_submit_refresh"]["rechecked"])
        self.assertLess(Decimal(body["qty_requested"]), Decimal("0.02"))
        self.assertEqual(
            Decimal(body["qty_requested"]),
            self.fake.positions["BTCUSDT"]["qty"],
        )


if __name__ == "__main__":
    unittest.main()
