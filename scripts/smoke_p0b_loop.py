"""P0-b 端到端：模拟一个会犯错的模型，验证 Validator 回环真能让它自我修正。

关键验证三件事：
  1. 非法决策不会被静默吞掉，而是以 tool result 打回给模型
  2. 模型读到 issues 后能修正并成功提交
  3. 重试次数耗尽时会降级为 no_trade（而不是死循环烧 token）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.models import (  # noqa: E402
    MacroBrief, MarketBrief, OnchainBrief, StrategyRefs,
)
from agents.orchestrator import run_orchestrator  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.action_space import ActionSpace  # noqa: E402
from core.agent_recorder import AgentRecorder  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


class ScriptedLLM:
    """按脚本依次返回若干次 tool_call 响应，模拟模型的犯错与修正过程。"""

    def __init__(self, script: list[list[dict]]):
        self.script = list(script)
        self.calls = 0
        self.seen_tool_results: list[str] = []
        self._seen_ids: set[str] = set()

    def chat(self, messages, tools=None, tool_choice="auto"):
        # messages 是累积的，同一个 tool result 会在后续每一轮里重复出现，必须按 id 去重
        for m in messages:
            if m.get("role") == "tool":
                key = m.get("tool_call_id") or str(len(self.seen_tool_results))
                if key not in self._seen_ids:
                    self._seen_ids.add(key)
                    self.seen_tool_results.append(m.get("content") or "")
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        calls = self.script[idx]
        return {
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": calls}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "model": "scripted",
        }


def _tc(name, args, cid="c1"):
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}


def ctx():
    tmp = Path(tempfile.mkdtemp())
    db = AuditDatabase(tmp / "t.sqlite")
    return db, AgentRecorder(db, JsonlLogger(tmp / "logs"), tmp / "artifacts")


def run(script, space, db, rec, prices=None):
    llm = ScriptedLLM(script)
    d = run_orchestrator(
        llm=llm, model="scripted", recorder=rec, db=db,
        logger=JsonlLogger(Path(tempfile.mkdtemp()) / "logs"), run_id="r1",
        market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
        strategy=StrategyRefs(), risk_envelope={},
        portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]},
        action_space=space, prices=prices or {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0},
    )
    return d, llm


SPACE = ActionSpace.from_context(
    risk_config={"max_leverage": 20, "ai_max_leverage": 20, "strong_max_leverage": 50,
                 "max_open_positions": 8, "supported_symbols": ["BTCUSDT", "ETHUSDT"]},
    allow_new_positions=True,
    portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]},
)

print("\n[1] 模型第一次 SL 方向填反 -> 收到 issues -> 第二次修正成功")
script = [
    [_tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                     "leverage": 5, "stop_loss": 61000.0, "take_profit": 62000.0,
                     "invalidation": "跌破 60000 即证伪"}],
        "reasoning": "看多",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]})],
    [_tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                     "leverage": 5, "stop_loss": 59000.0, "take_profit": 62000.0,
                     "invalidation": "跌破 58000 即证伪"}],
        "reasoning": "看多（已修正止损方向）",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]}, cid="c2")],
]
d, llm = run(script, SPACE, *ctx())
check("最终决策是 open 而非 no_trade", d.decision == "open", d.decision)
check("采纳的是修正后的止损", d.intents and d.intents[0].stop_loss == 59000.0,
      str([i.stop_loss for i in d.intents]))
check("错误以 tool result 回传了", len(llm.seen_tool_results) == 1, str(len(llm.seen_tool_results)))
if llm.seen_tool_results:
    err = json.loads(llm.seen_tool_results[0])
    check("回传内容含 issues 清单", bool(err.get("issues")), str(err)[:200])
    check("issues 点明了方向错误", any("方向错误" in i for i in err["issues"]), str(err["issues"]))
    check("带 hint 与 attempt 计数", bool(err.get("hint")) and err.get("attempt") == "1/3", str(err.get("attempt")))

print("\n[2] 模型连续 3 次不改 -> 耗尽重试后降级 no_trade，不死循环")
bad = [_tc("submit_decision", {
    "decision": "open",
    "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                 "leverage": 5, "stop_loss": 61000.0, "take_profit": 62000.0,
                     "invalidation": "跌破 60000 即证伪"}],
    "reasoning": "坚持不改",
    "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]}, cid=f"c{i}") for i in range(1, 6)]
script2 = [[c] for c in bad]
d2, llm2 = run(script2, SPACE, *ctx())
check("降级为 no_trade", d2.decision == "no_trade", d2.decision)
check("reasoning 写明了降级原因", "业务校验未通过" in d2.reasoning, d2.reasoning[:200])
check("最多回传 2 次错误后收敛", len(llm2.seen_tool_results) == 2, str(len(llm2.seen_tool_results)))
check("LLM 未被无限调用", llm2.calls <= 3, f"calls={llm2.calls}")

print("\n[3] 禁开仓时：模型尝试 open 被拦，改用 close 管理存量成功")
closed_space = ActionSpace.from_context(
    risk_config={"max_leverage": 20, "ai_max_leverage": 20, "max_open_positions": 8,
                 "supported_symbols": ["BTCUSDT", "ETHUSDT"]},
    allow_new_positions=False,
    portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]},
)
script3 = [
    [_tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "ETHUSDT", "action": "open", "side": "SHORT", "qty": 0.1,
                     "leverage": 5, "stop_loss": 3100.0, "take_profit": 2900.0,
                     "invalidation": "突破 3050 即证伪"}],
        "reasoning": "想开空",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]})],
    [_tc("submit_decision", {
        "decision": "close",
        "intents": [{"symbol": "BTCUSDT", "action": "close", "side": "LONG", "qty": 0.01}],
        "reasoning": "既然不能开仓，那就把 BTC 平掉",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "close", "reason": "不再持有"}]}, cid="c2")],
]
d3, llm3 = run(script3, closed_space, *ctx())
check("最终执行了平仓", d3.decision == "close", d3.decision)
check("存量管理动作在禁开仓时可用", d3.intents and d3.intents[0].action == "close")
err3 = json.loads(llm3.seen_tool_results[0])
check("open 被拦且说明了原因", any("禁止新开仓" in i for i in err3["issues"]), str(err3["issues"]))

print("\n[4] 幻觉 symbol：模型平一个没持有的币，被拦后改平真实持仓")
script4 = [
    [_tc("submit_decision", {
        "decision": "close",
        "intents": [{"symbol": "ETHUSDT", "action": "close", "side": "LONG", "qty": 0.1}],
        "reasoning": "平 ETH",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]})],
    [_tc("submit_decision", {
        "decision": "close",
        "intents": [{"symbol": "BTCUSDT", "action": "close", "side": "LONG", "qty": 0.01}],
        "reasoning": "改平 BTC",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "close", "reason": "不再持有"}]}, cid="c2")],
]
d4, llm4 = run(script4, SPACE, *ctx())
check("最终平了真实持仓", d4.decision == "close" and d4.intents[0].symbol == "BTCUSDT",
      f"{d4.decision} {[i.symbol for i in d4.intents]}")
err4 = json.loads(llm4.seen_tool_results[0])
check("幻觉平仓被拦并列出真实持仓", any("当前无持仓" in i for i in err4["issues"]), str(err4["issues"]))

print("\n[5] 降级事件落库，复盘时可见")
db, rec = ctx()
run(script2, SPACE, db, rec)
rows = db.fetch_all("SELECT event_type, details FROM risk_events WHERE event_type='decision_downgraded'")
check("decision_downgraded 已落库", len(rows) >= 1, str(len(rows)))
if rows:
    check("details 含具体原因", "方向错误" in (rows[0]["details"] or ""), rows[0]["details"][:160])
db.close()

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
