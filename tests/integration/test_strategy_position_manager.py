"""U02 策略仓管理 / 有界退出授权 集成回归（K01、K04；F03/F06/F09/F19/F21；R09/R11）。

运行：``python tests/integration/test_strategy_position_manager.py``
（本项目不使用 pytest；本文件既可被 pytest 收集，也可直接执行，无网、只写临时目录）

覆盖任务卡 U02 的七条必达要点，全部走**真实模块路径**：

[1] 硬退出与预期持仓分离（两个命名空间互不覆盖）
[2] 旧仓应用策略显式、重启不隐式批量退出
[3] 重复退出不反向开仓
[4] 管理预留原子处理并发退出（守恒，R09）
[5] 过期授权不阻断 UNKNOWN 查单/收口
[6] 保留人工 pause/emergency 语义（R11，不得用健康故障触发全平）
[7] 与 K04 契约一致（绑定账户/仓位/最大可减量/revision/时效，只减不增）

以及 U15 跨任务阻塞面：账本侧 ``account_ref`` 必须与运行代派生值一致。本文件
**不依赖**任何共享适配补丁是否已应用：它只用 U00 冻结契约、U15 ``runtime_identity``、
U04 ``protection_commands`` 与 U02 的两个独占模块。
"""
from __future__ import annotations

import sys
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from core.contracts import (  # noqa: E402
    ManagementAuthorization,
    validate_authorization,
)
from core.contracts.k04_management import ProtectionTarget  # noqa: E402
from core.management_authorization import (  # noqa: E402
    ManagementAuthorizationError,
    ManagementReservationLedger,
    REASON_ACCOUNT_DOMAIN_MISMATCH,
    REASON_AUTHORIZATION_UNBOUND,
    REASON_REDUCE_ONLY_VIOLATED,
    REASON_RESERVATION_ALREADY_CLOSED,
    REASON_RESERVATION_CONSUME_EXCEEDS,
    REASON_RESERVATION_DUPLICATE,
    REASON_RESERVATION_EXCEEDS_POSITION,
    REASON_RESERVATION_STALE,
    assert_account_domains_consistent,
    evaluate_management_request,
    issue_management_authorization,
    ledger_account_ref,
    reduce_only_guard,
    reservation_conserved,
    resolve_account_domain,
)
from core.runtime_identity import (  # noqa: E402
    derive_account_ref,
    mint_runtime_generation,
    rotate_credential,
)
from core.strategy_position_manager import (  # noqa: E402
    ExpectationState,
    ImplicitBulkExitError,
    PositionClass,
    REASON_DUPLICATE_EXIT_IGNORED,
    REASON_IMPLICIT_BULK_EXIT_BLOCKED,
    REASON_LEGACY_REQUIRES_MIGRATION,
    StrategyPositionManager,
    exit_side_for,
)
from exchange.protection_commands import (  # noqa: E402
    CommandKind,
    execute_unknown_reconcile,
    plan_unknown_reconcile,
)

FAILURES: list[str] = []
PASS_COUNT = 0
_NOW = 1_789_862_400_000


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL  {name}: {detail}")


def _generation(*, environment: str = "demo", account: str = "SYNTH_ACCT",
                instance: str = "rt-synth-1", started_at: float = 1.0):
    return mint_runtime_generation(
        environment=environment,
        account_ref=derive_account_ref(environment, declared_account_ref=account),
        runtime_instance_id=instance, started_at=started_at,
    )


def _manager(**kwargs) -> StrategyPositionManager:
    return StrategyPositionManager(account_ref="demo:SYNTH_ACCT", environment="demo",
                                   **kwargs)


