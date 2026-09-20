"""U14 外部告警送达与主机失联检测（F06/F12/F22；R13/R14；规格 §11.2）。

本模块是 U14 的独占交付物，只做**离线可证明**的事：不启动真实服务、不联网、
不发送真实告警、不读 ``.env``/密钥。它把规格 §11.2 的四条硬要求落成可执行代码：

1. **接收 / 送达 / 确认三级分开记录（R14）**：投递阶梯**完全复用**冻结契约
   :mod:`core.contracts.k05_trace` 的 ``AlertDelivery`` / ``transition_state`` /
   ``delivery_violations``，本模块**不另立第二套阶梯**。供应商"已接受"
   （``ACCEPTED`` + ``provider_receipt``）**不等于**"已送达"
   （``DELIVERED`` 另需 ``delivered_at_ms`` **且** 设备回执）；HTTP 2xx 只到
   ``SUBMITTED``。任何写入后都跑一次 ``delivery_violations`` 自检，违规即抛
   :class:`AlertLadderViolation`（fail-closed，杜绝"先提升后补证"）。

2. **本机主进程故障与主机关机分别可检测（F06/F12/F22）**：两者成因不同，
   **不得混为一类**。判定只用一个读-only 判别式：
   ``宿主连续运行时长(uptime)`` 是否**短于**最后一条交易证据的年龄。

   - ``uptime >= evidence_age``：宿主机在最后一条交易证据之前就已启动且一直
     在运行 ⇒ 故障只可能出在交易进程本身 ⇒ :data:`PROCESS_MISSING`；
   - ``uptime < evidence_age``：宿主机在最后一条交易证据之后才启动 ⇒ 证据缺口
     跨越了一次**宿主重启/断电** ⇒ :data:`HOST_DOWN`；
   - uptime 不可得 ⇒ :data:`HOST_CONTINUITY_UNKNOWN`（既不断言主机关机，也不
     把主机关机误报成进程故障）。

   心跳进程活着**但交易证据陈旧**时给独立的 :data:`TRADING_STALLED`
   （F22 的核心场景），绝不把"心跳发送进程还活着"当交易健康（R13）。

3. **真实渠道未确认时保留 OPS_BLOCKED**：:func:`ops_status` 在通道未验证时恒返回
   :data:`OPS_BLOCKED`；:meth:`AlertQueue.dispatch_due` 在通道未验证且未显式声明
   ``drill=True`` 时**拒绝提交**，绝不伪造 ``DELIVERED``。

4. **投递队列独立于交易库（R14/F22）**：:class:`AlertQueue` 落在**独立**的
   SQLite 文件（默认 ``<state_root>/ops/alert_queue.sqlite``）。它不打开、不
   锁、不写交易库；交易库损坏（F22）时告警路径照常工作。:func:`trading_evidence_timestamp`
   只在**只读**连接上取"交易证据时间"，读不到就明确报告不可用。

另外两条规格要求也在此落地：

- **心跳携带交易证据时间、拒绝陈旧重放（R13）**：:class:`Heartbeat` 必须携带
  ``trading_evidence_at_ms``（最后持久化证据时间）、``sequence``（序号）、
  ``session_id``/``generation_id``（boot/session 与运行代）。陈旧 payload、
  序号回退、证据时间回退、来自未来的时间戳一律 ``accepted=False``，且**不更新**
  健康时间线（:func:`advance_heartbeat` 不推进 state）。
- **无设备回执用可审计的人工确认（规格 §11.2）**：:meth:`AlertQueue.record_manual_ack`
  要求显式 ``confirm``、非自动执行者 ``actor`` 与非空 ``note``，结果标为
  ``ACKNOWLEDGED`` 而**不是** ``DELIVERED``（``delivered_at_ms`` 保持 NULL），
  并把确认动作写入 append-only 的 ``alert_audit``。

本模块的对外边界：不导入 ``exchange``/``Broker``/LLM 客户端；不读交易密钥；
不写交易库；异常一律结构化（携带 ``reason_codes``），不泄漏裸
``TypeError``/``KeyError``。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from core.contracts.k05_trace import (
    ALERT_STATE_ORDER,
    AlertDelivery,
    TransitionError,
    delivery_violations,
    stable_incident_key,
    transition_state,
)

__all__ = [
    "ALERT_STATE_ORDER",
    "BACKLOG_OVERDUE",
    "BACKLOG_OVERDUE_SECONDS",
    "BREACH_DELIVERY_SLO",
    "CHANNEL_UNVERIFIED",
    "DEFAULT_CAPACITY",
    "DEFAULT_COOLDOWN_SECONDS",
    "DELIVERY_SLO_SECONDS",
    "HOST_CONTINUITY_UNKNOWN",
    "HOST_DOWN",
    "HOST_JUST_BOOTED",
    "HOST_REBOOT_OBSERVED",
    "MANUAL_ACK_ONLY",
    "MANUAL_ACK_REQUIRED",
    "MAX_BACKOFF_SECONDS",
    "MAINTENANCE_WINDOW_ACTIVE",
    "MAINTENANCE_WINDOW_UNBOUNDED",
    "MESSAGE_FORBIDDEN_KEYS",
    "NO_CONFIRMED_DELIVERY",
    "OPS_BLOCKED",
    "OPS_DEGRADED",
    "OPS_OK",
    "OUTAGE_HEALTHY",
    "PROCESS_MISSING",
    "PROCESS_UNVERIFIED",
    "Alert",
    "AlertLadderViolation",
    "AlertQueue",
    "AlertReceiptRejected",
    "ChannelPolicy",
    "EnqueueOutcome",
    "Heartbeat",
    "HeartbeatState",
    "HeartbeatVerdict",
    "MaintenanceWindow",
    "OpsVerdict",
    "OutageVerdict",
    "RecordingSink",
    "TRADING_EVIDENCE_SOURCES",
    "TRADING_STALLED",
    "TRANSITION_REJECTED",
    "TradingEvidence",
    "TransportOutcome",
    "UnconfiguredTransport",
    "advance_heartbeat",
    "build_message",
    "build_recovery_message",
    "classify_outage",
    "evaluate_heartbeat",
    "host_uptime_seconds",
    "maintenance_active",
    "ops_status",
    "read_heartbeat_record",
    "trading_evidence_timestamp",
    "validate_message",
]

# --------------------------------------------------------------------------
# 运维总状态与原因码
# --------------------------------------------------------------------------

#: 通道未验证 / 未确认送达：无人值守告警**尚未**成立（规格 §11.2 明示
#: "仅将 verified=true 写入配置不通过 OPS"）。
OPS_BLOCKED = "OPS_BLOCKED"
OPS_DEGRADED = "OPS_DEGRADED"
OPS_OK = "OPS_OK"

CHANNEL_UNVERIFIED = "CHANNEL_UNVERIFIED"
BACKLOG_OVERDUE = "BACKLOG_OVERDUE"
NO_CONFIRMED_DELIVERY = "NO_CONFIRMED_DELIVERY"
MANUAL_ACK_ONLY = "MANUAL_ACK_ONLY"
MANUAL_ACK_REQUIRED = "MANUAL_ACK_REQUIRED"
BREACH_DELIVERY_SLO = "BREACH_DELIVERY_SLO"

#: 规格 §6：「本机告警投递：检测后 ≤60 秒提交，端到端送达目标 ≤120 秒」。
DELIVERY_SLO_SECONDS = 120.0
#: 投递积压超过该秒数仍未送达即视为运维缺口（不静默）。
BACKLOG_OVERDUE_SECONDS = 300.0

#: 故障类别：本机主进程故障（宿主一直在运行）。
PROCESS_MISSING = "PROCESS_MISSING"
#: 故障类别：主机关机/断电/失联（证据缺口跨越了一次宿主重启）。
HOST_DOWN = "HOST_DOWN"
#: 心跳进程存活但交易证据陈旧（F22：心跳正常掩盖交易停摆）。
TRADING_STALLED = "TRADING_STALLED"
#: 宿主连续运行时长不可得：**不**断言任何一种，避免把两类故障混为一类。
HOST_CONTINUITY_UNKNOWN = "HOST_CONTINUITY_UNKNOWN"
#: 进程存活状态**不可证**（锁文件缺失/损坏/PID 不可解析）。它与"确证进程消失"
#: （``process_alive is False``）成因不同，必须能用原因码区分（P2-1）。
PROCESS_UNVERIFIED = "PROCESS_UNVERIFIED"
#: 宿主刚启动、仍在启动宽限内：不告警，也不算健康结论。
HOST_JUST_BOOTED = "HOST_JUST_BOOTED"
#: 宿主刚启动**且**最后一条交易证据早于本次启动 ⇒ 已经能看出发生过宿主重启，
#: 只是被启动宽限延后成告警（宽限一过 :func:`classify_outage` 即给 HOST_DOWN）。
HOST_REBOOT_OBSERVED = "HOST_REBOOT_OBSERVED"
OUTAGE_HEALTHY = "HEALTHY"

HEARTBEAT_OK = "HEARTBEAT_OK"
HEARTBEAT_SEQUENCE_REPLAY = "HEARTBEAT_SEQUENCE_REPLAY"
HEARTBEAT_REPLAY_OUT_OF_ORDER = "HEARTBEAT_REPLAY_OUT_OF_ORDER"
HEARTBEAT_FROM_FUTURE = "HEARTBEAT_FROM_FUTURE"
HEARTBEAT_TIMESTAMP_MISSING = "HEARTBEAT_TIMESTAMP_MISSING"
HEARTBEAT_GENERATION_MISMATCH = "HEARTBEAT_GENERATION_MISMATCH"
HEARTBEAT_MALFORMED = "HEARTBEAT_MALFORMED"
TRADING_EVIDENCE_MISSING = "TRADING_EVIDENCE_MISSING"
TRADING_EVIDENCE_STALE = "TRADING_EVIDENCE_STALE"
TRADING_EVIDENCE_REPLAY = "TRADING_EVIDENCE_REPLAY"
TRADING_EVIDENCE_FROM_FUTURE = "TRADING_EVIDENCE_FROM_FUTURE"
MAINTENANCE_WINDOW_ACTIVE = "MAINTENANCE_WINDOW_ACTIVE"
MAINTENANCE_WINDOW_UNBOUNDED = "MAINTENANCE_WINDOW_UNBOUNDED"

QUEUE_CAPACITY_EXCEEDED = "QUEUE_CAPACITY_EXCEEDED"
ALERT_DEDUPLICATED = "ALERT_DEDUPLICATED"
ALERT_ENQUEUED = "ALERT_ENQUEUED"
ALERT_RECOVERED = "ALERT_RECOVERED"
TRANSITION_REJECTED = "TRANSITION_REJECTED"
TRANSPORT_NOT_CONFIGURED = "TRANSPORT_NOT_CONFIGURED"
TRANSPORT_FAILED = "TRANSPORT_FAILED"
#: 通道（``transport.submit``）**自身抛异常**：任何异常（超时/连接重置/DNS/TLS/SDK
#: 自定义异常）都收敛为该结构化原因码（``TRANSPORT_EXCEPTION:<异常类名>``），
#: 绝不穿出 :meth:`AlertQueue.dispatch_due`（P1-4）。
TRANSPORT_EXCEPTION = "TRANSPORT_EXCEPTION"
#: 单条隔离单元的**兜底**原因码：既不是通道异常、也不是阶梯/回执拒绝的其它异常
#: （如队列行数据损坏）。只影响该条，并计入返回结构的 ``errors``（P1-4）。
INTERNAL_ERROR = "INTERNAL_ERROR"
#: 连"记录失败 + 退避"都写不进去（持久化故障）：如实暴露，但同样不得中断整批（P1-4）。
PERSIST_FAILED = "PERSIST_FAILED"
DELIVERED_WITHOUT_DEVICE_RECEIPT = "DELIVERED_WITHOUT_DEVICE_RECEIPT"
MANUAL_ACK_WITHOUT_SUBMISSION = "MANUAL_ACK_WITHOUT_SUBMISSION"
MANUAL_ACK_ACTOR_INVALID = "MANUAL_ACK_ACTOR_INVALID"

# --------------------------------------------------------------------------
# 送达回执的身份绑定与时间戳边界（P1-3；R14：供应商接受 ≠ 送达）
# --------------------------------------------------------------------------

#: 回执未声明所属消息 ⇒ 无法证明它是**本条**告警的回执，拒绝。
RECEIPT_UNBOUND = "RECEIPT_UNBOUND"
#: 回执声明的所属消息与本条告警不一致 ⇒ 他消息的回执不得标送达，拒绝。
RECEIPT_BINDING_MISMATCH = "RECEIPT_BINDING_MISMATCH"
#: 回执类型不符（非字符串，如 ``12345``）⇒ 结构化拒绝，**不**做 ``str()`` 强转。
RECEIPT_TYPE_INVALID = "RECEIPT_TYPE_INVALID"
#: 回执为**空/全空白**⇒ 与"类型不符"分开的结构化拒绝码（P1-4：既有的空回执负例
#: 仍拿到含字段名的 ``ValueError``，但它必须能被单条隔离捕获）。
RECEIPT_EMPTY = "RECEIPT_EMPTY"
#: 回执时间戳落在未来（超出容差）⇒ 拒绝，避免用未来时间伪造"已送达"。
RECEIPT_TIMESTAMP_FUTURE = "RECEIPT_TIMESTAMP_FUTURE"
#: 供应商回执时间略早于本地提交时刻，已按有界容差归一（如实标注，不静默）。
PROVIDER_CLOCK_SKEW_TOLERATED = "PROVIDER_CLOCK_SKEW_TOLERATED"

#: 回执时间戳相对本地时钟的**有界**容差：供应商网关普遍上报自己的时钟，
#: 与本地提交时刻存在有界偏斜；超出该容差的时间戳一律拒绝（不静默放大）。
RECEIPT_TIMESTAMP_TOLERANCE_MS = 120_000

#: 人工确认缺 note/confirm：常量名与值必须一致（旧名值不符会误导按名过滤日志的消费者）。
MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED = "MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED"
#: 兼容别名：值与 :data:`MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED` 相同，仅为不破坏既有导入。
MANUAL_ACK_REQUIRED = MANUAL_ACK_NOTE_AND_CONFIRM_REQUIRED
UNKNOWN_ALERT = "UNKNOWN_ALERT"

DEFAULT_CAPACITY = 500
DEFAULT_COOLDOWN_SECONDS = 900.0
DEFAULT_DEDUP_SECONDS = 300.0
DEFAULT_BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 3600.0

#: 告警消息**禁止**出现的键名（脱敏边界：消息不含密钥、签名或原始 prompt）。
#: 键名匹配复用 U15 :func:`core.runtime_identity.is_sensitive_name` 的子串口径，
#: 这里只补 K05/规格点名的三项；不做值内容扫描（见 docs/contracts/README.md L1）。
MESSAGE_FORBIDDEN_KEYS: tuple[str, ...] = (
    "pipeline_signature",
    "raw_prompt",
    "prompt",
    "raw_payload",
)

_AUTO_ACTORS = frozenset({"auto", "system", "supervisor", "watchdog"})


class AlertLadderViolation(RuntimeError):
    """投递阶梯自检失败：说明出现"先提升后补证"或伪造送达。"""

    def __init__(self, reason_codes: Iterable[str]) -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__("alert ladder violated: " + ", ".join(self.reason_codes))


class AlertReceiptRejected(ValueError):
    """送达回执被**结构化**拒绝（P1-3）。

    四类：类型不符（:data:`RECEIPT_TYPE_INVALID`）、空/全空白
    （:data:`RECEIPT_EMPTY`）、未绑定/绑定他消息（:data:`RECEIPT_UNBOUND` /
    :data:`RECEIPT_BINDING_MISMATCH`）、时间戳越界（:data:`RECEIPT_TIMESTAMP_FUTURE`）。
    带 :attr:`reason_codes` 供调用方按码分支，而不是靠解析字符串。

    它**仍**是 :class:`ValueError` 的子类（既有"空回执拿到 ValueError"的负例不受影响）；
    ``field`` 只用于让错误串保留字段名，便于人读。
    """

    def __init__(self, reason_codes: Iterable[str], *, field: str | None = None) -> None:
        self.reason_codes = tuple(reason_codes)
        self.field = field
        detail = f" ({field} required: 回执不得为空)" if field else ""
        super().__init__("alert receipt rejected: " + ", ".join(self.reason_codes) + detail)


# --------------------------------------------------------------------------
# 时间工具（全部只读；monotonic 只用其"自宿主启动以来的秒数"语义）
# --------------------------------------------------------------------------


def _now_ms(now_ms: int | None = None) -> int:
    if now_ms is not None:
        return int(now_ms)
    return int(time.time() * 1000)


def parse_iso_ms(value: Any) -> int | None:
    """解析 ISO-8601 或 epoch 毫秒/秒为 UTC 毫秒；不可解析返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return int(number if abs(number) >= 1e11 else number * 1000)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return parse_iso_ms(float(text))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def iso_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000.0, tz=timezone.utc).isoformat(
        timespec="milliseconds")


