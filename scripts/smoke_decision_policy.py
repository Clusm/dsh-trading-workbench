"""决策质量 shadow 管线离线回归；不联网、不读取账户、不修改风险档。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agents.models import DecisionEvidence, TradeIntent  # noqa: E402
from agents.strategy_library import run_strategy_agent  # noqa: E402
from core.decision_policy import (  # noqa: E402
    enforced_issues,
    evaluate_open_intent,
    load_decision_policy,
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


def make_intent(**evidence_updates) -> TradeIntent:
    evidence = {
        "market_regime": "trend_up", "higher_timeframe_trend": "up",
        "supporting_evidence": ["1h与4h闭合K线趋势向上"], "counter_evidence": [],
        "trigger_price": 100.0, "invalidation_price": 95.0,
        "invalidation_operator": "close_below", "confirmation_interval": "15m",
        "risk_reward_ratio": 2.0, "chase_distance_pct": 0.2,
        "data_observed_at_ms": 1_700_000_000_000, "confidence_basis": "趋势和结构一致",
    }
    evidence.update(evidence_updates)
    return TradeIntent(
        symbol="BTCUSDT", action="open", side="LONG", qty=0.01, leverage=5,
        stop_loss=95.0, take_profit=110.0, strategy_id="ai:trend_follow:v1",
        invalidation="15m收盘跌破95即证伪", evidence=DecisionEvidence(**evidence),
    )


def by_id(results: list[dict], check_id: str) -> dict:
    return next(item for item in results if item["check_id"] == check_id)


config = load_decision_policy()
print("\n[1] 正常顺势样本全部通过")
normal = evaluate_open_intent(make_intent(), config)
check("五项检查均产生结果", len(normal) == 5, str(normal))
check("正常样本无 BLOCK", all(item["status"] == "PASS" for item in normal), str(normal))
check("默认全部 shadow", all(item["mode"] == "shadow" for item in normal), str(normal))
check("shadow BLOCK 不形成执行问题", enforced_issues(normal) == [])

print("\n[2] 方向、RR、追价、反证和无效点分别可识别")
trend = evaluate_open_intent(
    make_intent(market_regime="trend_down", higher_timeframe_trend="down"), config,
)
check("逆高周期做多被识别", by_id(trend, "trend_alignment")["status"] == "BLOCK")

low_rr = make_intent(risk_reward_ratio=0.5)
low_rr = low_rr.model_copy(update={"take_profit": 102.5})
rr = evaluate_open_intent(low_rr, config)
check("低实际RR被识别", by_id(rr, "risk_reward")["status"] == "BLOCK")

reported_wrong = evaluate_open_intent(make_intent(risk_reward_ratio=9.0), config)
check("模型报告RR与价格不一致被识别",
      by_id(reported_wrong, "risk_reward")["status"] == "BLOCK")

chase = evaluate_open_intent(make_intent(chase_distance_pct=1.2), config)
check("过度追价被识别", by_id(chase, "entry_chase")["status"] == "BLOCK")

conflict = evaluate_open_intent(
    make_intent(counter_evidence=["4h趋势向下"]), config,
)
check("反证不少于支持证据被识别",
      by_id(conflict, "evidence_conflict")["status"] == "BLOCK")

bad_invalidation = evaluate_open_intent(
    make_intent(invalidation_operator="close_above", invalidation_price=105.0), config,
)
check("LONG无效点方向错误被识别",
      by_id(bad_invalidation, "invalidation_consistency")["status"] == "BLOCK")

print("\n[3] 只有显式 enforce 的 BLOCK 才能影响执行")
enforced = copy.deepcopy(config)
enforced["checks"]["trend_alignment"]["mode"] = "enforce"
enforced_result = evaluate_open_intent(
    make_intent(market_regime="trend_down", higher_timeframe_trend="down"), enforced,
)
issues = enforced_issues(enforced_result)
check("enforce 生成稳定问题码", len(issues) == 1 and "trend_alignment" in issues[0], str(issues))
check("其他检查仍为 shadow", all(
    item["mode"] == "shadow" for item in enforced_result if item["check_id"] != "trend_alignment"
))

print("\n[4] 非开仓不进入决策质量阻断")
close_intent = TradeIntent(symbol="BTCUSDT", action="close", side="LONG", qty=0.01)
check("close 返回空检查", evaluate_open_intent(close_intent, config) == [])

print("\n[5] 配置拼写错误不能静默退化")
tmp = Path(tempfile.mkdtemp()) / "decision_policy.yaml"
tmp.write_text("checks:\n  trend_alignment:\n    mode: shdaow\n", encoding="utf-8")
try:
    load_decision_policy(tmp)
    check("非法 mode 被拒绝", False, "未抛异常")
except ValueError:
    check("非法 mode 被拒绝", True)

print("\n[6] 现有策略卡全部声明吸收族、必需证据和禁用条件")
cards = json.loads((PROJECT_ROOT / "knowledge" / "strategy_cards.json").read_text(encoding="utf-8"))["cards"]
check("仍保持9张已验证策略卡", len(cards) == 9, str(len(cards)))
check("九张卡全部具有决策适配元数据", all(
    (card.get("decision_policy") or {}).get("absorption_family")
    and (card.get("decision_policy") or {}).get("required_evidence")
    and (card.get("decision_policy") or {}).get("disabled_when")
    for card in cards
))
families = {card["decision_policy"]["absorption_family"] for card in cards}
check("已吸收动量、突破回踩、均值/供需与流动性反转族", {
    "regime_momentum", "breakout_retest", "range_mean_reversion_or_retest",
    "liquidity_sweep_reversal",
}.issubset(families), str(sorted(families)))
triggered = run_strategy_agent(
    PROJECT_ROOT / "knowledge",
    tickets=[{
        "strategy_id": "F_OLD_KELTNER_2H", "ticket_id": "t-1", "side": "LONG",
        "timeframe": "2h", "signal_price": 100, "entry_zone_low": 99,
        "entry_zone_high": 101, "expires_at": "2026-09-12T00:00:00Z",
    }],
)
check("实际触发的策略引用展示适配族和必需证据",
      bool(triggered.refs) and "适配族=regime_momentum_breakout_retest" in triggered.refs[0].detail
      and "必需证据=" in triggered.refs[0].detail, triggered.model_dump_json())

print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
