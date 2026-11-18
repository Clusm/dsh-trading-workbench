"""Per-strategy attribution, AI veto counterfactuals, and circuit-breaker checks."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..ledger.numbers import dec_text, to_decimal

if TYPE_CHECKING:
    from .store import StrategyStore


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _iso_to_ms(value: Any) -> int | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _closed_trades_by_strategy(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    if not (
        _table_exists(conn, "strategy_position_ownership")
        and _table_exists(conn, "orders")
        and _table_exists(conn, "fills")
    ):
        return {}

    ownership_rows = [
        dict(row)
        for row in conn.execute(
            "SELECT rowid AS own_rowid, * FROM strategy_position_ownership ORDER BY opened_at ASC, rowid ASC"
        ).fetchall()
    ]
    if not ownership_rows:
        return {}

    by_symbol_own: dict[str, list[dict[str, Any]]] = {}
    for row in ownership_rows:
        by_symbol_own.setdefault(str(row["symbol"]).upper(), []).append(row)

    trades_by_sid: dict[str, list[dict[str, Any]]] = {}

    for symbol, owns in by_symbol_own.items():
        entry_orders = [
            dict(row)
            for row in conn.execute(
                "SELECT rowid AS order_rowid, client_order_id, cycle_id, created_at "
                "FROM orders WHERE symbol=? AND purpose='ENTRY' ORDER BY rowid ASC",
                (symbol,),
            ).fetchall()
        ]
        entry_by_cycle = {
            str(row["cycle_id"]): row for row in entry_orders if row.get("cycle_id")
        }
        owned_cycles = {str(o["cycle_id"]) for o in owns}

        events: list[dict[str, Any]] = []
        for o in owns:
            cyc = str(o["cycle_id"])
            ord_row = entry_by_cycle.get(cyc)
            events.append(
                {
                    "strategy_id": str(o["strategy_id"]),
                    "version": int(o["version"]),
                    "cycle_id": cyc,
                    "opened_at": str(o["opened_at"] or ""),
                    "opened_ms": _iso_to_ms(o["opened_at"]) or 0,
                    "order_rowid": int(ord_row["order_rowid"]) if ord_row is not None else None,
                    "own_rowid": int(o["own_rowid"]),
                }
            )
        for ord_row in entry_orders:
            cyc = str(ord_row.get("cycle_id") or "")
            if cyc and cyc in owned_cycles:
                continue
            events.append(
                {
                    "strategy_id": None,
                    "version": None,
                    "cycle_id": cyc,
                    "opened_at": str(ord_row.get("created_at") or ""),
                    "opened_ms": _iso_to_ms(ord_row.get("created_at")) or 0,
                    "order_rowid": int(ord_row["order_rowid"]),
                    "own_rowid": 10**12 + int(ord_row["order_rowid"]),
                }
            )

        if all(ev["order_rowid"] is not None for ev in events):
            events.sort(key=lambda ev: int(ev["order_rowid"]))
        else:
            events.sort(key=lambda ev: (int(ev["opened_ms"]), int(ev["order_rowid"] or 0), int(ev["own_rowid"])))

        exit_fills = [
            dict(row)
            for row in conn.execute(
                "SELECT o.rowid AS exit_order_rowid, f.rowid AS fill_rowid, "
                "f.client_order_id, f.realized_pnl, f.commission, f.commission_asset, f.trade_time_ms "
                "FROM fills f JOIN orders o ON o.client_order_id = f.client_order_id "
                "WHERE o.symbol=? AND o.purpose='EXIT' "
                "ORDER BY o.rowid ASC, f.trade_time_ms ASC, f.rowid ASC",
                (symbol,),
            ).fetchall()
        ]
        if not exit_fills:
            continue

        for idx, ev in enumerate(events):
            sid = ev["strategy_id"]
            if not sid:
                continue
            nxt = events[idx + 1] if idx + 1 < len(events) else None
            cur_ord_id = ev["order_rowid"]
            nxt_ord_id = nxt["order_rowid"] if nxt is not None else None

            matched_fills: list[dict[str, Any]] = []
            for fill in exit_fills:
                exit_ord_id = int(fill["exit_order_rowid"])
                fill_ms = int(fill.get("trade_time_ms") or 0)
                if cur_ord_id is not None and (nxt is None or nxt_ord_id is not None):
                    if exit_ord_id <= cur_ord_id:
                        continue
                    if nxt_ord_id is not None and exit_ord_id >= nxt_ord_id:
                        continue
                    matched_fills.append(fill)
                else:
                    if fill_ms < int(ev["opened_ms"]):
                        continue
                    if nxt is not None and fill_ms >= int(nxt["opened_ms"]):
                        continue
                    matched_fills.append(fill)

            if not matched_fills:
                continue

            pnl = sum(
                (to_decimal(f.get("realized_pnl"), Decimal("0")) or Decimal("0") for f in matched_fills),
                Decimal("0"),
            )
            close_ms = max(int(f.get("trade_time_ms") or 0) for f in matched_fills)
            trades_by_sid.setdefault(sid, []).append(
                {
                    "strategy_id": sid,
                    "version": ev["version"],
                    "cycle_id": ev["cycle_id"],
                    "symbol": symbol,
                    "opened_at": ev["opened_at"],
                    "opened_ms": ev["opened_ms"],
                    "close_time_ms": close_ms,
                    "pnl": pnl,
                }
            )

    for sid_trades in trades_by_sid.values():
        sid_trades.sort(key=lambda t: (int(t["close_time_ms"]), str(t["opened_at"])))
    return trades_by_sid


def _evaluate_veto_counterfactuals(
    conn: sqlite3.Connection,
    strategy_id: str,
    veto_rows: list[dict[str, Any]],
    market_service: Any,
) -> tuple[int, int, int, str]:
    if not veto_rows:
        return 0, 0, 0, "verified" if market_service is not None else "unverified"
    if market_service is None:
        return 0, 0, len(veto_rows), "unverified"

    version_rules_cache: dict[int, dict[str, Any]] = {}

    def _rules_for(ver: int) -> dict[str, Any]:
        if ver not in version_rules_cache:
            row = conn.execute(
                "SELECT payload_json FROM strategy_versions WHERE strategy_id=? AND version=?",
                (strategy_id, ver),
            ).fetchone()
            if row:
                try:
                    version_rules_cache[ver] = json.loads(row["payload_json"]).get("rules") or {}
                except Exception:  # noqa: BLE001
                    version_rules_cache[ver] = {}
            else:
                version_rules_cache[ver] = {}
        return version_rules_cache[ver]

    saved_sl = 0
    missed_tp = 0
    pending = 0

    for veto in veto_rows:
        symbol = str(veto.get("symbol") or "").upper()
        side = str(veto.get("side") or "").upper()
        ver = int(veto.get("version") or 1)
        rules = _rules_for(ver)
        frames = rules.get("timeframes") or ["15m"]
        timeframe = str(frames[0]) if isinstance(frames, list) and frames else "15m"

        entry_mark = to_decimal(veto.get("entry_mark"))
        sl_price = to_decimal(veto.get("sl_price"))
        tp_price = to_decimal(veto.get("tp_price"))
        exit_cfg = rules.get("exit") if isinstance(rules.get("exit"), dict) else {}
        if entry_mark is not None and entry_mark > 0:
            direction = Decimal(1) if side == "LONG" else Decimal(-1)
            if sl_price is None and exit_cfg.get("stop_loss_pct") is not None:
                sl_price = entry_mark * (Decimal(1) - direction * Decimal(str(exit_cfg["stop_loss_pct"])))
            if tp_price is None and exit_cfg.get("take_profit_pct") is not None:
                tp_price = entry_mark * (Decimal(1) + direction * Decimal(str(exit_cfg["take_profit_pct"])))

        if sl_price is None or tp_price is None or side not in ("LONG", "SHORT"):
            pending += 1
            continue

        details: dict[str, Any] = {}
        try:
            details = json.loads(veto.get("details_json") or "{}")
        except Exception:  # noqa: BLE001
            details = {}
        veto_ms = details.get("evaluated_at_ms") or details.get("veto_at_ms")
        if veto_ms is not None:
            try:
                veto_ms = int(veto_ms)
            except (TypeError, ValueError):
                veto_ms = None
        if veto_ms is None:
            veto_ms = _iso_to_ms(veto.get("created_at")) or 0

        klines_store = getattr(market_service, "klines", None)
        bars = ()
        if klines_store is not None:
            bars = klines_store.cached_bars(symbol, timeframe)
            if not bars and timeframe != "15m":
                bars = klines_store.cached_bars(symbol, "15m")

        after_bars = [b for b in bars if int(getattr(b, "close_time_ms", 0)) >= int(veto_ms)]
        if not after_bars:
            pending += 1
            continue

        outcome = "pending"
        for bar in after_bars:
            low = to_decimal(getattr(bar, "low", None))
            high = to_decimal(getattr(bar, "high", None))
            if low is None or high is None:
                continue
            if side == "LONG":
                hit_sl = low <= sl_price
                hit_tp = high >= tp_price
            else:
                hit_sl = high >= sl_price
                hit_tp = low <= tp_price
            if hit_sl:
                outcome = "saved_sl"
                break
            if hit_tp:
                outcome = "missed_tp"
                break

        if outcome == "saved_sl":
            saved_sl += 1
        elif outcome == "missed_tp":
            missed_tp += 1
        else:
            pending += 1

    return saved_sl, missed_tp, pending, "verified"


def compute_strategy_attribution(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    strategy_row: dict[str, Any] | None = None,
    market_service: Any = None,
    preloaded_trades: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    row = strategy_row
    if row is None:
        fetched = conn.execute(
            "SELECT * FROM strategies WHERE strategy_id=?", (strategy_id,)
        ).fetchone()
        row = dict(fetched) if fetched else {}

    sig_rows = (
        [
            dict(r)
            for r in conn.execute(
                "SELECT cycle_id, state FROM strategy_signals WHERE strategy_id=?",
                (strategy_id,),
            ).fetchall()
        ]
        if _table_exists(conn, "strategy_signals")
        else []
    )
    dec_rows = (
        [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM strategy_decisions WHERE strategy_id=? ORDER BY created_at ASC, rowid ASC",
                (strategy_id,),
            ).fetchall()
        ]
        if _table_exists(conn, "strategy_decisions")
        else []
    )

    executed_cycles = {
        str(r["cycle_id"]) for r in sig_rows if str(r.get("state") or "").lower() != "rejected"
    } | {
        str(r["cycle_id"]) for r in dec_rows if str(r.get("outcome") or "").lower() == "executed"
    }
    rejected_cycles = (
        {
            str(r["cycle_id"]) for r in sig_rows if str(r.get("state") or "").lower() == "rejected"
        }
        | {
            str(r["cycle_id"])
            for r in dec_rows
            if str(r.get("outcome") or "").lower() in ("rejected", "risk_rejected")
        }
    ) - executed_cycles
    veto_rows = [r for r in dec_rows if str(r.get("outcome") or "").lower() == "ai_veto"]
    veto_cycles = {str(r["cycle_id"]) for r in veto_rows}
    all_cycles = (
        executed_cycles
        | rejected_cycles
        | veto_cycles
        | {str(r["cycle_id"]) for r in dec_rows if r.get("cycle_id")}
    )

    saved_sl, missed_tp, veto_pending, veto_status = _evaluate_veto_counterfactuals(
        conn, strategy_id, veto_rows, market_service
    )

    trades_map = preloaded_trades if preloaded_trades is not None else _closed_trades_by_strategy(conn)
    trades = trades_map.get(strategy_id, [])
    closed_trades = len(trades)
    winning_trades = sum(1 for t in trades if t["pnl"] > 0)
    losing_trades = sum(1 for t in trades if t["pnl"] < 0)
    win_rate = round(winning_trades / closed_trades, 4) if closed_trades > 0 else None
    total_pnl = sum((t["pnl"] for t in trades), Decimal("0"))

    reset_at = row.get("circuit_reset_at")
    if not reset_at:
        post_reset_trades = trades
    else:
        reset_ms = _iso_to_ms(reset_at)
        post_reset_trades = [
            t
            for t in trades
            if (t.get("opened_at") and str(t["opened_at"]) > str(reset_at))
            or (reset_ms is not None and int(t["close_time_ms"]) > reset_ms)
        ]

    consecutive_losses = 0
    for t in reversed(post_reset_trades):
        if t["pnl"] < 0:
            consecutive_losses += 1
        else:
            break

    pnl_since_reset = sum((t["pnl"] for t in post_reset_trades), Decimal("0"))
    cum_loss_since_reset = max(Decimal("0"), -pnl_since_reset)

    return {
        "signals_count": len(all_cycles),
        "executed_count": len(executed_cycles),
        "rejected_count": len(rejected_cycles),
        "ai_veto_count": len(veto_rows),
        "veto_saved_sl": saved_sl,
        "veto_missed_tp": missed_tp,
        "veto_pending": veto_pending,
        "veto_status": veto_status,
        "closed_trades": closed_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": win_rate,
        "realized_pnl_usdt": dec_text(total_pnl),
        "consecutive_losses": consecutive_losses,
        "realized_pnl_since_reset_usdt": dec_text(pnl_since_reset),
        "cumulative_loss_since_reset_usdt": dec_text(cum_loss_since_reset),
    }


def list_attribution(store: "StrategyStore", *, market_service: Any = None) -> list[dict[str, Any]]:
    check_and_trip_circuits(store)
    with store.transaction() as conn:
        trades_map = _closed_trades_by_strategy(conn)
        rows = conn.execute(
            "SELECT strategy_id FROM strategies ORDER BY created_at, strategy_id"
        ).fetchall()
        results: list[dict[str, Any]] = []
        for item in rows:
            sid = str(item["strategy_id"])
            strategy = store._get(conn, sid, preloaded_trades=trades_map)
            stats = compute_strategy_attribution(
                conn,
                sid,
                strategy_row=strategy,
                market_service=market_service,
                preloaded_trades=trades_map,
            )
            results.append(
                {
                    "strategy_id": sid,
                    "name": strategy["name"],
                    "status": strategy["status"],
                    "enabled": strategy["enabled"],
                    "current_version": strategy["current_version"],
                    "generation": strategy["generation"],
                    "execution_mode": strategy.get("execution_mode", "ai_assisted"),
                    "circuit": strategy["circuit"],
                    **stats,
                }
            )
        return results


def check_and_trip_circuits(store: "StrategyStore") -> list[dict[str, Any]]:
    candidates_to_trip: list[tuple[str, str, dict[str, Any]]] = []
    with store.transaction() as conn:
        enabled_rows = conn.execute(
            "SELECT * FROM strategies WHERE enabled=1 ORDER BY created_at, strategy_id"
        ).fetchall()
        if not enabled_rows:
            return []
        trades_map = _closed_trades_by_strategy(conn)
        for raw in enabled_rows:
            row = dict(raw)
            sid = str(row["strategy_id"])
            ver = row.get("current_version")
            if not ver:
                continue
            v_row = conn.execute(
                "SELECT payload_json FROM strategy_versions WHERE strategy_id=? AND version=?",
                (sid, ver),
            ).fetchone()
            if not v_row:
                continue
            rules = (json.loads(v_row["payload_json"]).get("rules") or {})
            cb = rules.get("circuit_breaker")
            if not isinstance(cb, dict) or not cb:
                continue
            max_consec = int(cb.get("max_consecutive_losses") or 0)
            max_cum_loss = to_decimal(cb.get("max_cumulative_loss_usdt"), Decimal("0")) or Decimal("0")
            if max_consec <= 0 and max_cum_loss <= 0:
                continue

            stats = compute_strategy_attribution(
                conn, sid, strategy_row=row, preloaded_trades=trades_map
            )
            consec = int(stats["consecutive_losses"])
            cum_loss = to_decimal(stats["cumulative_loss_since_reset_usdt"], Decimal("0")) or Decimal("0")

            if max_consec > 0 and consec >= max_consec:
                candidates_to_trip.append(
                    (
                        sid,
                        f"连续亏损 {consec} 笔达到熔断上限 {max_consec} 笔",
                        {
                            "trigger": "max_consecutive_losses",
                            "consecutive_losses": consec,
                            "limit": max_consec,
                        },
                    )
                )
            elif max_cum_loss > 0 and cum_loss >= max_cum_loss:
                candidates_to_trip.append(
                    (
                        sid,
                        f"累计亏损 {dec_text(cum_loss)} USDT 达到熔断上限 {dec_text(max_cum_loss)} USDT",
                        {
                            "trigger": "max_cumulative_loss_usdt",
                            "cumulative_loss_usdt": dec_text(cum_loss),
                            "limit": dec_text(max_cum_loss),
                        },
                    )
                )

    tripped: list[dict[str, Any]] = []
    for sid, reason, details in candidates_to_trip:
        tripped.append(store.trip_circuit(sid, reason, details))
    return tripped
