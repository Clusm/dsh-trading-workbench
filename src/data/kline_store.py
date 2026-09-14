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


def _save_locked(key: tuple[str, str]) -> None:
    path = _path(key)
    if path is None:
        return
    payload = {"symbol": key[0], "interval": key[1], "updated_at": time.time(),
               "rows": _ROWS.get(key, [])}
    temp = path.with_name(f"{path.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


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
    fresh = bool(cached and int(cached[-1][6]) >= now_ms - 2_000)
    if len(cached) >= wanted and fresh:
        return [list(row) for row in cached[-wanted:]]

    params: dict[str, Any] = {"symbol": key[0], "interval": key[1]}
    full = len(cached) < wanted
    if full:
        # 15m是所有高周期的聚合底座；至少保留200根，覆盖完整日线组件并允许
        # 服务在任意时刻启动后立即续算1h/2h/4h/8h/1d。
        bootstrap = max(wanted, 200) if key[1] == "15m" else wanted
        params["limit"] = min(1500, bootstrap)
    else:
        interval_ms = INTERVAL_MS[key[1]]
        last_open = int(cached[-1][0])
        missing = max(2, min(100, int((now_ms - last_open) / interval_ms) + 2))
        params.update({"startTime": last_open + interval_ms, "limit": missing})

    if fetcher is None:
        rows = request_json(client, "/fapi/v1/klines", params, priority=priority)
    else:
        rows = fetcher(client, "/fapi/v1/klines", params)
    fetched = [list(row) for row in rows or [] if isinstance(row, list)]
    with _LOCK:
        current = _load_locked(key)
        _ROWS[key] = _normalize(current + fetched, keep=max(1200, wanted))
        _UPDATED_AT[key] = time.time()
        if full:
            _REST_FULL_LOADS += 1
        else:
            _REST_INCREMENTAL_LOADS += 1
        _save_locked(key)
        return [list(row) for row in _ROWS[key][-wanted:]]


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
            str(sum(float(row[7] or 0) for row in components)),
            sum(int(row[8] or 0) for row in components),
            str(sum(float(row[9] or 0) for row in components)),
            str(sum(float(row[10] or 0) for row in components)),
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
