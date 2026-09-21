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

U13-FU2 增补（复验 N1/N2 的收口）：

- **N1**：``before_entry()`` 的**裸形态**（未注入 facts）在"门禁状态根没有账本
  文件 / 账本没有 ``order_outbox`` 表"时**不得 fail-open**——读不出账本事实
  一律阻断（``EVIDENCE_UNVERIFIED``）；账本干净时仍然放行（不得误伤）。
- **N2**：``TradingService`` 的**启动**与 ``set_control`` 两条路径都要走
  "注入本进程账本事实"的形态，且启动/set_control 的结果真的受门禁约束
  （账本里有未收口 ENTRY → 启动收紧为 close_only、``set_control('normal')``
  被拒；账本干净 → 不被误伤）。本段会在**隔离临时状态根**里真的构造一次
  ``TradingService``（paper、不联网、不下单、不读真实 ``data``/``.env``）。
- **N2 回归（U13-FU3）**：门禁 ``CLEARED`` 之后账本里出现一颗**非 ENTRY** 的
  未收口单（``purpose='EXIT'``/``status='UNKNOWN'``，已 checkpoint 落主文件）
  时，注入形态必须**与裸形态同结论**（阻断启动 / 停在 ``close_only`` /
  拒绝 ``set_control('normal')``）。U13-FU2 的接线把这两列硬编码成 0（无参
  调用取不到形参），该场景下会**放行**；本段因此加**行为断言**（而不只是
  ``bare == 0`` 的接线 spy），并断言注入事实里 ``unknown_orders >= 1``。

**两态可跑**：不假定本补丁是否已应用——缺符号/缺行为时逐条判失败并给出原因码，
退出码非零。只写系统临时目录：不读 ``.env``/真实 ``data``/密钥、不联网、
不下单、不动风险档、只在 N2 段构造一次隔离服务实例。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
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

import core.ops.recovery as _recovery  # noqa: E402

#: 补丁未应用时缺失的符号（逐条判失败，不 ImportError 崩掉整个脚本）。
MISSING_SYMBOLS: list[str] = []


def _sym(name: str, fallback: object = None) -> object:
    value = getattr(_recovery, name, None)
    if value is None:
        MISSING_SYMBOLS.append(name)
        return fallback
    return value


GATE_CLEARED = _sym("GATE_CLEARED", "")
RECONCILE_REQUIRED_BEFORE_ENTRY = _sym("RECONCILE_REQUIRED_BEFORE_ENTRY", "")
RecoveryGate = _sym("RecoveryGate")
create_epoch_bundle = _sym("create_epoch_bundle")
drill_epoch_bundle = _sym("drill_epoch_bundle")
gate_install_marker = _sym("gate_install_marker")

#: 布局口径（P1-2）。补丁未应用时退回"旧实现实际用的落点"，这样本脚本仍能
#: 观察到"门禁不在状态根 / 状态文件不在状态根"这些**缺陷本身**并判失败，
#: 而不是因为缺符号直接崩掉——这正是"两态可跑"的要求。
restored_state_root = _sym(
    "restored_state_root",
    lambda target, _profile: Path(target) / "data")
restored_gate_path = _sym(
    "restored_gate_path",
    lambda target, profile: restored_state_root(target, profile) / "ops" / "recovery_gate.json")
ledger_reconcile_facts = _sym("ledger_reconcile_facts", lambda _db: None)
facts_digest = _sym("facts_digest", lambda _facts: "UNAVAILABLE")

if RecoveryGate is None or drill_epoch_bundle is None or create_epoch_bundle is None:
    print("FATAL core.ops.recovery 缺少验收所需的基础符号（RecoveryGate/drill/create）")
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
        if gate_file.is_file():
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


def _make_ledger(root: Path, kind: str) -> None:
    """按形态造"门禁所在状态根"的账本文件（只在本脚本的临时目录里）。

    ``none`` = 没有账本文件；``no_outbox`` = 账本在但没有 ``order_outbox`` 表
    （``ledger_reconcile_facts`` 判"不适用"）；``dirty`` = 有未收口 ENTRY 单；
    ``clean`` = ``order_outbox`` 为空。
    """
    if kind == "none":
        return
    connection = sqlite3.connect(root / "agent_trader.sqlite")
    try:
        if kind == "no_outbox":
            connection.execute("CREATE TABLE risk_events(ts REAL, event_type TEXT)")
        else:
            connection.execute(
                "CREATE TABLE order_outbox(status TEXT, purpose TEXT, environment TEXT)")
            if kind == "dirty":
                connection.execute(
                    "INSERT INTO order_outbox VALUES('SUBMITTING','ENTRY','demo')")
        connection.commit()
    finally:
        connection.close()


