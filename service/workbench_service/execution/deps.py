"""服务实例装配：把 ``ExecutionService`` 挂到 ``app.state``（首次使用时惰性构建）。

``app.py`` 只负责 include_router；实例化放在这里，便于测试注入 fake 上游
（``app.state.services = build_execution_service(..., client=fake_client)``）。
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅类型
    from fastapi import FastAPI

    from .service import ExecutionService

__all__ = ["get_services", "set_services", "reset_services"]

_LOCK = threading.Lock()


def get_services(app: "FastAPI") -> "ExecutionService":
    services = getattr(app.state, "services", None)
    if services is not None:
        return services
    from .service import build_execution_service

    with _LOCK:
        services = getattr(app.state, "services", None)
        if services is None:
            services = build_execution_service(app.state.settings)
            app.state.services = services
            # A06: start the read-only supervisor once per service instance.
            try:
                services.start_monitor()
            except Exception:  # noqa: BLE001 - a monitor failure must not break read APIs
                pass
        return services


def set_services(app: "FastAPI", services: "ExecutionService") -> None:
    app.state.services = services


def reset_services(app: "FastAPI") -> None:
    app.state.services = None
