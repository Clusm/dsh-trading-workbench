"""Agent 之间的结构化数据模型。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class MarketBrief(BaseModel):
    """市场 Agent 输出的结构化行情摘要与关键价格。"""
    agent: str = "market_agent"
    generated_at: str = Field(default_factory=now)
    regime: str = "unknown"
    top_movers: list[str] = Field(default_factory=list)
    anomalies: list[str] = Field(default_factory=list)
    last_prices: dict[str, float] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    belief: dict[str, Any] = Field(default_factory=dict)


class MacroBrief(BaseModel):
    """宏观 Agent 输出的风险环境摘要；缺失数据必须显式表示未知。"""
    agent: str = "macro_agent"
    generated_at: str = Field(default_factory=now)
    risk_events_24h: list[str] = Field(default_factory=list)
    market_bias: str = "neutral"
    notes: list[str] = Field(default_factory=list)
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    belief: dict[str, Any] = Field(default_factory=dict)


class OnchainBrief(BaseModel):
    """链上 Agent 输出的只读观察摘要，不具有订单执行权限。"""
    agent: str = "onchain_agent"
    generated_at: str = Field(default_factory=now)
    stablecoin_change_24h: str = "n/a"
    tvl_change_24h: str = "n/a"
    whale_activity: list[str] = Field(default_factory=list)
    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    belief: dict[str, Any] = Field(default_factory=dict)


class StrategyRef(BaseModel):
    """与当前市场状态匹配的一张策略卡引用。"""
    strategy_id: str
    name: str
    applicable: bool = False
    note: str = ""
    detail: str = ""


class StrategyRefs(BaseModel):
    """策略库节点返回的候选策略引用集合。"""
    agent: str = "strategy_agent"
    generated_at: str = Field(default_factory=now)
    refs: list[StrategyRef] = Field(default_factory=list)
    summary: str = ""


class TradeIntent(BaseModel):
    """单个结构化交易意图；仍需 ActionSpace 与确定性风控审批。"""
    symbol: str
    action: Literal["open", "close", "reduce", "adjust"] = "open"
    side: Literal["LONG", "SHORT"]
    qty: float = Field(default=0, ge=0)
    leverage: int = Field(default=1, ge=1, le=125)
    client_order_id: str | None = None
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    stop_loss: float | None = None
    take_profit: float | None = None
    expected_holding_minutes: int = Field(default=60, ge=0)
    trade_level: Literal["short", "hourly"] = "short"
    confidence: float = Field(default=0.5, ge=0, le=1)
    reasoning: str = ""
    strategy_refs: list[str] = Field(default_factory=list)
    # 双通道架构：订单来源与归属
    order_source: Literal["strategy", "ai"] = "ai"
    strategy_id: str | None = None
    signal_id: str | None = None
    ticket_id: str | None = None
    entry_mode: Literal["next_open", "retest_limit", "breakout_confirm", "ai_limit", "market", "ai_minute"] | None = None
    ai_reason_type: Literal["trend_follow", "range_fade", "breakout",
                             "liquidity_sweep", "news_event", "macro"] | None = None
    # 限价单与百分比止盈止损
    limit_price: float | None = None
    limit_expires_at: str | None = None
    sl_pct: float | None = None
    tp_pct: float | None = None
    # AI 决策单默认逐仓
    margin_mode: Literal["ISOLATED", "CROSSED"] = "ISOLATED"
    # thesis 证伪条件：什么行情会证明这笔交易的判断错误。唯一能沉淀学习信号的字段。
    invalidation: str | None = None


class PositionReview(BaseModel):
    """每轮对单个持仓的强制表态：结构性修复"只开不管"。"""
    symbol: str
    verdict: Literal["hold", "reduce", "close", "adjust"]
    reason: str = ""


class TradeDecision(BaseModel):
    """主决策的候选交易集合及其可审计理由，不等同于已下单。"""
    decision: Literal["no_trade", "open", "close", "reduce", "adjust"]
    intents: list[TradeIntent] = Field(default_factory=list)
    position_reviews: list[PositionReview] = Field(default_factory=list)
    reasoning: str = ""
    strategy_refs: list[str] = Field(default_factory=list)
    channel: Literal["strategy", "ai", "merged"] = "ai"


class RiskVerdict(BaseModel):
    """确定性风控对候选意图的审批结果和调整后可执行集合。"""
    verdict: Literal["approved", "reduced", "rejected"]
    reasons: list[str] = Field(default_factory=list)
    adjusted_intents: list[TradeIntent] = Field(default_factory=list)


class SignalEvent(BaseModel):
    """策略信号触发事件：由确定性 SignalScanner 产出。"""
    signal_id: str
    strategy_id: str
    symbol: str
    timeframe: str
    side: Literal["LONG", "SHORT"]
    signal_time: str = Field(default_factory=now)
    signal_price: float
    bar_open_time: int = 0
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["triggered", "skipped", "expired", "filled"] = "triggered"


class SignalTicket(BaseModel):
    """信号触发后生成的进场票据：方向/止盈止损固定，分钟级进场由策略 AI 决定。"""
    ticket_id: str
    signal_id: str
    strategy_id: str
    symbol: str
    side: Literal["LONG", "SHORT"]
    signal_time: str = Field(default_factory=now)
    signal_price: float
    entry_zone_low: float
    entry_zone_high: float
    expires_at: str
    stop_loss_pct: float
    take_profit_pct: float
    time_stop_hours: int = Field(default=48, ge=1)
    status: Literal["open", "filled", "expired", "skipped", "cancelled"] = "open"


class ChannelDecision(BaseModel):
    """单一通道的决策输出。"""
    channel: Literal["strategy", "ai"]
    generated_at: str = Field(default_factory=now)
    decision: Literal["no_trade", "open", "close", "reduce", "adjust"]
    intents: list[TradeIntent] = Field(default_factory=list)
    reasoning: str = ""
    strategy_refs: list[str] = Field(default_factory=list)
