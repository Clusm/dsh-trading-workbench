"""P1 一次性审批令牌（approval_token）。

设计（docs/03 §8.1/§8.2）：
- **HMAC 签名**：进程内随机密钥，令牌不可伪造；服务重启后旧令牌自动失效（可接受）；
- **绑定 plan hash**：P1 归一化计划后计算 sha256；P2 若携带 plan 必须与 token 内的一致；
- **TTL**：默认 120s（与决策证据 120s 对齐）；
- **单次**：``approvals`` 表原子 ``UPDATE ... WHERE status='ISSUED'``，重放返回
  ``E_APPROVAL_REPLAY``；执行失败也不返还（必须重新走 P1）。

令牌格式：``v1.<base64url(payload_json)>.<base64url(hmac_sha256)>``。

§8·A（T02）：令牌绑定的是``后端确定性计算``的可执行数量
（``approved_qty`` + ``sizing`` 算式 + ``leverage_cap``），不是模型申报的数量；
``confidence`` 只作为计划快照保留（``confidence_used_for_sizing=false``），
不参与任何授权。令牌里的风险额是**计划约束描述**（风险预算 × 入场到止损距离），
不是极端行情下的最大实际损失保证。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from ..ledger.numbers import dec_text
from ..ledger.store import Ledger, utc_now, utc_now_ms
from .config import RiskLimits
from .models import OrderPlan, RiskVerdict

__all__ = ["ApprovalError", "ApprovalManager", "AuthorizationPolicy"]

_TOKEN_VERSION = "v1"

#: 令牌里必须出现的确定性数量口径标记；旧代码签发的令牌没有这些标记。
_TOKEN_SIZING_POLICY = "deterministic"


@dataclass(frozen=True, slots=True)
class AuthorizationPolicy:
    """T19：绑定到令牌管理器的“当前授权策略”（令牌只证明它当初按这套策略批准过）。

    这不是第二套限额：``max_leverage`` / ``max_risk_pct`` 直接来自 ``RiskLimits`` 的
    普通档字段（由 ``RiskService`` 在构造时绑定），作用只有一个——**证伪旧令牌**：
    P1 之后阈值被改过、或令牌来自更宽松的旧版本（强信心档 15x / 2%），consume 直接拒绝。
    """

    rules_version: str
    max_leverage: int
    max_risk_pct: str
    sizing_policy: str = _TOKEN_SIZING_POLICY

    def as_dict(self) -> dict[str, Any]:
        return {
            "bound": True,
            "rules_version": self.rules_version,
            "max_leverage": int(self.max_leverage),
            "max_risk_pct": self.max_risk_pct,
            "sizing_policy": self.sizing_policy,
            "confidence_used_for_sizing": False,
        }


class ApprovalError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class ApprovalManager:
    def __init__(
        self,
        ledger: Ledger,
        *,
        ttl_s: int = 120,
        secret: bytes | None = None,
        clock=time.time,
    ) -> None:
        self.ledger = ledger
        self.ttl_s = max(1, int(ttl_s))
        self._secret = secret or secrets.token_bytes(32)
        self._clock = clock
        self._lock = threading.Lock()
        #: None = 未绑定（独立/单测用法，行为保持兼容）；RiskService 构造时必定绑定。
        self._authorization: AuthorizationPolicy | None = None

    # ------------------------------------------------------------------ policy
    def bind_authorization(
        self, *, rules_version: str, max_leverage: int, max_risk_pct: Any
    ) -> AuthorizationPolicy:
        """绑定当前授权策略（由 RiskService 用普通档限额构造）。

        绑定后 ``consume`` 会拒绝：① 没有 rules_version 的旧令牌；② rules_version 与当前
        不一致的令牌（阈值被改过）；③ 不带确定性 sizing 标记的令牌；④ 杠杆/杠杆上限超过
        普通档的令牌；⑤ sizing 自相矛盾（approved_qty ≠ sizing.approved_qty 或超过模型申报）。
        """

        policy = AuthorizationPolicy(
            rules_version=str(rules_version),
            max_leverage=int(max_leverage),
            max_risk_pct=dec_text(_decimal(max_risk_pct) or Decimal("0")),
        )
        self._authorization = policy
        return policy

    def authorization_policy(self) -> dict[str, Any]:
        """当前绑定状态（``/api/risk/envelope`` 自证：令牌受普通档限额约束）。"""

        policy = self._authorization
        return {"bound": False} if policy is None else policy.as_dict()

    # ------------------------------------------------------------------ issue
    def issue(
        self, plan: OrderPlan, verdict: RiskVerdict, *, context: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not verdict.approved:
            raise ApprovalError("E_APPROVAL_NOT_APPROVED", "verdict is not approved", status_code=422)
        plan_hash = plan.plan_hash()
        jti = secrets.token_hex(16)
        now_ms = int(self._clock() * 1000)
        expires_at_ms = now_ms + self.ttl_s * 1000
        context = context or {}
        market = dict(context.get("market") or {})
        payload = {
            "v": 1,
            "jti": jti,
            "iat_ms": now_ms,
            "exp_ms": expires_at_ms,
            "context_ts_ms": now_ms,
            "market": market,
            "market_hash": _hash_payload(market),
            "rules_version": context.get("rules_version")
            or (self._authorization.rules_version if self._authorization else None),
            "evidence": context.get("evidence"),
            "evidence_age_ms": context.get("evidence_age_ms"),
            "strategy_context": context.get("strategy_context"),
            "strategy_mode_revision": context.get("strategy_mode_revision"),
            "plan_hash": plan_hash,
            "plan": plan.normalized_dict(),
            "approved_qty": dec_text(verdict.approved_qty),
            "leverage": int(verdict.leverage),
            "leverage_cap": int(verdict.leverage_cap or verdict.leverage),
            "sizing": dict(verdict.sizing),
            "sizing_policy": "deterministic",
            "confidence_used_for_sizing": False,
            "cycle_id": plan.cycle_id,
        }
        token = self._sign(payload)
        self.ledger.insert_approval(
            {
                "jti": jti,
                "plan_hash": plan_hash,
                "cycle_id": plan.cycle_id,
                "approved_qty": verdict.approved_qty,
                "leverage": int(verdict.leverage),
                "created_at": utc_now(),
                "expires_at_ms": expires_at_ms,
            }
        )
        return {
            "token": token,
            "jti": jti,
            "plan_hash": plan_hash,
            "approved_qty": dec_text(verdict.approved_qty),
            "leverage": int(verdict.leverage),
            "ttl_s": self.ttl_s,
            "issued_at_ms": now_ms,
            "expires_at_ms": expires_at_ms,
            "expires_at": _iso_ms(expires_at_ms),
        }

    # ------------------------------------------------------------------ consume
    def consume(self, token: str | None, *, plan: OrderPlan | None = None) -> dict[str, Any]:
        if not token:
            raise ApprovalError(
                "E_APPROVAL_REQUIRED", "approval_token is required for P2", status_code=401
            )
        payload = self._verify(token)
        jti = str(payload.get("jti") or "")
        if not jti:
            raise ApprovalError("E_APPROVAL_INVALID", "token payload missing jti", status_code=403)

        row = self.ledger.get_approval(jti)
        if row is None:
            raise ApprovalError("E_APPROVAL_INVALID", "unknown approval", status_code=403)
        if str(row.get("status")) != "ISSUED":
            raise ApprovalError(
                "E_APPROVAL_REPLAY",
                "approval token already consumed (replay rejected)",
                status_code=409,
                details={"jti": jti},
            )

        expires_at_ms = int(payload.get("exp_ms") or 0)
        if int(self._clock() * 1000) >= expires_at_ms:
            self.ledger.purge_expired_approvals()
            raise ApprovalError(
                "E_APPROVAL_EXPIRED",
                "approval token expired",
                status_code=409,
                details={"expires_at_ms": expires_at_ms},
            )

        market = payload.get("market") or {}
        expected_market_hash = payload.get("market_hash")
        if expected_market_hash and _hash_payload(market) != expected_market_hash:
            raise ApprovalError(
                "E_APPROVAL_INVALID",
                "approval token market facts hash mismatch",
                status_code=403,
                details={"reason": "market_hash_mismatch"},
            )
        if plan is not None and plan.plan_hash() != str(payload.get("plan_hash")):
            raise ApprovalError(
                "E_APPROVAL_PLAN_MISMATCH",
                "plan does not match the approved plan hash",
                status_code=409,
                details={"plan_hash": plan.plan_hash(), "approved_hash": payload.get("plan_hash")},
            )

        # T19：授权证伪（绑定后生效）——令牌只能证明“当初按普通档批准过”，不能替任何
        # 一次执行提供超过普通档的授权。放在 consume 之前：被拒的令牌不烧掉也不放行
        # （它重放多少次都不可能通过）。
        self._assert_authorization(payload, jti=jti)

        if not self.ledger.consume_approval(jti):
            raise ApprovalError(
                "E_APPROVAL_REPLAY", "approval token already consumed", status_code=409
            )
        return payload

    # ------------------------------------------------------------------ fence
    def _assert_authorization(self, payload: dict[str, Any], *, jti: str) -> None:
        """T19：授权证伪。绑定策略后，令牌不得携带超过普通档的授权。

        覆盖四类旧令牌/异常令牌：
        1. 缺 rules_version（本次绑定之前签发，可能是强信心档时代）；
        2. rules_version 与当前普通档指纹不一致（阈值被改过）；
        3. 不带确定性 sizing 口径（confidence_used_for_sizing=True 或缺字段）；
        4. 杠杆/杠杆上限超过普通档，或 sizing 自相矛盾/超过模型申报上限。

        未绑定策略（独立用法/单测）时不改行为——生产路径由 RiskService 构造时绑定。
        """

        policy = self._authorization
        if policy is None:
            return

        token_rules = payload.get("rules_version")
        if not token_rules:
            raise ApprovalError(
                "E_APPROVAL_RULES_CHANGED",
                "approval token predates rules_version binding (legacy token); re-run P1",
                status_code=409,
                details={
                    "reason": "rules_version_missing",
                    "legacy_token": True,
                    "current": policy.rules_version,
                    "jti": jti,
                },
            )
        if str(token_rules) != policy.rules_version:
            raise ApprovalError(
                "E_APPROVAL_RULES_CHANGED",
                "risk limits changed since the approval was issued; re-run P1",
                status_code=409,
                details={
                    "reason": "rules_version_mismatch",
                    "approved": str(token_rules),
                    "current": policy.rules_version,
                    "jti": jti,
                },
            )

        if (
            str(payload.get("sizing_policy") or "") != policy.sizing_policy
            or payload.get("confidence_used_for_sizing") is not False
        ):
            raise ApprovalError(
                "E_APPROVAL_LEGACY_POLICY",
                "approval token does not carry the deterministic sizing policy; re-run P1",
                status_code=403,
                details={
                    "reason": "sizing_policy_not_deterministic",
                    "sizing_policy": payload.get("sizing_policy"),
                    "confidence_used_for_sizing": payload.get("confidence_used_for_sizing"),
                    "jti": jti,
                },
            )

        leverage = _int_or_none(payload.get("leverage"))
        leverage_cap = _int_or_none(payload.get("leverage_cap"))
        if leverage is None or leverage <= 0:
            raise ApprovalError(
                "E_APPROVAL_AUTHORIZATION_EXCEEDED",
                "approval token has no usable leverage",
                status_code=403,
                details={"reason": "leverage_missing", "jti": jti},
            )
        if leverage > policy.max_leverage:
            raise ApprovalError(
                "E_APPROVAL_AUTHORIZATION_EXCEEDED",
                f"approved leverage {leverage}x exceeds the regular-tier cap "
                f"{policy.max_leverage}x",
                status_code=403,
                details={
                    "reason": "leverage_above_regular_cap",
                    "field": "leverage",
                    "value": leverage,
                    "regular_max_leverage": policy.max_leverage,
                    "jti": jti,
                },
            )
        if leverage_cap is not None and leverage_cap > policy.max_leverage:
            raise ApprovalError(
                "E_APPROVAL_AUTHORIZATION_EXCEEDED",
                f"approved leverage_cap {leverage_cap}x exceeds the regular-tier cap "
                f"{policy.max_leverage}x",
                status_code=403,
                details={
                    "reason": "leverage_cap_above_regular_cap",
                    "field": "leverage_cap",
                    "value": leverage_cap,
                    "regular_max_leverage": policy.max_leverage,
                    "jti": jti,
                },
            )
        if leverage_cap is not None and 0 < leverage_cap < leverage:
            raise ApprovalError(
                "E_APPROVAL_POLICY_VIOLATION",
                "approval token leverage exceeds its own leverage_cap",
                status_code=403,
                details={
                    "reason": "leverage_above_token_cap",
                    "leverage": leverage,
                    "leverage_cap": leverage_cap,
                    "jti": jti,
                },
            )

        approved = _decimal(payload.get("approved_qty"))
        if approved is None or approved <= 0:
            raise ApprovalError(
                "E_APPROVAL_POLICY_VIOLATION",
                "approval token approved_qty is not a positive number",
                status_code=403,
                details={"reason": "approved_qty_invalid", "jti": jti},
            )
        sizing = payload.get("sizing")
        if isinstance(sizing, dict) and sizing:
            if (
                str(sizing.get("policy") or "") != policy.sizing_policy
                or str(sizing.get("confidence_used") or "").strip().lower() != "false"
            ):
                raise ApprovalError(
                    "E_APPROVAL_LEGACY_POLICY",
                    "approval token sizing block is not deterministic; re-run P1",
                    status_code=403,
                    details={
                        "reason": "sizing_block_not_deterministic",
                        "sizing_policy": sizing.get("policy"),
                        "confidence_used": sizing.get("confidence_used"),
                        "jti": jti,
                    },
                )
            sizing_approved = _decimal(sizing.get("approved_qty"))
            if sizing_approved is None or sizing_approved != approved:
                raise ApprovalError(
                    "E_APPROVAL_POLICY_VIOLATION",
                    "approved_qty does not match the recorded sizing computation",
                    status_code=403,
                    details={
                        "reason": "approved_qty_not_from_sizing",
                        "approved_qty": str(approved),
                        "sizing_approved_qty": None if sizing_approved is None else str(sizing_approved),
                        "jti": jti,
                    },
                )
            requested = _decimal(sizing.get("requested_qty"))
            if requested is not None and requested > 0 and approved > requested:
                raise ApprovalError(
                    "E_APPROVAL_POLICY_VIOLATION",
                    "approved_qty exceeds the model-requested upper bound",
                    status_code=403,
                    details={
                        "reason": "approved_qty_above_requested",
                        "approved_qty": str(approved),
                        "requested_qty": str(requested),
                        "jti": jti,
                    },
                )

    # ------------------------------------------------------------------ helpers
    def _sign(self, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_TOKEN_VERSION}.{_b64e(body)}.{_b64e(signature)}"

    def _verify(self, token: str) -> dict[str, Any]:
        try:
            version, body_b64, sig_b64 = token.split(".")
        except ValueError as exc:
            raise ApprovalError(
                "E_APPROVAL_INVALID", "malformed approval token", status_code=403
            ) from exc
        if version != _TOKEN_VERSION:
            raise ApprovalError(
                "E_APPROVAL_INVALID", f"unsupported token version {version!r}", status_code=403
            )
        try:
            body = _b64d(body_b64)
            signature = _b64d(sig_b64)
        except Exception as exc:  # noqa: BLE001 - base64 损坏
            raise ApprovalError(
                "E_APPROVAL_INVALID", "approval token is not valid base64", status_code=403
            ) from exc
        expected = hmac.new(self._secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ApprovalError(
                "E_APPROVAL_INVALID", "approval token signature mismatch", status_code=403
            )
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovalError(
                "E_APPROVAL_INVALID", "approval token payload is not JSON", status_code=403
            ) from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ApprovalError(
                "E_APPROVAL_INVALID", "approval token payload is invalid", status_code=403
            )
        return payload

    # ------------------------------------------------------------------ misc
    def plan_from_payload(self, payload: dict[str, Any]) -> OrderPlan:
        plan = payload.get("plan")
        if not isinstance(plan, dict):
            raise ApprovalError("E_APPROVAL_INVALID", "approval payload missing plan", status_code=403)
        try:
            return OrderPlan.from_payload(plan, cycle_id=payload.get("cycle_id"))
        except ValueError as exc:
            raise ApprovalError(
                "E_APPROVAL_INVALID", f"approved plan is invalid: {exc}", status_code=403
            ) from exc

    def ttl_seconds(self) -> int:
        return self.ttl_s

    def now_ms(self) -> int:
        return int(self._clock() * 1000) or utc_now_ms()


def _decimal(value: Any) -> Decimal | None:
    """把令牌字段解析为有限 Decimal；nan/inf/非数字一律 None（fail-closed）。"""

    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _hash_payload(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(body).hexdigest()


def _iso_ms(ms: int) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _limits_ttl(limits: RiskLimits) -> int:
    return int(limits.approval_ttl_s)
