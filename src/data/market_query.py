"""主 Agent 按需行情查询工具。"""
from __future__ import annotations

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

# 警告：不要在本模块再定义名为 `_get_json` 的函数（会遮蔽这里的导入，
# 使 query_symbol_detail 100% 抛 TypeError）。共享连接版请叫 `_shared_get`。
from data.binance_public import (
    _atr,
    _get_json,
    _trend_from_ema,
    get_all_24h_tickers,
    prepare_closed_klines,
    select_top_symbols,
)
from data.binance_transport import request_json
from data.kline_store import get_klines
from data.market_stream_cache import get_marks, get_tickers
from data.rate_limit import cooldown_remaining
from core.trading_scope import TradingScope

BASE_URL = "https://fapi.binance.com"
VALID_INTERVALS = {"1m", "5m", "15m", "1h", "4h", "1d"}

_TOOL_CACHE: dict[tuple, tuple[float, Any]] = {}
_TOOL_CACHE_LOCK = threading.Lock()
_TOOL_CACHE_TTL = 30.0


def _cache_get(key: tuple) -> Any | None:
    with _TOOL_CACHE_LOCK:
        item = _TOOL_CACHE.get(key)
        if item and time.time() - item[0] < _TOOL_CACHE_TTL:
            return item[1]
    return None


def _cache_set(key: tuple, value: Any) -> None:
    with _TOOL_CACHE_LOCK:
        _TOOL_CACHE[key] = (time.time(), value)


_SETTINGS_PATH = Path(__file__).resolve().parents[2] / "config" / "settings.yaml"
_CORE_SYMBOLS: list[str] | None = None


def _core_symbols() -> list[str]:
    """读取核心池白名单（进程内缓存，避免每次查工具都读盘）。

    核心池是「我们实际会交易的品种」白名单。`query_top_symbols` 的返回会被主 Agent
    当作可交易标的的权威来源——orchestrator 提示词里明确写着「以 query_top_symbols /
    实际下单结果为准」——所以绝不能把美股/商品代币化合约（SOXLUSDT 半导体ETF、
    XAGUSDT 白银、XAUUSDT 黄金、SKHYNIXUSDT、SNDKUSDT、SPCXUSDT 等）混进返回列表。
    它们在美股时段成交额会冲进全市场前十，但历史订单一次都没有，真下单就是事故。

    读不到配置时返回空列表；调用方必须 fail closed，不能再退回全市场扫描。
    """
    global _CORE_SYMBOLS
    if _CORE_SYMBOLS is None:
        try:
            cfg = yaml.safe_load(_SETTINGS_PATH.read_text(encoding="utf-8")) or {}
            raw_symbols = (cfg.get("market_universe", {}) or {}).get("core_symbols") or []
            _CORE_SYMBOLS = list(TradingScope.from_symbols(raw_symbols).symbols)
        except Exception:
            _CORE_SYMBOLS = []
    return _CORE_SYMBOLS


def _configured_scope() -> TradingScope | None:
    """返回行情工具使用的范围；配置不可用时返回 None 触发 fail closed。"""
    symbols = _core_symbols()
    if not symbols:
        return None
    try:
        return TradingScope.from_symbols(symbols)
    except ValueError:
        return None


def _fmt_symbol(symbol: str) -> str:
    s = str(symbol).strip().upper()
    if not s.endswith("USDT"):
        s = s + "USDT"
    return s


def _sig(x: Any, n: int = 6) -> float:
    """保留 n 位有效数字：去掉交易所返回的浮点噪声，显著降低 token 占用。"""
    try:
        return float(f"{float(x):.{n}g}")
    except (TypeError, ValueError):
        return 0.0


def _pack_candles(klines: list[list[Any]], interval: str, max_candles: int = 20) -> dict[str, Any]:
    """K 线列式压缩：时间戳改相对秒、价格按有效数字取整，同样信息约省 60% token。

    例：[{"open_time":..., "open":..., ...} × 20]  ->  {"t_sec":[0,900,...], "o":[...], ...}
    相对时间戳保留顺序与间隔信息，模型读结构不需要绝对 epoch。
    """
    ks = klines[-max_candles:]
    if not ks:
        return {"n": 0, "interval": interval}
    t0 = int(ks[0][0])
    return {
        "n": len(ks),
        "interval": interval,
        "t0_epoch_ms": t0,
        "t_sec": [round((int(k[0]) - t0) / 1000.0) for k in ks],
        "o": [_sig(k[1]) for k in ks],
        "h": [_sig(k[2]) for k in ks],
        "l": [_sig(k[3]) for k in ks],
        "c": [_sig(k[4]) for k in ks],
        "v": [_sig(k[5], 4) for k in ks],
    }


