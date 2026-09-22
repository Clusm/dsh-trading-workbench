"""U13 备份恢复：DB + JSON **同 epoch** 恢复包、可演练、恢复后先对账再开仓。

任务卡必达要点 4 与 6 在这里落地：

- **同 epoch 恢复包**：数据库快照与全部关键 JSON 状态必须在**同一个快照窗口**
  内取到，且清单里逐文件记录 ``snapshot_at_ms``；窗口跨度超过容差即
  ``EPOCH_MISMATCH``（拒绝）。这样"数据库里的持仓"与"JSON 里的资金/通道/风控
  水位"不会来自两个不同的时刻。
- **可演练**：:func:`drill_epoch_bundle` 把恢复包还原到一个 **全新的隔离目录**
  并复核（含 ``PRAGMA quick_check``），产出回执；``dry_run`` 不落盘。
- **同盘备份 ≠ 灾难恢复（R21）**：同卷演练会显式标注
  ``SAME_VOLUME_NOT_DISASTER_RECOVERY``；``require_independent_volume=True`` 时
  直接拒绝同卷目标。
- **恢复后先对账再开仓（F14）**：还原完成后门禁处于 ``RECONCILE_REQUIRED``，
  必须提交**结构化对账证据**才允许新开仓——"恢复完就直接开仓"在代码层面
  不可达。门禁是安全控制，因此**fail-closed**：``recovery_gate.json`` 损坏
  或不可解析 → ``GATE_CORRUPT`` 阻断；门禁被删除（但安装戳仍在）→
  ``GATE_MISSING`` 阻断；只有"文件与安装戳都不存在"（确实从未 restore）才
  回到 ``GATE_NOT_INSTALLED``。
- **还原布局与门禁落点由 profile 决定（P1-2）**：恢复包携带 ``epoch.profile``，
  还原时状态 JSON **与** ``ops/recovery_gate.json`` 一并落在
  ``RuntimePaths.state_root`` 之下（live → ``<target>/data/live``）。演练路径
  （:func:`drill_epoch_bundle`）与生产恢复脚本共用 :func:`restored_state_root`
  / :func:`restored_gate_path`，且**两条路径都真的装上并落盘门禁**（P1-1）——
  只写一句 ``entry_gate`` 字符串声明不算装门禁。
- **门禁证据读取时复验（P1-3）**：``before_entry`` 不只看 ``state`` +
  ``reconciled_at_ms``。证据必须携带与 :func:`facts_digest` 自洽的对账事实块，
  且**当前账本**（``order_outbox``）里没有未收口的 SUBMITTING/UNKNOWN ENTRY 单；
  手写一份 ``{"state":"CLEARED","evidence":{}}`` 走不通。

F05 相关：写盘采用"暂存目录 + 原子发布"，中断的恢复包**不会被发布**；目标已
存在时**拒绝覆盖**（坏包不得覆盖好包）；状态 JSON 损坏时 ``STATE_CORRUPT``
fail-closed。

- **停写证明 fail-closed（U13-FU5）**：``check_writer_stopped`` 严格区分四种
  情形（见其 docstring）。**"锁文件读不出"绝不等于"写者已停"**：锁被截断
  （含 ``O_EXCL`` 创建与 ``os.write`` 之间的真实竞态窗口）、坏 JSON、非 UTF-8、
  无可用 pid 时一律抛 :class:`RecoveryLockUnreadableError`（``code`` =
  ``RECOVERY_LOCK_UNREADABLE``）**拒绝建包**，绝不让一份"写者仍活着"的状态根
  被打上 ``writer_stopped: True`` 并被 :func:`verify_epoch_bundle` 接受。
  "锁文件不存在"与"遗留锁（持有者确认已死）"两条合法运维路径**保持不变**。

只在本机文件系统上工作：不联网、不读 ``.env``/密钥、不启动服务、不下单。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.instance_lock import (
    InstanceLock,
    LockUnreadableError,
    read_lock_holder,
)
from core.runtime_paths import REQUIRED_STATE_FILES, RuntimePaths

__all__ = [
    "BUNDLE_SCHEMA",
    "EPOCH_MISMATCH",
    "EVIDENCE_ACCOUNT_MISMATCH",
    "EVIDENCE_MISMATCH",
    "EVIDENCE_MISSING",
    "EVIDENCE_STALE",
    "EVIDENCE_TYPE_INVALID",
    "EVIDENCE_UNVERIFIED",
    "GATE_CLEARED",
    "GATE_CORRUPT",
    "GATE_MISSING",
    "GATE_NOT_INSTALLED",
    "GATE_STATE_CLEARED",
    "GATE_STATE_CORRUPT",
    "GATE_STATE_MISSING",
    "GATE_STATE_NOT_INSTALLED",
    "GATE_STATE_RECONCILE_REQUIRED",
    "OPTIONAL_STATE_FILES",
    "RECONCILE_REQUIRED_BEFORE_ENTRY",
    "RECOVERY_INCOMPLETE",
    "RECOVERY_LOCK_UNREADABLE",
    "RECOVERY_WRITER_RUNNING",
    "SAME_VOLUME_NOT_DISASTER_RECOVERY",
    "STATE_CORRUPT",
    "SUPPORTED_BUNDLE_SCHEMAS",
    "RecoveryEpoch",
    "RecoveryGate",
    "RecoveryGateDecision",
    "RecoveryLockUnreadableError",
    "check_writer_stopped",
    "create_epoch_bundle",
    "drill_epoch_bundle",
    "facts_digest",
    "gate_install_marker",
    "gate_state_root",
    "ledger_reconcile_facts",
    "recovery_plan",
    "restored_gate_path",
    "restored_state_root",
    "state_json_health",
    "verify_epoch_bundle",
]

BUNDLE_SCHEMA = 3
#: 可读取的恢复包 schema：本模块 3、生产脚本（create_recovery_bundle）1/2。
#: 兼容读是为了让"演练的那一套"与"真恢复的那一套"能互相验证（P2-4）。
SUPPORTED_BUNDLE_SCHEMAS = (1, 2, 3)

#: 关键状态（缺一即 ``RECOVERY_INCOMPLETE``，与 RuntimePaths 同源）。
OPTIONAL_STATE_FILES = (
    "futures_source_ledger.json",
    "market_universe_state.json",
    "public_rate_limit_state.json",
)

EPOCH_MISMATCH = "EPOCH_MISMATCH"
RECOVERY_INCOMPLETE = "RECOVERY_INCOMPLETE"
RECOVERY_WRITER_RUNNING = "RECOVERY_WRITER_RUNNING"
#: 单实例锁文件**存在但读不出持有者**（空/截断/坏 JSON/非对象/非 UTF-8/pid<=0）
#: → 无法确认写者是否已停 ⇒ fail-closed，**拒绝创建恢复包**（U13-FU5）。
#: 与 ``RECOVERY_WRITER_RUNNING`` 必须可区分：那条是"确认写者还活着"，
#: 这条是"不知道写者在不在"——把后者当成"写者已停"正是本缺陷的根因。
RECOVERY_LOCK_UNREADABLE = "RECOVERY_LOCK_UNREADABLE"
STATE_CORRUPT = "STATE_CORRUPT"
SAME_VOLUME_NOT_DISASTER_RECOVERY = "SAME_VOLUME_NOT_DISASTER_RECOVERY"
RECONCILE_REQUIRED_BEFORE_ENTRY = "RECONCILE_REQUIRED_BEFORE_ENTRY"
GATE_CLEARED = "GATE_CLEARED"
#: 未安装门禁（**从未恢复过**）：新开仓不受恢复门禁约束。
GATE_NOT_INSTALLED = "GATE_NOT_INSTALLED"
#: 门禁状态取值（持久化在 recovery_gate.json 的 ``state``）。
GATE_STATE_NOT_INSTALLED = GATE_NOT_INSTALLED
GATE_STATE_RECONCILE_REQUIRED = "RECONCILE_REQUIRED"
GATE_STATE_CLEARED = "CLEARED"
#: 门禁文件**存在但不可解析**（磁盘满/断电/篡改）→ fail-closed 阻断。
GATE_CORRUPT = "GATE_CORRUPT"
GATE_STATE_CORRUPT = "CORRUPT"
#: 门禁**曾经安装过**（有安装戳）但文件被删除 → fail-closed 阻断。
GATE_MISSING = "GATE_MISSING"
GATE_STATE_MISSING = "MISSING"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_TYPE_INVALID = "EVIDENCE_TYPE_INVALID"
EVIDENCE_STALE = "EVIDENCE_STALE"
#: 证据/调用方身份与门禁绑定的账户/运行代不符 → 阻断（**不**用一份对账洗白另一个账户）。
EVIDENCE_ACCOUNT_MISMATCH = "EVIDENCE_ACCOUNT_MISMATCH"
#: ``CLEARED`` 但证据不携带**可核事实**（缺失/类型错）→ 阻断。手写一份
#: ``{"state":"CLEARED","evidence":{}}`` 因此不再放行（P1-3）。
EVIDENCE_UNVERIFIED = "EVIDENCE_UNVERIFIED"
#: 证据声明的事实与摘要不一致，或与**账本真实事实**不一致 → 阻断（P1-3）。
EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"

#: 门禁证据必须绑定的"真实对账事实"字段（P1-3，内容可核）。
FACTS_KEYS = ("unknown_orders", "submitting_orders", "unresolved_entry_orders")
#: ``order_outbox`` 里"尚未收口"的状态（与 exchange.order_outbox._ACTIVE 同口径；
#: 这里只做只读计数，不导入该模块以免把交易所依赖带进 ops 层）。
UNRESOLVED_ORDER_STATUSES = (
    "SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED",
)

#: 已知的门禁状态；出现未知取值一律按 ``CORRUPT`` 处理（不猜、不放行）。
KNOWN_GATE_STATES = frozenset({
    GATE_STATE_NOT_INSTALLED, GATE_STATE_RECONCILE_REQUIRED, GATE_STATE_CLEARED,
    GATE_STATE_CORRUPT, GATE_STATE_MISSING,
})


def gate_install_marker(path: Path) -> Path:
    """门禁**安装戳**路径：证明"这个状态根恢复过、门禁装过"。

    P1-2 修复的关键：单纯"文件不存在"无法区分"从未恢复过"与"门禁被删除"。
    安装戳由 :meth:`RecoveryGate.require_reconcile`/:meth:`RecoveryGate.save`
    写入，因此门禁被删除时安装戳仍在 → 判定为 :data:`GATE_MISSING` 并 **阻断**；
    只有"文件与安装戳都不存在"（确实从未 restore）才回到 ``GATE_NOT_INSTALLED``。
    """
    resolved = Path(path)
    return resolved.with_name(f"{resolved.name}.installed")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def restored_state_root(target_root: Path, profile: str | None) -> Path:
    """恢复后的**状态根**（profile 感知，唯一口径，P1-2）。

    与 :attr:`core.runtime_paths.RuntimePaths.state_root` 同源：live 分区到
    ``<target>/data/live``，其余（demo/未声明）落在 ``<target>/data``。
    恢复还原的"项目根"是 ``target_root``，旧布局的状态基座即 ``<target>/data``。

    演练路径（:func:`drill_epoch_bundle`）与生产恢复脚本
    （``scripts/restore_recovery_bundle.restore_to_new_directory``）**必须**
    共用本函数：两边各写一套布局正是 P1-1/P1-2 缺陷的根源。
    """
    base = Path(target_root) / "data"
    return base / "live" if _text(profile).lower() == "live" else base


def restored_gate_path(target_root: Path, profile: str | None) -> Path:
    """恢复后门禁的落点：``<状态根>/ops/recovery_gate.json``（P1-2）。"""
    return restored_state_root(target_root, profile) / "ops" / "recovery_gate.json"


def gate_state_root(gate_path: Path) -> Path:
    """从门禁文件路径推出它所属的**状态根**（用于读取账本真实事实，P1-3）。"""
    parent = Path(gate_path).parent
    return parent.parent if parent.name.lower() == "ops" else parent


def _canonical_facts(facts: Mapping[str, Any] | None) -> dict[str, int]:
    """把对账事实归一成可摘要的整数三元组；非整数一律记 ``-1``（不可信）。"""
    source = facts if isinstance(facts, Mapping) else {}
    result: dict[str, int] = {}
    for key in FACTS_KEYS:
        value = source.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            try:
                value = int(value)  # 数字字符串可接受
            except (TypeError, ValueError):
                value = -1
        result[key] = int(value)
    return result


def facts_digest(facts: Mapping[str, Any] | None) -> str:
    """对账事实的内容摘要（证据与真实结果绑定的可核指纹，P1-3）。"""
    material = json.dumps(_canonical_facts(facts), sort_keys=True,
                          separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def ledger_reconcile_facts(db_file: Path) -> dict[str, Any] | None:
    """从**账本**读真实对账事实；不可用即 ``None``（不猜、不伪造，P1-3）。

    ``None`` 的含义只是"读不到"，判定方必须按 fail-closed 处理，绝不能把
    "读不到"当成"账本干净"。

    读取方式按介质状态二选一，**都不新增文件**：

    - ``-shm`` 边车在（服务正在写这个库）→ 普通 ``mode=ro``（WAL 感知，能看见
      已提交但未 checkpoint 的最新事实；边车本就存在，不会多出文件）；
    - 干净的停写快照（恢复包还原出来的库就是这种）→ ``immutable=1``，绝不在
      只读介质旁生成 WAL/SHM。
    """
    path = Path(db_file)
    if not path.is_file():
        return None
    wal_sidecar = path.with_name(f"{path.name}-shm")
    try:
        if wal_sidecar.exists():
            connection = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro", uri=True)
        else:
            connection = _open_immutable_readonly(path)
    except (OSError, sqlite3.Error):
        return None
    try:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='order_outbox'").fetchone()
        if not table:
            # 该账本根本没有 outbox（合成夹具/旧库）：**不适用**，不是"干净"。
            return {"unknown_orders": 0, "submitting_orders": 0,
                    "unresolved_entry_orders": 0, "source": "not_applicable"}
        placeholders = ",".join("?" for _ in UNRESOLVED_ORDER_STATUSES)
        row = connection.execute(
            "SELECT "
            " SUM(CASE WHEN status='UNKNOWN' THEN 1 ELSE 0 END) AS unknown_orders, "
            " SUM(CASE WHEN status='SUBMITTING' THEN 1 ELSE 0 END) AS submitting_orders, "
            f" SUM(CASE WHEN status IN ({placeholders}) "
            "          AND UPPER(COALESCE(purpose,''))='ENTRY' THEN 1 ELSE 0 END) "
            "   AS unresolved_entry_orders "
            "FROM order_outbox",
            UNRESOLVED_ORDER_STATUSES).fetchone()
    except sqlite3.Error:
        # outbox 在、但读不出来（损坏/被锁）→ 不可证明：返回 None，调用方阻断。
        return None
    finally:
        connection.close()
    return {
        "unknown_orders": int(row[0] or 0),
        "submitting_orders": int(row[1] or 0),
        "unresolved_entry_orders": int(row[2] or 0),
        "source": "ledger",
    }


def _facts_block_unresolved(facts: Mapping[str, Any] | None) -> bool:
    """事实里还能看到未收口订单（含"事实不可信"）→ 必须阻断。"""
    canonical = _canonical_facts(facts)
    return any(canonical.get(key, -1) != 0 for key in FACTS_KEYS)


def _facts_unreadable(facts: Mapping[str, Any] | None) -> bool:
    """事实**读不出/不适用** → 不能当"干净"（U13-FU2/N1）。

    ``None`` = 账本读不出（文件缺失 / 打不开 / SQL 失败）；
    ``source == "not_applicable"`` = 这个库根本没有 ``order_outbox`` 表，
    因此"库是干净的"这件事**无法证明**（见 :func:`ledger_reconcile_facts`
    自己写的"不适用，不是干净"）。

    两者都必须按"不可证明"处理：安全控制宁可多阻断，绝不把"读不到"当成"已收口"。
    """
    if facts is None:
        return True
    return str(facts.get("source") or "") == "not_applicable"

#: 同 epoch 容差：DB 快照与全部 JSON 的 ``snapshot_at_ms`` 必须落在同一窗口。
DEFAULT_EPOCH_TOLERANCE_MS = 5000
#: 对账证据可接受的最大年龄（默认 15 分钟）：过期证据必须重新对账。
DEFAULT_EVIDENCE_TTL_MS = 15 * 60 * 1000


class RecoveryError(RuntimeError):
    """恢复包不可安全创建/校验/演练（fail-closed）。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        # 原因码必须出现在异常文本里：调用方/回归断言按原因码比对，
        # 只留自然语言消息会让"确实被拒绝"与"拒绝原因正确"两件事分不开。
        super().__init__(f"{code}: {message}" if message else code)


