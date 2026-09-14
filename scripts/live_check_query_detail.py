"""实测 query_symbol_detail 在真实网络下能否成功（不 mock）。

用途：区分「代码有 bug」与「网络不通」。
用法: python scripts/live_check_query_detail.py [SYMBOL] [INTERVAL]
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "15m"

    # 基础探测也必须走共享网关。维护脚本绕过全局418冷却会让排障行为本身
    # 延长出口封禁，进而把网络问题误判成业务代码故障。
    import httpx
    from data.binance_transport import request_json
    from data.rate_limit import configure_rate_limit_state
    configure_rate_limit_state(ROOT / "data" / "public_rate_limit_state.json")
    print("=" * 78)
    print("【0】基础网络探测")
    print("=" * 78)
    t0 = time.time()
    try:
        with httpx.Client(timeout=8.0) as client:
            request_json(client, "/fapi/v1/time", attempts=1, priority="background")
        print(f"  Binance USD-M 公共网关  ->  HTTP 200  ({time.time()-t0:.2f}s)")
    except Exception as e:
        print(f"  Binance USD-M 公共网关  ->  {type(e).__name__}: "
              f"{str(e)[:110]}  ({time.time()-t0:.2f}s)")

    print()
    print("=" * 78)
    print(f"【1】真实调用 query_symbol_detail({symbol!r}, {interval!r})")
    print("=" * 78)
    try:
        from data.market_query import query_symbol_detail
    except Exception:
        traceback.print_exc()
        return 2

    t0 = time.time()
    try:
        out = query_symbol_detail(symbol=symbol, interval=interval, limit=120)
    except Exception as e:
        print(f"  抛异常: {type(e).__name__}: {str(e)[:250]}")
        traceback.print_exc()
        return 1
    dt = time.time() - t0
    s = str(out)
    print(f"  耗时 {dt:.2f}s   返回长度 {len(s)}")
    if "Invalid type for url" in s:
        print("  [FAIL] 仍在报 TypeError 名字遮蔽 -> 代码问题")
        return 1
    if '"error"' in s:
        print(f"  [FAIL] 返回错误: {s[:300]}")
        return 1
    print(f"  [OK] 前 260 字符:\n  {s[:260]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