def host_uptime_seconds(*, monotonic_value: float | None = None) -> float | None:
    """宿主"连续运行时长"（秒）的最佳可得估计；不可得返回 ``None``。

    语义与限制（**不得**当成绝对事实）：

    - Linux 优先读 ``/proc/uptime``（内核自启动以来的秒数）；
    - 其余平台退回 ``time.monotonic()``：Windows(QPC)/macOS/Linux 上该值均为
      **自宿主启动以来的秒数**，但这一性质由平台保证而非 Python 文档保证，
      因此调用方拿到 ``None`` 或可疑值时必须按"不可证明"处理；
    - ``monotonic_value`` 供离线测试注入（测试**不**重启真实宿主）。

    本函数只读，不写任何文件、不改系统时钟、不访问网络。
    """
    if monotonic_value is not None:
        try:
            return float(monotonic_value)
        except (TypeError, ValueError):
            return None
    try:
        first = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        return float(first)
    except (OSError, ValueError, IndexError):
        pass
    try:
        value = time.monotonic()
    except Exception:  # pragma: no cover - 平台异常
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):  # pragma: no cover - 防御
        return None
    return number if number >= 0 else None


# --------------------------------------------------------------------------
# 本机主进程故障 vs 主机关机（成因不同，分别可检测）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OutageVerdict:
    """一次本机失联判定；``kind`` 只取下列之一，**从不合并两类成因**。"""

    kind: str
    reason_codes: tuple[str, ...]
    host_uptime_seconds: float | None
    trading_evidence_age_seconds: float | None
    in_startup_grace: bool

    @property
    def host_continuity_proven(self) -> bool:
        return self.host_uptime_seconds is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason_codes": list(self.reason_codes),
            "host_uptime_seconds": self.host_uptime_seconds,
            "trading_evidence_age_seconds": self.trading_evidence_age_seconds,
            "host_continuity_proven": self.host_continuity_proven,
            "in_startup_grace": self.in_startup_grace,
        }


def classify_outage(
    *,
    now_seconds: float,
    process_alive: bool | None,
    trading_evidence_at_seconds: float | None,
    host_uptime_seconds: float | None,
    startup_grace_seconds: float = 120.0,
    trading_evidence_max_age_seconds: float = 120.0,
) -> OutageVerdict:
    """分别判定"本机主进程故障"与"主机关机"。

    判别式只有一条，但方向是本质的：**宿主连续运行时长 vs 交易证据年龄**。

    - 宿主在最后一条交易证据**之前**就已启动（``uptime >= evidence_age``）
      ⇒ 这段证据缺口里宿主一直在运行 ⇒ 停摆的只可能是交易进程
      ⇒ :data:`PROCESS_MISSING`；
    - 宿主在最后一条交易证据**之后**才启动（``uptime < evidence_age``）
      ⇒ 证据缺口跨越了一次宿主重启/断电 ⇒ :data:`HOST_DOWN`（同时也解释了
      进程为何不见了，但**不得**降级报成进程故障）；
    - 宿主连续运行时长不可得 ⇒ :data:`HOST_CONTINUITY_UNKNOWN`：不猜。
    - 交易证据陈旧（超过 ``trading_evidence_max_age_seconds``）且**进程仍存活**
      ⇒ :data:`TRADING_STALLED`（R13/F22：心跳进程活着不等于交易健康）。

    ``process_alive=None``（锁文件缺失/损坏/PID 不可解析）与 ``False``（确知进程消失）
    都进入失联分支，但原因码不同：``None`` 时在既有原因码之外**追加**
    :data:`PROCESS_UNVERIFIED`，消费者因此能区分"确证进程消失"与"进程状态不可读"；
    本函数只负责成因分类，进程可证性由探针方给出。
    """
    if host_uptime_seconds is not None and float(host_uptime_seconds) < 0:
        # 负的宿主运行时长是**无效读数**而不是"刚启动"（否则会落入启动宽限报
        # HOST_JUST_BOOTED，掩盖可能的关机）。归一为不可得 ⇒ 连续性不可证，不猜。
        host_uptime_seconds = None
    age: float | None = None
    if trading_evidence_at_seconds is not None:
        age = max(0.0, float(now_seconds) - float(trading_evidence_at_seconds))
    if host_uptime_seconds is not None and host_uptime_seconds <= max(
            0.0, float(startup_grace_seconds)):
        codes = [HOST_JUST_BOOTED]
        if age is not None and age > host_uptime_seconds:
            codes.append(HOST_REBOOT_OBSERVED)
        return OutageVerdict(HOST_JUST_BOOTED, tuple(codes),
                             host_uptime_seconds, age, True)

    evidence_stale = age is None or age > float(trading_evidence_max_age_seconds)
    if process_alive is True and not evidence_stale:
        return OutageVerdict(OUTAGE_HEALTHY, (HEARTBEAT_OK,),
                             host_uptime_seconds, age, False)
    if process_alive is True and evidence_stale:
        codes = [TRADING_STALLED]
        if age is None:
            codes.append(TRADING_EVIDENCE_MISSING)
        return OutageVerdict(TRADING_STALLED, tuple(codes),
                             host_uptime_seconds, age, False)

    # 进程不可证（消失或不可解析）：按宿主连续性区分成因。
    # process_alive is None ⇒ 进程侧"不可证"，与"确证消失"用独立原因码区分。
    unverified = () if process_alive is not None else (PROCESS_UNVERIFIED,)
    if host_uptime_seconds is None or age is None:
        process_code = (PROCESS_MISSING if process_alive is False
                        else TRADING_EVIDENCE_MISSING)
        return OutageVerdict(HOST_CONTINUITY_UNKNOWN,
                             (process_code, *unverified, HOST_CONTINUITY_UNKNOWN),
                             host_uptime_seconds, age, False)
    if host_uptime_seconds < age:
        return OutageVerdict(HOST_DOWN, (HOST_DOWN, *unverified),
                             host_uptime_seconds, age, False)
    return OutageVerdict(PROCESS_MISSING, (PROCESS_MISSING, *unverified),
                         host_uptime_seconds, age, False)


