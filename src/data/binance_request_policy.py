"""Binance USD-M 请求成本与优先级登记表。

所有请求在进入公共传输层或生产签名传输层前都必须经过本模块。这里的权重是
项目本地预算使用的保守值，不宣称替代 Binance 动态响应头；响应头仍由
``rate_limit.py`` 校正。未登记端点直接报错，避免新代码默默按 1 权重发送。
"""
from __future__ import annotations

from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import time
from typing import Any, Mapping


class UnknownBinanceEndpoint(RuntimeError):
    """请求端点尚未登记，发布前必须补齐成本和用途。"""


@dataclass(frozen=True)
class RequestPolicy:
    """一次请求的本地治理元数据。"""

    method: str
    endpoint: str
    ip_weight: int
    order_costs: dict[str, int]
    endpoint_window_costs: dict[str, int]
    priority: str
    purpose: str
    budget_scope: str


def parse_retry_after(headers: Mapping[str, Any] | None, *, default: float,
                     maximum: float, minimum: float = 5.0) -> float:
    """解析 Retry-After 的秒数或 HTTP 日期，缺失/非法值使用保守默认值。"""
    raw: Any = None
    for key, value in dict(headers or {}).items():
        if str(key).lower() == "retry-after":
            raw = value
            break
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            target = parsedate_to_datetime(str(raw))
            seconds = target.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            seconds = float(default)
    return min(float(maximum), max(float(minimum), seconds))


# endpoint -> 保守的 IP 权重。带 symbol/limit 的动态权重在 policy_for 中计算。
# 这里覆盖当前仓库所有真实发送入口；诊断脚本若要新增端点，也必须显式登记。
_ENDPOINTS: dict[tuple[str, str], int] = {
    ("GET", "/fapi/v1/time"): 1,
    ("GET", "/fapi/v1/exchangeInfo"): 1,
    ("GET", "/fapi/v1/klines"): 1,
    ("GET", "/fapi/v1/ticker/24hr"): 1,
    ("GET", "/fapi/v1/ticker/price"): 1,
    ("GET", "/fapi/v1/premiumIndex"): 1,
    ("GET", "/fapi/v1/openInterest"): 1,
    ("GET", "/fapi/v1/depth"): 2,
    ("GET", "/futures/data/globalLongShortAccountRatio"): 1,
    ("GET", "/futures/data/topLongShortAccountRatio"): 1,
    ("GET", "/futures/data/topLongShortPositionRatio"): 1,
    ("GET", "/futures/data/takerlongshortRatio"): 1,
    ("GET", "/fapi/v1/account"): 5,
    ("GET", "/fapi/v2/account"): 5,
    ("GET", "/fapi/v3/account"): 5,
    ("GET", "/fapi/v1/positionRisk"): 5,
    ("GET", "/fapi/v2/positionRisk"): 5,
    ("GET", "/fapi/v1/positionSide/dual"): 1,
    ("GET", "/fapi/v1/openOrders"): 1,
    ("GET", "/fapi/v1/order"): 1,
    ("GET", "/fapi/v1/userTrades"): 5,
    ("GET", "/fapi/v1/algoOrder"): 1,
    ("GET", "/fapi/v1/openAlgoOrders"): 1,
    ("POST", "/fapi/v1/order"): 1,
    ("DELETE", "/fapi/v1/order"): 1,
    ("POST", "/fapi/v1/algoOrder"): 1,
    ("DELETE", "/fapi/v1/algoOrder"): 1,
    ("POST", "/fapi/v1/marginType"): 1,
    ("POST", "/fapi/v1/leverage"): 1,
    ("GET", "/fapi/v1/leverageBracket"): 1,
    # endpoint_weight 的响应归因只知道路径，不知道实际的 POST/PUT/DELETE 方法，
    # 因此为 listenKey 保留 GET 这一成本别名。
    ("GET", "/fapi/v1/listenKey"): 1,
    ("POST", "/fapi/v1/listenKey"): 1,
    ("PUT", "/fapi/v1/listenKey"): 1,
    ("DELETE", "/fapi/v1/listenKey"): 1,
}


