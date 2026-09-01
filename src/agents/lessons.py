"""历史亏损案例复盘库：不直接塞进主 Agent 上下文，而是让 Agent 通过工具按需查询。"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase

LESSONS_FILE = Path(__file__).resolve().parents[2] / "knowledge" / "review_lessons.json"


def _parse(raw_json: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw_json or "{}")
    except Exception:
        return {}


def _iso_to_dt(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _lesson_text(item: dict[str, Any]) -> str:
    lessons = item.get("lessons") or ["避免重复类似亏损"]
    return "；".join(lessons)


def build_review_library(db: AuditDatabase, limit: int = 8, min_loss: float = 0.30) -> dict[str, Any]:
    """扫描最近亏损平仓订单，生成复盘库文件并返回内容。"""
    rows = db.fetch_all("SELECT * FROM orders WHERE status='FILLED' ORDER BY id ASC")
    orders: list[dict[str, Any]] = []
    for r in rows:
        raw = _parse(r["raw_json"] if "raw_json" in r.keys() else None)
        orders.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "symbol": r["symbol"],
            "side": r["side"],
            "qty": float(r["qty"] or 0.0),
            "price": float(r["price"] or 0.0),
            "raw": raw,
        })

    # 每个 symbol 最近的 open 用于配对
    open_positions: dict[str, list[dict[str, Any]]] = {}
    cases: list[dict[str, Any]] = []
    for o in orders:
        raw = o["raw"]
        action = str(raw.get("action") or "")
        if action == "open":
            open_positions.setdefault(o["symbol"], []).append(o)
            continue
        if action not in ("close", "close_all", "reduce"):
            continue
        net = raw.get("net_pnl")
        if net is None:
            net = raw.get("pnl")
        if net is None or float(net) >= -min_loss:
            continue
        # 配对最近一次 open
        opens = [x for x in open_positions.get(o["symbol"], []) if x["created_at"] < o["created_at"]]
        entry = opens[-1] if opens else None
        hold_min = None
        if entry:
            try:
                hold_min = int((_iso_to_dt(o["created_at"]) - _iso_to_dt(entry["created_at"])).total_seconds() / 60)
            except Exception:
                hold_min = None
        leverage = raw.get("leverage")
        lessons = []
        if hold_min is not None and hold_min <= 15:
            lessons.append("持有时间过短，入场过于急躁或追价")
        elif hold_min is not None and hold_min >= 60:
            lessons.append("亏损单持有过久，没有及时止损")
        try:
            if float(leverage or 0) >= 8:
                lessons.append("杠杆偏高，小波动造成大亏损")
        except Exception:
            pass
        fee = raw.get("commission")
        if fee is not None and float(fee) < -0.2:
            lessons.append("手续费占亏损比例较高，避免小仓位频繁交易")
        if not lessons:
            lessons.append("方向判断或入场位置不佳")
        cases.append({
            "ts": o["created_at"],
            "symbol": o["symbol"],
            "action": action,
            "side": o["side"],
            "qty": o["qty"],
            "entry_price": entry["price"] if entry else None,
            "exit_price": o["price"],
            "hold_minutes": hold_min,
            "leverage": leverage,
            "net_pnl": round(float(net), 4),
            "commission": round(float(fee), 4) if fee is not None else None,
            "lessons": lessons,
        })

    # 最近优先
    cases.sort(key=lambda x: x["ts"], reverse=True)
    cases = cases[:limit]
    LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    LESSONS_FILE.write_text(json.dumps({
        "updated_at": time.time(),
        "count": len(cases),
        "cases": cases,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "count": len(cases),
        "updated_at": time.time(),
        "cases": cases,
        "library_file": str(LESSONS_FILE),
    }


def query_review_library(db: AuditDatabase, limit: int = 8, min_loss: float = 0.30) -> dict[str, Any]:
    """工具入口：Agent 按需调用。"""
    return build_review_library(db, limit=limit, min_loss=min_loss)
