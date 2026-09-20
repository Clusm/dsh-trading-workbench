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

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Protocol

from core.contracts import (
    AllocationError,
    AttributionResult,
    ConservationViolation,
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
    income_content_hash,
    ingest_income,
    normalize_key_part,
    plan_next_page,
    zero_fee_proof,
)

#: ``QuarantineRecord`` 未在 ``core.contracts`` 的 ``__init__`` 再导出（该文件属契约侧、
#: 本批不在作者独占范围内），因此从 K03 子模块直接引用，不复制其定义。
from core.contracts.k03_income import QuarantineRecord  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量与 reason code
# --------------------------------------------------------------------------- #

INCOME_TYPE_FUNDING_FEE = "FUNDING_FEE"
INCOME_TYPE_COMMISSION = "COMMISSION"
INCOME_TYPE_REALIZED_PNL = "REALIZED_PNL"
INCOME_TYPE_TRANSFER = "TRANSFER"
INCOME_TYPE_INSURANCE_CLEAR = "INSURANCE_CLEAR"

#: 已治理的收入类型枚举（P1-8）：窗口声明的类型必须落在其中，未知类型一律结构化拒绝，
#: 不允许把 `FUNDINGFEE` 这类拼写变体当成独立收入族入库并建立独立覆盖率。
KNOWN_INCOME_TYPES = frozenset({
    INCOME_TYPE_FUNDING_FEE,
    INCOME_TYPE_COMMISSION,
    INCOME_TYPE_REALIZED_PNL,
    INCOME_TYPE_TRANSFER,
    INCOME_TYPE_INSURANCE_CLEAR,
})

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
REASON_FEE_CROSS_CHECK_UNAVAILABLE = "FILL_INCOME_FEE_NOT_COMPARABLE"
REASON_REALIZED_CROSS_CHECK_MISMATCH = "FILL_INCOME_REALIZED_PNL_MISMATCH"
REASON_REALIZED_CROSS_CHECK_OK = "FILL_INCOME_REALIZED_PNL_MATCH"
REASON_DOUBLE_COUNT = "INCOME_FEE_DOUBLE_COUNT"
REASON_WALLET_UNBALANCED = "WALLET_IDENTITY_UNBALANCED"
REASON_WALLET_BALANCED = "WALLET_IDENTITY_BALANCED"
REASON_LATE_REVISION_DUPLICATE = "LATE_REVISION_ALREADY_APPLIED"
REASON_LATE_REVISION_CONTENT_CHANGED = "LATE_REVISION_CONTENT_CHANGED"
#: 摄入行落在窗口之外（`[start,end)` 半开语义）：不得静默收下，否则相邻窗口双计。
REASON_ROWS_OUTSIDE_WINDOW = "ROWS_OUTSIDE_WINDOW"
#: 跨页页序回退：后一页的最大事件时间早于前一页 —— 页间可能丢行。
REASON_CROSS_PAGE_NOT_MONOTONIC = "CROSS_PAGE_NOT_MONOTONIC"
#: 重叠复读不稳定：同一 page 两次读取返回不同内容 —— 官方 page 翻页在该时刻不可信。
REASON_PAGE_WINDOW_UNSTABLE = "PAGE_WINDOW_UNSTABLE"
#: 窗口声明的 income_type 不在已治理枚举内（P1-8）。
REASON_UNKNOWN_INCOME_TYPE = "UNKNOWN_INCOME_TYPE"
#: 返回行的 income_type 与窗口声明不一致（按类型查询不应出现）。
REASON_INCOME_TYPE_MISMATCH = "INCOME_TYPE_MISMATCH"
#: 跨 profile/环境域写入：coverage 与事件必须落在同一环境域（P1-1）。
REASON_PROFILE_MISMATCH = "PROFILE_MISMATCH"
#: 非空窗口缺归因决策：不是「零费用不可证明」（P2-7）。
REASON_ATTRIBUTION_MISSING = "NO_ATTRIBUTION_DECISIONS"
#: 极小金额 + 多腿时分摊量化余数与源金额符号矛盾（P2-3）。
REASON_ALLOCATION_QUANTUM_CONFLICT = "ALLOCATION_QUANTUM_CONFLICT"

FX_TOLERANCE = Decimal("0.00000001")
MONEY_QUANTUM = Decimal("0.00000001")


class InvalidIncomeRow(ValueError):
    """收入行缺少真值所需字段；调用方必须整体拒绝而不是静默跳过。"""


class DoubleCountError(ValueError):
    """同一笔手续费/已实现盈亏同时经 fill 与 income 两条口径计入。"""


class WalletIdentityError(ValueError):
    """会话勾稽输入自相矛盾（例如同名字段被重复提供）。"""


class ProfileMismatchError(ValueError):
    """coverage 与事件的 environment/account_ref 不一致：拒绝写入，避免跨 profile 覆盖。"""