# --------------------------------------------------------------------------
# 心跳：必须携带交易证据时间，拒绝陈旧重放（R13）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MaintenanceWindow:
    """维护窗口：必须**有界**（scope + 到期时间），不能无限静音。"""

    scope: str
    starts_at_ms: int
    expires_at_ms: int
    actor: str = ""
    note: str = ""

    def bounded(self) -> bool:
        return bool(self.scope.strip()) and int(self.expires_at_ms) > int(self.starts_at_ms)


def maintenance_active(window: MaintenanceWindow | None, *, now_ms: int) -> tuple[bool, tuple[str, ...]]:
    """维护窗口是否生效；**无界窗口拒绝生效**（fail-closed，不静默静音）。"""
    if window is None:
        return False, ()
    if not window.bounded():
        return False, (MAINTENANCE_WINDOW_UNBOUNDED,)
    if int(window.starts_at_ms) <= int(now_ms) < int(window.expires_at_ms):
        return True, (MAINTENANCE_WINDOW_ACTIVE,)
    return False, ()


@dataclass(frozen=True)
class Heartbeat:
    """外部心跳 payload（规格 §11.2）：发送时间 + boot/session + 序号 +
    最后完成的快循环 + **最后持久化证据（交易证据）时间**。

    ``trading_evidence_at_ms`` 是本类存在的理由：只有心跳发送进程活着、
    没有交易证据时间时，:func:`evaluate_heartbeat` 一律判为不健康
    （``TRADING_EVIDENCE_MISSING``），绝不把"心跳进程活着"当交易健康。
    """

    environment: str
    emitted_at_ms: int
    trading_evidence_at_ms: int | None = None
    last_fast_cycle_at_ms: int | None = None
    sequence: int | None = None
    session_id: str = ""
    runtime_instance_id: str = ""
    generation_id: str = ""
    account_ref: str = ""
    maintenance: MaintenanceWindow | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "emitted_at_ms": self.emitted_at_ms,
            "trading_evidence_at_ms": self.trading_evidence_at_ms,
            "last_fast_cycle_at_ms": self.last_fast_cycle_at_ms,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "account_ref": self.account_ref,
            "maintenance": (None if self.maintenance is None else {
                "scope": self.maintenance.scope,
                "starts_at_ms": self.maintenance.starts_at_ms,
                "expires_at_ms": self.maintenance.expires_at_ms,
                "actor": self.maintenance.actor,
                "note": self.maintenance.note,
            }),
        }


@dataclass(frozen=True)
class HeartbeatState:
    """外部监测侧保存的心跳时间线（只保留"已被接受"的读数）。"""

    emitted_at_ms: int | None = None
    trading_evidence_at_ms: int | None = None
    sequence: int | None = None
    generation_id: str = ""
    session_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "emitted_at_ms": self.emitted_at_ms,
            "trading_evidence_at_ms": self.trading_evidence_at_ms,
            "sequence": self.sequence,
            "generation_id": self.generation_id,
            "session_id": self.session_id,
        }


@dataclass(frozen=True)
class HeartbeatVerdict:
    """心跳判定：``accepted=False`` 时**不得**推进健康时间线。"""

    accepted: bool
    healthy: bool
    reason_codes: tuple[str, ...]
    trading_evidence_age_seconds: float | None
    muted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "healthy": self.healthy,
            "reason_codes": list(self.reason_codes),
            "trading_evidence_age_seconds": self.trading_evidence_age_seconds,
            "muted": self.muted,
        }


def evaluate_heartbeat(
    heartbeat: Heartbeat | Mapping[str, Any] | None,
    *,
    now_ms: int,
    previous: HeartbeatState | None = None,
    trading_evidence_max_age_ms: int = 120_000,
    future_tolerance_ms: int = 5_000,
    expected_generation_id: str | None = None,
    expected_session_id: str | None = None,
) -> HeartbeatVerdict:
    """判定一次心跳；陈旧/重放/时钟异常**不**更新健康时间。

    拒绝面（任一命中即 ``accepted=False``，``healthy=False``）：

    - payload 形状非法 → ``HEARTBEAT_MALFORMED``；
    - 缺 ``emitted_at_ms`` → ``HEARTBEAT_TIMESTAMP_MISSING``；
    - 发送时间晚于 ``now + future_tolerance`` → ``HEARTBEAT_FROM_FUTURE``；
    - 发送时间/序号/交易证据时间**回退**于上一条已接受心跳
      → ``HEARTBEAT_REPLAY_OUT_OF_ORDER`` / ``HEARTBEAT_SEQUENCE_REPLAY`` /
      ``TRADING_EVIDENCE_REPLAY``（拒绝陈旧重放）；
    - 运行代/会话与预期不符 → ``HEARTBEAT_GENERATION_MISMATCH``；
    - 缺交易证据时间 → ``TRADING_EVIDENCE_MISSING``（心跳活着 ≠ 交易健康）；
    - 交易证据时间晚于发送时间，**或晚于本地 ``now``** → ``TRADING_EVIDENCE_FROM_FUTURE``
      （不得让"未来证据"被 ``max(0.0, …)`` 夹成 ``age=0`` 的假健康）；
    - 交易证据过旧 → ``TRADING_EVIDENCE_STALE``。

    维护窗口生效时 ``muted=True``（``healthy=False`` 但不升级告警）；窗口必须
    有界，无界窗口 → ``MAINTENANCE_WINDOW_UNBOUNDED`` 且不生效。
    """
    if heartbeat is None:
        return HeartbeatVerdict(False, False, (HEARTBEAT_MALFORMED,), None)
    if isinstance(heartbeat, Mapping):
        try:
            heartbeat = _heartbeat_from_mapping(heartbeat)
        except (TypeError, ValueError):
            return HeartbeatVerdict(False, False, (HEARTBEAT_MALFORMED,), None)
    if not isinstance(heartbeat, Heartbeat):
        return HeartbeatVerdict(False, False, (HEARTBEAT_MALFORMED,), None)

    reasons: list[str] = []
    emitted = heartbeat.emitted_at_ms
    if not isinstance(emitted, int) or isinstance(emitted, bool) or emitted <= 0:
        return HeartbeatVerdict(False, False, (HEARTBEAT_TIMESTAMP_MISSING,), None)
    if emitted > int(now_ms) + int(future_tolerance_ms):
        reasons.append(HEARTBEAT_FROM_FUTURE)

    prev = previous or HeartbeatState()
    if prev.emitted_at_ms is not None and emitted < int(prev.emitted_at_ms):
        reasons.append(HEARTBEAT_REPLAY_OUT_OF_ORDER)
    if (heartbeat.sequence is not None and prev.sequence is not None
            and int(heartbeat.sequence) <= int(prev.sequence)):
        reasons.append(HEARTBEAT_SEQUENCE_REPLAY)
    if expected_generation_id and heartbeat.generation_id != expected_generation_id:
        reasons.append(HEARTBEAT_GENERATION_MISMATCH)
    if expected_session_id and heartbeat.session_id != expected_session_id:
        reasons.append(HEARTBEAT_GENERATION_MISMATCH)

    evidence = heartbeat.trading_evidence_at_ms
    age: float | None = None
    if evidence is None:
        reasons.append(TRADING_EVIDENCE_MISSING)
    else:
        evidence = int(evidence)
        age = max(0.0, (int(now_ms) - evidence) / 1000.0)
        # 只比 emitted 不够：emitted 本身在容差内"略微向未来"时，未来证据会被判
        # 健康且 age 被 max(0.0, …) 夹成 0 ⇒ 时钟异常可长期掩盖停摆。故**严格**
        # 对比本地 now（证据由本机交易库产出，晚于本地当前时刻即属时钟异常）。
        if evidence > emitted or evidence > int(now_ms):
            reasons.append(TRADING_EVIDENCE_FROM_FUTURE)
        if prev.trading_evidence_at_ms is not None and evidence < int(prev.trading_evidence_at_ms):
            reasons.append(TRADING_EVIDENCE_REPLAY)
        if int(now_ms) - evidence > int(trading_evidence_max_age_ms):
            reasons.append(TRADING_EVIDENCE_STALE)

    # 维护窗口是**静音**而不是**拒绝**：payload 仍然合法（accepted=True、时间线
    # 照常推进，这样窗口到期后不需要重新"补心跳"），但不得据此宣称健康。
    muted, mute_reasons = maintenance_active(heartbeat.maintenance, now_ms=int(now_ms))
    # 无界维护窗口本身是**畸形声明**（"不能无限静音"），必须拒绝而不是静默忽略：
    # 静默忽略等于让一份无限静音的请求"看起来被接受"。
    rejections = list(dict.fromkeys(
        [*reasons, *[code for code in mute_reasons if code == MAINTENANCE_WINDOW_UNBOUNDED]]))
    accepted = not rejections
    healthy = accepted and not muted
    codes = list(dict.fromkeys([*rejections, *mute_reasons]))
    if not codes:
        codes = [HEARTBEAT_OK]
    return HeartbeatVerdict(accepted, healthy, tuple(codes), age, muted)


