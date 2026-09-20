"""K04 管理意图、管理授权与订单数量三分量契约。

管理幂等键由生命周期、触发类型、规则版本与触发事件确定，跨轮稳定；
旧 revision 授权自动失效。数量统一三分量：
``accepted = cumulative_filled + working_leaves + terminal_unfilled``。
撤单确认前 remaining 不得计入 terminal；终态迟到 fill 仍修订累计成交。
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .common import SCHEMA_VERSION, MoneyDecimal, canonical_hash, decimal_from_value
from .k01_identity import RuntimeIdentity

REASON_OPEN_NOT_ALLOWED = "OPEN_NOT_ALLOWED"
REASON_ACTION_NOT_MANAGEMENT = "ACTION_NOT_MANAGEMENT"
REASON_AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
REASON_STALE_REVISION = "STALE_POSITION_REVISION"
REASON_ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
REASON_LIFECYCLE_MISMATCH = "LIFECYCLE_MISMATCH"
REASON_SIDE_MISMATCH = "SIDE_MISMATCH"
REASON_REDUCE_EXCEEDS = "REDUCE_EXCEEDS_AUTHORIZATION"
REASON_INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
REASON_NET_EXPOSURE_INCREASE = "NET_EXPOSURE_INCREASE"
REASON_LAST_PROTECTION = "CANNOT_REMOVE_LAST_PROTECTION"

VIOLATION_NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
VIOLATION_FILLED_EXCEEDS_ACCEPTED = "FILLED_EXCEEDS_ACCEPTED"
VIOLATION_CONSERVATION = "THREE_COMPONENT_CONSERVATION_VIOLATION"
VIOLATION_TERMINAL_WHILE_WORKING = "TERMINAL_NONZERO_WHILE_WORKING"
VIOLATION_WORKING_WHILE_TERMINAL = "WORKING_NONZERO_WHILE_TERMINAL"
VIOLATION_FILLED_WITH_RESIDUAL = "FILLED_STATUS_WITH_RESIDUAL"
VIOLATION_UNCONFIRMED_CANCEL_TERMINAL = "CANCEL_UNCONFIRMED_TERMINAL_COUNTED"

MANAGEMENT_ACTIONS = ("CLOSE", "REDUCE", "MAINTAIN_PROTECTION")
WORKING_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
TERMINAL_STATUSES = frozenset({"CANCELED", "EXPIRED", "REJECTED"})


class QuantityError(ValueError):
    """数量三分量守恒或状态规则被违反。"""


class ManagementIntent(BaseModel):
    """有界管理意图；OPEN 不属于管理动作，不能出现在此契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    management_id: str
    identity: RuntimeIdentity
    lifecycle_id: str
    position_side: Literal["LONG", "SHORT"]
    action: Literal["CLOSE", "REDUCE", "MAINTAIN_PROTECTION"]
    max_reduce_qty: MoneyDecimal
    position_revision: int = Field(ge=0)
    reason_code: str
    policy_version: str
    expires_at_ms: int
    evidence_id: str
    trigger_type: str
    trigger_event_id: str
    schema_version: str = SCHEMA_VERSION


def management_idempotency_key(
    *,
    lifecycle_id: str,
    trigger_type: str,
    policy_version: str,
    trigger_event_id: str,
) -> str:
    """稳定幂等键：生命周期 + 触发类型 + 规则版本 + 触发事件。"""
    return "|".join((lifecycle_id, trigger_type, policy_version, trigger_event_id))


def derive_management_id(
    *,
    lifecycle_id: str,
    trigger_type: str,
    policy_version: str,
    trigger_event_id: str,
) -> str:
    """由幂等键派生的确定性 management_id；跨轮不得随机变化。"""
    key = management_idempotency_key(
        lifecycle_id=lifecycle_id,
        trigger_type=trigger_type,
        policy_version=policy_version,
        trigger_event_id=trigger_event_id,
    )
    return "mgmt-" + canonical_hash(key)[:20]


