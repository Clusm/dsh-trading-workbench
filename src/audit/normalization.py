"""审计写入前的纯数据规范化逻辑。"""
from __future__ import annotations

import hashlib
from typing import Any


def clean_price(value: float | None) -> float | None:
    """清除浮点尾差并保留八位有效数字；空值保持为空。"""
    if value is None:
        return None
    try:
        number = float(value)
        if number == 0:
            return 0.0
        return float(f"{number:.8g}")
    except (TypeError, ValueError):
        return value


def risk_incident_identity(
    event_type: str, raw: dict[str, Any] | None
) -> tuple[str, str]:
    """返回稳定 incident 键与脱敏范围；范围只取 symbol 或 environment。"""
    raw_data = raw if isinstance(raw, dict) else {}
    scope = str(raw_data.get("symbol") or raw_data.get("environment") or "")[:80]
    key = hashlib.sha256(f"{event_type}|{scope}".encode("utf-8")).hexdigest()
    return key, scope


def strongest_severity(current: str, incoming: str) -> str:
    """风险严重度只能保持或升级，不能因后续较轻事件自动降级。"""
    ranks = {"warn": 1, "error": 2, "critical": 3}
    return incoming if ranks.get(incoming, 0) >= ranks.get(current, 0) else current
