"""策略信号 v3（P5 第二轮重设计）。

只处理 v2 后仍明显不合格或几乎无信号的策略：
- S01：继续降低频率（ADX>=25 + volume>=2.5 + 冷却 48）。
- S04：FVG 增加最小缺口宽度、冲动位移与 EMA 趋势过滤。
- S06：把“连续上涨”改为发生在信号日前，BTC/ETH 阈值降到 3%。
- S07：量能门槛提高到 2.0，冷却 96。
- S08：改为两段式：先出现量价/RSI 背离，随后 N 根内跌破/升破 MA20 才触发。
- OLD_DONCHIAN / OLD_KELTNER：继续提高量能与冷却。
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


# ---------------------------------------------------------------- S01 v3
def s01_breakout_retest_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("breakout_lookback", 20))
    recent = int(p.get("retest_window", 3))
    vol_min = float(p.get("volume_ratio_min", 2.5))
    adx_min = float(p.get("adx_min", 25.0))
    cooldown = int(p.get("cooldown_bars", 48))
    f = build_features(df)
    ev_up = _cross_above(df["close"], f[f"donchian_up_{lb}"].shift(1))
    ev_dn = _cross_below(df["close"], f[f"donchian_low_{lb}"].shift(1))
    recent_up = ev_up.rolling(recent).max() > 0
    recent_dn = ev_dn.rolling(recent).max() > 0
    long = (recent_up & f["bullish_engulf"] & (df["close"] > f[f"donchian_mid_{lb}"])
            & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min))
    short = (recent_dn & f["bearish_engulf"] & (df["close"] < f[f"donchian_mid_{lb}"])
             & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S04 v3
def s04_fvg_trident_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    max_age = int(p.get("fvg_max_age", 5))
    min_gap_pct = float(p.get("min_gap_pct", 0.5))
    min_impulse_pct = float(p.get("min_impulse_pct", 0.3))
    cooldown = int(p.get("cooldown_bars", 48))
    f = build_features(df)
    gap_pct = (df["low"] - df["high"].shift(2)) / df["close"] * 100.0
    gap_pct_neg = (df["high"] - df["low"].shift(2)) / df["close"] * 100.0
    bull_flag = f["fvg_bull"] & (gap_pct >= min_gap_pct) & (f["ret_1"] > min_impulse_pct) \
        & (df["close"] > f["ema_200"]) & (f["ema_20"] > f["ema_50"])
    bear_flag = f["fvg_bear"] & (gap_pct_neg <= -min_gap_pct) & (f["ret_1"] < -min_impulse_pct) \
        & (df["close"] < f["ema_200"]) & (f["ema_20"] < f["ema_50"])
    bull_mid = _num(f["fvg_bull_mid"]).to_numpy()
    bear_mid = _num(f["fvg_bear_mid"]).to_numpy()
    bull_flag = bull_flag.to_numpy()
    bear_flag = bear_flag.to_numpy()
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    close = df["close"].to_numpy()
    n = len(df)
    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
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
                and low[i] <= last_bull_mid and close[i] >= last_bull_mid):
            long[i] = True
        if (bear_age <= max_age and not np.isnan(last_bear_mid)
                and high[i] >= last_bear_mid and close[i] <= last_bear_mid):
            short[i] = True
    return _finalize(df.index, long, short, "BOTH", cooldown)


# ---------------------------------------------------------------- S06 v3
def s06_buying_climax_short_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lookback = int(p.get("climax_lookback_days", 3))
    min_gain = float(p.get("min_cum_gain_pct", 3.0))
    vol_min = float(p.get("volume_ratio_min", 1.2))
    wick_min = float(p.get("wick_min", 15.0))
    pos_min = float(p.get("range_position_min", 70.0))
    cooldown = int(p.get("cooldown_bars", 5))
    f = build_features(df)
    cum = (df["close"] / df["close"].shift(lookback) - 1.0) * 100.0
    run_before = f["consecutive_up"].shift(1) >= 2
    short = ((cum >= min_gain) & run_before & f["bearish_candle"]
             & (f["volume_ratio_20"] > vol_min) & (f["upper_wick_pct"] > wick_min)
             & (f["range_position_20"] > pos_min))
    long = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long, short.to_numpy(), "SHORT", cooldown)


# ---------------------------------------------------------------- S07 v3
def s07_supply_demand_range_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("zone_lookback", 20))
    vol_min = float(p.get("volume_ratio_min", 2.0))
    cooldown = int(p.get("cooldown_bars", 96))
    f = build_features(df)
    in_demand = df["low"] <= f[f"low_{lb}"] * 1.005
    in_supply = df["high"] >= f[f"high_{lb}"] * 0.995
    long = (in_demand & f["bullish_engulf"] & (df["close"] > f["kc_mid"])
            & (f["volume_ratio_20"] > vol_min))
    short = (in_supply & f["bearish_engulf"] & (df["close"] < f["kc_mid"])
             & (f["volume_ratio_20"] > vol_min))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


# ---------------------------------------------------------------- S08 v3
def s08_volume_rsi_divergence_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("divergence_lookback", 30))
    max_age = int(p.get("break_max_age", 6))
    cooldown = int(p.get("cooldown_bars", 24))
    f = build_features(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    vol = df["volume"].to_numpy()
    rsi_v = _num(f["rsi_14"]).to_numpy()
    ema20 = _num(f["ema_20"]).to_numpy()
    n = len(df)
    div_short = np.zeros(n, dtype=bool)
    div_long = np.zeros(n, dtype=bool)
    for i in range(lb + 1, n):
        jh = i - lb + int(np.argmax(high[i - lb:i]))
        jl = i - lb + int(np.argmin(low[i - lb:i]))
        if high[i] > high[i - lb:i].max() and not np.isnan(rsi_v[i]) and not np.isnan(rsi_v[jh]):
            if rsi_v[i] < rsi_v[jh] and vol[i] < vol[jh]:
                div_short[i] = True
        if low[i] < low[i - lb:i].min() and not np.isnan(rsi_v[i]) and not np.isnan(rsi_v[jl]):
            if rsi_v[i] > rsi_v[jl] and vol[i] < vol[jl]:
                div_long[i] = True

    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    last_short_age = max_age + 1
    last_long_age = max_age + 1
    for i in range(n):
        last_short_age += 1
        last_long_age += 1
        if div_short[i]:
            last_short_age = 0
        if div_long[i]:
            last_long_age = 0
        if last_short_age <= max_age and not np.isnan(ema20[i]) and close[i] < ema20[i]:
            short[i] = True
        if last_long_age <= max_age and not np.isnan(ema20[i]) and close[i] > ema20[i]:
            long[i] = True
    return _finalize(df.index, long, short, "BOTH", cooldown)


# ---------------------------------------------------------------- OLD v3
def old_donchian_breakout_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    period = int(p.get("donchian_period", 20))
    vol_min = float(p.get("volume_ratio_min", 1.5))
    adx_min = float(p.get("adx_min", 20.0))
    cooldown = int(p.get("cooldown_bars", 24))
    f = build_features(df)
    up_event = _cross_above(df["close"], f[f"donchian_up_{period}"].shift(1))
    dn_event = _cross_below(df["close"], f[f"donchian_low_{period}"].shift(1))
    long = up_event & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min)
    short = dn_event & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


def old_keltner_v3(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    vol_min = float(p.get("volume_ratio_min", 1.5))
    adx_min = float(p.get("adx_min", 20.0))
    cooldown = int(p.get("cooldown_bars", 24))
    f = build_features(df)
    up_event = _cross_above(df["close"], f["kc_up"].shift(1))
    dn_event = _cross_below(df["close"], f["kc_low"].shift(1))
    long = up_event & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min)
    short = dn_event & (f["volume_ratio_20"] > vol_min) & (f["adx_14"] >= adx_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH", cooldown)


REGISTRY_V3 = {
    "s01_breakout_retest_v3": s01_breakout_retest_v3,
    "s04_fvg_trident_v3": s04_fvg_trident_v3,
    "s06_buying_climax_short_v3": s06_buying_climax_short_v3,
    "s07_supply_demand_range_v3": s07_supply_demand_range_v3,
    "s08_volume_rsi_divergence_v3": s08_volume_rsi_divergence_v3,
    "old_donchian_breakout_v3": old_donchian_breakout_v3,
    "old_keltner_v3": old_keltner_v3,
}
