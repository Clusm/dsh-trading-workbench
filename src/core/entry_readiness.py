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

from core.runtime_identity import (
    RUNTIME_INSTANCE_UNBOUND,
    is_placeholder_runtime_instance_id,
)
from core.trading_scope import TradingScope

#: 两态探测标记（U15-FU3 交付物存在性）：哨兵运行代在**消费入口**不再被当作
#: 合法身份。验收套件据此对"修前/修后"分别断言，不猜共享文件是否已应用补丁。
U15_FU3_ENTRY_READINESS_SENTINEL_GUARD = "U15-FU3"


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


def _identity_blocker_reasons(facts: Mapping[str, Any]) -> tuple[str, ...]:
    """并入调用方提供的 K01 身份阻断原因码（**存在性触发**）。

    只有当调用方显式放入 ``identity_blockers`` 事实时才生效：缺省（键不存在
    或为空）时完全维持原有判定，不新增任何默认闸门，因此旧调用方语义不变。
    只接受字符串序列；单个字符串按一个原因码处理，其它类型视为未提供。
    """
    raw = facts.get("identity_blockers")
    if isinstance(raw, str):
        candidates: tuple[Any, ...] = (raw,)
    elif isinstance(raw, (list, tuple, set, frozenset)):
        candidates = tuple(raw)
    else:
        return ()
    reasons: list[str] = []
    for item in candidates:
        text = str(item).strip()
        if text:
            reasons.append(text)
    return tuple(dict.fromkeys(reasons))


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

    U15 追加两条 **存在性触发** 的事实（不提供即完全维持原行为）：

    - ``identity_blockers``：身份类阻断原因码逐个并入 ``blocking_reasons``；
    - ``model_route_ok``：仅在键存在时判定，非 ``True`` 产出
      ``MODEL_ROUTE_UNPROVEN``（没有证据）或 ``MODEL_ROUTE_INVALID``（明确失败）。

    U13/F14 追加一条 **存在性触发** 的事实：

    - ``recovery_gate_allowed``：恢复门禁（``core.ops.recovery.RecoveryGate``）
      的开仓判定。非 ``True`` 时把 ``recovery_gate_reason_codes`` 并入阻断原因，
      因此"恢复完直接开仓"在执行层被拒，而不是只靠控制面状态间接兜底。

    U15-FU3 补一条**默认生效**的 fail-closed：运行代是占位哨兵（``"unknown"``
    等）与"没有运行代"同样是**未证明**，产出 ``RUNTIME_INSTANCE_UNBOUND``（复用
    既有码）。空/缺失仍走原有的 ``RUNTIME_INSTANCE_UNPROVEN``；真实例号不受
    影响。
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
    if _fact_bool(facts, "rate_coordination_available") is not True:
        reasons.append("RATE_COORDINATION_UNAVAILABLE")

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
    elif not facts.get("scope_hash"):
        reasons.append("SCOPE_HASH_UNPROVEN")
    elif facts.get("scope_hash") != scope.scope_hash:
        reasons.append("SCOPE_HASH_MISMATCH")
    effective_runtime_id = str(runtime_instance_id or facts.get("runtime_instance_id") or "")
    effective_environment = str(environment or facts.get("environment") or "")
    if not effective_runtime_id:
        reasons.append("RUNTIME_INSTANCE_UNPROVEN")
    elif is_placeholder_runtime_instance_id(effective_runtime_id):
        # U15-FU3：占位哨兵（``"unknown"`` 等，见
        # ``core.runtime_identity.RUNTIME_INSTANCE_PLACEHOLDERS``）非空，因此
        # 旧逻辑把它当成"身份已证明"。它与缺失同样**不能证明**运行代，按既有
        # 原因码 ``RUNTIME_INSTANCE_UNBOUND`` 阻断（复用 FU1/FU2 的唯一名单与
        # 唯一判定函数，未新造码、未另立第二套归一化）。真实例号不受影响。
        reasons.append(RUNTIME_INSTANCE_UNBOUND)
    if not effective_environment:
        reasons.append("ENVIRONMENT_UNPROVEN")
    if _fact_bool(facts, "persistence_available") is not True:
        reasons.append("PERSISTENCE_UNAVAILABLE")

    # 身份类阻断：仅当调用方显式提供事实时并入（存在性触发）。
    reasons.extend(_identity_blocker_reasons(facts))

    # U13/F14 恢复后先对账再开仓：调用方（TradingService）把
    # ``RecoveryGate.before_entry()`` 的判定作为事实注入。键不存在时完全不判定，
    # 避免给旧调用方新增闸门；键存在时非 True 即阻断，原因码用门禁自己的
    # （``RECONCILE_REQUIRED_BEFORE_ENTRY`` / ``GATE_CORRUPT`` / ``GATE_MISSING``
    # / ``EVIDENCE_STALE``），保证"看板红灯"与"执行层拒绝"是同一个判定。
    if "recovery_gate_allowed" in facts:
        gate_ok = _fact_bool(facts, "recovery_gate_allowed")
        if gate_ok is not True:
            provided = facts.get("recovery_gate_reason_codes")
            codes: tuple[Any, ...] = ()
            if isinstance(provided, str):
                codes = (provided,)
            elif isinstance(provided, (list, tuple, set, frozenset)):
                codes = tuple(provided)
            gate_reasons = tuple(
                dict.fromkeys(
                    str(item).strip() for item in codes if str(item).strip()))
            reasons.extend(gate_reasons or ("RECOVERY_GATE_UNPROVEN",))

    # 模型路由证明：键不存在时完全不判定，避免给旧调用方新增闸门。
    if "model_route_ok" in facts:
        route_ok = _fact_bool(facts, "model_route_ok")
        if route_ok is not True:
            reasons.append("MODEL_ROUTE_UNPROVEN" if route_ok is None else "MODEL_ROUTE_INVALID")

    unique_reasons = tuple(dict.fromkeys(reasons))
    allowed = not unique_reasons
    ttl = max(1.0, float(authorization_ttl_seconds))
    expires_at = observed_at + ttl if allowed else None
    return ReadinessSnapshot(
        snapshot_id=str(uuid.uuid4()),
        runtime_instance_id=effective_runtime_id,
        environment=effective_environment,
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
    if expires_at is None:
        return False
    try:
        if float(expires_at) <= float(time.time() if now is None else now):
            return False
    except (TypeError, ValueError):
        return False
    if not context:
        return False
    checks = {
        "environment": "pipeline_environment",
        "scope_hash": "pipeline_scope_hash",
        "runtime_instance_id": "pipeline_runtime_instance_id",
    }
    for expected_key, decision_attr in checks.items():
        expected = context.get(expected_key)
        actual = getattr(decision, decision_attr, None)
        if expected_key == "runtime_instance_id" and (
                is_placeholder_runtime_instance_id(expected)
                or is_placeholder_runtime_instance_id(actual)):
            # U15-FU3：占位哨兵不是实例号，"两侧逐字相等"因此**不构成**进程
            # 绑定（否则 ``"unknown" == "unknown"`` 会被读成"同一个运行代"）。
            # 复用 FU1/FU2 的唯一名单与唯一判定函数；比对项本身未改。
            return False
        if not expected or actual != expected:
            return False
    return True
