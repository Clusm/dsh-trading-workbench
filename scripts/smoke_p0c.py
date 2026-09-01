"""P0-c 冒烟测试：两级查询 + 查询预算 + 强制收口。

验证：
  1. K 线列式压缩（离线，合成数据）
  2. query_symbol_detail 的 include_candles 开关
  3. 预算计数：超过上限的查询被拒，错误说明原因
  4. 预算耗尽后 tool_choice 被钉死到 submit_decision（不会拖成无声 no_trade）
  5. 每次查询结果附带 _budget（已用/上限/已查清单），防重复查询
  6. query_review_library 不计入预算
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents import orchestrator as orch_mod  # noqa: E402
from agents.models import MacroBrief, MarketBrief, OnchainBrief, StrategyRefs  # noqa: E402
from agents.orchestrator import run_orchestrator  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from data.market_query import _pack_candles  # noqa: E402
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
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.received_choices: list = []
        self.tool_results: list[dict] = []

    def chat(self, messages, tools=None, tool_choice="auto"):
        self.received_choices.append(tool_choice)
        for m in messages:
            if m.get("role") == "tool":
                self.tool_results.append(json.loads(m["content"] or "{}"))
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return {
            "choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": self.script[idx]},
                         "finish_reason": "tool_calls"}],
            "usage": {}, "model": "scripted",
        }


def _q(symbol, cid):
    return {"id": cid, "type": "function",
            "function": {"name": "query_symbol_detail",
                         "arguments": json.dumps({"symbol": symbol, "interval": "15m"})}}


def _submit(decision="no_trade", cid="sub"):
    return {"id": cid, "type": "function",
            "function": {"name": "submit_decision",
                         "arguments": json.dumps({"decision": decision, "intents": [], "position_reviews": [], "reasoning": "x"})}}


def _review(cid):
    return {"id": cid, "type": "function",
            "function": {"name": "query_review_library", "arguments": "{}"}}


def make_ctx():
    tmp = Path(tempfile.mkdtemp())
    db = AuditDatabase(tmp / "t.sqlite")
    return db, AgentRecorder(db, JsonlLogger(tmp / "logs"), tmp / "artifacts")


def _fake_execute_tool(name, args):
    """预算逻辑测试用假行情：不发真实网络请求，保证测试快且确定。"""
    if name == "query_symbol_detail":
        return {"symbol": str(args.get("symbol", "")).upper(), "last_price": 100.0,
                "trend": "up", "candles": {"n": 20, "interval": args.get("interval", "15m")}}
    if name == "query_symbol_cards":
        syms = args.get("symbols") or []
        return {"n": len(syms), "symbols": [{"symbol": str(s).upper(), "last_price": 100.0} for s in syms]}
    return {"top_n": 1, "symbols": []}


def run(script, db, rec, budget=8):
    llm = ScriptedLLM(script)
    orig = orch_mod.execute_tool
    orch_mod.execute_tool = _fake_execute_tool
    try:
        d = run_orchestrator(
            llm=llm, model="scripted", recorder=rec, db=db,
            logger=JsonlLogger(Path(tempfile.mkdtemp()) / "logs"), run_id="r1",
            market=MarketBrief(), macro=MacroBrief(), onchain=OnchainBrief(),
            strategy=StrategyRefs(), risk_envelope={},
            portfolio={"positions": []}, query_budget=budget,
        )
    finally:
        orch_mod.execute_tool = orig
    return d, llm


print("\n[1] K 线列式压缩（离线）")
kl = [[1700000000000 + i * 900000, 60000.123456, 60100.5, 59900.2, 60050.77,
       12.345678, 0, 740000.123, 0, 0, 0, 0] for i in range(25)]
p = _pack_candles(kl, "15m")
check("列式字段齐全", {"t_sec", "o", "h", "l", "c", "v"} <= set(p.keys()))
check("相对时间戳", p["t_sec"][1] == 900 and p["t_sec"][-1] == 19 * 900)
check("有效数字取整", p["o"][0] == 60000.1, str(p["o"][0]))
check("只保留最近 20 根", p["n"] == 20, str(p["n"]))
empty = _pack_candles([], "15m")
check("空 K 线不崩", empty["n"] == 0)

print("\n[2] 预算内正常查询 + _budget 附带已查清单")
db, rec = make_ctx()
script = [
    [_q("BTCUSDT", "a"), _q("ETHUSDT", "b"), _q("SOLUSDT", "c")],
    [_submit()],
]
d, llm = run(script, db, rec, budget=8)
budgets = [r.get("_budget") for r in llm.tool_results if r.get("_budget")]
check("3 次查询各带 _budget", len(budgets) == 3, str(len(budgets)))
b3 = budgets[-1]
check("used 累计到 3", b3["used"] == 3 and b3["limit"] == 8, str(b3))
check("queried 记录全部品种", b3["queried"] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"], str(b3["queried"]))
check("本轮正常收尾为 no_trade", d.decision == "no_trade", d.decision)

print("\n[3] 预算耗尽：第 9 次查询被拒 + tool_choice 钉死 submit_decision")
db, rec = make_ctx()
script = [
    [_q("S1", "1"), _q("S2", "2"), _q("S3", "3")],
    [_q("S4", "4"), _q("S5", "5"), _q("S6", "6")],
    [_q("S7", "7"), _q("S8", "8")],
    [_q("S9", "9")],
    [_submit()],
]
d, llm = run(script, db, rec, budget=8)
errs = [r for r in llm.tool_results if "已用尽" in (r.get("error") or "")]
check("第 9 次查询被拒且说明预算用尽", len(errs) == 1, str([e.get("error") for e in errs]))
check("拒绝信息要求直接提交", "submit_decision" in (errs[0]["error"] if errs else ""))
pinned = [c for c in llm.received_choices if isinstance(c, dict)]
check("预算耗尽后 tool_choice 被钉到 submit_decision", any(
    c.get("function", {}).get("name") == "submit_decision" for c in pinned), str(pinned))
check("最终仍正常提交（不是静默 no_trade 以外的异常）", d.decision == "no_trade", d.decision)

print("\n[4] query_review_library 不计入预算")
db, rec = make_ctx()
script = [
    [_q("BTCUSDT", "a"), _review("r"), _q("ETHUSDT", "b")],
    [_submit()],
]
d, llm = run(script, db, rec, budget=2)
budgets = [r.get("_budget") for r in llm.tool_results if r.get("_budget")]
check("2 次行情查询均放行（review 不占额度）", len(budgets) == 2 and budgets[-1]["used"] == 2,
      str([b["used"] for b in budgets]))

print("\n[5] 预算为 1：只放行一次查询")
db, rec = make_ctx()
script = [[_q("S1", "1"), _q("S2", "2")], [_submit()]]
d, llm = run(script, db, rec, budget=1)
errs = [r for r in llm.tool_results if "已用尽" in (r.get("error") or "")]
check("第 2 次查询被拒", len(errs) == 1, str(len(errs)))

print("\n[6] query_symbol_detail 真实返回体压缩（网络）")
try:
    from data.market_query import query_symbol_detail, query_symbol_cards
    detail = query_symbol_detail("BTCUSDT", interval="15m", limit=200)
    check("candles 为列式结构", "candles" in detail and "t_sec" in detail["candles"],
          str(list(detail.keys())))
    check("列式 n=20", detail["candles"]["n"] == 20, str(detail["candles"].get("n")))
    nodl = query_symbol_detail("BTCUSDT", interval="15m", include_candles=False)
    check("include_candles=False 时无 K 线", nodl["candles"]["n"] == 0)
    cards = query_symbol_cards(["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    check("卡片批量返回", cards["n"] == 3 and len(cards["symbols"]) == 3, str(cards.get("n")))
    check("卡片字段精简", set(cards["symbols"][0].keys()) ==
          {"symbol", "last_price", "pct_24h", "quote_volume_24h", "high_24h", "low_24h"},
          str(cards["symbols"][0].keys()))
    import json as _j
    card_chars = len(_j.dumps(cards, ensure_ascii=False))
    detail_chars = len(_j.dumps(detail, ensure_ascii=False))
    check(f"3 张卡({card_chars}字符) 远小于 1 次 detail({detail_chars}字符)",
          card_chars < detail_chars * 0.5, f"{card_chars} vs {detail_chars}")
except Exception as e:
    check("真实接口可用", False, f"{type(e).__name__}: {e}")

db.close()
print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
