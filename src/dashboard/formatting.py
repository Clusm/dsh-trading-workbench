"""Dashboard 模板使用的纯格式化函数。

所有函数只改变展示字符串，不参与数据库、风控、下单或盈亏计算。
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any


def format_number(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def format_signed(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):+,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def format_percent(value: Any, digits: int = 2) -> str:
    try:
        # 保持旧模板行为：历史参数虽存在，但百分比始终固定两位小数。
        return f"{float(value) * 100:+.2f}%"
    except (TypeError, ValueError):
        return "n/a"


def format_price(value: Any) -> str:
    """把价格显示为五位有效数字，去掉尾零且不使用科学计数法。"""
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return "-"
    if number == 0:
        return "0"
    decimal_places = 4 - math.floor(math.log10(abs(number)))
    rounded = round(number, decimal_places)
    formatted = f"{rounded:.{max(0, decimal_places)}f}"
    if "." in formatted:
        formatted = formatted.rstrip("0").rstrip(".")
    return formatted or "0"


def format_age(value: Any, *, now: float | None = None) -> str:
    """把 ISO 或 epoch 时间显示为中文相对时间。"""
    try:
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        current = time.time() if now is None else float(now)
        seconds = max(0, int(current - parsed.timestamp()))
        if seconds < 60:
            return f"{seconds}秒前"
        if seconds < 3600:
            return f"{seconds // 60}分钟前"
        if seconds < 86400:
            return f"{seconds // 3600}小时前"
        return f"{seconds // 86400}天前"
    except (OSError, OverflowError, TypeError, ValueError):
        return str(value)


def pnl_class(value: Any) -> str:
    try:
        return "pos" if float(value) >= 0 else "neg"
    except (TypeError, ValueError):
        return ""
