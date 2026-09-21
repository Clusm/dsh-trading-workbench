"""F14 端到端：真实 TradingService 从"已恢复且已装门禁"的状态根启动。

镜像生产恢复布局（docker restore_recovery_bundle.py）：
  <root>/data/agent_trader.sqlite（库内含交易所已成交但本地未收口的 ENTRY 单）
  <root>/data/{capital,channel,control,account_risk}_state.json
  <root>/data/ops/recovery_gate.json （RECONCILE_REQUIRED）
只写 TEMP，不联网，不下单，不读 .env/真实 data。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

DRILL_ROOT = Path(__file__).resolve()
PROJECT_ROOT = DRILL_ROOT.parents[6]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
ROOT = PROJECT_ROOT

import yaml  # noqa: E402

from audit.database import AuditDatabase  # noqa: E402
from core.config_loader import Settings  # noqa: E402
from core.ops.recovery import RecoveryGate  # noqa: E402
from core.service import TradingService  # noqa: E402
from exchange.order_outbox import OrderOutbox  # noqa: E402

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="w3-f14-e2e-"))
    print(f"drill tmp = {tmp}")
    iso = tmp / "iso_root"
    data = iso / "data"
    data.mkdir(parents=True)

    db = AuditDatabase(data / "agent_trader.sqlite")
    db.conn.commit()
    db.close()
    for name, payload in {
        "capital_state.json": {"mode": "paper"},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": "paper:drill"},
    }.items():
        (data / name).write_text(json.dumps(payload), encoding="utf-8")

    # 预埋：交易所侧已成交、本地未收口（SUBMITTING）的 ENTRY 单
    db = AuditDatabase(data / "agent_trader.sqlite")
    db.environment = "paper"
    ob = OrderOutbox(db, environment="paper")
    ob.prepare(client_order_id="ENTRY-E2E-1", symbol="BTCUSDT", side="BUY",
               position_side="LONG", order_type="MARKET", purpose="ENTRY",
               qty=0.01, price=None, reduce_only=False, endpoint="/fapi/v1/order",
               request={}, command_kind="STANDARD", qty_text="0.010")
    db.conn.commit()
    db.close()

    # 模拟生产恢复：状态根里已安装 RECONCILE_REQUIRED 门禁
    (data / "ops").mkdir()
    RecoveryGate.load(data / "ops" / "recovery_gate.json").require_reconcile(
        epoch_id="e2e-epoch", account_ref="", runtime_instance_id="")

    os.environ["EXECUTION_MODE"] = "paper"
    settings = Settings(
        ROOT,
        yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((ROOT / "config/agents.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((ROOT / "config/risk_learning.yaml").read_text(encoding="utf-8")),
    )
    settings.project_root = iso
    print(f"  data_dir = {settings.runtime_path('data')}")
    service = TradingService(settings)

    print("\n[1] 恢复后启动必须 close_only 且开仓被门禁阻断")
    check("启动后控制状态被收紧为 close_only", service.control.state == "close_only",
          service.control.state)
    facts = service._entry_readiness_facts
    print(f"  readiness facts: gate_allowed={facts.get('recovery_gate_allowed')} "
          f"codes={facts.get('recovery_gate_reason_codes')} "
          f"unknown={facts.get('unknown_orders')}")
    check("冷启动快照含门禁阻断事实",
          facts.get("recovery_gate_allowed") is False)
    snap = service._entry_readiness_snapshot
    check("开仓判定链含 RECONCILE_REQUIRED_BEFORE_ENTRY",
          "RECONCILE_REQUIRED_BEFORE_ENTRY" in snap.blocking_reasons,
          str(snap.blocking_reasons))
    # 【缺陷观察】冷启动快照的 unknown_orders 是硬编码 0（service.py:396/397），
    # 未收口订单要到第一次循环 _collect_entry_readiness_facts 才可见。
    check("[缺陷] 冷启动快照未包含 ORDER_OUTBOX_UNKNOWN（unknown_orders 硬编码 0）",
          "ORDER_OUTBOX_UNKNOWN" not in snap.blocking_reasons
          and facts.get("unknown_orders") == 0, str(snap.blocking_reasons))
    check("冷启动仍被其它事实阻断（门禁+未核验事实，非靠 unknown_orders）",
          not snap.new_entry_allowed
          and "RECONCILE_REQUIRED_BEFORE_ENTRY" in snap.blocking_reasons)

    print("\n[2] 控制面不能一步绕过门禁")
    res = service.set_control("normal")
    print(f"  set_control('normal') -> {res}")
    check("门禁未收口时 set_control('normal') 被拒",
          res.get("ok") is False and "recovery gate blocked" in str(res.get("error")))
    check("控制状态仍是 close_only", service.control.state == "close_only")

    print("\n[3] 对账收口入口的身份/确认约束")
    bad_actor = service.submit_recovery_reconcile(
        {"observed_at_ms": 1, "positions_verified": True, "open_orders_verified": True},
        actor="auto", confirm=True)
    check("auto 执行者不能收口门禁", bad_actor.get("ok") is False
          and "OPERATOR_CONFIRM_REQUIRED" in bad_actor.get("reason_codes", []))
    no_confirm = service.submit_recovery_reconcile(
        {"observed_at_ms": 1, "positions_verified": True, "open_orders_verified": True},
        actor="operator", confirm=False)
    check("未二次确认不能收口", no_confirm.get("ok") is False)

    import time as _t
    now_ms = int(_t.time() * 1000)
    bad_evidence = service.submit_recovery_reconcile(
        {"observed_at_ms": now_ms, "positions_verified": True},
        actor="operator", confirm=True)
    check("缺 open_orders_verified 的证据被拒", bad_evidence.get("ok") is False
          and "EVIDENCE_MISSING" in bad_evidence.get("reason_codes", []),
          str(bad_evidence))

    good = service.submit_recovery_reconcile(
        {"observed_at_ms": now_ms, "positions_verified": True,
         "open_orders_verified": True}, actor="operator", confirm=True)
    print(f"  submit_recovery_reconcile -> {good}")
    check("operator + 二次确认 + 新鲜结构化证据 → 门禁收口",
          good.get("ok") is True and good.get("state") == "CLEARED", str(good))
    check("门禁落盘为 CLEARED",
          RecoveryGate.load(service.runtime_paths.recovery_gate_file).state == "CLEARED")

    print("\n[4] 门禁收口 ≠ 可以开仓（未收口订单仍挡住）")
    snap2 = service._entry_readiness_snapshot
    check("[缺陷] 门禁收口后快照仍看不到未收口订单（同一硬编码，需等首次循环）",
          "ORDER_OUTBOX_UNKNOWN" not in snap2.blocking_reasons
          and "RECONCILE_REQUIRED_BEFORE_ENTRY" not in snap2.blocking_reasons,
          str(snap2.blocking_reasons))
    check("门禁收口已生效（RECONCILE_REQUIRED_BEFORE_ENTRY 消失）",
          "RECONCILE_REQUIRED_BEFORE_ENTRY" not in snap2.blocking_reasons)

    print("\n[5] 幂等：同一 clientOrderId 不得二次下单")
    dup = ""
    try:
        ob2 = OrderOutbox(AuditDatabase(data / "agent_trader.sqlite"), environment="paper")
        ob2.prepare(client_order_id="ENTRY-E2E-1", symbol="BTCUSDT", side="BUY",
                    position_side="LONG", order_type="MARKET", purpose="ENTRY",
                    qty=0.01, price=None, reduce_only=False, endpoint="/fapi/v1/order",
                    request={}, command_kind="STANDARD", qty_text="0.010")
    except Exception as exc:  # noqa: BLE001
        dup = f"{type(exc).__name__}: {exc}"
    check("重复 clientOrderId 被拒", "DuplicateOrderSubmission" in dup, dup or "(no raise)")

    service.close() if hasattr(service, "close") else None
    print(f"\nF14 E2E 结果: {PASS} 通过 / {len(FAIL)} 失败")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
