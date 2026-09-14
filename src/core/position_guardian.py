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


def _check(state: str, mode: str, reason: str, evidence: dict[str, Any],
           reason_code: str | None = None) -> dict[str, Any]:
    status = "BLOCK" if state == "INVALIDATED" else (
        "NOT_EVALUATED" if state in {"REVIEW_DUE", "UNKNOWN"} else "PASS"
    )
    return {
        "check_id": "position_guardian", "version": 1, "mode": mode,
        "status": status, "state": state, "reason": reason,
        "reason_code": reason_code or str(evidence.get("reason_code") or "OK"),
        "evidence_at": evidence.get("observed_at_ms"), "evidence": evidence,
    }


def _stable_close_id(intent_id: str) -> str:
    """一条开仓论点只允许一个 Guardian 退出命令，跨 K 线和重启仍稳定。"""
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:24]
    return f"mta-g-{digest}"


def _log_state_once(db: Any, *, event_type: str, details: str,
                    action_taken: str, symbol: str, state: str,
                    reason_code: str = "UNKNOWN") -> None:
    """轮询状态只在首次进入时记事件；兼容测试替身与旧审计实现。"""
    payload = {"symbol": symbol, "state": state, "reason_code": reason_code}
    method = getattr(db, "log_risk_event_once_while_open", None)
    if callable(method):
        method(event_type=event_type, severity="critical", details=details,
               action_taken=action_taken, raw=payload)
    else:
        db.log_risk_event(event_type=event_type, severity="critical", details=details,
                          action_taken=action_taken, raw=payload)


def _resolve_state(db: Any, event_type: str, symbol: str) -> None:
    """只收口当前 symbol 的旧 Guardian 状态，不影响其他仓位。"""
    method = getattr(db, "resolve_risk_incident", None)
    if callable(method):
        method(event_type, {"symbol": symbol})


def _resolve_absent_position_states(db: Any, active_symbols: set[str]) -> None:
    """平仓后自动收口该标的 Guardian 旧事件，避免历史 critical 永久悬挂。"""
    list_method = getattr(db, "list_risk_incidents", None)
    resolve_method = getattr(db, "resolve_risk_incident", None)
    if not callable(list_method) or not callable(resolve_method):
        return
    for row in list_method(limit=500):
        event_type = str(row["event_type"])
        status = str(row["status"])
        symbol = str(row["scope"] or "").upper()
        if (event_type in {"guardian_review_due", "guardian_invalidation"}
                and status != "RESOLVED" and symbol and symbol not in active_symbols):
            resolve_method(event_type, {"symbol": symbol})


