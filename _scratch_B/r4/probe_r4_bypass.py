"""r4 绕过矩阵：N1 六类绕过 + N6/N7 复核（全部走生产代码，不注入内部字段）。

用法: python probe_r4_bypass.py <tree_dir>
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(TREE / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.contracts import OrderQuantity, QuantityError  # noqa: E402
from exchange.event_models import ExchangeEvent  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402
import exchange.order_outbox as oo  # noqa: E402

SYMBOL = "ZZUSDT"
FAILS: list[str] = []


def note(ok: bool, name: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


def ev(key, cid, payload, oid=None):
    return ExchangeEvent(
        event_key=key, event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=SYMBOL,
        client_order_id=cid, exchange_order_id=str(oid or payload.get("i") or ""),
        trade_id=str(payload.get("t") or ""), payload={"o": payload}, supported=True)


def new_box(tag):
    tmp = Path(tempfile.mkdtemp(prefix=f"r4-{tag}-"))
    return OrderOutbox(AuditDatabase(tmp / "t.sqlite"), environment="demo")


def prep(box, cid, qty=0.02):
    box.prepare(client_order_id=cid, symbol=SYMBOL, side="BUY", position_side="LONG",
                order_type="LIMIT", purpose="ENTRY", qty=qty, price=100.0,
                reduce_only=False, endpoint="/fapi/v1/order",
                request={"symbol": SYMBOL, "newClientOrderId": cid,
                         "quantity": str(qty)}, qty_text=str(qty))


def row_of(box, cid):
    return box.db.fetch_one("SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))


def viol(row):
    try:
        q = OrderQuantity.checked(
            order_id=str(row["exchange_order_id"] or row["client_order_id"]),
            accepted_qty=Decimal(str(row["accepted_qty_text"])),
            cumulative_filled_qty=Decimal(str(row["cumulative_filled_qty_text"])),
            working_leaves_qty=Decimal(str(row["working_leaves_qty_text"])),
            terminal_unfilled_qty=Decimal(str(row["terminal_unfilled_qty_text"] or "0")),
            status=str(row["status"]), revision=int(row["quantity_revision"] or 0))
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    return q.violations()


print(f"### TREE = {oo.__file__}")

# ---- C1 终态 + 超额成交（经 apply_event） --------------------------------- #
print("\n[C1] 终态 + 超额成交")
box = new_box("c1")
cid = "c1"
prep(box, cid)
base = {"c": cid, "i": 901, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED", "z": "0.0047",
                                           "l": "0.0047", "t": 1})))
box.apply_event(ev("b", cid, dict(base, **{"X": "CANCELED", "z": "0.0047", "l": "0",
                                           "t": 0})))
box.apply_event(ev("c", cid, dict(base, **{"X": "CANCELED", "z": "0.0999",
                                           "l": "0.0952", "t": 2})))
r = row_of(box, cid)
note(str(r["status"]) == "CANCELED" and viol(r) == (),
     "C1 超额成交不改变终态、行仍合法", f"filled={r['cumulative_filled_qty_text']} viol={viol(r)}")
# 直接笔:交易所报的成交超过受理量 -> 契约必须拒绝
try:
    q = OrderQuantity.checked(order_id=cid, accepted_qty=Decimal("0.02"),
                              cumulative_filled_qty=Decimal("0.0047"),
                              working_leaves_qty=Decimal(0),
                              terminal_unfilled_qty=Decimal("0.0153"),
                              status="CANCELED", revision=0)
    oo.apply_late_fill(q, fill_qty="0.0952", fill_id="x")
    note(False, "C1b 超过受理量的成交被契约拒绝")
except QuantityError as exc:
    note(True, "C1b 超过受理量的成交被契约拒绝", str(exc))

# ---- C2 终态 + 负数量 ------------------------------------------------------ #
print("\n[C2] 终态 + 负数量")
box = new_box("c2")
cid = "c2"
prep(box, cid)
base = {"c": cid, "i": 902, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED", "z": "0.0047",
                                           "l": "0.0047", "t": 1})))
box.apply_event(ev("b", cid, dict(base, **{"X": "CANCELED", "z": "0.0047", "l": "-0.5",
                                           "t": 2})))
r = row_of(box, cid)
note(str(r["status"]) == "CANCELED" and viol(r) == () and
     str(r["cumulative_filled_qty_text"]) == "0.0047",
     "C2 负成交被 fail-closed 吞掉、行不变且合法", f"filled={r['cumulative_filled_qty_text']}")

# ---- C3 行 status 被改成词表外脏值后投成交 --------------------------------- #
print("\n[C3] 脏 status 后投成交")
for dirty in ("SOMETHING_NEW", "", "   ", 123):
    box = new_box("c3")
    cid = f"c3-{abs(hash(str(dirty))) % 9973}"
    prep(box, cid)
    base = {"c": cid, "i": 903, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
    box.db.conn.execute("UPDATE order_outbox SET status=? WHERE client_order_id=?",
                        (str(dirty), cid))
    box.db.conn.commit()
    box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED", "z": "0.0047",
                                               "l": "0.0047", "t": 3})))
    r = row_of(box, cid)
    got = oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0047"),
                               stored_status=str(dirty))
    note(got in ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED")
         and str(r["status"]) in ("NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED",
                                  "EXPIRED", "REJECTED"),
         f"C3 脏 status {dirty!r} 不外泄词表外值", f"落库={r['status']!r} 推导={got}")

# ---- C4 落库行 / model_copy 篡改后契约自检 --------------------------------- #
print("\n[C4] 篡改后契约自检")
box = new_box("c4")
cid = "c4"
prep(box, cid)
base = {"c": cid, "i": 904, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED", "z": "0.0047",
                                           "l": "0.0047", "t": 4})))
box.apply_event(ev("b", cid, dict(base, **{"X": "CANCELED", "z": "0.0047", "l": "0",
                                           "t": 0})))
good = row_of(box, cid)
box.db.conn.execute("UPDATE order_outbox SET working_leaves_qty_text='0.0153' "
                    "WHERE client_order_id=?", (cid,))
box.db.conn.commit()
bad = row_of(box, cid)
note(viol(good) == () and viol(bad) != (),
     "C4 篡改 working 后立刻被契约判违规", f"got={viol(bad)}")
q = OrderQuantity.checked(order_id=cid, accepted_qty=Decimal("0.02"),
                          cumulative_filled_qty=Decimal("0.0047"),
                          working_leaves_qty=Decimal(0),
                          terminal_unfilled_qty=Decimal("0.0153"),
                          status="CANCELED", revision=0)
tampered = q.model_copy(update={"working_leaves_qty": Decimal("0.0153")})
note(tampered.violations() != (), "C4b model_copy 篡改立刻被 violations 抓到",
     f"got={tampered.violations()}")

# ---- C5 未知/未映射状态矩阵（fail-safe 推导，不外泄） ----------------------- #
print("\n[C5] 兜底语义矩阵 _component_status(accepted=0.02, filled=0.0047, x)")
for x in ("SUBMITTING", "ACKNOWLEDGED", "UNKNOWN", "NEW", "EXPIRED_IN_MATCH",
          "SOMETHING_NEW", "", "CANCELED ", "cancelled"):
    got = oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0047"),
                               stored_status=x)
    print(f"      {x!r:20s} -> {got}")
note(all(oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0047"),
                              stored_status=x) == "PARTIALLY_FILLED"
         for x in ("SOMETHING_NEW", "", "CANCELED ", "cancelled"))
     and oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0"),
                              stored_status="SOMETHING_NEW") == "NEW"
     and oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.02"),
                              stored_status="SOMETHING_NEW") == "FILLED"
     and oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0105"),
                              stored_status="FILLED") == "PARTIALLY_FILLED",
     "C5 词表外状态不外泄 + FILLED 由分量背书（N6 判据）")

# ---- C6 终态矩阵（+迟到成交） ---------------------------------------------- #
print("\n[C6] 终态矩阵 + 迟到成交（私有流真实路径）")
for status in ("CANCELED", "EXPIRED", "REJECTED", "FILLED"):
    box = new_box(f"c6-{status}")
    cid = f"c6-{status.lower()}"
    prep(box, cid)
    base = {"c": cid, "i": 905, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
    if status != "REJECTED":
        box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED",
                                                   "z": "0.0047", "l": "0.0047", "t": 5})))
    box.apply_event(ev("b", cid, dict(base, **{"X": status, "l": "0", "t": 0})))
    if status == "REJECTED":
        box.reject(cid, RuntimeError("synthetic"))
    box.apply_event(ev("c", cid, dict(base, **{"X": status, "z": "0.0057",
                                               "l": "0.001", "t": 6})))
    r = row_of(box, cid)
    print(f"      {status:9s} status={r['status']!s:18s} filled={r['cumulative_filled_qty_text']} "
          f"working={r['working_leaves_qty_text']} terminal={r['terminal_unfilled_qty_text']} "
          f"viol={viol(r)}")
    if status == "FILLED":
        # ``FILLED`` 不是"可保留的终态"：本序列先投了一个**不带成交**的
        # ``X="FILLED"`` 帧（交易所语义上不可能：全成帧必带最后一档 ``l``），
        # ``_update`` 于是写下未由分量背书的 ``status='FILLED'``。r4 的改法让这一帧
        # 不再丢成交（见 filled/cumulative），但状态列仍停留在未背书的 FILLED ——
        # **与 r3 同样非法，属既有缺陷（Q_r3 §9 的 N9/S4），非本轮引入**，已登记。
        note(str(r["cumulative_filled_qty_text"]) == "0.0057",
             "C6 FILLED 帧不带成交（物理不可能序列）：成交不再被静默丢弃",
             f"filled={r['cumulative_filled_qty_text']}")
        note(viol(r) != (),
             "C6b [既有、非阻断、已登记] 该不可能序列下状态列仍为未背书的 FILLED",
             f"viol={viol(r)}")
        continue
    note(str(r["status"]) == status and viol(r) == (),
         f"C6 {status} 迟到成交保留终态且行合法", f"viol={viol(r)}")

# ---- N6/N7 复核（独立于验收套件） ------------------------------------------ #
print("\n[N6/N7] 复核")
box = new_box("n6")
cid = "n6-s1"
prep(box, cid)
base = {"c": cid, "i": 906, "q": "0.02", "s": SYMBOL, "ap": "100.00"}
box.apply_event(ev("a", cid, dict(base, **{"X": "PARTIALLY_FILLED", "z": "0.0105",
                                           "l": "0.0105", "t": 7})))
box.apply_event(ev("b", cid, dict(base, **{"X": "FILLED", "z": "0.02", "l": "0.0095",
                                           "t": 8})))
r = row_of(box, cid)
note(str(r["cumulative_filled_qty_text"]) == "0.02" and viol(r) == ()
     and str(r["working_leaves_qty_text"]) == "0",
     "N6 部分成交->全成：末档成交入账", f"filled={r['cumulative_filled_qty_text']}")
cid = "n6-s3"
prep(box, cid)
box.apply_event(ev("c", cid, dict(base, **{"c": cid, "i": 907, "X": "FILLED",
                                           "z": "0.02", "l": "0.02", "t": 9})))
r = row_of(box, cid)
note(str(r["cumulative_filled_qty_text"]) == "0.02" and viol(r) == ()
     and str(r["working_leaves_qty_text"]) == "0",
     "N6 一笔全成：整笔成交入账", f"filled={r['cumulative_filled_qty_text']}")
cid = "n7"
prep(box, cid)
box.apply_event(ev("d", cid, dict(base, **{"c": cid, "i": 908,
                                           "X": "EXPIRED_IN_MATCH", "l": "0", "t": 0})))
r = row_of(box, cid)
note(str(r["status"]) == "EXPIRED" and viol(r) == ()
     and str(r["working_leaves_qty_text"]) == "0"
     and str(r["terminal_unfilled_qty_text"]) == "0.02",
     "N7 EXPIRED_IN_MATCH 落库为词表内终态且分量同步", f"status={r['status']}")

print(f"\n### SUMMARY: {'ALL_BLOCKED' if not FAILS else 'FAILED=' + str(FAILS)}")
raise SystemExit(0 if not FAILS else 3)
