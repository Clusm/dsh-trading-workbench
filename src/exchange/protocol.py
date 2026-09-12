"""Binance USD-M 协议响应与时间字段的纯解析函数。

这里不执行网络请求、不读取账户，也不修改订单。未知保护单响应必须 fail-closed，避免把
无法解释的交易所响应误判为“没有保护单”。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def parse_iso(value: Any) -> datetime | None:
    """解析 ISO 时间；无效或空值返回 ``None``。"""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError):
        return None


def parse_iso_ms(value: object) -> int | None:
    """把带或不带时区的 ISO 时间转成 UTC 毫秒时间戳。"""
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("Z", "+00:00")
    if "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def normalize_algo_orders_payload(data: Any) -> list[dict[str, Any]]:
    """规范化 Algo 列表包装；未知结构或非法条目必须抛错。"""
    payload = data
    if isinstance(payload, dict):
        for key in ("data", "orders", "algoOrders"):
            if key in payload:
                payload = payload[key]
                break
    if isinstance(payload, dict):
        for key in ("orders", "algoOrders", "rows"):
            if key in payload:
                payload = payload[key]
                break
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise RuntimeError("openAlgoOrders返回未知结构，拒绝解释为空保护单")
    if any(not isinstance(row, dict) for row in payload):
        raise RuntimeError("openAlgoOrders列表包含非法条目，拒绝继续保护对账")
    return [dict(row) for row in payload]
