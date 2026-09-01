"""币安 U 本位永续公开行情客户端，无需 API key。"""
from __future__ import annotations

import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from agents.models import MarketBrief
from data.cache import SnapshotCache
from data.rate_limit import backoff_seconds, cooldown_remaining, note_418, throttle

BASE_URL = "https://fapi.binance.com"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "cache"

FALLBACK_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT",
]

STABLE_BASES = {"USDC", "TUSD", "BUSD", "FDUSD", "USDP", "DAI", "EUR", "AEUR"}


def _get_json(client: httpx.Client, path: str, params: dict[str, Any] | None = None,
              timeout: float = 15.0, attempts: int = 4) -> Any:
    # 冷却期内直接短路，不再发请求，由上层走缓存/降级
    cool = cooldown_remaining()
    if cool > 0:
        raise RuntimeError(f"Binance 公开行情冷却中（剩余 {cool:.0f}s），本轮跳过请求")
    last_err: Exception | None = None
    for i in range(attempts):
        throttle(0.20)
        status: int | None = None
        try:
            r = client.get(f"{BASE_URL}{path}", params=params, timeout=timeout)
            status = r.status_code
            if status == 418:
                note_418()
                raise RuntimeError("HTTP 418: Binance rate limit / temporary IP ban")
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            if i < attempts - 1:
                time.sleep(backoff_seconds(i, status))
    raise last_err if last_err else RuntimeError("request failed")


def get_all_24h_tickers(client: httpx.Client | None = None) -> list[dict[str, Any]]:
    own = client is None
    c = client or httpx.Client(timeout=20.0)
    try:
        return _get_json(c, "/fapi/v1/ticker/24hr")
    finally:
        if own:
            c.close()


def select_top_symbols(tickers: list[dict[str, Any]], top_n: int = 40) -> list[str]:
    rows = []
    for t in tickers:
        symbol = str(t.get("symbol", ""))
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if base in STABLE_BASES:
            continue
        try:
            quote_volume = float(t.get("quoteVolume") or 0.0)
        except (TypeError, ValueError):
            continue
        rows.append((quote_volume, symbol))
    rows.sort(reverse=True)
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


def analyze_symbol(client: httpx.Client, symbol: str,
                   ticker: dict[str, Any] | None = None) -> dict[str, Any]:
    """对单个交易对做一次轻量分析。异常向上抛，由调用方收集。"""
    if ticker is None:
        ticker = _get_json(client, "/fapi/v1/ticker/24hr", {"symbol": symbol})
    try:
        pct_24h = float(ticker.get("priceChangePercent") or 0.0)
    except (TypeError, ValueError):
        pct_24h = 0.0

    k1m = _get_json(client, "/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": 600})
    k15 = _get_json(client, "/fapi/v1/klines", {"symbol": symbol, "interval": "15m", "limit": 80})
    k1h = _get_json(client, "/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "limit": 60})
    k4h = _get_json(client, "/fapi/v1/klines", {"symbol": symbol, "interval": "4h", "limit": 60})
    k1d = _get_json(client, "/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": 30})
    closes1m = [float(k[4]) for k in k1m]
    highs1m = [float(k[2]) for k in k1m]
    lows1m = [float(k[3]) for k in k1m]
    quote_vols1m = [float(k[7]) for k in k1m]
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

    # 15m 量能比：最近一根 vs 前 20 根均值
    recent_vol = quote_vols15[-1] if quote_vols15 else 0.0
    base_vol = statistics.fmean(quote_vols15[-21:-1]) if len(quote_vols15) >= 21 else 0.0
    volume_ratio = (recent_vol / base_vol) if base_vol > 0 else 0.0

    atr15 = _atr(highs15, lows15, closes15)
    atr_pct = (atr15 / closes15[-1] * 100) if atr15 and closes15[-1] else 0.0
    rng = (max(highs15[-20:]) - min(lows15[-20:]))
    range_position = ((closes15[-1] - min(lows15[-20:])) / rng * 100) if rng > 0 else 50.0

    atr1 = _atr(highs1m, lows1m, closes1m)
    atr1_pct = (atr1 / closes1m[-1] * 100) if atr1 and closes1m[-1] else 0.0
    atr1h = _atr(highs1h, lows1h, closes1h)
    atr1h_pct = (atr1h / closes1h[-1] * 100) if atr1h and closes1h[-1] else 0.0
    high_10h = max(highs1m[-600:]) if highs1m else closes1m[-1]
    low_10h = min(lows1m[-600:]) if lows1m else closes1m[-1]
    ret_10h = ((closes1m[-1] / closes1m[0] - 1) * 100) if len(closes1m) >= 2 else 0.0
    recent_vol1m = quote_vols1m[-1] if quote_vols1m else 0.0
    base_vol1m = statistics.fmean(quote_vols1m[-21:-1]) if len(quote_vols1m) >= 21 else 0.0
    vol_ratio_1m = (recent_vol1m / base_vol1m) if base_vol1m > 0 else 0.0

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
        depth = _get_json(client, "/fapi/v1/depth", {"symbol": symbol, "limit": 20})
        bid = float(depth["bids"][0][0]) if depth.get("bids") else 0.0
        ask = float(depth["asks"][0][0]) if depth.get("asks") else 0.0
        if bid > 0 and ask > 0:
            spread_pct = (ask - bid) / bid * 100
        bid_qty = sum(float(x[1]) for x in depth.get("bids", []))
        ask_qty = sum(float(x[1]) for x in depth.get("asks", []))
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
        premium = _get_json(client, "/fapi/v1/premiumIndex", {"symbol": symbol})
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
        "last_price": closes15[-1],
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
    }


def build_market_brief(top_n: int = 40, analyze_top: int = 10) -> MarketBrief:
    errors: list[str] = []
    cache = SnapshotCache(CACHE_DIR)
    with httpx.Client(timeout=20.0) as client:
        try:
            tickers = get_all_24h_tickers(client)
            cache.save("market_tickers", {"ts": time.time(), "tickers": tickers})
        except Exception as e:
            errors.append(f"ticker全量接口: {type(e).__name__} {e}")
            cached = cache.load("market_tickers")
            tickers = list(cached.get("tickers", [])) if cached else []
        if tickers:
            symbols = select_top_symbols(tickers, top_n=top_n)
            ticker_map = {t["symbol"]: t for t in tickers if t.get("symbol") in symbols}
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
