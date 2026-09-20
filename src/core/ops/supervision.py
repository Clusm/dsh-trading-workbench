"""U13 进程托管：有界重启、人工禁用优先、运行代持久化（F06/F08/F17/F18/F19）。

三条不可退让的边界（对应任务卡必达要点 1–3）：

1. **profile 显式**：托管决策必须知道 profile（demo/live）。未声明 profile 时
   一律 ``BLOCKED``（``PROFILE_NOT_DECLARED``），不回退到"公共根"式隐式语义。
2. **人工禁用 live 优先于自动恢复**：人工禁用（``manual_disable``）在决策顺序里
   是 **第一条** 判定，任何自动恢复路径都不可能把它放宽——包括"重启次数用尽"
   与"健康探针恢复"。要解除只有 operator 显式二次确认
   （``operator_clear_manual_recovery``）。
3. **重启有界**：退避 30/60/120 秒；窗口内（默认 10 分钟）最多 3 次重启，超出
   即进入 ``MANUAL_RECOVERY`` 并停止自动重启（**不无限重启**）。

同时提供两件下游必需的东西：

- :class:`RunRegistry`：``runtime_instance_id`` / 运行代 **持久化**（指纹报告
  diff-6）。重启沿用同一运行代；凭据轮换只改 ``credential_epoch``/``key_fingerprint``，
  不改运行代（沿用 U15 :func:`core.runtime_identity.mint_runtime_generation` 口径，
  **不重复实现**身份派生）。
- :class:`ClockGuard`：NTP 回拨/快进与 monotonic 期限（F17）。时钟不可信时
  托管决策 ``BLOCKED``，且不允许按墙钟重复日重置。

本模块是纯策略 + 原子 JSON 落盘：不启动/不结束任何真实进程，不联网，不读
``.env``/密钥，不发送告警。真实宿主托管归 U12 OPS。
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.runtime_paths import RuntimePaths

__all__ = [
    "AUTO_ACTOR",
    "BACKOFF_SCHEDULE_SECONDS",
    "CLOCK_FORWARD_JUMP",
    "CLOCK_ROLLBACK",
    "CLOCK_UNTRUSTED",
    "MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY",
    "MANUAL_RECOVERY_REQUIRED",
    "MONOTONIC_BACKWARDS",
    "OPERATOR_CONFIRM_REQUIRED",
    "RESTART_ALLOWED",
    "RESTART_LIMIT_EXCEEDED",
    "RESTART_WINDOW_SECONDS",
    "STATE_BLOCKED",
    "STATE_MANUAL_RECOVERY",
    "STATE_RESTART_BACKOFF",
    "STATE_RUNNING",
    "ClockGuard",
    "ClockReading",
    "ManualDisable",
    "RestartPolicy",
    "RunRecord",
    "RunRegistry",
    "SupervisionDecision",
    "SupervisionLedger",
    "load_or_mint_run_record",
    "lock_scope_report",
    "operator_clear_manual_recovery",
    "plan_supervision",
]

#: 有界退避：第 1/2/3 次重启的等待秒数（任务卡明示 30/60/120）。
BACKOFF_SCHEDULE_SECONDS: tuple[float, ...] = (30.0, 60.0, 120.0)
#: 滑动窗口（秒）：窗口内最多 :data:`RESTART_LIMIT_IN_WINDOW` 次重启。
RESTART_WINDOW_SECONDS = 600.0
RESTART_LIMIT_IN_WINDOW = 3

STATE_RUNNING = "RUNNING"
STATE_RESTART_BACKOFF = "RESTART_BACKOFF"
STATE_MANUAL_RECOVERY = "MANUAL_RECOVERY"
STATE_BLOCKED = "BLOCKED"

RESTART_ALLOWED = "RESTART_ALLOWED"
RESTART_LIMIT_EXCEEDED = "RESTART_LIMIT_EXCEEDED"
MANUAL_RECOVERY_REQUIRED = "MANUAL_RECOVERY_REQUIRED"
MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY = "MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY"
OPERATOR_CONFIRM_REQUIRED = "OPERATOR_CONFIRM_REQUIRED"
PROFILE_NOT_DECLARED = "PROFILE_NOT_DECLARED"
CLOCK_UNTRUSTED = "CLOCK_UNTRUSTED"
CLOCK_ROLLBACK = "CLOCK_ROLLBACK"
CLOCK_FORWARD_JUMP = "CLOCK_FORWARD_JUMP"
MONOTONIC_BACKWARDS = "MONOTONIC_BACKWARDS"

#: 与 U15 控制面同名的自动执行者（任何自动路径不得放宽人工禁用）。
AUTO_ACTOR = "auto"


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    """原子落盘：先写同目录临时文件再 ``os.replace``，崩溃不留半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    temp.write_text(data, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# --------------------------------------------------------------------------
# 时钟可信度（F17）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClockReading:
    """一次时钟读数：墙钟 + 单调钟 + 单调期限（截止点）。"""

    wall_seconds: float
    monotonic_seconds: float
    deadline_monotonic: float | None = None

    def deadline_expired(self, monotonic_now: float) -> bool:
        """单调期限判定：**不**用墙钟（NTP 回拨不得延长授权）。"""
        if self.deadline_monotonic is None:
            return False
        return monotonic_now >= self.deadline_monotonic


@dataclass(frozen=True)
class ClockVerdict:
    trusted: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"trusted": self.trusted, "reasons": list(self.reasons)}


