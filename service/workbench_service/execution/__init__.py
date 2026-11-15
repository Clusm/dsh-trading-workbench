"""Demo 执行包（M1.2）：签名客户端、Outbox、保护单、Broker、服务门面。"""

from __future__ import annotations

from .broker import BrokerSnapshot, DemoBroker, NormalizedPosition, derive_client_order_id
from .client import DemoBinanceClient
from .credentials import DemoCredentials, load_demo_credentials
from .deps import get_services, reset_services, set_services
from .errors import (
    DemoApiError,
    DemoGuardViolation,
    ExecutionConfigError,
    ExecutionRejected,
    OutboxUncertain,
)
from .outbox import ACTIVE_STATUSES, TERMINAL_STATUSES, OrderOutbox
from .service import ExecutionService, build_execution_service

__all__ = [
    "DemoBinanceClient",
    "DemoCredentials",
    "load_demo_credentials",
    "DemoBroker",
    "BrokerSnapshot",
    "NormalizedPosition",
    "derive_client_order_id",
    "OrderOutbox",
    "ACTIVE_STATUSES",
    "TERMINAL_STATUSES",
    "ExecutionService",
    "build_execution_service",
    "get_services",
    "set_services",
    "reset_services",
    "DemoApiError",
    "DemoGuardViolation",
    "ExecutionConfigError",
    "ExecutionRejected",
    "OutboxUncertain",
]
