"""P1/P2 主链路测试：approval_token、幂等、强制 SL/TP、平仓、AI 收紧。

验收映射：
- P1 返回 approval_token（HMAC/TTL/plan hash）；
- P2 校验 token/plan hash/TTL/单次：缺失 401、篡改 403、重放 409、计划不一致 409；
- 市价开仓强制 SL/TP（Algo 2 张）；平仓后仓位归零、保护单撤回；
- AI 只能收紧（R00）：放宽值被忽略，硬上限仍然生效。

T02（§8·A）：
- 同一计划只改 confidence，P1 的允许数量/杠杆上限与令牌绑定值完全一致；
- 模型数量绕不过后端计算：P2 实际下单数量 = min(令牌数量, 用提交时刷新事实重算的数量)；
- P2 用刷新后的 availableBalance 复算：余额下跌则缩小数量，归零则拒绝；
- 报告（envelope）标明 legacy strong_* 不用于执行授权。
"""

from __future__ import annotations

import base64
import json
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ServiceCase, approve, evaluate, plan, submit  # noqa: E402


def token_payload(token: str) -> dict:
    _version, body, _signature = token.split(".")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return json.loads(raw)


class P1P2FlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    # ------------------------------------------------------------------ P1
    def test_p1_returns_approval_token(self) -> None:
        response = evaluate(self.client)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        approval = body["approval"]
        self.assertTrue(approval["token"].startswith("v1."))
        self.assertEqual(approval["ttl_s"], 120)
        self.assertEqual(approval["approved_qty"], "0.02")
        self.assertEqual(approval["leverage"], 8)
        self.assertEqual(body["verdict"]["verdict"], "approved")
        self.assertEqual(body["verdict"]["tier"], "regular")
        self.assertEqual(body["context"]["equity_session"], "5000")
        self.assertTrue(body["plan"]["cycle_id"])

    def test_p1_ignores_forged_request_market(self) -> None:
        forged = {
            "mark": "1",
            "snapshot_age_ms": 0,
            "mark_move_60s_pct": 0,
            "mark_index_deviation_pct": 0,
            "spread_bps": 0,
        }
        response = evaluate(self.client, market_payload=forged)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["context"]["mark"], "100000")
        self.assertEqual(body["context"]["market_source"], "exchange")
        self.assertLessEqual(body["context"]["snapshot_age_ms"], 5000)

    def test_p1_fails_closed_when_server_facts_unavailable(self) -> None:
        self.fake.fail_market_facts = 5
        response = evaluate(self.client)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_MARKET_UNAVAILABLE")

    # ------------------------------------------------------------------ P2
    def test_p2_opens_market_position_with_forced_protection(self) -> None:
        approval = approve(self.client)
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertFalse(body["idempotent_replay"])
        self.assertEqual(body["status"], "FILLED")
        self.assertEqual(body["qty_filled"], "0.02")
        self.assertEqual(body["protection"]["status"], "OK")
        self.assertIsNotNone(body["protection"]["coverage"]["sl"])
        self.assertIsNotNone(body["protection"]["coverage"]["tp"])

        # 交易所侧：1 张普通单 + 2 张 Algo 保护单
        self.assertEqual(len(self.fake.orders), 1)
        self.assertEqual(len(self.fake.algos), 2)
        order_types = sorted(algo["orderType"] for algo in self.fake.algos.values())
        self.assertEqual(order_types, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertEqual(len(self.fake.positions), 1)

        status = self.client.get("/api/status").json()
        self.assertEqual(len(status["positions"]), 1)
        self.assertTrue(status["protection"]["ok"])
        self.assertTrue(status["protection"]["detail"]["BTCUSDT"]["status"], "OK")
        self.assertEqual(status["exchange"]["position_mode"], "ONE_WAY")
        self.assertEqual(status["equity"]["session"], "5000")

        positions = self.client.get("/api/positions").json()
        self.assertEqual(positions["count"], 1)
        self.assertEqual(positions["source"], "exchange")
        self.assertEqual(positions["positions"][0]["protection"]["status"], "OK")

        orders = self.client.get("/api/orders").json()
        purpose_by_cid = {row["client_order_id"]: row["purpose"] for row in orders["orders"]}
        self.assertIn("ENTRY", purpose_by_cid.values())
        self.assertEqual(sum(1 for value in purpose_by_cid.values() if value == "PROTECTION"), 2)

        curve = self.client.get("/api/equity-curve").json()
        self.assertGreaterEqual(curve["count"], 1)
        self.assertEqual(curve["points"][0]["equity_session"], "5000")

    def test_token_single_use(self) -> None:
        approval = approve(self.client)
        first = submit(self.client, approval)
        self.assertEqual(first.status_code, 201, first.text)
        second = submit(self.client, approval)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertEqual(second.json()["error"]["code"], "E_APPROVAL_REPLAY")
        self.assertEqual(len(self.fake.orders), 1, "重放不得产生第二张订单")

    def test_missing_token_is_401(self) -> None:
        response = self.client.post("/api/execution/order", json={})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_REQUIRED")

    def test_tampered_token_is_403(self) -> None:
        approval = approve(self.client)
        token = approval["approval"]["token"]
        version, body, signature = token.split(".")
        tampered = f"{version}.{body}.{signature[:-2]}xx"
        response = self.client.post("/api/execution/order", json={"approval_token": tampered})
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_INVALID")
        self.assertEqual(len(self.fake.orders), 0)

    def test_plan_mismatch_is_409(self) -> None:
        approval = approve(self.client)
        other = plan(qty="0.03")
        response = submit(self.client, approval, plan_payload=other)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_PLAN_MISMATCH")
        self.assertEqual(len(self.fake.orders), 0)

    def test_retry_same_cycle_is_idempotent(self) -> None:
        # 同一 cycle 的重复提交在 broker 层必须命中 client_order_id 幂等
        # （API 层在已持仓时会被 R02 挡住，这里直接验证执行器语义）。
        from decimal import Decimal

        from workbench_service.risk.models import OrderPlan

        service = self.case.service
        service.ensure_started()
        plan_obj = OrderPlan.from_payload(plan(), cycle_id="cycle-dup")
        first = service.broker.open_market(plan_obj, Decimal("0.02"), cycle_id="cycle-dup")
        self.assertFalse(first["idempotent_replay"])
        second = service.broker.open_market(plan_obj, Decimal("0.02"), cycle_id="cycle-dup")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(first["client_order_id"], second["client_order_id"])
        self.assertEqual(len(self.fake.orders), 1, "同一 cycle 只允许一笔真实订单")

    def test_opened_position_persists_live_sl_tp(self) -> None:
        # M1.4e：保护单挂上后，账本 positions 行必须记录真实 SL/TP，
        # 否则驾驶舱/复盘会显示"无保护"（交易所其实有）。
        from decimal import Decimal

        from workbench_service.risk.models import OrderPlan

        service = self.case.service
        service.ensure_started()
        plan_obj = OrderPlan.from_payload(plan(), cycle_id="cycle-sl-tp")
        service.broker.open_market(plan_obj, Decimal("0.02"), cycle_id="cycle-sl-tp")
        rows = [r for r in service.ledger.list_positions() if r["symbol"] == "BTCUSDT"]
        self.assertTrue(rows, "开仓后账本应有持仓行")
        self.assertIsNotNone(rows[0]["sl_price"], "sl_price 必须写入")
        self.assertIsNotNone(rows[0]["tp_price"], "tp_price 必须写入")
        self.assertEqual(rows[0]["protection_status"], "OK")

    def test_open_while_position_exists_is_rejected(self) -> None:
        first = approve(self.client, cycle_id="c1")
        self.assertEqual(submit(self.client, first).status_code, 201)
        second = evaluate(self.client, cycle_id="c1")
        self.assertEqual(second.status_code, 422)
        self.assertEqual(second.json()["error"]["code"], "E_RISK_SYMBOL_POSITION")

    # ------------------------------------------------------------------ R00
    def test_ai_cannot_loosen_hard_limits(self) -> None:
        loosened = plan(qty="0.2", ai_limits={"max_leverage": 50, "max_risk_pct": 0.5})
        response = evaluate(self.client, plan_payload=loosened)
        self.assertEqual(response.status_code, 200, response.text)
        verdict = response.json()["verdict"]
        self.assertEqual(verdict["verdict"], "reduced")
        # 1% × 5000 = 50U 风险 / 1000U 每单位 = 0.05
        self.assertEqual(verdict["approved_qty"], "0.05")
        rules = [item["rule"] for item in verdict["violations"]]
        self.assertIn("R00", rules)
        self.assertIn("R05", rules)

        submit_response = submit(self.client, response.json())
        self.assertEqual(submit_response.status_code, 201, submit_response.text)
        from decimal import Decimal

        self.assertEqual(
            Decimal(submit_response.json()["qty_requested"]), Decimal("0.05")
        )
        self.assertEqual(len(self.fake.orders), 1)

    def test_ai_may_tighten(self) -> None:
        tightened = plan(ai_limits={"max_risk_pct": 0.002})
        response = evaluate(self.client, plan_payload=tightened)
        self.assertEqual(response.status_code, 200, response.text)
        verdict = response.json()["verdict"]
        self.assertEqual(verdict["verdict"], "reduced")
        self.assertEqual(verdict["approved_qty"], "0.01")  # 5000×0.2% / 1000 = 0.01

    # ------------------------------------------------------------------ §8·A 权限收口
    def test_confidence_does_not_change_approved_qty_or_cap(self) -> None:
        bodies: dict[float, dict] = {}
        for confidence in (0.1, 0.5, 0.72, 0.95):
            response = evaluate(
                self.client,
                cycle_id=f"conf-{confidence}",
                plan_payload=plan(confidence=confidence, qty="0.10"),
            )
            self.assertEqual(response.status_code, 200, response.text)
            bodies[confidence] = response.json()

        reference = bodies[0.1]["verdict"]
        for confidence, body in bodies.items():
            verdict = body["verdict"]
            # 普通档 1% × 5000 = 50U；|入场−止损|=1000U → 0.05（旧强信心档会给 0.10）
            self.assertEqual(verdict["approved_qty"], "0.05", confidence)
            self.assertEqual(body["approval"]["approved_qty"], "0.05", confidence)
            self.assertEqual(verdict["tier"], "regular")
            self.assertEqual(verdict["leverage"], reference["leverage"])
            self.assertEqual(verdict["leverage_cap"], 10)
            self.assertEqual(verdict["sizing"]["confidence_used"], "false")

        payload = token_payload(bodies[0.1]["approval"]["token"])
        self.assertEqual(payload["approved_qty"], "0.05")
        self.assertEqual(payload["sizing"]["approved_qty"], "0.05")
        self.assertEqual(payload["sizing"]["binding_constraint"], "R05_risk_budget")
        self.assertEqual(payload["leverage_cap"], 10)
        self.assertFalse(payload["confidence_used_for_sizing"])
        self.assertEqual(payload["plan"]["confidence"], 0.1)

    def test_model_qty_cannot_bypass_backend_qty(self) -> None:
        approval = approve(self.client, plan_payload=plan(qty="0.20"))
        self.assertEqual(approval["verdict"]["approved_qty"], "0.05")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        # 实际下单数量来自后端计算，模型申报的 0.20 被风险预算截断。
        self.assertEqual(Decimal(body["qty_requested"]), Decimal("0.05"))
        self.assertEqual(len(self.fake.orders), 1)
        order = next(iter(self.fake.orders.values()))
        self.assertEqual(Decimal(order["origQty"]), Decimal("0.05"))

    def _case_with_clock(self) -> tuple[ServiceCase, list[float]]:
        """可推进时钟的独立 case：越过 broker 的 2s 快照缓存，模拟 P1→P2 之间的账户变化。"""

        clock = [time.time()]
        case = ServiceCase(with_fake=True, clock=lambda: clock[0])
        return case, clock

    def test_p2_recomputes_with_refreshed_available_balance(self) -> None:
        case, clock = self._case_with_clock()
        try:
            fake = case.fake
            client = case.client()
            with client:
                approval = approve(client, plan_payload=plan(qty="0.20"))
                self.assertEqual(approval["verdict"]["approved_qty"], "0.05")
                # P1 之后交易所可用保证金下跌：P2 必须用刷新后的余额重算，不能沿用令牌里的 0.05。
                fake.available_override = Decimal("50")
                clock[0] += 3.0  # 越过 2s 快照缓存 → P2 重新读取账户/持仓/挂单
                response = submit(client, approval)
                self.assertEqual(response.status_code, 201, response.text)
                self.assertEqual(Decimal(response.json()["qty_requested"]), Decimal("0.004"))
                order = next(iter(fake.orders.values()))
                self.assertEqual(Decimal(order["origQty"]), Decimal("0.004"))
        finally:
            case.close()

    def test_p2_rejects_when_available_balance_disappears(self) -> None:
        case, clock = self._case_with_clock()
        try:
            fake = case.fake
            client = case.client()
            with client:
                approval = approve(client, plan_payload=plan(qty="0.20"))
                fake.available_override = Decimal("0")
                clock[0] += 3.0
                response = submit(client, approval)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "E_RISK_MARGIN")
                self.assertEqual(len(fake.orders), 0, "余额不足时不得下单")
        finally:
            case.close()

    def test_envelope_marks_legacy_strong_fields_as_non_authoritative(self) -> None:
        body = self.client.get("/api/risk/envelope").json()
        limits = body["limits"]
        # 旧字段仍可读（旧配置/旧报告不崩）……
        self.assertEqual(limits["per_trade"]["strong_max_risk_pct"], 0.02)
        self.assertEqual(limits["per_trade"]["strong_max_leverage"], 15)
        self.assertEqual(limits["per_trade"]["max_leverage"], 10)
        # ……但序列化/报告标明不用于执行授权。
        self.assertFalse(limits["legacy_strong_tier"]["used_for_execution_authorization"])
        self.assertFalse(limits["legacy_strong_tier"]["confidence_used_for_sizing"])
        self.assertFalse(limits["sizing_policy"]["confidence_used"])
        self.assertEqual(limits["sizing_policy"]["policy"], "deterministic")
        self.assertFalse(body["legacy_strong_tier"]["used_for_execution_authorization"])
        rules = {row["rule_id"]: row for row in body["rules"]}
        self.assertNotIn("强信心", rules["R04"]["summary"])
        self.assertIn("普通档上限", rules["R04"]["summary"])

    # ------------------------------------------------------------------ 平仓
    def test_close_flattens_and_cancels_protection(self) -> None:
        approval = approve(self.client)
        opened = submit(self.client, approval)
        self.assertEqual(opened.status_code, 201, opened.text)

        closed = self.client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
        self.assertEqual(closed.status_code, 200, closed.text)
        body = closed.json()
        self.assertTrue(body["closed"])
        self.assertEqual(body["protection_canceled"], 2)

        self.assertEqual(self.fake.positions, {})
        self.assertEqual(self.fake._open_algos(), [])
        status = self.client.get("/api/status").json()
        self.assertEqual(status["positions"], [])
        self.assertEqual(status["equity"]["session"], "5000")

    def test_close_without_position_is_idempotent(self) -> None:
        response = self.client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "NO_POSITION")


if __name__ == "__main__":
    unittest.main()