class LateIncomeContentChanged(ValueError):
    """同一 tran_id 的迟到收入内容发生变化；契约侧 revision_id 不含金额，必须显式报出。"""


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
    """income_type 规范化：去空白 + 大写；位置参数缺失时返回空串。

    本函数**只做规范化**，不做枚举判定（保持纯函数语义）；枚举治理由
    :func:`is_known_income_type` 与 :func:`assert_known_income_type` 承担。
    """
    return str(value or "").strip().upper()


def is_known_income_type(value: Any) -> bool:
    """是否落在已治理的收入类型枚举内（P1-8）。"""
    return canonical_income_type(value) in KNOWN_INCOME_TYPES


def assert_known_income_type(value: Any) -> str:
    """窗口声明类型必须已在枚举内；未知类型（如 ``FUNDINGFEE``）结构化拒绝。"""
    normalized = canonical_income_type(value)
    if normalized not in KNOWN_INCOME_TYPES:
        raise InvalidIncomeRow(
            f"{REASON_UNKNOWN_INCOME_TYPE}: {normalized!r} not in "
            f"{sorted(KNOWN_INCOME_TYPES)}"
        )
    return normalized


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
    #: 本窗口运行时的环境域/账户域（coverage 与事件必须落在同一域，P1-1）。
    environment: str = ""
    account_ref: str = ""
    #: 被隔离的传入事件本体（与 ``quarantine`` 逐条对应），供落库审计（P1-7）。
    quarantine_events: tuple[IncomeEvent, ...] = ()

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
    overlap_recheck: bool = True,
) -> WindowSync:
    """按官方 ``page``/``limit`` 翻页读完一个窗口，并给出结构化终止证据。

    ``fetch_page(page_no)`` 由适配层提供，返回该页原始行（不得自行排序/去重）。
    短页即终止；满页必须页内非降序，且必须出现新 tran_id 才算前进——重复返回
    同一页一律判 ``PAGINATION_STALLED``，不允许死循环，也不允许用
    ``last_time + 1`` 跳过同毫秒记录。

    ``existing`` 可传 :meth:`IncomeReconciliationBook.load_existing` 的
    ``幂等键 -> content_hash`` 映射（**原样透传**给 K03 ``ingest_income``），
    也可传 ``IncomeEvent`` 序列；两种入参都不得被当成事件本体。
    跨页额外防线：页序回退（后一页最大时间早于前一页）与 ``overlap_recheck``
    重叠复读（同一 page 两次读取必须逐行一致）任一不成立即 ``GAP``，不宣称零丢失。
    """
    start, end = int(start_ms), int(end_ms)
    if end <= start:
        raise InvalidIncomeRow(REASON_INVALID_WINDOW)
    if isinstance(page_limit, bool) or not isinstance(page_limit, int) or not (1 <= page_limit <= OFFICIAL_PAGE_LIMIT_MAX):
        raise InvalidIncomeRow(REASON_INVALID_PAGE_LIMIT)
    income_type = assert_known_income_type(income_type)
    env = str(environment or "unknown").lower()
    account = str(account_ref or "unknown")

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
            environment=env, account_ref=account,
        )

    # 已知集：``load_existing()`` 返回 ``{幂等键文本: content_hash}``，必须原样透传给
    # ``ingest_income``（它按 Mapping 语义解释），**绝不能取 values() 当事件**。
    known: dict[Any, Any] = {}
    if isinstance(existing, Mapping):
        known.update(existing)
    elif existing is not None:
        for item in existing:
            existing_event = _as_income_event(item)
            known[idempotency_key_text(existing_event)] = income_content_hash(existing_event)

    # 事件集按幂等键唯一：跨页重复行只保留一份，否则窗口金额会被重复求和。
    by_key: dict[str, IncomeEvent] = {}
    inserted: list[str] = []
    duplicates: list[str] = []
    quarantined: list[QuarantineRecord] = []
    reason_codes: list[str] = []
    gaps: list[RangeGap] = []
    pages = 0
    terminated = False
    last_page_size = 0
    seen_page_ids: set[frozenset[str]] = set()
    prev_page_max_ms: int | None = None
    unknown_types: set[str] = set()
    mismatched_types: set[str] = set()
    escaped_rows = 0

    for page_no in range(1, max_pages + 1):
        rows = list(fetch_page(page_no))
        pages = page_no
        last_page_size = len(rows)
        page_events: list[IncomeEvent] = []
        for row in rows:
            event = normalize_income_row(
                row, environment=env, account_ref=account,
                received_at_ms=received_at_ms,
                source_hash=str(row.get("source_hash") or ""),
            )
            row_type = canonical_income_type(event.income_type)
            if row_type not in KNOWN_INCOME_TYPES:
                unknown_types.add(row_type)
                continue
            if row_type != income_type:
                mismatched_types.add(row_type)
                continue
            if not window_contains(start, end, event.event_time_ms):
                # 半开区间 [start,end)：越窗行属于相邻窗口，收下即双计。
                escaped_rows += 1
                continue
            page_events.append(event)
        page_ids = frozenset(idempotency_key_text(item) for item in page_events)
        page_times = [item.event_time_ms for item in page_events]
        page_max_ms = max(page_times) if page_times else None
        if (
            prev_page_max_ms is not None
            and page_max_ms is not None
            and page_max_ms < prev_page_max_ms
        ):
            # 页序回退：后一页的最大时间早于前一页 ⇒ 页间存在未读区间，宁可 GAP。
            reason_codes.append(REASON_CROSS_PAGE_NOT_MONOTONIC)
            gaps.append(RangeGap(
                start_ms=start, end_ms=end, reason=REASON_CROSS_PAGE_NOT_MONOTONIC))
            break
        result = ingest_income(page_events, known)
        inserted.extend(result.inserted)
        duplicates.extend(result.duplicates)
        quarantined.extend(result.quarantined)
        for item in page_events:
            by_key.setdefault(idempotency_key_text(item), item)
        digests = {
            idempotency_key_text(item): income_content_hash(item) for item in page_events
        }
        for key in result.inserted:
            known[key] = digests[key]

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
        if overlap_recheck and decision.overlap_recheck:
            # 重叠复读：同一 page 再读一次必须逐行一致。官方 page 翻页在并发写入下
            # 不保证稳定，读不一致说明该时刻的页视图不可信 → GAP（不得静默丢行）。
            recheck_rows = list(fetch_page(page_no))
            if recheck_rows != rows:
                reason_codes.append(REASON_PAGE_WINDOW_UNSTABLE)
                gaps.append(RangeGap(
                    start_ms=start, end_ms=end, reason=REASON_PAGE_WINDOW_UNSTABLE))
                break
        if page_max_ms is not None:
            prev_page_max_ms = page_max_ms
        seen_page_ids.add(page_ids)
    else:
        reason_codes.append(REASON_PAGE_CAP_EXCEEDED)
        gaps.append(RangeGap(start_ms=start, end_ms=end, reason=REASON_PAGE_CAP_EXCEEDED))

    if escaped_rows:
        # 越窗行显式回带：既不静默丢弃也不收进本窗口。
        reason_codes.append(REASON_ROWS_OUTSIDE_WINDOW)
    if unknown_types:
        reason_codes.append(REASON_UNKNOWN_INCOME_TYPE)
    if mismatched_types:
        reason_codes.append(REASON_INCOME_TYPE_MISMATCH)

    conflicted_events: list[IncomeEvent] = []
    if quarantined:
        reason_codes.append(REASON_ID_CONFLICT)
        # 冲突键的金额真值已不可确定：该 tran_id 必须整体退出求和，不能保留先到达的
        # 那一版（否则等于按到达顺序猜金额）。
        conflicted = {record.key for record in quarantined}
        conflicted_events = [
            item for key, item in by_key.items() if key in conflicted
        ]
        for key in conflicted:
            by_key.pop(key, None)
        inserted = [key for key in inserted if key not in conflicted]

    all_events = list(by_key.values())
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
        environment=env, account_ref=account,
        quarantine_events=tuple(conflicted_events),
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
    if sync.events and attribution is None:
        # 非空窗口缺归因决策：缺的是归因，不是零费用证明（P2-7 的 reason 误导）。
        return CostFinalization(
            lifecycle_id=lifecycle_id,
            cost_status="INCOMPLETE",
            coverage_status=sync.coverage.coverage_status,
            attribution_status="UNKNOWN",
            reason_codes=tuple(dict.fromkeys(
                tuple(coverage_problems(sync.coverage)) + (REASON_ATTRIBUTION_MISSING,)
            )),
        )
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
    """按事件拆分资金费：已归因 / 未归因 / 未知，三者互不混用。

    ``unconverted``：非 USDT 且无有来源汇率、**不进 USDT 口径**的事件，逐条回带
    （与 ``SessionReconciliation.unconverted`` 同口径），不再静默剔除。
    ``unknown_usdt is None`` 只表示「没有 UNKNOWN 状态的资金费事件」，
    不表示「没有未换算事件」——后者看 ``unconverted``。
    """

    attributed_usdt: Decimal
    unattributed_usdt: Decimal
    unknown_usdt: Decimal | None
    result: AttributionResult
    attributed_keys: tuple[str, ...]
    unattributed_keys: tuple[str, ...]
    unknown_keys: tuple[str, ...]
    unconverted: tuple[dict[str, Any], ...] = ()
    reason_codes: tuple[str, ...] = ()

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
    unconverted: list[dict[str, Any]] = []
    reasons: list[str] = []
    for event in funding:
        key = idempotency_key_text(event)
        amount = _usdt_amount(event, rates)
        if amount is None:
            # 不可换算：既不计入已归因也不计入未归因的 USDT 净值，但必须显式回带，
            # 否则金额被剔除却无任何可查询痕迹（P2-5）。
            unconverted.append({
                "event_id": key, "tran_id": event.tran_id,
                "income_type": canonical_income_type(event.income_type),
                "asset": event.asset,
                "native_amount": decimal_text(Decimal(event.amount)),
                "reason": REASON_UNCONVERTED_FX,
            })
            reasons.append(REASON_UNCONVERTED_FX)
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
        unconverted=tuple(unconverted),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def allocate_funding_to_lifecycles(
    attribution: IncomeAttribution,
    *,
    lifecycle_weights: Mapping[str, Any],
    version: str,
    evidence: str,
) -> tuple[CostAttribution, ...]:
    """把已归因资金费按明示权重分摊；Σ分摊 == 源金额（守恒）。

    极小金额 + 多腿时 K03 量化余数可能与源金额符号矛盾并抛 ``AllocationError``；
    本函数保持 fail-closed（抛异常），需要降级为「INCOMPLETE + reason code」的
    调用方请用 :func:`allocate_funding_or_incomplete`。
    """
    source = attribution.attributed_usdt
    allocations = allocate_weighted(
        source, lifecycle_weights,
        method="funding_weighted", version=version, evidence=evidence,
        event_id="FUNDING_AGGREGATE",
    )
    check_conservation(source, allocations)
    return allocations