def _review_reason_code(snapshot: dict[str, Any], interval: str,
                        invalidation_price: float, operator: str) -> str:
    """把 Guardian 的复核失败归类，便于值守区分网络/数据/证据问题。"""
    quality = snapshot.get("data_quality")
    if isinstance(quality, dict) and quality.get("reason_code"):
        return str(quality["reason_code"])
    if interval not in {"1m", "5m", "15m", "1h", "4h", "1d"}:
        return "EVIDENCE_INVALID_INTERVAL"
    if invalidation_price <= 0 or operator not in {"close_below", "close_above"}:
        return "EVIDENCE_INVALID_RULE"
    error = str(snapshot.get("error") or "")
    if any(marker in error for marker in ("Cooldown", "BudgetDeferred", "418", "429", "rate limit")):
        return "MARKET_DATA_UNAVAILABLE"
    if error:
        return "MARKET_DATA_UNAVAILABLE"
    return "KLINE_UNAVAILABLE"


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
    active_symbols = {
        str(position.get("symbol") or "").upper()
        for position in (positions or [])
        if str(position.get("source") or "ai") in allowed_sources
    }
    _resolve_absent_position_states(db, active_symbols)

    for position in positions or []:
        source = str(position.get("source") or "ai")
        if source not in allowed_sources:
            continue
        symbol = str(position.get("symbol") or "").upper()
        side = str(position.get("side") or "").upper()
        qty = abs(float(position.get("qty") or position.get("positionAmt") or 0.0))
        open_client_order_id = str(position.get("open_client_order_id") or "")
        row = (db.fetch_open_intent_by_client_order_id(open_client_order_id)
               if open_client_order_id else None)
        # legacy/人工接管仓位没有精确开仓命令，不能用同标的旧记录猜测论点。
        if row is None:
            evidence = {
                "symbol": symbol, "side": side, "source": source,
                "reason_code": "EVIDENCE_MISSING",
            }
            item = _check("REVIEW_DUE", effective_mode, "缺少可归因的 approved 开仓论点",
                          evidence, "EVIDENCE_MISSING")
            checks.append(item)
            _resolve_state(db, "guardian_invalidation", symbol)
            _log_state_once(
                db, event_type="guardian_review_due",
                details=f"{symbol} {side}: {item['reason']}",
                action_taken="keep exchange protection; require operator review",
                symbol=symbol, state="REVIEW_DUE", reason_code="EVIDENCE_MISSING",
            )
            continue
        try:
            raw = json.loads(row["raw_json"] or "{}")
            thesis = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else None
        except Exception:
            thesis = None
            raw = {}
        if not thesis:
            evidence = {
                "symbol": symbol, "side": side, "intent_id": row["intent_id"],
                "reason_code": "EVIDENCE_MISSING",
            }
            item = _check("REVIEW_DUE", effective_mode, "开仓记录缺少结构化 evidence",
                          evidence, "EVIDENCE_MISSING")
            checks.append(item)
            _resolve_state(db, "guardian_invalidation", symbol)
            _log_state_once(
                db, event_type="guardian_review_due",
                details=f"{symbol} {side}: {item['reason']}",
                action_taken="keep exchange protection; require operator review",
                symbol=symbol, state="REVIEW_DUE", reason_code="EVIDENCE_MISSING",
            )
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
            reason_code = _review_reason_code(
                snapshot, interval, invalidation_price, operator,
            )
            evidence["reason_code"] = reason_code
            item = _check("REVIEW_DUE", effective_mode, "闭合K线或结构无效点不可验证",
                          evidence, reason_code)
            checks.append(item)
            _resolve_state(db, "guardian_invalidation", symbol)
            _log_state_once(
                db, event_type="guardian_review_due",
                details=f"{symbol} {side}: {item['reason']}",
                action_taken="do not infer exit; keep protection",
                symbol=symbol, state="REVIEW_DUE", reason_code=reason_code,
            )
            continue
        closed_price = float(snapshot["closed_price"])
        invalidated = ((side == "LONG" and operator == "close_below" and closed_price < invalidation_price)
                       or (side == "SHORT" and operator == "close_above" and closed_price > invalidation_price))
        if not invalidated:
            _resolve_state(db, "guardian_review_due", symbol)
            _resolve_state(db, "guardian_invalidation", symbol)
            evidence["reason_code"] = "OK"
            checks.append(_check("ACTIVE", effective_mode, "最新闭合K线未证伪原论点",
                                 evidence, "OK"))
            continue
        evidence["reason_code"] = "THESIS_INVALIDATED"
        checks.append(_check("INVALIDATED", effective_mode, "最新闭合K线已证伪原论点",
                             evidence, "THESIS_INVALIDATED"))
        _resolve_state(db, "guardian_review_due", symbol)
        _log_state_once(
            db, event_type="guardian_invalidation",
            details=f"{symbol} {side}: close={closed_price}, {operator} {invalidation_price}",
            action_taken="create close intent" if effective_mode == "enforce" else "shadow only",
            symbol=symbol, state="INVALIDATED", reason_code="THESIS_INVALIDATED",
        )
        if effective_mode == "enforce" and qty > 0:
            intents.append(TradeIntent(
                symbol=symbol, action="close", side=side, qty=qty, leverage=1,
                client_order_id=_stable_close_id(str(row["intent_id"])),
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
