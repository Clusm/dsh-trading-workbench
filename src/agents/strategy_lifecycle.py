"""策略卡生命周期：根据策略通道的真实成交表现，给出每张策略卡的状态建议。

状态机：
  active            —— 表现正常，继续使用
  insufficient      —— 样本不足（默认不干预）
  cooling           —— 最近 N 笔累计亏损 / 胜率过低，建议冷却
  disabled          —— 累计亏损超过硬阈值，建议退役

数据源：trade_outcomes（source='strategy'，按 strategy_id 聚合）。
注意：本模块只输出"建议"，不自动改 strategy_cards.json ——
卡片文件是策略通道的运行依据，改它之前必须人工确认。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from audit.database import AuditDatabase

DEFAULT_MIN_SAMPLES = 5
DEFAULT_MIN_WIN_RATE = 0.35
DEFAULT_COOLING_NET = -5.0       # 最近 N 笔累计净亏超此值 -> cooling
DEFAULT_DISABLED_NET = -15.0     # 最近 N 笔累计净亏超此值 -> disabled


def evaluate_cards(db: AuditDatabase, cards: dict[str, Any],
                   min_samples: int = DEFAULT_MIN_SAMPLES,
                   min_win_rate: float = DEFAULT_MIN_WIN_RATE,
                   cooling_net: float = DEFAULT_COOLING_NET,
                   disabled_net: float = DEFAULT_DISABLED_NET,
                   per_card_limit: int = 20) -> list[dict[str, Any]]:
    """对策略卡逐一评估。cards 为 strategy_cards.json 的 dict（含 cards 列表）。"""
    card_list = cards.get("cards", []) if isinstance(cards, dict) else cards
    results: list[dict[str, Any]] = []
    for card in card_list:
        strategy_id = str(card.get("id") or card.get("strategy_id") or "")
        if not strategy_id:
            continue
        rows = db.fetch_all(
            """SELECT pnl FROM trade_outcomes
               WHERE source='strategy' AND strategy_id=? AND pnl IS NOT NULL
               ORDER BY id DESC LIMIT ?""",
            (strategy_id, per_card_limit),
        )
        pnls = [float(r["pnl"]) for r in rows]
        n = len(pnls)
        if n == 0:
            results.append(_verdict(card, strategy_id, "insufficient", n, None, None, 0.0))
            continue
        total_net = sum(pnls)
        wins = sum(1 for p in pnls if p >= 0)
        win_rate = wins / n
        if n < min_samples:
            results.append(_verdict(card, strategy_id, "insufficient", n, win_rate, total_net, total_net))
            continue
        if total_net <= disabled_net:
            status = "disabled"
        elif total_net <= cooling_net or win_rate < min_win_rate:
            status = "cooling"
        else:
            status = "active"
        results.append(_verdict(card, strategy_id, status, n, win_rate, total_net, total_net))
    return results


def _verdict(card: dict[str, Any], strategy_id: str, status: str, n: int,
             win_rate: float | None, total_net: float | None,
             recent_net: float) -> dict[str, Any]:
    return {
        "strategy_id": strategy_id,
        "name": card.get("name") or "",
        "status": status,
        "samples": n,
        "win_rate": round(win_rate, 3) if win_rate is not None else None,
        "total_pnl": round(total_net, 4) if total_net is not None else None,
        "recent_pnl": round(recent_net, 4),
        "recommendation": {
            "active": "继续使用",
            "insufficient": f"样本不足 {DEFAULT_MIN_SAMPLES} 笔，暂不干预",
            "cooling": "最近表现差：建议冷却（暂停新信号）并复盘",
            "disabled": "累计亏损超过阈值：建议退役",
        }.get(status, ""),
    }


def apply_suggested_status(cards: dict[str, Any], results: list[dict[str, Any]],
                           statuses_to_apply: tuple[str, ...] = ("disabled", "cooling")) -> dict[str, Any]:
    """把评估结果回写到卡片副本（返回新 dict，不落盘）。仅应用冷却/退役。

    落盘前调用方需人工确认 —— 策略卡是策略通道的运行依据。
    """
    out = json.loads(json.dumps(cards))
    by_id = {r["strategy_id"]: r["status"] for r in results}
    for card in out.get("cards", []):
        sid = str(card.get("id") or card.get("strategy_id") or "")
        if sid in by_id and by_id[sid] in statuses_to_apply:
            card["lifecycle"] = {"status": by_id[sid], "updated_at": time.time()}
    return out


def write_lifecycle_report(results: list[dict[str, Any]], path: Path) -> None:
    """把评估结果写成独立状态文件（不碰策略卡本体）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "updated_at": time.time(),
        "cards": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
