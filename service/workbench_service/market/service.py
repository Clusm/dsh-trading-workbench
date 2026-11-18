"""行情服务编排：时钟、tickers、prices、klines、health、证据构建入口。"""

from __future__ import annotations

import copy
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from . import config as C
from .evidence import EvidenceBuilder
from .klines import KlineStore
from .quality import (
    bar_issues,
    clock_issues,
    issue,
    summarize_health,
    ticker_issues,
    transport_issues,
)
from .transport import (
    MarketBudgetDeferred,
    MarketCooldown,
    MarketBanned,
    MarketTransport,
    MarketUpstreamError,
    summarize_status,
)
from .universe import normalize_tickers, scan_market, select_universe

logger = logging.getLogger("workbench.market.service")


class UnknownSymbol(ValueError):
    """symbol 不在 20 币白名单内。"""


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _utc_iso(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


class MarketService:
    """只读行情服务；所有上游访问经 ``MarketTransport``（无 key/签名/下单）。"""

    def __init__(
        self,
        settings: C.MarketSettings,
        *,
        transport: MarketTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport or MarketTransport(
            settings.base_url,
            timeout_s=settings.timeout_s,
            local_budget_1m=settings.local_budget_1m,
            state_path=settings.paths.rate_limit_state,
        )
        self.klines = KlineStore(self.transport, now_fn=self.now_ms)
        self.evidence = EvidenceBuilder(self)
        self._lock = threading.RLock()
        self._clock: dict[str, Any] | None = None
        self._clock_mono = 0.0
        self._tickers: list[dict[str, Any]] | None = None
        self._tickers_at = 0.0
        self._tickers_stale = False
        self._price_cache: dict[tuple[str, ...], tuple[float, dict[str, Any]]] = {}
        self._mark_lock = threading.Lock()
        self._marks: dict[str, tuple[float, dict[str, Any]]] = {}
        self._mark_retry: dict[str, tuple[float, str]] = {}
        self._mark_inflight: dict[str, threading.Event] = {}
        self._health_cache: dict[str, Any] | None = None
        self._health_at = 0.0
        # 全市场异动扫描按小时刷新：数据（24h ticker 全量）已在 tickers 缓存里，
        # 这里缓存的只是排序结果，避免每个 10 分钟循环重复算一遍。
        self._market_scan: dict[str, Any] | None = None
        self._market_scan_at = 0.0
        self.started_at = time.time()

    # ------------------------------------------------------------------ clock
    def sync_clock(self, *, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if (
                not force
                and self._clock is not None
                and (time.monotonic() - self._clock_mono) < C.CLOCK_TTL_S
            ):
                self.transport.note_cache_hit()
                return dict(self._clock)
        local_before = int(time.time() * 1000)
        started = time.monotonic()
        payload = self.transport.get_json("/fapi/v1/time", weight=C.WEIGHT_TIME, purpose="clock")
        rtt_ms = int(round((time.monotonic() - started) * 1000))
        local_after = int(time.time() * 1000)
        local_mid = (local_before + local_after) // 2
        server_ms = _int((payload or {}).get("serverTime")) or local_mid
        offset_ms = int(server_ms - local_mid)
        clock = {
            "server_time_ms": int(server_ms),
            "local_time_ms": int(local_after),
            "offset_ms": offset_ms,
            "rtt_ms": rtt_ms,
            "clock_trusted": abs(offset_ms) <= C.CLOCK_BLOCK_MS,
        }
        with self._lock:
            self._clock = clock
            self._clock_mono = time.monotonic()
        return dict(clock)

    def now_ms(self) -> int:
        with self._lock:
            clock = self._clock
            mono = self._clock_mono
        if clock is None:
            return int(time.time() * 1000)
        return int(clock["server_time_ms"] + (time.monotonic() - mono) * 1000)

    def clock_payload(self) -> dict[str, Any]:
        cached = False
        if self._clock is not None and (time.monotonic() - self._clock_mono) < C.CLOCK_TTL_S:
            cached = True
        clock = self.sync_clock(force=not cached)
        payload = dict(clock)
        payload.update(
            checked_at=_utc_iso(int(clock["local_time_ms"])),
            cached=cached,
        )
        return payload

    # ------------------------------------------------------------------ tickers
    def tickers(self, *, force: bool = False) -> list[dict[str, Any]]:
        with self._lock:
            if (
                not force
                and self._tickers is not None
                and (time.time() - self._tickers_at) < C.TICKER_TTL_S
            ):
                self.transport.note_cache_hit()
                return copy.deepcopy(self._tickers)
        try:
            rows = self.transport.get_json(
                "/fapi/v1/ticker/24hr",
                weight=C.WEIGHT_TICKER_24H_ALL,
                purpose="universe",
            )
        except MarketUpstreamError:
            with self._lock:
                if self._tickers:
                    self._tickers_stale = True
                    return copy.deepcopy(self._tickers)
            raise
        normalized = normalize_tickers(rows if isinstance(rows, list) else [])
        with self._lock:
            self._tickers = normalized
            self._tickers_at = time.time()
            self._tickers_stale = False
        return copy.deepcopy(normalized)

    def tickers_stale(self) -> bool:
        with self._lock:
            return self._tickers_stale

    def tickers_snapshot_age_s(self) -> float | None:
        """最近一次 24h ticker 快照的年龄（比单行 closeTime 更可靠）。"""

        with self._lock:
            if self._tickers is None:
                return None
            return round(max(0.0, time.time() - self._tickers_at), 3)

    def _store_mark_row(self, symbol: str, raw: Any, *, now_ms: int) -> None:
        mark = _float(raw.get("markPrice")) if isinstance(raw, dict) else None
        as_of = _int(raw.get("time")) if isinstance(raw, dict) else None
        if mark is None or mark <= 0:
            raise MarketUpstreamError(None, "MARK_INVALID", "invalid mark quote")
        if as_of is not None and as_of > now_ms + 10_000:
            raise MarketUpstreamError(None, "MARK_CLOCK_INVALID", "quote timestamp is in the future")
        cached = self._marks.get(symbol)
        if cached and as_of and cached[1].get("as_of_ms") and as_of < cached[1]["as_of_ms"]:
            raise MarketUpstreamError(None, "MARK_OUT_OF_ORDER", "older quote rejected")
        quote_data: dict[str, Any] = {
            "mark": mark,
            "as_of_ms": as_of,
            "received_at_ms": now_ms,
            "source": "rest:premiumIndex",
        }
        funding = _float(raw.get("lastFundingRate")) if isinstance(raw, dict) else None
        if funding is not None:
            quote_data["funding_rate"] = funding
        self._marks[symbol] = (time.monotonic(), quote_data)
        self._mark_retry.pop(symbol, None)

    def mark_snapshot(self, symbols: Iterable[str]) -> dict[str, Any]:
        """Lightweight display-only quotes; no depth/account/clock queries.

        Per-symbol cache and singleflight coalescing coordinate readers across tabs
        without holding the lock across slow network I/O or blocking analysis caches.
        Failure never advances evidence time.
        """
        wanted = sorted(set(str(symbol).upper() for symbol in symbols))
        if not wanted or len(wanted) > 10:
            raise ValueError("request 1 to 10 symbols")
        if any(symbol not in self.settings.whitelist for symbol in wanted):
            raise UnknownSymbol("symbol not in whitelist")

        deadline = time.monotonic() + 4.0
        need_fetch: list[str] = []
        own_events: dict[str, threading.Event] = {}
        wait_events: list[threading.Event] = []
        with self._mark_lock:
            mono = time.monotonic()
            for symbol in wanted:
                cached = self._marks.get(symbol)
                retry = self._mark_retry.get(symbol)
                expired = cached is None or (mono - cached[0]) >= C.PRICE_TTL_S_CACHE
                backed_off = retry is not None and retry[0] > mono
                if expired and not backed_off:
                    inflight = self._mark_inflight.get(symbol)
                    if inflight is not None:
                        wait_events.append(inflight)
                    else:
                        ev = threading.Event()
                        self._mark_inflight[symbol] = ev
                        own_events[symbol] = ev
                        need_fetch.append(symbol)

        try:
            remaining_symbols = list(need_fetch)
            if len(remaining_symbols) >= 5:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.1:
                        raise MarketUpstreamError(
                            None, "MARK_BUDGET_EXHAUSTED", "display quote batch budget exhausted"
                        )
                    raw_batch = self.transport.get_json(
                        "/fapi/v1/premiumIndex",
                        {},
                        weight=10,
                        purpose="prices",
                        timeout_s=min(2.5, remaining),
                    )
                    now_batch = self.now_ms()
                    if isinstance(raw_batch, list):
                        by_symbol = {
                            str(item.get("symbol")).upper(): item
                            for item in raw_batch
                            if isinstance(item, dict) and item.get("symbol")
                        }
                        with self._mark_lock:
                            for sym in remaining_symbols:
                                try:
                                    self._store_mark_row(sym, by_symbol.get(sym), now_ms=now_batch)
                                except MarketUpstreamError as exc:
                                    delay = max(5.0, float(getattr(exc, "retry_after_s", 0) or 0))
                                    self._mark_retry[sym] = (time.monotonic() + delay, exc.code)
                        remaining_symbols = []
                    elif isinstance(raw_batch, dict):
                        sym_in_dict = str(raw_batch.get("symbol") or "").upper()
                        if sym_in_dict and sym_in_dict in remaining_symbols:
                            with self._mark_lock:
                                try:
                                    self._store_mark_row(sym_in_dict, raw_batch, now_ms=now_batch)
                                except MarketUpstreamError as exc:
                                    delay = max(5.0, float(getattr(exc, "retry_after_s", 0) or 0))
                                    self._mark_retry[sym_in_dict] = (time.monotonic() + delay, exc.code)
                            remaining_symbols = [s for s in remaining_symbols if s != sym_in_dict]
                        elif not sym_in_dict and raw_batch.get("markPrice") is not None:
                            with self._mark_lock:
                                for sym in remaining_symbols:
                                    try:
                                        self._store_mark_row(sym, raw_batch, now_ms=now_batch)
                                    except MarketUpstreamError as exc:
                                        delay = max(5.0, float(getattr(exc, "retry_after_s", 0) or 0))
                                        self._mark_retry[sym] = (time.monotonic() + delay, exc.code)
                            remaining_symbols = []
                except MarketUpstreamError as exc:
                    delay = max(5.0, float(getattr(exc, "retry_after_s", 0) or 0))
                    with self._mark_lock:
                        until = time.monotonic() + delay
                        for sym in remaining_symbols:
                            self._mark_retry[sym] = (until, exc.code)
                    remaining_symbols = []

            for symbol in remaining_symbols:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.1:
                        raise MarketUpstreamError(
                            None, "MARK_BUDGET_EXHAUSTED", "display quote batch budget exhausted"
                        )
                    raw = self.transport.get_json(
                        "/fapi/v1/premiumIndex",
                        {"symbol": symbol},
                        weight=C.WEIGHT_PREMIUM,
                        purpose="prices",
                        timeout_s=min(1.5, remaining),
                    )
                    with self._mark_lock:
                        self._store_mark_row(symbol, raw, now_ms=self.now_ms())
                except MarketUpstreamError as exc:
                    delay = max(5.0, float(getattr(exc, "retry_after_s", 0) or 0))
                    with self._mark_lock:
                        self._mark_retry[symbol] = (time.monotonic() + delay, exc.code)
        finally:
            if own_events:
                with self._mark_lock:
                    for sym, ev in own_events.items():
                        if self._mark_inflight.get(sym) is ev:
                            self._mark_inflight.pop(sym, None)
                        ev.set()

        for ev in wait_events:
            ev.wait(timeout=max(0.05, deadline - time.monotonic()))

        now = self.now_ms()
        result: dict[str, Any] = {}
        with self._mark_lock:
            mono_now = time.monotonic()
            for symbol in wanted:
                cached = self._marks.get(symbol)
                error = self._mark_retry.get(symbol)
                quote = (
                    dict(cached[1])
                    if cached
                    else {"mark": None, "as_of_ms": None, "received_at_ms": None, "source": "unavailable"}
                )
                age = max(0, now - quote["as_of_ms"]) if quote.get("as_of_ms") else None
                quote.update(
                    age_ms=age,
                    stale=bool(error) or age is None or age > 5000,
                    error=error[1] if error else None,
                    retry_after_s=max(0.0, error[0] - mono_now) if error else 0,
                )
                result[symbol] = quote
        return {
            "prices": result,
            "received_at_ms": now,
            "source_base_url": self.settings.base_url,
        }

    # ------------------------------------------------------------------ prices
    def price_snapshot(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = [str(symbol).upper() for symbol in symbols]
        key = tuple(sorted(set(wanted)))
        with self._lock:
            entry = self._price_cache.get(key)
            if entry is not None and (time.monotonic() - entry[0]) < C.PRICE_TTL_S_CACHE:
                self.transport.note_cache_hit()
                return copy.deepcopy(entry[1])

        prices_rows = self.transport.get_json(
            "/fapi/v1/ticker/price",
            weight=C.WEIGHT_TICKER_PRICE_ALL,
            purpose="prices",
        )
        price_by_symbol = {
            str(row.get("symbol")): row
            for row in (prices_rows or [])
            if isinstance(row, dict) and row.get("symbol")
        }
        now = self.now_ms()
        result: dict[str, dict[str, Any]] = {}
        for symbol in key:
            row = price_by_symbol.get(symbol) or {}
            last = _float(row.get("price"))
            last_time_ms = _int(row.get("time"))
            premium: dict[str, Any] = {}
            depth: dict[str, Any] = {}
            errors: list[str] = []
            try:
                premium = self.transport.get_json(
                    "/fapi/v1/premiumIndex", {"symbol": symbol}, weight=C.WEIGHT_PREMIUM, purpose="prices"
                ) or {}
            except MarketUpstreamError as exc:
                errors.append(exc.code)
            try:
                depth = self.transport.get_json(
                    "/fapi/v1/depth",
                    {"symbol": symbol, "limit": 5},
                    weight=C.WEIGHT_DEPTH_LIMIT_5,
                    purpose="prices",
                ) or {}
            except MarketUpstreamError as exc:
                errors.append(exc.code)

            mark = _float(premium.get("markPrice"))
            index = _float(premium.get("indexPrice"))
            bid = ask = None
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            if bids and asks:
                bid = _float(bids[0][0]) if bids[0] else None
                ask = _float(asks[0][0]) if asks[0] else None
            bid_qty = sum(_float(item[1]) or 0.0 for item in bids[:5] if len(item) > 1)
            ask_qty = sum(_float(item[1]) or 0.0 for item in asks[:5] if len(item) > 1)
            spread_bps = None
            if bid and ask and bid > 0:
                spread_bps = round((ask - bid) / bid * 10_000.0, 2)
            imbalance = None
            if bid_qty + ask_qty > 0:
                imbalance = round((bid_qty - ask_qty) / (bid_qty + ask_qty), 4)
            price_time = _int(premium.get("time")) or last_time_ms
            result[symbol] = {
                "symbol": symbol,
                "last": last,
                "mark": mark,
                "index": index,
                "bid": bid,
                "ask": ask,
                "spread_bps": spread_bps,
                "top_imbalance": imbalance,
                "funding_rate": _float(premium.get("lastFundingRate")),
                "next_funding_time_ms": _int(premium.get("nextFundingTime")),
                "price_time_ms": price_time,
                "age_ms": None if price_time is None else max(0, now - price_time),
                "depth_time_ms": _int(depth.get("T")) or _int(depth.get("E")),
                "errors": errors,
            }
        with self._lock:
            self._price_cache[key] = (time.monotonic(), copy.deepcopy(result))
        return result

    # ------------------------------------------------------------------ universe
    def universe_payload(self, *, held: Sequence[str] = (), prev_pick: str | None = None) -> dict[str, Any]:
        tickers = self.tickers()
        selection = select_universe(
            tickers,
            whitelist=self.settings.whitelist,
            deep_max=self.settings.deep_max,
            held=held,
            prev_pick=prev_pick,
            min_quote_volume_usd=self.settings.min_quote_volume_usd,
            anomaly_pct=self.settings.anomaly_pct,
        )
        return {
            "as_of_ms": self.now_ms(),
            "cached": self.transport.stats()["cache_hits"] > 0,
            "tickers": len(tickers),
            "universe": selection,
        }

    # ------------------------------------------------------- market scan
    def market_scan(self, *, force: bool = False) -> dict[str, Any]:
        """全市场异动扫描（每小时刷新；不新增请求）。

        与 ``select_universe`` 的区别：选池只看 20 币白名单，这里看全市场。
        返回值里带 ``in_whitelist`` 标记，可交易集合不因此扩大。
        """

        tickers = self.tickers()
        with self._lock:
            fresh = (
                self._market_scan is not None
                and (time.time() - self._market_scan_at) < C.MARKET_SCAN_REFRESH_S
            )
            if fresh and not force:
                payload = copy.deepcopy(self._market_scan)
                payload["cached"] = True
                return payload
        scan = scan_market(
            tickers,
            whitelist=self.settings.whitelist,
            anomaly_pct=self.settings.anomaly_pct,
            min_quote_volume_usd=self.settings.min_quote_volume_usd,
        )
        scan["scanned_at_ms"] = self.now_ms()
        scan["cached"] = False
        with self._lock:
            self._market_scan = copy.deepcopy(scan)
            self._market_scan_at = time.time()
        return scan

    # ------------------------------------------------------------------ klines
    def klines_payload(self, symbol: str, interval: str, limit: int) -> dict[str, Any]:
        normalized = symbol.strip().upper()
        if normalized not in self.settings.whitelist:
            raise UnknownSymbol(f"{normalized} is not in the 20-symbol whitelist")
        if interval not in C.INTERVAL_MS:
            raise ValueError(f"interval must be one of {list(C.INTERVAL_MS)}")
        result = self.klines.get_with_fallback(normalized, interval, limit, now_ms=self.now_ms())
        return {
            "symbol": normalized,
            "interval": interval,
            "closed_only": True,
            "count": len(result.bars),
            "limit": int(limit),
            "bars": [bar.as_dict() for bar in result.bars],
            "meta": result.meta,
            "issues": result.issues,
        }

    # ------------------------------------------------------------------ health
    def health_payload(self, *, refresh: bool = False) -> dict[str, Any]:
        """轻量健康检查；默认走 10s 缓存，``refresh=True`` 才强制刷新。

        冷却/封禁时仍返回 200 + 明确状态（sources.rest.state / cooldown_s /
        last_probe），不因 transport 抛错而让端点不可达；BLOCKED 结果不入缓存，
        便于冷却恢复后立即重探。
        """

        with self._lock:
            if (
                not refresh
                and self._health_cache is not None
                and (time.monotonic() - self._health_at) < C.HEALTH_TTL_S
            ):
                # 冷却/封禁必须实时可见，不能拿旧缓存掩盖。
                if self.transport.status()["state"] not in {"cooldown", "banned"}:
                    self.transport.note_cache_hit()
                    cached = copy.deepcopy(self._health_cache)
                    cached["cached"] = True
                    if isinstance(cached.get("last_probe"), dict):
                        cached["last_probe"]["cached"] = True
                    return cached

        started = time.perf_counter()
        issues: list[dict[str, str]] = []
        clock: dict[str, Any] | None = None
        cooldown_hit = False
        try:
            clock = self.sync_clock(force=True)
            issues.extend(clock_issues(clock["offset_ms"]))
        except (MarketCooldown, MarketBanned):
            cooldown_hit = True
        except MarketUpstreamError as exc:
            issues.append(issue("CLOCK_SKEW", "error", "clock", f"time endpoint failed: {exc.code}"))

        tickers: list[dict[str, Any]] = []
        try:
            tickers = self.tickers(force=True)
        except (MarketCooldown, MarketBanned):
            cooldown_hit = True
        except MarketUpstreamError as exc:
            issues.append(
                issue("UNIVERSE_DEGRADED", "warn", "universe", f"ticker fetch failed: {exc.code}")
            )

        deep = ["BTCUSDT"]
        bar_meta: dict[str, dict[str, Any]] = {}
        for interval in C.INTERVALS:
            try:
                result = self.klines.get_with_fallback(
                    "BTCUSDT", interval, C.WARMUP_BARS[interval], now_ms=self.now_ms()
                )
            except (MarketCooldown, MarketBanned):
                cooldown_hit = True
                continue
            except MarketUpstreamError as exc:
                issues.append(
                    issue(
                        "KLINE_INSUFFICIENT",
                        "error",
                        f"BTCUSDT:{interval}",
                        f"klines unavailable: {exc.code}",
                    )
                )
                continue
            issues.extend(bar_issues(f"BTCUSDT:{interval}", interval, result.meta, held=False))
            bar_meta[interval] = result.meta

        transport_status = self.transport.status()
        issues.extend(transport_issues(transport_status))
        if tickers:
            ticker_age = self.tickers_snapshot_age_s()
            for row in tickers:
                if row["symbol"] not in self.settings.whitelist:
                    continue
                issues.extend(
                    ticker_issues(row["symbol"], ticker_age, deep=row["symbol"] in deep)
                )

        summary = summarize_health(issues)
        checked_at = _utc_iso(self.now_ms())
        latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        payload = {
            "service": C.PROVENANCE_SERVICE,
            "version": C.SERVICE_VERSION,
            "verdict": summary["verdict"],
            "score": summary["score"],
            "issues": summary["issues"],
            "sources": {
                "rest": summarize_status(transport_status),
                "ws": {"state": "disabled", "last_frame_age_s": None},
            },
            "cooldown_s": transport_status["cooldown_s"],
            "data_budget": {
                "used_5m": transport_status.get("data_weight_used_5m", 0),
                "limit_5m": transport_status.get("data_budget_5m", C.DATA_BUDGET_5M),
                "window_s": C.DATA_WINDOW_S,
            },
            "clock": clock,
            "universe": {
                "whitelist": len(self.settings.whitelist),
                "deep_probe": "BTCUSDT",
            },
            "last_closed_bar": {
                interval: _utc_iso(int(meta["last_closed_at_ms"]))
                if meta.get("last_closed_at_ms")
                else None
                for interval, meta in bar_meta.items()
            },
            "bar_age_s": {
                interval: int(meta["age_s"]) if meta.get("age_s") is not None else None
                for interval, meta in bar_meta.items()
            },
            "checked_at": checked_at,
            "latency_ms": latency_ms,
            "cached": False,
            "last_probe": {
                "at": checked_at,
                "verdict": summary["verdict"],
                "score": summary["score"],
                "latency_ms": latency_ms,
                "issues": len(summary["issues"]),
                "cooldown_s": transport_status["cooldown_s"],
                "cooldown_hit": cooldown_hit,
                "cached": False,
            },
        }
        with self._lock:
            if summary["verdict"] != "BLOCKED":
                self._health_cache = copy.deepcopy(payload)
                self._health_at = time.monotonic()
        return payload

    # ------------------------------------------------------------------ evidence
    def build_evidence(self, **kwargs: Any) -> dict[str, Any]:
        return self.evidence.build(**kwargs)

    def close(self) -> None:
        self.transport.close()
