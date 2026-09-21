"""W3 独立演练 F14：从恢复包启动 + 交易所已成交但本地未知的订单。

只写临时目录（TEMP 下），不读 .env / 真实 data / 密钥，不联网，不下单。
所有"交易所"交互都是一个进程内假 broker，只记账、不发起任何真实请求。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DRILL_ROOT = Path(__file__).resolve()
# W3-drill 根：.../_scratch/drill_f14.py -> parents[6]
PROJECT_ROOT = DRILL_ROOT.parents[6]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.ops.recovery import (  # noqa: E402
    GATE_CLEARED,
    GATE_CORRUPT,
    GATE_MISSING,
    GATE_NOT_INSTALLED,
    RECONCILE_REQUIRED_BEFORE_ENTRY,
    RecoveryGate,
    create_epoch_bundle,
    drill_epoch_bundle,
    gate_install_marker,
    verify_epoch_bundle,
)
from core.runtime_paths import REQUIRED_STATE_FILES, RuntimePaths  # noqa: E402
from exchange.order_outbox import (  # noqa: E402
    DuplicateOrderSubmission,
    OrderOutbox,
    PendingOrderConflict,
)

PASS = 0
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def note(text: str) -> None:
    print(f"  NOTE  {text}")


class FakeBroker:
    """进程内假交易所：只回答查单，绝不接受下单。

    记录每一次 POST 尝试（`posts`），用于证明"不重复下单"。
    """

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.posts: list[str] = []

    def place(self, client_order_id: str, **payload) -> dict:
        self.posts.append(client_order_id)
        return {"orderId": f"ex-{client_order_id}", "status": "NEW", **payload}

    def query_order(self, symbol: str, client_order_id: str) -> dict:
        rec = self.orders.get(client_order_id)
        if not rec:
            return {}
        return {**rec, "orderId": f"ex-{client_order_id}"}

    def query_algo_order(self, client_order_id: str) -> dict:
        return self.query_order("", client_order_id)


def build_state_root(base: Path, *, environment: str = "demo") -> RuntimePaths:
    """最小可用状态根：真实 AuditDatabase + 关键 JSON。"""
    paths = RuntimePaths.resolve(base, profile=None)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(paths.db_file)
    db.conn.commit()
    db.conn.close()
    payloads = {
        "capital_state.json": {"mode": environment},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": f"{environment}:drill"},
    }
    for name, payload in payloads.items():
        (paths.data_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    return paths


def open_outbox(db_file: Path, environment: str) -> tuple[OrderOutbox, AuditDatabase]:
    db = AuditDatabase(db_file)
    db.environment = environment
    return OrderOutbox(db, environment=environment), db


# --------------------------------------------------------------------------


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="w3-f14-drill-"))
    print(f"drill tmp = {tmp}")
    state_root = tmp / "state_root"
    paths = build_state_root(state_root)

    broker = FakeBroker()
    outbox, db = open_outbox(paths.db_file, "demo")

    # ---- 预埋：交易所侧已成交但本地未知的入场单 -------------------------
    # 场景：本进程 POST 了 ENTRY-BTC-1，交易所已成交，但响应丢失 → 本地落成
    # SUBMITTING（进程随后被杀，从未 ack）。
    planted_client_id = "ENTRY-BTC-1"
    broker.posts.clear()
    broker.place(planted_client_id, symbol="BTCUSDT", side="BUY")
    broker.orders[planted_client_id] = {
        "symbol": "BTCUSDT", "status": "FILLED",
        "executedQty": "0.010", "origQty": "0.010", "avgPrice": "60000",
    }
    outbox.prepare(
        client_order_id=planted_client_id, symbol="BTCUSDT", side="BUY",
        position_side="LONG", order_type="MARKET", purpose="ENTRY",
        qty=0.010, price=None, reduce_only=False, endpoint="/fapi/v1/order",
        request={"symbol": "BTCUSDT", "side": "BUY"}, command_kind="STANDARD",
        qty_text="0.010")
    posts_before = list(broker.posts)
    print(f"  planted: POSTs={posts_before} outbox_rows="
          f"{db.fetch_one('SELECT COUNT(*) c FROM order_outbox')['c']}")

    # ---- 建包 → 校验 → 隔离演练 ----------------------------------------
    bundle = create_epoch_bundle(
        paths, tmp / "bundles", environment="demo",
        account_ref="demo:drill", runtime_instance_id="rt-drill",
        generation_id="gen-drill")
    verified = verify_epoch_bundle(bundle, expected_environment="demo")
    check("恢复包校验通过且携带 epoch",
          verified["epoch"] is not None and verified["environment"] == "demo")
    check("恢复包数据库含预埋的未收口订单",
          db.fetch_one("SELECT COUNT(*) c FROM order_outbox")["c"] == 1)
    db.conn.close()

    restored = tmp / "restored"
    result = drill_epoch_bundle(bundle, restored, execute=True)
    check("演练还原到全新目录并生成回执",
          result["status"] == "restored" and (restored / "restore_receipt.json").is_file())
    receipt = json.loads((restored / "restore_receipt.json").read_text(encoding="utf-8"))
    print(f"  receipt.entry_gate = {receipt['entry_gate']}")

    # 还原后的状态根 = <restored>/data（drill_epoch_bundle 的布局）
    restored_paths = RuntimePaths.resolve(restored, profile=None,
                                          state_root=restored / "data")

    # ---- (A) 核心演练路径是否真的装上了门禁？ ---------------------------
    print("\n[A] core drill_epoch_bundle 还原后：门禁安装情况")
    gate_path = restored_paths.recovery_gate_file
    print(f"  gate_path      = {gate_path}  exists={gate_path.exists()}")
    print(f"  install_marker = exists={gate_install_marker(gate_path).exists()}")
    gate = RecoveryGate.load(gate_path)
    decision = gate.before_entry()
    print(f"  state={decision.state} allowed={decision.allowed} codes={decision.reason_codes}")
    check("core 演练路径还原后并未安装恢复门禁（before_entry 直接放行）",
          decision.allowed and GATE_NOT_INSTALLED in decision.reason_codes,
          f"actual state={decision.state} allowed={decision.allowed}")

    # 还原后的库：进程重启把 SUBMITTING 收敛为 UNKNOWN（绝不重发）
    outbox_r, db_r = open_outbox(restored_paths.db_file, "demo")
    moved = outbox_r.recover_stale_submitting()
    print(f"  recover_stale_submitting -> {moved}")
    st = outbox_r.status()
    print(f"  outbox.status: unknown={st['unknown_orders']} submitting={st['submitting_orders']}")
    check("重启后 SUBMITTING 收敛为 UNKNOWN（不重发）",
          moved == 1 and st["unknown_orders"] == 1)

    # 未收口订单必须继续阻断**新开仓**（与门禁无关的第二道闸）
    from core.entry_readiness import evaluate_entry_readiness

    class _Scope:
        scope_hash = "s"

    base_facts = {
        "service_accepting": True, "control_state": "normal",
        "equity_available": True, "account_risk_allow_new": True,
        "startup_safety_ready": True, "order_submission_ready": True,
        "market_cooldown_active": False, "data_budget_available": True,
        "rate_coordination_available": True, "order_mirror_ok": True,
        "protection_mirror_ok": True, "persistence_available": True,
        "scope_hash": "s", "runtime_instance_id": "r", "environment": "demo",
        **outbox_r.status(),
    }
    readiness = evaluate_entry_readiness(base_facts, scope=_Scope(), environment="demo")
    check("未收口 UNKNOWN 订单进入开仓阻断原因",
          not readiness.new_entry_allowed
          and "ORDER_OUTBOX_UNKNOWN" in readiness.blocking_reasons,
          str(readiness.blocking_reasons))

    # 交易所侧已成交 → 增量回拉收口（只查询，不 POST）
    posts_before_reconcile = list(broker.posts)
    resolved = outbox_r.reconcile_unknown(broker)
    st2 = outbox_r.status()
    print(f"  reconcile_unknown -> {resolved}; status_counts={st2['status_counts']}")
    check("对账把交易所已成交单回拉为 FILLED",
          resolved == 1 and st2["status_counts"].get("FILLED") == 1)
    check("回拉过程**没有**再次 POST（不重复下单）",
          broker.posts == posts_before_reconcile,
          f"posts={broker.posts}")

    # 对账后新开仓才被允许（就绪链）
    readiness2 = evaluate_entry_readiness(
        {**base_facts, **outbox_r.status()}, scope=_Scope(), environment="demo")
    check("回拉收口后开仓阻断消失",
          readiness2.new_entry_allowed, str(readiness2.blocking_reasons))

    # 同一 clientOrderId 重复提交被硬拒
    dup = ""
    try:
        outbox_r.prepare(
            client_order_id=planted_client_id, symbol="BTCUSDT", side="BUY",
            position_side="LONG", order_type="MARKET", purpose="ENTRY",
            qty=0.010, price=None, reduce_only=False, endpoint="/fapi/v1/order",
            request={}, command_kind="STANDARD", qty_text="0.010")
    except DuplicateOrderSubmission as exc:
        dup = f"DuplicateOrderSubmission: {exc}"
    check("同一 clientOrderId 二次提交被拒（幂等键）", bool(dup), dup or "(no raise)")

    # 再来一笔 SUBMITTING 时，新 ENTRY 被 PendingOrderConflict 拦住
    outbox_r.prepare(
        client_order_id="ENTRY-BTC-2", symbol="ETHUSDT", side="BUY",
        position_side="LONG", order_type="MARKET", purpose="ENTRY",
        qty=1.0, price=None, reduce_only=False, endpoint="/fapi/v1/order",
        request={}, command_kind="STANDARD", qty_text="1.0")
    conflict = ""
    try:
        outbox_r.prepare(
            client_order_id="ENTRY-BTC-3", symbol="SOLUSDT", side="BUY",
            position_side="LONG", order_type="MARKET", purpose="ENTRY",
            qty=1.0, price=None, reduce_only=False, endpoint="/fapi/v1/order",
            request={}, command_kind="STANDARD", qty_text="1.0")
    except PendingOrderConflict as exc:
        conflict = f"PendingOrderConflict: {exc}"
    check("存在未决 ENTRY 时新 ENTRY 被拒（不会并发双开）", bool(conflict),
          conflict or "(no raise)")

    # ---- (B) 生产恢复脚本路径：门禁是否装上 ------------------------------
    print("\n[B] scripts/restore_recovery_bundle.py 生产恢复路径")
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    import importlib
    rrb = importlib.import_module("restore_recovery_bundle")
    crb = importlib.import_module("create_recovery_bundle")

    # B0：core 建的包（schema 3）能否被生产恢复脚本读取？
    b0 = ""
    try:
        rrb.restore_to_new_directory(bundle, tmp / "prod_from_core", execute=True,
                                     expected_environment="demo")
    except Exception as exc:  # noqa: BLE001
        b0 = f"{type(exc).__name__}: {exc}"
    check("生产恢复脚本**读不了** core 建的 schema-3 恢复包（两套实现不互通）",
          bool(b0), b0 or "(no raise)")
    print(f"  B0 raise = {b0}")

    # B1：用生产建包脚本产 schema 2 包，再走生产恢复
    prod_root = tmp / "prod_root"
    build_state_root(prod_root)
    bundle2 = crb.create_recovery_bundle(prod_root, tmp / "bundles2")
    prod = tmp / "prod_restored"
    rrb.restore_to_new_directory(bundle2, prod, execute=True,
                                 expected_environment="demo")
    prod_gate = Path(prod) / "data" / "ops" / "recovery_gate.json"
    print(f"  prod gate = {prod_gate}  exists={prod_gate.exists()}")
    pdec = RecoveryGate.load(prod_gate).before_entry()
    print(f"  prod state={pdec.state} allowed={pdec.allowed} codes={pdec.reason_codes}")
    check("生产恢复脚本在目标状态根安装了 RECONCILE_REQUIRED 门禁",
          (not pdec.allowed)
          and RECONCILE_REQUIRED_BEFORE_ENTRY in pdec.reason_codes)
    check("两条恢复路径的门禁行为不一致（core 演练 = 放行 / 生产脚本 = 阻断）",
          decision.allowed != pdec.allowed)

    # ---- (C) 绕过尝试 ---------------------------------------------------
    print("\n[C] 门禁绕过尝试")
    gp = tmp / "gate_bypass" / "ops" / "recovery_gate.json"
    RecoveryGate.load(gp).require_reconcile(epoch_id="epoch-bypass",
                                            account_ref="demo:drill",
                                            runtime_instance_id="rt-drill")
    now_ms = int(time.time() * 1000)

    # C1: 直接手写 CLEARED（伪造证据绕过 complete_reconcile）
    gp.write_text(json.dumps({"schema": 1, "state": "CLEARED",
                              "reconciled_at_ms": now_ms, "epoch_id": "epoch-bypass",
                              "account_ref": "demo:drill",
                              "runtime_instance_id": "rt-drill",
                              "evidence": {}}), encoding="utf-8")
    forged = RecoveryGate.load(gp).before_entry(now_ms=now_ms)
    print(f"  C1 forged CLEARED -> state={forged.state} allowed={forged.allowed} "
          f"codes={forged.reason_codes}")
    check("C1 手写 CLEARED（evidence 空）即放行 —— 绕过成功", forged.allowed,
          f"allowed={forged.allowed}")

    # C2: 删除门禁文件 + 安装戳（两条都删）
    RecoveryGate.load(gp).require_reconcile(epoch_id="epoch-bypass")
    gp.unlink()
    marker = gate_install_marker(gp)
    if marker.exists():
        marker.unlink()
    both_gone = RecoveryGate.load(gp).before_entry(now_ms=now_ms)
    print(f"  C2 文件+安装戳都删 -> state={both_gone.state} allowed={both_gone.allowed}")
    check("C2 删除门禁+安装戳即回到 NOT_INSTALLED 放行 —— 绕过成功",
          both_gone.allowed and GATE_NOT_INSTALLED in both_gone.reason_codes,
          f"allowed={both_gone.allowed} state={both_gone.state}")

    # C3: 只删文件（安装戳仍在）→ 必须拒
    RecoveryGate.load(gp).require_reconcile(epoch_id="epoch-bypass")
    gp.unlink()
    only_marker_gone = RecoveryGate.load(gp).before_entry(now_ms=now_ms)
    check("C3 只删门禁文件（安装戳在）→ GATE_MISSING 拒绝",
          (not only_marker_gone.allowed) and GATE_MISSING in only_marker_gone.reason_codes)

    # C4: 半截 JSON → 拒
    gp.write_text("{corrupt", encoding="utf-8")
    corrupt = RecoveryGate.load(gp).before_entry(now_ms=now_ms)
    check("C4 半截 JSON → GATE_CORRUPT 拒绝",
          (not corrupt.allowed) and GATE_CORRUPT in corrupt.reason_codes)

    # C5: 未来时间戳的 CLEARED（用 complete_reconcile 的 time 偏移）
    RecoveryGate.load(gp).require_reconcile(epoch_id="epoch-bypass")
    fut = RecoveryGate.load(gp).complete_reconcile(
        {"observed_at_ms": now_ms + 10 ** 7, "positions_verified": True,
         "open_orders_verified": True}, now_ms=now_ms)
    check("C5 未来时间戳证据被拒", not fut.allowed)

    db_r.conn.close()
    print(f"\nF14 DRILL 结果: {PASS} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
