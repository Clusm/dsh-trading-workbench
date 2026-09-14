"""币安 U 本位永续公开行情客户端，无需 API key。"""
from __future__ import annotations

import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Mapping

import httpx

from agents.models import MarketBrief
from data.binance_transport import request_json
from data.cache import SnapshotCache
from data.kline_store import BarMeta, get_bar_metadata, get_klines
from data.market_stream_cache import get_books, get_marks, get_tickers

BASE_URL = "https://fapi.binance.com"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

INTERVAL_SECONDS = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "1d": 24 * 60 * 60,
}

# ticker 全量接口失败时的兜底采集列表。
# 必须与 config/settings.yaml 的 market_universe.core_symbols 保持一致，
# 顺序按 24h 成交额从高到低 —— 上层会 [:analyze_top] 截断，顺序错了会先砍掉活跃品种。
# 尤其不能漏掉 ZECUSDT：它是实际持仓品种，兜底时被砍掉就等于决策看不到自己的仓位。
FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT", "ARBUSDT",
    "UNIUSDT", "DOGEUSDT", "BNBUSDT", "NEARUSDT", "SUIUSDT", "FILUSDT",
    "ADAUSDT", "LINKUSDT", "OPUSDT", "TRXUSDT", "AAVEUSDT", "AVAXUSDT",
    "LTCUSDT", "DOTUSDT",
]

STABLE_BASES = {"USDC", "TUSD", "BUSD", "FDUSD", "USDP", "DAI", "EUR", "AEUR"}

# 全量 24h ticker 权重明显高于单 symbol 请求。市场池与市场简报可能在启动时并发读取，
# 用进程内单飞 + 短 TTL 确保同一时刻只打一次，避免共享代理 IP 被重复权重推入 429/418。
_ALL_TICKERS_LOCK = threading.Lock()
_ALL_TICKERS_CACHE: tuple[float, list[dict[str, Any]]] = (0.0, [])
_ALL_TICKERS_TTL_SECONDS = 30.0


def _get_json(client: httpx.Client, path: str, params: dict[str, Any] | None = None,
              timeout: float = 15.0, attempts: int = 4,
              deadline: float | None = None) -> Any:
    """兼容旧调用签名，实际发送统一委托给权重感知传输层。"""
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("market tool deadline exceeded")
        timeout = min(timeout, max(0.1, remaining))
    return request_json(
        client, path, params, timeout=timeout, attempts=attempts,
        priority="normal", base_url=BASE_URL, deadline=deadline,
    )


def get_all_24h_tickers(client: httpx.Client | None = None,
                        symbols: list[str] | None = None,
                        deadline: float | None = None) -> list[dict[str, Any]]:
    global _ALL_TICKERS_CACHE
    # 白名单行情流覆盖完整时直接使用推送快照。没有明确白名单的旧调用仍走全市场REST，
    # 避免把20币快照误当成交易所全部合约。
    if symbols:
        streamed = get_tickers(symbols, max_age=30.0)
        if streamed:
            return streamed
    with _ALL_TICKERS_LOCK:
        cached_at, cached_rows = _ALL_TICKERS_CACHE
        if cached_rows and time.monotonic() - cached_at < _ALL_TICKERS_TTL_SECONDS:
            return [dict(row) for row in cached_rows]
        own = client is None
        c = client or httpx.Client(timeout=20.0)
        try:
            rows = _get_json(c, "/fapi/v1/ticker/24hr", deadline=deadline)
            normalized = [dict(row) for row in rows if isinstance(row, dict)]
            _ALL_TICKERS_CACHE = (time.monotonic(), normalized)
            return [dict(row) for row in normalized]
        finally:
            if own:
                c.close()


