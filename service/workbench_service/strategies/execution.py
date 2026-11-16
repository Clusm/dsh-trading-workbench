"""Deterministic mounted-strategy checks at the existing P1/P2 boundary."""
from dataclasses import replace
from decimal import Decimal

from .validation import canonical, fail, integer, text

PERIOD_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def validate_entry(store, context, plan, market, *, now_ms, compile_plan=False, expected_mode_revision=None):
    config = store.config()
    if not compile_plan and expected_mode_revision is None and config["revision"] != 0:
        fail("E_STRATEGY_MODE_CHANGED", "旧批准缺少运行模式版本，请重新评估", 409)
    if expected_mode_revision is not None and config["revision"] != expected_mode_revision:
        fail("E_STRATEGY_MODE_CHANGED", "运行模式已变化，旧批准失效", 409)
    if context is None:
        if config["mode"] != "native":
            fail("E_STRATEGY_CONTEXT_REQUIRED", "挂载模式必须携带已授权策略", 409)
        return plan, None
    if not isinstance(context, dict) or set(context) != {"strategy_id", "version", "generation", "mode_revision", "signal_key"}:
        fail("E_STRATEGY_VALIDATION", "strategy_context 字段不完整或包含额外字段")
    sid = text(context["strategy_id"], "strategy_id", 128)
    version, generation = integer(context["version"], "version", 1), integer(context["generation"], "generation")
    if config["mode"] != "mounted" or integer(context["mode_revision"], "mode_revision") != config["revision"]:
        fail("E_STRATEGY_MODE_CHANGED", "策略模式授权已变化", 409)
    authorized = store.validate_authorization(sid, version, generation)
    rules = authorized["rules"]
    entry = rules["entry"]
    if plan.symbol not in rules["symbols"] or plan.side.lower() != entry["side"]:
        fail("E_STRATEGY_PLAN_MISMATCH", "标的或方向与已确认策略不一致")
    mark = Decimal(str(market.get("mark") or 0))
    if not mark.is_finite() or mark <= 0:
        fail("E_STRATEGY_MARK_UNAVAILABLE", "缺少有效标记价")
    threshold = Decimal(str(entry["value"]))
    if not (mark > threshold if entry["operator"] == "gt" else mark < threshold):
        fail("E_STRATEGY_SIGNAL_FALSE", "当前价格未满足入场条件", 409)
    timeframe = rules["timeframes"][0]
    expected_key = f"{timeframe}:{int(now_ms) // PERIOD_MS[timeframe]}"
    if context["signal_key"] != expected_key:
        fail("E_STRATEGY_SIGNAL_EXPIRED", "信号周期已过期或标识无效", 409)
    if compile_plan:
        exit_ = rules["exit"]
        direction = Decimal(1) if plan.side == "LONG" else Decimal(-1)
        sl = mark * (1 - direction * Decimal(str(exit_["stop_loss_pct"])))
        tp = mark * (1 + direction * Decimal(str(exit_["take_profit_pct"])))
        ai_limits = dict(plan.ai_limits)
        ai_limits["time_stop_hours"] = exit_.get("time_stop_hours", 0)
        plan = replace(plan, sl=sl, tp=tp, ai_limits=ai_limits)
    return plan, {**context}


def reserve_signal(store, context, plan, *, time_stop=None):
    """Durably reserve before dispatch; failures/uncertainty never auto-retry it."""
    if context is None:
        return
    with store.transaction(write=True) as conn:
        existing = conn.execute("SELECT 1 FROM strategy_signals WHERE (strategy_id=? AND version=? AND symbol=? AND side=? AND signal_key=?) OR cycle_id=?", (context["strategy_id"], context["version"], plan.symbol, plan.side, context["signal_key"], plan.cycle_id)).fetchone()
        if existing:
            fail("E_STRATEGY_SIGNAL_DUPLICATE", "该策略信号或轮次已提交，请查询原订单", 409)
        # One account/round cannot open multiple strategies, and native/outside
        # positions are never silently adopted by a mounted strategy.
        if any(Decimal(str(row["qty"])) != 0 for row in conn.execute("SELECT qty FROM positions")):
            fail("E_STRATEGY_POSITION_LIMIT", "已有仓位，不能新增策略开仓", 409)
        from ..db import utc_now_iso
        conn.execute("INSERT INTO strategy_signals VALUES (?,?,?,?,?,?,?,?,?,?)", (context["strategy_id"], context["version"], plan.symbol, plan.side, context["signal_key"], plan.cycle_id, "dispatched", canonical({**context, "time_stop": time_stop}), None, utc_now_iso()))


def record_result(store, context, plan, result):
    if context is None:
        return
    from ..db import utc_now_iso
    with store.transaction(write=True) as conn:
        conn.execute("UPDATE strategy_signals SET state=?,result_json=? WHERE cycle_id=?", (str(result.get("status") or "unknown"), canonical({"client_order_id": result.get("client_order_id"), "status": result.get("status")}), plan.cycle_id))
        # Record the immutable opening version with the dispatched intent; only
        # execution-side ledger evidence may use it to establish live ownership.
        if conn.execute("SELECT 1 FROM positions WHERE symbol=?", (plan.symbol,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO strategy_position_ownership VALUES (?,?,?,?,?,?,?)", (plan.symbol, plan.side, plan.cycle_id, context["strategy_id"], context["version"], context["generation"], utc_now_iso()))


def restore_position_metadata(store):
    """Restore only proven workbench positions; never adopt an external position.

    The latest entry order must identify the reserved strategy cycle and have
    confirmed fills. The original durable deadline is reused, never restarted.
    """
    import json
    from ..db import utc_now_iso
    with store.transaction(write=True) as conn:
        positions = conn.execute("SELECT * FROM positions WHERE origin='OPENED_BY_WORKBENCH'").fetchall()
        for position in positions:
            if Decimal(str(position["qty"])) == 0:
                continue
            order = conn.execute("SELECT * FROM orders WHERE symbol=? AND purpose='ENTRY' ORDER BY created_at DESC, rowid DESC LIMIT 1", (position["symbol"],)).fetchone()
            if not order or Decimal(str(order["qty_filled"])) <= 0:
                continue
            signal = conn.execute("SELECT * FROM strategy_signals WHERE cycle_id=? AND symbol=?", (order["cycle_id"], position["symbol"])).fetchone()
            if not signal:
                continue
            side = signal["side"]
            if order["side"] != ("BUY" if side == "LONG" else "SELL"):
                continue
            if position["position_side"] != "BOTH" and position["position_side"] != side:
                continue
            if position["position_side"] == "BOTH" and (Decimal(str(position["qty"])) > 0) != (side == "LONG"):
                continue
            context = json.loads(signal["context_json"])
            conn.execute("INSERT OR IGNORE INTO strategy_position_ownership VALUES (?,?,?,?,?,?,?)", (position["symbol"], side, signal["cycle_id"], signal["strategy_id"], signal["version"], context["generation"], signal["created_at"]))
            stop = context.get("time_stop")
            if stop and not position["time_stop_source"] and not position["deadline_at"]:
                conn.execute("UPDATE positions SET opened_at=?,deadline_at=?,time_stop_source=?,updated_at=? WHERE ledger_id=? AND symbol=? AND position_side=?", (stop["opened_at"], stop["deadline_at"], stop["source"], utc_now_iso(), position["ledger_id"], position["symbol"], position["position_side"]))
