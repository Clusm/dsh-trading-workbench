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
    "REASON_LEGACY_REQUIRES_MIGRATION",
    "REASON_NO_POSITION_TO_EXIT",
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
        return ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "actions": [item.as_dict() for item in self.actions],
            "legacy": [item.as_dict() for item in self.legacy],
            "reason_codes": list(self.reason_codes),
            "implicit_bulk_exit": self.implicit_bulk_exit,
            "exit_action_count": 0,
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
            for position in positions or ():
                record = self.classify_position(position)
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
            self.assert_no_implicit_bulk_exit(context="restart", actions=())
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
                reason=str(reason), migrated_at_ms=int(now_ms))
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
    ) -> ExitRequestOutcome:
        """注册一次退出请求；幂等、只减、方向固定，重复即 no-op。

        预留量在返回前即已占用，因此同一份仓量被两个并发退出请求同时读到
        "全部可用"的情形不会发生（R09）。提交量 = min(请求量, 可用量)，可用量
        为 0 时退化为 ``NO_POSITION_TO_EXIT`` 的 no-op，**不会**产生负仓/反向开仓。
        """
        key = str(exit_key)
        side = exit_side_for(position_side)
        with self._lock:
            duplicate = self._exit_keys.get(key)
            if duplicate is not None:
                return ExitRequestOutcome(
                    exit_key=key, accepted=False, duplicate=True, action="NOOP",
                    side=duplicate.side, submit_qty=Decimal(0), reduce_only=True,
                    reason_codes=(REASON_DUPLICATE_EXIT_IGNORED,))
            ledger = self._ledgers.get(str(lifecycle_id))
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
            reasons: list[str] = []
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
            reservation = ledger.reserve(reservation_id=key, qty=submit_qty)
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
            return outcome

    def _ledger_for_exit(self, exit_key: str) -> ManagementReservationLedger | None:
        with self._lock:
            lifecycle_id = self._exit_ledgers.get(str(exit_key))
            return self._ledgers.get(lifecycle_id) if lifecycle_id else None

    def confirm_exit(
        self, exit_key: str, *, engaged_qty: Decimal | str | int | None = None,
    ) -> bool:
        """退出成交回执：扣减预留。重复确认不重复扣减。"""
        ledger = self._ledger_for_exit(exit_key)
        if ledger is None:
            return False
        return bool(ledger.consume(str(exit_key), engaged_qty=engaged_qty).accepted)

    def release_exit(self, exit_key: str, *, reason: str = "") -> bool:
        ledger = self._ledger_for_exit(exit_key)
        if ledger is None:
            return False
        return bool(ledger.release(str(exit_key), reason=reason).accepted)

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
            "ledgers": {key: ledger.as_dict() for key, ledger in self._ledgers.items()},
        }
