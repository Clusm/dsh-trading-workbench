"""失败路径测试：保护失败即平仓、交易所拒绝、UNKNOWN 只查询不重发、部分成交、对账。

验收映射：
- 保护失败即平仓（绝不留裸仓）；
- Outbox UNKNOWN 只查询、不重发，并阻断后续新开仓；
- 查询失败（I5）不被当成"没有保护"去平仓；
- 部分成交按真实仓位挂保护。
"""

from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import (  # noqa: E402
    ServiceCase,
    approve,
    evaluate,
    plan,
    seed_evidence,
    submit,
)

from workbench_service.execution.client import DemoBinanceClient  # noqa: E402
from workbench_service.execution.credentials import DemoCredentials  # noqa: E402
from workbench_service.execution.outbox import OrderOutbox  # noqa: E402
from workbench_service.execution.protection import (  # noqa: E402
    ProtectionCoverage,
    ProtectionResult,
    coverage_from_rows,
)
from workbench_service.risk.config import RiskLimits  # noqa: E402
from workbench_service.risk.models import OrderPlan, RiskVerdict  # noqa: E402


class FailurePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def _open(self):
        approval = approve(self.client, cycle_id="open-1")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ------------------------------------------------------------------ 保护失败
    def test_protection_failure_triggers_emergency_close(self) -> None:
        self.fake.protection_failures = 2  # SL 的第一次 + 重试都失败
        approval = approve(self.client, cycle_id="prot-fail")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["protection"]["status"], "PROTECTION_FAILED")
        self.assertIsNotNone(body["emergency_close"])
        self.assertTrue(body["emergency_close"]["closed"])
        self.assertEqual(self.fake.positions, {}, "保护失败后必须已平仓")
        self.assertEqual(len(self.fake._open_algos()), 0)
        events = [row["event_type"] for row in self.service.ledger.list_risk_events(50)]
        self.assertIn("protection_incomplete", events)

    def test_protection_place_retry_once(self) -> None:
        self.fake.protection_failures = 1  # 第一次失败，重试成功
        approval = approve(self.client, cycle_id="prot-retry")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["protection"]["status"], "OK")
        self.assertEqual(len(self.fake._open_algos()), 2)

    # ------------------------------------------------------------------ 拒绝
    def test_exchange_rejection_is_terminal(self) -> None:
        self.fake.reject_orders = 1
        approval = approve(self.client, cycle_id="reject-1")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_EXCHANGE_REJECT")
        self.assertEqual(self.fake.positions, {})
        rejected = self.service.ledger.list_orders(status="REJECTED")
        self.assertEqual(len(rejected), 1)
        self.assertEqual(rejected[0]["binance_code"], -2019)
        # 明确拒绝是终态：不产生 UNKNOWN，下一轮 P1 不被 R17 阻断
        self.assertEqual(self.service.outbox.counts().get("unknown", 0), 0)
        self.assertEqual(approve(self.client, cycle_id="reject-2")["verdict"]["verdict"], "approved")

    # ------------------------------------------------------------------ UNKNOWN
    def test_unknown_order_is_queried_not_reposted(self) -> None:
        self.fake.timeout_orders = 1
        approval = approve(self.client, cycle_id="unknown-1")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_FILL_UNCONFIRMED")
        cid = response.json()["error"]["details"]["client_order_id"]

        # 只发过一次 POST；之后只能查询（fake 侧查询计数 > 0）
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 1)
        self.assertGreaterEqual(len(self.fake.calls_for("/fapi/v1/order", "GET")), 1)

        # 未决订单阻断新开仓（R17）
        blocked = evaluate(
            self.client,
            cycle_id="unknown-2",
            plan_payload={
                "symbol": "BTCUSDT", "side": "LONG", "leverage": 8, "qty": "0.02",
                "sl": "99000", "tp": "102800",
            },
        )
        self.assertEqual(blocked.status_code, 422)
        self.assertEqual(blocked.json()["error"]["code"], "E_OUTBOX_PENDING")

        # 同一 token 重放仍被拒（单次）
        again = submit(self.client, approval)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["error"]["code"], "E_APPROVAL_REPLAY")

        # 对账：交易所查不到该订单（-2013）→ 保持 UNKNOWN，不发新单
        result = self.client.post("/api/execution/reconcile").json()
        self.assertEqual(result["outbox"]["checked"], 1)
        self.assertEqual(result["outbox"]["resolved"], 0)
        self.assertEqual(self.service.outbox.get(cid)["status"], "UNKNOWN")
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 1)

    def test_unknown_but_created_is_recovered_by_query(self) -> None:
        self.fake.timeout_after_create = 1
        approval = approve(self.client, cycle_id="unknown-2")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["recovered_from_unknown"])
        self.assertEqual(body["status"], "FILLED")
        self.assertEqual(self.fake.positions["BTCUSDT"]["qty"], Decimal("0.02"))
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 1)

    # ------------------------------------------------------------------ 部分成交
    def test_partial_fill_uses_real_position_for_protection(self) -> None:
        self.fake.partial_fill = True
        approval = approve(self.client, cycle_id="partial-1")
        response = submit(self.client, approval)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "PARTIALLY_FILLED")
        self.assertEqual(Decimal(body["qty_filled"]), Decimal("0.01"))
        self.assertEqual(body["protection"]["status"], "OK")
        self.assertEqual(self.fake.positions["BTCUSDT"]["qty"], Decimal("0.01"))
        self.assertEqual(len(self.fake._open_algos()), 2)

    # ------------------------------------------------------------------ I5
    def test_algo_query_failure_does_not_flatten(self) -> None:
        self._open()
        # 连续查询失败：必须保持 UNKNOWN，绝不推断无保护。
        # T03 起保护覆盖有有限回查（每次 GET 客户端自身还会重试一次），
        # 所以失败次数要足够覆盖“快照 + 回查”的查询预算。
        self.fake.fail_algo_query = 20
        response = self.client.post("/api/protection/repair", json={})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["repaired"][0]["status"], "UNKNOWN")
        # 查询失败 ≠ 没有保护：绝不因此平仓
        self.assertEqual(len(self.fake.positions), 1)
        self.assertEqual(len(self.fake._open_algos()), 2)

    def test_repair_adds_fallback_protection_for_adopted_position(self) -> None:
        self.fake.add_position("BTCUSDT", "LONG", "0.01", "100000")
        # 首次请求触发的只读启动对账会收养交易所已有仓位（不自动挂保护）
        self.client.post("/api/execution/reconcile")
        startup_adopted = self.service.startup["reconcile"]["adopted"]
        self.assertEqual(startup_adopted, ["BTCUSDT"])
        status = self.client.get("/api/status").json()
        self.assertFalse(status["protection"]["ok"])
        self.assertIn("BTCUSDT", status["protection"]["missing"])

        repaired = self.client.post("/api/protection/repair", json={}).json()
        self.assertTrue(repaired["ok"], repaired)
        self.assertEqual(len(self.fake._open_algos()), 2)
        status = self.client.get("/api/status").json()
        self.assertTrue(status["protection"]["ok"])

    def test_repair_failure_does_not_auto_close(self) -> None:
        self.fake.add_position("BTCUSDT", "LONG", "0.01", "100000")
        self.client.post("/api/execution/reconcile")
        self.fake.protection_failures = 2
        repaired = self.client.post("/api/protection/repair", json={}).json()
        self.assertFalse(repaired["ok"])
        self.assertEqual(len(self.fake.positions), 1, "修复失败只告警，不自动平仓")

    # ------------------------------------------------------------------ 平仓 UNKNOWN
    def test_close_unknown_keeps_position(self) -> None:
        self._open()
        self.fake.timeout_orders = 1
        response = self.client.post("/api/execution/close", json={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["error"]["code"], "E_FILL_UNCONFIRMED")
        self.assertEqual(len(self.fake.positions), 1)
        self.assertEqual(self.service.outbox.counts().get("unknown", 0), 1)

    # ------------------------------------------------------------------ outbox 单元
    def test_outbox_reconcile_never_posts(self) -> None:
        outbox = OrderOutbox(self.service.ledger)
        outbox.prepare(
            client_order_id="unit-cid-1",
            purpose="ENTRY",
            symbol="BTCUSDT",
            side="BUY",
            position_side=None,
            order_type="MARKET",
            endpoint="/fapi/v1/order",
            request={"symbol": "BTCUSDT"},
            qty="0.01",
        )
        outbox.mark_unknown("unit-cid-1", RuntimeError("timeout"))
        before = len(self.fake.calls_for("/fapi/v1/order", "POST"))
        result = outbox.reconcile(lambda row: None)
        self.assertEqual(result, {"checked": 1, "resolved": 0, "errors": 0})
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), before)
        self.assertEqual(outbox.get("unit-cid-1")["status"], "UNKNOWN")

        resolved = outbox.reconcile(lambda row: {"orderId": 1, "status": "FILLED"})
        self.assertEqual(resolved["resolved"], 1)
        self.assertEqual(outbox.get("unit-cid-1")["status"], "FILLED")


