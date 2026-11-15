"""FastAPI 应用：生命周期、统一错误信封、trace_id、路由装配。

路由面（M1.3c）：
- 公共：``/health``、``/``、``/docs``、``/openapi.json``；
- 只读行情：``/api/market/*``（demo 公共数据，t13）；
- 只读账户：``/api/status``、``/api/positions``、``/api/orders``、``/api/fills``、
  ``/api/equity-curve``；
- 风控：``/api/risk/envelope``、``/api/risk/evaluate``（P1 计划闸）；
- 执行：``/api/execution/{order,close,reconcile}``、``/api/protection/repair``、
  ``/api/control``（P2 提交闸 + 控制态）；
- cycle/审计：``/api/cycles*``、``/api/ai-runs*``（M1.3c 持久化；不下单）。

装配顺序固定：health → market → account → risk → execution；实现位于
``execution/deps.py``（首次请求惰性构建），``app.py`` 不承载业务逻辑。
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import account as account_api
from .api import ai_audit as ai_audit_api
from .api import cycles as cycles_api
from .api import execution as execution_api
from .api import health as health_api
from .api import market as market_api
from .api import risk as risk_api
from .config import SERVICE_NAME, Settings
from .db import init_db, utc_now_iso
from .errors import ServiceError
from .guard import evaluate_demo_guard, evaluate_guard

logger = logging.getLogger("workbench.app")


def configure_logging(level: str = "info", log_dir: Path | None = None) -> Path | None:
    """UTC 时间戳、单行、无 key；重复调用安全。

    ``log_dir`` 非空时额外写 ``<log_dir>/service.log``（10MB × 5 轮转，docs/01 §10.6）。
    返回实际日志文件路径（未启用文件日志则为 ``None``）。
    """

    logging.Formatter.converter = time.gmtime
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    if log_dir is None:
        return None

    log_path = Path(log_dir) / "service.log"
    for existing in list(root.handlers):
        if getattr(existing, "_workbench_file", False):
            root.removeHandler(existing)
            existing.close()
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)sZ %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    file_handler._workbench_file = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    return log_path


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": details or {},
            },
            "trace_id": getattr(request.state, "trace_id", ""),
        },
        headers={"Cache-Control": "no-store"},
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.phase = "starting"
        app.state.started_at_iso = utc_now_iso()
        app.state.started_monotonic = time.monotonic()
        app.state.guard = evaluate_guard(settings.base_url, settings.venue)

        logger.info(
            "service_start service=%s version=%s schema=%s python=%s pid=%s",
            SERVICE_NAME,
            settings.service_version,
            settings.schema_version,
            sys.version.split()[0],
            os.getpid(),
        )
        logger.info(
            "demo_guard ok=%s reason=%s base_url=%s",
            app.state.guard.ok,
            app.state.guard.reason_code,
            app.state.guard.base_url,
        )
        if not app.state.guard.ok:
            logger.error("demo guard rejected configured market base url: %s", app.state.guard.detail)
        if not settings.bearer_required:
            logger.warning(
                "auth mode=dev-open (no WORKBENCH_API_TOKEN); service is loopback-only and M1.0 read-only"
            )

        db = init_db(settings.data_dir, pid=os.getpid(), version=settings.service_version)
        app.state.db_status = db
        if db.ok:
            logger.info(
                "db_ready path=%s schema=%s journal=%s boot_count=%s ledger=%s migrations=%s",
                db.path,
                db.schema_version,
                db.journal_mode,
                db.boot_count,
                (db.ledger_id or "")[:8],
                db.migrations_applied,
            )
        else:
            logger.error("db_unavailable path=%s error=%s", db.path, db.error)

        app.state.phase = "ready" if (app.state.guard.ok and db.ok) else "degraded"
        logger.info(
            "service_phase phase=%s ready=%s data_dir=%s",
            app.state.phase,
            app.state.phase == "ready",
            settings.data_dir,
        )
        # A06: build the execution service at process startup (not only on the
        # first API read) so the read-only supervisor starts with the process.
        if db.ok:
            try:
                from .execution.deps import get_services

                services = get_services(app)
                thread = getattr(services, "_monitor_thread", None)
                app.state.supervisor = "running" if thread is not None and thread.is_alive() else "read_only"
            except Exception as exc:  # noqa: BLE001 - degraded API remains usable
                app.state.supervisor = "error"
                logger.warning("supervisor_start_failed error=%s", type(exc).__name__)
        try:
            yield
        finally:
            app.state.phase = "stopping"
            services = getattr(app.state, "services", None)
            if services is not None:
                try:
                    services.stop_monitor()
                except Exception:  # noqa: BLE001 - shutdown best effort
                    logger.warning("supervisor_stop_failed")
            if services is not None and getattr(services, "ledger", None) is not None:
                try:
                    services.ledger.close()
                except Exception:  # noqa: BLE001 - shutdown best effort
                    logger.warning("execution_ledger_close_failed")
            market = getattr(app.state, "market", None)
            close_market = getattr(market, "close", None)
            if callable(close_market):
                try:
                    close_market()
                except Exception:  # noqa: BLE001 - shutdown best effort
                    logger.warning("market_close_failed")
            logger.info("service_stop pid=%s", os.getpid())

    app = FastAPI(
        title="trading-workbench-service",
        version=settings.service_version,
        summary=(
            "M1.2 demo-only execution: account/order/protection/risk/ledger, "
            "P1/P2 two-gate + R00–R25 hard rules (read-only market probe)"
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.state.settings = settings
    app.state.phase = "starting"
    app.state.guard = evaluate_guard(settings.base_url, settings.venue)
    app.state.started_at_iso = utc_now_iso()
    app.state.started_monotonic = time.monotonic()
    app.state.db_status = None
    app.state.last_probe = None

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        trace_id = (request.headers.get("x-request-id") or uuid.uuid4().hex)[:32]
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServiceError)
    async def _service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
        return _error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            retryable=exc.retryable,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        issues = [
            {
                "loc": list(item.get("loc", [])),
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
            for item in exc.errors()
        ]
        return _error_response(
            request, 422, "E_VALIDATION", "request validation failed", details={"issues": issues}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "E_NOT_FOUND" if exc.status_code == 404 else f"E_HTTP_{exc.status_code}"
        return _error_response(request, exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_error trace_id=%s path=%s error=%s",
            getattr(request.state, "trace_id", ""),
            request.url.path,
            type(exc).__name__,
        )
        return _error_response(
            request,
            500,
            "E_INTERNAL",
            "unexpected internal error; see service log",
            details={"type": type(exc).__name__},
        )

    app.include_router(health_api.router)
    app.include_router(market_api.router)
    app.include_router(account_api.router)
    app.include_router(risk_api.router)
    app.include_router(execution_api.router)
    app.include_router(cycles_api.router)
    app.include_router(ai_audit_api.router)

    @app.get("/", include_in_schema=False)
    def index() -> dict[str, Any]:
        return {
            "service": SERVICE_NAME,
            "service_version": __version__,
            "venue": "demo",
            "mode": "demo-read-only-market + signed-demo-execution",
            "endpoints": [
                "/health",
                "/api/ready",
                "/api/market/probe",
                "/api/status",
                "/api/positions",
                "/api/orders",
                "/api/fills",
                "/api/equity-curve",
                "/api/risk/envelope",
                "/api/risk/evaluate",
                "/api/execution/order",
                "/api/execution/close",
                "/api/execution/reconcile",
                "/api/protection/repair",
                "/api/control",
                "/api/cycles",
                "/api/cycles/{id}",
                "/api/cycles/{id}/evidence",
                "/api/cycles/{id}/finalize",
                "/api/cycles/{id}/ai-runs",
                "/api/cycles/{id}/artifacts/{agent}",
                "/api/ai-runs/{run_id}/calls/{call_id}",
                "/api/ai-runs/{run_id}/events",
                "/docs",
            ],
            "utc_now": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        }

    return app