def query_symbol_detail(symbol: str, interval: str = "15m", limit: int = 200,
                        include_candles: bool = True) -> dict[str, Any]:
    symbol = _fmt_symbol(symbol)
    scope = _configured_scope()
    if scope is None:
        return {"error": "trading_scope_unavailable，已拒绝行情工具请求"}
    if not scope.allows_open(symbol):
        return {"error": f"{symbol} 不在当前业务开仓白名单内，已拒绝行情工具请求"}
    interval = interval if interval in VALID_INTERVALS else "15m"
    limit = max(20, min(int(limit), 500))
    cache_key = ("detail", symbol, interval, limit, include_candles)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    with httpx.Client(timeout=20.0) as client:
        streamed_ticker = get_tickers([symbol], max_age=30.0)
        ticker = (streamed_ticker[0] if streamed_ticker else
                  _get_json(client, "/fapi/v1/ticker/24hr", {"symbol": symbol}))
        klines = get_klines(client, symbol, interval, limit, fetcher=_get_json)
        try:
            streamed_mark = get_marks([symbol], max_age=30.0)
            premium = (streamed_mark[0] if streamed_mark else
                       _get_json(client, "/fapi/v1/premiumIndex", {"symbol": symbol}))
        except Exception:
            premium = {}
        try:
            oi = _get_json(client, "/fapi/v1/openInterest", {"symbol": symbol})
        except Exception:
            oi = {}

    observed_at_ms = int(time.time() * 1000)
    closed_klines, live_bar, data_quality = prepare_closed_klines(
        klines, interval, observed_at_ms=observed_at_ms, min_closed=20,
    )
    ticker_price = ticker.get("lastPrice")
    mark_price = premium.get("markPrice")
    live_price_available = bool(ticker_price or mark_price)
    data_quality["sources"] = {
        "ticker": {
            "available": bool(ticker_price),
            "source_at_ms": ticker.get("closeTime"),
            "observed_at_ms": observed_at_ms,
        },
        "mark_funding": {
            "available": bool(premium),
            "source_at_ms": premium.get("time"),
            "observed_at_ms": observed_at_ms,
        },
        "open_interest": {
            "available": bool(oi),
            "source_at_ms": oi.get("time"),
            "observed_at_ms": observed_at_ms,
        },
    }
    if not live_price_available:
        data_quality["issues"].append("live_price_unavailable")
        data_quality["ok"] = False
    data_quality["optional_issues"] = [
        name for name in ("mark_funding", "open_interest")
        if not data_quality["sources"][name]["available"]
    ]
    if not data_quality["ok"]:
        return {
            "error": "closed kline data is stale or incomplete",
            "symbol": symbol,
            "interval": interval,
            "data_quality": data_quality,
        }

    closes = [float(k[4]) for k in closed_klines]
    highs = [float(k[2]) for k in closed_klines]
    lows = [float(k[3]) for k in closed_klines]
    vols = [float(k[7]) for k in closed_klines]
    last_closed = closes[-1]
    last = float(ticker_price or mark_price or last_closed)
    atr = _atr(highs, lows, closes)
    atr_pct = (atr / last_closed * 100) if atr and last_closed else 0.0
    recent_vol = vols[-1]
    base_vol = statistics.fmean(vols[-21:-1]) if len(vols) >= 21 else 0.0
    volume_ratio = recent_vol / base_vol if base_vol > 0 else 0.0
    high_window = max(highs[-limit:])
    low_window = min(lows[-limit:])
    funding_rate = float(premium.get("lastFundingRate") or 0.0)
    oi_usd = float(oi.get("openInterest") or 0.0) * last if oi else None

    result = {
        "symbol": symbol,
        "interval": interval,
        "limit": limit,
        "last_price": last,
        "last_closed_price": last_closed,
        "pct_24h": float(ticker.get("priceChangePercent") or 0.0),
        "quote_volume_24h": float(ticker.get("quoteVolume") or 0.0),
        "trend": _trend_from_ema(closes, 20 if interval not in ("1m",) else 50),
        "atr": round(atr, 8) if atr else None,
        "atr_pct": round(atr_pct, 2),
        "volume_ratio": round(volume_ratio, 2),
        "high_window": high_window,
        "low_window": low_window,
        "range_position_pct": round((last - low_window) / (high_window - low_window) * 100, 1)
        if high_window > low_window else 50.0,
        "return_window_pct": round((closes[-1] / closes[0] - 1) * 100, 2) if len(closes) >= 2 else 0.0,
        "funding_rate": funding_rate,
        "mark_price": float(premium.get("markPrice") or 0.0),
        "open_interest_usd": oi_usd,
        "candles": (_pack_candles(closed_klines, interval)
                    if include_candles else {"n": 0, "interval": interval}),
        "live_bar": (_pack_candles([live_bar], interval, max_candles=1)
                     if live_bar is not None else {"n": 0, "interval": interval}),
        "data_quality": data_quality,
    }
    _cache_set(cache_key, result)
    return result


