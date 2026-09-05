"""USD-M Algo保护单镜像与持仓覆盖对账。"""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

from audit.database import AuditDatabase, utc_now


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class ProtectionOrderMirror:
    def __init__(self, db: AuditDatabase, *, environment: str,
                 allowed_symbols: Iterable[str] = ()) -> None:
        self.db = db
        self.environment = str(environment)
        self.allowed_symbols = {str(s).upper() for s in allowed_symbols if s}
        self.last_reconcile_at: float | None = None
        self.last_error: str | None = None
        self.out_of_scope = 0

    def _allowed(self, symbol: str) -> bool:
        return symbol.endswith("USDT") and (
            not self.allowed_symbols or symbol in self.allowed_symbols)

    def reconcile_broker(self, broker: Any) -> dict[str, Any]:
        try:
            positions = broker.positions(force=True)
            algos = broker.open_algo_orders()
            result = self.reconcile_snapshot(positions, algos)
            self.last_error = None
            return result
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise

    def reconcile_snapshot(self, positions: list[dict[str, Any]],
                           algos: list[dict[str, Any]]) -> dict[str, Any]:
        held: dict[str, float] = {}
        for row in positions or []:
            symbol = str(row.get("symbol") or "").upper()
            amount = _float(row.get("positionAmt"))
            if self._allowed(symbol) and abs(amount) > 1e-12:
                held[symbol] = amount

        valid: list[dict[str, Any]] = []
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        orphan: list[str] = []
        direction: list[str] = []
        quantity: list[str] = []
        unsafe: list[str] = []
        for row in algos or []:
            symbol = str(row.get("symbol") or "").upper()
            if not self._allowed(symbol):
                self.out_of_scope += 1
                continue
            normalized = dict(row)
            normalized["symbol"] = symbol
            valid.append(normalized)
            by_symbol.setdefault(symbol, []).append(normalized)
            algo_id = str(row.get("algoId") or "")
            amount = held.get(symbol)
            if amount is None:
                orphan.append(algo_id)
                continue
            expected_side = "SELL" if amount > 0 else "BUY"
            if str(row.get("side") or "").upper() != expected_side:
                direction.append(algo_id)
            if abs(_float(row.get("quantity")) - abs(amount)) > max(
                    1e-9, abs(amount) * 0.000001):
                quantity.append(algo_id)
            if not _bool(row.get("reduceOnly")) or _float(row.get("triggerPrice")) <= 0:
                unsafe.append(algo_id)

        missing: list[str] = []
        for symbol, amount in held.items():
            expected_side = "SELL" if amount > 0 else "BUY"
            matching = [row for row in by_symbol.get(symbol, [])
                        if str(row.get("side") or "").upper() == expected_side and
                        abs(_float(row.get("quantity")) - abs(amount)) <= max(
                            1e-9, abs(amount) * 0.000001) and
                        _bool(row.get("reduceOnly")) and
                        _float(row.get("triggerPrice")) > 0]
            types = [str(row.get("orderType") or "").upper() for row in matching]
            if types.count("STOP_MARKET") != 1 or types.count("TAKE_PROFIT_MARKET") != 1:
                missing.append(symbol)

        details = {
            "orphan_orders": sorted(orphan),
            "missing_protection": sorted(missing),
            "direction_mismatch": sorted(direction),
            "qty_mismatch": sorted(quantity),
            "unsafe_orders": sorted(unsafe),
        }
        matched = not any(details.values())
        now = utc_now()
        now_ms = int(time.time() * 1000)
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE protection_order_mirror SET status='NOT_OPEN',updated_at=?,"
                "event_time_ms=?,source='rest_reconcile' "
                "WHERE environment=? AND status='NEW'",
                (now, now_ms, self.environment),
            )
            for row in valid:
                algo_id = str(row.get("algoId") or "")
                if not algo_id:
                    continue
                order_type = str(row.get("orderType") or "UNKNOWN").upper()
                purpose = "TAKE_PROFIT" if "TAKE_PROFIT" in order_type else (
                    "STOP" if "STOP" in order_type else "EXIT")
                event_ms = int(row.get("updateTime") or row.get("createTime") or now_ms)
                self.db.conn.execute(
                    """INSERT INTO protection_order_mirror(
                        environment,algo_id,client_algo_id,symbol,side,position_side,
                        order_type,purpose,status,quantity,actual_qty,trigger_price,
                        reduce_only,close_position,event_time_ms,updated_at,source
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(environment,algo_id) DO UPDATE SET
                        client_algo_id=excluded.client_algo_id,symbol=excluded.symbol,
                        side=excluded.side,position_side=excluded.position_side,
                        order_type=excluded.order_type,purpose=excluded.purpose,
                        status=excluded.status,quantity=excluded.quantity,
                        actual_qty=excluded.actual_qty,trigger_price=excluded.trigger_price,
                        reduce_only=excluded.reduce_only,close_position=excluded.close_position,
                        event_time_ms=excluded.event_time_ms,updated_at=excluded.updated_at,
                        source=excluded.source""",
                    (self.environment, algo_id,
                     str(row.get("clientAlgoId") or "") or None,
                     str(row.get("symbol")), str(row.get("side") or "").upper(),
                     str(row.get("positionSide") or "BOTH").upper(), order_type, purpose,
                     str(row.get("algoStatus") or "NEW").upper(),
                     _float(row.get("quantity")), _float(row.get("actualQty")),
                     _float(row.get("triggerPrice")), int(_bool(row.get("reduceOnly"))),
                     int(_bool(row.get("closePosition"))), event_ms, now, "rest_reconcile"),
                )
            self.db.conn.execute(
                """INSERT INTO protection_reconciliations(
                    checked_at,environment,matched,positions,open_algos,orphan_orders,
                    missing_protection,direction_mismatch,qty_mismatch,unsafe_orders,details_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (now, self.environment, int(matched), len(held), len(valid), len(orphan),
                 len(missing), len(direction), len(quantity), len(unsafe),
                 json.dumps(details, ensure_ascii=False, separators=(",", ":"))),
            )
            self.db.conn.commit()
        self.last_reconcile_at = time.time()
        return {"ok": matched, "matched": matched, "positions": len(held),
                "open_algos": len(valid), "missing": sorted(missing), **details,
                "reason": "" if matched else "保护单镜像与持仓不一致", "checked_at": now}

    def status(self) -> dict[str, Any]:
        latest = self.db.fetch_one(
            "SELECT * FROM protection_reconciliations "
            "WHERE environment=? ORDER BY id DESC LIMIT 1", (self.environment,))
        active = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM protection_order_mirror "
            "WHERE environment=? AND status='NEW'", (self.environment,))
        return {
            "enabled": True, "read_enabled": True, "environment": self.environment,
            "last_reconcile_at": self.last_reconcile_at, "last_error": self.last_error,
            "active_orders": int(active["count"] if active else 0),
            "out_of_scope": self.out_of_scope,
            "last_reconciliation": dict(latest) if latest else None,
        }
