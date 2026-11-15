"""风控服务：把规则、状态、审批令牌组装成 P1 网关。

本模块不碰网络；所有"账户/仓位/行情"事实由调用方（``execution.service``）传入，
以保证同一份规则既能在 P1（计划）又能在 P2（提交）复用。

§8·A（T02）：
- P1（``evaluate``）签发令牌时绑定的是**后端确定性计算**的数量 ``verdict.approved_qty``，
  模型申报的 ``plan.qty`` 只是上限；
- P2（``check``）用提交那一刻的服务端事实（权益/可用保证金由调用方刷新，
  价格快照按经过时间老化并受 R14 15s 门限约束）重跑同一套规则，
  返回的 ``approved_qty`` 由调用方与令牌里的数量取 ``min`` 后再下单——
  模型数量既不能放大，也不能绕过这次复算；
- 报告（``envelope``）显式带 ``sizing_policy`` 与 ``legacy_strong_tier``：
  confidence 不参与授权，``strong_*`` 旧字段仅为读取兼容。

T19（权限红队）：本类是“入口 3/4”的实现点——``envelope`` 用 ``authorization`` 块声明
哪些字段算执行授权、哪些明确不算；``check`` 是 P2 复核，永远重新计算而不是相信令牌。
令牌侧另有 ``ApprovalManager.bind_authorization`` 证伪旧令牌（缺 rules_version /
强信心档 15x / 非确定性 sizing 一律拒绝），见 ``approval.py``。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Mapping

from ..ledger.store import Ledger
from .approval import ApprovalManager
from .config import (
    AUTHORIZATION_FIELDS,
    LEGACY_STRONG_TIER,
    MAX_APPROVAL_TTL_S,
    SIZING_POLICY,
    RiskLimits,
    env_knobs,
    rule_table,
)
from .models import OrderPlan, RiskContext, RiskVerdict, SymbolFiltersView
from .rules import evaluate
from .state import ControlSnapshot, RiskStateStore

__all__ = ["RiskService"]


class RiskService:
    def __init__(
        self,
        limits: RiskLimits,
        ledger: Ledger,
        state: RiskStateStore,
        approvals: ApprovalManager,
    ) -> None:
        self.limits = limits
        self.ledger = ledger
        self.state = state
        self.approvals = approvals
        # T19：把普通档普通限额绑到令牌管理器上。绑定后 consume 会拒绝旧令牌
        # （缺 rules_version / 强信心档 15x / 非确定性 sizing），即“没有第二条
        # 放大路径”的第二道闸；生产路径必经此处，不存在漏绑。
        approvals.bind_authorization(
            rules_version=self.rules_version(),
            max_leverage=int(self.limits.max_leverage),
            max_risk_pct=self.limits.max_risk_pct,
        )

    # ------------------------------------------------------------------ P1
    def evaluate(
        self,
        plan: OrderPlan,
        context: RiskContext,
        *,
        filters: SymbolFiltersView | None = None,
        trace_id: str | None = None,
        stage: str = "P1",
        approval_context: dict[str, Any] | None = None,
    ) -> tuple[RiskVerdict, dict[str, Any] | None]:
        verdict = evaluate(plan, context, self.limits, filters=filters)
        self.ledger.log_risk_event(
            event_type=f"risk_evaluate_{verdict.verdict}",
            severity="info" if verdict.approved else "warn",
            rule_id=verdict.blocking_rule,
            action=stage,
            details={
                "symbol": plan.symbol,
                "side": plan.side,
                "leverage": plan.leverage,
                "leverage_cap": verdict.leverage_cap,
                "qty": str(plan.qty),
                "approved_qty": str(verdict.approved_qty),
                # §8·A：confidence 只记录为分析字段，不参与限额/数量决策。
                "confidence": plan.confidence,
                "confidence_used_for_sizing": False,
                "tier": verdict.tier,
                "sizing": dict(verdict.sizing),
                "violations": [item.as_dict() for item in verdict.violations],
            },
            trace_id=trace_id,
        )
        approval = None
        if verdict.approved:
            approval = self.approvals.issue(plan, verdict, context=approval_context)
            self.ledger.log_risk_event(
                event_type="approval_issued",
                severity="info",
                rule_id="R00",
                action="approval_token",
                details={
                    "jti": approval["jti"],
                    "plan_hash": approval["plan_hash"][:16],
                    "expires_at_ms": approval["expires_at_ms"],
                },
                trace_id=trace_id,
            )
        return verdict, approval

    # ------------------------------------------------------------------ P2
    def check(
        self,
        plan: OrderPlan,
        context: RiskContext,
        *,
        filters: SymbolFiltersView | None = None,
        trace_id: str | None = None,
    ) -> RiskVerdict:
        """P2 复检：只跑规则、**不签发**新令牌（避免 P2 自我续期）。

        ``context`` 必须是提交时刻的服务端事实（调用方在 P1 之后刷新权益、
        used/available 保证金与仓位，价格快照按经过时间老化并由 R14 15s 门限把关）。
        返回的 ``verdict.approved_qty`` 是**重新确定性计算**的数量：调用方必须与
        令牌内 approved_qty 取 min 后再下单，模型提供的数量不能绕过这次复算。
        T19：``check`` 刻意不签发令牌（P2 不能给自己续期），也不读令牌里的置信度/数量；
        令牌只是“P1 曾经按普通档批准过”的凭据。
        """

        verdict = evaluate(plan, context, self.limits, filters=filters)
        self.ledger.log_risk_event(
            event_type=f"risk_recheck_{verdict.verdict}",
            severity="info" if verdict.approved else "warn",
            rule_id=verdict.blocking_rule,
            action="P2",
            details={
                "symbol": plan.symbol,
                "leverage_cap": verdict.leverage_cap,
                "qty": str(plan.qty),
                "approved_qty": str(verdict.approved_qty),
                "confidence": plan.confidence,
                "confidence_used_for_sizing": False,
                "tier": verdict.tier,
                "sizing": dict(verdict.sizing),
                "equity_session": None if context.equity_session is None else str(context.equity_session),
                "available_margin": (
                    None if context.available_margin is None else str(context.available_margin)
                ),
                "snapshot_age_ms": context.snapshot_age_ms,
                "violations": [item.as_dict() for item in verdict.violations],
            },
            trace_id=trace_id,
        )
        return verdict

    # ------------------------------------------------------------------ envelope
    def envelope(self, base: Mapping[str, Any]) -> dict[str, Any]:
        """风险信封：限额 + 规则开关 + 控制态 + 阻断原因（``/api/risk/envelope``）。"""

        control: ControlSnapshot | None = base.get("control")
        risk_state = dict(base.get("risk_state") or {})
        equity = dict(base.get("equity") or {})
        account = dict(base.get("account") or {})
        positions_info = dict(base.get("positions") or {})
        outbox = dict(base.get("outbox") or {})

        blockers: list[str] = list(base.get("blocking_reasons") or [])
        if control is not None and control.state != "normal":
            blockers.append("CONTROL_NOT_NORMAL")
        if risk_state.get("daily_loss_latched"):
            blockers.append("DAILY_LOSS_LATCHED")
        if risk_state.get("drawdown_latched"):
            blockers.append("DRAWDOWN_LATCHED")
        if not equity.get("available"):
            blockers.append("EQUITY_UNAVAILABLE")
        if not account.get("credentials_configured"):
            blockers.append("E_DEMO_CREDENTIALS_MISSING")
        if outbox.get("unknown", 0) > 0:
            blockers.append("ORDER_OUTBOX_UNKNOWN")
        if outbox.get("submitting", 0) > 0:
            blockers.append("ORDER_OUTBOX_SUBMITTING")
        if positions_info.get("protection_ok") is False:
            blockers.append("PROTECTION_MISSING")

        return {
            "venue": "demo",
            "demo_only": True,
            "ledger_id": self.ledger.ledger_id,
            "limits": self.limits.as_dict(),
            "rules": rule_table(
                loss_streak=self.limits.enable_loss_streak,
                frequency=self.limits.enable_frequency,
            ),
            "control": None if control is None else control.as_dict(),
            "risk_state": risk_state,
            "equity": equity,
            "account": account,
            "positions": positions_info,
            "outbox": outbox,
            "approval": {
                "ttl_s": self.approvals.ttl_seconds(),
                "ttl_max_s": MAX_APPROVAL_TTL_S,
                "single_use": True,
                "plan_hash_bound": True,
                "rules_version_bound": True,
                "authorization_model": "recomputed_at_p1_and_p2",
                "policy": self.approvals.authorization_policy(),
            },
            # T19：明确“哪些字段算执行授权 / 哪些不算”，并给出权威上限值。
            # 令牌不是授权源：P2 会用提交时刻的服务端事实重跑同一套规则，
            # 允许数量取 min(令牌 approved_qty, P2 重算 approved_qty)。
            "authorization": {
                "execution_authorization_fields": list(AUTHORIZATION_FIELDS),
                "not_used_for_execution_authorization": [
                    "per_trade.strong_max_risk_pct",
                    "per_trade.strong_max_leverage",
                    "per_trade.strong_min_confidence",
                    "per_trade.strong_min_rr",
                    "confidence",
                    "plan.qty（模型申报上限）",
                ],
                "effective_caps": {
                    "max_risk_pct": self.limits.max_risk_pct,
                    "max_leverage": int(self.limits.max_leverage),
                    "max_notional_multiple": self.limits.max_notional_multiple,
                    "max_margin_usage": self.limits.max_margin_usage,
                    "max_open_positions": self.limits.max_open_positions,
                },
                "sizing_policy": "deterministic",
                "confidence_used_for_sizing": False,
                "authorization_model": "recomputed_at_p1_and_p2",
                "approval_token_is_authorization_source": False,
            },
            "env_knobs": env_knobs(),
            # §8·A：报告里明确"confidence 不提额、数量按确定性公式、legacy strong_* 不授权"。
            "sizing_policy": {
                **SIZING_POLICY,
                "inputs": list(SIZING_POLICY["inputs"]),  # type: ignore[arg-type]
            },
            "legacy_strong_tier": {
                **LEGACY_STRONG_TIER,
                "fields": list(LEGACY_STRONG_TIER["fields"]),  # type: ignore[arg-type]
            },
            "new_entry_allowed": not blockers,
            "blocking_reasons": list(dict.fromkeys(blockers)),
            "checked_at": base.get("checked_at"),
        }

    # ------------------------------------------------------------------ misc
    def issue_token_for_replay_tests(self, plan: OrderPlan, verdict: RiskVerdict) -> dict[str, Any]:
        """测试/运维用：对已批准的 verdict 重新签发令牌（不改变规则结果）。"""

        return self.approvals.issue(plan, verdict)

    def limits_dict(self) -> dict[str, Any]:
        return self.limits.as_dict()

    def rules_version(self) -> str:
        """规则/阈值指纹；P1 写入 token，P2 校验（阈值变更后旧令牌失效）。"""

        canonical = json.dumps(
            self.limits.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return "risk.v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def context_from_parts(self, **parts: Any) -> RiskContext:
        return RiskContext(**parts)

    def as_dict(self) -> dict[str, Any]:
        return {"limits": asdict(self.limits)}
