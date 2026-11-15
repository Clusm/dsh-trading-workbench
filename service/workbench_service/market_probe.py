"""公共行情只读探针：GET only，无 key、无签名、无下单。

只访问 ``https://demo-fapi.binance.com`` 的公开端点：
- ``/fapi/v1/time``
- ``/fapi/v1/exchangeInfo``
- ``/fapi/v1/klines``（默认 BTCUSDT 15m）
- ``/fapi/v1/ticker/24hr``
- ``/fapi/v1/premiumIndex``（衍生品，失败不阻断）
- ``/fapi/v1/openInterest``（衍生品，失败不阻断）

返回结构化摘要（状态码/延迟/样本字段）与 ``verdict``，不落任何请求头、
凭据或账户数据。错误映射见 ``PROBE_*`` 原因码。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

USER_AGENT = "trading-workbench-service/0.1.0-m1.0 (read-only public probe)"
FORBIDDEN_HEADERS = ("authorization", "x-mbx-apikey", "x-mbx-signature")

# 步骤名与上游路径（GET only）
STEP_TIME = "time"
STEP_EXCHANGE_INFO = "exchange_info"
STEP_KLINES = "klines"
STEP_TICKER = "ticker_24hr"
STEP_PREMIUM = "premium_index"
STEP_OPEN_INTEREST = "open_interest"
CORE_STEPS = (STEP_TIME, STEP_EXCHANGE_INFO, STEP_KLINES, STEP_TICKER)
DERIVATIVE_STEPS = (STEP_PREMIUM, STEP_OPEN_INTEREST)


@dataclass(slots=True)
class StepResult:
    name: str
    path: str
    status_code: int | None = None
    ok: bool = False
    latency_ms: float = 0.0
    error_code: str | None = None
    error_detail: str | None = None
    retry_after_s: float | None = None
    sample: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "method": "GET",
            "path": self.path,
            "status_code": self.status_code,
            "ok": self.ok,
            "latency_ms": self.latency_ms,
            "sample": self.sample,
        }
        if self.error_code:
            payload["error_code"] = self.error_code
            payload["error_detail"] = self.error_detail
        if self.retry_after_s is not None:
            payload["retry_after_s"] = self.retry_after_s
        return payload


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_iso(ms: int | None = None) -> str:
    moment = datetime.now(timezone.utc) if ms is None else datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return None if number is None else int(number)


def _retry_after_s(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _classify_failure(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, httpx.TimeoutException):
        return "PROBE_TIMEOUT", f"upstream timeout: {type(exc).__name__}"
    if isinstance(exc, httpx.HTTPError):
        return "PROBE_UNREACHABLE", f"upstream transport error: {type(exc).__name__}"
    if isinstance(exc, (ValueError, KeyError, TypeError, IndexError)):
        return "PROBE_BAD_JSON", f"unexpected payload: {type(exc).__name__}: {exc}"
    return "PROBE_INTERNAL", f"{type(exc).__name__}: {exc}"


def _run_step(
    client: httpx.Client,
    name: str,
    path: str,
    params: dict[str, Any] | None,
    parser: Callable[[Any], dict[str, Any]],
) -> StepResult:
    step = StepResult(name=name, path=path)
    started = time.perf_counter()
    try:
        response = client.get(path, params=params or None)
        step.status_code = response.status_code
        payload = response.json()
        if response.status_code == 429:
            step.error_code = "PROBE_RATE_LIMITED"
            step.retry_after_s = _retry_after_s(response)
            step.error_detail = "HTTP 429 rate limited; no retry (see docs/04 §8.3)"
            return step
        if response.status_code == 418:
            step.error_code = "PROBE_IP_BANNED"
            step.retry_after_s = _retry_after_s(response)
            step.error_detail = "HTTP 418 temporary IP ban; REST must stop"
            return step
        if response.status_code >= 500:
            step.error_code = "PROBE_HTTP_5XX"
            step.error_detail = f"upstream HTTP {response.status_code}"
            return step
        if response.status_code >= 400:
            step.error_code = "PROBE_HTTP_4XX"
            step.error_detail = f"upstream HTTP {response.status_code}"
            return step
        step.sample = parser(payload)
        step.ok = True
        return step
    except Exception as exc:  # noqa: BLE001 - 探针必须把任何失败转成原因码
        step.error_code, step.error_detail = _classify_failure(exc)
        return step
    finally:
        step.latency_ms = round((time.perf_counter() - started) * 1000.0, 1)


def _parse_time(payload: Any) -> dict[str, Any]:
    server_ms = _int((payload or {}).get("serverTime"))
    local_ms = _now_ms()
    return {
        "server_time_ms": server_ms,
        "local_time_ms": local_ms,
        "offset_ms": None if server_ms is None else server_ms - local_ms,
    }


def _parse_exchange_info(payload: Any) -> dict[str, Any]:
    data = payload or {}
    symbols = data.get("symbols") or []
    return {
        "symbols_total": len(symbols) if isinstance(symbols, list) else None,
        "server_timezone": data.get("timezone"),
    }


def _parse_exchange_symbol(payload: Any, symbol: str) -> dict[str, Any]:
    """从 ``exchangeInfo`` 里只取目标交易对的过滤器摘要（避免回传全量）。"""

    symbols = (payload or {}).get("symbols") or []
    match = None
    for item in symbols if isinstance(symbols, list) else []:
        if isinstance(item, dict) and item.get("symbol") == symbol:
            match = item
            break
    if match is None:
        return {"symbol": symbol, "found": False}
    filters = {
        str(item.get("filterType")): item
        for item in match.get("filters") or []
        if isinstance(item, dict)
    }
    price_filter = filters.get("PRICE_FILTER") or {}
    lot_size = filters.get("LOT_SIZE") or {}
    min_notional = filters.get("MIN_NOTIONAL") or {}
    return {
        "symbol": symbol,
        "found": True,
        "status": match.get("status"),
        "contract_type": match.get("contractType"),
        "quote_asset": match.get("quoteAsset"),
        "price_precision": match.get("pricePrecision"),
        "quantity_precision": match.get("quantityPrecision"),
        "tick_size": price_filter.get("tickSize"),
        "step_size": lot_size.get("stepSize"),
        "min_notional": min_notional.get("notional"),
    }


def _parse_klines(payload: Any, *, server_time_ms: int | None) -> dict[str, Any]:
    rows = payload if isinstance(payload, list) else []
    now_ms = server_time_ms or _now_ms()
    valid = [row for row in rows if isinstance(row, list) and len(row) >= 8]
    closed = []
    for row in valid:
        close_ms = _int(row[6])
        if close_ms is not None and close_ms <= now_ms:
            closed.append((row, close_ms))
    last_closed: dict[str, Any] | None = None
    if closed:
        row, close_ms = closed[-1]
        last_closed = {
            "open_time_ms": _int(row[0]),
            "close_time_ms": close_ms,
            "open": _num(row[1]),
            "high": _num(row[2]),
            "low": _num(row[3]),
            "close": _num(row[4]),
            "volume": _num(row[5]),
            "quote_volume": _num(row[7]),
            "trades": _int(row[8]) if len(row) > 8 else None,
            "taker_buy_quote": _num(row[10]) if len(row) > 10 else None,
            "age_s": round(max(0.0, (now_ms - close_ms) / 1000.0), 1),
        }
    return {
        "count": len(valid),
        "closed_count": len(closed),
        "has_live_bar": len(closed) < len(valid),
        "first_open_time_ms": _int(valid[0][0]) if valid else None,
        "last_closed": last_closed,
    }


def _parse_ticker(payload: Any) -> dict[str, Any]:
    data = payload or {}
    return {
        "symbol": data.get("symbol"),
        "last_price": _num(data.get("lastPrice")),
        "pct_24h": _num(data.get("priceChangePercent")),
        "high_24h": _num(data.get("highPrice")),
        "low_24h": _num(data.get("lowPrice")),
        "quote_volume_24h_usd": _num(data.get("quoteVolume")),
        "trades_24h": _int(data.get("count")),
    }


def _parse_premium(payload: Any) -> dict[str, Any]:
    data = payload or {}
    return {
        "symbol": data.get("symbol"),
        "mark_price": _num(data.get("markPrice")),
        "index_price": _num(data.get("indexPrice")),
        "funding_rate": _num(data.get("lastFundingRate")),
        "next_funding_time_ms": _int(data.get("nextFundingTime")),
    }


def _parse_open_interest(payload: Any) -> dict[str, Any]:
    data = payload or {}
    return {
        "symbol": data.get("symbol"),
        "open_interest": _num(data.get("openInterest")),
        "time_ms": _int(data.get("time")),
    }


def _build_verdict(steps: list[StepResult]) -> tuple[str, list[str]]:
    by_name = {step.name: step for step in steps}

    if any(step.status_code == 418 for step in steps):
        return "BLOCKED", ["PROBE_IP_BANNED"]

    core_ok = all(by_name[name].ok for name in CORE_STEPS)
    derivative_ok = all(by_name[name].ok for name in DERIVATIVE_STEPS)

    if core_ok and derivative_ok:
        return "GOOD", []
    if core_ok:
        return "DERIVATIVES_PARTIAL", [
            f"{by_name[name].error_code or 'PROBE_STEP_FAILED'}:{name}"
            for name in DERIVATIVE_STEPS
            if not by_name[name].ok
        ]
    if by_name[STEP_TIME].ok or by_name[STEP_EXCHANGE_INFO].ok:
        reasons = [
            f"{by_name[name].error_code or 'PROBE_STEP_FAILED'}:{name}"
            for name in CORE_STEPS
            if not by_name[name].ok
        ]
        if any(step.status_code == 429 for step in steps):
            reasons.append("PROBE_RATE_LIMITED")
        return "DEGRADED", reasons

    codes: list[str] = []
    for step in steps:
        if step.error_code and step.error_code not in codes:
            codes.append(step.error_code)
    if any(step.status_code == 429 for step in steps) and "PROBE_RATE_LIMITED" not in codes:
        codes.append("PROBE_RATE_LIMITED")
    return "BLOCKED", codes or ["PROBE_UPSTREAM_UNAVAILABLE"]


def _assert_no_credentials(client: httpx.Client) -> None:
    present = {str(key).lower() for key in client.headers}
    leaked = present.intersection(FORBIDDEN_HEADERS)
    if leaked:
        raise RuntimeError(f"probe client must not send credential headers: {sorted(leaked)}")


def probe_market(
    base_url: str,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    limit: int = 30,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    """执行一次只读探针；永不抛异常，失败写入 ``steps[].error_code``。"""

    symbol = symbol.upper()
    started = time.perf_counter()
    timeout = httpx.Timeout(timeout_s, connect=min(timeout_s, 5.0))
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        follow_redirects=False,
        headers=headers,
    ) as client:
        _assert_no_credentials(client)

        time_step = _run_step(client, STEP_TIME, "/fapi/v1/time", None, _parse_time)
        server_time_ms = time_step.sample.get("server_time_ms") if time_step.ok else None

        exchange_step = _run_step(
            client,
            STEP_EXCHANGE_INFO,
            "/fapi/v1/exchangeInfo",
            None,
            lambda payload: {
                **_parse_exchange_info(payload),
                "symbol": _parse_exchange_symbol(payload, symbol),
            },
        )
        klines_step = _run_step(
            client,
            STEP_KLINES,
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            lambda payload: _parse_klines(payload, server_time_ms=server_time_ms),
        )
        ticker_step = _run_step(
            client,
            STEP_TICKER,
            "/fapi/v1/ticker/24hr",
            {"symbol": symbol},
            _parse_ticker,
        )
        premium_step = _run_step(
            client,
            STEP_PREMIUM,
            "/fapi/v1/premiumIndex",
            {"symbol": symbol},
            _parse_premium,
        )
        oi_step = _run_step(
            client,
            STEP_OPEN_INTEREST,
            "/fapi/v1/openInterest",
            {"symbol": symbol},
            _parse_open_interest,
        )

    steps = [time_step, exchange_step, klines_step, ticker_step, premium_step, oi_step]
    verdict, degradation = _build_verdict(steps)
    total_ms = round((time.perf_counter() - started) * 1000.0, 1)
    checked_ms = _now_ms()

    return {
        "ok": verdict != "BLOCKED",
        "verdict": verdict,
        "base_url": base_url.rstrip("/"),
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "checked_at": _utc_iso(checked_ms),
        "checked_at_ms": checked_ms,
        "total_latency_ms": total_ms,
        "steps": [step.as_dict() for step in steps],
        "degradation": degradation,
        "request": {
            "method": "GET",
            "signed": False,
            "auth": "none",
            "follow_redirects": False,
            "timeout_s": timeout_s,
        },
    }
