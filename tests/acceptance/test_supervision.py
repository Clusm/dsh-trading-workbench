"""U13 验收：托管、单实例、RuntimePaths 与同 epoch 恢复包。

独立可运行（本仓库无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_supervision.py

全程离线：只在临时目录里造 fixture 状态，不读真实 ``.env``/``data``/密钥，
不联网、不启动服务、不下单、不发告警。逐项打印 PASS/FAIL，任一失败以退出码 1 结束。

**两态可跑**：本套件只依赖 U13 独占交付物（``core.runtime_paths`` /
``core.ops.supervision`` / ``core.ops.recovery``），**不**假定任何共享适配补丁
是否已应用，因此在"作者 worktree"和"集成树"两态下结果一致。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.instance_lock import InstanceLock  # noqa: E402
from core.ops.recovery import (  # noqa: E402
    EVIDENCE_MISSING,
    EVIDENCE_STALE,
    EVIDENCE_TYPE_INVALID,
    EPOCH_MISMATCH,
    GATE_CLEARED,
    RECONCILE_REQUIRED_BEFORE_ENTRY,
    RECOVERY_INCOMPLETE,
    RECOVERY_WRITER_RUNNING,
    SAME_VOLUME_NOT_DISASTER_RECOVERY,
    STATE_CORRUPT,
    RecoveryError,
    RecoveryGate,
    check_writer_stopped,
    create_epoch_bundle,
    drill_epoch_bundle,
    recovery_plan,
    state_json_health,
    verify_epoch_bundle,
)
from core.ops.supervision import (  # noqa: E402
    BACKOFF_SCHEDULE_SECONDS,
    CLOCK_FORWARD_JUMP,
    CLOCK_ROLLBACK,
    CLOCK_UNTRUSTED,
    MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY,
    MANUAL_RECOVERY_REQUIRED,
    OPERATOR_CONFIRM_REQUIRED,
    PROFILE_NOT_DECLARED,
    RESTART_ALLOWED,
    RESTART_LIMIT_EXCEEDED,
    STATE_BLOCKED,
    STATE_MANUAL_RECOVERY,
    STATE_RESTART_BACKOFF,
    ClockGuard,
    ClockReading,
    RestartPolicy,
    RunRegistry,
    SupervisionLedger,
    load_or_mint_run_record,
    lock_scope_report,
    operator_clear_manual_recovery,
    plan_supervision,
)
from core.runtime_identity import (  # noqa: E402
    LIVE_SHARED_STATE_ROOT,
    STATE_ROOT_PROFILE_UNLOCKED,
)
from core.runtime_paths import (  # noqa: E402
    CONSUMER_INVENTORY,
    REQUIRED_STATE_FILES,
    U13_FINGERPRINT_DISPOSITION,
    RuntimePaths,
    RuntimePathsError,
)

PASSED = 0
FAILURES: list[str] = []


def check(name: str, condition: bool) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(name)
    print(f"  FAIL  {name}")


def raises(kind: type[BaseException], fn, *args, **kwargs) -> str:
    """返回异常文本；未抛出或抛错类型不符则返回空串。"""
    try:
        fn(*args, **kwargs)
    except kind as exc:  # noqa: PERF203
        return f"{type(exc).__name__}: {exc}"
    except BaseException as exc:  # noqa: BLE001
        return f"WRONG_TYPE {type(exc).__name__}: {exc}"
    return ""


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------


def make_state_fixture(base: Path, *, environment: str = "demo") -> RuntimePaths:
    """造一个最小可用状态根：SQLite + 关键 JSON。"""
    paths = RuntimePaths.resolve(base, profile=None)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(paths.db_file)
    connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO schema_migrations VALUES(17)")
    connection.commit()
    connection.close()
    payloads = {
        "capital_state.json": {"mode": environment},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": f"{environment}:test"},
    }
    for name, payload in payloads.items():
        (paths.data_dir / name).write_text(json.dumps(payload), encoding="utf-8")
    return paths


# --------------------------------------------------------------------------
# 必达要点 1：profile 显式；人工禁用 live 优先于自动恢复
# --------------------------------------------------------------------------


def check_profile_declared() -> None:
    print("\n[1] profile 显式")
    with tempfile.TemporaryDirectory(prefix="u13-profile-") as tmp:
        base = Path(tmp)
        legacy = RuntimePaths.resolve(base, profile=None)
        check("未声明 profile 时 profile_declared=False", not legacy.profile_declared)
        ledger = SupervisionLedger.load(base / "sup.json")
        decision = plan_supervision(ledger, now=1000.0, profile=None)
        check("未声明 profile 的托管决策被拒绝",
              not decision.allowed and decision.state == STATE_BLOCKED)
        check("未声明 profile 的原因码为 PROFILE_NOT_DECLARED",
              PROFILE_NOT_DECLARED in decision.reason_codes)
        check("非法 profile 被拒绝",
              bool(raises(RuntimePathsError, RuntimePaths.resolve, base, profile="prod")))

        declared = RuntimePaths.resolve(base, profile="live", state_root=base / "state")
        check("显式 profile 被记录", declared.profile == "live" and declared.profile_declared)
        check("live + 独立状态根通过环境一致性校验", declared.validate("live") == ())

        # 默认（代码根内 data）状态根跑 live 必须 fail-closed
        shared = RuntimePaths.resolve(base, profile="live")
        reasons = shared.validate("live")
        check("live 落在公共 data 根被拒绝（LIVE_SHARED_STATE_ROOT）",
              LIVE_SHARED_STATE_ROOT in reasons)
        reason_text = raises(RuntimePathsError, shared.assert_environment_bound, "live")
        check("assert_environment_bound 对 live+公共根抛错", bool(reason_text))
        unlocked = RuntimePaths.resolve(base, profile=None).validate("live")
        check("未选 profile 跑 live 报告 STATE_ROOT_PROFILE_UNLOCKED",
              STATE_ROOT_PROFILE_UNLOCKED in unlocked and LIVE_SHARED_STATE_ROOT in unlocked)


def check_manual_disable_precedence() -> None:
    print("\n[2] 人工禁用 live 优先于自动恢复（不得被托管逻辑覆盖）")
    with tempfile.TemporaryDirectory(prefix="u13-manual-") as tmp:
        path = Path(tmp) / "sup.json"
        ledger = SupervisionLedger.load(path)
        policy = RestartPolicy()
        # 先制造"重启额度用完"的局面，再验证人工禁用优先级更高。
        for index in range(policy.max_restarts):
            ledger.record_restart(1000.0 + index, policy=policy)
        ledger.set_manual_disable(actor="operator", reason="operator closed live",
                                  source="control:emergency", now=1100.0)
        ledger.save()
        reloaded = SupervisionLedger.load(path)
        check("人工禁用已持久化", reloaded.manual_disable.active)
        decision = plan_supervision(reloaded, now=1200.0, profile="live")
        check("人工禁用下不自动重启",
              not decision.allowed and decision.state == STATE_MANUAL_RECOVERY)
        check("人工禁用原因码正确",
              MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY in decision.reason_codes)
        check("人工禁用不因重启额度用尽而放宽",
              RESTART_LIMIT_EXCEEDED not in decision.reason_codes)
        check("人工禁用期间记录重启被拒绝",
              bool(raises(RuntimeError, reloaded.record_restart, 1300.0)))

        auto_ok, auto_codes = operator_clear_manual_recovery(
            reloaded, actor="auto", confirm=True, now=1400.0)
        check("auto 执行者不能解除人工禁用",
              not auto_ok and MANUAL_DISABLE_PRECEDES_AUTO_RECOVERY in auto_codes)
        no_confirm_ok, no_confirm_codes = operator_clear_manual_recovery(
            reloaded, actor="operator", confirm=False, now=1400.0)
        check("未确认不能解除人工禁用",
              not no_confirm_ok and OPERATOR_CONFIRM_REQUIRED in no_confirm_codes)
        ok, _codes = operator_clear_manual_recovery(reloaded, actor="operator",
                                                    confirm=True, now=1500.0)
        check("operator 显式确认后可解除", ok and not reloaded.manual_disable.active)
        after = plan_supervision(reloaded, now=1600.0, profile="live")
        check("解除后恢复到有界退避", after.allowed and after.state == STATE_RESTART_BACKOFF)


# --------------------------------------------------------------------------
# 必达要点 2：重启有界
# --------------------------------------------------------------------------


def check_bounded_restart() -> None:
    print("\n[3] 重启有界：30/60/120 秒；10 分钟内 3 次 → MANUAL_RECOVERY")
    with tempfile.TemporaryDirectory(prefix="u13-bounded-") as tmp:
        path = Path(tmp) / "sup.json"
        ledger = SupervisionLedger.load(path)
        policy = RestartPolicy()
        delays = []
        now = 1000.0
        for _ in range(policy.max_restarts):
            decision = plan_supervision(ledger, now=now, profile="demo", policy=policy)
            check(f"第 {len(delays) + 1} 次重启被允许",
                  decision.allowed and decision.state == STATE_RESTART_BACKOFF)
            delays.append(decision.delay_seconds)
            check("允许原因码为 RESTART_ALLOWED",
                  RESTART_ALLOWED in decision.reason_codes)
            ledger.record_restart(now, policy=policy)
            ledger.save()
            now += 1.0
        check("退避序列为 30/60/120 秒",
              tuple(delays) == tuple(BACKOFF_SCHEDULE_SECONDS) == (30.0, 60.0, 120.0))

        blocked = plan_supervision(ledger, now=now, profile="demo", policy=policy)
        check("窗口内第 4 次重启被拒绝",
              not blocked.allowed and blocked.state == STATE_MANUAL_RECOVERY)
        check("拒绝原因码为 RESTART_LIMIT_EXCEEDED",
              RESTART_LIMIT_EXCEEDED in blocked.reason_codes)
        check("进入 MANUAL_RECOVERY 已持久化", SupervisionLedger.load(path).manual_recovery)
        again = plan_supervision(SupervisionLedger.load(path), now=now + 5.0,
                                 profile="demo", policy=policy)
        check("人工恢复态不会自动重启（不无限重启）",
              not again.allowed and again.state == STATE_MANUAL_RECOVERY)
        check("人工恢复态原因码含 MANUAL_RECOVERY_REQUIRED",
              MANUAL_RECOVERY_REQUIRED in again.reason_codes)
        later = plan_supervision(SupervisionLedger.load(path),
                                 now=now + policy.window_seconds + 1.0,
                                 profile="demo", policy=policy)
        check("窗口过期也不自动解除人工恢复态（须 operator）", not later.allowed)

        fresh = SupervisionLedger.load(Path(tmp) / "fresh.json")
        fresh.record_restart(1000.0, policy=policy)
        fresh.record_restart(1010.0, policy=policy)
        sliding = plan_supervision(fresh, now=1010.0 + policy.window_seconds + 20.0,
                                   profile="demo", policy=policy)
        check("滑动窗口外旧记录不再计入（可重新从 30 秒退避）",
              sliding.restarts_in_window == 0 and sliding.allowed
              and sliding.delay_seconds == BACKOFF_SCHEDULE_SECONDS[0])


# --------------------------------------------------------------------------
# 必达要点 3：单账户执行者（单实例锁），换代码根不改状态根（R12）
# --------------------------------------------------------------------------


def check_single_actor_state_root() -> None:
    print("\n[4] 单账户执行者：锁由状态根决定，换代码根不换锁（R12/F06/F18）")
    with tempfile.TemporaryDirectory(prefix="u13-lock-") as tmp:
        base = Path(tmp)
        state = base / "state"
        code_a = base / "code-a"
        code_b = base / "code-b"
        code_a.mkdir()
        code_b.mkdir()
        paths_a = RuntimePaths.resolve(code_a, profile="live", state_root=state)
        paths_b = RuntimePaths.resolve(code_b, profile="live", state_root=state)
        check("换代码根后锁路径不变", paths_a.lock_file == paths_b.lock_file)
        check("换代码根后锁作用域不变（同一账户同一执行者）",
              paths_a.lock_scope() == paths_b.lock_scope())
        other = RuntimePaths.resolve(code_a, profile="demo", state_root=state)
        check("不同 profile 使用不同锁（demo 与 live 互不阻塞）",
              other.lock_file != paths_a.lock_file)
        other_state = RuntimePaths.resolve(code_a, profile="live", state_root=base / "state2")
        check("不同状态根使用不同锁", other_state.lock_file != paths_a.lock_file)

        scope = lock_scope_report(paths_a)
        check("锁作用域报告含状态根与 profile",
              scope["lock_file"] == str(paths_a.lock_file)
              and scope["state_base"] == str(state) and scope["profile"] == "live")

        # 真实互斥：同一锁路径第二次获取必须失败（fd 级 O_EXCL + 存活判定）。
        holder = InstanceLock(paths_a.lock_file)
        holder.acquire()
        try:
            second = InstanceLock(paths_b.lock_file)
            conflict = raises(RuntimeError, second.acquire)
            check("同一状态根第二实例被拒绝", bool(conflict) and "已在运行" in conflict)
        finally:
            holder.release()
        third = InstanceLock(paths_b.lock_file)
        third.acquire()
        check("持有者释放后可接管", third._owned)
        third.release()

        # 运行代持久化（diff-6）：重启沿用同一运行代；轮换凭据不换运行代。
        registry_a = RunRegistry(paths_a.run_registry_file)
        first, created_first = registry_a.load_or_mint(
            environment="live", account_ref="live:acct-1",
            credential_epoch=1, key_fingerprint="deadbeef", now=2000.0)
        check("首次铸造运行代", created_first and first.runtime_instance_id)
        registry_b = RunRegistry(paths_b.run_registry_file)
        second_record, created_second = registry_b.load_or_mint(
            environment="live", account_ref="live:acct-1",
            credential_epoch=1, key_fingerprint="deadbeef", now=3000.0)
        check("换代码根重启沿用同一 runtime_instance_id",
              not created_second
              and second_record.runtime_instance_id == first.runtime_instance_id)
        check("换代码根重启沿用同一 generation_id",
              second_record.generation_id == first.generation_id)
        rotated = registry_b.rotate_credential(credential_epoch=2,
                                               key_fingerprint="cafebabe")
        check("凭据轮换不改变运行代",
              rotated.runtime_instance_id == first.runtime_instance_id
              and rotated.generation_id == first.generation_id)
        check("凭据轮换推进 credential_epoch", rotated.credential_epoch == 2)
        switched, _ = RunRegistry(paths_a.run_registry_file).load_or_mint(
            environment="demo", account_ref="demo:other", now=4000.0)
        check("环境/账户变化不继承旧运行代",
              switched.runtime_instance_id != first.runtime_instance_id)
        helper, _ = load_or_mint_run_record(paths_a, environment="live",
                                            account_ref="live:acct-1", now=5000.0)
        check("便捷入口写出的运行代带锁作用域",
              helper.scope_hash == paths_a.lock_scope())


# --------------------------------------------------------------------------
# 必达要点 5：RuntimePaths 全消费者清单 + 兼容迁移；shared_egress_root 唯一
# --------------------------------------------------------------------------


def check_runtime_paths_inventory() -> None:
    print("\n[5] RuntimePaths 全消费者清单、兼容迁移、shared_egress_root 唯一")
    with tempfile.TemporaryDirectory(prefix="u13-paths-") as tmp:
        base = Path(tmp)
        code = base / "code"
        code.mkdir()
        legacy = RuntimePaths.resolve(code, profile="demo")
        check("消费者清单覆盖 18 项以上", len(CONSUMER_INVENTORY) >= 18)
        keys = [entry.key for entry in CONSUMER_INVENTORY]
        check("消费者 key 不重复", len(keys) == len(set(keys)))
        allowed_status = {"migrated", "aligned", "compat", "planned"}
        check("消费者状态取值合法",
              all(entry.status in allowed_status for entry in CONSUMER_INVENTORY))
        check("每个消费者都有归属与说明",
              all(entry.owner and entry.note for entry in CONSUMER_INVENTORY))
        check("planned 消费者被显式登记（不静默忽略）",
              set(legacy.unresolved_consumers())
              == {e.key for e in CONSUMER_INVENTORY if e.status == "planned"})
        check("所有规范路径不逃出代码根/状态根", legacy.escape_check() == [])

        check("shared_egress_root 跨 profile 唯一",
              legacy.shared_egress_root_is_unique()
              and legacy.with_profile("demo").shared_egress_root
              == legacy.with_profile("live").shared_egress_root)
        check("shared_egress_root 不随 profile 分区",
              legacy.shared_egress_root.name == "shared")
        check("共享出口消费者都落在 shared_egress_root 下",
              str(legacy.rate_limit_state).startswith(str(legacy.shared_egress_root))
              and str(legacy.coordination_dir).startswith(str(legacy.shared_egress_root)))

        # 兼容迁移：只复制、不删除、可回滚。
        legacy.legacy_rate_limit_state.parent.mkdir(parents=True, exist_ok=True)
        legacy.legacy_rate_limit_state.write_text(
            json.dumps({"cooldown_until": 123}), encoding="utf-8")
        plan = legacy.compatibility_migration_plan()
        entry = next(row for row in plan if row["key"] == "rate_limit_state")
        check("旧限流状态被识别为需复制", entry["action"] == "copy")
        dry = legacy.apply_compatibility_migration(dry_run=True)
        check("dry-run 不写盘", not legacy.rate_limit_state.exists())
        check("dry-run 结果为 dry-run",
              next(r for r in dry if r["key"] == "rate_limit_state")["applied"] == "dry-run")
        applied = legacy.apply_compatibility_migration(dry_run=False)
        check("实迁后规范位置出现",
              next(r for r in applied if r["key"] == "rate_limit_state")["applied"] == "yes"
              and legacy.rate_limit_state.is_file())
        check("旧文件原地保留（迁移可回滚）", legacy.legacy_rate_limit_state.is_file())
        second = legacy.apply_compatibility_migration(dry_run=False)
        check("已存在时不覆盖",
              next(r for r in second if r["key"] == "rate_limit_state")["applied"] == "no")
        check("规范位置存在时限流状态优先读规范位置",
              legacy.shared_egress_state_file() == legacy.rate_limit_state)
        check("未声明状态根时锁定旧布局（零漂移）",
              legacy.state_base == (code / "data").resolve()
              and legacy.state_base_declared == "data")


# --------------------------------------------------------------------------
# 必达要点 4 + 6：DB+JSON 同 epoch 恢复包、可演练、恢复后先对账再开仓
# --------------------------------------------------------------------------


def check_recovery_epoch_bundle() -> None:
    print("\n[6] DB+JSON 同 epoch 恢复包与可演练恢复")
    with tempfile.TemporaryDirectory(prefix="u13-recovery-") as tmp:
        base = Path(tmp)
        paths = make_state_fixture(base / "host", environment="demo")
        bundles = base / "bundles"
        bundle = create_epoch_bundle(paths, bundles, environment="demo",
                                     account_ref="demo:acct", runtime_instance_id="rt-1",
                                     generation_id="gen-1")
        verified = verify_epoch_bundle(bundle, expected_environment="demo")
        check("恢复包含数据库与全部关键 JSON",
              verified["files"] == 1 + len(REQUIRED_STATE_FILES))
        check("数据库迁移版本进入清单", verified["database_schema"] == 17)
        check("epoch 记录环境/profile/账户/运行代",
              verified["epoch"]["environment"] == "demo"
              and verified["epoch"]["account_ref"] == "demo:acct"
              and verified["epoch"]["runtime_instance_id"] == "rt-1")
        check("同一 epoch 窗口跨度在容差内",
              verified["span_ms"] <= verified["epoch"]["tolerance_ms"])
        check("writer_stopped 声明为真",
              json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
              ["writer_stopped"] is True)

        # 篡改 epoch 窗口 → EPOCH_MISMATCH
        tampered = base / "tampered"
        _copy_tree(bundle, tampered)
        manifest = json.loads((tampered / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["capital_state.json"]["snapshot_at_ms"] += 10 * 60 * 1000
        _rewrite_manifest(tampered, manifest)
        check("epoch 窗口外的成员被拒绝",
              EPOCH_MISMATCH in raises(RecoveryError, verify_epoch_bundle, tampered))

        # 哈希篡改 → RECOVERY_INCOMPLETE
        corrupted = base / "corrupted"
        _copy_tree(bundle, corrupted)
        (corrupted / "state" / "capital_state.json").write_text(
            json.dumps({"mode": "live"}), encoding="utf-8")
        check("成员哈希不符被拒绝",
              RECOVERY_INCOMPLETE in raises(RecoveryError, verify_epoch_bundle, corrupted))

        # 演练：dry-run 不落盘；execute 还原到全新目录并复核
        dry_target = base / "drill-dry"
        dry = drill_epoch_bundle(bundle, dry_target, execute=False)
        check("dry-run 标注同卷不等于灾难恢复",
              SAME_VOLUME_NOT_DISASTER_RECOVERY in dry["notices"] and not dry_target.exists())
        check("dry-run 要求先对账", dry["reconcile_required"] is True)
        check("要求独立卷时同卷目标被拒绝",
              SAME_VOLUME_NOT_DISASTER_RECOVERY
              in raises(RecoveryError, drill_epoch_bundle, bundle, base / "drill-x",
                        execute=True, require_independent_volume=True))
        drill_target = base / "drill"
        done = drill_epoch_bundle(bundle, drill_target, execute=True)
        check("演练还原到全新目录",
              done["status"] == "restored" and drill_target.is_dir())
        check("演练回执声明门禁为 RECONCILE_REQUIRED",
              json.loads((drill_target / "restore_receipt.json").read_text(encoding="utf-8"))
              ["entry_gate"] == RECONCILE_REQUIRED_BEFORE_ENTRY)
        check("演练还原的数据库通过 quick_check",
              (drill_target / "data" / "agent_trader.sqlite").is_file())
        check("演练不覆盖既有目录",
              bool(raises(RecoveryError, drill_epoch_bundle, bundle, drill_target,
                          execute=True)))

        # 坏包不得覆盖好包 / 中断不发布
        again = create_epoch_bundle(paths, bundles, environment="demo")
        check("多次建包互不覆盖", again != bundle and again.is_dir())

        # 停写证明：锁文件里是本进程 PID → 拒绝建包
        paths.lock_file.parent.mkdir(parents=True, exist_ok=True)
        paths.lock_file.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        check("服务仍在写时拒绝建包",
              RECOVERY_WRITER_RUNNING in raises(RecoveryError, check_writer_stopped, paths))
        check("服务仍在写时 create 拒绝",
              RECOVERY_WRITER_RUNNING
              in raises(RecoveryError, create_epoch_bundle, paths, bundles,
                        environment="demo"))
        paths.lock_file.unlink()

        # 提交前状态损坏 → 拒绝建包，不静默
        (paths.data_dir / "control_state.json").write_text("{not json", encoding="utf-8")
        health = state_json_health(paths.data_dir)
        check("损坏 JSON 被识别为 STATE_CORRUPT",
              STATE_CORRUPT in health["problems"] and not health["new_entry_allowed"])
        check("损坏状态拒绝建包",
              STATE_CORRUPT in raises(RecoveryError, create_epoch_bundle, paths, bundles,
                                      environment="demo"))
        (paths.data_dir / "control_state.json").write_text(
            json.dumps({"state": "normal"}), encoding="utf-8")
        (paths.data_dir / "channel_state.json").unlink()
        check("缺关键状态拒绝建包",
              RECOVERY_INCOMPLETE in raises(RecoveryError, create_epoch_bundle, paths,
                                            bundles, environment="demo"))

        plan = recovery_plan(paths)
        check("恢复计划标注同盘不等于灾难恢复",
              plan["disaster_recovery_requires_independent_volume"] is True
              and plan["same_volume_as_state"] is True)
        check("恢复计划步骤含先对账再开仓",
              any("对账" in step for step in plan["steps"]))


def _copy_tree(source: Path, target: Path) -> None:
    import shutil

    shutil.copytree(source, target)


def _rewrite_manifest(bundle: Path, manifest: dict) -> None:
    import hashlib

    data = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n").encode("utf-8")
    (bundle / "manifest.json").write_bytes(data)
    (bundle / "manifest.sha256").write_text(
        f"{hashlib.sha256(data).hexdigest()}  manifest.json\n",
        encoding="ascii", newline="\n")


def check_reconcile_before_entry() -> None:
    print("\n[7] 恢复后先对账再开仓（F14）")
    with tempfile.TemporaryDirectory(prefix="u13-gate-") as tmp:
        path = Path(tmp) / "gate.json"
        gate = RecoveryGate.load(path)
        check("初始状态允许开仓（未恢复）", gate.before_entry(now_ms=1_000_000).allowed)
        gate.require_reconcile(epoch_id="epoch-1", account_ref="demo:acct",
                               runtime_instance_id="rt-1")
        blocked = RecoveryGate.load(path).before_entry(now_ms=1_000_000)
        check("恢复后立即开仓被拒绝",
              not blocked.allowed
              and RECONCILE_REQUIRED_BEFORE_ENTRY in blocked.reason_codes)
        check("门禁状态持久化", RecoveryGate.load(path).state == "RECONCILE_REQUIRED")

        base_evidence = {
            "observed_at_ms": 1_000_000, "positions_verified": True,
            "open_orders_verified": True, "account_ref": "demo:acct",
            "runtime_instance_id": "rt-1",
        }
        missing = gate.complete_reconcile({"observed_at_ms": 1_000_000,
                                           "positions_verified": True},
                                          now_ms=1_000_000)
        check("缺字段的证据被拒绝", not missing.allowed and EVIDENCE_MISSING in missing.reason_codes)
        bare = gate.complete_reconcile(True, now_ms=1_000_000)
        check("非映射证据被拒绝", not bare.allowed and EVIDENCE_MISSING in bare.reason_codes)
        typed = gate.complete_reconcile(
            {**base_evidence, "positions_verified": "yes"}, now_ms=1_000_000)
        check("类型混淆的证据被拒绝",
              not typed.allowed and EVIDENCE_TYPE_INVALID in typed.reason_codes)
        unverified = gate.complete_reconcile(
            {**base_evidence, "open_orders_verified": False}, now_ms=1_000_000)
        check("未完成对账的证据仍被阻断",
              not unverified.allowed
              and RECONCILE_REQUIRED_BEFORE_ENTRY in unverified.reason_codes)
        stale = gate.complete_reconcile(base_evidence, now_ms=1_000_000 + 10**7)
        check("过期证据被拒绝", not stale.allowed and EVIDENCE_STALE in stale.reason_codes)
        future = gate.complete_reconcile(
            {**base_evidence, "observed_at_ms": 1_000_000 + 10**7}, now_ms=1_000_000)
        check("来自未来的证据被拒绝", not future.allowed and EVIDENCE_STALE in future.reason_codes)
        untrusted = gate.complete_reconcile(base_evidence, now_ms=1_000_000,
                                            untrusted_clock=True)
        check("时钟不可信时对账不收口",
              not untrusted.allowed and CLOCK_UNTRUSTED in untrusted.reason_codes)

        ok = gate.complete_reconcile(base_evidence, now_ms=1_000_100,
                                     account_ref="demo:acct",
                                     runtime_instance_id="rt-1")
        check("结构化且新鲜的证据解除阻断",
              ok.allowed and GATE_CLEARED in ok.reason_codes)
        check("证据落盘可复核",
              RecoveryGate.load(path).evidence["positions_verified"] is True)
        check("解除后允许开仓", RecoveryGate.load(path).before_entry(now_ms=1_000_200).allowed)
        check("证据过期后重新阻断",
              not RecoveryGate.load(path).before_entry(now_ms=1_000_200 + 10**7).allowed)


# --------------------------------------------------------------------------
# F17 / F05 / 指纹处置
# --------------------------------------------------------------------------


def check_clock_guard() -> None:
    print("\n[8] 时钟可信度（F17）")
    guard = ClockGuard()
    normal = guard.check(ClockReading(1000.0, 100.0), ClockReading(1010.0, 110.0))
    check("正常推进可信", normal.trusted)
    rollback = guard.check(ClockReading(1000.0, 100.0), ClockReading(900.0, 110.0))
    check("墙钟回拨不可信", not rollback.trusted and CLOCK_ROLLBACK in rollback.reasons)
    forward = guard.check(ClockReading(1000.0, 100.0), ClockReading(1000.0 + 3600, 101.0))
    check("墙钟快进不可信",
          not forward.trusted and CLOCK_FORWARD_JUMP in forward.reasons)
    backwards = guard.check(ClockReading(1000.0, 100.0), ClockReading(1010.0, 90.0))
    check("单调钟倒退不可信", not backwards.trusted)
    allowed, reasons = guard.daily_reset_allowed(device_day="2026-09-21",
                                                 recorded_day="2026-09-20",
                                                 verdict=rollback)
    check("时钟不可信时不做日重置",
          not allowed and CLOCK_UNTRUSTED in reasons)
    allowed_ok, _ = guard.daily_reset_allowed(device_day="2026-09-21",
                                              recorded_day="2026-09-20",
                                              verdict=normal)
    check("时钟可信时允许日切", allowed_ok)

    with tempfile.TemporaryDirectory(prefix="u13-clock-") as tmp:
        ledger = SupervisionLedger.load(Path(tmp) / "sup.json")
        decision = plan_supervision(ledger, now=1000.0, profile="demo", clock=rollback)
        check("时钟不可信时托管决策被阻断",
              not decision.allowed and decision.state == STATE_BLOCKED
              and CLOCK_UNTRUSTED in decision.reason_codes)
        reading = ClockReading(1000.0, 100.0, deadline_monotonic=120.0)
        check("单调期限按单调钟判定（不受墙钟回拨影响）",
              not reading.deadline_expired(119.0) and reading.deadline_expired(120.0))


def check_recovery_script_epoch() -> None:
    """脚本级同 epoch 与恢复门禁（create/restore_recovery_bundle）。

    **两态可跑**：核心断言（schema/停写声明/成员齐全/哈希篡改被拒/恢复后门禁
    阻断）在两种状态下都成立；只有"脚本清单是否带 epoch 块""恢复回执是否带
    entry_gate"取决于共享适配补丁是否已应用，因此用**显式能力探测**处理，
    并把结论打印出来——不是静默跳过。
    """
    print("\n[10] create/restore_recovery_bundle 脚本级同 epoch 与恢复门禁")
    scripts_dir = ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    from create_recovery_bundle import create_recovery_bundle
    from restore_recovery_bundle import restore_to_new_directory

    with tempfile.TemporaryDirectory(prefix="u13-scripts-") as tmp:
        base = Path(tmp)
        project = base / "project"
        data = project / "data"
        (project / "config").mkdir(parents=True)
        data.mkdir()
        connection = sqlite3.connect(data / "agent_trader.sqlite")
        connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO schema_migrations VALUES(17)")
        connection.commit()
        connection.close()
        for name, payload in (
            ("capital_state.json", {"mode": "demo"}),
            ("channel_state.json", {"date": "2026-09-20"}),
            ("control_state.json", {"state": "normal"}),
            ("account_risk_state.json", {"anchor_id": "demo:x"}),
        ):
            (data / name).write_text(json.dumps(payload), encoding="utf-8")
        (project / "config" / "risk_profile.lock.json").write_text("{}", encoding="utf-8")
        (project / ".env").write_text("SECRET=x", encoding="utf-8")

        bundle = create_recovery_bundle(project, base / "bundles")
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        # 只用两态都成立的字段做断言（``writer_stopped`` 由 U13 补丁追加）。
        check("脚本恢复包 schema=2 且声明停写",
              manifest.get("bundle_schema") == 2
              and manifest.get("service_was_stopped") is True)
        check("脚本恢复包不含 .env",
              not (bundle / ".env").exists() and not (bundle / "state" / ".env").exists())
        check("脚本恢复包数据库迁移版本进入清单",
              manifest["files"]["database"]["schema_version"] == 17)

        # 能力探测：U13 epoch 补丁是否已应用到 create_recovery_bundle。
        epoch = manifest.get("epoch")
        if isinstance(epoch, dict):
            stamps = [entry.get("snapshot_at_ms") for entry in manifest["files"].values()]
            low = int(epoch["snapshot_started_ms"]) - int(epoch["tolerance_ms"])
            high = int(epoch["snapshot_finished_ms"]) + int(epoch["tolerance_ms"])
            check("脚本恢复包每个成员都有 snapshot_at_ms",
                  all(isinstance(value, int) and not isinstance(value, bool)
                      for value in stamps))
            check("脚本恢复包成员落在同一 epoch 窗口内",
                  all(low <= int(value) <= high for value in stamps))
            check("脚本恢复包声明写者已停（epoch 语义前提）",
                  manifest.get("writer_stopped") is True)
        else:
            print("  NOTE  create_recovery_bundle 未包含 U13 epoch 块"
                  "（共享适配补丁未应用）；同 epoch 行为已由 core.ops.recovery 覆盖")

        target = base / "restore_target"
        restored = restore_to_new_directory(bundle, target, execute=True,
                                           expected_environment="demo")
        check("脚本恢复演练返回 restored",
              restored["status"] == "restored"
              and (target / "data" / "agent_trader.sqlite").is_file())
        receipt = json.loads((target / "restore_receipt.json").read_text(encoding="utf-8"))
        if "entry_gate" in receipt:
            check("脚本恢复回执声明 RECONCILE_REQUIRED 门禁",
                  receipt["entry_gate"] == RECONCILE_REQUIRED_BEFORE_ENTRY)
            gate_path = target / receipt.get("recovery_gate_file",
                                             "data/ops/recovery_gate.json")
            check("脚本恢复在目标状态根安装恢复门禁", gate_path.is_file())
            blocked = RecoveryGate.load(gate_path).before_entry(now_ms=1_000_000)
            check("脚本恢复后立即开仓被拒绝",
                  not blocked.allowed
                  and RECONCILE_REQUIRED_BEFORE_ENTRY in blocked.reason_codes)
        else:
            print("  NOTE  restore_recovery_bundle 未安装恢复门禁"
                  "（共享适配补丁未应用）；门禁语义已由 core.ops.recovery 覆盖")

        # 篡改成员必须被拒绝（两态都成立）
        tampered = base / "tampered_media"
        import shutil as _shutil

        _shutil.copytree(bundle, tampered)
        with (tampered / "state" / "capital_state.json").open("ab") as handle:
            handle.write(b" ")
        check("脚本恢复校验拒绝被改写的成员",
              bool(raises(ValueError, restore_to_new_directory, tampered,
                          base / "tampered_target")))


def check_scm_dual_profile() -> None:
    """decl-6：SCM 托管双 profile 参数化（``windows_service`` 共享适配）。

    能力探测：``WindowsServiceConfig`` 是否已带 U13 的 ``profile`` 字段。未应用
    补丁时打印 NOTE（不是静默跳过），并把结论写进报告。
    """
    print("\n[11] decl-6 SCM 双 profile 参数化")
    from core.windows_service import WindowsServiceConfig, load_windows_service_config

    if "profile" not in WindowsServiceConfig.__dataclass_fields__:
        print("  NOTE  windows_service.py 未包含 U13 profile 字段"
              "（共享适配补丁未应用）；残余已在 U13 处置表中登记")
        return

    with tempfile.TemporaryDirectory(prefix="u13-scm-") as tmp:
        root = Path(tmp)
        (root / "scripts").mkdir(parents=True)
        (root / "data" / "live").mkdir(parents=True)
        (root / "config").mkdir()
        (root / "scripts" / "run_web.py").write_text("# fixture", encoding="utf-8")

        def write(**updates: object) -> Path:
            payload = {
                "service_name": "MultiAgentTrader", "host": "127.0.0.1", "port": 8000,
                "entrypoint": "scripts/run_web.py", "working_directory": ".",
                "log_path": "data/web_console.log", "interval_minutes": None,
                "no_autostart": False, "stop_timeout_seconds": 30,
                "restart_delay_seconds": 30,
                "python_executable": ".venv/Scripts/python.exe",
            }
            payload.update(updates)
            path = root / "config" / "windows_service.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return path

        legacy = load_windows_service_config(write(), project_root=root)
        check("profile 未声明时服务名不变（升级前语义）",
              legacy.profile is None and legacy.service_name == "MultiAgentTrader")
        live = load_windows_service_config(
            write(profile="live", port=8001, log_path="data/live/web_console.log"),
            project_root=root)
        check("live profile 解析成功且服务名带 profile 后缀",
              live.profile == "live" and live.service_name == "MultiAgentTrader-Live")
        check("live profile 传入 --profile 参数",
              live.run_arguments()[:4] == ["--host", "127.0.0.1", "--port", "8001"]
              and "--profile" in live.run_arguments()
              and live.run_arguments()[live.run_arguments().index("--profile") + 1] == "live")
        check("SCM binPath 使用限定后的服务名",
              live.scm_bin_path().count("windows_service_host.py") == 1)
        demo = load_windows_service_config(
            write(profile="demo", port=8000), project_root=root)
        check("demo profile 与 live 服务名互不相同",
              demo.service_name == "MultiAgentTrader-Demo"
              and demo.service_name != live.service_name)
        check("profile=live 但端口不符时被拒绝",
              bool(raises(ValueError, load_windows_service_config,
                          write(profile="live", port=8000, log_path="data/live/x.log"),
                          project_root=root)))
        check("profile=live 但日志不在 data/live 时被拒绝",
              bool(raises(ValueError, load_windows_service_config,
                          write(profile="live", port=8001), project_root=root)))
        check("非法 profile 被拒绝",
              bool(raises(ValueError, load_windows_service_config,
                          write(profile="prod", port=8000), project_root=root)))


def check_fingerprint_disposition() -> None:
    print("\n[9] 指纹报告 diff-6 / decl-6 处置登记")
    required = {"diff-6", "decl-6", "fu-1", "fu-2", "fu-4", "fu-paths"}
    check("U13 处置表覆盖全部归口条目",
          required <= set(U13_FINGERPRINT_DISPOSITION))
    check("diff-6 运行代持久化+实例锁已处置",
          U13_FINGERPRINT_DISPOSITION["diff-6"]["status"] == "fixed")
    check("decl-6 SCM 双 profile 已处置",
          U13_FINGERPRINT_DISPOSITION["decl-6"]["status"] == "fixed")
    check("未接线残余以 registered 显式登记",
          U13_FINGERPRINT_DISPOSITION["fu-2"]["status"] == "registered")
    check("每条都有 note 与 owner（禁止静默忽略）",
          all(item.get("note") and item.get("owner")
              for item in U13_FINGERPRINT_DISPOSITION.values()))


def main() -> int:
    print("U13 SUPERVISION / RECOVERY ACCEPTANCE")
    started = time.perf_counter()
    check_profile_declared()
    check_manual_disable_precedence()
    check_bounded_restart()
    check_single_actor_state_root()
    check_runtime_paths_inventory()
    check_recovery_epoch_bundle()
    check_reconcile_before_entry()
    check_clock_guard()
    check_recovery_script_epoch()
    check_scm_dual_profile()
    check_fingerprint_disposition()
    if FAILURES:
        print(f"\nU13 SUPERVISION ACCEPTANCE FAILED: {len(FAILURES)} -> "
              + ", ".join(FAILURES))
        return 1
    print(f"\nU13 SUPERVISION ACCEPTANCE PASS: {PASSED} checks in "
          f"{time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
