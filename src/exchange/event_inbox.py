"""持久化交易所事件收件箱；先落库，再由后续消费者更新账户镜像。"""
from __future__ import annotations

import json
from typing import Any

from audit.database import AuditDatabase, utc_now
from exchange.event_models import ExchangeEvent, normalize_user_event


class ExchangeEventInbox:
    """私有流事件的幂等收件箱；先落原始事件，再由各镜像状态机消费。"""
    def __init__(self, db: AuditDatabase):
        self.db = db

    def receive(self, raw: dict[str, Any], *, source: str = "user_stream") -> tuple[ExchangeEvent, bool]:
        event = normalize_user_event(raw, source=source)
        status = "RECEIVED" if event.supported else "IGNORED"
        processed_at = None if event.supported else utc_now()
        with self.db.lock:
            cursor = self.db.conn.execute(
                """INSERT OR IGNORE INTO exchange_events(
                    event_key, event_type, event_time_ms, transaction_time_ms,
                    received_at, processed_at, source, symbol, client_order_id,
                    exchange_order_id, trade_id, status, payload_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.event_key, event.event_type, event.event_time_ms,
                    event.transaction_time_ms, utc_now(), processed_at, event.source,
                    event.symbol, event.client_order_id, event.exchange_order_id,
                    event.trade_id, status,
                    json.dumps(event.payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            self.db.conn.commit()
            inserted = cursor.rowcount == 1
        return event, inserted

    def mark(self, event_key: str, status: str, error: str | None = None) -> None:
        normalized = str(status or "").upper()
        if normalized not in {"APPLIED", "IGNORED", "ERROR"}:
            raise ValueError(f"非法事件终态: {status!r}")
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE exchange_events SET status=?, error=?, processed_at=? WHERE event_key=?",
                (normalized, error, utc_now(), event_key),
            )
            self.db.conn.commit()

    def recover_transport_controls(self) -> int:
        """收口重启前已执行但未落终态的传输控制帧。"""
        with self.db.lock:
            cursor = self.db.conn.execute(
                "UPDATE exchange_events SET status='APPLIED', processed_at=? "
                "WHERE status='RECEIVED' AND event_type='listenKeyExpired'",
                (utc_now(),),
            )
            self.db.conn.commit()
            return max(0, int(cursor.rowcount))