@dataclass(frozen=True)
class FundingAllocation:
    """分摊结果或结构化降级；``complete=False`` 时成本链不得判 COMPLETE。"""

    allocations: tuple[CostAttribution, ...]
    complete: bool
    reason_codes: tuple[str, ...] = ()


def allocate_funding_or_incomplete(
    attribution: IncomeAttribution,
    *,
    lifecycle_weights: Mapping[str, Any],
    version: str,
    evidence: str,
) -> FundingAllocation:
    """分摊的降级入口：量化冲突/守恒失败折成 ``ALLOCATION_QUANTUM_CONFLICT``。

    单腿低至 1e-8 的资金费在真实账户并不罕见（5 个以上生命周期同时持仓即触发），
    整链中断不可接受；降级后由调用方给出 ``INCOMPLETE``，不得当作零成本。
    """
    try:
        return FundingAllocation(
            allocations=allocate_funding_to_lifecycles(
                attribution, lifecycle_weights=lifecycle_weights,
                version=version, evidence=evidence,
            ),
            complete=True,
        )
    except (AllocationError, ConservationViolation):
        return FundingAllocation(
            allocations=(), complete=False,
            reason_codes=(REASON_ALLOCATION_QUANTUM_CONFLICT,),
        )


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
    #: 被折算成 0 参与等式的未知分量键（P2-6）：等式成立但不可信时必须能看见它们。
    unknown_components: tuple[str, ...] = ()

    def identity_lhs(self) -> Decimal:
        return self.closing_wallet_usdt - self.opening_wallet_usdt

    def identity_rhs(self) -> Decimal:
        return sum(self.components.values(), Decimal(0)) + self.unexplained_usdt