def query_guardian_closed_bar(symbol: str, interval: str = "15m") -> dict[str, Any]:
    """只取 Guardian 所需的最新闭合 K 线，避免持仓复核触发整套行情查询。

    当前形成中的 K 线永远单独隔离；缺失、断档或陈旧时返回不可评估，调用方不得
    用 ticker/mark price 猜测“收盘已突破”。
    """
    symbol = _fmt_symbol(symbol)
    interval = interval if interval in VALID_INTERVALS else "15m"
    cache_key = ("guardian_closed_bar", symbol, interval)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    observed_at_ms = int(time.time() * 1000)
    try:
        with httpx.Client(timeout=12.0) as client:
            rows = get_klines(client, symbol, interval, 24, fetcher=_get_json)
        closed, live_bar, quality = prepare_closed_klines(
            rows, interval, observed_at_ms=observed_at_ms, min_closed=3,
        )
    except Exception as exc:
        return {
            "symbol": symbol, "interval": interval, "ok": False,
            "state": "REVIEW_DUE", "error": f"{type(exc).__name__}: {exc}",
            "observed_at_ms": observed_at_ms,
        }
    result = {
        "symbol": symbol, "interval": interval, "ok": bool(quality.get("ok")),
        "state": "ACTIVE" if quality.get("ok") else "REVIEW_DUE",
        "closed_price": float(closed[-1][4]) if closed else None,
        "closed_open_time_ms": int(closed[-1][0]) if closed else None,
        "closed_at_ms": int(closed[-1][6]) if closed else None,
        "has_live_bar": live_bar is not None,
        "observed_at_ms": observed_at_ms,
        "data_quality": quality,
    }
    _cache_set(cache_key, result)
    return result


def query_symbol_cards(symbols: list[str], top_n: int = 20) -> dict[str, Any]:
    """批量轻量指标卡：无 K 线，一次最多 20 个，适合先宽扫再深挖。"""
    scope = _configured_scope()
    if scope is None:
        return {"error": "trading_scope_unavailable，已拒绝行情工具请求"}
    syms = [_fmt_symbol(s) for s in (symbols or [])]
    syms = list(dict.fromkeys(syms))[:max(1, min(int(top_n), 40))]
    if not syms:
        return {"error": "symbols 列表不能为空，请给出至少一个交易对"}
    rejected = [symbol for symbol in syms if not scope.allows_open(symbol)]
    if rejected:
        return {"error": f"symbol 不在当前业务开仓白名单内，已拒绝: {', '.join(rejected)}"}
    cache_key = ("cards", tuple(syms))
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        tickers = get_all_24h_tickers(symbols=syms)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    by = {str(t.get("symbol")): t for t in tickers}
    rows = []
    for s in syms:
        t = by.get(s)
        if not t:
            rows.append({"symbol": s, "error": "无 24h 行情（可能是不存在的交易对）"})
            continue
        rows.append({
            "symbol": s,
            "last_price": _sig(t.get("lastPrice") or 0),
            "pct_24h": _sig(t.get("priceChangePercent") or 0, 4),
            "quote_volume_24h": _sig(t.get("quoteVolume") or 0, 4),
            "high_24h": _sig(t.get("highPrice") or 0),
            "low_24h": _sig(t.get("lowPrice") or 0),
        })
    result = {"n": len(rows), "symbols": rows}
    _cache_set(cache_key, result)
    return result


