"""确定性硬风控引擎（R00–R25，docs/03 §7.3/§7.4）。

设计要点：
- **纯函数**：不读网络、不写库、不依赖时间（时间相关量由 ``RiskContext`` 传入）；
- 顺序：先拒结构性错误（白名单/方向/状态/数据），再对数量类做降仓（风险/名义/保证金），
  降仓后必须重算最小名义（否则会出现"21U 降到 19U 被交易所拒单"的循环）；
- **AI 只能收紧**（R00）：``ai_limits`` 只参与 ``min()``，放宽值被忽略并记录；
- 任何"未证明"（缺 mark/缺新鲜度/缺保护状态）都落到拒绝，绝不放行。

§8·A（T02 权限收口）：
- ``confidence`` 只是分析字段：**不再有强信心档**，风险预算与杠杆上限只有普通档一套，
  同一计划的允许数量/杠杆上限不随 confidence 变化；
- 允许数量由确定性公式给出：``min(风险预算 / 入场到止损距离, 名义上限 / 入场,
  可用保证金 × 杠杆 / 入场)``，再按交易所 stepSize **向下取整**；
  模型数量只能是更保守的上限（``min(模型申报, 计算结果)``），放大不了任何约束；
- 达不到最小数量/最小名义时**拒绝**（R12 / `E_RISK_BELOW_MIN`），
  绝不为凑最小名义反向放大风险；
- 止损预算（风险预算 = 权益 × risk_pct，风险额 = 数量 × |入场−止损|）是**计划约束描述**，
  不是极端行情下的最大实际损失保证（滑点/跳空/资金费另计）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from ..ledger.numbers import dec_text, floor_to_step
from .config import RiskLimits, SIZING_POLICY
from .models import (
    OrderPlan,
    RiskContext,
    RiskVerdict,
    RuleViolation,
    SymbolFiltersView,
)

__all__ = ["evaluate", "BLOCKING_CODES", "effective_limits"]

#: R 编号 → 对外错误码（APi 层直接用）。
BLOCKING_CODES: dict[str, str] = {
    "R01": "E_RISK_SYMBOL",
    "R02": "E_RISK_SYMBOL_POSITION",
    "R03": "E_RISK_MAX_POSITIONS",
    "R04": "E_RISK_LEVERAGE",
    "R05": "E_RISK_PER_TRADE",
    "R06": "E_RISK_NOTIONAL",
    "R07": "E_RISK_MARGIN",
    "R08": "E_RISK_SLTP",
    "R09": "E_RISK_SL_DISTANCE",
    "R10": "E_RISK_RR",
    "R11": "E_RISK_LIQ_DISTANCE",
    "R12": "E_RISK_BELOW_MIN",
    "R13": "E_RISK_PRECISION",
    "R14": "E_MARKET_STALE",
    "R15": "E_MARKET_ANOMALY",
    "R16": "E_DUPLICATE_ORDER",
    "R17": "E_OUTBOX_PENDING",
    "R18": "E_RISK_DAILY_LOSS_LATCHED",
    "R19": "E_RISK_DRAWDOWN_LATCHED",
    "R20": "E_RISK_LOSS_STREAK",
    "R21": "E_RISK_FREQUENCY",
    "R22": "E_CONTROL_LOCKED",
    "R23": "E_PROTECTION_MISSING",
    "R24": "E_RISK_MARGIN_RATIO",
    "R25": "E_EQUITY_UNAVAILABLE",
}


@dataclass(frozen=True, slots=True)
class EffectiveLimits:
    """AI 建议与硬上限取 ``min`` 之后的实际生效值（R00 的输出）。

    §8·A：没有强信心档，风险比例与杠杆只有一套普通档上限；
    ``confidence`` 与 ``strong_*`` 都不参与这里的取值。
    """

    risk_pct: Decimal
    max_leverage: int
    max_notional_multiple: Decimal


def _dec(value: float | int | str | Decimal | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - 配置值来自内部常量
        return Decimal(default)


def effective_limits(
    plan: OrderPlan, limits: RiskLimits, violations: list[RuleViolation]
) -> EffectiveLimits:
    """R00：AI 建议只能收紧。"""

    # §8·A：只用普通档上限。strong_max_risk_pct / strong_max_leverage 是 legacy
    # 兼容字段，不参与取值，也不受 confidence 影响。
    risk_pct = _dec(limits.max_risk_pct)
    max_leverage = int(limits.max_leverage)
    notional_multiple = _dec(limits.max_notional_multiple)

    ai = plan.ai_limits or {}

    def _positive(value: Any) -> Decimal | None:
        try:
            parsed = Decimal(str(value))
        except Exception:  # noqa: BLE001
            return None
        return parsed if parsed > 0 else None

    ai_risk = _positive(ai.get("max_risk_pct"))
    if ai_risk is not None:
        if ai_risk < risk_pct:
            violations.append(
                RuleViolation("R00", "tighten", f"AI 收紧单笔风险 {risk_pct} -> {ai_risk}")
            )
            risk_pct = ai_risk
        elif ai_risk > risk_pct:
            violations.append(
                RuleViolation("R00", "tighten", f"AI 放宽单笔风险被忽略（{ai_risk} > {risk_pct}）")
            )

    # ai_limits.strong_* 一律忽略：强信心档已取消，模型不能借旧字段提额。
    try:
        ai_leverage = int(ai.get("max_leverage")) if ai.get("max_leverage") is not None else None
    except (TypeError, ValueError):
        ai_leverage = None
    if ai_leverage is not None:
        if 0 < ai_leverage < max_leverage:
            violations.append(
                RuleViolation("R00", "tighten", f"AI 收紧杠杆 {max_leverage} -> {ai_leverage}")
            )
            max_leverage = ai_leverage
        elif ai_leverage > max_leverage:
            violations.append(
                RuleViolation("R00", "tighten", f"AI 放宽杠杆被忽略（{ai_leverage} > {max_leverage}）")
            )

    ai_notional = _positive(ai.get("max_notional_multiple"))
    if ai_notional is not None:
        if ai_notional < notional_multiple:
            violations.append(
                RuleViolation(
                    "R00", "tighten", f"AI 收紧名义上限 {notional_multiple} -> {ai_notional}"
                )
            )
            notional_multiple = ai_notional
        elif ai_notional > notional_multiple:
            violations.append(
                RuleViolation(
                    "R00", "tighten", f"AI 放宽名义上限被忽略（{ai_notional} > {notional_multiple}）"
                )
            )

    return EffectiveLimits(
        risk_pct=risk_pct,
        max_leverage=max_leverage,
        max_notional_multiple=notional_multiple,
    )


def _reject(
    violations: list[RuleViolation],
    rule: str,
    detail: str,
) -> None:
    violations.append(RuleViolation(rule, "reject", detail))


def evaluate(
    plan: OrderPlan,
    ctx: RiskContext,
    limits: RiskLimits,
    *,
    filters: SymbolFiltersView | None = None,
) -> RiskVerdict:
    """对一份开仓计划做完整硬风控；返回 approved/reduced/rejected。"""

    violations: list[RuleViolation] = []
    eff = effective_limits(plan, limits, violations)

    # ---------------------------------------------------------------- R25 权益
    equity = ctx.equity_session
    if equity is None or equity <= 0:
        _reject(violations, "R25", "账户权益不可用（equity_session 缺失或为 0）")
    if ctx.equity_age_ms is None:
        _reject(violations, "R25", "权益新鲜度未证明（equity_age_ms 缺失）")
    elif ctx.equity_age_ms > limits.equity_block_new_ms:
        _reject(violations, "R25", f"权益已过期 {ctx.equity_age_ms}ms")

    # ---------------------------------------------------------------- R22 控制态
    if ctx.control_state != "normal":
        _reject(violations, "R22", f"控制态 {ctx.control_state} 禁止新开仓")

    # ---------------------------------------------------------------- R18/R19/R20
    if ctx.daily_loss_latched:
        _reject(violations, "R18", "当日亏损已达熔断线（close_only）")
    if ctx.drawdown_latched:
        _reject(violations, "R19", "会话回撤已达 8%（emergency）")
    if limits.enable_loss_streak and ctx.cooldown_active:
        _reject(violations, "R20", "连亏冷却中")

    # ---------------------------------------------------------------- R21 频率
    if limits.enable_frequency:
        if ctx.trades_today >= limits.max_trades_per_day:
            _reject(violations, "R21", f"当日开仓 {ctx.trades_today} ≥ {limits.max_trades_per_day}")
        elif ctx.trades_symbol_today >= limits.max_trades_per_symbol_per_day:
            _reject(
                violations,
                "R21",
                f"{plan.symbol} 当日开仓 {ctx.trades_symbol_today} ≥ "
                f"{limits.max_trades_per_symbol_per_day}",
            )

    # ---------------------------------------------------------------- R01 白名单
    if plan.symbol not in limits.symbols:
        _reject(violations, "R01", f"{plan.symbol} 不在交易范围 {list(limits.symbols)}")
    if filters is not None and filters.status.upper() != "TRADING":
        _reject(violations, "R01", f"{plan.symbol} 交易所状态 {filters.status!r}")

    # ---------------------------------------------------------------- R02/R03 仓位
    same_symbol = [pos for pos in ctx.open_positions if pos.symbol == plan.symbol]
    if same_symbol:
        sides = ", ".join(f"{pos.position_side}" for pos in same_symbol)
        _reject(violations, "R02", f"{plan.symbol} 已有仓位（{sides}），v1 不加仓/不反手")
    if len(ctx.open_positions) + 1 > limits.max_open_positions:
        _reject(
            violations,
            "R03",
            f"当前持仓 {len(ctx.open_positions)} 个，上限 {limits.max_open_positions}",
        )

    # ---------------------------------------------------------------- R04 杠杆
    # §8·A：杠杆上限 = 普通档上限（AI 只能收紧）与交易所上限取小；confidence 不提额。
    exchange_max = filters.exchange_max_leverage if filters else None
    leverage_cap = eff.max_leverage if exchange_max is None else min(
        eff.max_leverage, int(exchange_max)
    )
    if plan.leverage <= 0:
        _reject(violations, "R04", f"非法杠杆 {plan.leverage}")
    elif plan.leverage > eff.max_leverage:
        _reject(
            violations,
            "R04",
            f"杠杆 {plan.leverage}x 超过普通档上限 {eff.max_leverage}x（confidence 不提高上限）",
        )
    if exchange_max is not None and plan.leverage > exchange_max:
        _reject(violations, "R04", f"杠杆 {plan.leverage}x 超过交易所上限 {exchange_max}x")

    # ---------------------------------------------------------------- R14 数据新鲜度
    entry = ctx.mark
    if entry is None or entry <= 0:
        _reject(violations, "R14", "缺少有效标记价 mark，无法评估")
    if ctx.snapshot_age_ms is None:
        _reject(violations, "R14", "下单快照新鲜度未证明（snapshot_age_ms 缺失）")
    elif ctx.snapshot_age_ms > limits.entry_snapshot_max_age_ms:
        _reject(
            violations,
            "R14",
            f"行情快照已过期 {ctx.snapshot_age_ms}ms > {limits.entry_snapshot_max_age_ms}ms",
        )
    if ctx.evidence_age_ms is None:
        _reject(violations, "R14", "决策证据新鲜度未证明（evidence_age_ms 缺失）")
    elif ctx.evidence_age_ms > limits.decision_evidence_max_age_ms:
        _reject(
            violations,
            "R14",
            f"决策证据已过期 {ctx.evidence_age_ms}ms > "
            f"{limits.decision_evidence_max_age_ms}ms",
        )

    # ---------------------------------------------------------------- R15 异常行情
    _check_anomaly(ctx, limits, violations)

    # ---------------------------------------------------------------- R17 未决订单
    if limits.unknown_block_new_order and (ctx.unknown_orders > 0 or ctx.submitting_orders > 0):
        _reject(
            violations,
            "R17",
            f"存在未决订单（unknown={ctx.unknown_orders}, submitting={ctx.submitting_orders}）",
        )

    # ---------------------------------------------------------------- R16 重复单
    business_key = f"ENTRY:{plan.symbol}:{plan.position_side}"
    if business_key in ctx.pending_business_keys:
        _reject(violations, "R16", f"业务键 {business_key} 已有在途开仓单")

    # ---------------------------------------------------------------- R23 保护覆盖
    if ctx.protection_ok is None:
        _reject(violations, "R23", "持仓保护覆盖未证明")
    elif not ctx.protection_ok:
        missing = ", ".join(ctx.protection_missing) or "unknown"
        _reject(violations, "R23", f"存在保护缺失的持仓：{missing}")

    # ---------------------------------------------------------------- R24 保证金率
    if ctx.maint_margin_ratio is not None and ctx.maint_margin_ratio >= _dec(0.80):
        _reject(violations, "R24", f"维持保证金率 {ctx.maint_margin_ratio} ≥ 0.80")

    # ---------------------------------------------------------------- R08-R11 价格
    rr: Decimal | None = None
    if entry is not None and entry > 0:
        if plan.sl <= 0 or plan.tp <= 0:
            _reject(violations, "R08", "开仓必须同时提供 SL 与 TP")
        else:
            if plan.side == "LONG" and not (plan.sl < entry < plan.tp):
                _reject(
                    violations,
                    "R08",
                    f"LONG 需 SL<entry<TP，当前 SL={plan.sl}, entry={entry}, TP={plan.tp}",
                )
            elif plan.side == "SHORT" and not (plan.tp < entry < plan.sl):
                _reject(
                    violations,
                    "R08",
                    f"SHORT 需 TP<entry<SL，当前 TP={plan.tp}, entry={entry}, SL={plan.sl}",
                )
            else:
                risk_per_unit = abs(entry - plan.sl)
                sl_pct = risk_per_unit / entry
                if sl_pct < _dec(limits.sl_min_distance_pct):
                    _reject(
                        violations,
                        "R09",
                        f"止损距离 {sl_pct:.4%} < 下限 {limits.sl_min_distance_pct:.2%}",
                    )
                elif sl_pct > _dec(limits.sl_max_distance_pct):
                    _reject(
                        violations,
                        "R09",
                        f"止损距离 {sl_pct:.4%} > 上限 {limits.sl_max_distance_pct:.2%}",
                    )
                rr = plan.risk_reward(entry)
                if rr is None or rr < _dec(limits.min_risk_reward):
                    _reject(violations, "R10", f"盈亏比 {rr} < {limits.min_risk_reward}")
                leverage = Decimal(max(1, plan.leverage))
                if ctx.mmr is None:
                    # 无法取得逐 symbol 维持保证金率：不能假设 1%，直接拒绝 R11。
                    _reject(
                        violations,
                        "R11",
                        "无法确认该 symbol 的维持保证金率（leverageBracket 不可用），拒绝开仓",
                    )
                else:
                    liq_distance = (Decimal(1) / leverage) - ctx.mmr
                    if liq_distance < _dec(limits.min_liquidation_distance_multiple) * sl_pct:
                        _reject(
                            violations,
                            "R11",
                            f"爆仓距离 {liq_distance:.4%} < "
                            f"{limits.min_liquidation_distance_multiple}×SL({sl_pct:.4%})",
                        )

    if violations and any(item.action == "reject" for item in violations):
        return _rejected(plan, violations, rr, leverage_cap=leverage_cap)

    assert entry is not None and equity is not None  # 前置检查已保证

    # ---------------------------------------------------------------- 确定性数量
    # §8·A.3：允许数量只由确定性代码给出：
    #   风险预算 = 权益 × max_risk_pct                → 数量上限 = 风险预算 / |入场−止损|
    #   名义上限 = 权益 × max_notional_multiple       → 数量上限 = 名义上限 / 入场
    #   可用保证金 = min(权益×max_margin_usage − 已用, 交易所 availableBalance)
    #                                                  → 数量上限 = 可用保证金 × 杠杆 / 入场
    # 模型 qty 只能是更保守的申报上限（取 min 后不可能放大任何约束）；
    # confidence 不进入任何一个公式。
    requested = plan.qty
    risk_pct = eff.risk_pct
    risk_per_unit = abs(entry - plan.sl)
    step = filters.step_size if filters is not None else Decimal("0.001")
    min_qty = filters.min_qty if filters is not None else Decimal("0")
    max_qty = filters.max_qty if filters is not None else None
    min_notional = _dec(limits.min_notional_usdt)
    if filters is not None:
        min_notional = max(min_notional, filters.min_notional)

    risk_budget = equity * risk_pct
    qty_by_risk = risk_budget / risk_per_unit
    qty_by_notional = (equity * eff.max_notional_multiple) / entry
    margin_available = equity * _dec(limits.max_margin_usage) - ctx.used_margin
    if ctx.available_margin is not None:
        margin_available = min(margin_available, ctx.available_margin)
    if margin_available <= 0:
        _reject(
            violations,
            "R07",
            f"可用保证金不足（权益 {equity}×{limits.max_margin_usage} − 已用保证金 "
            f"{ctx.used_margin}，或交易所 availableBalance ≤ 0）",
        )
        return _rejected(plan, violations, rr, leverage_cap=leverage_cap)
    qty_by_margin = (margin_available * Decimal(max(1, plan.leverage))) / entry

    qty = requested
    if qty > qty_by_risk:
        qty = qty_by_risk
        violations.append(
            RuleViolation(
                "R05",
                "downsize",
                f"单笔风险超限：qty {requested} -> {qty.quantize(Decimal('0.00000001'))}"
                f"（风险预算 {dec_text(risk_budget)}U / 入场到止损 {dec_text(risk_per_unit)}U）",
            )
        )
    if qty > qty_by_notional:
        qty = qty_by_notional
        violations.append(
            RuleViolation(
                "R06",
                "downsize",
                f"名义金额超限：qty 降至 {qty.quantize(Decimal('0.00000001'))}",
            )
        )
    if qty > qty_by_margin:
        qty = qty_by_margin
        violations.append(
            RuleViolation(
                "R07",
                "downsize",
                f"保证金占用超限：qty 降至 {qty.quantize(Decimal('0.00000001'))}"
                f"（可用保证金 {dec_text(margin_available)}U）",
            )
        )

    deterministic_cap = min(qty_by_risk, qty_by_notional, qty_by_margin)
    binding = "model_qty"
    if deterministic_cap < requested:
        binding = min(
            (
                ("R05_risk_budget", qty_by_risk),
                ("R06_notional_cap", qty_by_notional),
                ("R07_available_margin", qty_by_margin),
            ),
            key=lambda item: item[1],
        )[0]

    # ---------------------------------------------------------------- R13 精度
    snapped = floor_to_step(qty, step)
    if snapped != qty:
        violations.append(
            RuleViolation("R13", "adjust", f"数量按 stepSize={step} 向下取整 {qty} -> {snapped}")
        )
    qty = snapped

    # ---------------------------------------------------------------- R12 最小名义
    if max_qty is not None and qty > max_qty:
        qty = floor_to_step(max_qty, step)
        binding = "exchange_max_qty"
        violations.append(
            RuleViolation("R12", "adjust", f"数量超过单笔上限 {max_qty}，已下调到 {qty}")
        )
    notional = qty * entry
    if qty <= 0:
        _reject(
            violations,
            "R12",
            "确定性计算后的允许数量为 0（风险预算/名义上限/可用保证金约束）",
        )
    elif qty < min_qty:
        _reject(
            violations,
            "R12",
            f"允许数量 {qty} < minQty {min_qty}（预算决定的定量已到精度下限，"
            "不放大风险凑最小数量）",
        )
    elif notional < min_notional:
        # §8·A.3：达不到最小订单要求就拒绝，绝不为凑最小名义反向放大风险。
        _reject(
            violations,
            "R12",
            f"允许数量 {qty} × 入场 {dec_text(entry)} = 名义 {notional:.2f} "
            f"< 最小名义 {min_notional}（风险预算 {dec_text(risk_budget)}U，"
            f"命中约束 {binding}）；拒绝而不是放大风险凑最小名义",
        )

    sizing = {
        "policy": "deterministic",
        "model_qty_role": str(SIZING_POLICY["model_qty_role"]),
        "confidence_used": "false",
        "requested_qty": dec_text(requested),
        "risk_budget": dec_text(risk_budget),
        "risk_per_unit": dec_text(risk_per_unit),
        "max_qty_by_risk": dec_text(qty_by_risk),
        "max_qty_by_notional": dec_text(qty_by_notional),
        "max_qty_by_margin": dec_text(qty_by_margin),
        "margin_available": dec_text(margin_available),
        "step_size": dec_text(step),
        "min_qty": dec_text(min_qty),
        "min_notional": dec_text(min_notional),
        "approved_qty": dec_text(qty),
        "binding_constraint": binding,
    }

    if any(item.action == "reject" for item in violations):
        return _rejected(plan, violations, rr, leverage_cap=leverage_cap, sizing=sizing)

    risk_amount = qty * risk_per_unit
    verdict = "reduced" if qty < requested else "approved"
    return RiskVerdict(
        verdict=verdict,
        approved_qty=qty,
        leverage=plan.leverage,
        violations=tuple(violations),
        risk_amount=risk_amount,
        risk_pct=(risk_amount / equity) if equity else None,
        notional=notional,
        rr=rr,
        # 历史快照兼容字段：新裁决恒为 regular（不再有强信心提额档）。
        tier="regular",
        leverage_cap=leverage_cap,
        sizing=sizing,
    )


def _rejected(
    plan: OrderPlan,
    violations: list[RuleViolation],
    rr: Decimal | None,
    *,
    leverage_cap: int = 0,
    sizing: Mapping[str, str] | None = None,
) -> RiskVerdict:
    first = next(item for item in violations if item.action == "reject")
    return RiskVerdict(
        verdict="rejected",
        approved_qty=Decimal("0"),
        leverage=plan.leverage,
        violations=tuple(violations),
        rr=rr,
        blocking_rule=first.rule,
        blocked_reason=first.detail,
        leverage_cap=leverage_cap,
        sizing=dict(sizing or {}),
    )


def _check_anomaly(
    ctx: RiskContext, limits: RiskLimits, violations: list[RuleViolation]
) -> None:
    fields = (
        ("mark_move_60s_pct", ctx.mark_move_60s_pct, _dec(limits.mark_move_60s_pct), "60s 标记价变动"),
        (
            "mark_index_deviation_pct",
            ctx.mark_index_deviation_pct,
            _dec(limits.mark_index_deviation_pct),
            "标记-指数偏离",
        ),
        ("spread_bps", ctx.spread_bps, _dec(limits.max_spread_bps), "买卖价差(bp)"),
    )
    for _name, value, threshold, label in fields:
        if value is None:
            if limits.require_anomaly_data:
                _reject(violations, "R15", f"{label} 未提供（fail-closed）")
            continue
        if value < 0:
            _reject(violations, "R15", f"{label} 为负值 {value}")
            continue
        if value > threshold:
            _reject(violations, "R15", f"{label} {value} > 阈值 {threshold}")
