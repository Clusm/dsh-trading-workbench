"""MUST FIX 4 修复前后对照：V1 兜底使第一笔成交必抛 STATUS_FILL_MISMATCH。"""
from __future__ import annotations
import sys, tempfile
from pathlib import Path
TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(TREE / "src"))
from audit.database import AuditDatabase  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="mta-p1p2-"))
db = AuditDatabase(tmp / "p.sqlite")
ob = OrderOutbox(db, environment="demo")
cid = "probe-fill-1"
ob.prepare(client_order_id=cid, symbol="ZZZUSDT", side="BUY", position_side="LONG",
           order_type="LIMIT", purpose="ENTRY", qty=0.02, price=100.0,
           reduce_only=False, endpoint="/fapi/v1/order",
           request={"symbol": "ZZZUSDT", "newClientOrderId": cid, "quantity": "0.02"},
           qty_text="0.02")
print(f"=== {TREE.name} : 落库后 status={db.fetch_one('SELECT status FROM order_outbox WHERE client_order_id=?',(cid,))['status']}")
try:
    out = ob.apply_fill_revision(cid, fill_qty="0.0047", fill_id="55501")
    row = db.fetch_one("SELECT * FROM order_outbox WHERE client_order_id=?", (cid,))
    print(f"apply_fill_revision(第一笔 0.0047) -> OK {out}")
    print(f"  cumulative_filled={row['cumulative_filled_qty_text']} "
          f"working={row['working_leaves_qty_text']} status={row['status']} "
          f"applied={row['applied_fill_ids_json']}")
except Exception as exc:
    print(f"apply_fill_revision(第一笔 0.0047) -> RAISED {type(exc).__name__}: {exc}")
db.close()
