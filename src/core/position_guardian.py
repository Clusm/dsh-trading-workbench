"""既有仓位的确定性论点监督器。

Guardian 不调用模型、不创建新仓，也不从自由文本猜测退出条件。它只接受开仓时
持久化的结构化 evidence，并只用对应周期的已收盘 K 线确认失效。数据不可证明时
进入 REVIEW_DUE 并告警，绝不把网络失败解释成应当平仓。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from agents.models import TradeDecision, TradeIntent
from core.decision_policy import load_decision_policy
from data.market_query import query_guardian_closed_bar


SnapshotProvider = Callable[[str, str], dict[str, Any]]


def _check(state: str, mode: str, reason: str, evidence: dict[str, Any]) -> dict[str, Any]:
    status = "BLOCK" if state == "INVALIDATED" else (
        "NOT_EVALUATED" if state in {"REVIEW_DUE", "UNKNOWN"} else "PASS"
    )
    return {
        "check_id": "position_guardian", "version": 1, "mode": mode,
        "status": status, "state": state, "reason": reason,
        "evidence_at": evidence.get("observed_at_ms"), "evidence": evidence,
    }


def _stable_close_id(intent_id: str, closed_at_ms: int | None) -> str:
    digest = hashlib.sha256(f"{intent_id}|{closed_at_ms}".encode("utf-8")).hexdigest()[:24]
    return f"mta-g-{digest}"


def evaluate_positions(*, positions: list[dict[str, Any]], db: Any,
                       execution_mode: str, config: dict[str, Any] | None = None,
                       snapshot_provider: SnapshotProvider = query_guardian_closed_bar) -> TradeDecision:
    """复核 AI 持仓并返回可进入原有 RiskGuard/Outbox 的退出意图。"""
    cfg = config or load_decision_policy()
    guard_cfg = cfg.get("position_guardian") or {}
    configured_mode = str(guard_cfg.get("mode") or "off")
    enforce_modes = {str(item) for item in (guard_cfg.get("enforce_modes") or [])}
    allowed_sources = {str(item) for item in (guard_cfg.get("sources") or ["ai"])}
    effective_mode = ("enforce" if configured_mode == "enforce" and execution_mode in enforce_modes
                      else "shadow" if configured_mode != "off" else "off")
    checks: list[dict[str, Any]] = []
    intents: list[TradeIntent] = []

    for position in positions or []:
        source = str(position.get("source") or "ai")
        if source not in allowed_sources:
            continue
        symbol = str(position.get("symbol") or "").upper()
        side = str(position.get("side") or "").upper()
        qty = abs(float(position.get("qty") or position.get("positionAmt") or 0.0))
        row = db.fetch_latest_open_intent(symbol, side, source)
        if row is None:
            item = _check("REVIEW_DUE", effective_mode, "缺少可归因的 approved 开仓论点", {
                "symbol": symbol, "side": side, "source": source,
            })
            checks.append(item)
            db.log_risk_event(event_type="guardian_review_due", severity="critical",
                              details=f"{symbol} {side}: {item['reason']}",
                              action_taken="keep exchange protection; require operator review",
                              raw={"symbol": symbol, "state": "REVIEW_DUE"})
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
            thesis = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else None
        except Exception:
            thesis = None
            raw = {}
        if not thesis:
            item = _check("REVIEW_DUE", effective_mode, "开仓记录缺少结构化 evidence", {
                "symbol": symbol, "side": side, "intent_id": row["intent_id"],
            })
            checks.append(item)
            db.log_risk_event(event_type="guardian_review_due", severity="critical",
                              details=f"{symbol} {side}: {item['reason']}",
                              action_taken="keep exchange protection; require operator review",
                              raw={"symbol": symbol, "state": "REVIEW_DUE"})
            continue
        interval = str(thesis.get("confirmation_interval") or "")
        operator = str(thesis.get("invalidation_operator") or "")
        try:
            invalidation_price = float(thesis.get("invalidation_price") or 0.0)
            snapshot = snapshot_provider(symbol, interval)
        except Exception as exc:
            snapshot = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            invalidation_price = 0.0
        evidence = {
            "symbol": symbol, "side": side, "source": source,
            "intent_id": row["intent_id"], "strategy_id": raw.get("strategy_id"),
            "operator": operator, "invalidation_price": invalidation_price,
            **snapshot,
        }
        if (not snapshot.get("ok") or interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}
                or invalidation_price <= 0 or operator not in {"close_below", "close_above"}):
            item = _check("REVIEW_DUE", effective_mode, "闭合K线或结构无效点不可验证", evidence)
            checks.append(item)
            db.log_risk_event(event_type="guardian_review_due", severity="critical",
                              details=f"{symbol} {side}: {item['reason']}",
                              action_taken="do not infer exit; keep protection",
                              raw={"symbol": symbol, "state": "REVIEW_DUE"})
            continue
        closed_price = float(snapshot["closed_price"])
        invalidated = ((side == "LONG" and operator == "close_below" and closed_price < invalidation_price)
                       or (side == "SHORT" and operator == "close_above" and closed_price > invalidation_price))
        if not invalidated:
            checks.append(_check("ACTIVE", effective_mode, "最新闭合K线未证伪原论点", evidence))
            continue
        checks.append(_check("INVALIDATED", effective_mode, "最新闭合K线已证伪原论点", evidence))
        db.log_risk_event(event_type="guardian_invalidation", severity="critical",
                          details=f"{symbol} {side}: close={closed_price}, {operator} {invalidation_price}",
                          action_taken="create close intent" if effective_mode == "enforce" else "shadow only",
                          raw={"symbol": symbol, "state": "INVALIDATED", "mode": effective_mode})
        if effective_mode == "enforce" and qty > 0:
            intents.append(TradeIntent(
                symbol=symbol, action="close", side=side, qty=qty, leverage=1,
                client_order_id=_stable_close_id(str(row["intent_id"]), snapshot.get("closed_at_ms")),
                order_source=source, strategy_id=raw.get("strategy_id"),
                reasoning="Position Guardian：闭合K线确认原开仓论点失效",
                invalidation=raw.get("invalidation"),
            ))
    return TradeDecision(
        decision="close" if intents else "no_trade", channel="merged", intents=intents,
        reasoning=(f"Position Guardian 复核 {len(checks)} 个 AI 仓位；"
                   f"有效退出 {len(intents)} 个；运行模式 {effective_mode}"),
        policy_checks=checks,
    )