class RecoveryLockUnreadableError(RecoveryError):
    """单实例锁**存在但读不出**持有者 ⇒ 拒绝创建恢复包（U13-FU5）。

    与 ``core.instance_lock.LockUnreadableError``（U13-FU4）同口径、同根因，只是
    发生在**恢复包创建**这条消费链上。单独成类（而不只是换个 ``code`` 字符串）是
    为了和 FU4 一致地"可区分"：调用方既能按 ``code`` 也能按类型把"锁读不出"与
    "写者确认还在跑"（:data:`RECOVERY_WRITER_RUNNING`）分开处理。

    继承 ``RecoveryError``，因此既有 ``except RecoveryError`` 消费者语义不变——
    它们本来就必须 fail-closed，这里只是**把 fail-open 翻成 fail-closed**。
    ``__cause__`` 指向底层 ``LockUnreadableError``（含其可行动文案）。
    """

    def __init__(self, message: str) -> None:
        super().__init__(RECOVERY_LOCK_UNREADABLE, message)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_bytes(data)
    os.replace(temp, path)


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_bytes(
        path, (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
               + "\n").encode("utf-8"))


def _open_immutable_readonly(path: Path) -> sqlite3.Connection:
    """只读不可变快照连接：绝不在介质旁生成 WAL/SHM（与既有 restore 脚本同口径）。"""
    uri = f"{Path(path).resolve().as_uri()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