# --------------------------------------------------------------------------- #
def test_hard_exit_and_expectation_are_separate_namespaces() -> None:
    print("\n[1] 硬退出与预期持仓分离")
    m = _manager()
    m.set_expectation(lifecycle_id="lc-1", symbol="BTCUSDT", position_side="LONG",
                      expected_qty="0.01", now_ms=_NOW, strategy_id="s-trend",
                      ticket_id="tk-1")
    record = m.record_hard_exit(exit_id="hx-1", lifecycle_id="lc-1",
                                trigger="STOP_MARKET", evidence_id="ev-stop-1",
                                now_ms=_NOW + 1)
    # 记录硬退出后，预期持仓**原封不动**（含 revision/state），只多了一条终态事实。
    exp = m.expectation("lc-1")
    check("硬退出不删除预期持仓", exp is not None)
    check("硬退出不覆盖预期数量", exp.expected_qty == Decimal("0.01"), exp.as_dict())
    check("硬退出不改变预期状态", exp.state is ExpectationState.HELD, exp.state)
    check("硬退出留下独立快照", record.expected_qty_at_trigger == Decimal("0.01"))
    check("硬退出流水可独立读取", len(m.hard_exits("lc-1")) == 1)

    # 反向：策略显式放弃持仓（预期归零）**不抹掉**已发生的硬退出事实。
    m.set_expectation(lifecycle_id="lc-1", symbol="BTCUSDT", position_side="LONG",
                      expected_qty="0", now_ms=_NOW + 2, strategy_id="s-trend",
                      ticket_id="tk-1")
    check("预期归零后硬退出事实仍在", len(m.hard_exits("lc-1")) == 1)
    check("预期归零表达为 FLAT",
          m.expectation("lc-1").state is ExpectationState.FLAT)
    check("两条命名空间版本独立",
          m.expectation("lc-1").revision == 1 and m.hard_exits("lc-1")[0].exit_id == "hx-1")


def test_legacy_positions_never_implicitly_exited_on_restart() -> None:
    print("\n[2] 旧仓显式迁移、重启不隐式批量退出")
    m = _manager()
    positions = [
        {"lifecycle_id": "lc-old", "symbol": "ETHUSDT", "position_side": "LONG",
         "qty": "0.5"},                                     # 无归属 → 旧仓
        {"lifecycle_id": "lc-new", "symbol": "BTCUSDT", "position_side": "SHORT",
         "qty": "0.02", "strategy_id": "s1", "ticket_id": "tk-1"},  # 有归属
    ]
    plan = m.plan_restart(positions, now_ms=_NOW)
    check("重启计划不含退出动作", plan.exit_actions == (), plan.as_dict())
    check("重启计划不自称批量退出", plan.implicit_bulk_exit is False)
    check("结构化导出退出动作数为 0", plan.as_dict()["exit_action_count"] == 0)
    legacy = [item for item in plan.actions if item.kind == "MIGRATE_EXPLICIT"]
    check("旧仓被标记为显式迁移", len(legacy) == 1 and legacy[0].symbol == "ETHUSDT",
          [a.as_dict() for a in plan.actions])
    check("旧仓迁移原因码正确",
          legacy[0].reason_code == REASON_LEGACY_REQUIRES_MIGRATION)
    check("旧仓分类为 LEGACY_UNATTRIBUTED",
          plan.legacy[0].classification is PositionClass.LEGACY_UNATTRIBUTED)
    check("重启后旧仓仍待迁移", len(m.pending_legacy()) == 1)
    # 归属仓位不得被判为旧仓：它既不进 legacy 集合，也不产生迁移动作。
    check("有归属仓位不误判为旧仓",
          len(plan.legacy) == 1
          and all(a.symbol != "BTCUSDT" or a.kind != "MIGRATE_EXPLICIT"
                  for a in plan.actions), [a.as_dict() for a in plan.actions])

    # 显式迁移需要 actor/reason；缺一即拒绝。
    raised = False
    try:
        m.migrate_legacy("lc-old", target_source="strategy", actor="", reason="x",
                         now_ms=_NOW)
    except ValueError:
        raised = True
    check("缺执行者的迁移被拒绝", raised)
    migration = m.migrate_legacy("lc-old", target_source="strategy", actor="operator-1",
                                 reason="运营确认接管旧仓", now_ms=_NOW + 1)
    check("显式迁移留下审计记录", migration.actor == "operator-1"
          and migration.lifecycle_id == "lc-old")
    check("迁移后不再待迁移", len(m.pending_legacy()) == 0)

    # 护栏：重启上下文里出现批量退出动作必须 fail-closed 抛错。
    blocked = False
    try:
        m.assert_no_implicit_bulk_exit(
            context="restart", actions=[{"action": "close_all", "symbol": "ETHUSDT"}])
    except ImplicitBulkExitError as exc:
        blocked = REASON_IMPLICIT_BULK_EXIT_BLOCKED in str(exc)
    check("重启批量退出被护栏拦截", blocked)


