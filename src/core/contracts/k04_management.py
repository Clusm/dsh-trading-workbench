"""K04 管理意图、管理授权与订单数量三分量契约。

管理幂等键由生命周期、触发类型、规则版本与触发事件归一化后确定，跨轮
稳定且不因分隔符碰撞；旧 revision 授权自动失效。数量统一三分量：
``accepted = cumulative_filled + working_leaves + terminal_unfilled``。
撤单确认前 remaining 不得计入 terminal；终态迟到 fill 仍修订累计成交，
fill 级幂等由调用方传入已应用 fill ID 集合；REJECTED 表示交易所未接受。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from .common import (
    SCHEMA_VERSION,
    MoneyDecimal,
    canonical_hash,
    decimal_from_value,
    join_key_parts,
    normalize_key_part,
)
from .k01_identity import RuntimeIdentity

REASON_OPEN_NOT_ALLOWED = "OPEN_NOT_ALLOWED"
REASON_ACTION_NOT_MANAGEMENT = "ACTION_NOT_MANAGEMENT"
REASON_AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
REASON_AUTHORIZATION_FROM_FUTURE = "AUTHORIZATION_FROM_FUTURE"
REASON_STALE_REVISION = "STALE_POSITION_REVISION"
REASON_ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
REASON_LIFECYCLE_MISMATCH = "LIFECYCLE_MISMATCH"
REASON_SIDE_MISMATCH = "SIDE_MISMATCH"
REASON_ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
REASON_ENVIRONMENT_UNBOUND = "ENVIRONMENT_UNBOUND"
REASON_RUN_GENERATION_MISMATCH = "RUN_GENERATION_MISMATCH"
REASON_RUN_GENERATION_UNBOUND = "RUN_GENERATION_UNBOUND"
REASON_NONCE_REPLAY = "NONCE_REPLAY"
REASON_REDUCE_QTY_MISSING = "REDUCE_QTY_MISSING"
REASON_REDUCE_QTY_INVALID = "REDUCE_QTY_INVALID"
REASON_CLOSE_QTY_MISSING = "CLOSE_QTY_MISSING"
REASON_CLOSE_QTY_INVALID = "CLOSE_QTY_INVALID"
REASON_POSITION_QTY_MISSING = "POSITION_QTY_MISSING"
REASON_POSITION_QTY_INVALID = "POSITION_QTY_INVALID"
REASON_NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
REASON_REDUCE_EXCEEDS = "REDUCE_EXCEEDS_AUTHORIZATION"
REASON_CLOSE_EXCEEDS_AUTHORIZATION = "CLOSE_EXCEEDS_AUTHORIZATION"
REASON_INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
REASON_CLOSE_EXCEEDS_POSITION = "CLOSE_EXCEEDS_POSITION"
REASON_NET_DELTA_MISSING = "NET_DELTA_MISSING"
REASON_NET_DELTA_INVALID = "NET_DELTA_INVALID"
REASON_NET_EXPOSURE_INCREASE = "NET_EXPOSURE_INCREASE"
REASON_LAST_PROTECTION = "CANNOT_REMOVE_LAST_PROTECTION"
REASON_PROTECTION_TARGET_MISSING = "PROTECTION_TARGET_MISSING"
REASON_PROTECTION_TARGET_MISMATCH = "PROTECTION_TARGET_MISMATCH"

VIOLATION_NEGATIVE_QUANTITY = "NEGATIVE_QUANTITY"
VIOLATION_FILLED_EXCEEDS_ACCEPTED = "FILLED_EXCEEDS_ACCEPTED"
VIOLATION_CONSERVATION = "THREE_COMPONENT_CONSERVATION_VIOLATION"
VIOLATION_TERMINAL_WHILE_WORKING = "TERMINAL_NONZERO_WHILE_WORKING"
VIOLATION_WORKING_WHILE_TERMINAL = "WORKING_NONZERO_WHILE_TERMINAL"
VIOLATION_FILLED_WITH_RESIDUAL = "FILLED_STATUS_WITH_RESIDUAL"
VIOLATION_STATUS_FILL_MISMATCH = "STATUS_FILL_MISMATCH"
VIOLATION_REJECTED_WITH_ACCEPTED = "REJECTED_WITH_ACCEPTED_QTY"
VIOLATION_UNCONFIRMED_CANCEL_TERMINAL = "CANCEL_UNCONFIRMED_TERMINAL_COUNTED"
VIOLATION_FILLED_WITH_CANCEL_CONFIRMED = "FILLED_WITH_CANCEL_CONFIRMED"

MANAGEMENT_ACTIONS = ("CLOSE", "REDUCE", "MAINTAIN_PROTECTION")
WORKING_STATUSES = frozenset({"NEW", "PARTIALLY_FILLED"})
TERMINAL_STATUSES = frozenset({"CANCELED", "EXPIRED", "REJECTED"})
CANCEL_FORBIDDEN_STATUSES = TERMINAL_STATUSES | {"FILLED"}


class QuantityError(ValueError):
    """数量三分量守恒或状态规则被违反。"""


class DuplicateFillError(QuantityError):
    """同一 fill ID 被重复应用；调用方必须先做幂等去重。"""


class ManagementIntent(BaseModel):
    """有界管理意图；OPEN 不属于管理动作，不能出现在此契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    management_id: str = Field(min_length=1)
    identity: RuntimeIdentity
    lifecycle_id: str = Field(min_length=1)
    position_side: Literal["LONG", "SHORT"]
    action: Literal["CLOSE", "REDUCE", "MAINTAIN_PROTECTION"]
    max_reduce_qty: MoneyDecimal = Field(gt=0)
    position_revision: int = Field(ge=0)
    reason_code: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    expires_at_ms: int = Field(gt=0)
    evidence_id: str = Field(min_length=1)
    trigger_type: str = Field(min_length=1)
    trigger_event_id: str = Field(min_length=1)
    environment: str | None = None
    schema_version: str = SCHEMA_VERSION

    @model_validator(mode="after")
    def _bind_management_id(self) -> "ManagementIntent":
        if self.environment is not None and not self.environment.strip():
            raise ValueError("environment must be non-empty when provided")
        base_kwargs = {
            "lifecycle_id": self.lifecycle_id,
            "trigger_type": self.trigger_type,
            "policy_version": self.policy_version,
            "trigger_event_id": self.trigger_event_id,
        }
        expected_ids = {derive_management_id(**base_kwargs)}
        if self.environment is not None:
            expected_ids.add(derive_management_id(**base_kwargs, environment=self.environment))
        if self.management_id not in expected_ids:
            raise ValueError(
                "management_id must equal derived id (optionally environment-scoped)"
            )
        return self


