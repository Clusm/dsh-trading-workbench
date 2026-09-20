"""新开仓的单一确定性入口与命名决策轨迹。"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from agents.models import TradeDecision
from core.entry_readiness import authorization_matches_context


def _execution_payload(decision: Any, verdict: Any) -> bytes:
    intents = verdict.adjusted_intents if verdict.adjusted_intents else decision.intents
    opens = [item.model_dump(mode="json") for item in intents if item.action == "open"]
    authorization = {
        "authorization_id": getattr(decision, "pipeline_authorization_id", None),
        "decision_id": getattr(decision, "pipeline_decision_id", None),
        "authorized_at": getattr(decision, "pipeline_authorized_at", None),
        "expires_at": getattr(decision, "pipeline_authorization_expires_at", None),
        "scope_hash": getattr(decision, "pipeline_scope_hash", None),
        "environment": getattr(decision, "pipeline_environment", None),
        "runtime_instance_id": getattr(decision, "pipeline_runtime_instance_id", None),
    }
    return json.dumps({"opens": opens, "authorization": authorization},
                      ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def sign_execution(decision: Any, verdict: Any, secret: str | None,
                   authorization_context: Mapping[str, Any] | None = None) -> Any:
    """在 RiskGuard 之后签署实际开仓集合；无开仓不需要签名。"""
    if not secret or not any(item.action == "open" for item in
                             (verdict.adjusted_intents or decision.intents)):
        return decision
    updates: dict[str, Any] = {}
    if authorization_context:
        updates = {
            "pipeline_authorization_id": authorization_context.get("authorization_id"),
            "pipeline_decision_id": authorization_context.get("decision_id"),
            "pipeline_authorized_at": authorization_context.get("authorized_at"),
            "pipeline_authorization_expires_at": authorization_context.get("expires_at"),
            "pipeline_scope_hash": authorization_context.get("scope_hash"),
            "pipeline_environment": authorization_context.get("environment"),
            "pipeline_runtime_instance_id": authorization_context.get("runtime_instance_id"),
        }
        decision = decision.model_copy(update=updates)
    signature = hmac.new(secret.encode("utf-8"), _execution_payload(decision, verdict),
                         hashlib.sha256).hexdigest()
    return decision.model_copy(update={"pipeline_signature": signature})


def _has_valid_signature(decision: Any, verdict: Any, secret: str | None,
                         authorization_context: Mapping[str, Any] | None = None) -> bool:
    if not secret or not decision.pipeline_signature:
        return False
    if not authorization_matches_context(decision, authorization_context):
        return False
    expected = hmac.new(secret.encode("utf-8"), _execution_payload(decision, verdict),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(str(decision.pipeline_signature), expected)


def filter_unauthorized_opens(decision: Any, verdict: Any, db: Any,
                              secret: str | None = None,
                              authorization_context: Mapping[str, Any] | None = None
                              ) -> tuple[Any, Any]:
    """同时检查原始与风控调整集合；拦截开仓后仍让退出及挂单维护继续。"""
    if (decision.pipeline_authorized
            and _has_valid_signature(decision, verdict, secret, authorization_context)):
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


# --------------------------------------------------------------------------- #
# U02：管理动作（DL03）——管理优先队列与非 OPEN 意图的授权引用
# --------------------------------------------------------------------------- #
#: 非 OPEN 意图缺少已绑定管理授权引用时的原因码（K04 §6）。
NEEDS_AUTH = "NEEDS_AUTH"


class ManagementEnforcementError(RuntimeError):
    """``enforce=True`` 在没有可绑定字段的意图模型上被启用（fail-closed）。"""

    def __init__(self, reason_codes: Any) -> None:
        self.reason_codes = tuple(str(item) for item in reason_codes)
        super().__init__("; ".join(self.reason_codes))

#: 管理动作集合：退出/减仓/调整保护；它们只减不增，不属于新开仓。
MANAGEMENT_ACTIONS = frozenset({"close", "reduce", "adjust"})


def management_intents(intents: Any) -> list[Any]:
    """从意图集合中挑出管理动作（退出/减仓/调整）。"""
    return [item for item in (intents or ())
            if str(getattr(item, "action", "") or "") in MANAGEMENT_ACTIONS]


def priority_order(intents: Any) -> list[Any]:
    """管理动作优先于新开仓进入执行队列。

    DL03 的可执行含义：模型/宏观卡死只应推迟新仓研究，不应推迟已授权的退出。
    排序稳定，两类内部各自保持原相对顺序，因此只改提交次序、不改语义。
    """
    return sorted(
        list(intents or ()),
        key=lambda item: (0 if str(getattr(item, "action", "") or "")
                          in MANAGEMENT_ACTIONS else 1),
    )


def intent_authorization_ref(intent: Any) -> str | None:
    """读取意图携带的管理授权引用（兼容两种字段名，均不存在时为 None）。"""
    for name in ("management_authorization_id", "management_id"):
        value = getattr(intent, name, None)
        if value:
            return str(value)
    return None


#: 意图模型无法携带管理授权引用时，``enforce=True`` 一律拒绝启用。
ENFORCEMENT_UNSUPPORTED = "MANAGEMENT_ENFORCEMENT_UNSUPPORTED"


def authorization_ref_supported(intents: Any) -> bool:
    """意图模型是否真的能携带管理授权引用（而不是被 pydantic 静默丢弃）。

    ``TradeIntent`` 目前**没有** ``management_authorization_id`` 字段，且没有
    ``model_config``（extra 默认 ``ignore``）——调用方即使传了也会被静默扔掉。
    因此 ``filter_unbound_management(enforce=True)`` 会把**所有**管理意图判为
    未绑定并全部摘除，等价于"翻一个布尔就关掉全部退出"（U02/A_r1 P2-6）。
    """
    for item in (intents or ()):
        model_fields = getattr(type(item), "model_fields", None)
        if model_fields is not None and any(
                name in model_fields
                for name in ("management_authorization_id", "management_id")):
            return True
        if hasattr(item, "management_authorization_id") or hasattr(item, "management_id"):
            return True
    return False


def filter_unbound_management(
    decision: Any, verdict: Any, db: Any, *,
    bound_authorization_ids: Any = (), enforce: bool = False,
) -> tuple[Any, Any, list[str]]:
    """非 OPEN 意图必须携带**已绑定**的管理授权引用，否则 ``NEEDS_AUTH``。

    ``enforce=False``（默认）只做可观测：把未绑定的管理意图记入风险事件与返回
    值，但**不改变执行集合**——未接入 U02 授权服务的既有调用方行为完全不变。
    ``enforce=True`` 时把未绑定的管理意图从原始与风控调整集合中一并摘除，关键点
    是**摘除而不是降级**：被摘除的意图绝不会被重新解释成开仓（摘除后若已无任何
    意图，决策降级为 ``no_trade`` 而非 ``open``）。

    **fail-closed 前置条件**：意图模型必须真的能携带授权引用
    （:func:`authorization_ref_supported`）。否则 ``enforce=True`` 会把全部退出
    静默摘掉——那是"一开就全停"的雷，而不是安全开关，因此在摘除任何意图之前
    直接抛 :class:`ManagementEnforcementError`。

    本函数当前**没有生产调用方**（属 U02 的接入面，交由后续任务接线）：默认
    ``enforce=False`` 不改变任何既有行为。
    """
    bound = {str(item) for item in (bound_authorization_ids or ())}
    raw = list(decision.intents)
    adjusted = list(verdict.adjusted_intents)
    if enforce and not authorization_ref_supported(raw + adjusted):
        raise ManagementEnforcementError([ENFORCEMENT_UNSUPPORTED])

    def _unbound(items: list[Any]) -> list[str]:
        return [
            str(getattr(item, "symbol", "") or "")
            for item in items
            if str(getattr(item, "action", "") or "") in MANAGEMENT_ACTIONS
            and intent_authorization_ref(item) not in bound
        ]

    unbound = list(dict.fromkeys(_unbound(raw) + _unbound(adjusted)))
    if not unbound:
        return decision, verdict, []
    if db is not None:
        db.log_risk_event(
            event_type="management_intent_unbound", severity="warn",
            details=f"非 OPEN 意图缺管理授权引用（{NEEDS_AUTH}）: {','.join(unbound)}",
            action_taken="record only" if not enforce else "drop management intents",
        )
    if not enforce:
        return decision, verdict, unbound

    def _keep(items: list[Any]) -> list[Any]:
        return [
            item for item in items
            if not (str(getattr(item, "action", "") or "") in MANAGEMENT_ACTIONS
                    and intent_authorization_ref(item) not in bound)
        ]

    kept_raw = _keep(raw)
    kept_adjusted = _keep(adjusted)
    effective = kept_adjusted or kept_raw
    if any(str(getattr(item, "action", "")) == "open" for item in effective):
        action = "open"
    elif effective:
        action = "adjust"
    else:
        action = "no_trade"
    return (decision.model_copy(update={"intents": kept_raw, "decision": action}),
            verdict.model_copy(update={"adjusted_intents": kept_adjusted}), unbound)


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
            "reason_code": str(item.get("reason_code") or evidence.get("reason_code") or "UNKNOWN"),
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
