"""WebSocket 行情的进程内只读快照。

网络协程只写入标准化快照，策略、情报和页面只读取副本。每条数据都带接收时间；
覆盖不完整或已经陈旧时返回空结果，让调用方决定是否走受控 REST，而不是混用
新旧行情生成不可解释的决策。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable

_LOCK = threading.RLock()
_TICKERS: dict[str, tuple[float, dict[str, Any]]] = {}
_MARKS: dict[str, tuple[float, dict[str, Any]]] = {}
_BOOKS: dict[str, tuple[float, dict[str, Any]]] = {}


def push_ticker(payload: dict[str, Any]) -> None:
    symbol = str(payload.get("s") or "").upper()
    if not symbol:
        return
    row = {
        "symbol": symbol,
        "lastPrice": payload.get("c"),
        "priceChangePercent": payload.get("P"),
        "quoteVolume": payload.get("q"),
        "highPrice": payload.get("h"),
        "lowPrice": payload.get("l"),
        "closeTime": payload.get("E") or payload.get("C"),
        "source": "websocket",
    }
    with _LOCK:
        _TICKERS[symbol] = (time.time(), row)


def push_mark(payload: dict[str, Any]) -> None:
    symbol = str(payload.get("s") or "").upper()
    if not symbol:
        return
    row = {
        "symbol": symbol,
        "markPrice": payload.get("p"),
        "indexPrice": payload.get("i"),
        "lastFundingRate": payload.get("r"),
        "nextFundingTime": payload.get("T"),
        "time": payload.get("E"),
        "source": "websocket",
    }
    with _LOCK:
        _MARKS[symbol] = (time.time(), row)


def push_book(payload: dict[str, Any]) -> None:
    symbol = str(payload.get("s") or "").upper()
    if not symbol:
        return
    row = {
        "symbol": symbol,
        "bidPrice": payload.get("b"),
        "bidQty": payload.get("B"),
        "askPrice": payload.get("a"),
        "askQty": payload.get("A"),
        "time": payload.get("E") or payload.get("T"),
        "source": "websocket",
    }
    with _LOCK:
        _BOOKS[symbol] = (time.time(), row)


def _fresh_rows(store: dict[str, tuple[float, dict[str, Any]]],
                symbols: Iterable[str], max_age: float) -> list[dict[str, Any]]:
    wanted = list(dict.fromkeys(str(s).upper() for s in symbols if s))
    if not wanted:
        return []
    now = time.time()
    rows: list[dict[str, Any]] = []
    with _LOCK:
        for symbol in wanted:
            item = store.get(symbol)
            if item is None or now - item[0] > max(0.1, float(max_age)):
                return []
            rows.append(dict(item[1]))
    return rows


def get_tickers(symbols: Iterable[str], max_age: float = 20.0) -> list[dict[str, Any]]:
    return _fresh_rows(_TICKERS, symbols, max_age)


def get_marks(symbols: Iterable[str], max_age: float = 20.0) -> list[dict[str, Any]]:
    return _fresh_rows(_MARKS, symbols, max_age)


def get_books(symbols: Iterable[str], max_age: float = 10.0) -> list[dict[str, Any]]:
    return _fresh_rows(_BOOKS, symbols, max_age)


def stream_cache_status() -> dict[str, Any]:
    now = time.time()
    with _LOCK:
        newest = max(
            [item[0] for store in (_TICKERS, _MARKS, _BOOKS) for item in store.values()],
            default=None,
        )
        return {
            "ticker_symbols": len(_TICKERS),
            "mark_symbols": len(_MARKS),
            "book_symbols": len(_BOOKS),
            "last_update_at": newest,
            "last_update_age_seconds": round(now - newest, 3) if newest else None,
        }


def _reset_for_test() -> None:
    with _LOCK:
        _TICKERS.clear()
        _MARKS.clear()
        _BOOKS.clear()
