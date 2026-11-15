"""纯 Python 指标库（M1.1 函数级移植，无 numpy 依赖、无未来函数）。

约定（与 docs/04 §6.3 一致）：
- EMA 用 SMA 种子 + alpha=2/(n+1)；RSI/ATR/ADX 用 Wilder 平滑（alpha=1/n）；
- 所有函数返回与输入等长的 list，warmup 期为 ``None``；
- 只使用 t 及以前的数据，禁止任何向后位移。
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Series = Sequence[float]
OptSeries = list[float | None]


def _as_floats(values: Iterable[float]) -> list[float]:
    return [float(value) for value in values]


def sma(values: Series, period: int) -> OptSeries:
    data = _as_floats(values)
    out: OptSeries = [None] * len(data)
    if period <= 0 or len(data) < period:
        return out
    window_sum = sum(data[:period])
    out[period - 1] = window_sum / period
    for index in range(period, len(data)):
        window_sum += data[index] - data[index - period]
        out[index] = window_sum / period
    return out


def ema(values: Series, period: int, *, wilder: bool = False) -> OptSeries:
    data = _as_floats(values)
    out: OptSeries = [None] * len(data)
    if period <= 0 or len(data) < period:
        return out
    alpha = 1.0 / period if wilder else 2.0 / (period + 1)
    seed = sum(data[:period]) / period
    out[period - 1] = seed
    previous = seed
    for index in range(period, len(data)):
        previous = previous + alpha * (data[index] - previous)
        out[index] = previous
    return out


def true_range(highs: Series, lows: Series, closes: Series) -> list[float]:
    high = _as_floats(highs)
    low = _as_floats(lows)
    close = _as_floats(closes)
    out: list[float] = []
    for index in range(len(high)):
        if index == 0:
            out.append(high[index] - low[index])
            continue
        out.append(
            max(
                high[index] - low[index],
                abs(high[index] - close[index - 1]),
                abs(low[index] - close[index - 1]),
            )
        )
    return out


def rsi(closes: Series, period: int = 14) -> OptSeries:
    data = _as_floats(closes)
    out: OptSeries = [None] * len(data)
    if period <= 0 or len(data) < period + 1:
        return out
    gains = [0.0]
    losses = [0.0]
    for index in range(1, len(data)):
        change = data[index] - data[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[1 : period + 1]) / period
    avg_loss = sum(losses[1 : period + 1]) / period

    def _value(gain: float, loss: float) -> float:
        if loss < 1e-12:
            return 100.0
        if gain < 1e-12:
            return 0.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out[period] = _value(avg_gain, avg_loss)
    alpha = 1.0 / period
    for index in range(period + 1, len(data)):
        avg_gain = avg_gain * (1 - alpha) + gains[index] * alpha
        avg_loss = avg_loss * (1 - alpha) + losses[index] * alpha
        out[index] = _value(avg_gain, avg_loss)
    return out


def atr(highs: Series, lows: Series, closes: Series, period: int = 14) -> OptSeries:
    high = _as_floats(highs)
    low = _as_floats(lows)
    close = _as_floats(closes)
    out: OptSeries = [None] * len(high)
    if period <= 0 or len(high) < period + 1:
        return out
    tr = true_range(high, low, close)
    seed = sum(tr[1 : period + 1]) / period
    out[period] = seed
    alpha = 1.0 / period
    previous = seed
    for index in range(period + 1, len(high)):
        previous = previous * (1 - alpha) + tr[index] * alpha
        out[index] = previous
    return out


def macd(
    closes: Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[OptSeries, OptSeries, OptSeries]:
    data = _as_floats(closes)
    fast_ema = ema(data, fast)
    slow_ema = ema(data, slow)
    line: OptSeries = [None] * len(data)
    for index in range(len(data)):
        if fast_ema[index] is not None and slow_ema[index] is not None:
            line[index] = fast_ema[index] - slow_ema[index]
    valid = [value for value in line if value is not None]
    signal_valid = ema(valid, signal) if len(valid) >= signal else [None] * len(valid)
    signal_line: OptSeries = [None] * len(data)
    cursor = 0
    for index, value in enumerate(line):
        if value is None:
            continue
        signal_line[index] = signal_valid[cursor]
        cursor += 1
    hist: OptSeries = [None] * len(data)
    for index in range(len(data)):
        if line[index] is not None and signal_line[index] is not None:
            hist[index] = line[index] - signal_line[index]
    return line, signal_line, hist


def rolling_std(values: Series, period: int) -> OptSeries:
    data = _as_floats(values)
    out: OptSeries = [None] * len(data)
    if period <= 0:
        return out
    for index in range(period - 1, len(data)):
        window = data[index - period + 1 : index + 1]
        mean = sum(window) / period
        variance = sum((item - mean) ** 2 for item in window) / period
        out[index] = math.sqrt(variance)
    return out


def rolling_max(values: Series, period: int) -> OptSeries:
    data = _as_floats(values)
    out: OptSeries = [None] * len(data)
    if period <= 0:
        return out
    for index in range(period - 1, len(data)):
        out[index] = max(data[index - period + 1 : index + 1])
    return out


def rolling_min(values: Series, period: int) -> OptSeries:
    data = _as_floats(values)
    out: OptSeries = [None] * len(data)
    if period <= 0:
        return out
    for index in range(period - 1, len(data)):
        out[index] = min(data[index - period + 1 : index + 1])
    return out


def bollinger(
    closes: Series, period: int = 20, num_std: float = 2.0
) -> tuple[OptSeries, OptSeries, OptSeries, OptSeries]:
    data = _as_floats(closes)
    mid = sma(data, period)
    std = rolling_std(data, period)
    upper: OptSeries = [None] * len(data)
    lower: OptSeries = [None] * len(data)
    width_pct: OptSeries = [None] * len(data)
    for index in range(len(data)):
        if mid[index] is None or std[index] is None:
            continue
        upper[index] = mid[index] + num_std * std[index]
        lower[index] = mid[index] - num_std * std[index]
        if mid[index]:
            width_pct[index] = (upper[index] - lower[index]) / mid[index] * 100.0
    return mid, upper, lower, width_pct


def donchian(highs: Series, lows: Series, period: int = 20) -> tuple[OptSeries, OptSeries, OptSeries]:
    high = _as_floats(highs)
    low = _as_floats(lows)
    upper = rolling_max(high, period)
    lower = rolling_min(low, period)
    mid: OptSeries = [
        None if upper[i] is None or lower[i] is None else (upper[i] + lower[i]) / 2.0
        for i in range(len(high))
    ]
    return upper, mid, lower


def adx(
    highs: Series, lows: Series, closes: Series, period: int = 14
) -> tuple[OptSeries, OptSeries, OptSeries]:
    high = _as_floats(highs)
    low = _as_floats(lows)
    close = _as_floats(closes)
    size = len(high)
    adx_out: OptSeries = [None] * size
    pdi_out: OptSeries = [None] * size
    ndi_out: OptSeries = [None] * size
    if period <= 0 or size < 2 * period:
        return adx_out, pdi_out, ndi_out

    tr = [0.0] * size
    plus_dm = [0.0] * size
    minus_dm = [0.0] * size
    for index in range(1, size):
        up = high[index] - high[index - 1]
        down = low[index - 1] - low[index]
        plus_dm[index] = up if (up > down and up > 0) else 0.0
        minus_dm[index] = down if (down > up and down > 0) else 0.0
        tr[index] = max(
            high[index] - low[index],
            abs(high[index] - close[index - 1]),
            abs(low[index] - close[index - 1]),
        )

    alpha = 1.0 / period
    atr_s = sum(tr[1 : period + 1]) / period
    p_s = sum(plus_dm[1 : period + 1]) / period
    m_s = sum(minus_dm[1 : period + 1]) / period
    dx: OptSeries = [None] * size
    for index in range(period, size):
        if index > period:
            atr_s = atr_s * (1 - alpha) + tr[index] * alpha
            p_s = p_s * (1 - alpha) + plus_dm[index] * alpha
            m_s = m_s * (1 - alpha) + minus_dm[index] * alpha
        if atr_s > 1e-12:
            pdi_out[index] = 100.0 * p_s / atr_s
            ndi_out[index] = 100.0 * m_s / atr_s
            total = pdi_out[index] + ndi_out[index]
            if total and total > 1e-12:
                dx[index] = 100.0 * abs(pdi_out[index] - ndi_out[index]) / total

    first = 2 * period - 1
    if first >= size:
        return adx_out, pdi_out, ndi_out
    window = [value for value in dx[period : first + 1] if value is not None]
    if not window:
        return adx_out, pdi_out, ndi_out
    adx_out[first] = sum(window) / len(window)
    previous = adx_out[first]
    for index in range(first + 1, size):
        if dx[index] is None:
            adx_out[index] = previous
        else:
            previous = previous * (1 - alpha) + dx[index] * alpha
            adx_out[index] = previous
    return adx_out, pdi_out, ndi_out


def percentile_rank(values: Series, window: int = 100) -> float | None:
    """最后一个值在过去 ``window`` 个值中的分位（0..1）。"""

    data = [float(value) for value in values if value is not None]
    if len(data) < 2:
        return None
    sample = data[-min(window, len(data)) :]
    last = sample[-1]
    rank = sum(1 for value in sample if value <= last) / len(sample)
    return round(rank, 4)


def volume_ratio(quote_volumes: Series, period: int = 20) -> float | None:
    data = [float(value) for value in quote_volumes]
    if len(data) < period + 1:
        return None
    baseline = sum(data[-period - 1 : -1]) / period
    if baseline <= 0:
        return None
    return round(data[-1] / baseline, 4)


def taker_buy_ratios(quote_volumes: Series, taker_buy_quotes: Series, period: int = 20) -> tuple[float | None, float | None]:
    quotes = [float(value) for value in quote_volumes]
    taker = [float(value) for value in taker_buy_quotes]
    size = min(len(quotes), len(taker))
    ratios: list[float] = []
    for index in range(size):
        if quotes[index] > 0:
            ratios.append(taker[index] / quotes[index])
    if not ratios:
        return None, None
    last = ratios[-1]
    window = ratios[-min(period, len(ratios)) :]
    return round(last, 4), round(sum(window) / len(window), 4)


def trend_from_ema(closes: Series, ema_values: OptSeries, lookback: int = 3) -> str:
    data = _as_floats(closes)
    if len(data) < 2 or len(ema_values) != len(data):
        return "unknown"
    last = ema_values[-1]
    previous = ema_values[-1 - lookback] if len(ema_values) > lookback else None
    if last is None or previous is None:
        return "unknown"
    if data[-1] > last and last >= previous:
        return "up"
    if data[-1] < last and last <= previous:
        return "down"
    return "range"


def range_position(closes: Series, highs: Series, lows: Series, lookback: int = 20) -> tuple[float, float, float, float]:
    close = _as_floats(closes)
    high = _as_floats(highs)
    low = _as_floats(lows)
    window_high = max(high[-lookback:])
    window_low = min(low[-lookback:])
    span = window_high - window_low
    position = 0.5 if span <= 0 else (close[-1] - window_low) / span
    return window_high, window_low, min(1.0, max(0.0, position)), span


def last_value(series: OptSeries) -> float | None:
    for value in reversed(series):
        if value is not None:
            return float(value)
    return None


def round_optional(value: float | None, digits: int = 8) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(float(value), digits)
