"""I 阶段 Position Guardian 离线回归；零网络、零真实账户写入。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agents.models import DecisionEvidence, TradeIntent  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.position_guardian import evaluate_positions as _evaluate_positions  # noqa: E402
from core.decision_policy import load_decision_policy  # noqa: E402


def evaluate_positions(**kwargs):
    """仅在隔离测试中显式开启退出，用例不能依赖生产配置提前晋级。"""
    config = load_decision_policy()
    config["position_guardian"]["mode"] = "enforce"
    return _evaluate_positions(config=config, **kwargs)

passed = failed = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


def open_intent(symbol: str = "BTCUSDT", side: str = "LONG") -> TradeIntent:
    is_long = side == "LONG"
    return TradeIntent(
        symbol=symbol, action="open", side=side, qty=0.01, leverage=5,
        stop_loss=95 if is_long else 105, take_profit=110 if is_long else 90,
        strategy_id="ai:trend:v1", invalidation="15m闭合K线确认结构失效",
        evidence=DecisionEvidence(
            market_regime="trend_up" if is_long else "trend_down",
            higher_timeframe_trend="up" if is_long else "down",
            supporting_evidence=["闭合K线趋势一致"], counter_evidence=[],
            trigger_price=100, invalidation_price=95 if is_long else 105,
            invalidation_operator="close_below" if is_long else "close_above",
            confirmation_interval="15m", risk_reward_ratio=2,
            chase_distance_pct=0.1, data_observed_at_ms=1_700_000_000_000,
            confidence_basis="趋势、结构和成交量一致",
        ),
    )


def snapshot(price: float, *, ok: bool = True, closed_at: int = 1_700_000_900_000):
    return lambda _symbol, _interval: {
        "ok": ok, "closed_price": price, "closed_at_ms": closed_at,
        "observed_at_ms": closed_at + 1000, "has_live_bar": True,
    }


db = AuditDatabase(Path(tempfile.mkdtemp()) / "guardian.sqlite")
intent = open_intent()
db.log_trade_intent(intent_id="open-1", decision_id="d-1", run_id="r-1",
                    intent=intent, status="approved")
position = {"symbol": "BTCUSDT", "side": "LONG", "qty": 0.01, "source": "ai"}

print("\n[1] 正常闭合K线保持 ACTIVE；当前形成K线不参与")
active = evaluate_positions(positions=[position], db=db, execution_mode="demo",
                            snapshot_provider=snapshot(96))
check("未失效不产生退出", active.decision == "no_trade" and not active.intents)
check("状态为 ACTIVE", active.policy_checks[0]["state"] == "ACTIVE")
check("live_bar 标记不会被当成收盘", active.policy_checks[0]["evidence"]["has_live_bar"] is True)

print("\n[2] Demo闭合K线失效产生唯一、可重复的全量退出")
invalid = evaluate_positions(positions=[position], db=db, execution_mode="demo",
                             snapshot_provider=snapshot(94))
repeat = evaluate_positions(positions=[position], db=db, execution_mode="demo",
                            snapshot_provider=snapshot(94))
check("失效产生一笔close", invalid.decision == "close" and len(invalid.intents) == 1
      and invalid.intents[0].action == "close")
check("退出数量等于持仓数量", invalid.intents[0].qty == position["qty"])
check("同一开仓和闭合bar得到稳定订单ID",
      invalid.intents[0].client_order_id == repeat.intents[0].client_order_id)
check("订单ID适合交易所长度", len(invalid.intents[0].client_order_id or "") <= 36)

print("\n[3] 实盘默认影子，不改变当前风险档或真实退出行为")
live = evaluate_positions(positions=[position], db=db, execution_mode="live",
                          snapshot_provider=snapshot(94))
check("live失效只记录不执行", live.decision == "no_trade" and not live.intents)
check("live有效模式为shadow", live.policy_checks[0]["mode"] == "shadow"
      and live.policy_checks[0]["status"] == "BLOCK")

print("\n[4] 数据或论点不可证明进入 REVIEW_DUE，绝不猜测平仓")
bad_data = evaluate_positions(positions=[position], db=db, execution_mode="demo",
                              snapshot_provider=snapshot(1, ok=False))
check("坏数据不产生退出", not bad_data.intents)
check("坏数据 NOT_EVALUATED", bad_data.policy_checks[0]["state"] == "REVIEW_DUE"
      and bad_data.policy_checks[0]["status"] == "NOT_EVALUATED")
missing = evaluate_positions(
    positions=[{"symbol": "ETHUSDT", "side": "SHORT", "qty": 0.2, "source": "ai"}],
    db=db, execution_mode="demo", snapshot_provider=snapshot(999),
)
check("缺开仓论点不产生退出", not missing.intents
      and missing.policy_checks[0]["state"] == "REVIEW_DUE")

print("\n[5] SHORT 使用 close_above；策略通道仓位不被 Guardian 越权")
short = open_intent("ETHUSDT", "SHORT")
db.log_trade_intent(intent_id="open-2", decision_id="d-2", run_id="r-2",
                    intent=short, status="approved")
short_result = evaluate_positions(
    positions=[{"symbol": "ETHUSDT", "side": "SHORT", "qty": 0.2, "source": "ai"}],
    db=db, execution_mode="paper", snapshot_provider=snapshot(106),
)
check("SHORT收盘高于无效点触发退出", len(short_result.intents) == 1)
strategy = evaluate_positions(
    positions=[{"symbol": "BTCUSDT", "side": "LONG", "qty": 1, "source": "strategy"}],
    db=db, execution_mode="demo", snapshot_provider=snapshot(1),
)
check("策略通道仓位保持原执行链", not strategy.intents and not strategy.policy_checks)

db.close()
print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