def _normalized(method: str, endpoint: str) -> tuple[str, str]:
    return str(method or "GET").upper(), str(endpoint or "").split("?", 1)[0]


def _dynamic_weight(method: str, endpoint: str, params: Mapping[str, Any]) -> int:
    """根据 Binance 公开规则为批量行情取保守权重。"""
    if endpoint == "/fapi/v1/klines":
        try:
            limit = int(params.get("limit") or 500)
        except (TypeError, ValueError):
            limit = 500
        if limit < 100:
            return 1
        if limit < 500:
            return 2
        if limit <= 1000:
            return 5
        return 10
    if endpoint == "/fapi/v1/ticker/24hr":
        return 1 if params.get("symbol") else 40
    if endpoint == "/fapi/v1/ticker/price":
        return 1 if params.get("symbol") else 2
    if endpoint == "/fapi/v1/premiumIndex":
        return 1 if params.get("symbol") else 10
    if endpoint == "/fapi/v1/depth":
        try:
            limit = int(params.get("limit") or 100)
        except (TypeError, ValueError):
            limit = 100
        if limit <= 50:
            return 2
        if limit <= 100:
            return 5
        if limit <= 500:
            return 10
        return 20
    if endpoint in {"/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"}:
        return 1 if params.get("symbol") else _ENDPOINTS[(method, endpoint)]
    return _ENDPOINTS[(method, endpoint)]


def _priority_for(purpose: str) -> str:
    normalized = str(purpose or "normal").lower()
    if normalized in {"protect", "exit", "account_reconcile", "order_reconcile",
                      "protection_reconcile", "trade_reconcile", "startup",
                      "user_stream", "setup"}:
        return "critical"
    if normalized in {"market_scan", "intel", "history", "diagnostic", "public_time"}:
        return "background"
    return "normal"


def policy_for(method: str, endpoint: str, params: Mapping[str, Any] | None = None,
               *, purpose: str = "normal", environment: str = "public",
               strict: bool = True) -> RequestPolicy:
    """获取端点治理策略；未知端点显式失败。"""
    normalized_method, normalized_endpoint = _normalized(method, endpoint)
    key = (normalized_method, normalized_endpoint)
    if key not in _ENDPOINTS:
        if not strict:
            normalized_purpose = str(purpose or "normal").lower()
            return RequestPolicy(
                method=normalized_method, endpoint=normalized_endpoint,
                ip_weight=1, order_costs={}, endpoint_window_costs={},
                priority=_priority_for(normalized_purpose), purpose=normalized_purpose,
                budget_scope=f"binance_{str(environment or 'unknown').lower()}",
            )
        raise UnknownBinanceEndpoint(
            f"未登记 Binance 端点: {normalized_method} {normalized_endpoint}")
    values = dict(params or {})
    weight = _dynamic_weight(normalized_method, normalized_endpoint, values)
    normalized_purpose = str(purpose or "normal").lower()
    is_order_write = normalized_method == "POST" and normalized_endpoint in {
        "/fapi/v1/order", "/fapi/v1/algoOrder",
    }
    order_costs = {"order_count_10s": 1, "order_count_1m": 1} if is_order_write else {}
    window_costs = ({"futures_data_5m": weight}
                    if normalized_endpoint.startswith("/futures/data/") else {})
    return RequestPolicy(
        method=normalized_method,
        endpoint=normalized_endpoint,
        ip_weight=weight,
        order_costs=order_costs,
        endpoint_window_costs=window_costs,
        priority=_priority_for(normalized_purpose),
        purpose=normalized_purpose,
        budget_scope=("binance_public_ip" if environment in {"public", "live"}
                      else f"binance_{str(environment or 'unknown').lower()}"),
    )


def registered_endpoints() -> tuple[tuple[str, str], ...]:
    """返回稳定排序的登记项，供发布检查和文档生成使用。"""
    return tuple(sorted(_ENDPOINTS))
