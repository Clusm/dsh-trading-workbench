"""K05 决策轨迹、事件与告警投递契约。

计数器把总轮次、API 尝试与决策分别计数并按 cycle 去重；span 不合法直接
拒绝。Incident 的 ACK 只是确认看到，RESOLVED 必须由 ACK 之后新观察到的、
带时间戳且类型明确的证据满足。告警状态阶梯把 HTTP 成功、供应商接收、
设备送达与人确认分开记录，缺前置事实的跃迁直接拒绝。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import SCHEMA_VERSION, canonical_hash

REASON_EVIDENCE_MISSING = "EVIDENCE_MISSING"
REASON_EVIDENCE_STALE = "EVIDENCE_STALE"
REASON_EVIDENCE_FROM_FUTURE = "EVIDENCE_FROM_FUTURE"
REASON_EVIDENCE_TIMESTAMP_MISSING = "EVIDENCE_TIMESTAMP_MISSING"
REASON_EVIDENCE_TYPE_INVALID = "EVIDENCE_TYPE_INVALID"
REASON_EVIDENCE_PREDATES_ACK = "EVIDENCE_PREDATES_ACK"
REASON_EVIDENCE_NOT_SERIALISABLE = "EVIDENCE_NOT_SERIALISABLE"
REASON_ACK_REQUIRED = "ACK_REQUIRED"
REASON_RECOVERY_FALSE = "RECOVERY_PREDICATE_FALSE"

VIOLATION_STATE_MISMATCH = "STATE_MISMATCH"
VIOLATION_DELIVERED_WITHOUT_RECEIPT = "DELIVERED_WITHOUT_DEVICE_RECEIPT"
VIOLATION_PROVIDER_RECEIPT_MISSING = "PROVIDER_RECEIPT_MISSING"
VIOLATION_SUBMITTED_WITHOUT_TIMESTAMP = "SUBMITTED_WITHOUT_TIMESTAMP"
VIOLATION_ACK_WITHOUT_SUBMISSION = "ACK_WITHOUT_SUBMISSION"
VIOLATION_TIME_ORDER = "TIME_ORDER_VIOLATION"

ALERT_STATE_ORDER = ("UNKNOWN", "SUBMITTED", "ACCEPTED", "DELIVERED", "ACKNOWLEDGED")


class UnclassifiedPurposeError(ValueError):
    """purpose 不在计数映射内；禁止猜测归类。"""


class SpanValidationError(ValueError):
    """span 时间/期限自相矛盾；tally 拒绝计数。"""

    def __init__(self, reason_codes: Sequence[str]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__("invalid span: " + ", ".join(self.reason_codes))


class TransitionError(ValueError):
    """告警状态阶梯被违反。"""


class ResolutionNotAllowedError(ValueError):
    """恢复证据不足时禁止把 Incident 标为 RESOLVED。"""

    def __init__(self, reason_codes: Sequence[str]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__("resolution not allowed: " + ", ".join(self.reason_codes))


class CounterKind(StrEnum):
    CYCLE = "cycle"
    API_ATTEMPT = "api_attempt"
    DECISION = "decision"


PURPOSE_KINDS: dict[str, CounterKind] = {
    "cycle": CounterKind.CYCLE,
    "api_attempt": CounterKind.API_ATTEMPT,
    "tool_call": CounterKind.API_ATTEMPT,
    "decision": CounterKind.DECISION,
}
IGNORED_PURPOSES = ("validation",)


def purpose_kind(purpose: str) -> CounterKind | None:
    """把 purpose 映射到计数器；未登记 purpose 抛错，validation 显式不计数。"""
    if purpose in PURPOSE_KINDS:
        return PURPOSE_KINDS[purpose]
    if purpose in IGNORED_PURPOSES:
        return None
    raise UnclassifiedPurposeError(f"unclassified purpose: {purpose}")


class DecisionTraceSpan(BaseModel):
    """单个阶段 span；deadline_remaining 由调用方在单调时钟上计算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_id: str
    stage: str
    request_id: str
    attempt: int = Field(ge=1)
    purpose: str
    started_ms: int
    ended_ms: int | None = None
    deadline_remaining_ms: int | None = None
    reason_code: str = ""
    schema_version: str = SCHEMA_VERSION

    def duration_ms(self) -> int | None:
        if self.ended_ms is None:
            return None
        return self.ended_ms - self.started_ms


