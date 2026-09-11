"""J01-J03 USD-M 回放、成本、未来函数与事故夹具回归。"""
from __future__ import annotations

import copy
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.usdm_replay import load_fixture, replay_fixture  # noqa: E402
from core.usdm_replay_export import export_incident_fixture  # noqa: E402

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

print("\n[4] 重复开仓、部分平仓与延迟归因")
lifecycle = {
    "schema_version": 1,
    "fixture_id": "lifecycle",
    "market": "BINANCE_USDM",
    "instruments": {"BTCUSDT": {"tick_size": 0.1, "step_size": 0.001, "min_qty": 0.001}},
    "costs": {"taker_fee_bps": 5, "slippage_bps": 0},
    "events": [
        {"type": "order", "symbol": "BTCUSDT", "side": "LONG", "action": "open",
         "qty": 1, "reference_price": 100, "signal_price": 99, "submitted_at_ms": 900,
         "timestamp_ms": 1000},
        {"type": "order", "symbol": "BTCUSDT", "side": "LONG", "action": "open",
         "qty": 1, "reference_price": 110, "signal_price": 109, "submitted_at_ms": 1900,
         "timestamp_ms": 2000},
        {"type": "order", "symbol": "BTCUSDT", "side": "LONG", "action": "close",
         "qty": 0.5, "reference_price": 108, "signal_price": 109, "submitted_at_ms": 2900,
         "timestamp_ms": 3000},
    ],
}
lifecycle_result = replay_fixture(lifecycle)
check("重复开仓合并为加权成本", lifecycle_result["trades"][0]["entry_price"] == 105.0)
check("部分平仓保留剩余仓位", lifecycle_result["open_positions"][0]["qty"] == 1.5)
check("延迟成本按成交数量分摊", lifecycle_result["trades"][0]["latency_cost"] == 1.0)
check("回放记录进出场延迟", lifecycle_result["trades"][0]["entry_latency_ms"] == 100
      and lifecycle_result["trades"][0]["exit_latency_ms"] == 100)
bad_latency = copy.deepcopy(lifecycle)
bad_latency["events"][0]["submitted_at_ms"] = 1001
try:
    replay_fixture(bad_latency)
    check("成交早于提交被拒绝", False)
except ValueError as exc:
    check("成交早于提交被拒绝", "submission_after_fill" in str(exc))

print("\n[5] 真实审计白名单导出与去敏")
with tempfile.TemporaryDirectory() as temp_dir:
    db_path = Path(temp_dir) / "audit.sqlite"
    connection = sqlite3.connect(db_path)
    connection.executescript("""
        CREATE TABLE orders (
            client_order_id TEXT, symbol TEXT, side TEXT, qty REAL, price REAL,
            status TEXT, created_at TEXT
        );
        CREATE TABLE trade_intents (
            client_order_id TEXT, action TEXT, strategy_id TEXT
        );
    """)
    private_ids = ["private-open-id", "private-close-id"]
    connection.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", [
        (private_ids[0], "ARBUSDT", "LONG", 100, 0.2, "FILLED",
         "2026-09-07T13:00:00+00:00"),
        (private_ids[1], "ARBUSDT", "LONG", 40, 0.19, "FILLED",
         "2026-09-07T14:00:00+00:00"),
    ])
    connection.executemany("INSERT INTO trade_intents VALUES (?,?,?)", [
        (private_ids[0], "open", "ai:trend:v1"),
        (private_ids[1], "close", "ai:trend:v1"),
    ])
    connection.commit()
    connection.close()
    exported = export_incident_fixture(db_path, [{
        "incident": "counter_trend_long",
        "open_client_order_ids": [private_ids[0]],
        "close_client_order_ids": [private_ids[1]],
        "evidence": {"trend_1h": "down"},
    }])
    exported_text = json.dumps(exported, ensure_ascii=False)
    exported_result = replay_fixture(exported)
    check("真实成交归一化后可回放", len(exported_result["trades"]) == 1)
    check("导出保留事故证据", exported["events"][0]["trend_1h"] == "down")
    check("导出不泄露 client_order_id", all(value not in exported_text for value in private_ids))
    check("导出不包含 raw_json/账户字段", "raw_json" not in exported_text
          and "account" not in exported_text)
    check("导出带来源行哈希", len(exported["provenance"]["selected_rows_sha256"]) == 64)

print("\n[6] 9月7日真实去敏事故夹具")
real_path = ROOT / "tests/fixtures/usdm_replay/real_incidents_2026_09.json"
real_fixture, _ = load_fixture(real_path)
real_result = replay_fixture(real_fixture)
check("四段真实成交生命周期均闭合", len(real_result["trades"]) == 4
      and not real_result["open_positions"])
check("真实事故类型完整", {trade["incident"] for trade in real_result["trades"]} == {
    "sui_low_rr", "arb_counter_trend_long", "link_counter_trend_long",
    "avax_live_bar_volume",
})
real_text = json.dumps(real_fixture, ensure_ascii=False)
check("真实夹具不含订单ID与审计原文", "client_order_id" not in real_text
      and "raw_json" not in real_text and "reasoning" not in real_text)
check("真实夹具声明审计来源哈希", real_fixture["provenance"]["kind"]
      == "local_audit_allowlist")

print(f"\n结果：PASS {passed} / FAIL {failed}")
raise SystemExit(1 if failed else 0)
