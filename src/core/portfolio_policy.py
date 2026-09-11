"""组合因子与同类连亏的 shadow 评价。

这里不修改仓位、杠杆或 RiskGuard。动态相关性有足够闭合K线时才计算；数据不足时
明确返回低置信静态簇，绝不把“未知”伪装成“低相关”。连亏状态以审计数据库为唯一
事实源，进程重启后可重建，避免再维护一份易损 JSON。
"""
from __future__ import annotations

import json
import math
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from core.decision_policy import load_decision_policy


MAJOR_FACTORS = {"BTCUSDT": "btc", "ETHUSDT": "eth"}


def _returns(snapshot: dict[str, Any] | None) -> list[float]:
    closes = (((snapshot or {}).get("candles") or {}).get("c") or [])
    values: list[float] = []
    for value in closes:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return []
    return [values[i] / values[i - 1] - 1 for i in range(1, len(values)) if values[i - 1] > 0]


def rolling_beta_correlation(symbol_snapshot: dict[str, Any] | None,
                             btc_snapshot: dict[str, Any] | None) -> tuple[float | None, float | None]:
    """以同周期尾部收益估算 beta/correlation；少于10个收益点视为未知。"""
    symbol_interval = ((symbol_snapshot or {}).get("interval")
                       or ((symbol_snapshot or {}).get("candles") or {}).get("interval"))
    btc_interval = ((btc_snapshot or {}).get("interval")
                    or ((btc_snapshot or {}).get("candles") or {}).get("interval"))
    if symbol_interval and btc_interval and symbol_interval != btc_interval:
        return None, None
    left = _returns(symbol_snapshot)
    right = _returns(btc_snapshot)
    size = min(len(left), len(right))
    if size < 10:
        return None, None
    left, right = left[-size:], right[-size:]
    mean_left = statistics.fmean(left)
    mean_right = statistics.fmean(right)
    covariance = statistics.fmean(
        (a - mean_left) * (b - mean_right) for a, b in zip(left, right)
    )
    variance_btc = statistics.fmean((b - mean_right) ** 2 for b in right)
    variance_left = statistics.fmean((a - mean_left) ** 2 for a in left)
    if variance_btc <= 0 or variance_left <= 0:
        return None, None
    beta = covariance / variance_btc
    correlation = covariance / math.sqrt(variance_left * variance_btc)
    return beta, max(-1.0, min(1.0, correlation))