def test_duplicate_exit_never_opens_reverse_position() -> None:
    print("\n[3] 重复退出不反向开仓")
    m = _manager()
    m.set_expectation(lifecycle_id="lc-1", symbol="BTCUSDT", position_side="LONG",
                      expected_qty="0.01", now_ms=_NOW, strategy_id="s1", ticket_id="t1")
    first = m.request_exit(exit_key="exit-1", lifecycle_id="lc-1", symbol="BTCUSDT",
                           position_side="LONG", qty="0.01", current_qty="0.01")
    check("首次退出被接受", first.accepted and first.action == "SUBMIT_REDUCE")
    check("退出方向由持仓侧决定", first.side == "SELL" and exit_side_for("SHORT") == "BUY")
    check("退出恒为 reduce_only", first.reduce_only is True)
    check("退出永不标注为开仓", first.opens_position is False)

    duplicate = m.request_exit(exit_key="exit-1", lifecycle_id="lc-1", symbol="BTCUSDT",
                               position_side="LONG", qty="0.01", current_qty="0.01")
    check("重复退出被幂等识别", duplicate.duplicate is True and duplicate.accepted is False)
    check("重复退出不产生提交量", duplicate.submit_qty == Decimal(0), duplicate.as_dict())
    check("重复退出的原因码正确",
          duplicate.reason_codes == (REASON_DUPLICATE_EXIT_IGNORED,))
    check("重复退出仍是 reduce_only/非开仓",
          duplicate.reduce_only is True and duplicate.opens_position is False)
    check("重复退出未二次占用仓量",
          m.ledger("lc-1").reserved_qty == Decimal("0.01"))

    # 请求量超过可减量：夹逼后不产生负仓/反向，剩余可减量为 0 时直接 no-op。
    over = m.request_exit(exit_key="exit-2", lifecycle_id="lc-1", symbol="BTCUSDT",
                         position_side="LONG", qty="5", current_qty="0.01")
    check("超量退出不产生提交", over.submit_qty == Decimal(0) and over.action == "NOOP")
    check("超量退出仍是减仓/非开仓",
          over.opens_position is False and over.side == "SELL")
    # 空仓（或已全部在途）时退出不得变成 BUY 开仓。
    empty = m.request_exit(exit_key="exit-3", lifecycle_id="lc-1", symbol="BTCUSDT",
                          position_side="SHORT", qty="0.01", current_qty="0")
    check("空仓退出是 no-op 而非反向开仓",
          empty.action == "NOOP" and empty.side == "BUY" and empty.opens_position is False)


