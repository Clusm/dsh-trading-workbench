"""Binance USD-M 公共 REST 的唯一发送入口。

业务模块只描述端点、参数和优先级；本模块负责权重、串行发送、响应头观测、
429/418 硬熔断以及有限的传输故障重试。全局发送锁是有意设计：公开数据吞吐主要
由 WebSocket 和缓存承担，REST 更重要的目标是可证明地不在限流后继续撞击出口。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Mapping

from data.rate_limit import (
    PublicMarketBudgetDeferred,
    PublicMarketCooldown,
    check_available,
    note_418,
    note_rate_limit,
    observe_failure,
    observe_response,
    reserve_request,
    throttle,
)

BASE_URL = "https://fapi.binance.com"
_HTTP_SEND_LOCK = threading.Lock()


def endpoint_weight(path: str, params: Mapping[str, Any] | None = None) -> int:
    """返回保守的 USD-M IP 请求权重；动态端点按参数上取整。"""
    params = params or {}
    normalized = str(path).split("?", 1)[0]
    if normalized == "/fapi/v1/klines":
        limit = int(params.get("limit") or 500)
        if limit < 100:
            return 1
        if limit < 500:
            return 2
        if limit <= 1000:
            return 5
        return 10
    if normalized == "/fapi/v1/ticker/24hr":
        return 1 if params.get("symbol") else 40
    if normalized == "/fapi/v1/ticker/price":
        return 1 if params.get("symbol") else 2
    if normalized == "/fapi/v1/premiumIndex":
        return 1 if params.get("symbol") else 10
    if normalized == "/fapi/v1/depth":
        limit = int(params.get("limit") or 100)
        if limit <= 50:
            return 2
        if limit <= 100:
            return 5
        if limit <= 500:
            return 10
        return 20
    # openInterest、exchangeInfo 和 futures/data 比率端点按至少一个权重保守排队。
    return 1


def endpoint_group(path: str) -> str:
    """返回限流治理分组；futures/data 使用独立的五分钟请求预算。"""
    normalized = str(path or "").split("?", 1)[0]
    if normalized.startswith("/futures/data/"):
        return "futures_data_5m"
    return "general"


def _retry_after(headers: Mapping[str, Any] | None, default: float,
                 maximum: float) -> float:
    raw: Any = None
    for key, value in dict(headers or {}).items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    try:
        value = float(raw or default)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(5.0, value))


def request_json(client: Any, path: str, params: dict[str, Any] | None = None,
                 *, timeout: float = 15.0, attempts: int = 3,
                 priority: str = "normal", base_url: str = BASE_URL) -> Any:
    """发送公共请求；429/418 永不重试，其它传输/5xx故障有界重试。"""
    weight = endpoint_weight(path, params)
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        request_id: str | None = None
        response_received = False
        try:
            # 发送锁覆盖最后一次冷却检查、HTTP和限流响应登记，确保后续排队者
            # 不会在首个429/418已经返回但尚未登记的窗口中穿透。
            with _HTTP_SEND_LOCK:
                check_available(priority)
                throttle(0.05, weight=weight, priority=priority)
                request_id = reserve_request(
                    path, weight=weight, priority=priority,
                    endpoint_group=endpoint_group(path), source="public_rest",
                    environment="public",
                )
                response = client.get(
                    f"{base_url}{path}", params=params, timeout=timeout,
                )
                response_received = True
                status = int(getattr(response, "status_code", 0) or 0)
                retry_after = None
                if status == 418:
                    retry_after = _retry_after(response.headers, 480.0, 604800.0)
                elif status == 429:
                    retry_after = _retry_after(response.headers, 60.0, 3600.0)
                observe_response(
                    getattr(response, "headers", None), path=path, weight=weight,
                    request_id=request_id, status=status,
                    retry_after_seconds=retry_after,
                    error_code=status if status >= 400 else None,
                    error_message=getattr(response, "text", None),
                )
                if status == 418:
                    note_418(retry_after)
                    raise PublicMarketCooldown(
                        f"HTTP 418: Binance temporary IP ban; retry after {retry_after:g}s"
                    )
                if status == 429:
                    retry_after = _retry_after(response.headers, 60.0, 3600.0)
                    note_rate_limit(retry_after, status=429)
                    raise PublicMarketCooldown(
                        f"HTTP 429: Binance rate limit; retry after {retry_after:g}s"
                    )
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("code") not in (None, 0):
                    raise RuntimeError(
                        f"Binance {path}: {payload.get('msg') or payload.get('code')}"
                    )
                return payload
        except (PublicMarketCooldown, PublicMarketBudgetDeferred):
            raise
        except Exception as exc:
            if request_id is not None and not response_received:
                observe_failure(
                    request_id, path=path, weight=weight,
                    error_type=type(exc).__name__, error_message=exc,
                )
            last_error = exc
            text = str(exc).lower()
            if "http 418" in text or "http 429" in text or "公开行情冷却" in text:
                raise
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and int(status) < 500:
                raise
            if attempt + 1 >= max(1, int(attempts)):
                break
            time.sleep(min(2.0, 0.25 * (2 ** attempt)))
    raise last_error if last_error is not None else RuntimeError("Binance public request failed")