# ======================================================================
# t24 repair regressions (t21 findings B02/H01/H02/H03/M01–M05/L01)
# ======================================================================

def _algo_row(symbol: str, order_type: str, price: str, *, position_side: str = "LONG") -> dict:
    return {
        "symbol": symbol,
        "position_side": position_side,
        "order_type": order_type,
        "trigger_price": Decimal(price),
        "quantity": Decimal("0"),
    }


class CoverageIsolationTests(unittest.TestCase):
    """H01：coverage_from_rows 必须先按 symbol（HEDGE 再按 positionSide）过滤。"""

    def test_other_symbol_algos_do_not_count(self) -> None:
        rows = [
            _algo_row("ETHUSDT", "STOP_MARKET", "3000"),
            _algo_row("ETHUSDT", "TAKE_PROFIT_MARKET", "3200"),
        ]
        coverage = coverage_from_rows("BTCUSDT", rows)
        self.assertEqual(coverage.status, "MISSING")
        self.assertEqual(coverage.stop_count, 0)

    def test_each_symbol_computed_independently(self) -> None:
        rows = [
            _algo_row("BTCUSDT", "STOP_MARKET", "99000"),
            _algo_row("BTCUSDT", "TAKE_PROFIT_MARKET", "102800"),
            _algo_row("ETHUSDT", "STOP_MARKET", "3000"),
        ]
        btc = coverage_from_rows("BTCUSDT", rows, position_side="LONG")
        eth = coverage_from_rows("ETHUSDT", rows, position_side="LONG")
        self.assertEqual(btc.status, "OK")
        self.assertEqual(eth.status, "PARTIAL")

    def test_hedge_position_side_filter(self) -> None:
        rows = [
            _algo_row("BTCUSDT", "STOP_MARKET", "99000", position_side="SHORT"),
            _algo_row("BTCUSDT", "TAKE_PROFIT_MARKET", "97000", position_side="SHORT"),
        ]
        self.assertEqual(
            coverage_from_rows("BTCUSDT", rows, position_side="LONG").status, "MISSING"
        )
        self.assertEqual(
            coverage_from_rows("BTCUSDT", rows, position_side="SHORT").status, "OK"
        )

    def test_snapshot_isolated_across_symbols(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            fake = case.fake
            fake.add_position("BTCUSDT", "LONG", "0.01", "100000")
            fake.add_position("ETHUSDT", "LONG", "0.1", "3000")
            fake.add_algo("ETHUSDT", "STOP_MARKET", "2900")
            fake.add_algo("ETHUSDT", "TAKE_PROFIT_MARKET", "3300")
            client = case.client()
            with client:
                status = client.get("/api/status").json()
            detail = status["protection"]["detail"]
            self.assertEqual(detail["BTCUSDT"]["status"], "MISSING")
            self.assertEqual(detail["ETHUSDT"]["status"], "OK")
            self.assertEqual(status["protection"]["missing"], ["BTCUSDT"])
        finally:
            case.close()


class ProtectionEscalationTests(unittest.TestCase):
    """H02：除 UNKNOWN 外，任何 not-ok 保护状态都必须紧急平仓。"""

    def _case(self) -> ServiceCase:
        case = ServiceCase(with_fake=True)
        case.service.ensure_started()
        return case

    def _stub(self, status: str, *, error: str | None = None):
        def _ensure(**kwargs):
            coverage = ProtectionCoverage(
                kwargs["symbol"],
                "UNKNOWN" if status == "UNKNOWN" else status,
                error=error,
            )
            return ProtectionResult(
                False, status, coverage, failed_kind=None if status == "UNKNOWN" else status
            )

        return _ensure

    def test_every_non_ok_status_escalates(self) -> None:
        for status in ("MISMATCH", "PARTIAL", "DUPLICATE", "MISSING", "PROTECTION_FAILED"):
            with self.subTest(status=status):
                case = self._case()
                try:
                    order_plan = OrderPlan.from_payload(plan(), cycle_id=f"h02-{status}")
                    with mock.patch.object(
                        case.service.broker.protection, "ensure", side_effect=self._stub(status)
                    ):
                        result = case.service.broker.open_market(
                            order_plan, Decimal("0.02"), cycle_id=f"h02-{status}"
                        )
                    self.assertEqual(result["protection"]["status"], status)
                    self.assertTrue(result["emergency_close"]["closed"], result)
                    self.assertEqual(case.fake.positions, {})
                finally:
                    case.close()

    def test_unknown_does_not_flatten(self) -> None:
        case = self._case()
        try:
            order_plan = OrderPlan.from_payload(plan(), cycle_id="h02-unknown")
            with mock.patch.object(
                case.service.broker.protection,
                "ensure",
                side_effect=self._stub("UNKNOWN", error="fetch failed"),
            ):
                result = case.service.broker.open_market(
                    order_plan, Decimal("0.02"), cycle_id="h02-unknown"
                )
            self.assertEqual(result["protection"]["status"], "UNKNOWN")
            self.assertIsNone(result["emergency_close"])
            self.assertEqual(len(case.fake.positions), 1, "查询失败不得平仓")
            self.assertEqual(len(case.fake._open_algos()), 0, "也不得重挂")
        finally:
            case.close()

    def test_close_failure_blocks_new_entries(self) -> None:
        case = self._case()
        try:
            with mock.patch.object(
                case.service.broker.protection, "ensure", side_effect=self._stub("MISMATCH")
            ), mock.patch.object(
                case.service.broker, "close_market", side_effect=RuntimeError("close failed")
            ):
                client = case.client()
                with client:
                    response = submit(client, approve(client, cycle_id="h02-close-fail"))
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertEqual(
                        response.json()["error"]["code"], "E_PROTECTION_CLOSE_FAILED"
                    )
                    status = client.get("/api/status").json()
                    self.assertEqual(status["control"]["state"], "close_only")
                    blocked = evaluate(client, cycle_id="after-fail", plan_payload=plan())
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertEqual(blocked.json()["error"]["code"], "E_CONTROL_LOCKED")
        finally:
            case.close()


class FillProofTests(unittest.TestCase):
    """H03：零成交必须由订单终态证明；无法证明时 fail-closed。"""

    def test_terminal_zero_fill_is_proven(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.cancel_no_fill = 1
            client = case.client()
            with client:
                response = submit(client, approve(client, cycle_id="zero-fill"))
            self.assertEqual(response.status_code, 201, response.text)
            body = response.json()
            self.assertTrue(body["zero_fill"])
            self.assertEqual(body["proof"], "order_terminal_zero_fill")
            self.assertEqual(case.fake.positions, {})
            self.assertEqual(body["protection"]["status"], "NOT_APPLICABLE")
        finally:
            case.close()

    def test_position_lag_is_retried(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.hide_position_once = 1  # 第一次 positionRisk 看不到仓位
            client = case.client()
            with client:
                response = submit(client, approve(client, cycle_id="lag"))
            self.assertEqual(response.status_code, 201, response.text)
            body = response.json()
            self.assertFalse(body["zero_fill"])
            self.assertEqual(body["protection"]["status"], "OK")
            self.assertEqual(len(case.fake.positions), 1)
        finally:
            case.close()

    def test_unprovable_position_fails_closed(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="unproven")
                # 订单提交后 positionRisk 持续失败：仓位/成交无法证明
                case.fake.fail_position_after_order = 12
                response = submit(client, approval)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertEqual(response.json()["error"]["code"], "E_POSITION_UNPROVEN")
                status = client.get("/api/status").json()
                self.assertEqual(status["control"]["state"], "close_only")
                case.fake.fail_position_after_order = 0  # 恢复读取，验证阻断来自控制态
                case.service.broker._cache = None  # 丢掉失败期间缓存的 stale 快照
                blocked = evaluate(client, cycle_id="unproven-2", plan_payload=plan())
                self.assertEqual(blocked.status_code, 422)
                self.assertEqual(blocked.json()["error"]["code"], "E_CONTROL_LOCKED")
        finally:
            case.close()


class MarketFactBindingTests(unittest.TestCase):
    """B02：P2 只接受 token 内服务端事实并强制老化；请求体事实无效。"""

    def test_p2_ignores_forged_request_market(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="forge-p2")
                response = submit(
                    client,
                    approval,
                    market_payload={
                        "mark": "1",
                        "snapshot_age_ms": 0,
                        "mark_move_60s_pct": 0,
                        "mark_index_deviation_pct": 0,
                        "spread_bps": 0,
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(case.fake.positions["BTCUSDT"]["entry"], Decimal("100000.0"))
        finally:
            case.close()

    def test_token_snapshot_ages_out(self) -> None:
        clock = [1_000_000.0]
        case = ServiceCase(with_fake=True, clock=lambda: clock[0])
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="age")
                clock[0] += 20.0  # 超过 entry_snapshot_max_age_ms=15s
                response = submit(client, approval)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_MARKET_STALE")
            self.assertEqual(response.json()["error"]["details"]["rule"], "R14")
        finally:
            case.close()

    def test_rules_version_change_invalidates_token(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.ensure_started()
            facts = service.broker.market_facts("BTCUSDT")
            verdict = RiskVerdict(verdict="approved", approved_qty=Decimal("0.02"), leverage=8)
            issued = service.approvals.issue(
                OrderPlan.from_payload(plan(), cycle_id="rules"),
                verdict,
                context={"market": facts, "evidence_age_ms": 0, "rules_version": "risk.v1:stale"},
            )
            client = case.client()
            with client:
                response = client.post(
                    "/api/execution/order", json={"approval_token": issued["token"]}
                )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_RULES_CHANGED")
        finally:
            case.close()

    def test_market_hash_mismatch_rejected_even_with_valid_signature(self) -> None:
        import base64
        import json as _json

        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.ensure_started()
            facts = service.broker.market_facts("BTCUSDT")
            verdict = RiskVerdict(verdict="approved", approved_qty=Decimal("0.02"), leverage=8)
            issued = service.approvals.issue(
                OrderPlan.from_payload(plan(), cycle_id="hash-bind"),
                verdict,
                context={
                    "market": facts,
                    "evidence_age_ms": 0,
                    "rules_version": service.risk.rules_version(),
                },
            )
            version, body, _sig = issued["token"].split(".")
            payload = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            payload["market"]["mark"] = "1"  # 保留原 market_hash，仅改事实
            forged = service.approvals._sign(payload)
            client = case.client()
            with client:
                response = client.post(
                    "/api/execution/order", json={"approval_token": forged}
                )
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_INVALID")
            self.assertEqual(
                response.json()["error"]["details"].get("reason"), "market_hash_mismatch"
            )
        finally:
            case.close()

    def test_tampered_token_market_is_rejected(self) -> None:
        import base64
        import json as _json

        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="tamper")
            token = approval["approval"]["token"]
            version, body, _sig = token.split(".")
            raw = _json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
            raw["market"]["mark"] = "1"
            forged_body = base64.urlsafe_b64encode(
                _json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
            ).decode().rstrip("=")
            forged = f"{version}.{forged_body}.{_sig}"
            with client:
                response = client.post("/api/execution/order", json={"approval_token": forged})
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_INVALID")
        finally:
            case.close()


class RiskIntegrationTests(unittest.TestCase):
    """M01/M05：trades_symbol_today、连亏冷却、availableBalance 与逐 symbol MMR。"""

    def test_trades_symbol_today_counts_entries(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                self.assertEqual(submit(client, approve(client, cycle_id="t1")).status_code, 201)
                self.assertTrue(
                    client.post("/api/execution/close", json={"symbol": "BTCUSDT"}).json()["closed"]
                )
                response = evaluate(client, cycle_id="t2")
            self.assertEqual(response.status_code, 200, response.text)
            context = response.json()["context"]
            self.assertEqual(context["trades_today"], 1)
            self.assertEqual(context["trades_symbol_today"], 1)
        finally:
            case.close()

    def test_loss_streak_activates_cooldown(self) -> None:
        limits = RiskLimits(
            enable_loss_streak=True,
            consecutive_losses=2,
            consecutive_loss_cooldown_hours=4,
        )
        case = ServiceCase(with_fake=True, limits=limits)
        try:
            client = case.client()
            with client:
                for index, (entry, mark) in enumerate(
                    ((Decimal("100000"), Decimal("99000")), (Decimal("99000"), Decimal("98000")))
                ):
                    case.fake.mark_price = entry
                    payload = plan(sl=str(entry - 1000), tp=str(entry + 2000))
                    self.assertEqual(
                        submit(client, approve(client, cycle_id=f"loss-{index}", plan_payload=payload)).status_code,
                        201,
                    )
                    case.fake.mark_price = mark
                    closed = client.post("/api/execution/close", json={"symbol": "BTCUSDT"}).json()
                    self.assertTrue(closed["closed"], closed)
                status = client.get("/api/status").json()
                self.assertGreaterEqual(status["risk"]["consecutive_losses"], 2)
                self.assertTrue(case.service.state.cooldown_active())
                case.fake.mark_price = Decimal("98000")
                blocked = evaluate(
                    client, cycle_id="loss-3", plan_payload=plan(sl="97000", tp="101000")
                )
            self.assertEqual(blocked.status_code, 422, blocked.text)
            self.assertEqual(blocked.json()["error"]["code"], "E_RISK_LOSS_STREAK")
        finally:
            case.close()

    def test_r07_uses_available_balance(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.available_override = Decimal("50")
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="r07")
            self.assertEqual(response.status_code, 200, response.text)
            verdict = response.json()["verdict"]
            self.assertEqual(verdict["verdict"], "reduced")
            self.assertEqual(Decimal(verdict["approved_qty"]), Decimal("0.004"))
            self.assertIn("R07", [item["rule"] for item in verdict["violations"]])
        finally:
            case.close()

    def test_r11_uses_symbol_mmr(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.bracket_mmr = "0.09"  # 1/10 − 0.09 = 1% < 2×SL(1%)
            client = case.client()
            with client:
                response = evaluate(
                    client, cycle_id="r11", plan_payload=plan(leverage=10)
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_RISK_LIQ_DISTANCE")
            self.assertEqual(response.json()["error"]["details"]["rule"], "R11")
        finally:
            case.close()


class ControlErrorTests(unittest.TestCase):
    """M02：控制态错误必须走统一错误信封（422/423），不能 TypeError。"""

    def test_invalid_control_state_is_422(self) -> None:
        case = ServiceCase(with_fake=False)
        try:
            client = case.client()
            with client:
                response = client.post(
                    "/api/control", json={"state": "foo", "confirm": "FOO", "reason": "bad"}
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_VALIDATION")
        finally:
            case.close()

    def test_normal_blocked_by_latch_is_423(self) -> None:
        case = ServiceCase(with_fake=False)
        try:
            service = case.service
            service.ledger.save_risk_state(
                {
                    "anchor_id": service.state.anchor_id,
                    "date": "2026-11-09",
                    "day_start_equity": "5000",
                    "peak_equity": "5000",
                    "daily_loss_latched": 1,
                    "daily_loss_latched_at": "2026-11-09T00:00:00Z",
                    "drawdown_latched": 0,
                    "consecutive_losses": 0,
                    "cooldown_until": None,
                    "recovery_required": 0,
                }
            )
            client = case.client()
            with client:
                response = client.post(
                    "/api/control",
                    json={"state": "normal", "confirm": "NORMAL", "reason": "recover"},
                )
            self.assertEqual(response.status_code, 423, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_CONTROL_LOCKED")
        finally:
            case.close()

    def test_control_requires_reason(self) -> None:
        case = ServiceCase(with_fake=False)
        try:
            client = case.client()
            with client:
                response = client.post(
                    "/api/control", json={"state": "pause", "confirm": "PAUSE", "reason": "  "}
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_VALIDATION")
        finally:
            case.close()


class ClockSkewRetryTests(unittest.TestCase):
    """M03：-1021/-1022 仅 GET 重校准后重试一次；POST 不重试。"""

    def _client(self, handler, calls: list) -> DemoBinanceClient:
        def wrapped(request):
            calls.append((request.method, request.url.path))
            return handler(request)

        return DemoBinanceClient(
            "https://demo-fapi.binance.com",
            DemoCredentials("k", "s"),
            transport=httpx.MockTransport(wrapped),
        )

    def test_get_retries_once_after_clock_skew(self) -> None:
        calls: list = []

        state = {"account_calls": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/fapi/v1/time":
                return httpx.Response(200, json={"serverTime": 1}, request=request)
            if request.url.path == "/fapi/v2/account":
                state["account_calls"] += 1
                if state["account_calls"] == 1:
                    return httpx.Response(
                        400,
                        json={"code": -1021, "msg": "Timestamp outside recvWindow"},
                        request=request,
                    )
                return httpx.Response(200, json={"totalMarginBalance": "1"}, request=request)
            return httpx.Response(200, json={}, request=request)

        client = self._client(handler, calls)
        result = client.account()
        self.assertEqual(result["totalMarginBalance"], "1")
        self.assertEqual(
            [path for _method, path in calls].count("/fapi/v2/account"), 2
        )
        client.close()

    def test_post_does_not_retry_on_clock_skew(self) -> None:
        calls: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"code": -1022, "msg": "Signature invalid"}, request=request
            )

        client = self._client(handler, calls)
        with self.assertRaises(Exception) as caught:
            client.submit_order({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01"})
        self.assertEqual(getattr(caught.exception, "code"), -1022)
        self.assertEqual([path for _m, path in calls].count("/fapi/v1/order"), 1)
        client.close()


class MarginModeTests(unittest.TestCase):
    """M04：保证金模式无法确认时必须拒绝开仓（不带着未知模式下单）。"""

    def test_margin_type_failure_blocks_order(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.fail_margin_type = 1
            client = case.client()
            with client:
                response = submit(client, approve(client, cycle_id="margin-fail"))
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_MARGIN_MODE_UNCONFIRMED")
            self.assertEqual(case.fake.calls_for("/fapi/v1/order", "POST"), [])
            self.assertEqual(case.fake.positions, {})
        finally:
            case.close()


class OutboxMonotonicityTests(unittest.TestCase):
    """L01：Outbox 状态只前进，迟到响应不得把终态改回工作态。"""

    def test_late_new_does_not_regress_filled(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            outbox = OrderOutbox(case.service.ledger)
            outbox.prepare(
                client_order_id="late-cid",
                purpose="ENTRY",
                symbol="BTCUSDT",
                side="BUY",
                position_side=None,
                order_type="MARKET",
                endpoint="/fapi/v1/order",
                request={},
                qty="0.01",
            )
            outbox.acknowledge("late-cid", {"status": "FILLED", "orderId": 1})
            outbox.acknowledge("late-cid", {"status": "NEW", "orderId": 1})
            self.assertEqual(outbox.get("late-cid")["status"], "FILLED")
            self.assertEqual(outbox.suppressed_transitions, 1)
        finally:
            case.close()

    def test_order_mirror_keeps_terminal_status(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            broker = case.service.broker
            broker._insert_order_record(
                "mirror-cid",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                qty="0.01",
                status="FILLED",
            )
            broker._insert_order_record(
                "mirror-cid",
                symbol="BTCUSDT",
                side="BUY",
                order_type="MARKET",
                qty="0.01",
                status="NEW",
            )
            self.assertEqual(case.service.ledger.get_order("mirror-cid")["status"], "FILLED")
        finally:
            case.close()


# ======================================================================
# t30 repair-round-3 (t25 findings T25-B01/M01/M02/L01/L02)
# ======================================================================

class ServerEvidenceTests(unittest.TestCase):
    """T25-B01：P1 证据年龄只来自服务端 md-evidence/1 快照（DB 引用 + 文件 hash/时间）。"""

    def test_p1_rejects_without_server_evidence(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="no-evidence", seed=False)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_EVIDENCE_UNAVAILABLE")
            self.assertEqual(
                response.json()["error"]["details"]["cycle_id"], "no-evidence"
            )
        finally:
            case.close()

    def test_p1_forged_evidence_age_is_ignored(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                # 服务端证据是 410s 前生成的（>400s 门限）；请求体伪造 evidence_age_ms=0
                response = evaluate(
                    client,
                    cycle_id="stale-evidence",
                    evidence_age_ms=0,
                    evidence_override_ms=410_000,
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_MARKET_STALE")
            self.assertEqual(response.json()["error"]["details"]["rule"], "R14")
        finally:
            case.close()

    def test_p1_reports_server_evidence_age(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="fresh-evidence", evidence_age_ms=0)
            self.assertEqual(response.status_code, 200, response.text)
            context = response.json()["context"]
            self.assertEqual(context["evidence_source"], "md-evidence/1")
            self.assertEqual(context["evidence_id"], "fresh-evidence")
            self.assertGreaterEqual(context["evidence_age_ms"], 0)
            self.assertLess(context["evidence_age_ms"], 15_000)
        finally:
            case.close()

    def test_p1_accepts_evidence_within_400s(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                # 390s < 400s：M1.4c 把门限提到 400s（链预算 340s 必须装得下）
                response = evaluate(
                    client, cycle_id="evidence-390s", evidence_override_ms=390_000
                )
            self.assertEqual(response.status_code, 200, response.text)
            age_ms = response.json()["context"]["evidence_age_ms"]
            self.assertGreaterEqual(age_ms, 390_000)
            self.assertLess(age_ms, 400_000)
        finally:
            case.close()

    def test_p1_rejects_file_hash_tamper(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            seed_evidence(case.service, "tamper-hash", age_ms=1000, tamper="hash")
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="tamper-hash", seed=False)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_EVIDENCE_UNVERIFIED")
        finally:
            case.close()

    def test_p1_rejects_db_reference_mismatch(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            seed_evidence(case.service, "tamper-db", age_ms=1000, tamper="db")
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="tamper-db", seed=False)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_EVIDENCE_UNVERIFIED")
        finally:
            case.close()

    def test_p2_ignores_forged_evidence_body(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = approve(client, cycle_id="p2-evidence-forge")
                response = client.post(
                    "/api/execution/order",
                    json={
                        "approval_token": approval["approval"]["token"],
                        "evidence_age_ms": 0,
                        "evidence": {
                            "evidence_id": "forged",
                            "schema_version": "md-evidence/1",
                            "age_ms": 0,
                            "sha256": "sha256:" + "0" * 64,
                        },
                        "market": {"mark": "1"},
                    },
                )
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(case.fake.positions["BTCUSDT"]["entry"], Decimal("100000.0"))
        finally:
            case.close()

    def test_p2_token_without_evidence_facts_is_rejected(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            service = case.service
            service.ensure_started()
            facts = service.broker.market_facts("BTCUSDT")
            verdict = RiskVerdict(verdict="approved", approved_qty=Decimal("0.02"), leverage=8)
            issued = service.approvals.issue(
                OrderPlan.from_payload(plan(), cycle_id="no-evidence-token"),
                verdict,
                context={
                    "market": facts,
                    "evidence_age_ms": 1000,
                    "rules_version": service.risk.rules_version(),
                },
            )
            client = case.client()
            with client:
                response = client.post(
                    "/api/execution/order", json={"approval_token": issued["token"]}
                )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_APPROVAL_INVALID")
            self.assertEqual(
                response.json()["error"]["details"].get("reason"), "evidence_facts_missing"
            )
        finally:
            case.close()


class LossStreakCooldownTests(unittest.TestCase):
    """T25-M01：cooldown 锚定最后一笔亏损，只在 streak 变化时写入，到期解除且不续期。"""

    @staticmethod
    def _limits() -> RiskLimits:
        return RiskLimits(
            enable_loss_streak=True,
            consecutive_losses=2,
            consecutive_loss_cooldown_hours=4,
        )

    def _two_losses(self, case: ServiceCase) -> None:
        client = case.client()
        with client:
            for index, (entry, mark) in enumerate(
                ((Decimal("100000"), Decimal("99000")), (Decimal("99000"), Decimal("98000")))
            ):
                case.fake.mark_price = entry
                case.fake.index_price = entry
                payload = plan(sl=str(entry - 1000), tp=str(entry + 2000))
                response = submit(
                    client, approve(client, cycle_id=f"cd-{index}", plan_payload=payload)
                )
                self.assertEqual(response.status_code, 201, response.text)
                case.fake.mark_price = mark
                case.fake.index_price = mark
                closed = client.post(
                    "/api/execution/close", json={"symbol": "BTCUSDT"}
                ).json()
                self.assertTrue(closed["closed"], closed)

    def test_cooldown_anchored_and_not_renewed(self) -> None:
        base = [time.time()]
        case = ServiceCase(with_fake=True, limits=self._limits(), clock=lambda: base[0])
        try:
            self._two_losses(case)
            state = case.service.ledger.load_risk_state()
            until_1 = state["cooldown_until"]
            self.assertIsNotNone(until_1)
            until_ms = int(
                datetime.fromisoformat(until_1.replace("Z", "+00:00")).timestamp() * 1000
            )
            expected_ms = int(base[0] * 1000) + 4 * 3600 * 1000
            self.assertLess(
                abs(until_ms - expected_ms), 120_000,
                "cooldown 应锚定最后一笔亏损成交时间，而不是每次 now+4h",
            )
            client = case.client()
            with client:
                client.get("/api/status")
                client.get("/api/status")
            self.assertEqual(
                case.service.ledger.load_risk_state()["cooldown_until"], until_1,
                "轮询/心跳不得续期 cooldown",
            )

            # 推进时钟到 cooldown 到期之后：清除冷却、保留 streak，并允许新开仓
            base[0] = until_ms / 1000 + 60
            case.fake.index_price = case.fake.mark_price
            case.service.broker._cache = None
            with client:
                status = client.get("/api/status").json()
                self.assertIsNone(status["risk"]["cooldown_until"])
                self.assertEqual(status["risk"]["consecutive_losses"], 2)
                response = evaluate(
                    client,
                    cycle_id="after-cooldown",
                    plan_payload=plan(sl="97000", tp="101000"),
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(case.service.ledger.load_risk_state()["cooldown_until"])
        finally:
            case.close()

    def test_partial_fills_merge_per_client_order_id(self) -> None:
        case = ServiceCase(with_fake=True, limits=self._limits())
        try:
            ledger = case.service.ledger
            now_ms = int(time.time() * 1000)

            def add_order(client_order_id: str) -> None:
                ledger.insert_order(
                    {
                        "client_order_id": client_order_id,
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "position_side": "LONG",
                        "order_type": "MARKET",
                        "status": "FILLED",
                        "qty_requested": "0.01",
                        "qty_filled": "0.01",
                        "purpose": "EXIT",
                    }
                )

            def add_fill(
                client_order_id: str, fill_id: str, pnl: str, trade_time_ms: int
            ) -> None:
                ledger.insert_fill(
                    {
                        "fill_id": fill_id,
                        "client_order_id": client_order_id,
                        "symbol": "BTCUSDT",
                        "side": "SELL",
                        "price": "100000",
                        "qty": "0.005",
                        "realized_pnl": pnl,
                        "commission": "0",
                        "trade_time_ms": trade_time_ms,
                        "source": "userTrades",
                    }
                )

            # 两笔亏损平仓，每笔各有 2 个分批成交 → 连亏应为 2 而不是 4
            add_order("merge-a")
            add_fill("merge-a", "f1", "-10", now_ms - 5000)
            add_fill("merge-a", "f2", "-5", now_ms - 4000)
            add_order("merge-b")
            add_fill("merge-b", "f3", "-3", now_ms - 3000)
            add_fill("merge-b", "f4", "-4", now_ms - 2000)
            case.service._refresh_loss_streak()
            state = ledger.load_risk_state()
            self.assertEqual(state["consecutive_losses"], 2)
            self.assertIsNotNone(state["cooldown_until"])

            # 一笔盈利平仓把 streak 归零并清除冷却
            add_order("merge-win")
            add_fill("merge-win", "f5", "+50", now_ms - 1000)
            case.service._refresh_loss_streak()
            state = ledger.load_risk_state()
            self.assertEqual(state["consecutive_losses"], 0)
            self.assertIsNone(state["cooldown_until"])
        finally:
            case.close()


class MmrFailClosedTests(unittest.TestCase):
    """T25-M02：leverageBracket 不可用时 R11 fail-closed，不再回退 1% MMR。"""

    def test_bracket_failure_blocks_p1(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.fail_leverage_bracket = 5
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="mmr-fail")
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_RISK_LIQ_DISTANCE")
            self.assertEqual(response.json()["error"]["details"]["rule"], "R11")
            self.assertIn("维持保证金率", response.json()["error"]["message"])
            self.assertIsNone(case.service.broker.mmr_for("BTCUSDT"))
        finally:
            case.close()

    def test_published_bracket_still_used(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            case.fake.bracket_mmr = "0.02"
            client = case.client()
            with client:
                response = evaluate(client, cycle_id="mmr-ok")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(Decimal(response.json()["context"]["mmr"]), Decimal("0.02"))
        finally:
            case.close()


class R14EvidenceBoundaryTests(unittest.TestCase):
    """R14 边界：固定时钟下恰好 400000ms 通过，400001ms 拒绝。"""

    FIXED = 1_800_000_000.0

    def _case(self) -> ServiceCase:
        return ServiceCase(with_fake=True, clock=lambda: self.FIXED)

    def test_exactly_400000ms_is_accepted(self) -> None:
        case = self._case()
        try:
            client = case.client()
            with client:
                response = evaluate(
                    client, cycle_id="boundary-400000", evidence_override_ms=400_000
                )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["context"]["evidence_age_ms"], 400_000)
        finally:
            case.close()

    def test_400001ms_is_rejected(self) -> None:
        case = self._case()
        try:
            client = case.client()
            with client:
                response = evaluate(
                    client, cycle_id="boundary-400001", evidence_override_ms=400_001
                )
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["error"]["code"], "E_MARKET_STALE")
            self.assertEqual(response.json()["error"]["details"]["rule"], "R14")
        finally:
            case.close()


class ApiSummaryTests(unittest.TestCase):
    """T25-L01：FastAPI summary 反映 M1.2，且路由装配不变。"""

    def test_summary_reports_m12(self) -> None:
        case = ServiceCase(with_fake=False)
        try:
            client = case.client()
            with client:
                spec = client.get("/openapi.json").json()
            self.assertIn("M1.2", spec["info"]["summary"])
            for path in (
                "/api/status",
                "/api/risk/evaluate",
                "/api/execution/order",
                "/api/execution/close",
                "/api/control",
            ):
                self.assertIn(path, spec["paths"])
        finally:
            case.close()


# ======================================================================
# T03 · §8·B：待确认状态、按原标识收敛、保护失败处置
# ======================================================================

def _seed_stale_new_protection_rows(case: ServiceCase) -> list[str]:
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
                "status": "NEW",
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        ledger.outbox_prepare(
            {
                "client_order_id": cid,
                "business_key": "PROTECTION:BTCUSDT:SHORT",
                "endpoint": "/fapi/v1/algoOrder",
                "request": {"symbol": "BTCUSDT"},
                "status": "NEW",
            }
        )
        ledger.execute(
            "UPDATE order_outbox SET status='NEW', created_at=?, updated_at=? "
            "WHERE client_order_id=?",
            (created_at, created_at, cid),
        )
    return [row[0] for row in rows]


class PendingConfirmationAndConvergenceTests(unittest.TestCase):
    """T03 §8·B1/B2 + T01 G4：待确认显示，终态只由交易所事实收敛。"""

    def setUp(self) -> None:
        self.case = ServiceCase(with_fake=True)
        self.fake = self.case.fake
        self.service = self.case.service
        self.client = self.case.client()
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.case.close()

    def test_unknown_is_pending_confirmation_and_converges_by_original_id(self) -> None:
        self.fake.timeout_orders = 1
        response = submit(self.client, approve(self.client, cycle_id="t03-pending"))
        self.assertEqual(response.status_code, 202, response.text)
        details = response.json()["error"]["details"]
        self.assertEqual(details["display"], "PENDING_CONFIRMATION")
        cid = details["client_order_id"]

        status = self.client.get("/api/status").json()
        self.assertEqual(status["outbox"]["pending_confirmation_count"], 1)
        self.assertEqual(
            status["outbox"]["pending"][0]["display"], "PENDING_CONFIRMATION"
        )
        self.assertIn("ORDER_OUTBOX_UNKNOWN", status["readiness"]["reasons"])

        # 一次“查不到”尚不足以定终态：仍是待确认
        first = self.client.get(f"/api/execution/order/{cid}").json()
        self.assertEqual(first["status"], "UNKNOWN")
        self.assertTrue(first["pending_confirmation"])
        self.assertFalse(first["converged"])

        # 反复对账不会把“待确认”改写成确定失败
        for _ in range(3):
            self.client.post("/api/execution/reconcile")
        row = next(
            item
            for item in self.client.get("/api/orders").json()["orders"]
            if item["client_order_id"] == cid
        )
        self.assertEqual(row["status"], "UNKNOWN")
        self.assertNotEqual(row["status"], "REJECTED")
        self.assertTrue(row["pending_confirmation"])

        # 交易所多次明确回答“无此单”后收敛到 CANCELED，原因可解释
        second = self.client.get(f"/api/execution/order/{cid}").json()
        self.assertEqual(second["status"], "CANCELED")
        self.assertTrue(second["converged"])
        self.assertEqual(second["converged_by"], "exchange_not_found")
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 1)

    def test_stale_new_protection_rows_converge_on_startup(self) -> None:
        seeded = _seed_stale_new_protection_rows(self.case)
        startup = self.client.get("/api/status").json()["startup"]
        self.assertTrue(startup["recovery"]["converged"], startup["recovery"])
        orders = {
            row["client_order_id"]: row
            for row in self.client.get("/api/orders").json()["orders"]
        }
        for cid in seeded:
            self.assertEqual(orders[cid]["status"], "CANCELED")
            self.assertEqual(
                orders[cid]["convergence_reason"], "canceled_not_found_at_exchange"
            )
            self.assertEqual(self.service.outbox.get(cid)["status"], "CANCELED")
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/order", "POST")), 0)
        self.assertEqual(len(self.fake.calls_for("/fapi/v1/algoOrder", "POST")), 0)

    def test_confirmed_protection_failure_reduces_system_position(self) -> None:
        response = submit(self.client, approve(self.client, cycle_id="t03-disposition"))
        self.assertEqual(response.status_code, 201, response.text)
        for algo in self.fake.algos.values():
            algo["algoStatus"] = "CANCELED"
        self.fake.protection_failures = 1000
        result = self.service.broker.enforce_protection()
        item = result["repaired"][0]
        self.assertEqual(item["disposition"], "reduce_position")
        self.assertTrue(item["closed"])
        self.assertEqual(self.fake.positions, {})
        operation = self.service.ledger.operations()[0]
        self.assertEqual(operation["action"], "reduce_position")
        self.assertEqual(operation["status"], "succeeded")
        events = [row["event_type"] for row in self.service.ledger.list_risk_events(50)]
        self.assertIn("protection_reduce_position", events)


if __name__ == "__main__":
    unittest.main()