def test_reservation_conserves_under_concurrent_exits() -> None:
    print("\n[4] 管理预留原子处理并发退出（R09 守恒）")
    ledger = ManagementReservationLedger(position_qty="1.0", lifecycle_id="lc-race")
    first = ledger.reserve(reservation_id="r-1", qty="0.6")
    check("首笔预留成功", first.accepted)
    dup = ledger.reserve(reservation_id="r-1", qty="0.6")
    check("重复预留幂等不二次占用",
          dup.duplicate and dup.reason_codes == (REASON_RESERVATION_DUPLICATE,))
    check("重复预留后预留量不变", dup.reserved_qty == Decimal("0.6"), dup.as_dict())
    oversell = ledger.reserve(reservation_id="r-2", qty="0.5")
    check("超出可用量的预留被拒",
          not oversell.accepted
          and REASON_RESERVATION_EXCEEDS_POSITION in oversell.reason_codes)
    check("被拒的预留不改变守恒式", reservation_conserved(ledger))
    check("可用量 = 仓位 - 在途预留",
          ledger.available_qty == Decimal("0.4"), ledger.as_dict())

    # 重复 consume 不得重复扣减（在途 TP/退出重复消耗仓量 = R09 复现形态）。
    consumed = ledger.consume("r-1")
    check("确认退出扣减预留", consumed.accepted and consumed.consumed_qty == Decimal("0.6"))
    again = ledger.consume("r-1")
    check("重复确认不重复扣减",
          not again.accepted
          and REASON_RESERVATION_ALREADY_CLOSED in again.reason_codes, again.as_dict())
    check("重复确认后累计成交不变",
          ledger.reservation("r-1").consumed_qty == Decimal("0.6"))
    partial = ledger.reserve(reservation_id="r-3", qty="0.2")
    too_much = ledger.consume("r-3", engaged_qty="0.5")
    check("确认量超预留被拒",
          partial.accepted and not too_much.accepted
          and REASON_RESERVATION_CONSUME_EXCEEDS in too_much.reason_codes,
          too_much.as_dict())
    check("被拒确认不改变累计成交",
          ledger.reservation("r-3").consumed_qty == Decimal(0))
    check("守恒式仍成立", reservation_conserved(ledger))

    # 部分成交/对账后仓位变小：溢出预留被释放并告警，而非把可用量算成负数。
    reasons = ledger.reconcile_position("0.1")
    check("仓位缩小释放溢出预留",
          REASON_RESERVATION_STALE in reasons, reasons)
    check("缩小后无超额预留", ledger.reserved_qty <= Decimal("0.1"),
          ledger.as_dict())
    check("缩小后可用量非负", ledger.available_qty >= 0)
    check("缩小后守恒式成立", reservation_conserved(ledger))

    # 真并发：8 个线程各预留 0.25，仓位 1.0 → 恰好 4 个成功，总量守恒。
    racing = ManagementReservationLedger(position_qty="1.0", lifecycle_id="lc-race2")
    accepted: list[str] = []
    guard = threading.Lock()

    def worker(index: int) -> None:
        outcome = racing.reserve(reservation_id=f"t-{index}", qty="0.25")
        if outcome.accepted:
            with guard:
                accepted.append(outcome.reservation_id)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("并发下超额预留被原子拒绝", len(accepted) == 4, sorted(accepted))
    check("并发后在途预留恰为仓量", racing.reserved_qty == Decimal("1.0"))
    check("并发后可用量为 0", racing.available_qty == Decimal("0"))
    check("并发后守恒式成立", reservation_conserved(racing))

    # Manager 层：两个并发退出键抢同一份仓量，第二个必须 no-op。
    m = _manager()
    for key in ("c-1", "c-2"):
        m.request_exit(exit_key=key, lifecycle_id="lc-1", symbol="BTCUSDT",
                       position_side="LONG", qty="0.01", current_qty="0.01")
    ledger_m = m.ledger("lc-1")
    check("Manager 层预留不超仓量", ledger_m.reserved_qty <= Decimal("0.01"))
    # 释放后可用量恢复（幂等）。
    check("释放在途退出成功", m.release_exit("c-1", reason="not_submitted"))
    check("重复释放被幂等识别", not m.release_exit("c-1", reason="again"))
    check("释放后可减量恢复", ledger_m.available_qty == Decimal("0.01"))


