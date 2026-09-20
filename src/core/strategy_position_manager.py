"""U02 策略仓管理：预期持仓与硬退出分离、旧仓显式迁移、重复退出幂等。

本模块只做**判定与记账**：不联网、不下单撤单、不读 ``.env``/``data/``、不构造
Broker、不改风险档与 pause/emergency 语义。它把四件在竞态下容易互相污染的
事情彻底分开：

1. **预期持仓（expected holdings）**：策略/通道"认为应该持有多少"。它是策略侧
   的声明，由 :meth:`StrategyPositionManager.set_expectation` 显式写入。
2. **硬退出（hard exit）**：保护触发（SL/TP/失效/人工急平）是**交易所侧的终态
   事实**，写入 append-only 的硬退出流水。两者是两个命名空间：记录硬退出**不会**
   删除或改写预期持仓（否则重启后的对账会拿"已触发"当"策略已放弃"，把仓位管理
   责任悄悄抹掉）；写预期持仓也**不会**抹掉已发生的硬退出事实。
3. **旧仓（无策略归属）**：没有 ``strategy_id``/``ticket_id`` 的仓位一律判为
   ``LEGACY_UNATTRIBUTED``。重启时 :meth:`plan_restart` **绝不**产生任何退出
   动作，只产生 ``MIGRATE_EXPLICIT`` 标记；只有 :meth:`migrate_legacy` 被显式
   调用（带 actor/reason）才完成迁移。这是"重启不得隐式批量退出"的落地。
4. **重复退出**：:meth:`request_exit` 以稳定 ``exit_key`` 幂等，重复请求返回
   ``duplicate`` 且 ``submit_qty=0``；退出方向由持仓侧唯一决定（LONG→SELL、
   SHORT→BUY），永远 ``reduce_only``、``opens_position=False``，且提交量被
   当前可减量（扣掉在途预留）夹逼——因此**不存在**把重复退出做成反向开仓的路径。
   幂等的边界是"这次退出还没结束"：一旦 :meth:`release_exit` 释放了预留
   （提交失败/成交未确认/异常路径），或 :meth:`reclaim_stale_exits` 回收了超过
   TTL 仍未结算的在途预留，该 ``exit_key`` 就**恢复可重发**——否则一次异常就会
   让该标的的退出通道被永久占死（U02/A_r1 P0-1）。已 ``CONSUMED``（成交已确认）
   的键永远保持幂等：**成交事实**记在账本的独立集合里
   （``ManagementReservationLedger.is_consumed``），不随预留记录被
   ``MAX_TERMINAL_RESERVATIONS`` 淘汰而丢失，因此"永不回收"对
   **记录已被淘汰**的键同样成立（U02/A_r2 NEW-1）。

R11：:meth:`should_flat_close` 复用 U04 的故障分类，健康/传输/未知故障一律不得
触发全平；只有"已确证的保护失败"与**人工 emergency** 可以。人工 pause/emergency
的语义由控制面持有，本模块只读取、不修改。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from core.contracts import decimal_from_value
from core.management_authorization import (
    ManagementReservationLedger,
    REASON_RESERVATION_EXCEEDS_POSITION,
)

# --------------------------------------------------------------------------- #
# reason codes
# --------------------------------------------------------------------------- #
REASON_LEGACY_REQUIRES_MIGRATION = "LEGACY_POSITION_REQUIRES_EXPLICIT_MIGRATION"
REASON_IMPLICIT_BULK_EXIT_BLOCKED = "IMPLICIT_BULK_EXIT_BLOCKED"
REASON_RESTART_NO_IMPLICIT_EXIT = "RESTART_HAS_NO_IMPLICIT_EXIT"
REASON_DUPLICATE_EXIT_IGNORED = "DUPLICATE_EXIT_IGNORED"
REASON_NO_POSITION_TO_EXIT = "NO_POSITION_TO_EXIT"
REASON_EXIT_EXCEEDS_POSITION = "EXIT_EXCEEDS_POSITION"
REASON_EXPECTATION_UNATTRIBUTED = "EXPECTATION_UNATTRIBUTED"
REASON_HARD_EXIT_RECORDED = "HARD_EXIT_RECORDED"
REASON_LEGACY_MIGRATED = "LEGACY_POSITION_MIGRATED"
#: 在途退出预留超时未结算 -> 回收并允许同键重发（否则该标的永久不可退出）。
REASON_STALE_RESERVATION_RECLAIMED = "STALE_EXIT_RESERVATION_RECLAIMED"
#: 迁移了一个**未知**生命周期（运营笔误）：不抛错，但必须可见（qty 会记成 0）。
REASON_LEGACY_MIGRATION_UNKNOWN_LIFECYCLE = "LEGACY_MIGRATION_UNKNOWN_LIFECYCLE"

BULK_EXIT_CONTEXTS = frozenset({"restart", "startup", "reconcile"})
EXIT_ACTION_PREFIXES = ("close", "reduce", "flatten", "exit")

__all__ = [
    "ExpectationState",
    "ExitRequestOutcome",
    "FlatCloseDecision",
    "HardExitRecord",
    "ImplicitBulkExitError",
    "LegacyMigrationRecord",
    "PositionClass",
    "PositionClassRecord",
    "RestartAction",
    "RestartPlan",
    "StrategyExpectation",
    "StrategyPositionManager",
    "REASON_DUPLICATE_EXIT_IGNORED",
    "REASON_EXIT_EXCEEDS_POSITION",
    "REASON_HARD_EXIT_RECORDED",
    "REASON_IMPLICIT_BULK_EXIT_BLOCKED",
    "REASON_LEGACY_MIGRATED",
    "REASON_LEGACY_MIGRATION_UNKNOWN_LIFECYCLE",
    "REASON_LEGACY_REQUIRES_MIGRATION",
    "REASON_NO_POSITION_TO_EXIT",
    "REASON_STALE_RESERVATION_RECLAIMED",
]


class ImplicitBulkExitError(RuntimeError):
    """重启/对账上下文中出现了未经人工显式确认的批量退出。"""

    def __init__(self, context: str, offenders: Sequence[str]) -> None:
        self.context = context
        self.offenders = tuple(offenders)
        super().__init__(
            f"{REASON_IMPLICIT_BULK_EXIT_BLOCKED}: context={context} "
            f"offenders={list(self.offenders)}")


# --------------------------------------------------------------------------- #
# 模型
# --------------------------------------------------------------------------- #
class ExpectationState(str, Enum):
    HELD = "HELD"
    EXIT_PENDING = "EXIT_PENDING"
    FLAT = "FLAT"
    LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"


class PositionClass(str, Enum):
    ATTRIBUTED = "ATTRIBUTED"
    LEGACY_UNATTRIBUTED = "LEGACY_UNATTRIBUTED"


@dataclass(frozen=True)
class StrategyExpectation:
    """策略对某个生命周期的"应持有量"声明（与硬退出事实无关）。"""

    lifecycle_id: str
    symbol: str
    position_side: str
    expected_qty: Decimal
    strategy_id: str | None
    ticket_id: str | None
    revision: int
    state: ExpectationState
    updated_at_ms: int
    reason_code: str = ""

    @property
    def attributed(self) -> bool:
        return bool(self.strategy_id or self.ticket_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "expected_qty": str(self.expected_qty),
            "strategy_id": self.strategy_id, "ticket_id": self.ticket_id,
            "revision": self.revision, "state": self.state.value,
            "updated_at_ms": self.updated_at_ms, "reason_code": self.reason_code,
            "attributed": self.attributed,
        }


@dataclass(frozen=True)
class HardExitRecord:
    """保护触发的终态退出事实；独立于预期持仓，append-only。"""

    exit_id: str
    lifecycle_id: str
    symbol: str
    position_side: str
    requested_qty: Decimal
    trigger: str
    evidence_id: str
    recorded_at_ms: int
    expected_qty_at_trigger: Decimal
    account_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_id": self.exit_id, "lifecycle_id": self.lifecycle_id,
            "symbol": self.symbol, "position_side": self.position_side,
            "requested_qty": str(self.requested_qty), "trigger": self.trigger,
            "evidence_id": self.evidence_id, "recorded_at_ms": self.recorded_at_ms,
            "expected_qty_at_trigger": str(self.expected_qty_at_trigger),
            "account_ref": self.account_ref,
        }


@dataclass(frozen=True)
class PositionClassRecord:
    lifecycle_id: str
    symbol: str
    position_side: str
    qty: Decimal
    classification: PositionClass
    strategy_id: str | None = None
    ticket_id: str | None = None

    @property
    def is_legacy(self) -> bool:
        return self.classification is PositionClass.LEGACY_UNATTRIBUTED

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "qty": str(self.qty),
            "classification": self.classification.value,
            "strategy_id": self.strategy_id, "ticket_id": self.ticket_id,
            "is_legacy": self.is_legacy,
        }


@dataclass(frozen=True)
class RestartAction:
    lifecycle_id: str
    symbol: str
    position_side: str
    kind: str  # MIGRATE_EXPLICIT | ALIGN_EXPECTATION | NOOP
    detail: str
    reason_code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "kind": self.kind,
            "detail": self.detail, "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class RestartPlan:
    """重启计划；``implicit_bulk_exit`` 恒为 False，且不含任何退出动作。"""

    actions: tuple[RestartAction, ...]
    legacy: tuple[PositionClassRecord, ...]
    reason_codes: tuple[str, ...] = ()
    implicit_bulk_exit: bool = False

    @property
    def exit_actions(self) -> tuple[RestartAction, ...]:
        """计划里**实际**的退出动作（按动作名筛选，不是字面空元组）。

        本属性是"重启不含退出动作"这条不变量的证据位：它由 :meth:`StrategyPosition
        Manager.plan_restart` 只产出 MIGRATE_EXPLICIT/ALREADY_MIGRATED/
        ALIGN_EXPECTATION/NOOP 这一构造保证为真，而不再是恒真式。
        """
        return tuple(item for item in self.actions
                     if str(item.kind or "").lower().startswith(EXIT_ACTION_PREFIXES))

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": [item.as_dict() for item in self.actions],
            "legacy": [item.as_dict() for item in self.legacy],
            "reason_codes": list(self.reason_codes),
            "implicit_bulk_exit": self.implicit_bulk_exit,
            "exit_action_count": len(self.exit_actions),
        }


@dataclass(frozen=True)
class LegacyMigrationRecord:
    lifecycle_id: str
    symbol: str
    position_side: str
    qty: Decimal
    target_source: str
    actor: str
    reason: str
    migrated_at_ms: int
    reason_code: str = REASON_LEGACY_MIGRATED

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id, "symbol": self.symbol,
            "position_side": self.position_side, "qty": str(self.qty),
            "target_source": self.target_source, "actor": self.actor,
            "reason": self.reason, "migrated_at_ms": self.migrated_at_ms,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ExitRequestOutcome:
    """一次退出请求的判定；``opens_position`` 恒为 False。"""

    exit_key: str
    accepted: bool
    duplicate: bool
    action: str  # SUBMIT_REDUCE | NOOP
    side: str  # 由持仓侧唯一决定：LONG→SELL、SHORT→BUY
    submit_qty: Decimal
    reduce_only: bool
    reason_codes: tuple[str, ...] = ()
    opens_position: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "exit_key": self.exit_key, "accepted": self.accepted,
            "duplicate": self.duplicate, "action": self.action, "side": self.side,
            "submit_qty": str(self.submit_qty), "reduce_only": self.reduce_only,
            "reason_codes": list(self.reason_codes),
            "opens_position": self.opens_position,
        }


@dataclass(frozen=True)
class FlatCloseDecision:
    allowed: bool
    action: str
    trigger: str
    reason_codes: tuple[str, ...] = ()
    #: 三条硬不变量：本判定永不改变仓外语义。
    changes_risk_profile: bool = False
    touches_pause_state: bool = False
    touches_emergency_state: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed, "action": self.action, "trigger": self.trigger,
            "reason_codes": list(self.reason_codes),
            "changes_risk_profile": self.changes_risk_profile,
            "touches_pause_state": self.touches_pause_state,
            "touches_emergency_state": self.touches_emergency_state,
        }


def exit_side_for(position_side: str) -> str:
    """退出方向由持仓侧唯一决定；未知持仓侧按最保守方向（减少敞口）。"""
    side = str(position_side or "").upper()
    if side == "LONG":
        return "SELL"
    if side == "SHORT":
        return "BUY"
    return "SELL"


def _position_field(position: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in position and position.get(name) is not None:
            return position.get(name)
    return None


# --------------------------------------------------------------------------- #
# 管理器
# --------------------------------------------------------------------------- #
class StrategyPositionManager:
    """预期持仓 / 硬退出 / 旧仓迁移 / 退出预留 的单一记账入口。"""

    #: 退出幂等记忆的上限：只淘汰**已终结**（成交/释放过）的旧键，防止长进程
    #: 无界增长。淘汰一个已终结的键只影响"极端老键上的重复抑制"——即使因此重发，
    #: 也只会走 request_exit 的"只减 + 可减量夹逼"，不可能被做成反向开仓。
    #: ACTIVE（在途）的键永不淘汰。注意：**已成交**的键即使在*这里*被淘汰，也
    #: 不会被重新占量——:meth:`request_exit` 会回到账本的成交事实
    #: （``is_consumed``）上把它判为重复。
    MAX_EXIT_KEYS = 2000
    #: 释放审计环形缓冲的上限。
    MAX_RELEASED_EXIT_AUDIT = 200

    def __init__(
        self,
        *,
        account_ref: str = "",
        environment: str = "",
        lock: threading.Lock | None = None,
    ) -> None:
        self.account_ref = str(account_ref or "")
        self.environment = str(environment or "")
        self._expectations: dict[str, StrategyExpectation] = {}
        self._hard_exits: list[HardExitRecord] = []
        self._legacy: dict[str, PositionClassRecord] = {}
        self._migrations: list[LegacyMigrationRecord] = []
        self._exit_keys: dict[str, ExitRequestOutcome] = {}
        self._exit_ledgers: dict[str, str] = {}
        self._exit_registered_at: dict[str, int] = {}
        self._released_exits: list[dict[str, Any]] = []
        self._ledgers: dict[str, ManagementReservationLedger] = {}
        self._lock = lock or threading.Lock()

    # -- 预期持仓 ---------------------------------------------------------- #
    def set_expectation(
        self,
        *,
        lifecycle_id: str,
        symbol: str,
        position_side: str,
        expected_qty: Decimal | str | int,
        now_ms: int,
        strategy_id: str | None = None,
        ticket_id: str | None = None,
        state: ExpectationState | None = None,
        reason_code: str = "",
    ) -> StrategyExpectation:
        qty = decimal_from_value(expected_qty)
        if qty < 0:
            raise ValueError("expected_qty must be >= 0")
        key = str(lifecycle_id)
        with self._lock:
            previous = self._expectations.get(key)
            revision = (previous.revision + 1) if previous else 0
            resolved_state = state or (
                ExpectationState.FLAT if qty == 0 else ExpectationState.HELD)
            if resolved_state in (ExpectationState.HELD, ExpectationState.EXIT_PENDING) \
                    and not (strategy_id or ticket_id):
                resolved_state = ExpectationState.LEGACY_UNATTRIBUTED
                reason_code = reason_code or REASON_EXPECTATION_UNATTRIBUTED
            expectation = StrategyExpectation(
                lifecycle_id=key, symbol=str(symbol).upper(),
                position_side=str(position_side).upper(), expected_qty=qty,
                strategy_id=strategy_id, ticket_id=ticket_id, revision=revision,
                state=resolved_state, updated_at_ms=int(now_ms), reason_code=reason_code,
            )
            self._expectations[key] = expectation
            return expectation

    def expectation(self, lifecycle_id: str) -> StrategyExpectation | None:
        with self._lock:
            return self._expectations.get(str(lifecycle_id))

    def expectations(self) -> tuple[StrategyExpectation, ...]:
        with self._lock:
            return tuple(sorted(self._expectations.values(),
                                key=lambda item: item.lifecycle_id))

    # -- 硬退出流水 -------------------------------------------------------- #
    def record_hard_exit(
        self,
        *,
        exit_id: str,
        lifecycle_id: str,
        trigger: str,
        evidence_id: str,
        now_ms: int,
        symbol: str = "",
        position_side: str = "",
        requested_qty: Decimal | str | int = 0,
    ) -> HardExitRecord:
        """记录保护触发的硬退出；**不触碰**预期持仓。

        ``expected_qty_at_trigger`` 只是当时的审计快照：它让"触发时策略以为持有
        多少"可复核，但不参与、也不覆盖预期持仓的状态机。
        """
        key = str(lifecycle_id)
        with self._lock:
            snapshot = self._expectations.get(key)
            record = HardExitRecord(
                exit_id=str(exit_id), lifecycle_id=key,
                symbol=str(symbol or (snapshot.symbol if snapshot else "")).upper(),
                position_side=str(
                    position_side or (snapshot.position_side if snapshot else "")).upper(),
                requested_qty=decimal_from_value(requested_qty),
                trigger=str(trigger).upper(), evidence_id=str(evidence_id),
                recorded_at_ms=int(now_ms),
                expected_qty_at_trigger=snapshot.expected_qty if snapshot else Decimal(0),
                account_ref=self.account_ref,
            )
            self._hard_exits.append(record)
            return record

    def hard_exits(self, lifecycle_id: str | None = None) -> tuple[HardExitRecord, ...]:
        with self._lock:
            items = list(self._hard_exits)
        if lifecycle_id is None:
            return tuple(items)
        return tuple(item for item in items if item.lifecycle_id == str(lifecycle_id))

    # -- 旧仓分类与显式迁移 ------------------------------------------------ #
    def classify_position(self, position: Mapping[str, Any]) -> PositionClassRecord:
        lifecycle_id = str(_position_field(
            position, "lifecycle_id", "id") or _position_field(
                position, "symbol", "position_side") or "")
        strategy_id = _position_field(position, "strategy_id")
        ticket_id = _position_field(position, "ticket_id")
        qty = decimal_from_value(_position_field(position, "qty", "positionAmt") or 0)
        classification = (
            PositionClass.ATTRIBUTED
            if (strategy_id or ticket_id) else PositionClass.LEGACY_UNATTRIBUTED)
        return PositionClassRecord(
            lifecycle_id=lifecycle_id,
            symbol=str(_position_field(position, "symbol") or "").upper(),
            position_side=str(_position_field(position, "position_side", "side")
                              or "").upper(),
            qty=qty, classification=classification,
            strategy_id=str(strategy_id) if strategy_id else None,
            ticket_id=str(ticket_id) if ticket_id else None,
        )

    def plan_restart(
        self, positions: Iterable[Mapping[str, Any]], *, now_ms: int = 0,
    ) -> RestartPlan:
        """重启计划：旧仓只标记迁移需求，**绝不**批量退出。

        返回对象结构上就没有退出动作（``exit_actions`` 恒为空、``as_dict()`` 的
        ``exit_action_count`` 恒为 0），因此"重启时隐式批量退出旧仓"在类型层面
        不成立；未归属仓位还要先经 :meth:`migrate_legacy` 才能进入策略管理。
        """
        actions: list[RestartAction] = []
        legacy: list[PositionClassRecord] = []
        reasons: list[str] = []
        with self._lock:
            migrated = {item.lifecycle_id for item in self._migrations}
            for position in positions or ():
                record = self.classify_position(position)
                if record.lifecycle_id and record.lifecycle_id in migrated:
                    # 已显式迁移的生命周期不再被判为"待迁移"：`plan_restart` 每轮
                    # 都跑，若在这里重新登记，运营者的 `migrate_legacy` 结果会在
                    # 下一轮被撤销（U02/A_r1 P2-7）。
                    actions.append(RestartAction(
                        lifecycle_id=record.lifecycle_id, symbol=record.symbol,
                        position_side=record.position_side, kind="ALREADY_MIGRATED",
                        detail="该生命周期已由人工显式迁移，本轮不再重复标记",
                        reason_code=REASON_LEGACY_MIGRATED))
                    continue
                if record.is_legacy:
                    self._legacy[record.lifecycle_id or record.symbol] = record
                    legacy.append(record)
                    actions.append(RestartAction(
                        lifecycle_id=record.lifecycle_id, symbol=record.symbol,
                        position_side=record.position_side, kind="MIGRATE_EXPLICIT",
                        detail="旧仓无策略归属，需人工显式迁移后才纳入策略管理",
                        reason_code=REASON_LEGACY_REQUIRES_MIGRATION))
                    continue
                expectation = self._expectations.get(record.lifecycle_id)
                if expectation is None:
                    actions.append(RestartAction(
                        lifecycle_id=record.lifecycle_id, symbol=record.symbol,
                        position_side=record.position_side, kind="ALIGN_EXPECTATION",
                        detail="有归属但无预期记录，按当前归属重建预期",
                        reason_code="EXPECTATION_REBUILT_FROM_ATTRIBUTION"))
                else:
                    actions.append(RestartAction(
                        lifecycle_id=record.lifecycle_id, symbol=record.symbol,
                        position_side=record.position_side, kind="NOOP",
                        detail="预期持仓与归属均已记录，无需动作"))
            reasons.append(REASON_RESTART_NO_IMPLICIT_EXIT)
            # 传入真实产出的动作（不是字面空元组）：护栏因此有实际检出能力，
            # "重启零退出动作"由构造 + 护栏共同保证（U02/A_r1 P2-8）。
            self.assert_no_implicit_bulk_exit(
                context="restart", actions=[item.as_dict() for item in actions])
        return RestartPlan(actions=tuple(actions), legacy=tuple(legacy),
                           reason_codes=tuple(dict.fromkeys(reasons)),
                           implicit_bulk_exit=False)

    def migrate_legacy(
        self,
        lifecycle_id: str,
        *,
        target_source: str,
        actor: str,
        reason: str,
        now_ms: int,
    ) -> LegacyMigrationRecord:
        """显式迁移旧仓：必须给出目标来源、执行者与原因，缺一不可。"""
        key = str(lifecycle_id)
        if not str(target_source).strip() or not str(actor).strip() \
                or not str(reason).strip():
            raise ValueError(
                "explicit migration requires target_source/actor/reason")
        with self._lock:
            record = self._legacy.get(key)
            qty = record.qty if record else Decimal(0)
            migration = LegacyMigrationRecord(
                lifecycle_id=key,
                symbol=record.symbol if record else "",
                position_side=record.position_side if record else "",
                qty=qty, target_source=str(target_source), actor=str(actor),
                reason=str(reason), migrated_at_ms=int(now_ms),
                # 未知生命周期多半是运营笔误：不抛错（迁移动作已由人确认），但记
                # 成一条**可见**的原因码，而不是静默写一条 qty=0 的记录。
                reason_code=(REASON_LEGACY_MIGRATED if record is not None
                             else REASON_LEGACY_MIGRATION_UNKNOWN_LIFECYCLE))
            self._migrations.append(migration)
            self._legacy.pop(key, None)
            return migration

    def migrations(self) -> tuple[LegacyMigrationRecord, ...]:
        with self._lock:
            return tuple(self._migrations)

    def pending_legacy(self) -> tuple[PositionClassRecord, ...]:
        with self._lock:
            return tuple(sorted(self._legacy.values(),
                                key=lambda item: item.lifecycle_id or item.symbol))

    # -- 退出预留与重复退出 ------------------------------------------------ #
    def ledger(self, lifecycle_id: str) -> ManagementReservationLedger | None:
        with self._lock:
            return self._ledgers.get(str(lifecycle_id))

    def open_ledger(
        self, lifecycle_id: str, *, position_qty: Decimal | str | int,
    ) -> ManagementReservationLedger:
        ledger = ManagementReservationLedger(
            position_qty=position_qty, lifecycle_id=str(lifecycle_id))
        with self._lock:
            self._ledgers[str(lifecycle_id)] = ledger
        return ledger

    def request_exit(
        self,
        *,
        exit_key: str,
        lifecycle_id: str,
        symbol: str,
        position_side: str,
        qty: Decimal | str | int,
        current_qty: Decimal | str | int,
        now_ms: int = 0,
        in_flight_ttl_ms: int | None = None,
    ) -> ExitRequestOutcome:
        """注册一次退出请求；幂等、只减、方向固定，重复即 no-op。

        预留量在返回前即已占用，因此同一份仓量被两个并发退出请求同时读到
        "全部可用"的情形不会发生（R09）。提交量 = min(请求量, 可用量)，可用量
        为 0 时退化为 ``NO_POSITION_TO_EXIT`` 的 no-op，**不会**产生负仓/反向开仓。

        ``in_flight_ttl_ms`` 是**泄漏回收**的兜底：同一 ``exit_key`` 重复到达时，
        若它在账本里的预留仍是 ``ACTIVE`` 且已超过 TTL 未结算，说明上一轮既没有
        成交也没有释放（典型是提交异常路径漏了释放）——此时先回收该预留再重新
        占量，而不是把请求当成"重复"直接吞掉。省略该参数即保持严格幂等语义。
        """
        key = str(exit_key)
        side = exit_side_for(position_side)
        with self._lock:
            duplicate = self._exit_keys.get(key)
            reclaimed: tuple[str, ...] = ()
            if duplicate is not None:
                reclaimed = self._reclaim_if_stale_locked(
                    key, now_ms=int(now_ms), ttl_ms=in_flight_ttl_ms)
                if not reclaimed:
                    return ExitRequestOutcome(
                        exit_key=key, accepted=False, duplicate=True, action="NOOP",
                        side=duplicate.side, submit_qty=Decimal(0), reduce_only=True,
                        reason_codes=(REASON_DUPLICATE_EXIT_IGNORED,))
            ledger = self._ledgers.get(str(lifecycle_id))
            if ledger is not None and ledger.is_consumed(key):
                # 幂等键可能已被 MAX_EXIT_KEYS 淘汰（`duplicate` 取不到），但
                # "这次退出已经成交"是不可回收的事实：必须仍然判为重复而不是
                # 重新占量提交（U02/A_r2 NEW-1 的另一条入口）。
                return ExitRequestOutcome(
                    exit_key=key, accepted=False, duplicate=True, action="NOOP",
                    side=side, submit_qty=Decimal(0), reduce_only=True,
                    reason_codes=(REASON_DUPLICATE_EXIT_IGNORED,))
            authoritative = decimal_from_value(current_qty)
            if authoritative < 0:
                authoritative = Decimal(0)
            if ledger is None:
                ledger = ManagementReservationLedger(
                    position_qty=authoritative, lifecycle_id=str(lifecycle_id))
                self._ledgers[str(lifecycle_id)] = ledger
            else:
                # 交易所快照是权威量：每轮以它校正仓位，绝不能沿用上一轮的旧值
                # （否则成交后的仓量收缩读不出来，可用量会凭空变大）。
                # 在途预留只在其超过新仓量时被释放，正常的在途退出不受影响。
                ledger.reconcile_position(authoritative)
            requested = decimal_from_value(qty)
            if requested < 0:
                requested = Decimal(0)
            available = ledger.available_qty
            reasons: list[str] = list(reclaimed)
            if requested > available:
                reasons.append(REASON_EXIT_EXCEEDS_POSITION)
            submit_qty = min(requested, available)
            if submit_qty <= 0:
                outcome = ExitRequestOutcome(
                    exit_key=key, accepted=False, duplicate=False, action="NOOP",
                    side=side, submit_qty=Decimal(0), reduce_only=True,
                    reason_codes=tuple(dict.fromkeys(
                        reasons + [REASON_NO_POSITION_TO_EXIT])))
                return outcome
            reservation = ledger.reserve(
                reservation_id=key, qty=submit_qty,
                now_ms=int(now_ms) or None)
            if not reservation.accepted:
                outcome = ExitRequestOutcome(
                    exit_key=key, accepted=False, duplicate=False, action="NOOP",
                    side=side, submit_qty=Decimal(0), reduce_only=True,
                    reason_codes=tuple(dict.fromkeys(
                        reasons + list(reservation.reason_codes)
                        + [REASON_RESERVATION_EXCEEDS_POSITION])))
                return outcome
            outcome = ExitRequestOutcome(
                exit_key=key, accepted=True, duplicate=False, action="SUBMIT_REDUCE",
                side=side, submit_qty=submit_qty, reduce_only=True,
                reason_codes=tuple(reasons), opens_position=False)
            self._exit_keys[key] = outcome
            self._exit_ledgers[key] = str(lifecycle_id)
            self._exit_registered_at[key] = int(now_ms)
            self._prune_exit_keys_locked()
            return outcome

    def _prune_exit_keys_locked(self) -> None:
        """淘汰最旧的**已终结**退出键；ACTIVE（在途）键永不淘汰。"""
        if len(self._exit_keys) > self.MAX_EXIT_KEYS:
            for key in list(self._exit_keys):
                if len(self._exit_keys) <= self.MAX_EXIT_KEYS:
                    break
                ledger = self._ledgers.get(self._exit_ledgers.get(key, ""))
                item = ledger.reservation(key) if ledger is not None else None
                if item is not None and item.status == "ACTIVE":
                    continue
                self._exit_keys.pop(key, None)
                self._exit_ledgers.pop(key, None)
                self._exit_registered_at.pop(key, None)
        if len(self._released_exits) > self.MAX_RELEASED_EXIT_AUDIT:
            del self._released_exits[:-self.MAX_RELEASED_EXIT_AUDIT]

    def _ledger_for_exit(self, exit_key: str) -> ManagementReservationLedger | None:
        with self._lock:
            lifecycle_id = self._exit_ledgers.get(str(exit_key))
            return self._ledgers.get(lifecycle_id) if lifecycle_id else None

    def _reclaim_if_stale_locked(
        self, exit_key: str, *, now_ms: int, ttl_ms: int | None,
    ) -> tuple[str, ...]:
        """回收同键下已超过 TTL 的在途预留；返回原因元组（未回收则为空）。

        只在调用方显式给出 TTL 时生效：没有 TTL 就没有"多旧算泄漏"的依据，
        严格幂等语义保持不变。**已 CONSUMED 的键永不回收**（那次退出确实成交了，
        重发就是二次减仓）——判据是账本里的**成交事实**
        （:meth:`ManagementReservationLedger.is_consumed`），不是"当前是否还有
        那条预留记录"：记录会被 ``MAX_TERMINAL_RESERVATIONS`` 淘汰，只看记录会
        让被淘汰的 CONSUMED 键落进下面的 "预留已不在账本里" 分支，在 TTL 到期
        后被回收并重发（U02/A_r2 NEW-1）。
        """
        if not ttl_ms or int(ttl_ms) <= 0 or int(now_ms) <= 0:
            return ()
        ledger = self._ledgers.get(self._exit_ledgers.get(exit_key, ""))
        if ledger is not None and ledger.is_consumed(exit_key):
            return ()
        reservation = ledger.reservation(exit_key) if ledger is not None else None
        registered = self._exit_registered_at.get(exit_key, 0)
        if reservation is not None and reservation.status == "CONSUMED":
            return ()
        if reservation is not None and reservation.status == "ACTIVE":
            created = reservation.created_at_ms or registered
            if created and int(now_ms) - created < int(ttl_ms):
                return ()
            if ledger is not None:
                ledger.release(exit_key, reason=REASON_STALE_RESERVATION_RECLAIMED)
        else:
            # 预留已不在账本里（账本被重建/条目丢失）：键残留同样会永久占死退出，
            # 只在登记时间明确超过 TTL 时回收。
            if not registered or int(now_ms) - registered < int(ttl_ms):
                return ()
        self._exit_keys.pop(exit_key, None)
        self._exit_ledgers.pop(exit_key, None)
        self._exit_registered_at.pop(exit_key, None)
        self._released_exits.append({
            "exit_key": exit_key, "reason": REASON_STALE_RESERVATION_RECLAIMED,
            "at_ms": int(now_ms)})
        return (REASON_STALE_RESERVATION_RECLAIMED,)

    def reclaim_stale_exits(self, *, now_ms: int, ttl_ms: int) -> tuple[str, ...]:
        """全量回收超过 TTL 的在途退出预留（运维/每轮巡检用），返回被回收的键。"""
        with self._lock:
            keys = list(self._exit_keys)
        reclaimed: list[str] = []
        for key in keys:
            with self._lock:
                if self._reclaim_if_stale_locked(
                        key, now_ms=int(now_ms), ttl_ms=int(ttl_ms)):
                    reclaimed.append(key)
        return tuple(reclaimed)

    def confirm_exit(
        self, exit_key: str, *, engaged_qty: Decimal | str | int | None = None,
    ) -> bool:
        """退出成交回执：扣减预留。重复确认不重复扣减。"""
        ledger = self._ledger_for_exit(exit_key)
        if ledger is None:
            return False
        return bool(ledger.consume(str(exit_key), engaged_qty=engaged_qty).accepted)

    def release_exit(self, exit_key: str, *, reason: str = "") -> bool:
        """释放预留（这次退出没有发生）；**释放后同键可以重发**。

        "释放"的语义是"该退出未执行/未确认"，如果继续把该键当作已受理，
        下一轮同一稳定 key（guardian 的 ``mta-g-<sha24>``）会被判为 ``duplicate``
        而永不重发——这正是"异常路径漏释放 -> 该标的永久不可退出"的第二半。
        已 CONSUMED（成交回执已到）的键**不在释放路径上**：账本会拒绝释放
        （``REASON_RESERVATION_ALREADY_CLOSED``），本方法随之返回 ``False`` 并
        **保留**该键的幂等记忆——"释放一笔已成交的退出"等于让它重新可重发
        （U02/A_r2 NEW-2）。
        """
        key = str(exit_key)
        ledger = self._ledger_for_exit(key)
        if ledger is None:
            return False
        if not bool(ledger.release(key, reason=reason).accepted):
            return False
        with self._lock:
            registered_at = self._exit_registered_at.pop(key, 0)
            self._exit_keys.pop(key, None)
            self._exit_ledgers.pop(key, None)
            self._released_exits.append({
                "exit_key": key, "reason": str(reason or ""),
                "registered_at_ms": registered_at})
            self._prune_exit_keys_locked()
        return True

    # -- R11：故障不得触发全平 --------------------------------------------- #
    def should_flat_close(
        self, *, fault: Any = "", operator_emergency: bool = False,
    ) -> FlatCloseDecision:
        """只有"已确证的保护失败"或**人工 emergency** 可以触发全平。

        健康/传输/限流/查单不确定/未归类故障一律拒绝（复用 U04 的故障分类，
        不复制判定表）。人工 emergency 语义保持：它由控制面显式触发，本函数
        不改变 pause/emergency 状态，只回答"这次全平是否被允许"。
        """
        if operator_emergency is True:
            return FlatCloseDecision(
                allowed=True, action="EMERGENCY_CLOSE", trigger="OPERATOR_EMERGENCY",
                reason_codes=("OPERATOR_EMERGENCY_CONFIRMED",))
        from exchange.protection_commands import (  # 延迟导入：避免 core→exchange 环
            FaultClass, ProtectionSafetyError, classify_execution_fault,
            guard_fault_action,
        )
        plan = classify_execution_fault(fault)
        if plan.fault_class is not FaultClass.PROTECTION:
            return FlatCloseDecision(
                allowed=False, action="ALERT_ONLY",
                trigger=getattr(plan, "fault", str(fault)),
                reason_codes=tuple(plan.reason_codes) or (
                    "HEALTH_FAULT_MUST_NOT_TRIGGER_FLAT_CLOSE",))
        try:
            guarded = guard_fault_action(fault, "CLOSE_ALL")
        except ProtectionSafetyError as exc:  # pragma: no cover - 分类表已挡住
            return FlatCloseDecision(
                allowed=False, action="ALERT_ONLY", trigger=str(fault),
                reason_codes=(str(exc),))
        return FlatCloseDecision(
            allowed=True, action=guarded.action, trigger=guarded.fault,
            reason_codes=tuple(guarded.reason_codes))

    def assert_no_implicit_bulk_exit(
        self, *, context: str, actions: Iterable[Mapping[str, Any]],
    ) -> None:
        """重启/对账上下文中出现批量退出动作即抛错（fail-closed 护栏）。"""
        ctx = str(context or "").lower()
        if ctx not in BULK_EXIT_CONTEXTS:
            return
        offenders: list[str] = []
        for action in actions or ():
            kind = str(action.get("action") or action.get("kind") or "").lower()
            if any(kind.startswith(prefix) for prefix in EXIT_ACTION_PREFIXES):
                offenders.append(str(action.get("symbol") or action.get("lifecycle_id") or kind))
        if offenders:
            raise ImplicitBulkExitError(ctx, offenders)

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_ref": self.account_ref,
            "environment": self.environment,
            "expectations": [item.as_dict() for item in self.expectations()],
            "hard_exits": [item.as_dict() for item in self.hard_exits()],
            "pending_legacy": [item.as_dict() for item in self.pending_legacy()],
            "migrations": [item.as_dict() for item in self.migrations()],
            "released_exits": [dict(item) for item in self._released_exits],
            "exit_key_count": len(self._exit_keys),
            "ledgers": {key: ledger.as_dict() for key, ledger in self._ledgers.items()},
        }
