"""Deterministic mounted-strategy checks at the existing P1/P2 boundary."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .attribution import check_and_trip_circuits
from .evaluator import (
    PERIOD_MS,
    entry_uses_funding,
    entry_uses_klines,
    evaluate_entry,
    recommended_fetch_bars,
)
from .validation import canonical, fail, integer, text


def _precise_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_entry(
    store,
    context,
    plan,
    market,
    *,
    now_ms,
    compile_plan: bool = False,
    expected_mode_revision: int | None = None,
    expected_signal_key: str | None = None,
    market_service: Any = None,
    bars: Any = None,
):
    config = store.config()
    if not compile_plan and expected_mode_revision is None and config["revision"] != 0:
        fail("E_STRATEGY_MODE_CHANGED", "旧批准缺少运行模式版本，请重新评估", 409)
    if expected_mode_revision is not None and config["revision"] != expected_mode_revision:
        fail("E_STRATEGY_MODE_CHANGED", "运行模式已变化，旧批准失效", 409)
    if context is None:
        if config["mode"] != "native":
            fail("E_STRATEGY_CONTEXT_REQUIRED", "挂载模式必须携带已授权策略", 409)
        return plan, None
    if not isinstance(context, dict) or set(context) != {
        "strategy_id",
        "version",
        "generation",
        "mode_revision",
        "signal_key",
    }:
        fail("E_STRATEGY_VALIDATION", "strategy_context 字段不完整或包含额外字段")

    sid = text(context["strategy_id"], "strategy_id", 128)
    version = integer(context["version"], "version", 1)
    generation = integer(context["generation"], "generation")
    signal_key = text(context["signal_key"], "signal_key", 128)
    if config["mode"] != "mounted" or integer(context["mode_revision"], "mode_revision") != config["revision"]:
        fail("E_STRATEGY_MODE_CHANGED", "策略模式授权已变化", 409)

    check_and_trip_circuits(store)
    authorized = store.validate_authorization(sid, version, generation)
    rules = authorized["rules"]
    entry = rules["entry"]
    if plan.symbol not in rules["symbols"] or plan.side.lower() != entry["side"]:
        fail("E_STRATEGY_PLAN_MISMATCH", "标的或方向与已确认策略不一致")

    mark_raw = (market or {}).get("mark")
    mark = Decimal(str(mark_raw or 0))
    if not mark.is_finite() or mark <= 0:
        fail("E_STRATEGY_MARK_UNAVAILABLE", "缺少有效标记价")

    timeframe = rules["timeframes"][0]
    mark_quote = dict(market or {})
    if entry_uses_funding(entry) and mark_quote.get("funding_rate") is None and market_service is not None:
        try:
            snap = market_service.mark_snapshot([plan.symbol]).get("prices", {}).get(plan.symbol) or {}
            if snap.get("funding_rate") is not None:
                mark_quote["funding_rate"] = snap["funding_rate"]
        except Exception:  # noqa: BLE001
            pass

    resolved_bars = bars if bars is not None else mark_quote.get("bars")
    if resolved_bars is None and entry_uses_klines(entry) and market_service is not None:
        try:
            resolved_bars = market_service.klines.get_with_fallback(
                plan.symbol,
                timeframe,
                recommended_fetch_bars(entry, timeframe),
                now_ms=int(now_ms),
            ).bars
        except Exception:  # noqa: BLE001
            klines_store = getattr(market_service, "klines", None)
            if klines_store is not None:
                resolved_bars = klines_store.cached_bars(plan.symbol, timeframe)

    eval_res = evaluate_entry(
        rules,
        plan.symbol,
        mark_quote,
        bars=resolved_bars,
        now_ms=int(now_ms),
    )
    if not eval_res["matched"]:
        rc = eval_res["reason_code"]
        if rc == "QUOTE_STALE":
            fail("E_STRATEGY_MARK_UNAVAILABLE", eval_res["detail"] or "缺少有效标记价", 409)
        if rc in ("KLINE_STALE", "INSUFFICIENT_BARS", "FUNDING_UNAVAILABLE"):
            fail("E_STRATEGY_DATA_UNAVAILABLE", eval_res["detail"] or "策略所需行情数据不可用", 409)
        fail("E_STRATEGY_SIGNAL_FALSE", eval_res["detail"] or "当前价格未满足入场条件", 409)

    period_ms = PERIOD_MS[timeframe]
    if compile_plan or expected_signal_key is None:
        now_int = int(now_ms)
        shifted = max(0, now_int - 400_000)
        valid_keys = {
            f"{timeframe}:{now_int // period_ms}",
            f"{timeframe}:{shifted // period_ms}",
        }
        if entry_uses_klines(entry):
            valid_keys.add(eval_res["signal_key"])
            prev_closed = (shifted // period_ms) - 1
            if prev_closed >= 0:
                valid_keys.add(f"{timeframe}:{prev_closed}")
        if signal_key not in valid_keys:
            fail("E_STRATEGY_SIGNAL_EXPIRED", "信号周期已过期或标识无效", 409)
    else:
        if not signal_key or signal_key != str(expected_signal_key):
            fail("E_STRATEGY_PLAN_MISMATCH", "P2 信号标识与批准不一致", 409)

    if compile_plan:
        with store.transaction() as conn:
            if store.is_signal_consumed(conn, sid, version, plan.symbol, plan.side, signal_key):
                fail("E_STRATEGY_SIGNAL_DUPLICATE", "该策略信号或轮次已提交，请查询原订单", 409)
        exit_ = rules["exit"]
        direction = Decimal(1) if plan.side == "LONG" else Decimal(-1)
        sl = mark * (1 - direction * Decimal(str(exit_["stop_loss_pct"])))
        tp = mark * (1 + direction * Decimal(str(exit_["take_profit_pct"])))
        ai_limits = dict(plan.ai_limits)
        ai_limits["time_stop_hours"] = exit_.get("time_stop_hours", 0)
        plan = replace(plan, sl=sl, tp=tp, ai_limits=ai_limits)
    return plan, {**context}


def reserve_signal(store, context, plan, *, time_stop=None):
    """Durably reserve before dispatch; uncertain orders never auto-retry."""
    if context is None:
        return
    from ..db import utc_now_iso

    with store.transaction(write=True) as conn:
        existing = conn.execute(
            "SELECT cycle_id, state FROM strategy_signals "
            "WHERE (strategy_id=? AND version=? AND symbol=? AND side=? AND signal_key=?) OR cycle_id=?",
            (
                context["strategy_id"],
                context["version"],
                plan.symbol,
                plan.side,
                context["signal_key"],
                plan.cycle_id,
            ),
        ).fetchone()
        if existing:
            if existing["cycle_id"] == plan.cycle_id or str(existing["state"] or "").lower() != "rejected":
                fail("E_STRATEGY_SIGNAL_DUPLICATE", "该策略信号或轮次已提交，请查询原订单", 409)
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='positions'"
        ).fetchone():
            if any(Decimal(str(row["qty"])) != 0 for row in conn.execute("SELECT qty FROM positions")):
                fail("E_STRATEGY_POSITION_LIMIT", "已有仓位，不能新增策略开仓", 409)
        now = utc_now_iso()
        ctx_json = canonical({**context, "time_stop": time_stop})
        if existing:
            conn.execute(
                "UPDATE strategy_signals SET cycle_id=?,state=?,context_json=?,result_json=NULL,created_at=? "
                "WHERE strategy_id=? AND version=? AND symbol=? AND side=? AND signal_key=?",
                (
                    plan.cycle_id,
                    "dispatched",
                    ctx_json,
                    now,
                    context["strategy_id"],
                    context["version"],
                    plan.symbol,
                    plan.side,
                    context["signal_key"],
                ),
            )
        else:
            conn.execute(
                "INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    context["strategy_id"],
                    context["version"],
                    plan.symbol,
                    plan.side,
                    context["signal_key"],
                    plan.cycle_id,
                    "dispatched",
                    ctx_json,
                    None,
                    now,
                ),
            )


def record_result(store, context, plan, result):
    if context is None:
        return
    with store.transaction(write=True) as conn:
        conn.execute(
            "UPDATE strategy_signals SET state=?,result_json=? WHERE cycle_id=?",
            (
                str(result.get("status") or "unknown"),
                canonical(
                    {
                        "client_order_id": result.get("client_order_id"),
                        "status": result.get("status"),
                    }
                ),
                plan.cycle_id,
            ),
        )
        if conn.execute("SELECT 1 FROM positions WHERE symbol=?", (plan.symbol,)).fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO strategy_position_ownership VALUES (?,?,?,?,?,?,?)",
                (
                    plan.symbol,
                    plan.side,
                    plan.cycle_id,
                    context["strategy_id"],
                    context["version"],
                    context["generation"],
                    _precise_now_iso(),
                ),
            )


def record_rejection(store, context, plan, error_code: Any, error_message: str = "") -> None:
    """Record definite exchange/pre-dispatch rejections accurately in strategy_signals & decisions."""
    if context is None:
        return
    code_str = str(error_code or "E_REJECTED")
    msg_str = str(error_message or "")[:500]
    with store.transaction(write=True) as conn:
        conn.execute(
            "UPDATE strategy_signals SET state=?,result_json=? WHERE cycle_id=?",
            (
                "rejected",
                canonical({"status": "rejected", "error_code": code_str, "message": msg_str}),
                plan.cycle_id,
            ),
        )
        v_row = conn.execute(
            "SELECT payload_json FROM strategy_versions WHERE strategy_id=? AND version=?",
            (context["strategy_id"], int(context["version"])),
        ).fetchone()
        exec_mode = "ai_assisted"
        if v_row:
            import json

            rules = json.loads(v_row["payload_json"]).get("rules") or {}
            if rules.get("execution_mode") in ("ai_assisted", "rule_direct"):
                exec_mode = rules["execution_mode"]
        store._record_decision_in_tx(
            conn,
            context["strategy_id"],
            {
                "cycle_id": str(plan.cycle_id or ""),
                "version": int(context["version"]),
                "symbol": plan.symbol,
                "side": plan.side,
                "signal_key": str(context["signal_key"]),
                "execution_mode": exec_mode,
                "outcome": "rejected",
                "reason_code": code_str,
                "sl_price": str(plan.sl) if plan.sl else None,
                "tp_price": str(plan.tp) if plan.tp else None,
                "details": {"message": msg_str},
            },
        )


def restore_position_metadata(store):
    """Restore only proven workbench positions; never adopt an external position."""
    import json
    from ..db import utc_now_iso

    with store.transaction(write=True) as conn:
        positions = conn.execute("SELECT * FROM positions WHERE origin='OPENED_BY_WORKBENCH'").fetchall()
        for position in positions:
            if Decimal(str(position["qty"])) == 0:
                continue
            order = conn.execute(
                "SELECT * FROM orders WHERE symbol=? AND purpose='ENTRY' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (position["symbol"],),
            ).fetchone()
            if not order or Decimal(str(order["qty_filled"])) <= 0:
                continue
            signal = conn.execute(
                "SELECT * FROM strategy_signals WHERE cycle_id=? AND symbol=?",
                (order["cycle_id"], position["symbol"]),
            ).fetchone()
            if not signal:
                continue
            side = signal["side"]
            if order["side"] != ("BUY" if side == "LONG" else "SELL"):
                continue
            if position["position_side"] != "BOTH" and position["position_side"] != side:
                continue
            if position["position_side"] == "BOTH" and (Decimal(str(position["qty"])) > 0) != (
                side == "LONG"
            ):
                continue
            context = json.loads(signal["context_json"])
            conn.execute(
                "INSERT OR IGNORE INTO strategy_position_ownership VALUES (?,?,?,?,?,?,?)",
                (
                    position["symbol"],
                    side,
                    signal["cycle_id"],
                    signal["strategy_id"],
                    signal["version"],
                    context["generation"],
                    signal["created_at"],
                ),
            )
            stop = context.get("time_stop")
            if stop and not position["time_stop_source"] and not position["deadline_at"]:
                conn.execute(
                    "UPDATE positions SET opened_at=?,deadline_at=?,time_stop_source=?,updated_at=? WHERE ledger_id=? AND symbol=? AND position_side=?",
                    (
                        stop["opened_at"],
                        stop["deadline_at"],
                        stop["source"],
                        utc_now_iso(),
                        position["ledger_id"],
                        position["symbol"],
                        position["position_side"],
                    ),
                )
