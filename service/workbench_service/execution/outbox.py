"""订单 Outbox：POST 前落库、结果不确定只查询不重发（docs/03 §5.7 / §2.4 I1/I2）。

状态词表与 ``orders`` 表一致；终态不可回退（迟到的查询结果只更新成交数量，
不改终态）。UNKNOWN 只能由交易所查询收敛；人工收口不在 M1.2 暴露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..ledger.store import DuplicateOrderError, Ledger, PendingOrderConflict, utc_now

__all__ = [
    "OrderOutbox",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "NOT_FOUND_REASON",
    "OutboxPrepareResult",
]

ACTIVE_STATUSES = frozenset(
    {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"}
)
# TRIGGERED（2026-11-14）：algos 的 algoStatus=FINISHED 是交易所明确事实——
# 该条件单已触发并产生实际订单（带 actualOrderId），不再处于挂单状态。
# 旧实现把它当成未知 → 回落 ACKNOWLEDGED（活动态），于是订单永久停在“活动”，
# 平仓后仍显示 7 张“在途保护单”。终态必须由交易所事实收敛，不能靠 TTL 猜测。
TERMINAL_STATUSES = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED", "TRIGGERED"})
#: 交易所按原 client_order_id 查不到该单时的收敛原因（明确终态，不是“重试用尽”）。
NOT_FOUND_REASON = "canceled_not_found_at_exchange"
_KNOWN_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
_RANK = {
    "SUBMITTING": 0,
    "UNKNOWN": 5,
    "ACKNOWLEDGED": 10,
    "NEW": 20,
    "PARTIALLY_FILLED": 30,
    "FILLED": 40,
    "CANCELED": 40,
    "EXPIRED": 40,
    "REJECTED": 40,
    "TRIGGERED": 45,
}


@dataclass(frozen=True, slots=True)
class OutboxPrepareResult:
    client_order_id: str
    business_key: str
    duplicate: bool = False


class OrderOutbox:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        #: 被拒绝的迟到状态回退次数（观测/测试用）。
        self.suppressed_transitions = 0

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def business_key(*, purpose: str, symbol: str, position_side: str | None) -> str:
        return f"{purpose.upper()}:{symbol.upper()}:{(position_side or 'BOTH').upper()}"

    @staticmethod
    def status_from_response(response: dict[str, Any]) -> str:
        raw = str(
            response.get("status") or response.get("algoStatus") or "ACKNOWLEDGED"
        ).upper()
        if raw == "EXPIRED_IN_MATCH":
            raw = "EXPIRED"
        # algos：FINISHED 表示已触发并产生实际订单；不再当作活动态。
        if raw == "FINISHED" and response.get("actualOrderId"):
            return "TRIGGERED"
        return raw if raw in _KNOWN_STATUSES else "ACKNOWLEDGED"

    # ------------------------------------------------------------------ prepare
    def prepare(
        self,
        *,
        client_order_id: str,
        purpose: str,
        symbol: str,
        side: str,
        position_side: str | None,
        order_type: str,
        endpoint: str,
        request: dict[str, Any],
        qty: Any,
        price: Any = None,
        reduce_only: bool = False,
        close_position: bool = False,
    ) -> OutboxPrepareResult:
        """POST 前持久化意图；重复 ID / 在途开仓冲突由 ledger 抛出。"""

        business_key = self.business_key(
            purpose=purpose, symbol=symbol, position_side=position_side
        )
        try:
            self.ledger.outbox_prepare(
                {
                    "client_order_id": client_order_id,
                    "environment": "demo",
                    "business_key": business_key,
                    "endpoint": endpoint,
                    "request": request,
                    "status": "SUBMITTING",
                }
            )
        except DuplicateOrderError:
            return OutboxPrepareResult(client_order_id, business_key, duplicate=True)
        except PendingOrderConflict:
            raise
        return OutboxPrepareResult(client_order_id, business_key)

    # ------------------------------------------------------------------ 状态
    @staticmethod
    def can_transition(current: str, new: str) -> bool:
        """状态只前进：终态不可回退，工作态不允许降级（迟到响应保护）。"""

        current = (current or "").upper()
        new = (new or "").upper()
        if current in TERMINAL_STATUSES:
            return new == current
        if current not in _KNOWN_STATUSES:
            return True
        return _RANK.get(new, 0) >= _RANK.get(current, 0)

    def acknowledge(self, client_order_id: str, response: dict[str, Any]) -> str:
        status = self.status_from_response(response)
        row = self.ledger.outbox_get(client_order_id)
        if row is not None and not self.can_transition(str(row.get("status")), status):
            self.suppressed_transitions += 1
            return str(row.get("status"))
        self.ledger.outbox_update(
            client_order_id,
            status=status,
            resolved_at=utc_now() if status in TERMINAL_STATUSES else None,
        )
        return status

    def mark_unknown(self, client_order_id: str, exc: BaseException) -> None:
        row = self.ledger.outbox_get(client_order_id)
        if row is not None and str(row.get("status")).upper() in TERMINAL_STATUSES:
            self.suppressed_transitions += 1
            return
        self.ledger.outbox_update(
            client_order_id,
            status="UNKNOWN",
            unknown_since=utc_now(),
            last_error=f"{type(exc).__name__}: {str(exc)[:200]}",
        )

    def reject(self, client_order_id: str, exc: BaseException) -> None:
        row = self.ledger.outbox_get(client_order_id)
        if row is not None and str(row.get("status")).upper() in TERMINAL_STATUSES:
            self.suppressed_transitions += 1
            return
        self.ledger.outbox_update(
            client_order_id,
            status="REJECTED",
            last_error=f"{type(exc).__name__}: {str(exc)[:200]}",
            resolved_at=utc_now(),
        )

    def resolve(self, client_order_id: str, status: str) -> None:
        status = status.upper()
        if status not in _KNOWN_STATUSES:
            status = "ACKNOWLEDGED"
        row = self.ledger.outbox_get(client_order_id)
        if row is not None and not self.can_transition(str(row.get("status")), status):
            self.suppressed_transitions += 1
            return
        self.ledger.outbox_update(
            client_order_id,
            status=status,
            resolved_at=utc_now() if status in TERMINAL_STATUSES else None,
        )

    def get(self, client_order_id: str) -> dict[str, Any] | None:
        return self.ledger.outbox_get(client_order_id)

    # --------------------------------------------------------------- 查询收敛
    def note_query(self, client_order_id: str, *, found: bool, error: str | None = None) -> int:
        """记录一次按原标识的查询事实；返回累计查询次数。"""

        return self.ledger.outbox_note_query(
            client_order_id, found=found, error=error
        )

    def converge_not_found(
        self,
        client_order_id: str,
        *,
        reason: str = NOT_FOUND_REASON,
        confirmations_required: int = 1,
    ) -> bool:
        """交易所明确回答“无此单”后收敛到 CANCELED。

        只有在累计独立“查不到”的事实达到 confirmations_required 时才收敛：
        一次迟到的 -2013 可能只是撮合尚未可见，提交重试次数本身不构成终态依据。
        """

        row = self.get(client_order_id)
        if row is None:
            return False
        status = str(row.get("status") or "").upper()
        if status in TERMINAL_STATUSES or status not in _KNOWN_STATUSES:
            return False
        confirmations = int(row.get("not_found_queries") or 0)
        if confirmations < max(1, int(confirmations_required)):
            return False
        return self.ledger.outbox_force_terminal(
            client_order_id, status="CANCELED", reason=reason
        )

    def active(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.ledger.outbox_active(limit)

    def pending_confirmation(self, limit: int = 20) -> list[dict[str, Any]]:
        """提交中/结果未知的行：对外只能显示“待确认”。"""

        return [
            row
            for row in self.ledger.outbox_active(limit)
            if str(row.get("status") or "").upper() in {"SUBMITTING", "UNKNOWN"}
        ]

    def counts(self) -> dict[str, int]:
        return self.ledger.outbox_counts()

    def unknown(self, limit: int = 5) -> list[dict[str, Any]]:
        return self.ledger.outbox_unknown(limit)

    def can_open_new(self) -> bool:
        counts = self.counts()
        return counts.get("unknown", 0) == 0 and counts.get("submitting", 0) == 0

    def stale_submitting(self, *, older_than_s: float = 120.0, limit: int = 20) -> list[dict[str, Any]]:
        """SUBMITTING rows older than ``older_than_s``.

        A crash, a rejected request that bypassed ``reject()``, or an aborted
        process leaves rows stuck in SUBMITTING; ``can_open_new()`` then blocks
        every later entry (R17) forever. Reconciliation resolves them by
        querying the exchange (never by re-sending).
        """

        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1.0, float(older_than_s)))
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        try:
            rows = self.ledger.query(
                "SELECT * FROM order_outbox WHERE status='SUBMITTING' AND created_at < ? "
                "ORDER BY created_at LIMIT ?",
                (cutoff, int(limit)),
            )
        except Exception:  # noqa: BLE001 - 老库/只读库不应让对账失败
            return []
        return [dict(row) for row in rows]
    # ------------------------------------------------------------------ 对账
    def reconcile(
        self,
        querier: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        limit: int = 3,
    ) -> dict[str, Any]:
        """限量查询 UNKNOWN；**只查询**，绝不重发。"""

        rows = self.unknown(limit)
        resolved = 0
        errors = 0
        for row in rows:
            try:
                response = querier(row)
            except Exception:  # noqa: BLE001 - 查询失败保持 UNKNOWN
                errors += 1
                continue
            if response:
                self.acknowledge(str(row["client_order_id"]), response)
                resolved += 1
            else:
                # 记录“交易所查不到”这一事实；是否收敛由调用方按年龄/确认次数决定。
                self.note_query(
                    str(row["client_order_id"]), found=False, error=NOT_FOUND_REASON
                )
        return {"checked": len(rows), "resolved": resolved, "errors": errors}


def status_rank(status: str) -> int:
    return _RANK.get(status.upper(), 0)
