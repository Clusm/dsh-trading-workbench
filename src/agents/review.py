"""复盘 Agent：按指定日期生成完整复盘报告。

默认复盘昨天（当前交易日可能还在运行）。
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from audit.database import AuditDatabase
from audit.jsonl_logger import JsonlLogger
from agents.calibration import compute_calibration, calibration_text
from agents.summarizer import summarize
from llm.base import LLMClient


def _date_range(review_date: str) -> tuple[str, str]:
    return f"{review_date}T00:00:00", f"{review_date}T23:59:59.999999"


def _q(db: AuditDatabase, sql: str, params: tuple = ()):
    return db.fetch_all(sql, params)


def _parse_raw(raw_json: str | None) -> dict:
    try:
        return json.loads(raw_json or "{}")
    except Exception:
        return {}


def _fmt(value: float | None, digits: int = 2) -> str:
    return f"{float(value or 0):,.{digits}f}"


def _build_trade_stats(db: AuditDatabase, start: str, end: str) -> dict:
    orders = _q(
        db,
        """SELECT id, created_at, symbol, side, qty, price, status, raw_json
           FROM orders WHERE created_at >= ? AND created_at <= ? ORDER BY id ASC""",
        (start, end),
    )
    open_n = close_n = fill_n = 0
    per_symbol: dict[str, dict] = {}
    loss_cases: list[dict] = []
    for r in orders:
        raw = _parse_raw(r["raw_json"])
        action = raw.get("action", "")
        if r["status"] == "FILLED":
            fill_n += 1
            if action == "open":
                open_n += 1
            elif action in ("close", "close_all", "reduce"):
                close_n += 1
                net = raw.get("net_pnl")
                if net is None:
                    net = raw.get("pnl")
                if net is not None:
                    symbol = r["symbol"]
                    item = per_symbol.setdefault(symbol, {
                        "net": 0.0, "wins": 0, "losses": 0, "closes": 0,
                    })
                    item["net"] += float(net)
                    item["closes"] += 1
                    if float(net) >= 0:
                        item["wins"] += 1
                    else:
                        item["losses"] += 1
                    if float(net) < -0.30:
                        loss_cases.append({
                            "ts": r["created_at"],
                            "symbol": symbol,
                            "side": r["side"],
                            "qty": r["qty"],
                            "price": r["price"],
                            "net": round(float(net), 4),
                            "commission": raw.get("commission"),
                        })

    realized = sum(v["net"] for v in per_symbol.values())
    wins = sum(v["wins"] for v in per_symbol.values())
    losses = sum(v["losses"] for v in per_symbol.values())
    return {
        "orders": orders,
        "open_n": open_n,
        "close_n": close_n,
        "fill_n": fill_n,
        "realized": realized,
        "wins": wins,
        "losses": losses,
        "per_symbol": per_symbol,
        "loss_cases": loss_cases,
    }


#: 只有这些事件才算「原论点被证伪」；invalidation 文本只是**预设条件**，
#: 它的存在本身不构成证伪（RV02：失效需触发事件）。
INVALIDATION_TRIGGER_EVENTS = ("guardian_invalidation",)
INVALIDATION_TRIGGER_REASON = "THESIS_INVALIDATED"


def inspect_invalidation_triggers(db: AuditDatabase, start: str, end: str) -> list[dict]:
    """读取区间内真实发生过的原点证伪事件（Guardian 收盘确认），供归因匹配。"""
    placeholders = ",".join("?" for _ in INVALIDATION_TRIGGER_EVENTS)
    rows = _q(
        db,
        f"""SELECT created_at, event_type, raw_json FROM risk_events
            WHERE event_type IN ({placeholders}) AND created_at >= ? AND created_at <= ?
            ORDER BY created_at ASC""",
        tuple(INVALIDATION_TRIGGER_EVENTS) + (start, end),
    )
    triggers: list[dict] = []
    for r in rows:
        payload = _parse_raw(r["raw_json"])
        reason = str(payload.get("reason_code") or "")
        state = str(payload.get("state") or "")
        if reason and reason != INVALIDATION_TRIGGER_REASON:
            continue
        if not reason and state != "INVALIDATED":
            continue
        triggers.append({
            "symbol": str(payload.get("symbol") or "").upper(),
            "at": str(r["created_at"]),
        })
    return triggers


def invalidation_triggered(triggers: list[dict], *, symbol: str,
                           opened_at: str | None, closed_at: str) -> bool:
    """是否存在落在此仓位生命周期内的证伪触发事件。"""
    symbol = str(symbol or "").upper()
    for item in triggers:
        if item["symbol"] and item["symbol"] != symbol:
            continue
        if opened_at and item["at"] < opened_at:
            continue
        if closed_at and item["at"] > closed_at:
            continue
        return True
    return False


def _build_outcome_stats(db: AuditDatabase, start: str, end: str) -> dict:
    """平仓归因：从 trade_outcomes 看盈亏 + thesis 证伪情况。

    * ``pnl IS NULL`` 的平仓结果盈亏未知：**既不算赢也不算输**，也不进入胜率分母
      （RV01：NULL 不计赢）。
    * ``invalidated_losses`` 只统计存在真实证伪触发事件的亏损单；仅有 invalidation
      文本（预设证伪条件）不算证伪（RV02）。
    """
    rows = _q(
        db,
        """SELECT symbol, side, source, pnl, pnl_pct, invalidation, exit_reason,
                  holding_minutes, expected_holding_minutes,
                  opened_at, closed_at
           FROM trade_outcomes WHERE closed_at >= ? AND closed_at <= ?""",
        (start, end),
    )
    outcome_symbols = {str(r["symbol"] or "").upper() for r in rows}
    triggers = inspect_invalidation_triggers(db, start, end)
    triggers = [t for t in triggers if not t["symbol"] or t["symbol"] in outcome_symbols]

    wins = losses = 0
    unknown_pnl = 0
    total = 0.0
    invalidated_losses = 0
    invalidation_text_only = 0
    over_hold_losses = 0
    for r in rows:
        if r["pnl"] is None:
            unknown_pnl += 1
            continue
        pnl = float(r["pnl"])
        total += pnl
        if pnl >= 0:
            wins += 1
            continue
        losses += 1
        triggered = invalidation_triggered(
            triggers, symbol=r["symbol"], opened_at=r["opened_at"],
            closed_at=str(r["closed_at"] or ""),
        )
        if triggered:
            invalidated_losses += 1
        elif r["invalidation"]:
            invalidation_text_only += 1
        hm = r["holding_minutes"]
        em = r["expected_holding_minutes"]
        if hm is not None and em is not None and hm > em * 2:
            over_hold_losses += 1
    decided = wins + losses
    return {
        "n": len(rows),
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "unknown_pnl": unknown_pnl,
        "total": total,
        "win_rate": (wins / decided) if decided else None,
        "invalidated_losses": invalidated_losses,
        "invalidation_text_only": invalidation_text_only,
        "over_hold_losses": over_hold_losses,
    }


def _build_calibration_stats(db: AuditDatabase) -> dict:
    """置信度校准（全周期样本；样本不足 3 时只有空表）。"""
    return compute_calibration(db, source="ai")


def run_review_agent(db: AuditDatabase, reviews_dir: Path,
                     review_date: str | None = None,
                     llm: LLMClient | None = None, model: str | None = None,
                     logger: JsonlLogger | None = None,
                     total_timeout_seconds: float = 120.0) -> Path:
    if review_date:
        date_obj = datetime.strptime(review_date, "%Y-%m-%d")
    else:
        # 默认复盘昨天，避免当天还没结束导致统计不完整
        date_obj = datetime.now(timezone.utc) - timedelta(days=1)
    review_date = date_obj.strftime("%Y-%m-%d")
    start, end = _date_range(review_date)
    deadline = time.monotonic() + max(0.0, float(total_timeout_seconds))
    reviews_dir = Path(reviews_dir)
    reviews_dir.mkdir(parents=True, exist_ok=True)

    agent_rows = _q(
        db,
        """SELECT agent_name, status, COUNT(*) n, COALESCE(AVG(latency_ms),0) ms
           FROM agent_runs WHERE started_at >= ? AND started_at <= ?
           GROUP BY agent_name, status ORDER BY agent_name, status""",
        (start, end),
    )
    llm_stats = db.fetch_one(
        """SELECT COUNT(*) n,
                  COALESCE(SUM(CASE WHEN status='ok' THEN prompt_tokens ELSE 0 END),0) pt,
                  COALESCE(SUM(CASE WHEN status='ok' THEN completion_tokens ELSE 0 END),0) ct,
                  SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) err
           FROM llm_calls WHERE started_at >= ? AND started_at <= ?""",
        (start, end),
    )
    decisions = _q(
        db,
        "SELECT decision, COUNT(*) n FROM trade_decisions WHERE created_at >= ? AND created_at <= ? GROUP BY decision",
        (start, end),
    )
    verdicts = _q(
        db,
        "SELECT verdict, COUNT(*) n FROM risk_verdicts WHERE created_at >= ? AND created_at <= ? GROUP BY verdict",
        (start, end),
    )
    risk_events = _q(
        db,
        "SELECT event_type, severity, COUNT(*) n FROM risk_events WHERE created_at >= ? AND created_at <= ? GROUP BY event_type, severity ORDER BY n DESC",
        (start, end),
    )
    trade = _build_trade_stats(db, start, end)
    outcome = _build_outcome_stats(db, start, end)
    calibration = _build_calibration_stats(db)

    lines = [
        f"# 每日复盘 {review_date}",
        "",
        f"生成时间(UTC): {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        "## 概览",
        "",
        f"- 成交订单: {trade['fill_n']}",
        f"- 开仓订单: {trade['open_n']}",
        f"- 平仓/减仓订单: {trade['close_n']}",
        f"- 已实现盈亏(订单口径): {trade['realized']:+.2f} U",
        f"- 盈利单: {trade['wins']}，亏损单: {trade['losses']}",
    ]

    lines += ["", "## 盈亏分布"]
    if trade["per_symbol"]:
        lines += ["| 交易对 | 净盈亏 | 平仓次数 | 盈利 | 亏损 |", "|---|---:|---:|---:|---:|"]
        for sym, v in sorted(trade["per_symbol"].items(), key=lambda x: x[1]["net"]):
            lines.append(f"| {sym} | {v['net']:+.2f} | {v['closes']} | {v['wins']} | {v['losses']} |")
    else:
        lines.append("无平仓记录。")

    lines += ["", "## 亏损案例"]
    if trade["loss_cases"]:
        lines += ["| 时间 | 交易对 | 方向 | 数量 | 平仓价 | 净亏损 |", "|---|---|---|---:|---:|---:|"]
        for c in sorted(trade["loss_cases"], key=lambda x: x["net"])[:15]:
            lines.append(
                f"| {c['ts'][:19].replace('T', ' ')} | {c['symbol']} | {c['side']} "
                f"| {c['qty']} | {c['price']} | {c['net']:.2f} |"
            )
    else:
        lines.append("无超过 0.3U 的亏损单。")

    lines += ["", "## 平仓归因（Outcome）"]
    if outcome["n"] > 0:
        if outcome["decided"]:
            lines.append(
                f"- 平仓 {outcome['n']} 笔（可判盈亏 {outcome['decided']} 笔）："
                f"盈利 {outcome['wins']} / 亏损 {outcome['losses']}"
                f"，胜率 {outcome['win_rate']:.0%}，净盈亏 {outcome['total']:+.2f} U"
            )
        else:
            lines.append(
                f"- 平仓 {outcome['n']} 笔，但全部盈亏未知（pnl 为 NULL），不给出胜率。"
            )
        if outcome["unknown_pnl"]:
            lines.append(f"- 盈亏未知（pnl 为 NULL，不计入胜率）：{outcome['unknown_pnl']} 笔")
        lines.append(f"- thesis 被证伪的亏损单：{outcome['invalidated_losses']} 笔"
                     f"（这类单是『行情判错』而不是『没按计划执行』；"
                     f"以真实证伪触发事件为准）")
        if outcome["invalidation_text_only"]:
            lines.append(
                f"- 仅写了证伪条件、但没有证伪触发事件的亏损单："
                f"{outcome['invalidation_text_only']} 笔（不计入「行情判错」）"
            )
        if outcome["over_hold_losses"]:
            lines.append(f"- 持有时间超过预期 2 倍以上的亏损单：{outcome['over_hold_losses']} 笔"
                         f"（该止损不止损，是『扛单』信号）")
    else:
        lines.append("当日无平仓归因记录（trade_outcomes 为空）。")

    lines += ["", "## 置信度校准"]
    if calibration["n"] >= calibration["min_samples"]:
        lines.append(calibration_text(calibration))
        if calibration.get("overconfident"):
            lines.append("⚠ 整体存在过度自信：高 confidence 桶的胜率明显低于置信度，"
                         "应降低高信心档的杠杆与仓位。")
    else:
        lines.append(f"样本不足（{calibration['n']}/{calibration['min_samples']}），待积累。")

    lines += ["", "## 主 Agent 决策"]
    if decisions:
        lines += ["| 决策 | 次数 |", "|---|---:|"]
        for r in decisions:
            lines.append(f"| {r['decision']} | {r['n']} |")
    else:
        lines.append("无决策。")

    lines += ["", "## 风控"]
    if verdicts:
        lines += ["| 风控裁决 | 次数 |", "|---|---:|"]
        for r in verdicts:
            lines.append(f"| {r['verdict']} | {r['n']} |")
    else:
        lines.append("无风控裁决。")

    lines += ["", "## 风险事件"]
    if risk_events:
        lines += ["| 类型 | 级别 | 次数 |", "|---|---|---:|"]
        for r in risk_events:
            lines.append(f"| {r['event_type']} | {r['severity']} | {r['n']} |")
    else:
        lines.append("无风险事件。")

    lines += ["", "## Agent 运行"]
    if agent_rows:
        lines += ["| Agent | 状态 | 次数 | 平均耗时ms |", "|---|---|---:|---:|"]
        for r in agent_rows:
            lines.append(f"| {r['agent_name']} | {r['status']} | {r['n']} | {r['ms'] or 0:.0f} |")
    else:
        lines.append("无运行记录。")

    lines += ["", "## 模型调用"]
    if llm_stats:
        lines.append(
            f"次数={llm_stats['n']}，输入tokens={llm_stats['pt']}，输出tokens={llm_stats['ct']}，"
            f"错误次数={llm_stats['err'] or 0}"
        )
    else:
        lines.append("无 LLM 调用。")

    lines += ["", "## 模型复盘"]
    if llm is not None and logger is not None:
        base_report = "\n".join(lines)
        llm_summary = summarize(
            "review_agent", llm, model,
            "你是交易复盘员。根据当日完整数据，用中文300字以内总结：盈亏结构、主要亏损原因、决策流程问题、次日应如何调整。",
            {"report": base_report}, "LLM复盘暂不可用", db, logger,
            deadline=deadline, purpose="review")
        lines.append(llm_summary)
    else:
        lines.append("- LLM 复盘待接入。")

    report = reviews_dir / f"{review_date}.md"
    report.write_text("\n".join(lines), encoding="utf-8")

    summary = f"已实现={trade['realized']:+.2f}U"
    db.conn.execute(
        """INSERT INTO daily_reviews(review_date, created_at, report_path, summary)
           VALUES(?,?,?,?)
           ON CONFLICT(review_date) DO UPDATE SET created_at=excluded.created_at,
             report_path=excluded.report_path, summary=excluded.summary""",
        (review_date, datetime.now(timezone.utc).isoformat(timespec="seconds"),
         str(report), summary),
    )
    db.conn.execute(
        """INSERT INTO agent_scorecard(score_date, agent_name, decision_count, error_count, score, notes)
           SELECT ?, agent_name, COUNT(*), SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END), 0, 'phase1'
           FROM agent_runs WHERE started_at >= ? AND started_at <= ?
           GROUP BY agent_name
           ON CONFLICT(score_date, agent_name) DO UPDATE SET
             decision_count=excluded.decision_count,
             error_count=excluded.error_count,
             score=excluded.score,
             notes=excluded.notes""",
        (review_date, start, end),
    )
    db.conn.commit()
    return report
