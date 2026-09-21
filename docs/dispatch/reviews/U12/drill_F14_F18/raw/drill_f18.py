"""W3 独立演练 F18：新代码目录+旧状态根 / PID 复用 / 账户-profile 互斥 / 迁移可回滚。

全程只写 TEMP 目录，不读 .env/真实 data/密钥，不联网，不下单，不动风险档。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

DRILL_ROOT = Path(__file__).resolve()
PROJECT_ROOT = DRILL_ROOT.parents[6]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from audit.migrations import apply_migration  # noqa: E402
from core.db_backup import DatabaseBackupManager  # noqa: E402
from core.instance_lock import InstanceLock  # noqa: E402
from core.ops.recovery import create_epoch_bundle, check_writer_stopped  # noqa: E402
from core.ops.supervision import lock_scope_report, load_or_mint_run_record  # noqa: E402
from core.runtime_identity import ENVIRONMENT_STATE_ROOT_MISMATCH  # noqa: E402
from core.runtime_paths import (  # noqa: E402
    STATE_ROOT_MISSING,
    RuntimePaths,
    RuntimePathsError,
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


def raises_text(kind, fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except kind as exc:
        return f"{type(exc).__name__}: {exc}"
    except BaseException as exc:  # noqa: BLE001
        return f"WRONG_TYPE {type(exc).__name__}: {exc}"
    return ""


def seed_state(data_dir: Path, *, mode: str = "demo") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    db = AuditDatabase(data_dir / "agent_trader.sqlite")
    db.conn.commit()
    db.conn.close()
    for name, payload in {
        "capital_state.json": {"mode": mode},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": f"{mode}:drill"},
    }.items():
        (data_dir / name).write_text(json.dumps(payload), encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="w3-f18-drill-"))
    print(f"drill tmp = {tmp}")
    state_base = tmp / "state_base"
    code_a = tmp / "code_A"
    code_b = tmp / "code_B"
    for d in (code_a, code_b):
        d.mkdir(parents=True)
    seed_state(state_base)

    # =====================================================================
    print("\n[A] 新代码目录 + 旧状态根（换代码根不得换状态根/身份）")
    pa = RuntimePaths.resolve(code_a, profile="demo", state_root=state_base)
    pb = RuntimePaths.resolve(code_b, profile="demo", state_root=state_base)
    ra, rb = lock_scope_report(pa), lock_scope_report(pb)
    print(f"  code_A lock={ra['lock_file']}\n  code_B lock={rb['lock_file']}")
    check("两个代码根 + 同一显式状态根 → 同一把锁（单账户互斥不被换目录绕过）",
          ra["lock_file"] == rb["lock_file"])
    check("两个代码根 → 同一 lock_scope（作用域证据一致）",
          ra["lock_scope"] == rb["lock_scope"])
    check("两个代码根 → 同一状态基座", ra["state_base"] == rb["state_base"])

    rec_a, new_a = load_or_mint_run_record(pa, environment="demo",
                                           account_ref="demo:acct-A")
    rec_b, new_b = load_or_mint_run_record(pb, environment="demo",
                                           account_ref="demo:acct-A")
    print(f"  A: iid={rec_a.runtime_instance_id[:12]} gen={rec_a.generation_id[:12]} new={new_a}")
    print(f"  B: iid={rec_b.runtime_instance_id[:12]} gen={rec_b.generation_id[:12]} new={new_b}")
    check("换代码根后沿用同一 runtime_instance_id（未新建错误基线）",
          rec_a.runtime_instance_id == rec_b.runtime_instance_id)
    check("换代码根后沿用同一 generation_id（运行代连续）",
          rec_a.generation_id == rec_b.generation_id and new_b is False)

    # 未声明状态根（旧布局）：两个代码根 → 两套状态根（漂移的本体）
    legacy_a = RuntimePaths.resolve(code_a, profile="demo")
    legacy_b = RuntimePaths.resolve(code_b, profile="demo")
    check("未声明状态根时两个代码根得到不同锁（这正是 F18 的漂移面）",
          legacy_a.lock_file != legacy_b.lock_file)
    reasons = legacy_a.validate("demo")
    print(f"  legacy_a.state_base_declared={legacy_a.state_base_declared!r} "
          f"source={legacy_a.state_base_source} explicit={legacy_a.state_base_explicit}")
    print(f"  legacy_a.validate('demo') = {reasons}")
    # 【缺陷确认】fallback 分支把字面量 "data" 当作"已声明"，于是
    # STATE_ROOT_MISSING 这条"启动期前置检查"在正常构造路径下永不可达。
    check("[缺陷] 未声明状态根时 validate() 为空（STATE_ROOT_MISSING 不可达）",
          reasons == () and STATE_ROOT_MISSING not in reasons, str(reasons))
    check("[缺陷] 未声明状态根时 assert_environment_bound 不抛错（demo 无拦截）",
          not raises_text(RuntimePathsError, legacy_a.assert_environment_bound, "demo"))
    reg_a, _ = load_or_mint_run_record(legacy_a, environment="demo",
                                       account_ref="demo:acct-A")
    reg_b, _ = load_or_mint_run_record(legacy_b, environment="demo",
                                       account_ref="demo:acct-A")
    print(f"  legacy A iid={reg_a.runtime_instance_id[:12]} / B iid={reg_b.runtime_instance_id[:12]}")
    check("[缺陷] 未声明状态根时两个代码根各自新建基线（不同运行代）",
          reg_a.runtime_instance_id != reg_b.runtime_instance_id
          and legacy_a.run_registry_file != legacy_b.run_registry_file)

    # 环境 ↔ 状态根：live 无 profile 必须被拦
    live_noprofile = RuntimePaths.resolve(code_a, profile=None, state_root=state_base)
    print(f"  live+profile=None reasons = {live_noprofile.validate('live')}")
    check("EXECUTION_MODE=live 但无 profile → 被拦（STATE_ROOT_PROFILE_UNLOCKED）",
          "STATE_ROOT_PROFILE_UNLOCKED" in live_noprofile.validate("live"))
    mismatch = RuntimePaths.resolve(code_a, profile="live", state_root=state_base)
    check("environment=demo 与 profile=live 冲突 → ENVIRONMENT_STATE_ROOT_MISMATCH",
          ENVIRONMENT_STATE_ROOT_MISMATCH in mismatch.validate("demo"))

    # =====================================================================
    print("\n[B] PID 复用与单实例互斥")
    lock_path = pa.lock_file
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # B1: 锁文件里的 PID 是**本进程**（=旧 PID 被新进程复用的等价场景）
    lock_path.write_text(json.dumps({"pid": os.getpid(), "thread": 1,
                                     "started_at": "x", "runtime_instance_id": "old"}),
                         encoding="utf-8")
    txt = raises_text(RuntimeError, InstanceLock(lock_path).acquire)
    print(f"  B1 acquire -> {txt}")
    check("B1 锁文件 PID = 当前进程 PID 时仍拒绝启动（不把新进程误认成自己）",
          bool(txt) and "已在运行" in txt, txt or "(no raise)")
    check("B1 被拒后锁文件未被删除（没有踩掉别人的锁）", lock_path.is_file())

    # B2: 锁文件 PID 已死 → 允许接管
    dead = 0x7FFFFFF0  # 极不可能是活进程
    lock_path.write_text(json.dumps({"pid": dead, "started_at": "x"}), encoding="utf-8")
    lk = InstanceLock(lock_path)
    ok2 = raises_text(RuntimeError, lk.acquire)
    check("B2 确认死亡的陈旧 PID 锁可安全接管", not ok2, ok2 or "(raised)")
    if lk._owned:
        lk.release()

    # B3: 锁文件为空/不可解析（create→write 之间的窗口，或半截写）
    race = tmp / "race"
    race.mkdir()
    r_lock = race / "trading_service.lock"
    # 确定性重放：持有者刚用 O_EXCL 建好文件、还没写入
    fd = os.open(str(r_lock), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    os.close(fd)
    size = r_lock.stat().st_size
    intruder = InstanceLock(r_lock)
    b3 = raises_text(RuntimeError, intruder.acquire)
    print(f"  B3 empty-lock(size={size}) acquire -> {b3 or 'SUCCEEDED'}")
    check("B3 空锁文件（O_EXCL 建好未写入的窗口）被当成陈旧锁 → 第二个实例接管 —— 绕过成功",
          (not b3) and intruder._owned, b3 or "not owned")

    # B4: 同一路径上的 check_writer_stopped 对不可解析锁静默放行
    rp = RuntimePaths.resolve(race, profile=None, state_root=race)
    rp.data_dir.mkdir(parents=True, exist_ok=True)
    r_lock.write_text("{corrupt", encoding="utf-8")
    cws = raises_text(Exception, check_writer_stopped, rp)
    check("B4 check_writer_stopped 对损坏锁不抛错（静默认为无写者）", not cws, cws)
    print(f"  B4 check_writer_stopped(corrupt lock) -> {cws or 'returned None (treated as stopped)'}")

    # B5: 由此 create_epoch_bundle 会在"锁损坏"时照常建包
    seed_state(rp.data_dir)
    r_lock.write_text("{corrupt", encoding="utf-8")
    b5 = raises_text(Exception, create_epoch_bundle, rp, race / "bundles",
                     environment="demo")
    check("B5 损坏锁下仍能建恢复包（写者是否真停无法证明）", not b5, b5)
    print(f"  B5 create_epoch_bundle(corrupt lock) -> {b5 or 'CREATED'}")

    # =====================================================================
    print("\n[C] 两账户同 profile / 同账户不同 profile")
    same_profile = RuntimePaths.resolve(code_a, profile="demo", state_root=state_base)
    rec1, _ = load_or_mint_run_record(same_profile, environment="demo",
                                      account_ref="demo:acct-A")
    rec2, new2 = load_or_mint_run_record(same_profile, environment="demo",
                                         account_ref="demo:acct-B")
    print(f"  acct-A iid={rec1.runtime_instance_id[:12]}")
    print(f"  acct-B iid={rec2.runtime_instance_id[:12]} new={new2}")
    check("两账户同一 profile 共用同一把锁（进程级互斥成立）",
          same_profile.lock_file == pa.lock_file)
    check("两账户在同一状态根上身份不同（account_ref 参与身份派生）",
          rec1.runtime_instance_id != rec2.runtime_instance_id)
    reg = json.loads(same_profile.run_registry_file.read_text(encoding="utf-8"))
    print(f"  registry after B: account_ref={reg['account_ref']}")
    check("后启动的账户**覆盖**了前一个账户的运行代基线（同一状态根无账户分区）",
          reg["account_ref"] == "demo:acct-B")
    reread, _ = load_or_mint_run_record(same_profile, environment="demo",
                                        account_ref="demo:acct-A")
    check("前一个账户再启动会拿到**新的** runtime_instance_id（基线被改写）",
          reread.runtime_instance_id != rec1.runtime_instance_id)

    live_paths = RuntimePaths.resolve(code_a, profile="live", state_root=state_base)
    demo_paths = RuntimePaths.resolve(code_a, profile="demo", state_root=state_base)
    print(f"  demo lock = {demo_paths.lock_file}")
    print(f"  live lock = {live_paths.lock_file}")
    check("同账户不同 profile → 不同锁文件（两个 profile 可并行，不是同账户互斥）",
          demo_paths.lock_file != live_paths.lock_file)
    check("demo/live 共享出口根唯一（限流冷却与 LLM 准入共用）",
          demo_paths.shared_egress_root_is_unique()
          and demo_paths.shared_egress_root == live_paths.shared_egress_root)

    # =====================================================================
    print("\n[D] 迁移推进后可回退、回退后不损坏")
    mdir = tmp / "mig"
    mdir.mkdir()
    db = AuditDatabase(mdir / "agent_trader.sqlite")
    versions = [r["version"] for r in db.fetch_all("SELECT version FROM schema_migrations")]
    print(f"  current versions: {versions[0]}..{versions[-1]} ({len(versions)})")
    check("全新库迁移到唯一版本序列", versions == list(range(1, len(versions) + 1)))
    db.log_risk_event(event_type="rollback_probe", severity="info",
                      details="survive rollback", action_taken="none")
    db.close()

    # 回退：把库复制成"旧版本"副本（删掉高版本登记行），再次打开必须能重新推进
    rolled = mdir / "rolled_back.sqlite"
    shutil.copy2(mdir / "agent_trader.sqlite", rolled)
    raw = sqlite3.connect(rolled)
    raw.execute("DELETE FROM schema_migrations WHERE version > 9")
    raw.commit()
    raw.close()
    reopened = AuditDatabase(rolled)
    v_after = [r["version"] for r in reopened.fetch_all("SELECT version FROM schema_migrations")]
    qc = reopened.conn.execute("PRAGMA quick_check").fetchone()[0]
    survived = reopened.fetch_one(
        "SELECT COUNT(*) c FROM risk_events WHERE event_type='rollback_probe'")["c"]
    print(f"  after re-migrate: versions={v_after[0]}..{v_after[-1]} quick_check={qc} probe={survived}")
    check("回退到 v9 后重新推进：版本序列仍唯一且到顶",
          v_after == list(range(1, len(v_after) + 1)) and v_after[-1] == versions[-1])
    check("回退再推进后 quick_check=ok（不损坏）", str(qc).lower() == "ok")
    check("回退再推进不丢数据（业务行仍在）", survived == 1)
    reopened.close()

    # 迁移执行器：动作失败必须整笔回滚且不登记版本
    rpath = mdir / "txn.sqlite"
    raw = sqlite3.connect(rpath)
    raw.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT)")
    raw.execute("CREATE TABLE probe (value TEXT)")
    raw.commit()

    def failing_action() -> None:
        raw.execute("INSERT INTO probe(value) VALUES('must_rollback')")
        raise RuntimeError("injected")

    err = raises_text(RuntimeError, apply_migration, raw, 99, "failing",
                      failing_action, applied_at="2026-09-21T00:00:00Z")
    check("迁移动作失败向上抛出", bool(err), err or "(no raise)")
    check("失败迁移整笔回滚（probe 无残留）",
          raw.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0)
    check("失败版本未登记",
          raw.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=99").fetchone()[0] == 0)
    raw.close()

    # 备份即回滚点：备份文件可独立打开且版本一致
    bdir = tmp / "backups"
    db2 = AuditDatabase(mdir / "live.sqlite")
    mgr = DatabaseBackupManager(db2, bdir, interval_hours=24, keep_days=7)
    st = mgr.maybe_backup(force=True)
    bfile = bdir / str(st["last_file"])
    bconn = sqlite3.connect(str(bfile))
    try:
        bqc = bconn.execute("PRAGMA quick_check").fetchone()[0]
        bver = bconn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    finally:
        bconn.close()
    print(f"  backup={bfile.name} quick_check={bqc} schema={bver}")
    check("在线备份可作为迁移回滚点（独立可打开 + quick_check=ok）",
          str(bqc).lower() == "ok" and int(bver) == versions[-1])
    db2.close()

    print(f"\nF18 DRILL 结果: {PASS} 通过 / {len(FAIL)} 失败")
    for name in FAIL:
        print(f"  FAILED: {name}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
