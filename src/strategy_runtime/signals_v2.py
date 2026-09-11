"""策略信号 v2（P5 重设计）。

只包含 P4 中“高频被成本击穿”和“0-1 笔信号”需要重做的策略。
v2 设计原则：
- 用“事件触发”替代“持续满足”：突破/穿越必须是当根刚发生的 cross。
- 所有重设计策略都加最小开仓冷却，避免刚平仓又开仓被手续费反复磨损。
- 提高确认门槛（量能、位置、K线形态），用信号稀疏化换取扣费后质量。
- S10 仍不实现仓位模块。

所有函数保持与 signals.py 相同的返回契约。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .features import build_features


def _num(col: pd.Series) -> pd.Series:
    return pd.to_numeric(col, errors="coerce")


def _finalize(index: pd.Index, long: np.ndarray, short: np.ndarray,
              direction: str, cooldown: int = 0) -> pd.DataFrame:
    long = np.asarray(long, dtype=bool)
    short = np.asarray(short, dtype=bool)
    if direction == "LONG":
        short = np.zeros_like(short)
    elif direction == "SHORT":
        long = np.zeros_like(long)

    if cooldown > 0:
        combined = long | short
        keep = np.zeros(len(long), dtype=bool)
        last = -cooldown - 1
        for i in range(len(long)):
            if combined[i] and i - last > cooldown:
                keep[i] = True
                last = i
        long = long & keep
        short = short & keep

    side = np.where(long, "LONG", np.where(short, "SHORT", ""))
    signal = np.where(long, 1, np.where(short, -1, 0)).astype(int)
    return pd.DataFrame({"long": long, "short": short, "side": side, "signal": signal}, index=index)


def _cross_above(close: pd.Series, level: pd.Series) -> pd.Series:
    return (close > level) & (close.shift(1) <= level.shift(1))


def _cross_below(close: pd.Series, level: pd.Series) -> pd.Series:
    return (close < level) & (close.shift(1) >= level.shift(1))


# ---------------------------------------------------------------- S01 v2
def s01_breakout_retest_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("breakout_lookback", 20))
    recent = int(p.get("retest_window", 6))
    vol_min = float(p.get("volume_ratio_min", 1.2))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    level_up = f[f"donchian_up_{lb}"].shift(1)
    level_dn = f[f"donchian_low_{lb}"].shift(1)
    event_up = _cross_above(df["close"], level_up)
    event_dn = _cross_below(df["close"], level_dn)
    recent_up = event_up.rolling(recent).max() > 0
    recent_dn = event_dn.rolling(recent).max() > 0
    long = (recent_up & f["bullish_engulf"] & (f["low"] >= f[f"donchian_mid_{lb}"] * 0.998)
            & (f["close"] > f[f"donchian_mid_{lb}"]) & (f["volume_ratio_20"] > vol_min))
    short = (recent_dn & f["bearish_engulf"] & (f["high"] <= f[f"donchian_mid_{lb}"] * 1.002)
             & (f["close"] < f[f"donchian_mid_{lb}"]) & (f["volume_ratio_20"] > vol_min))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S07 v2
def s07_supply_demand_range_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("zone_lookback", 20))
    vol_min = float(p.get("volume_ratio_min", 1.5))
    cooldown = int(p.get("cooldown_bars", 24))
    f = build_features(df)
    in_demand = df["low"] <= f[f"low_{lb}"] * 1.01
    in_supply = df["high"] >= f[f"high_{lb}"] * 0.99
    long = (in_demand & f["bullish_engulf"] & (f["close"] > f["kc_mid"])
            & (f["volume_ratio_20"] > vol_min))
    short = (in_supply & f["bearish_engulf"] & (f["close"] < f["kc_mid"])
             & (f["volume_ratio_20"] > vol_min))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S14 v2
def s14_ttm_squeeze_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    min_bars = int(p.get("min_squeeze_bars", 8))
    vol_min = float(p.get("volume_ratio_min", 1.2))
    cooldown = int(p.get("cooldown_bars", 24))
    f = build_features(df)
    prior_squeeze = f["squeeze_bars"].shift(1) >= min_bars
    up_event = _cross_above(df["close"], f["bb_up"].shift(1))
    dn_event = _cross_below(df["close"], f["bb_low"].shift(1))
    long = prior_squeeze & up_event & (f["macd_hist"] > 0) & (f["volume_ratio_20"] > vol_min)
    short = prior_squeeze & dn_event & (f["macd_hist"] < 0) & (f["volume_ratio_20"] > vol_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S15 v2
def s15_camarilla_inside_bar_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    vol_min = float(p.get("volume_ratio_min", 1.0))
    cooldown = int(p.get("cooldown_bars", 16))
    f = build_features(df)
    inside_prev = f["inside_bar"].shift(1)
    break_up = df["close"] > df["high"].shift(1)
    break_dn = df["close"] < df["low"].shift(1)
    long = (inside_prev & break_up & (df["close"] > f["camarilla_h3"])
            & (f["volume_ratio_20"] > vol_min))
    short = (inside_prev & break_dn & (df["close"] < f["camarilla_l3"])
             & (f["volume_ratio_20"] > vol_min))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- OLD_DONCHIAN v2
def old_donchian_breakout_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    period = int(p.get("donchian_period", 20))
    vol_min = float(p.get("volume_ratio_min", 1.2))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    up_event = _cross_above(df["close"], f[f"donchian_up_{period}"].shift(1))
    dn_event = _cross_below(df["close"], f[f"donchian_low_{period}"].shift(1))
    long = up_event & (f["volume_ratio_20"] > vol_min)
    short = dn_event & (f["volume_ratio_20"] > vol_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- OLD_KELTNER v2
def old_keltner_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    vol_min = float(p.get("volume_ratio_min", 1.2))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    up_event = _cross_above(df["close"], f["kc_up"].shift(1))
    dn_event = _cross_below(df["close"], f["kc_low"].shift(1))
    long = up_event & (f["volume_ratio_20"] > vol_min)
    short = dn_event & (f["volume_ratio_20"] > vol_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S04 v2
def s04_fvg_trident_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    max_age = int(p.get("fvg_max_age", 5))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    n = len(df)
    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    bull_mid = _num(f["fvg_bull_mid"]).to_numpy()
    bear_mid = _num(f["fvg_bear_mid"]).to_numpy()
    bull_flag = f["fvg_bull"].to_numpy()
    bear_flag = f["fvg_bear"].to_numpy()
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()
    ema200 = _num(f["ema_200"]).to_numpy()
    last_bull_mid = np.nan
    last_bear_mid = np.nan
    bull_age = max_age + 1
    bear_age = max_age + 1
    for i in range(n):
        bull_age += 1
        bear_age += 1
        if bull_flag[i] and not np.isnan(bull_mid[i]):
            last_bull_mid = bull_mid[i]
            bull_age = 0
        if bear_flag[i] and not np.isnan(bear_mid[i]):
            last_bear_mid = bear_mid[i]
            bear_age = 0
        if (bull_age <= max_age and not np.isnan(last_bull_mid)
                and low[i] <= last_bull_mid and close[i] >= last_bull_mid
                and not np.isnan(ema200[i]) and close[i] > ema200[i]):
            long[i] = True
        if (bear_age <= max_age and not np.isnan(last_bear_mid)
                and high[i] >= last_bear_mid and close[i] <= last_bear_mid
                and not np.isnan(ema200[i]) and close[i] < ema200[i]):
            short[i] = True
    return _finalize(df.index, long, short, "BOTH", cooldown)


# ---------------------------------------------------------------- S06 v2
def s06_buying_climax_short_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lookback = int(p.get("climax_lookback_days", 5))
    min_gain = float(p.get("min_cum_gain_pct", 15.0))
    vol_min = float(p.get("volume_ratio_min", 1.5))
    cooldown = int(p.get("cooldown_bars", 5))
    f = build_features(df)
    cum = (df["close"] / df["close"].shift(lookback) - 1.0) * 100.0
    short = ((cum >= min_gain) & (f["consecutive_up"] >= 2)
             & (f["volume_ratio_20"] > vol_min) & (f["upper_wick_pct"] > 20.0)
             & (f["range_position_20"] > 75.0) & f["bearish_candle"])
    long = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long, short.to_numpy(), "SHORT", cooldown)


# ---------------------------------------------------------------- S08 v2
def s08_volume_rsi_divergence_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("divergence_lookback", 30))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    vol = df["volume"].to_numpy()
    rsi_v = _num(f["rsi_14"]).to_numpy()
    ema20 = _num(f["ema_20"]).to_numpy()
    n = len(df)
    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    for i in range(lb + 1, n):
        win_high = high[i - lb:i]
        win_low = low[i - lb:i]
        jh = i - lb + int(np.argmax(win_high))
        jl = i - lb + int(np.argmin(win_low))
        if high[i] > win_high.max() and not np.isnan(rsi_v[i]) and not np.isnan(rsi_v[jh]):
            if rsi_v[i] < rsi_v[jh] and vol[i] < vol[jh] and close[i] < ema20[i]:
                short[i] = True
        if low[i] < win_low.min() and not np.isnan(rsi_v[i]) and not np.isnan(rsi_v[jl]):
            if rsi_v[i] > rsi_v[jl] and vol[i] < vol[jl] and close[i] > ema20[i]:
                long[i] = True
    return _finalize(df.index, long, short, "BOTH", cooldown)


# ---------------------------------------------------------------- S09 v2
def s09_climax_rebound_long_v2(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    vol_min = float(p.get("volume_ratio_min", 2.0))
    cooldown = int(p.get("cooldown_bars", 12))
    f = build_features(df)
    big_bear = (f["large_bearish"] & (f["volume_ratio_20"] > vol_min))
    near_low = df["low"] <= f["low_50"] * 1.02
    rebound = f["bullish_candle"] & (df["close"] > df["close"].shift(1))
    long = big_bear.shift(1) & near_low & rebound
    short = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long.to_numpy(), short, "LONG", cooldown)


REGISTRY_V2 = {
    "s01_breakout_retest_v2": s01_breakout_retest_v2,
    "s07_supply_demand_range_v2": s07_supply_demand_range_v2,
    "s14_ttm_squeeze_v2": s14_ttm_squeeze_v2,
    "s15_camarilla_inside_bar_v2": s15_camarilla_inside_bar_v2,
    "old_donchian_breakout_v2": old_donchian_breakout_v2,
    "old_keltner_v2": old_keltner_v2,
    "s04_fvg_trident_v2": s04_fvg_trident_v2,
    "s06_buying_climax_short_v2": s06_buying_climax_short_v2,
    "s08_volume_rsi_divergence_v2": s08_volume_rsi_divergence_v2,
    "s09_climax_rebound_long_v2": s09_climax_rebound_long_v2,
}