def test_expired_authorization_does_not_block_unknown_reconcile() -> None:
    print("\n[5] 过期授权不阻断 UNKNOWN 查单/收口")
    generation = _generation()
    authorization = issue_management_authorization(
        authorization_id="auth-1", generation=generation, lifecycle_id="lc-1",
        position_side="LONG", max_reduce_qty="0.01", position_revision=3,
        nonce="nonce-1", issued_at_ms=_NOW, ttl_ms=1000,
        target_protection=ProtectionTarget(sl_client_id="sl-1", tp_client_id="tp-1"),
    )
    request = {
        "action": "REDUCE", "account_ref": generation.account_ref,
        "lifecycle_id": "lc-1", "position_side": "LONG", "reduce_qty": "0.005",
        "current_position_qty": "0.01", "requested_net_delta": "0",
        "runtime_instance_id": generation.runtime_instance_id,
        "environment": generation.environment, "remove_protection": False,
    }
    fresh = evaluate_management_request(
        authorization, request=request, now_ms=_NOW + 100, current_revision=3,
        generation=generation)
    check("时效内授权可提交", fresh.submit_allowed and fresh.reconcile_allowed,
          fresh.as_dict())

    expired = evaluate_management_request(
        authorization, request=request, now_ms=_NOW + 5000, current_revision=3,
        generation=generation)
    check("过期授权禁止新提交", expired.submit_allowed is False)
    check("过期授权仍允许收口", expired.reconcile_allowed is True, expired.as_dict())
    check("过期原因码为 AUTHORIZATION_EXPIRED",
          "AUTHORIZATION_EXPIRED" in expired.reason_codes)
    check("过期不影响契约收口位",
          validate_authorization(authorization, request=request, now_ms=_NOW + 5000,
                                 current_revision=3).reconcile_allowed is True)

    # 真实 UNKNOWN 收口路径：授权已过期，但仍按原 client id 查单且有界收敛。
    row = {
        "client_order_id": "mta-unknown-1", "command_kind": CommandKind.STANDARD.value,
        "symbol": "BTCUSDT", "status": "UNKNOWN", "query_attempt_count": 1,
        "unknown_since": _NOW + 1000, "next_retry_at": _NOW + 1000,
    }
    plan = plan_unknown_reconcile(row, now_ms=_NOW + 5000)
    check("过期授权下 UNKNOWN 仍规划查单", plan.decision.value == "QUERY_DUE",
          plan.decision)

    class _Broker:
        def __init__(self) -> None:
            self.queries: list[tuple[str, str]] = []

        def query_order(self, symbol: str, client_order_id: str):
            self.queries.append((symbol, client_order_id))
            return {"orderId": "ex-1", "clientOrderId": client_order_id}

    class _Outbox:
        def __init__(self) -> None:
            self.acked: list[str] = []

        def acknowledge(self, client_order_id: str, payload) -> None:
            self.acked.append(client_order_id)

    broker, outbox = _Broker(), _Outbox()
    report = execute_unknown_reconcile([row], broker=broker, outbox=outbox,
                                       now_ms=_NOW + 5000)
    check("UNKNOWN 收口按原 client id 查单",
          broker.queries == [("BTCUSDT", "mta-unknown-1")], broker.queries)
    check("UNKNOWN 收口收敛成功", report.queried == 1 and report.resolved == 1,
          report)
    check("UNKNOWN 收口零提交", report.submit_calls == 0)
    check("收口确认写回 outbox", outbox.acked == ["mta-unknown-1"])
    check("过期授权未阻断收口",
          expired.reconcile_allowed is True and report.resolved == 1)


def test_flat_close_preserves_manual_semantics_and_blocks_health_faults() -> None:
    print("\n[6] 保留 pause/emergency 语义（R11）")
    m = _manager()
    for fault, expected in (("HEALTH_DEGRADED", False), ("HEARTBEAT_STALE", False),
                            ("TIMEOUT", False), ("CONNECTION_RESET", False),
                            ("RATE_LIMITED", False), ("QUERY_FAILED", False),
                            ("WS_DISCONNECT", False), ("UNKNOWN_FAULT", False),
                            ("PROTECTION_FAILED", True), ("PLACE_FAILED", True)):
        decision = m.should_flat_close(fault=fault)
        check(f"故障 {fault} 全平许可={expected}", decision.allowed is expected,
              decision.as_dict())
    health = m.should_flat_close(fault="HEALTH_DEGRADED")
    check("健康故障原因码为不得全平",
          "HEALTH_FAULT_MUST_NOT_TRIGGER_FLAT_CLOSE" in health.reason_codes)
    check("故障判定不改风险档",
          health.changes_risk_profile is False
          and health.touches_pause_state is False
          and health.touches_emergency_state is False)
    emergency = m.should_flat_close(operator_emergency=True)
    check("人工 emergency 语义保留", emergency.allowed and emergency.action == "EMERGENCY_CLOSE")
    check("emergency 由操作者触发", emergency.trigger == "OPERATOR_EMERGENCY")
    check("人工路径同样不动控制位",
          emergency.touches_pause_state is False
          and emergency.touches_emergency_state is False)


