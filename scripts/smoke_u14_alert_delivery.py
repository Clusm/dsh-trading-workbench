"""U14 离线冒烟：外部告警送达 / 心跳交易证据 / 主机失联检测（F06/F12/F22、R13/R14）。

全程离线：只在系统临时目录里造合成状态，不读真实 ``.env``/``data``/密钥、不联网、
不启动服务、**不发真实告警**（一切投递走内存 fake sink）。遵循现有 smoke 风格：
顶层脚本、``check(name, cond, extra)``、结尾 PASS/FAIL 计数、失败退出码非 0。

覆盖（对应任务卡 U14 五条 offline_acceptance）：

1. 接收/送达/确认**区别**记录（供应商"已接受" ≠ "已送达"，契约自证不接受）；
2. 本机主进程故障与主机关机**分别**可检测（判别式 = 宿主 uptime vs 证据年龄）；
3. 真实渠道未确认时保留 ``OPS_BLOCKED``（不伪造 DELIVERED）；
4. 心跳包含交易证据时间以防假健康，投递队列独立于交易 DB；
5. 无设备回执用可审计人类确认演练但不伪造 DELIVERED；
6. 单条隔离单元对**任意**异常 fail-closed（P1-4：通道异常 / 空白回执 /
   非数值 `delivered_at_ms` 都只让**该条**退避重试，绝不中断整批、不每轮复发）。

两态可跑：共享适配补丁（``src/core/health_watch.py`` / ``scripts/health_watch.py`` /
``src/audit/database.py``）**在**与**不在**都通过——补丁在则断言接入后的新事实，
补丁不在则断言旧行为没有被伪造（逐项都断言，不是 SKIP）。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.ops.alert_delivery import (  # noqa: E402
    HOST_DOWN,
    HOST_CONTINUITY_UNKNOWN,
    MANUAL_ACK_ONLY,
    NO_CONFIRMED_DELIVERY,
    OPS_BLOCKED,
    OPS_DEGRADED,
    OPS_OK,
    PROCESS_MISSING,
    PROVIDER_CLOCK_SKEW_TOLERATED,
    RECEIPT_BINDING_MISMATCH,
    RECEIPT_EMPTY,
    RECEIPT_TIMESTAMP_FUTURE,
    RECEIPT_TYPE_INVALID,
    TRADING_STALLED,
    TRANSITION_REJECTED,
    TRANSPORT_EXCEPTION,
    TRANSPORT_FAILED,
    AlertQueue,
    AlertReceiptRejected,
    ChannelPolicy,
    Heartbeat,
    HeartbeatState,
    RecordingSink,
    TransportOutcome,
    UnconfiguredTransport,
    advance_heartbeat,
    classify_outage,
    ops_status,
    trading_evidence_timestamp,
)

ok = fail = 0


def check(name: str, cond: bool, extra: object = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


#: 官方回归入口不清除状态根环境变量；本冒烟显式清掉，避免本机环境改变结论
#: （与 scripts/smoke_u13_supervision.py 同口径）。
_ENV_STATE_ROOT_BACKUP = {name: os.environ.pop(name, None)
                          for name in ("MTA_STATE_ROOT", "MTA_DATA_ROOT")}
TMP = Path(tempfile.mkdtemp(prefix="smoke_u14_"))
import atexit  # noqa: E402

atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))


def _incident(name: str) -> str:
    return f"live|{name}|SYNTHETIC_REASON"


def _enqueue(queue: AlertQueue, now_ms: int, name: str = "trading_service"):
    from core.ops.alert_delivery import Alert  # noqa: PLC0415

    return queue.enqueue(Alert(
        incident_key=_incident(name), environment="live", severity="CRITICAL",
        reason_code="SYNTHETIC_REASON", object_ref=name, profile="live",
        host="host-synthetic", occurrence_at_ms=now_ms,
        message={"schema": "u14.alert.v1", "profile": "live", "host": "host-synthetic",
                 "object_ref": name, "reason_code": "SYNTHETIC_REASON",
                 "severity": "CRITICAL", "impact": "synthetic", "new_entry_state": "SUSPENDED",
                 "handling_entry": "scripts/health_watch.py", "occurred_at_ms": now_ms}),
        now_ms=now_ms).alert_id


def _verified() -> ChannelPolicy:
    return ChannelPolicy(channel="fake-sink", status="verified",
                         supports_delivery_receipt=True,
                         escalation_path="operator-phone-synthetic")


def main() -> None:  # noqa: C901 - 冒烟脚本按段落线性展开
    now = int(time.time() * 1000)

    # ---- 1. 接收 / 送达 / 确认 区别记录 -----------------------------------
    print("\n[1] 接收/送达/确认分级（R14）")
    queue = AlertQueue(TMP / "ladder" / "q.sqlite", capacity=8)
    try:
        aid = _enqueue(queue, now)
        http_only = RecordingSink(http_status=200)
        result = queue.dispatch_due(http_only, _verified(), now_ms=now)
        check("HTTP200 只到 SUBMITTED（不是送达）",
              queue.get(aid)["state"] == "SUBMITTED" and result["confirmed"] == 0,
              queue.get(aid)["state"])
        accepted = queue.record_accepted(aid, now_ms=now + 10, provider_receipt="rcpt-synth",
                                        bound_alert_id=aid)
        check("供应商接收 = ACCEPTED 且 delivered_at_ms 仍为空（≠送达）",
              accepted == "ACCEPTED" and queue.get(aid)["delivered_at_ms"] is None,
              accepted)
        try:
            queue.record_delivered(aid, now_ms=now + 20, delivered_at_ms=now + 20,
                                   device_receipt="")
            no_device = "UNEXPECTED_PASS"
        except ValueError as exc:
            no_device = str(exc)
        check("无设备回执不得标 DELIVERED",
              "device_receipt" in no_device, no_device)
        delivered = queue.record_delivered(aid, now_ms=now + 30, delivered_at_ms=now + 30,
                                           device_receipt="dev-synth", bound_alert_id=aid)
        check("有设备回执才到 DELIVERED", delivered == "DELIVERED", delivered)
    finally:
        queue.close()

    # ---- 2. 本机主进程故障 vs 主机关机 -----------------------------------
    print("\n[2] 主进程故障与主机关机分别可检测（F06/F12/F22）")
    process_fault = classify_outage(
        now_seconds=1000.0, process_alive=False,
        trading_evidence_at_seconds=400.0, host_uptime_seconds=259200.0)
    host_fault = classify_outage(
        now_seconds=1000.0, process_alive=False,
        trading_evidence_at_seconds=400.0, host_uptime_seconds=300.0)
    stalled = classify_outage(
        now_seconds=1000.0, process_alive=True,
        trading_evidence_at_seconds=400.0, host_uptime_seconds=259200.0)
    unknown = classify_outage(
        now_seconds=1000.0, process_alive=False,
        trading_evidence_at_seconds=400.0, host_uptime_seconds=None)
    check("宿主持续运行 + 进程消失 ⇒ PROCESS_MISSING", process_fault.kind == PROCESS_MISSING,
          process_fault.as_dict())
    check("宿主重启（uptime < 证据年龄）⇒ HOST_DOWN", host_fault.kind == HOST_DOWN,
          host_fault.as_dict())
    check("两类成因是不同 kind（不得混为一类）",
          process_fault.kind != host_fault.kind, (process_fault.kind, host_fault.kind))
    check("心跳进程活着但交易证据陈旧 ⇒ TRADING_STALLED（不是 HEALTHY）",
          stalled.kind == TRADING_STALLED, stalled.as_dict())
    check("宿主连续性不可证明 ⇒ HOST_CONTINUITY_UNKNOWN（不猜）",
          unknown.kind == HOST_CONTINUITY_UNKNOWN
          and unknown.kind not in (PROCESS_MISSING, HOST_DOWN), unknown.as_dict())

    # ---- 3. 心跳必须携带交易证据时间，拒绝陈旧重放 -----------------------
    print("\n[3] 心跳交易证据时间与陈旧重放（R13）")
    previous = HeartbeatState(emitted_at_ms=now - 1000, trading_evidence_at_ms=now - 3000,
                              sequence=7, generation_id="gen-synth", session_id="sess-synth")
    good = Heartbeat(environment="live", emitted_at_ms=now, trading_evidence_at_ms=now - 1000,
                     sequence=8, generation_id="gen-synth", session_id="sess-synth")
    state, verdict = advance_heartbeat(previous, good, now_ms=now)
    check("新鲜心跳被接受且时间线推进",
          verdict.accepted and verdict.healthy and state.sequence == 8, verdict.as_dict())
    no_evidence = Heartbeat(environment="live", emitted_at_ms=now + 1000, sequence=9)
    _, v2 = advance_heartbeat(state, no_evidence, now_ms=now + 1000)
    check("心跳没有交易证据时间 ⇒ 不健康（心跳活着 ≠ 交易健康）",
          not v2.healthy and "TRADING_EVIDENCE_MISSING" in v2.reason_codes, v2.as_dict())
    replay = Heartbeat(environment="live", emitted_at_ms=now - 5000,
                       trading_evidence_at_ms=now - 6000, sequence=6)
    state3, v3 = advance_heartbeat(state, replay, now_ms=now)
    check("陈旧重放被拒绝且**不**更新健康时间",
          not v3.accepted and state3 == state, (v3.as_dict(), state3.as_dict()))

    # ---- 4. 真实渠道未确认 ⇒ 保留 OPS_BLOCKED ---------------------------
    print("\n[4] 未确认渠道保留 OPS_BLOCKED（不伪造 DELIVERED）")
    queue = AlertQueue(TMP / "blocked" / "q.sqlite", capacity=8)
    try:
        aid = _enqueue(queue, now)
        unverified = ChannelPolicy(channel="unverified", status="unverified")
        verdict = ops_status(queue, channel=unverified, now_ms=now)
        check("未验证通道 ⇒ OPS_BLOCKED / CHANNEL_UNVERIFIED",
              verdict.status == OPS_BLOCKED and verdict.blocked, verdict.as_dict())
        liar = RecordingSink(provider_receipt="rcpt", delivered_at_ms=now,
                             device_receipt="dev")
        result = queue.dispatch_due(liar, unverified, now_ms=now)
        check("未验证通道下**不**提交（transport 未被调用、状态保持 UNKNOWN）",
              liar.submissions == [] and queue.get(aid)["state"] == "UNKNOWN"
              and queue.get(aid)["delivered_at_ms"] is None,
              (result, queue.get(aid)["state"]))
        queue.dispatch_due(RecordingSink(http_status=200), _verified(), now_ms=now)
        v = ops_status(queue, channel=_verified(), now_ms=now)
        check("验证通道 + 仅 HTTP200 ⇒ OPS_DEGRADED + NO_CONFIRMED_DELIVERY",
              v.status == OPS_DEGRADED and NO_CONFIRMED_DELIVERY in v.reason_codes,
              v.as_dict())
        # 空队列 + 仅配置 verified=true 也必须 fail-closed（不得报 OPS_OK）。
        fresh = AlertQueue(TMP / "blocked-empty" / "q.sqlite", capacity=4)
        try:
            check("空队列 + 仅配置 verified=true 不报 OPS_OK（fail-closed）",
                  ops_status(fresh, channel=_verified(), now_ms=now).status != OPS_OK,
                  ops_status(fresh, channel=_verified(), now_ms=now).as_dict())
        finally:
            fresh.close()
        # 只有 HTTP200 的告警按冷却间隔复查，不忙轮询（P3-4）。
        again = queue.dispatch_due(RecordingSink(http_status=200), _verified(), now_ms=now + 1)
        check("只有 HTTP200 时不忙轮询（冷却期内不重复提交）", again["submitted"] == 0, again)
        # 冷却到期后再投递，拿到真实设备回执才 OPS_OK。
        delivered_at = now + 300_010
        queue.dispatch_due(RecordingSink(provider_receipt="r2", delivered_at_ms=delivered_at,
                                         device_receipt="dev2"), _verified(),
                           now_ms=delivered_at)
        check("真实送达 ⇒ OPS_OK",
              ops_status(queue, channel=_verified(), now_ms=delivered_at).status == OPS_OK,
              queue.get(aid)["state"])
    finally:
        queue.close()

    # ---- 4b. 修复轮 r2：回执身份绑定 / 时间戳上界 / 整批不中断 ------------
    print("\n[4b] 回执身份绑定与时间戳上界（R14：供应商接受 ≠ 送达）")
    queue = AlertQueue(TMP / "receipt-bound" / "q.sqlite", capacity=8, cooldown_seconds=0)
    try:
        aid = _enqueue(queue, now, "early-receipt")
        res = queue.dispatch_due(
            RecordingSink(provider_receipt="r-early", delivered_at_ms=now - 1,
                          device_receipt="dev-early"), _verified(), now_ms=now)
        check("回执略早于本地提交 ⇒ 有界容忍 + 如实标注，且整批不中断",
              res["confirmed"] == 1
              and PROVIDER_CLOCK_SKEW_TOLERATED in res["reason_codes"], res)
        check("归一是可审计的（alert_audit 留痕）",
              any(e["transition"] == PROVIDER_CLOCK_SKEW_TOLERATED
                  for e in queue.audit_trail(aid)), [e["transition"]
                                                     for e in queue.audit_trail(aid)])
        victim = _enqueue(queue, now, "victim")
        queue.record_submitted(victim, now_ms=now)
        rejected = _receipt_rejections(queue, victim, now)
        check("他消息回执 / 非字符串回执 / 未来时间戳 三类伪造全部被拒",
              rejected == [RECEIPT_BINDING_MISMATCH, RECEIPT_TYPE_INVALID,
                           RECEIPT_TIMESTAMP_FUTURE], rejected)
        check("三类伪造都没有把告警标成 DELIVERED",
              queue.get(victim)["state"] == "SUBMITTED"
              and queue.get(victim)["delivered_at_ms"] is None,
              {k: queue.get(victim)[k] for k in ("state", "delivered_at_ms")})
    finally:
        queue.close()

    # ---- 4c. 修复轮 r3：单条隔离对任意异常 fail-closed（P1-4）-------------
    print("\n[4c] 单条隔离：通道异常 / 畸形回执不得中断整批（P1-4）")

    def _isolation_case(name: str, behaviour, expect_transition: str) -> dict | None:
        queue = AlertQueue(TMP / f"isolation-{name}" / "q.sqlite", capacity=8,
                           cooldown_seconds=0)
        try:
            bad = _enqueue(queue, now, f"bad-{name}")
            others = [_enqueue(queue, now, f"ok{i}-{name}") for i in (1, 2)]
            try:
                res = queue.dispatch_due(_RawSink(behaviour), _verified(), now_ms=now)
            except Exception as exc:  # noqa: BLE001 - 穿出即 P1-4 未闭合
                check(f"P1-4[{name}]: 异常不得穿出 dispatch_due", False,
                      f"{type(exc).__name__}: {exc}")
                return None
            row = queue.get(bad)
            check(f"P1-4[{name}]: 该条被退避隔离，其余两条照常送达",
                  all(queue.get(a)["state"] == "DELIVERED" for a in others)
                  and row["next_attempt_ms"] is not None
                  and int(row["next_attempt_ms"]) > now
                  and row["delivered_at_ms"] is None,
                  (res, dict(row), [queue.get(a)["state"] for a in others]))
            check(f"P3-6[{name}]: 审计标签与原因一致（{expect_transition}）",
                  expect_transition in [e["transition"] for e in queue.audit_trail(bad)],
                  [e["transition"] for e in queue.audit_trail(bad)])
            again = queue.dispatch_due(_RawSink(behaviour), _verified(), now_ms=now + 1)
            check(f"P1-4[{name}]: 退避窗口内不复发（不每轮重试）",
                  again["submitted"] == 0 and again["rejected"] == 0
                  and again["failed"] == 0 and again["errors"] == 0, again)
            return res
        finally:
            queue.close()

    def _normal_outcome(alert, now_ms):
        return TransportOutcome(True, http_status=200, provider_receipt="rcpt-ok",
                                delivered_at_ms=now_ms, device_receipt="dev-ok",
                                receipt_alert_id=alert.alert_id())

    def _raises(exc_type):
        def _b(alert, now_ms):
            if alert.incident_key.split("|")[1].startswith("bad"):
                raise exc_type("synthetic gateway fault")
            return _normal_outcome(alert, now_ms)
        return _b

    for exc_type in (TimeoutError, RuntimeError):
        tag = exc_type.__name__
        res = _isolation_case(f"raise-{tag}", _raises(exc_type), TRANSPORT_FAILED)
        check(f"P1-4[通道抛 {tag}]: 隔离为 TRANSPORT_EXCEPTION:{tag} 且计入 failed",
              bool(res) and res["failed"] == 1
              and f"{TRANSPORT_EXCEPTION}:{tag}" in res["reason_codes"], res)

    def _blank_receipt(alert, now_ms):
        if alert.incident_key.split("|")[1].startswith("bad"):
            return TransportOutcome(True, http_status=200, provider_receipt="   ",
                                    receipt_alert_id=alert.alert_id())
        return _normal_outcome(alert, now_ms)

    res = _isolation_case("blank-receipt", _blank_receipt, TRANSITION_REJECTED)
    check("P1-4[空白回执]: 隔离为 RECEIPT_EMPTY 且计入 rejected（不是裸 ValueError）",
          bool(res) and res["rejected"] == 1 and RECEIPT_EMPTY in res["reason_codes"], res)

    def _bad_delivered_at(alert, now_ms):
        if alert.incident_key.split("|")[1].startswith("bad"):
            return TransportOutcome(True, http_status=200, provider_receipt="rcpt-bad",
                                    delivered_at_ms="not-a-number",
                                    device_receipt="dev-bad",
                                    receipt_alert_id=alert.alert_id())
        return _normal_outcome(alert, now_ms)

    res = _isolation_case("bad-delivered-at", _bad_delivered_at, TRANSITION_REJECTED)
    check("P1-4[delivered_at_ms 非数值]: 隔离为 RECEIPT_TYPE_INVALID",
          bool(res) and res["rejected"] == 1
          and RECEIPT_TYPE_INVALID in res["reason_codes"], res)

    # 不得过度捕获：BaseException 必须穿出隔离层。
    queue = AlertQueue(TMP / "isolation-baseexc" / "q.sqlite", capacity=4)
    try:
        _enqueue(queue, now, "bad-ki")
        try:
            queue.dispatch_due(_RawSink(lambda alert, now_ms: _raise_ki()),
                               _verified(), now_ms=now)
            ki = "SWALLOWED"
        except KeyboardInterrupt:
            ki = "KeyboardInterrupt"
        except Exception as exc:  # noqa: BLE001
            ki = f"WRONG:{type(exc).__name__}"
        check("P1-4: KeyboardInterrupt（BaseException）不得被吞", ki == "KeyboardInterrupt", ki)
    finally:
        queue.close()

    # ---- 5. 无设备回执 → 可审计人工确认（不伪造 DELIVERED） ---------------
    print("\n[5] 无设备回执用可审计人工确认（不伪造送达）")
    queue = AlertQueue(TMP / "manual" / "q.sqlite", capacity=4)
    try:
        aid = _enqueue(queue, now, "protection")
        channel = ChannelPolicy(channel="sms-no-receipt", status="verified",
                                supports_delivery_receipt=False,
                                escalation_path="operator-phone-synthetic")
        queue.dispatch_due(RecordingSink(http_status=200), channel, now_ms=now)
        refused, reasons = queue.record_manual_ack(aid, actor="auto", note="synthetic",
                                                  confirm=True, now_ms=now + 1)
        check("自动执行者不得做人工确认", not refused and "MANUAL_ACK_ACTOR_INVALID" in reasons,
              reasons)
        accepted, _ = queue.record_manual_ack(aid, actor="operator", note="synthetic call-back",
                                              confirm=True, now_ms=now + 2)
        row = queue.get(aid)
        check("人工确认 ⇒ ACKNOWLEDGED 且 delivered_at_ms 仍为空",
              accepted and row["state"] == "ACKNOWLEDGED" and row["delivered_at_ms"] is None,
              row["state"])
        v = ops_status(queue, channel=channel, now_ms=now + 2)
        check("仅人工确认 ⇒ OPS_DEGRADED + MANUAL_ACK_ONLY",
              v.status == OPS_DEGRADED and MANUAL_ACK_ONLY in v.reason_codes, v.as_dict())
        check("人工确认留审计轨迹（可审计）",
              any(e["transition"] == "ACKNOWLEDGED" and e["actor"] == "operator"
                  for e in queue.audit_trail(aid)),
              [e["transition"] for e in queue.audit_trail(aid)])
    finally:
        queue.close()

    # ---- 6. 投递队列独立于交易库 ----------------------------------------
    print("\n[6] 投递队列独立于交易库（积压不写交易库）")
    trading_dir = TMP / "independence" / "data" / "live"
    trading_dir.mkdir(parents=True, exist_ok=True)
    trading_db = trading_dir / "agent_trader.sqlite"
    connection = sqlite3.connect(str(trading_db))
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute(
        "CREATE TABLE release_observations (observation_id TEXT PRIMARY KEY, environment TEXT,"
        " observed_at TEXT, component_status_json TEXT)")
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".000+00:00"
    connection.execute("INSERT INTO release_observations VALUES (?,?,?,?)",
                       ("o1", "live", now_iso, "{}"))
    connection.commit()
    connection.close()
    before = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in trading_dir.rglob("*")
              if p.is_file()}
    before_bytes = trading_db.read_bytes()
    queue = AlertQueue(TMP / "independence" / "ops" / "alert_queue.sqlite", capacity=20,
                       cooldown_seconds=0)
    try:
        for index in range(60):
            _enqueue(queue, now + index, f"obj{index}")
        check("队列文件与交易库不同路径",
              queue.path.resolve() != trading_db.resolve(), str(queue.path))
    finally:
        rows, dropped = queue.count(), queue.dropped_total()
        queue.close()
    after = {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in trading_dir.rglob("*")
             if p.is_file()}
    check("60 条积压后交易库目录零新增/零修改", before == after, (before, after))
    check("交易库文件字节未变", trading_db.read_bytes() == before_bytes)
    check("容量有界且淘汰被计数", rows <= 20 and dropped > 0, (rows, dropped))
    check("transport 未配置时明确失败（不假装成功）",
          UnconfiguredTransport().submit(alert=None, receiver_ref="", attempt=1,
                                         now_ms=now).ok is False)
    connection = sqlite3.connect(str(trading_db))
    connection.row_factory = sqlite3.Row
    try:
        evidence = trading_evidence_timestamp(connection, environment="live")
    finally:
        connection.close()
    check("交易证据时间只读可得", evidence.available and evidence.at_ms is not None,
          evidence.as_dict())

    # ---- 7. 与共享适配的两态衔接 ---------------------------------------
    print("\n[7] 共享适配两态衔接（补丁在/不在各自断言）")
    try:
        from core import health_watch  # noqa: PLC0415

        marker = bool(getattr(health_watch, "U14_INTEGRATION", ""))
        if marker:
            config = health_watch.HealthWatchConfig.from_settings(ROOT, profile="demo")
            result = health_watch.evaluate_health(
                health_watch.HealthWatchConfig(
                    project_root=TMP / "shared", profile=None,
                    state_root_declared=config.state_root_declared))
            check("补丁态：HealthResult 暴露 trading_evidence_at / outage_kind",
                  hasattr(result, "trading_evidence_at") and hasattr(result, "outage_kind"),
                  dir(result))
            check("补丁态：outage_kind 落在登记的成因枚举内",
                  result.outage_kind in ("HEALTHY", PROCESS_MISSING, HOST_DOWN,
                                         TRADING_STALLED, HOST_CONTINUITY_UNKNOWN,
                                         "HOST_JUST_BOOTED"), result.outage_kind)
            check("补丁态：state_root_source 如实报告 fallback/declared",
                  result.state_root_source in ("fallback", "declared"),
                  result.state_root_source)
        else:
            check("未打补丁态：core.health_watch 无 U14 标记（未假装接入）", not marker)
    except Exception as exc:  # noqa: BLE001
        check("health_watch 两态探测不抛异常", False, f"{type(exc).__name__}: {exc}")
    try:
        from audit.database import AuditDatabase  # noqa: PLC0415

        db = AuditDatabase(TMP / "audit" / "x.sqlite")
        try:
            if hasattr(db, "record_alert_delivery"):
                check("补丁态：alert_deliveries 表存在", db.fetch_one(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='alert_deliveries'") is not None)
                check("补丁态：告警审计表**不**推进 schema 版本（既有版本断言不受影响）",
                      db.current_schema_version() == 17, db.current_schema_version())
                check("补丁态：无设备回执的 DELIVERED 被拒绝（不落库）", not _try_bad_delivery(db))
            else:
                check("未打补丁态：AuditDatabase 无告警审计 API（未假装接入）", True)
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        check("audit.database 两态探测不抛异常", False, f"{type(exc).__name__}: {exc}")

    print(f"\nSMOKE_U14_ALERT_DELIVERY ok={ok} fail={fail}")
    if fail:
        raise SystemExit(1)


class _RawSink:
    """按 incident_key 直接返回/抛出任意东西的通道（``RecordingSink`` 只返回合法回执）。

    P1-4 的逃逸向量（通道抛异常、通道返回"空白回执"/非数值时间戳）需要这种注入能力。
    """

    name = "raw-sink"

    def __init__(self, behaviour) -> None:
        self.behaviour = behaviour
        self.calls: list[str] = []

    def submit(self, *, alert, receiver_ref: str, attempt: int, now_ms: int) -> object:
        self.calls.append(alert.incident_key)
        return self.behaviour(alert, now_ms)


def _raise_ki() -> object:
    raise KeyboardInterrupt("synthetic operator interrupt")


def _receipt_rejections(queue, alert_id: str, now_ms: int) -> list[str]:
    """三类回执伪造各自应得的原因码（返回顺序：绑定/类型/时间戳）。"""
    outcomes: list[str] = []

    def _reason(fn) -> str:
        try:
            fn()
        except AlertReceiptRejected as exc:
            return "+".join(exc.reason_codes)
        return "ACCEPTED"

    outcomes.append(_reason(lambda: queue.record_accepted(
        alert_id, now_ms=now_ms + 1, provider_receipt="receipt-of-other",
        bound_alert_id="alert-belongs-to-someone-else")))
    outcomes.append(_reason(lambda: queue.record_delivered(
        alert_id, now_ms=now_ms + 2, delivered_at_ms=now_ms + 2, device_receipt=12345,
        bound_alert_id=alert_id)))
    outcomes.append(_reason(lambda: queue.record_delivered(
        alert_id, now_ms=now_ms + 3, delivered_at_ms=now_ms + 10 ** 12,
        device_receipt="dev-future", bound_alert_id=alert_id)))
    return outcomes


def _try_bad_delivery(db) -> bool:
    """返回 True 表示"竟然被接受"（期望 False）。"""
    try:
        db.record_alert_delivery(
            environment="live", incident_key="smoke|bad|DELIVERED",
            attempt=1, first_occurred_at_ms=int(time.time() * 1000),
            submitted_at_ms=int(time.time() * 1000), provider_receipt="rcpt",
            delivered_at_ms=int(time.time() * 1000))
    except ValueError:
        return False
    return True


if __name__ == "__main__":
    main()
