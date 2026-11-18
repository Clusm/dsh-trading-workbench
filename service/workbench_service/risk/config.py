"""硬风控阈值（docs/03 §7.2 的可执行版本）。

所有阈值集中在这里；规则实现（``rules.py``）只读取本配置，不写死数字。
环境变量只开放少量运维项（交易范围、初始权益、审批 TTL、是否强制异常行情证据）。

§8·A（T02 权限收口）：confidence 只是分析字段，不再选择更高的风险/杠杆上限。
允许的风险/杠杆只有普通档一套（``max_risk_pct`` / ``max_leverage``）；
``strong_*`` 旧字段仅为旧配置读取兼容而保留，**不参与执行授权**
（见 ``LEGACY_STRONG_TIER``），也不再通过 env 暴露提额开关。
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

from ..market.config import WHITELIST

__all__ = [
    "RiskLimits",
    "RULE_SPECS",
    "rule_table",
    "LEGACY_STRONG_TIER",
    "SIZING_POLICY",
    "AUTHORIZATION_FIELDS",
    "ENV_KNOBS",
    "MAX_APPROVAL_TTL_S",
    "env_knobs",
]

DEFAULT_SYMBOLS = WHITELIST
#: T19（权限红队）：能放大执行授权的字段只有这四个；它们不是 env 可写项，
#: 只能改代码 + 评审（见 ENV_KNOBS 与 tests_execution/test_risk_rules.py）。
AUTHORIZATION_FIELDS: tuple[str, ...] = (
    "max_risk_pct",
    "max_leverage",
    "max_notional_multiple",
    "max_margin_usage",
)

#: T19：审批令牌 TTL 的硬上限。env 只能在上限内收紧；否则
#: WORKBENCH_RISK_APPROVAL_TTL_S=31536000 等于“一年内可重放的旧计划”。
MAX_APPROVAL_TTL_S = 300

#: T19：env 只开这些运维项。名字里带 MAX_RISK / MAX_LEVERAGE / STRONG / NOTIONAL
#: 的变量一律不读——配置读取入口不存在提额路径。affects_execution_authorization
#: 全部为 False，测试断言这一点（谁加提额开关，套件就会红）。
ENV_KNOBS: dict[str, dict[str, object]] = {
    "WORKBENCH_RISK_SYMBOLS": {
        "field": "symbols",
        "affects_execution_authorization": False,
        "scope": "交易范围（R01 白名单），不改变数量/杠杆上限",
    },
    "WORKBENCH_RISK_INITIAL_EQUITY": {
        "field": "initial_equity",
        "affects_execution_authorization": False,
        "scope": "日亏/回撤基线锚点，不参与 sizing（风险预算取 ctx.equity_session）",
    },
    "WORKBENCH_RISK_APPROVAL_TTL_S": {
        "field": "approval_ttl_s",
        "affects_execution_authorization": False,
        "scope": "审批令牌 TTL；只能收紧到 [1, MAX_APPROVAL_TTL_S]",
        "clamp": [1, MAX_APPROVAL_TTL_S],
    },
    "WORKBENCH_RISK_REQUIRE_ANOMALY": {
        "field": "require_anomaly_data",
        "affects_execution_authorization": False,
        "scope": "R15 缺失异常行情数据时是否 fail-closed（只放宽该拒绝，不改上限）",
    },
    "WORKBENCH_RISK_ENABLE_LOSS_STREAK": {
        "field": "enable_loss_streak",
        "affects_execution_authorization": False,
        "scope": "开启 R20（只会增加拒绝）",
    },
    "WORKBENCH_RISK_ENABLE_FREQUENCY": {
        "field": "enable_frequency",
        "affects_execution_authorization": False,
        "scope": "开启 R21（只会增加拒绝）",
    },
}


def env_knobs() -> dict[str, dict[str, object]]:
    """env 开关清单（拷贝；供 /api/risk/envelope 自证没有提额环境变量）。"""

    return {name: dict(meta) for name, meta in ENV_KNOBS.items()}

#: 旧"强信心档"字段：仅为读取旧配置/旧报告兼容，**不参与执行授权**。
#: §8·A：confidence 只作分析字段，不再选择更高的风险或杠杆上限。
LEGACY_STRONG_TIER: dict[str, object] = {
    "fields": [
        "strong_max_risk_pct",
        "strong_max_leverage",
        "strong_min_confidence",
        "strong_min_rr",
    ],
    "used_for_execution_authorization": False,
    "confidence_used_for_sizing": False,
    "authoritative_fields": ["max_risk_pct", "max_leverage", "max_notional_multiple"],
    "note": (
        "confidence 仅为分析字段：允许风险与杠杆只取普通档上限；"
        "strong_* 保留读取兼容（旧配置不崩）但不参与执行授权。"
    ),
}

#: 允许数量的确定性口径（§8·A.3）：模型数量只是申报上限，最终数量由后端计算。
SIZING_POLICY: dict[str, object] = {
    "policy": "deterministic",
    "model_qty_role": "upper_bound_only",
    "confidence_used": False,
    "inputs": [
        "equity_session",
        "max_risk_pct",
        "entry_minus_stop_distance",
        "max_notional_multiple",
        "used_margin",
        "available_margin",
        "max_margin_usage",
        "step_size",
        "min_qty",
        "min_notional",
    ],
    "rounding": "floor_to_step",
    "below_min_order": "reject",
    "stop_loss_budget": (
        "止损预算是计划约束描述（风险预算 × 入场到止损距离），"
        "不是极端行情下的最大实际损失保证（滑点/跳空/资金费另计）"
    ),
}


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    """env 浮点项：非法值（含 nan/inf）一律回退默认值。

    T19：`WORKBENCH_RISK_INITIAL_EQUITY=nan` 曾会污染日亏/回撤基线计算
    （Decimal('NaN') 参与比较即抛 InvalidOperation），属于 env 注入异常值路径。
    """
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def _env_positive_float(name: str, default: float) -> float:
    """env 正数项：非法/非正/非有限值一律回退默认值（fail-closed）。

    T19：`WORKBENCH_RISK_INITIAL_EQUITY=-999999` 会让日亏/回撤基线变成
    `max(0, exchange - initial)` 的超大值，从而把会话权益算成负数——虽然 R25 会
    拒绝新开仓（等于失败关闭），但把非正数当配置读进来本身就是不该有的入口。
    """

    value = _env_float(name, default)
    return value if value > 0 else default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clamp_int(value: int, low: int, high: int) -> int:
    """把 env 整数卡在硬区间内（T19：TTL 只能收紧，不能放宽）。"""

    return max(low, min(high, int(value)))


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """v1 硬上限；AI 只能在此基础上收紧（R00）。

    §8·A：单笔风险与杠杆只有一套普通档上限；``strong_*`` 四字段是
    legacy 兼容值（旧配置/旧报告可读），不参与执行授权（见 ``LEGACY_STRONG_TIER``）。
    """

    # 会话
    initial_equity: float = 5000.0
    # 单笔
    max_risk_pct: float = 0.010
    #: legacy：不用于执行授权（§8·A）；允许风险固定取 max_risk_pct。
    strong_max_risk_pct: float = 0.020
    #: legacy：强信心档已取消，不再参与任何判定。
    strong_min_confidence: float = 0.65
    #: legacy：盈亏比下限只有 min_risk_reward，不再分档。
    strong_min_rr: float = 2.0
    max_leverage: int = 10
    #: legacy：不用于执行授权（§8·A）；杠杆上限固定取 max_leverage。
    strong_max_leverage: int = 15
    max_notional_multiple: float = 2.0
    min_risk_reward: float = 1.2
    sl_min_distance_pct: float = 0.002
    sl_max_distance_pct: float = 0.050
    min_liquidation_distance_multiple: float = 2.0
    # 组合
    max_open_positions: int = 1
    max_symbol_positions: int = 1
    max_margin_usage: float = 0.60
    # 账户损失
    daily_loss_stop: float = 0.030
    max_drawdown_from_peak: float = 0.080
    consecutive_losses: int = 2
    consecutive_loss_cooldown_hours: int = 4
    # 保护
    require_initial_sl_tp: bool = True
    fallback_sl_pct: float = 0.02
    fallback_tp_pct: float = 0.04
    protection_place_retry: int = 1
    # 数据新鲜度
    entry_snapshot_max_age_ms: int = 15_000
    #: 决策链预算是 340s（Host CHAIN_DEADLINE_MS，M1.4c 起）；R14 用该值拒绝过期证据。
    #: 必须严格大于链预算，否则任何跑满预算的轮次都会在 P1 被判 E_MARKET_STALE。
    #: M1.4c：M1.4 的 250s/300s 仍然不够——risk/decision 角色在真实轮里大量超时
    #: （RISK_UNAVAILABLE x26），实测最长轮 255.8s，故链预算提到 340s、门限 400s。
    #: 执行时的新鲜度仍由 entry_snapshot_max_age_ms=15s 在 P1/P2 用服务端实时行情把关。
    #: 刻意不提供 env 覆盖：R14 是安全门限，放宽必须改代码并评审（设计 02/03），
    #: 运维只能收紧、不能通过环境变量绕过。
    decision_evidence_max_age_ms: int = 400_000
    equity_block_new_ms: int = 60_000
    # 异常行情
    mark_move_60s_pct: float = 0.030
    mark_index_deviation_pct: float = 0.010
    max_spread_bps: float = 50.0
    require_anomaly_data: bool = True
    # 订单
    min_notional_usdt: float = 20.0
    max_orders_per_cycle: int = 2
    max_trades_per_day: int = 12
    max_trades_per_symbol_per_day: int = 3
    unknown_block_new_order: bool = True
    unknown_resolve_ttl_ms: int = 120_000
    # 审批
    approval_ttl_s: int = 120
    # 默认关闭的规则（R20/R21）
    enable_loss_streak: bool = False
    enable_frequency: bool = False
    # 交易范围
    symbols: tuple[str, ...] = field(default_factory=lambda: DEFAULT_SYMBOLS)

    # ------------------------------------------------------------------ 工厂
    @classmethod
    def from_env(cls) -> "RiskLimits":
        # 注意：freshness 门限（entry_snapshot/decision_evidence/equity_block）
        # 刻意不经 env 暴露，避免运维放宽 R14；如需调整必须改代码 + 评审。
        # T19：同理，max_risk_pct / max_leverage / max_notional_multiple / max_margin_usage
        # 与 strong_* 都不读 env（见 ENV_KNOBS）；TTL 只允许在 [1, MAX_APPROVAL_TTL_S] 内收紧。
        raw_symbols = _env("WORKBENCH_RISK_SYMBOLS")
        symbols = (
            tuple(item.strip().upper() for item in raw_symbols.split(",") if item.strip())
            if raw_symbols
            else DEFAULT_SYMBOLS
        )
        # slots dataclass 的类属性是 member_descriptor，必须从实例取默认值
        defaults = cls()
        return cls(
            initial_equity=_env_positive_float(
                "WORKBENCH_RISK_INITIAL_EQUITY", defaults.initial_equity
            ),
            approval_ttl_s=_clamp_int(
                _env_int("WORKBENCH_RISK_APPROVAL_TTL_S", defaults.approval_ttl_s),
                1,
                MAX_APPROVAL_TTL_S,
            ),
            require_anomaly_data=_env_bool(
                "WORKBENCH_RISK_REQUIRE_ANOMALY", defaults.require_anomaly_data
            ),
            enable_loss_streak=_env_bool(
                "WORKBENCH_RISK_ENABLE_LOSS_STREAK", defaults.enable_loss_streak
            ),
            enable_frequency=_env_bool(
                "WORKBENCH_RISK_ENABLE_FREQUENCY", defaults.enable_frequency
            ),
            symbols=symbols,
        )

    # ------------------------------------------------------------------ 视图
    def as_dict(self) -> dict[str, object]:
        return {
            "initial_equity": self.initial_equity,
            "per_trade": {
                "max_risk_pct": self.max_risk_pct,
                # legacy 兼容读取（旧配置/旧报告）；不参与执行授权，见 legacy_strong_tier。
                "strong_max_risk_pct": self.strong_max_risk_pct,
                "strong_min_confidence": self.strong_min_confidence,
                "strong_min_rr": self.strong_min_rr,
                "max_leverage": self.max_leverage,
                "strong_max_leverage": self.strong_max_leverage,
                "max_notional_multiple": self.max_notional_multiple,
                "min_risk_reward": self.min_risk_reward,
                "sl_min_distance_pct": self.sl_min_distance_pct,
                "sl_max_distance_pct": self.sl_max_distance_pct,
                "min_liquidation_distance_multiple": self.min_liquidation_distance_multiple,
            },
            "portfolio": {
                "max_open_positions": self.max_open_positions,
                "max_symbol_positions": self.max_symbol_positions,
                "max_margin_usage": self.max_margin_usage,
            },
            "loss": {
                "daily_loss_stop": self.daily_loss_stop,
                "max_drawdown_from_peak": self.max_drawdown_from_peak,
                "consecutive_losses": self.consecutive_losses,
                "consecutive_loss_cooldown_hours": self.consecutive_loss_cooldown_hours,
            },
            "protection": {
                "require_initial_sl_tp": self.require_initial_sl_tp,
                "fallback_sl_pct": self.fallback_sl_pct,
                "fallback_tp_pct": self.fallback_tp_pct,
                "protection_place_retry": self.protection_place_retry,
            },
            "data_freshness": {
                "entry_snapshot_max_age_ms": self.entry_snapshot_max_age_ms,
                "decision_evidence_max_age_ms": self.decision_evidence_max_age_ms,
                "equity_block_new_ms": self.equity_block_new_ms,
            },
            "anomaly": {
                "mark_move_60s_pct": self.mark_move_60s_pct,
                "mark_index_deviation_pct": self.mark_index_deviation_pct,
                "max_spread_bps": self.max_spread_bps,
            },
            "orders": {
                "min_notional_usdt": self.min_notional_usdt,
                "max_orders_per_cycle": self.max_orders_per_cycle,
                "max_trades_per_day": self.max_trades_per_day,
                "max_trades_per_symbol_per_day": self.max_trades_per_symbol_per_day,
            },
            "approval_ttl_s": self.approval_ttl_s,
            "symbols": list(self.symbols),
            # T19：执行授权口径自证——哪些字段算授权（AUTHORIZATION_FIELDS），
            # 哪些字段明确不用于授权（legacy strong_* / confidence / 模型 qty）。
            "authorization": {
                "execution_authorization_fields": list(AUTHORIZATION_FIELDS),
                "not_used_for_execution_authorization": [
                    "per_trade.strong_max_risk_pct",
                    "per_trade.strong_max_leverage",
                    "per_trade.strong_min_confidence",
                    "per_trade.strong_min_rr",
                    "confidence",
                    "plan.qty（模型申报上限）",
                ],
                "sizing_policy": "deterministic",
                "confidence_used_for_sizing": False,
                "approval_ttl_max_s": MAX_APPROVAL_TTL_S,
            },
            # §8·A：序列化必须标明 legacy 字段不用于执行授权，且数量口径是确定性计算。
            "legacy_strong_tier": {
                **LEGACY_STRONG_TIER,
                "fields": list(LEGACY_STRONG_TIER["fields"]),  # type: ignore[arg-type]
            },
            "sizing_policy": {
                **SIZING_POLICY,
                "inputs": list(SIZING_POLICY["inputs"]),  # type: ignore[arg-type]
            },
            "rules_default_off": [
                rule_id
                for rule_id, enabled in (
                    ("R20", self.enable_loss_streak),
                    ("R21", self.enable_frequency),
                )
                if not enabled
            ],
        }


@dataclass(frozen=True, slots=True)
class RuleSpec:
    rule_id: str
    name: str
    action: str
    summary: str
    default_enabled: bool = True


# R14 的展示文案必须与实际门限同源，避免 envelope rules[] 与 data_freshness 漂移。
_R14_SUMMARY = (
    f"行情 ≤{RiskLimits().entry_snapshot_max_age_ms // 1000}s、"
    f"证据 ≤{RiskLimits().decision_evidence_max_age_ms // 1000}s"
)

#: 26 条规则（R00–R25）的展示用元数据；实现见 ``rules.py``。
RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec("R00", "AI 只能收紧", "tighten", "最终限额 = min(AI 建议, 硬上限)，confidence 不提额"),
    RuleSpec("R01", "品种白名单", "reject", "symbol 在交易范围且交易所状态 TRADING"),
    RuleSpec("R02", "单标的单方向", "reject", "该 symbol 无现存仓位（禁止加仓/反手）"),
    RuleSpec("R03", "最大并发持仓", "reject", "open_positions + 1 ≤ max_open_positions"),
    RuleSpec(
        "R04",
        "杠杆上限",
        "reject",
        f"≤{RiskLimits().max_leverage}x 普通档上限且不超过交易所上限（confidence 不提高上限）",
    ),
    RuleSpec("R05", "单笔风险", "downsize", "qty×|entry−SL| ≤ equity×risk_pct"),
    RuleSpec("R06", "单笔名义", "downsize", "qty×entry ≤ equity×2.0"),
    RuleSpec("R07", "保证金占用", "downsize", "used+new ≤ equity×60%"),
    RuleSpec("R08", "SL/TP 必填+方向", "reject", "SL/TP>0 且方向正确"),
    RuleSpec("R09", "止损距离", "reject", "0.2% ≤ |entry−SL|/entry ≤ 5%"),
    RuleSpec("R10", "盈亏比", "reject", f"RR ≥ {RiskLimits().min_risk_reward}"),
    RuleSpec("R11", "爆仓距离", "reject", "(1/lev − mmr) ≥ 2×SL 距离"),
    RuleSpec("R12", "最小名义/数量", "reject", "≥ max(20U, 交易所 minNotional) 且 ≥ minQty"),
    RuleSpec("R13", "精度对齐", "adjust", "数量向下取整 stepSize，价格按方向取整"),
    RuleSpec("R14", "数据新鲜度", "reject", _R14_SUMMARY),
    RuleSpec("R15", "异常行情", "reject", "60s 波动 ≥3% / 标记-指数偏离 ≥1% / 价差 ≥50bp"),
    RuleSpec("R16", "重复单", "reject", "同 clientOrderId 或同业务键在途"),
    RuleSpec("R17", "未决订单", "reject", "UNKNOWN/SUBMITTING > 0 时禁新开"),
    RuleSpec("R18", "日亏熔断", "reject", "日亏 ≥3% → close_only"),
    RuleSpec("R19", "会话回撤", "reject", "回撤 ≥8% → emergency"),
    RuleSpec("R20", "连亏冷却", "reject", "连亏 2 笔 → 冷却 4h（默认关）", default_enabled=False),
    RuleSpec("R21", "交易频率", "reject", "日 ≤12 笔、单 symbol ≤3 笔（默认关）", default_enabled=False),
    RuleSpec("R22", "控制态", "reject", "control_state == normal"),
    RuleSpec("R23", "保护覆盖", "reject", "所有持仓保护状态 OK"),
    RuleSpec("R24", "保证金率", "reject", "维持保证金/权益 ≥80% → emergency"),
    RuleSpec("R25", "权益可用", "reject", "权益新鲜（≤60s）且 > 0"),
)


def rule_table(*, loss_streak: bool | None = None, frequency: bool | None = None) -> list[dict[str, object]]:
    """规则清单 + 当前开关状态（供 ``/api/risk/envelope``）。"""

    overrides = {"R20": loss_streak, "R21": frequency}
    rows: list[dict[str, object]] = []
    for spec in RULE_SPECS:
        enabled = spec.default_enabled
        override = overrides.get(spec.rule_id)
        if override is not None:
            enabled = bool(override)
        rows.append(
            {
                "rule_id": spec.rule_id,
                "name": spec.name,
                "action": spec.action,
                "summary": spec.summary,
                "enabled": enabled,
            }
        )
    return rows
