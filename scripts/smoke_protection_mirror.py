"""Algo保护单镜像、持仓覆盖、孤儿与错配回归。"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from exchange.protection_mirror import ProtectionOrderMirror  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  PASS  {name}")


def algo(algo_id: int, symbol: str, order_type: str, side: str, qty: str,
         trigger: str) -> dict:
    return {
        "algoId": algo_id, "clientAlgoId": f"client-{algo_id}",
        "algoType": "CONDITIONAL", "orderType": order_type,
        "symbol": symbol, "side": side, "positionSide": "BOTH",
        "quantity": qty, "actualQty": "0", "triggerPrice": trigger,
        "algoStatus": "NEW", "reduceOnly": True, "closePosition": False,
        "createTime": 1000, "updateTime": 1001,
    }


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-protection-")) / "mirror.sqlite")
mirror = ProtectionOrderMirror(
    db, environment="demo", allowed_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
positions = [
    {"symbol": "BTCUSDT", "positionAmt": "0.1"},
    {"symbol": "ETHUSDT", "positionAmt": "-2"},
]
valid = [
    algo(1, "BTCUSDT", "STOP_MARKET", "SELL", "0.1", "59000"),
    algo(2, "BTCUSDT", "TAKE_PROFIT_MARKET", "SELL", "0.1", "62000"),
    algo(3, "ETHUSDT", "STOP_MARKET", "BUY", "2", "3100"),
    algo(4, "ETHUSDT", "TAKE_PROFIT_MARKET", "BUY", "2", "2800"),
]

print("\n[1] 现有持仓保护单可被镜像接管")
result = mirror.reconcile_snapshot(positions, valid)
check("双向持仓SL/TP覆盖一致", result["ok"] is True and result["open_algos"] == 4)
check("四张保护单进入活动镜像", db.fetch_one(
    "SELECT COUNT(*) AS count FROM protection_order_mirror WHERE status='NEW'")["count"] == 4)
stop = db.fetch_one("SELECT * FROM protection_order_mirror WHERE algo_id='1'")
check("止损字段与只减仓标志正确", stop["purpose"] == "STOP" and
      stop["trigger_price"] == 59000 and stop["reduce_only"] == 1)

print("\n[2] 缺失、孤儿、方向和数量错配均可识别")
missing = mirror.reconcile_snapshot(positions, valid[:1] + valid[2:])
check("缺少止盈被识别", missing["missing_protection"] == ["BTCUSDT"])
bad = valid + [
    algo(5, "SOLUSDT", "STOP_MARKET", "SELL", "1", "100"),
    algo(6, "BTCUSDT", "STOP_MARKET", "BUY", "0.1", "58000"),
    algo(7, "ETHUSDT", "STOP_MARKET", "BUY", "3", "3200"),
    algo(8, "DOGEUSDT", "STOP_MARKET", "SELL", "10", "0.1"),
    algo(9, "BTCUSDT", "STOP_MARKET", "SELL", "0.1", "58500"),
    dict(algo(10, "ETHUSDT", "TAKE_PROFIT_MARKET", "BUY", "1", "2700"),
         reduceOnly=False),
]
bad_result = mirror.reconcile_snapshot(positions, bad)
check("非白名单Algo不进入接管镜像", mirror.out_of_scope == 1)
check("无仓位孤儿单被识别", bad_result["orphan_orders"] == ["5"])
check("错误方向被识别", bad_result["direction_mismatch"] == ["6"])
check("超量和不足保护单都被识别", bad_result["qty_mismatch"] == ["10", "7"])
check("非reduceOnly保护单被识别", bad_result["unsafe_orders"] == ["10"])
check("重复保护类型使覆盖校验失败", set(bad_result["missing_protection"]) == {"BTCUSDT"})

print("\n[3] REST消失的保护单退出活动镜像")
mirror.reconcile_snapshot(positions, valid[:2])
inactive = db.fetch_one(
    "SELECT COUNT(*) AS count FROM protection_order_mirror WHERE status='NOT_OPEN'")
check("不再开放的Algo被标记NOT_OPEN", inactive["count"] >= 2)

class FakeBroker:
    def positions(self, force=False):
        assert force is True
        return positions

    def open_algo_orders(self):
        return valid


check("Broker强制REST入口可完成接管对账", mirror.reconcile_broker(FakeBroker())["ok"] is True)
status = mirror.status()
check("健康状态标记read_enabled", status["read_enabled"] is True)
check("最新对账与活动单数量可观测", status["active_orders"] == 4 and
      status["last_reconciliation"]["matched"] == 1)
versions = [r["version"] for r in db.fetch_all(
    "SELECT version FROM schema_migrations ORDER BY version")]
check("数据库迁移版本1到14完整", versions == list(range(1, 15)))
db.close()

print("\n[4] 仓位归零后自动收口残留Algo保护单")
class CleanupDB:
    def __init__(self):
        self.canceled = []
        self.events = []

    def cancel_order_by_exchange_id(self, *, exchange_order_id):
        self.canceled.append(exchange_order_id)

    def log_risk_event(self, **kwargs):
        self.events.append(kwargs)


cleanup_db = CleanupDB()
broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker.db = cleanup_db
broker._resync_lock = threading.RLock()
broker.positions = lambda force=False: []
broker._open_algos = lambda symbol=None: [
    algo(99, "BTCUSDT", "STOP_MARKET", "SELL", "0.1", "59000")]
deletes = []
broker._signed = lambda method, path, params=None, attempts=2: deletes.append(
    (method, path, dict(params or {}))) or {"code": "200"}
check("无仓位时残留Algo被撤销", broker._cleanup_closed_symbol_algos("BTCUSDT") == 1)
check("撤销同时收口本地订单生命周期", cleanup_db.canceled == ["99"])
check("自动清理事件可审计", cleanup_db.events[0]["event_type"] ==
      "futures_orphan_protection_canceled")
broker.positions = lambda force=False: [{"symbol": "BTCUSDT", "positionAmt": "0.1"}]
check("二次确认仍有仓位时绝不撤保护单",
      broker._cleanup_closed_symbol_algos("BTCUSDT") == 0 and len(deletes) == 1)
broker.positions = lambda force=False: []
broker._open_algos = lambda symbol=None: [
    algo(100, "BTCUSDT", "STOP_MARKET", "SELL", "0.1", "59000")
] if symbol is None else [algo(100, "BTCUSDT", "STOP_MARKET", "SELL", "0.1", "59000")]
check("首次清理失败后后续同步仍会重试孤儿单",
      broker._retry_orphan_algo_cleanup(set()) == 1 and deletes[-1][2]["algoId"] == "100")
print(f"\n结果: {passed} 通过 / 0 失败")