SPAN_VIOLATION_TIME_ORDER = "ENDED_BEFORE_STARTED"
SPAN_VIOLATION_NEGATIVE_DEADLINE = "NEGATIVE_DEADLINE_REMAINING"


def span_violations(span: DecisionTraceSpan) -> tuple[str, ...]:
    found: list[str] = []
    if span.ended_ms is not None and span.ended_ms < span.started_ms:
        found.append(SPAN_VIOLATION_TIME_ORDER)
    if span.deadline_remaining_ms is not None and span.deadline_remaining_ms < 0:
        found.append(SPAN_VIOLATION_NEGATIVE_DEADLINE)
    return tuple(found)


class DecisionCounters(BaseModel):
    """三类计数器分离；禁止用一个“总次数”代表全部。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycles_total: int = 0
    api_attempts_total: int = 0
    decisions_total: int = 0


def increment(
    counters: DecisionCounters,
    kind: CounterKind,
    amount: int = 1,
) -> DecisionCounters:
    if amount < 0:
        raise ValueError("amount must be non-negative")
    if kind == CounterKind.CYCLE:
        return counters.model_copy(update={"cycles_total": counters.cycles_total + amount})
    if kind == CounterKind.API_ATTEMPT:
        return counters.model_copy(
            update={"api_attempts_total": counters.api_attempts_total + amount}
        )
    return counters.model_copy(update={"decisions_total": counters.decisions_total + amount})


def tally(spans: Sequence[DecisionTraceSpan]) -> DecisionCounters:
    """按 span 计数：违规 span 拒绝，轮次按 cycle_id 去重，API/决策不混。"""
    seen_cycles: set[str] = set()
    api_attempts = 0
    decisions = 0
    for span in spans:
        violations = span_violations(span)
        if violations:
            raise SpanValidationError(violations)
        kind = purpose_kind(span.purpose)
        if kind is None:
            continue
        if kind == CounterKind.CYCLE:
            seen_cycles.add(span.cycle_id)
        elif kind == CounterKind.API_ATTEMPT:
            api_attempts += 1
        else:
            decisions += 1
    return DecisionCounters(
        cycles_total=len(seen_cycles),
        api_attempts_total=api_attempts,
        decisions_total=decisions,
    )


class RecoveryPredicate(BaseModel):
    """恢复谓词：必须由新鲜证据满足，而不是缓存里的绿色状态。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    required_evidence: tuple[str, ...]
    max_age_ms: int = Field(gt=0)