def select_top_symbols(tickers: list[dict[str, Any]], top_n: int = 40,
                       core_symbols: list[str] | None = None) -> list[str]:
    """选出候选标的，默认按 24h 成交额从高到低。

    core_symbols 非空时**只在核心池内**按成交额排序选取。核心池是「会真正交易的品种」
    白名单，用它兜住选品是为了杜绝美股/商品代币化合约（SOXLUSDT 半导体ETF、
    XAGUSDT 白银、XAUUSDT 黄金、SKHYNIXUSDT、SNDKUSDT、SPCXUSDT 等）混入采集集合——
    这些标的在美股时段成交额会冲进全市场前十，但与加密交易无关、历史订单一次都没有。
    让它们进入采集会同时造成两件事：约 60% 的 prompt 字符被浪费，
    以及 regime 判定被美股涨跌带偏（拿美股趋势当币圈趋势）。

    标的正确性优先于覆盖率：核心池外的机会交给 Agent 用 query_symbol_detail 按需查询，
    拿到的数据还比这里 1 小时缓存的快照更新鲜。
    """
    vol: dict[str, float] = {}
    for t in tickers:
        symbol = str(t.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES:
            continue
        try:
            vol[symbol] = float(t.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue

    if core_symbols:
        # 核心池里已下线/拉不到行情的标的会被自然跳过
        rows = sorted(((vol[s], s) for s in core_symbols if s in vol), reverse=True)
        return [s for _, s in rows[:top_n]]

    rows = sorted(((v, s) for s, v in vol.items()), reverse=True)
    return [s for _, s in rows[:top_n]]


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    return statistics.fmean(trs[-period:]) if trs else None


def _trend_from_ema(closes: list[float], period: int = 20) -> str:
    if len(closes) < period + 3:
        return "unknown"
    ema = _ema(closes, period)
    last = ema[-1]
    prev = ema[-4]
    if closes[-1] > last and last >= prev:
        return "up"
    if closes[-1] < last and last <= prev:
        return "down"
    return "range"


def prepare_closed_klines(rows: list[list[Any]], interval: str,
                           observed_at_ms: int | None = None,
                           min_closed: int = 20,
                           bar_metadata: Mapping[Any, Any] | None = None
                           ) -> tuple[list[list[Any]], list[Any] | None, dict[str, Any]]:
    """隔离未完成 K 线并生成可审计的数据质量信息。

    Binance 的 kline 响应通常把当前正在形成的 bar 放在最后。它的 close、high、low
    和成交量仍会持续变化，不能用于 EMA、ATR、量比、突破或背离确认。过去两条行情链
    都直接使用最后一项，导致刚开盘几分钟的低量被解释为“缩量健康”，也可能把瞬时价格
    当成收盘突破。本函数是按需查询与全市场简报的共同口径：指标只读 ``closed``，当前
    bar 仅作为显式 ``live_bar`` 返回给展示/上下文层。

    ``bar_metadata`` 非空时，最终性以逐bar证据为准，而不是以墙钟跨过 ``close_time``
    自动推断。旧缓存、丢失的 ``x=true`` 帧和字段不全的bar会返回结构化数据错误。

    ``fresh`` 允许最近闭合时间距采样时刻最多 1.5 个周期；再久通常意味着接口缓存、
    网络恢复后旧响应或缺 bar。质量信息本阶段先随数据传播，执行层据此 fail-closed。
    """
    now_ms = int(observed_at_ms if observed_at_ms is not None else time.time() * 1000)
    interval_seconds = INTERVAL_SECONDS.get(interval)
    if interval_seconds is None:
        raise ValueError(f"unsupported kline interval: {interval}")

    closed: list[list[Any]] = []
    live_bar: list[Any] | None = None
    invalid_rows = 0
    live_rows = 0
    unknown_finality_rows = 0
    nonfinal_closed_rows = 0
    incomplete_rows = 0
    finality_clock_conflicts = 0

    def metadata_for(open_ms: int) -> BarMeta | None:
        if bar_metadata is None:
            return None
        value = bar_metadata.get(open_ms)
        if value is None:
            value = bar_metadata.get(str(open_ms))
        if isinstance(value, BarMeta):
            return value
        if isinstance(value, Mapping):
            return BarMeta.from_dict(value, fallback_received_at_ms=0)
        return None

    for row in rows or []:
        if not isinstance(row, (list, tuple)) or len(row) <= 6:
            invalid_rows += 1
            continue
        try:
            int(row[0])
            close_time = int(row[6])
        except (TypeError, ValueError):
            invalid_rows += 1
            continue
        open_ms = int(row[0])
        meta = metadata_for(open_ms)
        if bar_metadata is not None:
            if meta is None or meta.final is None:
                unknown_finality_rows += 1
                if close_time > now_ms:
                    live_rows += 1
                    if live_bar is None or open_ms > int(live_bar[0]):
                        live_bar = list(row)
                continue
            if meta.final is not True:
                live_rows += 1
                if close_time <= now_ms:
                    nonfinal_closed_rows += 1
                if live_bar is None or open_ms > int(live_bar[0]):
                    live_bar = list(row)
                continue
            if close_time > now_ms:
                finality_clock_conflicts += 1
                continue
            if not meta.complete:
                incomplete_rows += 1
                continue
            closed.append(list(row))
        elif close_time <= now_ms:
            closed.append(list(row))
        else:
            live_rows += 1
            if live_bar is None or open_ms > int(live_bar[0]):
                live_bar = list(row)

    # 先记录原始顺序，再按开盘时间排序交给指标层；否则接口/缓存偶发乱序会把
    # “最后一项”当成最新闭合 bar，造成方向和突破判断反转。
    original_open_order = [int(row[0]) for row in closed]
    closed.sort(key=lambda row: int(row[0]))

    latest_closed_at_ms = int(closed[-1][6]) if closed else None
    age_seconds = ((now_ms - latest_closed_at_ms) / 1000.0
                   if latest_closed_at_ms is not None else None)
    fresh = bool(age_seconds is not None and
                 age_seconds <= interval_seconds * 1.5 + 5)

    recent_gap = False
    non_monotonic = False
    exact_gap = False
    all_rows = closed
    if len(all_rows) >= 2:
        opens = [int(row[0]) for row in all_rows]
        close_times = [int(row[6]) for row in all_rows]
        non_monotonic = any(later <= earlier for earlier, later in
                            zip(original_open_order, original_open_order[1:]))
        exact_gap = any(later - earlier != interval_seconds * 1000
                        for earlier, later in zip(sorted(opens), sorted(opens)[1:]))
        recent = close_times[-10:]
        recent_gap = any(
            later - earlier > interval_seconds * 1000 * 1.5
            for earlier, later in zip(recent, recent[1:])
        )

    issues: list[str] = []
    if invalid_rows:
        issues.append("invalid_kline_rows")
    if len(closed) < min_closed:
        issues.append("insufficient_closed_klines")
    if not fresh:
        issues.append("stale_last_closed_kline")
    if recent_gap:
        issues.append("recent_kline_gap")
    if exact_gap:
        issues.append("kline_gap")
    if non_monotonic:
        issues.append("non_monotonic_klines")
    if live_rows > 1:
        issues.append("multiple_live_klines")
    if bar_metadata is not None:
        if unknown_finality_rows:
            issues.append("kline_finality_unknown")
        if nonfinal_closed_rows:
            issues.append("nonfinal_closed_kline")
        if incomplete_rows:
            issues.append("incomplete_closed_kline")
        if finality_clock_conflicts:
            issues.append("finality_clock_conflict")

    quality = {
        "interval": interval,
        "observed_at_ms": now_ms,
        "latest_closed_at_ms": latest_closed_at_ms,
        "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "closed_count": len(closed),
        "live_bar_present": live_bar is not None,
        "invalid_rows": invalid_rows,
        "finality_metadata_present": bar_metadata is not None,
        "unknown_finality_rows": unknown_finality_rows,
        "nonfinal_closed_rows": nonfinal_closed_rows,
        "incomplete_rows": incomplete_rows,
        "finality_clock_conflicts": finality_clock_conflicts,
        "recent_gap": recent_gap,
        "non_monotonic": non_monotonic,
        "fresh": fresh,
        "complete": (len(closed) >= min_closed and invalid_rows == 0
                     and not recent_gap and not exact_gap and not non_monotonic
                     and not unknown_finality_rows and not nonfinal_closed_rows
                     and not incomplete_rows and not finality_clock_conflicts),
        "ok": not issues,
        "issues": issues,
    }
    if not quality["ok"]:
        if any(issue in issues for issue in
               ("invalid_kline_rows", "kline_gap", "recent_kline_gap",
                "non_monotonic_klines", "multiple_live_klines",
                "finality_clock_conflict")):
            quality["reason_code"] = "KLINE_STRUCTURE_INVALID"
        elif "kline_finality_unknown" in issues:
            quality["reason_code"] = "KLINE_FINALITY_UNKNOWN"
        elif "nonfinal_closed_kline" in issues:
            quality["reason_code"] = "KLINE_NOT_FINAL"
        elif "incomplete_closed_kline" in issues:
            quality["reason_code"] = "KLINE_FIELDS_INCOMPLETE"
        elif "stale_last_closed_kline" in issues:
            quality["reason_code"] = "KLINE_STALE"
        else:
            quality["reason_code"] = "KLINE_INSUFFICIENT"
    else:
        quality["reason_code"] = "OK"
    return closed, live_bar, quality


def analyze_symbol(client: httpx.Client, symbol: str,
                   ticker: dict[str, Any] | None = None,
                   deadline: float | None = None) -> dict[str, Any]:
    """对单个交易对做一次轻量分析。异常向上抛，由调用方收集。"""
    if ticker is None:
        ticker = _get_json(
            client, "/fapi/v1/ticker/24hr", {"symbol": symbol}, deadline=deadline,
        )
    try:
        pct_24h = float(ticker.get("priceChangePercent") or 0.0)
    except (TypeError, ValueError):
        pct_24h = 0.0

    k1m = get_klines(client, symbol, "1m", 600, fetcher=_get_json, deadline=deadline)
    k15 = get_klines(client, symbol, "15m", 80, fetcher=_get_json, deadline=deadline)
    k1h = get_klines(client, symbol, "1h", 60, fetcher=_get_json, deadline=deadline)
    k4h = get_klines(client, symbol, "4h", 60, fetcher=_get_json, deadline=deadline)
    k1d = get_klines(client, symbol, "1d", 30, fetcher=_get_json, deadline=deadline)
    observed_at_ms = int(time.time() * 1000)
    k1m, _, quality1m = prepare_closed_klines(
        k1m, "1m", observed_at_ms, bar_metadata=get_bar_metadata(symbol, "1m", k1m),
    )
    k15, _, quality15 = prepare_closed_klines(
        k15, "15m", observed_at_ms, bar_metadata=get_bar_metadata(symbol, "15m", k15),
    )
    k1h, _, quality1h = prepare_closed_klines(
        k1h, "1h", observed_at_ms, bar_metadata=get_bar_metadata(symbol, "1h", k1h),
    )
    k4h, _, quality4h = prepare_closed_klines(
        k4h, "4h", observed_at_ms, bar_metadata=get_bar_metadata(symbol, "4h", k4h),
    )
    k1d, _, quality1d = prepare_closed_klines(
        k1d, "1d", observed_at_ms, bar_metadata=get_bar_metadata(symbol, "1d", k1d),
    )
    qualities = {
        "1m": quality1m,
        "15m": quality15,
        "1h": quality1h,
        "4h": quality4h,
        "1d": quality1d,
    }
    unusable = [tf for tf, quality in qualities.items() if not quality["ok"]]
    if unusable:
        raise ValueError(f"closed kline data incomplete for {symbol}: {','.join(unusable)}")
    closes1m = [float(k[4]) for k in k1m]
    highs1m = [float(k[2]) for k in k1m]
    lows1m = [float(k[3]) for k in k1m]
    closes15 = [float(k[4]) for k in k15]
    highs15 = [float(k[2]) for k in k15]
    lows15 = [float(k[3]) for k in k15]
    quote_vols15 = [float(k[7]) for k in k15]
    closes1h = [float(k[4]) for k in k1h]
    highs1h = [float(k[2]) for k in k1h]
    lows1h = [float(k[3]) for k in k1h]
    closes4h = [float(k[4]) for k in k4h]
    highs4h = [float(k[2]) for k in k4h]
    lows4h = [float(k[3]) for k in k4h]
    closes1d = [float(k[4]) for k in k1d]
    highs1d = [float(k[2]) for k in k1d]
    lows1d = [float(k[3]) for k in k1d]

    # 15m 量能比：最近一根已闭合 K 线 vs 此前 20 根均值。
    recent_vol = quote_vols15[-1] if quote_vols15 else 0.0
    base_vol = statistics.fmean(quote_vols15[-21:-1]) if len(quote_vols15) >= 21 else 0.0
    volume_ratio = (recent_vol / base_vol) if base_vol > 0 else 0.0

    atr15 = _atr(highs15, lows15, closes15)
    atr_pct = (atr15 / closes15[-1] * 100) if atr15 and closes15[-1] else 0.0
    rng = (max(highs15[-20:]) - min(lows15[-20:]))
    range_position = ((closes15[-1] - min(lows15[-20:])) / rng * 100) if rng > 0 else 50.0

    atr1h = _atr(highs1h, lows1h, closes1h)
    atr1h_pct = (atr1h / closes1h[-1] * 100) if atr1h and closes1h[-1] else 0.0
    high_10h = max(highs1m[-600:]) if highs1m else closes1m[-1]
    low_10h = min(lows1m[-600:]) if lows1m else closes1m[-1]
    ret_10h = ((closes1m[-1] / closes1m[0] - 1) * 100) if len(closes1m) >= 2 else 0.0
    atr4h = _atr(highs4h, lows4h, closes4h)
    atr4h_pct = (atr4h / closes4h[-1] * 100) if atr4h and closes4h[-1] else 0.0
    atr1d = _atr(highs1d, lows1d, closes1d)
    atr1d_pct = (atr1d / closes1d[-1] * 100) if atr1d and closes1d[-1] else 0.0
    ret_7d = ((closes1d[-1] / closes1d[0] - 1) * 100) if len(closes1d) >= 2 else 0.0
    high_7d = max(highs1d[-7:]) if highs1d else closes1d[-1]
    low_7d = min(lows1d[-7:]) if lows1d else closes1d[-1]

    spread_pct = None
    bid_imbalance = None
    try:
        books = get_books([symbol], max_age=15.0)
        if books:
            book = books[0]
            bid = float(book.get("bidPrice") or 0.0)
            ask = float(book.get("askPrice") or 0.0)
            bid_qty = float(book.get("bidQty") or 0.0)
            ask_qty = float(book.get("askQty") or 0.0)
        else:
            depth = _get_json(client, "/fapi/v1/depth", {"symbol": symbol, "limit": 20})
            bid = float(depth["bids"][0][0]) if depth.get("bids") else 0.0
            ask = float(depth["asks"][0][0]) if depth.get("asks") else 0.0
            bid_qty = sum(float(x[1]) for x in depth.get("bids", []))
            ask_qty = sum(float(x[1]) for x in depth.get("asks", []))
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / bid * 100
        if bid_qty + ask_qty > 0:
            bid_imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty)
    except Exception:
        pass

    ls_ratio = None
    taker_ratio = None
    try:
        ls = _get_json(client, "/futures/data/globalLongShortAccountRatio",
                       {"symbol": symbol, "period": "1h", "limit": 1})
        ls_ratio = float(ls[0].get("longShortRatio") or 0.0) if ls else None
    except Exception:
        pass
    try:
        taker = _get_json(client, "/futures/data/takerlongshortRatio",
                          {"symbol": symbol, "period": "1h", "limit": 1})
        taker_ratio = float(taker[0].get("buySellRatio") or 0.0) if taker else None
    except Exception:
        pass

    funding_rate = None
    mark_price = None
    oi_usd = None
    try:
        marks = get_marks([symbol], max_age=30.0)
        premium = (marks[0] if marks else
                   _get_json(client, "/fapi/v1/premiumIndex", {"symbol": symbol}))
        funding_rate = float(premium.get("lastFundingRate") or 0.0)
        mark_price = float(premium.get("markPrice") or 0.0)
    except Exception:
        pass
    try:
        oi = _get_json(client, "/fapi/v1/openInterest", {"symbol": symbol})
        oi_usd = float(oi.get("openInterest") or 0.0) * (mark_price or closes15[-1])
    except Exception:
        pass

    return {
        "symbol": symbol,
        "last_price": float(ticker.get("lastPrice") or closes15[-1]),
        "pct_24h": pct_24h,
        "trend_15m": _trend_from_ema(closes15, 20),
        "trend_1h": _trend_from_ema(closes1h, 20),
        "volume_ratio_15m": round(volume_ratio, 2),
        "atr_pct_15m": round(atr_pct, 2),
        "atr_pct_1h": round(atr1h_pct, 2),
        "range_position_15m": round(range_position, 1),
        "high_10h": round(high_10h, 8),
        "low_10h": round(low_10h, 8),
        "return_10h_pct": round(ret_10h, 2),
        "trend_4h": _trend_from_ema(closes4h, 20),
        "trend_1d": _trend_from_ema(closes1d, 10),
        "atr_pct_4h": round(atr4h_pct, 2),
        "atr_pct_1d": round(atr1d_pct, 2),
        "return_7d_pct": round(ret_7d, 2),
        "high_7d": round(high_7d, 8),
        "low_7d": round(low_7d, 8),
        "spread_pct": round(spread_pct, 5) if spread_pct is not None else None,
        "bid_imbalance": round(bid_imbalance, 4) if bid_imbalance is not None else None,
        "long_short_ratio": ls_ratio,
        "taker_buy_sell_ratio": taker_ratio,
        "funding_rate": funding_rate,
        "oi_usd": oi_usd,
        "data_quality": {
            "observed_at_ms": observed_at_ms,
            "ok": all(quality["ok"] for quality in qualities.values()),
            "issues": [f"{tf}:{issue}"
                       for tf, quality in qualities.items() for issue in quality["issues"]],
            "timeframes": qualities,
        },
    }


def build_market_brief(top_n: int = 40, analyze_top: int = 10,
                       core_symbols: list[str] | None = None,
                       extra_symbols: list[str] | None = None) -> MarketBrief:
    """生成市场简报。

    core_symbols   核心池白名单，非空时只从池内选标的（详见 select_top_symbols 注释）。
    extra_symbols  强制纳入采集的标的（通常是当前持仓）。持仓币种即便成交额排不进
                   核心池前 analyze_top，也必须有指标，否则决策时看不到自己的仓位。
    """
    errors: list[str] = []
    cache = SnapshotCache(CACHE_DIR)
    with httpx.Client(timeout=20.0) as client:
        try:
            tickers = get_all_24h_tickers(client, symbols=list(core_symbols or []))
            cache.save("market_tickers", {"ts": time.time(), "tickers": tickers})
        except Exception as e:
            errors.append(f"ticker全量接口: {type(e).__name__} {e}")
            cached = cache.load("market_tickers")
            tickers = list(cached.get("tickers", [])) if cached else []
        if tickers:
            symbols = select_top_symbols(tickers, top_n=top_n, core_symbols=core_symbols)
            wanted = set(symbols) | set(extra_symbols or ())
            ticker_map = {t["symbol"]: t for t in tickers if t.get("symbol") in wanted}
            if extra_symbols:
                # 置顶，保证后面 [:analyze_top] 截断时持仓标的不会被切掉。
                # 必须真正「前移」而不是只补进来：持仓标的通常本来就在核心池内，
                # 只是成交额排名靠后会被截断，因此需要把它挪到列表最前面。
                extra = [s for s in extra_symbols if s in ticker_map]
                have = set(extra)
                symbols = extra + [s for s in symbols if s not in have]
        else:
            symbols = FALLBACK_SYMBOLS[:analyze_top]
            ticker_map = {}
            errors.append("使用内置候选标的列表")

        metrics: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(analyze_symbol, client, sym, ticker_map.get(sym)): sym
                for sym in symbols[:analyze_top]
            }
            for fut in as_completed(futures):
                sym = futures[fut]
                try:
                    metrics.append(fut.result())
                except Exception as e:
                    errors.append(f"{sym}: {type(e).__name__} {e}")

    # 数据降级：全部失败时使用最近一次成功快照；部分失败则合并缓存补齐
    if len(metrics) < max(3, analyze_top // 2):
        cached = cache.load("market_metrics")
        if cached:
            cached_metrics = list(cached.get("metrics", []))
            have = {m["symbol"] for m in metrics}
            for cm in cached_metrics:
                if cm.get("symbol") not in have:
                    metrics.append(cm)
            errors.append(f"Binance实时行情失败，使用缓存快照 age={int(time.time() - float(cached.get('ts', 0)))}s")
    if len(metrics) >= max(3, analyze_top // 2):
        cache.save("market_metrics", {"ts": time.time(), "metrics": metrics})

    metrics.sort(key=lambda m: abs(m.get("pct_24h") or 0.0), reverse=True)
    up_count = sum(1 for m in metrics if m.get("trend_15m") == "up")
    down_count = sum(1 for m in metrics if m.get("trend_15m") == "down")
    if not metrics:
        regime = "degraded"
    elif up_count >= int(len(metrics) * 0.6):
        regime = "trend_up"
    elif down_count >= int(len(metrics) * 0.6):
        regime = "trend_down"
    elif errors:
        regime = "degraded"
    else:
        regime = "choppy"

    anomalies: list[str] = []
    for m in metrics:
        fr = m.get("funding_rate")
        if fr is not None and abs(float(fr)) >= 0.0005:
            anomalies.append(f"{m['symbol']} 资金费率异常: {float(fr):.5f}")
        if abs(float(m.get("pct_24h") or 0.0)) >= 10:
            anomalies.append(f"{m['symbol']} 24h涨跌: {m['pct_24h']:.2f}%")
        if float(m.get("volume_ratio_15m") or 0.0) >= 3:
            anomalies.append(f"{m['symbol']} 15m量能比: {m['volume_ratio_15m']:.1f}x")
        if float(m.get("atr_pct_15m") or 0.0) >= 1.5:
            anomalies.append(f"{m['symbol']} 15m ATR: {m['atr_pct_15m']:.2f}%")

    top_movers = [
        f"{m['symbol']} {m['pct_24h']:+.2f}%" for m in metrics[:5]
    ]
    summary = (
        f"扫描前 {top_n} 个USDT永续，深度分析 {len(metrics)} 个。"
        f"市场状态={regime}，15m多头={up_count}，空头={down_count}。"
        f"异动Top: {', '.join(top_movers) or '无'}。"
    )
    if errors:
        summary += f" 部分标的数据失败({len(errors)})。"
    return MarketBrief(
        regime=regime,
        top_movers=top_movers,
        anomalies=anomalies,
        last_prices={m["symbol"]: float(m["last_price"]) for m in metrics if m.get("last_price")},
        metrics=metrics,
        summary=summary,
        errors=errors,
    )
