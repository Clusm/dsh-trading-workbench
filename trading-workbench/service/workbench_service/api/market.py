"""公共行情只读探针路由：``GET /api/market/probe``。

约束（M1.0）：
- 只允许 GET，无 key、无签名、无下单；基址由 ``guard`` 锁定为 demo 域名；
- 5 秒进程内缓存，避免 UI/调试反复触发上游权重；
- **只缓存非 BLOCKED 结果**：失败结果不入缓存，下一次请求必须重新探测并仍然返回
  502/503 + ``error.code``（t12 F-01）；``cache=false`` 既不读也不写缓存；
- 上游全失败按类型映射（t12 F-02）：
  * 任一步 418 → 503 ``E_UPSTREAM_BANNED``；
  * 六步全 429 → 503 ``E_UPSTREAM_RATE_LIMITED``；
  * 其余 → 502 ``E_UPSTREAM_UNAVAILABLE``；
  ``Retry-After`` 取 ``steps[].retry_after_s`` 的最大值（缺省 418→480s、429→60s），
  ``details.steps`` 保留逐步诊断。
"""

from __future__ import annotations

import copy
import logging
import math
import threading
import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from ..config import SUPPORTED_INTERVALS, is_valid_symbol
from ..errors import ServiceError
from ..guard import GuardResult
from ..market import config as market_config
from ..market.service import MarketService, UnknownSymbol
from ..market.transport import (
    MarketBanned,
    MarketBudgetDeferred,
    MarketCooldown,
    MarketTimeout,
    MarketUpstreamError,
    cooldown_retry_after,
)
from ..market_probe import probe_market
from ..security import require_auth

router = APIRouter(prefix="/api/market", tags=["market"])
logger = logging.getLogger("workbench.market")

_CACHE: dict[tuple[str, str, int], tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def _market_service(request: Request) -> MarketService:
    """懒创建每进程唯一的 MarketService（离线测试可替换 app.state.market）。"""

    service = getattr(request.app.state, "market", None)
    if service is None:
        settings = request.app.state.settings
        service = MarketService(
            market_config.MarketSettings(
                base_url=settings.base_url,
                data_dir=settings.data_dir,
                timeout_s=settings.probe_timeout_s,
            )
        )
        request.app.state.market = service
    return service


def _market_error_response(request: Request, exc: MarketUpstreamError) -> JSONResponse:
    retry_after = cooldown_retry_after(exc)
    if isinstance(exc, MarketBanned):
        status_code, code = 503, "E_MARKET_BANNED"
    elif isinstance(exc, MarketCooldown):
        status_code, code = 503, "E_MARKET_COOLDOWN"
    elif isinstance(exc, MarketBudgetDeferred):
        status_code, code = 503, "E_MARKET_BUDGET_DEFERRED"
    elif isinstance(exc, MarketTimeout):
        status_code, code = 504, "E_MARKET_TIMEOUT"
    else:
        status_code, code = 502, "E_MARKET_UPSTREAM"
    headers = {
        "Cache-Control": "no-store",
        "X-Auth-Mode": request.app.state.settings.auth_mode,
    }
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(round(retry_after))))
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": exc.detail,
                "retryable": isinstance(exc, (MarketCooldown, MarketBudgetDeferred, MarketTimeout)),
                "details": {
                    "upstream_code": exc.code,
                    "status_code": exc.status_code,
                    "retry_after_s": retry_after,
                },
            },
            "trace_id": _trace_id(request),
        },
        headers=headers,
    )

#: 可缓存的 verdict；BLOCKED 永不入缓存（失败必须每次重新探测并返回错误码）。
_CACHEABLE_VERDICTS = frozenset({"GOOD", "DERIVATIVES_PARTIAL", "DEGRADED"})
_RETRYABLE_STEP_CODES = frozenset(
    {"PROBE_RATE_LIMITED", "PROBE_TIMEOUT", "PROBE_UNREACHABLE", "PROBE_HTTP_5XX"}
)


def _trace_id(request: Request) -> str:
    return getattr(request.state, "trace_id", "")


def _guard(request: Request) -> GuardResult:
    guard: GuardResult = request.app.state.guard
    if not guard.ok:
        raise ServiceError(
            403,
            guard.reason_code,
            "demo-only guard failed; public market probe is disabled",
            details={"base_url": guard.base_url, "detail": guard.detail},
        )
    return guard


