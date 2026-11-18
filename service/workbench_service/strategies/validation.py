"""Bounded data validation, not a strategy interpreter or code executor."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from ..errors import ServiceError
from ..market.config import WHITELIST

FIELDS = (
    "symbols",
    "timeframes",
    "entry",
    "exit",
    "invalidation",
    "position_constraints",
    "required_data",
    "requires_ai",
)
OPTIONAL_FIELDS = {"execution_mode", "circuit_breaker"}
ALLOWED_FIELDS = set(FIELDS) | OPTIONAL_FIELDS
TIMEFRAMES = {"15m", "1h", "4h", "1d"}
OPERATORS = ("gt", "lt", "gte", "lte")
EXECUTION_MODES = ("ai_assisted", "rule_direct")
REQUIRED_DATA_ALLOWED = {"mark_price", "klines", "funding_rate"}
WHITELIST_SET = frozenset(WHITELIST)


def fail(code: str, message: str, status: int = 422) -> None:
    raise ServiceError(status, code, message)


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        fail("E_STRATEGY_VALIDATION", "策略内容必须为有限 JSON 数据")


def digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        fail("E_STRATEGY_VALIDATION", f"{name} 必须为不小于 {minimum} 的整数")
    return value


def text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        fail("E_STRATEGY_VALIDATION", f"{name} 必须为非空文本且不超过 {limit} 字符")
    return value.strip()


def draft_payload(body: dict) -> dict:
    if not isinstance(body, dict):
        fail("E_STRATEGY_VALIDATION", "草稿必须为对象")
    source = text(body.get("source_text"), "source_text", 20_000)
    if len(source.encode("utf-8")) > 65_536:
        fail("E_STRATEGY_VALIDATION", "原文超过 64KB")
    payload = {
        "source_text": source,
        "rules": body.get("rules", {}),
        "questions": body.get("questions", []),
        "answers": body.get("answers", {}),
        "suggestions": body.get("suggestions", []),
        "summary": body.get("summary", ""),
        "sources": body.get("sources", {}),
    }
    if (
        not isinstance(payload["rules"], dict)
        or not isinstance(payload["answers"], dict)
        or not isinstance(payload["sources"], dict)
    ):
        fail("E_STRATEGY_VALIDATION", "rules/answers/sources 必须为对象")
    if not isinstance(payload["summary"], str) or len(payload["summary"]) > 5000:
        fail("E_STRATEGY_VALIDATION", "摘要格式或长度无效")
    if not isinstance(payload["questions"], list) or len(payload["questions"]) > 5:
        fail("E_STRATEGY_VALIDATION", "每次最多 5 个待答问题")
    if not isinstance(payload["suggestions"], list) or len(payload["suggestions"]) > 20:
        fail("E_STRATEGY_VALIDATION", "建议格式或数量无效")
    ids = set()
    for q in payload["questions"]:
        if not isinstance(q, dict):
            fail("E_STRATEGY_VALIDATION", "问题必须包含 id 和 question")
        qid = text(q.get("id"), "问题 ID", 100)
        text(q.get("question"), "问题", 2000)
        if qid in ids:
            fail("E_STRATEGY_VALIDATION", "问题 ID 重复")
        ids.add(qid)
    if len(canonical(payload).encode("utf-8")) > 131_072:
        fail("E_STRATEGY_VALIDATION", "草稿总大小超过 128KB")
    return payload


def _number(
    value: Any,
    low: float,
    high: float,
    *,
    inclusive_low: bool = False,
    inclusive_high: bool = False,
) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return False
    low_ok = low <= value if inclusive_low else low < value
    high_ok = value <= high if inclusive_high else value < high
    return low_ok and high_ok


def _int_range(value: Any, low: int, high: int) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _validate_atomic_entry(
    cond: Any, *, top_side: str | None = None, require_side: bool = True
) -> str | None:
    if not isinstance(cond, dict):
        return "入场子条件必须为对象"
    if require_side:
        if cond.get("side") not in ("long", "short"):
            return "入场 side 必须为 long 或 short"
    elif "side" in cond and cond.get("side") != top_side:
        return "复合条件子项 side 必须与顶层一致或省略"

    base_keys = set(cond) - {"side"}
    kind = cond.get("kind")
    if kind in ("mark_threshold", "candle_close"):
        if (
            base_keys != {"kind", "operator", "value"}
            or cond.get("operator") not in OPERATORS
            or not _number(cond.get("value"), 0, 1e15)
        ):
            return f"{kind} 需包含有效 operator(gt/lt/gte/lte) 与正数 value"
        return None
    if kind == "ema_cross":
        if (
            base_keys != {"kind", "fast", "slow", "direction"}
            or not _int_range(cond.get("fast"), 2, 100)
            or not _int_range(cond.get("slow"), 3, 200)
            or cond["slow"] <= cond["fast"]
            or cond.get("direction") not in ("bullish", "bearish")
        ):
            return "ema_cross 需满足 fast(2..100) < slow(3..200) 且 direction 为 bullish/bearish"
        return None
    if kind == "ema_compare":
        if (
            base_keys != {"kind", "fast", "slow", "operator"}
            or not _int_range(cond.get("fast"), 1, 100)
            or not _int_range(cond.get("slow"), 2, 200)
            or cond["slow"] <= cond["fast"]
            or cond.get("operator") not in OPERATORS
        ):
            return "ema_compare 需满足 fast(1..100) < slow(2..200) 且 operator 为 gt/lt/gte/lte"
        return None
    if kind == "rsi_threshold":
        if (
            not ({"kind", "operator", "value"} <= base_keys <= {"kind", "period", "operator", "value"})
            or ("period" in cond and not _int_range(cond.get("period"), 2, 50))
            or cond.get("operator") not in OPERATORS
            or not _number(cond.get("value"), 0, 100)
        ):
            return "rsi_threshold 需满足 period(2..50，默认14)、operator(gt/lt/gte/lte)、value(0..100)"
        return None
    if kind == "funding_threshold":
        if (
            base_keys != {"kind", "operator", "value"}
            or cond.get("operator") not in OPERATORS
            or not _number(cond.get("value"), -0.05, 0.05, inclusive_low=True, inclusive_high=True)
        ):
            return "funding_threshold 需满足 operator(gt/lt/gte/lte) 与 value[-0.05, 0.05]"
        return None
    return f"不支持的入场原语类型: {kind}"


def _validate_entry(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "入场条件为描述性文本，尚不支持自动执行"
    if entry.get("kind") == "all":
        if (
            set(entry) != {"kind", "side", "conditions"}
            or entry.get("side") not in ("long", "short")
            or not isinstance(entry.get("conditions"), list)
            or not (1 <= len(entry["conditions"]) <= 4)
        ):
            return "all 复合入场需包含 side(long/short) 与 1–4 条原子 conditions"
        for sub in entry["conditions"]:
            err = _validate_atomic_entry(sub, top_side=entry["side"], require_side=False)
            if err:
                return err
        return None
    return _validate_atomic_entry(entry, require_side=True)


def validate(payload: dict) -> dict:
    r = payload["rules"]
    issues: list[str] = []
    for field in FIELDS:
        value = r.get(field)
        if field == "requires_ai":
            if not isinstance(value, bool):
                issues.append("requires_ai 必须明确为 true 或 false")
        elif value is None or value == "" or value == [] or value == {}:
            issues.append(f"缺少 {field}")

    symbols = r.get("symbols")
    if (
        not isinstance(symbols, list)
        or not 1 <= len(symbols) <= 20
        or any(not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9]{2,15}USDT", v) for v in symbols)
    ):
        issues.append("symbols 必须为 1–20 个有效 USDT 标的")
    elif len(set(symbols)) != len(symbols):
        issues.append("symbols 不能重复")
    else:
        outside = [s for s in symbols if s not in WHITELIST_SET]
        if outside:
            issues.append(f"symbols 包含不在 20 币白名单内的标的: {', '.join(outside)}")

    frames = r.get("timeframes")
    if not isinstance(frames, list) or not frames or any(not isinstance(v, str) or v not in TIMEFRAMES for v in frames):
        issues.append("timeframes 含未支持周期")

    for field in ("entry", "exit", "invalidation"):
        value = r.get(field)
        if not isinstance(value, (dict, str)) or not value or (isinstance(value, str) and not value.strip()):
            issues.append(f"{field} 必须为非空规则对象或说明")

    if not isinstance(r.get("position_constraints"), dict):
        issues.append("position_constraints 必须为对象")

    req_data = r.get("required_data")
    if (
        not isinstance(req_data, list)
        or not req_data
        or any(not isinstance(v, str) or not v.strip() for v in req_data)
    ):
        issues.append("required_data 必须为非空数据需求列表")
    elif len(set(req_data)) != len(req_data):
        issues.append("required_data 不能包含重复项")

    if "execution_mode" in r and r["execution_mode"] not in EXECUTION_MODES:
        issues.append("execution_mode 必须为 ai_assisted 或 rule_direct")

    if "circuit_breaker" in r:
        cb = r["circuit_breaker"]
        if not isinstance(cb, dict) or (set(cb) - {"max_consecutive_losses", "max_cumulative_loss_usdt"}):
            issues.append("circuit_breaker 必须为仅含 max_consecutive_losses / max_cumulative_loss_usdt 的对象")
        else:
            if "max_consecutive_losses" in cb and not _int_range(cb["max_consecutive_losses"], 0, 20):
                issues.append("circuit_breaker.max_consecutive_losses 必须为 0–20 的整数")
            if "max_cumulative_loss_usdt" in cb and not _number(
                cb["max_cumulative_loss_usdt"], 0, 1e9, inclusive_low=True, inclusive_high=True
            ):
                issues.append("circuit_breaker.max_cumulative_loss_usdt 必须为 0–1e9 的有限数值")

    exit_ = r.get("exit")
    if isinstance(exit_, dict) and exit_ and ("stop_loss_pct" in exit_ or "take_profit_pct" in exit_):
        sl = exit_.get("stop_loss_pct")
        tp = exit_.get("take_profit_pct")
        sl_ok = _number(sl, 0.002, 0.05, inclusive_low=True, inclusive_high=True)
        tp_ok = _number(tp, 0, 1)
        if not sl_ok:
            issues.append("exit.stop_loss_pct 止损比例必须在 0.2%–5.0%（0.002–0.05）之间（对齐 R09）")
        if not tp_ok:
            issues.append("exit.take_profit_pct 止盈比例必须在 (0, 1) 之间")
        if sl_ok and tp_ok and float(tp) < float(sl) * 1.2 - 1e-9:
            issues.append("exit 止盈/止损盈亏比必须 ≥ 1.2（对齐 R10）")
        if "time_stop_hours" in exit_ and not _number(
            exit_.get("time_stop_hours"), 0, 168, inclusive_low=True, inclusive_high=True
        ):
            issues.append("exit.time_stop_hours 时限必须在 0–168 小时之间")

    for q in payload["questions"]:
        answer = payload["answers"].get(q["id"])
        if not isinstance(answer, str) or not answer.strip():
            issues.append(f"问题尚未回答：{q['id']}")

    unsupported: list[str] = []
    if set(r) - ALLOWED_FIELDS:
        unsupported.append("规则包含首版尚不能执行的额外条件，请保留并等待支持")

    entry_err = _validate_entry(r.get("entry"))
    if entry_err:
        unsupported.append(entry_err)

    if (
        not isinstance(exit_, dict)
        or not exit_
        or ("stop_loss_pct" not in exit_ and "take_profit_pct" not in exit_)
        or (set(exit_) - {"stop_loss_pct", "take_profit_pct", "time_stop_hours"})
    ):
        unsupported.append("退出需明确止损/止盈比例及可选 0–168 小时时限")

    if not isinstance(frames, list) or len(frames) != 1:
        unsupported.append("当前仅支持单周期")
    if r.get("position_constraints") != {"max_positions": 1}:
        unsupported.append("当前持仓约束仅支持 max_positions=1")
    if r.get("requires_ai") is not False:
        unsupported.append("当前不自动执行纯主观判断型策略（requires_ai=true）")
    if r.get("invalidation") != {"kind": "stop_loss"}:
        unsupported.append("当前失效条件仅支持 {kind:stop_loss}，沿用确认的退出止损")
    if not isinstance(req_data, list) or any(v not in REQUIRED_DATA_ALLOWED for v in req_data):
        unsupported.append("当前执行数据仅支持 mark_price、klines、funding_rate")

    return {
        "valid": not issues,
        "issues": issues,
        "execution_ready": not issues and not unsupported,
        "unsupported": unsupported,
    }