def check_n1_bare_ledger_failclosed(tmp: Path) -> None:
    """N1：裸 ``before_entry()`` 读不出账本事实时**不得**放行（U13-FU2）。"""
    print("\n[N1] 裸形态读不出账本事实时不得 fail-open（U13-FU2）")
    expected = (
        ("none", True, "EVIDENCE_UNVERIFIED"),
        ("no_outbox", True, "EVIDENCE_UNVERIFIED"),
        ("dirty", True, "RECONCILE_REQUIRED_BEFORE_ENTRY"),
        ("clean", False, "GATE_CLEARED"),
    )
    for kind, expect_block, expect_code in expected:
        root = tmp / f"n1_{kind}"
        (root / "ops").mkdir(parents=True, exist_ok=True)
        _make_ledger(root, kind)
        gate_file = root / "ops" / "recovery_gate.json"
        facts = {"unknown_orders": 0, "submitting_orders": 0, "unresolved_entry_orders": 0}
        # 手写一份**自洽摘要**的 CLEARED 门禁（= N1 的绕过形态）。
        _forged_gate(gate_file,
                     evidence={"facts": facts, "facts_digest": facts_digest(facts)})
        raw = ledger_reconcile_facts(root / "agent_trader.sqlite")
        decision = RecoveryGate.load(gate_file).before_entry()
        print(f"  [{kind}] ledger_reconcile_facts={raw}")
        print(f"           before_entry() -> allowed={decision.allowed} "
              f"codes={decision.reason_codes}")
        check(f"[{kind}] 裸形态阻断={expect_block}（期望码 {expect_code}）",
              bool(not decision.allowed) == expect_block
              and (not expect_block or expect_code in decision.reason_codes),
              f"allowed={decision.allowed} codes={decision.reason_codes}")
    # 注入形态同样不接受"不适用"（读不出事实的另一种写法）。
    root = tmp / "n1_injected"
    (root / "ops").mkdir(parents=True, exist_ok=True)
    _make_ledger(root, "clean")
    gate_file = root / "ops" / "recovery_gate.json"
    facts = {"unknown_orders": 0, "submitting_orders": 0, "unresolved_entry_orders": 0}
    _forged_gate(gate_file, evidence={"facts": facts,
                                      "facts_digest": facts_digest(facts)})
    injected = RecoveryGate.load(gate_file).before_entry(
        facts={**facts, "source": "not_applicable"})
    check("[injected:not_applicable] 注入'不适用'事实也不得放行",
          not injected.allowed, f"allowed={injected.allowed} codes={injected.reason_codes}")


