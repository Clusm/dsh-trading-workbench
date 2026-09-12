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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from agents.models import TradeIntent  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402


class FakeDB:
    def __init__(self, open_row, known_ids=()):
        self.open_row = open_row
        self.known_ids = set(known_ids)
        self.orders = []
        self.outcomes = []
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

    def fetch_open_intent_by_client_order_id(self, client_order_id):
        return None

    def fetch_latest_open_intent(self, symbol, side, source):
        return None

    def log_trade_outcome(self, **kw):
        self.outcomes.append(kw)


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
assert len(db.outcomes) == 1, db.outcomes
assert db.outcomes[0]["status"] == "CLOSED"
assert db.outcomes[0]["exit_reason"] == "exchange_sync"
assert abs(db.outcomes[0]["pnl"] - 0.2393) < 1e-9
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
assert not db2.outcomes, db2.outcomes
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

print("\n=== 用例 7：正常币安平仓 → 精确绑定开仓 intent 并写净盈亏 ===")
with tempfile.TemporaryDirectory() as temp_dir:
    audit = AuditDatabase(Path(temp_dir) / "audit.sqlite")
    open_intent = TradeIntent(
        symbol="BTCUSDT", side="LONG", qty=0.01, leverage=5,
        client_order_id="mta-open-exact", confidence=0.73,
        expected_holding_minutes=90, invalidation="15m 收盘跌破 59000",
        strategy_id="trend-follow-v1", ticket_id="ticket-1",
    )
    audit.log_trade_intent(
        intent_id="intent-open-exact", decision_id="decision-open-exact",
        run_id="run-1", intent=open_intent, status="approved",
    )
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.db = audit
    broker.environment = "demo"
    broker.positions = lambda force=False: ([] if force else [{
        "symbol": "BTCUSDT", "positionAmt": "0.01", "entryPrice": "60000",
    }])
    broker._ledger_source = lambda symbol: ("ai", {
        "open_client_order_id": "mta-open-exact",
        "strategy_id": "trend-follow-v1", "ticket_id": "ticket-1",
        "margin_mode": "ISOLATED",
    })
    broker._market_max_qty = lambda symbol: 100.0
    broker._format_qty = lambda symbol, qty: str(qty)
    broker._submit_standard_order = lambda params, purpose: {
        "orderId": "exchange-close-1", "status": "FILLED", "avgPrice": "61000",
    }
    broker._wait_fill = lambda symbol, order_id: {
        "orderId": order_id, "status": "FILLED", "avgPrice": "61000",
    }
    broker._order_trade_fee = lambda symbol, order_id: (10.0, -0.25)
    broker._position_margin = lambda symbol: 0.0
    broker._log_order = lambda **kwargs: "mta-close-exact"
    broker._invalidate_cache = lambda: None
    broker._remove_ledger_position = lambda symbol: None
    broker._record_ledger_position = lambda *args, **kwargs: {}
    broker._resync_protection = lambda *args, **kwargs: True

    receipt = broker._close_or_reduce(TradeIntent(
        symbol="BTCUSDT", action="close", side="LONG", qty=0.01,
        client_order_id="mta-close-exact", order_source="ai",
    ))
    rows = audit.fetch_all("SELECT * FROM trade_outcomes")
    assert receipt and receipt["fill_unconfirmed"] is False, receipt
    assert len(rows) == 1, [dict(row) for row in rows]
    outcome = dict(rows[0])
    assert outcome["outcome_id"] == "bn-mta-close-exact"
    assert outcome["intent_id"] == "intent-open-exact"
    assert outcome["decision_id"] == "decision-open-exact"
    assert outcome["strategy_id"] == "trend-follow-v1"
    assert outcome["ticket_id"] == "ticket-1"
    assert abs(float(outcome["pnl"]) - 9.75) < 1e-9
    assert abs(float(outcome["pnl_pct"]) - 1.625) < 1e-9
    assert outcome["status"] == "CLOSED"
    print("  ✓ 已确认成交按净盈亏归因，且精确连回原始开仓论点")

    print("\n=== 用例 8：同一成交重复补记 → outcome_id 幂等 ===")
    broker._record_trade_outcome(
        outcome_id="bn-mta-close-exact", symbol="BTCUSDT", side="LONG", source="ai",
        entry_price=60000.0, exit_price=61000.0, qty=0.01, net_pnl=9.75,
        exit_reason="ai_decision", ledger_meta={"open_client_order_id": "mta-open-exact"},
    )
    assert audit.fetch_one("SELECT COUNT(*) AS n FROM trade_outcomes")["n"] == 1
    print("  ✓ 重试不会重复计算交易笔数或盈亏")
    audit.close()

print("\n全部通过")