def _heartbeat_from_mapping(payload: Mapping[str, Any]) -> Heartbeat:
    """从结构化 Mapping 还原心跳（只接受显式字段，**不**猜默认值）。"""
    window = payload.get("maintenance")
    maintenance = None
    if isinstance(window, Mapping):
        maintenance = MaintenanceWindow(
            scope=str(window.get("scope") or ""),
            starts_at_ms=int(window.get("starts_at_ms") or 0),
            expires_at_ms=int(window.get("expires_at_ms") or 0),
            actor=str(window.get("actor") or ""),
            note=str(window.get("note") or ""),
        )
    return Heartbeat(
        environment=str(payload.get("environment") or ""),
        emitted_at_ms=int(payload["emitted_at_ms"]),
        trading_evidence_at_ms=(
            None if payload.get("trading_evidence_at_ms") is None
            else int(payload["trading_evidence_at_ms"])),
        last_fast_cycle_at_ms=(
            None if payload.get("last_fast_cycle_at_ms") is None
            else int(payload["last_fast_cycle_at_ms"])),
        sequence=None if payload.get("sequence") is None else int(payload["sequence"]),
        session_id=str(payload.get("session_id") or ""),
        runtime_instance_id=str(payload.get("runtime_instance_id") or ""),
        generation_id=str(payload.get("generation_id") or ""),
        account_ref=str(payload.get("account_ref") or ""),
        maintenance=maintenance,
    )


def advance_heartbeat(
    previous: HeartbeatState | None,
    heartbeat: Heartbeat | Mapping[str, Any] | None,
    *,
    now_ms: int,
    **kwargs: Any,
) -> tuple[HeartbeatState, HeartbeatVerdict]:
    """接受则推进时间线，拒绝则**原样返回**上一条状态（陈旧重放不更新健康时间）。"""
    prev = previous or HeartbeatState()
    verdict = evaluate_heartbeat(heartbeat, now_ms=now_ms, previous=prev, **kwargs)
    if not verdict.accepted or not isinstance(heartbeat, (Heartbeat, Mapping)):
        return prev, verdict
    payload = heartbeat if isinstance(heartbeat, Heartbeat) else _heartbeat_from_mapping(heartbeat)
    return HeartbeatState(
        emitted_at_ms=int(payload.emitted_at_ms),
        trading_evidence_at_ms=payload.trading_evidence_at_ms,
        sequence=payload.sequence,
        generation_id=payload.generation_id or prev.generation_id,
        session_id=payload.session_id or prev.session_id,
    ), verdict


def read_heartbeat_record(path: Path) -> HeartbeatState | None:
    """只读读取外部监测保存的心跳时间线（用于跨轮次拒绝重放）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    try:
        return HeartbeatState(
            emitted_at_ms=(None if payload.get("emitted_at_ms") is None
                           else int(payload["emitted_at_ms"])),
            trading_evidence_at_ms=(None if payload.get("trading_evidence_at_ms") is None
                                    else int(payload["trading_evidence_at_ms"])),
            sequence=None if payload.get("sequence") is None else int(payload["sequence"]),
            generation_id=str(payload.get("generation_id") or ""),
            session_id=str(payload.get("session_id") or ""),
        )
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 交易证据时间（只读；跨表取最新）
# --------------------------------------------------------------------------

#: 交易证据来源：``(表, 列, 语义)``。**只读**，只用于回答"交易路径最后一条
#: 持久化证据是什么时候写的"。每一条的既有语义与消费者见 README 补丁说明：
#:
#: - ``release_observations.observed_at``：服务每个快循环写入的观察证据时刻
#:   （ISO-8601 UTC，``audit.database.utc_now()`` 口径）；既有消费者
#:   ``core.health_watch.probe_latest_observation``（证据流时效）与
#:   ``core.release_evidence``（观察窗口覆盖/门禁）。
#: - ``execution_fills.received_at``：本地**收到**成交事实的时刻；既有消费者
#:   ``core.release_evidence``（成交完整性/去重）。
#: - ``protection_reconciliations.checked_at``：保护单对账完成时刻；既有消费者
#:   ``core.health_watch.probe_protection``（保护对账时效）。
TRADING_EVIDENCE_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("release_observations", "observed_at", "服务快循环写入的观察证据时刻"),
    ("execution_fills", "received_at", "本地收到成交事实的时刻"),
    ("protection_reconciliations", "checked_at", "保护单对账完成时刻"),
)


@dataclass(frozen=True)
class TradingEvidence:
    """交易证据时间的读取结果；读不到时明确报告不可用（不假装为零）。"""

    available: bool
    at_ms: int | None
    sources: dict[str, int | None] = field(default_factory=dict)
    reason_codes: tuple[str, ...] = ()

    @property
    def age_seconds(self) -> float | None:
        if self.at_ms is None:
            return None
        return max(0.0, time.time() - self.at_ms / 1000.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "at_ms": self.at_ms,
            "sources": dict(self.sources),
            "reason_codes": list(self.reason_codes),
        }


def trading_evidence_timestamp(
    connection: sqlite3.Connection,
    *,
    environment: str | None = None,
    sources: Sequence[tuple[str, str, str]] = TRADING_EVIDENCE_SOURCES,
) -> TradingEvidence:
    """只读取"交易证据时间" = 各来源最大值（毫秒）。

    **不写任何东西**：只发 ``SELECT MAX(...)``。任一张表缺失/列缺失/数据库损坏
    都只影响该来源（记入 ``reason_codes``），不抛裸异常——F22 要求"主库损坏时
    仍能发送严重告警"，因此调用方拿到 ``available=False`` 也必须能继续投递。
    """
    found: dict[str, int | None] = {}
    reasons: list[str] = []
    for table, column, _semantics in sources:
        key = f"{table}.{column}"
        clause = ""
        params: tuple[Any, ...] = ()
        try:
            columns = {
                str(row[1]) for row in
                connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns:
                raise sqlite3.OperationalError(f"no such table: {table}")
            if column not in columns:
                raise sqlite3.OperationalError(f"no such column: {column}")
            if environment and "environment" in columns:
                clause = " WHERE environment=?"
                params = (str(environment).lower(),)
            row = connection.execute(
                f"SELECT MAX({column}) AS at FROM {table}{clause}", params).fetchone()
        except (sqlite3.Error, OSError, ValueError) as exc:
            found[key] = None
            reasons.append(f"SOURCE_UNAVAILABLE:{key}:{type(exc).__name__}")
            continue
        raw = None
        if row is not None:
            raw = row["at"] if isinstance(row, sqlite3.Row) else row[0]
        found[key] = parse_iso_ms(raw)
    values = [value for value in found.values() if value is not None]
    if not values:
        return TradingEvidence(False, None, found, tuple(reasons) or ("NO_EVIDENCE",))
    return TradingEvidence(True, max(values), found, tuple(reasons))


# --------------------------------------------------------------------------
# 告警消息（脱敏边界）
# --------------------------------------------------------------------------


def validate_message(message: Mapping[str, Any]) -> tuple[str, ...]:
    """校验告警消息不含密钥/签名/原始 prompt；返回违规键名（空 = 通过）。

    只扫描**键名**（复用 U15 ``is_sensitive_name`` 的保守子串口径）+ 规格点名的
    三个禁键；**不做值内容扫描**——这是 ``docs/contracts/README.md`` §12 L1 已
    登记的已知边界，本模块不假装它不存在。
    """
    from core.runtime_identity import is_sensitive_name

    violations: list[str] = []
    for key in _walk_keys(message):
        name = str(key)
        if name in MESSAGE_FORBIDDEN_KEYS or is_sensitive_name(name):
            violations.append(name)
    return tuple(dict.fromkeys(violations))


def _walk_keys(payload: Any) -> Iterable[str]:
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            yield str(key)
            yield from _walk_keys(value)
    elif isinstance(payload, (list, tuple)):
        for item in payload:
            yield from _walk_keys(item)


def build_message(
    *,
    profile: str,
    host: str,
    object_ref: str,
    reason_code: str,
    severity: str,
    impact: str,
    new_entry_state: str,
    handling_entry: str,
    occurred_at_ms: int,
    observed_at_ms: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造脱敏告警消息（规格 §11.2 必含字段）；含禁键即拒绝。"""
    message: dict[str, Any] = {
        "schema": "u14.alert.v1",
        "profile": str(profile),
        "host": str(host),
        "object_ref": str(object_ref),
        "reason_code": str(reason_code),
        "severity": str(severity).upper(),
        "impact": str(impact),
        "new_entry_state": str(new_entry_state),
        "handling_entry": str(handling_entry),
        "occurred_at": iso_ms(int(occurred_at_ms)),
        "occurred_at_ms": int(occurred_at_ms),
    }
    if observed_at_ms is not None:
        message["observed_at"] = iso_ms(int(observed_at_ms))
    if extra:
        message.update({str(k): v for k, v in extra.items()})
    violations = validate_message(message)
    if violations:
        raise ValueError(f"alert message contains forbidden keys: {', '.join(violations)}")
    return message


def build_recovery_message(alert: "Alert", *, recovered_at_ms: int,
                           recovery_evidence: str) -> dict[str, Any]:
    """恢复消息：**保留首次发生时间**，不得把积压误报为新故障。"""
    message = dict(alert.message)
    message["recovered"] = True
    message["recovered_at"] = iso_ms(int(recovered_at_ms))
    message["recovery_evidence"] = str(recovery_evidence)
    message["first_occurred_at"] = iso_ms(int(alert.occurrence_at_ms))
    message["occurrence_count"] = int(alert.occurrence_count)
    message["backlog_delayed_seconds"] = round(
        max(0.0, (int(recovered_at_ms) - int(alert.occurrence_at_ms)) / 1000.0), 3)
    violations = validate_message(message)
    if violations:
        raise ValueError(f"recovery message contains forbidden keys: {', '.join(violations)}")
    return message


# --------------------------------------------------------------------------
# 通道策略
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelPolicy:
    """外部告警通道能力声明。

    ``verified`` **只**表示"已配置为已批准通道"，不表示任何送达证据；规格明确
    "仅将 verified=true 写入配置不通过 OPS"，因此 :func:`ops_status` 还要看
    真实回执（``DELIVERED``/``ACKNOWLEDGED``）。
    """

    channel: str = "unverified"
    status: str = "unverified"
    note: str = ""
    supports_delivery_receipt: bool = False
    receiver_ref: str = ""
    escalation_path: str = ""

    @property
    def verified(self) -> bool:
        return str(self.status).strip().lower() == "verified"

    @property
    def has_escalation(self) -> bool:
        return bool(str(self.escalation_path).strip())

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "status": self.status,
            "verified": self.verified,
            "supports_delivery_receipt": self.supports_delivery_receipt,
            "receiver_ref": self.receiver_ref,
            "escalation_path": self.escalation_path,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 投递队列（独立于交易库）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Alert:
    """一条待投递告警（已由调用方脱敏）。"""

    incident_key: str
    environment: str
    severity: str
    reason_code: str
    object_ref: str = ""
    profile: str = ""
    host: str = ""
    occurrence_at_ms: int = 0
    occurrence_count: int = 1
    receiver_ref: str = ""
    message: Mapping[str, Any] = field(default_factory=dict)

    def alert_id(self) -> str:
        seed = f"{self.environment}|{self.incident_key}|{int(self.occurrence_at_ms) // 60000}"
        return "alert-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]

    def as_dict(self) -> dict[str, Any]:
        return {
            "incident_key": self.incident_key,
            "environment": self.environment,
            "severity": self.severity,
            "reason_code": self.reason_code,
            "object_ref": self.object_ref,
            "profile": self.profile,
            "host": self.host,
            "occurrence_at_ms": int(self.occurrence_at_ms),
            "occurrence_count": int(self.occurrence_count),
            "receiver_ref": self.receiver_ref,
            "message": dict(self.message),
        }


