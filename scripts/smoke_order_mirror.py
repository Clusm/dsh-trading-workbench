"""订单影子状态机、终态保护与重启重放回归。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402
from exchange.event_models import normalize_user_event  # noqa: E402
from exchange.order_mirror import OrderMirror  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


def order_event(*, event_ms: int, status: str, execution: str,
                cumulative: str, last_qty: str = "0", order_id: int = 101,
                symbol: str = "BTCUSDT", order_type: str = "LIMIT",
                reduce_only: bool = False) -> dict:
    return {
        "e": "ORDER_TRADE_UPDATE", "E": event_ms, "T": event_ms,
        "o": {
            "s": symbol, "c": f"mta-{order_id}", "i": order_id,
            "S": "BUY", "ps": "LONG", "o": order_type,
            "X": status, "x": execution, "q": "0.2", "z": cumulative,
            "ap": "60010" if cumulative != "0" else "0",
            "l": last_qty, "L": "60020" if last_qty != "0" else "0",
            "rp": "1.25", "n": "0.03", "N": "USDT", "R": reduce_only,
            "cp": False, "t": event_ms,
        },
    }


db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-order-mirror-")) / "orders.sqlite")
mirror = OrderMirror(db, environment="demo", allowed_symbols=["BTCUSDT"])

print("\n[1] NEW到部分成交再到完全成交只能向前迁移")
check("NEW已接收", mirror.apply_event(normalize_user_event(
    order_event(event_ms=1000, status="NEW", execution="NEW", cumulative="0"))))
mirror.apply_event(normalize_user_event(order_event(
    event_ms=1100, status="PARTIALLY_FILLED", execution="TRADE",
    cumulative="0.1", last_qty="0.1")))
partial = db.fetch_one(
    "SELECT * FROM exchange_order_mirror WHERE environment='demo' AND exchange_order_id='101'")
check("部分成交累计正确", partial["status"] == "PARTIALLY_FILLED" and
      partial["cumulative_qty"] == 0.1 and partial["last_fill_qty"] == 0.1)
mirror.apply_event(normalize_user_event(order_event(
    event_ms=1200, status="FILLED", execution="TRADE",
    cumulative="0.2", last_qty="0.1")))
filled = db.fetch_one(
    "SELECT * FROM exchange_order_mirror WHERE environment='demo' AND exchange_order_id='101'")
check("完全成交终态正确", filled["status"] == "FILLED" and
      filled["cumulative_qty"] == 0.2 and filled["avg_price"] == 60010)
check("普通非减仓单归类ENTRY", filled["purpose"] == "ENTRY")

print("\n[2] 迟到事件和非法终态回退均被阻断")
mirror.apply_event(normalize_user_event(order_event(
    event_ms=900, status="NEW", execution="NEW", cumulative="0")))
mirror.apply_event(normalize_user_event(order_event(
    event_ms=1300, status="NEW", execution="NEW", cumulative="0")))
unchanged = db.fetch_one(
    "SELECT status,cumulative_qty FROM exchange_order_mirror WHERE exchange_order_id='101'")
check("终态未倒退", unchanged["status"] == "FILLED" and unchanged["cumulative_qty"] == 0.2)
check("迟到事件有计数", mirror.stale_events == 1)
check("非法迁移有计数", mirror.invalid_transitions == 1)

print("\n[3] 保护单、退出单和交易范围分类")
mirror.apply_event(normalize_user_event(order_event(
    event_ms=2000, status="NEW", execution="NEW", cumulative="0",
    order_id=102, order_type="STOP_MARKET", reduce_only=True)))
stop = db.fetch_one("SELECT purpose,reduce_only FROM exchange_order_mirror WHERE exchange_order_id='102'")
check("止损单归类STOP", stop["purpose"] == "STOP" and stop["reduce_only"] == 1)
mirror.apply_event(normalize_user_event(order_event(
    event_ms=2100, status="NEW", execution="NEW", cumulative="0",
    order_id=103, order_type="MARKET", reduce_only=True)))
exit_order = db.fetch_one("SELECT purpose FROM exchange_order_mirror WHERE exchange_order_id='103'")
check("普通减仓单归类EXIT", exit_order["purpose"] == "EXIT")
check("非白名单订单仅审计不入镜像", mirror.apply_event(normalize_user_event(order_event(
    event_ms=2200, status="NEW", execution="NEW", cumulative="0",
    order_id=104, symbol="ETHUSDT"))) and not db.fetch_one(
        "SELECT exchange_order_id FROM exchange_order_mirror WHERE exchange_order_id='104'"))

print("\n[4] 重启重放收件箱中的未处理订单事件")
inbox = ExchangeEventInbox(db)
pending, _ = inbox.receive(order_event(
    event_ms=3000, status="CANCELED", execution="CANCELED", cumulative="0",
    order_id=105))
restarted = OrderMirror(db, environment="demo", allowed_symbols=["BTCUSDT"])
check("未处理订单事件已重放", restarted.replay_pending() == 1)
pending_status = db.fetch_one(
    "SELECT status FROM exchange_events WHERE event_key=?", (pending.event_key,))
check("重放事件进入APPLIED", pending_status["status"] == "APPLIED")
replayed = db.fetch_one(
    "SELECT status FROM exchange_order_mirror WHERE exchange_order_id='105'")
check("取消终态已恢复", replayed["status"] == "CANCELED")
status = restarted.status()
check("状态统计可观测", status["status_counts"].get("FILLED") == 1 and
      status["status_counts"].get("CANCELED") == 1)

versions = [r["version"] for r in db.fetch_all(
    "SELECT version FROM schema_migrations ORDER BY version")]
check("数据库迁移版本1到4完整", versions == [1, 2, 3, 4])
db.close()
print("\n结果: 15 通过 / 0 失败")