def _build_service_root(base: Path, name: str, *, mode: str) -> Path:
    """造一个"已恢复且已装 CLEARED 门禁"的隔离状态根（镜像生产恢复布局）。

    ``mode``：

    - ``dirty_wal``：一颗 SUBMITTING **ENTRY** 单只提交进 ``-wal``，且**不带
      -shm** 地拷回：门禁自己的 ``immutable=1`` 只读快照看不到它，而服务本进程
      正常打开的连接会重放 ``-wal`` 看到它 —— 两者在同一份账本上结论相反，
      正是 N2 要接的线。
    - ``clean``：``order_outbox`` 为空（对照，不得误伤）。
    - ``unknown_exit``：门禁**已 CLEARED 之后**再出现一颗 ``purpose='EXIT'``/
      ``status='UNKNOWN'`` 的未收口单，并 checkpoint 落进主文件（= 复验者 Q
      推翻 N2 的那个场景）：门禁自己的账本读取是**不分 purpose** 全表计数，
      所以裸形态阻断；注入形态若只数 ENTRY、把 unknown/submitting 硬编码成 0
      就会放行（U13-FU3 修掉的回归）。
    """
    iso = base / name
    data = iso / "data"
    (data / "ops").mkdir(parents=True, exist_ok=True)

    db = AuditDatabase(data / "agent_trader.sqlite")
    db.environment = "paper"
    saved: Path | None = None
    if mode == "dirty_wal":
        saved = base / f"{name}_staging"
        saved.mkdir(parents=True, exist_ok=True)
        db.conn.execute("PRAGMA journal_mode=WAL")
        outbox = OrderOutbox(db, environment="paper")
        outbox.prepare(
            client_order_id="ENTRY-N2-1", symbol="BTCUSDT", side="BUY",
            position_side="LONG", order_type="MARKET", purpose="ENTRY",
            qty=0.010, price=None, reduce_only=False, endpoint="/fapi/v1/order",
            request={}, command_kind="STANDARD", qty_text="0.010")
        db.conn.commit()
        for member in ("agent_trader.sqlite", "agent_trader.sqlite-wal"):
            shutil.copy2(data / member, saved / member)
    db.conn.commit()
    db.close()
    if saved is not None:
        for member in ("agent_trader.sqlite", "agent_trader.sqlite-wal"):
            shutil.copy2(saved / member, data / member)

    for state_name, payload in {
        "capital_state.json": {"mode": "paper"},
        "channel_state.json": {"date": "2026-09-20"},
        "control_state.json": {"state": "normal"},
        "account_risk_state.json": {"anchor_id": "paper:u13fu2"},
    }.items():
        (data / state_name).write_text(json.dumps(payload), encoding="utf-8")

    now_ms = int(time.time() * 1000)
    RecoveryGate.load(data / "ops" / "recovery_gate.json").require_reconcile(
        epoch_id="verify-epoch", account_ref="", runtime_instance_id="")
    RecoveryGate.load(data / "ops" / "recovery_gate.json").complete_reconcile(
        {"observed_at_ms": now_ms, "positions_verified": True,
         "open_orders_verified": True}, now_ms=now_ms)

    if mode == "unknown_exit":
        # 门禁**已经 CLEARED 之后**才出现的未收口单：一颗**非 ENTRY** 的
        # EXIT/UNKNOWN（复验者 Q 的场景）。checkpoint 落进主文件，因此门禁
        # 自己的只读账本读取也看得见它（不分 purpose 计数 → 阻断）。
        connection = sqlite3.connect(data / "agent_trader.sqlite")
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "INSERT INTO order_outbox (client_order_id, environment, "
                "business_key, symbol, side, position_side, order_type, purpose, "
                "qty, price, reduce_only, endpoint, status, request_json, "
                "attempt_count, created_at, updated_at) VALUES "
                "('EXIT-N2-1','paper','EXIT:BTCUSDT:LONG','BTCUSDT','SELL',"
                "'LONG','MARKET','EXIT',0.010,NULL,1,'/fapi/v1/order','UNKNOWN',"
                "'{}',1,'2026-09-21T00:00:00Z','2026-09-21T00:00:00Z')")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    return iso