def _lock_unreadable_message(lock_file: Path, reason: str) -> str:
    """锁读不出时的**可行动**报错正文（U13-FU5）。

    这条消息必须能独立完成三件事：说清"为什么不能自动放行"、告诉操作者**先确认
    服务是否在运行**、再给出确认未运行后的手工删锁命令（Windows/POSIX 都给）。
    """
    return (
        f"单实例锁文件存在但读不出持有者 PID，无法确认交易服务是否已停机："
        f"{lock_file}\n"
        f"原因：{reason}\n"
        "为什么不能自动放行：读不出锁内容 ≠ 写者已停。此时创建恢复包会把一个"
        "**可能正在被写入**的状态根伪称为'同一时刻静止'的一致快照，"
        "灾难恢复时按它还原等于用撕裂状态覆盖线上账本。\n"
        "操作者请这样做：\n"
        "  1) 先确认交易服务是否正在运行（进程列表 / 控制台 / 健康巡检的核心环）；\n"
        "  2) 确认**未**运行后，手工删除该锁文件再重试建包：\n"
        f'     Windows: del "{lock_file}"\n'
        f'     POSIX:   rm "{lock_file}"\n'
        "  3) 若服务确实在运行，不要删除该文件——先正常停机再创建恢复包。"
    )


def check_writer_stopped(paths: RuntimePaths) -> None:
    """确认交易服务**没有**在写这个状态根（单实例锁语义）。

    复用 ``core.instance_lock.read_lock_holder``（U13-FU4 落地的唯一口径：读不出
    = fail-closed，不在这里重写 Windows 句柄逻辑，也不重写"什么叫可解析"）。

    **四种情形严格区分**（U13-FU5，修前第 3 种被当成第 1/4 种）：

    ====================================  ======================  ==========
    锁文件事实                             判定                    处置
    ====================================  ======================  ==========
    不存在（``FileNotFoundError``）        写者已停                允许
    可解析 + pid 有效且**存活**            写者在写                拒绝 ``RECOVERY_WRITER_RUNNING``
    可解析 + pid 有效且**已死**            遗留锁，写者已停        允许
    **存在但不可解析 / 无可用 pid**        **无法确认**            **拒绝** ``RECOVERY_LOCK_UNREADABLE``
    ====================================  ======================  ==========

    "存在但不可解析" 覆盖：空文件（含 ``O_EXCL`` 创建与 ``os.write`` 之间的真实
    竞态窗口）/ 截断 / 坏 JSON / 顶层非对象 / 非 UTF-8 / 缺 pid / pid<=0。
    """
    lock_file = paths.lock_file
    try:
        holder = read_lock_holder(lock_file)
    except LockUnreadableError as exc:
        # 修前这里是 ``except (...): return`` —— "读不出"被当成了"写者已停"。
        raise RecoveryLockUnreadableError(
            _lock_unreadable_message(lock_file, exc.reason)) from exc
    if holder is None:
        # 锁文件不存在 ⇒ 没有写者持有它，合法路径（不要修坏）。
        return
    pid, _data = holder
    if InstanceLock._pid_alive(pid):
        raise RecoveryError(
            RECOVERY_WRITER_RUNNING,
            f"交易服务仍在运行（PID={pid}，锁文件={lock_file.name}）；"
            "请先正常停机，再创建同 epoch 恢复包",
        )
    # 可解析 **且** pid 确认不存活 ⇒ 遗留锁（进程被 kill -9 / 断电留下），
    # 写者确实已停，合法运维路径（不要修坏）。


