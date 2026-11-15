"""闭合 K 线缓存、warmup 与本地聚合（docs/04 §6.1 / §7.3）。

规则：
- **只用闭合 bar**：``close_time_ms <= now_ms``；正在形成的 bar 只记录 ``live_bar_present``，
  永不进入指标或证据；
- 每 (symbol, interval) 一条 ring buffer（最多 1200 根），REST 增量拉取；
- 15m 可本地聚合出 1h/4h/1d；聚合只接受"完整且全部 final"的桶，不插值；
- 缺口/乱序/不足只报告 issue，不修补。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import config as C
from .transport import MarketTransport, MarketUpstreamError

logger = logging.getLogger("workbench.market.klines")


@dataclass(frozen=True, slots=True)
class Bar:
    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float
    final: bool
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "open_time_ms": self.open_time_ms,
            "close_time_ms": self.close_time_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "trades": self.trades,
            "taker_buy_base": self.taker_buy_base,
            "taker_buy_quote": self.taker_buy_quote,
            "final": self.final,
            "source": self.source,
        }


@dataclass(slots=True)
class KlineResult:
    symbol: str
    interval: str
    bars: tuple[Bar, ...]
    meta: dict[str, Any]
    issues: list[str]

    @property
    def last(self) -> Bar | None:
        return self.bars[-1] if self.bars else None


@dataclass(slots=True)
class _Series:
    bars: list[Bar]
    received_at: float
    live_bar_present: bool


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_kline_row(
    row: Iterable[Any], *, source: str = "rest", now_ms: int | None = None
) -> Bar | None:
    cells = list(row)
    if len(cells) < 8:
        return None
    try:
        open_time = int(cells[0])
        close_time = int(cells[6])
    except (TypeError, ValueError):
        return None
    effective_now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    return Bar(
        open_time_ms=open_time,
        close_time_ms=close_time,
        open=_number(cells[1]),
        high=_number(cells[2]),
        low=_number(cells[3]),
        close=_number(cells[4]),
        volume=_number(cells[5]),
        quote_volume=_number(cells[7]),
        trades=_integer(cells[8]) if len(cells) > 8 else 0,
        taker_buy_base=_number(cells[9]) if len(cells) > 9 else 0.0,
        taker_buy_quote=_number(cells[10]) if len(cells) > 10 else 0.0,
        final=close_time <= effective_now,
        source=source,
    )


def aggregate_closed(
    bars: Iterable[Bar],
    target_interval: str,
    limit: int,
    *,
    source: str = "aggregate_15m",
) -> list[Bar]:
    """把 15m 闭合 bar 聚合为 1h/4h/1d；只产出完整且全部 final 的桶。"""

    target_ms = C.INTERVAL_MS.get(target_interval)
    base_ms = C.INTERVAL_MS["15m"]
    if target_ms is None or target_interval == "15m":
        raise ValueError(f"unsupported aggregate target: {target_interval}")
    expected = target_ms // base_ms
    buckets: dict[int, list[Bar]] = {}
    for bar in bars:
        if not bar.final:
            continue
        bucket = bar.open_time_ms // target_ms * target_ms
        buckets.setdefault(bucket, []).append(bar)

    aggregated: list[Bar] = []
    for bucket in sorted(buckets):
        components = sorted(buckets[bucket], key=lambda item: item.open_time_ms)
        if len(components) != expected:
            continue
        if any(
            component.open_time_ms != bucket + index * base_ms
            for index, component in enumerate(components)
        ):
            continue
        aggregated.append(
            Bar(
                open_time_ms=bucket,
                close_time_ms=bucket + target_ms - 1,
                open=components[0].open,
                high=max(item.high for item in components),
                low=min(item.low for item in components),
                close=components[-1].close,
                volume=sum(item.volume for item in components),
                quote_volume=sum(item.quote_volume for item in components),
                trades=sum(item.trades for item in components),
                taker_buy_base=sum(item.taker_buy_base for item in components),
                taker_buy_quote=sum(item.taker_buy_quote for item in components),
                final=True,
                source=source,
            )
        )
    return aggregated[-max(1, int(limit)) :]


def _series_issues(
    bars: list[Bar],
    interval: str,
    needed: int,
    now_ms: int,
    *,
    gap: bool,
    non_monotonic: bool,
) -> list[str]:
    issues: list[str] = []
    if len(bars) < needed:
        issues.append("KLINE_INSUFFICIENT")
    if not bars:
        return issues
    if gap:
        issues.append("KLINE_GAP")
    if non_monotonic:
        issues.append("KLINE_STRUCTURE_INVALID")
    fresh_s, unusable_s = C.FRESHNESS_S[interval]
    age_s = max(0.0, (now_ms - bars[-1].close_time_ms) / 1000.0)
    if age_s > unusable_s:
        issues.append("KLINE_STALE")
    return issues


class KlineStore:
    def __init__(
        self,
        transport: MarketTransport,
        *,
        now_fn: Callable[[], int] | None = None,
    ) -> None:
        self.transport = transport
        self._now_fn = now_fn or (lambda: int(time.time() * 1000))
        self._series: dict[tuple[str, str], _Series] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ public
    def get_closed(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        now_ms: int | None = None,
    ) -> KlineResult:
        symbol = symbol.upper()
        if interval not in C.INTERVAL_MS:
            raise ValueError(f"unsupported interval: {interval}")
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = int(now_ms if now_ms is not None else self._now_fn())
        needed = max(int(limit), C.WARMUP_BARS.get(interval, 200))
        cached = self._get_series(symbol, interval)

        if cached is not None and self._cache_usable(cached, interval, needed, now):
            return self._result(symbol, interval, cached.bars, needed, now, cached.live_bar_present)

        fetch_limit, start_time = self._plan_fetch(cached, interval, needed, now)
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": fetch_limit}
        if start_time is not None:
            params["startTime"] = start_time
        logger.info(
            "kline_fetch symbol=%s interval=%s limit=%s incremental=%s",
            symbol,
            interval,
            fetch_limit,
            start_time is not None,
        )
        payload = self.transport.get_json(
            "/fapi/v1/klines",
            params,
            weight=C.klines_weight(fetch_limit),
            purpose="market_klines",
            group="general",
        )
        fetched = payload if isinstance(payload, list) else []
        if not fetched and cached is None:
            raise MarketUpstreamError(200, "MARKET_EMPTY_KLINES", f"empty klines for {symbol} {interval}")

        received_at = time.time()
        with self._lock:
            existing = self._series.get((symbol, interval))
            by_open: dict[int, Bar] = {bar.open_time_ms: bar for bar in existing.bars} if existing else {}
            live_present = existing.live_bar_present if existing else False
            for row in fetched:
                bar = parse_kline_row(row, source="rest", now_ms=now)
                if bar is None:
                    continue
                if bar.close_time_ms > now:
                    live_present = True
                    continue
                by_open[bar.open_time_ms] = bar
            merged = [by_open[key] for key in sorted(by_open)][-C.MAX_BARS_PER_SERIES :]
            self._series[(symbol, interval)] = _Series(
                bars=merged, received_at=received_at, live_bar_present=live_present
            )
        return self._result(symbol, interval, merged, needed, now, live_present)

    def get_with_fallback(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        now_ms: int | None = None,
    ) -> KlineResult:
        """目标周期失败/不足时，用 15m 本地聚合兜底（不插值）。"""

        now = int(now_ms if now_ms is not None else self._now_fn())
        try:
            result = self.get_closed(symbol, interval, limit, now_ms=now)
            if interval != "15m" or len(result.bars) >= min(limit, C.CANDLE_BARS):
                return result
        except MarketUpstreamError as exc:
            if interval == "15m":
                raise
            logger.warning("kline fallback to 15m aggregation symbol=%s interval=%s error=%s", symbol, interval, exc)

        if interval == "15m":
            return self.get_closed(symbol, interval, limit, now_ms=now)
        ratio = C.INTERVAL_MS[interval] // C.INTERVAL_MS["15m"]
        base_limit = min(C.MAX_BARS_PER_SERIES, max(int(limit) * ratio + ratio, C.WARMUP_BARS["15m"]))
        base = self.get_closed(symbol, "15m", base_limit, now_ms=now)
        aggregated = aggregate_closed(base.bars, interval, limit)
        if len(aggregated) < min(int(limit), C.CANDLE_BARS):
            raise MarketUpstreamError(
                200,
                "MARKET_KLINES_INSUFFICIENT",
                f"aggregated {interval} bars insufficient for {symbol}",
            )
        last = aggregated[-1]
        age_s = max(0.0, (now - last.close_time_ms) / 1000.0)
        meta = {
            "interval": interval,
            "count": len(aggregated),
            "needed": int(limit),
            "last_closed_at_ms": last.close_time_ms,
            "age_s": round(age_s, 3),
            "fresh": age_s <= C.FRESHNESS_S[interval][0],
            "gap": False,
            "non_monotonic": False,
            "live_bar_present": base.meta.get("live_bar_present", False),
            "source": "aggregate_15m",
        }
        return KlineResult(
            symbol=symbol.upper(),
            interval=interval,
            bars=tuple(aggregated),
            meta=meta,
            issues=[],
        )

    def cached_bars(self, symbol: str, interval: str) -> tuple[Bar, ...]:
        series = self._get_series(symbol.upper(), interval)
        return tuple(series.bars) if series else ()

    # ------------------------------------------------------------------ internals
    def _get_series(self, symbol: str, interval: str) -> _Series | None:
        with self._lock:
            series = self._series.get((symbol, interval))
            if series is None:
                return None
            return _Series(bars=list(series.bars), received_at=series.received_at, live_bar_present=series.live_bar_present)

    def _cache_usable(self, series: _Series, interval: str, needed: int, now: int) -> bool:
        if len(series.bars) < needed:
            return False
        interval_ms = C.INTERVAL_MS[interval]
        last_closed = series.bars[-1]
        current_open = now // interval_ms * interval_ms
        if last_closed.open_time_ms < current_open - 2 * interval_ms:
            return False
        return (time.time() - series.received_at) <= C.KLINE_REFRESH_S

    def _plan_fetch(
        self, series: _Series | None, interval: str, needed: int, now: int
    ) -> tuple[int, int | None]:
        interval_ms = C.INTERVAL_MS[interval]
        # Binance 的 limit 包含正在形成的 bar，因此多取 1 根才能保证 ``needed`` 根闭合。
        if series is None or len(series.bars) < needed:
            limit = min(1500, max(needed + 1, 200))
            return limit, None
        last = series.bars[-1]
        missing = max(2, (now - last.open_time_ms) // interval_ms + 2)
        if missing > 100:
            limit = min(1500, max(needed + 1, 200))
            return limit, None
        return int(missing), last.open_time_ms

    def _result(
        self,
        symbol: str,
        interval: str,
        bars: list[Bar],
        needed: int,
        now: int,
        live_present: bool,
    ) -> KlineResult:
        selected = list(bars)[-max(1, needed) :]
        opens = [bar.open_time_ms for bar in selected]
        step = C.INTERVAL_MS[interval]
        gap = any(later - earlier != step for earlier, later in zip(opens, opens[1:]))
        non_monotonic = any(later <= earlier for earlier, later in zip(opens, opens[1:]))
        last = selected[-1] if selected else None
        age_s = max(0.0, (now - last.close_time_ms) / 1000.0) if last else None
        issues = _series_issues(
            selected, interval, needed, now, gap=gap, non_monotonic=non_monotonic
        )
        meta: dict[str, Any] = {
            "interval": interval,
            "count": len(selected),
            "needed": needed,
            "last_closed_at_ms": last.close_time_ms if last else None,
            "age_s": round(age_s, 3) if age_s is not None else None,
            "fresh": bool(age_s is not None and age_s <= C.FRESHNESS_S[interval][0]),
            "gap": gap,
            "non_monotonic": non_monotonic,
            "live_bar_present": live_present,
            "source": "rest",
        }
        return KlineResult(
            symbol=symbol.upper(),
            interval=interval,
            bars=tuple(selected),
            meta=meta,
            issues=issues,
        )