def test_k04_contract_binding_only_reduces() -> None:
    print("\n[7] 与 K04 契约一致：绑定账户/仓位/上限/revision/时效，只减不增")
    generation = _generation()
    bound = issue_management_authorization(
        authorization_id="auth-2", generation=generation, lifecycle_id="lc-1",
        position_side="LONG", max_reduce_qty="0.005", position_revision=7,
        nonce="nonce-2", issued_at_ms=_NOW, ttl_ms=60_000)
    check("授权绑定运行代", bound.runtime_instance_id == generation.runtime_instance_id)
    check("授权绑定环境", bound.environment == generation.environment)
    check("授权绑定账户域", bound.account_ref == ledger_account_ref(generation))
    check("授权绑定仓位上限", bound.max_reduce_qty == Decimal("0.005"))
    check("授权绑定 revision", bound.position_revision == 7)
    check("授权带时效", bound.expires_at_ms == _NOW + 60_000)

    base = {
        "action": "REDUCE", "account_ref": generation.account_ref,
        "lifecycle_id": "lc-1", "position_side": "LONG",
        "current_position_qty": "0.01", "requested_net_delta": "0",
        "runtime_instance_id": generation.runtime_instance_id,
        "environment": generation.environment, "remove_protection": False,
    }

    def verdict(**overrides):
        payload = {**base, **overrides}
        return evaluate_management_request(
            bound, request=payload, now_ms=_NOW + 10, current_revision=7,
            generation=generation)

    ok = verdict(reduce_qty="0.005")
    check("授权范围内减仓放行", ok.submit_allowed, ok.as_dict())
    over = verdict(reduce_qty="0.01")
    check("超过授权上限被拒", not over.submit_allowed
          and "REDUCE_EXCEEDS_AUTHORIZATION" in over.reason_codes, over.reason_codes)
    open_req = verdict(action="OPEN", reduce_qty="0.001")
    check("OPEN 不属于管理动作", not open_req.submit_allowed
          and "OPEN_NOT_ALLOWED" in open_req.reason_codes)
    increase = verdict(reduce_qty="0.001", requested_net_delta="0.5")
    check("净敞口增加被拒", not increase.submit_allowed
          and "NET_EXPOSURE_INCREASE" in increase.reason_codes)
    check("reduce_only 守卫独立可复核",
          reduce_only_guard({"requested_net_delta": "0.5"}) == (REASON_REDUCE_ONLY_VIOLATED,)
          and reduce_only_guard({"requested_net_delta": "-0.5"}) == ())
    stale = evaluate_management_request(
        bound, request={**base, "reduce_qty": "0.001"}, now_ms=_NOW + 10,
        current_revision=8, generation=generation)
    check("revision 变化使授权失效", not stale.submit_allowed
          and "STALE_POSITION_REVISION" in stale.reason_codes)
    cross = verdict(reduce_qty="0.001", environment="live")
    check("跨环境请求被拒", not cross.submit_allowed
          and "ENVIRONMENT_MISMATCH" in cross.reason_codes)
    other = verdict(reduce_qty="0.001", account_ref="demo:OTHER")
    check("异账户请求被拒且不允许收口", not other.submit_allowed
          and other.reconcile_allowed is False
          and "ACCOUNT_MISMATCH" in other.reason_codes)
    replay = evaluate_management_request(
        bound, request={**base, "reduce_qty": "0.001"}, now_ms=_NOW + 10,
        current_revision=7, consumed_nonces=("nonce-2",), generation=generation)
    check("nonce 重放被拒", not replay.submit_allowed
          and "NONCE_REPLAY" in replay.reason_codes)

    # 最后保护不可撤（授权侧已把它标为最后一道）。
    last = issue_management_authorization(
        authorization_id="auth-3", generation=generation, lifecycle_id="lc-1",
        position_side="LONG", max_reduce_qty="0.005", position_revision=7,
        nonce="nonce-3", issued_at_ms=_NOW, ttl_ms=60_000,
        target_protection=ProtectionTarget(sl_client_id="sl-only", is_last_protection=True))
    remove_last = evaluate_management_request(
        last, request={**base, "action": "MAINTAIN_PROTECTION", "remove_protection": True,
                       "target_client_id": "sl-only"},
        now_ms=_NOW + 10, current_revision=7, generation=generation)
    check("不允许撤掉最后一道保护", not remove_last.submit_allowed
          and "CANNOT_REMOVE_LAST_PROTECTION" in remove_last.reason_codes)

    # 未绑定运行代/环境的授权 fail-closed，不可构造。
    raised = False
    try:
        issue_management_authorization(
            authorization_id="auth-4", generation=None, lifecycle_id="lc-1",
            position_side="LONG", max_reduce_qty="0.005", position_revision=1,
            nonce="n", issued_at_ms=_NOW, ttl_ms=1000)
    except ManagementAuthorizationError as exc:
        raised = REASON_AUTHORIZATION_UNBOUND in exc.reason_codes
    check("无运行代的授权拒绝构造", raised)
    unbindable = ManagementAuthorization(
        authorization_id="auth-5", account_ref=generation.account_ref,
        lifecycle_id="lc-1", position_side="LONG", max_reduce_qty=Decimal("0.005"),
        target_protection=ProtectionTarget(), position_revision=7, nonce="n5",
        issued_at_ms=_NOW, expires_at_ms=_NOW + 1000, environment="", runtime_instance_id="")
    unbound = evaluate_management_request(
        unbindable, request={**base, "reduce_qty": "0.001"}, now_ms=_NOW + 10,
        current_revision=7, generation=generation)
    check("空串绑定视同未绑定并拒绝提交", not unbound.submit_allowed
          and "RUN_GENERATION_UNBOUND" in unbound.reason_codes, unbound.reason_codes)


