"""Bounded data validation, not a strategy interpreter or code executor."""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from ..errors import ServiceError

FIELDS = ("symbols", "timeframes", "entry", "exit", "invalidation", "position_constraints", "required_data", "requires_ai")
TIMEFRAMES = {"15m", "1h", "4h", "1d"}


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
        "rules": body.get("rules", {}), "questions": body.get("questions", []),
        "answers": body.get("answers", {}), "suggestions": body.get("suggestions", []),
        "summary": body.get("summary", ""), "sources": body.get("sources", {}),
    }
    if not isinstance(payload["rules"], dict) or not isinstance(payload["answers"], dict) or not isinstance(payload["sources"], dict):
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


def _number(value: Any, low: float, high: float, *, inclusive_low: bool = False) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and (low <= value if inclusive_low else low < value) and value < high


def validate(payload: dict) -> dict:
    r = payload["rules"]
    issues = []
    for field in FIELDS:
        value = r.get(field)
        if field == "requires_ai":
            if not isinstance(value, bool):
                issues.append("requires_ai 必须明确为 true 或 false")
        elif value is None or value == "" or value == [] or value == {}:
            issues.append(f"缺少 {field}")
    symbols = r.get("symbols")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20 or any(not isinstance(v, str) or not re.fullmatch(r"[A-Z0-9]{2,15}USDT", v) for v in symbols):
        issues.append("symbols 必须为 1–20 个有效 USDT 标的")
    elif len(set(symbols)) != len(symbols):
        issues.append("symbols 不能重复")
    frames = r.get("timeframes")
    if not isinstance(frames, list) or not frames or any(not isinstance(v, str) or v not in TIMEFRAMES for v in frames):
        issues.append("timeframes 含未支持周期")
    for field in ("entry", "exit", "invalidation"):
        value = r.get(field)
        if not isinstance(value, (dict, str)) or not value or (isinstance(value, str) and not value.strip()):
            issues.append(f"{field} 必须为非空规则对象或说明")
    if not isinstance(r.get("position_constraints"), dict):
        issues.append("position_constraints 必须为对象")
    if not isinstance(r.get("required_data"), list) or not r.get("required_data") or any(not isinstance(v, str) or not v.strip() for v in r.get("required_data", [])):
        issues.append("required_data 必须为非空数据需求列表")
    for q in payload["questions"]:
        answer = payload["answers"].get(q["id"])
        if not isinstance(answer, str) or not answer.strip():
            issues.append(f"问题尚未回答：{q['id']}")
    unsupported = []
    if set(r) - set(FIELDS):
        unsupported.append("规则包含首版尚不能执行的额外条件，请保留并等待支持")
    entry, exit_ = r.get("entry"), r.get("exit")
    if not isinstance(entry, dict) or set(entry) != {"kind", "side", "operator", "value"} or entry.get("kind") != "mark_threshold" or entry.get("side") not in ("long", "short") or entry.get("operator") not in ("gt", "lt") or not _number(entry.get("value"), 0, 1e15):
        unsupported.append("首版入场仅支持 mark_threshold（side/operator/value）")
    if not isinstance(exit_, dict) or set(exit_) - {"stop_loss_pct", "take_profit_pct", "time_stop_hours"} or not _number(exit_.get("stop_loss_pct"), 0, 1) or not _number(exit_.get("take_profit_pct"), 0, 1) or not _number(exit_.get("time_stop_hours", 0), 0, 168.000001, inclusive_low=True):
        unsupported.append("退出需明确止损/止盈比例及可选 0–168 小时时限")
    if not isinstance(frames, list) or len(frames) != 1:
        unsupported.append("首版仅支持单周期")
    if r.get("position_constraints") != {"max_positions": 1}:
        unsupported.append("首版持仓约束仅支持 max_positions=1")
    if r.get("requires_ai") is not False:
        unsupported.append("首版不执行判断型策略")
    if r.get("invalidation") != {"kind": "stop_loss"}:
        unsupported.append("首版失效条件仅支持 {kind:stop_loss}，沿用确认的退出止损")
    if not isinstance(r.get("required_data"), list) or any(v != "mark_price" for v in r.get("required_data", [])):
        unsupported.append("首版执行数据仅支持 mark_price")
    return {"valid": not issues, "issues": issues, "execution_ready": not issues and not unsupported, "unsupported": unsupported}