def _validated_income_events(income_events: Sequence[Any]) -> list[IncomeEvent]:
    """入参校验：只接受 ``IncomeEvent`` 或可校验映射；其余结构化拒绝（P3-6）。

    原实现对 ``dict`` 入参会在下游抛 ``AttributeError``，与本模块「一律结构化拒绝」
    的口径不一致。
    """
    if isinstance(income_events, (str, bytes)):
        raise WalletIdentityError(
            "income_events must be a sequence of IncomeEvent, not text")
    validated: list[IncomeEvent] = []
    for item in income_events:
        if isinstance(item, IncomeEvent):
            validated.append(item)
            continue
        if not isinstance(item, Mapping):
            raise WalletIdentityError(
                f"income_events item must be IncomeEvent or mapping, "
                f"got {type(item).__name__}"
            )
        try:
            validated.append(IncomeEvent.model_validate(dict(item)))
        except Exception as exc:  # noqa: BLE001 - 结构化拒绝，不泄漏 pydantic 细节
            raise WalletIdentityError(
                f"{REASON_INVALID_ROW}: income_events item not a valid IncomeEvent: {exc}"
            ) from exc
    return validated


def _sum_commissions(
    fills: Iterable[Mapping[str, Any]], *, fx_rates: Mapping[str, str | Decimal]
) -> tuple[Decimal | None, tuple[dict[str, Any], ...]]:
    """fill 手续费合计（成本为负）；非 USDT 且无汇率时整体未知（None）。

    口径说明（P3-7）：对 fill ``commission`` 取 ``abs()`` 强制记为成本——币安
    userTrades 的 commission 恒为正数（成本），返佣走独立字段；若上游把返佣以
    负 commission 传入，本函数会将其符号翻转为成本。该约定已登记（known_limits）。
    """
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
    extra_components: Mapping[str, Any] | None = None,
) -> SessionReconciliation:
    """会话钱包勾稽；income 侧的手续费/已实现盈亏只交叉核对，不重复计入。

    ``net_fees_from_fills`` 是唯一费用入口：``COMMISSION`` 收入行与之核对
    （``FILL_INCOME_FEE_MATCH/MISMATCH``），``REALIZED_PNL`` 同样与 fill 侧
    已实现盈亏核对（``FILL_INCOME_REALIZED_PNL_MATCH/MISMATCH``）；二者都不会被
    再加进 ``realized_trade_pnl``。调用方可经 ``extra_components`` 追加已分类现金流，
    但含 ``commission``/``realized`` 的键会被 :func:`assert_no_double_count` 拒绝。

    ``balanced`` 只有在未解释差额为 0 **且** 没有任何阻断性 reason code
    （未知分量 / 交叉核对不一致）时才成立——与 ``fee_cross_check["status"]`` 不矛盾。
    """
    rates = dict(fx_rates or {})
    opening = Decimal(str(opening_wallet_usdt))
    closing = Decimal(str(closing_wallet_usdt))
    income_events = _validated_income_events(income_events)

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

    extras = {str(key): Decimal(str(value)) for key, value in (extra_components or {}).items()}
    # 重复计费守卫：调用方追加的分量键若含 commission/realized 一律拒绝。
    assert_no_double_count(extras, income_events)

    components: dict[str, Decimal] = {
        "realized_trade_pnl_usdt": realized if realized is not None else Decimal(0),
        "net_fees_from_fills_usdt": net_fees if net_fees is not None else Decimal(0),
        "funding_attributed_usdt": funding_attributed,
        "funding_unattributed_usdt": funding_unattributed,
        "transfers_usdt": Decimal(str(transfers_usdt)),
        "other_classified_usdt": Decimal(str(other_classified_usdt)),
    }
    components.update(extras)
    reason_codes: list[str] = []
    blocking_reasons: list[str] = []
    # 未知分量被折算成 0 会污染等式：显式登记，不让「未知」静默变成 0（P2-6）。
    unknown_components: list[str] = []
    if realized is None:
        unknown_components.append("realized_trade_pnl_usdt")
    if net_fees is None:
        unknown_components.append("net_fees_from_fills_usdt")
    if realized is None or net_fees is None or funding_unknown is not None:
        # 关键分量未知时不得输出“已平衡”结论。
        reason_codes.append(REASON_UNCONVERTED_FX)
        blocking_reasons.append(REASON_UNCONVERTED_FX)

    unexplained = (closing - opening) - sum(components.values(), Decimal(0))

    fee_cross_check: dict[str, Any] = {
        "fill_fees_usdt": net_fees,
        "income_commission_usdt": commissions_income,
        "income_realized_usdt": realized_income,
        "status": "UNKNOWN",
    }
    # COMMISSION 收入只与「非零手续费收入」比较：收入侧没有 COMMISSION 行时
    # 用 0 去比 fill 手续费会误报 MISMATCH（P1-6a）。
    if commissions_income != 0 and net_fees is not None:
        if abs(commissions_income - net_fees) <= FX_TOLERANCE:
            fee_cross_check["status"] = "MATCH"
            reason_codes.append(REASON_FEE_CROSS_CHECK_OK)
        else:
            fee_cross_check["status"] = "MISMATCH"
            reason_codes.append(REASON_FEE_CROSS_CHECK_MISMATCH)
            blocking_reasons.append(REASON_FEE_CROSS_CHECK_MISMATCH)
    elif commissions_income == 0 or net_fees is None:
        fee_cross_check["status"] = "NOT_COMPARABLE"
        if net_fees is not None:
            reason_codes.append(REASON_FEE_CROSS_CHECK_UNAVAILABLE)
    # REALIZED_PNL 同样核对（docstring 原本声称「同理」却从未比较，P1-6c）。
    fee_cross_check["realized_cross_check"] = "UNKNOWN"
    if realized_income != 0 and realized is not None:
        if abs(realized_income - realized) <= FX_TOLERANCE:
            fee_cross_check["realized_cross_check"] = "MATCH"
            reason_codes.append(REASON_REALIZED_CROSS_CHECK_OK)
        else:
            fee_cross_check["realized_cross_check"] = "MISMATCH"
            reason_codes.append(REASON_REALIZED_CROSS_CHECK_MISMATCH)
            blocking_reasons.append(REASON_REALIZED_CROSS_CHECK_MISMATCH)
    elif realized_income == 0 or realized is None:
        fee_cross_check["realized_cross_check"] = "NOT_COMPARABLE"

    # balanced 必须在交叉核对之后计算，且只由「阻断性」reason 否定（P1-6b）。
    balanced = unexplained == 0 and not blocking_reasons
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
        unknown_components=tuple(unknown_components),
    )


