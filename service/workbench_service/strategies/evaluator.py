"""Deterministic Phase-2 strategy rule evaluator and rule summary helpers.

Pure functions only: zero eval/exec, zero network/DB side effects.
Reuses ``workbench_service.market.indicators`` (``ema``, ``rsi``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from ..market import config as market_config
from ..market.indicators import ema, rsi

PERIOD_MS: dict[str, int] = {
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

KLINE_KINDS = frozenset({"candle_close", "ema_cross", "ema_compare", "rsi_threshold"})

_OP_ZH = {"gt": ">", "lt": "<", "gte": "≥", "lte": "≤"}
_OP_EN = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}


@dataclass(frozen=True, slots=True)
class NormalizedBar:
    open_time_ms: int
    close_time_ms: int
    close: float


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _to_dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return dec if dec.is_finite() else None


def entry_uses_klines(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    kind = entry.get("kind")
    if kind in KLINE_KINDS:
        return True
    if kind == "all":
        return any(
            isinstance(sub, Mapping) and sub.get("kind") in KLINE_KINDS
            for sub in (entry.get("conditions") or ())
        )
    return False


def entry_uses_funding(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    kind = entry.get("kind")
    if kind == "funding_threshold":
        return True
    if kind == "all":
        return any(
            isinstance(sub, Mapping) and sub.get("kind") == "funding_threshold"
            for sub in (entry.get("conditions") or ())
        )
    return False


def min_bars_for_entry(entry: Any) -> int:
    if not isinstance(entry, Mapping):
        return 0
    kind = entry.get("kind")
    if kind == "candle_close":
        return 1
    if kind == "ema_compare":
        return max(2, int(entry.get("slow") or 2))
    if kind == "ema_cross":
        return max(4, int(entry.get("slow") or 3) + 1)
    if kind == "rsi_threshold":
        return max(3, int(entry.get("period") or 14) + 1)
    if kind == "all":
        return max(
            (min_bars_for_entry(sub) for sub in (entry.get("conditions") or ())),
            default=0,
        )
    return 0


def recommended_fetch_bars(entry: Any, timeframe: str) -> int:
    minimum = min_bars_for_entry(entry)
    if minimum <= 0:
        return 0
    warmup = market_config.WARMUP_BARS.get(timeframe, 200)
    return max(minimum + 50, warmup)


def _normalize_closed_bars(
    bars: Sequence[Any] | None, timeframe: str, now_ms: int
) -> list[NormalizedBar]:
    if not bars:
        return []
    period = PERIOD_MS.get(timeframe, 900_000)
    normalized: list[NormalizedBar] = []
    for item in bars:
        if isinstance(item, Mapping):
            final = item.get("final", True)
            open_ms = item.get("open_time_ms", item.get("open_time"))
            close_ms = item.get("close_time_ms", item.get("close_time"))
            close_val = _to_float(item.get("close"))
        else:
            final = getattr(item, "final", True)
            open_ms = getattr(item, "open_time_ms", None)
            close_ms = getattr(item, "close_time_ms", None)
            close_val = _to_float(getattr(item, "close", None))
        if final is False or open_ms is None or close_val is None:
            continue
        open_int = int(open_ms)
        close_int = int(close_ms) if close_ms is not None else (open_int + period - 1)
        if close_int > int(now_ms):
            continue
        normalized.append(NormalizedBar(open_time_ms=open_int, close_time_ms=close_int, close=close_val))
    normalized.sort(key=lambda b: b.open_time_ms)
    return normalized


def _compare_float(lhs: float, operator: str, rhs: float) -> bool:
    if operator == "gt":
        return lhs > rhs
    if operator == "lt":
        return lhs < rhs
    if operator == "gte":
        return lhs >= rhs
    if operator == "lte":
        return lhs <= rhs
    return False


def _compare_dec(lhs: Decimal, operator: str, rhs: Decimal) -> bool:
    if operator == "gt":
        return lhs > rhs
    if operator == "lt":
        return lhs < rhs
    if operator == "gte":
        return lhs >= rhs
    if operator == "lte":
        return lhs <= rhs
    return False


def _distance_pct(lhs: float, rhs: float) -> float | None:
    if not math.isfinite(lhs) or not math.isfinite(rhs):
        return None
    if abs(rhs) < 1e-12:
        return round((lhs - rhs) * 100.0, 4)
    return round((lhs - rhs) / abs(rhs) * 100.0, 4)


def compute_signal_key(
    rules: Mapping[str, Any],
    *,
    now_ms: int,
    closed_bars: Sequence[NormalizedBar] | None = None,
) -> str:
    frames = rules.get("timeframes") or ["15m"]
    timeframe = str(frames[0]) if isinstance(frames, Sequence) and frames else "15m"
    period = PERIOD_MS.get(timeframe, 900_000)
    if entry_uses_klines(rules.get("entry")) and closed_bars:
        return f"{timeframe}:{int(closed_bars[-1].open_time_ms) // period}"
    return f"{timeframe}:{int(now_ms) // period}"


def _evaluate_atomic(
    cond: Mapping[str, Any],
    *,
    mark_dec: Decimal,
    mark_float: float,
    mark_quote: Mapping[str, Any],
    closed_bars: list[NormalizedBar],
) -> dict[str, Any]:
    kind = str(cond.get("kind") or "")
    if kind == "mark_threshold":
        op = str(cond["operator"])
        rhs_dec = _to_dec(cond["value"])
        rhs = float(cond["value"])
        matched = _compare_dec(mark_dec, op, rhs_dec) if rhs_dec is not None else _compare_float(mark_float, op, rhs)
        dist = _distance_pct(mark_float, rhs)
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": f"mark {mark_float:g} {_OP_ZH.get(op, op)} {rhs:g}",
            "facts": {"kind": kind, "mark": mark_float, "operator": op, "threshold": rhs},
            "distance_pct": dist,
        }

    if kind == "candle_close":
        if not closed_bars:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": "闭合 K 线数量不足",
                "facts": {"kind": kind, "bars": 0},
                "distance_pct": None,
            }
        op = str(cond["operator"])
        rhs = float(cond["value"])
        last_close = closed_bars[-1].close
        matched = _compare_float(last_close, op, rhs)
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": f"close {last_close:g} {_OP_ZH.get(op, op)} {rhs:g}",
            "facts": {
                "kind": kind,
                "candle_close": round(last_close, 8),
                "operator": op,
                "threshold": rhs,
                "bar_open_time_ms": closed_bars[-1].open_time_ms,
            },
            "distance_pct": _distance_pct(last_close, rhs),
        }

    if kind == "ema_cross":
        fast_p = int(cond["fast"])
        slow_p = int(cond["slow"])
        direction = str(cond["direction"])
        closes = [b.close for b in closed_bars]
        if len(closes) < slow_p + 1:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"EMA({fast_p},{slow_p}) 需要至少 {slow_p + 1} 根闭合 K 线，当前 {len(closes)}",
                "facts": {"kind": kind, "bars": len(closes), "needed": slow_p + 1},
                "distance_pct": None,
            }
        fast_vals = ema(closes, fast_p)
        slow_vals = ema(closes, slow_p)
        prev_f, curr_f = fast_vals[-2], fast_vals[-1]
        prev_s, curr_s = slow_vals[-2], slow_vals[-1]
        if prev_f is None or curr_f is None or prev_s is None or curr_s is None:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"EMA({fast_p},{slow_p}) 预热不足",
                "facts": {"kind": kind, "bars": len(closes)},
                "distance_pct": None,
            }
        if direction == "bullish":
            matched = prev_f <= prev_s and curr_f > curr_s
        else:
            matched = prev_f >= prev_s and curr_f < curr_s
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": (
                f"EMA({fast_p})={curr_f:.4g} vs EMA({slow_p})={curr_s:.4g} "
                f"({'金叉' if direction == 'bullish' else '死叉'}: {'满足' if matched else '未满足'})"
            ),
            "facts": {
                "kind": kind,
                "fast": fast_p,
                "slow": slow_p,
                "direction": direction,
                "ema_fast": round(curr_f, 8),
                "ema_slow": round(curr_s, 8),
                "prev_ema_fast": round(prev_f, 8),
                "prev_ema_slow": round(prev_s, 8),
                "bar_open_time_ms": closed_bars[-1].open_time_ms,
            },
            "distance_pct": _distance_pct(curr_f, curr_s),
        }

    if kind == "ema_compare":
        fast_p = int(cond["fast"])
        slow_p = int(cond["slow"])
        op = str(cond["operator"])
        closes = [b.close for b in closed_bars]
        if len(closes) < slow_p:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"EMA({slow_p}) 需要至少 {slow_p} 根闭合 K 线，当前 {len(closes)}",
                "facts": {"kind": kind, "bars": len(closes), "needed": slow_p},
                "distance_pct": None,
            }
        curr_f = closes[-1] if fast_p == 1 else ema(closes, fast_p)[-1]
        curr_s = ema(closes, slow_p)[-1]
        if curr_f is None or curr_s is None:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"EMA({fast_p},{slow_p}) 预热不足",
                "facts": {"kind": kind, "bars": len(closes)},
                "distance_pct": None,
            }
        matched = _compare_float(curr_f, op, curr_s)
        lhs_label = "close" if fast_p == 1 else f"EMA({fast_p})"
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": f"{lhs_label}={curr_f:.4g} {_OP_ZH.get(op, op)} EMA({slow_p})={curr_s:.4g}",
            "facts": {
                "kind": kind,
                "fast": fast_p,
                "slow": slow_p,
                "operator": op,
                "ema_fast": round(curr_f, 8),
                "ema_slow": round(curr_s, 8),
                "bar_open_time_ms": closed_bars[-1].open_time_ms,
            },
            "distance_pct": _distance_pct(curr_f, curr_s),
        }

    if kind == "rsi_threshold":
        period = int(cond.get("period", 14))
        op = str(cond["operator"])
        rhs = float(cond["value"])
        closes = [b.close for b in closed_bars]
        if len(closes) < period + 1:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"RSI({period}) 需要至少 {period + 1} 根闭合 K 线，当前 {len(closes)}",
                "facts": {"kind": kind, "bars": len(closes), "needed": period + 1},
                "distance_pct": None,
            }
        rsi_val = rsi(closes, period)[-1]
        if rsi_val is None:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"RSI({period}) 预热不足",
                "facts": {"kind": kind, "bars": len(closes)},
                "distance_pct": None,
            }
        matched = _compare_float(rsi_val, op, rhs)
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": f"RSI({period})={rsi_val:.2f} {_OP_ZH.get(op, op)} {rhs:g}",
            "facts": {
                "kind": kind,
                "period": period,
                "rsi": round(rsi_val, 4),
                "operator": op,
                "threshold": rhs,
                "bar_open_time_ms": closed_bars[-1].open_time_ms,
            },
            "distance_pct": _distance_pct(rsi_val, rhs),
        }

    if kind == "funding_threshold":
        funding = _to_float(mark_quote.get("funding_rate"))
        if funding is None:
            return {
                "matched": False,
                "reason_code": "FUNDING_UNAVAILABLE",
                "detail": "缺少实时资金费率数据",
                "facts": {"kind": kind},
                "distance_pct": None,
            }
        op = str(cond["operator"])
        rhs = float(cond["value"])
        matched = _compare_float(funding, op, rhs)
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": f"funding {funding:.6f} {_OP_ZH.get(op, op)} {rhs:.6f}",
            "facts": {
                "kind": kind,
                "funding_rate": round(funding, 8),
                "operator": op,
                "threshold": rhs,
            },
            "distance_pct": _distance_pct(funding, rhs),
        }

    return {
        "matched": False,
        "reason_code": "CONDITION_FALSE",
        "detail": f"不支持的条件类型: {kind}",
        "facts": {"kind": kind},
        "distance_pct": None,
    }


def evaluate_entry(
    rules: Mapping[str, Any],
    symbol: str,
    mark_quote: Mapping[str, Any] | None,
    bars: Sequence[Any] | None = None,
    *,
    now_ms: int,
) -> dict[str, Any]:
    """Evaluate a confirmed/draft strategy's entry condition deterministically."""
    del symbol
    frames = rules.get("timeframes") or ["15m"]
    timeframe = str(frames[0]) if isinstance(frames, Sequence) and frames else "15m"
    entry = rules.get("entry")
    closed_bars = _normalize_closed_bars(bars, timeframe, int(now_ms))
    signal_key = compute_signal_key(rules, now_ms=int(now_ms), closed_bars=closed_bars)

    quote = dict(mark_quote or {})
    mark_dec = _to_dec(quote.get("mark"))
    mark_float = _to_float(quote.get("mark"))
    age_ms = _to_float(quote.get("age_ms", quote.get("snapshot_age_ms")))
    quote_stale = bool(quote.get("stale")) or (age_ms is not None and age_ms > 15_000)

    if mark_dec is None or mark_dec <= 0 or mark_float is None or mark_float <= 0 or quote_stale:
        return {
            "matched": False,
            "reason_code": "QUOTE_STALE",
            "detail": "标记价缺失或已过期",
            "signal_key": signal_key,
            "mark": mark_float if (mark_float is not None and mark_float > 0) else None,
            "facts": {"age_ms": age_ms, "stale": True},
            "distance_pct": None,
        }

    if not isinstance(entry, Mapping):
        return {
            "matched": False,
            "reason_code": "CONDITION_FALSE",
            "detail": "入场条件尚未结构化",
            "signal_key": signal_key,
            "mark": mark_float,
            "facts": {"mark": mark_float},
            "distance_pct": None,
        }

    if entry_uses_klines(entry):
        needed = min_bars_for_entry(entry)
        if not closed_bars:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"缺少 {timeframe} 闭合 K 线（至少需 {needed} 根）",
                "signal_key": signal_key,
                "mark": mark_float,
                "facts": {"bars": 0, "needed": needed},
                "distance_pct": None,
            }
        unusable_s = market_config.FRESHNESS_S.get(timeframe, (960, 1800))[1]
        bar_age_ms = max(0, int(now_ms) - closed_bars[-1].close_time_ms)
        if bar_age_ms > unusable_s * 1000:
            return {
                "matched": False,
                "reason_code": "KLINE_STALE",
                "detail": f"{timeframe} 闭合 K 线已过期（{bar_age_ms // 1000}s > {unusable_s}s）",
                "signal_key": signal_key,
                "mark": mark_float,
                "facts": {
                    "bar_age_s": round(bar_age_ms / 1000.0, 1),
                    "max_age_s": unusable_s,
                    "bar_open_time_ms": closed_bars[-1].open_time_ms,
                },
                "distance_pct": None,
            }
        if len(closed_bars) < needed:
            return {
                "matched": False,
                "reason_code": "INSUFFICIENT_BARS",
                "detail": f"{timeframe} 闭合 K 线不足（{len(closed_bars)} < {needed}）",
                "signal_key": signal_key,
                "mark": mark_float,
                "facts": {"bars": len(closed_bars), "needed": needed},
                "distance_pct": None,
            }

    if entry.get("kind") == "all":
        sub_results = [
            _evaluate_atomic(
                sub,
                mark_dec=mark_dec,
                mark_float=mark_float,
                mark_quote=quote,
                closed_bars=closed_bars,
            )
            for sub in (entry.get("conditions") or ())
        ]
        merged_facts: dict[str, Any] = {
            "kind": "all",
            "mark": mark_float,
            "conditions": [item["facts"] for item in sub_results],
        }
        for key_fact in ("ema_fast", "ema_slow", "rsi", "candle_close", "funding_rate", "bar_open_time_ms"):
            for item in sub_results:
                if key_fact in item["facts"] and key_fact not in merged_facts:
                    merged_facts[key_fact] = item["facts"][key_fact]

        for item in sub_results:
            if item["reason_code"] not in ("OK", "CONDITION_FALSE"):
                return {
                    "matched": False,
                    "reason_code": item["reason_code"],
                    "detail": item["detail"],
                    "signal_key": signal_key,
                    "mark": mark_float,
                    "facts": merged_facts,
                    "distance_pct": item["distance_pct"],
                }
        matched = bool(sub_results) and all(item["matched"] for item in sub_results)
        distances = [item["distance_pct"] for item in sub_results if item["distance_pct"] is not None]
        return {
            "matched": matched,
            "reason_code": "OK" if matched else "CONDITION_FALSE",
            "detail": " 且 ".join(item["detail"] for item in sub_results),
            "signal_key": signal_key,
            "mark": mark_float,
            "facts": merged_facts,
            "distance_pct": distances[0] if distances else None,
        }

    single = _evaluate_atomic(
        entry,
        mark_dec=mark_dec,
        mark_float=mark_float,
        mark_quote=quote,
        closed_bars=closed_bars,
    )
    facts = {"mark": mark_float, **single["facts"]}
    return {
        "matched": single["matched"],
        "reason_code": single["reason_code"],
        "detail": single["detail"],
        "signal_key": signal_key,
        "mark": mark_float,
        "facts": facts,
        "distance_pct": single["distance_pct"],
    }


