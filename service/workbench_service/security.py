"""鉴权依赖（M1.1）。

``/health``、``/`` 免鉴权；其余 ``/api/*`` 默认要求
``Authorization: Bearer <token>``。

token 解析顺序（由 ``config.resolve_runtime_auth`` 在启动时完成）：
1. ``WORKBENCH_API_TOKEN`` 进程环境变量；
2. ``data/.api-token`` 文件；
3. 首次启动生成 32 字节随机 token 并写入文件（0600）。

只有显式 ``WORKBENCH_AUTH_MODE=dev-open`` 才允许无 token（离线测试）。
token 值永不写入日志/健康响应；``/health.auth`` 只暴露 mode/ready/source/path。
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from fastapi import Request, Response
from starlette.requests import Request as StarletteRequest

from .config import AUTH_MODE_BEARER, AUTH_MODE_DEV_OPEN
from .errors import ServiceError

#: ``/health.auth`` / ``/health.lock`` 的最小装配契约（t54 L02）。
_HEALTH_AUTH_FIELDS = frozenset(
    {"mode", "bearer_required", "token_ready", "token_source", "token_path"}
)
_HEALTH_LOCK_FIELDS = frozenset({"state", "path"})

#: 兼容旧审计/测试引用；M1.1 已实现 token 文件与默认 Bearer。
M1_AUTH_INCOMPLETE = "M1.1: data/.api-token + default Bearer"


def require_auth(request: Request, response: Response) -> None:
    settings = request.app.state.settings

    if settings.auth_mode == AUTH_MODE_DEV_OPEN:
        response.headers["X-Auth-Mode"] = AUTH_MODE_DEV_OPEN
        return

    response.headers["X-Auth-Mode"] = AUTH_MODE_BEARER
    token: str | None = settings.api_token
    if not token:
        # Fail closed: Bearer mode without a token is a configuration error,
        # never an implicit dev-open.
        raise ServiceError(
            503,
            "E_AUTH_NOT_CONFIGURED",
            "bearer mode requires an API token",
            retryable=False,
            details={"mode": AUTH_MODE_BEARER},
        )

    header = request.headers.get("authorization", "")
    expected = f"Bearer {token}"
    if not secrets.compare_digest(header, expected):
        response.headers["WWW-Authenticate"] = "Bearer"
        raise ServiceError(
            401,
            "E_AUTH_REQUIRED",
            "missing or invalid bearer token",
            details={"scheme": "Bearer"},
        )


def auth_status(request: Request) -> dict[str, Any]:
    """安全可展示的鉴权状态；绝不含 token 值。"""

    settings = request.app.state.settings
    bearer = settings.auth_mode == AUTH_MODE_BEARER
    return {
        "mode": settings.auth_mode,
        "bearer_required": bearer,
        "token_ready": settings.token_ready,
        "token_source": settings.auth_token_source,
        "token_path": str(settings.api_token_path),
        "m1_incomplete": not bearer,
        "m1_target": M1_AUTH_INCOMPLETE if not bearer else None,
    }


def _iter_route_paths(routes: Any) -> Any:
    """递归枚举路由路径；兼容 FastAPI 0.141 的 included-router 包装。"""

    for route in routes or ():
        path = getattr(route, "path", None)
        if isinstance(path, str):
            yield path
        nested = getattr(route, "routes", None)
        if nested is None:
            router = getattr(route, "router", None) or getattr(route, "original_router", None)
            if router is None:
                include_context = getattr(route, "include_context", None)
                router = getattr(include_context, "included_router", None)
            nested = getattr(router, "routes", None)
        if nested:
            yield from _iter_route_paths(nested)


def _iter_routes(routes: Any) -> Any:
    for route in routes or ():
        yield route
        nested = getattr(route, "routes", None)
        if nested is None:
            router = getattr(route, "router", None) or getattr(route, "original_router", None)
            if router is None:
                include_context = getattr(route, "include_context", None)
                router = getattr(include_context, "included_router", None)
            nested = getattr(router, "routes", None)
        if nested:
            yield from _iter_routes(nested)


def _health_route(app: Any) -> Any | None:
    for route in _iter_routes(getattr(app.router, "routes", ())):
        if getattr(route, "path", None) == "/health":
            return route
    return None


def _health_view_contract(app: Any, route: Any | None) -> bool:
    """Invoke the real ``/health`` handler and assert auth/lock fields."""

    if route is None or not callable(getattr(route, "endpoint", None)):
        return False
    settings = getattr(app.state, "settings", None)
    if settings is None:
        return False
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/health",
        "raw_path": b"/health",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 0),
        "server": (settings.host, settings.port),
        "app": app,
    }
    try:
        body = route.endpoint(StarletteRequest(scope))
    except Exception:  # noqa: BLE001 - projection failure means startup must refuse
        return False
    if not isinstance(body, Mapping):
        return False
    auth = body.get("auth")
    lock = body.get("lock")
    return (
        isinstance(auth, Mapping)
        and _HEALTH_AUTH_FIELDS.issubset(auth.keys())
        and isinstance(lock, Mapping)
        and _HEALTH_LOCK_FIELDS.issubset(lock.keys())
    )


def install_health_auth_view(app: Any) -> bool:
    """校验 ``/health`` 路由存在且 auth/lock 投影字段齐全；幂等。

    投影由 ``api/health.py`` 在 handler 内完成（不再运行时改写路由对象，
    避免依赖 FastAPI 内部结构）。本函数做**装配断言**：

    - FastAPI 0.141 的 ``app.router.routes`` 只暴露 ``_IncludedRouter`` 包装，
      不携带 ``path``，因此优先用 ``app.openapi()["paths"]`` 判断；
    - 找到真实 handler 后调用一次，确认 ``auth``/``lock`` 字段存在；
    - 返回 ``False`` 表示装配失败，调用方（``run_service.py``）必须拒绝启动。
    """

    route = _health_route(app)
    if route is None:
        return False
    try:
        paths = app.openapi().get("paths", {})
    except Exception:  # noqa: BLE001 - schema 生成失败同样视为装配失败
        paths = {}
    if "/health" not in paths and "/health" not in set(
        _iter_route_paths(getattr(app.router, "routes", ()))
    ):
        return False
    return _health_view_contract(app, route)