#: 分量键名中的「已由 fill 计入」标记：白名单式反向判定，不再只拦字面 `commission`。
_FILL_OWNED_KEY_MARKERS = {
    INCOME_TYPE_COMMISSION: "commission",
    INCOME_TYPE_REALIZED_PNL: "realized",
}


def assert_no_double_count(
    components: Mapping[str, Any], income_events: Sequence[IncomeEvent]
) -> None:
    """禁止把 ``COMMISSION``/``REALIZED_PNL`` 收入行放进可加分量。

    判定为**反向白名单**：只要分量键名含 ``commission`` / ``realized``（大小写不敏感），
    且存在对应类型的收入事件，即结构化拒绝。``commission_income_usdt``、
    ``income_fees``、``net_realized_pnl_usdt`` 这类真实字段名不再被放行（P1-5）。
    """
    keys = [str(key) for key in components]
    for event in income_events:
        kind = canonical_income_type(event.income_type)
        marker = _FILL_OWNED_KEY_MARKERS.get(kind)
        if marker is None:
            continue
        for key in keys:
            if marker in key.lower():
                raise DoubleCountError(
                    f"{REASON_DOUBLE_COUNT}: {kind} tran_id={event.tran_id} "
                    f"component_key={key}"
                )


# --------------------------------------------------------------------------- #
# 迟到收入修订
# --------------------------------------------------------------------------- #