def factor_for(symbol: str, details: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """返回稳定因子组及证据质量；静态回退不是低风险结论。"""
    normalized = str(symbol or "").upper()
    if normalized in MAJOR_FACTORS:
        return {"group": MAJOR_FACTORS[normalized], "confidence": "high",
                "method": "major_asset", "beta": 1.0 if normalized == "BTCUSDT" else None,
                "correlation": 1.0 if normalized == "BTCUSDT" else None}
    snapshots = details or {}
    beta, correlation = rolling_beta_correlation(
        snapshots.get(normalized), snapshots.get("BTCUSDT"),
    )
    if beta is not None and correlation is not None:
        if correlation >= 0.65:
            group = "btc_beta_alts"
        elif correlation <= -0.50:
            group = "btc_inverse"
        else:
            group = "idiosyncratic_alts"
        return {"group": group, "confidence": "medium", "method": "rolling_returns",
                "beta": round(beta, 4), "correlation": round(correlation, 4)}
    return {"group": "btc_beta_alts", "confidence": "low", "method": "static_fallback",
            "beta": None, "correlation": None}


def _position_notional(position: dict[str, Any]) -> float | None:
    try:
        price = float(position.get("mark_price") or position.get("last_price")
                      or position.get("entry_price") or 0.0)
        qty = abs(float(position.get("qty") or position.get("positionAmt") or 0.0))
        return price * qty if price > 0 and qty > 0 else None
    except (TypeError, ValueError):
        return None


def _position_side(position: dict[str, Any]) -> str:
    side = str(position.get("side") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    try:
        return "SHORT" if float(position.get("positionAmt") or 0.0) < 0 else "LONG"
    except (TypeError, ValueError):
        return "UNKNOWN"


def _parse_outcomes(db: Any, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """SELECT o.symbol, o.side, o.strategy_id, o.pnl, o.closed_at, i.raw_json
           FROM trade_outcomes o
           LEFT JOIN trade_intents i ON i.intent_id=o.intent_id
           WHERE o.status='CLOSED' AND o.pnl IS NOT NULL
           ORDER BY o.closed_at DESC LIMIT ?""",
        (int(limit),),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            raw = json.loads(item.get("raw_json") or "{}")
        except Exception:
            raw = {}
        evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        item["strategy_id"] = str(item.get("strategy_id") or raw.get("strategy_id") or "unknown")
        item["regime"] = str(evidence.get("market_regime") or "unknown")
        item["factor_group"] = factor_for(str(item.get("symbol") or ""))["group"]
        try:
            closed_dt = datetime.fromisoformat(
                str(item.get("closed_at") or "").replace("Z", "+00:00")
            )
            item["closed_dt"] = (closed_dt.replace(tzinfo=timezone.utc)
                                 if closed_dt.tzinfo is None else closed_dt.astimezone(timezone.utc))
        except Exception:
            item["closed_dt"] = None
        out.append(item)
    return out


def evaluate_portfolio_intent(intent: Any, portfolio: dict[str, Any] | None,
                              query_details: dict[str, dict[str, Any]] | None,
                              db: Any, config: dict[str, Any] | None = None,
                              now: datetime | None = None) -> list[dict[str, Any]]:
    """评价同因子方向聚集和同策略/状态连亏；首版结果只供 shadow。"""
    if getattr(intent, "action", None) != "open":
        return []
    cfg = config or load_decision_policy()
    checks = cfg.get("checks") or {}
    symbol = str(getattr(intent, "symbol", "") or "").upper()
    side = str(getattr(intent, "side", "") or "").upper()
    factor = factor_for(symbol, query_details)
    positions = [p for p in ((portfolio or {}).get("positions") or []) if isinstance(p, dict)]

    same_factor: list[dict[str, Any]] = []
    directional_notional = 0.0
    worst_case_stop_loss = 0.0
    stop_coverage = True
    for position in positions:
        if _position_side(position) != side:
            continue
        position_factor = factor_for(str(position.get("symbol") or ""), query_details)
        if position_factor["group"] != factor["group"]:
            continue
        same_factor.append(position)
        notional = _position_notional(position)
        if notional is not None:
            directional_notional += notional
        try:
            entry = float(position.get("entry_price") or 0.0)
            stop = float(position.get("stop_loss") or 0.0)
            qty = abs(float(position.get("qty") or position.get("positionAmt") or 0.0))
            if entry > 0 and stop > 0 and qty > 0:
                worst_case_stop_loss += abs(entry - stop) * qty
            else:
                stop_coverage = False
        except (TypeError, ValueError):
            stop_coverage = False
    candidate_notional = _position_notional({
        "entry_price": getattr(getattr(intent, "evidence", None), "trigger_price", 0.0),
        "qty": getattr(intent, "qty", 0.0),
    }) or 0.0
    directional_notional += candidate_notional
    try:
        candidate_entry = float(getattr(getattr(intent, "evidence", None), "trigger_price", 0.0))
        candidate_stop = float(getattr(intent, "stop_loss", 0.0) or 0.0)
        candidate_qty = abs(float(getattr(intent, "qty", 0.0) or 0.0))
        if candidate_entry > 0 and candidate_stop > 0 and candidate_qty > 0:
            worst_case_stop_loss += abs(candidate_entry - candidate_stop) * candidate_qty
        else:
            stop_coverage = False
    except (TypeError, ValueError):
        stop_coverage = False

    factor_cfg = checks.get("factor_concentration") or {}
    factor_mode = str(factor_cfg.get("mode") or "off")
    maximum = int(factor_cfg.get("maximum_same_direction") or 0)
    resulting_count = len(same_factor) + 1
    factor_status = "BLOCK" if maximum and resulting_count > maximum else (
        "WARN" if factor["confidence"] == "low" else "PASS"
    )
    factor_result = {
        "check_id": "factor_concentration", "version": 1, "mode": factor_mode,
        "status": factor_status,
        "reason": (f"同方向同因子持仓将达到 {resulting_count}，超过 {maximum}"
                   if factor_status == "BLOCK" else
                   f"同方向同因子持仓将达到 {resulting_count}；因子置信度={factor['confidence']}"),
        "evidence": {
            "symbol": symbol, "side": side, "factor": factor,
            "existing_symbols": [str(p.get("symbol") or "") for p in same_factor],
            "resulting_count": resulting_count,
            "directional_notional": round(directional_notional, 8),
            "worst_case_stop_loss_known": stop_coverage,
            "known_worst_case_stop_loss": round(worst_case_stop_loss, 8),
        },
    }

    loss_cfg = checks.get("loss_streak") or {}
    loss_mode = str(loss_cfg.get("mode") or "off")
    threshold = int(loss_cfg.get("consecutive_losses") or 0)
    cooldown_hours = float(loss_cfg.get("cooldown_hours") or 0.0)
    choppy_hours = float(loss_cfg.get("choppy_window_hours") or 0.0)
    current_regime = str(getattr(getattr(intent, "evidence", None), "market_regime", "unknown"))
    strategy_id = str(getattr(intent, "strategy_id", None) or "unknown")
    outcomes = _parse_outcomes(db)
    matching = [item for item in outcomes
                if item["strategy_id"] == strategy_id and item["regime"] == current_regime
                and str(item.get("side") or "").upper() == side
                and item["factor_group"] == factor["group"]]
    streak = 0
    for item in matching:
        if float(item.get("pnl") or 0.0) < 0:
            streak += 1
        else:
            break
    now_dt = now or datetime.now(timezone.utc)
    latest_loss_at = matching[0].get("closed_dt") if matching and streak else None
    cooldown_until = (latest_loss_at + timedelta(hours=cooldown_hours)
                      if latest_loss_at is not None else None)
    in_cooldown = bool(threshold and streak >= threshold and cooldown_until
                       and now_dt < cooldown_until)

    choppy_since = now_dt - timedelta(hours=choppy_hours)
    recent_loss_sides = {
        str(item.get("side") or "").upper()
        for item in outcomes
        if item["strategy_id"] == strategy_id and item["factor_group"] == factor["group"]
        and float(item.get("pnl") or 0.0) < 0 and item.get("closed_dt") is not None
        and item["closed_dt"] >= choppy_since
    }
    choppy = {"LONG", "SHORT"}.issubset(recent_loss_sides)
    loss_status = "BLOCK" if in_cooldown else ("WARN" if choppy else "PASS")
    loss_result = {
        "check_id": "loss_streak", "version": 1, "mode": loss_mode,
        "status": loss_status,
        "reason": (f"同类连续亏损 {streak} 次，冷却至 {cooldown_until.isoformat()}"
                   if in_cooldown else
                   "同策略同因子近期双向亏损，疑似震荡/模型失配" if choppy else
                   f"同类连续亏损 {streak} 次，未进入冷却"),
        "evidence": {
            "strategy_id": strategy_id, "regime": current_regime, "side": side,
            "factor_group": factor["group"], "consecutive_losses": streak,
            "cooldown_until": cooldown_until.isoformat() if cooldown_until else None,
            "choppy_or_model_mismatch": choppy,
        },
    }
    return [factor_result, loss_result]
