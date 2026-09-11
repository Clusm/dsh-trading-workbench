"""策略信号实现（P2b）。

每个函数签名统一：fn(df, params) -> DataFrame。
- df 为标准化 OHLCV（可以是原生周期，也可以由 engine 重采样）。
- 返回 index 对齐的 DataFrame，列: long, short, side, signal。
  side: 'LONG'/'SHORT'/''；signal: +1/-1/0。
- 所有信号只在 bar t 收盘后生成；engine 负责在 t+1 open 执行，
  因此本文件禁止使用 shift(-1) 或任何未来数据。

S10 不实现仓位模块，因此本文件不包含 S10。
"""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .features import build_features
from .indicators import crossover, crossunder


def _finalize(index: pd.Index, long: np.ndarray, short: np.ndarray,
              direction: str) -> pd.DataFrame:
    long = np.asarray(long, dtype=bool)
    short = np.asarray(short, dtype=bool)
    if direction == "LONG":
        short = np.zeros_like(short)
    elif direction == "SHORT":
        long = np.zeros_like(long)
    side = np.where(long, "LONG", np.where(short, "SHORT", ""))
    signal = np.where(long, 1, np.where(short, -1, 0)).astype(int)
    return pd.DataFrame({"long": long, "short": short, "side": side, "signal": signal}, index=index)


def _num(col: pd.Series) -> pd.Series:
    return pd.to_numeric(col, errors="coerce")


# ---------------------------------------------------------------- S01
def s01_breakout_retest(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("breakout_lookback", 20))
    f = build_features(df)
    recent_up = f[f"donchian_break_up_{lb}"].rolling(max(1, int(p.get("retest_bars", 3)))).max() > 0
    recent_dn = f[f"donchian_break_dn_{lb}"].rolling(max(1, int(p.get("retest_bars", 3)))).max() > 0
    long = recent_up & (f["close"] > f[f"donchian_mid_{lb}"]) & f["bullish_engulf"]
    short = recent_dn & (f["close"] < f[f"donchian_mid_{lb}"]) & f["bearish_engulf"]
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S02
def s02_stoch_divergence(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("divergence_lookback", 20))
    f = build_features(df)
    low = df["low"].to_numpy()
    high = df["high"].to_numpy()
    k = _num(f["stoch_k"]).to_numpy()
    cross_up = f["stoch_cross_up"].to_numpy()
    cross_dn = f["stoch_cross_dn"].to_numpy()
    n = len(df)
    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    for i in range(lb, n):
        j = i - lb
        if np.isnan(k[i]) or np.isnan(k[j]):
            continue
        # 价格新低，但 Stoch 不创新低且从超卖区拐头
        if low[i] < low[j] and k[i] > k[j] and k[j] < 20 and cross_up[i]:
            long[i] = True
        # 价格新高，但 Stoch 不创新高且从超买区拐头
        if high[i] > high[j] and k[i] < k[j] and k[j] > 80 and cross_dn[i]:
            short[i] = True
    return _finalize(df.index, long, short, "BOTH")