class ProtectionTarget(BaseModel):
    """授权绑定的目标保护单；last_protection 标记用于拒绝撤最后保护。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sl_client_id: str | None = None
    tp_client_id: str | None = None
    is_last_protection: bool = False


class ManagementAuthorization(BaseModel):
    """管理能力凭证；绑定账户、仓位、数量、保护目标与时效。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    account_ref: str
    lifecycle_id: str
    position_side: Literal["LONG", "SHORT"]
    max_reduce_qty: MoneyDecimal
    target_protection: ProtectionTarget
    position_revision: int = Field(ge=0)
    nonce: str
    issued_at_ms: int
    expires_at_ms: int


class AuthorizationVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    allowed: bool
    reason_codes: tuple[str, ...] = ()


def validate_authorization(
    authorization: ManagementAuthorization,
    *,
    request: Mapping[str, Any],
    now_ms: int,
    current_revision: int,
) -> AuthorizationVerdict:
    """核验管理请求；不允许 OPEN、提高净敞口或撤掉最后保护。"""
    reasons: list[str] = []
    action = str(request.get("action") or "").upper()
    if str(request.get("account_ref") or "") != authorization.account_ref:
        reasons.append(REASON_ACCOUNT_MISMATCH)
    if str(request.get("lifecycle_id") or "") != authorization.lifecycle_id:
        reasons.append(REASON_LIFECYCLE_MISMATCH)
    if str(request.get("position_side") or "").upper() != authorization.position_side:
        reasons.append(REASON_SIDE_MISMATCH)
    if now_ms >= authorization.expires_at_ms:
        reasons.append(REASON_AUTHORIZATION_EXPIRED)
    if current_revision != authorization.position_revision:
        reasons.append(REASON_STALE_REVISION)
    if action == "OPEN":
        reasons.append(REASON_OPEN_NOT_ALLOWED)
    elif action not in MANAGEMENT_ACTIONS:
        reasons.append(REASON_ACTION_NOT_MANAGEMENT)
    if action == "REDUCE":
        requested = _optional_decimal(request.get("reduce_qty"))
        if requested is not None and requested > authorization.max_reduce_qty:
            reasons.append(REASON_REDUCE_EXCEEDS)
        position_qty = _optional_decimal(request.get("current_position_qty"))
        if requested is not None and position_qty is not None and requested > position_qty:
            reasons.append(REASON_INSUFFICIENT_POSITION)
    net_delta = _optional_decimal(request.get("requested_net_delta"))
    if net_delta is not None and net_delta > 0:
        reasons.append(REASON_NET_EXPOSURE_INCREASE)
    if bool(request.get("remove_protection")) and authorization.target_protection.is_last_protection:
        reasons.append(REASON_LAST_PROTECTION)
    return AuthorizationVerdict(
        authorization_id=authorization.authorization_id,
        allowed=not reasons,
        reason_codes=tuple(reasons),
    )


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return decimal_from_value(value)
    except ValueError:
        return None


