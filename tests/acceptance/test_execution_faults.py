"""U04 执行故障验收：部分成交守恒、UNKNOWN 有界升级、保护随真实仓位。

独立可运行脚本（本项目不使用 pytest）：``python tests/acceptance/test_execution_faults.py``。
全部离线：临时目录建临时 SQLite，不读真实 ``.env``/``data/``，不构造真实 Broker，
不发网络请求，不下单/撤单。golden 期望来自 ``tests/fixtures/execution/`` 中由公式与
交易所语义独立推导的 ``derived_golden``，不是被测实现的输出回放。

覆盖：F01、F02、F03、F04、F21；DC04、DC13；R07、R08、R10。
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
import threading
import traceback
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.contracts import DuplicateFillError, OrderQuantity, QuantityError  # noqa: E402
from core.contracts.k04_management import REASON_LAST_PROTECTION  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from exchange.event_models import ExchangeEvent  # noqa: E402
from exchange.order_outbox import (  # noqa: E402
    DuplicateOrderSubmission,
    OrderOutbox,
)
from exchange.protection_commands import (  # noqa: E402
    REASON_CANCEL_NOT_EXECUTED,
    REASON_FLAT_CLOSE_NOT_CONFIRMED,
    REASON_FLAT_NOT_CONFIRMED,
    REASON_HEALTH_NO_FLAT_CLOSE,
    REASON_PARTIAL_FILL_PRESENT,
    CommandKind,
    CommandKindConflict,
    CrossKindCoverageClaim,
    FaultClass,
    ProtectionSafetyError,
    ProtectionSyncReasons,
    QuantizationInputError,
    QuantityStateConflict,
    SubmissionOutcome,
    UnknownDecision,
    apply_late_fill,
    assert_kind_coverage,
    can_cancel_protection,
    cancel_then_late_fill_probe,
    classify_execution_fault,
    classify_row,
    coverage_report,
    defers_flat_close,
    ensure_execution_schema,
    execute_protection_plan,
    execute_unknown_reconcile,
    execution_state_flags,
    guard_fault_action,
    guard_state_flags_unchanged,
    parse_qty,
    parse_timestamp_ms,
    plan_protection_refresh,
    plan_unknown_reconcile,
    protection_view_from_algo,
    read_exchange_order,
    read_submission_result,
    real_position_from_snapshot,
)
from exchange.protection_mirror import ProtectionOrderMirror  # noqa: E402


FIXTURES = ROOT / "tests" / "fixtures" / "execution"
PASSED = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"  FAIL  {name}  {detail}")


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _is_raised(exc_type: type[BaseException], fn: Any) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 - 其它异常也算未命中期望类型
        return False
    return False


def row_quantity_violations(row: Any) -> tuple[str, ...] | str:
    """把 ``order_outbox`` 行重水化成 ``OrderQuantity`` 并返回违规项。

    落库的行必须**永远**能被契约自身接受（DC04/K04）。返回 ``()`` 表示合法；
    返回字符串表示连构造都失败（如状态词表外的原样交易所状态）。
    """
    try:
        quantity = OrderQuantity.checked(
            order_id=str(row["exchange_order_id"] or row["client_order_id"]),
            accepted_qty=Decimal(str(row["accepted_qty_text"])),
            cumulative_filled_qty=Decimal(str(row["cumulative_filled_qty_text"])),
            working_leaves_qty=Decimal(str(row["working_leaves_qty_text"])),
            terminal_unfilled_qty=Decimal(str(row["terminal_unfilled_qty_text"] or "0")),
            status=str(row["status"]),
            revision=int(row["quantity_revision"] or 0),
        )
    except QuantityError as exc:
        return f"QuantityError: {exc}"
    except Exception as exc:  # noqa: BLE001 - pydantic 校验失败也要如实报出
        return f"{type(exc).__name__}: {exc}"
    return quantity.violations()


class RecordingBroker:
    """只记录调用、绝不下单的假 Broker；用调用记录证明"只查不发"。"""

    def __init__(self, *, standard: Any = None, algo: Any = None) -> None:
        self.standard_result = standard
        self.algo_result = algo
        self.calls: list[tuple[str, str, str]] = []
        self.submit_calls: list[dict[str, Any]] = []
        self.close_all_calls = 0

    def query_order(self, symbol: str, client_order_id: str) -> Any:
        self.calls.append(("query_order", str(symbol), str(client_order_id)))
        if isinstance(self.standard_result, Exception) or self.standard_result is None:
            raise self.standard_result or LookupError("not found")
        return dict(self.standard_result)

    def query_algo_order(self, client_algo_id: str) -> Any:
        self.calls.append(("query_algo_order", "", str(client_algo_id)))
        if isinstance(self.algo_result, Exception) or self.algo_result is None:
            raise self.algo_result or LookupError("not found")
        return dict(self.algo_result)

    def submit_order(self, **kwargs: Any) -> dict[str, Any]:
        """真实路径可能存在的提交入口；本模块任何流程都不得调用它。"""
        self.submit_calls.append(dict(kwargs))
        raise AssertionError("U04 收口流程不得提交订单")

    def close_all(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.close_all_calls += 1
        raise AssertionError("健康故障不得触发全平")


def section_dc04_partial_fill() -> None:
    """[1][2] DC04/F02/F21：三分量守恒、Decimal 精度、终态迟到成交修订。"""
    fixture = load_fixture("dc04_partial_fill_conservation.json")
    inputs = fixture["inputs"]
    golden = fixture["derived_golden"]
    print("\n[1] DC04 三分量守恒与终态迟到成交（F02/F21）")

    first_fill = inputs["fills"][0]
    working = read_exchange_order({
        "orderId": inputs["order_id"], "status": "NEW",
        "origQty": inputs["requested_qty"], "executedQty": "0",
    }).quantity
    step1 = apply_late_fill(
        working, fill_qty=first_fill["qty"], fill_id=first_fill["fill_id"],
    ).quantity
    after_first = golden["after_first_fill"]
    check("首笔部分成交后分数与状态与夹具一致",
          step1.status == after_first["status"]
          and str(step1.cumulative_filled_qty) == after_first["filled"]
          and str(step1.working_leaves_qty) == after_first["working_leaves"]
          and str(step1.terminal_unfilled_qty) == after_first["terminal_unfilled"],
          f"actual={step1.to_components()}")
    check("首笔后三分量守恒精确成立",
          step1.accepted_qty == step1.cumulative_filled_qty
          + step1.working_leaves_qty + step1.terminal_unfilled_qty)
    check("首笔后无守恒/状态违规", step1.violations() == (), str(step1.violations()))

    pending, confirmed, revision = cancel_then_late_fill_probe(
        step1, late_fill_qty=inputs["lifecycle_events"][2]["fill"]["qty"],
        fill_id=inputs["lifecycle_events"][2]["fill"]["fill_id"],
    )
    after_request = golden["after_cancel_request"]
    check("撤单确认前 working 未提前计入 terminal",
          str(pending.terminal_unfilled_qty) == after_request["terminal_unfilled"]
          and str(pending.working_leaves_qty) == after_request["working_leaves"],
          f"actual={pending.to_components()}")
    after_confirm = golden["after_cancel_confirmed"]
    check("撤单确认后余量转入 terminal 且状态为 CANCELED",
          confirmed.status == after_confirm["status"]
          and str(confirmed.terminal_unfilled_qty) == after_confirm["terminal_unfilled"]
          and str(confirmed.working_leaves_qty) == after_confirm["working_leaves"],
          f"actual={confirmed.to_components()}")
    after_late = golden["after_late_fill"]
    check("CANCELED 后迟到成交仍修订累计成交（终态可修正）",
          str(revision.quantity.cumulative_filled_qty) == after_late["filled"]
          and str(revision.quantity.terminal_unfilled_qty) == after_late["terminal_unfilled"]
          and revision.quantity.status == after_late["status"],
          f"actual={revision.quantity.to_components()}")
    check("迟到成交被标记为 revision_only（只改事实不改状态）",
          revision.revision_only and revision.reason_codes == ("TERMINAL_REVISION_ONLY",))
    check("迟到成交后三分量仍守恒",
          revision.quantity.accepted_qty == revision.quantity.cumulative_filled_qty
          + revision.quantity.working_leaves_qty
          + revision.quantity.terminal_unfilled_qty)
    check("迟到成交推高 revision 序号", revision.quantity.revision > confirmed.revision)

    replay = inputs["replayed_fill"]
    applied_ids = [first_fill["fill_id"], revision.fill_id]
    duplicated = _is_raised(
        DuplicateFillError,
        lambda: apply_late_fill(
            revision.quantity, fill_qty=replay["qty"], fill_id=replay["fill_id"],
            applied_fill_ids=applied_ids),
    )
    check("重复 fill_id 被幂等拒绝（不重复累计成交）", duplicated,
          "DuplicateFillError 未抛出")

    print("\n[2] 0.01 -> 0.0047 的 Decimal 精度（禁止浮点近似）")
    trap = inputs["float_trap"]
    float_terminal = float("0.01") - float("0.0047") - float("0.0047")
    check("浮点累减得不到精确余量（夹具陷阱成立）",
          Decimal(repr(float_terminal)) != Decimal(trap["expected_exact"])
          and repr(float_terminal) == repr(trap["binary_float_result"]),
          f"float={float_terminal!r}")
    check("Decimal 勾稽终态余量精确得到 0.0006",
          revision.quantity.terminal_unfilled_qty == Decimal(trap["expected_exact"])
          and str(revision.quantity.terminal_unfilled_qty) == trap["expected_exact"],
          f"actual={revision.quantity.terminal_unfilled_qty!r}")
    decimal_delta = step1.accepted_qty - step1.cumulative_filled_qty
    check("Decimal 勾稽精确得到 0.0053",
          decimal_delta == Decimal(after_first["working_leaves"])
          and str(decimal_delta) == after_first["working_leaves"],
          f"actual={decimal_delta!r}")
    check("成交数量用 Decimal 表示且无指数/尾零漂移",
          str(step1.cumulative_filled_qty) == "0.0047"
          and step1.cumulative_filled_qty == Decimal(after_first["filled"]))
    check("浮点数量入参被拒绝（fail-closed 到十进制文本）",
          _is_raised(ValueError, lambda: parse_qty(0.0047, field="fill_qty")))
    check("四舍五入不会把 0.0047 归零",
          parse_qty("0.0047", field="fill_qty") == Decimal("0.0047")
          and parse_qty("0.0047", field="fill_qty") != 0)


def section_dc13_unknown(tmpdir: Path) -> None:
    """[3][4] DC13/F01/F04：UNKNOWN 只查单不重发、有界升级、重启断点。"""
    fixture = load_fixture("dc13_unknown_no_resubmit.json")
    inputs = fixture["inputs"]
    submission = inputs["submission"]
    variant_a = inputs["variants"][0]["expected"]
    variant_b = inputs["variants"][1]["expected"]
    variant_c = inputs["variants"][2]
    client_id = inputs["intent"]["client_order_id"]
    print("\n[3] DC13 UNKNOWN 只查单、不重发、有界升级（F01）")

    db = AuditDatabase(tmpdir / "unknown.sqlite")
    outbox = OrderOutbox(db, environment=inputs["environment"])
    outbox.prepare(
        client_order_id=client_id, symbol=inputs["symbol"], side="BUY",
        position_side="LONG", order_type="MARKET", purpose="ENTRY",
        qty=float(inputs["intent"]["qty"]), price=None, reduce_only=False,
        endpoint=inputs["intent"]["endpoint"],
        request={"symbol": inputs["symbol"], "newClientOrderId": client_id},
    )
    outbox.mark_unknown(client_id, RuntimeError(submission["client_outcome"]))
    row = db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("503/响应丢失后进入 UNKNOWN", str(row["status"]) == "UNKNOWN")
    check("SUBMITTING 与 UNKNOWN 都不是成交状态",
          str(row["status"]) not in {"FILLED", "PARTIALLY_FILLED"})

    broker = RecordingBroker(standard={
        "orderId": "SYNTH_ORDER_A1", **variant_a_fields(inputs, variant_a),
    })
    # 用记录自身的 unknown_since 推进时钟：真实 mark_unknown 写入的是墙钟时间，
    # 固定夹具时间戳会落在退避窗口内，测不到"到期查单"这条路径。
    now_ms = int(parse_timestamp_ms(row["unknown_since"]) or 0) + 1000
    report = execute_unknown_reconcile(
        [row], broker=broker, outbox=outbox, now_ms=now_ms,
        budget=inputs["query_policy"]["budget"],
        manual_after_ms=inputs["query_policy"]["manual_after_ms"],
    )
    check("只按原 clientOrderId 查询（查询集合等于原 ID）",
          {call[2] for call in broker.calls} == {client_id}
          and variant_a["adopted_client_order_id"] == client_id)
    check("整个恢复过程没有任何提交调用",
          report.submit_calls == 0 and broker.submit_calls == [],
          f"submit_calls={report.submit_calls}")
    check("恢复后记录被采纳交易所回报",
          str(db.fetch_one(
              "SELECT status FROM order_outbox WHERE client_order_id=?",
              (client_id,))["status"]) == variant_a["outcome"],
          variant_a["outcome"])
    adopted = read_exchange_order(adopted_payload(inputs, variant_a))
    check("采纳回报的三分量与夹具一致",
          str(adopted.quantity.cumulative_filled_qty) == variant_a["filled"]
          and str(adopted.quantity.working_leaves_qty) == variant_a["working_leaves"]
          and str(adopted.quantity.terminal_unfilled_qty) == variant_a["terminal_unfilled"],
          f"actual={adopted.quantity.to_components()}")
    check("采纳回报的成交被确认为真实成交",
          read_submission_result(adopted_payload(inputs, variant_a)).fill_confirmed)

    unqueryable_id = "synth-u04-unk-bounded"
    outbox.prepare(
        client_order_id=unqueryable_id, symbol=inputs["symbol"], side="BUY",
        position_side="LONG", order_type="MARKET", purpose="EXIT", qty=3.0,
        price=None, reduce_only=True, endpoint=inputs["intent"]["endpoint"],
        request={"symbol": inputs["symbol"], "newClientOrderId": unqueryable_id},
    )
    outbox.mark_unknown(unqueryable_id, TimeoutError("lost"))
    bounded = dict(db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (unqueryable_id,)))
    bounded["query_attempt_count"] = inputs["query_policy"]["budget"]
    plan_b = plan_unknown_reconcile(
        bounded, now_ms=now_ms,
        budget=inputs["query_policy"]["budget"],
        manual_after_ms=inputs["query_policy"]["manual_after_ms"])
    check("查询预算耗尽转 MANUAL_RECOVERY 并告警",
          plan_b.decision is UnknownDecision.MANUAL_RECOVERY
          and plan_b.alert_required
          and plan_b.escalation == variant_b["escalation"],
          f"actual={plan_b}")
    check("MANUAL_RECOVERY 仍阻断新开仓且禁止重发/换 ID",
          plan_b.blocks_new_open and not plan_b.repost_allowed
          and not plan_b.new_client_id_allowed and plan_b.later_requery_allowed)
    check("预算耗尽后记录仍保留且状态未被猜成终态",
          bool(variant_b["outbox_row_retained"])
          and db.fetch_one(
              "SELECT COUNT(*) AS n FROM order_outbox WHERE client_order_id=?",
              (unqueryable_id,))["n"] == 1
          and str(db.fetch_one(
              "SELECT status FROM order_outbox WHERE client_order_id=?",
              (unqueryable_id,))["status"]) == "UNKNOWN")

    retry_at_ms = parse_timestamp_ms(variant_c["next_retry_at"])
    plan_c = plan_unknown_reconcile(
        {"client_order_id": client_id, "endpoint": inputs["intent"]["endpoint"],
         "status": "UNKNOWN", "query_attempt_count": 1,
         "next_retry_at": retry_at_ms, "unknown_since": retry_at_ms - 60000},
        now_ms=retry_at_ms - 1, budget=inputs["query_policy"]["budget"])
    check("退避期内判定 BACKOFF 且仍禁止重发",
          plan_c.decision is UnknownDecision.BACKOFF and not plan_c.repost_allowed
          and plan_c.blocks_new_open, f"actual={plan_c.decision}")
    plan_due = plan_unknown_reconcile(
        {"client_order_id": client_id, "endpoint": inputs["intent"]["endpoint"],
         "status": "UNKNOWN", "query_attempt_count": 1},
        now_ms=retry_at_ms, budget=inputs["query_policy"]["budget"])
    check("到期后的查询入口是普通订单查询",
          plan_due.decision is UnknownDecision.QUERY_DUE
          and plan_due.query_api == "query_order")
    check("人工收口不允许猜成 FILLED",
          _is_raised(ValueError, lambda: outbox.manual_resolve_unknown(
              client_id, status="FILLED", operator="reviewer",
              evidence="synthetic")))
    check("假 Broker 的全平入口从未被调用", broker.close_all_calls == 0)

    # A/P3-1：脏的查询计数列（'abc'/'3.0'）不得抛裸 ValueError 而中断整条收口规划。
    # 解析不了计数时最不该走的方向是"当作 0 次、无限重试"，因此按预算耗尽处理。
    for dirty in ("abc", "-1", "1e3"):
        dirty_plan = plan_unknown_reconcile(
            {"client_order_id": client_id, "endpoint": inputs["intent"]["endpoint"],
             "status": "UNKNOWN", "query_attempt_count": dirty},
            now_ms=retry_at_ms, budget=inputs["query_policy"]["budget"])
        check(f"脏计数 {dirty!r} 不抛异常且 fail-closed 到 MANUAL_RECOVERY",
              dirty_plan.decision is UnknownDecision.MANUAL_RECOVERY
              and dirty_plan.blocks_new_open and dirty_plan.alert_required,
              f"actual={dirty_plan.decision} attempts={dirty_plan.attempt_no}")
    # 十进制形式的计数（'3.0'）本身合法：只要求不再抛裸 ValueError，且不因此放宽预算。
    decimal_count = plan_unknown_reconcile(
        {"client_order_id": client_id, "endpoint": inputs["intent"]["endpoint"],
         "status": "UNKNOWN", "query_attempt_count": "3.0"},
        now_ms=retry_at_ms, budget=inputs["query_policy"]["budget"])
    check("十进制计数 '3.0' 被解析为 3 而不是抛 ValueError",
          decimal_count.attempt_no == 3
          and decimal_count.decision in {
              UnknownDecision.QUERY_DUE, UnknownDecision.BACKOFF,
              UnknownDecision.MANUAL_RECOVERY},
          f"actual={decimal_count.decision} attempts={decimal_count.attempt_no}")

    print("\n[4] F04 重启断点：SUBMITTING -> UNKNOWN，不重发（F04）")
    db2 = AuditDatabase(tmpdir / "restart.sqlite")
    outbox2 = OrderOutbox(db2, environment=inputs["environment"])
    crash_id = "synth-u04-crash-0001"
    outbox2.prepare(
        client_order_id=crash_id, symbol=inputs["symbol"], side="BUY",
        position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=3.0,
        price=None, reduce_only=False, endpoint=inputs["intent"]["endpoint"],
        request={"symbol": inputs["symbol"], "newClientOrderId": crash_id},
    )
    recovered = outbox2.recover_stale_submitting()
    crash_row = db2.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (crash_id,))
    check("重启把悬空 SUBMITTING 转成 UNKNOWN 且仅一条",
          recovered == 1 and str(crash_row["status"]) == "UNKNOWN")
    crash_plan = plan_unknown_reconcile(
        crash_row, now_ms=int(parse_timestamp_ms(crash_row["unknown_since"]) or 0) + 1000)
    check("重启后只规划按原 ID 查单，不重发",
          crash_plan.decision is UnknownDecision.QUERY_DUE
          and crash_plan.query_api == "query_order"
          and not crash_plan.repost_allowed)
    check("同一 clientOrderId 被数据库幂等约束阻断再次提交",
          _is_raised(DuplicateOrderSubmission, lambda: outbox2.prepare(
              client_order_id=crash_id, symbol=inputs["symbol"], side="BUY",
              position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=3.0,
              price=None, reduce_only=False, endpoint=inputs["intent"]["endpoint"],
              request={"symbol": inputs["symbol"], "newClientOrderId": crash_id})))
    db.close()
    db2.close()


def variant_a_fields(inputs: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    """夹具变体 A 的查询回报字段（来自夹具，不在测试里另编数值）。"""
    return {
        "status": expected["outcome"],
        "origQty": inputs["intent"]["qty"],
        "executedQty": expected["filled"],
        "avgPrice": "100.00",
        "clientOrderId": inputs["intent"]["client_order_id"],
    }


def adopted_payload(inputs: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    return variant_a_fields(inputs, expected)


def section_f03_protection(tmpdir: Path) -> None:
    """[5] F03/F02：保护随真实仓位、不撤最后一道保护、先挂新后撤旧。"""
    fixture = load_fixture("f03_protection_follows_position.json")
    inputs = fixture["inputs"]
    golden = fixture["derived_golden"]
    print("\n[5] F03 保护随真实仓位更新、不撤最后一道保护（F03/F02）")

    protections = [protection_view_from_algo(row) for row in inputs["protective_orders"]]
    position = real_position_from_snapshot(
        inputs["position_after_partial_reduce"], revision=43)
    check("真实仓位解析为 2.0 且方向为 LONG",
          position.abs_qty == Decimal("2.0") and position.close_side == "SELL")
    desired = inputs["desired_after_partial_reduce"]
    plan = plan_protection_refresh(
        position=position, protections=protections,
        desired_sl=desired["desired_sl"], desired_tp=desired["desired_tp"])

    target = golden["after_partial_reduce_to_2_0"]
    check("保护目标数量随真实仓位变为 2.0",
          plan.target_qty == Decimal(target["target_qty"]))
    check("closePosition 型 SL 被保留（自动覆盖当前数量）",
          [a.client_id for a in plan.keeps] == target["keep"],
          f"keeps={[a.client_id for a in plan.keeps]}")
    check("数量过期的 TP 被替换为新保护",
          [a.order_type for a in plan.places] == target["place"]
          and plan.places[0].qty == Decimal(target["target_qty"]),
          f"places={[a.to_dict() for a in plan.places]}")
    check("旧 TP 被撤销", [a.client_id for a in plan.cancels] == target["cancel"])
    check("PLACE 严格排在 CANCEL 之前（先建新后撤旧）",
          [a.action for a in plan.ordered()][0] == "PLACE"
          and [a.action for a in plan.ordered()].index("CANCEL")
          > max(i for i, a in enumerate(plan.ordered()) if a.action == "PLACE"))
    check("计划无阻塞原因且可执行", plan.safe_to_execute and not plan.blocked_reason)

    allowed, reason = can_cancel_protection(
        position=position, protections=[protections[0]], target_client_id="SYNTH_SL_03")
    check("拒绝撤销最后一道保护",
          not allowed and reason == golden["remove_last_protection"]["reason_code"],
          f"allowed={allowed} reason={reason}")

    flat_unconfirmed = real_position_from_snapshot(inputs["position_flat_unconfirmed"])

    # A/P1-A1：positionAmt==0 只是"快照为零"，不等于"已确认归零"。把 is_flat 当
    # 已确认会允许撤掉最后一道保护，并在 reason 里谎称 FLAT_CONFIRMED_CLEANUP。
    for probe_key in ("position_flat_unconfirmed",
                      "position_flat_unconfirmed_zero_text"):
        if probe_key not in inputs:
            continue
        probe_position = real_position_from_snapshot(inputs[probe_key])
        probe_allowed, probe_reason = can_cancel_protection(
            position=probe_position, protections=[protections[0]],
            target_client_id="SYNTH_SL_03")
        check(f"未确认归零（{probe_key}）不得撤掉最后一道保护、不得谎报已确认",
              not probe_allowed
              and probe_reason in {REASON_LAST_PROTECTION, REASON_FLAT_NOT_CONFIRMED}
              and probe_reason != "FLAT_CONFIRMED_CLEANUP",
              f"allowed={probe_allowed} reason={probe_reason}")
        # 同口径：与 plan_protection_refresh 对同一情形都拒绝。
        probe_plan = plan_protection_refresh(
            position=probe_position, protections=protections, flat_confirmed=False)
        check(f"未确认归零（{probe_key}）与 plan_protection_refresh 同口径拒绝",
              probe_plan.blocked_reason == REASON_FLAT_NOT_CONFIRMED
              and not probe_plan.cancels,
              f"blocked={probe_plan.blocked_reason} cancels={probe_plan.cancels}")
        # 已确认归零才允许清理，reason 才允许是 FLAT_CONFIRMED_CLEANUP。
        confirmed_allowed, confirmed_reason = can_cancel_protection(
            position=probe_position, protections=[protections[0]],
            target_client_id="SYNTH_SL_03", flat_confirmed=True)
        check(f"确认归零（{probe_key}）才允许清理且 reason 如实标注",
              confirmed_allowed and confirmed_reason == "FLAT_CONFIRMED_CLEANUP",
              f"allowed={confirmed_allowed} reason={confirmed_reason}")

    # N3：``flat_confirmed`` 只能是**严格布尔** ``True``。"已确认归零"会直接授权清理
    # 最后一道保护，不允许靠 Python 真值转换蒙对；非布尔真值（'false'/'true'/1/[0]/…）
    # 一律按"未确认"fail-closed，且不得谎报 FLAT_CONFIRMED_CLEANUP。
    for bogus in ("false", "true", "0", 1, 1.0, [0], (0,), {"a": 0}):
        bogus_allowed, bogus_reason = can_cancel_protection(
            position=flat_unconfirmed, protections=[protections[0]],
            target_client_id="SYNTH_SL_03", flat_confirmed=bogus)
        bogus_plan = plan_protection_refresh(
            position=flat_unconfirmed, protections=protections, flat_confirmed=bogus)
        check(f"非布尔 flat_confirmed={bogus!r} 不得授权清理（fail-closed）",
              not bogus_allowed and bogus_reason != "FLAT_CONFIRMED_CLEANUP"
              and bogus_plan.blocked_reason == REASON_FLAT_NOT_CONFIRMED
              and not bogus_plan.cancels,
              f"allowed={bogus_allowed} reason={bogus_reason} "
              f"blocked={bogus_plan.blocked_reason} cancels={bogus_plan.cancels}")

    # A/P3-5：镜像写出的十进制文本列必须有真实读取方，否则"精确勾稽"是空承诺。
    text_view = protection_view_from_algo({
        "clientAlgoId": "SYNTH_SL_TEXT", "orderType": "STOP_MARKET",
        "quantity": 2.0, "triggerPrice": 100.5,
        "quantity_text": "2", "trigger_price_text": "100.5"})
    check("镜像文本列优先于浮点原列被读取（精确路径成立）",
          text_view.qty == Decimal("2") and text_view.trigger_price == Decimal("100.5"),
          f"actual={text_view}")
    check("文本列缺失时不静默接受浮点原值（仍拒绝浮点直传）",
          _is_raised(QuantizationInputError, lambda: protection_view_from_algo({
              "clientAlgoId": "X", "orderType": "STOP_MARKET",
              "quantity": 2.0, "triggerPrice": "100.5"})))
    plan_flat_pending = plan_protection_refresh(
        position=flat_unconfirmed, protections=protections, flat_confirmed=False)
    check("仓位归零未确认时不得清理保护",
          len(plan_flat_pending.cancels) == golden["flat_unconfirmed"]["cancel_actions"]
          and plan_flat_pending.blocked_reason
          == golden["flat_unconfirmed"]["blocked_reason"],
          f"actual={plan_flat_pending.to_dict()}")
    flat_confirmed = real_position_from_snapshot(inputs["position_flat_confirmed"])
    plan_flat_done = plan_protection_refresh(
        position=flat_confirmed, protections=protections, flat_confirmed=True)
    check("归零确认后允许清理遗留保护",
          len(plan_flat_done.cancels) == golden["flat_confirmed"]["cancel_actions"]
          and not plan_flat_done.blocked_reason)

    ops: list[str] = []

    def place(action: Any) -> str:
        ops.append(f"place:{action.order_type}")
        return f"synth-new-{action.order_type}"

    def cancel(action: Any) -> bool:
        ops.append(f"cancel:{action.client_id}")
        return True

    executed = execute_protection_plan(plan, place=place, cancel=cancel)
    check("成功路径先挂新后撤旧（调用顺序可复核）",
          ops == [f"place:{target['place'][0]}", f"cancel:{target['cancel'][0]}"],
          f"ops={ops}")
    check("成功路径确实挂新且撤旧各一次",
          len(executed.placed) == 1 and list(executed.canceled) == target["cancel"]
          and not executed.cancel_skipped and executed.cancel_failed == ()
          and executed.cancel_unexecuted_reason == "")

    # A/P2-A6：撤旧回调"什么也没做"（例如只知 clientAlgoId、匹配不到交易所
    # algoId 而无法发 DELETE）绝不能被记成"已撤销"，否则账本宣称旧保护已终结，
    # 而交易所侧旧保护仍在（重复保护 + 过期触发器仍会触发）。
    def noop_cancel(action: Any) -> None:
        ops.append(f"noop-cancel:{action.client_id}")
        return None

    ops.clear()
    noop = execute_protection_plan(plan, place=place, cancel=noop_cancel)
    check("撤旧回调未执行时记入 cancel_failed 而不是 canceled",
          noop.cancel_failed == tuple(target["cancel"]) and noop.canceled == ()
          and noop.cancel_unexecuted_reason == REASON_CANCEL_NOT_EXECUTED
          and not noop.cancel_skipped,
          f"actual={noop}")

    place_failure = golden["place_failure"]
    fail_order_type = inputs["place_failure_injection"]["fail_order_type"]

    def failing_place(action: Any) -> str:
        if action.order_type == fail_order_type:
            raise RuntimeError("synthetic place failure")
        ops.append(f"place:{action.order_type}")
        return "synth-new"

    ops.clear()
    failed = execute_protection_plan(plan, place=failing_place, cancel=cancel)
    check("新保护挂单失败时停止后续撤旧（cancel_skipped）",
          failed.cancel_skipped and len(failed.canceled) == place_failure["canceled"]
          and failed.failed_place == fail_order_type,
          f"actual={failed}")

    print("\n[5b] 保护镜像与真实仓位口径一致（组合验证）")
    db = AuditDatabase(tmpdir / "protection.sqlite")
    mirror = ProtectionOrderMirror(
        db, environment=inputs["environment"], allowed_symbols=[inputs["symbol"]])
    snapshot = mirror.reconcile_snapshot(
        [{"symbol": inputs["symbol"], "positionAmt": "2.0", "positionSide": "LONG"}],
        inputs["protective_orders"],
    )
    check("镜像识别出数量错配的 TP（5.0 != 2.0）",
          snapshot["qty_mismatch"] == ["8002"], f"actual={snapshot['qty_mismatch']}")
    check("镜像判定该仓位保护覆盖不一致（需要刷新）",
          snapshot["ok"] is False
          and snapshot["missing"] == [
              f"{inputs['symbol']}:{inputs['position_after_partial_reduce']['position_side']}"],
          f"actual={snapshot['missing']}")
    db.close()


def section_f21_kinds(tmpdir: Path) -> None:
    """[6] F21/R07：普通与 Algo 命令分账、分派查单、覆盖声明、不漏仓。"""
    fixture = load_fixture("f21_algo_standard_separation.json")
    inputs = fixture["inputs"]
    golden = fixture["derived_golden"]
    rows = inputs["ledger_rows"]
    print("\n[6] F21 普通命令与 Algo 命令分别持久化与查单（R07）")

    standard_row = dict(rows[0])
    algo_row = dict(rows[1])
    check("端点判定命令种类：普通", classify_row(standard_row) is CommandKind.STANDARD)
    check("端点判定命令种类：Algo", classify_row(algo_row) is CommandKind.ALGO)
    check("端点缺失时按 clientAlgoId 兜底识别为 Algo",
          classify_row({"client_order_id": "x", "client_algo_id": "y",
                        "status": "UNKNOWN"}) is CommandKind.ALGO)
    conflict = dict(inputs["kind_conflict_row"])
    check("command_kind 与端点矛盾时拒绝该记录",
          _is_raised(CommandKindConflict, lambda: classify_row(conflict)))
    check("未知端点不被默认当成普通订单",
          _is_raised(Exception, lambda: classify_row(
              {"client_order_id": "x", "endpoint": "/fapi/v1/unknown"})))

    plan_std = plan_unknown_reconcile(standard_row, now_ms=1789862406000)
    plan_algo = plan_unknown_reconcile(algo_row, now_ms=1789862406000)
    check("普通命令规划走 query_order",
          plan_std.query_api == golden["dispatch"]["standard_queried_via"])
    check("Algo 命令规划走 query_algo_order",
          plan_algo.query_api == golden["dispatch"]["algo_queried_via"])

    db = AuditDatabase(tmpdir / "kinds.sqlite")
    outbox = OrderOutbox(db, environment=inputs["environment"])
    for row in rows:
        outbox.prepare(
            client_order_id=row["client_order_id"], symbol=row["symbol"], side="BUY",
            position_side="LONG",
            order_type="MARKET" if row["command_kind"] == "STANDARD"
            else "STOP_MARKET",
            purpose="ENTRY" if row["command_kind"] == "STANDARD" else "PROTECTION",
            qty=1.0, price=None,
            reduce_only=row["command_kind"] != "STANDARD",
            endpoint=row["endpoint"], request={"newClientOrderId": row["client_order_id"]},
        )
        outbox.mark_unknown(row["client_order_id"], RuntimeError("HTTP_503"))
    live_rows = db.fetch_all(
        "SELECT * FROM order_outbox WHERE environment=? ORDER BY client_order_id",
        (inputs["environment"],))
    broker = RecordingBroker(
        standard=inputs["query_results"]["query_order"],
        algo=inputs["query_results"]["query_algo_order"],
    )
    now_ms = max(
        int(parse_timestamp_ms(r["unknown_since"]) or 0) for r in live_rows) + 1000
    report = execute_unknown_reconcile(
        live_rows, broker=broker, outbox=outbox, now_ms=now_ms)
    apis = {call[0] for call in broker.calls}
    check("分派查单只使用对应端点（无交叉调用）",
          apis == {"query_order", "query_algo_order"}
          and report.query_calls_by_api == {"query_order": 1, "query_algo_order": 1},
          f"calls={broker.calls}")
    check("普通命令按 symbol+clientOrderId 查询",
          ("query_order", inputs["symbol"], rows[0]["client_order_id"]) in broker.calls)
    check("Algo 命令只按 clientAlgoId 查询",
          ("query_algo_order", "", rows[1]["client_order_id"]) in broker.calls)
    check("查单过程零提交、零全平",
          report.submit_calls == 0 and broker.submit_calls == []
          and broker.close_all_calls == 0)
    check("两类命令都被各自端点收口为 NEW",
          {str(r["status"]) for r in db.fetch_all(
              "SELECT status FROM order_outbox WHERE environment=?",
              (inputs["environment"],))} == {"NEW"},
          "存在未收口记录")

    report_rows = [
        {"client_order_id": "a", "endpoint": "/fapi/v1/order", "status": "NEW",
         "unknown_since": None},
        {"client_order_id": "b", "endpoint": "/fapi/v1/algoOrder", "status": "UNKNOWN",
         "unknown_since": 1789862000000},
    ]
    coverage = coverage_report(report_rows, environment="demo", now_ms=1789862400000)
    check("覆盖报告按种类分开统计",
          coverage.kind(CommandKind.STANDARD).covered
          and not coverage.kind(CommandKind.ALGO).covered
          and coverage.mixed_kinds,
          f"report={coverage.to_dict()}")
    check("Algo 未知命令保留可观测年龄",
          coverage.kind(CommandKind.ALGO).oldest_unknown_age_ms == 400000)
    # A/P2-A5：SUBMITTING = "已 POST、结果未知"，保护是否存在并不确定，不能算已覆盖；
    # 词表外状态也不得被计成 terminal（那是 fail-open 方向）。
    submitting_only = coverage_report(
        [{"client_order_id": "s", "endpoint": "/fapi/v1/algoOrder",
          "status": "SUBMITTING", "unknown_since": None}], environment="demo")
    check("SUBMITTING 的保护命令不被声明为已覆盖",
          not submitting_only.kind(CommandKind.ALGO).covered
          and submitting_only.kind(CommandKind.ALGO).submitting == 1,
          f"actual={submitting_only.to_dict()}")
    check("词表外状态按未决计入（fail-closed），不冒充已覆盖",
          not coverage_report(
              [{"client_order_id": "q", "endpoint": "/fapi/v1/algoOrder",
                "status": "PARTIAL_UNKNOWN", "unknown_since": None}],
              environment="demo").kind(CommandKind.ALGO).covered)
    check("已确认存在的保护命令（NEW）仍算覆盖",
          coverage_report(
              [{"client_order_id": "n", "endpoint": "/fapi/v1/algoOrder",
                "status": "NEW", "unknown_since": None}],
              environment="demo").kind(CommandKind.ALGO).covered)
    check("用普通订单记录声明 Algo 已覆盖被拒绝",
          _is_raised(CrossKindCoverageClaim, lambda: assert_kind_coverage(
              inputs["coverage_claim"]["rows_offered_as_proof"],
              kind=CommandKind.ALGO, claimed_covered=True)))
    check("普通订单记录可以支撑普通订单覆盖声明",
          assert_kind_coverage(
              inputs["coverage_claim"]["rows_offered_as_proof"],
              kind=CommandKind.STANDARD, claimed_covered=True).covered)

    print("\n[6b] Algo 提交结果丢失后按 clientAlgoId 恢复且只 POST 一次")
    broker_obj = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker_obj.db = db
    broker_obj.order_outbox = outbox
    broker_obj.environment = inputs["environment"]
    posts: list[dict[str, Any]] = []

    def signed(method: str, path: str, params: dict[str, Any] | None = None,
               attempts: int = 2) -> dict[str, Any]:
        if method == "POST":
            posts.append(dict(params or {}))
            raise TimeoutError("algo response lost")
        return dict(inputs["query_results"]["query_algo_order"])

    broker_obj._signed = signed
    result = broker_obj._submit_algo_order(
        {"symbol": inputs["symbol"], "clientAlgoId": "synth-u04-algo-post",
         "side": "SELL", "type": "STOP_MARKET", "quantity": "1.0",
         "triggerPrice": "90.00"},
        purpose="PROTECTION",
    )
    check("响应丢失后按 clientAlgoId 采纳交易所事实",
          result.get("algoId") == inputs["query_results"]["query_algo_order"]["algoId"])
    check("Algo 恢复过程只 POST 一次",
          len(posts) == 1 and posts[0].get("clientAlgoId") == "synth-u04-algo-post")
    check("Algo 记录以 algoId 收口为 NEW",
          str(db.fetch_one(
              "SELECT status FROM order_outbox WHERE client_order_id=?",
              ("synth-u04-algo-post",))["status"]) == "NEW")
    db.close()


def section_submission_and_faults(tmpdir: Path) -> None:
    """[7][8] 请求成功 != 成交；健康故障不触发全平且不改 pause/emergency。"""
    fixture = load_fixture("f21_algo_standard_separation.json")
    inputs = fixture["inputs"]
    print("\n[7] 请求成功 != 成交")
    ack = read_submission_result({"orderId": 1, "status": "NEW"})
    check("仅有 orderId/状态 NEW 时判为已受理而非成交",
          ack.outcome is SubmissionOutcome.ACKNOWLEDGED
          and not ack.fill_confirmed and ack.filled_qty == 0,
          f"outcome={ack.outcome}")
    filled = read_submission_result({
        "orderId": 2, "status": "FILLED", "origQty": "0.01",
        "executedQty": "0.01", "avgPrice": "100.00"})
    check("终态 + 成交数量 + 可用成交价才算成交",
          filled.outcome is SubmissionOutcome.FILLED and filled.fill_confirmed
          and filled.filled_qty == Decimal("0.01"))
    no_price = read_submission_result({
        "orderId": 3, "status": "FILLED", "origQty": "0.01",
        "executedQty": "0.01", "avgPrice": "0"})
    check("成交价不可用时不得当成交（退回已受理并标注未确认）",
          no_price.outcome is SubmissionOutcome.ACKNOWLEDGED
          and not no_price.fill_confirmed
          and "FILL_UNCONFIRMED" in no_price.reason_codes,
          f"actual={no_price}")
    partial = read_submission_result({
        "orderId": 4, "status": "PARTIALLY_FILLED", "origQty": "0.01",
        "executedQty": "0.0047", "avgPrice": "100.00"})
    check("部分成交被确认为真实成交且工作余量精确",
          partial.outcome is SubmissionOutcome.PARTIALLY_FILLED
          and partial.fill_confirmed and partial.filled_qty == Decimal("0.0047"))
    # A/P2-A3：终态带成交与终态零成交必须可分——只读 outcome 的消费者不能把一笔
    # 已确认的部分成交当成"什么都没有"。
    for terminal_status in ("CANCELED", "EXPIRED"):
        canceled_partial = read_submission_result({
            "orderId": 6, "status": terminal_status, "origQty": "0.01",
            "executedQty": "0.0053", "avgPrice": "100.00"})
        check(f"{terminal_status} 带已确认成交时主分类可见且带 reason",
              canceled_partial.outcome is SubmissionOutcome.TERMINAL_PARTIAL
              and canceled_partial.fill_confirmed
              and canceled_partial.filled_qty == Decimal("0.0053")
              and REASON_PARTIAL_FILL_PRESENT in canceled_partial.reason_codes,
              f"actual={canceled_partial}")
        canceled_empty = read_submission_result({
            "orderId": 7, "status": terminal_status, "origQty": "0.01",
            "executedQty": "0", "avgPrice": "100.00"})
        check(f"{terminal_status} 零成交仍为 TERMINAL_UNFILLED 且无成交 reason",
              canceled_empty.outcome is SubmissionOutcome.TERMINAL_UNFILLED
              and not canceled_empty.fill_confirmed
              and REASON_PARTIAL_FILL_PRESENT not in canceled_empty.reason_codes,
              f"actual={canceled_empty}")
    rejected = read_submission_result({
        "orderId": 5, "status": "REJECTED", "origQty": "0.01",
        "executedQty": "0.005", "avgPrice": "0"})
    check("拒绝回报携带成交时判为自相矛盾而不是静默丢弃成交",
          rejected.outcome is SubmissionOutcome.UNKNOWN
          and any(code.startswith("RESPONSE_INCONSISTENT") for code in rejected.reason_codes),
          f"actual={rejected}")
    conflict = _is_raised(QuantityStateConflict, lambda: read_exchange_order(
        {"origQty": "0.01", "executedQty": "0.02", "status": "PARTIALLY_FILLED"}))
    check("成交超过受理量时结构化拒绝", conflict)

    print("\n[8] 健康故障不得触发全平、不改 pause/emergency")
    for item in inputs["health_faults"]:
        plan = guard_fault_action(item["fault"], item["proposed_action"])
        check(f"{item['fault']} 不触发全平且不动控制位",
              not plan.triggers_flat_close and not plan.touches_pause_state
              and not plan.touches_emergency_state and not plan.changes_risk_profile
              and plan.action == item["proposed_action"],
              f"actual={plan}")
    forbidden = inputs["forbidden_proposal"]
    check("健康故障提议全平被拒绝",
          _is_raised(ProtectionSafetyError, lambda: guard_fault_action(
              forbidden["fault"], forbidden["proposed_action"])))

    # A/P1-A2：V1 只拦 _HEALTH_LIKE 里登记过的 4 个类，未登记的健康类落进 UNKNOWN
    # 后被放行全平。健康类名永远枚举不全（镜像滞后、Algo 查单失败都不是
    # QUERY_UNCERTAIN），所以必须改成白名单 fail-closed。
    blocked_faults: list[str] = []
    for item in inputs["unregistered_health_like_faults"]:
        blocked_faults.append(
            f"{item['fault']}/{item['proposed_action']}"
            if _is_raised(ProtectionSafetyError, lambda i=item: guard_fault_action(
                i["fault"], i["proposed_action"]))
            else f"ALLOWED({item['fault']}/{item['proposed_action']})")
    check("未登记的健康类/提交未知/未归类故障提议全平全部被拒（白名单 fail-closed）",
          all(not item.startswith("ALLOWED(") for item in blocked_faults),
          f"actual={blocked_faults}")
    check("白名单里只有已确证的保护失败",
          {item["fault"] for item in inputs["unregistered_health_like_faults"]
           if not _is_raised(ProtectionSafetyError, lambda i=item: guard_fault_action(
               i["fault"], i["proposed_action"]))} == set())

    # A/P2-A7：唯一被授权的兜底平仓必须如实写进审计计划（V1 恒为 False = 放行全平
    # 却在计划里否认发生了仓位动作）。
    authorized = inputs["flat_close_authorized_fault"]
    authorized_plan = guard_fault_action(authorized["fault"], authorized["proposed_action"])
    check("保护确证失败的全平被授权且审计计划如实记录 triggers_flat_close",
          authorized_plan.triggers_flat_close
          and authorized_plan.fault_class is FaultClass.PROTECTION
          and authorized_plan.action == authorized["proposed_action"]
          and not authorized_plan.touches_pause_state
          and not authorized_plan.touches_emergency_state,
          f"actual={authorized_plan}")
    # 推迟兜底平仓只认"已确证的观测故障"（与 FLAT_CLOSE_AUTHORIZED 方向相反但同样
    # 是白名单）：空值/保护失败/提交结果未知都必须照常全平，绝不让"证据缺失"
    # 或"提交结果未知"变成保留裸仓。
    check("推迟兜底平仓只认已确证的观测故障，空值/保护失败/提交未知一律照常平仓",
          defers_flat_close("QUERY_FAILED") and defers_flat_close("WS_DISCONNECT")
          and defers_flat_close("HEALTH_DEGRADED") and defers_flat_close("RATE_LIMITED")
          and not defers_flat_close("") and not defers_flat_close(None)
          and not defers_flat_close("PROTECTION_FAILED")
          and not defers_flat_close("SOMETHING_NEW")
          and not defers_flat_close("TIMEOUT") and not defers_flat_close("HTTP_503"))
    timeout = classify_execution_fault(TimeoutError("connection timed out"))
    check("超时映射为按原 ID 查单而不是全平",
          timeout.action == "QUERY_BY_ORIGINAL_ID" and not timeout.triggers_flat_close
          and timeout.blocks_new_open)
    unknown_fault = classify_execution_fault("SOMETHING_NEW")
    check("未识别故障 fail-closed 到人工复核且不被当成健康故障",
          unknown_fault.action == "ALERT_AND_MANUAL_REVIEW"
          and unknown_fault.fault_class is FaultClass.UNKNOWN
          and not unknown_fault.triggers_flat_close
          and "FAULT_UNRECOGNIZED_FAIL_CLOSED" in unknown_fault.reason_codes)
    protection_fault = classify_execution_fault("PROTECTION_FAILED")
    check("已确证的保护失败属保护类而非健康类（不触发推迟平仓分支）",
          protection_fault.fault_class is FaultClass.PROTECTION
          and not protection_fault.triggers_flat_close
          and REASON_HEALTH_NO_FLAT_CLOSE not in protection_fault.reason_codes)

    before = dict(inputs["state_flags_before"])
    after = dict(inputs["state_flags_after_expected"])
    guard_state_flags_unchanged(before, after)
    check("控制位在执行故障前后保持一致",
          execution_state_flags(before) == execution_state_flags(after))
    mutated = dict(after)
    mutated["emergency"] = True
    check("控制位被改动时守护函数抛错",
          _is_raised(ProtectionSafetyError,
                     lambda: guard_state_flags_unchanged(before, mutated)))

    db = AuditDatabase(tmpdir / "faults.sqlite")
    outbox = OrderOutbox(db, environment="demo")
    outbox.prepare(
        client_order_id="synth-u04-state-1", symbol="ZZZUSDT", side="BUY",
        position_side="LONG", order_type="MARKET", purpose="ENTRY", qty=1.0,
        price=None, reduce_only=False, endpoint="/fapi/v1/order",
        request={"newClientOrderId": "synth-u04-state-1"})
    outbox.mark_unknown("synth-u04-state-1", TimeoutError("lost"))
    row = db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id='synth-u04-state-1'")
    state = {"paused": False, "emergency": False, "kill_switch": False,
             "live_enabled": False}
    snapshot_before = dict(state)
    broker = RecordingBroker(standard=Exception("query failed"))
    report = execute_unknown_reconcile(
        [row], broker=broker, outbox=outbox, now_ms=1789862406000)
    guard_state_flags_unchanged(snapshot_before, state)
    check("UNKNOWN 收口失败路径不改动控制位",
          execution_state_flags(state) == execution_state_flags(snapshot_before))
    check("查询失败时保持 UNKNOWN 且无提交",
          report.submit_calls == 0 and str(db.fetch_one(
              "SELECT status FROM order_outbox WHERE client_order_id='synth-u04-state-1'"
          )["status"]) == "UNKNOWN")
    db.close()


def section_schema_and_guard(tmpdir: Path) -> None:
    """[9][10] 向前兼容列补齐；保护核验失败时的平仓路由（健康故障不平仓）。"""
    print("\n[9] 执行命令分账列的幂等补齐（不抬升 schema 版本号）")
    db = AuditDatabase(tmpdir / "schema.sqlite")
    versions_before = [
        row["version"] for row in db.fetch_all(
            "SELECT version FROM schema_migrations ORDER BY version")]
    check("补列前迁移版本为 1..16（U04 不新增迁移）",
          versions_before == list(range(1, 17)), f"actual={versions_before}")
    legacy_columns = (
        "client_order_id,environment,release_id,business_key,symbol,side,position_side,"
        "order_type,purpose,qty,price,reduce_only,endpoint,status,request_json,"
        "attempt_count,created_at,updated_at"
    )
    stamp = "2026-01-01T00:00:00+00:00"
    for client_id, endpoint in (("legacy-1", "/fapi/v1/order"),
                                ("legacy-2", "/fapi/v1/legacyUnknown")):
        db.conn.execute(
            f"INSERT INTO order_outbox({legacy_columns}) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (client_id, "demo", "legacy", f"ENTRY:ZZZUSDT:BOTH", "ZZZUSDT", "BUY", "BOTH",
             "MARKET", "ENTRY", 0.1, None, 0, endpoint, "FILLED", "{}", 1, stamp, stamp),
        )
    db.conn.commit()
    added = ensure_execution_schema(db.conn)
    check("首次补齐同时覆盖两张表的列",
          "order_outbox.command_kind" in added
          and "protection_order_mirror.quantity_text" in added
          and len(added) == 10, f"added={added}")
    check("第二次调用为空操作（幂等）", ensure_execution_schema(db.conn) == ())
    check("补列不改变迁移版本号",
          [row["version"] for row in db.fetch_all(
              "SELECT version FROM schema_migrations ORDER BY version")] == versions_before)
    legacy = db.fetch_one("SELECT * FROM order_outbox WHERE client_order_id='legacy-1'")
    check("旧行仍可读且新列为空（不重写历史数据）",
          str(legacy["status"]) == "FILLED" and legacy["command_kind"] is None
          and legacy["accepted_qty_text"] is None)
    check("旧行按端点回推命令种类（无需新列也能收口）",
          classify_row(dict(legacy)) is CommandKind.STANDARD)
    unknown_endpoint = db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id='legacy-2'")
    check("历史未知端点的命令种类判定 fail-closed（不默认成普通订单）",
          _is_raised(Exception, lambda: classify_row(dict(unknown_endpoint))))
    db.close()

    # A/P3-2：并发进程已抢先补齐时，"本次新增"必须为空（不能把那列算成自己的），
    # 且本次没有 DDL 时不得替调用方提交它尚未完成的事务。
    db3 = AuditDatabase(tmpdir / "concurrent.sqlite")
    ensure_execution_schema(db3.conn)          # 模拟另一个进程先跑完
    db3.conn.execute("CREATE TABLE probe_txn(x TEXT)")
    db3.conn.execute("INSERT INTO probe_txn VALUES('uncommitted')")
    check("并发方已加好的列不计入本次返回值",
          ensure_execution_schema(db3.conn) == ())
    db3.conn.rollback()
    remaining = db3.conn.execute(
        "SELECT COUNT(*) AS c FROM probe_txn").fetchone()["c"]
    check("本次无 DDL 时不替调用方提交未完成事务（rollback 仍能撤销）",
          int(remaining) == 0, f"remaining={remaining}")
    db3.close()

    print("\n[10] 保护核验失败时的平仓路由：健康故障不平仓，保护缺口仍兜底平仓")
    fixture = load_fixture("f21_algo_standard_separation.json")
    inputs21 = fixture["inputs"]
    symbol = inputs21["symbol"]
    # N2：夹具里 ``ledger_rows[*].symbol`` 与 ``inputs.symbol`` **同为 ZZZUSDT**，
    # 直接拿它当"另一标的"会让"按标的隔离"退化成"同标的"——等于没测（已实测：
    # 修复树上也是 get(Z)=QUERY_FAILED）。这里显式用一个不同的标的。
    other_symbol = "YYYUSDT"
    assert other_symbol != symbol, "跨标的回归必须使用不同标的，否则护栏空转"
    db2 = AuditDatabase(tmpdir / "guard.sqlite")
    close_calls: list[Any] = []

    # A/P1-P1 的修法要求"测试不得直接给内部字段赋值"。因此这里只替换**交易所 I/O
    # 边界**（查 Algo 单/查持仓/挂单/DELETE），让 _ensure_protection_from_order ->
    # _resync_protection -> _resync_protection_impl 走**真实生产路径**：失败类别由
    # 生产代码自己产生，测试不注入任何"本轮新鲜"的假设。
    def make_broker(*, algos: Any = None, position: Any = None,
                    place_raises: bool = False) -> Any:
        broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
        broker.db = db2
        broker.order_outbox = None
        broker.environment = inputs21["environment"]
        broker._resync_lock = threading.RLock()
        broker._open_algos = lambda _symbol: (None if algos is None else list(algos))
        broker._algo_prices = lambda _symbol: {}
        broker._single_symbol_position = lambda _symbol: dict(position or {})
        broker._format_qty = lambda _symbol, qty: str(qty)
        broker._tick_size = lambda _symbol: 0.01
        broker._min_qty = lambda _symbol: Decimal("0.001")
        broker._is_hedge_mode = lambda: False
        broker._cancel_symbol_algos = lambda _symbol: None

        def place(*_args: Any, **_kwargs: Any) -> Any:
            if place_raises:
                raise RuntimeError("synthetic protection place failure")
            return None

        broker._place_algo_protection = place
        broker._close_or_reduce = lambda intent: close_calls.append(intent)
        return broker

    open_order = {"symbol": symbol, "side": "LONG", "qty": 0.1, "source": "ai",
                  "stop_loss": 95.0, "take_profit": 105.0}
    # 无 SL/TP 记录的成交单才会走到"查询 Algo 失败 -> 无法证明有无保护"这条分支
    # （有 SL/TP 时生产代码携带明确目标，不是观测路径）。
    query_fail_order = {"symbol": symbol, "side": "LONG", "qty": 0.1, "source": "ai"}
    live_position = {"symbol": symbol, "positionAmt": "0.1",
                     "positionSide": "LONG", "markPrice": "100.0"}
    flat_position = {"symbol": symbol, "positionAmt": "0.0",
                     "positionSide": "LONG", "markPrice": "100.0"}
    guard_installed = any(
        marker in inspect.getsource(BinanceFuturesBroker._ensure_protection_from_order)
        for marker in ("protection_sync", "last_protection_sync_reason"))

    if not guard_installed:
        # 共享适配（src/exchange/binance_futures.py）由 I 应用，本分支尚未包含。
        # 仍然走真实路径记录"修复前"的基线行为：无守护时保护核验失败都全平。
        print("  PENDING  binance_futures 健康故障守护未安装（待 I 应用适配补丁）")
        baseline = len(close_calls)
        make_broker(algos=None)._ensure_protection_from_order(dict(query_fail_order))
        check("修复前基线：查询失败无守护时仍兜底全平（对照用）",
              len(close_calls) - baseline == 1, f"close_calls={len(close_calls)}")
        baseline = len(close_calls)
        make_broker(algos=[], position=live_position,
                    place_raises=True)._ensure_protection_from_order(dict(open_order))
        check("修复前基线：保护挂单失败仍兜底全平（对照用）",
              len(close_calls) - baseline == 1, f"close_calls={len(close_calls)}")
    else:
        # A/N2：以下三条回归断言必须跑在**同一个 broker 实例**上，否则是空转护栏。
        # ``ProtectionSyncReasons`` 是每实例惰性新建的（``self.__dict__.get``），
        # 每次 ``make_broker()`` 都得到一颗全新实例的话，状态天然不跨调用共享——
        # "成功即复位"与"按标的隔离"在**有缺陷的 V1 树上也全过**（复验者实测）。
        # 这里改成单个实例 + 可变状态单元，让失败类别真的存活到下一轮。
        sync_state: dict[str, Any] = {
            "algos": None, "position": dict(flat_position), "place_raises": False}
        shared = make_broker()
        shared._open_algos = lambda _symbol: (
            None if sync_state["algos"] is None else list(sync_state["algos"]))
        shared._single_symbol_position = lambda _symbol: dict(sync_state["position"] or {})
        shared._algo_prices = lambda _symbol: {}
        shared._format_qty = lambda _symbol, qty: str(qty)
        shared._tick_size = lambda _symbol: 0.01
        shared._min_qty = lambda _symbol: Decimal("0.001")
        shared._is_hedge_mode = lambda: False
        shared._cancel_symbol_algos = lambda _symbol: None

        def shared_place(*_args: Any, **_kwargs: Any) -> Any:
            if sync_state["place_raises"]:
                raise RuntimeError("synthetic protection place failure")
            return None

        shared._place_algo_protection = shared_place
        shared._close_or_reduce = lambda intent: close_calls.append(intent)

        # (1) 查询故障一轮：不平仓、只告警。同一实例沉淀 QUERY_FAILED。
        sync_state.update({"algos": None, "position": dict(flat_position),
                           "place_raises": False})
        shared._ensure_protection_from_order(dict(query_fail_order))
        check("确证为查询故障时不平仓（只告警）", close_calls == [],
              f"close_calls={len(close_calls)}")
        deferred = db2.fetch_all(
            "SELECT * FROM risk_events WHERE event_type='futures_sync_error' "
            "AND action_taken='alert_only_no_flat_close'")
        check("推迟平仓写入可审计的风险事件", len(deferred) >= 1)

        # A/N2 关键：**单实例顺序断言本身无法区分 V1 与 r2**。原因是 V1 的标量字段
        # 在"真实保护失败"那一轮返回前会被自己改写成 PROTECTION_FAILED，读者看到的
        # 永远是新鲜值；残余只有**并发窗口**内才可达（复验者用 20000 轮双线程实证
        # 16->0）。所以这里补一条**确定性**的结构断言：失败类别必须由**生产实例**按
        # 标的存放（V1 只有进程级标量 ``last_protection_sync_reason``，没有该对象）。
        production_reasons = getattr(shared, "protection_sync", None)
        check("生产实例按标的存放失败类别（V1 的进程级标量不满足）",
              production_reasons is not None
              and production_reasons.get(symbol) == "QUERY_FAILED",
              f"protection_sync={production_reasons!r}")
        if production_reasons is not None:
            check("生产实例：A 标的的失败在 B 标的键下不可见（按标的隔离）",
                  production_reasons.get(other_symbol) == ""
                  and production_reasons.defers(other_symbol) is False,
                  f"get(B)={production_reasons.get(other_symbol)!r}")

        # (2) 残余状态回归（A/P1-P1）：查询失败一轮 **之后** 来一轮成功的同步，再发生
        # 真实的保护挂单失败，必须仍然全平。V1 的成功路径提前 return 且不复位字段，
        # 残留的 QUERY_FAILED 会把这次真实的保护缺口误判成观测故障而放过裸仓。
        sync_state.update({"algos": [], "position": dict(flat_position)})
        shared._ensure_protection_from_order(dict(query_fail_order))
        check("同一实例：成功的一轮同步不留下残余失败类别（成功即复位）",
              close_calls == [], f"close_calls={len(close_calls)}")
        if production_reasons is not None:
            check("生产实例：轮次入口复位失败类别（成功一轮后本标的无残余）",
                  production_reasons.get(symbol) == "",
                  f"get(symbol)={production_reasons.get(symbol)!r}")
        sync_state.update({"position": dict(live_position), "place_raises": True})
        shared._ensure_protection_from_order(dict(open_order))
        check("同一实例：查询失败之后的真实保护失败仍按 P0 兜底全平（残余状态不再抑制）",
              len(close_calls) == 1, f"close_calls={len(close_calls)}")
        close_calls.clear()

        # (3) 跨标的隔离：A 标的的 QUERY_FAILED 不得抑制 B 标的的兜底平仓。
        sync_state.update({"algos": None, "position": dict(flat_position),
                           "place_raises": False})
        shared._ensure_protection_from_order(dict(query_fail_order))
        other_order = dict(open_order)
        other_order["symbol"] = other_symbol
        other_live = dict(live_position)
        other_live["symbol"] = other_symbol
        sync_state.update({"algos": [], "position": dict(other_live),
                           "place_raises": True})
        shared._ensure_protection_from_order(other_order)
        check("同一实例：另一标的的查询失败不抑制本标的的兜底平仓（按标的隔离）",
              len(close_calls) == 1, f"close_calls={len(close_calls)}")
        close_calls.clear()

    # 与本分支是否安装共享适配无关：ProtectionSyncReasons 的"按标的 + 每轮复位"
    # 语义在本模块内可直接验证（这就是修复前标量状态缺的两个属性）。
    reasons = ProtectionSyncReasons()
    reasons.begin(symbol)
    reasons.mark(symbol, "QUERY_FAILED")
    stale_before = reasons.defers(symbol)
    reasons.begin(symbol)                      # 新的一轮开始 -> 先复位
    stale_after = reasons.defers(symbol)
    reasons.mark("OTHERUSDT", "QUERY_FAILED")
    check("同步失败类别跨轮次不复位 -> 会抑制平仓（修复前语义，对照）",
          stale_before is True)
    check("同步失败类别在轮次入口复位 -> 不再抑制平仓（修复后语义）",
          stale_after is False and reasons.defers(symbol) is False
          and reasons.defers("OTHERUSDT") is True
          and reasons.get(symbol) == "")
    check("失败类别按标的隔离，不跨标的传染",
          reasons.defers("OTHERUSDT") is True and reasons.defers(symbol) is False)

    db2.close()


def section_fill_revision_wiring(tmpdir: Path) -> None:
    """[11] DC04 落库与重启重放：走私有流真实路径（补丁安装后启用）。

    本节**不**直接调用 ``apply_fill_revision``，也**不**注入任何内部字段：成交被伪造
    成订单私有流事件交给 ``OrderOutbox.apply_event``，由生产代码自己完成
    "解析事件 -> 落三分量 -> 记 fill id"。
    """
    print("\n[11] 私有流成交落库与重启重放（补丁接线后启用）")
    if not hasattr(OrderOutbox, "apply_fill_revision"):
        print("  PENDING  order_outbox 成交落库接线未安装（待 I 应用补丁）")
        return
    db_path = tmpdir / "fill_wiring.sqlite"
    client_id = "synth-u04-fill-wiring"
    symbol = "ZZZUSDT"

    def make_outbox() -> Any:
        return OrderOutbox(AuditDatabase(db_path), environment="demo")

    outbox = make_outbox()
    outbox.prepare(
        client_order_id=client_id, symbol=symbol, side="BUY", position_side="LONG",
        order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
        reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": client_id, "quantity": "0.02"},
        qty_text="0.02")
    # 私有流首笔部分成交：真实路径（V1 的兜底会在这里抛 STATUS_FILL_MISMATCH）
    event = {
        "c": client_id, "i": 901, "X": "PARTIALLY_FILLED", "q": "0.02", "z": "0.0047",
        "l": "0.0047", "t": 55501, "ap": "100.00", "s": symbol,
    }
    first = outbox.apply_event(ExchangeEvent(
        event_key="k1", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55501",
        payload={"o": event}, supported=True))
    row = outbox.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("首笔成交经私有流真实路径落库（第一笔不再抛 STATUS_FILL_MISMATCH）",
          first and str(row["cumulative_filled_qty_text"]) == "0.0047"
          and str(row["working_leaves_qty_text"]) == "0.0153",
          f"actual={dict(row) if row else None}")
    check("首笔成交后状态由分量推导为 PARTIALLY_FILLED",
          str(row["status"]) == "PARTIALLY_FILLED", f"status={row['status']}")
    check("成交 id 被持久化（fill 级幂等的依据）",
          "55501" in str(row["applied_fill_ids_json"]))

    # 重启（关闭 + 重开同一文件 + 新 OrderOutbox）后重放同一笔成交：必须幂等
    outbox.db.close()
    reopened = make_outbox()
    reopened.apply_event(ExchangeEvent(
        event_key="k1", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55501",
        payload={"o": event}, supported=True))
    after = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("重启后重放同一 fill 不二次计账（累计成交不变）",
          str(after["cumulative_filled_qty_text"]) == "0.0047"
          and str(after["working_leaves_qty_text"]) == "0.0153",
          f"actual={dict(after) if after else None}")
    # 第二笔新成交仍能继续推进（证明不是"只认第一笔"）
    reopened.apply_event(ExchangeEvent(
        event_key="k2", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55502",
        payload={"o": dict(event, **{"l": "0.0058", "z": "0.0105", "t": 55502,
                                     "X": "PARTIALLY_FILLED"})}, supported=True))
    second = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("后续成交继续累加（0.0047 -> 0.0105）",
          str(second["cumulative_filled_qty_text"]) == "0.0105",
          f"filled={second['cumulative_filled_qty_text']}")
    # 终态转 CANCELED 时三分量同步为终态口径（working 归零、余量进 terminal）
    reopened.apply_event(ExchangeEvent(
        event_key="k3", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id=None,
        payload={"o": dict(event, **{"X": "CANCELED", "l": "0", "t": 0})},
        supported=True))
    canceled = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("终态行三分量同步（working=0，余量进 terminal）",
          str(canceled["status"]) == "CANCELED"
          and str(canceled["working_leaves_qty_text"]) == "0"
          and str(canceled["terminal_unfilled_qty_text"]) == "0.0095",
          f"actual={dict(canceled) if canceled else None}")
    check("终态行仍守恒（accepted == filled + working + terminal）",
          Decimal(str(canceled["accepted_qty_text"]))
          == Decimal(str(canceled["cumulative_filled_qty_text"]))
          + Decimal(str(canceled["working_leaves_qty_text"]))
          + Decimal(str(canceled["terminal_unfilled_qty_text"])),
          f"actual={dict(canceled) if canceled else None}")

    # ----------------------------------------------------------------- #
    # 防复发（r3 / N1）：CANCELED 之后交易所又回报一笔迟到成交。
    # r2 的 ``_component_status`` 用 ``_STATUS_ALIASES.get(x, "")`` 兜底，把 CANCELED
    # 当成"未知状态"后按分量推导成 PARTIALLY_FILLED —— "终态保留原状态"分支对
    # CANCELED/EXPIRED/REJECTED/FILLED 永久失效。后果：终态行被去终态化，
    # working 非零、terminal 归零，该行随即违反 K04 终态不变量
    # （``WORKING_NONZERO_WHILE_TERMINAL``），且终态单被报成"工作中"。
    # 这一节走生产私有流路径断言"落库的行永远能被契约自身接受"。
    # ----------------------------------------------------------------- #
    reopened.apply_event(ExchangeEvent(
        event_key="k4", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55503",
        payload={"o": dict(event, **{"X": "CANCELED", "z": "0.0108", "l": "0.0003",
                                     "t": 55503})},
        supported=True))
    late = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("CANCELED 后的迟到成交不复活订单（状态仍是 CANCELED）",
          str(late["status"]) == "CANCELED", f"status={late['status']}")
    check("CANCELED 后的迟到成交：working 归零、余量进 terminal",
          str(late["working_leaves_qty_text"]) == "0"
          and str(late["terminal_unfilled_qty_text"]) == "0.0092",
          f"actual={dict(late) if late else None}")
    check("CANCELED 后的迟到成交：累计成交推进且仍守恒",
          str(late["cumulative_filled_qty_text"]) == "0.0108"
          and Decimal(str(late["accepted_qty_text"]))
          == Decimal(str(late["cumulative_filled_qty_text"]))
          + Decimal(str(late["working_leaves_qty_text"]))
          + Decimal(str(late["terminal_unfilled_qty_text"])),
          f"actual={dict(late) if late else None}")
    check("CANCELED 后的迟到成交：落库行仍被 OrderQuantity 契约接受",
          row_quantity_violations(late) == (),
          f"violations={row_quantity_violations(late)}")
    # 终态之后再投一笔成交仍必须幂等推进（不能"只认第一笔迟到成交"）
    reopened.apply_event(ExchangeEvent(
        event_key="k5", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55504",
        payload={"o": dict(event, **{"X": "CANCELED", "z": "0.0111", "l": "0.0003",
                                     "t": 55504})},
        supported=True))
    late2 = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("CANCELED 后第二笔迟到成交继续推进且不破坏终态",
          str(late2["cumulative_filled_qty_text"]) == "0.0111"
          and str(late2["working_leaves_qty_text"]) == "0"
          and str(late2["terminal_unfilled_qty_text"]) == "0.0089"
          and str(late2["status"]) == "CANCELED"
          and row_quantity_violations(late2) == (),
          f"actual={dict(late2) if late2 else None}")
    # 重复投递同一 trade_id：at-least-once 语义下不得二次计账
    reopened.apply_event(ExchangeEvent(
        event_key="k5", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id="901", trade_id="55504",
        payload={"o": dict(event, **{"X": "CANCELED", "z": "0.0111", "l": "0.0003",
                                     "t": 55504})},
        supported=True))
    replay_row = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (client_id,))
    check("终态后重复投递同一 trade_id 不二次计账",
          str(replay_row["cumulative_filled_qty_text"]) == "0.0111",
          f"filled={replay_row['cumulative_filled_qty_text']}")

    # EXPIRED 走同一条兜底分支，同样必须保留终态
    expired_id = "synth-u04-expired-late-fill"
    reopened.prepare(
        client_order_id=expired_id, symbol=symbol, side="BUY", position_side="LONG",
        order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
        reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": expired_id, "quantity": "0.02"},
        qty_text="0.02")
    reopened.apply_event(ExchangeEvent(
        event_key="e1", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=expired_id, exchange_order_id="902", trade_id="66601",
        payload={"o": dict(event, **{"c": expired_id, "i": 902, "X": "PARTIALLY_FILLED",
                                     "z": "0.0047", "l": "0.0047", "t": 66601})},
        supported=True))
    reopened.apply_event(ExchangeEvent(
        event_key="e2", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=expired_id, exchange_order_id="902", trade_id=None,
        payload={"o": dict(event, **{"c": expired_id, "i": 902, "X": "EXPIRED",
                                     "l": "0", "t": 0})},
        supported=True))
    reopened.apply_event(ExchangeEvent(
        event_key="e3", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=expired_id, exchange_order_id="902", trade_id="66602",
        payload={"o": dict(event, **{"c": expired_id, "i": 902, "X": "EXPIRED",
                                     "z": "0.0057", "l": "0.001", "t": 66602})},
        supported=True))
    expired_row = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (expired_id,))
    check("EXPIRED 后的迟到成交保留终态且行仍被契约接受",
          str(expired_row["status"]) == "EXPIRED"
          and str(expired_row["cumulative_filled_qty_text"]) == "0.0057"
          and str(expired_row["working_leaves_qty_text"]) == "0"
          and str(expired_row["terminal_unfilled_qty_text"]) == "0.0143"
          and row_quantity_violations(expired_row) == (),
          f"actual={dict(expired_row) if expired_row else None}")

    # 第 4 项已闭合行为的回归对照（r3 必须不破坏）：reject 清零 + 拒单后成交 fail-closed。
    # K04 要求 REJECTED 的 accepted/filled 必须为 0；清零是后续任何成交/对账路径
    # 能构造出合法 OrderQuantity 的前提。
    reject_id = "synth-u04-reject-zero"
    reopened.prepare(
        client_order_id=reject_id, symbol=symbol, side="BUY", position_side="LONG",
        order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
        reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": reject_id, "quantity": "0.02"},
        qty_text="0.02")
    reopened.reject(reject_id, RuntimeError("synthetic exchange reject"))
    reject_row = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (reject_id,))
    check("reject 后四分量全部清零（K04: REJECTED 不得带受理量）",
          str(reject_row["status"]) == "REJECTED"
          and str(reject_row["accepted_qty_text"]) == "0"
          and str(reject_row["cumulative_filled_qty_text"]) == "0"
          and str(reject_row["working_leaves_qty_text"]) == "0"
          and str(reject_row["terminal_unfilled_qty_text"]) == "0",
          f"actual={dict(reject_row) if reject_row else None}")
    check("reject 后的行仍被 OrderQuantity 契约接受",
          row_quantity_violations(reject_row) == (),
          f"violations={row_quantity_violations(reject_row)}")
    reopened.apply_event(ExchangeEvent(
        event_key="r1", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=reject_id, exchange_order_id="903", trade_id="77701",
        payload={"o": dict(event, **{"c": reject_id, "i": 903, "X": "REJECTED",
                                     "z": "0.0047", "l": "0.0047", "t": 77701})},
        supported=True))
    after_reject = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (reject_id,))
    check("拒单后的成交入账 fail-closed（不把已拒单变成有成交的单）",
          str(after_reject["cumulative_filled_qty_text"]) == "0"
          and str(after_reject["status"]) == "REJECTED",
          f"actual={dict(after_reject) if after_reject else None}")

    # 第二个新方法的接线：UNKNOWN 收口成功时把交易所三分量落库（apply_exchange_quantities）
    unknown_id = "synth-u04-quantity-wiring"
    reopened.prepare(
        client_order_id=unknown_id, symbol=symbol, side="BUY", position_side="LONG",
        order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
        reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": unknown_id, "quantity": "0.02"},
        qty_text="0.02")
    reopened.mark_unknown(unknown_id, TimeoutError("synthetic"))

    class _QueryBroker:
        def query_order(self, _symbol: str, _client_id: str) -> Any:
            return {"orderId": 777, "status": "PARTIALLY_FILLED", "origQty": "0.02",
                    "executedQty": "0.0047"}

        def query_algo_order(self, _client_id: str) -> Any:
            raise LookupError("not algo")

    resolved = reopened.reconcile_unknown(_QueryBroker())
    qrow = reopened.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (unknown_id,))
    check("UNKNOWN 收口成功时交易所三分量落库（apply_exchange_quantities 有生产调用点）",
          resolved == 1 and str(qrow["accepted_qty_text"]) == "0.02"
          and str(qrow["cumulative_filled_qty_text"]) == "0.0047"
          and str(qrow["working_leaves_qty_text"]) == "0.0153"
          and str(qrow["terminal_unfilled_qty_text"]) == "0",
          f"actual={dict(qrow) if qrow else None}")
    reopened.db.close()


def main() -> int:
    print("U04 执行故障验收（离线；临时目录、假 Broker、无网络）")
    tmpdir = Path(tempfile.mkdtemp(prefix="mta-u04-exec-"))
    print(f"临时数据目录: {tmpdir}")
    try:
        section_dc04_partial_fill()
        section_dc13_unknown(tmpdir)
        section_f03_protection(tmpdir)
        section_f21_kinds(tmpdir)
        section_submission_and_faults(tmpdir)
        section_schema_and_guard(tmpdir)
        section_fill_revision_wiring(tmpdir)
    except Exception:  # noqa: BLE001 - 保留完整栈便于定位
        FAILURES.append("unexpected_exception")
        traceback.print_exc()
    print(f"\n结果: {PASSED} 通过 / {len(FAILURES)} 失败")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED  {name}")
        return 1
    print("U04 EXECUTION FAULTS OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
