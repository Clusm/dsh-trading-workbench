"""统一特征矩阵（P2b）。

所有特征都以 bar t 及 t 之前的数据计算，不访问 t 之后的数据。
测试会对每个特征做“前缀重算一致性”检查。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .indicators import (
    adx, atr, bollinger, crossover, crossunder, donchian, ema, ichimoku,
    keltner, macd, rsi, sma, stoch,
)


def _series(a: np.ndarray, index: pd.Index) -> pd.Series:
    return pd.Series(a, index=index, dtype="float64")


_FEATURE_CACHE: dict[tuple[int, int], pd.DataFrame] = {}


def clear_feature_cache() -> None:
    _FEATURE_CACHE.clear()


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """由标准化 OHLCV DataFrame 构建特征矩阵。

    返回 DataFrame，index 与 df 相同。
    对同一 DataFrame 对象做进程内缓存：P2b/P3 会反复调用多个 signal_fn，
    避免每个策略都重建一次 100+ 列特征。
    """
    key = (id(df), len(df))
    if key in _FEATURE_CACHE:
        return _FEATURE_CACHE[key]

    if df.empty:
        return pd.DataFrame(index=df.index)
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    open_ = df["open"].astype(float)
    vol = df["volume"].astype(float) if "volume" in df.columns else pd.Series(0.0, index=df.index)
    idx = df.index
    out = pd.DataFrame(index=idx)

    # ---- 原始 OHLCV（供信号统一从特征矩阵读取） ----
    out["open"] = open_
    out["high"] = high
    out["low"] = low
    out["close"] = close
    out["volume"] = vol

    # ---- 收益与位置 ----
    out["ret_1"] = close.pct_change() * 100.0
    out["ret_5"] = close.pct_change(5) * 100.0
    out["ret_10"] = close.pct_change(10) * 100.0
    out["ret_20"] = close.pct_change(20) * 100.0
    out["range_position_20"] = (close - low.rolling(20).min()) / (high.rolling(20).max() - low.rolling(20).min()) * 100.0
    out["range_position_50"] = (close - low.rolling(50).min()) / (high.rolling(50).max() - low.rolling(50).min()) * 100.0

    # ---- 均线 ----
    for p in (5, 9, 13, 20, 21, 26, 50, 100, 200):
        out[f"ema_{p}"] = _series(ema(close.values, p), idx)
    out["sma_20"] = _series(sma(close.values, 20), idx)
    out["ema_bull_5_9_13_21"] = (
        (out["ema_5"] > out["ema_9"]) & (out["ema_9"] > out["ema_13"]) & (out["ema_13"] > out["ema_21"])
    )

    # ---- 振荡器 ----
    out["rsi_14"] = _series(rsi(close.values, 14), idx)
    macd_line, macd_signal, macd_hist = macd(close.values)
    out["macd"] = _series(macd_line, idx)
    out["macd_signal"] = _series(macd_signal, idx)
    out["macd_hist"] = _series(macd_hist, idx)
    out["macd_cross_up"] = crossover(macd_line, macd_signal)
    out["macd_cross_dn"] = crossunder(macd_line, macd_signal)

    adx_line, pdi, ndi = adx(high.values, low.values, close.values, 14)
    out["adx_14"] = _series(adx_line, idx)
    out["plus_di_14"] = _series(pdi, idx)
    out["minus_di_14"] = _series(ndi, idx)
    out["atr_14"] = _series(atr(high.values, low.values, close.values, 14), idx)
    out["atr_pct_14"] = out["atr_14"] / close * 100.0

    k, d = stoch(high.values, low.values, close.values, 14, 3, 3)
    out["stoch_k"] = _series(k, idx)
    out["stoch_d"] = _series(d, idx)
    out["stoch_cross_up"] = crossover(k, d)
    out["stoch_cross_dn"] = crossunder(k, d)

    # ---- 通道 ----
    bb_mid, bb_up, bb_low = bollinger(close.values, 20, 2.0)
    out["bb_mid"] = _series(bb_mid, idx)
    out["bb_up"] = _series(bb_up, idx)
    out["bb_low"] = _series(bb_low, idx)
    out["bb_width"] = (out["bb_up"] - out["bb_low"]) / out["bb_mid"] * 100.0
    out["bb_width_ratio_5"] = out["bb_width"] / out["bb_width"].rolling(5).mean().shift(1)

    kc_mid, kc_up, kc_low = keltner(high.values, low.values, close.values, 20, 10, 2.0)
    out["kc_mid"] = _series(kc_mid, idx)
    out["kc_up"] = _series(kc_up, idx)
    out["kc_low"] = _series(kc_low, idx)
    out["squeeze_on"] = (out["bb_up"] < out["kc_up"]) & (out["bb_low"] > out["kc_low"])
    out["squeeze_bars"] = out["squeeze_on"].astype(int).groupby((~out["squeeze_on"]).cumsum()).cumcount() + 1
    out.loc[~out["squeeze_on"], "squeeze_bars"] = 0

    for p in (10, 20):
        up, mid, lo = donchian(high.values, low.values, p)
        out[f"donchian_up_{p}"] = _series(up, idx)
        out[f"donchian_mid_{p}"] = _series(mid, idx)
        out[f"donchian_low_{p}"] = _series(lo, idx)
    out["donchian_break_up_20"] = close > out["donchian_up_20"].shift(1)
    out["donchian_break_dn_20"] = close < out["donchian_low_20"].shift(1)

    # ---- Ichimoku ----
    tenkan, kijun, spa, spb = ichimoku(high.values, low.values, close.values)
    out["ichimoku_tenkan"] = _series(tenkan, idx)
    out["ichimoku_kijun"] = _series(kijun, idx)
    out["ichimoku_span_a"] = _series(spa, idx)
    out["ichimoku_span_b"] = _series(spb, idx)
    cloud_max = np.maximum(spa, spb)
    cloud_min = np.minimum(spa, spb)
    out["ichimoku_cross_up"] = crossover(tenkan, kijun)
    out["ichimoku_cross_dn"] = crossunder(tenkan, kijun)
    out["cloud_bull"] = _series(spa > spb, idx)
    out["price_above_cloud"] = close.values > cloud_max
    out["price_below_cloud"] = close.values < cloud_min

    # ---- K线形态 ----
    rng = (high - low).replace(0, np.nan)
    body = (close - open_).abs()
    out["body_pct"] = body / rng * 100.0
    out["upper_wick_pct"] = (high - pd.concat([close, open_], axis=1).max(axis=1)) / rng * 100.0
    out["lower_wick_pct"] = (pd.concat([close, open_], axis=1).min(axis=1) - low) / rng * 100.0
    out["bullish_candle"] = close > open_
    out["bearish_candle"] = close < open_
    out["doji"] = out["body_pct"] < 10.0
    out["inside_bar"] = (high <= high.shift(1)) & (low >= low.shift(1))
    prev_close = close.shift(1)
    prev_open = open_.shift(1)
    out["bullish_engulf"] = (close > open_) & (prev_close < prev_open) & (close >= prev_open) & (open_ <= prev_close)
    out["bearish_engulf"] = (close < open_) & (prev_close > prev_open) & (close <= prev_open) & (open_ >= prev_close)

    # ---- 成交量 ----
    out["volume_ratio_20"] = vol / vol.rolling(20).mean().shift(1)
    out["volume_spike_3x"] = out["volume_ratio_20"] >= 3.0
    out["volume_spike_2x"] = out["volume_ratio_20"] >= 2.0

    # ---- 流动性与价格行为 ----
    for lb in (10, 20, 50, 100):
        prior_high = high.shift(1).rolling(lb).max()
        prior_low = low.shift(1).rolling(lb).min()
        out[f"high_{lb}"] = prior_high
        out[f"low_{lb}"] = prior_low
        out[f"sweep_high_{lb}"] = (high > prior_high) & (close < prior_high)
        out[f"sweep_low_{lb}"] = (low < prior_low) & (close > prior_low)
        out[f"range_mid_{lb}"] = (prior_high + prior_low) / 2.0

    out["fvg_bull"] = low > high.shift(2)
    out["fvg_bear"] = high < low.shift(2)
    out["fvg_bull_mid"] = (high.shift(2) + low) / 2.0
    out["fvg_bear_mid"] = (low.shift(2) + high) / 2.0

    # ---- 连续涨跌 ----
    up_close = close.diff() > 0
    down_close = close.diff() < 0
    out["consecutive_up"] = up_close.astype(int).groupby((~up_close).cumsum()).cumsum()
    out["consecutive_down"] = down_close.astype(int).groupby((~down_close).cumsum()).cumsum()

    # ---- 跨日特征（前一个完整交易日） ----
    day = pd.Series(idx.normalize(), index=idx)
    daily_high = high.groupby(day).max().shift(1)
    daily_low = low.groupby(day).min().shift(1)
    daily_close = close.groupby(day).last().shift(1)
    daily_open = open_.groupby(day).first().shift(1)
    out["prev_day_high"] = day.map(daily_high)
    out["prev_day_low"] = day.map(daily_low)
    out["prev_day_close"] = day.map(daily_close)
    out["prev_day_open"] = day.map(daily_open)
    prev_range = (out["prev_day_high"] - out["prev_day_low"]).replace(0, np.nan)
    out["camarilla_h3"] = out["prev_day_close"] + prev_range * 1.1 / 2.0
    out["camarilla_l3"] = out["prev_day_close"] - prev_range * 1.1 / 2.0
    out["camarilla_h4"] = out["prev_day_close"] + prev_range * 1.1 / 2.0 * 2.0
    out["camarilla_l4"] = out["prev_day_close"] - prev_range * 1.1 / 2.0 * 2.0

    # ---- 大 K 线 ----
    out["large_bearish"] = (close < open_) & (out["body_pct"] > out["body_pct"].rolling(100).median() * 2.0)
    out["large_bullish"] = (close > open_) & (out["body_pct"] > out["body_pct"].rolling(100).median() * 2.0)
    out["gap_up"] = open_ > high.shift(1)
    out["gap_dn"] = open_ < low.shift(1)

    _FEATURE_CACHE[key] = out
    return out
