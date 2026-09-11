"""J04-J06 走步、bootstrap 与晋级报告离线回归。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.strategy_evaluation import bootstrap_net_pnl, promotion_report, walk_forward  # noqa: E402

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

print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