def _validated_query(
    request: Request, symbol: str | None, interval: str | None, limit: int | None
) -> tuple[str, str, int]:
    settings = request.app.state.settings
    resolved_symbol = (symbol or settings.probe_symbol).strip().upper()
    if not is_valid_symbol(resolved_symbol):
        raise ServiceError(
            400,
            "E_INVALID_SYMBOL",
            "symbol must match [A-Z0-9]{2,15}USDT",
            details={"symbol": resolved_symbol},
        )
    resolved_interval = (interval or settings.probe_interval).strip().lower()
    if resolved_interval not in SUPPORTED_INTERVALS:
        raise ServiceError(
            400,
            "E_INVALID_INTERVAL",
            f"interval must be one of {list(SUPPORTED_INTERVALS)}",
            details={"interval": resolved_interval},
        )
    resolved_limit = int(limit if limit is not None else settings.probe_limit)
    if not 1 <= resolved_limit <= 100:
        raise ServiceError(
            400,
            "E_INVALID_LIMIT",
            "limit must be within [1, 100]",
            details={"limit": resolved_limit},
        )
    return resolved_symbol, resolved_interval, resolved_limit


def _cache_get(key: tuple[str, str, int], ttl_s: float) -> tuple[dict[str, Any], float] | None:
    if ttl_s <= 0:
        return None
    with _CACHE_LOCK:
        item = _CACHE.get(key)
    if item is None:
        return None
    stored_at, payload = item
    age = time.monotonic() - stored_at
    if age > ttl_s:
        return None
    return copy.deepcopy(payload), round(age, 3)


def _cache_put(key: tuple[str, str, int], payload: dict[str, Any]) -> None:
    """写入缓存；调用方保证只传非 BLOCKED 结果。"""

    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), copy.deepcopy(payload))


def _cache_clear() -> None:
    """测试辅助：清空进程内缓存。"""

    with _CACHE_LOCK:
        _CACHE.clear()


def _retry_after_from_steps(steps: list[dict[str, Any]]) -> int | None:
    values: list[float] = []
    for step in steps:
        raw = step.get("retry_after_s")
        if raw is None:
            continue
        try:
            values.append(float(raw))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return int(math.ceil(max(values)))


def _upstream_error_response(
    request: Request,
    guard: GuardResult,
    payload: dict[str, Any],
) -> JSONResponse:
    """把 BLOCKED 探针结果映射为 502/503 + 统一错误信封（含 Retry-After）。"""

    steps: list[dict[str, Any]] = list(payload.get("steps") or [])
    status_codes = [step.get("status_code") for step in steps]
    retry_after = _retry_after_from_steps(steps)

    if any(code == 418 for code in status_codes):
        status_code = 503
        code = "E_UPSTREAM_BANNED"
        message = "public market upstream temporarily banned this IP"
        retryable = False
        retry_after = retry_after or 480
    elif status_codes and all(code == 429 for code in status_codes):
        status_code = 503
        code = "E_UPSTREAM_RATE_LIMITED"
        message = "public market upstream rate limited"
        retryable = True
        retry_after = retry_after or 60
    else:
        status_code = 502
        code = "E_UPSTREAM_UNAVAILABLE"
        message = "public market probe failed: upstream unavailable"
        error_codes = {step.get("error_code") for step in steps}
        retryable = bool(error_codes.intersection(_RETRYABLE_STEP_CODES))
        if 429 in status_codes:
            retry_after = retry_after or 60

    headers = {
        "Cache-Control": "no-store",
        "X-Auth-Mode": request.app.state.settings.auth_mode,
    }
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "details": {
                    "base_url": guard.base_url,
                    "verdict": payload.get("verdict"),
                    "degradation": payload.get("degradation", []),
                    "steps": steps,
                    "request": payload.get("request", {}),
                    "retry_after_s": retry_after,
                },
            },
            "trace_id": _trace_id(request),
        },
        headers=headers,
    )


@router.get("/probe", summary="read-only public market probe (demo venue)")
def probe(
    request: Request,
    symbol: str | None = Query(default=None, max_length=20),
    interval: str | None = Query(default=None, max_length=8),
    limit: int | None = Query(default=None, ge=1, le=100),
    cache: bool = Query(default=True),
    _auth: None = Depends(require_auth),
) -> Any:
    guard = _guard(request)
    settings = request.app.state.settings
    resolved_symbol, resolved_interval, resolved_limit = _validated_query(
        request, symbol, interval, limit
    )

    key = (resolved_symbol, resolved_interval, resolved_limit)
    cached = _cache_get(key, settings.probe_cache_ttl_s) if cache else None
    if cached is not None:
        payload, age = cached
        payload.update(
            cached=True,
            cache_age_s=age,
            trace_id=_trace_id(request),
        )
        logger.info(
            "market_probe cached symbol=%s interval=%s verdict=%s",
            resolved_symbol,
            resolved_interval,
            payload.get("verdict"),
        )
        return payload

    payload = probe_market(
        settings.base_url,
        symbol=resolved_symbol,
        interval=resolved_interval,
        limit=resolved_limit,
        timeout_s=settings.probe_timeout_s,
    )
    payload.update(cached=False, trace_id=_trace_id(request))

    request.app.state.last_probe = {
        "checked_at": payload["checked_at"],
        "verdict": payload["verdict"],
        "total_latency_ms": payload["total_latency_ms"],
        "symbol": resolved_symbol,
        "interval": resolved_interval,
    }

    logger.info(
        "market_probe symbol=%s interval=%s verdict=%s ok=%s total_ms=%s cache=%s",
        resolved_symbol,
        resolved_interval,
        payload["verdict"],
        payload["ok"],
        payload["total_latency_ms"],
        cache,
    )

    if payload["verdict"] == "BLOCKED":
        # 失败结果不写缓存；下一次请求会重新探测并再次返回同样的错误码。
        return _upstream_error_response(request, guard, payload)

    if cache and payload["verdict"] in _CACHEABLE_VERDICTS:
        _cache_put(key, payload)

    return payload