class Incident(BaseModel):
    """稳定 key 的故障事件；ACK 与 RESOLVED 是两种不同事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_key: str
    first_seen_ms: int
    last_seen_ms: int
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    status: Literal["OPEN", "ACK", "RESOLVED"] = "OPEN"
    recovery_predicate: RecoveryPredicate
    profile: str
    object_ref: str
    ack_at_ms: int | None = None
    resolved_at_ms: int | None = None
    resolution_evidence_hash: str | None = None


def stable_incident_key(*, profile: str, object_ref: str, reason_code: str) -> str:
    return f"{profile}|{object_ref}|{reason_code}"


class RecoveryCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    recovered: bool
    reason_codes: tuple[str, ...] = ()


def check_recovery(
    incident: Incident,
    recovery_evidence: Mapping[str, Any] | None,
    *,
    now_ms: int,
) -> RecoveryCheck:
    """逐项核验恢复谓词；无时间戳、类型混淆、陈旧或为假都不得恢复。

    证据必须是显式 Mapping：``{"observed_at_ms": <int>, "value": <bool>}``
    （或 ``satisfied``）。裸布尔、字符串真值（``"false"``/``"0"``）与
    缺少时间戳的字典一律拒绝。
    """
    reasons: list[str] = []
    evidence = dict(recovery_evidence or {})
    ack_at = incident.ack_at_ms if incident.status == "ACK" else None
    for key in incident.recovery_predicate.required_evidence:
        if key not in evidence:
            reasons.append(REASON_EVIDENCE_MISSING)
            continue
        entry = evidence[key]
        if not isinstance(entry, Mapping):
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        observed = entry.get("observed_at_ms")
        if observed is None:
            reasons.append(REASON_EVIDENCE_TIMESTAMP_MISSING)
            continue
        if not isinstance(observed, int) or isinstance(observed, bool):
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        has_value = "value" in entry
        has_satisfied = "satisfied" in entry
        if has_value and not isinstance(entry["value"], bool):
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        if has_satisfied and not isinstance(entry["satisfied"], bool):
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        if has_value and has_satisfied and entry["value"] != entry["satisfied"]:
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        raw_value = entry.get("value", entry.get("satisfied"))
        if not isinstance(raw_value, bool):
            reasons.append(REASON_EVIDENCE_TYPE_INVALID)
            continue
        if observed > now_ms:
            reasons.append(REASON_EVIDENCE_FROM_FUTURE)
        elif now_ms - observed > incident.recovery_predicate.max_age_ms:
            reasons.append(REASON_EVIDENCE_STALE)
        if ack_at is not None and observed < ack_at:
            reasons.append(REASON_EVIDENCE_PREDATES_ACK)
        if not raw_value:
            reasons.append(REASON_RECOVERY_FALSE)
    unique = list(dict.fromkeys(reasons))
    return RecoveryCheck(recovered=not unique, reason_codes=tuple(unique))


def acknowledge(incident: Incident, *, now_ms: int) -> Incident:
    """仅允许 OPEN→ACK；重复 ACK 幂等，已 RESOLVED 不得降级为 ACK。"""
    if incident.status == "RESOLVED":
        raise TransitionError("cannot acknowledge resolved incident")
    if incident.status == "ACK":
        return incident
    return incident.model_copy(update={"status": "ACK", "ack_at_ms": now_ms})


def resolve_incident(
    incident: Incident,
    *,
    recovery_evidence: Mapping[str, Any] | None,
    now_ms: int,
) -> Incident:
    """必须先 ACK，再用 ACK 之后新观察到的证据满足谓词才允许 RESOLVED。"""
    if incident.status != "ACK":
        raise ResolutionNotAllowedError((REASON_ACK_REQUIRED,))
    check = check_recovery(incident, recovery_evidence, now_ms=now_ms)
    if not check.recovered:
        raise ResolutionNotAllowedError(check.reason_codes)
    try:
        evidence_hash = canonical_hash(dict(recovery_evidence or {}))
    except (TypeError, ValueError):
        raise ResolutionNotAllowedError((REASON_EVIDENCE_NOT_SERIALISABLE,)) from None
    return incident.model_copy(update={
        "status": "RESOLVED",
        "resolved_at_ms": now_ms,
        "resolution_evidence_hash": evidence_hash,
    })


class AlertDelivery(BaseModel):
    """一次告警投递尝试；每一级状态只由对应事实支撑。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    incident_key: str
    attempt: int = Field(ge=1)
    receiver_ref: str
    provider_receipt: str | None = None
    submitted_at_ms: int | None = None
    delivered_at_ms: int | None = None
    ack_at_ms: int | None = None
    state: Literal["UNKNOWN", "SUBMITTED", "ACCEPTED", "DELIVERED", "ACKNOWLEDGED"] = "UNKNOWN"
    schema_version: str = SCHEMA_VERSION


