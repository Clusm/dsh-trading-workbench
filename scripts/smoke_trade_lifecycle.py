"""生命周期数据库与幂等补账回归；只使用临时 SQLite。"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.trade_reconciliation import (  # noqa: E402
    TradeReconciliationBook,
    normalize_execution_fill,
)


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


def make_fill(trade_id: str, qty: str, price: str, time_ms: int,
              realized: str = "0.5", commission: str = "0.01"):
    return normalize_execution_fill(
        {
            "symbol": "BTCUSDT", "tradeId": trade_id, "orderId": "order-" + trade_id,
            "side": "SELL", "positionSide": "LONG", "qty": qty, "price": price,
            "realizedPnl": realized, "commission": commission,
            "commissionAsset": "USDT", "time": time_ms,
        }, environment="demo", account_ref="demo:acct", source="rest_reconcile")


with tempfile.TemporaryDirectory(prefix="mta-lifecycle-") as temp_dir:
    db = AuditDatabase(Path(temp_dir) / "audit.sqlite")
    book = TradeReconciliationBook(db, environment="demo", account_ref="demo:acct")

    print("\n[1] 同一仓位创建唯一生命周期")
    lifecycle = book.ensure_lifecycle(
        symbol="BTCUSDT", position_side="LONG", entry_qty=Decimal("20"),
        entry_price=Decimal("100"), opened_at_ms=1000,
        attribution_status="ATTRIBUTED", entry_release_id="release-1",
    )
    same = book.ensure_lifecycle(
        symbol="BTCUSDT", position_side="LONG", entry_qty=Decimal("20"),
        entry_price=Decimal("100"), opened_at_ms=1000,
        attribution_status="ATTRIBUTED", entry_release_id="release-1",
    )
    check("重复同步不创建第二个活动生命周期", lifecycle["lifecycle_id"] == same["lifecycle_id"])
    check("生命周期保存开仓数量与边界", lifecycle["entry_qty"] == 20 and lifecycle["opened_at_ms"] == 1000)
    db.update_position_lifecycle(
        lifecycle_id=lifecycle["lifecycle_id"], entry_fee=0.1, funding_cashflow=0.0)

    print("\n[2] 分两次部分平仓，数量和消费关系守恒")
    first_raw = [
        {"symbol": "BTCUSDT", "tradeId": "1", "orderId": "close-a", "side": "SELL",
         "positionSide": "LONG", "qty": "3", "price": "101", "realizedPnl": "3",
         "commission": "0.03", "commissionAsset": "USDT", "time": 2000},
        {"symbol": "BTCUSDT", "tradeId": "2", "orderId": "close-a", "side": "SELL",
         "positionSide": "LONG", "qty": "7", "price": "102", "realizedPnl": "7",
         "commission": "0.07", "commissionAsset": "USDT", "time": 2001},
    ]
    first, inserted = book.ingest(first_raw, source="rest_reconcile")
    check("REST首批成交全部进入统一流水", inserted == 2)
    partial = book.reconcile(
        lifecycle["lifecycle_id"], first, position_qty=Decimal("10"), step_size=Decimal("1"))
    check("部分平仓只分配实际10张", partial.allocated_qty == Decimal("10"))
    check("仓位仍有10张时不能提前关闭", partial.status == "OPEN" and not partial.flat_evidence)

    second_raw = [
        {"symbol": "BTCUSDT", "tradeId": "3", "orderId": "close-b", "side": "SELL",
         "positionSide": "LONG", "qty": "8", "price": "103", "realizedPnl": "8",
         "commission": "0.08", "commissionAsset": "USDT", "time": 3000},
        {"symbol": "BTCUSDT", "tradeId": "4", "orderId": "close-b", "side": "SELL",
         "positionSide": "LONG", "qty": "2", "price": "104", "realizedPnl": "2",
         "commission": "0.02", "commissionAsset": "USDT", "time": 3001},
    ]
    second, inserted_second = book.ingest(second_raw, source="user_stream")
    check("WS重复来源也写入同一成交表", inserted_second == 2)
    closed = book.reconcile(
        lifecycle["lifecycle_id"], second, position_qty=Decimal("0"), step_size=Decimal("1"))
    check("第二次平仓后数量完全守恒", closed.allocated_qty == Decimal("20") and closed.remaining_qty == 0)
    check("只有仓位快照与成交证据一致才关闭", closed.status == "CLOSED" and closed.flat_evidence)
    check("真实成交时间作为关闭时间候选", closed.actual_closed_at_ms == 3001)
    lifecycle_row = db.fetch_one(
        "SELECT exit_notional FROM position_lifecycles WHERE lifecycle_id=?",
        (lifecycle["lifecycle_id"],))
    check("退出名义金额按分配数量乘成交价计算", lifecycle_row is not None
          and lifecycle_row["exit_notional"] == 2049.0)
    check("realizedPnl/费用完整时可计算净收益", closed.net_pnl == Decimal("19.7"))
    check("四笔成交各只消费一次", db.fetch_one(
        "SELECT COUNT(*) n FROM lifecycle_fill_allocations WHERE lifecycle_id=?",
        (lifecycle["lifecycle_id"],))["n"] == 4)

    print("\n[3] 重试、跨来源去重和任务游标")
    _, duplicate_inserted = book.ingest(second_raw, source="rest_reconcile")
    check("REST/WS重叠重试不新增成交", duplicate_inserted == 0)
    repeated = book.reconcile(
        lifecycle["lifecycle_id"], second, position_qty=Decimal("0"), step_size=Decimal("1"))
    check("重复补账不新增分配", repeated.inserted_allocations == 0)
    db.upsert_trade_reconciliation_job(
        job_id="job-1", lifecycle_id=lifecycle["lifecycle_id"], environment="demo",
        account_ref="demo:acct", symbol="BTCUSDT", position_side="LONG",
        lower_bound_ms=1000, upper_bound_ms=5000, status="RETRY", reason="网络暂时失败",
        cursor_time_ms=2001, cursor_trade_id="2", attempt_count=2,
        last_error="TimeoutError",
    )
    db.update_trade_reconciliation_job(
        job_id="job-1", status="DONE", cursor_time_ms=3001, cursor_trade_id="4",
        reason="已收口")
    job = db.fetch_one("SELECT * FROM trade_reconciliation_jobs WHERE job_id='job-1'")
    check("补账任务保留边界和稳定游标", job["lower_bound_ms"] == 1000 and job["cursor_trade_id"] == "4")
    check("生命周期状态摘要可观测", db.trade_reconciliation_status("demo")["fills"] == 4)

    print("\n[4] 事务失败完整回滚")
    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO execution_fills(fill_key,environment,account_ref,symbol,side,qty,price,"
                "trade_time_ms,source,received_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("rollback", "demo", "demo:acct", "BTCUSDT", "SELL", 1, 100,
                 9000, "test", "now", "{}"),
            )
            raise RuntimeError("injected")
    except RuntimeError:
        pass
    check("成交和分配事务异常时不留下半行", db.fetch_one(
        "SELECT COUNT(*) n FROM execution_fills WHERE fill_key='rollback'")["n"] == 0)
    db.close()

print("\n结果: 19 通过 / 0 失败")
