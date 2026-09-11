"""新开仓的单一确定性入口与命名决策轨迹。"""
from __future__ import annotations

from typing import Any

from agents.models import TradeDecision


def filter_unauthorized_opens(decision: Any, verdict: Any, db: Any) -> tuple[Any, Any]:
    """同时检查原始与风控调整集合；拦截开仓后仍让退出及挂单维护继续。"""
    if decision.pipeline_authorized:
        return decision, verdict
    raw = list(decision.intents)
    adjusted = list(verdict.adjusted_intents)
    if not any(item.action == "open" for item in raw + adjusted):
        return decision, verdict
    db.log_risk_event(event_type="pipeline_bypass_blocked", severity="critical",
                      details="Broker 收到未授权开仓", action_taken="drop opens; preserve exits")
    kept_raw = [item for item in raw if item.action != "open"]
    # Broker 的空调整集合会回退原始集合，因此两份集合必须同时过滤。
    kept_adjusted = [item for item in adjusted if item.action != "open"]
    effective = kept_adjusted or kept_raw
    action = ("close" if any(item.action in {"close", "reduce"} for item in effective)
              else "adjust" if effective else "no_trade")
    return (decision.model_copy(update={"intents": kept_raw, "decision": action}),
            verdict.model_copy(update={"adjusted_intents": kept_adjusted}))


VALID_STATUSES = {"PASS", "WARN", "BLOCK", "NOT_EVALUATED"}
VALID_MODES = {"off", "shadow", "enforce"}
OPEN_CHECK_IDS = {
    "trend_alignment", "risk_reward", "entry_chase", "evidence_conflict",
    "invalidation_consistency", "factor_concentration", "loss_streak",
}


def normalize_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将各评价器输出收敛为同一契约；非法结果不得静默冒充 PASS。"""
    normalized: list[dict[str, Any]] = []
    for raw in checks or []:
        item = dict(raw or {})
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        status = str(item.get("status") or "NOT_EVALUATED").upper()
        mode = str(item.get("mode") or "off").lower()
        if status not in VALID_STATUSES:
            status = "NOT_EVALUATED"
        if mode not in VALID_MODES:
            mode, status = "enforce", "NOT_EVALUATED"
        normalized.append({
            "check_id": str(item.get("check_id") or "unknown_check"),
            "version": int(item.get("version") or 1), "mode": mode,
            "status": status, "reason": str(item.get("reason") or "未提供原因"),
            "evidence_at": item.get("evidence_at") or evidence.get("observed_at_ms")
                           or evidence.get("data_observed_at_ms"),
            "evidence": evidence,
            **({"state": item["state"]} if item.get("state") else {}),
        })
    return normalized


def authorize_decision(decision: TradeDecision, *, db: Any,
                       run_id: str | None = None) -> TradeDecision:
    """验证汇合后的候选并签发一次性管线授权；有缺口只丢弃 open，退出不受影响。"""
    checks = normalize_checks(list(decision.policy_checks))
    opens = [intent for intent in decision.intents if intent.action == "open"]
    ai_opens = [intent for intent in opens if intent.order_source == "ai"]
    strategy_opens = [intent for intent in opens if intent.order_source == "strategy"]
    exits = [intent for intent in decision.intents if intent.action != "open"]
    reasons: list[str] = []
    missing_evidence: list[str] = []
    if ai_opens:
        missing_evidence = [intent.symbol for intent in ai_opens if intent.evidence is None]
        present = {str(item.get("check_id")) for item in checks}
        missing_checks = sorted(OPEN_CHECK_IDS - present)
        enforced_blocks = [item for item in checks
                           if item["mode"] == "enforce" and item["status"] in {"BLOCK", "NOT_EVALUATED"}
                           and item["check_id"] != "position_guardian"]
        if missing_evidence:
            reasons.append(f"开仓缺结构化证据: {','.join(missing_evidence)}")
        if missing_checks:
            reasons.append(f"开仓缺管线检查: {','.join(missing_checks)}")
        if enforced_blocks:
            reasons.append("存在 enforce 阻断: " + ",".join(item["check_id"] for item in enforced_blocks))
    missing_strategy_provenance = [intent.symbol for intent in strategy_opens
                                   if not intent.strategy_id or not intent.ticket_id]
    if missing_strategy_provenance:
        reasons.append(f"策略开仓缺触发票据: {','.join(missing_strategy_provenance)}")
    accepted_opens = [] if reasons else opens
    if reasons:
        db.log_risk_event(event_type="pipeline_open_blocked", severity="critical",
                          details="；".join(reasons), action_taken="drop open intents",
                          raw={"run_id": run_id or "", "symbols": [item.symbol for item in opens]})
    intents = exits + accepted_opens
    stages = [
        {"stage": "position_guardian", "status": _stage(checks, {"position_guardian"})},
        {"stage": "data_and_evidence", "status": "PASS"},
        {"stage": "decision_quality", "status": _stage(checks, OPEN_CHECK_IDS - {"factor_concentration", "loss_streak"})},
        {"stage": "portfolio_context", "status": _stage(checks, {"factor_concentration", "loss_streak"})},
        {"stage": "risk_guard", "status": "PENDING"},
        {"stage": "outbox_usdm", "status": "PENDING"},
    ]
    # 修正条件表达式可读性：无开仓时数据证据阶段不应显示缺失。
    stages[1]["status"] = "BLOCK" if opens and reasons else "PASS"
    if not intents:
        decision_type = "no_trade"
    elif any(item.action == "open" for item in intents):
        decision_type = "open"
    elif any(item.action in {"close", "reduce"} for item in intents):
        decision_type = "close"
    else:
        decision_type = "adjust"
    return decision.model_copy(update={
        "decision": decision_type, "intents": intents, "policy_checks": checks,
        "pipeline_trace": stages, "pipeline_authorized": not reasons,
        "reasoning": decision.reasoning + (f" | [Pipeline] {'；'.join(reasons)}" if reasons else " | [Pipeline] 已授权"),
    })


def _stage(checks: list[dict[str, Any]], ids: set[str]) -> str:
    relevant = [item for item in checks if item["check_id"] in ids]
    if not relevant:
        return "NOT_EVALUATED"
    if any(item["mode"] == "enforce" and item["status"] in {"BLOCK", "NOT_EVALUATED"}
           for item in relevant):
        return "BLOCK"
    if any(item["status"] in {"BLOCK", "WARN", "NOT_EVALUATED"} for item in relevant):
        return "WARN"
    return "PASS"
