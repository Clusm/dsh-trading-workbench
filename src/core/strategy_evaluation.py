"""回放结果的走步、稳健性与策略晋级报告。

所有门槛在评估前由调用方传入并写入哈希；结果只报告，不修改 decision_policy、
策略卡或运行配置，避免“看完候选结果再降低门槛”。
"""
from __future__ import annotations

import hashlib
import json
import random
import statistics
from typing import Any


def trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(trades, key=lambda item: int(item.get("closed_at_ms") or 0))
    pnls = [float(item.get("net_pnl") or 0.0) for item in ordered]
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    equity = peak = drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    holdings = [
        (int(item.get("closed_at_ms") or 0) - int(item.get("opened_at_ms") or 0)) / 60_000
        for item in ordered
    ]
    incidents = {str(item.get("incident")) for item in ordered if item.get("incident")}
    return {
        "trades": len(ordered), "net_pnl": round(sum(pnls), 12),
        "profit_factor": (round(gross_profit / gross_loss, 6) if gross_loss > 0
                          else (None if gross_profit == 0 else "INF")),
        "max_drawdown": round(drawdown, 12),
        "win_rate": round(sum(value > 0 for value in pnls) / len(pnls), 6) if pnls else None,
        "average_holding_minutes": round(statistics.fmean(holdings), 6) if holdings else None,
        "known_incident_coverage": len(incidents), "incidents": sorted(incidents),
    }


def walk_forward(trades: list[dict[str, Any]], calibration_ratio: float = 0.6,
                 validation_ratio: float = 0.2) -> dict[str, Any]:
    """按平仓时间切分校准/验证/样本外；不随机打乱，避免未来数据泄漏。"""
    if not 0 < calibration_ratio < 1 or not 0 <= validation_ratio < 1:
        raise ValueError("invalid walk-forward ratios")
    if calibration_ratio + validation_ratio >= 1:
        raise ValueError("walk-forward must retain an out-of-sample window")
    ordered = sorted(trades, key=lambda item: int(item.get("closed_at_ms") or 0))
    size = len(ordered)
    calibration_end = int(size * calibration_ratio)
    validation_end = int(size * (calibration_ratio + validation_ratio))
    windows = {
        "calibration": ordered[:calibration_end],
        "validation": ordered[calibration_end:validation_end],
        "out_of_sample": ordered[validation_end:],
    }
    return {
        name: {
            "start_ms": int(rows[0].get("closed_at_ms") or 0) if rows else None,
            "end_ms": int(rows[-1].get("closed_at_ms") or 0) if rows else None,
            "metrics": trade_metrics(rows),
        }
        for name, rows in windows.items()
    }


def bootstrap_net_pnl(trades: list[dict[str, Any]], *, samples: int = 1000,
                      seed: int = 20260911, slippage_jitter: float = 0.10) -> dict[str, Any]:
    """对交易顺序/成交成本扰动做确定性 bootstrap，不把分位数表述为收益保证。"""
    pnls = [float(item.get("net_pnl") or 0.0) for item in trades]
    if not pnls or samples < 1:
        return {"samples": 0, "p05": None, "median": None, "p95": None,
                "disclaimer": "insufficient samples; not a return guarantee"}
    rng = random.Random(seed)
    totals: list[float] = []
    for _ in range(samples):
        total = 0.0
        for _ in pnls:
            value = rng.choice(pnls)
            adverse = abs(value) * rng.uniform(0.0, max(0.0, slippage_jitter))
            total += value - adverse
        totals.append(total)
    totals.sort()
    def at(q: float) -> float:
        return totals[min(len(totals) - 1, int((len(totals) - 1) * q))]
    return {
        "samples": samples, "seed": seed, "p05": round(at(0.05), 12),
        "median": round(at(0.5), 12), "p95": round(at(0.95), 12),
        "disclaimer": "simulation sensitivity only; not a return guarantee",
    }


def promotion_report(*, baseline_trades: list[dict[str, Any]],
                     candidate_trades: list[dict[str, Any]], fixture_hash: str,
                     code_version: str, coverage_gaps: list[str],
                     directional_candidates: int, demo_closed_trades: int,
                     observation_days: int,
                     thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    frozen = thresholds or {
        "minimum_directional_candidates": 200, "minimum_demo_closed_trades": 30,
        "minimum_observation_days": 7, "maximum_opportunity_cost": 0.25,
    }
    threshold_hash = hashlib.sha256(json.dumps(
        frozen, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    base_walk = walk_forward(baseline_trades)
    candidate_walk = walk_forward(candidate_trades)
    base = base_walk["out_of_sample"]["metrics"]
    candidate = candidate_walk["out_of_sample"]["metrics"]
    enough = (directional_candidates >= int(frozen["minimum_directional_candidates"])
              and demo_closed_trades >= int(frozen["minimum_demo_closed_trades"])
              and observation_days >= int(frozen["minimum_observation_days"]))
    base_pf = base["profit_factor"]
    candidate_pf = candidate["profit_factor"]
    pf_ok = (candidate_pf == "INF" or (isinstance(candidate_pf, (int, float))
             and isinstance(base_pf, (int, float)) and candidate_pf >= base_pf))
    drawdown_ok = float(candidate["max_drawdown"]) <= float(base["max_drawdown"])
    incident_ok = int(candidate["known_incident_coverage"]) >= int(base["known_incident_coverage"])
    status = "eligible_for_manual_review" if enough and pf_ok and drawdown_ok and incident_ok else "continue_observation"
    return {
        "report_version": 1, "status": status, "auto_promoted": False,
        "fixture_hash": fixture_hash, "code_version": code_version,
        "thresholds": frozen, "threshold_hash": threshold_hash,
        "sample_gate": {"passed": enough, "directional_candidates": directional_candidates,
                        "demo_closed_trades": demo_closed_trades,
                        "observation_days": observation_days},
        "coverage_gaps": sorted(set(coverage_gaps)),
        "baseline_walk_forward": base_walk, "candidate_walk_forward": candidate_walk,
        "out_of_sample_gate": {"profit_factor_not_lower": pf_ok,
                               "drawdown_not_higher": drawdown_ok,
                               "incident_coverage_not_lower": incident_ok},
        "candidate_bootstrap": bootstrap_net_pnl(candidate_trades),
    }
