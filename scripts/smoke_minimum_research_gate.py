"""最低研究流程回归：空仓可开仓时必须宽扫、深挖后才允许提交。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import agents.orchestrator as orch  # noqa: E402
from agents.models import MacroBrief, MarketBrief, OnchainBrief, StrategyRefs  # noqa: E402
from core.action_space import ActionSpace  # noqa: E402


class DB:
    def log_llm_call(self, **_kwargs):
        pass

    def log_risk_event(self, **_kwargs):
        pass

    def resolve_risk_incidents_by_type(self, _event_type):
        pass


class Logger:
    def log_llm(self, _name, _obj):
        return Path(tempfile.gettempdir()) / "minimum_research_gate.jsonl"


class Recorder:
    def record(self, **_kwargs):
        pass


class LLM:
    def __init__(self):
        self.choices = []

    def chat(self, _messages, tools=None, tool_choice="auto", **_kwargs):
        self.choices.append(tool_choice)
        forced = (tool_choice.get("function", {}).get("name")
                  if isinstance(tool_choice, dict) else "submit_decision")
        args = {
            "query_symbol_cards": {"symbols": ["BTCUSDT", "ETHUSDT"]},
            "query_symbol_detail": {"symbol": "BTCUSDT", "interval": "15m"},
            "submit_decision": {
                "decision": "no_trade", "intents": [], "position_reviews": [],
                "reasoning": "完成最低研究后证据仍不足，保持空仓。", "strategy_refs": [],
            },
        }[forced]
        return {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": f"call-{len(self.choices)}", "type": "function",
            "function": {"name": forced, "arguments": json.dumps(args, ensure_ascii=False)},
        }]}}], "usage": {}}


space = ActionSpace.from_context(
    risk_config={"supported_symbols": ["BTCUSDT", "ETHUSDT"]},
    allow_new_positions=True, portfolio={"positions": []}, channel="ai",
)
llm = LLM()
old_execute = orch.execute_tool


def fake_execute(name, args, **_kwargs):
    if name == "query_symbol_cards":
        return {"cards": [{"symbol": "BTCUSDT", "trend_15m": "up"}]}
    return {
        "symbol": args["symbol"], "last_price": 60000.0,
        "data_quality": {"ok": True, "observed_at_ms": 1_700_000_000_000, "issues": []},
    }


try:
    orch.execute_tool = fake_execute
    decision = orch.run_orchestrator(
        llm=llm, model="fake", recorder=Recorder(), db=DB(), logger=Logger(), run_id="gate",
        market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
        strategy=StrategyRefs(), risk_envelope={}, portfolio={"positions": []},
        action_space=space, prices={"BTCUSDT": 60000.0}, query_budget=4,
    )
finally:
    orch.execute_tool = old_execute

names = [item.get("function", {}).get("name") if isinstance(item, dict) else "auto"
         for item in llm.choices]
assert names[:2] == ["query_symbol_cards", "query_symbol_detail"], names
assert decision.decision == "no_trade", decision
print("PASS minimum research gate:", " -> ".join(names))
