"""P2-6 复现/回归探针：``filter_unbound_management(enforce=True)`` 不得静默关掉全部退出。

``TradeIntent`` 目前没有 ``management_authorization_id`` 字段（且 extra 默认
ignore，传入会被静默丢弃），因此 enforce=True 会把**所有**管理意图判为未绑定并
全部摘除 = "翻一个布尔就关掉全部退出"。修复后改为在摘除任何意图之前 fail-closed。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from agents.models import TradeDecision, TradeIntent, RiskVerdict  # noqa: E402
from core.decision_pipeline import (  # noqa: E402
    authorization_ref_supported, filter_unbound_management,
)


class _Db:
    def __init__(self) -> None:
        self.events: list[str] = []

    def log_risk_event(self, *, event_type: str, **_: Any) -> None:
        self.events.append(event_type)


def _decision() -> Any:
    return TradeDecision(decision="close", intents=[
        TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01)])


def main() -> int:
    db = _Db()
    decision = _decision()
    verdict = RiskVerdict(verdict="approved", adjusted_intents=list(decision.intents))

    print(f"INFO authorization_ref_supported={authorization_ref_supported(decision.intents)}",
          flush=True)
    observed, _, unbound = filter_unbound_management(decision, verdict, db)
    ok_observe = (len(observed.intents) == 1 and unbound == ["BTCUSDT"]
                  and db.events == ["management_intent_unbound"])
    print(f"PASS enforce_false_only_observes {ok_observe} {unbound}", flush=True)

    raised = ""
    dropped = None
    try:
        kept, _, _ = filter_unbound_management(decision, verdict, db, enforce=True)
        dropped = len(kept.intents)
    except Exception as exc:  # noqa: BLE001
        raised = type(exc).__name__
    print(f"INFO enforce=True -> raised={raised!r} kept_intents={dropped}", flush=True)
    ok_gate = raised == "ManagementEnforcementError" and dropped is None
    print(f"PASS enforce_true_fails_closed_instead_of_dropping_all_exits {ok_gate}",
          flush=True)

    # 有可绑定字段的模型（替身）在 enforce=True 下仍按原语义工作：摘除不降级为 open。
    class _BindableIntent(TradeIntent):
        management_authorization_id: str | None = None

    bind_decision = TradeDecision(decision="close", intents=[
        _BindableIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01)])
    bind_verdict = RiskVerdict(verdict="approved",
                               adjusted_intents=list(bind_decision.intents))
    kept, _, _ = filter_unbound_management(bind_decision, bind_verdict, db, enforce=True)
    ok_drop = len(kept.intents) == 0 and kept.decision == "no_trade"
    print(f"PASS bindable_model_drops_to_no_trade_not_open {ok_drop} {kept.decision}",
          flush=True)
    bound_decision = TradeDecision(decision="close", intents=[
        _BindableIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01,
                        management_authorization_id="auth-1")])
    bound_verdict = RiskVerdict(verdict="approved",
                                adjusted_intents=list(bound_decision.intents))
    bound_kept, _, _ = filter_unbound_management(
        bound_decision, bound_verdict, db, enforce=True,
        bound_authorization_ids=["auth-1"])
    print(f"PASS bound_reference_is_kept {len(bound_kept.intents) == 1}",
          flush=True)

    fails = [name for name, ok in
             [("enforce_false_only_observes", ok_observe),
              ("enforce_true_fails_closed", ok_gate),
              ("bindable_model_drops_to_no_trade_not_open", ok_drop),
              ("bound_reference_is_kept", len(bound_kept.intents) == 1)] if not ok]
    print(f"\nENFORCE PROBE FAIL={len(fails)} {fails}", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
