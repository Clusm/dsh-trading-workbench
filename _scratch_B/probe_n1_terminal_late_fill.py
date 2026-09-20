"""N1 探针：CANCELED/EXPIRED 终态行收到迟到成交（走私有流真实路径）。

只替换交易所 I/O 边界之外的一切都走生产代码：
  OrderOutbox.prepare -> apply_event(ORDER_TRADE_UPDATE) -> ...
不注入任何内部字段，不直接调 apply_fill_revision。

用法：python _scratch_B/probe_n1_terminal_late_fill.py <tree_dir> [label]
"""
from __future__ import annotations

import sys
import tempfile
from decimal import Decimal
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
LABEL = sys.argv[2] if len(sys.argv) > 2 else TREE.name
sys.path.insert(0, str(TREE / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.contracts import OrderQuantity, QuantityError  # noqa: E402
from exchange.event_models import ExchangeEvent  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402
import exchange.order_outbox as oo  # noqa: E402

print(f"### TREE = {oo.__file__}")
print(f"### LABEL = {LABEL}")


def ev(client_id, symbol, payload):
    return ExchangeEvent(
        event_key="k", event_type="ORDER_TRADE_UPDATE", event_time_ms=None,
        transaction_time_ms=None, source="user_stream", symbol=symbol,
        client_order_id=client_id, exchange_order_id=str(payload.get("i") or ""),
        trade_id=str(payload.get("t") or ""), payload={"o": payload},
        supported=True)


def row_of(outbox, cid):
    return dict(outbox.db.fetch_one(
        "SELECT * FROM order_outbox WHERE client_order_id=?", (cid,)))


def scenario(terminal_status, label):
    tmpdir = Path(tempfile.mkdtemp(prefix="mta-n1-"))
    db_path = tmpdir / "n1.sqlite"
    cid = f"n1-{terminal_status.lower()}"
    symbol = "N1USDT"
    outbox = OrderOutbox(AuditDatabase(db_path), environment="demo")
    outbox.prepare(
        client_order_id=cid, symbol=symbol, side="BUY", position_side="LONG",
        order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
        reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": symbol, "newClientOrderId": cid, "quantity": "0.02"},
        qty_text="0.02")

    base = {"c": cid, "i": 901, "q": "0.02", "s": symbol, "ap": "100.00"}
    # 1) 首笔部分成交
    outbox.apply_event(ev(cid, symbol, dict(
        base, **{"X": "PARTIALLY_FILLED", "z": "0.0047", "l": "0.0047", "t": 55501})))
    r1 = row_of(outbox, cid)
    # 2) 转终态
    outbox.apply_event(ev(cid, symbol, dict(
        base, **{"X": terminal_status, "z": "0.0047", "l": "0", "t": 0})))
    r2 = row_of(outbox, cid)
    # 3) 迟到成交帧（交易所撤单后又回报一笔成交）
    outbox.apply_event(ev(cid, symbol, dict(
        base, **{"X": terminal_status, "z": "0.0057", "l": "0.001", "t": 55502})))
    r3 = row_of(outbox, cid)
    last_error = outbox.last_error

    print(f"\n--- [{label}] {terminal_status} ---")
    for tag, r in (("after 首笔部分成交", r1), ("after 转终态", r2),
                   ("after 迟到成交(l=0.001,t=55502)", r3)):
        print(f"  {tag:34s} status={r['status']:16s} "
              f"accepted={r['accepted_qty_text']} filled={r['cumulative_filled_qty_text']} "
              f"working={r['working_leaves_qty_text']} terminal={r['terminal_unfilled_qty_text']}")

    # 契约自检：该行今天还能否被 OrderQuantity.checked 接受？
    problem = None
    try:
        OrderQuantity.checked(
            order_id=str(r3["exchange_order_id"] or cid),
            accepted_qty=Decimal(str(r3["accepted_qty_text"])),
            cumulative_filled_qty=Decimal(str(r3["cumulative_filled_qty_text"])),
            working_leaves_qty=Decimal(str(r3["working_leaves_qty_text"])),
            terminal_unfilled_qty=Decimal(str(r3["terminal_unfilled_qty_text"])),
            status=str(r3["status"]), revision=int(r3["quantity_revision"] or 0))
    except QuantityError as exc:
        problem = str(exc)
    except Exception as exc:  # noqa: BLE001
        problem = f"{type(exc).__name__}: {exc}"

    acc = Decimal(str(r3["accepted_qty_text"]))
    fil = Decimal(str(r3["cumulative_filled_qty_text"]))
    wrk = Decimal(str(r3["working_leaves_qty_text"]))
    ter = Decimal(str(r3["terminal_unfilled_qty_text"]))
    conserved = acc == fil + wrk + ter
    print(f"  last_error(apply_event 吞掉了吗) = {last_error!r}")
    print(f"  K04 终态不变量 working==0 ? {wrk == 0}   (working={wrk})")
    print(f"  余量进 terminal 期望 {acc - fil} 实际 {ter} -> {'OK' if ter == acc - fil else 'WRONG'}")
    print(f"  守恒 accepted == filled+working+terminal ? {conserved}")
    print(f"  OrderQuantity.checked(该行) -> "
          f"{'ACCEPTED' if problem is None else 'REJECTED: ' + problem}")
    expected_ok = (str(r3["status"]) == terminal_status and wrk == 0
                   and ter == acc - fil and conserved and problem is None)
    print(f"  VERDICT = {'CORRECT_TERMINAL' if expected_ok else 'WRONG_TERMINAL'}")
    outbox.db.close()
    return expected_ok


ok = True
for ts in ("CANCELED", "EXPIRED"):
    ok = scenario(ts, LABEL) and ok

# 未知/未映射状态的兜底语义：EXPIRED_IN_MATCH 应归一到 EXPIRED（终态保留）
tmpdir = Path(tempfile.mkdtemp(prefix="mta-n1-alias-"))
db_path = tmpdir / "alias.sqlite"
cid, symbol = "n1-alias", "N1USDT"
ob = OrderOutbox(AuditDatabase(db_path), environment="demo")
ob.prepare(client_order_id=cid, symbol=symbol, side="BUY", position_side="LONG",
           order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
           reduce_only=False, endpoint="/fapi/v1/order",
           request={"symbol": symbol, "newClientOrderId": cid, "quantity": "0.02"},
           qty_text="0.02")
base = {"c": cid, "i": 901, "q": "0.02", "s": symbol, "ap": "100.00"}
ob.apply_event(ev(cid, symbol, dict(
    base, **{"X": "PARTIALLY_FILLED", "z": "0.0047", "l": "0.0047", "t": 55501})))
ob.apply_event(ev(cid, symbol, dict(
    base, **{"X": "EXPIRED_IN_MATCH", "z": "0.0047", "l": "0", "t": 0})))
ob.apply_event(ev(cid, symbol, dict(
    base, **{"X": "EXPIRED_IN_MATCH", "z": "0.0057", "l": "0.001", "t": 55502})))
ra = row_of(ob, cid)
print("\n--- [alias] EXPIRED_IN_MATCH 迟到成交 ---")
print(f"  status={ra['status']} filled={ra['cumulative_filled_qty_text']} "
      f"working={ra['working_leaves_qty_text']} terminal={ra['terminal_unfilled_qty_text']}")
alias_ok = (str(ra["working_leaves_qty_text"]) == "0"
            and str(ra["terminal_unfilled_qty_text"]) == "0.0143")
print("  分量口径正确（working=0, 余量进 terminal）? "
      f"{alias_ok}（status 列按设计原样保留交易所别名 EXPIRED_IN_MATCH）")
ob.db.close()

# 兜底语义矩阵（单元级）：未知/未映射状态必须走 fail-safe 分量推导，不外泄原值
print("\n--- 兜底语义矩阵 _component_status(accepted=0.02, filled=0.0047, stored_status=x) ---")
for x in ("SUBMITTING", "ACKNOWLEDGED", "UNKNOWN", "NEW", "PARTIALLY_FILLED",
          "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH",
          "SOMETHING_NEW", "PARTIALLY_FILLEDX", "", "CANCELED "):
    got = oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0047"),
                               stored_status=x)
    print(f"  {x!r:22s} -> {got}")
fallback_ok = (
    oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.0047"),
                         stored_status="SOMETHING_NEW") == "PARTIALLY_FILLED"
    and oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0"),
                             stored_status="SOMETHING_NEW") == "NEW"
    and oo._component_status(accepted=Decimal("0.02"), filled=Decimal("0.02"),
                             stored_status="SOMETHING_NEW") == "FILLED")
print(f"  未知状态不外泄、按分量 fail-safe 推导 ? {fallback_ok}")

allok = ok and alias_ok and fallback_ok
print(f"\n### SUMMARY {LABEL}: {'ALL_CORRECT' if allok else 'N1_PRESENT'}")
raise SystemExit(0 if allok else 3)