@dataclass(frozen=True)
class ClockGuard:
    """墙钟回拨/快进与单调钟倒退检测。

    - 墙钟回拨超过 ``rollback_tolerance_seconds`` → ``CLOCK_ROLLBACK``；
    - 墙钟前跳超过 ``forward_jump_tolerance_seconds`` 而单调钟几乎没动 →
      ``CLOCK_FORWARD_JUMP``（NTP 快进 / 人为改表）；
    - 单调钟倒退 → ``MONOTONIC_BACKWARDS``（不应发生；发生即不可信）。

    判定只看两个读数，不改系统时钟、不读网络时间。
    """

    rollback_tolerance_seconds: float = 5.0
    forward_jump_tolerance_seconds: float = 120.0
    monotonic_tolerance_seconds: float = 1.0

    def check(self, previous: ClockReading, current: ClockReading) -> ClockVerdict:
        reasons: list[str] = []
        wall_delta = current.wall_seconds - previous.wall_seconds
        monotonic_delta = current.monotonic_seconds - previous.monotonic_seconds
        if monotonic_delta < -self.monotonic_tolerance_seconds:
            reasons.append(MONOTONIC_BACKWARDS)
        if wall_delta < -self.rollback_tolerance_seconds:
            reasons.append(CLOCK_ROLLBACK)
        if (wall_delta - max(monotonic_delta, 0.0)) > self.forward_jump_tolerance_seconds:
            reasons.append(CLOCK_FORWARD_JUMP)
        return ClockVerdict(not reasons, tuple(reasons))

    def daily_reset_allowed(
        self, *, device_day: str, recorded_day: str, verdict: ClockVerdict,
    ) -> tuple[bool, tuple[str, ...]]:
        """日切重置：时钟不可信时 **不** 做第二次日重置（F17：不重复日重置）。"""
        if not verdict.trusted:
            return False, (CLOCK_UNTRUSTED,)
        return device_day != recorded_day, ()


# --------------------------------------------------------------------------
# 运行代持久化（diff-6）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """持久化的运行代：跨重启稳定，凭据轮换不改变前四项。"""

    environment: str
    account_ref: str
    runtime_instance_id: str
    generation_id: str
    credential_epoch: int = 0
    key_fingerprint: str | None = None
    scope_hash: str | None = None
    started_at: float | None = None
    restarts: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "credential_epoch": self.credential_epoch,
            "key_fingerprint": self.key_fingerprint,
            "scope_hash": self.scope_hash,
            "started_at": self.started_at,
            "restarts": self.restarts,
        }


