"""J04-J06 走步、bootstrap 与晋级报告离线回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.strategy_evaluation import (  # noqa: E402
    bootstrap_net_pnl,
    promotion_report,
    walk_forward,
    walk_forward_comparison,
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


def rows(pnls: list[float], variant: str) -> list[dict]:
    return [{"variant": variant, "net_pnl": pnl, "opened_at_ms": i * 1000,
             "closed_at_ms": i * 1000 + 500, "incident": f"case-{i % 3}"}
            for i, pnl in enumerate(pnls, 1)]


baseline = rows([1, -1, 1, -1, 1, -1, 1, -1, 1, -1], "baseline")
# 样本内看似优秀，但最后20%样本外连续亏损；不得因总体收益漂亮而晋级。
candidate = rows([2, 2, 2, 2, 2, 1, 1, 1, -3, -3], "candidate")

print("\n[1] 时间走步严格保留样本外窗口")
walk = walk_forward(candidate)
check("切分为校准/验证/样本外", set(walk) == {"calibration", "validation", "out_of_sample"})
check("样本外是时间尾部", walk["out_of_sample"]["start_ms"] > walk["validation"]["end_ms"])
check("样本外恶化可见", walk["out_of_sample"]["metrics"]["net_pnl"] < 0)

print("\n[2] bootstrap 固定种子可重复且声明非收益保证")
boot1 = bootstrap_net_pnl(candidate, samples=200, seed=7)
boot2 = bootstrap_net_pnl(candidate, samples=200, seed=7)
check("相同种子结果一致", boot1 == boot2)
check("分位数单调", boot1["p05"] <= boot1["median"] <= boot1["p95"])
check("报告含免责声明", "not a return guarantee" in boot1["disclaimer"])

print("\n[3] 样本门槛和样本外门槛共同控制晋级")
report = promotion_report(
    baseline_trades=baseline, candidate_trades=candidate, fixture_hash="a" * 64,
    code_version="test", coverage_gaps=["historical_order_book"],
    directional_candidates=199, demo_closed_trades=29, observation_days=6,
)
check("样本不足只能继续观察", report["status"] == "continue_observation"
      and not report["sample_gate"]["passed"])
check("永不自动晋级", report["auto_promoted"] is False)
check("门槛配置已哈希冻结", len(report["threshold_hash"]) == 64)
check("覆盖缺口保留在报告", report["coverage_gaps"] == ["historical_order_book"])

enough_but_bad_oos = promotion_report(
    baseline_trades=baseline, candidate_trades=candidate, fixture_hash="b" * 64,
    code_version="test", coverage_gaps=[], directional_candidates=200,
    demo_closed_trades=30, observation_days=7,
)
check("样本足但样本外恶化仍不晋级", enough_but_bad_oos["sample_gate"]["passed"]
      and enough_but_bad_oos["status"] == "continue_observation")

print("\n[4] 基线与候选严格共用时间窗口")
shifted_candidate = [dict(row, closed_at_ms=row["closed_at_ms"] + 1000)
                     for row in candidate]
comparison = walk_forward_comparison(
    baseline_trades=baseline, candidate_trades=shifted_candidate)
check("共同窗口取两组时间交集", comparison["common_window"]["start_ms"] == 2500
      and comparison["common_window"]["end_ms"] == 10500)
for window in ("calibration", "validation", "out_of_sample"):
    check(f"{window}边界完全一致",
          comparison["baseline"][window]["start_ms"]
          == comparison["candidate"][window]["start_ms"]
          and comparison["baseline"][window]["end_ms"]
          == comparison["candidate"][window]["end_ms"])

print("\n[5] 机会成本和数据覆盖是独立晋级门禁")
good_baseline = rows([1, 1, 1, 1, 1, 1, 1, 1, 1, 1], "baseline")
good_candidate = [dict(row, variant="candidate") for row in good_baseline]
good_candidate[-1]["net_pnl"] = 2
low_coverage = promotion_report(
    baseline_trades=good_baseline, candidate_trades=good_candidate,
    fixture_hash="c" * 64, code_version="test", coverage_gaps=["order_book"],
    directional_candidates=200, demo_closed_trades=30, observation_days=7,
    data_coverage=0.89,
)
check("低数据覆盖单独阻止晋级", not low_coverage["data_coverage_gate"]["passed"]
      and low_coverage["status"] == "continue_observation")
costly_candidate = [dict(row, variant="candidate") for row in good_baseline[3:]]
cost_report = promotion_report(
    baseline_trades=good_baseline, candidate_trades=costly_candidate,
    fixture_hash="d" * 64, code_version="test", coverage_gaps=[],
    directional_candidates=200, demo_closed_trades=30, observation_days=7,
)
check("漏掉盈利机会超过上限时阻止晋级",
      cost_report["opportunity_cost"]["missed_positive_ratio"] == 0.3
      and not cost_report["out_of_sample_gate"]["opportunity_cost_within_limit"])
check("机会成本同时报告避免损失", "avoided_negative_pnl" in cost_report["opportunity_cost"])

print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