def _describe_cond(cond: Any) -> tuple[str, str]:
    if isinstance(cond, str):
        return cond.strip(), cond.strip()
    if not isinstance(cond, Mapping):
        return "未设置入场条件", "No entry condition"
    kind = cond.get("kind")
    if kind == "mark_threshold":
        op = str(cond.get("operator", ""))
        val = cond.get("value")
        return f"标记价 {_OP_ZH.get(op, op)} {val}", f"Mark {_OP_EN.get(op, op)} {val}"
    if kind == "candle_close":
        op = str(cond.get("operator", ""))
        val = cond.get("value")
        return f"收盘价 {_OP_ZH.get(op, op)} {val}", f"Candle close {_OP_EN.get(op, op)} {val}"
    if kind == "ema_cross":
        fast_p, slow_p = cond.get("fast"), cond.get("slow")
        bull = cond.get("direction") == "bullish"
        return (
            f"EMA({fast_p}) {'上穿(金叉)' if bull else '下穿(死叉)'} EMA({slow_p})",
            f"EMA({fast_p}) {'bullish cross above' if bull else 'bearish cross below'} EMA({slow_p})",
        )
    if kind == "ema_compare":
        fast_p, slow_p = cond.get("fast"), cond.get("slow")
        op = str(cond.get("operator", ""))
        lhs_zh = "收盘价" if fast_p == 1 else f"EMA({fast_p})"
        lhs_en = "Close" if fast_p == 1 else f"EMA({fast_p})"
        return (
            f"{lhs_zh} {_OP_ZH.get(op, op)} EMA({slow_p})",
            f"{lhs_en} {_OP_EN.get(op, op)} EMA({slow_p})",
        )
    if kind == "rsi_threshold":
        period = cond.get("period", 14)
        op = str(cond.get("operator", ""))
        val = cond.get("value")
        return (
            f"RSI({period}) {_OP_ZH.get(op, op)} {val}",
            f"RSI({period}) {_OP_EN.get(op, op)} {val}",
        )
    if kind == "funding_threshold":
        op = str(cond.get("operator", ""))
        val = cond.get("value")
        return (
            f"资金费率 {_OP_ZH.get(op, op)} {val}",
            f"Funding rate {_OP_EN.get(op, op)} {val}",
        )
    if kind == "all":
        subs = [_describe_cond(sub) for sub in (cond.get("conditions") or ())]
        return (
            " 且 ".join(s[0] for s in subs) or "复合条件",
            " AND ".join(s[1] for s in subs) or "Composite condition",
        )
    return f"条件({kind})", f"Condition({kind})"


