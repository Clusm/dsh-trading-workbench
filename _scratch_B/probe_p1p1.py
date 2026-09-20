"""P1-P1 修复前后对照探针（离线；两个树各跑一次）。

用法：python probe_p1p1.py <tree_root>

只替换交易所 I/O 边界（查 Algo 单 / 查持仓 / 挂单 / DELETE），让
``_ensure_protection_from_order -> _resync_protection -> _resync_protection_impl``
走真实生产路径。失败类别由生产代码自己产生，探针**不注入**任何内部字段。

场景（与 A 的 P1-P1 反例逐周期对应）：
  周期 N   查询 Algo 单失败（无 SL/TP 记录）  -> 类别 QUERY_FAILED
  周期 N+1 查询成功且无既有保护              -> 期望"已复位"
  周期 N+2 真实保护挂单失败                  -> 必须按 P0 兜底全平
  跨标的   A 标的 QUERY_FAILED，B 标的保护失败 -> B 必须全平
"""
from __future__ import annotations

import sys
import tempfile
import threading
from decimal import Decimal
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(TREE / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402

SYMBOL = "ZZZUSDT"
OTHER = "AAAUSDT"


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mta-p1p1-"))
    db = AuditDatabase(tmp / "probe.sqlite")
    closes: list = []
    log: list[str] = []

    def make_broker(*, algos, position, place_raises=False):
        b = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
        b.db = db
        b.order_outbox = None
        b._resync_lock = threading.RLock()
        b._open_algos = lambda _s: (None if algos is None else list(algos))
        b._algo_prices = lambda _s: {}
        b._single_symbol_position = lambda _s: dict(position or {})
        b._format_qty = lambda _s, q: str(q)
        b._tick_size = lambda _s: 0.01
        b._min_qty = lambda _s: Decimal("0.001")
        b._is_hedge_mode = lambda: False
        b._cancel_symbol_algos = lambda _s: None

        def place(*_a, **_k):
            if place_raises:
                raise RuntimeError("synthetic protection place failure")
            return None

        b._place_algo_protection = place
        b._close_or_reduce = lambda intent: (closes.append(intent),
                                             log.append(f"CLOSE {intent.symbol}"))
        return b

    query_fail_order = {"symbol": SYMBOL, "side": "LONG", "qty": 0.1, "source": "ai"}
    open_order = {"symbol": SYMBOL, "side": "LONG", "qty": 0.1, "source": "ai",
                  "stop_loss": 95.0, "take_profit": 105.0}
    live = {"symbol": SYMBOL, "positionAmt": "0.1", "positionSide": "LONG",
            "markPrice": "100.0"}
    flat = {"symbol": SYMBOL, "positionAmt": "0.0", "positionSide": "LONG",
            "markPrice": "100.0"}

    print(f"=== tree: {TREE} ===")

    # ---- A 报告里的顺序场景：N 查询失败 -> N+1 成功 -> N+2 保护失败 ----
    # 注意：_ensure_protection_from_order 每次都自己跑一轮 resync，失败路径在**同一次
    # 调用内**重写失败类别，因此"上一轮残余"在该顺序下读不到。
    make_broker(algos=None, position=live)._ensure_protection_from_order(
        dict(query_fail_order))
    make_broker(algos=[], position=flat)._ensure_protection_from_order(
        dict(query_fail_order))
    make_broker(algos=[], position=live, place_raises=True
                )._ensure_protection_from_order(dict(open_order))
    a_seq = len(closes)
    print(f"[A 顺序场景] N/N+1 之后真实保护失败 -> closes={a_seq} {log}")
    print(f"   => 顺序残余{'抑制了' if a_seq == 0 else '未抑制'}兜底平仓")

    # ---- 可达形式：**同一个** broker 实例上，另一标的的周期在本调用返回后、判定前写入 ----
    # 真实循环里一个 broker 对象串行处理多个标的，且 _resync_protection 释放锁之后
    # 才读类别 -> 另一标的的 QUERY_FAILED 会盖掉本标的刚写入的 PROTECTION_FAILED。
    closes.clear()
    log.clear()
    target = make_broker(algos=[], position=live, place_raises=True)
    # 同一实例内按标的分派：本标的查询成功、另一标的查询失败
    target._open_algos = lambda s: (None if str(s) == OTHER else [])
    real_resync = target._resync_protection

    def interleaved(symbol, **kwargs):
        ok = real_resync(symbol, **kwargs)
        # 另一标的的查询失败周期此刻写入失败类别（同一实例、另一线程）
        real_resync(OTHER)
        return ok

    target._resync_protection = interleaved
    target._ensure_protection_from_order(dict(open_order))
    print(f"[同一实例跨标的] 本标的保护确证失败 -> closes={len(closes)} {log}")
    print(f"   => 另一标的的残余{'抑制了' if len(closes) == 0 else '未抑制'}"
          f"本标的的兜底平仓")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