def evaluate_state(
    *,
    submitted_at_ms: int | None = None,
    provider_receipt: str | None = None,
    delivered_at_ms: int | None = None,
    ack_at_ms: int | None = None,
) -> str:
    """由事实推导最高可信状态；HTTP200 只到 SUBMITTED。"""
    if ack_at_ms is not None:
        return "ACKNOWLEDGED"
    if delivered_at_ms is not None:
        return "DELIVERED"
    if provider_receipt:
        return "ACCEPTED"
    if submitted_at_ms is not None:
        return "SUBMITTED"
    return "UNKNOWN"


def delivery_violations(delivery: AlertDelivery) -> tuple[str, ...]:
    found: list[str] = []
    derived = evaluate_state(
        submitted_at_ms=delivery.submitted_at_ms,
        provider_receipt=delivery.provider_receipt,
        delivered_at_ms=delivery.delivered_at_ms,
        ack_at_ms=delivery.ack_at_ms,
    )
    if delivery.state != derived:
        found.append(VIOLATION_STATE_MISMATCH)
    if delivery.state == "DELIVERED" and delivery.delivered_at_ms is None:
        found.append(VIOLATION_DELIVERED_WITHOUT_RECEIPT)
    if delivery.state in ("ACCEPTED", "DELIVERED") and not delivery.provider_receipt:
        found.append(VIOLATION_PROVIDER_RECEIPT_MISSING)
    if delivery.submitted_at_ms is None and delivery.state in (
        "SUBMITTED", "ACCEPTED", "DELIVERED", "ACKNOWLEDGED",
    ):
        found.append(VIOLATION_SUBMITTED_WITHOUT_TIMESTAMP)
    if delivery.ack_at_ms is not None and delivery.submitted_at_ms is None:
        found.append(VIOLATION_ACK_WITHOUT_SUBMISSION)
    timestamps = [
        item for item in (
            delivery.submitted_at_ms,
            delivery.delivered_at_ms,
            delivery.ack_at_ms,
        ) if item is not None
    ]
    if timestamps != sorted(timestamps):
        found.append(VIOLATION_TIME_ORDER)
    return tuple(found)


def transition_state(
    delivery: AlertDelivery,
    target: str,
    *,
    now_ms: int,
) -> AlertDelivery:
    """按阶梯提升状态；前置事实缺失时直接拒绝，不允许“先提升后补证”。"""
    if target not in ALERT_STATE_ORDER:
        raise TransitionError(f"unknown alert state: {target}")
    current = ALERT_STATE_ORDER.index(delivery.state)
    target_index = ALERT_STATE_ORDER.index(target)
    if target_index < current:
        raise TransitionError(f"cannot move backward: {delivery.state} -> {target}")
    updates: dict[str, Any] = {"state": target}
    if target == "SUBMITTED" and delivery.submitted_at_ms is None:
        updates["submitted_at_ms"] = now_ms
    if target == "ACCEPTED" and not delivery.provider_receipt:
        raise TransitionError("ACCEPTED requires provider_receipt")
    if target == "ACCEPTED" and delivery.submitted_at_ms is None:
        raise TransitionError("ACCEPTED requires prior SUBMITTED fact")
    if target == "DELIVERED" and delivery.submitted_at_ms is None:
        raise TransitionError("DELIVERED requires prior SUBMITTED fact")
    if target == "DELIVERED" and not delivery.provider_receipt:
        raise TransitionError("DELIVERED requires provider_receipt")
    if target == "DELIVERED" and delivery.delivered_at_ms is None:
        raise TransitionError("DELIVERED requires delivered_at_ms")
    if target == "ACKNOWLEDGED":
        submitted = updates.get("submitted_at_ms", delivery.submitted_at_ms)
        if submitted is None:
            raise TransitionError("ACKNOWLEDGED requires prior SUBMITTED fact")
        if delivery.ack_at_ms is None:
            updates["ack_at_ms"] = now_ms
    return delivery.model_copy(update=updates)


class K05SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    trace_span: DecisionTraceSpan
    counters: DecisionCounters
    incident: Incident
    recovery_check: RecoveryCheck
    alert_delivery: AlertDelivery