def _drive_service(iso: Path, tree: Path, settings_cls: object,
                   service_cls: object) -> dict:
    """在隔离状态根上真的构造一次 ``TradingService`` 并驱动两条路径。"""
    import os

    import yaml

    os.environ["EXECUTION_MODE"] = "paper"
    os.environ.pop("MTA_STATE_ROOT", None)
    os.environ.pop("MTA_DATA_ROOT", None)
    settings = settings_cls(
        tree,
        yaml.safe_load((tree / "config/settings.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((tree / "config/agents.yaml").read_text(encoding="utf-8")),
        yaml.safe_load((tree / "config/risk_learning.yaml").read_text(encoding="utf-8")),
    )
    settings.project_root = iso

    calls: list[object] = []
    original = RecoveryGate.before_entry

    def spy(self, *args, **kwargs):  # noqa: ANN001
        calls.append(kwargs.get("facts"))
        return original(self, *args, **kwargs)

    RecoveryGate.before_entry = spy
    try:
        service = service_cls(settings)
    finally:
        RecoveryGate.before_entry = original
    startup_bare = sum(1 for facts in calls if facts is None)
    startup_injected = [facts for facts in calls if facts is not None]

    calls.clear()
    RecoveryGate.before_entry = spy
    try:
        set_result = service.set_control("normal")
    finally:
        RecoveryGate.before_entry = original
    set_bare = sum(1 for facts in calls if facts is None)
    set_injected = [facts for facts in calls if facts is not None]
    try:
        service.close()
    except Exception:  # pragma: no cover - 清理失败不影响判定
        pass
    def _max_fact(key: str, batches: list) -> int | None:
        """注入事实里某一列的最大值（没注入 → None）。"""
        values = [int(facts.get(key) or 0) for facts in batches
                  if isinstance(facts, dict) and key in facts]
        return max(values) if values else None

    return {"control": service.control.state,
            "gate_allowed": service._entry_readiness_facts.get("recovery_gate_allowed"),
            "startup_bare": startup_bare, "startup_injected": startup_injected,
            "startup_unknown": _max_fact("unknown_orders", startup_injected),
            "startup_submitting": _max_fact("submitting_orders", startup_injected),
            "set_result": set_result, "set_bare": set_bare,
            "set_injected": set_injected,
            "set_unknown": _max_fact("unknown_orders", set_injected)}


def check_n2_service_paths(tmp: Path) -> None:
    """N2：服务启动与 ``set_control`` 两条路径都注入账本事实（U13-FU2）。"""
    print("\n[N2] service 启动 / set_control 注入账本事实（U13-FU2）")
    try:
        from core.config_loader import Settings
        from core.service import TradingService
    except ImportError as exc:  # pragma: no cover
        check("N2 段可导入 Settings/TradingService", False, str(exc))
        return
    for name in ("dirty_wal", "clean", "unknown_exit"):
        try:
            iso = _build_service_root(tmp, name, mode=name)
            observed = _drive_service(iso, ROOT, Settings, TradingService)
        except Exception as exc:
            check(f"[{name}] 隔离服务可构造并驱动（paper/离线）", False,
                  f"{type(exc).__name__}: {exc}")
            continue
        print(f"  [{name}] control={observed['control']} "
              f"gate_allowed={observed['gate_allowed']} "
              f"startup_bare={observed['startup_bare']} "
              f"startup_injected={observed['startup_injected']} "
              f"set_control_bare={observed['set_bare']} "
              f"set_control_injected={observed['set_injected']} "
              f"set_control={observed['set_result']}")
        check(f"[{name}] 启动路径注入账本事实（无裸 before_entry 调用）",
              observed["startup_bare"] == 0 and bool(observed["startup_injected"]),
              f"bare={observed['startup_bare']} injected={observed['startup_injected']}")
        check(f"[{name}] set_control 路径注入账本事实",
              observed["set_bare"] == 0 and bool(observed["set_injected"]),
              f"bare={observed['set_bare']} injected={observed['set_injected']}")
        if name == "clean":
            check("[clean] 账本干净 → 启动不被误伤（控制状态保持 normal）",
                  observed["control"] == "normal", observed["control"])
            check("[clean] 账本干净 → set_control('normal') 成功",
                  observed["set_result"].get("ok") is True, str(observed["set_result"]))
            continue
        if name == "dirty_wal":
            check("[dirty_wal] 账本有未收口 ENTRY → 启动收紧为 close_only 且门禁判定阻断",
                  observed["control"] == "close_only"
                  and observed["gate_allowed"] is False,
                  f"control={observed['control']} allowed={observed['gate_allowed']}")
        else:
            # U13-FU3/N2 回归：注入事实必须**与裸形态同结论**。硬编码 0 的旧接线
            # 会在这里注入 unknown_orders=0（甚至注入事实与账本矛盾），启动因此
            # 被放行；断言注入值本身，可以在行为断言之外直接钉住"这两列读了账本"。
            check("[unknown_exit] 注入事实里的 unknown_orders 来自账本（>=1，"
                  "非 ENTRY 未收口单也计入）",
                  (observed["startup_unknown"] or 0) >= 1
                  and (observed["set_unknown"] or 0) >= 1,
                  f"startup_unknown={observed['startup_unknown']} "
                  f"set_unknown={observed['set_unknown']} "
                  f"startup_injected={observed['startup_injected']}")
            check("[unknown_exit] 账本里只有非 ENTRY 未收口单 → 启动收紧为 "
                  "close_only（与裸形态同结论）",
                  observed["control"] == "close_only",
                  f"control={observed['control']} allowed={observed['gate_allowed']}")
            check("[unknown_exit] 冷启动就绪快照的门禁判定也阻断"
                  "（recovery_gate_allowed is False）",
                  observed["gate_allowed"] is False,
                  f"allowed={observed['gate_allowed']} control={observed['control']}")
        check(f"[{name}] set_control('normal') 被门禁拒绝",
              observed["set_result"].get("ok") is False
              and "recovery gate blocked" in str(observed["set_result"].get("error")),
              str(observed["set_result"]))


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="u13f1-gate-"))
    print(f"U13-FU1 verify tmp = {tmp}")
    if MISSING_SYMBOLS:
        # 不是崩溃：显式登记"补丁未应用"，并让每个受影响的小节继续跑出可读结论。
        print(f"  NOTE  缺失符号（视作补丁未应用）: {', '.join(sorted(set(MISSING_SYMBOLS)))}")
        NOTES.append(f"missing symbols: {', '.join(sorted(set(MISSING_SYMBOLS)))}")
    try:
        check_p1_1_and_p1_2(tmp)
        check_p1_3(tmp)
        check_p1_4(tmp)
        check_n1_bare_ledger_failclosed(tmp)
        check_n2_service_paths(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nU13-FU1/U13-FU2 RECOVERY GATE: {PASS} 通过 / {len(FAIL)} 失败")
    for name in FAIL:
        print(f"  FAILED: {name}")
    for text in NOTES:
        print(f"  NOTE: {text}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