class LateIncomeLedger:
    """迟到收入的审计修订台账；同一修订 ID 不重复闭合。

    契约侧 ``revision_id`` 只哈希 ``(outcome_id, source_event_id, closed_at_ms)``，
    ``source_event_id`` 又不含金额：同 ``tran_id`` 改额会得到**同一个** revision_id。
    本台账因此自行比对内容指纹，把「同内容重放」与「同键内容变更」分开：
    前者回带 ``LATE_REVISION_ALREADY_APPLIED``，后者抛
    :class:`LateIncomeContentChanged`（结构化，不静默吞掉）。把「内容变更」升格为
    新修订需要 K03 新增 reason code（属 I 侧契约小版本演进），本批不越权改契约。
    """

    def __init__(self) -> None:
        self._applied: dict[str, OutcomeRevision] = {}
        self._content: dict[str, str] = {}
        self._duplicates = 0

    @property
    def revisions(self) -> tuple[OutcomeRevision, ...]:
        return tuple(self._applied.values())

    @property
    def duplicate_reason_codes(self) -> tuple[str, ...]:
        """重复投递次数对应的 reason code 列表（供审计聚合）。"""
        return (REASON_LATE_REVISION_DUPLICATE,) * self._duplicates

    def apply(
        self,
        event: IncomeEvent,
        closed_outcomes: Sequence[Mapping[str, Any]],
        *,
        lifecycle_id: str,
    ) -> tuple[OutcomeRevision, bool]:
        """返回 ``(revision, first_application)``；重复到达只回放不重复闭合。"""
        event_model = _as_income_event(event)
        key = idempotency_key_text(event_model)
        digest = income_content_hash(event_model)
        previous_digest = self._content.get(key)
        if previous_digest is not None and previous_digest != digest:
            raise LateIncomeContentChanged(
                f"{REASON_LATE_REVISION_CONTENT_CHANGED}: tran_id={event_model.tran_id} "
                f"previous={previous_digest[:12]} incoming={digest[:12]}"
            )
        revision = apply_late_income(
            event_model, closed_outcomes, lifecycle_id=lifecycle_id,
            previous_revision_id=(self.revisions[-1].revision_id if self._applied else None),
        )
        existing = self._applied.get(revision.revision_id)
        if existing is not None:
            self._duplicates += 1
            return existing, False
        self._applied[revision.revision_id] = revision
        self._content[key] = digest
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

INCOME_CONFLICTS_DDL = """
CREATE TABLE IF NOT EXISTS income_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    income_type TEXT NOT NULL,
    tran_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    existing_content_hash TEXT NOT NULL,
    incoming_content_hash TEXT NOT NULL,
    amount TEXT,
    asset TEXT,
    event_time_ms INTEGER,
    observed_at_ms INTEGER NOT NULL,
    UNIQUE(environment, account_ref, income_type, tran_id, incoming_content_hash)
)
"""


