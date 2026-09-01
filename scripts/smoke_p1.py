"""P1 冒烟测试：position_reviews 必答 + invalidation 落库 + Outcome Tracker。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents import orchestrator as orch_mod  # noqa: E402
from agents.models import (  # noqa: E402
    MacroBrief, MarketBrief, OnchainBrief, PositionReview, RiskVerdict,
    StrategyRefs, TradeDecision, TradeIntent,
)
from agents.orchestrator import (  # noqa: E402
    DecisionValidationError, _build_decision, build_user_prompt, run_orchestrator,
)
from agents.execution import PaperBroker  # noqa: E402
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


def make_ctx():
    tmp = Path(tempfile.mkdtemp())
    db = AuditDatabase(tmp / "t.sqlite")
    return db, AgentRecorder(db, JsonlLogger(tmp / "logs"), tmp / "artifacts"), tmp


PORTFOLIO = {"positions": [
    {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01, "entry_price": 60000.0,
     "source": "ai", "opened_at": "2026-09-01T00:00:00+00:00", "expected_holding_minutes": 60},
]}
SPACE = ActionSpace.from_context(
    risk_config={"max_leverage": 20, "ai_max_leverage": 20, "ai_strong_max_leverage": 25,
                 "max_open_positions": 8, "supported_symbols": ["BTCUSDT", "ETHUSDT"]},
    allow_new_positions=True, portfolio=PORTFOLIO,
)

print("\n[1] position_reviews schema 校验")
good = {"decision": "no_trade", "intents": [], "reasoning": "x",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]}
d, issues = _build_decision(good, strict=True)
check("合法 reviews 通过", issues == [] and d.position_reviews[0].verdict == "hold", str(issues))
check("PositionReview 模型解析", isinstance(d.position_reviews[0], PositionReview))

bad1 = {"decision": "no_trade", "intents": [], "reasoning": "x",
        "position_reviews": [{"symbol": "BTCUSDT", "verdict": "HODL"}]}
try:
    _build_decision(bad1, strict=True)
    check("verdict 非法被拦", False, "未抛错")
except DecisionValidationError as e:
    check("verdict 非法被拦", any("verdict" in i for i in e.issues), str(e.issues))
bad2 = {"decision": "no_trade", "intents": [], "reasoning": "x",
        "position_reviews": [{"verdict": "hold"}]}
try:
    _build_decision(bad2, strict=True)
    check("缺 symbol 被拦", False, "未抛错")
except DecisionValidationError as e:
    check("缺 symbol 被拦", any("symbol" in i for i in e.issues), str(e.issues))
bad3 = {"decision": "no_trade", "intents": [], "reasoning": "x"}  # 缺 position_reviews
try:
    _build_decision(bad3, strict=True)
    check("strict 下缺 position_reviews 被拦", False, "未抛错")
except DecisionValidationError as e:
    check("strict 下缺 position_reviews 被拦", any("position_reviews" in i for i in e.issues), str(e.issues))
d, issues = _build_decision(bad3, strict=False)
check("宽松路径缺省不误伤", issues == [] and d.position_reviews == [], str(issues))

print("\n[2] ActionSpace 交叉校验")
check("全持仓覆盖通过", SPACE.check_position_reviews(
    [{"symbol": "BTCUSDT", "verdict": "hold"}], []) == [],
    str(SPACE.check_position_reviews([{"symbol": "BTCUSDT", "verdict": "hold"}], [])))
missing = SPACE.check_position_reviews([], [])
check("漏掉持仓被点名", any("缺少对 BTCUSDT 的表态" in i for i in missing), str(missing))
fake = SPACE.check_position_reviews([{"symbol": "ETHUSDT", "verdict": "hold"}], [])
check("评述非持仓被拦", any("非持仓 symbol：ETHUSDT" in i for i in fake), str(fake))
mismatch = SPACE.check_position_reviews(
    [{"symbol": "BTCUSDT", "verdict": "close"}], [])
check("声称 close 但无 close intent 被拦",
      any("没有对应的 close 动作" in i for i in mismatch), str(mismatch))
consistent = SPACE.check_position_reviews(
    [{"symbol": "BTCUSDT", "verdict": "close"}],
    [TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01)])
check("声称 close 且有对应 intent 通过", consistent == [], str(consistent))
empty_space = ActionSpace.from_context(risk_config={}, allow_new_positions=True,
                                       portfolio={"positions": []})
check("无持仓时给了 reviews 被拦",
      any("应为空数组" in i for i in empty_space.check_position_reviews([{"symbol": "X", "verdict": "hold"}], [])))

print("\n[3] 端到端：模型漏掉持仓表态 -> 收到 issues -> 修正后成功")
class SLLM:
    def __init__(self, script): self.script = script; self.calls = 0; self.results = []
    def chat(self, messages, tools=None, tool_choice="auto"):
        for m in messages:
            if m.get("role") == "tool":
                self.results.append(json.loads(m["content"] or "{}"))
        idx = min(self.calls, len(self.script) - 1); self.calls += 1
        return {"choices": [{"message": {"role": "assistant", "content": None,
                                         "tool_calls": self.script[idx]},
                             "finish_reason": "tool_calls"}], "usage": {}, "model": "s"}

def sub(payload, cid):
    return {"id": cid, "type": "function",
            "function": {"name": "submit_decision",
                         "arguments": json.dumps(payload, ensure_ascii=False)}}

db, rec, _ = make_ctx()
script = [
    [sub({"decision": "no_trade", "intents": [], "reasoning": "无操作",
          "position_reviews": []}, "c1")],   # 漏了 BTCUSDT 的表态
    [sub({"decision": "no_trade", "intents": [], "reasoning": "无操作",
          "position_reviews": [{"symbol": "BTCUSDT", "verdict": "hold", "reason": "趋势未破"}]}, "c2")],
]
llm = SLLM(script)
orig = orch_mod.execute_tool
orch_mod.execute_tool = lambda n, a: {"ok": True}
try:
    d = run_orchestrator(llm=llm, model="s", recorder=rec, db=db,
                         logger=JsonlLogger(Path(tempfile.mkdtemp()) / "logs"), run_id="r1",
                         market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
                         strategy=StrategyRefs(), risk_envelope={},
                         portfolio=PORTFOLIO, action_space=SPACE)
finally:
    orch_mod.execute_tool = orig
err = llm.results[0]
check("漏表态收到 issues", any("缺少对 BTCUSDT 的表态" in i for i in err.get("issues", [])),
      str(err.get("issues")))
check("最终成功提交", d.decision == "no_trade" and d.position_reviews[0].verdict == "hold",
      f"{d.decision} {d.position_reviews}")

print("\n[4] invalidation：开仓必填 + 落库")
db2, rec2, _ = make_ctx()
no_inv = {"decision": "open", "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG",
                                          "qty": 0.01, "stop_loss": 59000.0, "take_profit": 62000.0}],
          "position_reviews": [], "reasoning": "x"}
try:
    _build_decision(no_inv, strict=True)
    check("open 缺 invalidation 被拦", False, "未抛错")
except DecisionValidationError as e:
    check("open 缺 invalidation 被拦", any("invalidation" in i for i in e.issues), str(e.issues))
with_inv = {**no_inv,
            "intents": [{**no_inv["intents"][0], "invalidation": "跌破前低 58000 即证伪"}]}
d, issues = _build_decision(with_inv, strict=True)
check("带 invalidation 通过", issues == [] and d.intents[0].invalidation == "跌破前低 58000 即证伪", str(issues))
db2.log_trade_intent(intent_id="i-1", decision_id="d-1", run_id="r", intent=d.intents[0], status="approved")
row = db2.fetch_one("SELECT invalidation FROM trade_intents WHERE intent_id='i-1'")
check("invalidation 落库", row["invalidation"] == "跌破前低 58000 即证伪", str(row["invalidation"]))

print("\n[5] Outcome Tracker：开仓 -> 平仓 -> 归因到 thesis")
from datetime import datetime, timedelta, timezone as _tz  # noqa: E402
db3, rec3, tmp3 = make_ctx()
broker = PaperBroker(tmp3 / "broker.json", initial_equity=200.0)
risk_cfg = {"max_leverage": 20, "max_margin_usage": 0.80, "max_position_notional": 100000,
            "max_open_positions": 8, "max_risk_per_trade": 0.02,
            "channel_status": {"ai": {"allow_new": True, "margin_used": 0, "margin_cap": 1000, "equity": 200}},
            "supported_symbols": ["BTCUSDT", "ETHUSDT"]}
# 注意：PaperBroker 新建后按 60/40 重平衡，AI 账本只有 80 USDT，保证金上限 = 80*0.8 = 64
open_intent = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.001, leverage=5,
                          stop_loss=59000.0, take_profit=62000.0, confidence=0.5,
                          invalidation="跌破 59000 即证伪", expected_holding_minutes=45,
                          client_order_id="mta-open-0")
db3.log_trade_intent(intent_id="i-open", decision_id="d-open", run_id="r", intent=open_intent, status="approved")
dec_open = TradeDecision(decision="open", intents=[open_intent])
v_open = RiskVerdict(verdict="approved", reasons=["ok"], adjusted_intents=[open_intent])
prices = {"BTCUSDT": 60000.0}
broker.execute(dec_open, v_open, prices, risk_cfg, db3)

close_intent = TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.001,
                           client_order_id="mta-close-0")
db3.log_trade_intent(intent_id="i-close", decision_id="d-close", run_id="r", intent=close_intent, status="approved")
dec_close = TradeDecision(decision="close", intents=[close_intent])
v_close = RiskVerdict(verdict="approved", reasons=["ok"], adjusted_intents=[close_intent])
broker.execute(dec_close, v_close, {"BTCUSDT": 61500.0}, risk_cfg, db3)

outcomes = db3.fetch_all("SELECT * FROM trade_outcomes")
check("outcome 已写入", len(outcomes) == 1, f"实际 {len(outcomes)}")
if outcomes:
    o = dict(outcomes[0])
    check("归因到 open intent", o["intent_id"] == "i-open", str(o.get("intent_id")))
    check("盈亏正确 (LONG 0.001*(61500-60000)=1.5)", abs(float(o["pnl"]) - 1.5) < 1e-6, str(o["pnl"]))
    check("pnl_pct 正确", abs(float(o["pnl_pct"]) - 2.5) < 1e-6, str(o["pnl_pct"]))
    check("invalidation 从 open intent 带回", o["invalidation"] == "跌破 59000 即证伪",
          str(o.get("invalidation")))
    check("expected_holding 记录", o["expected_holding_minutes"] == 45, str(o.get("expected_holding_minutes")))
    check("holding_minutes 非负", o["holding_minutes"] is not None and o["holding_minutes"] >= 0,
          str(o.get("holding_minutes")))
    check("exit_reason", o["exit_reason"] == "ai_decision", str(o.get("exit_reason")))
    check("entry/exit 价格", float(o["entry_price"]) == 60000.0 and float(o["exit_price"]) == 61500.0,
          f"{o['entry_price']}->{o['exit_price']}")

print("\n[6] holding pressure：超时持仓会在提示词里被点名")
now_utc = datetime.now(_tz.utc)
old = (now_utc - timedelta(minutes=120)).isoformat()   # 2 小时前开仓 -> 超时
recent = (now_utc - timedelta(minutes=1)).isoformat()  # 1 分钟前开仓 -> 不超时
over_portfolio = {"positions": [
    {"symbol": "ETHUSDT", "side": "SHORT", "qty": 0.1, "entry_price": 3000.0, "source": "ai",
     "opened_at": old, "expected_holding_minutes": 10},
    {"symbol": "SOLUSDT", "side": "LONG", "qty": 1, "entry_price": 100.0, "source": "ai",
     "opened_at": recent, "expected_holding_minutes": 1000},
]}
prompt = build_user_prompt(MarketBrief(), MacroBrief(), OnchainBrief(), StrategyRefs(),
                           {}, portfolio=over_portfolio, action_space=SPACE)
check("超时持仓被点名", "ETHUSDT 已持有" in prompt and "超过你设定的预期持有时间 10 分钟" in prompt,
      "ETHUSDT" in prompt)
check("未超时的不点", "SOLUSDT" not in prompt.split("持仓超时提醒")[1] if "持仓超时提醒" in prompt else True)
check("压力提示以警告形式注入", "持仓超时提醒" in prompt)

print("\n[7] 全链路兼容：graph 模块仍可导入")
from core import graph, service  # noqa: F401
check("graph/service 导入正常", True)

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
