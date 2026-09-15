"""L04：9张策略卡的 prefix/回放前视差分，以及共享特征层的前视不变性（全离线）。

为什么需要这个脚本
----------------
`knowledge/strategy_cards.json` 的 9 张卡全部 `indicatorized=true`，信号由
`strategy_runtime.REGISTRY` 里的函数基于 OHLCV 计算。扫描器只取 `iloc[-1]`
（`src/signal_engine/scanner.py:357-361`），因此存在一类只在实盘才会暴露的缺陷：
**指标或规则用到了未来 bar**。这类前视让回测/影子样本看起来有效，上线后失效。

原有覆盖的空档
------------
- `smoke_kline_finality` 覆盖 K 线定稿与高周期聚合，不覆盖卡片决策。
- `smoke_usdm_replay` 在**回放引擎**层拒绝 `future_data_leakage`，但那是引擎的
  顺序校验，不检验卡片自身的计算是否稳定。
- `smoke_signal_scanner_integration` 用临时卡文件验证扫描器接线，不检验 9 张卡。
=> 没有任何测试真正加载这 9 张卡做前视差分。

判据（比"信号相等"更强）
----------------------
对任意 bar i，把行情截断到 i 重新计算，其**最后一根**的全部输出列，必须等于
用完整行情计算后第 i 行的值。等价于：第 i 根的历史不会被后续 bar 改写。

三层判据：
1. 共享特征层 `build_features`（114 列）——对每根 bar 都计算，**天然非空转**：
   即使某张卡当轮不产生信号，它依赖的指标也被逐列验证。
2. 9 张卡的决策输出（long/short/side/signal）——逐卡验证。
3. 非空转棘轮：断言"能触发信号的卡集合"不小于已登记基线。若样本退化导致某些卡
   不再被任何形态触发，本项失败，避免测试退化成空转。

运行时约 15 秒：序列长度取 360 根（满足 ema_200 预热与扫描器 ≥210 的要求），
全量结果在判据 2/3 间复用，避免重复计算。
"""
from __future__ import annotations

import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import strategy_runtime as sr  # noqa: E402

OHLCV = ["open", "high", "low", "close", "volume"]
TAPE_BARS = 360
FEATURE_PROBES = 4
CARD_PROBES = 2
DECISION_COLUMNS = ("long", "short", "side", "signal")

# 已登记的触发基线：这些卡必须能被下方形态中的至少一种触发。
# 未列入的卡（F_S03_4H / F_S07_2H / F_S09_4H）需要推段-回撤之外的结构条件，
# 合成行情无法稳定复现；它们的覆盖依赖判据 1 的特征验证与真实行情对照
# （后者属联网门槛项，详见 docs 的 F-04 待办）。
BASELINE_FIRED = {
    "F_OLD_MACD_RSI_1H", "F_OLD_MACD_RSI_2H", "F_OLD_MACD_RSI_8H",
    "F_OLD_PO3_ANY_SIG_8H", "F_S08_1H", "F_OLD_KELTNER_2H",
}


def check(name: str, condition: bool, extra: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} {extra}".strip())
    print(f"  PASS  {name}" + (f"  [{extra}]" if extra else ""))


def _frame(closes, highs, lows, opens, vols) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(closes), freq="1h", tz="UTC")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=index)


