"""USD-M 收入（资金费/手续费/已实现盈亏）分页同步、幂等入库、归因与会话勾稽。

本模块只做纯解析、分页决策、幂等判定与勾稽等式，**不发网络请求、不直接写数据库、
不读 .env/data**。调用方（``binance_futures`` 适配层）把官方 ``GET /fapi/v1/income``
的行转换成原始映射后传入；持久化由 :class:`IncomeReconciliationBook` 通过注入的
存储协议完成。

刻意保守的不变量（对应 U01 必达要点）：

* 官方分页是**包含端点**（``startTime``/``endTime`` 均含），内部区间固定半开
  ``[start, end)``；适配时必须 ``endTime = end - 1ms``，不允许用近似值。
* 幂等主键 ``(environment, account_ref, income_type, tran_id)``（K03 归一化）；
  重复投递只记重复，同键不同内容进隔离，**绝不按金额/时间相近猜测合并**。
* ``coverage_status`` / ``cost_status`` / ``attribution_status`` 是三组独立枚举；
  「窗口无行」只有在来源正常 + 分页终止 + 水位达到窗口末尾 + 无已声明缺口时才
  是「已验证为零」，否则是「未知」，未知**不得静默折算为零**。
* 迟到收入只产生 ``OutcomeRevision``（``new_closed_outcome_created=False``），
  同一迟到事件重复到达不重复闭合。
* 会话勾稽：fill 手续费是唯一费用入口；``COMMISSION``/``REALIZED_PNL`` 收入只用于
  交叉核对，**不再次加进**已由 fill 计入的成本或收益。非 USDT 费用在事件时点无
  有来源汇率时不出净值（``converted=None``），账户层保留原币金额。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from core.contracts import (
    AttributionResult,
    CostAttribution,
    CostFinalization,
    IncomeCoverage,
    IncomeEvent,
    InsertResult,
    OutcomeRevision,
    RangeGap,
    TerminationProof,
    allocate_weighted,
    apply_late_income,
    attribute_income,
    check_conservation,
    coverage_problems,
    decimal_text,
    finalize_costs,
    idempotency_key_text,
    ingest_income,
    plan_next_page,
    zero_fee_proof,
)

# --------------------------------------------------------------------------- #
# 常量与 reason code
# --------------------------------------------------------------------------- #

INCOME_TYPE_FUNDING_FEE = "FUNDING_FEE"
INCOME_TYPE_COMMISSION = "COMMISSION"
INCOME_TYPE_REALIZED_PNL = "REALIZED_PNL"
INCOME_TYPE_TRANSFER = "TRANSFER"
INCOME_TYPE_INSURANCE_CLEAR = "INSURANCE_CLEAR"

#: 由 fill 侧（userTrades）计入成本的类型；收入侧同类型行只做交叉核对。
FILL_OWNED_TYPES = frozenset({INCOME_TYPE_COMMISSION, INCOME_TYPE_REALIZED_PNL})

OFFICIAL_PAGE_LIMIT_MAX = 1000
DEFAULT_PAGE_LIMIT = 1000

REASON_INVALID_PAGE_LIMIT = "INVALID_PAGE_LIMIT"
REASON_INVALID_WINDOW = "INVALID_WINDOW"
REASON_INVALID_ROW = "INCOME_ROW_INVALID"
REASON_SOURCE_ERROR = "SOURCE_ERROR"
REASON_PAGE_CAP_EXCEEDED = "PAGE_CAP_EXCEEDED"
REASON_PAGINATION_STALLED = "PAGINATION_STALLED"
REASON_PAGE_NOT_SORTED = "PAGE_NOT_SORTED"
REASON_ID_CONFLICT = "INCOME_ID_CONFLICT"
REASON_ZERO_PROVEN = "ZERO_FEE_PROVEN"
REASON_UNCONVERTED_FX = "NO_SOURCED_FX_RATE"
REASON_FEE_CROSS_CHECK_MISMATCH = "FILL_INCOME_FEE_MISMATCH"
REASON_FEE_CROSS_CHECK_OK = "FILL_INCOME_FEE_MATCH"
REASON_DOUBLE_COUNT = "INCOME_FEE_DOUBLE_COUNT"
REASON_WALLET_UNBALANCED = "WALLET_IDENTITY_UNBALANCED"
REASON_WALLET_BALANCED = "WALLET_IDENTITY_BALANCED"
REASON_LATE_REVISION_DUPLICATE = "LATE_REVISION_ALREADY_APPLIED"

FX_TOLERANCE = Decimal("0.00000001")
MONEY_QUANTUM = Decimal("0.00000001")


class InvalidIncomeRow(ValueError):
    """收入行缺少真值所需字段；调用方必须整体拒绝而不是静默跳过。"""


class DoubleCountError(ValueError):
    """同一笔手续费/已实现盈亏同时经 fill 与 income 两条口径计入。"""


class WalletIdentityError(ValueError):
    """会话勾稽输入自相矛盾（例如同名字段被重复提供）。"""


# --------------------------------------------------------------------------- #
# 官方包含端点 <-> 内部半开区间
# --------------------------------------------------------------------------- #


def inclusive_bounds(start_ms: int, end_ms: int) -> tuple[int, int]:
    """内部 ``[start, end)`` → 官方包含端点 ``[start, end-1]``（毫秒）。"""
    start, end = int(start_ms), int(end_ms)
    if end <= start:
        raise InvalidIncomeRow(REASON_INVALID_WINDOW)
    return start, end - 1


def bounds_from_inclusive(start_ms: int, end_ms_inclusive: int) -> tuple[int, int]:
    """官方包含端点 ``[start, end]`` → 内部 ``[start, end+1)``。"""
    start, end = int(start_ms), int(end_ms_inclusive)
    if end < start:
        raise InvalidIncomeRow(REASON_INVALID_WINDOW)
    return start, end + 1


def build_income_query(
    start_ms: int,
    end_ms: int,
    *,
    page: int,
    limit: int,
    income_type: str | None = None,
) -> dict[str, Any]:
    """构造官方 Get Income History 查询参数（含端点映射与 page/limit 校验）。"""
    if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= OFFICIAL_PAGE_LIMIT_MAX):
        raise InvalidIncomeRow(REASON_INVALID_PAGE_LIMIT)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise InvalidIncomeRow(REASON_INVALID_PAGE_LIMIT)
    start, end_inclusive = inclusive_bounds(start_ms, end_ms)
    params: dict[str, Any] = {
        "startTime": start,
        "endTime": end_inclusive,
        "page": page,
        "limit": limit,
    }
    if income_type:
        params["incomeType"] = str(income_type).upper()
    return params


def window_contains(start_ms: int, end_ms: int, point_ms: int) -> bool:
    """半开区间归属判断；``end`` 端点本身不属于本窗口。"""
    return int(start_ms) <= int(point_ms) < int(end_ms)


# --------------------------------------------------------------------------- #
# 行规范化
# --------------------------------------------------------------------------- #


def canonical_income_type(value: Any) -> str:
    """income_type 规范化：去空白 + 大写；未知类型保留原文以便审计。"""
    return str(value or "").strip().upper()


def _required_text(raw: Mapping[str, Any], *names: str) -> str:
    for name in names:
        if name in raw and raw[name] not in (None, ""):
            return str(raw[name]).strip()
    raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: missing {'/'.join(names)}")


def _required_int(raw: Mapping[str, Any], *names: str) -> int:
    value = _required_text(raw, *names)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: {'/'.join(names)} not int") from exc


def _required_money(raw: Mapping[str, Any], *names: str) -> str:
    value = _required_text(raw, *names)
    try:
        amount = Decimal(value)
    except Exception as exc:  # noqa: BLE001 - 任何不可解析都按结构化拒绝
        raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: {'/'.join(names)} not decimal") from exc
    if not amount.is_finite():
        raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: {'/'.join(names)} not finite")
    return decimal_text(amount)


def normalize_income_row(
    raw: Mapping[str, Any],
    *,
    environment: str,
    account_ref: str,
    received_at_ms: int,
    source_hash: str = "",
    default_asset: str = "USDT",
) -> IncomeEvent:
    """官方收入行 → :class:`IncomeEvent`；缺字段直接抛错，不静默跳过。

    ``symbol`` 允许为空（账户级收入如 TRANSFER 无 symbol），但 ``tran_id`` 必须存在：
    它是同用户同 incomeType 内的唯一身份，缺失即无法幂等，必须整体拒收。
    """
    if not isinstance(raw, Mapping):
        raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: row not mapping")
    income_type = canonical_income_type(_required_text(raw, "incomeType", "income_type"))
    tran_id = _required_text(raw, "tranId", "tran_id")
    event_time = _required_int(raw, "time", "event_time_ms")
    if event_time <= 0:
        raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: time must be positive")
    return IncomeEvent(
        environment=str(environment or "unknown").lower(),
        account_ref=str(account_ref or "unknown"),
        income_type=income_type,
        tran_id=tran_id,
        symbol=str(raw.get("symbol") or "").strip().upper(),
        asset=str(raw.get("asset") or default_asset).strip().upper(),
        amount=_required_money(raw, "income", "amount"),
        event_time_ms=event_time,
        received_at_ms=int(received_at_ms),
        source_hash=str(source_hash or ""),
    )


# --------------------------------------------------------------------------- #
# 单窗口分页同步
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WindowSync:
    """一个 income_type 在一个 ``[start, end)`` 窗口内的同步证据。"""

    income_type: str
    start_ms: int
    end_ms: int
    coverage: IncomeCoverage
    events: tuple[IncomeEvent, ...]
    insert: InsertResult
    source_available: bool
    pages_fetched: int
    reason_codes: tuple[str, ...]
    quarantine: tuple[Any, ...] = ()

    @property
    def confirmed_empty(self) -> bool:
        """「已验证为零」：来源正常 + 分页终止 + 水位到窗口末尾 + 无缺口 + 无事件。"""
        return (
            self.source_available
            and not self.events
            and self.coverage.coverage_status == "COMPLETE"
            and not self.coverage.gaps
        )


def sync_income_window(
    fetch_page: Callable[[int], Sequence[Mapping[str, Any]]],
    *,
    environment: str,
    account_ref: str,
    income_type: str,
    start_ms: int,
    end_ms: int,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    received_at_ms: int,
    existing: Mapping[Any, Any] | Sequence[IncomeEvent] | None = None,
    source_available: bool = True,
    source_error: str | None = None,
    max_pages: int = 2000,
) -> WindowSync:
    """按官方 ``page``/``limit`` 翻页读完一个窗口，并给出结构化终止证据。

    ``fetch_page(page_no)`` 由适配层提供，返回该页原始行（不得自行排序/去重）。
    短页即终止；满页必须页内非降序，且必须出现新 tran_id 才算前进——重复返回
    同一页一律判 ``PAGINATION_STALLED``，不允许死循环，也不允许用
    ``last_time + 1`` 跳过同毫秒记录。
    """
    start, end = int(start_ms), int(end_ms)
    if end <= start:
        raise InvalidIncomeRow(REASON_INVALID_WINDOW)
    if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not (1 <= page_limit <= OFFICIAL_PAGE_LIMIT_MAX):
        raise InvalidIncomeRow(REASON_INVALID_PAGE_LIMIT)
    income_type = canonical_income_type(income_type)

    if not source_available:
        coverage = IncomeCoverage(
            income_type=income_type,
            start_ms=start,
            end_ms=end,
            watermark=start,
            page_limit=page_limit,
            coverage_status="GAP",
            gaps=(RangeGap(start_ms=start, end_ms=end, reason=source_error or REASON_SOURCE_ERROR),),
            synced_at_ms=int(received_at_ms),
        )
        return WindowSync(
            income_type=income_type, start_ms=start, end_ms=end, coverage=coverage,
            events=(), insert=InsertResult(), source_available=False, pages_fetched=0,
            reason_codes=tuple(coverage_problems(coverage)) + (REASON_SOURCE_ERROR,),
        )

    if isinstance(existing, Mapping):
        existing_events: list[Any] = list(existing.values())
    else:
        existing_events = list(existing or ())
    all_events: list[IncomeEvent] = []
    inserted: list[str] = []
    duplicates: list[str] = []
    quarantined: list[Any] = []
    reason_codes: list[str] = []
    gaps: list[RangeGap] = []
    pages = 0
    terminated = False
    last_page_size = 0
    seen_page_ids: set[frozenset[str]] = set()

    for page_no in range(1, max_pages + 1):
        rows = list(fetch_page(page_no))
        pages = page_no
        last_page_size = len(rows)
        page_events = [
            normalize_income_row(
                row, environment=environment, account_ref=account_ref,
                received_at_ms=received_at_ms,
                source_hash=str(row.get("source_hash") or ""),
            )
            for row in rows
        ]
        page_ids = frozenset(idempotency_key_text(item) for item in page_events)
        result = ingest_income(page_events, list(all_events) + existing_events)
        inserted.extend(result.inserted)
        duplicates.extend(result.duplicates)
        quarantined.extend(result.quarantined)
        all_events.extend(
            item for item in page_events
            if idempotency_key_text(item) not in {record.key for record in result.quarantined}
        )

        if len(rows) < page_limit:
            terminated = True
            break

        decision = plan_next_page(
            page_events, start_ms=start, end_ms=end, page_limit=page_limit
        )
        if decision.status == "PAGE_NOT_SORTED":
            reason_codes.append(REASON_PAGE_NOT_SORTED)
            gaps.append(RangeGap(start_ms=start, end_ms=end, reason=REASON_PAGE_NOT_SORTED))
            break
        if decision.status == "TERMINATED":  # 窗口末尾已被覆盖
            terminated = True
            break
        if decision.status == "PAGINATION_STALLED":
            reason_codes.append(REASON_PAGINATION_STALLED)
            gaps.append(RangeGap(start_ms=start, end_ms=end, reason=REASON_PAGINATION_STALLED))
            break
        if page_ids and page_ids in seen_page_ids:
            # 满页且内容与上一页完全相同：官方 page 参数没有前进，禁止无限翻页。
            reason_codes.append(REASON_PAGINATION_STALLED)
            gaps.append(RangeGap(start_ms=start, end_ms=end, reason=REASON_PAGINATION_STALLED))
            break
        seen_page_ids.add(page_ids)
    else:
        reason_codes.append(REASON_PAGE_CAP_EXCEEDED)
        gaps.append(RangeGap(start_ms=start, end_ms=end, reason=REASON_PAGE_CAP_EXCEEDED))

    if quarantined:
        reason_codes.append(REASON_ID_CONFLICT)
        # 冲突键的金额真值已不可确定：该 tran_id 必须整体退出求和，不能保留先到达的
        # 那一版（否则等于按到达顺序猜金额）。
        conflicted = {record.key for record in quarantined}
        all_events = [
            item for item in all_events
            if idempotency_key_text(item) not in conflicted
        ]
        inserted = [key for key in inserted if key not in conflicted]

    # 水位 = 已证明覆盖到的时点：分页终止 ⇒ 整个 [start,end) 已覆盖 ⇒ watermark=end。
    watermark = end if terminated else max([start] + [item.event_time_ms for item in all_events])
    proof = (
        TerminationProof(
            start_ms=start, end_ms=end, page_limit=page_limit,
            last_page_size=last_page_size, terminated_at_ms=max(int(received_at_ms), end),
        )
        if terminated else None
    )
    if terminated and not gaps:
        coverage_status = "COMPLETE"
    else:
        coverage_status = "GAP"
    coverage = IncomeCoverage(
        income_type=income_type,
        start_ms=start,
        end_ms=end,
        watermark=watermark,
        page_limit=page_limit,
        termination_proof=proof,
        coverage_status=coverage_status,
        gaps=tuple(gaps),
        synced_at_ms=int(received_at_ms),
    )
    reason_codes.extend(coverage_problems(coverage))
    unique_reasons = tuple(dict.fromkeys(reason_codes))
    return WindowSync(
        income_type=income_type, start_ms=start, end_ms=end, coverage=coverage,
        events=tuple(all_events),
        insert=InsertResult(
            inserted=tuple(inserted), duplicates=tuple(duplicates),
            quarantined=tuple(quarantined),
            reason_codes=(REASON_ID_CONFLICT,) if quarantined else (),
        ),
        source_available=True, pages_fetched=pages, reason_codes=unique_reasons,
        quarantine=tuple(quarantined),
    )


def window_zero_fee_proof(
    sync: WindowSync, *, late_recheck_done: bool
) -> Any:
    """把窗口同步证据折成 K03 零费用证明（自报布尔一律不采信）。"""
    return zero_fee_proof(
        sync.coverage,
        source_available=sync.source_available,
        pages_complete=sync.coverage.coverage_status == "COMPLETE",
        late_recheck_done=late_recheck_done,
    )


def window_cost_finalization(
    sync: WindowSync,
    *,
    attribution: AttributionResult | None,
    late_recheck_done: bool,
    lifecycle_id: str,
) -> CostFinalization:
    """窗口成本判定：来源不可用时是 **UNKNOWN**（未知），不是 INCOMPLETE 也不是零。

    来源正常但覆盖/归因/隔离存在未闭合项时，由 K03 ``finalize_costs`` 回带
    ``INCOMPLETE`` 与具体 reason code；只有全部可复核条件成立才可能 COMPLETE。
    """
    if not sync.source_available:
        return finalize_costs(lifecycle_id, coverage=None, attribution=None)
    proof = None
    if not sync.events:
        # 空窗口必须走零费用证明；不得因为没有事件就默认零。
        proof = window_zero_fee_proof(sync, late_recheck_done=late_recheck_done)
    result = finalize_costs(
        lifecycle_id, coverage=sync.coverage,
        attribution=(attribution.result if attribution is not None else None) if sync.events else None,
        zero_fee=proof,
    )
    if sync.quarantine:
        # 存在隔离冲突时成本不可能闭合：宁可 INCOMPLETE 也不能静默为零。
        status = "INCOMPLETE" if result.cost_status == "COMPLETE" else result.cost_status
        return result.model_copy(update={
            "cost_status": status,
            "reason_codes": tuple(dict.fromkeys(result.reason_codes + (REASON_ID_CONFLICT,))),
        })
    return result


# --------------------------------------------------------------------------- #
# 归因与守恒
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class IncomeAttribution:
    """按事件拆分资金费：已归因 / 未归因 / 未知，三者互不混用。"""

    attributed_usdt: Decimal
    unattributed_usdt: Decimal
    unknown_usdt: Decimal | None
    result: AttributionResult
    attributed_keys: tuple[str, ...]
    unattributed_keys: tuple[str, ...]
    unknown_keys: tuple[str, ...]

    def conservation_source_amount(self) -> Decimal:
        return self.attributed_usdt + self.unattributed_usdt


def _usdt_amount(event: IncomeEvent, fx_rates: Mapping[str, str | Decimal]) -> Decimal | None:
    """事件在 USDT 口径下的金额；非 USDT 且无有来源汇率时返回 None（不是 0）。"""
    asset = str(event.asset or "").upper()
    amount = Decimal(event.amount)
    if asset == "USDT":
        return amount
    rate = fx_rates.get(asset)
    if rate in (None, ""):
        return None
    try:
        return amount * Decimal(str(rate))
    except Exception:  # noqa: BLE001
        return None


def attribute_funding(
    events: Sequence[IncomeEvent],
    position_history: Sequence[Mapping[str, Any]],
    coverage: IncomeCoverage | None,
    *,
    fx_rates: Mapping[str, str | Decimal] | None = None,
) -> IncomeAttribution:
    """资金费按事件时点的可证明仓位归因；缺覆盖 fail-closed 为 UNKNOWN。

    非 USDT 事件没有有来源汇率时不进入 USDT 口径（返回 None 而不是 0）。
    """
    rates = dict(fx_rates or {})
    # 幂等键唯一是上游不变量；此处再去重一次，确保重复投递不会被归因两遍。
    funding: list[IncomeEvent] = []
    seen: set[str] = set()
    for item in events:
        if canonical_income_type(item.income_type) != INCOME_TYPE_FUNDING_FEE:
            continue
        key = idempotency_key_text(item)
        if key in seen:
            continue
        seen.add(key)
        funding.append(item)
    result = attribute_income(funding, position_history, coverage)
    status_by_key = {item.event_id: item.status for item in result.decisions}
    attributed = Decimal(0)
    unattributed = Decimal(0)
    unknown = Decimal(0)
    has_unknown = False
    a_keys: list[str] = []
    u_keys: list[str] = []
    k_keys: list[str] = []
    for event in funding:
        key = idempotency_key_text(event)
        amount = _usdt_amount(event, rates)
        if amount is None:
            # 不可换算：既不计入已归因也不计入未归因的 USDT 净值。
            continue
        status = status_by_key.get(key, "UNKNOWN")
        if status == "ATTRIBUTED":
            attributed += amount
            a_keys.append(key)
        elif status == "UNATTRIBUTED":
            unattributed += amount
            u_keys.append(key)
        else:
            unknown += amount
            has_unknown = True
            k_keys.append(key)
    return IncomeAttribution(
        attributed_usdt=attributed,
        unattributed_usdt=unattributed,
        unknown_usdt=unknown if has_unknown else None,
        result=result,
        attributed_keys=tuple(a_keys),
        unattributed_keys=tuple(u_keys),
        unknown_keys=tuple(k_keys),
    )


def allocate_funding_to_lifecycles(
    attribution: IncomeAttribution,
    *,
    lifecycle_weights: Mapping[str, Any],
    version: str,
    evidence: str,
) -> tuple[CostAttribution, ...]:
    """把已归因资金费按明示权重分摊；Σ分摊 == 源金额（守恒）。"""
    source = attribution.attributed_usdt
    allocations = allocate_weighted(
        source, lifecycle_weights,
        method="funding_weighted", version=version, evidence=evidence,
        event_id="FUNDING_AGGREGATE",
    )
    check_conservation(source, allocations)
    return allocations


# --------------------------------------------------------------------------- #
# 会话钱包勾稽
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionReconciliation:
    """账户级勾稽等式结果；未知项显式报告，不折算为零。"""

    opening_wallet_usdt: Decimal
    closing_wallet_usdt: Decimal
    components: dict[str, Decimal]
    unexplained_usdt: Decimal
    balanced: bool
    converted_usdt: Decimal
    unconverted: tuple[dict[str, Any], ...]
    fee_cross_check: dict[str, Any]
    reason_codes: tuple[str, ...]

    def identity_lhs(self) -> Decimal:
        return self.closing_wallet_usdt - self.opening_wallet_usdt

    def identity_rhs(self) -> Decimal:
        return sum(self.components.values(), Decimal(0)) + self.unexplained_usdt


def _sum_commissions(
    fills: Iterable[Mapping[str, Any]], *, fx_rates: Mapping[str, str | Decimal]
) -> tuple[Decimal | None, tuple[dict[str, Any], ...]]:
    """fill 手续费合计（成本为负）；非 USDT 且无汇率时整体未知（None）。"""
    total = Decimal(0)
    unconverted: list[dict[str, Any]] = []
    for row in fills:
        commission = row.get("commission")
        if commission in (None, ""):
            return None, tuple(unconverted)
        asset = str(row.get("commission_asset") or row.get("asset") or "USDT").upper()
        raw = abs(Decimal(str(commission)))
        if asset == "USDT":
            total -= raw
            continue
        rate = fx_rates.get(asset)
        if rate in (None, ""):
            unconverted.append({
                "asset": asset, "native_amount": decimal_text(raw),
                "reason": REASON_UNCONVERTED_FX,
            })
            continue
        total -= raw * Decimal(str(rate))
    return total, tuple(unconverted)


def _sum_realized_pnl(
    fills: Iterable[Mapping[str, Any]], *, fx_rates: Mapping[str, str | Decimal]
) -> Decimal | None:
    total = Decimal(0)
    for row in fills:
        pnl = row.get("realized_pnl", row.get("realizedPnl"))
        if pnl in (None, ""):
            return None
        asset = str(row.get("commission_asset") or row.get("asset") or "USDT").upper()
        value = Decimal(str(pnl))
        if asset == "USDT":
            total += value
            continue
        rate = fx_rates.get(asset)
        if rate in (None, ""):
            return None
        total += value * Decimal(str(rate))
    return total


def reconcile_session(
    *,
    opening_wallet_usdt: Any,
    closing_wallet_usdt: Any,
    fills: Sequence[Mapping[str, Any]] = (),
    income_events: Sequence[IncomeEvent] = (),
    attribution: IncomeAttribution | None = None,
    transfers_usdt: Any = "0",
    other_classified_usdt: Any = "0",
    fx_rates: Mapping[str, str | Decimal] | None = None,
) -> SessionReconciliation:
    """会话钱包勾稽；income 侧的手续费/已实现盈亏只交叉核对，不重复计入。

    ``net_fees_from_fills`` 是唯一费用入口：``COMMISSION`` 收入行与之核对
    （``FILL_INCOME_FEE_MATCH/MISMATCH``），``REALIZED_PNL`` 同理，二者都不会被
    再加进 ``realized_trade_pnl``。若调用方把同类型收入行塞进可加分量，
    抛 :class:`DoubleCountError`（见 :func:`assert_no_double_count`）。
    """
    rates = dict(fx_rates or {})
    opening = Decimal(str(opening_wallet_usdt))
    closing = Decimal(str(closing_wallet_usdt))

    net_fees, fee_unconverted = _sum_commissions(fills, fx_rates=rates)
    realized = _sum_realized_pnl(fills, fx_rates=rates)

    funding_attributed = attribution.attributed_usdt if attribution else Decimal(0)
    funding_unattributed = attribution.unattributed_usdt if attribution else Decimal(0)
    funding_unknown = attribution.unknown_usdt if attribution else None

    commissions_income = Decimal(0)
    realized_income = Decimal(0)
    income_unconverted: list[dict[str, Any]] = []
    for event in income_events:
        kind = canonical_income_type(event.income_type)
        if kind not in FILL_OWNED_TYPES:
            continue
        amount = _usdt_amount(event, rates)
        if amount is None:
            income_unconverted.append({
                "tran_id": event.tran_id, "income_type": kind,
                "asset": event.asset, "native_amount": decimal_text(Decimal(event.amount)),
                "reason": REASON_UNCONVERTED_FX,
            })
            continue
        if kind == INCOME_TYPE_COMMISSION:
            commissions_income += amount
        else:
            realized_income += amount

    components: dict[str, Decimal] = {
        "realized_trade_pnl_usdt": realized if realized is not None else Decimal(0),
        "net_fees_from_fills_usdt": net_fees if net_fees is not None else Decimal(0),
        "funding_attributed_usdt": funding_attributed,
        "funding_unattributed_usdt": funding_unattributed,
        "transfers_usdt": Decimal(str(transfers_usdt)),
        "other_classified_usdt": Decimal(str(other_classified_usdt)),
    }
    reason_codes: list[str] = []
    if realized is None or net_fees is None or funding_unknown is not None:
        # 关键分量未知时不得输出“已平衡”结论。
        reason_codes.append(REASON_UNCONVERTED_FX)

    unexplained = (closing - opening) - sum(components.values(), Decimal(0))
    balanced = unexplained == 0 and not reason_codes

    fee_cross_check: dict[str, Any] = {
        "fill_fees_usdt": net_fees,
        "income_commission_usdt": commissions_income,
        "income_realized_usdt": realized_income,
        "status": "UNKNOWN",
    }
    if net_fees is not None and (commissions_income or realized_income):
        # 两侧都是「成本为负」口径：COMMISSION 收入行应等于 fill 手续费合计。
        if abs(commissions_income - net_fees) <= FX_TOLERANCE:
            fee_cross_check["status"] = "MATCH"
            reason_codes.append(REASON_FEE_CROSS_CHECK_OK)
        else:
            fee_cross_check["status"] = "MISMATCH"
            reason_codes.append(REASON_FEE_CROSS_CHECK_MISMATCH)
    reason_codes.append(REASON_WALLET_BALANCED if balanced else REASON_WALLET_UNBALANCED)

    return SessionReconciliation(
        opening_wallet_usdt=opening,
        closing_wallet_usdt=closing,
        components=components,
        unexplained_usdt=unexplained,
        balanced=balanced,
        converted_usdt=sum(
            (value for key, value in components.items()
             if key in {"funding_attributed_usdt", "funding_unattributed_usdt"}),
            Decimal(0),
        ),
        unconverted=tuple(fee_unconverted) + tuple(income_unconverted),
        fee_cross_check=fee_cross_check,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )


def assert_no_double_count(
    components: Mapping[str, Any], income_events: Sequence[IncomeEvent]
) -> None:
    """禁止把 ``COMMISSION``/``REALIZED_PNL`` 收入行放进可加分量。"""
    additive = {
        str(key) for key in components
        if str(key) not in {
            "realized_trade_pnl_usdt", "net_fees_from_fills_usdt",
            "funding_attributed_usdt", "funding_unattributed_usdt",
            "transfers_usdt",
        }
    }
    for event in income_events:
        kind = canonical_income_type(event.income_type)
        if kind in FILL_OWNED_TYPES and kind.lower() in additive:
            raise DoubleCountError(
                f"{REASON_DOUBLE_COUNT}: {kind} tran_id={event.tran_id}"
            )


# --------------------------------------------------------------------------- #
# 迟到收入修订
# --------------------------------------------------------------------------- #


class LateIncomeLedger:
    """迟到收入的审计修订台账；同一修订 ID 不重复闭合。"""

    def __init__(self) -> None:
        self._applied: dict[str, OutcomeRevision] = {}

    @property
    def revisions(self) -> tuple[OutcomeRevision, ...]:
        return tuple(self._applied.values())

    def apply(
        self,
        event: IncomeEvent,
        closed_outcomes: Sequence[Mapping[str, Any]],
        *,
        lifecycle_id: str,
    ) -> tuple[OutcomeRevision, bool]:
        """返回 ``(revision, first_application)``；重复到达只回放不重复闭合。"""
        revision = apply_late_income(
            event, closed_outcomes, lifecycle_id=lifecycle_id,
            previous_revision_id=(self.revisions[-1].revision_id if self._applied else None),
        )
        existing = self._applied.get(revision.revision_id)
        if existing is not None:
            return existing, False
        self._applied[revision.revision_id] = revision
        return revision, True

    def requires_recalculation(self) -> bool:
        return any(item.requires_recalculation for item in self._applied.values())

    def closed_outcomes_created(self) -> int:
        return sum(1 for item in self._applied.values() if item.new_closed_outcome_created)


# --------------------------------------------------------------------------- #
# 持久化协议（DDL 与 SQL 由 I 应用到 src/audit/database.py）
# --------------------------------------------------------------------------- #

INCOME_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS income_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    income_type TEXT NOT NULL,
    tran_id TEXT NOT NULL,
    symbol TEXT,
    asset TEXT NOT NULL,
    amount TEXT NOT NULL,
    event_time_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    source_hash TEXT,
    content_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    raw_json TEXT,
    UNIQUE(environment, account_ref, income_type, tran_id)
)
"""

