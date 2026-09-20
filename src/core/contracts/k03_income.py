"""K03 收入事件、覆盖证明与成本归因契约。

金额一律用 ``Decimal`` 十进制字符串并在指纹前归一化；内部区间语义
``[start, end)``。幂等键 ``(environment, account_ref, income_type, tran_id)``
经去空白/大小写归一化并转义分隔符；同键不同内容进入隔离。空窗口不等于
零费用：只有来源正常、分页完整、区间覆盖（无任何已声明缺口）且完成迟到
复查时才可证明。成本状态与归因状态是两组独立枚举。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import (
    SCHEMA_VERSION,
    MoneyDecimal,
    canonical_hash,
    decimal_from_value,
    decimal_text,
    half_open_contains,
    half_open_problems,
    join_key_parts,
    normalize_key_part,
)

REASON_ID_CONFLICT = "INCOME_ID_CONFLICT"
REASON_INVALID_RANGE = "INVALID_RANGE"
REASON_GAP_INVALID_RANGE = "GAP_INVALID_RANGE"
REASON_COMPLETE_WITHOUT_PAGINATION_EVIDENCE = "COMPLETE_WITHOUT_PAGINATION_EVIDENCE"
REASON_COMPLETE_WITHOUT_COVERAGE = "COMPLETE_WITHOUT_COVERAGE"
REASON_COVERAGE_GAPS_DECLARED = "COVERAGE_GAPS_DECLARED"
REASON_NO_COVERAGE = "NO_COVERAGE"
REASON_COVERAGE_NOT_COMPLETE = "COVERAGE_NOT_COMPLETE"
REASON_COVERAGE_GAP = "COVERAGE_GAP"
REASON_PAGINATION_NOT_TERMINATED = "PAGINATION_NOT_TERMINATED"
REASON_WATERMARK_BEFORE_END = "WATERMARK_BEFORE_WINDOW_END"
REASON_SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
REASON_PAGINATION_INCOMPLETE = "PAGINATION_INCOMPLETE"
REASON_LATE_RECHECK_MISSING = "LATE_RECHECK_MISSING"
REASON_NO_MATCHING_LIFECYCLE = "NO_MATCHING_LIFECYCLE"
REASON_AMBIGUOUS_TWO_SIDED = "AMBIGUOUS_TWO_SIDED_POSITION"
REASON_AMBIGUOUS_LIFECYCLE = "AMBIGUOUS_LIFECYCLE"
REASON_ATTRIBUTION_UNKNOWN = "ATTRIBUTION_UNKNOWN"
REASON_UNATTRIBUTED_EXPOSURE = "UNATTRIBUTED_EXPOSURE"
REASON_ZERO_FEE_NOT_PROVABLE = "ZERO_FEE_NOT_PROVABLE"
REASON_NO_SOURCE_EVENTS = "NO_SOURCE_EVENTS"
REASON_COST_COMPLETE = "COST_COMPLETE"
REASON_PAGE_SHORT = "PAGE_SHORT"
REASON_FULL_PAGE = "FULL_PAGE"
REASON_SAME_MS_BOUNDARY = "SAME_MS_BOUNDARY"
REASON_WINDOW_EXHAUSTED = "WINDOW_EXHAUSTED"
REASON_PAGE_NOT_SORTED = "PAGE_NOT_SORTED"
REASON_PAGINATION_STALLED = "PAGINATION_STALLED"
REASON_TERMINATION_PROOF_MISSING = "TERMINATION_PROOF_MISSING"
REASON_TERMINATION_PROOF_MISMATCH = "TERMINATION_PROOF_MISMATCH"
REASON_TERMINATION_PROOF_INVALID = "TERMINATION_PROOF_INVALID"


class AllocationError(ValueError):
    """分摊结果出现与源金额符号矛盾的分量（含负权重放大）。"""


class IncomeEvent(BaseModel):
    """单条收入流水；tran_id 在同一用户同 incomeType 内唯一。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str
    account_ref: str
    income_type: str
    tran_id: str
    symbol: str
    asset: str
    amount: MoneyDecimal
    event_time_ms: int
    received_at_ms: int
    source_hash: str
    schema_version: str = SCHEMA_VERSION


