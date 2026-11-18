"""硬风控规则单元测试（R00–R25）：不经过 HTTP，直接验证确定性引擎。

覆盖验收：R00–R19/R22–R25 生效；R20/R21 默认关闭；AI 不能放宽硬上限。

T02（§8·A 权限收口）另有专项：
- confidence 只作分析字段，不改变允许数量/杠杆上限（``ConfidenceAuthorizationTests``）；
- 允许数量 = min(风险预算/入场到止损, 名义上限/入场, 可用保证金×杠杆/入场) 向下取 stepSize；
- 达不到最小数量/最小名义一律拒绝，不为凑最小名义放大风险；
- strong_* 旧字段可读但不授权，历史快照按原样展示，止损预算只是计划约束。

T19（权限红队复核）：A 项之后仍要证明「没有第二条放大路径」，因此逐个入口用对抗
输入做证伪 + 断言守住（本文件末尾四组）：
- 入口 1 配置读取：env 只能改运维项，caps/strong_* 类变量一律不读，TTL 只能收紧；
- 入口 2 审批令牌：绑定普通档策略后，旧令牌（缺 rules_version / 强信心档 15x /
  非确定性 sizing / approved_qty 与算式不符）一律拒绝；
- 入口 3 /api/risk/envelope：authorization 块声明哪些字段算授权、哪些不算，且
  effective_caps 与真正参与计算的上限漂移会被抓到；
- 入口 4 执行前复核：P2 用提交时刻事实重算并只取 min，从不相信令牌里的数量。
"""

from __future__ import annotations

import base64
import json
import math
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support import ServiceCase, approve as support_approve, plan as support_plan  # noqa: E402

from workbench_service.ledger.store import Ledger  # noqa: E402
from workbench_service.risk.approval import ApprovalError, ApprovalManager  # noqa: E402
from workbench_service.risk.config import (  # noqa: E402
    AUTHORIZATION_FIELDS,
    ENV_KNOBS,
    MAX_APPROVAL_TTL_S,
    RiskLimits,
)
from workbench_service.risk.models import (  # noqa: E402
    OrderPlan,
    PositionView,
    RiskContext,
    RiskVerdict,
    SymbolFiltersView,
)
from workbench_service.risk.rules import evaluate  # noqa: E402
from workbench_service.risk.service import RiskService  # noqa: E402
from workbench_service.risk.state import RiskStateStore  # noqa: E402

LIMITS = RiskLimits()
FILTERS = SymbolFiltersView(
    symbol="BTCUSDT",
    step_size=Decimal("0.001"),
    tick_size=Decimal("0.10"),
    min_qty=Decimal("0.001"),
    min_notional=Decimal("5"),
    max_qty=Decimal("100"),
    exchange_max_leverage=125,
    status="TRADING",
)


def make_plan(**overrides) -> OrderPlan:
    payload = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "leverage": 8,
        "qty": "0.02",
        "sl": "99000",
        "tp": "102800",
        "confidence": 0.5,
    }
    payload.update(overrides)
    return OrderPlan.from_payload(payload, cycle_id="unit")


def make_ctx(**overrides) -> RiskContext:
    base = dict(
        mark=Decimal("100000"),
        snapshot_age_ms=1000,
        evidence_age_ms=1000,
        equity_session=Decimal("5000"),
        equity_age_ms=1000,
        used_margin=Decimal("0"),
        open_positions=(),
        control_state="normal",
        protection_ok=True,
        unknown_orders=0,
        submitting_orders=0,
        mark_move_60s_pct=Decimal("0.004"),
        mark_index_deviation_pct=Decimal("0.001"),
        spread_bps=Decimal("3"),
        mmr=Decimal("0.01"),
    )
    base.update(overrides)
    return RiskContext(**base)


def rules_of(verdict) -> list[str]:
    return [item.rule for item in verdict.violations]


