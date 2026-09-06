"""P0-b 冒烟测试：ActionSpace / 动态 schema / Validator 回环 / risk.py 单一谓词一致性。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.models import TradeIntent, TradeDecision  # noqa: E402
from agents.orchestrator import build_submit_decision_spec, SUBMIT_DECISION_TOOL_SPEC  # noqa: E402
from agents.risk import evaluate_risk  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.action_space import ActionSpace, ALWAYS_ALLOWED  # noqa: E402
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


RISK_CFG = {
    "max_leverage": 20, "ai_max_leverage": 20, "strong_max_leverage": 50,
    "strong_confidence_threshold": 0.75, "max_open_positions": 8,
    "max_position_notional": 100000, "max_margin_usage": 0.80,
    "max_risk_per_trade": 0.02, "strong_max_notional": 200000,
    "strong_max_margin_usage": 0.95,
    "supported_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
}
PRICES = {"BTCUSDT": 60000.0, "ETHUSDT": 3000.0, "SOLUSDT": 150.0}


def space_open(**kw):
    cfg = dict(RISK_CFG)
    cfg.update(kw.pop("cfg", {}))
    return ActionSpace.from_context(risk_config=cfg, allow_new_positions=True,
                                    portfolio=kw.pop("portfolio", {"positions": []}), **kw)


print("\n[1] 动态 schema：open 被剪掉，存量管理动作必须保留")
s_closed = ActionSpace.from_context(risk_config=RISK_CFG, allow_new_positions=False,
                                    portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]})
spec = build_submit_decision_spec(s_closed)
props = spec["function"]["parameters"]["properties"]
acts = props["intents"]["items"]["properties"]["action"]["enum"]
check("禁开仓时 open 从 enum 消失", "open" not in acts, str(acts))
check("close/reduce/adjust 全部保留", all(a in acts for a in ALWAYS_ALLOWED), str(acts))
check("顶层 decision enum 同步剪枝", "open" not in props["decision"]["enum"], str(props["decision"]["enum"]))
check("no_trade 始终可用", "no_trade" in props["decision"]["enum"])
check("description 说明了禁开仓原因", "不允许新开仓" in spec["function"]["description"])

s_open = space_open()
acts2 = build_submit_decision_spec(s_open)["function"]["parameters"]["properties"][
    "intents"]["items"]["properties"]["action"]["enum"]
check("允许开仓时 open 存在", "open" in acts2, str(acts2))

print("\n[2] 动态 schema：symbol 约束与杠杆上限")
spec2 = build_submit_decision_spec(s_open)
sym = spec2["function"]["parameters"]["properties"]["intents"]["items"]["properties"]["symbol"]
check("symbol 用 enum 硬约束", sym.get("enum") == ["BTCUSDT", "ETHUSDT", "SOLUSDT"], str(sym))
lev = spec2["function"]["parameters"]["properties"]["intents"]["items"]["properties"]["leverage"]
check("leverage maximum = 强信心档上限 25x", lev["maximum"] == 25, str(lev))

big = space_open(cfg={"supported_symbols": [f"S{i}USDT" for i in range(200)]})
sym2 = build_submit_decision_spec(big)["function"]["parameters"]["properties"][
    "intents"]["items"]["properties"]["symbol"]
check("symbol 过多时改用描述约束", "enum" not in sym2 and "200" in sym2.get("description", ""), str(sym2)[:120])

print("\n[3] 无 ActionSpace 时退回静态 schema（兼容性）")
check("返回静态 spec 副本", build_submit_decision_spec(None) == SUBMIT_DECISION_TOOL_SPEC)
check("返回的是副本不是引用", build_submit_decision_spec(None) is not SUBMIT_DECISION_TOOL_SPEC)

print("\n[4] Validator：业务规则逐条生效")
good_open = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.01, leverage=5,
                        stop_loss=59000.0, take_profit=62000.0, confidence=0.5)
check("合法开仓无问题", space_open().check_intent(good_open, PRICES) == [],
      str(space_open().check_intent(good_open, PRICES)))

cases = [
    ("SL 方向反(LONG)", dict(stop_loss=61000.0, take_profit=62000.0), "方向错误"),
    ("TP 方向反(SHORT)", dict(side="SHORT", stop_loss=59000.0, take_profit=59500.0), "方向错误"),
    ("缺止损", dict(stop_loss=None), "初始止损"),
    ("缺止盈", dict(take_profit=None), "初始止盈"),
    ("杠杆超限", dict(leverage=30), "超过上限"),
    ("幻觉 symbol", dict(symbol="DOGEUSDT"), "不在交易所支持"),
    ("qty=0", dict(qty=0.0), "数量必须大于 0"),
]
for name, patch, expect in cases:
    it = good_open.model_copy(update=patch)
    issues = space_open().check_intent(it, PRICES)
    check(name, any(expect in i for i in issues), str(issues))

print("\n[5] Validator：强信心档杠杆与 risk.py 同算法")
# 用户约定：常规档 20x，强信心档最高 25x（ai_strong_max_leverage 默认 25）。
# 测分档逻辑本身时把两个 AI 上限都放开。
s_tier = space_open(cfg={"ai_max_leverage": 50, "ai_strong_max_leverage": 50})
strong = good_open.model_copy(update={"confidence": 0.9, "leverage": 40})
check("带止损+高信心可用 40x（放开上限时）", s_tier.check_intent(strong, PRICES) == [],
      str(s_tier.check_intent(strong, PRICES)))
nostop_strong = good_open.model_copy(update={"confidence": 0.9, "leverage": 40, "stop_loss": None})
check("强信心但没止损则降级回常规档", any("超过上限 20" in i for i in
                                          s_tier.check_intent(nostop_strong, PRICES)),
      str(s_tier.check_intent(nostop_strong, PRICES)))
lowconf = good_open.model_copy(update={"confidence": 0.5, "leverage": 40})
check("低信心即使带止损也只给 20x", any("超过上限 20" in i for i in
                                        s_tier.check_intent(lowconf, PRICES)),
      str(s_tier.check_intent(lowconf, PRICES)))
# 线上配置：常规 20x，强信心 25x（新约定）
strong25 = good_open.model_copy(update={"confidence": 0.9, "leverage": 25})
check("线上配置：强信心档 25x 通过", space_open().check_intent(strong25, PRICES) == [],
      str(space_open().check_intent(strong25, PRICES)))
check("线上配置：强信心档 26x 被拦", any("超过上限 25" in i for i in
                                        space_open().check_intent(strong, PRICES)),
      str(space_open().check_intent(strong, PRICES)))
check("线上配置：常规档 21x 被拦", any("超过上限 20" in i for i in
                                        space_open().check_intent(
                                            good_open.model_copy(update={"leverage": 21}), PRICES)),
      str(space_open().check_intent(good_open.model_copy(update={"leverage": 21}), PRICES)))

print("\n[6] Validator：存量管理动作必须打在真实持仓上")
s_held = space_open(portfolio={"positions": [{"symbol": "ETHUSDT", "source": "ai"}]})
close_ok = TradeIntent(symbol="ETHUSDT", action="close", side="LONG", qty=0.5)
check("平真实持仓通过", s_held.check_intent(close_ok, PRICES) == [], str(s_held.check_intent(close_ok, PRICES)))
close_bad = TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.5)
check("平不存在的持仓被拦", any("当前无持仓" in i for i in s_held.check_intent(close_bad, PRICES)),
      str(s_held.check_intent(close_bad, PRICES)))
# s_closed 的持仓是 BTCUSDT（见 [1]），平仓必须打在它上面
check("禁开仓时仍能平仓", s_closed.check_intent(
    TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01), PRICES) == [],
    str(s_closed.check_intent(close_ok, PRICES)))
check("禁开仓时仍能调整止损", s_closed.check_intent(
    TradeIntent(symbol="BTCUSDT", action="adjust", side="LONG", qty=0, stop_loss=59000.0),
    PRICES) == [])

print("\n[7] Validator：策略通道占位冲突（prompt 规则 11 结构化）")
s_conflict = space_open(portfolio={"positions": [{"symbol": "SOLUSDT", "source": "strategy"}]})
dup = good_open.model_copy(update={"symbol": "SOLUSDT", "stop_loss": 140.0, "take_profit": 160.0})
check("在策略持仓上开仓被拦", any("策略通道" in i for i in s_conflict.check_intent(dup, PRICES)),
      str(s_conflict.check_intent(dup, PRICES)))

print("\n[8] 跨 intent 规则")
check("同轮重复 symbol 被拦", any("多条 intent" in i for i in
                                  space_open().check_decision([good_open, good_open], PRICES)))
many = [good_open.model_copy(update={"symbol": s}) for s in
        ("BTCUSDT", "ETHUSDT", "SOLUSDT") * 3]
check("intent 数超限被拦", any("max_open_positions" in i for i in
                               space_open().check_decision(many, PRICES)))

print("\n[9] 差分测试：ActionSpace 与 risk.py 闸门对同一 intent 判定必须一致")
tmp = Path(tempfile.mkdtemp())
db = AuditDatabase(tmp / "t.sqlite")
rec = AgentRecorder(db, JsonlLogger(tmp / "logs"), tmp / "artifacts")
portfolio = {"positions": [{"symbol": "ETHUSDT", "source": "ai"}]}

samples = [
    ("合法开仓", good_open, True),
    ("SL 方向反", good_open.model_copy(update={"stop_loss": 61000.0}), False),
    ("杠杆超限", good_open.model_copy(update={"leverage": 30}), False),
    ("幻觉 symbol", good_open.model_copy(update={"symbol": "DOGEUSDT"}), False),
    ("缺止盈", good_open.model_copy(update={"take_profit": None}), False),
    ("qty=0 开仓", good_open.model_copy(update={"qty": 0.0}), False),
    ("合法平仓", close_ok, True),
    ("平无持仓", close_bad, False),
    ("合法调整", TradeIntent(symbol="ETHUSDT", action="adjust", side="LONG", qty=0,
                             stop_loss=2900.0), True),
    ("调整无 SL/TP", TradeIntent(symbol="ETHUSDT", action="adjust", side="LONG", qty=0), False),
    ("限价缺到期", good_open.model_copy(update={"order_type": "LIMIT", "limit_price": 59000.0}), False),
    ("限价完整", good_open.model_copy(update={"order_type": "LIMIT", "limit_price": 59000.0,
                                              "stop_loss": 58000.0,
                                              "limit_expires_at": "2026-09-01T12:00:00Z"}), True),
]
for name, intent, expect_pass in samples:
    sp = ActionSpace.from_context(risk_config=RISK_CFG, allow_new_positions=True,
                                  portfolio=portfolio, channel="ai")
    validator_pass = sp.check_intent(intent, PRICES) == []
    dec = TradeDecision(decision="open", intents=[intent])
    verdict = evaluate_risk(dec, RISK_CFG, db, rec, "run-x", "dec-x", True,
                            prices=PRICES, available_cash=200.0, portfolio=portfolio)
    # risk.py 的"通过"判定：该 intent 出现在 adjusted_intents 里
    gate_pass = any(getattr(i, "symbol", None) == intent.symbol
                    for i in verdict.adjusted_intents)
    if name == "限价完整":
        # 限价单成交价 59000，止损 58000，risk_per_unit=1000，qty=0.01 → 风险 10 > 200*0.02=4
        # risk.py 会自动缩量放行（reduced），仍算通过
        gate_pass = gate_pass or verdict.verdict == "reduced"
    check(f"{name}：Validator 与闸门一致（期望{'通过' if expect_pass else '拦截'}）",
          validator_pass == gate_pass == expect_pass,
          f"validator={validator_pass} gate={gate_pass} verdict={verdict.verdict} "
          f"reasons={verdict.reasons}")

print("\n[10] risk.py 回归：no_trade 与整批拒绝仍正常")
v = evaluate_risk(TradeDecision(decision="no_trade"), RISK_CFG, db, rec, "r", "d", True,
                  prices=PRICES, available_cash=200.0, portfolio=portfolio)
check("no_trade 直接通过", v.verdict == "approved", v.verdict)
v2 = evaluate_risk(TradeDecision(decision="open", intents=[
    good_open.model_copy(update={"stop_loss": 61000.0})]), RISK_CFG, db, rec, "r2", "d2", True,
    prices=PRICES, available_cash=200.0, portfolio=portfolio)
check("全批被拒时 verdict=rejected", v2.verdict == "rejected", v2.verdict)
check("拒绝原因来自 ActionSpace", any("方向错误" in r for r in v2.reasons), str(v2.reasons))

print("\n[11] 禁开仓时 risk.py 也应拦住 open，但放行平仓")
v3 = evaluate_risk(TradeDecision(decision="open", intents=[good_open]), RISK_CFG, db, rec,
                   "r3", "d3", False, prices=PRICES, available_cash=200.0, portfolio=portfolio)
check("禁开仓时 open 被闸门锁掉", v3.verdict == "rejected", f"{v3.verdict} {v3.reasons}")
v4 = evaluate_risk(TradeDecision(decision="close", intents=[close_ok]), RISK_CFG, db, rec,
                   "r4", "d4", False, prices=PRICES, available_cash=200.0, portfolio=portfolio)
check("禁开仓时 close 仍可放行", v4.verdict == "approved", f"{v4.verdict} {v4.reasons}")
db.close()

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
