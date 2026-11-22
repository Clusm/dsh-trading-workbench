"""执行层错误类型（与 API 层 ``ServiceError`` 解耦，便于离线单测）。"""

from __future__ import annotations

from typing import Any

#: P0②：交易所 451（出口 IP 受限）在上层的统一错误码；broker/service/日志共用。
VENUE_GEO_BLOCKED_CODE = "E_VENUE_GEO_BLOCKED"

__all__ = [
    "DemoGuardViolation",
    "DemoApiError",
    "ExecutionRejected",
    "OutboxUncertain",
    "ExecutionConfigError",
    "VENUE_GEO_BLOCKED_CODE",
]


class DemoGuardViolation(RuntimeError):
    """非 demo 主机/协议；必须在发请求前抛出。"""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.url = url


class DemoApiError(RuntimeError):
    """币安 Demo REST 错误。

    ``kind``：
    - ``rejected``：交易所明确拒绝（4xx + ``code``），**不重试**；
    - ``unknown``：传输错误/超时/5xx，结果不确定，只能按 clientOrderId 查询；
    - ``rate_limited``：429；``banned``：418；
    - ``geo_blocked``：451 地区访问限制（出口 IP 位于受限地区）。重试没有意义，
      由上层映射为 ``E_VENUE_GEO_BLOCKED`` 并进入指数退避（docs/19 §4 P0②）。
    """

    def __init__(
        self,
        message: str,
        *,
        path: str,
        status_code: int | None = None,
        code: int | None = None,
        kind: str = "unknown",
        retry_after_s: float | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.status_code = status_code
        self.code = code
        self.kind = kind
        self.retry_after_s = retry_after_s
        self.payload = payload

    @property
    def definitely_rejected(self) -> bool:
        return self.kind == "rejected"

    @property
    def geo_blocked(self) -> bool:
        """HTTP 451：出口 IP 位于交易所受限地区（不是限流，重试无意义）。"""

        return self.kind == "geo_blocked"

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status_code": self.status_code,
            "binance_code": self.code,
            "kind": self.kind,
            "retry_after_s": self.retry_after_s,
            "message": self.message,
        }


class ExecutionRejected(RuntimeError):
    """执行被拒绝（交易所/风控/本地前置条件）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class OutboxUncertain(RuntimeError):
    """下单结果不确定（UNKNOWN）；禁止重发，只能查询。"""

    def __init__(self, client_order_id: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.client_order_id = client_order_id
        self.message = message
        self.details = details or {}


class ExecutionConfigError(RuntimeError):
    """缺少 Demo 凭据/合约过滤器等前置条件。"""