def query_top_symbols(top_n: int = 20) -> dict[str, Any]:
    top_n = max(5, min(int(top_n), 40))
    cache_key = ("top", top_n)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    scope = _configured_scope()
    if scope is None:
        return {"error": "trading_scope_unavailable，已拒绝全市场回退"}
    core_symbols = list(scope.symbols)
    tickers = get_all_24h_tickers(symbols=core_symbols)
    # 只在核心池内选：这份列表会被主 Agent 当作可交易标的的权威来源
    symbols = select_top_symbols(tickers, top_n=top_n, core_symbols=core_symbols)
    rows = []
    for t in tickers:
        if t["symbol"] in symbols:
            rows.append({
                "symbol": t["symbol"],
                "last_price": float(t.get("lastPrice") or 0),
                "pct_24h": float(t.get("priceChangePercent") or 0),
                "quote_volume_24h": float(t.get("quoteVolume") or 0),
            })
    rows.sort(key=lambda x: x["quote_volume_24h"], reverse=True)
    result = {"top_n": top_n, "symbols": rows}
    _cache_set(cache_key, result)
    return result


_PRICE_CACHE: dict[str, tuple[float, float]] = {}
_PRICE_LOCK = threading.Lock()
_ALL_PRICE_CACHE: dict[str, Any] = {"ts": 0.0, "prices": {}}

# 行情请求必须复用长连接。用模块级 httpx.get() 会每次新建 Client，
# 每次都要重做一遍 TLS 握手 + 代理 CONNECT 握手（实测单次固定开销 ~3.5 秒，
# 无论查 1 个币还是 706 个币都一样），页面价格因此几分钟才动一次。
_CLIENT_LOCK = threading.Lock()
_CLIENT: httpx.Client | None = None

# 少于等于这个数量的 symbol 走并发单点查询；超过则拉全市场快照（权重更省）。
# 持仓刷新只有两三个币，走单点可以把流量从 150KB 降到几百字节。
_SINGLE_QUERY_MAX = 6


def _shared_client() -> httpx.Client:
    """单例 Client，保持 keep-alive；连接失效时自动重建。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT.is_closed:
            _CLIENT = httpx.Client(
                timeout=httpx.Timeout(6.0, connect=5.0),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4,
                                    keepalive_expiry=30.0),
                headers={"Connection": "keep-alive"},
            )
        return _CLIENT


def _reset_client() -> None:
    """连接被对端强制断开（WinError 10054）等情况下丢弃旧连接。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:
                pass
        _CLIENT = None


def _shared_get(url: str, params: dict | None = None, timeout: float = 6.0) -> httpx.Response:
    """带一次连接重建重试的 GET：处理代理回收 keep-alive 后的 WinError 10054。

    注意：本函数严禁命名为 `_get_json`。
    本模块顶部从 data.binance_public 导入了同名的 `_get_json(client, path, params)`，
    （那个版本带限流冷却、418 处理、指数退避重试，query_symbol_detail 依赖它）。
    Python 在函数体内按名字解析，模块级 def 会覆盖 import 的同名对象：
    一旦这里也叫 `_get_json`，query_symbol_detail 会静默改调用本函数，
    把 httpx.Client 当成 url 传进来 -> TypeError: Invalid type for url，
    导致该工具 100% 失败、主 Agent 连续重试到轮次耗尽后整体 no_trade。
    """
    try:
        return _shared_client().get(url, params=params, timeout=timeout)
    except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
        _reset_client()
        return _shared_client().get(url, params=params, timeout=timeout)


def _fetch_price(symbol: str) -> float | None:
    if cooldown_remaining() > 0:
        return None
    try:
        payload = request_json(
            _shared_client(), "/fapi/v1/ticker/price", {"symbol": symbol},
            timeout=3.0, attempts=2, priority="normal", base_url=BASE_URL,
        )
        return float(payload.get("price") or 0.0)
    except Exception:
        return None


def _fetch_all_prices() -> dict[str, float]:
    data = request_json(
        _shared_client(), "/fapi/v1/ticker/price", timeout=6.0,
        attempts=2, priority="background", base_url=BASE_URL,
    )
    out: dict[str, float] = {}
    for item in data or []:
        symbol = str(item.get("symbol") or "")
        if not symbol.endswith("USDT"):
            continue
        try:
            out[symbol] = float(item.get("price") or 0.0)
        except (TypeError, ValueError):
            continue
    return out


def push_stream_price(symbol: str, price: float) -> None:
    """WebSocket 推送到达时直接写入价格缓存。

    推送链路可用时，缓存始终新鲜，`get_live_prices` 会直接命中、不再发 REST 请求；
    推送断开后缓存自然过期，自动回落到 REST 轮询，无需任何额外切换逻辑。
    """
    if not symbol or not price:
        return
    try:
        value = float(price)
    except (TypeError, ValueError):
        return
    if value <= 0:
        return
    with _PRICE_LOCK:
        _PRICE_CACHE[str(symbol).upper()] = (time.time(), value)


