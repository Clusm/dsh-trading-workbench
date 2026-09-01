"""策略库 Agent（轻量版）：读取 JSON 策略卡片，按当前市场/宏观/链上状态打分匹配。

策略库规模小，不使用向量检索。主 Agent 拿到的 StrategyRefs 会包含完整卡片摘要，
足够直接参考。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import MacroBrief, MarketBrief, OnchainBrief, StrategyRef, StrategyRefs


def load_cards(knowledge_dir: Path, file_name: str = "strategy_cards.json") -> list[dict[str, Any]]:
    p = knowledge_dir / file_name
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data.get("cards", []))


def _has_short(direction: str) -> bool:
    return "SHORT" in direction


def _has_long(direction: str) -> bool:
    return "LONG" in direction or direction == "LONG_SHORT"


def _is_volatile(market: MarketBrief) -> bool:
    return any(("ATR" in a) or ("24h涨跌" in a) for a in market.anomalies)


def _score_card(card: dict[str, Any], market: MarketBrief, macro: MacroBrief,
                onchain: OnchainBrief) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    regimes = set(card.get("regimes", []))
    if "all" in regimes:
        score += 2
        reasons.append("regime=all")
    elif market.regime in regimes:
        score += 3
        reasons.append(f"regime={market.regime}")
    else:
        score += 1
        reasons.append(f"regime弱匹配:{market.regime}")

    direction = str(card.get("direction", "LONG_SHORT"))
    if market.regime == "trend_up" and _has_long(direction):
        score += 2
        reasons.append("trend_up+支持做多")
        if direction == "LONG":
            score += 1
            reasons.append("纯多头策略加成")
    if market.regime == "trend_down" and _has_short(direction):
        score += 2
        reasons.append("trend_down+支持做空")
        if direction == "SHORT":
            score += 1
            reasons.append("纯空头策略加成")
    if market.regime == "trend_down" and not _has_short(direction):
        score -= 2
        reasons.append("trend_down但策略不支持做空")

    tags = set(card.get("tags", []))
    if _is_volatile(market) and tags & {"buying_climax", "counter_trend", "liquidity_sweep", "gap", "event_driven"}:
        score += 2
        reasons.append("市场波动放大")
    if market.regime == "choppy" and tags & {"supply_demand", "squeeze", "dormant_liquidity", "range_filter"}:
        score += 2
        reasons.append("震荡市匹配")

    if macro.market_bias == "risk_off" and _has_short(direction):
        score += 1
        reasons.append("宏观risk_off支持做空")
    if macro.market_bias == "risk_on" and _has_long(direction):
        score += 1
        reasons.append("宏观risk_on支持做多")
    if "涨" in onchain.stablecoin_change_24h and _has_long(direction):
        score += 1
        reasons.append("稳定币扩张")

    return score, reasons


def _display_name(name: str) -> str:
    """去掉会误导用户的旧策略库前缀，只保留策略名。"""
    return str(name).replace("旧策略库", "").replace("旧策略", "").strip()


def run_strategy_agent(knowledge_dir: Path, market: MarketBrief | None = None,
                       macro: MacroBrief | None = None,
                       onchain: OnchainBrief | None = None,
                       tickets: list[dict[str, Any]] | None = None) -> StrategyRefs:
    market = market or MarketBrief()
    macro = macro or MacroBrief()
    onchain = onchain or OnchainBrief()
    cards = load_cards(knowledge_dir)
    card_by_id = {str(c.get("id")): c for c in cards}

    # 真正触发检测优先：有 ticket 时只展示实际触发的策略；
    # 没有 ticket 时不再把“参考打分卡”伪装成策略匹配。
    if tickets is not None:
        if not tickets:
            return StrategyRefs(
                refs=[],
                summary="本轮无实际触发信号（SignalScanner 未产生 ticket），策略通道不交易。",
            )
        refs: list[StrategyRef] = []
        seen: set[str] = set()
        for ticket in tickets:
            strategy_id = str(ticket.get("strategy_id", ""))
            if not strategy_id or strategy_id in seen:
                continue
            seen.add(strategy_id)
            card = card_by_id.get(strategy_id, {})
            side = str(ticket.get("side", ""))
            refs.append(StrategyRef(
                strategy_id=strategy_id,
                name=_display_name(str(card.get("name") or strategy_id)),
                applicable=True,
                note=f"signal triggered: side={side} tf={ticket.get('timeframe','')} price={ticket.get('signal_price','')}",
                detail=(f"ticket={ticket.get('ticket_id','')}; 方向={side}; "
                        f"入场区=[{ticket.get('entry_zone_low')},{ticket.get('entry_zone_high')}]; "
                        f"过期={ticket.get('expires_at','')}"),
            ))
        return StrategyRefs(
            refs=refs,
            summary=f"SignalScanner 实际触发 {len(refs)} 张策略，进入策略调度者决策。",
        )

    scored: list[tuple[int, list[str], dict[str, Any]]] = []
    for c in cards:
        s, reasons = _score_card(c, market, macro, onchain)
        scored.append((s, reasons, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    refs: list[StrategyRef] = []
    for score, reasons, c in scored[:3]:
        detail = (
            f"方向={c.get('direction')}; 周期={','.join(c.get('timeframes', []))}; "
            f"入场={c.get('entry', '')} 止损={c.get('stop_loss', '')} "
            f"止盈={c.get('take_profit', '')} 注意={c.get('risk_notes', '')}"
        )
        refs.append(StrategyRef(
            strategy_id=str(c.get("id", "?")),
            name=_display_name(str(c.get("name", "?"))),
            applicable=score >= 5,
            note=f"score={score}; " + "; ".join(reasons),
            detail=detail,
        ))
    return StrategyRefs(
        refs=refs,
        summary=(
            f"策略库共 {len(cards)} 张卡，当前市场={market.regime}，"
            f"推荐 {len([r for r in refs if r.applicable])}/{len(refs)} 张参考卡。"
        ),
    )