def _as_event(raw: IncomeEvent | Mapping[str, Any]) -> IncomeEvent:
    if isinstance(raw, IncomeEvent):
        return raw
    return IncomeEvent.model_validate(raw)


def idempotency_key(event: IncomeEvent | Mapping[str, Any]) -> tuple[str, str, str, str]:
    """幂等主键（归一化后）；symbol/asset 属于被校验内容，不进键。"""
    item = _as_event(event)
    return (
        normalize_key_part(item.environment),
        normalize_key_part(item.account_ref),
        normalize_key_part(item.income_type),
        normalize_key_part(item.tran_id),
    )


def idempotency_key_text(event: IncomeEvent | Mapping[str, Any]) -> str:
    return join_key_parts(*idempotency_key(event))


def _canonical_existing_key(key: Any) -> str:
    """existing 映射键规范化：元组/列表按组件归一化，文本键去空白折叠大小写。"""
    if isinstance(key, (tuple, list)):
        return join_key_parts(*(normalize_key_part(part) for part in key))
    return normalize_key_part(key)


def income_content_hash(event: IncomeEvent | Mapping[str, Any]) -> str:
    """业务内容指纹；金额先归一化，不含接收时间与来源分页信息。"""
    item = _as_event(event)
    return canonical_hash({
        "environment": normalize_key_part(item.environment),
        "account_ref": normalize_key_part(item.account_ref),
        "income_type": normalize_key_part(item.income_type),
        "tran_id": normalize_key_part(item.tran_id),
        "symbol": item.symbol,
        "asset": item.asset,
        "amount": decimal_text(item.amount),
        "event_time_ms": item.event_time_ms,
    })


class QuarantineRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    reason_code: str
    existing_content_hash: str
    incoming_content_hash: str


class InsertResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    inserted: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    quarantined: tuple[QuarantineRecord, ...] = ()
    reason_codes: tuple[str, ...] = ()


def ingest_income(
    page: Sequence[IncomeEvent | Mapping[str, Any]],
    existing: Mapping[Any, IncomeEvent | str] | Sequence[IncomeEvent | Mapping[str, Any]] | None = None,
) -> InsertResult:
    """幂等吸收一页收入；同键同内容只记重复，同键异内容隔离。"""
    known: dict[str, str] = {}
    if isinstance(existing, Mapping):
        for key, value in existing.items():
            digest = value if isinstance(value, str) else income_content_hash(value)
            known[_canonical_existing_key(key)] = digest
    elif existing is not None:
        for item in existing:
            event = _as_event(item)
            known[idempotency_key_text(event)] = income_content_hash(event)

    inserted: list[str] = []
    duplicates: list[str] = []
    quarantined: list[QuarantineRecord] = []
    for raw in page:
        event = _as_event(raw)
        key = idempotency_key_text(event)
        digest = income_content_hash(event)
        if key in known:
            if known[key] == digest:
                duplicates.append(key)
            else:
                quarantined.append(QuarantineRecord(
                    key=key,
                    reason_code=REASON_ID_CONFLICT,
                    existing_content_hash=known[key],
                    incoming_content_hash=digest,
                ))
            continue
        inserted.append(key)
        known[key] = digest
    reason_codes = (REASON_ID_CONFLICT,) if quarantined else ()
    return InsertResult(
        inserted=tuple(inserted),
        duplicates=tuple(duplicates),
        quarantined=tuple(quarantined),
        reason_codes=reason_codes,
    )