def state_json_health(data_dir: Path, *, names: tuple[str, ...] | None = None) -> dict[str, Any]:
    """只读巡检关键状态 JSON（F05：状态 JSON 损坏 → 停新仓，不静默 normal）。"""
    checked = names or (REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES)
    broken: list[str] = []
    missing: list[str] = []
    for name in checked:
        path = data_dir / name
        if not path.is_file():
            if name in REQUIRED_STATE_FILES:
                missing.append(name)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            broken.append(name)
            continue
        if not isinstance(payload, dict):
            broken.append(name)
    problems = [STATE_CORRUPT] if broken else []
    if missing:
        problems.append(RECOVERY_INCOMPLETE)
    return {
        "checked": list(checked),
        "corrupt": broken,
        "missing_required": missing,
        "problems": problems,
        "new_entry_allowed": not problems,
    }


@dataclass(frozen=True)
class RecoveryEpoch:
    """一次恢复包的 epoch 标识：它是"这一包状态属于同一时刻"的主张。"""

    epoch_id: str
    snapshot_started_ms: int
    snapshot_finished_ms: int
    environment: str
    profile: str
    account_ref: str
    runtime_instance_id: str
    generation_id: str
    db_schema_version: int | None = None
    tolerance_ms: int = DEFAULT_EPOCH_TOLERANCE_MS

    def as_dict(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "snapshot_started_ms": self.snapshot_started_ms,
            "snapshot_finished_ms": self.snapshot_finished_ms,
            "environment": self.environment,
            "profile": self.profile,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "db_schema_version": self.db_schema_version,
            "tolerance_ms": self.tolerance_ms,
        }

    def accepts(self, snapshot_at_ms: int) -> bool:
        return (self.snapshot_started_ms - self.tolerance_ms
                <= snapshot_at_ms
                <= self.snapshot_finished_ms + self.tolerance_ms)