def get_live_prices(symbols: list[str], ttl_seconds: float = 5.0) -> dict[str, float]:
    now = time.time()
    missing: list[str] = []
    out: dict[str, float] = {}
    with _PRICE_LOCK:
        for s in symbols:
            item = _PRICE_CACHE.get(s)
            if item and now - item[0] < ttl_seconds:
                out[s] = item[1]
            else:
                # 先带回过期但有效的最后价格；后续仍会尝试刷新。
                # 若当前请求失败，UI 至少保留旧值而不是退回开仓价/显示为 0。
                if item:
                    out[s] = item[1]
                missing.append(s)

    if missing and len(missing) <= _SINGLE_QUERY_MAX:
        # 持仓刷新这类高频场景：symbol 很少，并发单点查询最快也最省流量。
        # 全市场快照 weight 同样是 2，但一次要下 150KB、解析 706 条，1 秒一次轮询时太浪费。
        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as pool:
            results = list(pool.map(lambda s: (s, _fetch_price(s)), missing))
        with _PRICE_LOCK:
            for s, price in results:
                if price:
                    _PRICE_CACHE[s] = (now, price)
                    out[s] = price
        missing = [s for s, price in results if not price]

    if missing:
        # 选币扫描等低频场景：一次请求拿全市场价格，避免逐个 symbol 请求造成卡顿
        with _PRICE_LOCK:
            if now - float(_ALL_PRICE_CACHE["ts"]) < ttl_seconds and _ALL_PRICE_CACHE["prices"]:
                all_prices = _ALL_PRICE_CACHE["prices"]
            else:
                try:
                    all_prices = _fetch_all_prices()
                    _ALL_PRICE_CACHE["ts"] = now
                    _ALL_PRICE_CACHE["prices"] = all_prices
                except Exception:
                    # 网络抖动时先用最近一次全市场快照，页面不应因为一次 502/超时清空或卡死。
                    all_prices = dict(_ALL_PRICE_CACHE.get("prices") or {})
        if all_prices:
            for s in missing:
                price = all_prices.get(s)
                if price:
                    _PRICE_CACHE[s] = (now, price)
                    out[s] = price
            # 只有确实拿到本轮新价格的 symbol 才算完成；out 里也可能是过期兜底值。
            missing = [s for s in missing if not all_prices.get(s)]

    if missing:
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = pool.map(lambda s: (s, _fetch_price(s)), missing)
        with _PRICE_LOCK:
            for s, price in results:
                if price:
                    _PRICE_CACHE[s] = (now, price)
                    out[s] = price
    return out

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "query_symbol_cards",
            "description": "批量查询多只币的轻量指标卡（最新价/24h涨跌/24h成交额/24h高低点），不含K线。"
                         "用于在一轮里宽泛筛选候选币；token 成本约为 query_symbol_detail 的 1/10。"
                         "确认候选后再用 query_symbol_detail 深挖。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"},
                                "description": "交易对列表，如 ['BTCUSDT','ETHUSDT']，最多 20 个"},
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_symbol_detail",
            "description": "查询单个交易对的分钟级/小时级数据：实时K线（列式压缩，含OHLCV与相对时间戳）、"
                         "ATR、量能比、资金费率、OI、窗口高低点等。symbol 如 BTCUSDT；"
                         "interval 可选 1m/5m/15m/1h/4h/1d。只对候选币调用，每轮预算有限。",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "交易对，例如 BTCUSDT、ETHUSDT"},
                    "interval": {"type": "string", "enum": list(VALID_INTERVALS), "description": "K线周期"},
                    "limit": {"type": "integer", "minimum": 20, "maximum": 500, "description": "K线根数（返回时只保留最近 20 根）"},
                    "include_candles": {"type": "boolean", "description": "是否返回K线列，false 时只返回指标（更省 token）"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_top_symbols",
            "description": "按24小时成交额查询当前最活跃的可交易 USDT 永续合约列表"
                           "（限核心池白名单内，即我们实际会交易的品种；"
                           "不含美股/商品代币化合约）。可用它确认标的能否交易。",
            "parameters": {
                "type": "object",
                "properties": {
                    "top_n": {"type": "integer", "minimum": 5, "maximum": 40, "description": "返回数量"},
                },
                "required": [],
            },
        },
    },
]

TOOL_HANDLERS = {
    "query_symbol_cards": query_symbol_cards,
    "query_symbol_detail": query_symbol_detail,
    "query_top_symbols": query_top_symbols,
}


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"unknown tool {name}"}
    try:
        result = handler(**arguments)
        if isinstance(result, dict):
            return result
        return {"result": result}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
