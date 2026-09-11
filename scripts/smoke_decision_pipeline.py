"""K01/K02 单一开仓入口、旁路闸门和命名检查结果回归。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.execution import PaperBroker  # noqa: E402
from agents.models import DecisionEvidence, RiskVerdict, TradeDecision, TradeIntent  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.decision_pipeline import authorize_decision, normalize_checks, filter_unauthorized_opens  # noqa: E402
from core.decision_policy import evaluate_open_intent  # noqa: E402
from core.portfolio_policy import evaluate_portfolio_intent  # noqa: E402

passed = failed = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


tmp = Path(tempfile.mkdtemp())
db = AuditDatabase(tmp / "audit.sqlite")
evidence = DecisionEvidence(
    market_regime="trend_up", higher_timeframe_trend="up",
    supporting_evidence=["闭合K线趋势一致"], counter_evidence=[],
    trigger_price=100, invalidation_price=95, invalidation_operator="close_below",
    confirmation_interval="15m", risk_reward_ratio=2, chase_distance_pct=0.2,
    data_observed_at_ms=1_700_000_000_000, confidence_basis="结构一致",
)
intent = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.01,
                     leverage=5, stop_loss=95, take_profit=110,
                     strategy_id="ai:trend:v1", invalidation="close below 95", evidence=evidence)
checks = evaluate_open_intent(intent) + evaluate_portfolio_intent(
    intent, {"positions": []}, {}, db,
)

print("\n[1] 合格 AI 开仓获得唯一管线授权和完整轨迹")
authorized = authorize_decision(
    TradeDecision(decision="open", channel="merged", intents=[intent], policy_checks=checks),
    db=db, run_id="r-1",
)
check("合格开仓已授权", authorized.pipeline_authorized and len(authorized.intents) == 1)
check("七项开仓检查齐全", len(authorized.policy_checks) == 7)
check("检查状态/模式统一", all(item["status"] in {"PASS", "WARN", "BLOCK", "NOT_EVALUATED"}
      and item["mode"] in {"off", "shadow", "enforce"} for item in authorized.policy_checks))
check("每项有 evidence_at 字段", all("evidence_at" in item for item in authorized.policy_checks))
check("轨迹含 Guardian 到 Outbox", [item["stage"] for item in authorized.pipeline_trace] == [
    "position_guardian", "data_and_evidence", "decision_quality",
    "portfolio_context", "risk_guard", "outbox_usdm",
])

print("\n[2] 缺检查的新开仓 fail-closed，但退出分支保留")
close = TradeIntent(symbol="ETHUSDT", action="close", side="LONG", qty=0.2)
blocked = authorize_decision(
    TradeDecision(decision="open", channel="merged", intents=[intent, close]), db=db,
)
check("未检查open被移除", all(item.action != "open" for item in blocked.intents))
check("close仍保留", len(blocked.intents) == 1 and blocked.intents[0].action == "close")
check("管线拦截已审计", db.fetch_one(
    "SELECT COUNT(*) n FROM risk_events WHERE event_type='pipeline_open_blocked'"
)["n"] == 1)

print("\n[3] Broker 运行时拒绝绕过管线的 open，不影响退出")
broker = PaperBroker(tmp / "paper.json", initial_equity=500)
broker.require_pipeline_authorization = True
verdict = RiskVerdict(verdict="approved", adjusted_intents=[intent])
receipts = broker.execute(
    TradeDecision(decision="open", intents=[intent]), verdict,
    {"BTCUSDT": 100}, {}, db,
)
check("未授权open零成交", receipts == [] and broker.state["positions"] == [])
check("旁路拦截事件已记录", db.fetch_one(
    "SELECT COUNT(*) n FROM risk_events WHERE event_type='pipeline_bypass_blocked'"
)["n"] == 1)
close_only = authorize_decision(
    TradeDecision(decision="close", channel="merged", intents=[close]), db=db,
)
check("退出无需开仓证据仍获管线放行", close_only.pipeline_authorized
      and close_only.intents[0].action == "close")

print("\n[4] 非法检查值不能静默变成 PASS")
normalized = normalize_checks([{"check_id": "bad", "mode": "typo", "status": "OK"}])[0]
check("非法模式/状态 fail-closed", normalized["mode"] == "enforce"
      and normalized["status"] == "NOT_EVALUATED")

mixed, mixed_verdict = filter_unauthorized_opens(
    TradeDecision(decision="open", intents=[intent, close]),
    RiskVerdict(verdict="approved", adjusted_intents=[intent, close]), db,
)
check("混合批次保留两份集合中的退出", mixed.intents == [close]
      and mixed_verdict.adjusted_intents == [close] and mixed.decision == "close")
injected, injected_verdict = filter_unauthorized_opens(
    TradeDecision(decision="close", intents=[close]),
    RiskVerdict(verdict="approved", adjusted_intents=[intent, close]), db,
)
check("调整集合单独注入开仓仍被拦截", injected_verdict.adjusted_intents == [close])

db.close()
print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
