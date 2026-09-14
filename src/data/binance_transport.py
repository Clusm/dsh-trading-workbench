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
    RateLimitCoordinationUnavailable,
    check_available,
    note_418,
    note_rate_limit,
    observe_failure,
    observe_response,
    reserve_request,
    throttle,
)
from data.binance_request_policy import parse_retry_after, policy_for

BASE_URL = "https://fapi.binance.com"
_HTTP_SEND_LOCK = threading.Lock()


def endpoint_weight(path: str, params: Mapping[str, Any] | None = None) -> int:
    """返回登记表给出的保守 USD-M IP 权重。"""
    return policy_for("GET", path, params).ip_weight


def endpoint_group(path: str) -> str:
    """返回限流治理分组；futures/data 使用独立的五分钟请求预算。"""
    normalized = str(path or "").split("?", 1)[0]
    if normalized.startswith("/futures/data/"):
        return "futures_data_5m"
    return "general"


def _retry_after(headers: Mapping[str, Any] | None, default: float,
                 maximum: float) -> float:
    return parse_retry_after(headers, default=default, maximum=maximum)


def request_json(client: Any, path: str, params: dict[str, Any] | None = None,
                 *, timeout: float = 15.0, attempts: int = 3,
                 priority: str = "normal", base_url: str = BASE_URL,
                 purpose: str | None = None,
                 deadline: float | None = None) -> Any:
    """发送公共请求；429/418 永不重试，其它传输/5xx故障有界重试。"""
    policy = policy_for(
        "GET", path, params,
        purpose=purpose or ("market_scan" if str(priority).lower() == "background" else "normal"),
        environment="public",
    )
    # 兼容少数显式 critical 调用，但不再把所有签名请求在传输层默认为 critical。
    request_priority = "critical" if str(priority).lower() == "critical" else policy.priority
    weight = policy.ip_weight
    last_error: Exception | None = None
    for attempt in range(max(1, int(attempts))):
        request_id: str | None = None
        response_received = False
        try:
            effective_timeout = float(timeout)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Binance request deadline exceeded")
                effective_timeout = min(effective_timeout, max(0.1, remaining))
            # 发送锁覆盖最后一次冷却检查、HTTP和限流响应登记，确保后续排队者
            # 不会在首个429/418已经返回但尚未登记的窗口中穿透。
            with _HTTP_SEND_LOCK:
                check_available(request_priority, endpoint_group=endpoint_group(path))
                throttle(0.05, weight=weight, priority=request_priority)
                request_id = reserve_request(
                    path, weight=weight, priority=request_priority,
                    endpoint_group=endpoint_group(path), source="public_rest",
                    environment="public", purpose=policy.purpose,
                )
                response = client.get(
                    f"{base_url}{path}", params=params, timeout=effective_timeout,
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
        except (PublicMarketCooldown, PublicMarketBudgetDeferred,
                RateLimitCoordinationUnavailable):
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
            delay = min(2.0, 0.25 * (2 ** attempt))
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(delay, remaining)
            time.sleep(delay)
    raise last_error if last_error is not None else RuntimeError("Binance public request failed")
