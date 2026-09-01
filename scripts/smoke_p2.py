"""P2 冒烟测试：校准表 / 策略卡生命周期 / replay / 复盘集成。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.calibration import compute_calibration, calibration_text  # noqa: E402
from agents.strategy_lifecycle import (  # noqa: E402
    evaluate_cards, apply_suggested_status, write_lifecycle_report,
)
from agents.replay import replay_run, replay_window, replay_summary  # noqa: E402
from agents.review import run_review_agent  # noqa: E402
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


def make_db():
    tmp = Path(tempfile.mkdtemp())
    return AuditDatabase(tmp / "t.sqlite"), tmp


_SEED_CTR = [0]


def seed_outcome(db, symbol="BTCUSDT", source="ai", strategy_id=None,
                 confidence=None, pnl=1.0, invalidation=None, closed_at="2026-08-31T10:00:00",
                 decision_id=None, intent_id=None):
    _SEED_CTR[0] += 1
    db.log_trade_outcome(
        outcome_id=f"oc-seed-{_SEED_CTR[0]}",
        intent_id=intent_id, decision_id=decision_id, symbol=symbol, side="LONG", source=source,
        strategy_id=strategy_id, confidence=confidence, entry_price=100.0, exit_price=101.0,
        qty=1.0, pnl=pnl, pnl_pct=1.0, invalidation=invalidation,
        exit_reason="ai_decision", closed_at=closed_at,
    )


print("\n[1] 校准表：分桶统计正确")
db, _ = make_db()
# 桶 >=0.85：confidence 0.9 × 10，6 胜 4 负 -> win 0.6, mid 0.93, err 0.33
for i in range(6):
    seed_outcome(db, confidence=0.9, pnl=1.0)
for i in range(4):
    seed_outcome(db, confidence=0.9, pnl=-1.0)
# 桶 0.40-0.55：confidence 0.5 × 4，2 胜 2 负 -> win 0.5, mid 0.475, err 0.025
for i in range(2):
    seed_outcome(db, confidence=0.5, pnl=1.0)
for i in range(2):
    seed_outcome(db, confidence=0.5, pnl=-1.0)
# 桶 <0.40：confidence 0.3 × 3
for i in range(2):
    seed_outcome(db, confidence=0.3, pnl=1.0)
seed_outcome(db, confidence=0.3, pnl=-1.0)
stats = compute_calibration(db)
check("总样本 17", stats["n"] == 17, str(stats["n"]))
b_hi = next(b for b in stats["buckets"] if b["bucket"] == ">=0.85")
check("高置信桶 n=10 win=0.6", b_hi["n"] == 10 and abs(b_hi["win_rate"] - 0.6) < 1e-9,
      str(b_hi))
check("高置信桶判定为未校准（过自信）", b_hi["calibrated"] is False, str(b_hi["calibrated"]))
b_mid = next(b for b in stats["buckets"] if b["bucket"] == "0.40-0.55")
check("中置信桶校准 OK", b_mid["calibrated"] is True, str(b_mid))
check("整体判定过自信", stats["overconfident"] is True, str(stats["overconfident"]))
check("miscalibration 为加权均值",
      abs(stats["miscalibration"] - (0.33 * 10 + 0.025 * 4 + 0.467 * 3) / 17) < 0.02,
      str(stats["miscalibration"]))
check("calibration_text 可渲染", "校准" in calibration_text(stats))
db.close()

print("\n[2] 校准表：样本不足不误报")
db, _ = make_db()
seed_outcome(db, confidence=0.9, pnl=1.0)
seed_outcome(db, confidence=0.9, pnl=-1.0)
s2 = compute_calibration(db)
check("样本不足时桶判定为 None", s2["buckets"][-1]["calibrated"] is None, str(s2["buckets"][-1]))
db.close()

print("\n[3] 策略卡生命周期")
db, _ = make_db()
CARDS = {"version": 2, "cards": [
    {"id": "A", "name": "健康卡"},
    {"id": "B", "name": "冷却卡"},
    {"id": "C", "name": "退役卡"},
    {"id": "D", "name": "样本不足卡"},
]}
for i in range(8):
    seed_outcome(db, source="strategy", strategy_id="A", pnl=1.5, closed_at=f"2026-08-{20+i:02d}T10:00:00")
for i in range(8):
    seed_outcome(db, source="strategy", strategy_id="B", pnl=-1.0, closed_at=f"2026-08-{20+i:02d}T10:00:00")
for i in range(8):
    seed_outcome(db, source="strategy", strategy_id="C", pnl=-2.5, closed_at=f"2026-08-{20+i:02d}T10:00:00")
for i in range(2):
    seed_outcome(db, source="strategy", strategy_id="D", pnl=0.5, closed_at=f"2026-08-{20+i:02d}T10:00:00")
res = evaluate_cards(db, CARDS)
by_id = {r["strategy_id"]: r for r in res}
check("A 健康 -> active", by_id["A"]["status"] == "active", str(by_id["A"]["status"]))
check("B 累计 -8 -> cooling", by_id["B"]["status"] == "cooling", str(by_id["B"]["status"]))
check("C 累计 -20 -> disabled", by_id["C"]["status"] == "disabled", str(by_id["C"]["status"]))
check("D 样本不足", by_id["D"]["status"] == "insufficient", str(by_id["D"]["status"]))
check("A 胜率 100%", by_id["A"]["win_rate"] == 1.0, str(by_id["A"]["win_rate"]))
applied = apply_suggested_status(CARDS, res)
check("应用后副本带 lifecycle 字段",
      applied["cards"][1].get("lifecycle", {}).get("status") == "cooling"
      and applied["cards"][2].get("lifecycle", {}).get("status") == "disabled",
      str([c.get("lifecycle") for c in applied["cards"]]))
check("原卡片文件 dict 未被修改", "lifecycle" not in CARDS["cards"][1])
tmp = Path(tempfile.mkdtemp())
write_lifecycle_report(res, tmp / "lifecycle.json")
check("状态报告落盘", (tmp / "lifecycle.json").exists() and "cards" in json.loads(
    (tmp / "lifecycle.json").read_text(encoding="utf-8")))
db.close()

print("\n[4] replay：单轮回放与窗口回放")
db, tmp = make_db()
db.log_trade_decision(decision_id="D-1", run_id="R-1", decision="open",
                      reasoning="看多 BTC", raw_json="{}", channel="merged")
db.log_trade_intent(intent_id="I-1", decision_id="D-1", run_id="R-1",
                    intent=__import__("agents.models", fromlist=["TradeIntent"]).TradeIntent(
                        symbol="BTCUSDT", action="open", side="LONG", qty=0.01,
                        invalidation="跌破 58000 即证伪", client_order_id="cid-1"),
                    status="approved")
db.log_risk_verdict(verdict_id="V-1", decision_id="D-1", verdict="approved",
                    reasons=json.dumps(["ok"]), raw_json="{}")
db.log_order(client_order_id="cid-1", exchange_order_id="cid-1", symbol="BTCUSDT",
             side="LONG", order_type="MARKET", qty=0.01, price=60000.0, status="FILLED",
             raw={"action": "open"})
seed_outcome(db, invalidation="跌破 58000 即证伪", pnl=1.5, decision_id="D-1", intent_id="I-1")
rp = replay_run(db, "D-1")
check("回放找到决策", rp["found"] and rp["decision"] == "open", str(rp.get("found")))
check("intent 带状态与证伪条件",
      rp["intents"] and rp["intents"][0]["status"] == "approved"
      and rp["intents"][0]["invalidation"] == "跌破 58000 即证伪", str(rp["intents"]))
check("风控裁决带原因", rp["risk_verdict"]["verdict"] == "approved", str(rp["risk_verdict"]))
check("outcome 带证伪条件", rp["outcomes"] and rp["outcomes"][0]["invalidation"] == "跌破 58000 即证伪",
      str(rp["outcomes"]))
check("窗口回放返回 1 条", len(replay_window(db, limit=10)) == 1)
summary = replay_summary(rp)
check("摘要含关键字段", "BTCUSDT" in summary and "D-1" in summary and "平仓" in summary)
db.close()

print("\n[5] 复盘集成：报告包含归因与校准段")
db, tmp = make_db()
seed_outcome(db, confidence=0.8, pnl=1.0, closed_at="2026-08-31T10:00:00")
seed_outcome(db, confidence=0.8, pnl=-1.0, closed_at="2026-08-31T11:00:00")
seed_outcome(db, confidence=0.8, pnl=-1.0, closed_at="2026-08-31T12:00:00")
seed_outcome(db, confidence=0.8, pnl=1.0, closed_at="2026-08-31T13:00:00")
seed_outcome(db, confidence=0.8, pnl=-1.0, closed_at="2026-08-31T14:00:00")
report = run_review_agent(db, tmp / "reviews", review_date="2026-08-31")
text = report.read_text(encoding="utf-8")
check("报告含平仓归因段", "## 平仓归因" in text)
check("报告含校准段", "## 置信度校准" in text)
check("归因统计正确（5 笔 2 胜 3 负）", "胜率 40%" in text, "胜率 40%" in text)
check("校准样本数正确", "校准（样本 5" in text)
db.close()

print("\n[6] _record_outcome 带回 confidence/strategy_id（PaperBroker 链路）")
from agents.execution import PaperBroker  # noqa: E402
from agents.models import TradeDecision, TradeIntent, RiskVerdict  # noqa: E402
db, tmp = make_db()
broker = PaperBroker(tmp / "b.json", initial_equity=200.0)
rc = {"max_leverage": 20, "max_margin_usage": 0.80, "max_position_notional": 100000,
      "max_open_positions": 8, "max_risk_per_trade": 0.02,
      "channel_status": {"ai": {"allow_new": True, "margin_used": 0, "margin_cap": 1000, "equity": 200}},
      "supported_symbols": ["BTCUSDT", "ETHUSDT"]}
oi = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.001, leverage=5,
                 stop_loss=59000.0, take_profit=62000.0, confidence=0.85,
                 invalidation="跌破 59000 即证伪", client_order_id="cid-open")
db.log_trade_intent(intent_id="I-O", decision_id="D-O", run_id="R", intent=oi, status="approved")
broker.execute(TradeDecision(decision="open", intents=[oi]),
               RiskVerdict(verdict="approved", adjusted_intents=[oi]), {"BTCUSDT": 60000.0}, rc, db)
ci = TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.001, client_order_id="cid-close")
broker.execute(TradeDecision(decision="close", intents=[ci]),
               RiskVerdict(verdict="approved", adjusted_intents=[ci]), {"BTCUSDT": 61000.0}, rc, db)
o = dict(db.fetch_one("SELECT * FROM trade_outcomes"))
check("confidence 0.85 带回 outcome", abs(float(o["confidence"]) - 0.85) < 1e-9, str(o["confidence"]))
check("invalidation 带回", o["invalidation"] == "跌破 59000 即证伪", str(o["invalidation"]))
check("intent_id 归因", o["intent_id"] == "I-O", str(o["intent_id"]))
db.close()

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
