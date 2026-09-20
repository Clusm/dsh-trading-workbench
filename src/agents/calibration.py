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

#: 桶判定容差：|胜率 - 桶中点| 超过它即认为该桶有方向性偏差。
CALIBRATION_TOLERANCE = 0.15


def compute_calibration(db: AuditDatabase, source: str | None = None,
                        limit: int = 500) -> dict[str, Any]:
    """按 confidence 分桶统计胜率/盈亏，并给出整体失真度与**偏差方向**。

    ``miscalibration``：各桶 ``|胜率 - 桶中点置信度|`` 按样本数的加权平均，
    **只对样本充足的桶加权**——小样本桶不得把整体失真度带偏。
    ``signed_miscalibration``：同样的加权平均但保留符号，
    负值 = 过度自信（实际胜率低于置信度），正值 = 信心不足。
    ``direction``：``OVERCONFIDENT`` / ``UNDERCONFIDENT`` / ``CALIBRATED`` /
    ``INSUFFICIENT``（由**有符号**偏差得出，这才是区分高低估的字段）；
    桶级同理，小样本桶一律 ``INSUFFICIENT``。
    ``overconfident`` 保留历史语义（``|偏差|`` 超容差即置位，即「校准明显失真」），
    与 ``direction`` 可能不同：绝对偏差大但方向相互抵消时，``overconfident=True``
    而 ``direction=CALIBRATED``。新调用方应使用 ``direction``。
    小样本桶一律不参与整体加权；没有任何充足桶时 ``overconfident``/``underconfident``
    均为 False 且 ``small_sample=True``。
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

    # 只统计同时有 confidence 与 pnl 的样本：pnl 为 NULL 的平仓结果既不算赢也不算输。
    bucket_map = {label: {"n": 0, "wins": 0, "losses": 0, "flat": 0, "pnl": 0.0}
                  for _, _, label in BUCKETS}
    for r in rows:
        if r["confidence"] is None or r["pnl"] is None:
            continue
        conf = float(r["confidence"])
        pnl = float(r["pnl"])
        label = _bucket_label(conf)
        b = bucket_map[label]
        b["n"] += 1
        if pnl > 0:
            b["wins"] += 1
        elif pnl < 0:
            b["losses"] += 1
        else:
            b["flat"] += 1
        b["pnl"] += pnl

    buckets_out: list[dict[str, Any]] = []
    total_n = 0
    sufficient_n = 0
    weighted_err = 0.0
    signed_err = 0.0
    for lo, hi, label in BUCKETS:
        b = bucket_map[label]
        n = b["n"]
        total_n += n
        win_rate = (b["wins"] / n) if n else None
        mid = (lo + hi) / 2
        sufficient = n >= MIN_SAMPLES
        if sufficient:
            sufficient_n += n
            weighted_err += abs(win_rate - mid) * n
            signed_err += (win_rate - mid) * n
            if win_rate < mid - CALIBRATION_TOLERANCE:
                direction = "OVERCONFIDENT"
            elif win_rate > mid + CALIBRATION_TOLERANCE:
                direction = "UNDERCONFIDENT"
            else:
                direction = "CALIBRATED"
        else:
            direction = "INSUFFICIENT"
        buckets_out.append({
            "bucket": label,
            "mid_confidence": round(mid, 3),
            "n": n,
            "wins": b["wins"],
            "losses": b["losses"],
            "flat": b["flat"],
            "win_rate": round(win_rate, 3) if win_rate is not None else None,
            "avg_pnl": round(b["pnl"] / n, 4) if n else None,
            "total_pnl": round(b["pnl"], 4),
            # 兼容字段：保持 bool | None 语义（None = 样本不足）。
            "calibrated": None if not sufficient else direction == "CALIBRATED",
            "direction": direction,
        })

    if not sufficient_n:
        overall_direction = "INSUFFICIENT"
    elif signed_err / sufficient_n < -CALIBRATION_TOLERANCE:
        overall_direction = "OVERCONFIDENT"
    elif signed_err / sufficient_n > CALIBRATION_TOLERANCE:
        overall_direction = "UNDERCONFIDENT"
    else:
        overall_direction = "CALIBRATED"

    return {
        "n": total_n,
        "min_samples": MIN_SAMPLES,
        "sufficient_n": sufficient_n,
        "small_sample": sufficient_n == 0,
        "miscalibration": round(weighted_err / sufficient_n, 4) if sufficient_n else None,
        "signed_miscalibration": round(signed_err / sufficient_n, 4) if sufficient_n else None,
        "direction": overall_direction,
        # 历史字段：|偏差| 超容差即置位（校准明显失真），不区分方向。
        "overconfident": bool(
            sufficient_n and weighted_err / sufficient_n > CALIBRATION_TOLERANCE
        ),
        "underconfident": overall_direction == "UNDERCONFIDENT",
        "buckets": buckets_out,
    }


def _bucket_label(confidence: float) -> str:
    for lo, hi, label in BUCKETS:
        if lo <= confidence < hi:
            return label
    return BUCKETS[-1][2]


_DIRECTION_TEXT = {
    "OVERCONFIDENT": "过自信",
    "UNDERCONFIDENT": "信心不足",
    "CALIBRATED": "OK",
    "INSUFFICIENT": "样本不足",
}


def calibration_text(stats: dict[str, Any]) -> str:
    """给复盘报告用的文本；小样本桶显式标注而不给校准结论。"""
    lines = [f"校准（样本 {stats['n']}，充足样本 {stats.get('sufficient_n', 0)}，"
             f"失真度 {stats['miscalibration']}，方向 {stats.get('direction')}）",
             "| 置信度桶 | 样本 | 胜率 | 桶中点 | 判定 |", "|---|---:|---:|---:|---|"]
    for b in stats["buckets"]:
        if b["n"] == 0:
            continue
        verdict = _DIRECTION_TEXT.get(
            b.get("direction", "INSUFFICIENT"), "样本不足")
        lines.append(f"| {b['bucket']} | {b['n']} | {b['win_rate']:.0%} | "
                     f"{b['mid_confidence']:.0%} | {verdict} |")
    if stats.get("small_sample"):
        lines.append("样本不足，暂不给出整体校准方向。")
    elif stats.get("underconfident"):
        lines.append("整体信心不足：实际胜率高于置信度，可考虑提高信心档的仓位利用率。")
    return "\n".join(lines)
