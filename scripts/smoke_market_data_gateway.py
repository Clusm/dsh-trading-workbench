"""统一行情网关、WebSocket快照与增量K线缓存离线回归。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data import kline_store, market_stream_cache  # noqa: E402
from data.price_stream import PriceStream  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


print("\n[1] WebSocket一次覆盖白名单价格、标记价格和全部策略周期")
market_stream_cache._reset_for_test()
kline_store._reset_for_test()
stream = PriceStream(lambda: ["ETHUSDT", "BTCUSDT"])
names = stream._market_stream_names()
check("两个标的只生成8条常规行情流", len(names) == 8)
check("只订阅基础K线周期", "btcusdt@kline_1m" in names and
      "btcusdt@kline_15m" in names and "btcusdt@kline_8h" not in names)
check("bookTicker使用独立public流", stream._book_stream_names() ==
      ["btcusdt@bookTicker", "ethusdt@bookTicker"])

stream._handle_frame(json.dumps({"data": {
    "e": "24hrTicker", "E": int(time.time() * 1000), "s": "BTCUSDT",
    "c": "70000", "P": "2.5", "q": "1000000", "h": "71000", "l": "68000",
}}))
stream._handle_frame(json.dumps({"data": {
    "e": "markPriceUpdate", "E": int(time.time() * 1000), "s": "BTCUSDT",
    "p": "70001", "i": "70000", "r": "0.0001", "T": 1893456000000,
}}))
stream._handle_book_frame(json.dumps({"data": {
    "e": "bookTicker", "E": int(time.time() * 1000), "s": "BTCUSDT",
    "b": "70000", "B": "2", "a": "70001", "A": "1",
}}))
check("ticker快照可读", market_stream_cache.get_tickers(["BTCUSDT"])[0]["lastPrice"] == "70000")
check("mark快照可读", market_stream_cache.get_marks(["BTCUSDT"])[0]["markPrice"] == "70001")
check("book快照可读", market_stream_cache.get_books(["BTCUSDT"])[0]["askPrice"] == "70001")
check("覆盖不完整时拒绝混用新旧快照",
      market_stream_cache.get_tickers(["BTCUSDT", "ETHUSDT"]) == [])

print("\n[2] K线首次回补后命中缓存，WebSocket继续增量更新")
cache_dir = Path(tempfile.mkdtemp(prefix="mta-kline-store-"))
kline_store.configure_kline_store(cache_dir)
calls: list[dict] = []
interval_ms = 60_000
last_open = int(time.time() * 1000) // interval_ms * interval_ms


def fake_fetch(_client, _path, params):
    calls.append(dict(params))
    count = int(params["limit"])
    step = 900_000 if params["interval"] == "15m" else interval_ms
    current_open = int(time.time() * 1000) // step * step
    first_open = current_open - (count - 1) * step
    return [
        [open_ms, "1", "2", "0.5", "1.5", "10", open_ms + step - 1,
         "15", 1, "5", "7", "0"]
        for open_ms in range(first_open, current_open + 1, step)
    ]


first = kline_store.get_klines(object(), "BTCUSDT", "1m", 20, fetcher=fake_fetch)
second = kline_store.get_klines(object(), "BTCUSDT", "1m", 20, fetcher=fake_fetch)
check("首次完整回补20根", len(first) == 20 and calls[0]["limit"] == 20)
check("当前bar仍新鲜时第二次零REST", len(second) == 20 and len(calls) == 1)
check("K线快照已原子持久化", (cache_dir / "BTCUSDT_1m.json").is_file())

# 预热15m聚合底座；生产中市场简报首次读取会完成同一步。
kline_store.get_klines(object(), "BTCUSDT", "15m", 80, fetcher=fake_fetch)

stream._handle_frame(json.dumps({"data": {"e": "kline", "E": int(time.time() * 1000),
    "s": "BTCUSDT", "k": {"s": "BTCUSDT", "i": "1m", "t": last_open,
        "T": last_open + interval_ms - 1, "o": "1", "h": "2.1", "l": "0.5",
        "c": "1.6", "v": "11", "q": "17", "n": 2, "V": "6", "Q": "8",
        "B": "0", "x": False}}}))
open_15m = int(time.time() * 1000) // 900_000 * 900_000
stream._handle_frame(json.dumps({"data": {"e": "kline", "E": int(time.time() * 1000),
    "s": "BTCUSDT", "k": {"s": "BTCUSDT", "i": "15m", "t": open_15m,
        "T": open_15m + 899_999, "o": "1", "h": "2.1", "l": "0.5",
        "c": "1.6", "v": "11", "q": "17", "n": 2, "V": "6", "Q": "8",
        "B": "0", "x": False}}}))
status = kline_store.kline_store_status()
check("WebSocket更新进入同一K线缓存", status["websocket_updates"] == 2 and status["rows"] >= 21)
check("15m同步聚合全部高周期", status["aggregated_updates"] == 5)
check("行情流状态统计完整", stream.status()["messages"] == 4 and
      stream.status()["book_ticker_active"] is True)

print("\n结果：PASS 13 / FAIL 0")
