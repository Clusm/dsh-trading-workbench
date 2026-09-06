"""USD-M普通订单Outbox：POST前持久化，UNKNOWN只查询不重发。"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from audit.database import AuditDatabase, utc_now
from exchange.event_models import ExchangeEvent


_ACTIVE = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"}
_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
_RANK = {
    "SUBMITTING": 0, "UNKNOWN": 5, "ACKNOWLEDGED": 10,
    "NEW": 20, "PARTIALLY_FILLED": 30,
    "FILLED": 40, "CANCELED": 40, "EXPIRED": 40,
    "REJECTED": 40, "EXPIRED_IN_MATCH": 40,
}


class PendingOrderConflict(RuntimeError):
    """同一业务键已有未决开仓命令。"""


class DuplicateOrderSubmission(RuntimeError):
    """同一clientOrderId已持久化，禁止再次POST。"""


class OrderOutbox:
    """持久化普通订单提交状态，消除网络不确定性导致的重复下单。

    调用顺序必须是 ``prepare`` 落库后才能发送 POST。若请求超时或连接断开，订单进入
    ``UNKNOWN``，后续只按 ``clientOrderId`` 查询交易所，不允许重发。状态只能前进，
    终态不会被迟到的 WebSocket 事件回退。
    """
    def __init__(self, db: AuditDatabase, *, environment: str) -> None:
        self.db = db
        self.environment = str(environment)
        self.last_event_at: float | None = None
        self.last_reconcile_at: float | None = None
        self.last_error: str | None = None
        self.reconciled_unknown = 0

    @staticmethod
    def business_key(*, symbol: str, side: str, position_side: str | None,
                     purpose: str) -> str:
        """构造同一交易目的的稳定业务键，用于识别未决开仓冲突。"""
        return f"{purpose}:{symbol.upper()}:{(position_side or side).upper()}"

    def prepare(self, *, client_order_id: str, symbol: str, side: str,
                position_side: str | None, order_type: str, purpose: str,
                qty: float, price: float | None, reduce_only: bool,
                endpoint: str, request: dict[str, Any]) -> None:
        """在交易所 POST 前记录 SUBMITTING；重复 ID 或未决开仓会直接拒绝。"""
        now = utc_now()
        key = self.business_key(symbol=symbol, side=side,
                                position_side=position_side, purpose=purpose)
        with self.db.lock:
            existing = self.db.conn.execute(
                "SELECT status FROM order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if existing:
                raise DuplicateOrderSubmission(
                    f"clientOrderId {client_order_id} already {existing['status']}")
            if purpose.upper() == "ENTRY":
                unresolved = self.db.conn.execute(
                    "SELECT client_order_id FROM order_outbox WHERE environment=? "
                    "AND status IN ('SUBMITTING','UNKNOWN') LIMIT 1",
                    (self.environment,),
                ).fetchone()
                if unresolved:
                    raise PendingOrderConflict(
                        f"unresolved order {unresolved['client_order_id']} blocks new entry")
            try:
                self.db.conn.execute(
                    """INSERT INTO order_outbox(
                        client_order_id,environment,business_key,symbol,side,position_side,
                        order_type,purpose,qty,price,reduce_only,endpoint,status,request_json,
                        attempt_count,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (client_order_id, self.environment, key, symbol.upper(), side.upper(),
                     (position_side or "BOTH").upper(), order_type.upper(), purpose.upper(),
                     float(qty), price, int(reduce_only), endpoint, "SUBMITTING",
                     json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                     1, now, now),
                )
                self.db.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.db.conn.rollback()
                raise PendingOrderConflict(
                    f"pending entry already exists for {key}") from exc

    def recover_stale_submitting(self) -> int:
        """进程重启后SUBMITTING的POST结果未知，转换为UNKNOWN且绝不重发。"""
        with self.db.lock:
            cursor = self.db.conn.execute(
                "UPDATE order_outbox SET status='UNKNOWN',error_type='ProcessRestart',"
                "updated_at=? WHERE environment=? AND status='SUBMITTING'",
                (utc_now(), self.environment),
            )
            self.db.conn.commit()
            return int(cursor.rowcount)

    def acknowledge(self, client_order_id: str, response: dict[str, Any]) -> None:
        """用交易所同步响应确认订单；响应终态会同时写入解决时间。"""
        status = str(response.get("status") or "ACKNOWLEDGED").upper()
        if status not in _ACTIVE | _TERMINAL:
            status = "ACKNOWLEDGED"
        resolved = utc_now() if status in _TERMINAL else None
        self._update(
            client_order_id, status=status,
            exchange_order_id=str(response.get("orderId") or "") or None,
            response=response, error_type=None, resolved_at=resolved)

    def mark_unknown(self, client_order_id: str, exc: BaseException) -> None:
        """将无法确认提交结果的订单标为 UNKNOWN，等待查询或私有流收敛。"""
        self._update(client_order_id, status="UNKNOWN",
                     error_type=type(exc).__name__)

    def reject(self, client_order_id: str, exc: BaseException) -> None:
        """记录可确定未被交易所接受的订单失败。"""
        self._update(client_order_id, status="REJECTED",
                     error_type=type(exc).__name__, resolved_at=utc_now())

    def _update(self, client_order_id: str, *, status: str,
                exchange_order_id: str | None = None,
                response: dict[str, Any] | None = None,
                error_type: str | None = None,
                resolved_at: str | None = None) -> None:
        with self.db.lock:
            previous = self.db.conn.execute(
                "SELECT status FROM order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if not previous:
                return
            previous_status = str(previous["status"] or "SUBMITTING")
            if (previous_status in _TERMINAL and status != previous_status) or (
                    previous_status not in _TERMINAL and
                    _RANK.get(status, 0) < _RANK.get(previous_status, 0)):
                return
            self.db.conn.execute(
                """UPDATE order_outbox SET status=?,
                    exchange_order_id=COALESCE(?,exchange_order_id),
                    response_json=COALESCE(?,response_json),error_type=?,
                    updated_at=?,resolved_at=COALESCE(?,resolved_at)
                    WHERE client_order_id=?""",
                (status, exchange_order_id,
                 json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                 if response is not None else None,
                 error_type, utc_now(), resolved_at, client_order_id),
            )
            self.db.conn.commit()

    def apply_event(self, event: ExchangeEvent) -> bool:
        """应用匹配本环境 Outbox 的订单私有流事件；无关事件返回 False。"""
        if event.event_type != "ORDER_TRADE_UPDATE":
            return False
        order = event.payload.get("o") if isinstance(event.payload.get("o"), dict) else {}
        client_id = str(order.get("c") or "")
        if not client_id:
            return False
        row = self.db.fetch_one(
            "SELECT status FROM order_outbox WHERE client_order_id=? AND environment=?",
            (client_id, self.environment),
        )
        if not row:
            return False
        status = str(order.get("X") or "UNKNOWN").upper()
        if status not in _ACTIVE | _TERMINAL:
            status = str(row["status"])
        resolved = utc_now() if status in _TERMINAL else None
        self._update(client_id, status=status,
                     exchange_order_id=str(order.get("i") or "") or None,
                     response=order, error_type=None, resolved_at=resolved)
        self.last_event_at = time.time()
        self.last_error = None
        return True

    def reconcile_unknown(self, broker: Any) -> int:
        """按 clientOrderId 查询 UNKNOWN 订单，返回本轮成功收敛的数量。"""
        rows = self.db.fetch_all(
            "SELECT client_order_id,symbol FROM order_outbox "
            "WHERE environment=? AND status='UNKNOWN' ORDER BY updated_at",
            (self.environment,),
        )
        resolved = 0
        for row in rows:
            try:
                response = broker.query_order(
                    str(row["symbol"]), str(row["client_order_id"]))
                if isinstance(response, dict) and response.get("orderId"):
                    self.acknowledge(str(row["client_order_id"]), response)
                    resolved += 1
            except Exception as exc:
                self.last_error = type(exc).__name__
        self.reconciled_unknown += resolved
        self.last_reconcile_at = time.time()
        if not rows or resolved == len(rows):
            self.last_error = None
        return resolved

    def status(self) -> dict[str, Any]:
        """返回当前环境的状态计数和最近一次对账信息。"""
        counts = {str(row["status"]): int(row["count"]) for row in self.db.fetch_all(
            "SELECT status,COUNT(*) AS count FROM order_outbox "
            "WHERE environment=? GROUP BY status", (self.environment,))}
        return {
            "enabled": True,
            "environment": self.environment,
            "last_event_at": self.last_event_at,
            "last_reconcile_at": self.last_reconcile_at,
            "last_error": self.last_error,
            "unknown_orders": int(counts.get("UNKNOWN", 0)),
            "submitting_orders": int(counts.get("SUBMITTING", 0)),
            "reconciled_unknown": self.reconciled_unknown,
            "status_counts": counts,
        }
