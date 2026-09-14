"""闭合K线优先的增量缓存与可选磁盘恢复。

首次使用按调用方要求回补历史；此后只请求缺失的少量K线。WebSocket kline 帧会
直接更新同一缓存，因此流稳定时常规策略扫描不再产生K线 REST 请求。
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

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
_BAR_META: dict[tuple[str, str], dict[int, "BarMeta"]] = {}
_UPDATED_AT: dict[tuple[str, str], float] = {}
_CACHE_DIR: Path | None = None
_REST_FULL_LOADS = 0
_REST_INCREMENTAL_LOADS = 0
_WS_UPDATES = 0
_AGGREGATED_UPDATES = 0
_AGGREGATED_INTERVALS = {"1h", "2h", "4h", "8h", "1d"}
_FRESHNESS_TTL_CAP_SECONDS = 300.0
_CACHE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class BarMeta:
    """单根K线的来源和最终性证据。

    ``final`` 使用三态：``True`` 表示来源明确确认收盘，``False`` 表示明确仍在
    形成，``None`` 表示旧缓存或外部输入没有提供最终性证据。不能把 ``None`` 当作
    ``True``，否则服务重启后墙钟跨过收盘时间就会把陈旧bar误当成可交易数据。
    """

    source: str
    event_time_ms: int | None
    received_at_ms: int
    final: bool | None
    complete: bool
    revision: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "event_time_ms": self.event_time_ms,
            "received_at_ms": self.received_at_ms,
            "final": self.final,
            "complete": self.complete,
            "revision": self.revision,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], *, fallback_received_at_ms: int) -> "BarMeta":
        raw_final = payload.get("final")
        final = None if raw_final is None else bool(raw_final)
        try:
            event_time = payload.get("event_time_ms")
            event_time_ms = int(event_time) if event_time is not None else None
        except (TypeError, ValueError):
            event_time_ms = None
        try:
            received = int(payload.get("received_at_ms") or fallback_received_at_ms)
        except (TypeError, ValueError):
            received = fallback_received_at_ms
        try:
            revision = max(0, int(payload.get("revision") or 0))
        except (TypeError, ValueError):
            revision = 0
        return cls(
            source=str(payload.get("source") or "legacy_cache"),
            event_time_ms=event_time_ms,
            received_at_ms=max(0, received),
            final=final,
            complete=bool(payload.get("complete")),
            revision=revision,
        )


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
        _BAR_META.setdefault(key, {})
        return _ROWS[key]
    rows: list[list[Any]] = []
    stored_meta: dict[int, BarMeta] = {}
    path = _path(key)
    if path and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = [list(row) for row in payload.get("rows", []) if isinstance(row, list)]
            updated_at = payload.get("updated_at")
            if updated_at is not None:
                _UPDATED_AT[key] = float(updated_at)
            fallback_received = int(float(updated_at) * 1000) if updated_at is not None else 0
            raw_meta = payload.get("bar_meta")
            if isinstance(raw_meta, dict):
                for raw_open, value in raw_meta.items():
                    if not isinstance(value, dict):
                        continue
                    try:
                        open_ms = int(raw_open)
                    except (TypeError, ValueError):
                        continue
                    stored_meta[open_ms] = BarMeta.from_dict(
                        value, fallback_received_at_ms=fallback_received,
                    )
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            rows = []
            stored_meta = {}
    rows = _normalize(rows)
    _ROWS[key] = rows
    _BAR_META[key] = {}
    # schema_version=1或手工写入的旧缓存没有逐bar证据，全部保守标为UNKNOWN。
    # 只要后续收到可信REST/WS数据，该bar就会被逐根升级。
    fallback_received = int(_UPDATED_AT.get(key, 0.0) * 1000)
    for row in rows:
        try:
            open_ms = int(row[0])
        except (TypeError, ValueError, IndexError):
            continue
        _BAR_META[key][open_ms] = stored_meta.get(
            open_ms,
            BarMeta(
                source="legacy_cache", event_time_ms=None,
                received_at_ms=fallback_received, final=None,
                complete=False, revision=0,
            ),
        )
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


def _base_fields_complete(row: list[Any]) -> bool:
    """指标层所需字段完整；扩展成交字段由聚合层单独检查。"""
    if not isinstance(row, (list, tuple)) or len(row) < 7:
        return False
    try:
        int(row[0])
        int(row[6])
        for index in (1, 2, 3, 4, 5):
            value = row[index]
            if value in (None, ""):
                return False
            float(value)
    except (TypeError, ValueError, IndexError):
        return False
    return True


def _aggregate_fields_complete(row: list[Any]) -> bool:
    """聚合需要的OHLCV、成交额、成交笔数和主动买卖字段均可解析。"""
    if not _base_fields_complete(row) or len(row) < 11:
        return False
    try:
        for index in (7, 8, 9, 10):
            value = row[index]
            if value in (None, ""):
                return False
            float(value)
    except (TypeError, ValueError, IndexError):
        return False
    return True


def _unknown_meta(received_at_ms: int = 0) -> BarMeta:
    return BarMeta(
        source="legacy_cache", event_time_ms=None,
        received_at_ms=max(0, int(received_at_ms)), final=None,
        complete=False, revision=0,
    )


def _coerce_meta(value: Any) -> BarMeta | None:
    if isinstance(value, BarMeta):
        return value
    if isinstance(value, Mapping):
        try:
            return BarMeta.from_dict(value, fallback_received_at_ms=0)
        except (TypeError, ValueError):
            return None
    return None


def _lookup_meta(bar_metadata: Mapping[Any, Any] | None, open_ms: int) -> BarMeta | None:
    if bar_metadata is None:
        return None
    value = bar_metadata.get(open_ms)
    if value is None:
        value = bar_metadata.get(str(open_ms))
    return _coerce_meta(value)


def _prune_meta_locked(key: tuple[str, str]) -> None:
    opens = {int(row[0]) for row in _ROWS.get(key, []) if row and row[0] is not None}
    _BAR_META[key] = {
        open_ms: meta for open_ms, meta in _BAR_META.get(key, {}).items()
        if open_ms in opens
    }


_SOURCE_PRIORITY = {
    "ws": 30,
    "rest": 20,
    "aggregate_15m": 10,
    "legacy_cache": 0,
    "unknown": 0,
}


def _merge_row_locked(key: tuple[str, str], row: list[Any], meta: BarMeta) -> bool:
    """按最终性、事件时间和来源优先级合并一根bar，返回是否改变了真值。"""
    try:
        open_ms = int(row[0])
    except (TypeError, ValueError, IndexError):
        return False
    rows = _ROWS.setdefault(key, [])
    metadata = _BAR_META.setdefault(key, {})
    existing_index = next(
        (index for index, item in enumerate(rows)
         if item and str(item[0]) == str(open_ms)),
        None,
    )
    if existing_index is None:
        rows.append(list(row))
        metadata[open_ms] = meta
        return True

    existing_row = rows[existing_index]
    existing_meta = metadata.get(open_ms) or _unknown_meta()
    # 已定稿bar不能被迟到的未定稿帧回退。最终帧是明确的证据升级。
    if existing_meta.final is True and meta.final is not True:
        return False
    if (existing_meta.final is not True and meta.final is not True
            and existing_meta.event_time_ms is not None
            and meta.event_time_ms is not None
            and meta.event_time_ms < existing_meta.event_time_ms):
        return False
    if (existing_meta.final is True and meta.final is True
            and existing_meta.event_time_ms is not None
            and meta.event_time_ms is not None
            and meta.event_time_ms < existing_meta.event_time_ms):
        return False
    if (existing_meta.final is True and meta.final is True
            and existing_meta.source != meta.source
            and _SOURCE_PRIORITY.get(meta.source, 0) <
            _SOURCE_PRIORITY.get(existing_meta.source, 0)
            and meta.event_time_ms <= (existing_meta.event_time_ms or 0)):
        return False

    row_changed = list(existing_row) != list(row)
    meta_changed = (
        existing_meta.final != meta.final
        or existing_meta.complete != meta.complete
        or existing_meta.source != meta.source
        or existing_meta.event_time_ms != meta.event_time_ms
    )
    if not row_changed and not meta_changed:
        return False
    revision = max(existing_meta.revision, 0) + 1
    rows[existing_index] = list(row)
    metadata[open_ms] = BarMeta(
        source=meta.source,
        event_time_ms=meta.event_time_ms,
        received_at_ms=meta.received_at_ms,
        final=meta.final,
        complete=meta.complete,
        revision=revision,
    )
    return True


def _ingest_rows_locked(key: tuple[str, str], rows: list[list[Any]], *, source: str,
                        received_at_ms: int,
                        final_resolver: Callable[[list[Any], int], bool | None],
                        event_resolver: Callable[[list[Any]], int | None]) -> bool:
    _load_locked(key)
    changed = False
    for row in rows:
        if not isinstance(row, list) or not _base_fields_complete(row):
            continue
        try:
            int(row[0])
        except (TypeError, ValueError, IndexError):
            continue
        meta = BarMeta(
            source=source,
            event_time_ms=event_resolver(row),
            received_at_ms=received_at_ms,
            final=final_resolver(row, received_at_ms),
            complete=_base_fields_complete(row),
            revision=1,
        )
        changed = _merge_row_locked(key, row, meta) or changed
    _ROWS[key] = _normalize(_ROWS[key])
    _prune_meta_locked(key)
    return changed


def _metadata_ready_locked(key: tuple[str, str], rows: list[list[Any]], now_ms: int) -> bool:
    """判断缓存是否已为请求窗口提供逐根证据；当前形成中的bar允许final=False。"""
    _load_locked(key)
    metadata = _BAR_META.get(key, {})
    if not rows:
        return False
    for row in rows:
        try:
            open_ms = int(row[0])
            close_ms = int(row[6])
        except (TypeError, ValueError, IndexError):
            return False
        meta = metadata.get(open_ms)
        if meta is None or not meta.complete:
            return False
        if close_ms <= now_ms and meta.final is not True:
            return False
    return True


def get_bar_metadata(symbol: str, interval: str,
                     rows: list[list[Any]] | None = None) -> dict[int, BarMeta]:
    """返回指定序列的逐bar元数据快照，供所有指标入口共享闭合投影。"""
    key = _key(symbol, interval)
    with _LOCK:
        stored_rows = _load_locked(key)
        selected = rows if rows is not None else stored_rows
        metadata = _BAR_META.get(key, {})
        result: dict[int, BarMeta] = {}
        for row in selected or []:
            try:
                open_ms = int(row[0])
            except (TypeError, ValueError, IndexError):
                continue
            result[open_ms] = metadata.get(open_ms) or _unknown_meta()
        return result


def _cell(row: list[Any], index: int, default: Any = 0) -> Any:
    """兼容只含标准OHLCV字段的历史夹具；生产响应通常包含完整12列。"""
    if index >= len(row) or row[index] in (None, ""):
        return default
    return row[index]


def _save_locked(key: tuple[str, str]) -> None:
    path = _path(key)
    if path is None:
        return
    _prune_meta_locked(key)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "symbol": key[0], "interval": key[1],
        "updated_at": _UPDATED_AT.get(key, time.time()),
        "rows": _ROWS.get(key, []),
        "bar_meta": {
            str(open_ms): meta.as_dict()
            for open_ms, meta in _BAR_META.get(key, {}).items()
        },
    }
    temp = path.with_name(f"{path.stem}.tmp-{os.getpid()}-{threading.get_ident()}")
    temp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


def validate_kline_rows(rows: list[list[Any]] | None, interval: str, *,
                        observed_at_ms: int | None = None,
                        received_at_ms: int | None = None,
                        min_rows: int = 2,
                        require_fresh: bool = False,
                        bar_metadata: Mapping[Any, Any] | None = None,
                        require_finality: bool = False) -> dict[str, Any]:
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
    unknown_finality = 0
    nonfinal_closed = 0
    incomplete_closed = 0
    final_closed = 0
    if bar_metadata is not None:
        for row in valid:
            try:
                open_ms = int(row[0])
                close_ms = int(row[6])
            except (TypeError, ValueError, IndexError):
                continue
            meta = _lookup_meta(bar_metadata, open_ms)
            if meta is None or meta.final is None:
                unknown_finality += 1
            elif close_ms <= now_ms and meta.final is not True:
                nonfinal_closed += 1
            elif close_ms <= now_ms and not meta.complete:
                incomplete_closed += 1
            elif close_ms <= now_ms and meta.final is True:
                final_closed += 1
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
    if require_finality:
        if unknown_finality:
            issues.append("kline_finality_unknown")
        if nonfinal_closed:
            issues.append("nonfinal_closed_kline")
        if incomplete_closed:
            issues.append("incomplete_closed_kline")
        if final_closed < max(1, int(min_rows)):
            issues.append("insufficient_final_klines")
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
        "finality_metadata_present": bar_metadata is not None,
        "final_closed_count": final_closed,
        "unknown_finality_count": unknown_finality,
        "nonfinal_closed_count": nonfinal_closed,
        "incomplete_closed_count": incomplete_closed,
        "finality_ok": not (unknown_finality or nonfinal_closed or incomplete_closed),
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
        metadata_ready = _metadata_ready_locked(key, cached[-wanted:], now_ms)
    quality = validate_kline_rows(
        cached, key[1], observed_at_ms=now_ms,
        received_at_ms=received_at_ms, min_rows=wanted, require_fresh=True,
    )
    if len(cached) >= wanted and quality["ok"] and metadata_ready:
        result = [list(row) for row in cached[-wanted:]]
        if key[1] in _AGGREGATED_INTERVALS:
            _ensure_aggregation_base(
                client, key[0], priority=priority, fetcher=fetcher,
            )
        return result

    params: dict[str, Any] = {"symbol": key[0], "interval": key[1]}
    full = (len(cached) < wanted or not quality["continuity_ok"]
            or not metadata_ready)
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
    received_at_ms = int(time.time() * 1000)
    with _LOCK:
        _load_locked(key)
        _ingest_rows_locked(
            key, fetched, source="rest", received_at_ms=received_at_ms,
            final_resolver=lambda row, received: (
                int(row[6]) <= received if len(row) > 6 else None
            ),
            event_resolver=lambda row: int(row[6]) if len(row) > 6 else None,
        )
        _ROWS[key] = _normalize(_ROWS[key], keep=max(1200, wanted))
        _prune_meta_locked(key)
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
    received_at_ms = int(time.time() * 1000)
    event_time_ms = payload.get("E") or body.get("E") or body.get("T")
    try:
        event_time_ms = int(event_time_ms) if event_time_ms is not None else None
    except (TypeError, ValueError):
        event_time_ms = None
    with _LOCK:
        _load_locked(key)
        _ingest_rows_locked(
            key, [row], source="ws", received_at_ms=received_at_ms,
            final_resolver=lambda _row, _received: bool(body.get("x"))
            if "x" in body else None,
            event_resolver=lambda _row: event_time_ms,
        )
        _ROWS[key] = _normalize(_ROWS[key])
        _prune_meta_locked(key)
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
        opens = sorted(int(row[0]) for row in components)
        expected_opens = [bucket_open + i * INTERVAL_MS["15m"]
                          for i in range(expected)]
        contiguous = bool(opens and opens == expected_opens[:len(opens)])
        if (not components or not contiguous
                or any(not _aggregate_fields_complete(row) for row in components)):
            continue
        metadata = _BAR_META.get(base_key, {})
        component_meta = [metadata.get(int(row[0])) for row in components]
        if any(meta is None for meta in component_meta):
            continue
        all_final = len(components) == expected and all(
            meta.final is True for meta in component_meta if meta is not None
        )
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
        aggregate_meta = BarMeta(
            source="aggregate_15m",
            event_time_ms=max((meta.event_time_ms or 0) for meta in component_meta),
            received_at_ms=now_ms,
            final=all_final,
            complete=_aggregate_fields_complete(aggregate),
            revision=1,
        )
        changed = _merge_row_locked(target_key, aggregate, aggregate_meta)
        _ROWS[target_key] = _normalize(_ROWS[target_key])
        _prune_meta_locked(target_key)
        if changed:
            _UPDATED_AT[target_key] = time.time()
            _AGGREGATED_UPDATES += 1
        if all_final and changed:
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
            "finality_metadata": sum(len(meta) for meta in _BAR_META.values()),
            "unknown_bars": sum(
                1 for meta in _BAR_META.values()
                for item in meta.values() if item.final is None
            ),
        }


def _reset_for_test() -> None:
    global _CACHE_DIR, _REST_FULL_LOADS, _REST_INCREMENTAL_LOADS, _WS_UPDATES
    global _AGGREGATED_UPDATES
    with _LOCK:
        _ROWS.clear()
        _BAR_META.clear()
        _UPDATED_AT.clear()
        _CACHE_DIR = None
        _REST_FULL_LOADS = 0
        _REST_INCREMENTAL_LOADS = 0
        _WS_UPDATES = 0
        _AGGREGATED_UPDATES = 0
