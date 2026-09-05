"""订单Outbox状态机、幂等约束、重启恢复与POST异常对账回归。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402
from exchange.event_models import normalize_user_event  # noqa: E402
from exchange.order_outbox import (  # noqa: E402
    DuplicateOrderSubmission,
    OrderOutbox,
    PendingOrderConflict,
)


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  PASS  {name}")


def prepare(outbox: OrderOutbox, cid: str, *, symbol: str = "BTCUSDT",
            purpose: str = "ENTRY", side: str = "BUY") -> None:
    outbox.prepare(
        client_order_id=cid, symbol=symbol, side=side, position_side="LONG",
        order_type="MARKET", purpose=purpose, qty=0.1, price=None,
        reduce_only=purpose == "EXIT", endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": cid},
    )


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-outbox-")) / "outbox.sqlite")
outbox = OrderOutbox(db, environment="demo")

print("\n[1] POST前落库、clientOrderId幂等与未决开仓唯一约束")
prepare(outbox, "mta-entry-1")
row = db.fetch_one("SELECT * FROM order_outbox WHERE client_order_id='mta-entry-1'")
check("命令先进入SUBMITTING", row["status"] == "SUBMITTING" and row["attempt_count"] == 1)
try:
    prepare(outbox, "mta-entry-1")
    duplicate_blocked = False
except DuplicateOrderSubmission:
    duplicate_blocked = True
check("相同clientOrderId禁止重复POST", duplicate_blocked)
try:
    prepare(outbox, "mta-entry-2")
    business_blocked = False
except PendingOrderConflict:
    business_blocked = True
check("同symbol同方向未决开仓被唯一约束阻断", business_blocked)

print("\n[2] UNKNOWN阻断所有新开仓但允许退出单")
outbox.mark_unknown("mta-entry-1", TimeoutError("timeout"))
try:
    prepare(outbox, "mta-eth-entry", symbol="ETHUSDT")
    unknown_blocked = False
except PendingOrderConflict:
    unknown_blocked = True
check("任一UNKNOWN阻断其它新开仓", unknown_blocked)
prepare(outbox, "mta-exit-1", purpose="EXIT", side="SELL")
check("UNKNOWN期间仍允许减仓和平仓", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-exit-1'")["status"] == "SUBMITTING")

class QueryBroker:
    def query_order(self, symbol, client_order_id):
        return {"symbol": symbol, "clientOrderId": client_order_id,
                "orderId": 101, "status": "FILLED"}


check("UNKNOWN只查询并成功恢复", outbox.reconcile_unknown(QueryBroker()) == 1)
check("恢复后进入交易所终态", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-entry-1'")["status"] == "FILLED")

print("\n[3] 私有流推进状态且终态不可回退")
outbox.acknowledge("mta-exit-1", {"orderId": 102, "status": "NEW"})
event = normalize_user_event({
    "e": "ORDER_TRADE_UPDATE", "E": 1000, "T": 1000,
    "o": {"s": "BTCUSDT", "c": "mta-exit-1", "i": 102,
          "X": "FILLED", "x": "TRADE"},
})
check("私有流成交事件已应用", outbox.apply_event(event))
outbox.acknowledge("mta-exit-1", {"orderId": 102, "status": "NEW"})
check("迟到POST响应不能回退FILLED", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-exit-1'")["status"] == "FILLED")

print("\n[4] 重启把悬空SUBMITTING转换为UNKNOWN")
prepare(outbox, "mta-exit-stale", purpose="EXIT", side="SELL")
check("重启恢复转换一条记录", outbox.recover_stale_submitting() == 1)
check("悬空提交被标为UNKNOWN", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-exit-stale'")["status"] == "UNKNOWN")
outbox.acknowledge("mta-exit-stale", {"orderId": 202, "status": "CANCELED"})

print("\n[5] Broker唯一POST入口异常后按clientOrderId对账且不重发")
broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
broker.db = db
broker.order_outbox = outbox
post_calls = []

def signed(method, path, params=None, attempts=2):
    if method == "POST":
        post_calls.append(dict(params or {}))
        raise TimeoutError("response lost")
    return {"symbol": "ETHUSDT", "clientOrderId": "mta-lost-1",
            "orderId": 301, "status": "FILLED"}


broker._signed = signed
result = broker._submit_standard_order({
    "symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.2",
    "reduceOnly": "false", "newClientOrderId": "mta-lost-1",
}, purpose="ENTRY")
check("POST响应丢失后查询到原订单", result["orderId"] == 301)
check("整个恢复过程只POST一次", len(post_calls) == 1)
check("对账成功后Outbox进入FILLED", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-lost-1'")["status"] == "FILLED")

print("\n[6] 交易所明确拒绝不误标UNKNOWN")
def rejected(method, path, params=None, attempts=2):
    raise RuntimeError("Binance -2019: Margin is insufficient")


broker._signed = rejected
try:
    broker._submit_standard_order({
        "symbol": "SOLUSDT", "side": "SELL", "type": "MARKET", "quantity": "0.1",
        "reduceOnly": "true", "newClientOrderId": "mta-reject-1",
    }, purpose="EXIT")
    rejected_raised = False
except RuntimeError:
    rejected_raised = True
check("明确拒绝继续抛给调用方", rejected_raised)
check("明确拒绝进入REJECTED终态", db.fetch_one(
    "SELECT status FROM order_outbox WHERE client_order_id='mta-reject-1'")["status"] == "REJECTED")

status = outbox.status()
check("健康状态暴露UNKNOWN数量", status["unknown_orders"] == 0)
versions = [r["version"] for r in db.fetch_all(
    "SELECT version FROM schema_migrations ORDER BY version")]
check("数据库迁移版本1到7完整", versions == [1, 2, 3, 4, 5, 6, 7])
db.close()
print(f"\n结果: {passed} 通过 / 0 失败")
