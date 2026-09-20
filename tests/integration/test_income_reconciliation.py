"""U01 资金费流水与成本闭环的离线集成测试（可直接运行，无需 pytest）。

运行：``python tests/integration/test_income_reconciliation.py``
期望值全部来自 ``tests/fixtures/income/f10_window_sync.json`` 的 ``derived_golden``，
该文件由 K03 规格与数学守恒独立推导，不回放被测实现输出。
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.contracts import IncomeEvent, idempotency_key_text  # noqa: E402
from exchange.income_reconciliation import (  # noqa: E402
    DEFAULT_PAGE_LIMIT,
    INCOME_TYPE_COMMISSION,
    REASON_PAGE_NOT_SORTED,
    REASON_PAGINATION_STALLED,
    InvalidIncomeRow,
    LateIncomeLedger,
    IncomeReconciliationBook,
    allocate_funding_to_lifecycles,
    assert_no_double_count,
    attribute_funding,
    bounds_from_inclusive,
    build_income_query,
    canonical_income_type,
    dedupe_key_texts,
    idempotency_keys,
    inclusive_bounds,
    normalize_income_row,
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
    """最小存储替身：验证幂等入库与覆盖落库行为（不碰真实数据库）。"""

    def __init__(self) -> None:
        self.events: dict[tuple, dict] = {}
        self.coverage: dict[tuple, dict] = {}

    def insert_income_events(self, rows) -> int:
        inserted = 0
        for row in rows:
            key = (
                str(row["environment"]).lower(), str(row["account_ref"]),
                str(row["income_type"]), str(row["tran_id"]),
            )
            if key in self.events:
                continue
            self.events[key] = dict(row)
            inserted += 1
        return inserted

    def fetch_income_events(self, *, environment: str, account_ref: str) -> list[dict]:
        return [
            row for key, row in self.events.items()
            if key[0] == environment and key[1] == account_ref
        ]

    def upsert_income_coverage(self, row) -> None:
        key = (
            str(row["environment"]), str(row["account_ref"]),
            str(row["income_type"]), int(row["start_ms"]), int(row["end_ms"]),
        )
        self.coverage[key] = dict(row)

    def fetch_income_coverage(self, *, environment: str, account_ref: str, income_type: str):
        for key, row in self.coverage.items():
            if key[0] == environment and key[1] == account_ref and key[2] == income_type:
                return row
        return None


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
    golden = fx["derived_golden"]

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


def test_persistence_idempotency(fx: dict, sync) -> None:
    print("\n[7] 持久化幂等：重复拉取不重复入账")
    store = MemoryIncomeStore()
    book = IncomeReconciliationBook(
        store=store, environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER")
    first = book.persist(sync)
    second = book.persist(sync)
    check("首次写入 4 行", first == 4, first)
    check("重复写入新增 0 行", second == 0, second)
    check("存储中唯一 4 行", len(store.events) == 4, len(store.events))
    coverage = store.fetch_income_coverage(
        environment="demo", account_ref="SYNTH_ACCT_PLACEHOLDER", income_type="FUNDING_FEE")
    check("覆盖证据落库且为 COMPLETE",
          coverage is not None and coverage["coverage_status"] == "COMPLETE", coverage)
    check("覆盖证据回带结构化终止证明",
          isinstance(coverage["termination_proof"], dict), coverage["termination_proof"])
    loaded = book.load_existing()
    check("已入库键可回读用于幂等判定", len(loaded) == 4, loaded)


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


def main() -> int:
    fx = _fixture()
    test_inclusive_endpoint_mapping()
    sync_state = test_idempotency_and_pagination(fx)
    test_zero_fee_and_unknown(fx)
    test_attribution_and_conservation(fx, sync_state["sync2"])
    test_late_income_revision(fx)
    test_session_reconciliation(fx, sync_state["sync2"])
    test_persistence_idempotency(fx, sync_state["sync"])
    test_normalization_rejects_missing()
    print(f"\nU01 income reconciliation: PASS={PASS_COUNT} FAIL={len(FAILURES)}")
    for item in FAILURES:
        print(f"  - {item}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
