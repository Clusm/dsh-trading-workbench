"""N6 复现/验证探针：X=FILLED 帧（成交随帧到达）走生产私有流路径。

用法:  python probe_n6_filled_frame.py <tree_dir>
只读该树源码 + 临时 SQLite；不联网、不读 .env/data。
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


def violations(row) -> object:
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


def report(tag, outbox, cid):
    row = outbox.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))
    print(f"  {tag}")
    print(f"    status={row['status']} accepted={row['accepted_qty_text']} "
          f"filled={row['cumulative_filled_qty_text']} "
          f"working={row['working_leaves_qty_text']} "
          f"terminal={row['terminal_unfilled_qty_text']}")
    print(f"    violations={violations(row)}")
    risk = outbox.db.fetch_all(
        "SELECT details,action_taken,severity FROM risk_events ORDER BY id DESC LIMIT 3")
    for r in risk:
        print(f"    risk[{r['severity']}] {r['details']} action={r['action_taken']}")
    print(f"    last_error={outbox.last_error!r}")
    return row


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="n6-probe-"))
    print(f"tree={TREE}")
    db_path = tmp / "n6.sqlite"
    outbox = OrderOutbox(AuditDatabase(db_path), environment="demo")

    # ---------------- S1: 部分成交 -> 全成 ----------------
    print("\n[S1] 部分成交 -> 全成（X=PARTIALLY_FILLED 0.0105 后 X=FILLED 带 l=0.0095）")
    cid = "n6-s1"
    outbox.prepare(client_order_id=cid, symbol=SYMBOL, side="BUY", position_side="LONG",
                   order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
                   reduce_only=False, endpoint="/fapi/v1/order",
                   request={"symbol": SYMBOL, "newClientOrderId": cid,
                            "quantity": "0.02"}, qty_text="0.02")
    p1 = {"c": cid, "i": 901, "X": "PARTIALLY_FILLED", "q": "0.02", "z": "0.0105",
          "l": "0.0105", "t": 55501, "ap": "100.00", "s": SYMBOL}
    outbox.apply_event(ev("s1a", cid, p1, tid="55501"))
    report("after PARTIALLY_FILLED z=0.0105", outbox, cid)
    p2 = dict(p1, **{"X": "FILLED", "z": "0.02", "l": "0.0095", "t": 55502})
    outbox.apply_event(ev("s1b", cid, p2, tid="55502"))
    row = report("after FILLED z=0.02 (期望 filled=0.02 working=0 terminal=0.00)",
                 outbox, cid)
    s1_ok = (str(row["cumulative_filled_qty_text"]) == "0.02"
             and str(row["working_leaves_qty_text"]) == "0"
             and Decimal(str(row["terminal_unfilled_qty_text"])) == 0
             and violations(row) == ())

    # ---------------- S3: 一笔全成 ----------------
    print("\n[S3] 一笔全成（首帧即 X=FILLED z=0.02 l=0.02）")
    cid3 = "n6-s3"
    outbox.prepare(client_order_id=cid3, symbol=SYMBOL, side="BUY", position_side="LONG",
                   order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
                   reduce_only=False, endpoint="/fapi/v1/order",
                   request={"symbol": SYMBOL, "newClientOrderId": cid3,
                            "quantity": "0.02"}, qty_text="0.02")
    p3 = {"c": cid3, "i": 903, "X": "FILLED", "q": "0.02", "z": "0.02",
          "l": "0.02", "t": 55511, "ap": "100.00", "s": SYMBOL}
    outbox.apply_event(ev("s3a", cid3, p3, tid="55511", oid="903"))
    row3 = report("after FILLED z=0.02 (期望 filled=0.02 working=0 terminal=0.00)",
                  outbox, cid3)
    s3_ok = (str(row3["cumulative_filled_qty_text"]) == "0.02"
             and str(row3["working_leaves_qty_text"]) == "0"
             and Decimal(str(row3["terminal_unfilled_qty_text"])) == 0
             and violations(row3) == ())

    # ---------------- N1 不回退：CANCELED/EXPIRED/REJECTED 迟到成交 ----------------
    print("\n[N1] 终态 + 迟到成交（不得回退）")
    results = {}
    for status, oid, late in (("CANCELED", "911", "0.0003"),
                              ("EXPIRED", "912", "0.0010"),
                              ("REJECTED", "913", None)):
        c = f"n1-{status.lower()}"
        outbox.prepare(client_order_id=c, symbol=SYMBOL, side="BUY",
                       position_side="LONG", order_type="LIMIT", purpose="ENTRY",
                       qty=0.02, price=100.0, reduce_only=False,
                       endpoint="/fapi/v1/order",
                       request={"symbol": SYMBOL, "newClientOrderId": c,
                                "quantity": "0.02"}, qty_text="0.02")
        base = {"c": c, "i": int(oid), "q": "0.02", "ap": "100.00", "s": SYMBOL}
        outbox.apply_event(ev(f"{c}-p", c, dict(base, **{
            "X": "PARTIALLY_FILLED", "z": "0.0047", "l": "0.0047", "t": 70001}),
            tid="70001", oid=oid))
        if status == "REJECTED":
            row = outbox.db.fetch_one(
                "SELECT * FROM order_outbox WHERE client_order_id=?", (c,))
            results[status] = ("REJECTED 前置（部分成交后被拒，物理不可达）",
                               dict(row), violations(row))
            continue
        outbox.apply_event(ev(f"{c}-t", c, dict(base, **{
            "X": status, "l": "0", "t": 0}), oid=oid))
        rt = outbox.db.fetch_one(
            "SELECT * FROM order_outbox WHERE client_order_id=?", (c,))
        print(f"  {status} 终态帧后: status={rt['status']} "
              f"working={rt['working_leaves_qty_text']} "
              f"terminal={rt['terminal_unfilled_qty_text']} "
              f"viol={violations(rt)}")
        rev = outbox.apply_fill_revision(c, fill_qty=late, fill_id="79999")
        rr = outbox.db.fetch_one(
            "SELECT * FROM order_outbox WHERE client_order_id=?", (c,))
        print(f"  {status} 迟到成交 {late} 后: status={rr['status']} "
              f"filled={rr['cumulative_filled_qty_text']} "
              f"working={rr['working_leaves_qty_text']} "
              f"terminal={rr['terminal_unfilled_qty_text']} "
              f"viol={violations(rr)} rev={rev and rev.get('status')}")
        results[status] = ("详见上", dict(rr), violations(rr))

    n1_ok = all(
        r[1]["status"] in ("CANCELED", "EXPIRED", "REJECTED")
        and str(r[1]["working_leaves_qty_text"]) == "0"
        and r[2] == ()
        for k, r in results.items() if k != "REJECTED")

    print("\n=== SUMMARY ===")
    print(f"S1 partial->full  OK={s1_ok}")
    print(f"S3 one-shot full  OK={s3_ok}")
    print(f"N1 terminal kept  OK={n1_ok}")
    outbox.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
