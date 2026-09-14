"""闭合K线优先的增量缓存与可选磁盘恢复。

首次使用按调用方要求回补历史；此后只请求缺失的少量K线。WebSocket kline 帧会
直接更新同一缓存，因此流稳定时常规策略扫描不再产生K线 REST 请求。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from data.binance_transport import request_json

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "1d": 86_400_000,
}

_LOCK = threading.RLock()
_ROWS: dict[tuple[str, str], list[list[Any]]] = {}
_UPDATED_AT: dict[tuple[str, str], float] = {}
_CACHE_DIR: Path | None = None
_REST_FULL_LOADS = 0
_REST_INCREMENTAL_LOADS = 0
_WS_UPDATES = 0
_AGGREGATED_UPDATES = 0
_AGGREGATED_INTERVALS = {"1h", "2h", "4h", "8h", "1d"}
_FRESHNESS_TTL_CAP_SECONDS = 300.0


class KlineDataQualityError(RuntimeError):
    """K线已返回但结构/连续性/覆盖范围不足，调用方应跳过本轮。"""

    def __init__(self, message: str, quality: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.quality = dict(quality or {})
        self.reason_code = str(self.quality.get("reason_code") or "KLINE_INVALID")


def configure_kline_store(cache_dir: Path) -> None:
    global _CACHE_DIR
    with _LOCK:
        _CACHE_DIR = Path(cache_dir)
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _key(symbol: str, interval: str) -> tuple[str, str]:
    normalized = str(interval)
    if normalized not in INTERVAL_MS:
        raise ValueError(f"不支持的K线周期: {interval}")
    return str(symbol).upper(), normalized


def _path(key: tuple[str, str]) -> Path | None:
    if _CACHE_DIR is None:
        return None
    return _CACHE_DIR / f"{key[0]}_{key[1]}.json"


def _load_locked(key: tuple[str, str]) -> list[list[Any]]:
    if key in _ROWS:
        return _ROWS[key]
    rows: list[list[Any]] = []
    path = _path(key)
    if path and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = [list(row) for row in payload.get("rows", []) if isinstance(row, list)]
            updated_at = payload.get("updated_at")
            if updated_at is not None:
                _UPDATED_AT[key] = float(updated_at)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            rows = []
    rows = _normalize(rows)
    _ROWS[key] = rows
    return rows


def _normalize(rows: list[list[Any]], keep: int = 1200) -> list[list[Any]]:
    by_open: dict[int, list[Any]] = {}
    for row in rows:
        try:
            if len(row) >= 7:
                by_open[int(row[0])] = list(row)
        except (TypeError, ValueError):
            continue
    return [by_open[k] for k in sorted(by_open)[-max(1, keep):]]


def _cell(row: list[Any], index: int, default: Any = 0) -> Any:
    """兼容只含标准OHLCV字段的历史夹具；生产响应通常包含完整12列。"""
    if index >= len(row) or row[index] in (None, ""):
        return default
    return row[index]


def _save_locked(key: tuple[str, str]) -> None:
    path = _path(key)
    if path is None:
        return
    payload = {"symbol": key[0], "interval": key[1],
               "updated_at": _UPDATED_AT.get(key, time.time()),
               "rows": _ROWS.get(key, [])}
    temp = path.with_name(f"{path.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def validate_kline_rows(rows: list[list[Any]] | None, interval: str, *,
                        observed_at_ms: int | None = None,
                        received_at_ms: int | None = None,
                        min_rows: int = 2,
                        require_fresh: bool = False) -> dict[str, Any]:
    """校验 K 线结构、连续性、覆盖范围和缓存接收时效。

    ``close_time`` 只能说明一根 bar 的理论结束时间，不能说明缓存何时收到最后
    一次更新。因此缓存命中必须同时检查 ``received_at_ms``；这会淘汰服务重启后
    留在磁盘里的陈旧“当前 bar”，避免它被误当成新鲜行情。
    """
    step = INTERVAL_MS.get(str(interval))
    if step is None:
        raise ValueError(f"不支持的K线周期: {interval}")
    now_ms = int(observed_at_ms if observed_at_ms is not None else time.time() * 1000)
    valid: list[list[Any]] = []
    invalid_rows = 0
    opens: list[int] = []
    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            invalid_rows += 1
            continue
        try:
            open_ms = int(row[0])
            int(row[6])
            for index in (1, 2, 3, 4, 5):
                float(row[index])
        except (TypeError, ValueError, IndexError):
            invalid_rows += 1
            continue
        valid.append(list(row))
        opens.append(open_ms)

    non_monotonic = any(later <= earlier for earlier, later in zip(opens, opens[1:]))
    duplicate = len(set(opens)) != len(opens)
    sorted_opens = sorted(set(opens))
    gap = any(later - earlier != step
              for earlier, later in zip(sorted_opens, sorted_opens[1:]))
    latest_open = opens[-1] if opens else None
    latest_close = None
    if valid:
        try:
            latest_close = int(valid[-1][6])
        except (TypeError, ValueError):
            latest_close = None
    current_open = (now_ms // step) * step
    coverage_ok = bool(latest_open is not None and latest_open >= current_open - step)
    receive_age_seconds = None
    if received_at_ms is not None:
        receive_age_seconds = max(0.0, (now_ms - int(received_at_ms)) / 1000.0)
    interval_seconds = step / 1000.0
    freshness_ttl = max(10.0, min(_FRESHNESS_TTL_CAP_SECONDS, interval_seconds))
    fresh = bool(
        received_at_ms is not None
        and receive_age_seconds is not None
        and receive_age_seconds <= freshness_ttl
        and coverage_ok
        and latest_close is not None
        and latest_close >= now_ms - step - 5_000
    )
    issues: list[str] = []
    if invalid_rows:
        issues.append("invalid_kline_rows")
    if duplicate:
        issues.append("duplicate_kline_open")
    if non_monotonic:
        issues.append("non_monotonic_klines")
    if gap:
        issues.append("kline_gap")
    if len(valid) < max(1, int(min_rows)):
        issues.append("insufficient_klines")
    if not coverage_ok:
        issues.append("kline_coverage_gap")
    if require_fresh and not fresh:
        issues.append("stale_kline_cache")
    return {
        "interval": str(interval),
        "observed_at_ms": now_ms,
        "received_at_ms": int(received_at_ms) if received_at_ms is not None else None,
        "receive_age_seconds": round(receive_age_seconds, 3)
        if receive_age_seconds is not None else None,
        "latest_open_ms": latest_open,
        "latest_close_ms": latest_close,
        "current_open_ms": current_open,
        "count": len(valid),
        "invalid_rows": invalid_rows,
        "duplicate_open": duplicate,
        "non_monotonic": non_monotonic,
        "gap": gap,
        "coverage_ok": coverage_ok,
        "fresh": fresh,
        "continuity_ok": not (invalid_rows or duplicate or non_monotonic or gap),
        "ok": not issues,
        "issues": issues,
    }


def get_klines(client: Any, symbol: str, interval: str, limit: int,
               *, priority: str = "normal",
               fetcher: Callable[..., Any] | None = None) -> list[list[Any]]:
    """返回连续K线；缓存充足且包含当前形成中的bar时完全不访问REST。"""
    global _REST_FULL_LOADS, _REST_INCREMENTAL_LOADS
    key = _key(symbol, interval)
    wanted = max(2, int(limit))
    with _LOCK:
        cached = list(_load_locked(key))

    now_ms = int(time.time() * 1000)
    with _LOCK:
        received_at_ms = (int(_UPDATED_AT[key] * 1000)
                          if key in _UPDATED_AT else None)
    quality = validate_kline_rows(
        cached, key[1], observed_at_ms=now_ms,
        received_at_ms=received_at_ms, min_rows=wanted, require_fresh=True,
    )
    if len(cached) >= wanted and quality["ok"]:
        result = [list(row) for row in cached[-wanted:]]
        if key[1] in _AGGREGATED_INTERVALS:
            _ensure_aggregation_base(
                client, key[0], priority=priority, fetcher=fetcher,
            )
        return result

    params: dict[str, Any] = {"symbol": key[0], "interval": key[1]}
    full = len(cached) < wanted or not quality["continuity_ok"]
    if full:
        # 15m是所有高周期的聚合底座；至少保留200根，覆盖完整日线组件并允许
        # 服务在任意时刻启动后立即续算1h/2h/4h/8h/1d。
        bootstrap = max(wanted, 200) if key[1] == "15m" else wanted
        params["limit"] = min(1500, bootstrap)
    else:
        interval_ms = INTERVAL_MS[key[1]]
        last_open = int(cached[-1][0])
        missing = max(2, min(100, int((now_ms - last_open) / interval_ms) + 2))
        # 必须从最后一根 open 开始，刷新正在形成的 bar；跳到下一根会永久保留
        # 旧 close/high/low/volume，扫描器随后只能看到陈旧的当前 bar。
        if int((now_ms - last_open) / interval_ms) + 2 > 100:
            full = True
            bootstrap = max(wanted, 200) if key[1] == "15m" else wanted
            params = {"symbol": key[0], "interval": key[1],
                      "limit": min(1500, bootstrap)}
        else:
            params.update({"startTime": last_open, "limit": missing})

    if fetcher is None:
        rows = request_json(client, "/fapi/v1/klines", params, priority=priority)
    else:
        rows = fetcher(client, "/fapi/v1/klines", params)
    fetched = [list(row) for row in rows or [] if isinstance(row, list)]
    with _LOCK:
        current = _load_locked(key)
        _ROWS[key] = _normalize(current + fetched, keep=max(1200, wanted))
        if fetched:
            _UPDATED_AT[key] = time.time()
            if full:
                _REST_FULL_LOADS += 1
            else:
                _REST_INCREMENTAL_LOADS += 1
        _save_locked(key)
        result = [list(row) for row in _ROWS[key][-wanted:]]

    # 高周期首次回补成功后，再用一笔受预算控制的15m请求建立增量聚合底座。
    # 底座失败不能推翻已经取得的目标周期数据；429/418 已由统一网关登记熔断。
    if key[1] in _AGGREGATED_INTERVALS:
        _ensure_aggregation_base(
            client, key[0], priority=priority, fetcher=fetcher,
        )
    return result


def _ensure_aggregation_base(client: Any, symbol: str, *, priority: str,
                             fetcher: Callable[..., Any] | None) -> None:
    """惰性建立15m历史底座，并立即生成当前高周期bar。"""
    try:
        base_rows = get_klines(
            client, symbol, "15m", 200, priority=priority, fetcher=fetcher,
        )
    except Exception:
        return
    if not base_rows:
        return
    with _LOCK:
        _aggregate_higher_timeframes_locked(symbol, base_rows[-1])


def push_kline_event(payload: dict[str, Any]) -> None:
    """接收 Binance kline 事件；闭合bar会立即持久化。"""
    global _WS_UPDATES
    body = payload.get("k") if isinstance(payload.get("k"), dict) else payload
    symbol = str(body.get("s") or payload.get("s") or "").upper()
    interval = str(body.get("i") or "")
    if not symbol or interval not in INTERVAL_MS:
        return
    row = [
        body.get("t"), body.get("o"), body.get("h"), body.get("l"), body.get("c"),
        body.get("v"), body.get("T"), body.get("q"), body.get("n"), body.get("V"),
        body.get("Q"), body.get("B") or "0",
    ]
    key = _key(symbol, interval)
    with _LOCK:
        current = _load_locked(key)
        _ROWS[key] = _normalize(current + [row])
        _UPDATED_AT[key] = time.time()
        _WS_UPDATES += 1
        if interval == "15m":
            _aggregate_higher_timeframes_locked(symbol, row)
        if body.get("x") is True:
            _save_locked(key)


def _aggregate_higher_timeframes_locked(symbol: str, latest: list[Any]) -> None:
    """由15m组件更新高周期当前bar；闭合bar只有组件完整时才覆盖REST真值。"""
    global _AGGREGATED_UPDATES
    base_key = _key(symbol, "15m")
    source = _load_locked(base_key)
    now_ms = int(time.time() * 1000)
    latest_open = int(latest[0])
    for interval in ("1h", "2h", "4h", "8h", "1d"):
        target_ms = INTERVAL_MS[interval]
        bucket_open = latest_open // target_ms * target_ms
        bucket_close = bucket_open + target_ms - 1
        components = [row for row in source if bucket_open <= int(row[0]) <= bucket_close]
        expected = target_ms // INTERVAL_MS["15m"]
        is_live = bucket_close >= now_ms
        opens = sorted(int(row[0]) for row in components)
        contiguous = bool(opens and opens == [bucket_open + i * INTERVAL_MS["15m"]
                                              for i in range(len(opens))])
        if not components or not contiguous or (not is_live and len(components) < expected):
            continue
        first, last = components[0], components[-1]
        aggregate = [
            bucket_open,
            first[1],
            str(max(float(row[2]) for row in components)),
            str(min(float(row[3]) for row in components)),
            last[4],
            str(sum(float(row[5]) for row in components)),
            bucket_close,
            str(sum(float(_cell(row, 7)) for row in components)),
            sum(int(_cell(row, 8)) for row in components),
            str(sum(float(_cell(row, 9)) for row in components)),
            str(sum(float(_cell(row, 10)) for row in components)),
            "0",
        ]
        target_key = _key(symbol, interval)
        _ROWS[target_key] = _normalize(_load_locked(target_key) + [aggregate])
        _UPDATED_AT[target_key] = time.time()
        _AGGREGATED_UPDATES += 1
        if not is_live:
            _save_locked(target_key)


def kline_store_status() -> dict[str, Any]:
    with _LOCK:
        newest = max(_UPDATED_AT.values(), default=None)
        return {
            "series": len(_ROWS),
            "rows": sum(len(rows) for rows in _ROWS.values()),
            "rest_full_loads": _REST_FULL_LOADS,
            "rest_incremental_loads": _REST_INCREMENTAL_LOADS,
            "websocket_updates": _WS_UPDATES,
            "aggregated_updates": _AGGREGATED_UPDATES,
            "last_update_at": newest,
        }


def _reset_for_test() -> None:
    global _CACHE_DIR, _REST_FULL_LOADS, _REST_INCREMENTAL_LOADS, _WS_UPDATES
    global _AGGREGATED_UPDATES
    with _LOCK:
        _ROWS.clear()
        _UPDATED_AT.clear()
        _CACHE_DIR = None
        _REST_FULL_LOADS = 0
        _REST_INCREMENTAL_LOADS = 0
        _WS_UPDATES = 0
        _AGGREGATED_UPDATES = 0