class IncomeStore(Protocol):
    """``AuditDatabase`` 需实现的最小收入存储面。

    事务语义（与实现一致，P2-4）：事件与覆盖**分别提交**，顺序为先事件后覆盖
    （fail-closed：覆盖未落库时宁可留下事件也不谎报覆盖）。真正的单事务提交未实现，
    已登记为限制。
    """

    def insert_income_events(self, rows: Iterable[Mapping[str, Any]]) -> int: ...

    def fetch_income_events(
        self, *, environment: str, account_ref: str, income_type: str | None = None
    ) -> list[Mapping[str, Any]]: ...

    def upsert_income_coverage(self, row: Mapping[str, Any]) -> None: ...

    def fetch_income_coverage(
        self, *, environment: str, account_ref: str, income_type: str,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> Mapping[str, Any] | None: ...

    def fetch_income_coverage_range(
        self, *, environment: str, account_ref: str, income_type: str,
        start_ms: int, end_ms: int,
    ) -> list[Mapping[str, Any]]: ...

    def insert_income_conflicts(self, rows: Iterable[Mapping[str, Any]]) -> int: ...

    def fetch_income_conflicts(
        self, *, environment: str, account_ref: str
    ) -> list[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class CoverageAggregate:
    """区间内多窗口覆盖聚合（P1-2）：只有整段被 COMPLETE 窗口无缝覆盖才算已验证。

    ``missing`` 列出区间内**没有任何覆盖证据**的子区间——消费者不得把它们当成
    「已验证为零」；``incomplete`` 列出有证据但非 COMPLETE 的窗口。
    """

    complete: bool
    covered: tuple[tuple[int, int], ...] = ()
    missing: tuple[tuple[int, int], ...] = ()
    incomplete: tuple[tuple[int, int], ...] = ()
    reason_codes: tuple[str, ...] = ()

    def covers(self, start_ms: int, end_ms: int) -> bool:
        """区间是否已被 COMPLETE 证据完整覆盖（缺失即 False，绝不当零）。"""
        cursor = int(start_ms)
        for item_start, item_end in self.covered:
            if item_end <= cursor:
                continue
            if item_start > cursor:
                return False
            cursor = max(cursor, item_end)
            if cursor >= int(end_ms):
                return True
        return cursor >= int(end_ms)


def income_coverage_from_row(row: Mapping[str, Any]) -> IncomeCoverage:
    """存储行 → :class:`IncomeCoverage`（解析 JSON 列，绝不把缺失折成零）。"""
    payload = dict(row)
    gaps = payload.get("gaps_json")
    if isinstance(gaps, (str, bytes)):
        gaps = json.loads(gaps or "[]")
    proof = payload.get("termination_proof_json")
    if isinstance(proof, (str, bytes)):
        proof = json.loads(proof) if proof not in ("", "null") else None
    payload["gaps"] = tuple(gaps or ())
    payload["termination_proof"] = proof
    payload.pop("gaps_json", None)
    payload.pop("termination_proof_json", None)
    payload.pop("id", None)
    return IncomeCoverage.model_validate(payload)


def aggregate_coverage(
    rows: Sequence[Mapping[str, Any]],
    *,
    start_ms: int,
    end_ms: int,
) -> CoverageAggregate:
    """把区间内多个窗口的覆盖证据聚合成一个可判定对象。

    「任一窗口缺失」与「窗口非 COMPLETE」都使 ``complete=False``；
    调用方据此得出结论「该区间未知」，而不是「该区间为零」。
    """
    start, end = int(start_ms), int(end_ms)
    if end <= start:
        raise InvalidIncomeRow(REASON_INVALID_WINDOW)
    covered: list[tuple[int, int]] = []
    incomplete: list[tuple[int, int]] = []
    for raw in rows:
        window_start = int(raw.get("start_ms", 0))
        window_end = int(raw.get("end_ms", 0))
        if window_end <= window_start:
            incomplete.append((window_start, window_end))
            continue
        if window_end <= start or window_start >= end:
            continue
        clipped = (max(window_start, start), min(window_end, end))
        if str(raw.get("coverage_status") or "") == "COMPLETE":
            covered.append(clipped)
        else:
            incomplete.append(clipped)
    covered.sort()
    missing: list[tuple[int, int]] = []
    cursor = start
    for item_start, item_end in covered:
        if item_end <= cursor:
            continue
        if item_start > cursor:
            missing.append((cursor, item_start))
        cursor = max(cursor, item_end)
    if cursor < end:
        missing.append((cursor, end))
    reasons: list[str] = []
    if missing:
        reasons.append(REASON_NO_COVERAGE)
    if incomplete:
        reasons.append(REASON_COVERAGE_NOT_COMPLETE)
    return CoverageAggregate(
        complete=not missing and not incomplete,
        covered=tuple(covered),
        missing=tuple(missing),
        incomplete=tuple(incomplete),
        reason_codes=tuple(reasons),
    )


@dataclass(frozen=True)
class PersistReport:
    """一次持久化的结果：新增行数 + 被隔离（丢弃）的冲突清单。

    ``conflicts`` 非空时窗口金额不可信，调用方必须据此进入 INCOMPLETE；
    冲突同时已落 ``income_conflicts`` 表，进程重启后仍可审计（P1-7）。
    """

    inserted: int
    conflicts: tuple[QuarantineRecord, ...] = ()
    environment: str = ""
    account_ref: str = ""


@dataclass
class IncomeReconciliationBook:
    """把窗口同步结果写入存储；重复拉取不重复入账。

    事务语义（P2-4）：事件与覆盖**分别提交**（先事件、后覆盖），并非单事务；
    该差异已登记，本类不再声称「写入与水位同事务」。
    环境域（P1-1）：coverage 与事件一律使用同一个、经校验一致的环境域/账户域，
    ``sync`` 与 book 不一致时抛 :class:`ProfileMismatchError`，杜绝跨 profile 覆盖。
    """

    store: IncomeStore
    environment: str
    account_ref: str
    applied_revisions: LateIncomeLedger = field(default_factory=LateIncomeLedger)

    def _bound_domain(self, sync: WindowSync) -> tuple[str, str]:
        """校验并回带本窗口唯一的环境域/账户域。"""
        book_env = normalize_key_part(self.environment)
        book_account = normalize_key_part(self.account_ref)
        sync_env = normalize_key_part(sync.environment) if sync.environment else book_env
        sync_account = (
            normalize_key_part(sync.account_ref) if sync.account_ref else book_account
        )
        if sync_env != book_env or sync_account != book_account:
            raise ProfileMismatchError(
                f"{REASON_PROFILE_MISMATCH}: sync=({sync_env!r},{sync_account!r}) "
                f"book=({book_env!r},{book_account!r})"
            )
        for event in sync.events:
            if (normalize_key_part(event.environment) != sync_env
                    or normalize_key_part(event.account_ref) != sync_account):
                raise ProfileMismatchError(
                    f"{REASON_PROFILE_MISMATCH}: event tran_id={event.tran_id} "
                    f"domain=({event.environment!r},{event.account_ref!r}) "
                    f"window=({sync_env!r},{sync_account!r})"
                )
        return sync_env, sync_account

    def persist(self, sync: WindowSync) -> PersistReport:
        """写入本窗口事件、覆盖证据与冲突审计；返回 :class:`PersistReport`。"""
        env, account = self._bound_domain(sync)
        rows = [
            {
                "environment": env,
                "account_ref": account,
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
            "environment": env,
            "account_ref": account,
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
        conflicts = tuple(sync.quarantine)
        if conflicts:
            self.store.insert_income_conflicts([
                {
                    "environment": env,
                    "account_ref": account,
                    "income_type": event.income_type,
                    "tran_id": event.tran_id,
                    "idempotency_key": record.key,
                    "reason_code": record.reason_code,
                    "existing_content_hash": record.existing_content_hash,
                    "incoming_content_hash": record.incoming_content_hash,
                    "amount": decimal_text(Decimal(event.amount)),
                    "asset": event.asset,
                    "event_time_ms": event.event_time_ms,
                    "observed_at_ms": int(coverage.synced_at_ms),
                }
                for record, event in zip(conflicts, sync.quarantine_events)
            ])
        return PersistReport(
            inserted=inserted, conflicts=conflicts,
            environment=env, account_ref=account,
        )

    def load_existing(self) -> dict[str, str]:
        """已入库事件的幂等键 -> 内容哈希，供重复拉取判定 duplicates/隔离。"""
        known: dict[str, str] = {}
        for row in self.store.fetch_income_events(
            environment=normalize_key_part(self.environment),
            account_ref=normalize_key_part(self.account_ref),
        ):
            record = dict(row)
            known[idempotency_key_text(_event_from_storage_row(record))] = str(
                record.get("content_hash") or ""
            )
        return known

    def coverage_for_window(
        self, *, income_type: str, start_ms: int, end_ms: int
    ) -> IncomeCoverage | None:
        """按窗口读取覆盖证据；不存在返回 ``None``（缺失绝不当零，P1-2）。"""
        row = self.store.fetch_income_coverage(
            environment=normalize_key_part(self.environment),
            account_ref=normalize_key_part(self.account_ref),
            income_type=assert_known_income_type(income_type),
            start_ms=int(start_ms), end_ms=int(end_ms),
        )
        return income_coverage_from_row(row) if row is not None else None

    def aggregate_coverage(self, *, income_type: str, start_ms: int, end_ms: int) -> CoverageAggregate:
        """按区间聚合覆盖证据；任一窗口缺失/未完成 ⇒ 该区间未知。"""
        rows = self.store.fetch_income_coverage_range(
            environment=normalize_key_part(self.environment),
            account_ref=normalize_key_part(self.account_ref),
            income_type=assert_known_income_type(income_type),
            start_ms=int(start_ms), end_ms=int(end_ms),
        )
        return aggregate_coverage(rows, start_ms=int(start_ms), end_ms=int(end_ms))


# --------------------------------------------------------------------------- #
# 幂等入库 (environment, account_ref, income_type, tran_id)
# --------------------------------------------------------------------------- #


#: IncomeEvent 的输入字段；存储行会多带 content_hash/status 等审计列，必须剔除。
_EVENT_STORAGE_FIELDS = (
    "environment", "account_ref", "income_type", "tran_id", "symbol", "asset",
    "amount", "event_time_ms", "received_at_ms", "source_hash",
)


def _as_income_event(raw: Any) -> IncomeEvent:
    """``IncomeEvent`` 或原始映射 → ``IncomeEvent``；只做契约校验。"""
    if isinstance(raw, IncomeEvent):
        return raw
    return IncomeEvent.model_validate(dict(raw) if isinstance(raw, Mapping) else raw)


def _event_from_storage_row(row: Mapping[str, Any]) -> IncomeEvent:
    """存储行 → IncomeEvent；只取契约字段（IncomeEvent 是 extra='forbid'）。"""
    if isinstance(row, IncomeEvent):
        return row
    payload = {name: row[name] for name in _EVENT_STORAGE_FIELDS if name in row}
    payload.setdefault("source_hash", "")
    return IncomeEvent.model_validate(payload)


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
