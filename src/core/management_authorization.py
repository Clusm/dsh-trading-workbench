"""U02 有界退出授权：K04 授权 + U15 运行身份绑定 + 并发退出的预留守恒。

本模块是 **组合层**，不是第二套判定器：

* 提交许可的真值仍来自契约 :func:`core.contracts.k04_management.validate_authorization`，
  本模块只把它的结论与"运行身份绑定（U15）"和"在途退出预留（R09）"拼接起来；
* 账户权威域只有一个：U15 ``RuntimeGeneration.account_ref``（``<env>:<declared>``
  或 ``<env>:local``）。K03 收入幂等键、订单/生命周期分区使用的 ``account_ref``
  必须与它逐字相等，否则同一进程内会有两个账户标识（U15/B_r1 P1-4）。
  :func:`assert_account_domains_consistent` 把这个不一致变成**可见的 reason**，
  而不是让它静默漂移。

不做的事（与 U04 边界一致）：不发起网络请求、不构造 Broker、不读 ``.env``/``data/``、
不下单撤单、不改风险档，也不改动 pause/emergency 语义（本模块只读取控制位）。

两条硬不变量：

* **过期授权不阻断 UNKNOWN 查单/收口**。``reconcile_allowed`` 只由账户/生命周期/
  方向域决定（K04 口径），授权过期只让 ``submit_allowed`` 为假；拒绝新管理动作与
  继续按原 client id 查单是两件事（规格 §10.2 / K04-N7）。
* **只减不增**。任何管理请求的 ``requested_net_delta`` 必须 ``<= 0``（契约已强制），
  预留账本另外保证 ``Σ(在途预留) + 可用量 == 当前仓位量``，重复的退出请求既不
  重复消耗仓量，也不可能被解释成反向开仓。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.contracts import (
    AuthorizationVerdict,
    ManagementAuthorization,
    decimal_from_value,
    validate_authorization,
)
from core.contracts.k04_management import ProtectionTarget
from core.runtime_identity import (
    RUNTIME_GENERATION_MALFORMED,
    RUNTIME_GENERATION_UNPROVEN,
    RuntimeGeneration,
    verify_account_binding,
)

# --------------------------------------------------------------------------- #
# reason codes（U02 新增，全部只影响"新管理动作"，不影响按原 id 的查单收口）
# --------------------------------------------------------------------------- #
REASON_ACCOUNT_DOMAIN_MISMATCH = "ACCOUNT_DOMAIN_MISMATCH"
REASON_AUTHORIZATION_UNBOUND = "MANAGEMENT_AUTHORIZATION_UNBOUND"
REASON_AUTHORIZATION_MALFORMED = "MANAGEMENT_AUTHORIZATION_MALFORMED"

REASON_RESERVATION_EXCEEDS_POSITION = "RESERVATION_EXCEEDS_POSITION"
REASON_RESERVATION_UNKNOWN = "RESERVATION_UNKNOWN"
REASON_RESERVATION_ALREADY_CLOSED = "RESERVATION_ALREADY_CONSUMED"
REASON_RESERVATION_CONSUME_EXCEEDS = "RESERVATION_CONSUME_EXCEEDS_RESERVED"
REASON_RESERVATION_NON_POSITIVE = "RESERVATION_NON_POSITIVE_QUANTITY"
REASON_RESERVATION_DUPLICATE = "DUPLICATE_RESERVATION_IGNORED"
REASON_RESERVATION_STALE = "RESERVATION_RELEASED_POSITION_SHRUNK"
#: 在途预留超过 TTL 仍未结算 -> 判定为泄漏并回收（关键：不回收就等于该标的
#: 的可用量被永久占死，退出通道静默失效。见 U02/A_r1 P0-1）。
REASON_RESERVATION_RECLAIMED = "STALE_EXIT_RESERVATION_RECLAIMED"

REASON_REDUCE_ONLY_VIOLATED = "REDUCE_ONLY_VIOLATED"

__all__ = [
    "ManagementAccountDomain",
    "ManagementAuthorizationError",
    "ManagementDecision",
    "ManagementReservation",
    "ManagementReservationLedger",
    "ReservationOutcome",
    "assert_account_domains_consistent",
    "reservation_conserved",
    "evaluate_management_request",
    "issue_management_authorization",
    "ledger_account_ref",
    "reduce_only_guard",
    "resolve_account_domain",
    "REASON_ACCOUNT_DOMAIN_MISMATCH",
    "REASON_AUTHORIZATION_MALFORMED",
    "REASON_AUTHORIZATION_UNBOUND",
    "REASON_REDUCE_ONLY_VIOLATED",
    "REASON_RESERVATION_ALREADY_CLOSED",
    "REASON_RESERVATION_CONSUME_EXCEEDS",
    "REASON_RESERVATION_DUPLICATE",
    "REASON_RESERVATION_EXCEEDS_POSITION",
    "REASON_RESERVATION_NON_POSITIVE",
    "REASON_RESERVATION_RECLAIMED",
    "REASON_RESERVATION_STALE",
    "REASON_RESERVATION_UNKNOWN",
]


class ManagementAuthorizationError(ValueError):
    """管理授权在本模块边界上不可构造/不可使用（fail-closed）。"""

    def __init__(self, reason_codes: Sequence[str], message: str = "") -> None:
        self.reason_codes = tuple(reason_codes)
        super().__init__(message or "; ".join(self.reason_codes))


# --------------------------------------------------------------------------- #
# 1. 账户域：U15 运行代是唯一权威来源
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ManagementAccountDomain:
    """一次管理动作的账户绑定域；``bound`` 为假即不得提交任何管理动作。"""

    environment: str
    account_ref: str
    runtime_instance_id: str
    generation_id: str
    stable: bool
    source: str
    reason_codes: tuple[str, ...] = ()

    @property
    def bound(self) -> bool:
        return not self.reason_codes

    def as_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "account_ref": self.account_ref,
            "runtime_instance_id": self.runtime_instance_id,
            "generation_id": self.generation_id,
            "stable": self.stable,
            "source": self.source,
            "bound": self.bound,
            "reason_codes": list(self.reason_codes),
        }


def ledger_account_ref(generation: RuntimeGeneration | Any) -> str:
    """账本/K03 幂等键必须使用的 ``account_ref`` = 运行代的派生值。

    这是把"账本侧 account_ref 仍由 API key 派生"这条漂移收口的唯一出口：
    消费方从这里取值，而不是自己 ``sha256(api_key)``。
    """
    if not isinstance(generation, RuntimeGeneration):
        raise ManagementAuthorizationError(
            [REASON_AUTHORIZATION_UNBOUND], "generation must be a RuntimeGeneration")
    return str(generation.account_ref or "")


def resolve_account_domain(
    generation: RuntimeGeneration | Any,
    expected: RuntimeGeneration | Any | None = None,
    *,
    environment: str | None = None,
    account_ref: str | None = None,
    runtime_instance_id: str | None = None,
    scope_hash: str | None = None,
) -> ManagementAccountDomain:
    """把 U15 的绑定验证收敛成管理动作可直接消费的账户域。

    ``expected`` 省略时以 ``generation`` 自身为期望（"我只要求它自洽"），显式
    传入时逐项比对（调用方通常传入授权记录里绑定的那个运行代）。运行代缺失/
    形状不对一律 fail-closed，不抛裸异常。
    """
    if generation is None:
        return ManagementAccountDomain(
            "", "", "", "", False, "unbound", (RUNTIME_GENERATION_UNPROVEN,))
    if not isinstance(generation, RuntimeGeneration):
        return ManagementAccountDomain(
            "", "", "", "", False, "malformed", (RUNTIME_GENERATION_MALFORMED,))
    target = generation if expected is None else expected
    if not isinstance(target, RuntimeGeneration):
        return ManagementAccountDomain(
            "", "", "", "", False, "malformed", (RUNTIME_GENERATION_MALFORMED,))
    binding = verify_account_binding(
        target, generation, environment=environment, account_ref=account_ref,
        runtime_instance_id=runtime_instance_id, scope_hash=scope_hash,
    )
    reasons = list(binding.reason_codes)
    if not str(generation.runtime_instance_id or "").strip():
        reasons.append("RUNTIME_INSTANCE_UNBOUND")
    if not str(generation.environment or "").strip():
        reasons.append("ENVIRONMENT_UNBOUND")
    return ManagementAccountDomain(
        environment=str(generation.environment or ""),
        account_ref=str(generation.account_ref or ""),
        runtime_instance_id=str(generation.runtime_instance_id or ""),
        generation_id=str(generation.generation_id or ""),
        stable=bool(getattr(generation, "account_ref_stable", False)),
        source=str(getattr(generation, "account_ref_source", "") or "generation"),
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def assert_account_domains_consistent(
    generation: RuntimeGeneration | Any, ledger_ref: str | None,
) -> tuple[str, ...]:
    """身份域与账本域的逐字一致性；不一致即 ``ACCOUNT_DOMAIN_MISMATCH``。

    "不一致"必须**可见**：它意味着 K03 幂等键分区（``environment,account_ref,...``）
    与决策身份指向两个不同的账户，收入归集与生命周期归属会对不上。本函数只返回
    reason，不做任何写操作，供启动自检/看板展示。
    """
    expected = ledger_account_ref(generation) if isinstance(
        generation, RuntimeGeneration) else ""
    actual = str(ledger_ref or "").strip()
    if not expected or not actual:
        return ("ACCOUNT_DOMAIN_UNPROVEN",) if expected != actual else ()
    return () if expected == actual else (REASON_ACCOUNT_DOMAIN_MISMATCH,)


# --------------------------------------------------------------------------- #
# 2. 授权签发与核验
# --------------------------------------------------------------------------- #
def issue_management_authorization(
    *,
    authorization_id: str,
    generation: RuntimeGeneration | Any,
    lifecycle_id: str,
    position_side: str,
    max_reduce_qty: Decimal | str | int,
    position_revision: int,
    nonce: str,
    issued_at_ms: int,
    ttl_ms: int,
    target_protection: ProtectionTarget | None = None,
) -> ManagementAuthorization:
    """签发绑定运行代/环境/账户的管理授权；运行代或数量不合法即拒绝构造。

    K04 要求授权必须携带 ``environment`` 与 ``runtime_instance_id``，两者缺失
    即 ``RUN_GENERATION_UNBOUND``/``ENVIRONMENT_UNBOUND``（fail-closed）。这里
    在签发侧就挡住，避免把一个"注定被拒"的授权写进审计再让运行期去发现。
    """
    reasons: list[str] = []
    if not isinstance(generation, RuntimeGeneration):
        reasons.append(REASON_AUTHORIZATION_UNBOUND)
        account_ref = ""
        environment = None
        instance = None
    else:
        account_ref = str(generation.account_ref or "")
        environment = str(generation.environment or "") or None
        instance = str(generation.runtime_instance_id or "") or None
        if instance is None:
            reasons.append("RUNTIME_INSTANCE_UNBOUND")
        if environment is None:
            reasons.append("ENVIRONMENT_UNBOUND")
        if not account_ref:
            reasons.append("ACCOUNT_REF_UNPROVEN")
    try:
        max_qty = decimal_from_value(max_reduce_qty)
    except ValueError:
        reasons.append("MAX_REDUCE_QTY_INVALID")
        max_qty = Decimal(0)
    if max_qty <= 0:
        reasons.append("MAX_REDUCE_QTY_NON_POSITIVE")
    if int(ttl_ms) <= 0:
        reasons.append("AUTHORIZATION_TTL_NON_POSITIVE")
    if int(issued_at_ms) <= 0:
        reasons.append("AUTHORIZATION_ISSUED_AT_INVALID")
    if reasons:
        raise ManagementAuthorizationError(reasons, "management authorization not issuable")
    return ManagementAuthorization(
        authorization_id=str(authorization_id),
        account_ref=account_ref,
        lifecycle_id=str(lifecycle_id),
        position_side=str(position_side).upper(),  # type: ignore[arg-type]
        max_reduce_qty=max_qty,
        target_protection=target_protection or ProtectionTarget(),
        position_revision=int(position_revision),
        nonce=str(nonce),
        issued_at_ms=int(issued_at_ms),
        expires_at_ms=int(issued_at_ms) + int(ttl_ms),
        environment=environment,
        runtime_instance_id=instance,
    )


@dataclass(frozen=True)
class ManagementDecision:
    """管理请求的最终判定；提交与收口是两条独立的许可。"""

    allowed: bool
    submit_allowed: bool
    reconcile_allowed: bool
    reason_codes: tuple[str, ...] = ()
    binding_reason_codes: tuple[str, ...] = ()
    verdict: AuthorizationVerdict | None = None

    @property
    def submit_reason_codes(self) -> tuple[str, ...]:
        return self.reason_codes

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "submit_allowed": self.submit_allowed,
            "reconcile_allowed": self.reconcile_allowed,
            "reason_codes": list(self.reason_codes),
            "binding_reason_codes": list(self.binding_reason_codes),
        }


def evaluate_management_request(
    authorization: ManagementAuthorization | Any,
    *,
    request: Mapping[str, Any],
    now_ms: int,
    current_revision: int,
    consumed_nonces: Sequence[str] = (),
    generation: RuntimeGeneration | Any | None = None,
    expected_generation: RuntimeGeneration | Any | None = None,
    scope_hash: str | None = None,
) -> ManagementDecision:
    """组合 K04 契约判定与 U15 身份绑定，返回提交/收口两路许可。

    * ``reconcile_allowed`` **只**取契约结论（账户/生命周期/方向域），因此授权
      过期、运行代不匹配都不会阻断按原 client id 的 UNKNOWN 查单与收口；
    * 运行代/环境/scope 绑定失败只追加到``binding_reason_codes``，令
      ``submit_allowed`` 为假——新增管理动作被挡住，既有订单的收口照常。
    """
    if not isinstance(authorization, ManagementAuthorization):
        return ManagementDecision(
            False, False, False,
            (REASON_AUTHORIZATION_MALFORMED,), (REASON_AUTHORIZATION_MALFORMED,), None)
    verdict = validate_authorization(
        authorization, request=request, now_ms=int(now_ms),
        current_revision=int(current_revision), consumed_nonces=tuple(consumed_nonces),
    )
    binding_reasons: list[str] = []
    if generation is not None or expected_generation is not None:
        domain = resolve_account_domain(
            generation if generation is not None else expected_generation,
            expected_generation if generation is not None else None,
            environment=authorization.environment,
            account_ref=authorization.account_ref,
            runtime_instance_id=authorization.runtime_instance_id,
            scope_hash=scope_hash,
        )
        binding_reasons.extend(domain.reason_codes)
    binding = tuple(dict.fromkeys(binding_reasons))
    combined = tuple(dict.fromkeys(tuple(verdict.reason_codes) + binding))
    allowed = bool(verdict.allowed) and not binding
    return ManagementDecision(
        allowed=allowed,
        submit_allowed=allowed,
        # 契约已按"账户/生命周期/方向域"判定；过期与运行代不参与，故不被阻断。
        reconcile_allowed=bool(verdict.reconcile_allowed),
        reason_codes=combined,
        binding_reason_codes=binding,
        verdict=verdict,
    )


def reduce_only_guard(request: Mapping[str, Any]) -> tuple[str, ...]:
    """管理请求的净敞口方向守卫：``requested_net_delta > 0`` 即反向开仓。"""
    raw = request.get("requested_net_delta")
    if raw in (None, ""):
        return ()
    try:
        delta = decimal_from_value(raw)
    except ValueError:
        return ()
    return (REASON_REDUCE_ONLY_VIOLATED,) if delta > 0 else ()


# --------------------------------------------------------------------------- #
# 3. 并发退出的预留账本（R09）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ManagementReservation:
    """一笔在途减仓/退出的数量预留。"""

    reservation_id: str
    qty: Decimal
    consumed_qty: Decimal = Decimal(0)
    status: str = "ACTIVE"  # ACTIVE | CONSUMED | RELEASED
    reason_code: str = ""
    #: 预留建立时刻（毫秒）。用于"在途预留超时回收"；0 表示未记录（不参与回收）。
    created_at_ms: int = 0

    @property
    def remaining_qty(self) -> Decimal:
        return self.qty - self.consumed_qty

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "qty": str(self.qty),
            "consumed_qty": str(self.consumed_qty),
            "remaining_qty": str(self.remaining_qty),
            "status": self.status,
            "reason_code": self.reason_code,
            "created_at_ms": self.created_at_ms,
        }


@dataclass(frozen=True)
class ReservationOutcome:
    accepted: bool
    duplicate: bool
    reservation_id: str
    reserved_qty: Decimal
    consumed_qty: Decimal
    available_qty: Decimal
    reason_codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "duplicate": self.duplicate,
            "reservation_id": self.reservation_id,
            "reserved_qty": str(self.reserved_qty),
            "consumed_qty": str(self.consumed_qty),
            "available_qty": str(self.available_qty),
            "reason_codes": list(self.reason_codes),
        }


class ManagementReservationLedger:
    """单账户/单仓位域内的退出预留账本；只减不增、并发原子。

    不变量：``position_qty == Σ(ACTIVE 预留的 remaining) + available_qty``，且
    ``available_qty >= 0``。所有变更都在同一把锁内完成，因此并发到达的退出请求
    不会各自看到"尚未被预留"的同一份仓量（R09：在途 TP/退出不得重复消耗仓量）。
    """

    #: 终结预留（CONSUMED/RELEASED）的保留上限：长进程里每次退出都会新增一条
    #: 记录，不设上限就是内存泄漏。**只淘汰终结条目**，ACTIVE（在途）永不淘汰，
    #: 因此守恒式与"在途不重复占量"不受影响。
    MAX_TERMINAL_RESERVATIONS = 2000

    def __init__(
        self,
        *,
        position_qty: Decimal | str | int = 0,
        lifecycle_id: str = "",
        lock: threading.Lock | None = None,
    ) -> None:
        self.position_qty = decimal_from_value(position_qty)
        if self.position_qty < 0:
            raise ManagementAuthorizationError(
                [REASON_RESERVATION_NON_POSITIVE], "position_qty must be >= 0")
        self.lifecycle_id = str(lifecycle_id or "")
        self._reservations = {}
        #: **成交事实**：曾经被 ``consume`` 确认成交的 reservation id。
        #:
        #: 它必须与 ``_reservations`` 分开存放，因为后者会被
        #: :meth:`_prune_locked` 淘汰（内存有界），而"这笔退出已经成交"是不可
        #: 回收的事实：一旦随记录一起丢失，同一个退出键就能被再次 ``reserve``
        #: 并重新提交——那是**二次减仓**（U02/A_r2 NEW-1/NEW-2）。所以这里用
        #: 一个只增不淘汰的 id 集合保存事实：单个 id 只是一个短字符串，比整条
        #: ``ManagementReservation`` 记录轻得多；这是"已成交的退出永不可重开"
        #: 这条硬不变量的代价（见 :meth:`is_consumed`）。
        self._consumed_ids: set[str] = set()
        self._lock = lock or threading.Lock()

    def _prune_locked(self) -> None:
        """淘汰最旧的**终结**预留，保持内存有界（插入序 = 迭代序）。"""
        if len(self._reservations) <= self.MAX_TERMINAL_RESERVATIONS:
            return
        for rid in list(self._reservations):
            if len(self._reservations) <= self.MAX_TERMINAL_RESERVATIONS:
                break
            if self._reservations[rid].status == "ACTIVE":
                continue
            del self._reservations[rid]

    # -- 读侧 -------------------------------------------------------------- #
    @property
    def reserved_qty(self) -> Decimal:
        with self._lock:
            return self._reserved_qty_locked()

    def _reserved_qty_locked(self) -> Decimal:
        return sum(
            (item.remaining_qty for item in self._reservations.values()
             if item.status == "ACTIVE"), Decimal(0))

    @property
    def available_qty(self) -> Decimal:
        with self._lock:
            return self._available_locked()

    def _available_locked(self) -> Decimal:
        available = self.position_qty - self._reserved_qty_locked()
        return available if available > 0 else Decimal(0)

    def active_reservations(self) -> tuple[ManagementReservation, ...]:
        with self._lock:
            return tuple(sorted(
                (item for item in self._reservations.values() if item.status == "ACTIVE"),
                key=lambda item: item.reservation_id))

    def reservation(self, reservation_id: str) -> ManagementReservation | None:
        with self._lock:
            return self._reservations.get(str(reservation_id))

    def is_consumed(self, reservation_id: str) -> bool:
        """该 ID 是否**曾经**被确认成交（即使它的预留记录已被淘汰）。

        这是"已 CONSUMED 的退出永不回收/永不重开"的权威依据：调用方必须查
        **成交事实**而不是"当前是否还有那条记录"——记录会被
        :meth:`_prune_locked` 淘汰，事实不会。
        """
        with self._lock:
            return str(reservation_id or "") in self._consumed_ids

    @property
    def consumed_id_count(self) -> int:
        """已成交 id 的基数（运维可见性：它只增不淘汰，需要能被观测）。"""
        with self._lock:
            return len(self._consumed_ids)

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "lifecycle_id": self.lifecycle_id,
                "position_qty": str(self.position_qty),
                "reserved_qty": str(self._reserved_qty_locked()),
                "available_qty": str(self._available_locked()),
                # 成交事实只增不淘汰，单独暴露基数供运维观测（它不参与守恒式）。
                "consumed_id_count": len(self._consumed_ids),
                "reservations": [item.as_dict() for item in self._reservations.values()],
            }

    # -- 写侧 -------------------------------------------------------------- #
    def reconcile_position(self, position_qty: Decimal | str | int) -> tuple[str, ...]:
        """以交易所权威量校正仓位；缩小到低于已预留量时把溢出预留转为 RELEASED。

        这一步让"部分成交/对账后仓位变小"不会留下超额预留，也不会把可用量算成
        负数——宁可释放预留并**显式告警**，也不允许预留凭空扩张仓位。
        """
        target = decimal_from_value(position_qty)
        if target < 0:
            target = Decimal(0)
        with self._lock:
            self.position_qty = target
            reasons: list[str] = []
            overflow = self._reserved_qty_locked() - target
            if overflow > 0:
                reasons.append(REASON_RESERVATION_STALE)
                for key, item in list(self._reservations.items()):
                    if overflow <= 0:
                        break
                    if item.status != "ACTIVE":
                        continue
                    take = min(item.remaining_qty, overflow)
                    self._reservations[key] = ManagementReservation(
                        reservation_id=item.reservation_id,
                        qty=item.qty,
                        consumed_qty=item.qty - (item.remaining_qty - take),
                        status="ACTIVE" if item.remaining_qty - take > 0 else "RELEASED",
                        reason_code=REASON_RESERVATION_STALE if item.remaining_qty - take <= 0
                        else item.reason_code,
                        created_at_ms=item.created_at_ms,
                    )
                    overflow -= take
            self._prune_locked()
            return tuple(reasons)

    def reserve(
        self, *, reservation_id: str, qty: Decimal | str | int,
        now_ms: int | None = None,
    ) -> ReservationOutcome:
        """预留退出量；重复 ID 幂等（不二次占用），超额即拒绝且不改变任何状态。

        **已 RELEASED 的 ID 可以重新预留**（释放的语义是"这次退出没有发生"，
        因此同一个退出键必须能重发）；已 CONSUMED 的 ID 永远不可重开——这条
        依据的是 :attr:`_consumed_ids` 里的**成交事实**，因此**预留记录被淘汰
        之后依然成立**（U02/A_r2 NEW-1：只看当前记录会让同键在 TTL 后被回收并
        重发，等于二次减仓）。
        """
        rid = str(reservation_id or "")
        with self._lock:
            existing = self._reservations.get(rid)
            if rid in self._consumed_ids:
                return ReservationOutcome(
                    accepted=False, duplicate=True, reservation_id=rid,
                    reserved_qty=self._reserved_qty_locked(),
                    consumed_qty=existing.consumed_qty if existing else Decimal(0),
                    available_qty=self._available_locked(),
                    reason_codes=(REASON_RESERVATION_ALREADY_CLOSED,),
                )
            if existing is not None and existing.status != "RELEASED":
                return ReservationOutcome(
                    accepted=False, duplicate=True, reservation_id=rid,
                    reserved_qty=self._reserved_qty_locked(),
                    consumed_qty=existing.consumed_qty,
                    available_qty=self._available_locked(),
                    reason_codes=(REASON_RESERVATION_DUPLICATE,),
                )
            try:
                amount = decimal_from_value(qty)
            except ValueError:
                return self._rejected(rid, REASON_RESERVATION_NON_POSITIVE, existing=None)
            if amount <= 0:
                return self._rejected(rid, REASON_RESERVATION_NON_POSITIVE, existing=None)
            if amount > self._available_locked():
                return self._rejected(
                    rid, REASON_RESERVATION_EXCEEDS_POSITION, existing=None)
            self._reservations[rid] = ManagementReservation(
                reservation_id=rid, qty=amount,
                created_at_ms=int(now_ms) if now_ms is not None else 0)
            self._prune_locked()
            return ReservationOutcome(
                accepted=True, duplicate=False, reservation_id=rid,
                reserved_qty=self._reserved_qty_locked(), consumed_qty=Decimal(0),
                available_qty=self._available_locked(),
            )

    def reclaim_stale(
        self, *, now_ms: int, ttl_ms: int, reason: str = REASON_RESERVATION_RECLAIMED,
    ) -> tuple[str, ...]:
        """回收超过 TTL 仍在途的预留；返回被回收的 ID（可审计）。

        这是"预留泄漏 -> 该标的永久不可退出"的**最后一道防线**：正常路径在
        finally 里结算/释放，本方法只处理"结算与释放都没跑到"的残留。TTL 必须
        显著大于一次提交+等待成交的时间，否则会误伤真正在途的退出。
        """
        if int(ttl_ms) <= 0 or int(now_ms) <= 0:
            return ()
        deadline = int(now_ms) - int(ttl_ms)
        reclaimed: list[str] = []
        with self._lock:
            for rid, item in list(self._reservations.items()):
                if item.status != "ACTIVE" or item.created_at_ms <= 0:
                    continue
                if item.created_at_ms > deadline:
                    continue
                self._reservations[rid] = ManagementReservation(
                    reservation_id=item.reservation_id, qty=item.qty,
                    consumed_qty=item.consumed_qty, status="RELEASED",
                    reason_code=str(reason), created_at_ms=item.created_at_ms)
                reclaimed.append(rid)
        return tuple(reclaimed)

    def _rejected(
        self, rid: str, reason: str, *, existing: ManagementReservation | None,
    ) -> ReservationOutcome:
        return ReservationOutcome(
            accepted=False, duplicate=False, reservation_id=rid,
            reserved_qty=self._reserved_qty_locked(),
            consumed_qty=existing.consumed_qty if existing else Decimal(0),
            available_qty=self._available_locked(), reason_codes=(reason,),
        )

    def consume(
        self, reservation_id: str, *, engaged_qty: Decimal | str | int | None = None,
    ) -> ReservationOutcome:
        """确认一笔退出实际扣减的仓量；同一预留重复 consume 不重复扣减。

        ``engaged_qty`` 省略表示按预留全额扣减。超出预留量即拒绝，绝不"多减"
        从而把仓位推成负值（负仓在交易所语义里就是反向开仓）。
        """
        rid = str(reservation_id or "")
        with self._lock:
            item = self._reservations.get(rid)
            if item is None:
                return self._rejected(rid, REASON_RESERVATION_UNKNOWN, existing=None)
            if item.status != "ACTIVE":
                return ReservationOutcome(
                    accepted=False, duplicate=False, reservation_id=rid,
                    reserved_qty=self._reserved_qty_locked(),
                    consumed_qty=item.consumed_qty, available_qty=self._available_locked(),
                    reason_codes=(REASON_RESERVATION_ALREADY_CLOSED,),
                )
            if engaged_qty is None:
                take = item.remaining_qty
            else:
                try:
                    take = decimal_from_value(engaged_qty)
                except ValueError:
                    take = Decimal(-1)
            if take <= 0 or take > item.remaining_qty:
                return ReservationOutcome(
                    accepted=False, duplicate=False, reservation_id=rid,
                    reserved_qty=self._reserved_qty_locked(),
                    consumed_qty=item.consumed_qty, available_qty=self._available_locked(),
                    reason_codes=(REASON_RESERVATION_CONSUME_EXCEEDS,),
                )
            consumed = item.consumed_qty + take
            status = "CONSUMED" if consumed >= item.qty else "ACTIVE"
            if status == "CONSUMED":
                # 先落"成交事实"，再落记录：即使随后 _prune_locked 把这条记录
                # 淘汰掉，该 ID 也不可能被重新 reserve / 被 TTL 回收重发。
                self._consumed_ids.add(rid)
            self._reservations[rid] = ManagementReservation(
                reservation_id=item.reservation_id, qty=item.qty, consumed_qty=consumed,
                status=status,
                created_at_ms=item.created_at_ms,
            )
            self._prune_locked()
            return ReservationOutcome(
                accepted=True, duplicate=False, reservation_id=rid,
                reserved_qty=self._reserved_qty_locked(), consumed_qty=consumed,
                available_qty=self._available_locked(),
            )

    def release(self, reservation_id: str, *, reason: str = "") -> ReservationOutcome:
        """释放预留（退出未执行/被拒）；对已终结预留幂等。

        **已 CONSUMED 的预留不可被释放**：那笔退出已经成交，"释放"等于撤销一笔
        已确认的成交，并让同一个退出键重新变成可重发（U02/A_r2 NEW-2）。因此这里
        返回 ``REASON_RESERVATION_ALREADY_CLOSED`` 且**不改变任何状态**；记录已被
        淘汰时同样按 :attr:`_consumed_ids` 里的成交事实拒绝。
        """
        rid = str(reservation_id or "")
        with self._lock:
            item = self._reservations.get(rid)
            if item is None:
                if rid in self._consumed_ids:
                    return ReservationOutcome(
                        accepted=False, duplicate=True, reservation_id=rid,
                        reserved_qty=self._reserved_qty_locked(),
                        consumed_qty=Decimal(0),
                        available_qty=self._available_locked(),
                        reason_codes=(REASON_RESERVATION_ALREADY_CLOSED,),
                    )
                return self._rejected(rid, REASON_RESERVATION_UNKNOWN, existing=None)
            if item.status in ("RELEASED", "CONSUMED"):
                return ReservationOutcome(
                    accepted=False, duplicate=True, reservation_id=rid,
                    reserved_qty=self._reserved_qty_locked(),
                    consumed_qty=item.consumed_qty, available_qty=self._available_locked(),
                    reason_codes=(REASON_RESERVATION_ALREADY_CLOSED,),
                )
            self._reservations[rid] = ManagementReservation(
                reservation_id=item.reservation_id, qty=item.qty,
                consumed_qty=item.consumed_qty, status="RELEASED",
                reason_code=str(reason or item.reason_code),
                created_at_ms=item.created_at_ms,
            )
            self._prune_locked()
            return ReservationOutcome(
                accepted=True, duplicate=False, reservation_id=rid,
                reserved_qty=self._reserved_qty_locked(), consumed_qty=item.consumed_qty,
                available_qty=self._available_locked(),
            )


def reservation_conserved(ledger: ManagementReservationLedger) -> bool:
    """守恒式自检：``Σ(ACTIVE.remaining) + available == position_qty``。"""
    snapshot = ledger.as_dict()
    total = Decimal(snapshot["reserved_qty"]) + Decimal(snapshot["available_qty"])
    return total == Decimal(snapshot["position_qty"])
