"""验证外部平仓补记逻辑（不联网：mock 掉交易所成交流水和数据库写入）。

覆盖点：
1. 能从成交流水里认出方向相反的那笔平仓
2. 已经记过的成交（系统自己下的单）不会重复补
3. 查不到成交时不写任何东西（绝不凭空捏造）
4. 写进去的字段能让 _enrich_orders 显示成"平多/平空"并带出盈亏
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402


class FakeDB:
    def __init__(self, open_row, known_ids=()):
        self.open_row = open_row
        self.known_ids = set(known_ids)
        self.orders = []
        self.events = []
        self.queries = []

    def fetch_all(self, sql, args=()):
        self.queries.append((" ".join(sql.split()), args))
        if "FROM positions" in sql:
            return [self.open_row] if self.open_row else []
        if "FROM orders" in sql:  # 去重查询：args[0] 是 exchange_order_id
            return [{"id": 1}] if args and args[0] in self.known_ids else []
        return []

    def log_order(self, **kw):
        self.orders.append(kw)

    def log_risk_event(self, **kw):
        self.events.append(kw)


def make_broker(db, trades):
    b = BinanceFuturesBroker.__new__(BinanceFuturesBroker)  # 绕过 __init__，不连网络
    b.db = db
    b._signed = lambda method, path, params=None, attempts=2: trades
    return b


NOW_MS = int(time.time() * 1000)
OPEN_LONG = {
    "id": 275, "symbol": "ZECUSDT", "side": "LONG", "qty": 0.12,
    "leverage": 10, "entry_price": 859.33,
    "opened_at": datetime.fromtimestamp((NOW_MS - 60_000) / 1000, timezone.utc).isoformat(),
}

TRADES = [
    # 最新在前（userTrades 默认倒序）
    {"orderId": 900003, "side": "SELL", "qty": "0.12", "price": "861.50",
     "realizedPnl": "0.26", "commission": "0.0207", "time": NOW_MS},
    {"orderId": 900002, "side": "BUY", "qty": "0.12", "price": "859.33",
     "realizedPnl": "0", "commission": "0.0206", "time": NOW_MS - 30_000},
]

print("=== 用例 1：本地 LONG，交易所有一笔未记录的 SELL → 应补记 ===")
db = FakeDB(OPEN_LONG)
make_broker(db, TRADES)._reconcile_one_close("ZECUSDT")
assert len(db.orders) == 1, db.orders
o = db.orders[0]
print(f"  side={o['side']} qty={o['qty']} price={o['price']} status={o['status']}")
print(f"  raw={o['raw']}")
assert o["side"] == "LONG", "side 应为原仓位方向，配合 action=close 显示成『平多』"
assert o["raw"]["action"] == "close"
assert o["qty"] == 0.12 and o["price"] == 861.50
assert abs(o["raw"]["net_pnl"] - (0.26 - 0.0207)) < 1e-9
assert o["order_source"] == "ai", "补记盈亏必须回到原持仓通道"
assert o["raw"]["sync_source"] == "exchange_sync"
print("  ✓ 补记内容正确")

# 验证 _enrich_orders 能把它显示成"平多"
from dashboard.app import _enrich_orders  # noqa: E402
row = {"id": 1, "symbol": "ZECUSDT", "side": o["side"], "qty": o["qty"],
       "price": o["price"], "status": "FILLED", "raw_json": __import__("json").dumps(o["raw"])}
enriched = _enrich_orders([row])[0]
print(f"  页面显示：{enriched['display_side']}  盈亏 {enriched['pnl']}  仓位 {enriched['notional']:.2f}U")
assert enriched["display_side"] == "平多", enriched["display_side"]
assert abs(enriched["pnl"] - 0.2393) < 1e-3
print("  ✓ 页面上会显示「平多」并带出盈亏")

print("\n=== 用例 2：该笔成交已记过（系统自己下的单）→ 不重复补 ===")
db2 = FakeDB(OPEN_LONG, known_ids={"900003"})
make_broker(db2, TRADES)._reconcile_one_close("ZECUSDT")
assert not db2.orders, db2.orders
print("  ✓ 未重复插入")

print("\n=== 用例 3：查不到方向匹配的成交 → 什么都不写 ===")
db3 = FakeDB(OPEN_LONG)
make_broker(db3, [{"orderId": 1, "side": "BUY", "qty": "0.12", "price": "859.33",
                   "realizedPnl": "0", "commission": "0", "time": NOW_MS}]
            )._reconcile_one_close("ZECUSDT")
assert not db3.orders, db3.orders
print("  ✓ 无成交证据时不凭空捏造")

print("\n=== 用例 4：本地没有 OPEN 记录 → 跳过 ===")
db4 = FakeDB(None)
make_broker(db4, TRADES)._reconcile_one_close("ZECUSDT")
assert not db4.orders
print("  ✓ 跳过")

print("\n=== 用例 5：交易所接口超时抛异常 → 由外层兜住，不冒泡 ===")
db5 = FakeDB(OPEN_LONG)
b5 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
b5.db = db5


def boom(*a, **k):
    raise TimeoutError("read timeout")


b5._signed = boom
b5._reconcile_external_closes(["ZECUSDT"])  # 应被内部 try 吃掉
assert not db5.orders
assert any(e["event_type"] == "futures_close_reconcile_error" for e in db5.events)
print("  ✓ 异常被兜住并记为风险事件，不影响同步主流程")

print("\n=== 用例 6：一次最多补 3 个 symbol（避免重接口拖垮慢循环）===")
db6 = FakeDB(OPEN_LONG)
calls = []
b6 = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
b6.db = db6
b6._signed = lambda *a, **k: (calls.append(a[1]), TRADES)[1]
b6._reconcile_external_closes(["A", "B", "C", "D", "E"])
assert len(calls) == 3, calls
print(f"  ✓ 5 个待补只处理了 {len(calls)} 个")

print("\n全部通过")