def management_idempotency_key(
    *,
    lifecycle_id: str,
    trigger_type: str,
    policy_version: str,
    trigger_event_id: str,
    environment: str | None = None,
) -> str:
    """稳定幂等键：生命周期 + 触发类型 + 规则版本 + 触发事件（归一化并转义）。

    显式传入 ``environment`` 时附加环境域组件，避免 demo/live 同生命周期的
    管理意图互相碰撞；不传时保持 3.0 键文本兼容。
    """
    parts = [
        normalize_key_part(lifecycle_id),
        normalize_key_part(trigger_type),
        normalize_key_part(policy_version),
        normalize_key_part(trigger_event_id),
    ]
    if environment is not None:
        parts.append(f"env={normalize_key_part(environment)}")
    return join_key_parts(*parts)


def derive_management_id(
    *,
    lifecycle_id: str,
    trigger_type: str,
    policy_version: str,
    trigger_event_id: str,
    environment: str | None = None,
) -> str:
    """由幂等键派生的确定性 management_id；跨轮不得随机变化。"""
    key = management_idempotency_key(
        lifecycle_id=lifecycle_id,
        trigger_type=trigger_type,
        policy_version=policy_version,
        trigger_event_id=trigger_event_id,
        environment=environment,
    )
    return "mgmt-" + canonical_hash(key)[:20]


