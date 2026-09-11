"""J01-J03 USD-M 回放、成本、未来函数与事故夹具回归。"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.usdm_replay import load_fixture, replay_fixture  # noqa: E402

passed = failed = 0


def check(name: str, condition: bool, extra: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {extra}")


path = ROOT / "tests/fixtures/usdm_replay/incidents_2026_09.json"
fixture, digest = load_fixture(path)
original = copy.deepcopy(fixture)

print("\n[1] 不可变去敏事故夹具与确定性")
first = replay_fixture(fixture)
second = replay_fixture(fixture)
check("夹具市场严格限定 BINANCE_USDM", fixture["market"] == "BINANCE_USDM")
check("相同夹具重复结果一致", first == second)
check("回放不修改输入", fixture == original)
check("内容哈希稳定", len(digest) == 64)
check("三类事故均形成闭合交易", {t["incident"] for t in first["trades"]} == {
    "counter_trend_long", "live_bar_volume", "model_chain_interruption"})

print("\n[2] 成本、资金费率、滑点、精度与部分成交")
no_cost = replay_fixture(fixture, fees_enabled=False, funding_enabled=False,
                         slippage_enabled=False)
net = sum(t["net_pnl"] for t in first["trades"])
net_no_cost = sum(t["net_pnl"] for t in no_cost["trades"])
check("启用成本后净PnL不优于无成本", net < net_no_cost, f"{net} vs {net_no_cost}")
check("正资金费率对LONG为成本", first["trades"][0]["funding_pnl"] < 0)
check("盘口深度产生部分成交", any(t["partial_fill"] for t in first["trades"]))
check("数量遵循stepSize", first["trades"][1]["qty"] == 1.0)
check("缺历史盘口显式 coverage_gap", "historical_order_book" in first["coverage_gaps"])

print("\n[3] 时序和未来数据泄漏 fail-closed")
unordered = copy.deepcopy(fixture)
unordered["events"][1]["timestamp_ms"] = 500
try:
    replay_fixture(unordered)
    check("乱序事件被拒绝", False)
except ValueError as exc:
    check("乱序事件被拒绝", "ordered" in str(exc))
future = copy.deepcopy(fixture)
future["events"][0]["source_at_ms"] = 1001
try:
    replay_fixture(future)
    check("未来数据被拒绝", False)
except ValueError as exc:
    check("未来数据被拒绝", "future_data_leakage" in str(exc))

print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
