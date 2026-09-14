"""只读情报侧车回归：临时库 + 模拟公开响应，不访问账户和订单。"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("MTA_BINANCE_WEIGHT_BUDGET_1M", "60000")

from audit.database import AuditDatabase  # noqa: E402
from intel.service import ReadOnlyIntelService  # noqa: E402
import httpx  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-intel-smoke-")) / "audit.sqlite")
service = ReadOnlyIntelService(
    db=db,
    config={"enabled": True, "refresh_seconds": 60, "priority_count": 1,
            "rotation_count": 1, "watched_wallets": [], "token_contracts": {}},
    symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XAUUSDT", "bad"],
)

mark_multiplier = {"value": 1.0}


def fake_get(_client, path, params=None):
    if path == "/fapi/v1/premiumIndex":
        return [
            {"symbol": symbol, "markPrice": str(price * mark_multiplier["value"]),
             "indexPrice": str(price), "lastFundingRate": "0.0001",
             "nextFundingTime": 1893456000000, "time": 1788652800000}
            for symbol, price in (("BTCUSDT", 100000), ("ETHUSDT", 4000),
                                  ("SOLUSDT", 200), ("XAUUSDT", 3500))
        ]
    if path.endswith("openInterest"):
        return {"openInterest": "1000", "time": 1788652800000}
    if path.endswith("depth"):
        return {"lastUpdateId": 123, "bids": [["99", "3"]], "asks": [["101", "1"]]}
    if path.endswith("takerlongshortRatio"):
        return [{"buySellRatio": "1.20", "timestamp": 1788652800000}]
    return [{"longShortRatio": "1.15", "timestamp": 1788652800000}]


service._get_json = fake_get  # type: ignore[method-assign]
print("\n[1] 首轮采集与隔离")
first = service.maybe_refresh(force=True)
check("固定影子模式", first["shadow_only"] and first["execution_weight"] == 0)
check("未配置钱包明确标识", first["wallet_tracker"]["status"] == "unconfigured")
check("分层轮转只采2个", len(first["symbols"]) == 2)
check("只保留标准USDT格式", all(row["symbol"].endswith("USDT") for row in first["symbols"]))
check("一致看多信号", all(row["signal"] == "bullish" for row in first["symbols"]))
check("迁移版本9已应用", db.fetch_one("SELECT version FROM schema_migrations WHERE version=9") is not None)
check("情报事件已落库", db.fetch_one("SELECT COUNT(*) n FROM intel_events")["n"] == 2)
check("影子决策权重为零", db.fetch_one("SELECT MAX(execution_weight) w FROM shadow_decisions")["w"] == 0)
sources = {row["source"] for row in db.fetch_all("SELECT DISTINCT source FROM shadow_decisions")}
check("B/C影子组同时记录", sources == {"B_contract_market", "C_contract_plus_web3"})
check("无Web3配置时C组未知", db.fetch_one(
    "SELECT COUNT(*) n FROM shadow_decisions WHERE source='C_contract_plus_web3' AND signal='unknown'")["n"] == 2)
baseline_intent = SimpleNamespace(action="open", symbol="BTCUSDT", side="LONG", confidence=0.7)
baseline_decision = SimpleNamespace(intents=[baseline_intent])
check("A组复制当前系统开仓方向", service.record_baseline_shadow(
    run_id="smoke", decision=baseline_decision, prices={"BTCUSDT": 100000}) == 1)
check("A组仍为零执行权重", db.fetch_one(
    "SELECT execution_weight FROM shadow_decisions WHERE source='A_current_baseline'")["execution_weight"] == 0)
service.record_liquidation({"E": int(time.time() * 1000), "o": {"s": "BTCUSDT", "S": "SELL",
                                                    "ap": "100000", "z": "0.1"}})
service.record_liquidation({"E": int(time.time() * 1000), "o": {"s": "BADUSDT", "S": "BUY",
                                                    "ap": "1", "z": "1"}})
check("强平流只保留白名单", len(service.snapshot()["liquidations"]) == 1)

retry_service = ReadOnlyIntelService(db=db, config={"enabled": False}, symbols=["BTCUSDT"])


class RetryClient:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        request = httpx.Request("GET", url)
        if self.calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)


retry_client = RetryClient()
check("公开行情503有界重试后恢复", retry_service._get_json(retry_client, "/fapi/v1/time") == {"ok": True}
      and retry_client.calls == 2)

print("\n[2] 到期影子回看")
with db.lock:
    old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="milliseconds")
    db.conn.execute("UPDATE shadow_decisions SET created_at=?", (old,))
    db.conn.commit()
mark_multiplier["value"] = 1.01
second = service.maybe_refresh(force=True)
db.evaluate_shadow_decisions(
    prices={row["symbol"]: row["mark_price"] for row in second["symbols"]}, grace_minutes=400)
outcomes = db.fetch_one("SELECT COUNT(*) n FROM shadow_outcomes")
check("轮转覆盖下一个标的", second["symbols"][1]["symbol"] != first["symbols"][1]["symbol"])
check("生成5m/15m/1h/4h回看", int(outcomes["n"]) >= 4)
check("看多且上涨判定正确", db.fetch_one("SELECT MIN(correct) c FROM shadow_outcomes WHERE correct IS NOT NULL")["c"] == 1)
metric = db.fetch_one(
    "SELECT gross_signal_return_pct,net_return_pct,mfe_pct,mae_pct,sample_count "
    "FROM shadow_outcomes WHERE correct IS NOT NULL LIMIT 1")
check("影子结果扣除费用与滑点", metric["net_return_pct"] < metric["gross_signal_return_pct"])
check("影子结果记录MFE/MAE", metric["mfe_pct"] is not None and metric["mae_pct"] is not None
      and metric["sample_count"] >= 2 and metric["mfe_pct"] >= 0 and metric["mae_pct"] <= 0)

db.close()
print("\n结果: 19 通过 / 0 失败")