def _copy_state_json(source: Path, destination: Path, epoch: RecoveryEpoch) -> dict[str, Any]:
    """校验并复制一个状态 JSON；返回清单条目（含 ``snapshot_at_ms``）。"""
    started = _now_ms()
    try:
        raw = source.read_bytes()
        parsed = json.loads(raw.decode("utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RecoveryError(STATE_CORRUPT, f"状态文件不可解析: {source.name}") from exc
    if not isinstance(parsed, dict):
        raise RecoveryError(STATE_CORRUPT, f"状态文件不是 JSON 对象: {source.name}")
    destination.write_bytes(raw)
    if _sha256_file(destination) != _sha256_bytes(raw):
        raise RecoveryError(RECOVERY_INCOMPLETE, f"状态文件复制校验失败: {source.name}")
    snapshot_at = _now_ms()
    if not epoch.accepts(started) or not epoch.accepts(snapshot_at):
        raise RecoveryError(EPOCH_MISMATCH, f"{source.name} 不在同一快照窗口内")
    return {
        "path": f"state/{source.name}",
        "size": len(raw),
        "sha256": _sha256_bytes(raw),
        "snapshot_at_ms": snapshot_at,
    }


def create_epoch_bundle(
    paths: RuntimePaths,
    output_dir: Path,
    *,
    environment: str,
    account_ref: str = "unknown",
    runtime_instance_id: str = "unknown",
    generation_id: str = "unknown",
    now_ms: int | None = None,
) -> Path:
    """创建同 epoch 恢复包（DB + JSON），中断不发布、已存在不覆盖。

    调用方必须保证交易服务已停机；本函数会在建包前复核单实例锁
    （:func:`check_writer_stopped`：锁**读不出**即 fail-closed 拒绝，
    U13-FU5），并在复制前巡检状态 JSON（:func:`state_json_health`）。

    因此第 :data:`RECOVERY_LOCK_UNREADABLE` 条拒绝路径成立时，本函数**不会**
    写出 ``writer_stopped: True`` —— 这个声明只有在"锁不存在"或"锁可解析且持有者
    确认已死"两种情况后才可能落盘，不会是"读不出"被伪装的静止声明。
    """
    data_dir = paths.data_dir
    db_source = paths.db_file
    if not db_source.is_file():
        raise RecoveryError(RECOVERY_INCOMPLETE, f"数据库不存在: {db_source}")
    check_writer_stopped(paths)
    health = state_json_health(data_dir)
    if health["corrupt"]:
        raise RecoveryError(STATE_CORRUPT,
                            "状态 JSON 损坏，拒绝用它建包: " + ", ".join(health["corrupt"]))
    missing = health["missing_required"]
    if missing:
        raise RecoveryError(RECOVERY_INCOMPLETE, "缺少关键状态文件: " + ", ".join(missing))

    target_root = Path(output_dir).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    epoch = RecoveryEpoch(
        epoch_id=uuid.uuid4().hex,
        snapshot_started_ms=now_ms if now_ms is not None else _now_ms(),
        snapshot_finished_ms=0,
        environment=str(environment),
        profile=str(paths.profile or ""),
        account_ref=str(account_ref),
        runtime_instance_id=str(runtime_instance_id),
        generation_id=str(generation_id),
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    final = target_root / f"recovery_{stamp}"
    if final.exists():
        raise RecoveryError("BACKUP_TARGET_EXISTS", f"恢复包目标已存在，拒绝覆盖: {final}")
    stage = target_root / f".{final.name}.tmp-{os.getpid()}"
    if stage.exists():
        shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=False, exist_ok=False)
    (stage / "state").mkdir()
    try:
        db_target = stage / "agent_trader.sqlite"
        source_conn = sqlite3.connect(str(db_source))
        destination_conn = sqlite3.connect(str(db_target))
        try:
            source_conn.backup(destination_conn)
            check = destination_conn.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RecoveryError(RECOVERY_INCOMPLETE, "SQLite 恢复副本完整性校验失败")
            row = destination_conn.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()
            schema_version = int(row[0]) if row and row[0] is not None else None
        finally:
            destination_conn.close()
            source_conn.close()
        db_at = _now_ms()
        files: dict[str, Any] = {
            "database": {
                "path": db_target.name,
                "size": db_target.stat().st_size,
                "sha256": _sha256_file(db_target),
                "snapshot_at_ms": db_at,
                "schema_version": schema_version,
            }
        }
        # DB 快照与 JSON 复制必须落在同一个容差窗口内才叫"同 epoch"。
        epoch = RecoveryEpoch(**{**epoch.as_dict(),
                                 "snapshot_finished_ms": db_at,
                                 "db_schema_version": schema_version})
        for name in REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES:
            source = data_dir / name
            if source.is_file():
                files[name] = _copy_state_json(source, stage / "state" / name, epoch)
        if not epoch.accepts(db_at):
            raise RecoveryError(EPOCH_MISMATCH, "数据库快照不在同一快照窗口内")
        manifest = {
            "bundle_schema": BUNDLE_SCHEMA,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "writer_stopped": True,
            "epoch": epoch.as_dict(),
            "files": files,
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n").encode("utf-8")
        (stage / "manifest.json").write_bytes(manifest_bytes)
        (stage / "manifest.sha256").write_text(
            f"{_sha256_bytes(manifest_bytes)}  manifest.json\n",
            encoding="ascii", newline="\n")
        os.replace(stage, final)
        return final
    except Exception:
        if stage.is_dir() and stage.parent == target_root:
            shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_epoch_bundle(
    bundle_dir: Path, *, expected_environment: str | None = None,
) -> dict[str, Any]:
    """校验同 epoch 恢复包：哈希、白名单、schema、SQLite、epoch 窗口。

    **兼容读（P2-4）**：``scripts/create_recovery_bundle.py``（生产灾难恢复那一套）
    产出 ``bundle_schema: 2``，本模块产出 ``3``。历史上的"演练用一套、真恢复用
    另一套"使两者无法互相验证；这里接受 ``1/2/3`` 三种 schema，并对老包按
    "有 epoch 才校验 epoch"处理（缺 epoch 的老包在结果里显式标 ``legacy_bundle``，
    **不**假装校验过）。
    """
    bundle = Path(bundle_dir).resolve()
    if not bundle.is_dir() or bundle.is_symlink():
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包目录不存在或是符号链接")
    manifest_path = bundle / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    schema = int(manifest.get("bundle_schema") or 0)
    if schema not in SUPPORTED_BUNDLE_SCHEMAS:
        raise RecoveryError("BUNDLE_SCHEMA_UNSUPPORTED",
                            f"不支持的恢复包版本: {schema}")
    legacy_bundle = schema < BUNDLE_SCHEMA
    if schema >= 2:
        expected_sha = (bundle / "manifest.sha256").read_text(encoding="ascii").split()[0]
        if expected_sha != _sha256_bytes(manifest_bytes):
            raise RecoveryError(RECOVERY_INCOMPLETE, "manifest 哈希不匹配")
    # 停写一致性：本模块写 ``writer_stopped``，生产脚本历史上写
    # ``service_was_stopped``；两者含义相同，任一为 True 即成立。
    if not (manifest.get("writer_stopped") is True
            or manifest.get("service_was_stopped") is True):
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包未声明停写一致性")

    epoch: RecoveryEpoch | None = None
    if isinstance(manifest.get("epoch"), dict):
        epoch = RecoveryEpoch(**_epoch_kwargs(manifest["epoch"]))
    elif not legacy_bundle:
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包缺少 epoch（同 epoch 声明缺失）")
    files = manifest.get("files")
    if not isinstance(files, dict) or "database" not in files:
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包缺少数据库清单")
    missing = sorted(set(REQUIRED_STATE_FILES) - set(files))
    if missing:
        raise RecoveryError(RECOVERY_INCOMPLETE,
                            "恢复包缺少关键成员: " + ", ".join(missing))

    allowed = {"manifest.json", "manifest.sha256"}
    verified: dict[str, Path] = {}
    for key, metadata in files.items():
        if not isinstance(metadata, dict):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件清单格式错误: {key}")
        relative = str(metadata.get("path") or "")
        member = (bundle / relative).resolve()
        if bundle not in member.parents and member != bundle:
            raise RecoveryError(RECOVERY_INCOMPLETE, f"恢复包路径越界: {relative}")
        if not member.is_file():
            raise RecoveryError(RECOVERY_INCOMPLETE, f"恢复包成员缺失: {relative}")
        if member.stat().st_size != int(metadata.get("size") or -1):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件大小不匹配: {relative}")
        if _sha256_file(member) != str(metadata.get("sha256") or ""):
            raise RecoveryError(RECOVERY_INCOMPLETE, f"文件哈希不匹配: {relative}")
        if epoch is not None:
            snapshot_at = metadata.get("snapshot_at_ms")
            if not isinstance(snapshot_at, int) or not epoch.accepts(snapshot_at):
                raise RecoveryError(EPOCH_MISMATCH, f"{relative} 不在同一 epoch 窗口内")
        allowed.add(relative.replace("\\", "/"))
        verified[str(key)] = member
    actual = {path.relative_to(bundle).as_posix()
              for path in bundle.rglob("*") if path.is_file()}
    extras = sorted(actual - allowed)
    if extras:
        raise RecoveryError(RECOVERY_INCOMPLETE,
                            "恢复包包含白名单外文件: " + ", ".join(extras))

    connection = _open_immutable_readonly(verified["database"])
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RecoveryError(RECOVERY_INCOMPLETE, "恢复数据库 quick_check 失败")
        row = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
        actual_schema = int(row[0]) if row and row[0] is not None else None
    finally:
        connection.close()
    expected_schema = files["database"].get("schema_version")
    if expected_schema is not None and actual_schema != int(expected_schema):
        raise RecoveryError(RECOVERY_INCOMPLETE, "数据库迁移版本与清单不一致")
    environment = (epoch.environment if epoch is not None
                   else str(manifest.get("environment") or ""))
    if expected_environment and environment != expected_environment:
        raise RecoveryError("ENVIRONMENT_MISMATCH", f"恢复包环境不匹配: {environment}")
    return {
        "bundle": str(bundle),
        "schema": schema,
        "legacy_bundle": legacy_bundle,
        "epoch": epoch.as_dict() if epoch is not None else None,
        "environment": environment,
        "profile": epoch.profile if epoch is not None else "",
        "account_ref": epoch.account_ref if epoch is not None else "",
        "database_schema": actual_schema,
        "files": len(verified),
        "span_ms": ((epoch.snapshot_finished_ms - epoch.snapshot_started_ms)
                    if epoch is not None else None),
    }


def _epoch_kwargs(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RecoveryError(RECOVERY_INCOMPLETE, "恢复包缺少 epoch")
    return {
        "epoch_id": str(payload.get("epoch_id") or ""),
        "snapshot_started_ms": int(payload.get("snapshot_started_ms") or 0),
        "snapshot_finished_ms": int(payload.get("snapshot_finished_ms") or 0),
        "environment": str(payload.get("environment") or ""),
        "profile": str(payload.get("profile") or ""),
        "account_ref": str(payload.get("account_ref") or ""),
        "runtime_instance_id": str(payload.get("runtime_instance_id") or ""),
        "generation_id": str(payload.get("generation_id") or ""),
        "db_schema_version": payload.get("db_schema_version"),
        "tolerance_ms": int(payload.get("tolerance_ms") or DEFAULT_EPOCH_TOLERANCE_MS),
    }


def drill_epoch_bundle(
    bundle_dir: Path, target_dir: Path, *, execute: bool = False,
    require_independent_volume: bool = False,
    expected_environment: str | None = None,
) -> dict[str, Any]:
    """恢复演练：校验 → （可选）还原到全新隔离目录 → 复核 → 回执。

    绝不覆盖运行目录或既有文件；同卷演练显式标注不是灾难恢复（R21）。
    """
    started = time.perf_counter()
    verified = verify_epoch_bundle(bundle_dir, expected_environment=expected_environment)
    # 恢复包携带的 profile 决定"状态 JSON 与门禁落在哪个状态根"（P1-2）。
    epoch_profile = str(verified.get("profile") or "")
    bundle = Path(bundle_dir).resolve()
    target = Path(target_dir).resolve()
    same_volume = bundle.anchor.casefold() == target.anchor.casefold()
    if require_independent_volume and same_volume:
        raise RecoveryError(SAME_VOLUME_NOT_DISASTER_RECOVERY,
                            "恢复目标不在独立卷；同盘恢复演练不能证明灾难恢复能力")
    if target.exists():
        raise RecoveryError("TARGET_EXISTS", f"恢复目标必须是不存在的新目录: {target}")
    if bundle == target or bundle in target.parents or target in bundle.parents:
        raise RecoveryError("TARGET_NESTED", "恢复目标不能与恢复包互相嵌套")
    notices = [SAME_VOLUME_NOT_DISASTER_RECOVERY] if same_volume else []
    if not execute:
        return {**verified, "status": "dry_run", "target": str(target),
                "same_volume": same_volume, "notices": notices,
                "reconcile_required": True}
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.restore-{os.getpid()}"
    if stage.exists():
        raise RecoveryError("TARGET_EXISTS", f"恢复暂存目录已存在: {stage}")
    stage.mkdir(parents=False)
    try:
        (stage / "config").mkdir()
        # P1-2：状态 JSON/数据库/门禁**一律**落在 profile 感知的状态根之下，
        # 与 RuntimePaths.state_root 同口径（live → <target>/data/live）。
        state_root = restored_state_root(stage, epoch_profile)
        state_root.mkdir(parents=True)
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        for key, metadata in manifest["files"].items():
            source = (bundle / str(metadata["path"])).resolve()
            destination = (state_root / "agent_trader.sqlite") if key == "database" \
                else (state_root / source.name)
            shutil.copy2(source, destination)
        check = _open_immutable_readonly(state_root / "agent_trader.sqlite")
        try:
            if str(check.execute("PRAGMA quick_check").fetchone()[0]).lower() != "ok":
                raise RecoveryError(RECOVERY_INCOMPLETE, "恢复后数据库 quick_check 失败")
        finally:
            check.close()
        # P1-1：还原**必须**与生产恢复路径行为一致地装上恢复门禁。只写一个
        # ``entry_gate`` 字符串声明等于没装：还原后的状态根 ``before_entry()``
        # 会返回 ``GATE_NOT_INSTALLED`` 放行，"恢复完直接开仓"可达。
        gate_file = restored_gate_path(stage, epoch_profile)
        RecoveryGate.load(gate_file).require_reconcile(
            epoch_id=str((verified.get("epoch") or {}).get("epoch_id") or ""),
            account_ref="", runtime_instance_id="")
        receipt = {
            "restored_at": datetime.now(timezone.utc).isoformat(),
            "source_manifest_sha256": _sha256_file(bundle / "manifest.json"),
            "epoch_id": (verified["epoch"] or {}).get("epoch_id"),
            "environment": verified["environment"],
            "profile": epoch_profile,
            "state_root": state_root.relative_to(stage).as_posix(),
            "database_schema": verified["database_schema"],
            "same_volume": same_volume,
            "notices": notices,
            # 恢复完成 **不等于** 可以开仓：门禁仍在 RECONCILE_REQUIRED。
            "entry_gate": RECONCILE_REQUIRED_BEFORE_ENTRY,
            "recovery_gate_file": gate_file.relative_to(stage).as_posix(),
        }
        _atomic_write_json(stage / "restore_receipt.json", receipt)
        os.replace(stage, target)
    except Exception:
        if stage.is_dir() and stage.parent == target.parent:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return {**verified, "status": "restored", "target": str(target),
            "same_volume": same_volume, "notices": notices,
            "reconcile_required": True,
            "state_root": str(restored_state_root(target, epoch_profile)),
            "recovery_gate_file": str(restored_gate_path(target, epoch_profile)),
            "duration_ms": round((time.perf_counter() - started) * 1000, 3)}


def recovery_plan(paths: RuntimePaths, *, metadata_only: bool = False) -> dict[str, Any]:
    """恢复计划（只读）：证明"同盘备份 ≠ 灾难恢复"并给出所需步骤。

    ``metadata_only=True`` 时 **不读任何状态文件内容**（只做存在性判断），供
    对真实运行目录做只读盘点——真实 ``data/`` 的内容不属于可读范围。
    """
    data_dir = paths.data_dir
    if metadata_only:
        names = REQUIRED_STATE_FILES + OPTIONAL_STATE_FILES
        present = [name for name in names if (data_dir / name).is_file()]
        missing = [name for name in REQUIRED_STATE_FILES if name not in present]
        health = {
            "checked": list(names),
            "present": present,
            "missing_required": missing,
            "problems": [RECOVERY_INCOMPLETE] if missing else [],
            "new_entry_allowed": not missing,
            "content_inspected": False,
        }
    else:
        health = state_json_health(data_dir)
        health["content_inspected"] = True
    db_present = paths.db_file.is_file()
    return {
        "state_base": str(paths.state_base),
        "state_root": str(paths.state_root),
        "profile": paths.profile,
        "database": str(paths.db_file),
        "database_present": db_present,
        "backups_dir": str(paths.backups_dir),
        "recovery_bundles_dir": str(paths.recovery_bundles_dir),
        "bundles_dir_volume": paths.recovery_bundles_dir.anchor.casefold(),
        "state_volume": paths.state_base.anchor.casefold(),
        # 同盘：恢复包与状态根在同一卷 → 卷故障会同时丢两者。
        "same_volume_as_state": (paths.recovery_bundles_dir.anchor.casefold()
                                 == paths.state_base.anchor.casefold()),
        "disaster_recovery_requires_independent_volume": True,
        "state_health": health,
        "required_state_files": list(REQUIRED_STATE_FILES),
        "optional_state_files": list(OPTIONAL_STATE_FILES),
        "steps": [
            "1. 正常停机并确认单实例锁无存活 PID（check_writer_stopped；"
            "锁文件存在但不可解析 → 拒绝建包，U13-FU5）",
            "2. state_json_health：任一关键 JSON 损坏即停新仓，不得用坏状态建包",
            "3. create_epoch_bundle：DB+JSON 同一 epoch 窗口，暂存后原子发布",
            "4. 把恢复包复制到**独立卷**（同卷演练只证明流程，不证明灾难恢复，R21）",
            "5. drill_epoch_bundle(require_independent_volume=True) 还原到全新目录并 quick_check",
            "6. 启动后门禁 = RECONCILE_REQUIRED：先全量对账并提交结构化证据",
            "7. 对账证据通过后才允许新开仓（F14：不得恢复完就直接开仓）",
        ],
    }


# --------------------------------------------------------------------------
# 恢复后先对账再开仓（F14）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryGateDecision:
    allowed: bool
    reason_codes: tuple[str, ...]
    state: str

    def as_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason_codes": list(self.reason_codes),
                "state": self.state}


@dataclass
class RecoveryGate:
    """恢复门禁：恢复完成后必须提交结构化对账证据才允许新开仓。

    **fail-closed**：这是安全控制，缺失/损坏/不可解析都不放行，且三种失败
    各有明确原因码：

    ==========================  ====================  ========================
    磁盘事实                    状态                    ``before_entry`` 结果
    ==========================  ====================  ========================
    文件+安装戳都不存在          ``NOT_INSTALLED``     allowed（从未 restore 过）
    文件存在但不可解析/未知状态  ``CORRUPT``           拒绝 ``GATE_CORRUPT``
    安装戳存在但文件不存在       ``MISSING``           拒绝 ``GATE_MISSING``
    文件 = RECONCILE_REQUIRED    ``RECONCILE_REQUIRED`` 拒绝 ``RECONCILE_REQUIRED_BEFORE_ENTRY``
    文件 = CLEARED 但证据不可核  ``CLEARED``           拒绝 ``EVIDENCE_UNVERIFIED``
    文件 = CLEARED 但账本仍有    ``CLEARED``           拒绝 ``RECONCILE_REQUIRED_BEFORE_ENTRY``
    未收口 ENTRY 单
    文件 = CLEARED 且证据新鲜    ``CLEARED``            allowed
    + 内容可核 + 账本无未收口单
    ==========================  ====================  ========================

    证据必须是显式 Mapping（与 K05 ``resolve_incident`` 同口径，fail-closed）：

    .. code-block:: text

        {"observed_at_ms": int, "positions_verified": true,
         "open_orders_verified": true, "account_ref": "...",
         "runtime_instance_id": "...",
         "facts": {"unknown_orders": 0, "submitting_orders": 0,
                   "unresolved_entry_orders": 0},
         "facts_digest": "<sha256 of canonical facts>"}

    缺字段 → ``EVIDENCE_MISSING``；类型混淆/裸布尔 → ``EVIDENCE_TYPE_INVALID``；
    时间戳过旧或来自未来 → ``EVIDENCE_STALE``；调用方身份与门禁绑定的
    账户/运行代不符 → ``EVIDENCE_ACCOUNT_MISMATCH``；**证据不携带可核事实
    或摘要对不上 → ``EVIDENCE_UNVERIFIED`` / ``EVIDENCE_MISMATCH``**。
    **不伪造**：证据不足即保持阻断。
    """

    path: Path
    state: str = GATE_NOT_INSTALLED
    epoch_id: str = ""
    reconciled_at_ms: int | None = None
    account_ref: str = ""
    runtime_instance_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    evidence_ttl_ms: int = DEFAULT_EVIDENCE_TTL_MS

    @classmethod
    def load(cls, path: Path) -> "RecoveryGate":
        """从磁盘加载门禁；**任何异常形态都落到阻断态**（P1-2）。

        - 文件缺失 + 无安装戳 → ``NOT_INSTALLED``（确实从未 restore）；
        - 文件缺失 + 有安装戳 → ``MISSING``（门禁被删除 → fail-closed）；
        - 读/解析失败、不是 JSON 对象、``state`` 未知 → ``CORRUPT``（fail-closed）。
        """
        resolved = Path(path)
        installed = gate_install_marker(resolved).is_file()
        try:
            raw = resolved.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path=resolved,
                       state=GATE_STATE_MISSING if installed else GATE_STATE_NOT_INSTALLED)
        except (OSError, UnicodeDecodeError):
            # 磁盘满/权限/编码损坏：读到半截文件也算损坏，绝不当成"未安装"。
            return cls(path=resolved, state=GATE_STATE_CORRUPT)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return cls(path=resolved, state=GATE_STATE_CORRUPT)
        if not isinstance(payload, dict):
            return cls(path=resolved, state=GATE_STATE_CORRUPT)
        state = str(payload.get("state") or "")
        if state not in KNOWN_GATE_STATES:
            return cls(path=resolved, state=GATE_STATE_CORRUPT)
        return cls(
            path=resolved,
            state=state,
            epoch_id=str(payload.get("epoch_id") or ""),
            reconciled_at_ms=payload.get("reconciled_at_ms"),
            account_ref=str(payload.get("account_ref") or ""),
            runtime_instance_id=str(payload.get("runtime_instance_id") or ""),
            evidence=payload.get("evidence") or {},
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": 1, "state": self.state, "epoch_id": self.epoch_id,
            "reconciled_at_ms": self.reconciled_at_ms,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "evidence": self.evidence,
        }

    def save(self) -> None:
        """落盘门禁**并**写下安装戳（安装戳只在门禁真被写过之后存在）。"""
        _atomic_write_json(self.path, self.as_dict())
        _atomic_write_json(
            gate_install_marker(self.path),
            {"installed_at_ms": _now_ms(), "state": self.state,
             "epoch_id": self.epoch_id, "account_ref": self.account_ref,
             "runtime_instance_id": self.runtime_instance_id},
        )

    def require_reconcile(self, *, epoch_id: str, account_ref: str = "",
                          runtime_instance_id: str = "") -> None:
        """恢复/接管后进入"必须先对账"状态（清空旧证据）。"""
        self.state = GATE_STATE_RECONCILE_REQUIRED
        self.epoch_id = epoch_id
        self.reconciled_at_ms = None
        self.account_ref = account_ref
        self.runtime_instance_id = runtime_instance_id
        self.evidence = {}
        self.save()

    def bind_runtime(self, *, account_ref: str, runtime_instance_id: str) -> bool:
        """把门禁绑定到**当前运行代**（P2-6）：只补齐空字段，绝不放宽已有绑定。

        恢复包安装门禁时还不知道运行代（restore 时服务未起），因此绑定由服务
        启动时补齐。返回是否发生了写入（便于审计/测试）。
        """
        changed = False
        if account_ref and not self.account_ref:
            self.account_ref = str(account_ref)
            changed = True
        if runtime_instance_id and not self.runtime_instance_id:
            self.runtime_instance_id = str(runtime_instance_id)
            changed = True
        if changed:
            self.save()
        return changed

    def before_entry(self, *, now_ms: int | None = None,
                     facts: Mapping[str, Any] | None = None
                     ) -> RecoveryGateDecision:
        """新开仓前的唯一判定入口（fail-closed）。

        **P1-3（读取时复验）**：``state == CLEARED`` 不再只看 ``state`` +
        ``reconciled_at_ms``。必须同时满足：

        1. 证据里携带**内容可核**的对账事实块（``facts`` + ``facts_digest``），
           且摘要自洽、事实里没有未收口订单 —— 手写一份
           ``{"state":"CLEARED","evidence":{}}`` 因此**不再放行**；
        2. **账本真实事实**（当前状态根下 ``agent_trader.sqlite`` 的
           ``order_outbox``）里没有未收口 ENTRY 单 —— 声明"已对账"但账本里
           订单还挂着时一律维持阻断。``facts`` 形参供调用方（服务运行代）注入
           已经采集到的事实，避免判定时再打开一次账本。

        账本不可读时**不**假定干净：见 :func:`ledger_reconcile_facts`。

        **U13-FU2/N1（裸形态不再 fail-open）**：``facts`` **未注入**时，若账本
        根本读不出事实（状态根里没有账本文件 / 打不开 / SQL 失败 → ``None``），
        或账本里没有 ``order_outbox`` 表（``source="not_applicable"``，**不适用
        不等于干净**），一律返回 ``EVIDENCE_UNVERIFIED`` 阻断 —— 只要证据自带
        自洽摘要就放行的旧行为与"读不出即不假定干净"的契约相悖，已删除。
        调用方（服务运行代）应通过 ``facts=`` 注入**本进程账本的真实事实**：
        注入形态同样不接受 ``source="not_applicable"``。
        """
        now = now_ms if now_ms is not None else _now_ms()
        if self.state == GATE_STATE_CORRUPT:
            return RecoveryGateDecision(False, (GATE_CORRUPT,), self.state)
        if self.state == GATE_STATE_MISSING:
            return RecoveryGateDecision(False, (GATE_MISSING,), self.state)
        if self.state == GATE_NOT_INSTALLED:
            # 从未恢复过（文件与安装戳都不存在）：门禁尚未安装，不阻断
            # （也不伪造"已对账"）。
            return RecoveryGateDecision(True, (GATE_NOT_INSTALLED,), self.state)
        if self.state != GATE_STATE_CLEARED:
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        if self.reconciled_at_ms is None:
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        if now - int(self.reconciled_at_ms) > self.evidence_ttl_ms:
            return RecoveryGateDecision(False, (EVIDENCE_STALE,), self.state)
        # (1) 证据必须内容可核。
        evidence = self.evidence if isinstance(self.evidence, Mapping) else {}
        declared = evidence.get("facts")
        digest = str(evidence.get("facts_digest") or "")
        if not isinstance(declared, Mapping) or not digest:
            return RecoveryGateDecision(False, (EVIDENCE_UNVERIFIED,), self.state)
        if digest != facts_digest(declared):
            return RecoveryGateDecision(False, (EVIDENCE_MISMATCH,), self.state)
        if _facts_block_unresolved(declared):
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        # (2) 用当前账本的**真实事实**复验（调用方注入的实时事实优先）。
        if isinstance(facts, Mapping):
            live: Mapping[str, Any] | None = facts
        else:
            live = self.live_reconcile_facts()
        # U13-FU2/N1：**读不出账本事实就不得放行**。旧实现在这里按"是不是停写
        # 快照"决定要不要复验，于是"状态根没有账本文件""账本没有 order_outbox
        # 表"两条路径都 fail-open——只要证据摘要自洽就放行（Q1/Q2 实测），与
        # :func:`ledger_reconcile_facts` 的契约相悖。现在无论注入与否，只要事实
        # 读不出/不适用就按"未 CLEARED"处理（宁可多阻断，绝不假定干净）。
        if _facts_unreadable(live):
            return RecoveryGateDecision(False, (EVIDENCE_UNVERIFIED,), self.state)
        if _facts_block_unresolved(live):
            return RecoveryGateDecision(
                False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
        return RecoveryGateDecision(True, (GATE_CLEARED,), self.state)

    def gate_state_root(self) -> Path:
        """门禁所在的状态根（``<状态根>/ops/recovery_gate.json``）。"""
        return gate_state_root(self.path)

    def ledger_file(self) -> Path:
        return self.gate_state_root() / "agent_trader.sqlite"

    def ledger_snapshot_present(self) -> bool:
        """账本是"停写快照"（文件在且没有活动写者的 ``-shm`` 边车）。

        U13-FU2/N1 之后 :meth:`before_entry` 的判定**不再**依赖本方法：裸形态
        读不出事实一律阻断，不再区分"停写快照"与"活动库"。保留它作为
        **诊断/报告**用的介质判定（活动 WAL 库 vs 停写快照），不参与放行。
        """
        db = self.ledger_file()
        if not db.is_file():
            return False
        return not db.with_name(f"{db.name}-shm").exists()

    def live_reconcile_facts(self) -> dict[str, Any] | None:
        """当前账本（门禁所在状态根）的真实对账事实；读不到 → ``None``。"""
        return ledger_reconcile_facts(self.ledger_file())

    def complete_reconcile(
        self, evidence: Any, *, now_ms: int | None = None,
        account_ref: str = "", runtime_instance_id: str = "",
        untrusted_clock: bool = False,
    ) -> RecoveryGateDecision:
        """提交对账证据；只有结构化且新鲜的证据才解除阻断。

        ``account_ref``/``runtime_instance_id`` 是**调用方（服务运行代）注入**的
        身份，不是请求体自证：门禁若已绑定某个账户/运行代，身份不符即
        ``EVIDENCE_ACCOUNT_MISMATCH``（P2-6：一份对账不得洗白另一个账户）。

        **P1-3（证据与真实对账结果绑定）**：收口前先读**账本真实事实**
        （``order_outbox`` 里未收口的 SUBMITTING/UNKNOWN ENTRY 单）。账本里还有
        未收口订单 → 直接 ``RECONCILE_REQUIRED_BEFORE_ENTRY``（不许用一份
        "我认为对完了"把门禁洗白）。账本不可读时（合成夹具/旧库）才退回证据
        自声明，且把声明事实连同 :func:`facts_digest` 一起落盘，供
        :meth:`before_entry` 读取时复核。
        """
        now = now_ms if now_ms is not None else _now_ms()
        if untrusted_clock:
            return RecoveryGateDecision(False, ("CLOCK_UNTRUSTED",), self.state)
        if not isinstance(evidence, Mapping):
            return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
        if "observed_at_ms" not in evidence:
            return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
        observed = evidence.get("observed_at_ms")
        if isinstance(observed, bool) or not isinstance(observed, int):
            return RecoveryGateDecision(False, (EVIDENCE_TYPE_INVALID,), self.state)
        for key in ("positions_verified", "open_orders_verified"):
            if key not in evidence:
                return RecoveryGateDecision(False, (EVIDENCE_MISSING,), self.state)
            if evidence.get(key) is not True:
                # 裸布尔之外的形状（字符串真值等）一律类型错误；False 即未完成。
                if not isinstance(evidence.get(key), bool):
                    return RecoveryGateDecision(False, (EVIDENCE_TYPE_INVALID,), self.state)
                return RecoveryGateDecision(False, (RECONCILE_REQUIRED_BEFORE_ENTRY,),
                                            self.state)
        if observed > now + 1000 or now - observed > self.evidence_ttl_ms:
            return RecoveryGateDecision(False, (EVIDENCE_STALE,), self.state)
        # 身份绑定（P2-6）：门禁已绑定的账户/运行代必须与本次提交一致。
        # 空绑定（恢复包安装时还不知道运行代）不阻拦，由 bind_runtime 在服务
        # 启动时补齐。
        if self.account_ref and account_ref and str(account_ref) != self.account_ref:
            return RecoveryGateDecision(False, (EVIDENCE_ACCOUNT_MISMATCH,), self.state)
        if (self.runtime_instance_id and runtime_instance_id
                and str(runtime_instance_id) != self.runtime_instance_id):
            return RecoveryGateDecision(False, (EVIDENCE_ACCOUNT_MISMATCH,), self.state)
        # P1-3：证据必须绑定**真实的**对账事实，而不是操作者自证的裸布尔。
        ledger = self.live_reconcile_facts()
        if ledger is not None:
            if _facts_block_unresolved(ledger):
                return RecoveryGateDecision(
                    False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
            bound_facts: dict[str, Any] = dict(ledger)
        else:
            declared: dict[str, Any] = {key: 0 for key in FACTS_KEYS}
            for key in FACTS_KEYS:
                if key not in evidence:
                    continue
                value = evidence.get(key)
                if isinstance(value, bool) or not isinstance(value, int):
                    return RecoveryGateDecision(
                        False, (EVIDENCE_TYPE_INVALID,), self.state)
                declared[key] = int(value)
            if _facts_block_unresolved(declared):
                return RecoveryGateDecision(
                    False, (RECONCILE_REQUIRED_BEFORE_ENTRY,), self.state)
            bound_facts = {**declared, "source": "evidence"}
        self.state = GATE_STATE_CLEARED
        self.reconciled_at_ms = now
        self.account_ref = account_ref or self.account_ref
        self.runtime_instance_id = runtime_instance_id or self.runtime_instance_id
        self.evidence = {
            "observed_at_ms": observed,
            "positions_verified": True,
            "open_orders_verified": True,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "facts": bound_facts,
            "facts_digest": facts_digest(bound_facts),
        }
        self.save()
        return RecoveryGateDecision(True, (GATE_CLEARED,), self.state)