def _walk(seed: int, n: int = TAPE_BARS) -> pd.DataFrame:
    """固定种子随机游走；确定性可复现，覆盖通用波动形态。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0, 0.006, n) + 0.0015 * np.sin(np.arange(n) / 40.0)
    close = 100.0 * np.exp(np.cumsum(ret))
    spread = np.abs(rng.normal(0, 0.004, n)) + 0.002
    volume = 800.0 + 500.0 * np.abs(rng.normal(0, 1, n))
    return _frame(close, close * (1 + spread), close * (1 - spread),
                  np.concatenate([[close[0]], close[:-1]]), volume)


def _range_tape(n: int = TAPE_BARS) -> pd.DataFrame:
    close = np.array([100.0 + 3.0 * math.sin(i / 9.0) + 1.2 * math.sin(i / 2.3)
                      for i in range(n)])
    volume = np.array([900.0 + 400.0 * abs(math.sin(i / 5.0)) for i in range(n)])
    return _frame(close, close * 1.004, close * 0.996,
                  np.concatenate([[close[0]], close[:-1]]), volume)


def _trend_tape(n: int = TAPE_BARS) -> pd.DataFrame:
    close = []
    price = 100.0
    for i in range(n):
        price *= 1.0 + 0.0012 + 0.004 * math.sin(i / 11.0)
        close.append(price)
    close = np.array(close)
    volume = np.array([1000.0 + 500.0 * abs(math.sin(i / 7.0)) for i in range(n)])
    return _frame(close, close * 1.005, close * 0.995,
                  np.concatenate([[close[0]], close[:-1]]), volume)


def _climax_tape(n: int = TAPE_BARS) -> pd.DataFrame:
    """在定点注入放量长实体，覆盖高潮/衰竭类规则。"""
    up, down = {120, 121, 122}, {240, 241, 242}
    close = []
    price = 100.0
    for i in range(n):
        if i in up:
            price *= 1.09
        elif i in down:
            price *= 0.91
        else:
            price *= 1.0 + 0.0015 * math.sin(i / 8.0)
        close.append(price)
    close = np.array(close)
    volume = np.array([4000.0 if i in (up | down) else 800.0 for i in range(n)])
    high = np.array([c * (1.03 if i in up else 1.004) for i, c in enumerate(close)])
    low = np.array([c * (0.97 if i in down else 0.996) for i, c in enumerate(close)])
    return _frame(close, high, low, np.concatenate([[close[0]], close[:-1]]), volume)


TAPES = {
    "range": _range_tape,
    "trend": _trend_tape,
    "climax": _climax_tape,
    "walk1": lambda: _walk(1),
    "walk12": lambda: _walk(12),
}


def _same(a, b) -> bool:
    """NaN 视为相等；其余要求严格相等（含符号与整数/浮点语义）。"""
    try:
        if a is None or b is None:
            return a is None and b is None
        if isinstance(a, np.generic):
            a = a.item()
        if isinstance(b, np.generic):
            b = b.item()
        a_nan = isinstance(a, float) and math.isnan(a)
        b_nan = isinstance(b, float) and math.isnan(b)
        if a_nan or b_nan:
            return a_nan and b_nan
        return a == b
    except (TypeError, ValueError):
        return False


def _probe_indices(length: int, count: int) -> list[int]:
    """探针必须含尾部连续区间，因为扫描器取的就是最后一根。"""
    start = max(210, length // 2)
    step = max(1, (length - start - count) // max(1, count))
    probes = list(range(start, length - 1, step))[:count]
    tail = list(range(max(start, length - 2), length))
    return sorted(set(probes + tail))


# ---------------------------------------------------------------- 判据 1
print("判据 1：共享特征层 build_features 的 prefix 不变性")
feature_probes = 0
feature_mismatches: list[str] = []
feature_columns = 0
for name, maker in TAPES.items():
    base = maker()[OHLCV]
    full = sr.build_features(base.copy())
    if full is None or full.empty:
        raise AssertionError(f"build_features 对 {name} 返回空结果")
    feature_columns = max(feature_columns, full.shape[1])
    for i in _probe_indices(len(base), FEATURE_PROBES):
        prefix = sr.build_features(base.iloc[: i + 1].copy())
        feature_probes += 1
        if prefix is None or len(prefix) != i + 1:
            feature_mismatches.append(f"{name}@i={i} 长度异常")
            continue
        last, ref = prefix.iloc[-1], full.iloc[i]
        for column in full.columns:
            if not _same(last.get(column), ref.get(column)):
                if len(feature_mismatches) < 5:
                    feature_mismatches.append(
                        f"{name}@i={i} {column}: prefix={last.get(column)!r} "
                        f"full={ref.get(column)!r}")
check("特征列已加载", feature_columns >= 100, f"columns={feature_columns}")
check("共享特征层无前视", not feature_mismatches,
      f"probes={feature_probes} columns={feature_columns} mismatch={len(feature_mismatches)}")
for line in feature_mismatches:
    print(f"        ! {line}")

# ---------------------------------------------------------------- 判据 2/3
print("\n判据 2：9 张策略卡的决策输出 prefix 不变性（全量结果同时用于判据 3）")
cards = json.loads(
    (ROOT / "knowledge" / "strategy_cards.json").read_text(encoding="utf-8"))["cards"]
check("策略卡数量为 9", len(cards) == 9, f"cards={len(cards)}")

card_probes = 0
card_mismatches: list[str] = []
signal_counts: dict[str, int] = {}
for card in cards:
    card_id = str(card.get("id"))
    fn_name = str(card.get("signal_fn") or "")
    fn = sr.REGISTRY.get(fn_name)
    if fn is None:
        card_mismatches.append(f"{card_id}: signal_fn={fn_name} 未注册")
        continue
    params = card.get("params") or {}
    fired_total = 0
    for name in ("range", "walk1", "walk12"):
        base = TAPES[name]()[OHLCV]
        full = fn(base.copy(), params)
        if full is None or full.empty:
            card_mismatches.append(f"{card_id}@{name}: 返回空结果")
            continue
        if "signal" in full.columns:
            fired_total += int((full["signal"].fillna(0) != 0).sum())
        for i in _probe_indices(len(base), CARD_PROBES):
            prefix = fn(base.iloc[: i + 1].copy(), params)
            card_probes += 1
            if prefix is None or len(prefix) != i + 1:
                card_mismatches.append(f"{card_id}@{name}@i={i}: 输出长度异常")
                continue
            last, ref = prefix.iloc[-1], full.iloc[i]
            for column in DECISION_COLUMNS:
                if column in full.columns and not _same(last.get(column), ref.get(column)):
                    if len(card_mismatches) < 5:
                        card_mismatches.append(
                            f"{card_id}@{name}@i={i} {column}: "
                            f"prefix={last.get(column)!r} full={ref.get(column)!r}")
    signal_counts[card_id] = fired_total

check("9张策略卡决策输出无前视", not card_mismatches,
      f"cards={len(cards)} probes={card_probes} mismatch={len(card_mismatches)}")
for line in card_mismatches:
    print(f"        ! {line}")

print("\n判据 3：非空转棘轮（信号触发覆盖率不得退化）")
fired = {card_id for card_id, count in signal_counts.items() if count > 0}
for card_id in sorted(signal_counts):
    print(f"        {card_id:<28} 触发 {signal_counts[card_id]}")
missing = sorted(BASELINE_FIRED - fired)
check("基线卡均被合成形态触发", not missing,
      f"fired={len(fired)}/9 missing={missing}")

print(f"\n结果: 5 通过 / 0 失败；特征列 {feature_columns}，"
      f"特征探针 {feature_probes}，卡片探针 {card_probes}；"
      f"已触发 {len(fired)}/9，未触发 {sorted({c['id'] for c in cards} - fired)}")
