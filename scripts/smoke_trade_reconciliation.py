"""成交解析、时间/持仓侧匹配和 Decimal 分配回归；不联网、不写真实数据库。"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from exchange.trade_reconciliation import (  # noqa: E402
    TradeReconciliationBook,
    aggregate_fills,
    allocate_fifo,
    close_side,
    compute_net_pnl,
    normalize_execution_fill,
    quantity_is_flat,
    select_close_fills,
)
from audit.database import AuditDatabase  # noqa: E402
from exchange.binance_futures import BinanceFuturesBroker  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


def fill(**overrides):
    raw = {
        "symbol": "BTCUSDT", "tradeId": "1", "orderId": "100",
        "side": "SELL", "positionSide": "LONG", "qty": "1",
        "price": "100", "realizedPnl": "0", "commission": "0.1",
        "commissionAsset": "USDT", "time": 1_000,
    }
    raw.update(overrides)
    return normalize_execution_fill(
        raw, environment="demo", account_ref="demo:abc", source="rest_reconcile")


print("\n[1] 真实零值与字段缺失严格区分")
zero = fill(realizedPnl="0")
missing = fill(tradeId="2", realizedPnl=None)
check("realizedPnl=0 保留为 Decimal 0", zero.realized_pnl == Decimal("0"))
check("缺失 realizedPnl 保留为 None", missing.realized_pnl is None)
aggregate = aggregate_fills([zero, missing])
check("混合完整性不会伪造总 realizedPnl", aggregate.realized_pnl is None)
check("未知成本不会计算伪造净收益", compute_net_pnl(Decimal("1"), Decimal("0"), None, Decimal("0")) is None)
check("完整成本按统一公式计算", compute_net_pnl(Decimal("1"), Decimal("0.1"), Decimal("0.2"), Decimal("-0.05")) == Decimal("0.65"))

print("\n[2] 时间、方向和持仓侧边界")
before = fill(tradeId="3", time=999)
short = fill(tradeId="4", positionSide="SHORT")
wrong_side = fill(tradeId="5", side="BUY")
after = fill(tradeId="6", time=1_001)
selected = select_close_fills(
    [after, wrong_side, short, before, zero],
    symbol="BTCUSDT", position_side="LONG", opened_at_ms=1_000,
)
check("只接受开仓边界之后的退出成交", [row.trade_id for row in selected] == ["1", "6"])
check("LONG平仓方向固定为 SELL", close_side("LONG") == "SELL")
check("SHORT平仓方向固定为 BUY", close_side("SHORT") == "BUY")
check("显式错误持仓侧不能混入", all(row.position_side in {"LONG", "BOTH"} for row in selected))

print("\n[3] 同订单拆分成交按真实数量聚合")
parts = [
    fill(tradeId="10", qty="3", price="101", realizedPnl="0.3", commission="0.03", time=2_000),
    fill(tradeId="11", qty="8", price="102", realizedPnl="0.8", commission="0.08", time=2_001),
    fill(tradeId="12", qty="9", price="103", realizedPnl="0.9", commission="0.09", time=2_002),
]
summary = aggregate_fills(parts)
check("3+8+9 数量完整守恒", summary.qty == Decimal("20"))
check("加权均价正确", summary.average_price == Decimal("2046") / Decimal("20"))
check("realizedPnl逐笔累加", summary.realized_pnl == Decimal("2.0"))
check("手续费按成交累加", summary.commission == Decimal("0.20"))

print("\n[4] 部分平仓 FIFO 与 stepSize")
allocations, remaining = allocate_fifo(parts, Decimal("20"), step_size=Decimal("1"))
check("全部三笔成交各只分配一次", [row.fill_key.split(":")[-1] for row in allocations] == ["10", "11", "12"])
check("数量完全归零", sum((row.allocated_qty for row in allocations), Decimal("0")) == Decimal("20") and remaining == 0)
allocations2, remaining2 = allocate_fifo(parts, Decimal("10"), step_size=Decimal("1"))
check("部分退出只抵扣目标数量", sum((row.allocated_qty for row in allocations2), Decimal("0")) == Decimal("10"))
check("部分退出保留未平数量", remaining2 == Decimal("0"))
check("非零剩余不能判定平仓", not quantity_is_flat(Decimal("0.6"), Decimal("1")))
check("stepSize内残余才判定平仓", quantity_is_flat(Decimal("0.4"), Decimal("1")))

print("\n[5] Broker分页、零值与完整生命周期归因")
with tempfile.TemporaryDirectory(prefix="mta-reconcile-") as temp_dir:
    db = AuditDatabase(Path(temp_dir) / "audit.sqlite")
    book = TradeReconciliationBook(db, environment="demo", account_ref="demo:acct")
    lifecycle = book.ensure_lifecycle(
        symbol="BTCUSDT", position_side="LONG", entry_qty=Decimal("6"),
        entry_price=Decimal("100"), opened_at_ms=1000,
        attribution_status="ATTRIBUTED", entry_release_id="open-1",
    )
    db.update_position_lifecycle(
        lifecycle_id=lifecycle["lifecycle_id"], entry_fee=0.0, funding_cashflow=0.0)
    calls = []
    first_page = [
        {"id": str(index), "orderId": "old", "symbol": "BTCUSDT", "side": "SELL",
         "positionSide": "LONG", "qty": "1", "price": "99", "realizedPnl": "0",
         "commission": "0", "commissionAsset": "USDT", "time": 900}
        for index in range(1, 1000)
    ]
    first_page.append({
        "id": "1000", "orderId": "close-1", "symbol": "BTCUSDT", "side": "SELL",
        "positionSide": "LONG", "qty": "3", "price": "101", "realizedPnl": "0",
        "commission": "0.03", "commissionAsset": "USDT", "time": 2000,
    })
    second_page = [{
        "id": "1001", "orderId": "close-1", "symbol": "BTCUSDT", "side": "SELL",
        "positionSide": "LONG", "qty": "3", "price": "102", "realizedPnl": "0",
        "commission": "0.03", "commissionAsset": "USDT", "time": 2001,
    }]
    broker = BinanceFuturesBroker.__new__(BinanceFuturesBroker)
    broker.db = db
    broker.environment = "demo"
    broker.trade_reconciliation = book
    broker._ledger_source = lambda symbol: ("ai", {"source": "ai"})
    broker._step_size = lambda symbol: Decimal("1")

    def signed(method, path, params=None, attempts=1):
        calls.append(dict(params or {}))
        return first_page if "fromId" not in (params or {}) else second_page

    broker._signed = signed
    broker._reconcile_one_close("BTCUSDT")
    check("满页后用 fromId 推进且保持明确 endTime", len(calls) == 2 and "startTime" in calls[0]
          and calls[0].get("endTime") is not None and calls[1].get("fromId") == 1001)
    check("分页重叠不会漏掉第二页成交", db.fetch_one(
        "SELECT COUNT(*) n FROM execution_fills")["n"] == 1001)
    outcome = db.fetch_one("SELECT * FROM trade_outcomes WHERE lifecycle_id=?",
                           (lifecycle["lifecycle_id"],))
    check("真实 realizedPnl=0 未被手算收益覆盖", outcome is not None
          and outcome["gross_realized"] == 0 and outcome["pnl"] == -0.06)
    lifecycle_row = db.fetch_one(
        "SELECT exit_notional FROM position_lifecycles WHERE lifecycle_id=?",
        (lifecycle["lifecycle_id"],))
    check("退出名义金额按分配数量乘成交价计算", lifecycle_row is not None
          and lifecycle_row["exit_notional"] == 609.0)
    check("同一订单两笔成交只展示一条订单", db.fetch_one(
        "SELECT COUNT(*) n FROM orders WHERE exchange_order_id='close-1'")["n"] == 1)
    check("补账任务最终收口为 DONE", db.fetch_one(
        "SELECT status FROM trade_reconciliation_jobs WHERE lifecycle_id=?",
        (lifecycle["lifecycle_id"],))["status"] == "DONE")
    db.close()

print("\n结果: 23 通过 / 0 失败")