INCOME_COVERAGE_DDL = """
CREATE TABLE IF NOT EXISTS income_coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    income_type TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    watermark INTEGER NOT NULL,
    page_limit INTEGER NOT NULL,
    coverage_status TEXT NOT NULL,
    gaps_json TEXT,
    termination_proof_json TEXT,
    synced_at_ms INTEGER NOT NULL,
    UNIQUE(environment, account_ref, income_type, start_ms, end_ms)
)
"""


class IncomeStore(Protocol):
    """``AuditDatabase`` 需实现的最小收入存储面（分页结果与水位同事务提交）。"""

    def insert_income_events(self, rows: Iterable[Mapping[str, Any]]) -> int: ...

    def fetch_income_events(
        self, *, environment: str, account_ref: str
    ) -> list[Mapping[str, Any]]: ...

    def upsert_income_coverage(self, row: Mapping[str, Any]) -> None: ...

    def fetch_income_coverage(
        self, *, environment: str, account_ref: str, income_type: str
    ) -> Mapping[str, Any] | None: ...


@dataclass
class IncomeReconciliationBook:
    """把窗口同步结果写入存储；写入与水位同事务，重复拉取不重复入账。"""

    store: IncomeStore
    environment: str
    account_ref: str
    applied_revisions: LateIncomeLedger = field(default_factory=LateIncomeLedger)

    def persist(self, sync: WindowSync) -> int:
        """写入本窗口事件与覆盖证据；返回实际新增行数。"""
        rows = [
            {
                "environment": event.environment,
                "account_ref": event.account_ref,
                "income_type": event.income_type,
                "tran_id": event.tran_id,
                "symbol": event.symbol,
                "asset": event.asset,
                "amount": decimal_text(Decimal(event.amount)),
                "event_time_ms": event.event_time_ms,
                "received_at_ms": event.received_at_ms,
                "source_hash": event.source_hash,
            }
            for event in sync.events
        ]
        inserted = self.store.insert_income_events(rows) if rows else 0
        coverage = sync.coverage
        self.store.upsert_income_coverage({
            "environment": str(self.environment).lower(),
            "account_ref": str(self.account_ref),
            "income_type": coverage.income_type,
            "start_ms": coverage.start_ms,
            "end_ms": coverage.end_ms,
            "watermark": coverage.watermark,
            "page_limit": coverage.page_limit,
            "coverage_status": coverage.coverage_status,
            "gaps": [gap.model_dump() for gap in coverage.gaps],
            "termination_proof": (
                coverage.termination_proof.model_dump()
                if coverage.termination_proof is not None else None
            ),
            "synced_at_ms": coverage.synced_at_ms,
        })
        return inserted

    def load_existing(self) -> dict[str, str]:
        """已入库事件的幂等键 -> 内容哈希，供重复拉取判定 duplicates/隔离。"""
        known: dict[str, str] = {}
        for row in self.store.fetch_income_events(
            environment=str(self.environment).lower(), account_ref=str(self.account_ref)
        ):
            event = IncomeEvent.model_validate(dict(row))
            known[idempotency_key_text(event)] = str(row.get("content_hash") or "")
        return known


# --------------------------------------------------------------------------- #
# 幂等入库 (environment, account_ref, income_type, tran_id)
# --------------------------------------------------------------------------- #


def idempotency_keys(events: Sequence[IncomeEvent]) -> tuple[tuple[str, str, str, str], ...]:
    """显式暴露幂等主键四元组，便于审计与跨库比对。"""
    return tuple(
        (event.environment.strip().casefold(), event.account_ref.strip().casefold(),
         event.income_type.strip().casefold(), event.tran_id.strip().casefold())
        for event in events
    )


def dedupe_key_texts(events: Sequence[IncomeEvent]) -> tuple[str, ...]:
    return tuple(idempotency_key_text(event) for event in events)


def assert_no_silent_zero(events: Sequence[IncomeEvent]) -> None:
    """显式拒绝：把缺失收入行当作 0 金额事件入库。"""
    for event in events:
        if event.amount is None:
            raise InvalidIncomeRow(f"{REASON_INVALID_ROW}: amount missing for {event.tran_id}")
