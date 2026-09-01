"""Step 0 冒烟测试：校验器 / 结构化截断 / intent 落库（含旧库迁移）。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.orchestrator import (  # noqa: E402
    DecisionValidationError, _build_decision, _pack_tool_result, MAX_STRICT_SUBMITS,
)
from agents.models import TradeIntent, TradeDecision, RiskVerdict  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


print("\n[1] 严格模式：qty=0 的 open 不再被静默吞掉")
payload = {
    "decision": "open",
    "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "LONG", "qty": 0.0,
                 "stop_loss": 100.0, "take_profit": 200.0, "reasoning": "test"}],
    "reasoning": "想开多",
}
try:
    _build_decision(payload, strict=True)
    check("qty=0 open 应抛错", False, "没有抛错，仍在静默吞单")
except DecisionValidationError as e:
    check("qty=0 open 抛 DecisionValidationError", True)
    check("错误信息点名了 intent 下标与 symbol",
          "intents[0]" in e.issues[0] and "BTCUSDT" in e.issues[0], str(e.issues))
    check("hint 提示不要绕道 no_trade", "no_trade" in e.hint, e.hint)

print("\n[2] 严格模式：合法 payload 正常通过")
good = {
    "decision": "open",
    "intents": [{"symbol": "BTCUSDT", "action": "open", "side": "long", "qty": 0.01,
                 "leverage": 5, "stop_loss": 100.0, "take_profit": 200.0,
                 "invalidation": "跌破 100 即证伪"}],
    "position_reviews": [],
    "reasoning": "ok",
}
d, issues = _build_decision(good, strict=True)
check("合法 payload 无问题", issues == [] and d.decision == "open", str(issues))
check("side 被规范为大写", d.intents[0].side == "LONG", d.intents[0].side)

print("\n[3] 其他非法输入")
for name, p in [
    ("非法 decision", {"decision": "hold", "intents": [], "reasoning": "x"}),
    ("intents 非数组", {"decision": "no_trade", "intents": {}, "reasoning": "x"}),
    ("no_trade 但带 intents", {"decision": "no_trade",
                              "intents": [{"symbol": "A", "action": "open", "side": "LONG", "qty": 1}]}),
    ("open 但 intents 空", {"decision": "open", "intents": [], "reasoning": "x"}),
    ("缺 symbol", {"decision": "open", "intents": [{"action": "open", "side": "LONG", "qty": 1}]}),
    ("side 非法", {"decision": "open", "intents": [{"symbol": "A", "action": "open", "side": "BUY", "qty": 1}]}),
    ("action 非法", {"decision": "open", "intents": [{"symbol": "A", "action": "hold", "side": "LONG", "qty": 1}]}),
]:
    try:
        _build_decision(p, strict=True)
        check(name, False, "未抛错")
    except DecisionValidationError:
        check(name, True)

print("\n[4] adjust 的 qty 允许为 0（这是合法的）")
p = {"decision": "adjust",
     "intents": [{"symbol": "BTCUSDT", "action": "adjust", "side": "LONG", "qty": 0, "stop_loss": 100.0}],
     "position_reviews": [{"symbol": "BTCUSDT", "verdict": "adjust", "reason": "挪止损"}],
     "reasoning": "挪止损"}
d, issues = _build_decision(p, strict=True)
check("adjust qty=0 通过", issues == [] and d.decision == "adjust", str(issues))

print("\n[5] 宽松模式：重试耗尽后必须收敛，且原因写进 reasoning")
d, issues = _build_decision(payload, strict=False)
check("降级为 no_trade", d.decision == "no_trade", d.decision)
check("降级原因可见", "参数校验未通过" in d.reasoning and "qty" in d.reasoning, d.reasoning)
check("返回了问题清单", len(issues) >= 1, str(issues))

print("\n[6] 结构化截断：绝不输出半截 JSON")
big = {"symbol": "BTCUSDT", "last_price": 100.0,
       "recent_candles": [{"open_time": 1, "open": 1.0, "high": 2.0, "low": 0.5,
                           "close": 1.5, "volume": 10.0, "quote_volume": 15.0} for _ in range(200)]}
out = _pack_tool_result(big, max_chars=2000)
try:
    parsed = json.loads(out)
    check("截断后仍是合法 JSON", True)
    check("K线字段被整体丢弃而非切断", "recent_candles" not in parsed or isinstance(parsed.get("recent_candles"), str))
    check("保留了关键标量字段", parsed.get("symbol") == "BTCUSDT")
    check("给出了可操作的提示", "_omitted" in parsed or "_truncated" in parsed or "_note" in parsed)
except json.JSONDecodeError as e:
    check("截断后仍是合法 JSON", False, str(e))

small = {"symbol": "ETHUSDT", "last_price": 2.0}
out2 = _pack_tool_result(small, max_chars=8000)
check("未超限时不改动内容", json.loads(out2) == small)

huge = {"error": "", "symbol": "XUSDT", "last_price": 1.0,
        "recent_candles": [{"open_time": i, "open": 1.0, "high": 2.0, "low": 0.5,
                            "close": 1.5, "volume": 10.0, "quote_volume": 15.0} for i in range(500)],
        "blob": "x" * 50000}
out3 = _pack_tool_result(huge, max_chars=8000)
parsed3 = json.loads(out3)
check("超大返回体被裁剪到最小集", parsed3.get("_truncated") is True and "blob" not in parsed3)

print("\n[7] intent 落库：新库建表")
tmp = Path(tempfile.mkdtemp()) / "t.sqlite"
db = AuditDatabase(tmp)
cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(trade_intents)").fetchall()}
need = {"run_id", "action", "client_order_id", "invalidation", "blocked_reason"}
check("新库包含新增列", need <= cols, f"缺 {need - cols}")

it = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.01, leverage=5,
                 stop_loss=100.0, take_profit=200.0, client_order_id="mta-abc-0",
                 confidence=0.8, reasoning="看多", ai_reason_type="breakout")
db.log_trade_intent(intent_id="i1", decision_id="d1", run_id="r1", intent=it, status="approved")
db.log_trade_intent(intent_id="i2", decision_id="d1", run_id="r1", intent=it, status="rejected",
                    blocked_reason="已跳过: BTCUSDT leverage=5 超过常规档上限 20")
rows = db.fetch_all("SELECT * FROM trade_intents ORDER BY id")
check("两条 intent 都写入", len(rows) == 2, f"实际 {len(rows)}")
r0 = dict(rows[0])
check("symbol/action/side 正确", (r0["symbol"], r0["action"], r0["side"]) == ("BTCUSDT", "open", "LONG"))
check("client_order_id 落库", r0["client_order_id"] == "mta-abc-0", str(r0.get("client_order_id")))
check("invalidation 列存在且可空", r0.get("invalidation") is None)
check("被拒原因落库", dict(rows[1])["blocked_reason"].startswith("已跳过"), str(dict(rows[1])["blocked_reason"]))
check("raw_json 可解析", json.loads(r0["raw_json"])["ai_reason_type"] == "breakout")
db.close()

print("\n[8] 旧库迁移：在真实库的副本上加列，不能破坏已有数据")
src_db = ROOT / "data" / "agent_trader.sqlite"
if src_db.exists():
    copy_db = Path(tempfile.mkdtemp()) / "agent_trader.sqlite"
    shutil.copy2(src_db, copy_db)
    db2 = AuditDatabase(copy_db)
    cols2 = {r["name"] for r in db2.conn.execute("PRAGMA table_info(trade_intents)").fetchall()}
    check("旧库迁移补上新列", need <= cols2, f"缺 {need - cols2}")
    before = db2.fetch_one("SELECT COUNT(*) c FROM trade_decisions")["c"]
    check("已有 decision 数据未丢失", before > 0, f"count={before}")
    db2.log_trade_intent(intent_id="mig1", decision_id="d", run_id="r",
                         intent=it, status="approved")
    check("迁移后写入正常", db2.fetch_one("SELECT COUNT(*) c FROM trade_intents")["c"] == 1)
    db2.close()
else:
    print("  SKIP  未找到 data/agent_trader.sqlite")

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