class RiskRuleTests(unittest.TestCase):
    def assertRejected(self, verdict, rule: str) -> None:
        self.assertEqual(verdict.verdict, "rejected", verdict.as_dict())
        self.assertEqual(verdict.blocking_rule, rule, verdict.as_dict())

    # ------------------------------------------------------------------ 基线
    def test_baseline_is_approved(self) -> None:
        verdict = evaluate(make_plan(), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "approved", verdict.as_dict())
        self.assertEqual(verdict.approved_qty, Decimal("0.02"))
        self.assertEqual(verdict.tier, "regular")
        self.assertAlmostEqual(float(verdict.rr), 2.8, places=2)

    def test_confidence_never_widens_risk_or_leverage(self) -> None:
        # §8·A：同一计划只改 confidence，允许数量与杠杆上限完全一致。
        results = {}
        for confidence in (0.1, 0.5, 0.72, 0.95):
            verdict = evaluate(
                make_plan(confidence=confidence, qty="0.10"), make_ctx(), LIMITS, filters=FILTERS
            )
            results[confidence] = verdict
            self.assertEqual(verdict.tier, "regular", verdict.as_dict())
            self.assertEqual(verdict.verdict, "reduced", verdict.as_dict())
            # 普通档 1% × 5000 = 50U；|入场−止损|=1000U → 0.05（不再是强信心 100U→0.1）
            self.assertEqual(verdict.approved_qty, Decimal("0.05"))
            self.assertEqual(verdict.leverage, 8)
            self.assertEqual(verdict.leverage_cap, 10)
            self.assertEqual(verdict.sizing["confidence_used"], "false")

    def test_confidence_cannot_unlock_higher_leverage(self) -> None:
        # 旧强信心档允许 15x；现在 12x 在任何 confidence 下都被普通档 10x 拦下。
        for confidence in (0.1, 0.5, 0.72, 0.95):
            verdict = evaluate(
                make_plan(confidence=confidence, leverage=12), make_ctx(), LIMITS, filters=FILTERS
            )
            self.assertRejected(verdict, "R04")
            self.assertIn("普通档上限", verdict.blocked_reason or "")
            self.assertEqual(verdict.leverage_cap, 10)

    def test_legacy_strong_fields_do_not_authorize(self) -> None:
        # 旧配置显式给出 strong_*（旧部署可能就是这么配的）也不提额。
        legacy = RiskLimits(strong_max_risk_pct=0.05, strong_max_leverage=25)
        strong_ai = evaluate(
            make_plan(confidence=0.99, qty="0.30", ai_limits={"strong_max_risk_pct": 0.5}),
            make_ctx(),
            legacy,
            filters=FILTERS,
        )
        baseline = evaluate(make_plan(confidence=0.1, qty="0.30"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(strong_ai.approved_qty, baseline.approved_qty)
        self.assertEqual(strong_ai.approved_qty, Decimal("0.05"))
        self.assertEqual(strong_ai.leverage_cap, 10)

    # ------------------------------------------------------------------ R00
    def test_ai_cannot_loosen_limits(self) -> None:
        plan = make_plan(
            qty="0.2", ai_limits={"max_leverage": 50, "max_risk_pct": 0.5, "max_notional_multiple": 9}
        )
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced")
        self.assertEqual(verdict.approved_qty, Decimal("0.05"))
        rules = rules_of(verdict)
        self.assertIn("R00", rules)
        self.assertIn("R05", rules)
        self.assertEqual(verdict.leverage, 8)

    def test_ai_can_tighten(self) -> None:
        plan = make_plan(ai_limits={"max_risk_pct": 0.002})
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced")
        self.assertEqual(verdict.approved_qty, Decimal("0.01"))

    def test_ai_tightened_leverage_is_enforced(self) -> None:
        plan = make_plan(leverage=8, ai_limits={"max_leverage": 5})
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R04")

    # ------------------------------------------------------------------ 结构性
    def test_r01_whitelist(self) -> None:
        self.assertEqual(len(LIMITS.symbols), 20)
        unlisted = evaluate(make_plan(symbol="PEPEUSDT"), make_ctx(), LIMITS, filters=None)
        self.assertRejected(unlisted, "R01")
        listed = evaluate(make_plan(symbol="DOGEUSDT"), make_ctx(), LIMITS, filters=None)
        self.assertNotIn("R01", rules_of(listed))
        self.assertEqual(listed.verdict, "approved")

    def test_r01_exchange_status(self) -> None:
        filters = SymbolFiltersView(symbol="BTCUSDT", status="BREAK")
        verdict = evaluate(make_plan(), make_ctx(), LIMITS, filters=filters)
        self.assertRejected(verdict, "R01")

    def test_r02_existing_position(self) -> None:
        ctx = make_ctx(
            open_positions=(
                PositionView(symbol="BTCUSDT", position_side="LONG", qty=Decimal("0.01")),
            )
        )
        verdict = evaluate(make_plan(), ctx, LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R02")

    def test_r03_max_positions(self) -> None:
        ctx = make_ctx(
            open_positions=(
                PositionView(symbol="ETHUSDT", position_side="SHORT", qty=Decimal("0.1")),
            )
        )
        verdict = evaluate(make_plan(), ctx, LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R03")

    def test_r04_leverage_above_cap(self) -> None:
        verdict = evaluate(make_plan(leverage=11), make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R04")

    def test_r04_exchange_cap(self) -> None:
        filters = SymbolFiltersView(symbol="BTCUSDT", exchange_max_leverage=5)
        verdict = evaluate(make_plan(leverage=8), make_ctx(), LIMITS, filters=filters)
        self.assertRejected(verdict, "R04")

    # ------------------------------------------------------------------ 数量
    def test_r05_downsizes_risk(self) -> None:
        verdict = evaluate(make_plan(qty="0.2"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced")
        self.assertEqual(verdict.approved_qty, Decimal("0.05"))
        self.assertIn("R05", rules_of(verdict))

    def test_r06_downsizes_notional(self) -> None:
        plan = make_plan(sl="99800", tp="104000", qty="0.2")  # risk/unit=200 → notional 上限先命中
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced", verdict.as_dict())
        self.assertEqual(verdict.approved_qty, Decimal("0.1"))  # 2×5000/100000
        self.assertIn("R06", rules_of(verdict))

    def test_r07_downsizes_margin(self) -> None:
        verdict = evaluate(
            make_plan(qty="0.05"),
            make_ctx(used_margin=Decimal("2950")),
            LIMITS,
            filters=FILTERS,
        )
        self.assertEqual(verdict.verdict, "reduced", verdict.as_dict())
        self.assertEqual(verdict.approved_qty, Decimal("0.004"))
        self.assertIn("R07", rules_of(verdict))

    def test_r07_rejects_when_no_margin(self) -> None:
        verdict = evaluate(
            make_plan(), make_ctx(used_margin=Decimal("3100")), LIMITS, filters=FILTERS
        )
        self.assertRejected(verdict, "R07")

    # ------------------------------------------------------------------ 价格
    def test_r08_direction(self) -> None:
        verdict = evaluate(make_plan(sl="101000"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R08")

    def test_r09_too_tight_and_too_wide(self) -> None:
        tight = evaluate(make_plan(sl="99990"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(tight, "R09")
        wide = evaluate(make_plan(sl="90000", tp="120000"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(wide, "R09")

    def test_r10_risk_reward(self) -> None:
        verdict = evaluate(make_plan(tp="100500"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R10")

    def test_r11_liquidation_distance(self) -> None:
        plan = make_plan(leverage=10, sl="95100", tp="110000")  # 4.9% SL × 2 = 9.8% > 9%
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R11")

    def test_r12_min_notional(self) -> None:
        filters = SymbolFiltersView(symbol="BTCUSDT", min_notional=Decimal("200"))
        verdict = evaluate(make_plan(qty="0.001"), make_ctx(), LIMITS, filters=filters)
        self.assertRejected(verdict, "R12")

    def test_r12_below_min_qty(self) -> None:
        filters = SymbolFiltersView(symbol="BTCUSDT", min_qty=Decimal("0.01"))
        verdict = evaluate(make_plan(qty="0.004"), make_ctx(), LIMITS, filters=filters)
        self.assertRejected(verdict, "R12")

    def test_r13_snaps_qty(self) -> None:
        verdict = evaluate(make_plan(qty="0.0123"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced")
        self.assertEqual(verdict.approved_qty, Decimal("0.012"))
        self.assertIn("R13", rules_of(verdict))
        self.assertEqual(
            next(item.action for item in verdict.violations if item.rule == "R13"), "adjust"
        )

    # ------------------------------------------------------------------ 数据
    def test_r14_snapshot_and_evidence_age(self) -> None:
        old_snapshot = evaluate(
            make_plan(), make_ctx(snapshot_age_ms=20_000), LIMITS, filters=FILTERS
        )
        self.assertRejected(old_snapshot, "R14")
        old_evidence = evaluate(
            make_plan(), make_ctx(evidence_age_ms=410_000), LIMITS, filters=FILTERS
        )
        self.assertRejected(old_evidence, "R14")
        missing = evaluate(make_plan(), make_ctx(mark=None), LIMITS, filters=FILTERS)
        self.assertRejected(missing, "R14")

    def test_r15_anomaly(self) -> None:
        for override in (
            {"mark_move_60s_pct": Decimal("0.05")},
            {"mark_index_deviation_pct": Decimal("0.02")},
            {"spread_bps": Decimal("80")},
            {"mark_move_60s_pct": None},
        ):
            verdict = evaluate(make_plan(), make_ctx(**override), LIMITS, filters=FILTERS)
            self.assertRejected(verdict, "R15")

    def test_r15_can_be_relaxed_for_offline(self) -> None:
        limits = RiskLimits(require_anomaly_data=False)
        verdict = evaluate(
            make_plan(), make_ctx(mark_move_60s_pct=None), limits, filters=FILTERS
        )
        self.assertEqual(verdict.verdict, "approved")

    # ------------------------------------------------------------------ 状态
    def test_r16_duplicate_business_key(self) -> None:
        ctx = make_ctx(pending_business_keys=("ENTRY:BTCUSDT:LONG",))
        verdict = evaluate(make_plan(), ctx, LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R16")

    def test_r17_unknown_orders(self) -> None:
        verdict = evaluate(make_plan(), make_ctx(unknown_orders=1), LIMITS, filters=FILTERS)
        self.assertRejected(verdict, "R17")

    def test_r18_daily_loss_latch(self) -> None:
        verdict = evaluate(
            make_plan(), make_ctx(daily_loss_latched=True), LIMITS, filters=FILTERS
        )
        self.assertRejected(verdict, "R18")

    def test_r19_drawdown_latch(self) -> None:
        verdict = evaluate(
            make_plan(), make_ctx(drawdown_latched=True), LIMITS, filters=FILTERS
        )
        self.assertRejected(verdict, "R19")

    def test_r20_r21_default_off(self) -> None:
        ctx = make_ctx(cooldown_active=True, trades_today=99, trades_symbol_today=9)
        verdict = evaluate(make_plan(), ctx, LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "approved", verdict.as_dict())

    def test_r20_r21_when_enabled(self) -> None:
        limits = RiskLimits(enable_loss_streak=True, enable_frequency=True)
        cool = evaluate(make_plan(), make_ctx(cooldown_active=True), limits, filters=FILTERS)
        self.assertRejected(cool, "R20")
        freq = evaluate(make_plan(), make_ctx(trades_today=12), limits, filters=FILTERS)
        self.assertRejected(freq, "R21")
        symbol_freq = evaluate(
            make_plan(), make_ctx(trades_symbol_today=3), limits, filters=FILTERS
        )
        self.assertRejected(symbol_freq, "R21")

    def test_r22_control_state(self) -> None:
        verdict = evaluate(
            make_plan(), make_ctx(control_state="close_only"), LIMITS, filters=FILTERS
        )
        self.assertRejected(verdict, "R22")

    def test_r23_protection(self) -> None:
        false_case = evaluate(
            make_plan(), make_ctx(protection_ok=False, protection_missing=("ETHUSDT",)),
            LIMITS, filters=FILTERS,
        )
        self.assertRejected(false_case, "R23")
        unknown_case = evaluate(make_plan(), make_ctx(protection_ok=None), LIMITS, filters=FILTERS)
        self.assertRejected(unknown_case, "R23")

    def test_r24_margin_ratio(self) -> None:
        verdict = evaluate(
            make_plan(), make_ctx(maint_margin_ratio=Decimal("0.85")), LIMITS, filters=FILTERS
        )
        self.assertRejected(verdict, "R24")

    def test_r25_equity(self) -> None:
        none_equity = evaluate(make_plan(), make_ctx(equity_session=None), LIMITS, filters=FILTERS)
        self.assertRejected(none_equity, "R25")
        stale_equity = evaluate(make_plan(), make_ctx(equity_age_ms=90_000), LIMITS, filters=FILTERS)
        self.assertRejected(stale_equity, "R25")
        unproven = evaluate(make_plan(), make_ctx(equity_age_ms=None), LIMITS, filters=FILTERS)
        self.assertRejected(unproven, "R25")


class DeterministicSizingTests(unittest.TestCase):
    """§8·A.3/§8·A.4：允许数量由确定性代码计算，最小订单失败即拒绝。"""

    def test_sizing_inputs_are_explicit(self) -> None:
        verdict = evaluate(make_plan(qty="0.20"), make_ctx(), LIMITS, filters=FILTERS)
        sizing = verdict.sizing
        # 风险预算 = 5000 × 1% = 50U；|入场−止损| = 1000U → 0.05
        self.assertEqual(sizing["policy"], "deterministic")
        self.assertEqual(sizing["requested_qty"], "0.2")
        self.assertEqual(sizing["risk_budget"], "50")
        self.assertEqual(sizing["risk_per_unit"], "1000")
        self.assertEqual(sizing["max_qty_by_risk"], "0.05")
        # 名义上限 = 5000 × 2.0 / 100000 = 0.1；可用保证金 = 5000×60%×8/100000 = 0.24
        self.assertEqual(sizing["max_qty_by_notional"], "0.1")
        self.assertEqual(sizing["max_qty_by_margin"], "0.24")
        self.assertEqual(sizing["step_size"], "0.001")
        self.assertEqual(sizing["approved_qty"], "0.05")
        self.assertEqual(sizing["binding_constraint"], "R05_risk_budget")
        self.assertEqual(verdict.approved_qty, Decimal("0.05"))

    def test_model_qty_cannot_exceed_backend_computation(self) -> None:
        for requested in ("0.051", "0.2", "5"):
            verdict = evaluate(make_plan(qty=requested), make_ctx(), LIMITS, filters=FILTERS)
            self.assertEqual(verdict.approved_qty, Decimal("0.05"), requested)
            self.assertEqual(verdict.verdict, "reduced")
            self.assertIn("R05", rules_of(verdict))

    def test_quantizes_down_to_step_size(self) -> None:
        # |入场−止损| = 4050U（4.05%，仍在 R09 上限内）→ 50/4050 = 0.012345679… → 向下取 0.012
        plan = make_plan(qty="0.05", sl="95950", tp="105000")
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(verdict.verdict, "reduced", verdict.as_dict())
        self.assertEqual(verdict.approved_qty, Decimal("0.012"))
        self.assertIn("R13", rules_of(verdict))
        # 向上取整到 0.013 会超出风险预算：不允许四舍五入放大风险
        self.assertGreater(Decimal("0.013") * Decimal("4050"), Decimal("50"))

    def test_below_min_notional_is_rejected_not_inflated(self) -> None:
        # 5% 止损 → 允许数量 0.01（名义 1000U）；最小名义要求 2000U → 拒绝，不放大到 0.02。
        filters = SymbolFiltersView(
            symbol="BTCUSDT",
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.001"),
            min_notional=Decimal("2000"),
            exchange_max_leverage=125,
            status="TRADING",
        )
        plan = make_plan(qty="0.20", sl="95000", tp="115000")
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=filters)
        self.assertEqual(verdict.verdict, "rejected", verdict.as_dict())
        self.assertEqual(verdict.blocking_rule, "R12")
        reason = verdict.blocked_reason or ""
        self.assertIn("拒绝而不是放大风险凑最小名义", reason)
        self.assertEqual(verdict.approved_qty, Decimal("0"))
        # 拒绝路径也要留下决定性算式，便于复盘"为什么不下单"。
        self.assertEqual(verdict.sizing["approved_qty"], "0.01")
        self.assertEqual(verdict.sizing["min_notional"], "2000")

    def test_below_min_qty_is_rejected(self) -> None:
        filters = SymbolFiltersView(
            symbol="BTCUSDT",
            step_size=Decimal("0.001"),
            min_qty=Decimal("0.02"),
            min_notional=Decimal("5"),
            exchange_max_leverage=125,
            status="TRADING",
        )
        verdict = evaluate(make_plan(qty="0.20", sl="95000", tp="115000"), make_ctx(), LIMITS, filters=filters)
        self.assertEqual(verdict.blocking_rule, "R12")
        self.assertIn("不放大风险凑最小数量", verdict.blocked_reason or "")

    def test_available_margin_only_tightens(self) -> None:
        # 交易所 availableBalance 低于权益比例上限时，以可用余额为准（P2 复算同一公式）。
        tight = evaluate(
            make_plan(qty="0.20"),
            make_ctx(available_margin=Decimal("50")),
            LIMITS,
            filters=FILTERS,
        )
        self.assertEqual(tight.approved_qty, Decimal("0.004"))
        self.assertEqual(tight.sizing["margin_available"], "50")
        self.assertEqual(tight.sizing["binding_constraint"], "R07_available_margin")
        self.assertIn("R07", rules_of(tight))

    def test_stop_loss_budget_is_plan_constraint_not_loss_guarantee(self) -> None:
        # §8·A.4：止损预算只能描述为计划约束，不能宣称极端行情下的最大实际损失保证。
        from workbench_service.risk import config as risk_config
        from workbench_service.risk import models as risk_models
        from workbench_service.risk import rules as risk_rules

        policy = risk_config.SIZING_POLICY["stop_loss_budget"]
        self.assertIn("不是极端行情下的最大实际损失保证", str(policy))
        for text in (risk_rules.__doc__ or "", risk_models.RiskVerdict.__doc__ or ""):
            self.assertIn("不是极端行情下的最大实际损失保证", text)
        verdict = evaluate(make_plan(), make_ctx(), LIMITS, filters=FILTERS)
        self.assertNotIn("保证", " ".join(verdict.sizing.values()))

    def test_legacy_config_serialization_is_marked_and_readable(self) -> None:
        limits = RiskLimits()
        payload = limits.as_dict()
        per_trade = payload["per_trade"]
        # 旧配置/旧报告仍能读到 strong_*（旧配置不崩）
        self.assertEqual(per_trade["strong_max_risk_pct"], 0.020)
        self.assertEqual(per_trade["strong_max_leverage"], 15)
        self.assertEqual(per_trade["strong_min_confidence"], 0.65)
        self.assertEqual(per_trade["strong_min_rr"], 2.0)
        # 序列化必须标明"不用于执行授权"
        legacy = payload["legacy_strong_tier"]
        self.assertFalse(legacy["used_for_execution_authorization"])
        self.assertFalse(legacy["confidence_used_for_sizing"])
        self.assertIn("不参与执行授权", str(legacy["note"]))
        self.assertIn("strong_max_risk_pct", list(legacy["fields"]))
        # 数量口径也随配置/报告一起输出
        sizing = payload["sizing_policy"]
        self.assertEqual(sizing["policy"], "deterministic")
        self.assertFalse(sizing["confidence_used"])
        self.assertEqual(sizing["model_qty_role"], "upper_bound_only")

    def test_historical_verdict_snapshot_is_not_rewritten(self) -> None:
        # 旧轮次/旧报告里的 tier="strong" 与 15x 是历史快照，展示层按原值输出。
        from workbench_service.risk.models import RiskVerdict

        legacy_snapshot = RiskVerdict(
            verdict="approved",
            approved_qty=Decimal("0.1"),
            leverage=15,
            tier="strong",
        )
        payload = legacy_snapshot.as_dict()
        self.assertEqual(payload["tier"], "strong")
        self.assertEqual(payload["leverage"], 15)
        self.assertEqual(payload["approved_qty"], "0.1")


# ---------------------------------------------------------------------------- T19 夹具


def make_risk_service(
    *, limits: RiskLimits | None = None, ttl_s: int = 120, clock=None
):
    """直接组装风险服务（不经过 HTTP）：临时库 + 真实 Ledger/RiskStateStore。"""

    tmp = tempfile.TemporaryDirectory()
    ledger = Ledger(Path(tmp.name))
    resolved = limits or RiskLimits()
    state = RiskStateStore(ledger, resolved)
    approvals = ApprovalManager(
        ledger, ttl_s=ttl_s, clock=clock or (lambda: 1_000_000.0)
    )
    service = RiskService(resolved, ledger, state, approvals)
    return service, ledger, tmp


def decode_token(token: str) -> dict:
    _version, body, _sig = token.split(".")
    return json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))


class ConfigEnvInjectionTests(unittest.TestCase):
    """入口 1/4：配置读取——env 只能改运维项，任何名字都不能放大授权（T19）。"""

    def test_env_allowlist_contains_no_authorization_switch(self) -> None:
        # 谁能在这张表里加一个 affects_execution_authorization=True 的开关，套件就红。
        self.assertEqual(
            set(AUTHORIZATION_FIELDS),
            {"max_risk_pct", "max_leverage", "max_notional_multiple", "max_margin_usage"},
        )
        for name, meta in ENV_KNOBS.items():
            self.assertIs(
                meta["affects_execution_authorization"],
                False,
                f"{name} 不得影响执行授权",
            )
            self.assertNotIn(meta["field"], AUTHORIZATION_FIELDS, name)
            for token in ("MAX_RISK", "LEVERAGE", "STRONG", "NOTIONAL", "CONFIDENCE"):
                self.assertNotIn(token, name.upper(), f"{name} 的命名像提额开关")

    def test_cap_and_strong_named_env_vars_are_ignored(self) -> None:
        hostile = {
            "WORKBENCH_RISK_MAX_RISK_PCT": "0.5",
            "WORKBENCH_RISK_MAX_LEVERAGE": "50",
            "WORKBENCH_RISK_MAX_NOTIONAL_MULTIPLE": "9",
            "WORKBENCH_RISK_MAX_MARGIN_USAGE": "0.99",
            "WORKBENCH_RISK_STRONG_MAX_RISK_PCT": "0.5",
            "WORKBENCH_RISK_STRONG_MAX_LEVERAGE": "50",
            "WORKBENCH_RISK_STRONG_MIN_CONFIDENCE": "0",
            "WORKBENCH_RISK_CONFIDENCE_TIER": "strong",
            "WORKBENCH_RISK_TIER": "strong",
        }
        with mock.patch.dict(os.environ, hostile, clear=False):
            limits = RiskLimits.from_env()
            verdict = evaluate(
                make_plan(qty="1.0", confidence=1.0),
                make_ctx(),
                limits,
                filters=FILTERS,
            )
        self.assertEqual(limits.max_risk_pct, LIMITS.max_risk_pct)
        self.assertEqual(limits.max_leverage, LIMITS.max_leverage)
        self.assertEqual(limits.max_notional_multiple, LIMITS.max_notional_multiple)
        self.assertEqual(limits.max_margin_usage, LIMITS.max_margin_usage)
        self.assertEqual(limits.strong_max_risk_pct, 0.020)
        self.assertEqual(limits.strong_max_leverage, 15)
        self.assertEqual(verdict.approved_qty, Decimal("0.05"))
        self.assertEqual(verdict.leverage_cap, 10)

    def test_nan_and_inf_env_values_fall_back_to_defaults(self) -> None:
        for raw in ("nan", "inf", "-inf", "1e309", "-999999", "abc"):
            with mock.patch.dict(
                os.environ, {"WORKBENCH_RISK_INITIAL_EQUITY": raw}, clear=False
            ):
                limits = RiskLimits.from_env()
            self.assertTrue(math.isfinite(limits.initial_equity), raw)
            self.assertEqual(limits.initial_equity, 5000.0, raw)

    def test_approval_ttl_env_can_only_tighten(self) -> None:
        cases = {
            "999999999": MAX_APPROVAL_TTL_S,
            "31536000": MAX_APPROVAL_TTL_S,
            "0": 1,
            "-30": 1,
            "60": 60,
            "abc": 120,
        }
        for raw, expected in cases.items():
            with mock.patch.dict(
                os.environ, {"WORKBENCH_RISK_APPROVAL_TTL_S": raw}, clear=False
            ):
                limits = RiskLimits.from_env()
            self.assertEqual(limits.approval_ttl_s, expected, raw)
            self.assertLessEqual(limits.approval_ttl_s, MAX_APPROVAL_TTL_S)

    def test_symbol_env_changes_whitelist_not_caps(self) -> None:
        with mock.patch.dict(
            os.environ, {"WORKBENCH_RISK_SYMBOLS": "btcusdt, ethusdt"}, clear=False
        ):
            limits = RiskLimits.from_env()
        self.assertEqual(limits.symbols, ("BTCUSDT", "ETHUSDT"))
        self.assertEqual(limits.max_risk_pct, LIMITS.max_risk_pct)
        self.assertEqual(limits.max_leverage, LIMITS.max_leverage)

class ApprovalAuthorizationFenceTests(unittest.TestCase):
    """入口 2/4：审批令牌签发/校验——旧令牌重放不得带来超过普通档的授权（T19）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name))
        self.clock = [1_000_000.0]
        self.manager = ApprovalManager(
            self.ledger, ttl_s=120, clock=lambda: self.clock[0]
        )
        self.service, self._svc_ledger, self._svc_tmp = make_risk_service()
        # 用真实风险服务绑定出来的策略（生产路径就是这一条）。
        self.policy = self.service.approvals._authorization
        self.assertIsNotNone(self.policy)

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()
        self._svc_ledger.close()
        self._svc_tmp.cleanup()

    # ------------------------------------------------------------------ 夹具
    def _bind_self_manager(self, *, rules_version: str | None = None) -> None:
        self.manager.bind_authorization(
            rules_version=rules_version or self.policy.rules_version,
            max_leverage=int(LIMITS.max_leverage),
            max_risk_pct=LIMITS.max_risk_pct,
        )

    def _legacy_token(
        self,
        *,
        jti: str,
        approved_qty: str = "0.10",
        leverage: int = 15,
        leverage_cap: int = 15,
        requested_qty: str = "0.20",
        rules_version: str | None = "current",
        confidence_used_for_sizing: bool = False,
        sizing_approved: str | None = None,
    ) -> str:
        """手工签一张"旧版本签发"的令牌（同密钥，模拟旧进程/旧代码签出的令牌）。"""

        plan = make_plan(qty=requested_qty)
        now_ms = int(self.clock[0] * 1000)
        payload = {
            "v": 1,
            "jti": jti,
            "iat_ms": now_ms,
            "exp_ms": now_ms + 120_000,
            "context_ts_ms": now_ms,
            "market": {},
            "plan_hash": plan.plan_hash(),
            "plan": plan.normalized_dict(),
            "approved_qty": approved_qty,
            "leverage": leverage,
            "leverage_cap": leverage_cap,
            "sizing_policy": "deterministic",
            "confidence_used_for_sizing": confidence_used_for_sizing,
            "sizing": {
                "policy": "deterministic",
                "confidence_used": "false",
                "requested_qty": requested_qty,
                "approved_qty": approved_qty if sizing_approved is None else sizing_approved,
            },
        }
        if rules_version == "current":
            payload["rules_version"] = self.policy.rules_version
        elif rules_version is not None:
            payload["rules_version"] = rules_version
        token = self.manager._sign(payload)
        self.ledger.insert_approval(
            {
                "jti": jti,
                "plan_hash": payload["plan_hash"],
                "cycle_id": "unit",
                "approved_qty": approved_qty,
                "leverage": leverage,
                "expires_at_ms": payload["exp_ms"],
            }
        )
        return token

    def _consume(self, token: str):
        """消费令牌并断言被拒：返回 ApprovalError。"""

        with self.assertRaises(ApprovalError) as ctx:
            self.manager.consume(token)
        return ctx.exception

    # ------------------------------------------------------------------ 新令牌
    def test_issued_token_is_bound_to_ordinary_tier(self) -> None:
        plan = make_plan(qty="0.20")
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        issued = self.service.evaluate(plan, make_ctx(), filters=FILTERS)[1]
        self.assertIsNotNone(issued)
        payload = self.service.approvals._verify(issued["token"])
        self.assertEqual(payload["sizing_policy"], "deterministic")
        self.assertIs(payload["confidence_used_for_sizing"], False)
        self.assertEqual(payload["rules_version"], self.policy.rules_version)
        self.assertEqual(Decimal(payload["approved_qty"]), verdict.approved_qty)
        self.assertLessEqual(int(payload["leverage_cap"]), LIMITS.max_leverage)
        self.assertEqual(payload["sizing"]["confidence_used"], "false")
        self.assertEqual(payload["sizing"]["policy"], "deterministic")
        consumed = self.service.approvals.consume(issued["token"], plan=plan)
        self.assertEqual(Decimal(consumed["approved_qty"]), verdict.approved_qty)

    # ------------------------------------------------------------------ 旧令牌
    def test_legacy_token_without_rules_version_is_rejected(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(jti="legacy-no-version", rules_version=None)
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_RULES_CHANGED")
        self.assertEqual(error.status_code, 409)
        self.assertEqual(error.details.get("reason"), "rules_version_missing")
        self.assertTrue(error.details.get("legacy_token"))

    def test_legacy_strong_leverage_token_is_rejected(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(jti="legacy-strong-15x", leverage=15, leverage_cap=15)
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_AUTHORIZATION_EXCEEDED")
        self.assertEqual(error.details.get("field"), "leverage")
        self.assertEqual(error.details.get("regular_max_leverage"), LIMITS.max_leverage)

    def test_legacy_strong_leverage_cap_is_rejected(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(jti="legacy-strong-cap", leverage=8, leverage_cap=15)
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_AUTHORIZATION_EXCEEDED")
        self.assertEqual(error.details.get("field"), "leverage_cap")

    def test_legacy_confidence_sizing_token_is_rejected(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(
            jti="legacy-confidence", leverage=8, leverage_cap=8,
            confidence_used_for_sizing=True,
        )
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_LEGACY_POLICY")
        self.assertEqual(error.details.get("reason"), "sizing_policy_not_deterministic")

    def test_legacy_token_rules_version_mismatch_is_rejected(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(
            jti="legacy-stale-rules", leverage=8, leverage_cap=8,
            rules_version="risk.v1:0000000000000000",
        )
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_RULES_CHANGED")
        self.assertEqual(error.details.get("reason"), "rules_version_mismatch")

    def test_token_approved_qty_must_match_sizing_block(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(
            jti="legacy-mismatch", leverage=8, leverage_cap=8,
            approved_qty="0.10", sizing_approved="0.05",
        )
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_POLICY_VIOLATION")
        self.assertEqual(error.details.get("reason"), "approved_qty_not_from_sizing")

    def test_token_approved_qty_cannot_exceed_requested(self) -> None:
        self._bind_self_manager()
        token = self._legacy_token(
            jti="legacy-above-requested", leverage=8, leverage_cap=8,
            approved_qty="0.10", requested_qty="0.02",
        )
        error = self._consume(token)
        self.assertEqual(error.code, "E_APPROVAL_POLICY_VIOLATION")
        self.assertEqual(error.details.get("reason"), "approved_qty_above_requested")

    def test_rules_change_invalidates_already_issued_token(self) -> None:
        self._bind_self_manager()
        plan = make_plan(qty="0.20")
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        issued = self.manager.issue(plan, verdict)
        # 模拟「P1 之后普通档阈值被改过」（配置变更 → rules_version 变化）。
        self.manager.bind_authorization(
            rules_version="risk.v1:changed000000000",
            max_leverage=int(LIMITS.max_leverage),
            max_risk_pct=LIMITS.max_risk_pct,
        )
        error = self._consume(issued["token"])
        self.assertEqual(error.code, "E_APPROVAL_RULES_CHANGED")
        self.assertEqual(error.details.get("reason"), "rules_version_mismatch")

    def test_fence_only_applies_after_binding(self) -> None:
        # 未绑定（独立用法）行为不变；RiskService 构造即绑定，所以生产不会漏绑。
        plan = make_plan(qty="0.20")
        verdict = evaluate(plan, make_ctx(), LIMITS, filters=FILTERS)
        issued = self.manager.issue(plan, verdict)
        self.assertIsNone(self.manager._authorization)
        self.assertEqual(
            Decimal(self.manager.consume(issued["token"], plan=plan)["approved_qty"]),
            verdict.approved_qty,
        )
        self._bind_self_manager()
        legacy = self._legacy_token(jti="legacy-after-bind", rules_version=None)
        self.assertEqual(self._consume(legacy).code, "E_APPROVAL_RULES_CHANGED")

    def test_risk_service_binds_ordinary_tier_policy(self) -> None:
        policy = self.service.approvals.authorization_policy()
        self.assertTrue(policy["bound"])
        self.assertEqual(policy["max_leverage"], int(LIMITS.max_leverage))
        self.assertEqual(Decimal(policy["max_risk_pct"]), Decimal("0.01"))
        self.assertEqual(policy["rules_version"], self.service.rules_version())
        self.assertFalse(policy["confidence_used_for_sizing"])
        envelope = self.service.envelope({})
        self.assertTrue(envelope["approval"]["policy"]["bound"])
        self.assertTrue(envelope["approval"]["rules_version_bound"])

class EnvelopeAuthorizationMarkerTests(unittest.TestCase):
    """入口 3/4：/api/risk/envelope 输出——哪些字段算授权必须白纸黑字（T19）。"""

    def setUp(self) -> None:
        self.service, self.ledger, self.tmp = make_risk_service()

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def test_authorization_block_marks_non_authorizing_fields(self) -> None:
        envelope = self.service.envelope({"checked_at": "unit"})
        auth = envelope["authorization"]
        self.assertEqual(
            list(auth["execution_authorization_fields"]), list(AUTHORIZATION_FIELDS)
        )
        not_used = set(auth["not_used_for_execution_authorization"])
        for field in (
            "per_trade.strong_max_risk_pct",
            "per_trade.strong_max_leverage",
            "per_trade.strong_min_confidence",
            "per_trade.strong_min_rr",
            "confidence",
        ):
            self.assertIn(field, not_used)
        self.assertFalse(auth["confidence_used_for_sizing"])
        self.assertFalse(auth["approval_token_is_authorization_source"])
        self.assertEqual(auth["sizing_policy"], "deterministic")
        self.assertEqual(auth["effective_caps"]["max_leverage"], int(LIMITS.max_leverage))
        self.assertEqual(auth["effective_caps"]["max_risk_pct"], LIMITS.max_risk_pct)
        # limits 内部（旧配置读取兼容层）也要带同样的标记。
        limits_auth = envelope["limits"]["authorization"]
        self.assertEqual(
            list(limits_auth["execution_authorization_fields"]), list(AUTHORIZATION_FIELDS)
        )
        self.assertFalse(limits_auth["confidence_used_for_sizing"])
        self.assertLessEqual(LIMITS.approval_ttl_s, limits_auth["approval_ttl_max_s"])
        legacy = envelope["legacy_strong_tier"]
        self.assertFalse(legacy["used_for_execution_authorization"])
        self.assertFalse(legacy["confidence_used_for_sizing"])
        self.assertTrue(envelope["approval"]["policy"]["bound"])
        self.assertEqual(envelope["approval"]["ttl_max_s"], MAX_APPROVAL_TTL_S)
        self.assertEqual(
            envelope["approval"]["authorization_model"], "recomputed_at_p1_and_p2"
        )

    def test_env_knobs_are_exported_as_non_authorizing(self) -> None:
        knobs = self.service.envelope({})["env_knobs"]
        self.assertEqual(set(knobs), set(ENV_KNOBS))
        for name, meta in knobs.items():
            self.assertFalse(meta["affects_execution_authorization"], name)

    def test_effective_caps_match_enforced_sizing(self) -> None:
        # 漂移守卫：envelope 报出来的上限必须就是 rules 真正用的那一套。
        caps = self.service.envelope({})["authorization"]["effective_caps"]
        verdict = evaluate(make_plan(qty="5"), make_ctx(), self.service.limits, filters=FILTERS)
        risk_pct = Decimal(str(caps["max_risk_pct"]))
        notional_mult = Decimal(str(caps["max_notional_multiple"]))
        margin_usage = Decimal(str(caps["max_margin_usage"]))
        equity = Decimal("5000")
        entry = Decimal("100000")
        risk_per_unit = entry - Decimal("99000")
        by_risk = equity * risk_pct / risk_per_unit
        by_notional = equity * notional_mult / entry
        by_margin = equity * margin_usage * Decimal(8) / entry
        expected = min(by_risk, by_notional, by_margin)
        step = Decimal("0.001")
        expected = (expected / step).to_integral_value(rounding="ROUND_DOWN") * step
        self.assertEqual(verdict.approved_qty, expected)
        self.assertEqual(verdict.leverage_cap, int(caps["max_leverage"]))


class PreExecutionRecheckTests(unittest.TestCase):
    """入口 4/4：执行前复核（P2 check）——重算、只取 min、从不续签（T19）。"""

    def setUp(self) -> None:
        self.service, self.ledger, self.tmp = make_risk_service()

    def tearDown(self) -> None:
        self.ledger.close()
        self.tmp.cleanup()

    def _approval_rows(self) -> int:
        return len(self.ledger.query("SELECT jti FROM approvals"))

    def test_check_recomputes_and_never_issues_token(self) -> None:
        before = self._approval_rows()
        verdict = self.service.check(
            make_plan(qty="5"), make_ctx(), filters=FILTERS, trace_id="p2"
        )
        self.assertEqual(verdict.approved_qty, Decimal("0.05"))
        self.assertEqual(verdict.verdict, "reduced")
        self.assertEqual(verdict.leverage_cap, int(LIMITS.max_leverage))
        self.assertEqual(self._approval_rows(), before, "P2 不允许自我续签令牌")

    def test_check_rejects_legacy_strong_leverage_plan(self) -> None:
        verdict = self.service.check(
            make_plan(leverage=15, qty="0.20"), make_ctx(), filters=FILTERS
        )
        self.assertEqual(verdict.verdict, "rejected")
        self.assertEqual(verdict.blocking_rule, "R04")
        self.assertEqual(verdict.approved_qty, Decimal("0"))
        self.assertEqual(verdict.leverage_cap, int(LIMITS.max_leverage))

    def test_check_tightens_with_worse_submit_facts(self) -> None:
        tight = self.service.check(
            make_plan(qty="0.20"),
            make_ctx(equity_session=Decimal("2500")),
            filters=FILTERS,
        )
        self.assertEqual(tight.approved_qty, Decimal("0.025"))
        # 令牌里的 0.10 与复算 0.025 取 min 才是可执行数量（execution 侧执行该 min）。
        self.assertLess(tight.approved_qty, Decimal("0.10"))

    def test_check_ignores_confidence_and_model_qty(self) -> None:
        low = self.service.check(make_plan(qty="0.20", confidence=0.1), make_ctx(), filters=FILTERS)
        high = self.service.check(
            make_plan(qty="0.20", confidence=1.0), make_ctx(), filters=FILTERS
        )
        self.assertEqual(low.approved_qty, high.approved_qty)
        self.assertEqual(low.leverage_cap, high.leverage_cap)
        huge = self.service.check(make_plan(qty="100"), make_ctx(), filters=FILTERS)
        self.assertEqual(huge.approved_qty, low.approved_qty)

class NoAmplificationRegressionTests(unittest.TestCase):
    """T19 回归守卫：改坏（重新引入强信心提额 / 改错上限）必须让套件变红。"""

    def test_ordinary_tier_fields_are_the_only_way_to_raise_caps(self) -> None:
        # 变异证明：普通档字段确实生效（所以下面的“strong_* 无效”断言不是空转）。
        base = evaluate(make_plan(qty="1.0"), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(base.approved_qty, Decimal("0.05"))
        doubled = evaluate(
            make_plan(qty="1.0"),
            make_ctx(),
            RiskLimits(max_risk_pct=0.02),
            filters=FILTERS,
        )
        self.assertEqual(doubled.approved_qty, Decimal("0.1"))
        # 杠杆上限：普通档 10x 下 12x 被拒；把普通档改成 15x 就放行（证明该字段生效）。
        capped = evaluate(make_plan(leverage=12), make_ctx(), LIMITS, filters=FILTERS)
        self.assertEqual(capped.blocking_rule, "R04")
        levered = evaluate(
            make_plan(leverage=12),
            make_ctx(),
            RiskLimits(max_leverage=15),
            filters=FILTERS,
        )
        self.assertTrue(levered.approved)
        self.assertEqual(levered.leverage_cap, 15)
        self.assertEqual(levered.approved_qty, Decimal("0.02"))

    def test_legacy_strong_config_and_ai_limits_cannot_amplify(self) -> None:
        baseline = evaluate(make_plan(qty="1.0"), make_ctx(), LIMITS, filters=FILTERS)
        hostile = RiskLimits(strong_max_risk_pct=0.5, strong_max_leverage=50)
        plan = make_plan(
            qty="1.0",
            confidence=1.0,
            ai_limits={
                "strong_max_risk_pct": 0.5,
                "strong_max_leverage": 50,
                "confidence": 1.0,
            },
        )
        attempt = evaluate(plan, make_ctx(), hostile, filters=FILTERS)
        self.assertEqual(attempt.approved_qty, baseline.approved_qty)
        self.assertEqual(attempt.leverage_cap, baseline.leverage_cap)
        self.assertLessEqual(attempt.leverage_cap, LIMITS.max_leverage)

    def test_confidence_one_is_identical_to_confidence_zero(self) -> None:
        results = []
        for confidence in (0.0, 1.0):
            verdict = evaluate(
                make_plan(qty="1.0", confidence=confidence),
                make_ctx(),
                LIMITS,
                filters=FILTERS,
            )
            results.append(
                (verdict.verdict, verdict.approved_qty, verdict.leverage, verdict.leverage_cap)
            )
            self.assertEqual(verdict.tier, "regular")
            self.assertEqual(verdict.sizing["confidence_used"], "false")
        self.assertEqual(results[0], results[1])

    def test_strong_tier_reintroduction_would_be_caught(self) -> None:
        # 若有人把「confidence >= 0.65 → 2% / 15x」重新加回 rules，这三条断言必红：
        # ① 任何 confidence 下 approved_qty 都是普通档公式值；② tier 恒 regular；
        # ③ leverage_cap 恒 ≤ 普通档上限（交易所上限更大时也不放宽）。
        for confidence in (0.0, 0.65, 0.72, 0.99, 1.0):
            verdict = evaluate(
                make_plan(qty="1.0", confidence=confidence),
                make_ctx(),
                LIMITS,
                filters=FILTERS,
            )
            self.assertEqual(verdict.approved_qty, Decimal("0.05"), confidence)
            self.assertEqual(verdict.tier, "regular", confidence)
            self.assertEqual(verdict.leverage_cap, int(LIMITS.max_leverage), confidence)
            self.assertLess(verdict.leverage_cap, FILTERS.exchange_max_leverage)


class HttpAuthorizationSurfaceTests(unittest.TestCase):
    """入口 3/4 的 HTTP 面：envelope 标记可读、旧令牌重放不能放大真实下单数量。"""

    def test_envelope_endpoint_exposes_authorization_markers(self) -> None:
        case = ServiceCase(with_fake=False)
        try:
            client = case.client()
            with client:
                response = client.get("/api/risk/envelope")
            self.assertEqual(response.status_code, 200, response.text)
            body = response.json()
            auth = body["authorization"]
            self.assertEqual(
                list(auth["execution_authorization_fields"]), list(AUTHORIZATION_FIELDS)
            )
            self.assertIn(
                "per_trade.strong_max_leverage",
                auth["not_used_for_execution_authorization"],
            )
            self.assertIn("confidence", auth["not_used_for_execution_authorization"])
            self.assertFalse(auth["confidence_used_for_sizing"])
            self.assertFalse(auth["approval_token_is_authorization_source"])
            self.assertEqual(auth["effective_caps"]["max_leverage"], 10)
            self.assertEqual(auth["effective_caps"]["max_risk_pct"], 0.01)
            self.assertTrue(body["approval"]["policy"]["bound"])
            self.assertEqual(body["approval"]["policy"]["max_leverage"], 10)
            self.assertLessEqual(
                body["approval"]["ttl_s"], body["approval"]["ttl_max_s"]
            )
            self.assertEqual(set(body["env_knobs"]), set(ENV_KNOBS))
            for meta in body["env_knobs"].values():
                self.assertFalse(meta["affects_execution_authorization"])
        finally:
            case.close()

    def test_replayed_strong_sizing_token_cannot_raise_order_qty(self) -> None:
        # 模拟“旧版本签发的令牌”：approved_qty/sizing 都写着 0.10（强信心档 2%）。
        # 令牌能通过签名与授权证伪，但 P2 用提交时刻事实重算 0.05 并取 min，
        # 所以真实下单数量只能是 0.05——这就是「没有第二条放大路径」的证据。
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = support_approve(
                    client, plan_payload=support_plan(qty="0.20")
                )
                payload = decode_token(approval["approval"]["token"])
                self.assertEqual(Decimal(payload["approved_qty"]), Decimal("0.05"))
                payload["approved_qty"] = "0.10"
                payload["sizing"]["approved_qty"] = "0.10"
                payload["sizing"]["risk_budget"] = "100"
                forged = case.service.approvals._sign(payload)
                response = client.post(
                    "/api/execution/order", json={"approval_token": forged}
                )
            self.assertEqual(response.status_code, 201, response.text)
            orders = list(case.fake.orders.values())
            self.assertEqual(len(orders), 1, orders)
            self.assertEqual(orders[0]["origQty"], "0.05")
            self.assertEqual(case.fake.positions["BTCUSDT"]["qty"], Decimal("0.05"))
        finally:
            case.close()

    def test_forged_leverage_above_regular_cap_token_is_rejected(self) -> None:
        case = ServiceCase(with_fake=True)
        try:
            client = case.client()
            with client:
                approval = support_approve(client)
                payload = decode_token(approval["approval"]["token"])
                payload["leverage"] = 15
                payload["leverage_cap"] = 15
                forged = case.service.approvals._sign(payload)
                response = client.post(
                    "/api/execution/order", json={"approval_token": forged}
                )
            self.assertEqual(response.status_code, 403, response.text)
            error = response.json()["error"]
            self.assertEqual(error["code"], "E_APPROVAL_AUTHORIZATION_EXCEEDED")
            self.assertEqual(error["details"]["field"], "leverage")
            self.assertEqual(error["details"]["regular_max_leverage"], 10)
            self.assertEqual(len(case.fake.orders), 0, "被拒令牌不得下单")
        finally:
            case.close()

if __name__ == "__main__":
    unittest.main()
