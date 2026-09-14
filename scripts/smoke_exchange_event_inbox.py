"""USD-M私有事件标准化、脱敏和幂等收件箱回归。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db = AuditDatabase(Path(tempfile.mkdtemp()) / "events.sqlite")
inbox = ExchangeEventInbox(db)

print("\n[1] ORDER_TRADE_UPDATE字段提取与幂等")
order_frame = {
    "stream": "private",
    "data": {
        "e": "ORDER_TRADE_UPDATE", "E": 1002, "T": 1001,
        "o": {"s": "BTCUSDT", "c": "mta-abc", "i": 12345, "t": 77,
              "x": "TRADE", "X": "PARTIALLY_FILLED"},
    },
}
event, inserted = inbox.receive(order_frame)
_, inserted_again = inbox.receive(order_frame)
check("首次事件已插入", inserted)
check("重复事件被唯一键去重", not inserted_again)
check("订单标识提取正确", event.client_order_id == "mta-abc" and event.exchange_order_id == "12345")
check("交易对标准化", event.symbol == "BTCUSDT")

print("\n[2] listenKeyExpired落库前脱敏")
expired, _ = inbox.receive({"e": "listenKeyExpired", "E": 2000, "listenKey": "secret-value"})
row = db.fetch_one("SELECT payload_json FROM exchange_events WHERE event_key=?", (expired.event_key,))
payload = json.loads(row["payload_json"])
check("listenKey未写入数据库", payload["listenKey"] == "<redacted>" and "secret-value" not in row["payload_json"])
check("遗留过期控制帧可恢复终态", inbox.recover_transport_controls() == 1 and
      db.fetch_one("SELECT status FROM exchange_events WHERE event_key=?", (expired.event_key,))["status"] == "APPLIED")

print("\n[3] 未知事件可审计但不进入消费者")
unknown, inserted_unknown = inbox.receive({"e": "NEW_FUTURE_EVENT", "E": 3000})
unknown_row = db.fetch_one("SELECT status FROM exchange_events WHERE event_key=?", (unknown.event_key,))
check("未知事件已记录", inserted_unknown)
check("未知事件标记IGNORED", unknown_row["status"] == "IGNORED")

print("\n[4] 合法状态迁移")
inbox.mark(event.event_key, "APPLIED")
applied = db.fetch_one("SELECT status, processed_at FROM exchange_events WHERE event_key=?", (event.event_key,))
check("事件可标记已应用", applied["status"] == "APPLIED" and bool(applied["processed_at"]))
versions = [row["version"] for row in db.fetch_all("SELECT version FROM schema_migrations ORDER BY version")]
check("数据库已记录迁移1到16", versions == list(range(1, 17)))

db.close()
print("\n结果: 10 通过 / 0 失败")
