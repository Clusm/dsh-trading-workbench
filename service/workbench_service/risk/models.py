"""风控数据模型：订单计划、上下文、规则违规与裁决。

字段口径对齐 docs/03 §8.2（OrderPlan / RiskVerdict）与 §12（跨文档契约）：
- ``OrderPlan`` 不携带 ``clientOrderId``（由执行器派生，AI 不生成）；
- ``cycle_id`` 属于请求信封但不是 plan 的必填字段：缺失时由服务生成。

§8·A（T02 权限收口）：
- ``confidence`` 只是分析字段，不参与限额选择；
- ``OrderPlan.qty`` 是模型"申报上限"，不是授权数量：允许数量由 ``rules.evaluate``
  按风险预算 / 入场到止损距离 / 名义上限 / 可用保证金 / 交易所精度确定性计算
  （``RiskVerdict.sizing`` 记录算式，``RiskVerdict.approved_qty`` 才是可执行数量）；
- ``RiskVerdict.tier`` 仅为历史快照兼容字段（旧报告里的 ``tier="strong"`` 按原
  快照展示，不重写旧交易解释）；新裁决恒为 ``"regular"``，不再代表授权档位。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from ..ledger.numbers import to_decimal

__all__ = [
    "PlanValidationError",
    "OrderPlan",
    "PositionView",
    "RiskContext",
    "RuleViolation",
    "RiskVerdict",
    "SymbolFiltersView",
]

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}USDT$")
_SIDES = {"LONG", "SHORT"}
_ACTIONS = {"open"}
_ENTRY_TYPES = {"MARKET"}


class PlanValidationError(ValueError):
    """计划 JSON 不合法（schema 层，非风控层）。"""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def _require(payload: Mapping[str, Any], key: str) -> Any:
    if key not in payload:
        raise PlanValidationError(f"missing field: {key}", details={"field": key})
    return payload[key]


@dataclass(frozen=True, slots=True)
class OrderPlan:
    """一份可执行的市价开仓计划（已通过结构校验，未通过风控）。

    ``qty`` 是模型的申报上限；执行数量由风控按确定性公式重算
    （见 ``RiskVerdict.approved_qty`/``sizing``）。模型给小数量只会更保守，
    给大数量不能绕过风险预算、名义上限与可用保证金。
    """

    symbol: str
    side: str
    leverage: int
    qty: Decimal
    sl: Decimal
    tp: Decimal
    confidence: float = 0.0
    reasoning_ref: str = ""
    cycle_id: str = ""
    action: str = "open"
    entry_type: str = "MARKET"
    ai_limits: Mapping[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any], *, cycle_id: str | None = None
    ) -> "OrderPlan":
        if not isinstance(payload, Mapping):
            raise PlanValidationError("plan must be an object")
        symbol = str(_require(payload, "symbol") or "").strip().upper()
        if not _SYMBOL_RE.match(symbol):
            raise PlanValidationError(
                f"invalid symbol: {symbol!r}", details={"symbol": symbol}
            )
        side = str(_require(payload, "side") or "").strip().upper()
        if side not in _SIDES:
            raise PlanValidationError(f"side must be LONG/SHORT, got {side!r}")
        action = str(payload.get("action") or "open").strip().lower()
        if action not in _ACTIONS:
            raise PlanValidationError(f"action must be 'open', got {action!r}")
        entry_type = str(payload.get("entry_type") or "MARKET").strip().upper()
        if entry_type not in _ENTRY_TYPES:
            raise PlanValidationError(
                f"entry_type must be MARKET (limit entries are v1.1), got {entry_type!r}"
            )
        try:
            leverage = int(payload.get("leverage") or 0)
        except (TypeError, ValueError) as exc:
            raise PlanValidationError("leverage must be an integer") from exc
        qty = to_decimal(payload.get("qty"))
        sl = to_decimal(payload.get("sl"))
        tp = to_decimal(payload.get("tp"))
        if qty is None or qty <= 0:
            raise PlanValidationError("qty must be a positive decimal")
        if sl is None or sl <= 0:
            raise PlanValidationError("sl must be a positive decimal")
        if tp is None or tp <= 0:
            raise PlanValidationError("tp must be a positive decimal")
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence))
        ai_limits_raw = payload.get("ai_limits")
        ai_limits = dict(ai_limits_raw) if isinstance(ai_limits_raw, Mapping) else {}
        return cls(
            symbol=symbol,
            side=side,
            leverage=leverage,
            qty=qty,
            sl=sl,
            tp=tp,
            confidence=confidence,
            reasoning_ref=str(payload.get("reasoning_ref") or "")[:200],
            cycle_id=str(cycle_id or payload.get("cycle_id") or ""),
            action=action,
            entry_type=entry_type,
            ai_limits=ai_limits,
        )

    # ------------------------------------------------------------------ 派生
    def entry_price(self, mark: Decimal) -> Decimal:
        return mark

    @property
    def position_side(self) -> str:
        return "LONG" if self.side == "LONG" else "SHORT"

    def risk_per_unit(self, mark: Decimal) -> Decimal:
        return abs(self.entry_price(mark) - self.sl)

    def reward_per_unit(self, mark: Decimal) -> Decimal:
        entry = self.entry_price(mark)
        return (self.tp - entry) if self.side == "LONG" else (entry - self.tp)

    def risk_reward(self, mark: Decimal) -> Decimal | None:
        risk = self.risk_per_unit(mark)
        if risk <= 0:
            return None
        return self.reward_per_unit(mark) / risk

    def normalized_dict(self) -> dict[str, Any]:
        """稳定序列化（plan hash 与审批记录用）。"""

        return {
            "symbol": self.symbol,
            "side": self.side,
            "action": self.action,
            "entry_type": self.entry_type,
            "leverage": self.leverage,
            "qty": format(self.qty.normalize(), "f"),
            "sl": format(self.sl.normalize(), "f"),
            "tp": format(self.tp.normalize(), "f"),
            "confidence": round(self.confidence, 6),
            "reasoning_ref": self.reasoning_ref,
            "cycle_id": self.cycle_id,
            "ai_limits": _stable_json(self.ai_limits),
        }

    def plan_hash(self) -> str:
        canonical = json.dumps(
            self.normalized_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _stable_json(value: Any) -> Any:
    """把任意 JSON 值稳定化（Decimal→str，dict 排序）。"""

    if isinstance(value, Mapping):
        return {str(key): _stable_json(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [_stable_json(item) for item in value]
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return round(value, 10)
    return value


@dataclass(frozen=True, slots=True)
class SymbolFiltersView:
    """风控需要的合约过滤器（由执行层从 ``exchangeInfo`` 解析后传入）。"""

    symbol: str
    step_size: Decimal = Decimal("0.001")
    tick_size: Decimal = Decimal("0.01")
    min_qty: Decimal = Decimal("0")
    min_notional: Decimal = Decimal("5")
    max_qty: Decimal | None = None
    exchange_max_leverage: int | None = None
    status: str = "TRADING"


@dataclass(frozen=True, slots=True)
class PositionView:
    symbol: str
    position_side: str
    qty: Decimal
    margin: Decimal | None = None
    origin: str = "OPENED_BY_WORKBENCH"

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "position_side": self.position_side,
            "qty": format(self.qty.normalize(), "f"),
            "margin": None if self.margin is None else format(self.margin.normalize(), "f"),
            "origin": self.origin,
        }


@dataclass(frozen=True, slots=True)
class RiskContext:
    """P1/P2 共用的确定性输入；缺失关键事实按“未证明”处理（fail-closed）。"""

    mark: Decimal | None = None
    snapshot_age_ms: int | None = None
    evidence_age_ms: int | None = None
    equity_session: Decimal | None = None
    equity_age_ms: int | None = None
    available_margin: Decimal | None = None
    used_margin: Decimal = Decimal("0")
    open_positions: tuple[PositionView, ...] = ()
    day_start_equity: Decimal | None = None
    peak_equity: Decimal | None = None
    daily_loss_latched: bool = False
    drawdown_latched: bool = False
    control_state: str = "normal"
    position_mode: str = "ONE_WAY"
    maint_margin_ratio: Decimal | None = None
    #: None 表示逐 symbol MMR 未证明；R11 会 fail-closed。
    mmr: Decimal | None = Decimal("0.01")
    unknown_orders: int = 0
    submitting_orders: int = 0
    protection_ok: bool | None = None
    protection_missing: tuple[str, ...] = ()
    trades_today: int = 0
    trades_symbol_today: int = 0
    pending_business_keys: tuple[str, ...] = ()
    mark_move_60s_pct: Decimal | None = None
    mark_index_deviation_pct: Decimal | None = None
    spread_bps: Decimal | None = None
    cooldown_active: bool = False

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any] | None, *, base: "RiskContext | None" = None
    ) -> "RiskContext":
        """把请求里的 ``context``/``market`` 合并到服务端事实之上。

        调用方（UI/测试）只需提供市场事实；账户/风控事实由服务端填充，且
        **服务端事实优先**（客户端不能放宽）。
        """

        payload = payload or {}
        market = payload.get("market") if isinstance(payload.get("market"), Mapping) else payload

        def pick(key: str, *, source: Mapping[str, Any] | None = None) -> Any:
            container: Mapping[str, Any] = source if source is not None else payload
            if key in container and container[key] is not None:
                return container[key]
            if container is not payload and key in payload:
                return payload[key]
            return None

        mark = to_decimal(pick("mark", source=market))
        snapshot_age = _int_or_none(pick("snapshot_age_ms", source=market))
        mark_move = to_decimal(pick("mark_move_60s_pct", source=market))
        index_dev = to_decimal(pick("mark_index_deviation_pct", source=market))
        spread = to_decimal(pick("spread_bps", source=market))
        evidence_age = _int_or_none(payload.get("evidence_age_ms"))
        overrides = {
            "mark": mark,
            "snapshot_age_ms": snapshot_age,
            "mark_move_60s_pct": mark_move,
            "mark_index_deviation_pct": index_dev,
            "spread_bps": spread,
            "evidence_age_ms": evidence_age,
        }
        if base is None:
            return cls(**{key: value for key, value in overrides.items() if value is not None})
        merged = {key: value for key, value in overrides.items() if value is not None}
        return _replace(base, merged)


def _replace(context: RiskContext, changes: Mapping[str, Any]) -> RiskContext:
    values = {name: getattr(context, name) for name in context.__slots__}
    values.update(changes)
    return RiskContext(**values)


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class RuleViolation:
    rule: str
    action: str  # reject | downsize | adjust | tighten
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"rule": self.rule, "action": self.action, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    """一次裁决。

    ``approved_qty`` 是后端确定性计算出来的可执行数量（不是模型数量）；
    ``sizing`` 记录算式输入与命中约束（已格式化为十进制字符串，便于落账本/回放）；
    ``leverage_cap`` 是本次实际生效的杠杆上限（不含 confidence 提额）。
    ``tier`` 仅为历史快照兼容：新裁决恒为 ``"regular"``。

    ``risk_amount``/``risk_pct`` 是"计划约束"口径的预算风险额
    （数量 × 入场到止损距离），不是极端行情下的最大实际损失保证
    （滑点、跳空、资金费与强平规则另计）。
    """

    verdict: str  # approved | reduced | rejected
    approved_qty: Decimal
    leverage: int
    violations: tuple[RuleViolation, ...] = ()
    risk_amount: Decimal | None = None
    risk_pct: Decimal | None = None
    notional: Decimal | None = None
    rr: Decimal | None = None
    #: 历史快照兼容字段；§8·A 之后新裁决恒为 "regular"，不再代表授权档位。
    tier: str = "regular"
    blocking_rule: str | None = None
    blocked_reason: str | None = None
    #: 实际生效的杠杆上限（普通档 + 交易所上限取小）。
    leverage_cap: int = 0
    #: 确定性数量口径：risk_budget / max_qty_by_* / step_size / binding_constraint 等。
    sizing: Mapping[str, str] = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.verdict in {"approved", "reduced"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "approved_qty": format(self.approved_qty.normalize(), "f") if self.approved_qty else "0",
            "leverage": self.leverage,
            "tier": self.tier,
            "risk_amount": None if self.risk_amount is None else format(self.risk_amount.normalize(), "f"),
            "risk_pct": None if self.risk_pct is None else format(self.risk_pct.normalize(), "f"),
            "notional": None if self.notional is None else format(self.notional.normalize(), "f"),
            "rr": None if self.rr is None else format(self.rr.quantize(Decimal("0.01")), "f"),
            "violations": [item.as_dict() for item in self.violations],
            "blocking_rule": self.blocking_rule,
            "blocked_reason": self.blocked_reason,
            "leverage_cap": self.leverage_cap,
            "sizing": dict(self.sizing),
        }
