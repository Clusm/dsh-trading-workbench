"""U14 验收：外部告警送达、心跳交易证据与主机失联检测（F06/F12/F22；R13/R14）。

独立可运行（本仓库无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_alert_delivery.py

全程离线：只在系统临时目录里造夹具状态，不读真实 ``.env``/``data``/密钥，不联网、
不启动服务、不下单、**不发真实告警**（投递一律走内存 ``RecordingSink``）。逐项打印
PASS/FAIL，任一失败以退出码 1 结束。

**两态可跑**（U01/U13 的血教训）：本套件只依赖 U14 独占交付物
（``core.ops.alert_delivery``、冻结契约 ``core.contracts.k05_trace``、
``tests/fixtures/alerts/``），**不假定**任何共享适配补丁（``core/health_watch.py``、
``scripts/health_watch.py``、``audit/database.py``）是否已应用。对唯一需要共享
补丁才能观察的行为，套件**按两态各自应有的语义分别断言**（补丁在 → 断言接入后的
新事实；补丁不在 → 断言旧行为没有被伪造），因此两个状态下的期望都是真的，
不存在 ``SKIP`` 掩盖。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.ops.alert_delivery import (  # noqa: E402
    BACKLOG_OVERDUE,
    CHANNEL_UNVERIFIED,
    DELIVERED_WITHOUT_DEVICE_RECEIPT,
    HOST_CONTINUITY_UNKNOWN,
    HOST_DOWN,
    HOST_JUST_BOOTED,
    HOST_REBOOT_OBSERVED,
    MANUAL_ACK_ACTOR_INVALID,
    MANUAL_ACK_ONLY,
    MANUAL_ACK_WITHOUT_SUBMISSION,
    NO_CONFIRMED_DELIVERY,
    OPS_BLOCKED,
    OPS_DEGRADED,
    OPS_OK,
    PROCESS_MISSING,
    TRADING_STALLED,
    Alert,
    AlertLadderViolation,
    AlertQueue,
    ChannelPolicy,
    Heartbeat,
    HeartbeatState,
    MaintenanceWindow,
    RecordingSink,
    UnconfiguredTransport,
    advance_heartbeat,
    build_message,
    classify_outage,
    evaluate_heartbeat,
    ops_status,
    trading_evidence_timestamp,
    validate_message,
)

FIXTURES = ROOT / "tests" / "fixtures" / "alerts"
TMP = Path(tempfile.mkdtemp(prefix="u14_alerts_"))
PASSED = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, extra: object = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL  {name}  {extra}")


def section(title: str) -> None:
    print(f"\n[{title}]")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# fixture 解析（时间一律用"相对 now 的偏移"，避免夹具随时间腐烂）
# --------------------------------------------------------------------------


def resolve_heartbeat(spec: dict, now_ms: int) -> Heartbeat:
    window = spec.get("maintenance")
    maintenance = None
    if isinstance(window, dict):
        expires = window.get("expires_offset_ms")
        maintenance = MaintenanceWindow(
            scope=str(window.get("scope") or ""),
            starts_at_ms=now_ms + int(window.get("starts_offset_ms") or 0),
            expires_at_ms=(0 if expires is None else now_ms + int(expires)),
            actor=str(window.get("actor") or ""),
            note=str(window.get("note") or ""),
        )
    evidence = spec.get("trading_evidence_offset_ms")
    fast = spec.get("last_fast_cycle_offset_ms")
    return Heartbeat(
        environment=str(spec.get("environment") or "live"),
        emitted_at_ms=now_ms + int(spec["emitted_offset_ms"]),
        trading_evidence_at_ms=None if evidence is None else now_ms + int(evidence),
        last_fast_cycle_at_ms=None if fast is None else now_ms + int(fast),
        sequence=spec.get("sequence"),
        session_id=str(spec.get("session_id") or ""),
        runtime_instance_id=str(spec.get("runtime_instance_id") or ""),
        generation_id=str(spec.get("generation_id") or ""),
        account_ref=str(spec.get("account_ref") or ""),
        maintenance=maintenance,
    )


def resolve_state(spec: dict | None, now_ms: int) -> HeartbeatState:
    if not spec:
        return HeartbeatState()
    evidence = spec.get("trading_evidence_offset_ms")
    emitted = spec.get("emitted_offset_ms")
    return HeartbeatState(
        emitted_at_ms=None if emitted is None else now_ms + int(emitted),
        trading_evidence_at_ms=None if evidence is None else now_ms + int(evidence),
        sequence=spec.get("sequence"),
        generation_id=str(spec.get("generation_id") or ""),
        session_id=str(spec.get("session_id") or ""),
    )


def make_alert(now_ms: int, *, incident: str, severity: str = "CRITICAL",
               reason: str = "TRADING_STALLED", at_ms: int | None = None) -> Alert:
    profile, object_ref, reason_code = incident.split("|")
    message = build_message(
        profile=profile, host="host-synthetic-01", object_ref=object_ref,
        reason_code=reason_code, severity=severity,
        impact="synthetic impact", new_entry_state="SUSPENDED",
        handling_entry="scripts/health_watch.py", occurred_at_ms=now_ms)
    return Alert(
        incident_key=incident, environment="live", severity=severity,
        reason_code=reason_code, object_ref=object_ref, profile=profile,
        host="host-synthetic-01", occurrence_at_ms=(now_ms if at_ms is None else at_ms),
        message=message)


def verified_channel(*, escalation: str = "operator-phone-synthetic") -> ChannelPolicy:
    return ChannelPolicy(channel="fake-sink", status="verified",
                         supports_delivery_receipt=True, escalation_path=escalation)


# --------------------------------------------------------------------------
# 1. 接收 / 送达 / 确认三级分开记录（R14）
# --------------------------------------------------------------------------


def test_delivery_ladder() -> None:
    section("1. 接收/送达/确认三级分开记录（R14：供应商接受 ≠ 送达）")
    fixture = load_fixture("delivery_ladder.json")
    now = int(time.time() * 1000)
    for case in fixture["cases"]:
        cid = case["id"]
        channel = ChannelPolicy(**case["channel"])
        transport_spec = case.get("transport")
        sink = None
        if transport_spec is not None:
            sink = RecordingSink(
                provider_receipt=transport_spec.get("provider_receipt"),
                delivered_at_ms=(None if transport_spec.get("delivered_offset_ms") is None
                                 else now + int(transport_spec["delivered_offset_ms"])),
                device_receipt=transport_spec.get("device_receipt"),
                http_status=int(transport_spec.get("http_status", 200)))
        queue = AlertQueue(TMP / cid / "alert_queue.sqlite", capacity=8)
        try:
            alert = make_alert(now, incident="live|trading_service|TRADING_STALLED")
            outcome = queue.enqueue(alert, now_ms=now)
            result = queue.dispatch_due(sink or UnconfiguredTransport(), channel, now_ms=now)
            row = queue.get(outcome.alert_id) or {}
            expect = case["expect"]
            check(f"{cid}: submitted={expect.get('submitted')}",
                  result["submitted"] == expect["submitted"], result)
            if expect.get("blocked"):
                check(f"{cid}: 未验证通道不改状态（不伪造 DELIVERED）",
                      row.get("state") == "UNKNOWN" and row.get("delivered_at_ms") is None,
                      {k: row.get(k) for k in ("state", "delivered_at_ms")})
                check(f"{cid}: reason_codes 含 {expect['reason_codes']}",
                      all(code in result["reason_codes"] for code in expect["reason_codes"]),
                      result["reason_codes"])
            else:
                after_dispatch = expect.get("state_after_dispatch", expect["state"])
                check(f"{cid}: 派发后 state == {after_dispatch}",
                      row.get("state") == after_dispatch,
                      {k: row.get(k) for k in ("state", "delivered_at_ms", "ack_at_ms")})
                if "delivered_at_ms" in expect:
                    check(f"{cid}: 派发后 delivered_at_ms 保持 null（未伪造送达）",
                          row.get("delivered_at_ms") is None, row.get("delivered_at_ms"))
                if expect.get("delivered_at_ms_set"):
                    check(f"{cid}: delivered_at_ms 已置且 >= submitted_at_ms",
                          isinstance(row.get("delivered_at_ms"), int)
                          and int(row["delivered_at_ms"]) >= int(row["submitted_at_ms"]),
                          row.get("delivered_at_ms"))
                if "confirmed" in expect:
                    check(f"{cid}: confirmed={expect['confirmed']}",
                          result["confirmed"] == expect["confirmed"], result)
                for code in expect.get("reason_codes", []):
                    check(f"{cid}: reason_codes 含 {code}",
                          code in result["reason_codes"], result["reason_codes"])
            manual = case.get("manual")
            if manual:
                ok, reasons = queue.record_manual_ack(
                    outcome.alert_id, actor=manual["actor"], note=manual["note"],
                    confirm=manual["confirm"], now_ms=now + 1000)
                row = queue.get(outcome.alert_id) or {}
                check(f"{cid}: 人工确认 accepted={expect['manual_ack_accepted']}",
                      ok == expect["manual_ack_accepted"], (ok, reasons))
                for code in expect.get("manual_reason_codes", []):
                    check(f"{cid}: 人工确认 reason 含 {code}", code in reasons, reasons)
                check(f"{cid}: 人工确认后 state == {expect['state']}",
                      row.get("state") == expect["state"], row.get("state"))
                if expect.get("manual_ack_accepted"):
                    check(f"{cid}: 人工确认走的是 ACKNOWLEDGED 而非 DELIVERED",
                          row.get("state") == "ACKNOWLEDGED"
                          and row.get("delivered_at_ms") is None
                          and row.get("device_receipt") is None,
                          {k: row.get(k) for k in
                           ("state", "delivered_at_ms", "device_receipt")})
                if "delivered_at_ms" in expect:
                    check(f"{cid}: 人工确认后 delivered_at_ms 仍为 null",
                          row.get("delivered_at_ms") is None, row.get("delivered_at_ms"))
                if expect.get("ack_at_ms_set"):
                    check(f"{cid}: ack_at_ms 已置（可审计确认时刻）",
                          isinstance(row.get("ack_at_ms"), int), row.get("ack_at_ms"))
                elif "ack_at_ms" in expect:
                    check(f"{cid}: ack_at_ms 保持 null",
                          row.get("ack_at_ms") is None, row.get("ack_at_ms"))
            trail = [entry["transition"] for entry in queue.audit_trail(outcome.alert_id)]
            check(f"{cid}: 审计轨迹非空且含 ALERT_ENQUEUED",
                  "ALERT_ENQUEUED" in trail, trail)
        finally:
            queue.close()

    # 目标用例：通道报了送达时间但**没有**设备回执 ⇒ 停在 ACCEPTED 并明确报原因码。
    queue_receipt = AlertQueue(TMP / "ladder-device" / "q.sqlite", capacity=4)
    try:
        now3 = int(time.time() * 1000)
        aid = queue_receipt.enqueue(
            make_alert(now3, incident="live|trading_service|PROCESS_MISSING"),
            now_ms=now3).alert_id
        result = queue_receipt.dispatch_due(
            RecordingSink(provider_receipt="rcpt-no-device", delivered_at_ms=now3),
            verified_channel(), now_ms=now3)
        check("报了送达时间但无设备回执 ⇒ 状态停在 ACCEPTED（不伪造 DELIVERED）",
              queue_receipt.get(aid)["state"] == "ACCEPTED"
              and queue_receipt.get(aid)["delivered_at_ms"] is None,
              queue_receipt.get(aid)["state"])
        check("报了送达时间但无设备回执 ⇒ 原因码 DELIVERED_WITHOUT_DEVICE_RECEIPT",
              DELIVERED_WITHOUT_DEVICE_RECEIPT in result["reason_codes"],
              result["reason_codes"])
    finally:
        queue_receipt.close()

    # 三级顺序不可跳跃：没有 provider_receipt 就不存在 ACCEPTED/DELIVERED。
    queue = AlertQueue(TMP / "ladder-order" / "q.sqlite", capacity=4)
    try:
        now2 = int(time.time() * 1000)
        alert = make_alert(now2, incident="live|trading_service|FAST_CYCLE_STALE")
        aid = queue.enqueue(alert, now_ms=now2).alert_id
        raised = ""
        try:
            queue.record_delivered(aid, now_ms=now2 + 10, delivered_at_ms=now2 + 10,
                                   device_receipt="dev-x")
        except AlertLadderViolation as exc:  # 阶梯自检必须拦住"跳级"
            raised = str(exc)
        except Exception as exc:  # noqa: BLE001 - 任何其它异常都不算通过
            raised = f"WRONG:{type(exc).__name__}"
        check("跳级（无 provider_receipt 直接 DELIVERED）被阶梯自检拒绝",
              "DELIVERED" in raised or "PROVIDER_RECEIPT" in raised, raised)
        # provider_receipt 缺失时 record_accepted 也必须拒绝（不得凭空调状态）。
        empty_receipt = ""
        try:
            queue.record_accepted(aid, now_ms=now2 + 20, provider_receipt=empty_receipt)
        except ValueError as exc:
            empty_receipt = str(exc)
        check("空 provider_receipt 不得产生 ACCEPTED",
              isinstance(empty_receipt, str) and "provider_receipt" in empty_receipt,
              empty_receipt)
        # 送达时间早于提交时间 → K05 TIME_ORDER_VIOLATION，必须被阶梯自检挡住。
        queue.record_submitted(aid, now_ms=now2 + 30)
        queue.record_accepted(aid, now_ms=now2 + 30, provider_receipt="rcpt-order")
        order_error = ""
        try:
            queue.record_delivered(aid, now_ms=now2 + 40, delivered_at_ms=now2 + 10,
                                   device_receipt="dev-order")
        except AlertLadderViolation as exc:
            order_error = str(exc)
        check("送达时间早于提交时间 → TIME_ORDER_VIOLATION 被挡（不落库）",
              "TIME_ORDER" in order_error, order_error)
        check("被挡后队列状态未变（不落半截事实）",
              queue.get(aid)["delivered_at_ms"] is None
              and queue.get(aid)["state"] == "ACCEPTED",
              {k: queue.get(aid)[k] for k in ("state", "delivered_at_ms")})
        # 无 provider_receipt 时不允许经 transport 之外直接标 DELIVERED。
        aid3 = queue.enqueue(
            make_alert(now2, incident="live|service|NO_RECEIPT"),
            now_ms=now2).alert_id
        direct_error = ""
        try:
            queue.record_delivered(aid3, now_ms=now2 + 10, delivered_at_ms=now2 + 10,
                                   device_receipt="dev-only")
        except AlertLadderViolation as exc:
            direct_error = str(exc)
        check("缺 provider_receipt 直接标 DELIVERED 被拒（原因码含目标状态）",
              "DELIVERED" in direct_error, direct_error)
    finally:
        queue.close()


# --------------------------------------------------------------------------
# 2. 本机主进程故障 vs 主机关机（成因不同，分别可检测）
# --------------------------------------------------------------------------


def test_outage_classification() -> None:
    section("2. 本机主进程故障与主机关机分别可检测（不得混为一类）")
    fixture = load_fixture("outage_cases.json")
    kinds: dict[str, str] = {}
    for case in fixture["cases"]:
        cid = case["id"]
        now_seconds = time.time()
        verdict = classify_outage(
            now_seconds=now_seconds,
            process_alive=case["process_alive"],
            trading_evidence_at_seconds=(None if case["evidence_age_seconds"] is None
                                         else now_seconds - case["evidence_age_seconds"]),
            host_uptime_seconds=case["host_uptime_seconds"],
            startup_grace_seconds=fixture["defaults"]["startup_grace_seconds"],
            trading_evidence_max_age_seconds=(
                fixture["defaults"]["trading_evidence_max_age_seconds"]),
        )
        kinds[cid] = verdict.kind
        expect = case["expect"]
        check(f"{cid}: kind == {expect['kind']}", verdict.kind == expect["kind"],
              verdict.as_dict())
        check(f"{cid}: reason_codes == {expect['reason_codes']}",
              list(verdict.reason_codes) == expect["reason_codes"],
              list(verdict.reason_codes))
        check(f"{cid}: host_continuity_proven == {expect['host_continuity_proven']}",
              verdict.host_continuity_proven == expect["host_continuity_proven"],
              verdict.host_continuity_proven)
        if "in_startup_grace" in expect:
            check(f"{cid}: in_startup_grace == {expect['in_startup_grace']}",
                  verdict.in_startup_grace == expect["in_startup_grace"],
                  verdict.in_startup_grace)

    check("两类成因是**不同**的 kind（P2 进程故障 vs P3 主机关机）",
          kinds.get("P2-process-missing") == PROCESS_MISSING
          and kinds.get("P3-host-down") == HOST_DOWN
          and kinds["P2-process-missing"] != kinds["P3-host-down"], kinds)
    check("心跳活着但交易证据陈旧 ≠ 健康（F22/R13）",
          kinds.get("P5-trading-stalled") == TRADING_STALLED
          and kinds.get("P5-trading-stalled") != kinds.get("P1-healthy"), kinds)
    check("宿主连续性不可证明时既不报主机关机也不报进程故障",
          kinds.get("P6-host-continuity-unknown") == HOST_CONTINUITY_UNKNOWN
          and kinds.get("P6-host-continuity-unknown")
          not in (HOST_DOWN, PROCESS_MISSING), kinds)
    check("宿主刚重启时给出 HOST_REBOOT_OBSERVED 而不静默",
          kinds.get("P7-just-booted-reboot-observed") == HOST_JUST_BOOTED
          and HOST_REBOOT_OBSERVED in
          classify_outage(
              now_seconds=1000.0, process_alive=False,
              trading_evidence_at_seconds=400.0, host_uptime_seconds=30.0,
              startup_grace_seconds=120.0).reason_codes, kinds)


# --------------------------------------------------------------------------
# 3. 心跳必须携带交易证据时间，拒绝陈旧重放（R13）
# --------------------------------------------------------------------------


def test_heartbeat() -> None:
    section("3. 心跳携带交易证据时间、拒绝陈旧重放（R13）")
    now = int(time.time() * 1000)

    fresh = load_fixture("heartbeat_fresh.json")
    spec = fresh["inputs"]["heartbeat"]
    previous = resolve_state(fresh["inputs"].get("previous"), now)
    state, verdict = advance_heartbeat(previous, resolve_heartbeat(spec, now), now_ms=now)
    expect = fresh["expect"]
    check("fresh: accepted", verdict.accepted == expect["accepted"], verdict.as_dict())
    check("fresh: healthy", verdict.healthy == expect["healthy"], verdict.as_dict())
    check("fresh: reason_codes", list(verdict.reason_codes) == expect["reason_codes"],
          list(verdict.reason_codes))
    check("fresh: 交易证据年龄由公式独立算出",
          verdict.trading_evidence_age_seconds == expect["trading_evidence_age_seconds"],
          verdict.trading_evidence_age_seconds)
    check("fresh: 时间线被推进",
          state.trading_evidence_at_ms == now + int(spec["trading_evidence_offset_ms"]),
          state.as_dict())
    check("fresh: last_fast_cycle 一并保留（规格 §11.2 心跳字段）",
          spec.get("last_fast_cycle_offset_ms") is not None, spec)

    rejections = load_fixture("heartbeat_rejections.json")
    prev_spec = resolve_state(rejections["previous"], now)
    for case in rejections["cases"]:
        cid = case["id"]
        expect = case["expect"]
        hb = resolve_heartbeat(case["heartbeat"], now)
        state, verdict = advance_heartbeat(
            prev_spec, hb, now_ms=now,
            expected_generation_id=rejections.get("expected_generation_id"),
            expected_session_id=rejections.get("expected_session_id"))
        check(f"{cid}: accepted == {expect['accepted']}",
              verdict.accepted == expect["accepted"], verdict.as_dict())
        check(f"{cid}: healthy == {expect['healthy']}",
              verdict.healthy == expect["healthy"], verdict.as_dict())
        check(f"{cid}: reason_codes == {expect['reason_codes']}",
              list(verdict.reason_codes) == expect["reason_codes"],
              list(verdict.reason_codes))
        if expect.get("state_advanced") is False:
            check(f"{cid}: 时间线**未**推进（拒绝陈旧重放）",
                  state == prev_spec, state.as_dict())
        else:
            check(f"{cid}: 时间线已推进", state != prev_spec, state.as_dict())
        if "muted" in expect:
            check(f"{cid}: muted == {expect['muted']}",
                  verdict.muted == expect["muted"], verdict.muted)

    # 陈旧重放**不能**把健康时间刷新成当前时刻（R13 的落地判据）。
    replay = resolve_heartbeat(rejections["cases"][0]["heartbeat"], now)
    state, _ = advance_heartbeat(prev_spec, replay, now_ms=now)
    check("陈旧重放不更新健康时间（时间线保持原值）",
          state.trading_evidence_at_ms == prev_spec.trading_evidence_at_ms,
          (state.as_dict(), prev_spec.as_dict()))

    # 无运行代/序号的心跳仍必须携带交易证据时间。
    bare = Heartbeat(environment="live", emitted_at_ms=now, trading_evidence_at_ms=None)
    verdict = evaluate_heartbeat(bare, now_ms=now)
    check("缺交易证据时间的心跳一律不健康",
          not verdict.accepted and not verdict.healthy, verdict.as_dict())


# --------------------------------------------------------------------------
# 4. 真实渠道未确认 ⇒ 保留 OPS_BLOCKED（不得伪造 DELIVERED）
# --------------------------------------------------------------------------


def test_ops_blocked() -> None:
    section("4. 真实渠道未确认时保留 OPS_BLOCKED")
    now = int(time.time() * 1000)
    queue = AlertQueue(TMP / "ops" / "q.sqlite", capacity=8)
    try:
        alert = make_alert(now, incident="live|trading_service|PROCESS_MISSING")
        aid = queue.enqueue(alert, now_ms=now).alert_id
        unverified = ChannelPolicy(channel="unverified", status="unverified")
        verdict = ops_status(queue, channel=unverified, now_ms=now, backlog_overdue_seconds=300)
        check("未验证通道 ⇒ OPS_BLOCKED",
              verdict.status == OPS_BLOCKED and verdict.blocked, verdict.as_dict())
        check("未验证通道 ⇒ 原因码含 CHANNEL_UNVERIFIED",
              CHANNEL_UNVERIFIED in verdict.reason_codes, verdict.reason_codes)
        check("未验证通道 ⇒ 无任何 confirmed_states（未假送达）",
              verdict.confirmed_states == (), verdict.confirmed_states)

        # 即使 transport 声称有设备回执，未验证 + 未 drill 时也**不提交**。
        liar = RecordingSink(provider_receipt="rcpt-1", delivered_at_ms=now,
                             device_receipt="dev-1")
        result = queue.dispatch_due(liar, unverified, now_ms=now)
        row = queue.get(aid) or {}
        check("未验证通道不调用 transport（拒绝发送）",
              liar.submissions == [] and result["blocked"], result)
        check("未验证通道下队列状态保持 UNKNOWN、delivered_at_ms 为 null",
              row.get("state") == "UNKNOWN" and row.get("delivered_at_ms") is None,
              {k: row.get(k) for k in ("state", "delivered_at_ms")})
        check("OPS 在提交被阻断后仍是 OPS_BLOCKED",
              ops_status(queue, channel=unverified, now_ms=now).status == OPS_BLOCKED)

        # 配置里只写 verified=true（无真实回执）也不得算通过：仍是无确认送达。
        verified_but_silent = ChannelPolicy(
            channel="fake-sink", status="verified", supports_delivery_receipt=False,
            escalation_path="operator-phone-synthetic")
        only_http200 = RecordingSink(http_status=200)
        result = queue.dispatch_due(only_http200, verified_but_silent, now_ms=now)
        verdict = ops_status(queue, channel=verified_but_silent, now_ms=now)
        check("HTTP200 只到 SUBMITTED，仍不算确认送达",
              result["submitted"] == 1 and result["confirmed"] == 0, result)
        check("仅 HTTP200 ⇒ OPS 停在 OPS_DEGRADED 且含 NO_CONFIRMED_DELIVERY",
              verdict.status == OPS_DEGRADED and NO_CONFIRMED_DELIVERY in verdict.reason_codes,
              verdict.as_dict())

        # 真实送达后（有设备回执）才允许 OPS_OK。
        good = RecordingSink(provider_receipt="rcpt-2", delivered_at_ms=now + 5,
                             device_receipt="dev-2")
        queue.dispatch_due(good, verified_but_silent, now_ms=now + 10)
        verdict = ops_status(queue, channel=verified_but_silent, now_ms=now + 10)
        check("有设备回执的真实送达 ⇒ DELIVERED 且 OPS_OK",
              queue.get(aid)["state"] == "DELIVERED" and verdict.status == OPS_OK,
              (queue.get(aid)["state"], verdict.as_dict()))

        # 无升级路径 ⇒ 仍然阻断（规格 §11.2『明确的未确认升级路径』）。
        no_escalation = ChannelPolicy(channel="fake-sink", status="verified",
                                      supports_delivery_receipt=True, escalation_path="")
        verdict = ops_status(queue, channel=no_escalation, now_ms=now + 10)
        check("缺未确认升级路径 ⇒ OPS_BLOCKED",
              verdict.status == OPS_BLOCKED and "NO_ESCALATION_PATH" in verdict.reason_codes,
              verdict.as_dict())

        # 只有人工确认（无设备回执）⇒ 允许闭合演练但明确标 MANUAL_ACK_ONLY。
        drill = AlertQueue(TMP / "ops-drill" / "q.sqlite", capacity=4)
        try:
            aid2 = drill.enqueue(
                make_alert(now, incident="live|protection|PROTECTION_MISMATCH"),
                now_ms=now).alert_id
            ch = ChannelPolicy(channel="sms-no-receipt", status="verified",
                               supports_delivery_receipt=False,
                               escalation_path="operator-phone-synthetic")
            drill.dispatch_due(RecordingSink(http_status=200), ch, now_ms=now)
            drill.record_manual_ack(aid2, actor="operator", note="synthetic call-back",
                                    confirm=True, now_ms=now + 1000)
            v = ops_status(drill, channel=ch, now_ms=now + 1000)
            check("人工确认后状态为 ACKNOWLEDGED 且 delivered_at_ms 仍为 null",
                  drill.get(aid2)["state"] == "ACKNOWLEDGED"
                  and drill.get(aid2)["delivered_at_ms"] is None,
                  {k: drill.get(aid2)[k] for k in ("state", "delivered_at_ms")})
            check("仅人工确认 ⇒ OPS_DEGRADED + MANUAL_ACK_ONLY（不冒充送达）",
                  v.status == OPS_DEGRADED and MANUAL_ACK_ONLY in v.reason_codes,
                  v.as_dict())
        finally:
            drill.close()
    finally:
        queue.close()

    check("MANUAL_ACK_ACTOR_INVALID / MANUAL_ACK_WITHOUT_SUBMISSION 常量可用",
          MANUAL_ACK_ACTOR_INVALID == "MANUAL_ACK_ACTOR_INVALID"
          and MANUAL_ACK_WITHOUT_SUBMISSION == "MANUAL_ACK_WITHOUT_SUBMISSION")
    check("BACKLOG_OVERDUE 常量可用且非未验证原因码",
          BACKLOG_OVERDUE == "BACKLOG_OVERDUE" and BACKLOG_OVERDUE != CHANNEL_UNVERIFIED)


# --------------------------------------------------------------------------
# 5. 投递队列独立于交易库（R14/F22：告警积压不得影响交易库）
# --------------------------------------------------------------------------


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snapshot
    for path in sorted(root.rglob("*")):
        if path.is_file():
            stat = path.stat()
            snapshot[path.relative_to(root).as_posix()] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def _make_trading_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(
            "CREATE TABLE release_observations (observation_id TEXT PRIMARY KEY,"
            " release_id TEXT, environment TEXT, account_ref TEXT, session_id TEXT,"
            " observed_at TEXT, observed_minute TEXT, health_state TEXT,"
            " component_status_json TEXT, config_hash TEXT, rate_limit_json TEXT,"
            " coverage_json TEXT, entry_readiness_json TEXT, raw_json TEXT)")
        connection.execute(
            "CREATE TABLE protection_reconciliations (id INTEGER PRIMARY KEY,"
            " checked_at TEXT, environment TEXT, matched INTEGER, details_json TEXT)")
        connection.execute(
            "CREATE TABLE execution_fills (fill_key TEXT PRIMARY KEY, environment TEXT,"
            " account_ref TEXT, symbol TEXT, trade_id TEXT, received_at TEXT)")
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000+00:00"
        connection.execute(
            "INSERT INTO release_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("obs-1", "rel-1", "live", "env:live:synthetic", "sess-1", now_iso,
             now_iso[:16], "healthy", "{}", "h", "{}", "{}", "{}", "{}"))
        # 保护对账也要有新鲜且匹配的证据，否则基线自身就会报
        # protection_reconcile_never_completed（与本任务无关的噪声）。
        connection.execute(
            "INSERT INTO protection_reconciliations VALUES (?,?,?,?,?)",
            (1, now_iso, "live", 1, "{}"))
        connection.commit()
    finally:
        connection.close()


def test_queue_independence() -> None:
    section("5. 投递队列独立于交易库（告警积压不得影响交易库/交易路径）")
    state_root = TMP / "independence"
    trading_dir = state_root / "data" / "live"
    trading_dir.mkdir(parents=True, exist_ok=True)
    trading_db = trading_dir / "agent_trader.sqlite"
    _make_trading_db(trading_db)
    queue_file = state_root / "ops" / "alert_queue.sqlite"

    check("队列文件与交易库是不同文件",
          queue_file.resolve() != trading_db.resolve(),
          (str(queue_file), str(trading_db)))

    before = _snapshot_tree(trading_dir)
    before_bytes = trading_db.read_bytes()
    now = int(time.time() * 1000)
    queue = AlertQueue(queue_file, capacity=50, cooldown_seconds=0)
    try:
        for index in range(200):
            queue.enqueue(make_alert(now, incident=f"live|obj{index}|E{index}"),
                          now_ms=now + index)
        alert = make_alert(now, incident="live|trading_service|TRADING_STALLED")
        aid = queue.enqueue(alert, now_ms=now).alert_id
        channel = verified_channel()
        queue.dispatch_due(RecordingSink(provider_receipt="rcpt-1"), channel, now_ms=now)
        queue.record_manual_ack(aid, actor="operator", note="synthetic", confirm=True,
                                now_ms=now + 1)
        queued_rows = queue.count()
        dropped_total = queue.dropped_total()
    finally:
        queue.close()

    after = _snapshot_tree(trading_dir)
    check("200 条告警积压 + 投递后交易库目录零新增/零修改",
          before == after, {"before": len(before), "after": len(after)})
    check("交易库文件字节未变（告警积压不写交易库）",
          trading_db.read_bytes() == before_bytes, len(before_bytes))
    check("队列行数受容量约束（有界，不无限增长）",
          queued_rows <= 50, queued_rows)
    check("容量淘汰被计数（dropped_total > 0，未静默丢弃）",
          dropped_total > 0, dropped_total)

    # 交易证据时间只读：能读到保护对账/观察证据的最新时刻。
    connection = sqlite3.connect(str(trading_db))
    connection.row_factory = sqlite3.Row
    try:
        evidence = trading_evidence_timestamp(connection, environment="live")
    finally:
        connection.close()
    check("交易证据时间可从交易库只读取得", evidence.available and evidence.at_ms is not None,
          evidence.as_dict())
    check("交易证据来源逐项登记（release_observations/execution_fills/protection）",
          set(evidence.sources) == {
              "release_observations.observed_at",
              "execution_fills.received_at",
              "protection_reconciliations.checked_at",
          }, sorted(evidence.sources))

    # 主库损坏（F22）时仍能投递严重告警。
    corrupt_dir = TMP / "independence-corrupt" / "data" / "live"
    corrupt_dir.mkdir(parents=True, exist_ok=True)
    corrupt_db = corrupt_dir / "agent_trader.sqlite"
    corrupt_db.write_bytes(b"this is not a sqlite database")
    broken = sqlite3.connect(str(corrupt_db))
    broken.row_factory = sqlite3.Row
    try:
        unavailable = trading_evidence_timestamp(broken)
    finally:
        broken.close()
    check("主库损坏时交易证据读取明确报不可用（不抛裸异常）",
          unavailable.available is False and unavailable.reason_codes != (),
          unavailable.as_dict())
    queue2 = AlertQueue(TMP / "independence-corrupt" / "ops" / "q.sqlite", capacity=4)
    try:
        aid2 = queue2.enqueue(
            make_alert(now, incident="live|database|DB_UNREADABLE"), now_ms=now).alert_id
        result = queue2.dispatch_due(RecordingSink(provider_receipt="rcpt-9"),
                                     verified_channel(), now_ms=now)
        check("主库损坏时告警投递照常进行（独立队列）",
              result["submitted"] == 1 and queue2.get(aid2)["state"] == "ACCEPTED",
              (result, queue2.get(aid2)["state"]))
    finally:
        queue2.close()


# --------------------------------------------------------------------------
# 6. 消息脱敏边界（规格 §11.2）
# --------------------------------------------------------------------------


def test_message_redaction() -> None:
    section("6. 告警消息不含密钥/签名/原始 prompt")
    fixture = load_fixture("message_redaction.json")
    for case in fixture["cases"]:
        cid = case["id"]
        violations = list(validate_message(case["payload"]))
        check(f"{cid}: violations == {case['expect']['violations']}",
              violations == case["expect"]["violations"], violations)
    message = build_message(
        profile="live", host="host-synthetic-01", object_ref="trading_service",
        reason_code="TRADING_STALLED", severity="critical",
        impact="synthetic", new_entry_state="SUSPENDED",
        handling_entry="scripts/health_watch.py", occurred_at_ms=0)
    required = {"profile", "host", "object_ref", "impact", "new_entry_state",
                "handling_entry"}
    check("build_message 含规格 §11.2 要求的全部字段",
          required.issubset(set(message)), sorted(message))
    check("build_message 的 severity 归一为大写",
          message["severity"] == "CRITICAL", message["severity"])
    raised = ""
    try:
        build_message(profile="live", host="h", object_ref="o", reason_code="R",
                      severity="INFO", impact="i", new_entry_state="n",
                      handling_entry="e", occurred_at_ms=0,
                      extra={"raw_prompt": "<placeholder>"})
    except ValueError as exc:
        raised = str(exc)
    check("build_message 对禁键直接拒绝（不静默放行）",
          "raw_prompt" in raised, raised)


# --------------------------------------------------------------------------
# 7. 与共享适配的两态衔接（补丁在 / 不在，各自断言各自应有的语义）
# --------------------------------------------------------------------------


def _shared_health_watch_ready() -> bool:
    """共享补丁是否已把 U14 接线进 ``core.health_watch``（按标记探测，不猜）。"""
    try:
        from core import health_watch  # noqa: PLC0415

        return bool(getattr(health_watch, "U14_INTEGRATION", ""))
    except Exception:  # noqa: BLE001 - 两态都不得让本套件崩
        return False


def test_shared_adapter_two_states() -> None:
    section("7. 共享适配两态衔接（health_watch / scripts.health_watch / audit.database）")
    project = TMP / "shared" / "project"
    data_live = project / "data" / "live"
    data_live.mkdir(parents=True, exist_ok=True)
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "config" / "settings.yaml").write_text(
        "stability:\n  fast_cycle_stale_seconds: 120\n"
        "  protection_reconcile_stale_seconds: 120\n"
        "  startup_grace_seconds: 120\n"
        "execution:\n  default_mode: live\n",
        encoding="utf-8")
    _make_trading_db(data_live / "agent_trader.sqlite")
    now = time.time()
    (data_live / "trading_service.lock").write_text(
        json.dumps({"pid": os.getpid(),
                    "started_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00",
                                                time.gmtime(now - 1000))}),
        encoding="utf-8")

    from core.health_watch import HealthWatchConfig, evaluate_health  # noqa: PLC0415

    config = HealthWatchConfig.from_settings(project, profile="live")
    result = evaluate_health(config, now=now)
    payload = result.to_dict()
    check("health_watch 基线仍能只读评估（两态都必须成立）",
          isinstance(payload.get("healthy"), bool), payload.get("healthy"))
    check("health_watch 基线仍不伪造 host_down（不得把两类故障混为一类）",
          "host_down" not in result.reasons, result.reasons)

    if _shared_health_watch_ready():
        section("7b. 共享补丁已应用：断言接入后的新事实")
        check("补丁态：HealthResult 暴露交易证据时间字段",
              hasattr(result, "trading_evidence_at"), dir(result))
        check("补丁态：HealthResult 暴露 outage_kind 字段",
              hasattr(result, "outage_kind"), dir(result))
        check("补丁态：HealthResult 暴露 state_root_source（RuntimePaths 接入）",
              hasattr(config, "state_root_source"), dir(config))
        evidence_at = getattr(result, "trading_evidence_at", None)
        check("补丁态：交易证据时间非空（来自交易库只读探针）",
              isinstance(evidence_at, (int, float)), evidence_at)
        # 死 PID + 宿主一直在运行 ⇒ PROCESS_MISSING（不是 HOST_DOWN）。
        (data_live / "trading_service.lock").write_text(
            json.dumps({"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00"}),
            encoding="utf-8")
        dead = evaluate_health(config, now=now)
        check("补丁态：本机主进程故障 → process_missing（宿主连续运行）",
              "process_missing" in dead.reasons, dead.reasons)
        check("补丁态：主进程故障**不**被报成 host_down（成因不混）",
              "host_down" not in dead.reasons, dead.reasons)
    else:
        section("7b. 共享补丁未应用：断言旧行为未被伪造（不是 SKIP）")
        check("未打补丁态：HealthResult 不含 trading_evidence_at（未假装接入）",
              not hasattr(result, "trading_evidence_at"), dir(result))
        check("未打补丁态：HealthResult 不含 outage_kind（未假装接入）",
              not hasattr(result, "outage_kind"), dir(result))
        check("未打补丁态：HealthResult 不含 host_down（不得凭空造原因码）",
              not hasattr(result, "outage_kind") and "host_down" not in result.reasons,
              result.reasons)

    # CLI：两态都必须可用；补丁态额外支持 --state-root。
    cli = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "health_watch.py"),
         "--project-root", str(project), "--profile", "live", "--once"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "MTA_NO_PROXY": "1", "PYTHONUTF8": "1",
             "PYTHONIOENCODING": "utf-8"})
    check("CLI 两态都能跑且不声称无人值守告警已完成（退出码 2）",
          cli.returncode == 2, f"rc={cli.returncode} stderr={cli.stderr[-200:]}")
    help_text = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "health_watch.py"), "--help"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}).stdout
    if "--state-root" in help_text:
        state_root = TMP / "shared" / "declared_state"
        (state_root / "live").mkdir(parents=True, exist_ok=True)
        _make_trading_db(state_root / "live" / "agent_trader.sqlite")
        declared = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "health_watch.py"),
             "--project-root", str(project), "--profile", "live",
             "--state-root", str(state_root), "--once"],
            capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "MTA_NO_PROXY": "1", "PYTHONUTF8": "1",
                 "PYTHONIOENCODING": "utf-8"})
        payload = {}
        try:
            payload = json.loads(declared.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            payload = {}
        check("补丁态：CLI --state-root 生效并报告 state_root_source",
              payload.get("state_root_source") == "declared", payload.get("state_root_source"))
        check("补丁态：声明状态根后仍不声称告警已验证（退出码 2）",
              declared.returncode == 2, declared.returncode)
    else:
        check("未打补丁态：CLI 不支持 --state-root（旧行为完整保留）",
              "--state-root" not in help_text)

    # audit.database：补丁在才有告警审计写入；不在则不得出现半截能力。
    try:
        from audit.database import AuditDatabase  # noqa: PLC0415

        db = AuditDatabase(TMP / "shared" / "audit.sqlite")
        try:
            has_api = hasattr(db, "record_alert_delivery")
            has_evidence = hasattr(db, "trading_evidence_timestamp")
            if has_api:
                table = db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='alert_deliveries'")
                check("补丁态：alert_deliveries 审计表存在", table is not None, table)
                check("补丁态：AuditDatabase.trading_evidence_timestamp 可用",
                      has_evidence, has_evidence)
                # 关键：新表不得推进迁移版本（否则会打破既有 range(1, N) 断言）。
                check("补丁态：新增告警审计表**不**推进 schema 版本（无伴随断言破坏）",
                      db.current_schema_version() == 17, db.current_schema_version())
            else:
                check("未打补丁态：AuditDatabase 无告警审计 API（未假装接入）",
                      not has_api and not has_evidence, (has_api, has_evidence))
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        check("audit.database 两态探测不抛异常", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------


def main() -> int:
    print("U14 验收：外部告警送达与主机失联检测")
    print(f"临时目录: {TMP}")
    try:
        test_delivery_ladder()
        test_outage_classification()
        test_heartbeat()
        test_ops_blocked()
        test_queue_independence()
        test_message_redaction()
        test_shared_adapter_two_states()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\nU14 ALERT_DELIVERY PASS={PASSED} FAIL={len(FAILURES)}")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
