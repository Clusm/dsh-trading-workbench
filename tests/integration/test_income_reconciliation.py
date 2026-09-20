"""U01 资金费流水与成本闭环的离线集成测试（可直接运行，无需 pytest）。

运行：``python tests/integration/test_income_reconciliation.py``
期望值全部来自 ``tests/fixtures/income/f10_window_sync.json`` 的 ``derived_golden``，
该文件由 K03 规格与数学守恒独立推导，不回放被测实现输出。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.contracts import (  # noqa: E402
    AllocationError,
    AttributionResult,
    IncomeEvent,
    idempotency_key_text,
    income_content_hash,
)
from exchange.income_reconciliation import (  # noqa: E402
    DEFAULT_PAGE_LIMIT,
    KNOWN_INCOME_TYPES,
    REASON_ALLOCATION_QUANTUM_CONFLICT,
    REASON_ATTRIBUTION_MISSING,
    REASON_CROSS_PAGE_NOT_MONOTONIC,
    REASON_PAGE_NOT_SORTED,
    REASON_PAGE_WINDOW_UNSTABLE,
    REASON_PAGINATION_STALLED,
    REASON_ROWS_OUTSIDE_WINDOW,
    REASON_UNKNOWN_INCOME_TYPE,
    InvalidIncomeRow,
    IncomeAttribution,
    LateIncomeContentChanged,
    LateIncomeLedger,
    IncomeReconciliationBook,
    ProfileMismatchError,
    WalletIdentityError,
    _event_from_storage_row,
    aggregate_coverage,
    allocate_funding_or_incomplete,
    allocate_funding_to_lifecycles,
    assert_no_double_count,
    attribute_funding,
    bounds_from_inclusive,
    build_income_query,
    canonical_income_type,
    dedupe_key_texts,
    idempotency_keys,
    income_coverage_from_row,
    inclusive_bounds,
    is_known_income_type,
    normalize_income_row,
    normalize_key_part,
    reconcile_session,
    sync_income_window,
    window_contains,
    window_cost_finalization,
    window_zero_fee_proof,
)

FIXTURE = ROOT / "tests" / "fixtures" / "income" / "f10_window_sync.json"
FAILURES: list[str] = []
PASS_COUNT = 0


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL  {name}: {detail}")


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _page_fetcher(pages: list[list[dict]]):
    def fetch(page_no: int) -> list[dict]:
        index = page_no - 1
        if index >= len(pages):
            return []
        return list(pages[index])
    return fetch


def _events(rows: list[dict], *, received_at_ms: int) -> list[IncomeEvent]:
    return [
        normalize_income_row(
            row, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
            received_at_ms=received_at_ms,
        )
        for row in rows
    ]


def _sync_window(window: dict, *, received_at_ms: int = 1789870000000, existing=None):
    return sync_income_window(
        _page_fetcher(window["pages"]),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type=window["income_type"],
        start_ms=window["start_ms"], end_ms=window["end_ms"],
        page_limit=2, received_at_ms=received_at_ms,
        existing=existing,
        source_available=window.get("source_available", True),
        source_error=window.get("source_error"),
    )


class MemoryIncomeStore:
    """最小存储替身：验证幂等入库/覆盖/隔离落库行为（不碰真实数据库）。

    键归一化与真实存储层保持同一口径：``normalize_key_part``（strip + casefold），
    否则 DB 唯一约束与 K03 幂等键不等价（P1-8）。
    """

    def __init__(self) -> None:
        self.events: dict[tuple, dict] = {}
        self.coverage: dict[tuple, dict] = {}
        self.conflicts: list[dict] = []

    def insert_income_events(self, rows) -> int:
        inserted = 0
        for row in rows:
            key = self._event_key(row)
            if key in self.events:
                continue
            self.events[key] = dict(row)
            inserted += 1
        return inserted

    @staticmethod
    def _event_key(row) -> tuple:
        return (
            normalize_key_part(row["environment"]), normalize_key_part(row["account_ref"]),
            normalize_key_part(row["income_type"]), normalize_key_part(row["tran_id"]),
        )

    def fetch_income_events(self, *, environment: str, account_ref: str,
                            income_type: str | None = None) -> list[dict]:
        return [
            row for key, row in self.events.items()
            if key[0] == normalize_key_part(environment)
            and key[1] == normalize_key_part(account_ref)
            and (income_type is None or key[2] == normalize_key_part(income_type))
        ]

    def upsert_income_coverage(self, row) -> None:
        key = (
            normalize_key_part(row["environment"]), normalize_key_part(row["account_ref"]),
            normalize_key_part(row["income_type"]),
            int(row["start_ms"]), int(row["end_ms"]),
        )
        self.coverage[key] = dict(row)

    def fetch_income_coverage(self, *, environment: str, account_ref: str, income_type: str,
                              start_ms: int | None = None, end_ms: int | None = None):
        matches = self._coverage_rows(
            environment=environment, account_ref=account_ref, income_type=income_type,
            start_ms=start_ms, end_ms=end_ms)
        return matches[-1] if matches else None

    def fetch_income_coverage_range(self, *, environment: str, account_ref: str,
                                    income_type: str, start_ms: int, end_ms: int) -> list[dict]:
        return self._coverage_rows(
            environment=environment, account_ref=account_ref, income_type=income_type,
            start_ms=start_ms, end_ms=end_ms)

    def _coverage_rows(self, *, environment: str, account_ref: str, income_type: str,
                       start_ms: int | None, end_ms: int | None) -> list[dict]:
        rows = [
            row for key, row in self.coverage.items()
            if key[0] == normalize_key_part(environment)
            and key[1] == normalize_key_part(account_ref)
            and key[2] == normalize_key_part(income_type)
        ]
        if start_ms is not None:
            rows = [row for row in rows if int(row["start_ms"]) >= int(start_ms)]
        if end_ms is not None:
            rows = [row for row in rows if int(row["end_ms"]) <= int(end_ms)]
        return sorted(rows, key=lambda row: (int(row["start_ms"]), int(row["end_ms"])))

    def insert_income_conflicts(self, rows) -> int:
        inserted = 0
        for row in rows:
            key = (normalize_key_part(row["environment"]),
                   normalize_key_part(row["account_ref"]),
                   normalize_key_part(row["income_type"]),
                   normalize_key_part(row["tran_id"]),
                   str(row["incoming_content_hash"]))
            if any(item["_key"] == key for item in self.conflicts):
                continue
            self.conflicts.append({**dict(row), "_key": key})
            inserted += 1
        return inserted

    def fetch_income_conflicts(self, *, environment: str, account_ref: str) -> list[dict]:
        return [
            {key: value for key, value in row.items() if key != "_key"}
            for row in self.conflicts
            if normalize_key_part(row["environment"]) == normalize_key_part(environment)
            and normalize_key_part(row["account_ref"]) == normalize_key_part(account_ref)
        ]


# --------------------------------------------------------------------------- #


def test_inclusive_endpoint_mapping() -> None:
    print("\n[1] 官方 page/limit 与包含端点映射")
    start, end = 1789862400000, 1789872000000
    start_inc, end_inc = inclusive_bounds(start, end)
    check("端点上界映射为 end-1ms", end_inc == end - 1, end_inc)
    check("端点下界保持 start", start_inc == start, start_inc)
    check("半开区间往返一致", bounds_from_inclusive(start_inc, end_inc) == (start, end))

    params = build_income_query(start, end, page=2, limit=1000, income_type="funding_fee")
    check("查询带 page/limit", params["page"] == 2 and params["limit"] == 1000, params)
    check("查询 endTime 为包含端点", params["endTime"] == end - 1, params)
    check("incomeType 规范化大写", params["incomeType"] == "FUNDING_FEE", params)

    ok = True
    for bad in (0, -1, 1001, True, "1000"):
        try:
            build_income_query(start, end, page=1, limit=bad)
            ok = False
        except InvalidIncomeRow:
            pass
    check("非法 page_limit 结构化拒绝不崩溃", ok)

    check("内部区间为半开 [start,end)", window_contains(start, end, end - 1)
          and not window_contains(start, end, end))


def test_idempotency_and_pagination(fx: dict) -> dict:
    print("\n[2] 分页完整性 + 幂等键 (environment, account_ref, income_type, tran_id)")
    window = fx["inputs"]["windows"][0]
    sync = _sync_window(window)

    check("页数 = 3（短页终止）", sync.pages_fetched == 3, sync.pages_fetched)
    check("W1 coverage COMPLETE", sync.coverage.coverage_status == "COMPLETE",
          sync.coverage.coverage_status)
    proof = sync.coverage.termination_proof
    check("终止证据为结构化且短页",
          proof is not None and proof.last_page_size < proof.page_limit, proof)
    check("终止证据窗口与覆盖绑定",
          proof.start_ms == window["start_ms"] and proof.end_ms == window["end_ms"], proof)
    check("终止时间不早于窗口末尾", proof.terminated_at_ms >= window["end_ms"], proof)
    check("水位达到窗口末尾", sync.coverage.watermark == window["end_ms"],
          sync.coverage.watermark)
    check("无已声明缺口", sync.coverage.gaps == (), sync.coverage.gaps)

    keys = dedupe_key_texts(sync.events)
    check("入库唯一 tran_id = 4", len(set(keys)) == 4 and len(keys) == 4, keys)
    check("幂等主键四元组可审计",
          all(len(item) == 4 for item in idempotency_keys(sync.events)))
    check("SYN-TR-9002/9003/9004 同毫秒行未丢",
          {"SYN-TR-9002", "SYN-TR-9003", "SYN-TR-9004"}.issubset(
              {e.tran_id for e in sync.events}), [e.tran_id for e in sync.events])
    check("lost_rows = 0（同毫秒满页继续翻页）", len(sync.events) == 4, len(sync.events))

    # 二次投递：同内容 -> duplicates；冲突版本 -> 隔离
    redelivered = list(window["pages"])
    redelivered[2] = list(window["redelivery"]["rows"])[1:]
    sync2 = sync_income_window(
        _page_fetcher(redelivered),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type=window["income_type"],
        start_ms=window["start_ms"], end_ms=window["end_ms"],
        page_limit=2, received_at_ms=1789870100000, existing=list(sync.events),
    )
    check("重复拉取不重复入账（inserted 为空）",
          sync2.insert.inserted == (), sync2.insert.inserted)
    quarantined_ids = {record.key for record in sync2.insert.quarantined}
    check("内容冲突进入隔离", len(sync2.insert.quarantined) == 1 and
          "syn-tr-9002" in "".join(quarantined_ids).lower(), quarantined_ids)
    check("冲突 tran_id 整体退出事件集合（不按到达顺序猜金额）",
          "SYN-TR-9002" not in {e.tran_id for e in sync2.events}
          and all(Decimal(e.amount) != Decimal("-2.50000000") for e in sync2.events),
          [(e.tran_id, e.amount) for e in sync2.events])
    check("隔离不改覆盖状态（coverage 仍 COMPLETE）",
          sync2.coverage.coverage_status == "COMPLETE", sync2.coverage.coverage_status)

    # 死循环防护：满页且内容完全重复
    stalled = sync_income_window(
        lambda page_no: list(window["pages"][0]),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=window["start_ms"], end_ms=window["end_ms"],
        page_limit=2, received_at_ms=1789870200000, max_pages=6,
    )
    check("重复满页判 PAGINATION_STALLED 不无限翻页",
          REASON_PAGINATION_STALLED in stalled.reason_codes
          and stalled.coverage.coverage_status == "GAP", stalled.reason_codes)

    # 乱序页拒绝
    unsorted = sync_income_window(
        _page_fetcher([list(reversed(window["pages"][1]))]),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=window["start_ms"], end_ms=window["end_ms"],
        page_limit=2, received_at_ms=1789870300000,
    )
    check("乱序满页判 PAGE_NOT_SORTED 且覆盖 GAP",
          REASON_PAGE_NOT_SORTED in unsorted.reason_codes
          and unsorted.coverage.coverage_status == "GAP", unsorted.reason_codes)
    return {"sync": sync, "sync2": sync2}


def test_zero_fee_and_unknown(fx: dict) -> None:
    print("\n[3] 「已验证为零」与「未知」必须区分")
    windows = {item["window_id"]: item for item in fx["inputs"]["windows"]}

    w2 = _sync_window(windows["W2"])
    check("W2 确认为空窗口", w2.confirmed_empty and not w2.events, len(w2.events))
    proof = window_zero_fee_proof(w2, late_recheck_done=True)
    check("W2 零费用可证明（结构化证明）", proof.provable is True, proof.reason_codes)
    final2 = window_cost_finalization(
        w2, attribution=None, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("W2 cost_status = COMPLETE", final2.cost_status == "COMPLETE", final2)

    late_missing = window_zero_fee_proof(w2, late_recheck_done=False)
    check("缺迟到复查时不可证明零", late_missing.provable is False, late_missing.reason_codes)

    w3 = _sync_window(windows["W3"])
    check("W3 来源不可用 -> coverage GAP", w3.coverage.coverage_status == "GAP",
          w3.coverage.coverage_status)
    check("W3 不得判为「已验证为零」", w3.confirmed_empty is False)
    check("W3 缺口显式声明", len(w3.coverage.gaps) == 1, w3.coverage.gaps)
    final3 = window_cost_finalization(
        w3, attribution=None, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("W3 cost_status = UNKNOWN（未知不是零）", final3.cost_status == "UNKNOWN", final3)
    check("W3 无零费用证明", window_zero_fee_proof(w3, late_recheck_done=True).provable is False)

    # 空窗口但覆盖非 COMPLETE 时不得判零
    partial = sync_income_window(
        lambda page_no: list(windows["W1"]["pages"][0]),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=windows["W1"]["start_ms"],
        end_ms=windows["W1"]["end_ms"], page_limit=2,
        received_at_ms=1789870400000, max_pages=1,
    )
    check("未读完窗口不得标 COMPLETE", partial.coverage.coverage_status == "GAP",
          partial.coverage.coverage_status)
    check("未读完窗口 cost_status 非 COMPLETE",
          window_cost_finalization(partial, attribution=None, late_recheck_done=True,
                                   lifecycle_id="LC-ZZZ-1").cost_status != "COMPLETE")


def test_attribution_and_conservation(fx: dict, sync) -> None:
    """sync 为「已检测到 SYN-TR-9002 内容冲突之后」的窗口视图。"""
    print("\n[4] 金额归因守恒 + 三状态独立")
    events = list(sync.events) + _events(
        [fx["inputs"]["dual_side_event"]], received_at_ms=1789862700000,
    )
    late = _events([fx["inputs"]["late_arrival"]], received_at_ms=1789862700000)[0]
    events.append(late)

    attribution = attribute_funding(
        events, fx["inputs"]["positions"], sync.coverage, fx_rates={})
    golden = fx["derived_golden"]["attribution"]
    check("已归因资金费 = -1.70",
          attribution.attributed_usdt == Decimal(golden["attributed_usdt"]),
          attribution.attributed_usdt)
    check("双向仓 -> UNATTRIBUTED 且金额 -0.40",
          attribution.unattributed_usdt == Decimal(golden["unattributed_usdt"]),
          attribution.unattributed_usdt)
    check("无 UNKNOWN 归因项", attribution.unknown_usdt is None, attribution.unknown_usdt)
    statuses = {d.event_id: d.status for d in attribution.result.decisions}
    dual_key = idempotency_key_text(normalize_income_row(
        fx["inputs"]["dual_side_event"], environment="demo",
        account_ref="SYNTH_ACCT_PLACEHOLDER", received_at_ms=0))
    check("双仓事件判定为 UNATTRIBUTED", statuses.get(dual_key) == "UNATTRIBUTED",
          statuses.get(dual_key))

    check("守恒：Σclean == 归因 + 未归因",
          attribution.conservation_source_amount()
          == Decimal(golden["attributed_usdt"]) + Decimal(golden["unattributed_usdt"]),
          attribution.conservation_source_amount())

    allocations = allocate_funding_to_lifecycles(
        attribution, lifecycle_weights={"LC-ZZZ-1": 1},
        version="v1", evidence="funding_window:W1")
    check("分摊总额精确等于源金额",
          sum((a.allocated_amount for a in allocations), Decimal(0))
          == attribution.attributed_usdt, allocations)

    # 三状态独立
    final = window_cost_finalization(
        sync, attribution=attribution, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("三状态独立：coverage=COMPLETE", final.coverage_status == "COMPLETE", final)
    check("三状态独立：attribution=UNATTRIBUTED", final.attribution_status == "UNATTRIBUTED", final)
    check("三状态独立：cost=INCOMPLETE", final.cost_status == "INCOMPLETE", final)
    check("INCOMPLETE 回带 UNATTRIBUTED_EXPOSURE",
          "UNATTRIBUTED_EXPOSURE" in final.reason_codes, final.reason_codes)

    # 有冲突隔离时，即使归因全部成功也不得静默闭合为 COMPLETE
    only_clean = [e for e in events if e.tran_id != "SYN-TR-9002"]
    attr_clean = attribute_funding(only_clean, fx["inputs"]["positions"], sync.coverage)
    final_conflict = window_cost_finalization(
        sync, attribution=attr_clean, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("存在隔离冲突 -> cost 不得 COMPLETE",
          final_conflict.cost_status == "INCOMPLETE"
          and "INCOME_ID_CONFLICT" in final_conflict.reason_codes, final_conflict)

    # 无冲突 + 归因全部成功 -> cost 才可能 COMPLETE
    ws = fx["inputs"]["windows"][0]
    clean_pages = [
        [ws["pages"][1][0], ws["pages"][0][1]],
        [fx["inputs"]["late_arrival"], ws["pages"][1][1]],
        [],
    ]
    clean_sync = sync_income_window(
        _page_fetcher(clean_pages),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=ws["start_ms"], end_ms=ws["end_ms"],
        page_limit=2, received_at_ms=1789870500000,
    )
    check("干净窗口无隔离", clean_sync.quarantine == (), clean_sync.quarantine)
    attr_clean2 = attribute_funding(
        list(clean_sync.events), fx["inputs"]["positions"], clean_sync.coverage)
    check("干净窗口归因全部成功",
          attr_clean2.result.allocation_status == "ATTRIBUTED", attr_clean2.result)
    final_clean = window_cost_finalization(
        clean_sync, attribution=attr_clean2, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("无冲突且全归因 -> cost COMPLETE", final_clean.cost_status == "COMPLETE", final_clean)


def test_late_income_revision(fx: dict) -> None:
    print("\n[5] 迟到收入可修订且不重复闭合")
    late = _events([fx["inputs"]["late_arrival"]], received_at_ms=1789862700000)[0]
    closed = fx["inputs"]["closed_outcomes"]
    ledger = LateIncomeLedger()
    revision, first = ledger.apply(late, closed, lifecycle_id="LC-ZZZ-1")
    check("迟到收入产生修订", first is True and revision.requires_recalculation is True, revision)
    check("不新增闭合结果", revision.new_closed_outcome_created is False, revision)
    check("修订绑定原始闭合结果",
          revision.outcome_id == "OUT-1" and revision.original_closed_at_ms == 1789862500000,
          revision)

    again, first_again = ledger.apply(late, closed, lifecycle_id="LC-ZZZ-1")
    check("重复投递不重复闭合",
          first_again is False and again.revision_id == revision.revision_id
          and len(ledger.revisions) == 1, ledger.revisions)
    check("不产生第二个闭合结果", ledger.closed_outcomes_created() == 0)

    missing = LateIncomeLedger()
    try:
        missing.apply(late, [], lifecycle_id="LC-NOT-EXIST")
        check("无闭合结果时明确报错", False, "no error")
    except LookupError:
        check("无闭合结果时明确报错", True)


def test_session_reconciliation(fx: dict, sync) -> None:
    print("\n[6] 钱包现金流与会话口径独立勾稽（不重复计费）")
    inputs = fx["inputs"]
    events = list(sync.events) + _events(
        [inputs["dual_side_event"], inputs["non_usdt_fee"]] + inputs["session"]["commission_income"],
        received_at_ms=1789862700000,
    )
    events.append(_events([inputs["late_arrival"]], received_at_ms=1789862700000)[0])
    attribution = attribute_funding(events, inputs["positions"], sync.coverage, fx_rates={})

    session = inputs["session"]
    result = reconcile_session(
        opening_wallet_usdt=session["opening_wallet_usdt"],
        closing_wallet_usdt=session["closing_wallet_usdt"],
        fills=session["fills"],
        income_events=events,
        attribution=attribution,
        transfers_usdt=session["transfers_usdt"],
        other_classified_usdt=session["other_classified_usdt"],
        fx_rates={},
    )
    golden = fx["derived_golden"]["session"]
    check("LHS = 期末 - 期初 = -3.10",
          result.identity_lhs() == Decimal(golden["lhs_closing_minus_opening"]),
          result.identity_lhs())
    check("RHS 分量和 = -3.10",
          sum(result.components.values(), Decimal(0)) == Decimal(golden["rhs_components_sum"]),
          result.components)
    check("未解释差额 = 0", result.unexplained_usdt == Decimal(golden["unexplained_usdt"]),
          result.unexplained_usdt)
    check("勾稽平衡标记", result.balanced is True, result.reason_codes)

    check("资金费已归因进入勾稽 = -1.70",
          result.components["funding_attributed_usdt"] == Decimal("-1.70000000"),
          result.components)
    check("资金费未归因仍作为已分类现金流 = -0.40",
          result.components["funding_unattributed_usdt"] == Decimal("-0.40000000"),
          result.components)
    check("fill 手续费 = -0.70（唯一费用入口）",
          result.components["net_fees_from_fills_usdt"] == Decimal("-0.70000000"),
          result.components)

    # 不重复计：KZ COMMISSION 收入行无汇率 -> 不进 USDT 净值，仅记录原币
    check("非 USDT 费用不出净值（None 而非 0）",
          any(item.get("asset") == "KZ" for item in result.unconverted), result.unconverted)
    kz = next(item for item in result.unconverted if item.get("asset") == "KZ")
    check("非 USDT 保留原币金额",
          Decimal(kz["native_amount"]) == Decimal("-0.01000000"), kz)
    check("费用交叉核对 MATCH（fill 手续费 == COMMISSION 收入）",
          result.fee_cross_check["status"] == "MATCH", result.fee_cross_check)
    check("COMMISSION 收入未被加进可加分量（否则 unexplained 变 +0.70）",
          result.components["net_fees_from_fills_usdt"] == Decimal("-0.70000000")
          and sum(result.components.values(), Decimal(0)) == Decimal("-3.10000000")
          and "income_commission_usdt" not in result.components, list(result.components))

    # 若调用方把 COMMISSION 塞进可加分量 -> 必须报错
    try:
        assert_no_double_count({"commission": Decimal("-0.01")}, events)
        check("重复计入被结构化拒绝", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("重复计入被结构化拒绝", "INCOME_FEE_DOUBLE_COUNT" in str(exc), exc)

    # 关键分量未知时不得输出「已平衡」
    unknown_session = reconcile_session(
        opening_wallet_usdt="100", closing_wallet_usdt="100",
        fills=[{"realized_pnl": None, "commission": "0.1", "commission_asset": "USDT"}],
        income_events=[], fx_rates={},
    )
    check("未知分量时不报平衡", unknown_session.balanced is False, unknown_session.reason_codes)


def test_persistence_idempotency(fx: dict, sync, sync2) -> None:
    print("\n[7] 持久化幂等：重复拉取不重复入账")
    store = MemoryIncomeStore()
    book = IncomeReconciliationBook(
        store=store, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
    first = book.persist(sync)
    second = book.persist(sync)
    check("首次写入 4 行", first.inserted == 4, first)
    check("重复写入新增 0 行", second.inserted == 0, second)
    check("存储中唯一 4 行", len(store.events) == 4, len(store.events))
    coverage = store.fetch_income_coverage(
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER", income_type="FUNDING_FEE")
    check("覆盖证据落库且为 COMPLETE",
          coverage is not None and coverage["coverage_status"] == "COMPLETE", coverage)
    check("覆盖证据回带结构化终止证明",
          isinstance(coverage["termination_proof"], dict), coverage["termination_proof"])
    loaded = book.load_existing()
    check("已入库键可回读用于幂等判定", len(loaded) == 4, loaded)

    # 隔离（quarantine）不再是「内存里消失」：既回带又落库（P1-7）
    conflict_report = book.persist(sync2)
    check("冲突窗口 persist 回带隔离清单",
          len(conflict_report.conflicts) == 1
          and "syn-tr-9002" in conflict_report.conflicts[0].key.lower(),
          conflict_report)
    check("隔离清单落库可重启后审计",
          len(store.fetch_income_conflicts(
              environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")) == 1,
          store.conflicts)
    check("隔离行的入账金额不再出现在事件集",
          all(Decimal(row["amount"]) != Decimal("-2.50000000") for row in store.events.values()),
          [row["amount"] for row in store.events.values()])


def test_persistence_profile_binding(fx: dict, sync) -> None:
    print("\n[10] 跨 profile：coverage 与事件必须同域（不得互相覆盖）")
    store = MemoryIncomeStore()
    live_book = IncomeReconciliationBook(
        store=store, environment="live", account_ref="SYNTH_ACCT_PLACEHOLDER")
    try:
        live_book.persist(sync)  # sync 的环境域是 demo
        check("跨 profile 写入被结构化拒绝", False, "no error")
    except ProfileMismatchError as exc:
        check("跨 profile 写入被结构化拒绝", "PROFILE_MISMATCH" in str(exc), exc)
    check("被拒绝时不落任何覆盖证据", store.coverage == {}, store.coverage)
    check("被拒绝时不落任何事件", store.events == {}, store.events)

    demo_book = IncomeReconciliationBook(
        store=store, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
    report = demo_book.persist(sync)
    check("域一致时写入成功且域被回带",
          report.inserted == 4 and report.environment == "demo", report)
    check("coverage 落库域与事件域一致（live 查不到 demo 证据）",
          demo_book.coverage_for_window(
              income_type="FUNDING_FEE", start_ms=sync.start_ms, end_ms=sync.end_ms)
          is not None
          and store.fetch_income_coverage(
              environment="live", account_ref="SYNTH_ACCT_PLACEHOLDER",
              income_type="FUNDING_FEE") is None,
          store.coverage)


def test_coverage_window_queries(fx: dict, sync) -> None:
    print("\n[11] 按窗口读覆盖：缺失不得当零（旧接口只取最新窗口）")
    windows = {item["window_id"]: item for item in fx["inputs"]["windows"]}
    store = MemoryIncomeStore()
    book = IncomeReconciliationBook(
        store=store, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
    book.persist(sync)                                     # W1：有 1 笔以上事件
    book.persist(_sync_window(windows["W2"]))               # W2：确认为空

    w1 = book.coverage_for_window(
        income_type="FUNDING_FEE", start_ms=windows["W1"]["start_ms"],
        end_ms=windows["W1"]["end_ms"])
    w2 = book.coverage_for_window(
        income_type="FUNDING_FEE", start_ms=windows["W2"]["start_ms"],
        end_ms=windows["W2"]["end_ms"])
    check("W1 覆盖证据可按区间取回（不再被最新窗口顶掉）",
          w1 is not None and w1.coverage_status == "COMPLETE"
          and w1.income_type == "FUNDING_FEE"
          and income_coverage_from_row(store.coverage[
              next(key for key in store.coverage if int(key[3]) == windows["W1"]["start_ms"])
          ]).coverage_status == "COMPLETE", w1)
    check("W2 覆盖证据可按区间取回",
          w2 is not None and w2.coverage_status == "COMPLETE", w2)
    check("未同步窗口返回 None（缺失不是零）",
          book.coverage_for_window(
              income_type="FUNDING_FEE", start_ms=windows["W3"]["start_ms"],
              end_ms=windows["W3"]["end_ms"]) is None)

    aggregate = book.aggregate_coverage(
        income_type="FUNDING_FEE", start_ms=windows["W1"]["start_ms"],
        end_ms=windows["W2"]["end_ms"])
    check("区间 [W1,W2) 全部 COMPLETE ⇒ 可判定已验证",
          aggregate.complete is True and aggregate.missing == (), aggregate)
    check("区间覆盖判定为真", aggregate.covers(
        windows["W1"]["start_ms"], windows["W2"]["end_ms"]) is True)

    # 含 W3（来源错误未同步）的区间：缺口必须显式，绝不当零
    book.persist(_sync_window(windows["W3"]))
    with_w3 = book.aggregate_coverage(
        income_type="FUNDING_FEE", start_ms=windows["W1"]["start_ms"],
        end_ms=windows["W3"]["end_ms"])
    check("区间含非 COMPLETE 窗口 ⇒ 不可宣称已验证",
          with_w3.complete is False and with_w3.incomplete != (), with_w3)
    partial = book.aggregate_coverage(
        income_type="FUNDING_FEE", start_ms=windows["W1"]["start_ms"],
        end_ms=windows["W3"]["end_ms"] + 3_600_000)
    check("区间含未同步子区间 ⇒ missing 显式列出",
          partial.complete is False and partial.missing != ()
          and partial.covers(
              windows["W1"]["start_ms"], windows["W3"]["end_ms"] + 3_600_000) is False,
          partial)
    empty = aggregate_coverage([], start_ms=10, end_ms=20)
    check("完全无覆盖证据 ⇒ 整段 missing（缺失绝不当零）",
          empty.complete is False and empty.missing == ((10, 20),)
          and empty.covers(10, 20) is False, empty)


def test_normalization_rejects_missing() -> None:
    print("\n[8] 缺字段不静默跳过")
    bad_cases = [
        {"incomeType": "FUNDING_FEE", "time": 1, "income": "1"},          # 缺 tranId
        {"incomeType": "FUNDING_FEE", "tranId": "T", "income": "1"},      # 缺 time
        {"incomeType": "FUNDING_FEE", "tranId": "T", "time": 1},          # 缺 income
        {"incomeType": "FUNDING_FEE", "tranId": "T", "time": 1, "income": "abc"},
        {"incomeType": "FUNDING_FEE", "tranId": "T", "time": 0, "income": "1"},
    ]
    ok = True
    for row in bad_cases:
        try:
            normalize_income_row(row, environment="demo", account_ref="A",
                                 received_at_ms=1)
            ok = False
        except InvalidIncomeRow:
            pass
    check("缺 tranId/time/income 一律结构化拒绝", ok)
    check("income_type 规范化大写",
          canonical_income_type(" funding_fee ") == "FUNDING_FEE")
    check("默认 page_limit 为官方上限", DEFAULT_PAGE_LIMIT == 1000, DEFAULT_PAGE_LIMIT)


def test_cross_page_defenses(fx: dict) -> None:
    print("\n[12] 跨页防线：页序回退 / 重叠复读不稳定 / 跨页重复不双计 / 越窗行")
    ws = fx["inputs"]["windows"][0]
    base = dict(
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=ws["start_ms"], end_ms=ws["end_ms"],
        page_limit=2, received_at_ms=1789871100000,
    )

    # (a) 跨页重复行：事件集必须按幂等键唯一，金额不得双计（P2-2）
    dup_pages = [
        [ws["pages"][0][0], ws["pages"][0][1]],
        [ws["pages"][1][0], ws["pages"][0][1]],   # 9003 第二页重复出现
        [],
    ]
    dup = sync_income_window(_page_fetcher(dup_pages), **base)
    dup_keys = dedupe_key_texts(dup.events)
    check("跨页重复行后事件集按幂等键唯一",
          len(dup_keys) == len(set(dup_keys)) == 3, dup_keys)
    check("跨页重复行不重复求和（Σ = -1.50）",
          sum((Decimal(item.amount) for item in dup.events), Decimal(0))
          == Decimal("-1.50000000"),
          sum((Decimal(item.amount) for item in dup.events), Decimal(0)))
    check("跨页重复只记 duplicates、不新增 inserted",
          dup.insert.inserted == () and len(dup.insert.duplicates) == 1,
          (dup.insert.inserted, dup.insert.duplicates))

    # (b) 页序回退：后一页最大时间早于前一页 -> GAP（P1-3）
    backward = sync_income_window(_page_fetcher([
        list(ws["pages"][0]),
        [ws["pages"][1][0], fx["inputs"]["late_arrival"]],
    ]), **base)
    check("页序回退判 CROSS_PAGE_NOT_MONOTONIC 且覆盖 GAP",
          REASON_CROSS_PAGE_NOT_MONOTONIC in backward.reason_codes
          and backward.coverage.coverage_status == "GAP", backward.reason_codes)

    # (c) 重叠复读不稳定：同一 page 两次读取不一致 -> GAP（P1-3）
    calls = {"page1": 0}

    def unstable_fetch(page_no: int) -> list[dict]:
        if page_no == 1:
            calls["page1"] += 1
            rows = list(ws["pages"][0])
            return rows if calls["page1"] == 1 else list(reversed(rows))
        return list(ws["pages"][1]) if page_no == 2 else []

    unstable = sync_income_window(unstable_fetch, **base)
    check("重叠复读不一致判 PAGE_WINDOW_UNSTABLE 且覆盖 GAP",
          REASON_PAGE_WINDOW_UNSTABLE in unstable.reason_codes
          and unstable.coverage.coverage_status == "GAP", unstable.reason_codes)

    # (d) 越窗行（t == end）：半开区间外，不得静默收下（P2-1）
    escaped = sync_income_window(
        _page_fetcher([[ws["pages"][1][0], ws["pages"][0][0]]]),
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        income_type="FUNDING_FEE", start_ms=ws["start_ms"],
        end_ms=1789862402000, page_limit=2, received_at_ms=1789871200000,
    )
    check("t == end 的行不进本窗口事件集",
          {item.tran_id for item in escaped.events} == {"SYN-TR-9001"},
          [item.tran_id for item in escaped.events])
    check("越窗行显式回带 ROWS_OUTSIDE_WINDOW",
          REASON_ROWS_OUTSIDE_WINDOW in escaped.reason_codes, escaped.reason_codes)


def test_allocation_quantum_conflict() -> None:
    print("\n[13] 极小金额 + 多腿：量化冲突降级而非整条成本链中断")
    tiny = IncomeAttribution(
        attributed_usdt=Decimal("0.00000003"),
        unattributed_usdt=Decimal(0),
        unknown_usdt=None,
        result=AttributionResult(allocation_status="ATTRIBUTED"),
        attributed_keys=(), unattributed_keys=(), unknown_keys=(),
    )
    weights = {f"LC-{index}": 1 for index in range(5)}
    try:
        allocate_funding_to_lifecycles(
            tiny, lifecycle_weights=weights, version="v1", evidence="e")
        check("极小金额多腿仍抛 AllocationError（fail-closed）", False, "no error")
    except AllocationError as exc:
        check("极小金额多腿仍抛 AllocationError（fail-closed）", True, exc)
    degraded = allocate_funding_or_incomplete(
        tiny, lifecycle_weights=weights, version="v1", evidence="e")
    check("降级入口给出 ALLOCATION_QUANTUM_CONFLICT 且不中断",
          degraded.complete is False and degraded.allocations == ()
          and degraded.reason_codes == (REASON_ALLOCATION_QUANTUM_CONFLICT,),
          degraded)

    normal = IncomeAttribution(
        attributed_usdt=Decimal("-1.70"), unattributed_usdt=Decimal(0),
        unknown_usdt=None, result=AttributionResult(allocation_status="ATTRIBUTED"),
        attributed_keys=(), unattributed_keys=(), unknown_keys=(),
    )
    ok = allocate_funding_or_incomplete(
        normal, lifecycle_weights={"LC-A": 1, "LC-B": 3}, version="v1", evidence="e")
    check("正常金额分摊守恒且 complete",
          ok.complete is True
          and sum((item.allocated_amount for item in ok.allocations), Decimal(0))
          == Decimal("-1.70"), ok)


def test_fee_cross_check(fx: dict) -> None:
    print("\n[14] 交叉核对：不误报、balanced 与 status 不矛盾、REALIZED_PNL 也核对")
    realized_only = _events([{
        "incomeType": "REALIZED_PNL", "tranId": "SYN-TR-9300",
        "time": 1789863000000, "symbol": "ZZZUSDT", "asset": "USDT",
        "income": "5.00000000",
    }], received_at_ms=1789863000000)
    fills = [{"realized_pnl": "0.00000000", "commission": "0.70000000",
              "commission_asset": "USDT"}]

    no_commission_income = reconcile_session(
        opening_wallet_usdt="100", closing_wallet_usdt="99.3",
        fills=fills, income_events=realized_only, fx_rates={})
    check("收入侧无 COMMISSION 行时不得误报手续费 MISMATCH",
          no_commission_income.fee_cross_check["status"] == "NOT_COMPARABLE"
          and "FILL_INCOME_FEE_MISMATCH" not in no_commission_income.reason_codes,
          no_commission_income.fee_cross_check)
    check("REALIZED_PNL 差异被核对出来（原先完全不核对）",
          no_commission_income.fee_cross_check["realized_cross_check"] == "MISMATCH"
          and "FILL_INCOME_REALIZED_PNL_MISMATCH" in no_commission_income.reason_codes,
          no_commission_income.fee_cross_check)
    check("差额为 0 但存在不一致时 balanced 不得为 True（消除自相矛盾）",
          no_commission_income.unexplained_usdt == Decimal(0)
          and no_commission_income.balanced is False,
          (no_commission_income.unexplained_usdt, no_commission_income.balanced))

    matched = reconcile_session(
        opening_wallet_usdt="100", closing_wallet_usdt="100",
        fills=fills,
        income_events=_events([fx["inputs"]["session"]["commission_income"][0]],
                              received_at_ms=1789863000000),
        fx_rates={})
    check("COMMISSION 收入与 fill 手续费同额 ⇒ MATCH",
          matched.fee_cross_check["status"] == "MATCH"
          and "FILL_INCOME_FEE_MATCH" in matched.reason_codes,
          matched.fee_cross_check)


def test_late_income_content_change(fx: dict) -> None:
    print("\n[15] 迟到修订：同内容重放 vs 同键改额必须分开")
    closed = fx["inputs"]["closed_outcomes"]
    ledger = LateIncomeLedger()
    late = _events([fx["inputs"]["late_arrival"]], received_at_ms=1789862700000)[0]
    revision, first = ledger.apply(late, closed, lifecycle_id="LC-ZZZ-1")
    again, first_again = ledger.apply(late, closed, lifecycle_id="LC-ZZZ-1")
    check("同内容重放回带 LATE_REVISION_ALREADY_APPLIED",
          first is True and first_again is False
          and ledger.duplicate_reason_codes == ("LATE_REVISION_ALREADY_APPLIED",),
          ledger.duplicate_reason_codes)
    changed = normalize_income_row(
        {**fx["inputs"]["late_arrival"], "income": "-9.90000000"},
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
        received_at_ms=1789862700000)
    try:
        ledger.apply(changed, closed, lifecycle_id="LC-ZZZ-1")
        check("同键改额被结构化报出（不再静默当成重放）", False, "no error")
    except LateIncomeContentChanged as exc:
        check("同键改额被结构化报出（不再静默当成重放）",
              "LATE_REVISION_CONTENT_CHANGED" in str(exc), exc)
    check("改额未被当成已应用（修订数仍为 1）", len(ledger.revisions) == 1,
          ledger.revisions)
    check("契约 revision_id 确实不含金额（说明必须由本层兜底）",
          again.revision_id == revision.revision_id, again)


def test_attribution_unconverted_and_missing(fx: dict, sync) -> None:
    print("\n[16] 归因侧：不可换算事件显式回带；非空窗口缺给对 reason")
    kz_funding = _events([{
        "incomeType": "FUNDING_FEE", "tranId": "SYN-TR-9400", "time": 1789863000000,
        "symbol": "ZZZUSDT", "asset": "KZ", "income": "-0.01000000",
    }], received_at_ms=1789863000000)
    attribution = attribute_funding(
        kz_funding, fx["inputs"]["positions"], sync.coverage, fx_rates={})
    check("不可换算资金费不再被静默剔除",
          len(attribution.unconverted) == 1
          and attribution.unconverted[0]["tran_id"] == "SYN-TR-9400"
          and attribution.unconverted[0]["reason"] == "NO_SOURCED_FX_RATE",
          attribution.unconverted)
    check("不可换算事件给 reason code",
          attribution.reason_codes == ("NO_SOURCED_FX_RATE",), attribution.reason_codes)
    check("不可换算不计入净额（attributed+unattributed = 0）",
          attribution.conservation_source_amount() == Decimal(0)
          and attribution.unknown_usdt is None,
          attribution.conservation_source_amount())

    missing_attribution = window_cost_finalization(
        sync, attribution=None, late_recheck_done=True, lifecycle_id="LC-ZZZ-1")
    check("非空窗口缺归因 → NO_ATTRIBUTION_DECISIONS（不是零费用证明问题）",
          REASON_ATTRIBUTION_MISSING in missing_attribution.reason_codes
          and "ZERO_FEE_NOT_PROVABLE" not in missing_attribution.reason_codes
          and missing_attribution.cost_status == "INCOMPLETE",
          missing_attribution)


def test_income_type_governance() -> None:
    print("\n[17] income_type 枚举治理（未知类型不得成为独立收入族）")
    check("枚举覆盖五种已治理类型",
          KNOWN_INCOME_TYPES == {
              "FUNDING_FEE", "COMMISSION", "REALIZED_PNL", "TRANSFER", "INSURANCE_CLEAR",
          }, sorted(KNOWN_INCOME_TYPES))
    check("拼写变体不被视为已知类型",
          is_known_income_type("FUNDINGFEE") is False
          and is_known_income_type(" funding_fee ") is True)
    try:
        sync_income_window(
            lambda page_no: [], environment="demo", account_ref="A",
            income_type="FUNDINGFEE", start_ms=1, end_ms=1000, received_at_ms=1)
        check("窗口声明未知类型被结构化拒绝", False, "no error")
    except InvalidIncomeRow as exc:
        check("窗口声明未知类型被结构化拒绝",
              REASON_UNKNOWN_INCOME_TYPE in str(exc), exc)

    mixed = sync_income_window(
        _page_fetcher([
            [{"incomeType": "COMMISSION", "tranId": "X-MIX", "time": 10,
              "asset": "USDT", "income": "0.10000000"}],
            [{"incomeType": "WEIRD_FEE", "tranId": "X-UNK", "time": 11,
              "asset": "USDT", "income": "0.10000000"}],
        ]),
        environment="demo", account_ref="A", income_type="FUNDING_FEE",
        start_ms=1, end_ms=1000, page_limit=1, received_at_ms=1,
    )
    check("非本窗口类型的行被排除且显式回带 reason",
          mixed.events == () and "INCOME_TYPE_MISMATCH" in mixed.reason_codes
          and "UNKNOWN_INCOME_TYPE" in mixed.reason_codes, mixed.reason_codes)


def test_session_input_edges(fx: dict) -> None:
    print("\n[18] 会话勾稽输入边界：结构化拒绝 + 重复计费守卫 + 未知分量显式")
    try:
        reconcile_session(
            opening_wallet_usdt="0", closing_wallet_usdt="0", fills=(),
            income_events=[{"incomeType": "COMMISSION"}])
        check("dict 入参结构化拒绝（不再 AttributeError）", False, "no error")
    except WalletIdentityError as exc:
        check("dict 入参结构化拒绝（不再 AttributeError）", True, exc)

    events = _events([
        fx["inputs"]["session"]["commission_income"][0],
        {"incomeType": "REALIZED_PNL", "tranId": "SYN-TR-9500", "time": 1789863000000,
         "symbol": "ZZZUSDT", "asset": "USDT", "income": "1.00000000"},
    ], received_at_ms=1789863000000)
    blocked = ["commission", "commission_income_usdt", "income_fees",
               "net_realized_pnl_usdt", "realized_pnl_from_income"]
    rejected = 0
    for key in blocked:
        try:
            assert_no_double_count({key: Decimal("1")}, events)
        except Exception as exc:  # noqa: BLE001
            rejected += 1 if "INCOME_FEE_DOUBLE_COUNT" in str(exc) else 0
    check("真实字段名（非字面 commission）也被拒绝",
          rejected == len(blocked), f"{rejected}/{len(blocked)}")
    allowed = ["net_fees_from_fills_usdt", "funding_attributed_usdt",
               "transfers_usdt", "insurance_income_usdt"]
    ok = True
    for key in allowed:
        try:
            assert_no_double_count({key: Decimal("1")}, events)
        except Exception:  # noqa: BLE001
            ok = False
    check("合法可加分量不被误伤", ok, allowed)

    # 调用方经 reconcile_session 追加分量时同样被守卫拦截
    try:
        reconcile_session(
            opening_wallet_usdt="0", closing_wallet_usdt="0", fills=(),
            income_events=events, extra_components={"commission_income_usdt": "1"})
        check("reconcile_session 入口对追加分量执行同一守卫", False, "no error")
    except Exception as exc:  # noqa: BLE001
        check("reconcile_session 入口对追加分量执行同一守卫",
              "INCOME_FEE_DOUBLE_COUNT" in str(exc), exc)

    unknown = reconcile_session(
        opening_wallet_usdt="0", closing_wallet_usdt="0",
        fills=[{"realized_pnl": None, "commission": "0.1", "commission_asset": "USDT"}],
        income_events=(), fx_rates={})
    check("被折算为 0 的未知分量显式列出",
          unknown.unknown_components == ("realized_trade_pnl_usdt",)
          and unknown.balanced is False, unknown)


def _patched_database_dir() -> tuple[Path, str]:
    """返回 (可导入 ``audit`` 包的 src 目录, 模式说明)。

    1) 仓库已应用补丁 01（I 集成后）→ 直接用仓库 ``src``：真实 ``AuditDatabase``。
    2) 否则把补丁 01 应用到 ``src`` 的**临时副本**上再导入——被测的仍是补丁本身，
       而不是「作者另写一份实现」。两条路径都真实执行，**不再 SKIP**（P0-2/P2-8）。
    """
    patch = (ROOT / "docs" / "dispatch" / "patches" / "U01" / "author-2c73c9e"
             / "01_audit_database_income_store.diff")
    sandbox = Path(tempfile.mkdtemp(prefix="mta-u01-patched-"))
    shutil.copytree(ROOT / "src", sandbox / "src")
    applied = subprocess.run(
        ["git", "apply", "-p1", str(patch)],
        cwd=str(sandbox), capture_output=True, text=True,
    )
    if applied.returncode != 0:
        raise RuntimeError(
            "补丁 01 无法应用到 src 副本（作者必须保证补丁可 git apply）: "
            f"{applied.stderr.strip() or applied.stdout.strip()}"
        )
    return sandbox / "src", f"补丁沙箱 git apply（{'已应用'}）"


def _load_audit_database():
    """导入（真实或补丁沙箱的）``audit.database`` 模块，返回 (类, 模式说明, 清理句柄)。"""
    import importlib

    sandbox_src: Path | None = None
    try:
        from audit.database import AuditDatabase  # type: ignore[attr-defined]
        if hasattr(AuditDatabase, "insert_income_events"):
            return AuditDatabase, "仓库 src（补丁已由 I 应用）", None
    except Exception:  # noqa: BLE001 - 仓库 src 不可用时走沙箱
        pass
    sandbox_src, mode = _patched_database_dir()
    for name in [key for key in list(sys.modules) if key.split(".")[0] == "audit"]:
        del sys.modules[name]
    sys.path.insert(0, str(sandbox_src))
    try:
        module = importlib.import_module("audit.database")
    finally:
        if sys.path and sys.path[0] == str(sandbox_src):
            sys.path.pop(0)
    return module.AuditDatabase, mode, sandbox_src


def test_real_store_integration(fx: dict) -> None:
    """真实存储层：补丁适配后的 ``AuditDatabase`` 幂等入库、覆盖落库与**重放不抹账**。"""
    print("\n[9] 真实存储层（AuditDatabase）幂等入库、覆盖落库、重放不抹账")
    try:
        db_cls, mode, sandbox = _load_audit_database()
    except Exception as exc:  # noqa: BLE001 - 结构性失败必须暴露，不得 SKIP
        check("真实存储层可用（补丁可应用或已应用）", False, exc)
        return
    print(f"  证据来源：{mode}")
    tmp = Path(tempfile.mkdtemp(prefix="mta-u01-store-"))
    db = db_cls(tmp / "store.sqlite")
    try:
        check("迁移版本 >= 17（收入表已建）", db.current_schema_version() >= 17,
              db.current_schema_version())
        window = fx["inputs"]["windows"][0]
        sync = _sync_window(window)
        book = IncomeReconciliationBook(
            store=db, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
        check("真实库首次写入 4 行", book.persist(sync).inserted == 4)
        check("真实库重复写入新增 0 行", book.persist(sync).inserted == 0)
        check("真实库已入库键可回读 4 条", len(book.load_existing()) == 4,
              book.load_existing())
        coverage = db.fetch_income_coverage(
            environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
            income_type="FUNDING_FEE",
            start_ms=window["start_ms"], end_ms=window["end_ms"])
        check("真实库覆盖证据 COMPLETE（含终止证明）",
              coverage is not None and coverage["coverage_status"] == "COMPLETE"
              and coverage["termination_proof_json"] not in (None, "", "null"), coverage)
        check("真实库环境域大小写归一（DEMO 可查到）",
              len(db.fetch_income_events(environment="DEMO",
                                        account_ref="SYNTH_ACCT_PLACEHOLDER")) == 4)

        # 【P0-2 核心】真实库回读 -> 重放：必须判 duplicates（不是 ID 冲突）、
        # 事件集不得缩水、已入账行不得被抹掉、金额不得丢失。
        before = db.fetch_income_events(
            environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
        replay = sync_income_window(
            _page_fetcher(window["pages"]),
            environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
            income_type=window["income_type"],
            start_ms=window["start_ms"], end_ms=window["end_ms"],
            page_limit=2, received_at_ms=1789870900000,
            existing=book.load_existing(),   # 键 -> content_hash 映射，直接回灌
        )
        check("重放不崩溃且全部判为 duplicates",
              replay.insert.inserted == () and replay.insert.quarantined == ()
              and len(replay.insert.duplicates) == 4,
              (replay.insert.inserted, replay.insert.duplicates))
        check("重放不抹账：窗口事件集仍为 4 且与库内一致",
              len(replay.events) == 4
              and {e.tran_id for e in replay.events}
              == {row["tran_id"] for row in before},
              [(e.tran_id, e.amount) for e in replay.events])
        check("重放金额真值不变（Σ = -1.70）",
              sum((Decimal(e.amount) for e in replay.events), Decimal(0))
              == Decimal("-1.70000000"),
              sum((Decimal(e.amount) for e in replay.events), Decimal(0)))
        check("重放后库内行数不减", len(db.fetch_income_events(
            environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")) == 4)
        check("补丁 content_hash 与 K03 单一真值源一致（否则重放会被误判 ID 冲突）",
              all(row["content_hash"] == income_content_hash(
                  _event_from_storage_row(dict(row)))
                  for row in db.fetch_income_events(
                      environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")),
              [row["content_hash"] for row in
               db.fetch_income_events(environment="demo",
                                      account_ref="SYNTH_ACCT_PLACEHOLDER")])

        # 真实库上的冲突隔离：既不落事件行，也留下可审计的冲突行（P1-7）
        conflict_sync = sync_income_window(
            _page_fetcher([list(window["redelivery"]["rows"])]),
            environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER",
            income_type=window["income_type"],
            start_ms=window["start_ms"], end_ms=window["end_ms"],
            page_limit=2, received_at_ms=1789871000000,
            existing=book.load_existing(),   # 库内 9002 为 -0.50，重投为 -2.50
        )
        check("真实库上同键不同内容判为隔离",
              len(conflict_sync.insert.quarantined) == 1,
              conflict_sync.insert.quarantined)
        report = book.persist(conflict_sync)
        check("真实库冲突行落库可审计",
              len(report.conflicts) == 1
              and len(db.fetch_income_conflicts(
                  environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")) == 1,
              db.fetch_income_conflicts(
                  environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER"))
        check("冲突行未污染事件表",
              len(db.fetch_income_events(environment="demo",
                                         account_ref="SYNTH_ACCT_PLACEHOLDER")) == 4)
    finally:
        db.close()
        if sandbox is not None:
            shutil.rmtree(sandbox, ignore_errors=True)


def main() -> int:
    fx = _fixture()
    test_inclusive_endpoint_mapping()
    sync_state = test_idempotency_and_pagination(fx)
    test_zero_fee_and_unknown(fx)
    test_attribution_and_conservation(fx, sync_state["sync2"])
    test_late_income_revision(fx)
    test_session_reconciliation(fx, sync_state["sync2"])
    test_persistence_idempotency(fx, sync_state["sync"], sync_state["sync2"])
    test_normalization_rejects_missing()
    test_real_store_integration(fx)
    test_persistence_profile_binding(fx, sync_state["sync"])
    test_coverage_window_queries(fx, sync_state["sync"])
    test_cross_page_defenses(fx)
    test_allocation_quantum_conflict()
    test_fee_cross_check(fx)
    test_late_income_content_change(fx)
    test_attribution_unconverted_and_missing(fx, sync_state["sync"])
    test_income_type_governance()
    test_session_input_edges(fx)
    print(f"\nU01 income reconciliation: PASS={PASS_COUNT} FAIL={len(FAILURES)}")
    for item in FAILURES:
        print(f"  - {item}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
