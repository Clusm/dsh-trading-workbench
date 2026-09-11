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
from agents.orchestrator import SYSTEM_PROMPT, run_orchestrator  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.action_space import ActionSpace  # noqa: E402
from core.agent_recorder import AgentRecorder  # noqa: E402

ok = fail = 0
OBSERVED_AT_MS = 1_700_000_000_000


def evidence(side: str, trigger_price: float) -> dict:
    is_long = side == "LONG"
    return {
        "market_regime": "trend_up" if is_long else "trend_down",
        "higher_timeframe_trend": "up" if is_long else "down",
        "supporting_evidence": ["闭合K线趋势一致"], "counter_evidence": [],
        "trigger_price": trigger_price,
        "invalidation_price": trigger_price * (0.98 if is_long else 1.02),
        "invalidation_operator": "close_below" if is_long else "close_above",
        "confirmation_interval": "15m", "risk_reward_ratio": 2.0,
        "chase_distance_pct": 0.2, "data_observed_at_ms": OBSERVED_AT_MS,
        "confidence_basis": "趋势与结构一致",
    }


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
                    content = m.get("content") or ""
                    try:
                        parsed = json.loads(content)
                    except Exception:
                        parsed = {}
                    if isinstance(parsed, dict) and parsed.get("issues"):
                        self.seen_tool_results.append(content)
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


def run(script, space, db, rec, prices=None, quality_ok: bool = True):
    import agents.orchestrator as orch

    llm = ScriptedLLM(script)
    original_execute = orch.execute_tool
    orch.execute_tool = lambda _name, args: {
        "symbol": str(args.get("symbol") or "").upper(),
        "data_quality": {"ok": quality_ok, "observed_at_ms": OBSERVED_AT_MS,
                         "issues": [] if quality_ok else ["stale_last_closed_kline"]},
    }
    try:
        d = run_orchestrator(
            llm=llm, model="scripted", recorder=rec, db=db,
            logger=JsonlLogger(Path(tempfile.mkdtemp()) / "logs"), run_id="r1",
            market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
            strategy=StrategyRefs(), risk_envelope={},
            portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]},
            action_space=space, prices=prices or {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0},
        )
    finally:
        orch.execute_tool = original_execute
    return d, llm


SPACE = ActionSpace.from_context(
    risk_config={"max_leverage": 20, "ai_max_leverage": 20, "strong_max_leverage": 50,
                 "max_open_positions": 8, "supported_symbols": ["BTCUSDT", "ETHUSDT"]},
    allow_new_positions=True,
    portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]},
)

print("\n[1] 模型第一次 SL 方向填反 -> 收到 issues -> 第二次修正成功")
script = [
    [_tc("query_symbol_detail", {"symbol": "BTCUSDT", "interval": "15m"}, cid="q1"),
     _tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                     "leverage": 5, "stop_loss": 61000.0, "take_profit": 62000.0,
                     "invalidation": "跌破 60000 即证伪", "strategy_id": "ai:trend_follow:v1",
                     "evidence": evidence("LONG", 60000.0)}],
        "reasoning": "看多",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]})],
    [_tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                     "leverage": 5, "stop_loss": 59000.0, "take_profit": 62000.0,
                     "invalidation": "跌破 58000 即证伪", "strategy_id": "ai:trend_follow:v1",
                     "evidence": evidence("LONG", 60000.0)}],
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
                 "invalidation": "跌破 60000 即证伪", "strategy_id": "ai:trend_follow:v1",
                 "evidence": evidence("LONG", 60000.0)}],
    "reasoning": "坚持不改",
    "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]}, cid=f"c{i}") for i in range(1, 6)]
script2 = [[c] for c in bad]
script2[0].insert(0, _tc("query_symbol_detail", {"symbol": "BTCUSDT", "interval": "15m"}, cid="q1"))
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
    [_tc("query_symbol_detail", {"symbol": "ETHUSDT", "interval": "15m"}, cid="q3"),
     _tc("submit_decision", {
        "decision": "open",
        "intents": [{"symbol": "ETHUSDT", "action": "open", "side": "SHORT", "qty": 0.1,
                     "leverage": 5, "stop_loss": 3100.0, "take_profit": 2900.0,
                     "invalidation": "突破 3050 即证伪", "strategy_id": "ai:trend_follow:v1",
                     "evidence": evidence("SHORT", 3000.0)}],
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

print("\n[6] 开仓证据必须来自本轮成功的 detail 查询")
valid_open = {
    "decision": "open",
    "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.01,
                 "leverage": 5, "stop_loss": 59000.0, "take_profit": 62000.0,
                 "invalidation": "15m收盘跌破 59000 即证伪", "strategy_id": "ai:trend_follow:v1",
                 "evidence": evidence("LONG", 60000.0)}],
    "reasoning": "顺势候选",
    "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}],
}
no_trade = {
    "decision": "no_trade", "intents": [], "reasoning": "没有完整证据，本轮不交易",
    "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}],
}
d6, llm6 = run([
    [_tc("submit_decision", valid_open, cid="missing-query")],
    [_tc("submit_decision", no_trade, cid="fallback-no-trade")],
], SPACE, *ctx())
check("未做 detail 查询的开仓被拒绝", d6.decision == "no_trade")
check("拒绝原因点明本轮 detail 查询",
      any("未完成本轮 query_symbol_detail" in item
          for item in json.loads(llm6.seen_tool_results[0])["issues"]),
      llm6.seen_tool_results[0])

wrong_time_open = json.loads(json.dumps(valid_open, ensure_ascii=False))
wrong_time_open["intents"][0]["evidence"]["data_observed_at_ms"] += 1
d7, llm7 = run([
    [_tc("query_symbol_detail", {"symbol": "BTCUSDT", "interval": "15m"}, cid="q7"),
     _tc("submit_decision", wrong_time_open, cid="wrong-time")],
    [_tc("submit_decision", no_trade, cid="fallback-7")],
], SPACE, *ctx())
check("伪造/抄错数据时点的开仓被拒绝", d7.decision == "no_trade")
check("拒绝原因点明数据时点不一致",
      any("data_observed_at_ms 与本轮查询不一致" in item
          for item in json.loads(llm7.seen_tool_results[0])["issues"]),
      llm7.seen_tool_results[0])

d8, llm8 = run([
    [_tc("query_symbol_detail", {"symbol": "BTCUSDT", "interval": "15m"}, cid="q8"),
     _tc("submit_decision", valid_open, cid="stale-data")],
    [_tc("submit_decision", no_trade, cid="fallback-8")],
], SPACE, *ctx(), quality_ok=False)
check("陈旧 detail 数据不能开仓", d8.decision == "no_trade")
check("陈旧原因透传到校验结果", "stale_last_closed_kline" in llm8.seen_tool_results[0],
      llm8.seen_tool_results[0])

print("\n[7] no_trade 是无压力的完整决策")
d9, llm9 = run([[_tc("submit_decision", no_trade, cid="clean-no-trade")]], SPACE, *ctx())
check("no_trade 首轮直接通过", d9.decision == "no_trade" and llm9.calls == 1,
      f"decision={d9.decision} calls={llm9.calls}")
check("系统提示已移除催单语句", "不要轻易 no_trade" not in SYSTEM_PROMPT and "积极寻找" not in SYSTEM_PROMPT)

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