class OrderQuantity(BaseModel):
    """订单数量的三个分量与状态规则；终态后迟到 fill 只改事实不复活订单。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    order_id: str
    accepted_qty: MoneyDecimal
    cumulative_filled_qty: MoneyDecimal
    working_leaves_qty: MoneyDecimal
    terminal_unfilled_qty: MoneyDecimal
    status: Literal["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"]
    revision: int = Field(default=0, ge=0)
    cancel_requested: bool = False
    cancel_confirmed: bool = False

    def violations(self) -> tuple[str, ...]:
        found: list[str] = []
        components = (
            self.accepted_qty,
            self.cumulative_filled_qty,
            self.working_leaves_qty,
            self.terminal_unfilled_qty,
        )
        if any(item < 0 for item in components):
            found.append(VIOLATION_NEGATIVE_QUANTITY)
        if self.cumulative_filled_qty > self.accepted_qty:
            found.append(VIOLATION_FILLED_EXCEEDS_ACCEPTED)
        if (
            self.accepted_qty
            != self.cumulative_filled_qty + self.working_leaves_qty + self.terminal_unfilled_qty
        ):
            found.append(VIOLATION_CONSERVATION)
        if self.status in WORKING_STATUSES and self.terminal_unfilled_qty != 0:
            found.append(VIOLATION_TERMINAL_WHILE_WORKING)
        if self.status in TERMINAL_STATUSES and self.working_leaves_qty != 0:
            found.append(VIOLATION_WORKING_WHILE_TERMINAL)
        if self.status == "FILLED" and (
            self.working_leaves_qty != 0 or self.terminal_unfilled_qty != 0
        ):
            found.append(VIOLATION_FILLED_WITH_RESIDUAL)
        if (
            self.cancel_requested
            and not self.cancel_confirmed
            and self.terminal_unfilled_qty != 0
        ):
            found.append(VIOLATION_UNCONFIRMED_CANCEL_TERMINAL)
        return tuple(found)

    def to_components(self) -> dict[str, str]:
        return {
            "order_id": self.order_id,
            "status": self.status,
            "revision": str(self.revision),
            "accepted_qty": str(self.accepted_qty),
            "cumulative_filled_qty": str(self.cumulative_filled_qty),
            "working_leaves_qty": str(self.working_leaves_qty),
            "terminal_unfilled_qty": str(self.terminal_unfilled_qty),
        }


def apply_fill(
    quantity: OrderQuantity,
    fill_qty: Decimal | str | int,
) -> OrderQuantity:
    """应用一笔成交；终态订单接受迟到 fill 并修订终态事实。"""
    fill = decimal_from_value(fill_qty)
    if fill <= 0:
        raise QuantityError("fill must be positive")
    if quantity.cumulative_filled_qty + fill > quantity.accepted_qty:
        raise QuantityError("fill exceeds accepted quantity")
    new_filled = quantity.cumulative_filled_qty + fill
    if quantity.status in WORKING_STATUSES:
        new_working = quantity.accepted_qty - new_filled
        new_terminal = Decimal(0)
    else:
        new_working = quantity.working_leaves_qty
        new_terminal = quantity.accepted_qty - new_filled - new_working
        if new_terminal < 0:
            raise QuantityError("terminal unfilled would become negative")
    updated = quantity.model_copy(update={
        "cumulative_filled_qty": new_filled,
        "working_leaves_qty": new_working,
        "terminal_unfilled_qty": new_terminal,
        "revision": quantity.revision + 1,
    })
    violations = updated.violations()
    if violations:
        raise QuantityError("; ".join(violations))
    return updated


def request_cancel(quantity: OrderQuantity) -> OrderQuantity:
    return quantity.model_copy(update={"cancel_requested": True})


def confirm_cancel(
    quantity: OrderQuantity,
    *,
    status: Literal["CANCELED", "EXPIRED", "REJECTED"] = "CANCELED",
) -> OrderQuantity:
    """撤单确认：working 转入 terminal，确认前不得提前计入。"""
    if not quantity.cancel_requested:
        raise QuantityError("cancel confirmation without cancel request")
    if quantity.status in WORKING_STATUSES:
        remaining = quantity.working_leaves_qty
        updated = quantity.model_copy(update={
            "status": status,
            "working_leaves_qty": Decimal(0),
            "terminal_unfilled_qty": quantity.terminal_unfilled_qty + remaining,
            "cancel_confirmed": True,
            "revision": quantity.revision + 1,
        })
    else:
        updated = quantity.model_copy(update={"cancel_confirmed": True})
    violations = updated.violations()
    if violations:
        raise QuantityError("; ".join(violations))
    return updated


class K04SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    management_intent: ManagementIntent
    authorization: ManagementAuthorization
    authorization_verdict: AuthorizationVerdict
    order_quantity: OrderQuantity