def describe_rules(rules: Mapping[str, Any] | None) -> dict[str, str]:
    """Build a concise bilingual human-readable summary of strategy rules."""
    if not isinstance(rules, Mapping) or not rules:
        return {"zh": "暂无策略规则", "en": "No strategy rules configured"}
    symbols = rules.get("symbols") or []
    sym_text = "/".join(str(s) for s in symbols[:4])
    if len(symbols) > 4:
        sym_text += f" 等{len(symbols)}币"
    sym_text_en = "/".join(str(s) for s in symbols[:4])
    if len(symbols) > 4:
        sym_text_en += f" (+{len(symbols) - 4} more)"

    frames = rules.get("timeframes") or []
    tf_text = ",".join(str(f) for f in frames) if isinstance(frames, Sequence) else ""

    entry = rules.get("entry")
    side = entry.get("side") if isinstance(entry, Mapping) else None
    side_zh = "做多" if side == "long" else "做空" if side == "short" else "待定方向"
    side_en = "LONG" if side == "long" else "SHORT" if side == "short" else "UNSPECIFIED"
    cond_zh, cond_en = _describe_cond(entry)

    exit_ = rules.get("exit")
    if isinstance(exit_, Mapping) and "stop_loss_pct" in exit_ and "take_profit_pct" in exit_:
        sl_pct = _to_float(exit_.get("stop_loss_pct"))
        tp_pct = _to_float(exit_.get("take_profit_pct"))
        ts_h = _to_float(exit_.get("time_stop_hours"))
        exit_zh = (
            f"止损 {sl_pct * 100:.2f}% / 止盈 {tp_pct * 100:.2f}%"
            if sl_pct is not None and tp_pct is not None
            else "已配置止损止盈"
        )
        exit_en = (
            f"SL {sl_pct * 100:.2f}% / TP {tp_pct * 100:.2f}%"
            if sl_pct is not None and tp_pct is not None
            else "SL/TP configured"
        )
        if ts_h and ts_h > 0:
            exit_zh += f" / 时间止损 {ts_h:g}h"
            exit_en += f" / Time-stop {ts_h:g}h"
    elif isinstance(exit_, str) and exit_.strip():
        exit_zh = exit_.strip()
        exit_en = exit_.strip()
    else:
        exit_zh = "未设置退出"
        exit_en = "No exit configured"

    mode = rules.get("execution_mode", "ai_assisted")
    mode_zh = "规则直通" if mode == "rule_direct" else "AI 辅助复核"
    mode_en = "Rule Direct" if mode == "rule_direct" else "AI Assisted"

    zh = f"[{mode_zh}] {sym_text or '未选标的'} ({tf_text or '-'}) {side_zh}：当 {cond_zh} 时入场；{exit_zh}。"
    en = f"[{mode_en}] {sym_text_en or 'No symbol'} ({tf_text or '-'}) {side_en}: Enter when {cond_en}; {exit_en}."
    return {"zh": zh, "en": en}
