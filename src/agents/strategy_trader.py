"""策略交易 AI（P1 确定性版）。

P1 先做安全的最小闭环：
- 只执行 SignalScanner 生成、且仍在 entry_zone 内的 ticket；
- 方向/SL/TP/品种/周期全部来自策略卡，不允许修改；
- 同 symbol 通道互斥：已有任何仓位则跳过；
- 每张策略同时最多 1 个仓位。

后续 P3 再让 LLM 在 ticket 有效期内做分钟级进场择时，并用影子基准评估。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from audit.database import AuditDatabase
from data.market_query import get_live_prices

from .models import ChannelDecision, TradeIntent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _price_in_zone(price: float, ticket: dict[str, Any]) -> bool:
    return float(ticket["entry_zone_low"]) <= float(price) <= float(ticket["entry_zone_high"])


def _not_expired(ticket: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or utc_now()
    try:
        expires = datetime.fromisoformat(str(ticket["expires_at"]).replace("Z", "+00:00"))
    except Exception:
        return True
    return expires >= now


def build_intent_from_ticket(ticket: dict[str, Any], price: float,
                             notional: float, leverage: int,
                             holding_minutes: int | None = None) -> TradeIntent:
    qty = max(0.0, float(notional) / float(price)) if price > 0 else 0.0
    side = str(ticket["side"]).upper()
    sl_pct = float(ticket["stop_loss_pct"])
    tp_pct = float(ticket["take_profit_pct"])
    if side == "LONG":
        sl = price * (1.0 - sl_pct)
        tp = price * (1.0 + tp_pct)
    else:
        sl = price * (1.0 + sl_pct)
        tp = price * (1.0 - tp_pct)
    return TradeIntent(
        symbol=str(ticket["symbol"]).strip().upper(),
        action="open",
        side="LONG" if side == "LONG" else "SHORT",
        qty=round(qty, 8),
        leverage=int(leverage),
        order_type="MARKET",
        stop_loss=round(sl, 8),
        take_profit=round(tp, 8),
        expected_holding_minutes=int(holding_minutes or 0),
        trade_level="hourly" if str(ticket.get("timeframe", "1h")) not in ("30m",) else "short",
        confidence=0.6,
        reasoning=f"ticket={ticket['ticket_id']} signal_price={ticket['signal_price']} "
                  f"current_price={price} notional={notional:.2f} leverage={leverage}x",
        strategy_refs=[str(ticket.get("strategy_id", ""))],
        order_source="strategy",
        strategy_id=str(ticket.get("strategy_id", "")),
        signal_id=str(ticket.get("signal_id", "")),
        ticket_id=str(ticket.get("ticket_id", "")),
        entry_mode="market",
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        margin_mode="ISOLATED",
    )


def run_strategy_trader(*, tickets: list[dict[str, Any]],
                        portfolio: dict[str, Any] | None = None,
                        prices: dict[str, float] | None = None,
                        min_notional: float = 20.0,
                        max_notional: float = 120.0,
                        max_leverage: int = 20,
                        risk_per_trade: float = 0.02,
                        channel_status: dict[str, Any] | None = None,
                        db: AuditDatabase | None = None,
                        allowed_symbols: Iterable[str] | None = None) -> ChannelDecision:
    intents: list[TradeIntent] = []
    notes: list[str] = []
    portfolio = portfolio or {}
    positions = list(portfolio.get("positions") or [])

    if min_notional <= 0 or max_notional <= 0:
        return ChannelDecision(
            channel="strategy", decision="no_trade",
            reasoning="strategy_min_notional_usdt/strategy_max_notional_usdt=0，策略通道只扫描不下单。",
            intents=[],
        )

    # 策略账本权益：优先取 ChannelManager 计算的策略通道权益；没有时按总权益 60% 估算
    ch_strategy = (channel_status or {}).get("strategy") or {}
    strategy_equity = float(ch_strategy.get("equity") or 0.0)
    if strategy_equity <= 0:
        session_equity = float((portfolio or {}).get("session_equity") or 0.0)
        if session_equity <= 0:
            session_equity = float((portfolio or {}).get("cash") or 0.0)
        strategy_equity = session_equity * 0.6 if session_equity > 0 else 0.0
    margin_cap = strategy_equity * 0.80

    now = utc_now()
    allowed_set = ({str(s).strip().upper() for s in allowed_symbols if s}
                   if allowed_symbols is not None else None)
    symbols = [str(t["symbol"]).strip().upper() for t in tickets
               if allowed_set is None or str(t.get("symbol") or "").strip().upper() in allowed_set]
    prices = dict(prices or {})
    missing = [s for s in symbols if s not in prices]
    if missing:
        try:
            prices.update(get_live_prices(missing))
        except Exception:
            pass

    held_symbols = {str(p.get("symbol")) for p in positions if p.get("symbol")}
    held_strategy = {str(p.get("strategy_id")) for p in positions
                     if str(p.get("source", "ai")) == "strategy" and p.get("strategy_id")}

    for ticket in tickets:
        symbol = str(ticket["symbol"]).strip().upper()
        strategy_id = str(ticket.get("strategy_id", ""))
        if allowed_set is not None and symbol not in allowed_set:
            notes.append(f"{ticket.get('ticket_id', symbol)} 不在当前业务开仓白名单内，跳过")
            if db:
                db.log_risk_event(
                    event_type="strategy_scope_skip", severity="info",
                    details=f"{symbol} 不在当前业务开仓白名单内", action_taken="skip open")
            continue
        if not _not_expired(ticket, now):
            notes.append(f"{ticket['ticket_id']} 已过期，跳过")
            if db:
                db.log_risk_event(event_type="strategy_ticket_expired", severity="info",
                                  details=f"{ticket['ticket_id']} 过期跳过", action_taken="skip")
                db.update_signal_ticket(ticket_id=str(ticket["ticket_id"]), status="expired")
            continue
        if symbol in held_symbols:
            notes.append(f"{ticket['ticket_id']} 同 symbol 已有仓位，跳过")
            if db:
                db.log_risk_event(event_type="strategy_conflict_skip", severity="warn",
                                  details=f"{ticket['ticket_id']} 与现有 {symbol} 仓位冲突",
                                  action_taken="skip")
            continue
        if strategy_id in held_strategy:
            notes.append(f"{ticket['ticket_id']} 策略 {strategy_id} 已有持仓，跳过")
            continue
        price = prices.get(symbol)
        if not price:
            notes.append(f"{ticket['ticket_id']} 无最新价，跳过")
            continue
        if not _price_in_zone(float(price), ticket):
            notes.append(
                f"{ticket['ticket_id']} 当前价 {price} 不在入场区 "
                f"[{ticket['entry_zone_low']}, {ticket['entry_zone_high']}]，本轮跳过")
            continue
        holding_minutes = int(ticket.get("time_stop_hours", 48) or 48) * 60
        # 自适应 sizing：先按单笔风险预算和 SL% 定名义金额，再按保证金上限动态选杠杆
        sl_pct = float(ticket.get("stop_loss_pct", 0.03) or 0.03)
        risk_budget = strategy_equity * float(risk_per_trade)
        notional_by_risk = risk_budget / sl_pct if sl_pct > 0 else max_notional
        notional = min(float(max_notional), max(float(min_notional), notional_by_risk))
        # 保证金约束：notional / leverage <= margin_cap
        if margin_cap > 0:
            max_notional_by_margin = margin_cap * float(max_leverage)
            notional = min(notional, max_notional_by_margin)
            import math
            needed_leverage = max(1, int(math.ceil(notional / margin_cap)))
            leverage = min(int(max_leverage), needed_leverage)
        else:
            leverage = 1
        intent = build_intent_from_ticket(ticket, float(price), notional, leverage,
                                          holding_minutes=holding_minutes)
        intents.append(intent)
        notes.append(f"{ticket['ticket_id']} 进入待执行: {symbol} {intent.side} @ {price} "
                     f"名义={notional:.2f}U 杠杆={leverage}x")

    if not intents:
        return ChannelDecision(
            channel="strategy", decision="no_trade",
            reasoning="; ".join(notes) if notes else "无 ticket 或没有可执行 ticket。",
            intents=[],
        )
    return ChannelDecision(
        channel="strategy", decision="open",
        intents=intents,
        reasoning="; ".join(notes),
        strategy_refs=[i.strategy_id or "" for i in intents],
    )