class RunRegistry:
    """把运行代持久化到状态根（``RuntimePaths.run_registry_file``）。

    **重启沿用同一运行代**：只要环境/账户/凭据纪元不变，重新加载得到同一个
    ``runtime_instance_id`` 与 ``generation_id``；凭据轮换（``credential_epoch``
    变化）只更新纪元与指纹，不换运行代，因此账本连续归集不被轮换打断。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.record: RunRecord | None = None

    def load(self) -> RunRecord | None:
        payload = _read_json(self.path)
        if payload is None:
            self.record = None
            return None
        try:
            self.record = RunRecord(
                environment=str(payload["environment"]),
                account_ref=str(payload["account_ref"]),
                runtime_instance_id=str(payload["runtime_instance_id"]),
                generation_id=str(payload["generation_id"]),
                credential_epoch=int(payload.get("credential_epoch") or 0),
                key_fingerprint=payload.get("key_fingerprint"),
                scope_hash=payload.get("scope_hash"),
                started_at=payload.get("started_at"),
                restarts=int(payload.get("restarts") or 0),
            )
        except (KeyError, TypeError, ValueError):
            # 运行代文件损坏 → 视为不存在（重新铸造），但绝不静默当作"同代"。
            self.record = None
        return self.record

    def persist(self, record: RunRecord) -> RunRecord:
        self.record = record
        _atomic_write_json(self.path, record.as_dict())
        return record

    def load_or_mint(
        self, *, environment: str, account_ref: str, credential_epoch: int = 0,
        key_fingerprint: str | None = None, scope_hash: str | None = None,
        now: float | None = None, runtime_instance_id: str | None = None,
    ) -> tuple[RunRecord, bool]:
        """返回 ``(记录, 是否新铸)``。环境/账户不符即重新铸造（不继承他人运行代）。"""
        import uuid as _uuid

        from core.runtime_identity import mint_runtime_generation

        existing = self.load()
        # 只有"同一环境 + 同一账户"才继承运行代。``generation_id`` 由环境/账户/
        # 实例/起始时间决定，所以**必须先定下 started_at 再铸造**：否则每次重启
        # 传入不同的 ``now`` 都会算出新运行代，把一次重启误报成一个新代（diff-6）。
        same_scope = (existing is not None
                      and existing.environment == environment
                      and existing.account_ref == account_ref)
        started_at = (existing.started_at if same_scope and existing.started_at is not None
                      else (now if now is not None else time.time()))
        if same_scope and existing.runtime_instance_id:
            instance = existing.runtime_instance_id
        else:
            instance = runtime_instance_id or _uuid.uuid4().hex
        generation = mint_runtime_generation(
            environment=environment,
            account_ref=account_ref,
            runtime_instance_id=instance,
            credential_epoch=int(credential_epoch),
            key_fingerprint_value=key_fingerprint,
            scope_hash=scope_hash or (existing.scope_hash if same_scope else None),
            started_at=started_at,
        )
        if (same_scope
                and existing.generation_id == generation.generation_id
                and existing.credential_epoch == generation.credential_epoch
                and existing.key_fingerprint == generation.key_fingerprint
                and existing.scope_hash == generation.scope_hash):
            # 逐字段相同的重启：不重写文件（避免无意义的运行期写盘）。
            return existing, False
        record = RunRecord(
            environment=generation.environment,
            account_ref=generation.account_ref,
            runtime_instance_id=generation.runtime_instance_id,
            generation_id=generation.generation_id,
            credential_epoch=generation.credential_epoch,
            key_fingerprint=generation.key_fingerprint,
            scope_hash=generation.scope_hash,
            started_at=started_at,
            restarts=existing.restarts if same_scope else 0,
        )
        self.persist(record)
        return record, True

    def rotate_credential(
        self, *, credential_epoch: int, key_fingerprint: str | None = None,
    ) -> RunRecord:
        """凭据轮换：只推进纪元/指纹，运行代四要素保持不变。"""
        if self.record is None:
            raise RuntimeError("运行代尚未铸造，不能轮换凭据")
        from core.runtime_identity import mint_runtime_generation

        generation = mint_runtime_generation(
            environment=self.record.environment,
            account_ref=self.record.account_ref,
            runtime_instance_id=self.record.runtime_instance_id,
            credential_epoch=int(credential_epoch),
            key_fingerprint_value=key_fingerprint,
            scope_hash=self.record.scope_hash,
            started_at=self.record.started_at,
        )
        record = RunRecord(
            environment=generation.environment, account_ref=generation.account_ref,
            runtime_instance_id=generation.runtime_instance_id,
            generation_id=generation.generation_id,
            credential_epoch=generation.credential_epoch,
            key_fingerprint=generation.key_fingerprint,
            scope_hash=generation.scope_hash, started_at=self.record.started_at,
            restarts=self.record.restarts,
        )
        self.persist(record)
        return record

    def note_restart(self, at: float) -> RunRecord:
        if self.record is None:
            raise RuntimeError("运行代尚未铸造")
        return self.persist(RunRecord(**{**self.record.as_dict(),
                                         "restarts": self.record.restarts + 1}))


def load_or_mint_run_record(
    paths: RuntimePaths, *, environment: str, account_ref: str,
    credential_epoch: int = 0, key_fingerprint: str | None = None,
    scope_hash: str | None = None, now: float | None = None,
) -> tuple[RunRecord, bool]:
    """便捷入口：按状态根里的运行代文件加载或铸造（diff-6）。"""
    return RunRegistry(paths.run_registry_file).load_or_mint(
        environment=environment, account_ref=account_ref,
        credential_epoch=credential_epoch, key_fingerprint=key_fingerprint,
        scope_hash=scope_hash or paths.lock_scope(), now=now,
    )


# --------------------------------------------------------------------------
# 托管账本与决策（F06/F19）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RestartPolicy:
    backoff_seconds: tuple[float, ...] = BACKOFF_SCHEDULE_SECONDS
    window_seconds: float = RESTART_WINDOW_SECONDS
    max_restarts: int = RESTART_LIMIT_IN_WINDOW

    def delay_for(self, attempt_index: int) -> float:
        """第 ``attempt_index``（0 基）次重启的等待秒数；超出表长取上限。"""
        schedule = self.backoff_seconds or BACKOFF_SCHEDULE_SECONDS
        return float(schedule[min(max(attempt_index, 0), len(schedule) - 1)])


@dataclass
class ManualDisable:
    """人工禁用（live 不得自动启动）。一旦置位，托管逻辑不得放宽。"""

    active: bool = False
    actor: str = ""
    reason: str = ""
    source: str = ""
    recorded_at: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"active": bool(self.active), "actor": self.actor, "reason": self.reason,
                "source": self.source, "recorded_at": self.recorded_at}


@dataclass
class SupervisionLedger:
    """托管账本：重启时间线 + 人工禁用 + 人工恢复态；原子落盘。"""

    path: Path
    restart_times: list[float] = field(default_factory=list)
    manual_recovery: bool = False
    manual_recovery_reason: str = ""
    manual_disable: ManualDisable = field(default_factory=ManualDisable)
    clock: ClockVerdict | None = None
    schema: int = 1

    @classmethod
    def load(cls, path: Path) -> "SupervisionLedger":
        ledger = cls(path=Path(path))
        payload = _read_json(Path(path))
        if payload is None:
            return ledger
        times: list[float] = []
        for value in payload.get("restart_times") or []:
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                continue
        disable = payload.get("manual_disable") or {}
        ledger.restart_times = times
        ledger.manual_recovery = bool(payload.get("manual_recovery"))
        ledger.manual_recovery_reason = str(payload.get("manual_recovery_reason") or "")
        ledger.manual_disable = ManualDisable(
            active=bool(disable.get("active")), actor=str(disable.get("actor") or ""),
            reason=str(disable.get("reason") or ""), source=str(disable.get("source") or ""),
            recorded_at=disable.get("recorded_at"),
        )
        return ledger

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "restart_times": [round(value, 3) for value in self.restart_times],
            "manual_recovery": self.manual_recovery,
            "manual_recovery_reason": self.manual_recovery_reason,
            "manual_disable": self.manual_disable.as_dict(),
        }

    def save(self) -> None:
        _atomic_write_json(self.path, self.as_dict())

    def restarts_in_window(self, now: float, window_seconds: float) -> list[float]:
        cutoff = now - window_seconds
        return sorted(value for value in self.restart_times if value >= cutoff)

    def prune(self, now: float, window_seconds: float) -> None:
        cutoff = now - window_seconds
        self.restart_times = [value for value in self.restart_times if value >= cutoff]

    # -- 人工禁用 --

    def set_manual_disable(self, *, actor: str, reason: str, source: str,
                           now: float) -> ManualDisable:
        """置位人工禁用。自动执行者也可以 **置位**（按健康判定收紧），
        但任何执行者都不能把它 **放宽**（放宽只走 operator_clear_manual_recovery）。
        """
        self.manual_disable = ManualDisable(active=True, actor=actor, reason=reason,
                                            source=source, recorded_at=now)
        return self.manual_disable

    def record_restart(self, at: float, *, policy: RestartPolicy | None = None) -> None:
        """记录一次重启。人工禁用生效期间 **拒绝** 记录（不得用它绕过禁用）。"""
        if self.manual_disable.active:
            raise RuntimeError(MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY)
        self.restart_times.append(float(at))
        if policy is not None:
            self.prune(float(at), policy.window_seconds)


@dataclass(frozen=True)
class SupervisionDecision:
    allowed: bool
    state: str
    delay_seconds: float
    reason_codes: tuple[str, ...]
    restarts_in_window: int
    manual_disable_active: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "state": self.state,
            "delay_seconds": self.delay_seconds,
            "reason_codes": list(self.reason_codes),
            "restarts_in_window": self.restarts_in_window,
            "manual_disable_active": self.manual_disable_active,
        }


def plan_supervision(
    ledger: SupervisionLedger, *, now: float, profile: str | None,
    policy: RestartPolicy | None = None, clock: ClockVerdict | None = None,
) -> SupervisionDecision:
    """托管决策（**判定顺序即优先级**，人工禁用永远第一）。

    顺序：

    1. profile 未显式声明 → ``BLOCKED`` / ``PROFILE_NOT_DECLARED``；
    2. 人工禁用生效 → ``MANUAL_RECOVERY`` /
       ``MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY``（**不因健康恢复或重启用尽而放宽**）；
    3. 已进入人工恢复态 → ``MANUAL_RECOVERY`` / ``MANUAL_RECOVERY_REQUIRED``；
    4. 时钟不可信 → ``BLOCKED`` / ``CLOCK_UNTRUSTED``；
    5. 窗口内重启次数达上限 → 置 ``manual_recovery`` 并返回
       ``MANUAL_RECOVERY`` / ``RESTART_LIMIT_EXCEEDED``；
    6. 其余 → ``RESTART_BACKOFF`` / ``RESTART_ALLOWED``，退避 30/60/120 秒。
    """
    policy = policy or RestartPolicy()
    declared = (profile or "").strip().lower()
    in_window = len(ledger.restarts_in_window(now, policy.window_seconds))

    if not declared:
        return SupervisionDecision(False, STATE_BLOCKED, 0.0,
                                   (PROFILE_NOT_DECLARED,), in_window,
                                   ledger.manual_disable.active)
    if ledger.manual_disable.active:
        # 人工禁用优先：**不** torch 任何自动恢复分支，直接人工恢复态。
        return SupervisionDecision(False, STATE_MANUAL_RECOVERY, 0.0,
                                   (MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY,), in_window,
                                   True)
    if ledger.manual_recovery:
        reasons = (MANUAL_RECOVERY_REQUIRED,)
        if ledger.manual_recovery_reason:
            reasons = (ledger.manual_recovery_reason, MANUAL_RECOVERY_REQUIRED)
        return SupervisionDecision(False, STATE_MANUAL_RECOVERY, 0.0, reasons,
                                   in_window, False)
    verdict = clock if clock is not None else ledger.clock
    if verdict is not None and not verdict.trusted:
        return SupervisionDecision(False, STATE_BLOCKED, 0.0,
                                   (CLOCK_UNTRUSTED, *verdict.reasons), in_window, False)
    if in_window >= policy.max_restarts:
        ledger.manual_recovery = True
        ledger.manual_recovery_reason = RESTART_LIMIT_EXCEEDED
        # 立即持久化：进入人工恢复态必须跨重启可见，不能只留在内存里。
        ledger.save()
        return SupervisionDecision(False, STATE_MANUAL_RECOVERY, 0.0,
                                   (RESTART_LIMIT_EXCEEDED, MANUAL_RECOVERY_REQUIRED),
                                   in_window, False)
    return SupervisionDecision(True, STATE_RESTART_BACKOFF,
                               policy.delay_for(in_window), (RESTART_ALLOWED,),
                               in_window, False)


def operator_clear_manual_recovery(
    ledger: SupervisionLedger, *, actor: str, confirm: bool, now: float,
    reason: str = "",
) -> tuple[bool, tuple[str, ...]]:
    """解除人工恢复态／人工禁用：**只允许 operator 且必须显式确认**。

    ``actor=auto`` → 拒绝（``MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY``）；
    未确认 → 拒绝（``OPERATOR_CONFIRM_REQUIRED``）。这与 U15 控制面
    ``authorize_control_request`` 的人工二次确认语义同向（自动不得放宽人工禁用）。
    """
    if (actor or "").strip().lower() == AUTO_ACTOR:
        return False, (MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY,)
    if (actor or "").strip().lower() != "operator":
        return False, (OPERATOR_CONFIRM_REQUIRED,)
    if not confirm:
        return False, (OPERATOR_CONFIRM_REQUIRED,)
    ledger.manual_recovery = False
    ledger.manual_recovery_reason = ""
    ledger.manual_disable = ManualDisable(active=False, actor=actor,
                                          reason=reason or "operator cleared",
                                          source="operator", recorded_at=now)
    ledger.restart_times = []
    ledger.save()
    return True, ()


def lock_scope_report(paths: RuntimePaths) -> dict[str, Any]:
    """单账户执行者证明：锁路径与 scope 只由状态根+profile 决定（R12/F18）。

    同一 ``state_base`` 下两个不同 ``code_root`` 必须得到 **同一个** 锁路径与
    scope_hash——这就是"换代码根不改状态根"的可执行证据。
    """
    return {
        "lock_file": str(paths.lock_file),
        "lock_scope": paths.lock_scope(),
        "state_base": str(paths.state_base),
        "profile": paths.profile,
        "shared_egress_root": str(paths.shared_egress_root),
        "run_registry_file": str(paths.run_registry_file),
    }
