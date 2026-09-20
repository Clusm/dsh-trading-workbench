"""N7 探针：EXPIRED_IN_MATCH 经私有流落库后的 status 与分量。

用法: python probe_n7_expired_in_match.py <tree_dir>
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(TREE / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.contracts import OrderQuantity  # noqa: E402
from exchange.event_models import ExchangeEvent  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402

SYMBOL = "ZZZUSDT"


def rehydrate(row):
    try:
        q = OrderQuantity.checked(
            order_id=str(row["exchange_order_id"] or row["client_order_id"]),
            accepted_qty=Decimal(str(row["accepted_qty_text"])),
            cumulative_filled_qty=Decimal(str(row["cumulative_filled_qty_text"])),
            working_leaves_qty=Decimal(str(row["working_leaves_qty_text"])),
            terminal_unfilled_qty=Decimal(str(row["terminal_unfilled_qty_text"] or "0")),
            status=str(row["status"]),
            revision=int(row["quantity_revision"] or 0),
        )
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return q.violations()


def ev(key, cid, payload, tid=None, oid="901"):
    return ExchangeEvent(
        event_key=key, event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=SYMBOL,
        client_order_id=cid, exchange_order_id=oid, trade_id=tid,
        payload={"o": payload}, supported=True)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="n7-probe-"))
    print(f"tree={TREE}")
    outbox = OrderOutbox(AuditDatabase(tmp / "n7.sqlite"), environment="demo")
    for tag, cid, oid, partial in (
            ("B1 无成交（挂单在撮合中 STP 过期）", "n7-b1", "921", False),
            ("B2 部分成交后过期", "n7-b2", "922", True)):
        print(f"\n[{tag}]")
        outbox.prepare(client_order_id=cid, symbol=SYMBOL, side="BUY",
                       position_side="LONG", order_type="LIMIT", purpose="ENTRY",
                       qty=0.02, price=100.0, reduce_only=False,
                       endpoint="/fapi/v1/order",
                       request={"symbol": SYMBOL, "newClientOrderId": cid,
                                "quantity": "0.02"}, qty_text="0.02")
        base = {"c": cid, "i": int(oid), "q": "0.02", "ap": "100.00", "s": SYMBOL}
        if partial:
            outbox.apply_event(ev(f"{cid}-p", cid, dict(base, **{
                "X": "PARTIALLY_FILLED", "z": "0.0047", "l": "0.0047", "t": 81001}),
                tid="81001", oid=oid))
        outbox.apply_event(ev(f"{cid}-x", cid, dict(base, **{
            "X": "EXPIRED_IN_MATCH", "l": "0", "t": 0}), oid=oid))
        row = outbox.db.fetch_one(
            "SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))
        print(f"  落库行: status={row['status']!r} accepted={row['accepted_qty_text']} "
              f"filled={row['cumulative_filled_qty_text']} "
              f"working={row['working_leaves_qty_text']} "
              f"terminal={row['terminal_unfilled_qty_text']}")
        print(f"  重水化 -> {rehydrate(row)}")
        # 迟到成交（终态修订）
        rev = outbox.apply_fill_revision(cid, fill_qty="0.001", fill_id="81999")
        r2 = outbox.db.fetch_one(
            "SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))
        print(f"  迟到成交后: status={r2['status']!r} "
              f"filled={r2['cumulative_filled_qty_text']} "
              f"working={r2['working_leaves_qty_text']} "
              f"terminal={r2['terminal_unfilled_qty_text']} "
              f"rev_status={rev and rev.get('status')}")
        print(f"  重水化 -> {rehydrate(r2)}")

    # 历史遗留别名行（模拟修复前写的行）在下一个事件到达时自愈
    print("\n[B3 历史遗留别名行的自愈]")
    cid = "n7-b3"
    outbox.prepare(client_order_id=cid, symbol=SYMBOL, side="BUY",
                   position_side="LONG", order_type="LIMIT", purpose="ENTRY",
                   qty=0.02, price=100.0, reduce_only=False,
                   endpoint="/fapi/v1/order",
                   request={"symbol": SYMBOL, "newClientOrderId": cid,
                            "quantity": "0.02"}, qty_text="0.02")
    outbox.db.conn.execute(
        "UPDATE order_outbox SET status='EXPIRED_IN_MATCH' WHERE client_order_id=?",
        (cid,))
    outbox.db.conn.commit()
    outbox.apply_event(ev("b3-x", cid, {"c": cid, "i": 931, "X": "EXPIRED_IN_MATCH",
                                        "q": "0.02", "l": "0", "t": 0,
                                        "ap": "100.00", "s": SYMBOL}, oid="931"))
    r3 = outbox.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))
    print(f"  自愈后: status={r3['status']!r} working={r3['working_leaves_qty_text']} "
          f"terminal={r3['terminal_unfilled_qty_text']} 重水化 -> {rehydrate(r3)}")
    outbox.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
