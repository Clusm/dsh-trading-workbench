"""M1.1 离线测试：假上游 200/429/418/timeout、闭合 bar、指标、schema、证据、端点。

不访问币安；所有请求都发往 127.0.0.1 的临时假服务。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlparse

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from workbench_service.app import create_app  # noqa: E402
from workbench_service.config import Settings  # noqa: E402
from workbench_service.market import config as C  # noqa: E402
from workbench_service.market import indicators as I  # noqa: E402
from workbench_service.market import quality as Q  # noqa: E402
from workbench_service.market import schema as S  # noqa: E402
from workbench_service.market.evidence import (  # noqa: E402
    EvidenceBuilder,
    EvidenceError,
    canonical_hash,
    verify_hash,
)
from workbench_service.market.klines import (  # noqa: E402
    Bar,
    KlineStore,
    aggregate_closed,
    parse_kline_row,
)
from workbench_service.market.service import MarketService  # noqa: E402
from workbench_service.market.transport import (  # noqa: E402
    MarketBanned,
    MarketBudgetDeferred,
    MarketCooldown,
    MarketTimeout,
    MarketTransport,
)
from workbench_service.market.universe import normalize_tickers, scan_market, select_universe  # noqa: E402

WHITELIST = list(C.WHITELIST)
FIXED_CYCLE = "cyc-20261109T000000Z-abcdef"


def _price(symbol: str) -> float:
    try:
        index = WHITELIST.index(symbol)
    except ValueError:
        index = 7
    return 100.0 + index * 10.0


def _volume(symbol: str) -> float:
    try:
        index = WHITELIST.index(symbol)
    except ValueError:
        index = 19
    return 5.0e10 * (0.6 ** index)


_PCT = {
    "BTCUSDT": 1.2,
    "ETHUSDT": -0.8,
    "SOLUSDT": 2.5,
    "ZECUSDT": 11.0,
    "XRPUSDT": -6.0,
    "ARBUSDT": 5.5,
}


def _ticker_row(symbol: str, now_ms: int) -> dict[str, Any]:
    price = _price(symbol)
    return {
        "symbol": symbol,
        "lastPrice": f"{price:.6f}",
        "priceChangePercent": f"{_PCT.get(symbol, 1.0):.3f}",
        "quoteVolume": f"{_volume(symbol):.2f}",
        "highPrice": f"{price * 1.02:.6f}",
        "lowPrice": f"{price * 0.98:.6f}",
        "closeTime": now_ms - 1000,
    }


def _kline_rows(
    symbol: str,
    interval: str,
    limit: int,
    now_ms: int,
    start_time: int | None = None,
) -> list[list[Any]]:
    step = C.INTERVAL_MS[interval]
    if start_time is not None:
        first = int(start_time) // step * step
        opens = [first + index * step for index in range(limit)]
    else:
        current = now_ms // step * step
        opens = [current - (limit - 1 - index) * step for index in range(limit)]
    base = _price(symbol)
    rows: list[list[Any]] = []
    for index, open_ms in enumerate(opens):
        drift = 1.0 + ((index % 11) - 5) * 0.0005
        open_price = base * drift
        close_price = open_price * 1.0008
        high = max(open_price, close_price) * 1.002
        low = min(open_price, close_price) * 0.998
        volume = 100.0 + index
        rows.append(
            [
                open_ms,
                f"{open_price:.6f}",
                f"{high:.6f}",
                f"{low:.6f}",
                f"{close_price:.6f}",
                f"{volume:.4f}",
                open_ms + step - 1,
                f"{volume * open_price:.4f}",
                50 + index,
                f"{volume * 0.5:.4f}",
                f"{volume * open_price * 0.5:.4f}",
                "0",
            ]
        )
    return rows


class _FakeMarketHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: "_FakeMarketServer"

    def do_GET(self) -> None:  # noqa: N802 - 协议方法
        self.server.requests.append(self.path)
        mode = self.server.mode
        if mode == "429":
            self._json(429, {"code": -1003, "msg": "too many"}, {"Retry-After": "7"})
            return
        if mode == "418":
            self._json(418, {"code": -1000, "msg": "banned"}, {"Retry-After": "11"})
            return
        if mode == "timeout":
            time.sleep(0.4)
            self._json(200, {})
            return

        parsed = urlparse(self.path)
        path = parsed.path
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        now_ms = int(time.time() * 1000)

        if path == "/fapi/v1/time":
            self._json(200, {"serverTime": now_ms})
        elif path == "/fapi/v1/ticker/24hr":
            if "symbol" in params:
                self._json(200, _ticker_row(params["symbol"].upper(), now_ms))
            else:
                rows = [_ticker_row(symbol, now_ms) for symbol in WHITELIST]
                rows.append(_ticker_row("SOXLUSDT", now_ms))
                self._json(200, rows)
        elif path == "/fapi/v1/ticker/price":
            self._json(
                200,
                [
                    {"symbol": symbol, "price": f"{_price(symbol):.6f}", "time": now_ms - 500}
                    for symbol in WHITELIST
                ],
            )
        elif path == "/fapi/v1/premiumIndex":
            symbol = params.get("symbol", "BTCUSDT").upper()
            price = _price(symbol)
            self._json(
                200,
                {
                    "symbol": symbol,
                    "markPrice": f"{price * 1.0001:.6f}",
                    "indexPrice": f"{price * 0.9999:.6f}",
                    "lastFundingRate": "0.0002",
                    "nextFundingTime": now_ms + 3_600_000,
                    "time": now_ms - 300,
                },
            )
        elif path == "/fapi/v1/depth":
            symbol = params.get("symbol", "BTCUSDT").upper()
            price = _price(symbol)
            self._json(
                200,
                {
                    "lastUpdateId": 1,
                    "E": now_ms - 200,
                    "T": now_ms - 200,
                    "bids": [[f"{price * 0.9999:.6f}", "1.5"]],
                    "asks": [[f"{price * 1.0001:.6f}", "1.2"]],
                },
            )
        elif path == "/fapi/v1/klines":
            symbol = params.get("symbol", "BTCUSDT").upper()
            interval = params.get("interval", "15m")
            limit = int(params.get("limit", "100"))
            if interval in self.server.fail_intervals:
                self._json(500, {"code": -1000, "msg": "injected failure"})
                return
            start_time = int(params["startTime"]) if "startTime" in params else None
            rows = _kline_rows(symbol, interval, limit, now_ms, start_time)
            if self.server.skip_kline_index is not None and rows:
                rows.pop(self.server.skip_kline_index % len(rows))
            self._json(200, rows)
        elif path == "/fapi/v1/openInterest":
            self._json(200, {"symbol": params.get("symbol", "BTCUSDT").upper(), "openInterest": "1000000.0", "time": now_ms - 200})
        elif path == "/futures/data/openInterestHist":
            self._json(
                200,
                [
                    {"symbol": params.get("symbol", "BTCUSDT").upper(), "sumOpenInterest": str(1_000_000.0 + index * 5_000.0), "timestamp": now_ms - (5 - index) * 900_000}
                    for index in range(5)
                ],
            )
        elif path == "/futures/data/globalLongShortAccountRatio":
            self._json(200, [{"symbol": params.get("symbol", "BTCUSDT").upper(), "longShortRatio": "1.8", "timestamp": now_ms - 600_000}])
        elif path == "/futures/data/takerlongshortRatio":
            self._json(200, [{"symbol": params.get("symbol", "BTCUSDT").upper(), "buySellRatio": "1.05", "timestamp": now_ms - 600_000}])
        else:
            self._json(404, {"code": -1121, "msg": "not found"})

    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        return


class _FakeMarketServer(ThreadingHTTPServer):
    daemon_threads = True
    requests: list[str]
    mode: str
    skip_kline_index: int | None
    fail_intervals: set[str]
    base_url: str


class ServerFixture:
    def __init__(self) -> None:
        self.server = _FakeMarketServer(("127.0.0.1", 0), _FakeMarketHandler)
        self.server.requests = []
        self.server.mode = "ok"
        self.server.skip_kline_index = None
        self.server.fail_intervals = set()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeMarketServer:
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.server.base_url = f"http://{host}:{port}"
        return self.server

    def __exit__(self, *exc_info: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _transport(server: _FakeMarketServer, **kwargs: Any) -> MarketTransport:
    return MarketTransport(
        server.base_url,
        timeout_s=kwargs.pop("timeout_s", 2.0),
        local_budget_1m=kwargs.pop("local_budget_1m", C.LOCAL_WEIGHT_BUDGET_1M),
        state_path=kwargs.pop("state_path", None),
        **kwargs,
    )


def _service(server: _FakeMarketServer, data_dir: Path, **transport_kwargs: Any) -> MarketService:
    settings = C.MarketSettings(base_url=server.base_url, data_dir=data_dir, timeout_s=2.0)
    return MarketService(settings, transport=_transport(server, **transport_kwargs))


class _FakeClock:
    """可推进的单调时钟，用于 5m 窗口/冷却边界测试。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class TransportTests(unittest.TestCase):
    def test_ok_and_stats(self) -> None:
        with ServerFixture() as server:
            transport = _transport(server)
            payload = transport.get_json("/fapi/v1/time", weight=1)
            self.assertIn("serverTime", payload)
            self.assertEqual(transport.stats()["requests"], 1)
            self.assertEqual(transport.status()["state"], "ok")
            transport.close()

    def test_429_no_retry_and_cooldown(self) -> None:
        with ServerFixture() as server:
            server.mode = "429"
            transport = _transport(server)
            with self.assertRaises(MarketCooldown) as context:
                transport.get_json("/fapi/v1/time", weight=1)
            self.assertEqual(context.exception.retry_after_s, 7.0)
            self.assertEqual(len(server.requests), 1, "429 不得重试")
            self.assertGreater(transport.cooldown_remaining(), 0)
            with self.assertRaises(MarketCooldown):
                transport.get_json("/fapi/v1/time", weight=1)
            self.assertEqual(len(server.requests), 1, "冷却期内不得再发请求")
            self.assertEqual(transport.status()["state"], "cooldown")
            transport.close()

    def test_418_no_retry_and_ban_state(self) -> None:
        with ServerFixture() as server:
            server.mode = "418"
            transport = _transport(server)
            with self.assertRaises(MarketBanned) as context:
                transport.get_json("/fapi/v1/time", weight=1)
            self.assertEqual(context.exception.retry_after_s, 11.0)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(transport.status()["state"], "banned")
            transport.close()

    def test_timeout_mapping(self) -> None:
        with ServerFixture() as server:
            server.mode = "timeout"
            transport = _transport(server, timeout_s=0.05)
            with self.assertRaises(MarketTimeout):
                transport.get_json("/fapi/v1/time", weight=1)
            transport.close()

    def test_local_budget_deferred(self) -> None:
        with ServerFixture() as server:
            transport = _transport(server, local_budget_1m=1)
            transport.get_json("/fapi/v1/time", weight=1)
            with self.assertRaises(MarketBudgetDeferred):
                transport.get_json("/fapi/v1/time", weight=1)
            self.assertEqual(len(server.requests), 1)
            transport.close()

    def test_futures_data_5m_budget_survives_1m_trim(self) -> None:
        """T23-H01 回归：5m 子预算必须独立于 60s 全局记录裁剪。"""

        with ServerFixture() as server:
            clock = _FakeClock()
            transport = _transport(
                server, local_budget_1m=100_000, data_budget_5m=3, now_fn=clock
            )
            for _ in range(3):
                transport.get_json(
                    "/futures/data/openInterestHist",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            clock.advance(61.0)  # 60s 全局窗口已清空，但 300s 数据窗口仍有效
            self.assertEqual(transport.status()["weight_used_1m"], 0)
            self.assertEqual(transport.status()["data_weight_used_5m"], 3)
            with self.assertRaises(MarketBudgetDeferred):
                transport.get_json(
                    "/futures/data/openInterestHist",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            self.assertEqual(len(server.requests), 3, "5m 超限不得发请求")

            clock.advance(240.0)  # 距首条 301s，窗口滚出
            transport.get_json(
                "/futures/data/openInterestHist",
                {"symbol": "BTCUSDT"},
                weight=1,
                group="futures_data",
            )
            self.assertEqual(transport.status()["data_weight_used_5m"], 1)
            transport.close()

    def test_futures_data_5m_boundary_and_priority(self) -> None:
        with ServerFixture() as server:
            clock = _FakeClock()
            transport = _transport(server, local_budget_1m=100_000, data_budget_5m=2, now_fn=clock)
            for _ in range(2):
                transport.get_json(
                    "/futures/data/takerlongshortRatio",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            clock.advance(299.0)
            with self.assertRaises(MarketBudgetDeferred):
                transport.get_json(
                    "/futures/data/takerlongshortRatio",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            clock.advance(2.0)  # 301s：窗口滚动
            transport.get_json(
                "/futures/data/takerlongshortRatio",
                {"symbol": "BTCUSDT"},
                weight=1,
                group="futures_data",
            )
            transport.close()

        with ServerFixture() as server:
            server.mode = "429"
            clock = _FakeClock()
            transport = _transport(server, local_budget_1m=100_000, data_budget_5m=1, now_fn=clock)
            with self.assertRaises(MarketCooldown):
                transport.get_json("/fapi/v1/time", weight=1)
            # 429 冷却优先于 5m 预算：直接抬冷却，不发 futures/data 请求。
            with self.assertRaises(MarketCooldown):
                transport.get_json(
                    "/futures/data/openInterestHist",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            self.assertEqual(len(server.requests), 1)
            transport.close()

        with ServerFixture() as server:
            # 本地 600/min 预算优先于 5m 数据预算。
            clock = _FakeClock()
            transport = _transport(server, local_budget_1m=1, data_budget_5m=100, now_fn=clock)
            transport.get_json("/fapi/v1/time", weight=1)
            with self.assertRaises(MarketBudgetDeferred) as context:
                transport.get_json(
                    "/futures/data/openInterestHist",
                    {"symbol": "BTCUSDT"},
                    weight=1,
                    group="futures_data",
                )
            self.assertIn("weight budget", str(context.exception))
            transport.close()

    def test_cooldown_persisted_across_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            state_path = Path(tmp) / "rate.json"
            # 冻结时钟：这条用例验证的是“冷却被持久化并跨实例生效”，
            # 不是“墙上时钟还没走完”。用真实时间时，机器一慢（29s 跑一条）
            # Retry-After=7 早就过期，断言必然失败——那是环境噪声，不是产品行为。
            frozen = {"t": 1_000_000.0}
            clock = lambda: frozen["t"]
            server.mode = "429"
            first = _transport(server, state_path=state_path, now_fn=clock)
            with self.assertRaises(MarketCooldown):
                first.get_json("/fapi/v1/time", weight=1)
            first.close()

            server.mode = "ok"
            second = _transport(server, state_path=state_path, now_fn=clock)
            self.assertGreater(second.cooldown_remaining(), 0)
            with self.assertRaises(MarketCooldown):
                second.get_json("/fapi/v1/time", weight=1)
            self.assertEqual(len(server.requests), 1)
            second.close()


class KlineTests(unittest.TestCase):
    def test_closed_only_and_warmup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            service = _service(server, Path(tmp))
            result = service.klines.get_closed("BTCUSDT", "15m", 10, now_ms=service.now_ms())
            self.assertEqual(result.meta["count"], C.WARMUP_BARS["15m"])
            self.assertTrue(result.meta["live_bar_present"])
            now = service.now_ms()
            self.assertTrue(all(bar.close_time_ms <= now for bar in result.bars))
            self.assertNotIn("KLINE_INSUFFICIENT", result.issues)
            service.close()

    def test_each_interval_warmup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            service = _service(server, Path(tmp))
            for interval, warmup in C.WARMUP_BARS.items():
                result = service.klines.get_closed("BTCUSDT", interval, warmup, now_ms=service.now_ms())
                self.assertEqual(result.meta["count"], warmup, interval)
                self.assertTrue(all(bar.close_time_ms <= service.now_ms() for bar in result.bars))
            service.close()

    def test_cache_reuse_within_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            service = _service(server, Path(tmp))
            service.klines.get_closed("BTCUSDT", "15m", 100, now_ms=service.now_ms())
            service.klines.get_closed("BTCUSDT", "15m", 100, now_ms=service.now_ms())
            kline_requests = [path for path in server.requests if "/fapi/v1/klines" in path]
            self.assertEqual(len(kline_requests), 1)
            service.close()

    def test_gap_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            server.skip_kline_index = 5
            service = _service(server, Path(tmp))
            result = service.klines.get_closed("BTCUSDT", "15m", 100, now_ms=service.now_ms())
            self.assertTrue(result.meta["gap"])
            self.assertIn("KLINE_GAP", result.issues)
            service.close()

    def test_parse_kline_final_uses_supplied_now(self) -> None:
        """T23-L03：final 必须使用注入时钟，而不是宿主 time.time()。"""

        row: list[Any] = [0, "1", "2", "0.5", "1.5", "10", 999, "15", 1, "5", "7"]
        self.assertTrue(parse_kline_row(row, now_ms=999).final)
        self.assertFalse(parse_kline_row(row, now_ms=998).final)
        self.assertFalse(parse_kline_row(row, now_ms=999 - 1).final)
        self.assertTrue(parse_kline_row(row, now_ms=999 + 1).final)

    def test_get_closed_respects_injected_now_clock_offset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            service = _service(server, Path(tmp))
            shifted_now = int(time.time() * 1000) - 45 * 60 * 1000
            result = service.klines.get_closed("BTCUSDT", "15m", 10, now_ms=shifted_now)
            self.assertTrue(result.bars)
            self.assertTrue(all(bar.close_time_ms <= shifted_now for bar in result.bars))
            self.assertTrue(all(bar.final for bar in result.bars))
            self.assertIn("KLINE_INSUFFICIENT", result.issues)  # 偏移后不足 warmup
            service.close()

    def test_aggregate_ohlcv(self) -> None:
        step = C.INTERVAL_MS["15m"]
        bucket = 1_700_000_000_000 // step * step
        bars = [
            Bar(
                open_time_ms=bucket + index * step,
                close_time_ms=bucket + (index + 1) * step - 1,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.5 + index,
                volume=10.0 + index,
                quote_volume=1000.0 + index,
                trades=5 + index,
                taker_buy_base=5.0,
                taker_buy_quote=500.0,
                final=True,
                source="rest",
            )
            for index in range(8)
        ]
        aggregated = aggregate_closed(bars, "1h", 10)
        self.assertEqual(len(aggregated), 2)
        first = aggregated[0]
        self.assertEqual(first.open_time_ms, bucket)
        self.assertEqual(first.open, 100.0)
        self.assertEqual(first.close, bars[3].close)
        self.assertEqual(first.high, 104.0)
        self.assertEqual(first.low, 99.0)
        self.assertAlmostEqual(first.volume, sum(bar.volume for bar in bars[:4]))
        self.assertEqual(first.source, "aggregate_15m")

        incomplete = aggregate_closed(bars[:3], "1h", 10)
        self.assertEqual(incomplete, [])

    def test_aggregate_fallback_when_target_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            server.fail_intervals = {"1h"}
            service = _service(server, Path(tmp))
            result = service.klines.get_with_fallback("BTCUSDT", "1h", 200, now_ms=service.now_ms())
            self.assertEqual(result.meta["source"], "aggregate_15m")
            self.assertGreaterEqual(len(result.bars), 200)
            self.assertTrue(all(bar.close_time_ms <= service.now_ms() for bar in result.bars))
            service.close()


def _assert_series_equal(test: unittest.TestCase, left: list[Any], right: list[Any]) -> None:
    test.assertEqual(len(left), len(right))
    for index, (a, b) in enumerate(zip(left, right)):
        if a is None or b is None:
            test.assertIs(a, b, f"index {index}: {a} != {b}")
        else:
            test.assertAlmostEqual(a, b, places=9, msg=f"index {index}")


class IndicatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.closes = [100 + (index % 7) * 1.5 + index * 0.3 for index in range(160)]
        self.highs = [value + 1.0 for value in self.closes]
        self.lows = [value - 1.0 for value in self.closes]

    def test_prefix_consistency_no_lookahead(self) -> None:
        cut = 120
        cases = [
            (I.sma(self.closes, 20), I.sma(self.closes[:cut], 20)),
            (I.ema(self.closes, 20), I.ema(self.closes[:cut], 20)),
            (I.rsi(self.closes, 14), I.rsi(self.closes[:cut], 14)),
            (
                I.atr(self.highs, self.lows, self.closes, 14),
                I.atr(self.highs[:cut], self.lows[:cut], self.closes[:cut], 14),
            ),
            (
                I.atr(self.highs, self.lows, self.closes, 14),
                I.atr(self.highs[:cut], self.lows[:cut], self.closes[:cut], 14),
            ),
        ]
        for full, prefix in cases:
            _assert_series_equal(self, full[:cut], prefix)

        for full, prefix in (
            (I.macd(self.closes)[0], I.macd(self.closes[:cut])[0]),
            (I.macd(self.closes)[1], I.macd(self.closes[:cut])[1]),
            (I.macd(self.closes)[2], I.macd(self.closes[:cut])[2]),
            (I.bollinger(self.closes)[0], I.bollinger(self.closes[:cut])[0]),
            (I.bollinger(self.closes)[3], I.bollinger(self.closes[:cut])[3]),
            (I.donchian(self.highs, self.lows, 20)[0], I.donchian(self.highs[:cut], self.lows[:cut], 20)[0]),
        ):
            _assert_series_equal(self, full[:cut], prefix)

        full_adx = I.adx(self.highs, self.lows, self.closes, 14)[0]
        prefix_adx = I.adx(self.highs[:cut], self.lows[:cut], self.closes[:cut], 14)[0]
        _assert_series_equal(self, full_adx[:cut], prefix_adx)

    def test_boundaries(self) -> None:
        self.assertTrue(all(value is None for value in I.ema([1, 2, 3], 5)))
        self.assertTrue(all(value is None for value in I.rsi([1, 2, 3], 14)))
        self.assertTrue(all(value is None for value in I.atr([1, 2], [0, 1], [1, 2], 14)))
        self.assertTrue(all(value is None for value in I.adx([1, 2], [0, 1], [1, 2], 14)[0]))
        sma_values = I.sma([1, 2, 3, 4], 4)
        self.assertEqual(sma_values[:3], [None, None, None])
        self.assertAlmostEqual(sma_values[3], 2.5)

    def test_numeric_contracts(self) -> None:
        values = [float(index) for index in range(1, 31)]
        self.assertAlmostEqual(I.sma(values, 5)[-1], 28.0)
        ema_values = I.ema(values, 5)
        self.assertAlmostEqual(ema_values[4], 3.0)
        self.assertGreater(ema_values[-1], ema_values[4])

        rising = [float(index) for index in range(1, 40)]
        self.assertEqual(I.rsi(rising, 14)[-1], 100.0)
        falling = list(reversed(rising))
        self.assertEqual(I.rsi(falling, 14)[-1], 0.0)

        closes = [100 + (index % 5) for index in range(60)]
        mid, upper, lower, width = I.bollinger(closes, 20, 2.0)
        self.assertGreaterEqual(upper[-1], mid[-1])
        self.assertLessEqual(lower[-1], mid[-1])
        self.assertGreaterEqual(width[-1], 0.0)

        highs = [value + 2 for value in closes]
        lows = [value - 2 for value in closes]
        donchian_upper, _, donchian_lower = I.donchian(highs, lows, 20)
        self.assertEqual(donchian_upper[-1], max(highs[-20:]))
        self.assertEqual(donchian_lower[-1], min(lows[-20:]))

        volume_ratio = I.volume_ratio([10.0] * 20 + [30.0], 20)
        self.assertAlmostEqual(volume_ratio, 3.0)
        last_ratio, avg_ratio = I.taker_buy_ratios([10.0] * 20 + [10.0], [5.0] * 20 + [6.0], 20)
        self.assertEqual(last_ratio, 0.6)
        self.assertGreater(avg_ratio, 0.5)

    def test_trend_classification(self) -> None:
        closes = [float(index) for index in range(1, 40)]
        self.assertEqual(I.trend_from_ema(closes, I.ema(closes, 20)), "up")
        self.assertEqual(I.trend_from_ema(list(reversed(closes)), I.ema(list(reversed(closes)), 20)), "down")


class UniverseTests(unittest.TestCase):
    def _tickers(self) -> list[dict[str, Any]]:
        rows = []
        for index, symbol in enumerate(WHITELIST):
            rows.append(
                {
                    "symbol": symbol,
                    "lastPrice": str(_price(symbol)),
                    "priceChangePercent": str(_PCT.get(symbol, 1.0)),
                    "quoteVolume": str(_volume(symbol)),
                    "highPrice": str(_price(symbol) * 1.02),
                    "lowPrice": str(_price(symbol) * 0.98),
                    "closeTime": 1_700_000_000_000,
                }
            )
        rows.append({**rows[0], "symbol": "SOXLUSDT", "quoteVolume": "9e10"})
        return normalize_tickers(rows)

    def test_deep_selection(self) -> None:
        selection = select_universe(self._tickers(), whitelist=WHITELIST)
        self.assertEqual(len(selection["deep"]), C.DEEP_MAX)
        symbols = [item["symbol"] for item in selection["deep"]]
        self.assertIn("BTCUSDT", symbols)
        self.assertIn("ZECUSDT", symbols)
        self.assertTrue(all(item["reason"] for item in selection["deep"]))
        self.assertEqual(len(selection["whitelist"]), 20)
        self.assertNotIn("SOXLUSDT", selection["whitelist"])
        self.assertTrue(set(selection["index"]).isdisjoint(symbols))

    def test_held_priority(self) -> None:
        selection = select_universe(
            self._tickers(), whitelist=WHITELIST, held=["DOTUSDT"], deep_max=8
        )
        first = selection["deep"][0]
        self.assertEqual(first["symbol"], "DOTUSDT")
        self.assertIn("held", first["reason"])

    def test_missing_ticker_omitted(self) -> None:
        tickers = [row for row in self._tickers() if row["symbol"] != "DOTUSDT"]
        selection = select_universe(tickers, whitelist=WHITELIST)
        self.assertIn("DOTUSDT", [item["symbol"] for item in selection["omitted"]])


def _scan_tickers() -> list[dict[str, Any]]:
    """三条真实形态：白名单内平盘、白名单外暴涨、老鼠仓（成交量不足）。"""

    def row(symbol: str, pct: float, quote_volume: float, **extra: Any) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "last": 1.0,
            "pct_24h": pct,
            "quote_volume": quote_volume,
            "high_24h": 1.0,
            "low_24h": 1.0,
            "close_time_ms": 1,
            **extra,
        }

    return [
        row("BTCUSDT", 1.2, C.MIN_QUOTE_VOLUME_USD * 10),
        row("PEPEUSDT", 42.0, C.MIN_QUOTE_VOLUME_USD * 2),
        row("TINYUSDT", 90.0, 1_000.0),
    ]


class MarketScanTests(unittest.TestCase):
    def test_outside_whitelist_move_is_visible(self) -> None:
        scan = scan_market(_scan_tickers(), whitelist=WHITELIST, anomaly_pct=C.ANOMALY_PCT)
        outside = [item["symbol"] for item in scan["outside_whitelist_anomalies"]]
        self.assertEqual(outside, ["PEPEUSDT"])
        top = {item["symbol"]: item for item in scan["top_gainers"]}
        self.assertFalse(top["PEPEUSDT"]["in_whitelist"])
        self.assertTrue(top["BTCUSDT"]["in_whitelist"])
        # 阈值只在"过了流动性下限"的集合里算；TINY 记在 below_volume_floor，不算异动。
        self.assertEqual(scan["anomaly_count"], 1)

    def test_volume_floor_keeps_noise_out(self) -> None:
        scan = scan_market(_scan_tickers(), whitelist=WHITELIST, anomaly_pct=C.ANOMALY_PCT)
        symbols = {item["symbol"] for item in scan["top_gainers"]}
        self.assertNotIn("TINYUSDT", symbols)
        self.assertEqual(scan["below_volume_floor"], 1)

    def test_deep_pool_stays_whitelist_only(self) -> None:
        """扫描是知情，不是扩容：白名单外的币不得进入可交易 deep 池。"""

        tickers = _scan_tickers()
        selection = select_universe(tickers, whitelist=WHITELIST)
        deep = {item["symbol"] for item in selection["deep"]}
        self.assertTrue(deep.issubset(set(WHITELIST)))
        self.assertNotIn("PEPEUSDT", deep)
        self.assertNotIn("PEPEUSDT", selection["index"])

    def test_missing_volume_is_skipped_not_zero(self) -> None:
        tickers = _scan_tickers()
        tickers.append({
            "symbol": "NOVOLUSDT",
            "last": 1.0,
            "pct_24h": 80.0,
            "quote_volume": None,
            "high_24h": None,
            "low_24h": None,
            "close_time_ms": None,
        })
        scan = scan_market(tickers, whitelist=WHITELIST, anomaly_pct=C.ANOMALY_PCT)
        symbols = {item["symbol"] for item in scan["top_gainers"]}
        self.assertNotIn("NOVOLUSDT", symbols, "缺成交量必须跳过，不能折算成 0 参与排序")


class QualityTests(unittest.TestCase):
    def test_verdicts(self) -> None:
        self.assertEqual(Q.verdict_of([]), "GOOD")
        self.assertEqual(Q.verdict_of([Q.issue("KLINE_GAP", "error", "BTCUSDT", "gap")]), "DEGRADED")
        warns = [Q.issue("TICKER_STALE", "warn", f"S{index}", "age") for index in range(4)]
        self.assertEqual(Q.verdict_of(warns), "DEGRADED")
        self.assertEqual(Q.verdict_of([], held_15m_unusable=True), "STALE")
        self.assertEqual(Q.verdict_of([Q.issue("IP_BANNED_418", "error", "rest", "banned")]), "BLOCKED")
        self.assertEqual(
            Q.verdict_of([Q.issue("CLOCK_SKEW", "error", "clock", "offset")]), "BLOCKED"
        )

    def test_bar_freshness_thresholds(self) -> None:
        warn = Q.bar_issues(
            "BTCUSDT:15m", "15m", {"count": 500, "needed": 500, "age_s": 1000, "gap": False}, held=False
        )
        self.assertEqual([item["code"] for item in warn], ["KLINE_STALE"])
        self.assertEqual(warn[0]["severity"], "warn")

        error = Q.bar_issues(
            "BTCUSDT:15m", "15m", {"count": 500, "needed": 500, "age_s": 2000, "gap": False}, held=False
        )
        self.assertEqual(error[0]["severity"], "error")

        insufficient = Q.bar_issues(
            "BTCUSDT:15m", "15m", {"count": 100, "needed": 500, "age_s": 10, "gap": False}, held=False
        )
        self.assertIn("KLINE_INSUFFICIENT", [item["code"] for item in insufficient])

    def test_transport_issues(self) -> None:
        self.assertEqual(Q.transport_issues({"state": "ok"}), [])
        self.assertEqual(Q.transport_issues({"state": "cooldown", "cooldown_s": 30})[0]["code"], "RATE_LIMITED_429")
        self.assertEqual(Q.transport_issues({"state": "banned", "cooldown_s": 60})[0]["code"], "IP_BANNED_418")

    def test_systemic_issue_deduped_for_verdict(self) -> None:
        many = [Q.issue("DERIVATIVES_PARTIAL", "warn", f"S{index}", "missing") for index in range(8)]
        self.assertEqual(Q.verdict_of(many), "GOOD")
        self.assertEqual(Q.score_of(many), 0.95)
        mixed = many + [Q.issue("KLINE_GAP", "error", "BTCUSDT", "gap")]
        self.assertEqual(Q.verdict_of(mixed), "DEGRADED")

    def test_summarize_health_returns_effective_issues(self) -> None:
        """T23-L01：对外 issues 与 score/verdict 使用同一去重集合。"""

        many = [Q.issue("DERIVATIVES_PARTIAL", "warn", f"S{index}", "missing") for index in range(8)]
        summary = Q.summarize_health(many)
        self.assertEqual(len(summary["issues"]), 1)
        self.assertEqual(summary["issues"][0]["code"], "DERIVATIVES_PARTIAL")
        self.assertEqual(summary["verdict"], "GOOD")
        self.assertEqual(summary["score"], 0.95)
        self.assertEqual(len(Q.summarize_health(many)["issues"]), 1)
        per_symbol = [Q.issue("KLINE_GAP", "error", f"S{index}", "gap") for index in range(3)]
        self.assertEqual(len(Q.summarize_health(per_symbol)["issues"]), 3)

    def test_score(self) -> None:
        self.assertEqual(Q.score_of([]), 1.0)
        self.assertEqual(Q.score_of([Q.issue("KLINE_GAP", "error", "x", "y")]), 0.75)
        self.assertEqual(
            Q.score_of([Q.issue("KLINE_GAP", "error", "x", "y"), Q.issue("TICKER_STALE", "warn", "x", "y")]),
            0.7,
        )


class _EvidenceFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._fixture = ServerFixture()
        cls.server = cls._fixture.__enter__()
        cls._tmp = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls._tmp.name)
        cls.service = _service(cls.server, cls.data_dir)
        cls.result = cls.service.build_evidence(
            cycle_id=FIXED_CYCLE, deep_symbols=["BTCUSDT"], held_symbols=["BTCUSDT"]
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.close()
        cls._fixture.__exit__(None, None, None)
        cls._tmp.cleanup()


class EvidenceTests(_EvidenceFixture):
    def test_build_and_persist(self) -> None:
        evidence = self.result["evidence"]
        self.assertEqual(evidence["schema_version"], "md-evidence/1")
        self.assertEqual(evidence["cycle_id"], FIXED_CYCLE)
        self.assertIn(self.result["verdict"], {"GOOD", "DEGRADED"})
        self.assertIn("BTCUSDT", evidence["symbols"])
        timeframes = evidence["symbols"]["BTCUSDT"]["timeframes"]
        for interval in ("15m", "1h", "4h", "1d"):
            self.assertIn(interval, timeframes)
        self.assertEqual(timeframes["15m"]["candles"]["n"], 20)
        self.assertEqual(evidence["data_health"]["sources"]["rest"]["state"], "ok")
        self.assertEqual(S.validate(evidence), [])
        self.assertTrue(verify_hash(evidence))
        self.assertEqual(self.result["sha256"], evidence["provenance"]["snapshot_hash"])

        from datetime import datetime, timezone
        day = datetime.fromtimestamp(self.result["evidence"]["produced_at_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        path = self.data_dir / "evidence" / day / FIXED_CYCLE / "evidence.json"
        self.assertTrue(path.exists())
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["provenance"]["snapshot_hash"], self.result["sha256"])
        self.assertEqual(len(on_disk["universe"]["whitelist"]), 20)

        connection = sqlite3.connect(self.data_dir / "workbench-v1.sqlite")
        try:
            row = connection.execute(
                "SELECT path, sha256, verdict FROM evidence_snapshots WHERE cycle_id=?",
                (FIXED_CYCLE,),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[1], self.result["sha256"])
        self.assertEqual(row[0], f"evidence/{day}/{FIXED_CYCLE}/evidence.json")

    def test_closed_bars_only(self) -> None:
        evidence = self.result["evidence"]
        server_time = evidence["as_of"]["server_time_ms"]
        last_closed = evidence["as_of"]["last_closed_bar"]["15m"]
        self.assertIsNotNone(last_closed)
        from datetime import datetime

        closed_ms = int(datetime.fromisoformat(last_closed.replace("Z", "+00:00")).timestamp() * 1000)
        self.assertLessEqual(closed_ms, server_time)
        self.assertGreaterEqual(evidence["as_of"]["bar_age_s"]["15m"], 0)

    def test_no_forbidden_fields(self) -> None:
        forbidden = {"api_key", "apikey", "secret", "signature", "account", "positions", "orders", "balance", "token"}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    self.assertNotIn(str(key).lower(), forbidden)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.result["evidence"])

    def test_schema_negative_cases(self) -> None:
        base = copy.deepcopy(self.result["evidence"])
        mutations = []
        broken = copy.deepcopy(base)
        broken.pop("data_health")
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["data_health"]["verdict"] = "MAYBE"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["unexpected"] = 1
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["snapshot_id"] = "ev-bad"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["symbols"]["BTCUSDT"]["price"]["last"] = 0
        mutations.append(broken)
        broken = copy.deepcopy(base)
        if not broken["data_health"]["issues"]:
            broken["data_health"]["issues"].append(
                {"code": "TICKER_STALE", "severity": "warn", "scope": "BTCUSDT", "detail": "x"}
            )
        broken["data_health"]["issues"][0]["severity"] = "fatal"
        mutations.append(broken)
        broken = copy.deepcopy(base)
        broken["delivery"]["order_gate_ttl_s"] = 120
        mutations.append(broken)
        for index, payload in enumerate(mutations):
            self.assertTrue(S.validate(payload), f"mutation {index} should fail schema")

    def test_hash_tamper_detected(self) -> None:
        tampered = copy.deepcopy(self.result["evidence"])
        tampered["market"]["regime"] = "trend_down"
        self.assertFalse(verify_hash(tampered))
        self.assertNotEqual(
            canonical_hash(tampered), canonical_hash(self.result["evidence"])
        )

    def test_token_budget_recorded(self) -> None:
        limits = self.result["evidence"]["limits"]
        self.assertEqual(limits["prompt_token_budget"], C.PROMPT_TOKEN_BUDGET)
        self.assertIsInstance(limits["prompt_token_estimate"], int)
        self.assertLessEqual(limits["prompt_token_estimate"], C.PROMPT_TOKEN_BUDGET)
        self.assertEqual(self.result["evidence"]["symbols"]["BTCUSDT"]["timeframes"]["15m"]["candles"]["n"], 20)

    def test_db_reference_failure_is_fail_closed(self) -> None:
        """T23-L04：DB 引用失败不得留下可被误读的 evidence.json。"""

        with tempfile.TemporaryDirectory() as tmp, ServerFixture() as server:
            service = _service(server, Path(tmp))
            cycle = "cyc-20261109T000000Z-dbfa11"
            try:
                with mock.patch.object(
                    EvidenceBuilder,
                    "_record_reference",
                    side_effect=sqlite3.OperationalError("disk I/O error"),
                ):
                    with self.assertRaises(EvidenceError) as context:
                        service.build_evidence(
                            cycle_id=cycle,
                            deep_symbols=["BTCUSDT"],
                            held_symbols=["BTCUSDT"],
                        )
                self.assertIn("PERSIST_FAILED", str(context.exception))
                matches = list((Path(tmp) / "evidence").glob(f"*/{cycle}/evidence.json"))
                temps = list((Path(tmp) / "evidence").glob(f"*/{cycle}/*.tmp"))
                self.assertEqual(matches, [])
                self.assertEqual(temps, [])
            finally:
                service.close()


class EvidenceFullPoolTests(_EvidenceFixture):
    def test_full_deep_pool_is_schema_valid(self) -> None:
        result = self.service.build_evidence(
            cycle_id="cyc-20261109T000001Z-abcdef", held_symbols=[]
        )
        evidence = result["evidence"]
        limits = evidence["limits"]
        self.assertEqual(S.validate(evidence), [])
        # T23-M02：硬预算必须收敛到 <= prompt_token_budget，而不是只标注。
        self.assertLessEqual(limits["prompt_token_estimate"], C.PROMPT_TOKEN_BUDGET)
        self.assertTrue(limits["truncated"])
        self.assertTrue(limits["dropped_fields"])
        deep = [entry["symbol"] for entry in evidence["universe"]["deep"]]
        self.assertGreaterEqual(len(deep), 1)
        self.assertLessEqual(len(deep), C.DEEP_MAX)
        self.assertIn("BTCUSDT", deep, "BTC 必须始终保留在 deep 中")
        self.assertLessEqual(len(evidence["universe"]["index"]), 20)
        for symbol in deep + evidence["universe"]["index"]:
            self.assertIn(symbol, evidence["symbols"])
        # 全市场扫描是证据的一部分（可选块），且它不能把白名单外的币塞进 deep。
        market_scan = evidence["universe"].get("market_scan")
        self.assertIsInstance(market_scan, dict)
        self.assertEqual(market_scan["threshold_pct"], C.ANOMALY_PCT)
        self.assertNotIn("cached", market_scan, "运行时缓存标记不进证据快照")
        for row in market_scan["top_gainers"]:
            if not row["in_whitelist"]:
                self.assertNotIn(row["symbol"], deep)
        if len(deep) < C.DEEP_MAX:
            self.assertTrue(any(item.startswith("deep.") for item in limits["dropped_fields"]))


class ApiMarketTests(_EvidenceFixture):
    def _client(self) -> tuple[TestClient, Any]:
        settings = dataclasses.replace(Settings.from_env(), data_dir=self.data_dir)
        app = create_app(settings)
        client = TestClient(app)
        client.__enter__()
        return client, app

    def _replace_service(self, app: Any, service: MarketService) -> None:
        # 不关闭旧 service（类级共享，后续用例仍要使用）
        app.state.market = service

    def test_health_clock_universe_klines_prices(self) -> None:
        client, app = self._client()
        try:
            self._replace_service(app, self.service)

            health = client.get("/api/market/health")
            self.assertEqual(health.status_code, 200, health.text)
            body = health.json()
            self.assertIn(body["verdict"], {"GOOD", "DEGRADED"})
            self.assertIn("issues", body)
            self.assertEqual(body["sources"]["rest"]["state"], "ok")
            second_health = client.get("/api/market/health")
            self.assertEqual(second_health.status_code, 200)
            self.assertTrue(second_health.json()["cached"], "API 默认应命中 health 10s 缓存")

            clock = client.get("/api/market/clock")
            self.assertEqual(clock.status_code, 200)
            self.assertIn("offset_ms", clock.json())

            universe = client.get("/api/market/universe")
            self.assertEqual(universe.status_code, 200)
            self.assertEqual(len(universe.json()["universe"]["whitelist"]), 20)

            # 全市场扫描：读路径，白名单外的成交额大户可见但标记为非白名单。
            # refresh=true 强制算一次：这个 service 是类级共享的，普通调用可能命中别的用例算过的缓存。
            scan = client.get("/api/market/scan", params={"refresh": "true"})
            self.assertEqual(scan.status_code, 200, scan.text)
            scan_body = scan.json()
            self.assertGreaterEqual(scan_body["total_symbols"], 20)
            self.assertIn("top_gainers", scan_body)
            self.assertIn("in_whitelist", scan_body["top_gainers"][0])
            self.assertFalse(scan_body["cached"], "refresh=true 不应报缓存命中")
            self.assertTrue(client.get("/api/market/scan").json()["cached"], "随后应命中 1h 缓存")

            klines = client.get(
                "/api/market/klines",
                params={"symbol": "BTCUSDT", "interval": "15m", "limit": 100},
            )
            self.assertEqual(klines.status_code, 200, klines.text)
            payload = klines.json()
            self.assertTrue(payload["closed_only"])
            self.assertGreaterEqual(payload["count"], 100)
            self.assertTrue(all(bar["final"] for bar in payload["bars"]))
            self.assertEqual(payload["meta"]["source"], "rest")

            prices = client.get("/api/market/prices", params={"symbols": "BTCUSDT,ETHUSDT"})
            self.assertEqual(prices.status_code, 200, prices.text)
            self.assertEqual(set(prices.json()["prices"]), {"BTCUSDT", "ETHUSDT"})
            self.assertIsNotNone(prices.json()["prices"]["BTCUSDT"]["last"])
        finally:
            client.__exit__(None, None, None)

    def test_error_mapping_429(self) -> None:
        with ServerFixture() as server:
            server.mode = "429"
            service = _service(server, Path(tempfile.mkdtemp()))
            client, app = self._client()
            try:
                self._replace_service(app, service)
                response = client.get("/api/market/klines", params={"symbol": "BTCUSDT"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "E_MARKET_COOLDOWN")
                self.assertEqual(response.headers.get("retry-after"), "7")
            finally:
                client.__exit__(None, None, None)
                service.close()

    def test_error_mapping_418(self) -> None:
        with ServerFixture() as server:
            server.mode = "418"
            service = _service(server, Path(tempfile.mkdtemp()))
            client, app = self._client()
            try:
                self._replace_service(app, service)
                response = client.get("/api/market/klines", params={"symbol": "BTCUSDT"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "E_MARKET_BANNED")
            finally:
                client.__exit__(None, None, None)
                service.close()

    def test_error_mapping_timeout(self) -> None:
        with ServerFixture() as server:
            server.mode = "timeout"
            settings = C.MarketSettings(base_url=server.base_url, data_dir=Path(tempfile.mkdtemp()), timeout_s=0.05)
            service = MarketService(settings, transport=_transport(server, timeout_s=0.05))
            client, app = self._client()
            try:
                self._replace_service(app, service)
                response = client.get("/api/market/klines", params={"symbol": "BTCUSDT"})
                self.assertEqual(response.status_code, 504)
                self.assertEqual(response.json()["error"]["code"], "E_MARKET_TIMEOUT")
            finally:
                client.__exit__(None, None, None)
                service.close()

    def test_error_mapping_budget(self) -> None:
        with ServerFixture() as server:
            transport = _transport(server, local_budget_1m=1)
            transport.get_json("/fapi/v1/time", weight=1)
            settings = C.MarketSettings(base_url=server.base_url, data_dir=Path(tempfile.mkdtemp()))
            service = MarketService(settings, transport=transport)
            client, app = self._client()
            try:
                self._replace_service(app, service)
                response = client.get("/api/market/klines", params={"symbol": "BTCUSDT"})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["error"]["code"], "E_MARKET_BUDGET_DEFERRED")
            finally:
                client.__exit__(None, None, None)
                service.close()

    def test_unknown_symbol_rejected(self) -> None:
        client, app = self._client()
        try:
            self._replace_service(app, self.service)
            response = client.get("/api/market/klines", params={"symbol": "SOXLUSDT"})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "E_SYMBOL_NOT_ALLOWED")
            bad_interval = client.get(
                "/api/market/klines", params={"symbol": "BTCUSDT", "interval": "2h"}
            )
            self.assertEqual(bad_interval.status_code, 400)
            self.assertEqual(bad_interval.json()["error"]["code"], "E_INVALID_INTERVAL")
        finally:
            client.__exit__(None, None, None)


class HealthCacheTests(unittest.TestCase):
    """T23-M01：health 默认走缓存、可强制刷新，且冷却时仍返回明确状态。"""

    def setUp(self) -> None:
        self._fixture = ServerFixture()
        self.server = self._fixture.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.service = _service(self.server, Path(self._tmp.name))

    def tearDown(self) -> None:
        self.service.close()
        self._fixture.__exit__(None, None, None)
        self._tmp.cleanup()

    def test_default_cache_and_force_refresh(self) -> None:
        first = self.service.health_payload()
        self.assertFalse(first["cached"])
        self.assertIn("last_probe", first)
        self.assertEqual(first["verdict"], "GOOD")
        requests_after_first = self.service.transport.stats()["requests"]

        second = self.service.health_payload()
        self.assertTrue(second["cached"])
        self.assertEqual(
            self.service.transport.stats()["requests"],
            requests_after_first,
            "缓存命中不得再发上游请求",
        )
        self.assertTrue(second["last_probe"]["cached"])

        forced = self.service.health_payload(refresh=True)
        self.assertFalse(forced["cached"])
        self.assertGreater(
            self.service.transport.stats()["requests"],
            requests_after_first,
            "refresh=True 必须保留强制刷新路径",
        )

    def test_health_reachable_during_cooldown(self) -> None:
        self.service.health_payload()  # 先建立 GOOD 缓存
        self.server.mode = "429"
        with self.assertRaises(MarketCooldown):
            self.service.transport.get_json("/fapi/v1/time", weight=1)
        payload = self.service.health_payload()  # 默认缓存路径，但冷却必须实时透出
        self.assertEqual(payload["sources"]["rest"]["state"], "cooldown")
        self.assertGreater(payload["cooldown_s"], 0)
        self.assertTrue(
            any(item["code"] == "RATE_LIMITED_429" for item in payload["issues"])
        )
        self.assertIn(payload["verdict"], {"DEGRADED", "STALE", "BLOCKED"})
        self.assertIsNotNone(payload["last_probe"])
        self.assertTrue(payload["last_probe"]["cooldown_hit"])
        self.assertIsNotNone(payload["data_budget"]["limit_5m"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