class ProtectionTarget(BaseModel):
    """授权绑定的目标保护单；last_protection 标记用于拒绝撤最后保护。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sl_client_id: str | None = None
    tp_client_id: str | None = None
    is_last_protection: bool = False


class ManagementAuthorization(BaseModel):
    """管理能力凭证；绑定账户、仓位的运行代、数量、保护目标与时效。"""

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
    environment: str | None = None
    runtime_instance_id: str | None = None


class AuthorizationVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authorization_id: str
    allowed: bool
    reconcile_allowed: bool = True
    reason_codes: tuple[str, ...] = ()

    @computed_field
    @property
    def submit_allowed(self) -> bool:
        """新提交许可 = allowed；由 allowed 单一来源派生，避免双字段漂移。"""
        return self.allowed


def _required_decimal(
    request: Mapping[str, Any],
    field: str,
    missing_code: str,
    invalid_code: str,
) -> tuple[Decimal | None, str | None]:
    """必需数值字段：缺失与畸形分别给出明确 reason，绝不按“无值即跳过”放行。"""
    if field not in request or request.get(field) is None:
        return None, missing_code
    try:
        return decimal_from_value(request[field]), None
    except ValueError:
        return None, invalid_code


def validate_authorization(
    authorization: ManagementAuthorization,
    *,
    request: Mapping[str, Any],
    now_ms: int,
    current_revision: int,
    consumed_nonces: Sequence[str] = (),
) -> AuthorizationVerdict:
    """核验管理请求；缺失/畸形/非正/超量输入一律拒绝并给出具体 reason。

    运行代/环境绑定按 strip 后非空判定：空串/空白视同未绑定
    （``RUN_GENERATION_UNBOUND/ENVIRONMENT_UNBOUND``），fail-closed。
    ``reconcile_allowed`` 仅当账户/生命周期/方向域匹配才为真；授权过期只
    限制新提交，不阻断按原 client ID 的 UNKNOWN 查单与对账（规格 §10.2）。
    """
    reasons: list[str] = []
    action = str(request.get("action") or "").upper()
    account_ok = str(request.get("account_ref") or "") == authorization.account_ref
    lifecycle_ok = str(request.get("lifecycle_id") or "") == authorization.lifecycle_id
    side_ok = str(request.get("position_side") or "").upper() == authorization.position_side
    if not account_ok:
        reasons.append(REASON_ACCOUNT_MISMATCH)
    if not lifecycle_ok:
        reasons.append(REASON_LIFECYCLE_MISMATCH)
    if not side_ok:
        reasons.append(REASON_SIDE_MISMATCH)
    if now_ms < authorization.issued_at_ms:
        reasons.append(REASON_AUTHORIZATION_FROM_FUTURE)
    if now_ms >= authorization.expires_at_ms:
        reasons.append(REASON_AUTHORIZATION_EXPIRED)
    if current_revision != authorization.position_revision:
        reasons.append(REASON_STALE_REVISION)
    runtime_bound = (
        authorization.runtime_instance_id
        if isinstance(authorization.runtime_instance_id, str)
        else None
    )
    if runtime_bound is None or not runtime_bound.strip():
        reasons.append(REASON_RUN_GENERATION_UNBOUND)
    elif str(request.get("runtime_instance_id") or "") != runtime_bound:
        reasons.append(REASON_RUN_GENERATION_MISMATCH)
    environment_bound = (
        authorization.environment if isinstance(authorization.environment, str) else None
    )
    if environment_bound is None or not environment_bound.strip():
        reasons.append(REASON_ENVIRONMENT_UNBOUND)
    elif str(request.get("environment") or "") != environment_bound:
        reasons.append(REASON_ENVIRONMENT_MISMATCH)
    if authorization.nonce and authorization.nonce in set(consumed_nonces):
        reasons.append(REASON_NONCE_REPLAY)

    if action == "OPEN":
        reasons.append(REASON_OPEN_NOT_ALLOWED)
    elif action not in MANAGEMENT_ACTIONS:
        reasons.append(REASON_ACTION_NOT_MANAGEMENT)
    else:
        position_qty, position_error = _required_decimal(
            request, "current_position_qty",
            REASON_POSITION_QTY_MISSING, REASON_POSITION_QTY_INVALID,
        )
        if position_error:
            reasons.append(position_error)
        quantity: Decimal | None = None
        if action == "REDUCE":
            quantity, quantity_error = _required_decimal(
                request, "reduce_qty",
                REASON_REDUCE_QTY_MISSING, REASON_REDUCE_QTY_INVALID,
            )
        elif action == "CLOSE":
            quantity, quantity_error = _required_decimal(
                request, "close_qty",
                REASON_CLOSE_QTY_MISSING, REASON_CLOSE_QTY_INVALID,
            )
        else:
            quantity_error = None
        if quantity_error:
            reasons.append(quantity_error)
        if quantity is not None:
            if quantity <= 0:
                reasons.append(REASON_NON_POSITIVE_QUANTITY)
            else:
                if quantity > authorization.max_reduce_qty:
                    reasons.append(
                        REASON_REDUCE_EXCEEDS if action == "REDUCE"
                        else REASON_CLOSE_EXCEEDS_AUTHORIZATION
                    )
                if position_qty is not None and quantity > position_qty:
                    reasons.append(
                        REASON_INSUFFICIENT_POSITION if action == "REDUCE"
                        else REASON_CLOSE_EXCEEDS_POSITION
                    )
        net_delta, net_error = _required_decimal(
            request, "requested_net_delta",
            REASON_NET_DELTA_MISSING, REASON_NET_DELTA_INVALID,
        )
        if net_error:
            reasons.append(net_error)
        elif net_delta is not None and net_delta > 0:
            reasons.append(REASON_NET_EXPOSURE_INCREASE)

    if bool(request.get("remove_protection")):
        if authorization.target_protection.is_last_protection:
            reasons.append(REASON_LAST_PROTECTION)
        target = str(request.get("target_client_id") or "")
        known_targets = {
            item for item in (
                authorization.target_protection.sl_client_id,
                authorization.target_protection.tp_client_id,
            ) if item
        }
        if not target:
            reasons.append(REASON_PROTECTION_TARGET_MISSING)
        elif target not in known_targets:
            reasons.append(REASON_PROTECTION_TARGET_MISMATCH)

    allowed = not reasons
    return AuthorizationVerdict(
        authorization_id=authorization.authorization_id,
        allowed=allowed,
        reconcile_allowed=account_ok and lifecycle_ok and side_ok,
        reason_codes=tuple(reasons),
    )


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
    requested_qty: MoneyDecimal | None = None

    @classmethod
    def checked(cls, **data: Any) -> "OrderQuantity":
        """严格构造工厂：任何守恒/状态违规直接拒绝。"""
        instance = cls(**data)
        violations = instance.violations()
        if violations:
            raise QuantityError("; ".join(violations))
        return instance

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
        if self.status == "NEW" and self.cumulative_filled_qty != 0:
            found.append(VIOLATION_STATUS_FILL_MISMATCH)
        if self.status == "PARTIALLY_FILLED" and not (
            0 < self.cumulative_filled_qty < self.accepted_qty
        ):
            found.append(VIOLATION_STATUS_FILL_MISMATCH)
        if self.status == "FILLED" and self.cumulative_filled_qty != self.accepted_qty:
            found.append(VIOLATION_STATUS_FILL_MISMATCH)
        if self.status == "REJECTED" and (
            self.accepted_qty != 0 or self.cumulative_filled_qty != 0
        ):
            found.append(VIOLATION_REJECTED_WITH_ACCEPTED)
        if self.status == "FILLED" and self.cancel_confirmed:
            found.append(VIOLATION_FILLED_WITH_CANCEL_CONFIRMED)
        if (
            self.cancel_requested
            and not self.cancel_confirmed
            and self.terminal_unfilled_qty != 0
        ):
            found.append(VIOLATION_UNCONFIRMED_CANCEL_TERMINAL)
        return tuple(found)

    def to_components(self) -> dict[str, Any]:
        """无损分量导出：包含撤销确认与原始请求量，可安全重水化。"""
        return {
            "order_id": self.order_id,
            "status": self.status,
            "revision": self.revision,
            "accepted_qty": str(self.accepted_qty),
            "cumulative_filled_qty": str(self.cumulative_filled_qty),
            "working_leaves_qty": str(self.working_leaves_qty),
            "terminal_unfilled_qty": str(self.terminal_unfilled_qty),
            "cancel_requested": self.cancel_requested,
            "cancel_confirmed": self.cancel_confirmed,
            "requested_qty": str(self.requested_qty) if self.requested_qty is not None else None,
        }


def apply_fill(
    quantity: OrderQuantity,
    fill_qty: Decimal | str | int,
    *,
    fill_id: str | None = None,
    applied_fill_ids: Sequence[str] = (),
) -> OrderQuantity:
    """应用一笔成交；推进 working 状态，终态迟到 fill 只修订事实。

    ``fill_id`` 与 ``applied_fill_ids`` 提供 fill 级幂等：空串/非法 ID 与
    重复 ID 分别抛 ``QuantityError``/``DuplicateFillError``；REJECTED 订单
    不得接受 fill。
    """
    if quantity.status == "REJECTED":
        raise QuantityError("REJECTED order cannot accept fills")
    if fill_id is not None:
        if not isinstance(fill_id, str) or not fill_id.strip():
            raise QuantityError("fill_id must be a non-empty string")
        if fill_id in set(applied_fill_ids):
            raise DuplicateFillError(f"fill already applied: {fill_id}")
    fill = decimal_from_value(fill_qty)
    if fill <= 0:
        raise QuantityError("fill must be positive")
    if quantity.cumulative_filled_qty + fill > quantity.accepted_qty:
        raise QuantityError("fill exceeds accepted quantity")
    new_filled = quantity.cumulative_filled_qty + fill
    if quantity.status in WORKING_STATUSES:
        new_working = quantity.accepted_qty - new_filled
        new_terminal = Decimal(0)
        if new_filled == quantity.accepted_qty:
            new_status = "FILLED"
        else:
            new_status = "PARTIALLY_FILLED"
    else:
        new_working = quantity.working_leaves_qty
        new_terminal = quantity.accepted_qty - new_filled - new_working
        new_status = quantity.status
        if new_terminal < 0:
            raise QuantityError("terminal unfilled would become negative")
    updated = quantity.model_copy(update={
        "cumulative_filled_qty": new_filled,
        "working_leaves_qty": new_working,
        "terminal_unfilled_qty": new_terminal,
        "status": new_status,
        "revision": quantity.revision + 1,
    })
    violations = updated.violations()
    if violations:
        raise QuantityError("; ".join(violations))
    return updated


def request_cancel(quantity: OrderQuantity) -> OrderQuantity:
    if quantity.status in CANCEL_FORBIDDEN_STATUSES:
        raise QuantityError("cannot request cancel on filled or terminal order")
    return quantity.model_copy(update={"cancel_requested": True})


def confirm_cancel(
    quantity: OrderQuantity,
    *,
    status: Literal["CANCELED", "EXPIRED", "REJECTED"] = "CANCELED",
) -> OrderQuantity:
    """撤单确认：working 转入 terminal，确认前不得提前计入。"""
    if quantity.status == "FILLED":
        raise QuantityError("filled order cannot be cancel-confirmed")
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
