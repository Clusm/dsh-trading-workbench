"""校准表：AI 的 confidence 是否可信？

把平仓结果按开仓时的 confidence 分桶，统计每桶胜率与平均盈亏。
如果 confidence=0.8 的单只有 50% 胜率，说明模型过度自信 ——
校准表是给模型调仓/降杠杆决策用的第一手证据。

数据源：trade_outcomes（P1 的 Outcome Tracker 落库）。
"""
from __future__ import annotations

from typing import Any

from audit.database import AuditDatabase

# (下界, 上界, 标签)；下界含、上界不含
BUCKETS: list[tuple[float, float, str]] = [
    (0.00, 0.40, "<0.40"),
    (0.40, 0.55, "0.40-0.55"),
    (0.55, 0.70, "0.55-0.70"),
    (0.70, 0.85, "0.70-0.85"),
    (0.85, 1.01, ">=0.85"),
]

MIN_SAMPLES = 3


def compute_calibration(db: AuditDatabase, source: str | None = None,
                        limit: int = 500) -> dict[str, Any]:
    """按 confidence 分桶统计胜率/盈亏，并给出整体失真度。

    miscalibration：各桶 |胜率 - 桶中点置信度| 按样本数的加权平均。
    0 = 完美校准；正值越大越过度自信。
    """
    sql = """SELECT confidence, pnl FROM trade_outcomes
             WHERE confidence IS NOT NULL AND pnl IS NOT NULL"""
    params: tuple = ()
    if source:
        sql += " AND source=?"
        params = (source,)
    sql += " ORDER BY id DESC LIMIT ?"
    params += (limit,)
    rows = db.fetch_all(sql, params)

    bucket_map = {label: {"n": 0, "wins": 0, "pnl": 0.0} for _, _, label in BUCKETS}
    for r in rows:
        conf = float(r["confidence"])
        pnl = float(r["pnl"])
        label = _bucket_label(conf)
        b = bucket_map[label]
        b["n"] += 1
        if pnl >= 0:
            b["wins"] += 1
        b["pnl"] += pnl

    buckets_out: list[dict[str, Any]] = []
    total_n = 0
    weighted_err = 0.0
    for lo, hi, label in BUCKETS:
        b = bucket_map[label]
        n = b["n"]
        total_n += n
        win_rate = (b["wins"] / n) if n else None
        mid = (lo + hi) / 2
        err = abs(win_rate - mid) if win_rate is not None else 0.0
        weighted_err += err * n
        buckets_out.append({
            "bucket": label,
            "mid_confidence": round(mid, 3),
            "n": n,
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "avg_pnl": round(b["pnl"] / n, 4) if n else None,
            "total_pnl": round(b["pnl"], 4),
            "calibrated": None if n < MIN_SAMPLES else (abs(win_rate - mid) <= 0.15),
        })

    return {
        "n": total_n,
        "min_samples": MIN_SAMPLES,
        "miscalibration": round(weighted_err / total_n, 4) if total_n else None,
        "overconfident": True if (total_n and weighted_err / total_n > 0.15) else False,
        "buckets": buckets_out,
    }


def _bucket_label(confidence: float) -> str:
    for lo, hi, label in BUCKETS:
        if lo <= confidence < hi:
            return label
    return BUCKETS[-1][2]


def calibration_text(stats: dict[str, Any]) -> str:
    """给复盘报告用的文本。"""
    lines = [f"校准（样本 {stats['n']}，失真度 {stats['miscalibration']}）",
             "| 置信度桶 | 样本 | 胜率 | 桶中点 | 判定 |", "|---|---:|---:|---:|---|"]
    for b in stats["buckets"]:
        if b["n"] == 0:
            continue
        verdict = "过自信" if (b["calibrated"] is False) else ("OK" if b["calibrated"] else "样本不足")
        lines.append(f"| {b['bucket']} | {b['n']} | {b['win_rate']:.0%} | "
                     f"{b['mid_confidence']:.0%} | {verdict} |")
    return "\n".join(lines)