# ---------------------------------------------------------------- S03
def s03_structure_ote(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    left = int(p.get("swing_left", 8))
    right = int(p.get("swing_right", 2))
    f = build_features(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    n = len(df)
    swing_low = np.zeros(n, dtype=bool)
    swing_high = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        if low[i] <= np.min(low[i - left:i + right + 1]):
            swing_low[i] = True
        if high[i] >= np.max(high[i - left:i + right + 1]):
            swing_high[i] = True

    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    last_sl = -1
    last_sh = -1
    for i in range(n):
        if swing_low[i]:
            last_sl = i
        if swing_high[i]:
            last_sh = i
        if last_sl > 0 and i - last_sl <= 30:
            sl_low = low[last_sl]
            # 找 swing low 之后的最高点作为波段高点
            seg_high = float(np.max(high[last_sl:i + 1]))
            rng = seg_high - sl_low
            if rng > 0:
                pullback = (seg_high - df["close"].iloc[i]) / rng
                if 0.58 <= pullback <= 0.75 and f["fvg_bull"].iloc[i]:
                    long[i] = True
        if last_sh > 0 and i - last_sh <= 30:
            sh_high = high[last_sh]
            seg_low = float(np.min(low[last_sh:i + 1]))
            rng = sh_high - seg_low
            if rng > 0:
                pullback = (df["close"].iloc[i] - seg_low) / rng
                if 0.58 <= pullback <= 0.75 and f["fvg_bear"].iloc[i]:
                    short[i] = True
    return _finalize(df.index, long, short, "BOTH")


# ---------------------------------------------------------------- S04
def s04_fvg_trident(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    long = (f["fvg_bull"] & f["doji"] & (f["low"] <= f["fvg_bull_mid"])
            & (f["close"] >= f["fvg_bull_mid"]) & (f["close"] > f["ema_200"]))
    short = (f["fvg_bear"] & f["doji"] & (f["high"] >= f["fvg_bear_mid"])
             & (f["close"] <= f["fvg_bear_mid"]) & (f["close"] < f["ema_200"]))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S05
def s05_liquidity_sweep_short(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("sweep_lookback", 20))
    f = build_features(df)
    short = (f[f"sweep_high_{lb}"] & f["bearish_candle"]
             & (f["close"] < f[f"donchian_mid_{lb}"]) & (f["ret_1"] < 0))
    long = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long, short.to_numpy(), "SHORT")


# ---------------------------------------------------------------- S06
def s06_buying_climax_short(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    days = int(p.get("climax_days", 3))
    min_gain = float(p.get("min_cum_gain_pct", 80.0))
    f = build_features(df)
    cum = (df["close"] / df["close"].shift(days) - 1.0) * 100.0
    short = ((cum >= min_gain) & (f["consecutive_up"] >= 2)
             & (f["volume_spike_2x"]) & (f["upper_wick_pct"] > 25.0)
             & (f["range_position_20"] > 75.0) & f["bearish_candle"])
    long = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long, short.to_numpy(), "SHORT")


# ---------------------------------------------------------------- S07
def s07_supply_demand_range(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("range_lookback", 20))
    f = build_features(df)
    long = (f["bullish_candle"] & (f["close"] > f["kc_mid"])
            & f[f"donchian_break_up_{lb}"] & (f["volume_ratio_20"] > 1.0))
    short = (f["bearish_candle"] & (f["close"] < f["kc_mid"])
             & f[f"donchian_break_dn_{lb}"] & (f["volume_ratio_20"] > 1.0))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S08
def s08_volume_rsi_divergence(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    left = int(p.get("swing_left", 10))
    right = int(p.get("swing_right", 2))
    f = build_features(df)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    vol = df["volume"].to_numpy()
    rsi_v = _num(f["rsi_14"]).to_numpy()
    ma20 = _num(f["ema_20"]).to_numpy()
    n = len(df)
    swing_high = np.zeros(n, dtype=bool)
    swing_low = np.zeros(n, dtype=bool)
    for i in range(left, n - right):
        if high[i] >= np.max(high[i - left:i + right + 1]):
            swing_high[i] = True
        if low[i] <= np.min(low[i - left:i + right + 1]):
            swing_low[i] = True
    short = np.zeros(n, dtype=bool)
    long = np.zeros(n, dtype=bool)
    last_sh = -1
    last_sl = -1
    for i in range(n):
        if swing_high[i]:
            last_sh = i
        if swing_low[i]:
            last_sl = i
        if last_sh > 0 and i - last_sh <= 30:
            if (high[i] > high[last_sh] and rsi_v[i] < rsi_v[last_sh]
                    and vol[i] < vol[last_sh] and close[i] < ma20[i]):
                short[i] = True
        if last_sl > 0 and i - last_sl <= 30:
            if (low[i] < low[last_sl] and rsi_v[i] > rsi_v[last_sl]
                    and vol[i] < vol[last_sl] and close[i] > ma20[i]):
                long[i] = True
    return _finalize(df.index, long, short, "BOTH")


# ---------------------------------------------------------------- S09
def s09_climax_rebound_long(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    near_low = (df["low"] <= f["low_50"] * 1.03)
    prior_climax = f["large_bearish"].shift(1) & f["volume_spike_3x"].shift(1)
    long = prior_climax & f["bullish_engulf"] & near_low
    short = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long.to_numpy(), short, "LONG")


# ---------------------------------------------------------------- S11
def s11_adf_short(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    short = (f["gap_up"] & f["bearish_candle"] & (f["upper_wick_pct"] > 25.0)
             & (f["volume_ratio_20"] < 1.5) & (f["range_position_20"] > 60.0))
    long = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long, short.to_numpy(), "SHORT")


# ---------------------------------------------------------------- S12
def s12_po3_sweep(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("range_lookback", 20))
    f = build_features(df)
    long = f[f"sweep_low_{lb}"] & (f["close"] > f[f"range_mid_{lb}"])
    short = f[f"sweep_high_{lb}"] & (f["close"] < f[f"range_mid_{lb}"])
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S13
def s13_dormant_liquidity(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("old_level_lookback", 100))
    f = build_features(df)
    long = f[f"sweep_low_{lb}"] & f["bullish_engulf"]
    short = f[f"sweep_high_{lb}"] & f["bearish_engulf"]
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S14
def s14_ttm_squeeze(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    min_bars = int(p.get("min_squeeze_bars", 5))
    f = build_features(df)
    prior_squeeze = f["squeeze_bars"].shift(1) >= min_bars
    long = prior_squeeze & (f["close"] > f["bb_up"].shift(1)) & (f["macd_hist"] > 0)
    short = prior_squeeze & (f["close"] < f["bb_low"].shift(1)) & (f["macd_hist"] < 0)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- S15
def s15_camarilla_inside_bar(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    inside_prev = f["inside_bar"].shift(1)
    long = inside_prev & (f["close"] > df["high"].shift(1))
    short = inside_prev & (f["close"] < df["low"].shift(1))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


# ---------------------------------------------------------------- old strategies
def old_ichimoku_cross(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    long = f["ichimoku_cross_up"] & f["price_above_cloud"]
    short = f["ichimoku_cross_dn"] & f["price_below_cloud"]
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_donchian_breakout(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    period = int(p.get("donchian_period", 10))
    f = build_features(df)
    long = df["close"] > f[f"donchian_up_{period}"].shift(1)
    short = df["close"] < f[f"donchian_low_{period}"].shift(1)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_macd_rsi(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    adx_min = float(p.get("adx_min", 25.0))
    rsi_max = float(p.get("rsi_max", 60.0))
    f = build_features(df)
    long = np.zeros(len(df), dtype=bool)
    short = f["macd_cross_dn"] & (f["rsi_14"] < rsi_max) & (f["adx_14"] >= adx_min)
    return _finalize(df.index, long, short.to_numpy(), "SHORT")


def old_trend_rsi(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    rsi_min = float(p.get("rsi_min", 60.0))
    f = build_features(df)
    long = np.zeros(len(df), dtype=bool)
    short = (df["close"] < f["ema_200"]) & (f["rsi_14"] > rsi_min)
    return _finalize(df.index, long, short.to_numpy(), "SHORT")


def old_ema_rsi(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    ema20 = _num(f["ema_20"]).to_numpy()
    ema26 = _num(f["ema_26"]).to_numpy()
    long = crossover(ema20, ema26) & (f["rsi_14"] > 30) & (f["rsi_14"] < 70)
    short = crossunder(ema20, ema26) & (f["rsi_14"] > 30) & (f["rsi_14"] < 70)
    return _finalize(df.index, long, short, "BOTH")


def old_keltner(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    long = df["close"] > f["kc_up"].shift(1)
    short = df["close"] < f["kc_low"].shift(1)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_bb_squeeze(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    ratio_max = float(p.get("squeeze_ratio", 0.9))
    f = build_features(df)
    prior_squeeze = f["bb_width_ratio_5"].shift(1) < ratio_max
    long = prior_squeeze & (df["close"] > f["bb_up"].shift(1))
    short = prior_squeeze & (df["close"] < f["bb_low"].shift(1))
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_multi_sig(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    f = build_features(df)
    long1 = f["ichimoku_cross_up"] & f["price_above_cloud"]
    long2 = (df["close"] > f["ema_200"]) & (f["rsi_14"] < 40)
    long3 = f["macd_cross_up"]
    long4 = df["close"] > f["donchian_up_10"].shift(1)
    long_count = long1.astype(int) + long2.astype(int) + long3.astype(int) + long4.astype(int)
    long = long_count >= 2
    short = np.zeros(len(df), dtype=bool)
    return _finalize(df.index, long.to_numpy(), short, "LONG")


def old_po3_any(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("range_lookback", 10))
    f = build_features(df)
    confirm_long = f["bullish_engulf"] | f["macd_cross_up"] | f["stoch_cross_up"]
    confirm_short = f["bearish_engulf"] | f["macd_cross_dn"] | f["stoch_cross_dn"]
    long = f[f"sweep_low_{lb}"] & confirm_long
    short = f[f"sweep_high_{lb}"] & confirm_short
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_po3_macd(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("range_lookback", 10))
    f = build_features(df)
    long = f[f"sweep_low_{lb}"] & f["macd_cross_up"]
    short = f[f"sweep_high_{lb}"] & f["macd_cross_dn"]
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


def old_po3_macd_adx(df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    p = params or {}
    lb = int(p.get("range_lookback", 10))
    adx_min = float(p.get("adx_min", 20.0))
    f = build_features(df)
    long = f[f"sweep_low_{lb}"] & f["macd_cross_up"] & (f["adx_14"] >= adx_min)
    short = f[f"sweep_high_{lb}"] & f["macd_cross_dn"] & (f["adx_14"] >= adx_min)
    return _finalize(df.index, long.to_numpy(), short.to_numpy(), "BOTH")


REGISTRY: dict[str, Callable] = {
    "s01_breakout_retest": s01_breakout_retest,
    "s02_stoch_divergence": s02_stoch_divergence,
    "s03_structure_ote": s03_structure_ote,
    "s04_fvg_trident": s04_fvg_trident,
    "s05_liquidity_sweep_short": s05_liquidity_sweep_short,
    "s06_buying_climax_short": s06_buying_climax_short,
    "s07_supply_demand_range": s07_supply_demand_range,
    "s08_volume_rsi_divergence": s08_volume_rsi_divergence,
    "s09_climax_rebound_long": s09_climax_rebound_long,
    "s11_adf_short": s11_adf_short,
    "s12_po3_sweep": s12_po3_sweep,
    "s13_dormant_liquidity": s13_dormant_liquidity,
    "s14_ttm_squeeze": s14_ttm_squeeze,
    "s15_camarilla_inside_bar": s15_camarilla_inside_bar,
    "old_ichimoku_cross": old_ichimoku_cross,
    "old_donchian_breakout": old_donchian_breakout,
    "old_macd_rsi": old_macd_rsi,
    "old_trend_rsi": old_trend_rsi,
    "old_ema_rsi": old_ema_rsi,
    "old_keltner": old_keltner,
    "old_bb_squeeze": old_bb_squeeze,
    "old_multi_sig": old_multi_sig,
    "old_po3_any": old_po3_any,
    "old_po3_macd": old_po3_macd,
    "old_po3_macd_adx": old_po3_macd_adx,
}


# P5 v2/v3 重设计策略注册（定义在独立文件，避免 v1 结果被覆盖）
from .signals_v2 import REGISTRY_V2  # noqa: E402
from .signals_v3 import REGISTRY_V3  # noqa: E402

REGISTRY.update(REGISTRY_V2)
REGISTRY.update(REGISTRY_V3)


def generate_signals(signal_fn: str, df: pd.DataFrame, params: dict | None = None) -> pd.DataFrame:
    fn = REGISTRY.get(signal_fn)
    if fn is None:
        raise KeyError(f"未注册的 signal_fn: {signal_fn}")
    out = fn(df, params)
    if not isinstance(out, pd.DataFrame):
        raise TypeError(f"{signal_fn} 必须返回 DataFrame")
    if len(out) != len(df):
        raise ValueError(f"{signal_fn} 输出长度不一致: {len(out)} != {len(df)}")
    return out
