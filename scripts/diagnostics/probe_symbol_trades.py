"""按 symbol 打印交易所成交流水，用于核对补记数量/价格是否对得上。

用法：
    python scripts/probe_symbol_trades.py UNIUSDT
    python scripts/probe_symbol_trades.py UNIUSDT BTCUSDT ETHUSDT SOLUSDT
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from probe_exchange_positions import signed_get  # noqa: E402


def ts(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%m-%d %H:%M:%S")


def main() -> None:
    symbols = sys.argv[1:] or ["UNIUSDT"]
    for sym in symbols:
        print("=" * 78)
        print(f"{sym}  成交流水（最近 50 笔）")
        print(f"{'时间(UTC)':<16}{'方向':<7}{'数量':<10}{'价格':<12}{'已实现盈亏':<14}{'手续费':<12}{'orderId'}")
        trades = signed_get("/fapi/v1/userTrades", {"symbol": sym, "limit": 50})
        if not trades:
            print("  （无成交）")
            continue
        net = 0.0
        for t in sorted(trades, key=lambda x: int(x.get("time", 0))):
            side = t["side"]
            qty = float(t["qty"])
            net += qty if side == "BUY" else -qty
            print(
                f"{ts(int(t['time'])):<16}{side:<7}{qty:<10.4f}{float(t['price']):<12.4f}"
                f"{float(t.get('realizedPnl') or 0):<14.6f}"
                f"{float(t.get('commission') or 0):<12.6f}{t.get('orderId')}"
            )
        print(f"  BUY 累计 - SELL 累计 = {net:.4f}（0 表示已平干净）")


if __name__ == "__main__":
    main()