def test_account_domain_single_source_and_u15_blocker() -> None:
    print("\n[U15] 账本 account_ref 消费运行代派生值（跨任务阻塞面）")
    generation = _generation()
    check("账本 account_ref = 运行代派生值",
          ledger_account_ref(generation) == "demo:SYNTH_ACCT")
    domain = resolve_account_domain(generation)
    check("账户域自洽可绑定", domain.bound, domain.as_dict())
    check("身份域与账本域一致时无 reason",
          assert_account_domains_consistent(generation, "demo:SYNTH_ACCT") == ())
    drifted = assert_account_domains_consistent(
        generation, "demo:8c9c2ba2efcdc984")   # 旧 sha256(api_key)[:16] 形态
    check("身份域与账本域不一致被显式报告",
          drifted == (REASON_ACCOUNT_DOMAIN_MISMATCH,), drifted)

    # 轮换密钥不改变账户域（U15 要求 4），因此账本/K03 幂等键分区不被轮换切裂。
    rotated = rotate_credential(generation, new_key_fingerprint="deadbeefdeadbeef")
    check("轮换后账户域不变", rotated.account_ref == generation.account_ref)
    check("轮换后运行代不变", rotated.generation_id == generation.generation_id)
    check("轮换后账本引用仍一致",
          assert_account_domains_consistent(rotated, "demo:SYNTH_ACCT") == ())

    # 账户域不符的运行代不得通过绑定。
    other = _generation(account="SOMEONE-ELSE", instance="rt-other", started_at=9.0)
    mismatch = resolve_account_domain(other, generation)
    check("异账户运行代绑定失败", not mismatch.bound
          and "ACCOUNT_REF_MISMATCH" in mismatch.reason_codes, mismatch.as_dict())
    check("异账户运行代不得用于提交",
          not evaluate_management_request(
              issue_management_authorization(
                  authorization_id="auth-x", generation=generation,
                  lifecycle_id="lc-1", position_side="LONG", max_reduce_qty="0.005",
                  position_revision=0, nonce="nx", issued_at_ms=_NOW, ttl_ms=1000),
              request={"action": "REDUCE", "account_ref": generation.account_ref,
                       "lifecycle_id": "lc-1", "position_side": "LONG",
                       "reduce_qty": "0.001", "current_position_qty": "0.01",
                       "requested_net_delta": "0", "environment": "demo",
                       "runtime_instance_id": "rt-other", "remove_protection": False},
              now_ms=_NOW + 10, current_revision=0, generation=other).submit_allowed)
    other_env = _generation(environment="live", account="SYNTH_ACCT",
                            instance="rt-live", started_at=2.0)
    env_mismatch = resolve_account_domain(other_env, generation)
    check("跨环境运行代绑定失败", not env_mismatch.bound, env_mismatch.reason_codes)


def main() -> int:
    test_hard_exit_and_expectation_are_separate_namespaces()
    test_legacy_positions_never_implicitly_exited_on_restart()
    test_duplicate_exit_never_opens_reverse_position()
    test_reservation_conserves_under_concurrent_exits()
    test_expired_authorization_does_not_block_unknown_reconcile()
    test_flat_close_preserves_manual_semantics_and_blocks_health_faults()
    test_k04_contract_binding_only_reduces()
    test_account_domain_single_source_and_u15_blocker()
    print(f"\nU02 strategy position manager: PASS={PASS_COUNT} FAIL={len(FAILURES)}")
    if FAILURES:
        for item in FAILURES:
            print(f"  - {item}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
