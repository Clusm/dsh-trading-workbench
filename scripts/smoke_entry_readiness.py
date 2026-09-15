"""L02 离线回归：共享开仓就绪判定与授权时效。"""
from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.models import RiskVerdict, TradeDecision, TradeIntent  # noqa: E402
from core.decision_pipeline import filter_unauthorized_opens, sign_execution  # noqa: E402
from core.entry_readiness import evaluate_entry_readiness  # noqa: E402
from core.trading_scope import TradingScope  # noqa: E402


class DummyDB:
    def __init__(self) -> None:
        self.events: list[str] = []

    def log_risk_event(self, *, event_type: str, **_: object) -> None:
        self.events.append(event_type)


def _facts(scope: TradingScope) -> dict[str, object]:
    return {
        "service_accepting": True,
        "control_state": "normal",
        "equity_available": True,
        "account_risk_allow_new": True,
        "startup_safety_ready": True,
        "order_submission_ready": True,
        "market_cooldown_active": False,
        "data_budget_available": True,
        "rate_coordination_available": True,
        "order_mirror_ok": True,
        "protection_mirror_ok": True,
        "unknown_orders": 0,
        "submitting_orders": 0,
        "scope_hash": scope.scope_hash,
        "persistence_available": True,
    }


def main() -> None:
    scope = TradingScope.from_symbols(["BTCUSDT", "ETHUSDT"])
    now = time.time()
    snapshot = evaluate_entry_readiness(
        _facts(scope), now=now, scope=scope,
        runtime_instance_id="runtime-a", environment="demo",
    )
    assert snapshot.new_entry_allowed
    assert snapshot.authorization_expires_at == now + 90.0

    for key, reason in (
        ("order_mirror_ok", "ORDER_MIRROR_UNPROVEN"),
        ("protection_mirror_ok", "PROTECTION_MIRROR_MISMATCH"),
        ("market_cooldown_active", "MARKET_COOLDOWN"),
        ("persistence_available", "PERSISTENCE_UNAVAILABLE"),
    ):
        facts = _facts(scope)
        facts[key] = None if key == "order_mirror_ok" else (
            True if key == "market_cooldown_active" else False)
        blocked = evaluate_entry_readiness(
            facts, now=now, scope=scope,
            runtime_instance_id="runtime-a", environment="demo",
        )
        assert not blocked.new_entry_allowed, (key, blocked.as_dict())
        assert reason in blocked.blocking_reasons, (key, blocked.as_dict())

    intent = TradeIntent(
        symbol="BTCUSDT", action="open", side="LONG", qty=1, leverage=2,
        stop_loss=90, take_profit=110,
    )
    verdict = RiskVerdict(verdict="approved", adjusted_intents=[intent])
    decision = TradeDecision(
        decision="open", channel="merged", intents=[intent], pipeline_authorized=True,
    )
    signed = sign_execution(
        decision, verdict, "secret",
        snapshot.authorization_context(decision_id="decision-a"),
    )
    db = DummyDB()
    verified, _ = filter_unauthorized_opens(
        signed, verdict, db, "secret",
        {"environment": "demo", "scope_hash": scope.scope_hash,
         "runtime_instance_id": "runtime-a"},
    )
    assert verified.intents == [intent]

    missing_expiry = signed.model_copy(update={"pipeline_authorization_expires_at": None})
    missing_expiry_result, _ = filter_unauthorized_opens(
        missing_expiry, verdict, db, "secret",
        {"environment": "demo", "scope_hash": scope.scope_hash,
         "runtime_instance_id": "runtime-a"},
    )
    assert missing_expiry_result.intents == []

    no_context_result, _ = filter_unauthorized_opens(signed, verdict, db, "secret", None)
    assert no_context_result.intents == []

    missing_scope = _facts(scope)
    missing_scope.pop("scope_hash")
    unproven = evaluate_entry_readiness(
        missing_scope, now=now, scope=scope,
        runtime_instance_id="runtime-a", environment="demo",
    )
    assert not unproven.new_entry_allowed
    assert "SCOPE_HASH_UNPROVEN" in unproven.blocking_reasons

    expired = signed.model_copy(update={"pipeline_authorization_expires_at": now - 1})
    expired_result, _ = filter_unauthorized_opens(
        expired, verdict, db, "secret",
        {"environment": "demo", "scope_hash": scope.scope_hash,
         "runtime_instance_id": "runtime-a"},
    )
    assert expired_result.intents == []
    assert db.events.count("pipeline_bypass_blocked") == 3

    print("PASS entry readiness: shared gate, missing evidence, expiry and context binding")


if __name__ == "__main__":
    main()
