"""H阶段组合因子与连亏分类账离线回归。"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agents.models import DecisionEvidence, TradeIntent  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.portfolio_policy import (  # noqa: E402
    evaluate_portfolio_intent,
    factor_for,
    rolling_beta_correlation,
)

passed = failed = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def candles(multiplier: float) -> dict:
    returns = [0.01, -0.004, 0.008, 0.003, -0.006, 0.012, -0.002, 0.007,
               -0.005, 0.009, 0.004, -0.003, 0.011, -0.004, 0.006]
    values = [100.0]
    for value in returns:
        values.append(values[-1] * (1 + value * multiplier))
    return {"candles": {"c": values}}


def make_intent(symbol: str = "AVAXUSDT", side: str = "LONG",
                regime: str = "trend_up") -> TradeIntent:
    is_long = side == "LONG"
    evidence = DecisionEvidence(
        market_regime=regime,
        higher_timeframe_trend="up" if is_long else "down",
        supporting_evidence=["闭合K线趋势一致"], counter_evidence=[],
        trigger_price=100, invalidation_price=95 if is_long else 105,
        invalidation_operator="close_below" if is_long else "close_above",
        confirmation_interval="15m", risk_reward_ratio=2, chase_distance_pct=0.2,
        data_observed_at_ms=1_700_000_000_000, confidence_basis="趋势结构一致",
    )
    return TradeIntent(
        symbol=symbol, action="open", side=side, qty=0.1, leverage=5,
        stop_loss=95 if is_long else 105, take_profit=110 if is_long else 90,
        strategy_id="ai:trend_follow:v1", invalidation="结构失效", evidence=evidence,
    )


def result_by_id(results: list[dict], check_id: str) -> dict:
    return next(item for item in results if item["check_id"] == check_id)


print("\n[1] 有数据时计算滚动相关与beta；缺数据明确降级")
details = {"BTCUSDT": candles(1.0), "AVAXUSDT": candles(1.8)}
beta, correlation = rolling_beta_correlation(details["AVAXUSDT"], details["BTCUSDT"])
check("高beta相关序列可计算", beta is not None and 1.6 < beta < 2.0,
      f"beta={beta} corr={correlation}")
check("相关系数接近1", correlation is not None and correlation > 0.99, str(correlation))
dynamic = factor_for("AVAXUSDT", details)
check("动态归入BTC beta山寨因子", dynamic["group"] == "btc_beta_alts"
      and dynamic["method"] == "rolling_returns", str(dynamic))
fallback = factor_for("AVAXUSDT", {})
check("缺数据不是低风险，而是低置信静态回退", fallback["confidence"] == "low"
      and fallback["method"] == "static_fallback", str(fallback))
mismatched = {
    "BTCUSDT": {**candles(1.0), "interval": "1h"},
    "AVAXUSDT": {**candles(1.8), "interval": "15m"},
}
check("不同周期不能伪算相关性",
      rolling_beta_correlation(mismatched["AVAXUSDT"], mismatched["BTCUSDT"]) == (None, None))

print("\n[2] 同方向同因子敞口聚集可见，反向仓不误计")
db_path = Path(tempfile.mkdtemp()) / "audit.sqlite"
db = AuditDatabase(db_path)
portfolio = {"positions": [
    {"symbol": "LINKUSDT", "side": "LONG", "qty": 2, "entry_price": 20, "stop_loss": 19},
    {"symbol": "ARBUSDT", "side": "LONG", "qty": 100, "entry_price": 0.5, "stop_loss": 0.47},
    {"symbol": "SUIUSDT", "side": "SHORT", "qty": 10, "entry_price": 3, "stop_loss": 3.1},
]}
factor_result = result_by_id(
    evaluate_portfolio_intent(make_intent(), portfolio, {}, db), "factor_concentration",
)
check("第三个同向山寨候选被shadow识别", factor_result["status"] == "BLOCK"
      and factor_result["evidence"]["resulting_count"] == 3, str(factor_result))
check("反向SUI未计入同向集合",
      "SUIUSDT" not in factor_result["evidence"]["existing_symbols"], str(factor_result))
check("名义敞口和已知止损损失均有记录",
      factor_result["evidence"]["directional_notional"] > 0
      and factor_result["evidence"]["known_worst_case_stop_loss"] > 0, str(factor_result))

print("\n[3] 连亏从审计库重建并形成冷却")
for index, closed_at in enumerate(("2026-09-11T11:30:00+00:00", "2026-09-11T10:30:00+00:00")):
    intent = make_intent(symbol="LINKUSDT")
    intent_id = f"loss-intent-{index}"
    db.log_trade_intent(
        intent_id=intent_id, decision_id=None, run_id="loss-run", intent=intent, status="approved",
    )
    db.log_trade_outcome(
        outcome_id=f"loss-{index}", intent_id=intent_id, decision_id=None,
        symbol="LINKUSDT", side="LONG", strategy_id=intent.strategy_id,
        pnl=-1.0, status="CLOSED", closed_at=closed_at,
    )
now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
loss_result = result_by_id(
    evaluate_portfolio_intent(make_intent(), {"positions": []}, {}, db, now=now), "loss_streak",
)
check("两次同类亏损进入shadow冷却", loss_result["status"] == "BLOCK"
      and loss_result["evidence"]["consecutive_losses"] == 2, str(loss_result))
check("冷却截止时间可审计", bool(loss_result["evidence"]["cooldown_until"]), str(loss_result))

print("\n[4] 重启后状态仍在，独立数据库不串账")
db.close()
reopened = AuditDatabase(db_path)
after_restart = result_by_id(
    evaluate_portfolio_intent(make_intent(), {"positions": []}, {}, reopened, now=now), "loss_streak",
)
check("重启后连续亏损仍为2", after_restart["evidence"]["consecutive_losses"] == 2,
      str(after_restart))
empty_db = AuditDatabase(Path(tempfile.mkdtemp()) / "other.sqlite")
separate = result_by_id(
    evaluate_portfolio_intent(make_intent(), {"positions": []}, {}, empty_db, now=now), "loss_streak",
)
check("另一环境数据库不继承冷却", separate["evidence"]["consecutive_losses"] == 0,
      str(separate))

print("\n[5] 双向近期亏损标记震荡/模型失配，退出动作不受影响")
short_intent = make_intent(symbol="ARBUSDT", side="SHORT", regime="trend_down")
reopened.log_trade_intent(
    intent_id="loss-short-intent", decision_id=None, run_id="loss-run",
    intent=short_intent, status="approved",
)
reopened.log_trade_outcome(
    outcome_id="loss-short", intent_id="loss-short-intent", decision_id=None,
    symbol="ARBUSDT", side="SHORT", strategy_id=short_intent.strategy_id,
    pnl=-1.0, status="CLOSED", closed_at="2026-09-11T11:45:00+00:00",
)
choppy = result_by_id(
    evaluate_portfolio_intent(make_intent(), {"positions": []}, {}, reopened, now=now), "loss_streak",
)
check("同策略同因子双向亏损被标记", choppy["evidence"]["choppy_or_model_mismatch"],
      str(choppy))
close_intent = TradeIntent(symbol="AVAXUSDT", action="close", side="LONG", qty=0.1)
check("close不进入组合冷却检查",
      evaluate_portfolio_intent(close_intent, portfolio, {}, reopened, now=now) == [])

reopened.close()
empty_db.close()
print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
