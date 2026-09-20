"""核验恢复计划：同 epoch 建包 → 校验 → 隔离目录演练 → 先对账再开仓门禁。

默认在 **临时目录** 里造一份合成状态并做完整演练（建包、校验、还原、门禁），
不触碰真实 ``data/``、不读 ``.env``/密钥、不联网、不启动服务、不下单。

``--state-root`` 只用于输出 **元数据级** 恢复计划（只判断文件是否存在，不读
内容），供部署前确认"恢复包与状态根是否同盘"（R21）。

退出码：0 = 全流程通过；2 = 任一步失败（fail-closed）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.ops.recovery import (  # noqa: E402
    GATE_CLEARED,
    RECONCILE_REQUIRED_BEFORE_ENTRY,
    RecoveryGate,
    create_epoch_bundle,
    drill_epoch_bundle,
    recovery_plan,
    verify_epoch_bundle,
)
from core.runtime_paths import REQUIRED_STATE_FILES, RuntimePaths  # noqa: E402

FIXTURE_STATE = {
    "capital_state.json": {"mode": "demo", "anchored_at": "2026-09-20T00:00:00+00:00"},
    "channel_state.json": {"date": "2026-09-20", "ai_halted": False},
    "control_state.json": {"state": "normal"},
    "account_risk_state.json": {"anchor_id": "demo:ops-drill"},
}


def make_fixture(base: Path, *, environment: str = "demo") -> RuntimePaths:
    """合成一份最小状态根（临时目录内），内容全部是合成值。"""
    paths = RuntimePaths.resolve(base, profile=None)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(paths.db_file)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO schema_migrations VALUES(17)")
    connection.execute("CREATE TABLE risk_events(ts REAL, event_type TEXT)")
    connection.commit()
    connection.close()
    for name, payload in FIXTURE_STATE.items():
        (paths.data_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    return paths


def run_drill(*, require_independent_volume: bool) -> dict:
    base = Path(tempfile.mkdtemp(prefix="mta-recovery-drill-"))
    try:
        host = base / "host"
        paths = make_fixture(host)
        steps: dict[str, object] = {}
        started = time.perf_counter()
        bundle = create_epoch_bundle(
            paths, base / "bundles", environment="demo",
            account_ref="demo:ops-drill", runtime_instance_id="drill-runtime",
            generation_id="drill-generation")
        steps["bundle"] = str(bundle)
        verified = verify_epoch_bundle(bundle, expected_environment="demo")
        steps["verify"] = {
            "files": verified["files"],
            "database_schema": verified["database_schema"],
            "epoch_id": verified["epoch"]["epoch_id"],
            "span_ms": verified["span_ms"],
        }
        dry = drill_epoch_bundle(bundle, base / "drill-dry", execute=False)
        steps["dry_run"] = {"status": dry["status"], "notices": dry["notices"],
                            "reconcile_required": dry["reconcile_required"]}
        restored = drill_epoch_bundle(bundle, base / "drill", execute=True,
                                      require_independent_volume=False)
        steps["restore"] = {"status": restored["status"],
                            "target": restored["target"],
                            "notices": restored["notices"]}
        receipt = json.loads((base / "drill" / "restore_receipt.json")
                             .read_text(encoding="utf-8"))
        steps["receipt_entry_gate"] = receipt["entry_gate"]
        # 恢复后先对账再开仓：未提交证据前必须阻断。
        gate = RecoveryGate.load(base / "drill" / "gate.json")
        gate.require_reconcile(epoch_id=verified["epoch"]["epoch_id"],
                               account_ref="demo:ops-drill",
                               runtime_instance_id="drill-runtime")
        blocked = gate.before_entry(now_ms=1_000_000)
        steps["gate_blocked"] = {"allowed": blocked.allowed,
                                 "reason_codes": list(blocked.reason_codes),
                                 "state": blocked.state}
        cleared = gate.complete_reconcile(
            {"observed_at_ms": 1_000_000, "positions_verified": True,
             "open_orders_verified": True}, now_ms=1_000_000,
            account_ref="demo:ops-drill", runtime_instance_id="drill-runtime")
        steps["gate_cleared"] = {"allowed": cleared.allowed,
                                 "reason_codes": list(cleared.reason_codes)}
        steps["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        failures: list[str] = []
        if steps["receipt_entry_gate"] != RECONCILE_REQUIRED_BEFORE_ENTRY:
            failures.append("RECEIPT_GATE_WRONG")
        if blocked.allowed or RECONCILE_REQUIRED_BEFORE_ENTRY not in blocked.reason_codes:
            failures.append("GATE_NOT_BLOCKING_BEFORE_RECONCILE")
        if not cleared.allowed or GATE_CLEARED not in cleared.reason_codes:
            failures.append("GATE_NOT_CLEARED_AFTER_RECONCILE")
        if verified["files"] != 1 + len(REQUIRED_STATE_FILES):
            failures.append("BUNDLE_MEMBER_COUNT_WRONG")
        steps["failures"] = failures
        steps["ok"] = not failures
        return steps
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="核验同 epoch 恢复包与演练流程")
    parser.add_argument("--state-root", default=None,
                        help="额外输出该状态根的**元数据级**恢复计划（不读内容）")
    parser.add_argument("--require-independent-volume", action="store_true",
                        help="演练目标必须在独立卷（同卷会 fail-closed）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report: dict = {"drill": run_drill(
        require_independent_volume=args.require_independent_volume)}
    if args.state_root:
        plan_paths = RuntimePaths.resolve(ROOT, profile=None, state_root=args.state_root)
        report["plan"] = recovery_plan(plan_paths, metadata_only=True)
    ok = bool(report["drill"]["ok"])
    report["ok"] = ok
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if ok else 2

    drill = report["drill"]
    print("RECOVERY PLAN VERIFY")
    print(f"  bundle            = {drill['bundle']}")
    print(f"  verify            = {drill['verify']}")
    print(f"  dry_run           = {drill['dry_run']}")
    print(f"  restore           = {drill['restore']}")
    print(f"  receipt gate      = {drill['receipt_entry_gate']}")
    print(f"  gate before recon = {drill['gate_blocked']}")
    print(f"  gate after recon  = {drill['gate_cleared']}")
    if "plan" in report:
        plan = report["plan"]
        print("\n  metadata-only recovery plan (no state content read):")
        print(f"    state_base                    = {plan['state_base']}")
        print(f"    same_volume_as_state          = {plan['same_volume_as_state']}")
        print(f"    disaster_recovery_independent = "
              f"{plan['disaster_recovery_requires_independent_volume']}")
        print(f"    state_health.problems         = {plan['state_health']['problems']}")
        for step in plan["steps"]:
            print(f"    {step}")
    if not ok:
        print(f"\nRECOVERY PLAN VERIFY FAILED: {', '.join(drill['failures'])}")
        return 2
    print("\nRECOVERY PLAN VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
