"""核心指标库（P1）。

只做向量化预计算，不产生交易信号。所有函数返回与输入等长的 ndarray，
前段 warm-up 期为 np.nan。策略只能在指标非 NaN 的位置产生信号。

约定：
- EMA 使用 SMA 种子 + alpha=2/(period+1)（与旧因子库一致）。
- RSI / ATR / ADX 使用 Wilder 平滑。
- Ichimoku 云图使用“先算滚动值，再向后位移”的向量化写法；在 t 时刻只使用
  t 及以前的数据，测试中会做增量一致性验证。
"""
from __future__ import annotations

from typing import Tuple

import numpy as np

ArrayLike = np.ndarray | list | tuple


def _arr(a: ArrayLike) -> np.ndarray:
    arr = np.asarray(a, dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError("指标输入必须是一维数组")
    return arr


# ---------------------------------------------------------------- moving stats
def sma(a: ArrayLike, period: int) -> np.ndarray:
    a = _arr(a)
    out = np.full(len(a), np.nan)
    if len(a) >= period and period > 0:
        kernel = np.ones(period) / period
        out[period - 1:] = np.convolve(a, kernel, mode="valid")
    return out


def ema(a: ArrayLike, period: int, wilder: bool = False) -> np.ndarray:
    """EMA。wilder=True 时 alpha=1/period；否则 alpha=2/(period+1)。"""
    a = _arr(a)
    out = np.full(len(a), np.nan)
    n = len(a)
    if n < period or period <= 0:
        return out
    alpha = 1.0 / period if wilder else 2.0 / (period + 1)
    seed = float(np.mean(a[:period]))
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = out[i - 1] + alpha * (a[i] - out[i - 1])
    return out


def rolling_std(a: ArrayLike, period: int, ddof: int = 0) -> np.ndarray:
    a = _arr(a)
    out = np.full(len(a), np.nan)
    if period <= 0:
        return out
    for i in range(period - 1, len(a)):
        out[i] = float(np.std(a[i - period + 1:i + 1], ddof=ddof))
    return out


def rolling_max(a: ArrayLike, period: int) -> np.ndarray:
    a = _arr(a)
    out = np.full(len(a), np.nan)
    if period <= 0:
        return out
    for i in range(period - 1, len(a)):
        out[i] = float(np.max(a[i - period + 1:i + 1]))
    return out


def rolling_min(a: ArrayLike, period: int) -> np.ndarray:
    a = _arr(a)
    out = np.full(len(a), np.nan)
    if period <= 0:
        return out
    for i in range(period - 1, len(a)):
        out[i] = float(np.min(a[i - period + 1:i + 1]))
    return out


# ---------------------------------------------------------------- oscillators
def macd(close: ArrayLike, fast: int = 12, slow: int = 26, signal: int = 9
         ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    close = _arr(close)
    ema_fast = ema(close, fast)
    ema_slow = ema(close, slow)
    line = np.where(np.isnan(ema_fast) | np.isnan(ema_slow), np.nan, ema_fast - ema_slow)
    valid = ~np.isnan(line)
    line_valid = line[valid]
    sig_valid = ema(line_valid, signal)
    signal_line = np.full(len(close), np.nan)
    signal_line[valid] = sig_valid
    hist = line - signal_line
    return line, signal_line, hist


def rsi(close: ArrayLike, period: int = 14) -> np.ndarray:
    close = _arr(close)
    out = np.full(len(close), np.nan)
    n = len(close)
    if n < period + 1:
        return out
    diff = np.diff(close)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_gain = float(np.mean(gain[:period]))
    avg_loss = float(np.mean(loss[:period]))

    def _rsi(g: float, l: float) -> float:
        if l < 1e-12:
            return 100.0
        if g < 1e-12:
            return 0.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _rsi(avg_gain, avg_loss)
    alpha = 1.0 / period
    for i in range(period + 1, n):
        avg_gain = avg_gain * (1 - alpha) + gain[i - 1] * alpha
        avg_loss = avg_loss * (1 - alpha) + loss[i - 1] * alpha
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def true_range(high: ArrayLike, low: ArrayLike, close: ArrayLike) -> np.ndarray:
    high, low, close = map(_arr, (high, low, close))
    n = len(high)
    out = np.zeros(n)
    for i in range(1, n):
        out[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    return out


def atr(high: ArrayLike, low: ArrayLike, close: ArrayLike, period: int = 14) -> np.ndarray:
    high, low, close = map(_arr, (high, low, close))
    out = np.full(len(high), np.nan)
    n = len(high)
    if n < period + 1:
        return out
    tr = true_range(high, low, close)[1:]
    seed = float(np.mean(tr[:period]))
    out[period] = seed
    alpha = 1.0 / period
    for i in range(period + 1, n):
        out[i] = out[i - 1] * (1 - alpha) + tr[i - 1] * alpha
    return out


def adx(high: ArrayLike, low: ArrayLike, close: ArrayLike, period: int = 14
        ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    high, low, close = map(_arr, (high, low, close))
    n = len(high)
    adx_out = np.full(n, np.nan)
    pdi_out = np.full(n, np.nan)
    ndi_out = np.full(n, np.nan)
    if n < 2 * period:
        return adx_out, pdi_out, ndi_out

    tr = np.zeros(n)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    alpha = 1.0 / period
    atr_s = float(np.mean(tr[1:period + 1]))
    p_s = float(np.mean(plus_dm[1:period + 1]))
    m_s = float(np.mean(minus_dm[1:period + 1]))
    for i in range(period, n):
        if i > period:
            atr_s = atr_s * (1 - alpha) + tr[i] * alpha
            p_s = p_s * (1 - alpha) + plus_dm[i] * alpha
            m_s = m_s * (1 - alpha) + minus_dm[i] * alpha
        if atr_s > 1e-12:
            pdi_out[i] = 100.0 * p_s / atr_s
            ndi_out[i] = 100.0 * m_s / atr_s

    dx = np.full(n, np.nan)
    for i in range(period, n):
        s = pdi_out[i] + ndi_out[i]
        if np.isnan(s) or s <= 1e-12:
            continue
        dx[i] = 100.0 * abs(pdi_out[i] - ndi_out[i]) / s

    first_adx = 2 * period - 1
    if first_adx < n:
        window = dx[period:first_adx + 1]
        if np.isnan(window).all():
            return adx_out, pdi_out, ndi_out
        adx_out[first_adx] = float(np.nanmean(window))
        for i in range(first_adx + 1, n):
            prev = adx_out[i - 1]
            cur = dx[i]
            if np.isnan(cur):
                adx_out[i] = prev
            else:
                adx_out[i] = prev * (1 - alpha) + cur * alpha
    return adx_out, pdi_out, ndi_out


def stoch(high: ArrayLike, low: ArrayLike, close: ArrayLike,
          k_period: int = 14, d_period: int = 3, smooth: int = 3
          ) -> Tuple[np.ndarray, np.ndarray]:
    high, low, close = map(_arr, (high, low, close))
    n = len(high)
    k_raw = np.full(n, np.nan)
    if n >= k_period:
        for i in range(k_period - 1, n):
            hh = float(np.max(high[i - k_period + 1:i + 1]))
            ll = float(np.min(low[i - k_period + 1:i + 1]))
            if hh - ll > 1e-12:
                k_raw[i] = 100.0 * (close[i] - ll) / (hh - ll)
            else:
                k_raw[i] = 50.0
    k = np.full(n, np.nan)
    if smooth > 1:
        k_s = sma(k_raw[~np.isnan(k_raw)], smooth)
        k[~np.isnan(k_raw)] = k_s
    else:
        k = k_raw.copy()
    d = np.full(n, np.nan)
    d_s = sma(k[~np.isnan(k)], d_period)
    d[~np.isnan(k)] = d_s
    return k, d


# ---------------------------------------------------------------- channels
def bollinger(close: ArrayLike, period: int = 20, num_std: float = 2.0
              ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    close = _arr(close)
    mid = sma(close, period)
    sd = rolling_std(close, period, ddof=0)
    return mid, mid + num_std * sd, mid - num_std * sd


def keltner(high: ArrayLike, low: ArrayLike, close: ArrayLike,
            ema_period: int = 20, atr_period: int = 10, mult: float = 2.0
            ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    high, low, close = map(_arr, (high, low, close))
    mid = ema(close, ema_period)
    a = atr(high, low, close, atr_period)
    return mid, mid + mult * a, mid - mult * a


def donchian(high: ArrayLike, low: ArrayLike, period: int = 20
             ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    high, low = map(_arr, (high, low))
    upper = rolling_max(high, period)
    lower = rolling_min(low, period)
    mid = (upper + lower) / 2.0
    return upper, mid, lower


def ichimoku(high: ArrayLike, low: ArrayLike, close: ArrayLike,
             tenkan: int = 9, kijun: int = 26, senkou_b: int = 52,
             displacement: int = 26
             ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回 (tenkan_sen, kijun_sen, senkou_span_a, senkou_span_b)。

    云图两根线是“先滚动计算、再向后位移”。在 t 时刻取 span_a[t]，
    其信息截止于 t-displacement，因此无未来函数。
    """
    high, low = map(_arr, (high, low))
    n = len(high)
    tenkan = (rolling_max(high, tenkan) + rolling_min(low, tenkan)) / 2.0
    kijun = (rolling_max(high, kijun) + rolling_min(low, kijun)) / 2.0
    span_a_raw = (tenkan + kijun) / 2.0
    span_b_raw = (rolling_max(high, senkou_b) + rolling_min(low, senkou_b)) / 2.0
    span_a = np.full(n, np.nan)
    span_b = np.full(n, np.nan)
    if n > displacement:
        span_a[displacement:] = span_a_raw[:-displacement]
        span_b[displacement:] = span_b_raw[:-displacement]
    return tenkan, kijun, span_a, span_b


# ---------------------------------------------------------------- crosses
def crossover(a: ArrayLike, b: ArrayLike) -> np.ndarray:
    a, b = map(_arr, (a, b))
    out = np.zeros(len(a), dtype=bool)
    for i in range(1, len(a)):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
            continue
        out[i] = a[i] > b[i] and a[i - 1] <= b[i - 1]
    return out


def crossunder(a: ArrayLike, b: ArrayLike) -> np.ndarray:
    a, b = map(_arr, (a, b))
    out = np.zeros(len(a), dtype=bool)
    for i in range(1, len(a)):
        if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
            continue
        out[i] = a[i] < b[i] and a[i - 1] >= b[i - 1]
    return out
