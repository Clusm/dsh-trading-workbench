"""USD-M订单影子状态机；只消费交易所事实，不提交、撤销或修改订单。"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterable

from audit.database import AuditDatabase, utc_now
from exchange.event_models import ExchangeEvent, normalize_user_event


_USDT_PERPETUAL = re.compile(r"^[0-9A-Z]+USDT$")
_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
_RANK = {
    "NEW": 10,
    "PARTIALLY_FILLED": 20,
    "FILLED": 30,
    "CANCELED": 30,
    "EXPIRED": 30,
    "REJECTED": 30,
    "EXPIRED_IN_MATCH": 30,
}


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


class OrderMirror:
    def __init__(self, db: AuditDatabase, *, environment: str,
                 allowed_symbols: Iterable[str] = ()) -> None:
        self.db = db
        self.environment = str(environment)
        self.allowed_symbols = {str(s).upper() for s in allowed_symbols if s}
        self.last_event_at: float | None = None
        self.last_error: str | None = None
        self.events_applied = 0
        self.stale_events = 0
        self.invalid_transitions = 0
        self.out_of_scope = 0

    def _allowed(self, symbol: str) -> bool:
        return bool(_USDT_PERPETUAL.fullmatch(symbol)) and (
            not self.allowed_symbols or symbol in self.allowed_symbols)

    @staticmethod
    def _purpose(order_type: str, reduce_only: bool, close_position: bool) -> str:
        upper = order_type.upper()
        if "TAKE_PROFIT" in upper:
            return "TAKE_PROFIT"
        if "STOP" in upper:
            return "STOP"
        if reduce_only or close_position:
            return "EXIT"
        return "ENTRY"

    def apply_event(self, event: ExchangeEvent) -> bool:
        if event.event_type != "ORDER_TRADE_UPDATE":
            return False
        order = event.payload.get("o") if isinstance(event.payload.get("o"), dict) else {}
        symbol = str(order.get("s") or "").upper()
        if not self._allowed(symbol):
            self.out_of_scope += 1
            return True
        exchange_id = str(order.get("i") or "")
        if not exchange_id:
            self.last_error = "MissingExchangeOrderId"
            return False
        status = str(order.get("X") or "UNKNOWN").upper()
        event_ms = int(event.transaction_time_ms or event.event_time_ms or 0)
        reduce_only = bool(order.get("R", False))
        close_position = bool(order.get("cp", False))
        order_type = str(order.get("o") or "UNKNOWN").upper()
        now = utc_now()
        with self.db.lock:
            previous = self.db.conn.execute(
                "SELECT status,event_time_ms FROM exchange_order_mirror "
                "WHERE environment=? AND exchange_order_id=?",
                (self.environment, exchange_id),
            ).fetchone()
            if previous and event_ms < int(previous["event_time_ms"] or 0):
                self.stale_events += 1
                return True
            previous_status = str(previous["status"] or "UNKNOWN") if previous else None
            if previous_status and previous_status in _TERMINAL and status != previous_status:
                self.invalid_transitions += 1
                return True
            if (previous_status and previous_status not in _TERMINAL and
                    _RANK.get(status, 0) < _RANK.get(previous_status, 0)):
                self.invalid_transitions += 1
                return True
            self.db.conn.execute(
                """INSERT INTO exchange_order_mirror(
                    environment,exchange_order_id,client_order_id,symbol,side,
                    position_side,order_type,purpose,status,execution_type,
                    original_qty,cumulative_qty,avg_price,last_fill_qty,last_fill_price,
                    realized_pnl,commission,commission_asset,reduce_only,close_position,
                    trade_id,event_time_ms,updated_at,source
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(environment,exchange_order_id) DO UPDATE SET
                    client_order_id=excluded.client_order_id,symbol=excluded.symbol,
                    side=excluded.side,position_side=excluded.position_side,
                    order_type=excluded.order_type,purpose=excluded.purpose,
                    status=excluded.status,execution_type=excluded.execution_type,
                    original_qty=excluded.original_qty,cumulative_qty=excluded.cumulative_qty,
                    avg_price=excluded.avg_price,last_fill_qty=excluded.last_fill_qty,
                    last_fill_price=excluded.last_fill_price,realized_pnl=excluded.realized_pnl,
                    commission=excluded.commission,commission_asset=excluded.commission_asset,
                    reduce_only=excluded.reduce_only,close_position=excluded.close_position,
                    trade_id=excluded.trade_id,event_time_ms=excluded.event_time_ms,
                    updated_at=excluded.updated_at,source=excluded.source""",
                (
                    self.environment, exchange_id, str(order.get("c") or "") or None,
                    symbol, str(order.get("S") or "").upper() or None,
                    str(order.get("ps") or "BOTH").upper(), order_type,
                    self._purpose(order_type, reduce_only, close_position), status,
                    str(order.get("x") or "").upper() or None,
                    _float(order.get("q")), _float(order.get("z")), _float(order.get("ap")),
                    _float(order.get("l")), _float(order.get("L")), _float(order.get("rp")),
                    _float(order.get("n")), str(order.get("N") or "") or None,
                    int(reduce_only), int(close_position), str(order.get("t") or "") or None,
                    event_ms, now, "user_stream",
                ),
            )
            self.db.conn.commit()
        self.last_event_at = time.time()
        self.last_error = None
        self.events_applied += 1
        return True

    def replay_pending(self) -> int:
        rows = self.db.fetch_all(
            "SELECT event_key,payload_json,source FROM exchange_events "
            "WHERE status='RECEIVED' AND event_type='ORDER_TRADE_UPDATE' ORDER BY id")
        if not rows:
            return 0
        from exchange.event_inbox import ExchangeEventInbox
        inbox = ExchangeEventInbox(self.db)
        applied = 0
        for row in rows:
            try:
                event = normalize_user_event(
                    json.loads(row["payload_json"]), source=str(row["source"]))
                if self.apply_event(event):
                    inbox.mark(str(row["event_key"]), "APPLIED")
                    applied += 1
            except Exception as exc:
                self.last_error = type(exc).__name__
        return applied

    def status(self) -> dict[str, Any]:
        counts = {str(row["status"]): int(row["count"]) for row in self.db.fetch_all(
            "SELECT status,COUNT(*) AS count FROM exchange_order_mirror "
            "WHERE environment=? GROUP BY status", (self.environment,))}
        return {
            "enabled": True,
            "shadow_only": True,
            "environment": self.environment,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "events_applied": self.events_applied,
            "stale_events": self.stale_events,
            "invalid_transitions": self.invalid_transitions,
            "out_of_scope": self.out_of_scope,
            "status_counts": counts,
        }