@dataclass(frozen=True)
class EnqueueOutcome:
    alert_id: str
    accepted: bool
    deduplicated: bool
    dropped_oldest: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "accepted": self.accepted,
            "deduplicated": self.deduplicated,
            "dropped_oldest": self.dropped_oldest,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class TransportOutcome:
    """一次通道提交的结果。**只有**携带真实回执才算送达。

    ``delivered_at_ms`` **必须**与 ``device_receipt`` 同时出现才被采纳
    （见 :meth:`AlertQueue.dispatch_due`）；HTTP 2xx 单独出现只到 ``SUBMITTED``。

    ``receipt_alert_id`` 是回执的**身份绑定**：通道必须声明这次回执属于哪条消息
    （真实实现可回带请求里的 ``alert_id``/cid/消息 id）。缺省 ``None`` ⇒ 未绑定，
    :meth:`AlertQueue.dispatch_due` 会按 :data:`RECEIPT_UNBOUND` 拒绝，避免
    "他消息的回执"把本告警标成送达（R14）。
    """

    ok: bool
    http_status: int | None = None
    provider_receipt: str | None = None
    delivered_at_ms: int | None = None
    device_receipt: str | None = None
    error: str = ""
    receipt_alert_id: str | None = None


class AlertTransport(Protocol):
    """告警通道协议：只依赖结构化入参，不接触交易密钥。"""

    name: str

    def submit(
        self, *, alert: Alert, receiver_ref: str, attempt: int, now_ms: int,
    ) -> TransportOutcome:  # pragma: no cover - 协议
        ...


class UnconfiguredTransport:
    """未配置任何真实通道：明确拒绝，绝不假装成功。"""

    name = "unconfigured"

    def submit(self, *, alert: Alert, receiver_ref: str, attempt: int,
               now_ms: int) -> TransportOutcome:
        return TransportOutcome(False, error=TRANSPORT_NOT_CONFIGURED)


@dataclass
class RecordingSink:
    """离线演练用的 fake sink（规格："未选告警渠道时用 fake sink 做测试"）。

    只把提交内容记在内存里，**不**联网、不发消息、不伪造送达回执：默认只返回
    ``http_status=200``（⇒ 只能到 ``SUBMITTED``）。要模拟"供应商接受"必须显式
    传 ``provider_receipt``；要模拟"设备送达"必须**同时**传
    ``delivered_at_ms`` 与 ``device_receipt``。

    回执身份绑定：默认回带**本次提交的那条消息**的 ``alert_id``（真实网关的正常
    行为）；要构造"他消息的回执"这类伪造，显式传 ``receipt_alert_id``。
    """

    provider_receipt: str | None = None
    delivered_at_ms: int | None = None
    device_receipt: str | None = None
    http_status: int = 200
    fail_with: str = ""
    receipt_alert_id: str | None = None
    name = "fake-sink"
    submissions: list[dict[str, Any]] = field(default_factory=list)

    def submit(self, *, alert: Alert, receiver_ref: str, attempt: int,
               now_ms: int) -> TransportOutcome:
        self.submissions.append({
            "alert_id": alert.alert_id(),
            "incident_key": alert.incident_key,
            "attempt": int(attempt),
            "at_ms": int(now_ms),
            "receiver_ref": receiver_ref,
        })
        if self.fail_with:
            return TransportOutcome(False, http_status=self.http_status, error=self.fail_with)
        return TransportOutcome(
            True, http_status=int(self.http_status),
            provider_receipt=self.provider_receipt,
            delivered_at_ms=self.delivered_at_ms,
            device_receipt=self.device_receipt,
            receipt_alert_id=(self.receipt_alert_id
                              if self.receipt_alert_id is not None
                              else alert.alert_id()),
        )


_QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS alert_queue (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL UNIQUE,
    incident_key TEXT NOT NULL,
    environment TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'UNKNOWN',
    occurrence_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ms INTEGER,
    last_error TEXT,
    payload_json TEXT NOT NULL,
    provider_receipt TEXT,
    device_receipt TEXT,
    submitted_at_ms INTEGER,
    delivered_at_ms INTEGER,
    ack_at_ms INTEGER,
    receiver_ref TEXT,
    receipt_bound_alert_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_queue_due ON alert_queue(state, next_attempt_ms);
CREATE INDEX IF NOT EXISTS idx_alert_queue_incident ON alert_queue(incident_key, last_seen_at_ms);
CREATE TABLE IF NOT EXISTS alert_audit (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id TEXT NOT NULL,
    transition TEXT NOT NULL,
    at_ms INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_alert_audit_alert ON alert_audit(alert_id, at_ms);
CREATE TABLE IF NOT EXISTS alert_queue_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AlertQueue:
    """独立于交易库的告警投递队列（有限容量 / 去重 / 冷却聚合 / 指数退避）。

    设计边界（R14/F22）：

    - 队列落在**自己的 SQLite 文件**（默认 ``<state_root>/ops/alert_queue.sqlite``），
      不打开、不锁、不写交易库；交易库损坏时仍可投递严重告警；
    - 每个投递状态变化都经 :func:`core.contracts.k05_trace.transition_state`，
      写入后立即跑 ``delivery_violations`` 自检；违规抛
      :class:`AlertLadderViolation` 并回滚该次写入；
    - ``occurrence_at_ms`` 一经写入**不再改写**（积压恢复后保留发生时间）；
    - 投递失败按指数退避（30→60→120…上限 3600 秒）重排，不忙轮询、不无限重试。
    """

    def __init__(
        self,
        path: Path,
        *,
        capacity: int = DEFAULT_CAPACITY,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        dedup_seconds: float = DEFAULT_DEDUP_SECONDS,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.capacity = max(1, int(capacity))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.dedup_seconds = max(0.0, float(dedup_seconds))
        self.base_backoff_seconds = max(0.0, float(base_backoff_seconds))
        self.max_backoff_seconds = max(self.base_backoff_seconds, float(max_backoff_seconds))
        self.conn = sqlite3.connect(str(self.path), timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(_QUEUE_SCHEMA)
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self) -> None:
        """幂等补列：``receipt_bound_alert_id`` 只为既存队列文件补（新库已含）。

        ``ALTER TABLE ... ADD COLUMN`` 没有 ``IF NOT EXISTS``，故先读
        ``PRAGMA table_info``；本表无历史数据需要迁移顺序约束，不涉及迁移版本。
        """
        existing = {str(row["name"]) for row in
                    self.conn.execute("PRAGMA table_info(alert_queue)")}
        if "receipt_bound_alert_id" not in existing:
            self.conn.execute(
                "ALTER TABLE alert_queue ADD COLUMN receipt_bound_alert_id TEXT")
            self.conn.commit()

    # ---------------- 生命周期 ----------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "AlertQueue":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------------- 入队 ----------------

    def _meta_int(self, key: str) -> int:
        row = self.conn.execute(
            "SELECT value FROM alert_queue_meta WHERE key=?", (key,)).fetchone()
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _bump_meta(self, key: str, delta: int) -> None:
        self.conn.execute(
            "INSERT INTO alert_queue_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(CAST(value AS INTEGER)+? AS TEXT)",
            (key, str(self._meta_int(key) + delta), delta))

    def dropped_total(self) -> int:
        """容量淘汰累计条数（有限容量的可审计证据）。"""
        return self._meta_int("dropped_total")

    def count(self, *, state: str | None = None) -> int:
        if state:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM alert_queue WHERE state=?", (state,)).fetchone()
        else:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM alert_queue").fetchone()
        return int(row["n"])

    def enqueue(self, alert: Alert, *, now_ms: int) -> EnqueueOutcome:
        """入队（幂等 + 冷却聚合 + 有界容量）。

        冷却窗口内同一 ``incident_key`` 再次发生只做**聚合**：``occurrence_count``
        +1、``last_seen_at_ms`` 刷新，``occurrence_at_ms`` **保持不变**
        （积压恢复后不得误报为新故障）。
        """
        now = _now_ms(now_ms)
        alert_id = alert.alert_id()
        reasons: list[str] = []
        with self.conn:
            recent = self.conn.execute(
                "SELECT * FROM alert_queue WHERE incident_key=? AND last_seen_at_ms>=? "
                "ORDER BY last_seen_at_ms DESC LIMIT 1",
                (alert.incident_key, now - int(self.cooldown_seconds * 1000)),
            ).fetchone()
            if recent is not None:
                self.conn.execute(
                    "UPDATE alert_queue SET occurrence_count=occurrence_count+1, "
                    "last_seen_at_ms=? WHERE row_id=?", (now, int(recent["row_id"])))
                self._audit(str(recent["alert_id"]), ALERT_DEDUPLICATED, now,
                            note=f"occurrence_count={int(recent['occurrence_count']) + 1}")
                self.conn.commit()
                return EnqueueOutcome(str(recent["alert_id"]), True, True, False,
                                     (ALERT_DEDUPLICATED,))
            existing = self.conn.execute(
                "SELECT alert_id FROM alert_queue WHERE alert_id=?", (alert_id,)).fetchone()
            if existing is not None:
                return EnqueueOutcome(alert_id, True, True, False, (ALERT_DEDUPLICATED,))

            dropped = self._evict_if_needed(now)
            if dropped:
                reasons.append(QUEUE_CAPACITY_EXCEEDED)
            self.conn.execute(
                "INSERT INTO alert_queue(alert_id,incident_key,environment,severity,state,"
                "occurrence_at_ms,created_at_ms,last_seen_at_ms,occurrence_count,attempts,"
                "next_attempt_ms,payload_json,receiver_ref) "
                "VALUES(?,?,?,?,'UNKNOWN',?,?,?,?,0,?,?,?)",
                (alert_id, alert.incident_key, alert.environment,
                 str(alert.severity).upper(), int(alert.occurrence_at_ms), now,
                 max(now, int(alert.occurrence_at_ms)), max(1, int(alert.occurrence_count)),
                 0 if not dropped else now,
                 json.dumps(alert.as_dict(), ensure_ascii=False, sort_keys=True),
                 alert.receiver_ref),
            )
            self._audit(alert_id, ALERT_ENQUEUED, now,
                        note=f"severity={alert.severity} reason={alert.reason_code}")
            self.conn.commit()
        reasons.append(ALERT_ENQUEUED)
        return EnqueueOutcome(alert_id, True, False, bool(dropped), tuple(reasons))

    def _evict_if_needed(self, now_ms: int) -> bool:
        """有界容量：超限时淘汰**最早入队**的一条，并记入累计淘汰数。"""
        dropped = False
        while self.count() >= self.capacity:
            row = self.conn.execute(
                "SELECT row_id, alert_id FROM alert_queue "
                "ORDER BY created_at_ms ASC, row_id ASC LIMIT 1").fetchone()
            if row is None:
                break
            self.conn.execute("DELETE FROM alert_queue WHERE row_id=?", (int(row["row_id"]),))
            self._bump_meta("dropped_total", 1)
            self._audit(str(row["alert_id"]), QUEUE_CAPACITY_EXCEEDED, now_ms,
                        note="evicted for bounded capacity")
            dropped = True
        return dropped

    def _audit(self, alert_id: str, transition: str, at_ms: int, *,
               actor: str = "", note: str = "", detail: Mapping[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO alert_audit(alert_id,transition,at_ms,actor,note,detail_json) "
            "VALUES(?,?,?,?,?,?)",
            (alert_id, transition, int(at_ms), actor, note,
             json.dumps(dict(detail or {}), ensure_ascii=False, sort_keys=True)))

    def audit_trail(self, alert_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM alert_audit WHERE alert_id=? ORDER BY row_id", (alert_id,))]

    # ---------------- 状态查询 ----------------

    def get(self, alert_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM alert_queue WHERE alert_id=?", (alert_id,)).fetchone()
        return dict(row) if row else None

    def due(self, *, now_ms: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM alert_queue WHERE state IN ('UNKNOWN','SUBMITTED','ACCEPTED') "
            "AND (next_attempt_ms IS NULL OR next_attempt_ms<=?) "
            "ORDER BY created_at_ms ASC LIMIT ?",
            (_now_ms(now_ms), int(limit))).fetchall()
        return [dict(row) for row in rows]

    def oldest_pending_age_seconds(self, *, now_ms: int) -> float | None:
        row = self.conn.execute(
            "SELECT MIN(occurrence_at_ms) AS at FROM alert_queue "
            "WHERE state IN ('UNKNOWN','SUBMITTED','ACCEPTED')").fetchone()
        if row is None or row["at"] is None:
            return None
        return max(0.0, (_now_ms(now_ms) - int(row["at"])) / 1000.0)

    def states(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT state, COUNT(*) AS n FROM alert_queue GROUP BY state").fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    # ---------------- 阶梯写入（全部经 K05 契约） ----------------

    def _delivery(self, row: Mapping[str, Any]) -> AlertDelivery:
        return AlertDelivery(
            incident_key=str(row["incident_key"]),
            attempt=max(1, int(row["attempts"])),
            receiver_ref=str(row["receiver_ref"] or ""),
            provider_receipt=row["provider_receipt"],
            submitted_at_ms=row["submitted_at_ms"],
            delivered_at_ms=row["delivered_at_ms"],
            ack_at_ms=row["ack_at_ms"],
            state=str(row["state"]),
        )

    @staticmethod
    def _raise_to(delivery: AlertDelivery, target: str, *, now_ms: int) -> AlertDelivery:
        """单调提升到 ``target``（已在此级或更高则原样返回，不倒退）。

        前置事实缺失（例如没有 ``provider_receipt`` 就想标 ``DELIVERED``）由冻结
        契约 :func:`transition_state` 拒绝；这里把它归一成本模块的结构化
        :class:`AlertLadderViolation`（原因码带目标状态），调用方**不会**因为
        "补证顺序不对"而拿到一个裸 ``TransitionError``。
        """
        if ALERT_STATE_ORDER.index(target) <= ALERT_STATE_ORDER.index(delivery.state):
            return delivery
        try:
            return transition_state(delivery, target, now_ms=int(now_ms))
        except TransitionError as exc:
            raise AlertLadderViolation((f"{TRANSITION_REJECTED}:{target}:{exc}",)) from exc

    def _persist(self, alert_id: str, delivery: AlertDelivery, *, now_ms: int,
                 transition: str, actor: str = "", note: str = "",
                 next_attempt_ms: int | None = None, last_error: str = "") -> None:
        # 阶梯自检（K05 是唯一判定源）：违规即拒不落库，杜绝"先提升后补证"。
        violations = delivery_violations(delivery)
        if violations:
            raise AlertLadderViolation(violations)
        with self.conn:
            # device_receipt 不在 K05 模型里（合同 L5 明示无该字段），只存队列行，
            # 且只由 record_delivered 写入——这里不覆盖，避免抹掉送达回执审计。
            self.conn.execute(
                "UPDATE alert_queue SET state=?, provider_receipt=?, "
                "submitted_at_ms=?, delivered_at_ms=?, ack_at_ms=?, next_attempt_ms=?, "
                "last_error=? WHERE alert_id=?",
                (delivery.state, delivery.provider_receipt,
                 delivery.submitted_at_ms, delivery.delivered_at_ms, delivery.ack_at_ms,
                 next_attempt_ms, last_error, alert_id))
            self._audit(alert_id, transition, int(now_ms), actor=actor, note=note,
                        detail={"state": delivery.state})
            self.conn.commit()

    def _schedule_recheck(self, alert_id: str, *, now_ms: int, delay_seconds: float) -> None:
        """已接受但未送达时按冷却间隔复查，避免对通道重复轰炸。"""
        with self.conn:
            self.conn.execute(
                "UPDATE alert_queue SET next_attempt_ms=? WHERE alert_id=?",
                (int(now_ms) + int(max(0.0, delay_seconds) * 1000), alert_id))
            self.conn.commit()

    def record_submitted(self, alert_id: str, *, now_ms: int, receiver_ref: str = "") -> str:
        """记录"已提交到通道"（HTTP 2xx 只到这一级）。"""
        row = self._require(alert_id)
        delivery = self._delivery(row)
        if receiver_ref:
            delivery = delivery.model_copy(update={"receiver_ref": receiver_ref})
        if delivery.submitted_at_ms is None:
            delivery = self._raise_to(delivery, "SUBMITTED", now_ms=now_ms)
        self._persist(alert_id, delivery, now_ms=now_ms, transition="SUBMITTED",
                      note="transport accepted submission (no delivery receipt yet)")
        return delivery.state

    @staticmethod
    def _bind_receipt(alert_id: str, receipt: object, *, field: str,
                      bound_alert_id: object) -> str:
        """回执的**身份绑定 + 类型 + 非空**校验（P1-3 / P1-4）：不绑定本消息一律结构化拒绝。

        顺序刻意如此：类型 → 非空 → 绑定存在 → 绑定一致。**全部**拒绝都抛
        :class:`AlertReceiptRejected`（它本身是 :class:`ValueError`，且错误串里保留
        ``field`` 名），不再混用裸 :class:`ValueError`——裸 ``ValueError`` 不是
        ``AlertReceiptRejected``，会让 :meth:`AlertQueue.dispatch_due` 的单条隔离漏网
        （P1-4：异常穿出 ⇒ 整批中断 + 该行无退避 + 每轮复发）。
        """
        if not isinstance(receipt, str):
            raise AlertReceiptRejected((RECEIPT_TYPE_INVALID,))
        if not receipt.strip():
            raise AlertReceiptRejected((RECEIPT_EMPTY,), field=field)
        if not isinstance(bound_alert_id, str) or not bound_alert_id.strip():
            raise AlertReceiptRejected((RECEIPT_UNBOUND,))
        if bound_alert_id != alert_id:
            raise AlertReceiptRejected((RECEIPT_BINDING_MISMATCH,))
        return receipt

    def _bind_receipt_columns(self, alert_id: str, bound_alert_id: str) -> None:
        """把"回执已绑定到哪条消息"落库，供事后审计核对（P1-3）。"""
        with self.conn:
            self.conn.execute(
                "UPDATE alert_queue SET receipt_bound_alert_id=? WHERE alert_id=?",
                (str(bound_alert_id), alert_id))
            self.conn.commit()

    def record_accepted(self, alert_id: str, *, now_ms: int, provider_receipt: str,
                        bound_alert_id: str | None = None) -> str:
        """供应商**接收**：``ACCEPTED`` ≠ 送达（R14 的核心区分）。

        ``provider_receipt`` 必须是字符串且**声明属于本条消息**
        （``bound_alert_id == alert_id``），否则结构化拒绝：他消息的回执不得把
        本告警标成 ``ACCEPTED``。
        """
        self._bind_receipt(alert_id, provider_receipt, field="provider_receipt",
                           bound_alert_id=bound_alert_id)
        row = self._require(alert_id)
        delivery = self._delivery(row)
        if delivery.submitted_at_ms is None:
            delivery = self._raise_to(delivery, "SUBMITTED", now_ms=now_ms)
        delivery = delivery.model_copy(update={"provider_receipt": provider_receipt})
        delivery = self._raise_to(delivery, "ACCEPTED", now_ms=now_ms)
        self._persist(alert_id, delivery, now_ms=now_ms, transition="ACCEPTED",
                      note="provider receipt recorded; delivery NOT confirmed")
        self._bind_receipt_columns(alert_id, str(bound_alert_id))
        return delivery.state

    def record_delivered(self, alert_id: str, *, now_ms: int, delivered_at_ms: int,
                         device_receipt: str, provider_receipt: str | None = None,
                         bound_alert_id: str | None = None) -> str:
        """真实**送达**：必须同时有设备回执与送达时间，否则拒绝（不伪造 DELIVERED）。

        P1-3 的两道额外门槛：

        - 设备回执必须**绑定本条消息**（``bound_alert_id == alert_id``，且为字符串）；
        - ``delivered_at_ms`` 必须是非布尔整数，且不得超出 ``now + 容差``
          （未来时间戳不得用来伪造"已送达"）；下界仍由 K05 ``delivery_violations``
          的 ``TIME_ORDER_VIOLATION`` 把关（严格，不在此处放宽）。
        """
        self._bind_receipt(alert_id, device_receipt, field="device_receipt",
                           bound_alert_id=bound_alert_id)
        if not isinstance(delivered_at_ms, int) or isinstance(delivered_at_ms, bool):
            raise AlertReceiptRejected((RECEIPT_TYPE_INVALID,))
        if int(delivered_at_ms) > _now_ms(now_ms) + RECEIPT_TIMESTAMP_TOLERANCE_MS:
            raise AlertReceiptRejected((RECEIPT_TIMESTAMP_FUTURE,))
        row = self._require(alert_id)
        delivery = self._delivery(row)
        if provider_receipt:
            delivery = delivery.model_copy(update={"provider_receipt": str(provider_receipt)})
        if delivery.submitted_at_ms is None:
            delivery = self._raise_to(delivery, "SUBMITTED", now_ms=now_ms)
        if delivery.provider_receipt:
            delivery = self._raise_to(delivery, "ACCEPTED", now_ms=now_ms)
        delivery = delivery.model_copy(update={"delivered_at_ms": int(delivered_at_ms)})
        delivery = self._raise_to(delivery, "DELIVERED", now_ms=now_ms)
        self._persist(alert_id, delivery, now_ms=now_ms, transition="DELIVERED",
                      note=f"device_receipt={device_receipt[:32]} "
                           f"bound_alert_id={bound_alert_id[:32]}")
        with self.conn:
            self.conn.execute(
                "UPDATE alert_queue SET device_receipt=?, receipt_bound_alert_id=? "
                "WHERE alert_id=?",
                (device_receipt, str(bound_alert_id), alert_id))
            self.conn.commit()
        return delivery.state

    def record_manual_ack(self, alert_id: str, *, actor: str, note: str, confirm: bool,
                          now_ms: int) -> tuple[bool, tuple[str, ...]]:
        """无设备回执时用**可审计的人工确认**（规格 §11.2）。

        要求：``actor`` 非空且不是自动执行者（``auto``/``system``/…）、``note``
        非空、``confirm is True``；且该告警**已经提交过**（否则没有任何"端到端
        演练"事实，只能记 ``MANUAL_ACK_WITHOUT_SUBMISSION`` 而不改状态）。
        结果状态是 ``ACKNOWLEDGED``，``delivered_at_ms`` 保持 NULL——**不**伪造送达。

        本函数**只**返回 ``(bool, reasons)``，任何阶梯违规（例如对一条已 ``DELIVERED``
        的告警用更早的 ``now_ms`` 确认）都收敛成 ``(False, (TRANSITION_REJECTED, …))``，
        不把 :class:`AlertLadderViolation` 穿给调用方（P3-2）。
        """
        who = str(actor).strip()
        if not who or who.lower() in _AUTO_ACTORS:
            return False, (MANUAL_ACK_ACTOR_INVALID,)
        if not confirm or not str(note).strip():
            return False, (MANUAL_ACK_REQUIRED,)
        row = self._require(alert_id)
        delivery = self._delivery(row)
        if delivery.submitted_at_ms is None:
            with self.conn:
                self._audit(alert_id, MANUAL_ACK_WITHOUT_SUBMISSION, int(now_ms), actor=who,
                            note=str(note))
                self.conn.commit()
            return False, (MANUAL_ACK_WITHOUT_SUBMISSION,)
        delivery = delivery.model_copy(update={"ack_at_ms": int(now_ms)})
        try:
            delivery = self._raise_to(delivery, "ACKNOWLEDGED", now_ms=now_ms)
            self._persist(alert_id, delivery, now_ms=now_ms, transition="ACKNOWLEDGED",
                          actor=who, note=str(note))
        except AlertLadderViolation as exc:
            # 本函数契约是返回 ``(bool, reasons)``：已 DELIVERED 的告警再用更早的
            # now_ms 确认会触发阶梯时间序违规，必须收敛成元组而不是把异常穿给调用方。
            return False, tuple(dict.fromkeys((TRANSITION_REJECTED, *exc.reason_codes)))
        return True, ()

    def record_recovery(self, incident_key: str, *, now_ms: int, evidence: str) -> int:
        """记录恢复：只标记已闭合，不改写 ``occurrence_at_ms``（规格 §11.2）。"""
        rows = self.conn.execute(
            "SELECT alert_id FROM alert_queue WHERE incident_key=? AND state NOT IN "
            "('RESOLVED')", (incident_key,)).fetchall()
        with self.conn:
            for row in rows:
                self._audit(str(row["alert_id"]), ALERT_RECOVERED, int(now_ms),
                            note=str(evidence)[:200])
            self.conn.commit()
        return len(rows)

    def _require(self, alert_id: str) -> Mapping[str, Any]:
        row = self.get(alert_id)
        if row is None:
            raise KeyError(f"{UNKNOWN_ALERT}: {alert_id}")
        return row

    # ---------------- 投递 ----------------

    def dispatch_due(
        self,
        transport: AlertTransport,
        channel: ChannelPolicy,
        *,
        now_ms: int,
        limit: int = 20,
        drill: bool = False,
        accepted_poll_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """投递到期告警；通道未验证时**不**提交（除非显式 ``drill=True`` 演练）。

        返回结构含 ``submitted``/``confirmed``/``rejected``/``failed``/``blocked``/
        ``slo_breached`` 与原因码；``blocked=True`` 表示队列里已有到期告警但因通道
        未验证而未提交（这一事实会让 :func:`ops_status` 保持 ``OPS_BLOCKED``）。

        两条与真实网关有关的鲁棒性约束（P1-1 / P1-3）：

        1. **回执时间略早于本地提交**是真实网关的常态（供应商上报自己的时钟）。
           ``delivered_at_ms`` 落在 ``[submitted_at_ms - 容差, submitted_at_ms)`` 时
           按有界容差归一到 ``submitted_at_ms`` 并记
           :data:`PROVIDER_CLOCK_SKEW_TOLERATED` 审计行（如实标注，不静默掩盖）；
           超出容差则交由阶梯自检拒绝。
        2. **单条告警的异常不得中断整批**（P1-1 / P1-4）：每条 due 行是一个
           **隔离单元**，该单元内**任何** :class:`Exception` 都不得穿出本函数——
           一律收敛成该条的结构化失败记录（审计标签 + ``last_error``）并按退避重排，
           然后 ``continue`` 处理其余行。三类原因分别可辨：

           - 通道异常 ⇒ :data:`TRANSPORT_EXCEPTION` + ``TRANSPORT_EXCEPTION:<异常类名>``
             （计入 ``failed``）。真实网关的超时/连接重置/DNS/TLS 抖动只会让**该条**
             退避重试，不会让整条告警通路停摆。
           - 阶梯违规 / 回执拒绝 ⇒ :data:`TRANSITION_REJECTED` + 原因码（计入 ``rejected``）。
           - 其余内部异常 ⇒ :data:`INTERNAL_ERROR`（计入 ``errors``）。

           只有 :class:`BaseException`（``KeyboardInterrupt``/``SystemExit``）**不**被吞。
        """
        now = _now_ms(now_ms)
        result: dict[str, Any] = {
            "submitted": 0, "confirmed": 0, "failed": 0, "rejected": 0, "skipped": 0,
            "errors": 0, "blocked": False, "slo_breached": False, "reason_codes": [],
        }
        if not channel.verified and not drill:
            pending = self.due(now_ms=now, limit=limit)
            if pending:
                result["blocked"] = True
                result["skipped"] = len(pending)
                result["reason_codes"].append(CHANNEL_UNVERIFIED)
            return result
        if not channel.verified:
            result["reason_codes"].append(CHANNEL_UNVERIFIED)

        for row in self.due(now_ms=now, limit=limit):
            alert_id = str(row["alert_id"])
            attempt = 0
            try:
                attempt = int(row["attempts"]) + 1
                alert = _alert_from_row(row)
                try:
                    # 通道是唯一的外部依赖，也是"一次网络抖动即可让告警通路永久停摆"
                    # 的那条路径：它抛出的**任何**异常都在这里收敛成结构化的传输失败，
                    # 绝不穿出 dispatch_due（P1-4）。
                    outcome = transport.submit(
                        alert=alert,
                        receiver_ref=str(row["receiver_ref"] or channel.receiver_ref),
                        attempt=attempt, now_ms=now)
                except Exception as exc:  # noqa: BLE001 - 见上：通道侧兜底
                    outcome = TransportOutcome(
                        False, error=f"{TRANSPORT_EXCEPTION}:{type(exc).__name__}")
                if not outcome.ok:
                    error = outcome.error or TRANSPORT_FAILED
                    self._isolate_failure(alert_id, now_ms=now, attempt=attempt,
                                          error=error, transition=TRANSPORT_FAILED,
                                          result=result)
                    result["failed"] += 1
                    result["reason_codes"].append(error)
                    continue
                self._record_attempt(alert_id, now_ms=now, attempt=attempt)
                state = self.record_submitted(alert_id, now_ms=now,
                                              receiver_ref=str(row["receiver_ref"] or ""))
                result["submitted"] += 1
                bound = outcome.receipt_alert_id
                if outcome.provider_receipt:
                    state = self.record_accepted(
                        alert_id, now_ms=now, provider_receipt=outcome.provider_receipt,
                        bound_alert_id=bound)
                    if state == "ACCEPTED":
                        self._schedule_recheck(alert_id, now_ms=now,
                                               delay_seconds=accepted_poll_seconds)
                if outcome.delivered_at_ms is not None and outcome.device_receipt:
                    # 类型校验而非裸 int()（P1-4）：非数值的 delivered_at_ms 必须是
                    # **结构化的回执拒绝**，而不是把 ValueError/TypeError 抛到隔离单元外。
                    if not isinstance(outcome.delivered_at_ms, int) or isinstance(
                            outcome.delivered_at_ms, bool):
                        raise AlertReceiptRejected((RECEIPT_TYPE_INVALID,))
                    delivered_at = self._normalize_receipt_time(
                        alert_id, delivered_at_ms=outcome.delivered_at_ms,
                        submitted_at_ms=(int(row["submitted_at_ms"])
                                         if row["submitted_at_ms"] is not None else now),
                        now_ms=now, result=result)
                    state = self.record_delivered(
                        alert_id, now_ms=now, delivered_at_ms=delivered_at,
                        device_receipt=outcome.device_receipt,
                        provider_receipt=outcome.provider_receipt,
                        bound_alert_id=bound)
                elif outcome.delivered_at_ms is not None:
                    result["reason_codes"].append(DELIVERED_WITHOUT_DEVICE_RECEIPT)
                if state == "SUBMITTED":
                    # 只有 HTTP 2xx、还没有任何回执：也要按冷却间隔复查，否则
                    # next_attempt_ms 保持 NULL ⇒ 每轮 dispatch 都重新提交（无退避）。
                    self._schedule_recheck(alert_id, now_ms=now,
                                           delay_seconds=accepted_poll_seconds)
                if state in ("DELIVERED", "ACKNOWLEDGED"):
                    result["confirmed"] += 1
                latency = (now - int(row["occurrence_at_ms"])) / 1000.0
                if state in ("DELIVERED", "ACKNOWLEDGED") and latency > DELIVERY_SLO_SECONDS:
                    result["slo_breached"] = True
                    result["reason_codes"].append(BREACH_DELIVERY_SLO)
            except (AlertLadderViolation, AlertReceiptRejected) as exc:
                codes = getattr(exc, "reason_codes", ())
                self._isolate_failure(
                    alert_id, now_ms=now, attempt=attempt,
                    error=":".join((TRANSITION_REJECTED, *codes)) or TRANSITION_REJECTED,
                    transition=TRANSITION_REJECTED, result=result)
                result["rejected"] += 1
                result["reason_codes"].extend(codes or (TRANSITION_REJECTED,))
                continue
            except Exception as exc:  # noqa: BLE001 - P1-4：兜底，任意异常都不得中断整批
                error = f"{INTERNAL_ERROR}:{type(exc).__name__}"
                self._isolate_failure(alert_id, now_ms=now, attempt=attempt, error=error,
                                      transition=INTERNAL_ERROR, result=result)
                result["errors"] += 1
                result["reason_codes"].append(error)
                continue
        result["reason_codes"] = list(dict.fromkeys(result["reason_codes"]))
        return result

    def _normalize_receipt_time(self, alert_id: str, *, delivered_at_ms: int,
                                submitted_at_ms: int | None, now_ms: int,
                                result: dict[str, Any]) -> int:
        """把"略早于本地提交"的供应商回执时间按**有界**容差归一（P1-1）。

        返回可交给 :meth:`record_delivered` 的时间戳；超出容差时原样返回，让 K05 的
        ``TIME_ORDER_VIOLATION`` 去拒绝（不在这里放宽阶梯）。归一事实写入审计行与
        返回结构的 ``PROVIDER_CLOCK_SKEW_TOLERATED``，不静默。
        """
        if submitted_at_ms is None or delivered_at_ms >= int(submitted_at_ms):
            return delivered_at_ms
        skew_ms = int(submitted_at_ms) - delivered_at_ms
        if skew_ms > RECEIPT_TIMESTAMP_TOLERANCE_MS:
            return delivered_at_ms
        with self.conn:
            self._audit(alert_id, PROVIDER_CLOCK_SKEW_TOLERATED, int(now_ms),
                        note=f"provider delivered_at_ms={delivered_at_ms} earlier than "
                             f"submitted_at_ms={int(submitted_at_ms)} by {skew_ms}ms "
                             f"(<= tolerance {RECEIPT_TIMESTAMP_TOLERANCE_MS}ms); clamped")
            self.conn.commit()
        result["reason_codes"].append(PROVIDER_CLOCK_SKEW_TOLERATED)
        return int(submitted_at_ms)

    def _record_attempt(self, alert_id: str, *, now_ms: int, attempt: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE alert_queue SET attempts=?, next_attempt_ms=NULL, "
                              "last_error='' WHERE alert_id=?", (int(attempt), alert_id))
            self.conn.commit()

    def _isolate_failure(self, alert_id: str, *, now_ms: int, attempt: int, error: str,
                         transition: str, result: dict[str, Any]) -> None:
        """隔离单元的最后一环：记失败 + 退避，**连记录失败也不抛出**（P1-4）。

        "记录失败"本身是严重的持久化故障（磁盘满/库被锁），用
        ``PERSIST_FAILED:<异常类名>`` 如实暴露到返回结构里；但不得因此把整批投递带走。
        """
        try:
            self._record_failure(alert_id, now_ms=now_ms, attempt=attempt, error=error,
                                 transition=transition)
        except Exception as exc:  # noqa: BLE001 - 兜底：绝不再抛
            result["reason_codes"].append(f"{PERSIST_FAILED}:{type(exc).__name__}")

    def _record_failure(self, alert_id: str, *, now_ms: int, attempt: int, error: str,
                        transition: str = TRANSPORT_FAILED) -> None:
        """记一次失败并按 ``min(base * 2**(attempt-1), max)`` 退避（该条不再每轮复发）。

        ``transition`` 是审计表里的**原因类型**标签（P3-6），必须与 ``error`` 的语义一致：
        传输失败用 :data:`TRANSPORT_FAILED`（默认）、阶梯/回执拒绝用
        :data:`TRANSITION_REJECTED`、内部兜底用 :data:`INTERNAL_ERROR`。
        否则按 ``transition`` 过滤日志的消费者会把"阶梯拒绝"误读成"传输失败"。
        """
        delay = min(self.base_backoff_seconds * (2 ** max(0, attempt - 1)),
                    self.max_backoff_seconds)
        with self.conn:
            self.conn.execute(
                "UPDATE alert_queue SET attempts=?, next_attempt_ms=?, last_error=? "
                "WHERE alert_id=?",
                (int(attempt), int(now_ms) + int(delay * 1000), str(error)[:200], alert_id))
            self._audit(alert_id, transition, int(now_ms),
                        note=f"attempt={attempt} backoff={delay}s error={str(error)[:120]}",
                        detail={"reason": str(transition), "error": str(error)[:200],
                                "attempt": int(attempt), "backoff_seconds": float(delay)})
            self.conn.commit()


def _alert_from_row(row: Mapping[str, Any]) -> Alert:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, Mapping):
        payload = {}
    return Alert(
        incident_key=str(row["incident_key"]),
        environment=str(row["environment"]),
        severity=str(row["severity"]),
        reason_code=str(payload.get("reason_code") or ""),
        object_ref=str(payload.get("object_ref") or ""),
        profile=str(payload.get("profile") or ""),
        host=str(payload.get("host") or ""),
        occurrence_at_ms=int(row["occurrence_at_ms"]),
        occurrence_count=int(row["occurrence_count"]),
        receiver_ref=str(row["receiver_ref"] or ""),
        message=payload.get("message") if isinstance(payload.get("message"), Mapping) else {},
    )


# --------------------------------------------------------------------------
# 运维总状态（真实渠道未确认 ⇒ 保留 OPS_BLOCKED）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OpsVerdict:
    status: str
    reason_codes: tuple[str, ...]
    counts: dict[str, int]
    confirmed_states: tuple[str, ...] = ()
    manual_ack_only: bool = False
    backlog_overdue_seconds: float | None = None

    @property
    def blocked(self) -> bool:
        return self.status == OPS_BLOCKED

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "blocked": self.blocked,
            "reason_codes": list(self.reason_codes),
            "counts": dict(self.counts),
            "confirmed_states": list(self.confirmed_states),
            "manual_ack_only": self.manual_ack_only,
            "backlog_overdue_seconds": self.backlog_overdue_seconds,
        }


def ops_status(
    queue: AlertQueue,
    *,
    channel: ChannelPolicy,
    now_ms: int,
    backlog_overdue_seconds: float = BACKLOG_OVERDUE_SECONDS,
) -> OpsVerdict:
    """无人值守告警的运维总状态。

    判定顺序（严格 fail-closed）：

    1. 通道**未验证** → :data:`OPS_BLOCKED` / ``CHANNEL_UNVERIFIED``
       （配置里写 ``verified=true`` 只改变通道声明；这里还要真实回执，规格
       "仅将 verified=true 写入配置不通过 OPS"）；
    2. 队列里有到期未投递 / 积压超时 → :data:`OPS_BLOCKED` / ``BACKLOG_OVERDUE``；
    3. **当前**没有任何已确认送达证据（``DELIVERED``/``ACKNOWLEDGED`` 都为 0）
       → ``OPS_DEGRADED`` + ``NO_CONFIRMED_DELIVERY``：空队列 + 只写配置
       ``verified=true`` **也**不通过 OPS（配置不是送达证据）；
    4. **还存在待投递项**（``UNKNOWN``/``SUBMITTED``/``ACCEPTED``）→ 追加
       ``NO_CONFIRMED_DELIVERY``：队列里别的行曾经 ``DELIVERED`` **不能**作为
       当前这条卡住告警的健康证据（判据只看**当前**未闭合告警，不看历史行）；
    5. 只有人工确认（``ACKNOWLEDGED``）无任何设备回执 → ``OPS_DEGRADED`` +
       ``MANUAL_ACK_ONLY``（可审计，但**不等于**送达）；
    6. 只有"当前无待投递项 **且** 有已确认送达证据"才可能 :data:`OPS_OK`。
    """
    now = _now_ms(now_ms)
    counts = queue.states()
    pending = sum(counts.get(state, 0) for state in ("UNKNOWN", "SUBMITTED", "ACCEPTED"))
    confirmed = {"DELIVERED": counts.get("DELIVERED", 0),
                 "ACKNOWLEDGED": counts.get("ACKNOWLEDGED", 0)}
    confirmed_states = tuple(sorted(state for state, n in confirmed.items() if n))
    reasons: list[str] = []
    if not channel.verified:
        reasons.append(CHANNEL_UNVERIFIED)
    if not channel.has_escalation:
        reasons.append("NO_ESCALATION_PATH")
    oldest = queue.oldest_pending_age_seconds(now_ms=now)
    if oldest is not None and oldest > float(backlog_overdue_seconds):
        reasons.append(BACKLOG_OVERDUE)
    if pending == 0 and confirmed["DELIVERED"] == 0 and confirmed["ACKNOWLEDGED"] > 0:
        reasons.append(MANUAL_ACK_ONLY)
    # fail-closed：待投递项本身就是"未闭合"证据，无论队列别处是否有历史 DELIVERED；
    # 而"当前无任何已确认送达证据"同样不得报 OPS_OK。
    if pending > 0 or not confirmed_states:
        reasons.append(NO_CONFIRMED_DELIVERY)

    status = OPS_OK
    hard = [CHANNEL_UNVERIFIED, "NO_ESCALATION_PATH", BACKLOG_OVERDUE]
    if any(code in reasons for code in hard):
        status = OPS_BLOCKED
    elif reasons:
        status = OPS_DEGRADED
    return OpsVerdict(status, tuple(dict.fromkeys(reasons)), counts, confirmed_states,
                      manual_ack_only=MANUAL_ACK_ONLY in reasons,
                      backlog_overdue_seconds=oldest)


# --------------------------------------------------------------------------
# 便捷入口：从一个健康事件造告警（脱敏，不含密钥/原始 prompt）
# --------------------------------------------------------------------------


def alert_from_health_event(
    *,
    environment: str,
    profile: str,
    object_ref: str,
    reason_code: str,
    severity: str,
    host: str = "",
    impact: str = "",
    new_entry_state: str = "UNKNOWN",
    handling_entry: str = "scripts/health_watch.py",
    now_ms: int,
    extra: Mapping[str, Any] | None = None,
) -> Alert:
    """把脱敏健康事件转成待投递告警；消息构造复用 :func:`build_message`。"""
    message = build_message(
        profile=profile, host=host, object_ref=object_ref, reason_code=reason_code,
        severity=severity, impact=impact, new_entry_state=new_entry_state,
        handling_entry=handling_entry, occurred_at_ms=int(now_ms),
        observed_at_ms=int(now_ms), extra=extra)
    return Alert(
        incident_key=stable_incident_key(profile=profile, object_ref=object_ref,
                                         reason_code=reason_code),
        environment=environment, severity=str(severity).upper(), reason_code=reason_code,
        object_ref=object_ref, profile=profile, host=host,
        occurrence_at_ms=int(now_ms), message=message)


def new_alert_id(incident_key: str, environment: str, at_ms: int) -> str:
    """确定性告警 id（同一分钟同一 incident 幂等）。"""
    return "alert-" + hashlib.sha256(
        f"{environment}|{incident_key}|{int(at_ms) // 60000}".encode("utf-8")
    ).hexdigest()[:24]