class RangeGap(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start_ms: int
    end_ms: int
    reason: str = ""


class TerminationProof(BaseModel):
    """分页终止的结构化证据；自由文本不足以证明“已读完窗口”。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_ms: int
    end_ms: int
    page_limit: int = Field(gt=0)
    last_page_size: int = Field(ge=0)
    terminated_at_ms: int


class IncomeCoverage(BaseModel):
    """一个 income_type 的同步覆盖证据；[start, end) 内部语义。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    income_type: str
    start_ms: int
    end_ms: int
    watermark: int
    page_limit: int = Field(gt=0)
    pagination_terminated_evidence: str | None = None
    termination_proof: TerminationProof | None = None
    coverage_status: Literal["PENDING", "COMPLETE", "GAP"] = "PENDING"
    gaps: tuple[RangeGap, ...] = ()
    synced_at_ms: int = 0
    schema_version: str = SCHEMA_VERSION


def coverage_problems(coverage: IncomeCoverage | Mapping[str, Any] | None) -> list[str]:
    """覆盖自洽性缺口；不检查来源可用性（由调用方提供）。"""
    coverage_model = _coverage_model(coverage)
    if coverage_model is None:
        return [REASON_NO_COVERAGE]
    problems: list[str] = list(
        half_open_problems(coverage_model.start_ms, coverage_model.end_ms)
    )
    for gap in coverage_model.gaps:
        if gap.end_ms <= gap.start_ms:
            problems.append(REASON_GAP_INVALID_RANGE)
    if coverage_model.watermark < coverage_model.start_ms:
        problems.append(REASON_WATERMARK_BEFORE_END)
    if coverage_model.coverage_status == "PENDING":
        problems.append(REASON_COVERAGE_NOT_COMPLETE)
    if coverage_model.coverage_status == "COMPLETE":
        if coverage_model.termination_proof is None and not (
            coverage_model.pagination_terminated_evidence
        ):
            problems.append(REASON_COMPLETE_WITHOUT_PAGINATION_EVIDENCE)
        if coverage_model.watermark < coverage_model.end_ms:
            problems.append(REASON_COMPLETE_WITHOUT_COVERAGE)
        if coverage_model.gaps:
            problems.append(REASON_COVERAGE_GAPS_DECLARED)
    if coverage_model.coverage_status == "GAP":
        problems.append(REASON_COVERAGE_GAP)
    return list(dict.fromkeys(problems))


class ZeroFeeProof(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provable: bool
    reason_codes: tuple[str, ...] = ()


def zero_fee_proof(
    coverage: IncomeCoverage | Mapping[str, Any] | None,
    *,
    source_available: bool,
    pages_complete: bool,
    late_recheck_done: bool,
) -> ZeroFeeProof:
    """空窗口“零费用”可证明条件：来源正常 + 完整分页 + 区间覆盖 + 无缺口 + 迟到复查。"""
    coverage_model = _coverage_model(coverage)
    reasons: list[str] = []
    if coverage_model is None:
        reasons.append(REASON_NO_COVERAGE)
    else:
        if coverage_model.coverage_status == "GAP":
            reasons.append(REASON_COVERAGE_GAP)
        elif coverage_model.coverage_status != "COMPLETE":
            reasons.append(REASON_COVERAGE_NOT_COMPLETE)
        if coverage_model.gaps:
            if coverage_model.coverage_status == "COMPLETE":
                reasons.append(REASON_COVERAGE_GAPS_DECLARED)
            elif REASON_COVERAGE_GAP not in reasons:
                reasons.append(REASON_COVERAGE_GAP)
        proof = coverage_model.termination_proof
        if proof is None:
            reasons.append(REASON_TERMINATION_PROOF_MISSING)
        else:
            if (
                proof.start_ms != coverage_model.start_ms
                or proof.end_ms != coverage_model.end_ms
            ):
                reasons.append(REASON_TERMINATION_PROOF_MISMATCH)
            if (
                proof.last_page_size >= proof.page_limit
                or proof.terminated_at_ms < proof.end_ms
            ):
                reasons.append(REASON_TERMINATION_PROOF_INVALID)
        if coverage_model.watermark < coverage_model.end_ms:
            reasons.append(REASON_WATERMARK_BEFORE_END)
        reasons.extend(
            half_open_problems(coverage_model.start_ms, coverage_model.end_ms)
        )
    if not source_available:
        reasons.append(REASON_SOURCE_UNAVAILABLE)
    if not pages_complete:
        reasons.append(REASON_PAGINATION_INCOMPLETE)
    if not late_recheck_done:
        reasons.append(REASON_LATE_RECHECK_MISSING)
    unique = list(dict.fromkeys(reasons))
    return ZeroFeeProof(provable=not unique, reason_codes=tuple(unique))


class PaginationDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal[
        "CONTINUE", "TERMINATED", "INVALID_PAGE_LIMIT", "PAGE_NOT_SORTED",
        "PAGINATION_STALLED",
    ]
    next_start_ms: int | None = None
    overlap_recheck: bool = False
    reason_codes: tuple[str, ...] = ()


def plan_next_page(
    page: Sequence[IncomeEvent | Mapping[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
    page_limit: int,
) -> PaginationDecision:
    """决定是否继续翻页；同毫秒满页时以该毫秒为新起点（不加 1ms）。

    满页必须单调非降序，且新起点必须大于当前 start。无进展或乱序一律不得
    返回 CONTINUE：前者会死循环，后者会静默跳过窗口内未读收入。
    """
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or page_limit <= 0
    ):
        return PaginationDecision(
            status="INVALID_PAGE_LIMIT",
            reason_codes=(REASON_INVALID_RANGE,),
        )
    events = [_as_event(item) for item in page]
    if len(events) < page_limit:
        return PaginationDecision(status="TERMINATED", reason_codes=(REASON_PAGE_SHORT,))
    times = [item.event_time_ms for item in events]
    if any(times[index] < times[index - 1] for index in range(1, len(times))):
        return PaginationDecision(
            status="PAGE_NOT_SORTED",
            overlap_recheck=True,
            reason_codes=(REASON_PAGE_NOT_SORTED,),
        )
    boundary = max(times)
    if boundary >= end_ms:
        return PaginationDecision(
            status="TERMINATED", reason_codes=(REASON_WINDOW_EXHAUSTED,),
        )
    if boundary <= start_ms:
        return PaginationDecision(
            status="PAGINATION_STALLED",
            reason_codes=(REASON_FULL_PAGE, REASON_PAGINATION_STALLED),
        )
    same_ms_count = sum(1 for item in times if item == boundary)
    reasons = [REASON_FULL_PAGE]
    if same_ms_count > 1:
        reasons.append(REASON_SAME_MS_BOUNDARY)
    return PaginationDecision(
        status="CONTINUE",
        next_start_ms=boundary,
        overlap_recheck=True,
        reason_codes=tuple(reasons),
    )


class AttributionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    status: Literal["UNKNOWN", "UNATTRIBUTED", "ATTRIBUTED"]
    lifecycle_id: str | None = None
    reason_codes: tuple[str, ...] = ()


class AttributionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allocation_status: Literal["UNKNOWN", "UNATTRIBUTED", "ATTRIBUTED"]
    decisions: tuple[AttributionDecision, ...] = ()
    coverage_status: Literal["PENDING", "COMPLETE", "GAP"] | None = None
    reason_codes: tuple[str, ...] = ()


def _event_ref(raw: IncomeEvent | Mapping[str, Any]) -> tuple[str, str, int]:
    if isinstance(raw, IncomeEvent):
        return idempotency_key_text(raw), raw.symbol, raw.event_time_ms
    event_id = str(raw.get("event_id") or "")
    if not event_id:
        event_id = idempotency_key_text(raw)
    return event_id, str(raw.get("symbol") or ""), int(raw.get("event_time_ms") or 0)


def _coverage_model(
    coverage: IncomeCoverage | Mapping[str, Any] | None,
) -> IncomeCoverage | None:
    if coverage is None or isinstance(coverage, IncomeCoverage):
        return coverage
    return IncomeCoverage.model_validate(coverage)


def attribute_income(
    events: Sequence[IncomeEvent | Mapping[str, Any]],
    position_history: Sequence[Mapping[str, Any]],
    coverage: IncomeCoverage | Mapping[str, Any] | None = None,
) -> AttributionResult:
    """按事件时点的可证明仓位归因；双向仓不凭符号硬分摊，缺覆盖 fail-closed。"""
    coverage_model = _coverage_model(coverage)
    positions = [dict(item) for item in position_history]
    decisions: list[AttributionDecision] = []
    if coverage_model is None:
        blocked_code = REASON_NO_COVERAGE
    elif coverage_model.coverage_status != "COMPLETE":
        blocked_code = REASON_COVERAGE_NOT_COMPLETE
    elif coverage_model.gaps:
        blocked_code = REASON_COVERAGE_GAPS_DECLARED
    else:
        blocked_code = None
    for raw in events:
        event_id, symbol, event_time = _event_ref(raw)
        if blocked_code is not None:
            decisions.append(AttributionDecision(
                event_id=event_id,
                status="UNKNOWN",
                reason_codes=(blocked_code,),
            ))
            continue
        matching = [
            item for item in positions
            if str(item.get("symbol")) == symbol
            and int(item.get("opened_at_ms") or 0) <= event_time
            and (item.get("closed_at_ms") is None or int(item["closed_at_ms"]) >= event_time)
        ]
        sides = {str(item.get("position_side")) for item in matching}
        if not matching:
            decisions.append(AttributionDecision(
                event_id=event_id,
                status="UNATTRIBUTED",
                reason_codes=(REASON_NO_MATCHING_LIFECYCLE,),
            ))
        elif len(sides) > 1:
            decisions.append(AttributionDecision(
                event_id=event_id,
                status="UNATTRIBUTED",
                reason_codes=(REASON_AMBIGUOUS_TWO_SIDED,),
            ))
        elif len(matching) > 1:
            decisions.append(AttributionDecision(
                event_id=event_id,
                status="UNATTRIBUTED",
                reason_codes=(REASON_AMBIGUOUS_LIFECYCLE,),
            ))
        else:
            decisions.append(AttributionDecision(
                event_id=event_id,
                status="ATTRIBUTED",
                lifecycle_id=str(matching[0].get("lifecycle_id")),
            ))
    if not decisions:
        allocation = "ATTRIBUTED"
        reasons = [REASON_NO_SOURCE_EVENTS]
    elif any(item.status == "UNKNOWN" for item in decisions):
        allocation = "UNKNOWN"
        reasons = sorted({code for item in decisions for code in item.reason_codes})
    elif any(item.status == "UNATTRIBUTED" for item in decisions):
        allocation = "UNATTRIBUTED"
        reasons = sorted({code for item in decisions for code in item.reason_codes})
    else:
        allocation = "ATTRIBUTED"
        reasons = []
    return AttributionResult(
        allocation_status=allocation,
        decisions=tuple(decisions),
        coverage_status=coverage_model.coverage_status if coverage_model else None,
        reason_codes=tuple(reasons),
    )


class CostAttribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lifecycle_id: str
    event_id: str
    allocated_amount: MoneyDecimal
    method: str
    evidence: str
    version: str


class ConservationViolation(ValueError):
    """分摊金额总和与源事件可归因金额不一致。"""

    def __init__(self, source_amount: Decimal, allocated_total: Decimal) -> None:
        self.source_amount = source_amount
        self.allocated_total = allocated_total
        super().__init__(
            f"conservation violated: source={source_amount} allocated={allocated_total}"
        )


def allocation_total(allocations: Sequence[CostAttribution]) -> Decimal:
    return sum((item.allocated_amount for item in allocations), Decimal(0))


def check_conservation(
    source_amount: Decimal | str | int,
    allocations: Sequence[CostAttribution],
    *,
    tolerance: Decimal = Decimal(0),
) -> Decimal:
    """守恒校验：Σallocated == 可归因金额（默认精确相等）。"""
    source = decimal_from_value(source_amount)
    total = allocation_total(allocations)
    if abs(source - total) > tolerance:
        raise ConservationViolation(source, total)
    return total


def allocate_weighted(
    source_amount: Decimal | str | int,
    weights: Mapping[str, Decimal | str | int],
    *,
    method: str,
    version: str,
    evidence: str,
    event_id: str,
) -> tuple[CostAttribution, ...]:
    """按明示权重分摊；末腿承接余数，保证 Σallocated 精确等于源金额。

    权重必须非负；任何与源金额符号矛盾的分量（含量化取整导致的负末腿）
    抛 ``AllocationError``，不允许“总量守恒但单腿为负”。
    """
    amount = decimal_from_value(source_amount)
    if not weights:
        raise ValueError("weights must not be empty")
    ordered = sorted((str(key), decimal_from_value(value)) for key, value in weights.items())
    if any(weight < 0 for _, weight in ordered):
        raise ValueError("weights must be non-negative")
    weight_sum = sum((value for _, value in ordered), Decimal(0))
    if weight_sum <= 0:
        raise ValueError("weights must sum to a positive value")
    quantum = Decimal("0.00000001")
    allocated: list[CostAttribution] = []
    remaining = amount
    for index, (lifecycle_id, weight) in enumerate(ordered):
        if index == len(ordered) - 1:
            share = remaining
        else:
            share = (amount * weight / weight_sum).quantize(quantum, rounding=ROUND_HALF_UP)
            remaining -= share
        if amount >= 0 and share < 0:
            raise AllocationError("negative share for non-negative source amount")
        if amount < 0 and share > 0:
            raise AllocationError("positive share for negative source amount")
        allocated.append(CostAttribution(
            lifecycle_id=lifecycle_id,
            event_id=event_id,
            allocated_amount=share,
            method=method,
            evidence=evidence,
            version=version,
        ))
    return tuple(allocated)


class CostFinalization(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lifecycle_id: str
    cost_status: Literal["UNKNOWN", "INCOMPLETE", "COMPLETE"]
    coverage_status: Literal["PENDING", "COMPLETE", "GAP"] | None = None
    attribution_status: Literal["UNKNOWN", "UNATTRIBUTED", "ATTRIBUTED"]
    reason_codes: tuple[str, ...] = ()


def finalize_costs(
    lifecycle_id: str,
    *,
    coverage: IncomeCoverage | Mapping[str, Any] | None = None,
    attribution: AttributionResult | None = None,
    zero_fee: ZeroFeeProof | Mapping[str, Any] | None = None,
) -> CostFinalization:
    """成本完成判定独立于归因判定；空窗口不等于零费用。

    无事件时必须传入 ``zero_fee_proof`` 的结果对象（``ZeroFeeProof``）：
    不得用调用方自报布尔跳过零费用证明条件。
    """
    coverage_model = _coverage_model(coverage)
    coverage_status = coverage_model.coverage_status if coverage_model else None
    attribution_status = attribution.allocation_status if attribution else "UNKNOWN"
    if isinstance(zero_fee, ZeroFeeProof):
        proof: ZeroFeeProof | None = zero_fee
    elif isinstance(zero_fee, Mapping):
        proof = ZeroFeeProof.model_validate(zero_fee)
    else:
        proof = None
    reasons: list[str] = []
    if coverage_model is None:
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="UNKNOWN",
            coverage_status=coverage_status,
            attribution_status=attribution_status,
            reason_codes=(REASON_NO_COVERAGE,),
        )
    gaps_declared = bool(coverage_model.gaps)
    if coverage_model.coverage_status == "GAP":
        reasons.append(REASON_COVERAGE_GAP)
    elif coverage_model.coverage_status != "COMPLETE":
        reasons.append(REASON_COVERAGE_NOT_COMPLETE)
    elif gaps_declared:
        reasons.append(REASON_COVERAGE_GAPS_DECLARED)
    if reasons:
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="INCOMPLETE",
            coverage_status=coverage_status,
            attribution_status=attribution_status,
            reason_codes=tuple(reasons),
        )
    if attribution is None or not attribution.decisions:
        if proof is not None and proof.provable:
            return CostFinalization(
                lifecycle_id=lifecycle_id,
                cost_status="COMPLETE",
                coverage_status=coverage_status,
                attribution_status="ATTRIBUTED",
                reason_codes=(REASON_COST_COMPLETE,),
            )
        zero_fee_reasons = tuple(proof.reason_codes) if proof is not None else ()
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="INCOMPLETE",
            coverage_status=coverage_status,
            attribution_status="UNKNOWN",
            reason_codes=zero_fee_reasons or (REASON_ZERO_FEE_NOT_PROVABLE,),
        )
    if attribution.allocation_status == "UNKNOWN":
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="INCOMPLETE",
            coverage_status=coverage_status,
            attribution_status="UNKNOWN",
            reason_codes=(REASON_ATTRIBUTION_UNKNOWN,),
        )
    if attribution.allocation_status == "UNATTRIBUTED":
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="INCOMPLETE",
            coverage_status=coverage_status,
            attribution_status="UNATTRIBUTED",
            reason_codes=(REASON_UNATTRIBUTED_EXPOSURE,),
        )
    return CostFinalization(
        lifecycle_id=lifecycle_id,
        cost_status="COMPLETE",
        coverage_status=coverage_status,
        attribution_status="ATTRIBUTED",
        reason_codes=(REASON_COST_COMPLETE,),
    )


