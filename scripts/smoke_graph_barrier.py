"""双通道会合屏障冒烟测试：不联网、不读写真实 data。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import core.graph as graph_module  # noqa: E402
from agents.models import (  # noqa: E402
    ChannelDecision,
    RiskVerdict,
    StrategyRefs,
    TradeDecision,
    TradeIntent,
)


calls = {"risk": 0, "execute": 0}
graph_module.get_live_prices = lambda symbols: {symbol: 60000.0 for symbol in symbols}
intent = TradeIntent(symbol="BTCUSDT", side="LONG", qty=0.001)

# 把所有外部依赖替换为进程内 stub，只测试 LangGraph 的分支调度行为。
graph_module.run_signal_scan = lambda **kwargs: []
graph_module.strategy_library.run_strategy_agent = (
    lambda **kwargs: StrategyRefs(summary="stub")
)
graph_module.strategy_trader.run_strategy_trader = (
    lambda **kwargs: ChannelDecision(
        channel="strategy", decision="open", intents=[intent], reasoning="stub"
    )
)
graph_module.orchestrator.run_orchestrator = (
    lambda **kwargs: TradeDecision(decision="no_trade", channel="ai", reasoning="stub")
)


def fake_evaluate_risk(decision, *args, **kwargs):
    calls["risk"] += 1
    return RiskVerdict(
        verdict="approved", reasons=[], adjusted_intents=list(decision.intents)
    )


graph_module.risk.evaluate_risk = fake_evaluate_risk


class DummyDB:
    def log_risk_event(self, **kwargs):
        pass

    def log_decision_conflict(self, **kwargs):
        pass

    def log_trade_intent(self, **kwargs):
        pass


class DummyRecorder:
    def record(self, **kwargs):
        pass


class DummyBroker:
    mode = "paper"
    state = {"cash": 500.0, "positions": []}

    def positions(self):
        return []

    def execute(self, decision, verdict, prices, risk_config, db):
        calls["execute"] += 1
        return [{"status": "stub-filled"}]


graph = graph_module.build_graph(
    llm=object(),
    model="stub",
    db=DummyDB(),
    logger=object(),
    recorder=DummyRecorder(),
    risk_config={},
    knowledge_dir=ROOT / "knowledge",
    enable_live_data=False,
    broker=DummyBroker(),
    available_cash=500.0,
    signal_scanner_config={},
)
result = graph.invoke({"run_id": "barrier-test", "decision_id": "decision-test"})

assert calls["risk"] == 1, calls
assert calls["execute"] == 1, calls
assert result.get("channels_merged") is True
assert len(result.get("execution") or []) == 1

print(f"PASS graph barrier: risk={calls['risk']} execute={calls['execute']}")
