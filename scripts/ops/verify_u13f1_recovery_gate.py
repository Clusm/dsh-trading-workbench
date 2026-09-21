"""U13-FU1 验收：恢复门禁四条 P1 缺陷的 fail-closed 行为（全程离线）。

覆盖（每条都对应一次独立恢复演练发现的 P1 缺陷）：

- **P1-1**：core 演练路径（``drill_epoch_bundle``）还原后**真的装上**恢复门禁，
  还原根上的 ``before_entry()`` 阻断；门禁落点由回执给出，不是测试自己另建一个。
- **P1-2**：恢复包携带 profile；demo / live 两种档位下状态 JSON **与**
  ``ops/recovery_gate.json`` 都落在 profile 感知的状态根（``RuntimePaths
  .state_root``）之下，两条路径都阻断（不是"门禁找不到 → 放行"）。
- **P1-3**：``CLEARED`` 读取时用账本**真实事实**复验；手写 JSON 放行与
  "账本里还挂着未收口 ENTRY 单"都走不通。
- **P1-4**：未声明状态根这一事实可被 ``validate()`` 检出（``STATE_ROOT_MISSING``
  可达），``state_base_explicit`` 有真实消费者（``state_root_drift``）。

**两态可跑**：不假定本补丁是否已应用——缺符号/缺行为时逐条判失败并给出原因码，
退出码非零。只写系统临时目录：不读 ``.env``/真实 ``data``/密钥、不联网、
不下单、不动风险档、不启动服务。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

PASS = 0
FAIL: list[str] = []
NOTES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")
    return ok


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  NOTE  {text}")


# --------------------------------------------------------------------------
# 被测符号：缺失即"补丁未应用"，逐条判失败（不 ImportError 崩掉整个脚本）。
# --------------------------------------------------------------------------
_MISSING: list[str] = []

try:
    from core.ops.recovery import (  # noqa: E402
        GATE_CLEARED,
        RECONCILE_REQUIRED_BEFORE_ENTRY,
        RecoveryGate,
        create_epoch_bundle,
        drill_epoch_bundle,
        facts_digest,
        gate_install_marker,
        ledger_reconcile_facts,
        restored_gate_path,
        restored_state_root,
    )
except ImportError as exc:  # pragma: no cover - 仅在补丁未应用时触发
    print(f"FATAL 无法导入 core.ops.recovery 的验收符号: {exc}")
    raise SystemExit(2)

try:
    from core.runtime_paths import (  # noqa: E402
        REQUIRED_STATE_FILES,
        STATE_ROOT_MISSING,
        RuntimePaths,
    )
except ImportError as exc:  # pragma: no cover
    print(f"FATAL 无法导入 core.runtime_paths: {exc}")
    raise SystemExit(2)

try:
    from audit.database import AuditDatabase  # noqa: E402
    from exchange.order_outbox import OrderOutbox  # noqa: E402
except ImportError as exc:  # pragma: no cover
    print(f"FATAL 无法导入账本/outbox: {exc}")
    raise SystemExit(2)


def _seed_state_root(paths: RuntimePaths, *, mode: str, entry_client_id: str | None) -> None:
    """最小可用状态根：真实 AuditDatabase + 四个必需 JSON（合成值）。"""
    state_root = paths.data_dir
    state_root.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(paths.db_file)
    try:
        db.environment = mode
        if entry_client_id:
            outbox = OrderOutbox(db, environment=mode)
            outbox.prepare(
                client_order_id=entry_client_id, symbol="BTCUSDT", side="BUY",
                position_side="LONG", order_type="MARKET", purpose="ENTRY",
                qty=0.010, price=None, reduce_only=False, endpoint="/fapi/v1/order",
                request={"symbol": "BTCUSDT", "side": "BUY"}, command_kind="STANDARD",
                qty_text="0.010")
        db.conn.commit()
    finally:
        db.close()
    for name, payload in {
        "capital_state.json": {"mode": mode},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": f"{mode}:u13f1"},
    }.items():
        (state_root / name).write_text(json.dumps(payload), encoding="utf-8")


def _build_bundle(tmp: Path, *, profile: str | None, mode: str,
                  entry_client_id: str | None) -> tuple[RuntimePaths, Path]:
    host = tmp / "host"
    host.mkdir(parents=True, exist_ok=True)
    paths = RuntimePaths.resolve(host, profile=profile, state_root=host / "state_base")
    _seed_state_root(paths, mode=mode, entry_client_id=entry_client_id)
    bundle = create_epoch_bundle(paths, tmp / "bundles", environment=mode,
                                 account_ref=f"{mode}:u13f1",
                                 runtime_instance_id="rt-u13f1",
                                 generation_id="gen-u13f1")
    return paths, bundle


def _forged_gate(gate_file: Path, *, evidence: dict | None) -> None:
    """手写一份 CLEARED 门禁（操作者自证路径），安装戳保留（门禁"装过"）。"""
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text(json.dumps({
        "schema": 1, "state": "CLEARED",
        "reconciled_at_ms": int(time.time() * 1000),
        "epoch_id": "forged", "account_ref": "", "runtime_instance_id": "",
        "evidence": evidence if evidence is not None else {},
    }), encoding="utf-8")


def check_p1_1_and_p1_2(tmp: Path) -> None:
    print("\n[P1-1/P1-2] 还原装门禁 + profile 感知布局（demo / live 两态）")
    for profile, mode, expected_rel in (
        (None, "demo", "data"),
        ("live", "live", "data/live"),
    ):
        label = f"profile={profile or '(demo)'}"
        sub = tmp / f"restore_{mode}"
        _, bundle = _build_bundle(sub, profile=profile, mode=mode,
                                 entry_client_id="ENTRY-BTC-1")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        check(f"{label} 恢复包携带 epoch.profile",
              str(manifest["epoch"]["profile"] or "") == (profile or ""),
              f"epoch.profile={manifest['epoch']['profile']!r}")

        restored = sub / "restored"
        result = drill_epoch_bundle(bundle, restored, execute=True,
                                    expected_environment=mode)
        state_root = restored_state_root(restored, profile)
        gate_file = restored_gate_path(restored, profile)
        receipt = json.loads((restored / "restore_receipt.json").read_text(encoding="utf-8"))
        print(f"  [{label}] state_root={state_root.relative_to(restored).as_posix()} "
              f"gate={gate_file.relative_to(restored).as_posix()}")

        check(f"{label} 状态根与 RuntimePaths.state_root 同口径",
              state_root.relative_to(restored).as_posix() == expected_rel,
              state_root.relative_to(restored).as_posix())
        present = sorted(name for name in REQUIRED_STATE_FILES if (state_root / name).is_file())
        check(f"{label} 四个必需状态 JSON 都落在状态根内",
              len(present) == len(REQUIRED_STATE_FILES), str(present))
        check(f"{label} 数据库落在状态根内",
              (state_root / "agent_trader.sqlite").is_file())
        # P1-1：还原路径**真的**落盘门禁（文件 + 安装戳），不是只写一句声明。
        check(f"{label} 还原后 ops/recovery_gate.json 真的存在",
              gate_file.is_file(), str(gate_file))
        check(f"{label} 门禁安装戳存在", gate_install_marker(gate_file).is_file())
        check(f"{label} 回执指向的门禁 = 还原根里真实那个门禁",
              str(receipt.get("recovery_gate_file") or "")
              == gate_file.relative_to(restored).as_posix(),
              str(receipt.get("recovery_gate_file")))
        decision = RecoveryGate.load(gate_file).before_entry()
        check(f"{label} 还原后 before_entry() 阻断（不是 GATE_NOT_INSTALLED 放行）",
              (not decision.allowed)
              and RECONCILE_REQUIRED_BEFORE_ENTRY in decision.reason_codes,
              f"state={decision.state} allowed={decision.allowed} "
              f"codes={decision.reason_codes}")
        check(f"{label} 结果对象带出同一个门禁路径",
              Path(str(result.get("recovery_gate_file") or "")) == gate_file,
              str(result.get("recovery_gate_file")))

        # 非循环性：删掉**还原根里那个真实门禁**（安装戳仍在）→ 判定必须翻转。
        gate_file.unlink()
        after_unlink = RecoveryGate.load(gate_file).before_entry()
        check(f"{label} 删掉真实门禁文件 → GATE_MISSING 阻断（证明用的就是它）",
              (not after_unlink.allowed) and "GATE_MISSING" in after_unlink.reason_codes,
              f"allowed={after_unlink.allowed} codes={after_unlink.reason_codes}")


def check_p1_3(tmp: Path) -> None:
    print("\n[P1-3] 门禁证据读取时用账本真实事实复验")
    sub = tmp / "p3"
    _, bundle = _build_bundle(sub, profile=None, mode="demo",
                              entry_client_id="ENTRY-BTC-1")
    restored = sub / "restored"
    drill_epoch_bundle(bundle, restored, execute=True, expected_environment="demo")
    state_root = restored_state_root(restored, None)
    gate_file = restored_gate_path(restored, None)

    live = ledger_reconcile_facts(state_root / "agent_trader.sqlite")
    check("账本真实事实可读，且能看到未收口的 ENTRY 单",
          isinstance(live, dict) and int(live.get("unresolved_entry_orders", 0)) >= 1,
          str(live))

    # (a) 手写 CLEARED + 空证据 —— 修复前这条 allowed=True。
    _forged_gate(gate_file, evidence=None)
    forged_empty = RecoveryGate.load(gate_file).before_entry()
    print(f"  forged(empty evidence) -> allowed={forged_empty.allowed} "
          f"codes={forged_empty.reason_codes}")
    check("手写 CLEARED（evidence 空）不再放行", not forged_empty.allowed,
          f"allowed={forged_empty.allowed}")

    # (b) 手写 CLEARED + 自算摘要但声明"零未收口" —— 账本事实必须压过声明。
    facts = {"unknown_orders": 0, "submitting_orders": 0, "unresolved_entry_orders": 0}
    _forged_gate(gate_file, evidence={"facts": facts, "facts_digest": facts_digest(facts)})
    forged_digest = RecoveryGate.load(gate_file).before_entry()
    print(f"  forged(self-consistent digest, ledger dirty) -> "
          f"allowed={forged_digest.allowed} codes={forged_digest.reason_codes}")
    check("手写 CLEARED + 自洽摘要，但账本仍有未收口 ENTRY → 仍阻断",
          (not forged_digest.allowed)
          and RECONCILE_REQUIRED_BEFORE_ENTRY in forged_digest.reason_codes,
          f"allowed={forged_digest.allowed} codes={forged_digest.reason_codes}")

    # (c) 摘要与事实对不上 → 阻断。
    _forged_gate(gate_file, evidence={"facts": facts, "facts_digest": "0" * 64})
    forged_bad_digest = RecoveryGate.load(gate_file).before_entry()
    check("证据摘要对不上 → 阻断",
          (not forged_bad_digest.allowed) and forged_bad_digest.reason_codes != (),
          f"allowed={forged_bad_digest.allowed} codes={forged_bad_digest.reason_codes}")

    # (d) 真实收口路径：先把未收口单收口，再提交结构化证据 → 放行。
    db = AuditDatabase(state_root / "agent_trader.sqlite")
    try:
        db.environment = "demo"
        outbox = OrderOutbox(db, environment="demo")
        # 与真实恢复流程同序：重启先把 SUBMITTING 收敛为 UNKNOWN（绝不重发），
        # 再按交易所回执把 UNKNOWN 收口为终态。
        moved = outbox.recover_stale_submitting()
        db.conn.execute(
            "UPDATE order_outbox SET status='FILLED' WHERE status IN ('UNKNOWN','SUBMITTING')")
        db.conn.commit()
    finally:
        db.close()
    print(f"  reconciled stale submitting rows = {moved}")
    clean = ledger_reconcile_facts(state_root / "agent_trader.sqlite")
    check("收口后账本事实归零",
          isinstance(clean, dict) and int(clean.get("unresolved_entry_orders", 0)) == 0,
          str(clean))

    now_ms = int(time.time() * 1000)
    gate = RecoveryGate.load(gate_file)
    gate.require_reconcile(epoch_id="epoch-p3", account_ref="demo:u13f1",
                           runtime_instance_id="rt-u13f1")
    blocked = gate.before_entry(now_ms=now_ms)
    check("require_reconcile 后立即开仓被拒",
          (not blocked.allowed) and RECONCILE_REQUIRED_BEFORE_ENTRY in blocked.reason_codes,
          f"allowed={blocked.allowed} codes={blocked.reason_codes}")
    cleared = RecoveryGate.load(gate_file).complete_reconcile(
        {"observed_at_ms": now_ms, "positions_verified": True,
         "open_orders_verified": True}, now_ms=now_ms,
        account_ref="demo:u13f1", runtime_instance_id="rt-u13f1")
    stored = RecoveryGate.load(gate_file)
    check("账本无未收口单 + 结构化证据 → 放行",
          cleared.allowed and GATE_CLEARED in cleared.reason_codes,
          f"allowed={cleared.allowed} codes={cleared.reason_codes}")
    check("落盘证据携带可核事实与摘要",
          isinstance(stored.evidence.get("facts"), dict)
          and bool(stored.evidence.get("facts_digest")),
          str(stored.evidence))
    check("放行后 before_entry() 仍放行",
          RecoveryGate.load(gate_file).before_entry(now_ms=now_ms).allowed)

    # (e) 放行之后再出现未收口 ENTRY 单 → 读取时必须重新阻断（读取时复验）。
    db = AuditDatabase(state_root / "agent_trader.sqlite")
    try:
        db.environment = "demo"
        outbox = OrderOutbox(db, environment="demo")
        outbox.prepare(
            client_order_id="ENTRY-BTC-2", symbol="ETHUSDT", side="BUY",
            position_side="LONG", order_type="MARKET", purpose="ENTRY",
            qty=1.0, price=None, reduce_only=False, endpoint="/fapi/v1/order",
            request={}, command_kind="STANDARD", qty_text="1.0")
        db.conn.commit()
    finally:
        db.close()
    regressed = RecoveryGate.load(gate_file).before_entry(now_ms=now_ms)
    print(f"  after new unresolved ENTRY -> allowed={regressed.allowed} "
          f"codes={regressed.reason_codes}")
    check("放行后账本又出现未收口 ENTRY → before_entry() 重新阻断",
          (not regressed.allowed)
          and RECONCILE_REQUIRED_BEFORE_ENTRY in regressed.reason_codes,
          f"allowed={regressed.allowed} codes={regressed.reason_codes}")

    # (f) 损坏 / 删除仍然 fail-closed（原有保证不得被放宽）。
    gate_file.unlink()
    deleted = RecoveryGate.load(gate_file).before_entry(now_ms=now_ms)
    check("门禁文件被删除（安装戳在）仍 GATE_MISSING 阻断",
          (not deleted.allowed) and "GATE_MISSING" in deleted.reason_codes,
          f"allowed={deleted.allowed} codes={deleted.reason_codes}")


def check_p1_4(tmp: Path) -> None:
    print("\n[P1-4] 未声明状态根可检出（STATE_ROOT_MISSING 可达）")
    code_a = tmp / "code_A"
    code_b = tmp / "code_B"
    code_a.mkdir(parents=True, exist_ok=True)
    code_b.mkdir(parents=True, exist_ok=True)

    legacy = RuntimePaths.resolve(code_a, profile="demo")
    reasons = legacy.validate("demo")
    print(f"  legacy state_base_declared={legacy.state_base_declared!r} "
          f"source={legacy.state_base_source} explicit={legacy.state_base_explicit}")
    print(f"  legacy.validate('demo') = {reasons}")
    check("未声明状态根时 validate() 产出 STATE_ROOT_MISSING",
          STATE_ROOT_MISSING in reasons, str(reasons))
    if not hasattr(legacy, "state_root_drift"):
        check("state_base_explicit 有真实消费者（state_root_drift）", False,
              "RuntimePaths.state_root_drift 不存在")
    else:
        drift = legacy.state_root_drift()
        check("state_root_drift 由 state_base_explicit 驱动且给出漂移码",
              drift["declared"] is False and drift["code"] == STATE_ROOT_MISSING
              and drift["lock_follows_code_root"] is True, str(drift))
    check("旧布局的 state_base_declared 保持 'data'（既有消费者零漂移）",
          legacy.state_base_declared == "data", repr(legacy.state_base_declared))
    check("旧布局解析出的状态基座仍是 <code_root>/data（默认部署不受影响）",
          legacy.state_base == (code_a / "data").resolve(), str(legacy.state_base))

    explicit = RuntimePaths.resolve(code_a, profile="demo", state_root=tmp / "shared")
    check("显式声明状态根时不产出 STATE_ROOT_MISSING",
          STATE_ROOT_MISSING not in explicit.validate("demo"),
          str(explicit.validate("demo")))
    check("显式声明下两个代码根仍解析到同一状态基座/同一把锁",
          RuntimePaths.resolve(code_b, profile="demo",
                               state_root=tmp / "shared").lock_file
          == explicit.lock_file)
    check("未声明下两个代码根解析到不同的锁（这正是要**报告**的漂移）",
          RuntimePaths.resolve(code_a, profile="demo").lock_file
          != RuntimePaths.resolve(code_b, profile="demo").lock_file)
    # 唯一判定源（U15）的既有原因码不得被新码语义重叠地替换。
    live_reasons = RuntimePaths.resolve(code_a, profile=None).validate("live")
    check("live + 无 profile 仍报原有 STATE_ROOT_PROFILE_UNLOCKED",
          "STATE_ROOT_PROFILE_UNLOCKED" in live_reasons, str(live_reasons))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="u13f1-gate-"))
    print(f"U13-FU1 verify tmp = {tmp}")
    try:
        check_p1_1_and_p1_2(tmp)
        check_p1_3(tmp)
        check_p1_4(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nU13-FU1 RECOVERY GATE: {PASS} 通过 / {len(FAIL)} 失败")
    for name in FAIL:
        print(f"  FAILED: {name}")
    for text in NOTES:
        print(f"  NOTE: {text}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