class OutcomeRevision(BaseModel):
    """迟到收入只触发审计修订，不新增闭合、不改原始记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome_id: str
    source_event_id: str
    revision_id: str = ""
    previous_revision_id: str | None = None
    reason_code: str = "LATE_INCOME"
    requires_recalculation: bool = True
    new_closed_outcome_created: bool = False
    original_closed_at_ms: int | None = None
    revision_version: str = SCHEMA_VERSION


def apply_late_income(
    event: IncomeEvent | Mapping[str, Any],
    closed_outcomes: Sequence[Mapping[str, Any]],
    *,
    lifecycle_id: str,
    previous_revision_id: str | None = None,
) -> OutcomeRevision:
    """把迟到收入绑定到唯一已闭合 outcome；缺失或多义时明确报错。"""
    event_model = _as_event(event)
    matches = [
        outcome for outcome in closed_outcomes
        if str(outcome.get("lifecycle_id")) == lifecycle_id
        and bool(outcome.get("closed"))
    ]
    if not matches:
        raise LookupError(f"no closed outcome for lifecycle {lifecycle_id}")
    if len(matches) > 1:
        raise LookupError(f"multiple closed outcomes for lifecycle {lifecycle_id}")
    outcome = matches[0]
    closed_at = outcome.get("closed_at_ms")
    source_event_id = idempotency_key_text(event_model)
    revision_id = "rev-" + canonical_hash({
        "outcome_id": str(outcome.get("outcome_id")),
        "source_event_id": source_event_id,
        "closed_at_ms": closed_at,
    })[:16]
    return OutcomeRevision(
        outcome_id=str(outcome.get("outcome_id")),
        source_event_id=source_event_id,
        revision_id=revision_id,
        previous_revision_id=previous_revision_id,
        original_closed_at_ms=int(closed_at) if closed_at is not None else None,
    )


def window_contains(coverage: IncomeCoverage, point_ms: int) -> bool:
    """区间覆盖判断；固定 [start, end) 语义。"""
    return half_open_contains(point_ms, coverage.start_ms, coverage.end_ms)


class K03SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    income_event: IncomeEvent
    income_coverage: IncomeCoverage
    insert_result: InsertResult
    attribution_decisions: AttributionDecision
    attribution_result: AttributionResult
    cost_attribution: CostAttribution
    cost_finalization: CostFinalization
    outcome_revision: OutcomeRevision
    range_gap: RangeGap
    zero_fee_proof: ZeroFeeProof
    pagination_decision: PaginationDecision