# ---------------------------------------------------------------------------
# M1.1 只读行情端点：health / clock / universe / klines / prices
# ---------------------------------------------------------------------------


@router.get("/health", summary="market data health (verdict/score/issues)")
def market_health(request: Request, _auth: None = Depends(require_auth)) -> Any:
    _guard(request)
    service = _market_service(request)
    try:
        return service.health_payload()
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)


@router.get("/clock", summary="server clock offset / rtt")
def market_clock(request: Request, _auth: None = Depends(require_auth)) -> Any:
    _guard(request)
    service = _market_service(request)
    try:
        return service.clock_payload()
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)


@router.get("/universe", summary="20-symbol whitelist with deep/index selection")
def market_universe(
    request: Request,
    held: str | None = Query(default=None, max_length=200),
    prev_pick: str | None = Query(default=None, max_length=20),
    _auth: None = Depends(require_auth),
) -> Any:
    _guard(request)
    service = _market_service(request)
    held_symbols = [
        item.strip().upper()
        for item in (held or "").split(",")
        if item.strip()
    ]
    for symbol in held_symbols:
        if symbol not in service.settings.whitelist:
            raise ServiceError(
                400,
                "E_SYMBOL_NOT_ALLOWED",
                f"{symbol} is not in the 20-symbol whitelist",
                details={"symbol": symbol},
            )
    try:
        return service.universe_payload(held=held_symbols, prev_pick=prev_pick)
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)


@router.get("/scan", summary="whole-market anomaly scan (whitelist-aware, read-only)")
def market_scan(
    request: Request,
    refresh: bool = Query(default=False),
    _auth: None = Depends(require_auth),
) -> Any:
    """全市场异动扫描：白名单外的异动可见，但不会因此变成可交易标的。"""

    _guard(request)
    service = _market_service(request)
    try:
        return service.market_scan(force=refresh)
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)


@router.get("/klines", summary="closed klines only (warmup + no lookahead)")
def market_klines(
    request: Request,
    symbol: str = Query(..., min_length=3, max_length=20),
    interval: str = Query(default="15m", max_length=8),
    limit: int = Query(default=100, ge=1, le=1000),
    _auth: None = Depends(require_auth),
) -> Any:
    _guard(request)
    service = _market_service(request)
    try:
        return service.klines_payload(symbol, interval, limit)
    except UnknownSymbol as exc:
        raise ServiceError(
            400,
            "E_SYMBOL_NOT_ALLOWED",
            str(exc),
            details={"symbol": symbol.upper()},
        ) from exc
    except ValueError as exc:
        raise ServiceError(
            400,
            "E_INVALID_INTERVAL",
            str(exc),
            details={"interval": interval},
        ) from exc
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)


@router.get("/prices", summary="read-only last/mark/index/spread snapshot")
def market_prices(
    request: Request,
    symbols: str = Query(default="BTCUSDT", max_length=300),
    _auth: None = Depends(require_auth),
) -> Any:
    _guard(request)
    service = _market_service(request)
    requested = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    requested = list(dict.fromkeys(requested))
    if not requested:
        raise ServiceError(400, "E_INVALID_SYMBOLS", "symbols must not be empty")
    if len(requested) > 10:
        raise ServiceError(
            400, "E_INVALID_SYMBOLS", "at most 10 symbols per request", details={"count": len(requested)}
        )
    for symbol in requested:
        if not is_valid_symbol(symbol) or symbol not in service.settings.whitelist:
            raise ServiceError(
                400,
                "E_SYMBOL_NOT_ALLOWED",
                f"{symbol} is not in the 20-symbol whitelist",
                details={"symbol": symbol},
            )
    try:
        snapshot = service.price_snapshot(requested)
    except MarketUpstreamError as exc:
        return _market_error_response(request, exc)

    prices: dict[str, Any] = {}
    for symbol in requested:
        row = snapshot.get(symbol) or {}
        prices[symbol] = {
            "last": row.get("last"),
            "mark": row.get("mark"),
            "index": row.get("index"),
            "bid": row.get("bid"),
            "ask": row.get("ask"),
            "spread_bps": row.get("spread_bps"),
            "age_ms": row.get("age_ms"),
            "source": "rest",
            "complete": not row.get("errors"),
            "errors": row.get("errors") or [],
        }
    return {
        "as_of_ms": service.now_ms(),
        "prices": prices,
        "trace_id": _trace_id(request),
    }
