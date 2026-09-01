"""Replay：从审计库回放任意一轮/一段时间窗口的完整决策链路。

一条回放记录回答：这一轮发生了什么 ——
  情报 → 双通道决策 → 冲突 → 风控裁决 → 执行结果 → 平仓归因 → 异常事件
用于事后复盘"模型当时为什么这么干"，以及排查静默失败。
"""
from __future__ import annotations

from typing import Any

from audit.database import AuditDatabase


def _parse_json(value: str | None) -> dict[str, Any]:
    try:
        import json
        return json.loads(value or "{}")
    except Exception:
        return {}


def replay_run(db: AuditDatabase, decision_id: str) -> dict[str, Any]:
    """回放单轮（按 decision_id）。"""
    decision = db.fetch_one(
        "SELECT * FROM trade_decisions WHERE decision_id=?", (decision_id,))
    if decision is None:
        return {"decision_id": decision_id, "found": False}
    d = dict(decision)
    intents = [dict(r) for r in db.fetch_all(
        "SELECT * FROM trade_intents WHERE decision_id=? ORDER BY id", (decision_id,))]
    verdict = db.fetch_one(
        "SELECT * FROM risk_verdicts WHERE decision_id=? ORDER BY id DESC LIMIT 1", (decision_id,))
    conflicts = [dict(r) for r in db.fetch_all(
        "SELECT * FROM decision_conflicts WHERE decision_id=? ORDER BY id", (decision_id,))]
    downgrades = [dict(r) for r in db.fetch_all(
        "SELECT * FROM risk_events WHERE event_type='decision_downgraded' AND details LIKE ?",
        (f"%{decision_id}%",))]
    run_id = d.get("run_id")
    orders = []
    outcomes = []
    for it in intents:
        cid = it.get("client_order_id")
        if cid:
            orders.extend(dict(r) for r in db.fetch_all(
                "SELECT * FROM orders WHERE client_order_id=?", (cid,)))
    if run_id:
        outcomes = [dict(r) for r in db.fetch_all(
            "SELECT * FROM trade_outcomes WHERE decision_id=? OR intent_id IN "
            "(SELECT intent_id FROM trade_intents WHERE decision_id=?)",
            (decision_id, decision_id))]
    return {
        "decision_id": decision_id,
        "found": True,
        "run_id": run_id,
        "created_at": d.get("created_at"),
        "decision": d.get("decision"),
        "channel": d.get("channel"),
        "reasoning": d.get("reasoning"),
        "intents": [
            {k: it.get(k) for k in ("symbol", "action", "side", "qty", "leverage", "status",
                                    "blocked_reason", "invalidation", "client_order_id")}
            for it in intents
        ],
        "risk_verdict": {
            "verdict": dict(verdict).get("verdict") if verdict else None,
            "reasons": dict(verdict).get("reasons") if verdict else None,
        } if verdict else None,
        "conflicts": [{"symbol": c.get("symbol"), "source": c.get("source"),
                       "reason": c.get("reason")} for c in conflicts],
        "downgrade_events": [{"details": e.get("details")} for e in downgrades],
        "orders": [{"symbol": o.get("symbol"), "action": _parse_json(o.get("raw_json")).get("action"),
                    "qty": o.get("qty"), "price": o.get("price"), "pnl": _parse_json(o.get("raw_json")).get("pnl"),
                    "status": o.get("status")} for o in orders],
        "outcomes": [{"symbol": o.get("symbol"), "pnl": o.get("pnl"), "pnl_pct": o.get("pnl_pct"),
                      "invalidation": o.get("invalidation"), "exit_reason": o.get("exit_reason"),
                      "holding_minutes": o.get("holding_minutes")} for o in outcomes],
    }


def replay_window(db: AuditDatabase, limit: int = 30,
                  start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
    """回放最近 N 轮（按时间倒序）。"""
    sql = "SELECT decision_id FROM trade_decisions"
    params: tuple = ()
    conds = []
    if start:
        conds.append("created_at >= ?")
        params += (start,)
    if end:
        conds.append("created_at <= ?")
        params += (end,)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params += (limit,)
    rows = db.fetch_all(sql, params)
    return [replay_run(db, r["decision_id"]) for r in rows]


def replay_summary(replay: dict[str, Any]) -> str:
    """把单轮回放压成几行文本，适合复盘报告或日志。"""
    if not replay.get("found"):
        return f"[replay] 未找到 {replay.get('decision_id')}"
    lines = [
        f"[{replay['created_at'][:19] if replay.get('created_at') else ''}] "
        f"{replay['decision_id']} channel={replay.get('channel')} decision={replay.get('decision')}"
    ]
    for it in replay.get("intents", []):
        mark = "✓" if it.get("status") == "approved" else "✗"
        block = f" [{it.get('blocked_reason')}]" if it.get("blocked_reason") else ""
        inv = f" 证伪条件:{it.get('invalidation')}" if it.get("invalidation") else ""
        lines.append(f"  {mark} {it.get('symbol')} {it.get('action')} {it.get('side')} "
                     f"qty={it.get('qty')}{block}{inv}")
    if replay.get("conflicts"):
        lines.append(f"  冲突: {len(replay['conflicts'])} 条")
    if replay.get("downgrade_events"):
        lines.append(f"  降级事件: {len(replay['downgrade_events'])} 条")
    verdict = replay.get("risk_verdict") or {}
    if verdict.get("verdict"):
        lines.append(f"  风控: {verdict['verdict']}")
    for o in replay.get("outcomes", []):
        lines.append(f"  平仓: {o.get('symbol')} pnl={o.get('pnl')} ({o.get('pnl_pct')}%) "
                     f"exit={o.get('exit_reason')} inv={o.get('invalidation')}")
    return "\n".join(lines)
