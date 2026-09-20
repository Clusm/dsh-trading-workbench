"""USD-M Algo保护单镜像与持仓覆盖对账。"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Iterable

from audit.database import AuditDatabase, utc_now
from exchange.protection_commands import ensure_execution_schema, protection_view_from_algo


def _float(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _text_decimal(value: Any) -> str | None:
    """数量的十进制文本形式；缺失/非法返回 None，绝不补 0 冒充数量。"""
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return "0" if result == 0 else format(result.normalize(), "f")


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class ProtectionOrderMirror:
    """只读镜像 USD-M Algo 止损/止盈单并核对持仓覆盖。

    对每个非零白名单仓位，安全覆盖要求恰好一张同方向、同 positionSide 且触发价
    有效的 STOP_MARKET 和 TAKE_PROFIT_MARKET。One-way 使用同数量 reduce-only；
    Hedge Mode 使用官方要求的 closePosition=true（不发送 quantity/reduceOnly）。
    镜像本身不撤单或补单，只向上层报告异常分类。
    """
    def __init__(self, db: AuditDatabase, *, environment: str,
                 allowed_symbols: Iterable[str] = ()) -> None:
        self.db = db
        self.environment = str(environment)
        self.allowed_symbols = {str(s).upper() for s in allowed_symbols if s}
        # U04：幂等补齐数量十进制文本列（与 order_outbox 同一套列清单）。
        ensure_execution_schema(self.db.conn)
        self.last_reconcile_at: float | None = None
        self.last_error: str | None = None
        self.out_of_scope = 0

    def _allowed(self, symbol: str) -> bool:
        return symbol.endswith("USDT") and (
            not self.allowed_symbols or symbol in self.allowed_symbols)

    def reconcile_broker(self, broker: Any) -> dict[str, Any]:
        """从 Broker 拉取真实持仓和 Algo 单后执行一次只读对账。"""
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
        """对给定快照做确定性覆盖检查，并原子写入镜像与对账记录。"""
        held: dict[tuple[str, str], float] = {}
        for row in positions or []:
            symbol = str(row.get("symbol") or "").upper()
            amount = _float(row.get("positionAmt"))
            if self._allowed(symbol) and abs(amount) > 1e-12:
                explicit_side = str(row.get("positionSide") or "BOTH").upper()
                position_side = explicit_side if explicit_side in {"LONG", "SHORT"} else "BOTH"
                held[(symbol, position_side)] = amount

        valid: list[dict[str, Any]] = []
        by_position: dict[tuple[str, str], list[dict[str, Any]]] = {}
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
            position_side = str(row.get("positionSide") or "BOTH").upper()
            position_key = (symbol, position_side)
            by_position.setdefault(position_key, []).append(normalized)
            algo_id = str(row.get("algoId") or "")
            amount = held.get(position_key)
            if amount is None:
                orphan.append(algo_id)
                continue
            expected_side = "SELL" if amount > 0 else "BUY"
            if str(row.get("side") or "").upper() != expected_side:
                direction.append(algo_id)
            close_all = _bool(row.get("closePosition"))
            if not close_all and abs(_float(row.get("quantity")) - abs(amount)) > max(
                    1e-9, abs(amount) * 0.000001):
                quantity.append(algo_id)
            if (not (_bool(row.get("reduceOnly")) or close_all)
                    or _float(row.get("triggerPrice")) <= 0):
                unsafe.append(algo_id)

        missing: list[str] = []
        for (symbol, position_side), amount in held.items():
            expected_side = "SELL" if amount > 0 else "BUY"
            matching = [row for row in by_position.get((symbol, position_side), [])
                        if str(row.get("side") or "").upper() == expected_side and
                        (_bool(row.get("closePosition")) or (
                            abs(_float(row.get("quantity")) - abs(amount)) <= max(
                                1e-9, abs(amount) * 0.000001) and
                            _bool(row.get("reduceOnly")))) and
                        _float(row.get("triggerPrice")) > 0]
            types = [str(row.get("orderType") or "").upper() for row in matching]
            if types.count("STOP_MARKET") != 1 or types.count("TAKE_PROFIT_MARKET") != 1:
                missing.append(symbol if position_side == "BOTH" else f"{symbol}:{position_side}")

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
                        reduce_only,close_position,event_time_ms,updated_at,source,
                        quantity_text,actual_qty_text,trigger_price_text
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(environment,algo_id) DO UPDATE SET
                        client_algo_id=excluded.client_algo_id,symbol=excluded.symbol,
                        side=excluded.side,position_side=excluded.position_side,
                        order_type=excluded.order_type,purpose=excluded.purpose,
                        status=excluded.status,quantity=excluded.quantity,
                        actual_qty=excluded.actual_qty,trigger_price=excluded.trigger_price,
                        reduce_only=excluded.reduce_only,close_position=excluded.close_position,
                        event_time_ms=excluded.event_time_ms,updated_at=excluded.updated_at,
                        source=excluded.source,
                        quantity_text=excluded.quantity_text,
                        actual_qty_text=excluded.actual_qty_text,
                        trigger_price_text=excluded.trigger_price_text""",
                    (self.environment, algo_id,
                     str(row.get("clientAlgoId") or "") or None,
                     str(row.get("symbol")), str(row.get("side") or "").upper(),
                     str(row.get("positionSide") or "BOTH").upper(), order_type, purpose,
                     str(row.get("algoStatus") or "NEW").upper(),
                     _float(row.get("quantity")), _float(row.get("actualQty")),
                     _float(row.get("triggerPrice")), int(_bool(row.get("reduceOnly"))),
                     int(_bool(row.get("closePosition"))), event_ms, now, "rest_reconcile",
                     # REAL 列只用于展示；精确数量以十进制文本列勾稽（U04/DC04），
                     # 读取方是 ProtectionOrderMirror.mirrored_protection_views()。
                     _text_decimal(row.get("quantity")),
                     _text_decimal(row.get("actualQty")),
                     _text_decimal(row.get("triggerPrice"))),
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

    def mirrored_protection_views(
        self, *, symbol: str | None = None,
    ) -> tuple[Any, ...]:
        """把镜像行读回成 ``ProtectionView``（**文本列优先**）。

        这是三个 ``*_text`` 列的读取方：镜像写库时把交易所原值同时存进 REAL 列
        （仅供展示）与十进制文本列，这里经 ``protection_view_from_algo`` 取文本列，
        因此审计/重建路径上的数量与触发价全程 Decimal，与内存路径同一口径。

        注意：**不**用它替代交易所实时查询——镜像可能滞后，用它冒充"当前保护状态"
        会把过期保护当成已覆盖。实时保护刷新仍以 ``_open_algos`` 的交易所回报为准。

        **接线状态（known_limit，U04/r3-N4）**：本方法目前**没有任何生产调用者**。
        存在的理由是让三个 ``*_text`` 列有一个"精确勾稽"的读取方，从而把
        "镜像里的数量/触发价全程 Decimal"从写侧承诺变成可验证的读侧能力；在
        审计/恢复工具接入之前，它只能由测试直接调用（见
        ``tests/acceptance/test_execution_faults.py``）。也就是说：文本列**已写入、
        可读出、但还没有自动化消费者**——不构成保护刷新链路的组成部分。
        """
        sql = ("SELECT * FROM protection_order_mirror WHERE environment=? "
               "AND status='NEW'")
        params: list[Any] = [self.environment]
        if symbol:
            sql += " AND symbol=?"
            params.append(str(symbol).upper())
        views = []
        for row in self.db.fetch_all(sql + " ORDER BY updated_at DESC", tuple(params)):
            try:
                views.append(protection_view_from_algo(dict(row)))
            except Exception:  # noqa: BLE001 - 单行畸形跳过，不影响整批读取
                continue
        return tuple(views)

    def status(self) -> dict[str, Any]:
        """返回最近一次保护覆盖结果和当前镜像中的活动 Algo 单数量。"""
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
