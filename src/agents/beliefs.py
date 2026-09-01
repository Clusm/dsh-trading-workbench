"""从情报 Agent 的 LLM 摘要中解析结构化 belief。"""
from __future__ import annotations

import json
from typing import Any


def extract_json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def parse_belief(text: str, fallback_summary: str = "",
                 default_belief: dict[str, Any] | None = None) -> tuple[str, dict[str, Any]]:
    data = extract_json_object(text)
    if not data:
        return (text or fallback_summary), dict(default_belief or {})
    summary = str(data.get("summary") or data.get("reasoning") or fallback_summary or text)
    # 模型有时把整个 JSON 又包进 summary 字段（双重转义），这里递归解包一层
    nested = extract_json_object(summary)
    if nested and isinstance(nested, dict) and (nested.get("summary") or nested.get("belief")):
        summary = str(nested.get("summary") or nested.get("reasoning") or summary)
    belief = data.get("belief")
    if not isinstance(belief, dict):
        belief = {k: v for k, v in data.items() if k not in ("summary", "reasoning")}
    return summary, dict(belief or {})


def market_default(regime: str) -> dict[str, Any]:
    trend = {"trend_up": "up", "trend_down": "down"}.get(regime, "range")
    return {"trend": trend, "strength": 0.5, "confidence": 0.5,
            "invalidation": "regime 反转或关键区间被反向突破"}


def macro_default(bias: str = "neutral") -> dict[str, Any]:
    return {"bias": bias or "neutral", "confidence": 0.5,
            "invalidation": "宏观关键数据或利率预期显著反向"}


def onchain_default() -> dict[str, Any]:
    return {"flow_bias": "neutral", "confidence": 0.5,
            "invalidation": "稳定币/TVL 出现反向趋势"}
