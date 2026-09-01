"""选品逻辑离线单元测试。

不联网、不碰数据库，用构造的 tickers 验证核心池过滤的正确性。
运行：python scripts/test_symbol_selection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from data.binance_public import (  # noqa: E402
    FALLBACK_SYMBOLS,
    select_top_symbols,
)

# 真实世界里被选进来的噪声标的：美股 / 商品代币化合约
NOISE = ["SOXLUSDT", "XAGUSDT", "XAUUSDT", "SKHYNIXUSDT", "SNDKUSDT", "SPCXUSDT"]

CORE = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "ZECUSDT", "XRPUSDT"]

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  -> {detail}" if detail else ""))


def tickers(pairs: list[tuple[str, float]]) -> list[dict]:
    return [{"symbol": s, "quoteVolume": str(v)} for s, v in pairs]


print("=" * 68)
print("用例 1：核心池过滤 —— 噪声标的成交额再高也不该被选中")
print("=" * 68)
# 噪声标的成交额最高（这正是它们在真实数据里冲进前十的原因）
raw = [("SOXLUSDT", 900.0), ("XAGUSDT", 800.0), ("BTCUSDT", 700.0),
       ("XAUUSDT", 600.0), ("ETHUSDT", 500.0), ("ZECUSDT", 400.0),
       ("SOLUSDT", 300.0), ("XRPUSDT", 200.0)]
got = select_top_symbols(tickers(raw), top_n=10, core_symbols=CORE)
check("结果全部落在核心池内", set(got) <= set(CORE), f"实际={got}")
check("不含任何噪声标的", not (set(got) & set(NOISE)), f"混入={set(got) & set(NOISE)}")
check("核心池标的全部被选中", set(got) == set(CORE), f"实际={got}")
check("按成交额降序", got == ["BTCUSDT", "ETHUSDT", "ZECUSDT", "SOLUSDT", "XRPUSDT"], f"实际={got}")

print()
print("=" * 68)
print("用例 2：反向验证 —— 不传核心池时噪声确实会混入（证明修复前的行为）")
print("=" * 68)
got_plain = select_top_symbols(tickers(raw), top_n=10)
mixed = set(got_plain) & set(NOISE)
check("不传 core_symbols 时噪声会混入（复现旧行为）", bool(mixed), f"实际混入={mixed}")
check("传 core_symbols 后同一份数据零噪声",
      not (set(select_top_symbols(tickers(raw), top_n=10, core_symbols=CORE)) & set(NOISE)))

print()
print("=" * 68)
print("用例 3：top_n 截断与成交额排序")
print("=" * 68)
got3 = select_top_symbols(tickers(raw), top_n=3, core_symbols=CORE)
check("top_n=3 只取前 3", got3 == ["BTCUSDT", "ETHUSDT", "ZECUSDT"], f"实际={got3}")

print()
print("=" * 68)
print("用例 4：核心池里的标的拉不到行情时自然跳过，不报错")
print("=" * 68)
partial = [("BTCUSDT", 700.0), ("ETHUSDT", 500.0)]  # SOL/ZEC/XRP 不在 tickers 里
got4 = select_top_symbols(tickers(partial), top_n=10, core_symbols=CORE)
check("只返回有行情的标的", got4 == ["BTCUSDT", "ETHUSDT"], f"实际={got4}")

print()
print("=" * 68)
print("用例 5：核心池完全匹配不上时返回空，由上层走 FALLBACK_SYMBOLS 降级")
print("=" * 68)
got5 = select_top_symbols(tickers([("SOXLUSDT", 900.0)]), top_n=10, core_symbols=CORE)
check("返回空列表（而非抛异常）", got5 == [], f"实际={got5}")
check("FALLBACK_SYMBOLS 可用作兜底", len(FALLBACK_SYMBOLS) >= 5, f"长度={len(FALLBACK_SYMBOLS)}")

print()
print("=" * 68)
print("用例 6：稳定币计价的稳定币合约被剔除（USDCUSDT 等）")
print("=" * 68)
stable = [("USDCUSDT", 999.0), ("BTCUSDT", 700.0), ("FDUSDUSDT", 600.0)]
got6 = select_top_symbols(tickers(stable), top_n=10, core_symbols=["USDCUSDT", "BTCUSDT", "FDUSDUSDT"])
check("USDCUSDT/FDUSDUSDT 被剔除", got6 == ["BTCUSDT"], f"实际={got6}")

print()
print("=" * 68)
print("用例 7：非 USDT 计价标的被剔除")
print("=" * 68)
got7 = select_top_symbols(tickers([("BTCBUSD", 900.0), ("ETHUSDT", 500.0)]),
                          top_n=10, core_symbols=["BTCBUSD", "ETHUSDT"])
check("BTCBUSD 被剔除", got7 == ["ETHUSDT"], f"实际={got7}")

print()
print("=" * 68)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 68)
sys.exit(1 if FAIL else 0)
