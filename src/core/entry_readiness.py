"""新开仓就绪判定与短时授权上下文。

看板是否“绿灯”和执行层是否允许 open 必须是同一个确定性判定。这里不发网络
请求，也不修改资金/订单，只消费已经采集的事实；因此 UI 轮询不会额外制造
交易所压力。缺少关键事实按不可证明处理，合法的 close/reduce/adjust 不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import Any, Mapping

from core.trading_scope import TradingScope


@dataclass(frozen=True)
class ReadinessSnapshot:
    """某一时刻的新开仓资格快照；只读且可安全放入运行状态。"""

    snapshot_id: str
    runtime_instance_id: str
    environment: str
    observed_at: float
    new_entry_allowed: bool
    blocking_reasons: tuple[str, ...]
    component_versions: dict[str, Any]
    scope_hash: str | None = None
    authorization_expires_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        """返回 JSON 可序列化的脱敏表示。"""
        return {
            "snapshot_id": self.snapshot_id,
            "runtime_instance_id": self.runtime_instance_id,
            "environment": self.environment,
            "observed_at": self.observed_at,
            "new_entry_allowed": self.new_entry_allowed,
            "blocking_reasons": list(self.blocking_reasons),
            "component_versions": dict(self.component_versions),
            "scope_hash": self.scope_hash,
            "authorization_expires_at": self.authorization_expires_at,
        }

    def authorization_context(self, *, decision_id: str | None = None) -> dict[str, Any]:
        """生成绑定到决策签名的上下文；禁止把快照对象直接交给 Broker。"""
        return {
            "authorization_id": self.snapshot_id,
            "decision_id": str(decision_id or ""),
            "authorized_at": self.observed_at,
            "expires_at": self.authorization_expires_at,
            "scope_hash": self.scope_hash,
            "environment": self.environment,
            "runtime_instance_id": self.runtime_instance_id,
        }


def _fact_bool(facts: Mapping[str, Any], key: str) -> bool | None:
    value = facts.get(key)
    return value if isinstance(value, bool) else None


def evaluate_entry_readiness(
    facts: Mapping[str, Any] | None,
    *,
    now: float | None = None,
    scope: TradingScope | None = None,
    runtime_instance_id: str | None = None,
    environment: str | None = None,
    authorization_ttl_seconds: float = 90.0,
) -> ReadinessSnapshot:
    """集中判断新开仓资格。

    关键布尔事实允许三态：True=已证明、False=明确失败、None=没有证据；后两者
    都会阻断 open。原因码稳定、可供 API、日志和测试直接比较。
    """
    facts = facts or {}
    observed_at = float(time.time() if now is None else now)
    reasons: list[str] = []

    def require_true(key: str, reason: str) -> None:
        if _fact_bool(facts, key) is not True:
            reasons.append(reason)

    if _fact_bool(facts, "service_accepting") is not True:
        reasons.append("SERVICE_NOT_ACCEPTING")
    if str(facts.get("control_state") or "") != "normal":
        reasons.append("CONTROL_NOT_NORMAL")
    require_true("equity_available", "EQUITY_UNAVAILABLE")
    require_true("account_risk_allow_new", "ACCOUNT_RISK_BLOCK")
    require_true("startup_safety_ready", "STARTUP_SAFETY_NOT_READY")
    require_true("order_submission_ready", "ORDER_SUBMISSION_NOT_READY")
    if _fact_bool(facts, "market_cooldown_active") is not False:
        reasons.append("MARKET_COOLDOWN")
    if _fact_bool(facts, "data_budget_available") is not True:
        reasons.append("MARKET_DATA_BUDGET")

    order_mirror = _fact_bool(facts, "order_mirror_ok")
    if order_mirror is not True:
        reasons.append("ORDER_MIRROR_UNPROVEN" if order_mirror is None else "ORDER_MIRROR_MISMATCH")
    protection_mirror = _fact_bool(facts, "protection_mirror_ok")
    if protection_mirror is not True:
        reasons.append("PROTECTION_MIRROR_UNPROVEN" if protection_mirror is None
                       else "PROTECTION_MIRROR_MISMATCH")

    try:
        unknown_orders = int(facts.get("unknown_orders", 0) or 0)
    except (TypeError, ValueError):
        unknown_orders = 1
    try:
        submitting_orders = int(facts.get("submitting_orders", 0) or 0)
    except (TypeError, ValueError):
        submitting_orders = 1
    if unknown_orders > 0:
        reasons.append("ORDER_OUTBOX_UNKNOWN")
    if submitting_orders > 0:
        reasons.append("ORDER_OUTBOX_SUBMITTING")

    scope_hash = scope.scope_hash if scope is not None else None
    if scope is None:
        reasons.append("SCOPE_UNAVAILABLE")
    elif facts.get("scope_hash") not in (None, scope.scope_hash):
        reasons.append("SCOPE_HASH_MISMATCH")
    if _fact_bool(facts, "persistence_available") is not True:
        reasons.append("PERSISTENCE_UNAVAILABLE")

    unique_reasons = tuple(dict.fromkeys(reasons))
    allowed = not unique_reasons
    ttl = max(1.0, float(authorization_ttl_seconds))
    expires_at = observed_at + ttl if allowed else None
    return ReadinessSnapshot(
        snapshot_id=str(uuid.uuid4()),
        runtime_instance_id=str(runtime_instance_id or facts.get("runtime_instance_id") or ""),
        environment=str(environment or facts.get("environment") or ""),
        observed_at=observed_at,
        new_entry_allowed=allowed,
        blocking_reasons=unique_reasons,
        component_versions=dict(facts.get("component_versions") or {}),
        scope_hash=scope_hash,
        authorization_expires_at=expires_at,
    )


def authorization_matches_context(decision: Any, context: Mapping[str, Any] | None = None,
                                 *, now: float | None = None) -> bool:
    """验证签名之外的授权时效/环境/scope/进程绑定。"""
    expires_at = getattr(decision, "pipeline_authorization_expires_at", None)
    if expires_at is not None:
        try:
            if float(expires_at) <= float(time.time() if now is None else now):
                return False
        except (TypeError, ValueError):
            return False
    if not context:
        return True
    checks = {
        "environment": "pipeline_environment",
        "scope_hash": "pipeline_scope_hash",
        "runtime_instance_id": "pipeline_runtime_instance_id",
    }
    for expected_key, decision_attr in checks.items():
        expected = context.get(expected_key)
        actual = getattr(decision, decision_attr, None)
        if expected is not None and actual != expected:
            return False
    return True
