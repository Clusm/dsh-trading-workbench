"""把 patch 03（service.py 接线）重写到**当前集成 HEAD** 的 service.py 上。

集成 HEAD 里 U13 已落地持久化运行代（``self.run_generation`` / RunRegistry），
因此 U02 的账户域绑定必须**复用**那份身份证据，而不是另铸一个（U15/B_r1 P1-4）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "r2" / "sb_gen"
SVC = "src/core/service.py"


def patch(old: str, new: str, *, count: int = 1) -> None:
    path = ROOT / SVC
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    assert found == count, f"{SVC}: 期望 {count} 次，实际 {found} 次\n---\n{old[:240]}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"OK {old.strip().splitlines()[0][:70]}")


# S1. 导入。
patch("""from core.risk_guard import AccountRiskGuard
from core.release_manifest import build_release_manifest, write_manifest
""", """from core.risk_guard import AccountRiskGuard
from core.management_authorization import (
    ManagementAccountDomain,
    assert_account_domains_consistent,
    ledger_account_ref,
    resolve_account_domain,
)
from core.release_manifest import build_release_manifest, write_manifest
from core.runtime_identity import derive_account_ref, mint_runtime_generation
from core.strategy_position_manager import StrategyPositionManager
""")

# S2. 账户域绑定 + 管理器注入。
patch("""        self.paper_broker = self.broker  # 兼容旧名称
""", """        # U02：把本次运行的"身份域"与"账本域"对齐，并把管理授权/策略仓管理接到
        # 执行路径上。账户引用**只**取账本侧那一个值（``reconciliation_book`` /
        # release_manifest 的结论），因此 K03 幂等键、生命周期分区与决策身份指向
        # 同一个账户域（U15/B_r1 P1-4）。
        self.account_domain = self._bind_account_domain(
            broker_environment, broker_account_ref, reconciliation_book)
        self.strategy_position_manager = StrategyPositionManager(
            account_ref=self.account_domain.account_ref,
            environment=self.account_domain.environment,
        )
        # Broker 若支持 U02 接入点则注入管理组件；没有该属性的替身/纸面 Broker
        # 不受影响（``_reserve_exit`` 用 getattr 探测）。**不改写** broker 的
        # account_ref：账本侧账户引用由 trade_reconciliation/binance_futures 的
        # 单一派生点持有，这里再造一个值只会制造第二个账户标识。
        try:
            self.broker.strategy_position_manager = self.strategy_position_manager
        except Exception:  # noqa: BLE001 - 替身 Broker 不可写时忽略
            pass
        self.paper_broker = self.broker  # 兼容旧名称
""")

# S3. 方法体。
patch("""    def _purge_old_logs(self, keep_days: int = 7) -> None:""", '''    def _plan_position_restart(self) -> dict[str, Any]:
        """重启/接手的仓位分类计划；结构上不含任何退出动作（U02 必达要点 2）。

        只在**旧仓集合发生变化**时告警（旧仓存续期间每轮一条 warn 会让
        ``risk_events`` 膨胀并污染看板）。已显式迁移过的生命周期不会再次被
        ``plan_restart`` 标记为旧仓（迁移结果不被下一轮撤销）。
        """
        state = self.broker.state if isinstance(self.broker.state, dict) else {}
        positions = [dict(item) for item in (state.get("positions") or [])]
        plan = self.strategy_position_manager.plan_restart(
            positions, now_ms=int(time.time() * 1000))
        # 护栏：重启上下文一旦出现批量退出动作即 fail-closed。传入的是**真实**
        # 产出的动作（不是字面空元组），因此"零退出动作"由构造+护栏共同保证。
        self.strategy_position_manager.assert_no_implicit_bulk_exit(
            context="restart", actions=plan.as_dict()["actions"])
        legacy_keys = {item.lifecycle_id or item.symbol for item in plan.legacy}
        reported = getattr(self, "_legacy_reported", None)
        if reported is None:
            reported = self._legacy_reported = set()
        newly = sorted(legacy_keys - reported)
        reported.intersection_update(legacy_keys)
        reported.update(legacy_keys)
        if newly:
            self.db.log_risk_event(
                event_type="legacy_position_requires_migration", severity="warn",
                details=(f"{len(plan.legacy)} 个旧仓无策略归属（新增 {len(newly)} 个："
                         f"{','.join(newly)}），需人工显式迁移；重启不会自动退出它们"),
                action_taken="mark for explicit migration")
        return plan.as_dict()

    @property
    def hard_exits(self) -> tuple[Any, ...]:
        """硬退出流水只读视图（保护触发/人工急平），与预期持仓分开。"""
        return self.strategy_position_manager.hard_exits()

    def _bind_account_domain(
        self, environment: str, ledger_ref: str | None, reconciliation_book: Any,
    ) -> ManagementAccountDomain:
        """把 U15 运行代与账本账户引用对齐；不一致必须**可见**（不是静默）。

        U13 已把运行代持久化（``self.run_generation``）：这里**复用**那份身份
        证据（同一环境/账户/实例跨重启稳定），而不是另铸一个，避免同进程内出现
        两个账户标识。运行代不可用时才按环境+账本引用退化铸造，并让降级在
        ``ManagementAccountDomain.reason_codes`` 里可见。

        fail-visible 而非 fail-open：不一致时记一条 critical/warn 事件（发布证据里
        K01 身份与 K03 收入幂等键分区对不上），但**不改**交易风险档、不停机、不
        阻断已在途的退出——重分区数据属显式迁移，由运营者在本模块之外执行
        （**迁移完成前不得在生产启用 MTA_ACCOUNT_REF**，见 U02 补丁包 README）。
        """
        declared = str(os.getenv("MTA_ACCOUNT_REF") or "").strip() or None
        ledger = str(ledger_ref or f"{environment}:local")
        record = getattr(self, "run_generation", None)
        try:
            if record is None or not getattr(record, "runtime_instance_id", None):
                raise ValueError("runtime generation unavailable")
            generation = mint_runtime_generation(
                environment=str(getattr(record, "environment", "") or environment),
                account_ref=str(getattr(record, "account_ref", "") or ledger),
                runtime_instance_id=str(record.runtime_instance_id),
                credential_epoch=int(getattr(record, "credential_epoch", 0) or 0),
                key_fingerprint_value=getattr(record, "key_fingerprint", None),
                scope_hash=getattr(record, "scope_hash", None),
                started_at=getattr(record, "started_at", None),
            )
        except Exception as exc:  # noqa: BLE001 - 身份证据不可用时降级且可见
            try:
                generation = mint_runtime_generation(
                    environment=environment,
                    account_ref=derive_account_ref(
                        environment, declared_account_ref=declared or ledger).value,
                    runtime_instance_id=str(
                        getattr(self, "runtime_instance_id", "") or "unbound"),
                )
            except Exception as exc2:  # pragma: no cover - 环境非法时不再铸造
                self.db.log_risk_event(
                    event_type="account_domain_mismatch", severity="warn",
                    details=(f"运行代不可用（{type(exc).__name__}/"
                             f"{type(exc2).__name__}），账户域未证明；"
                             f"账本域={ledger}"),
                    action_taken="record only; no risk profile change")
                return resolve_account_domain(None)
        domain = resolve_account_domain(generation)
        mismatches = assert_account_domains_consistent(generation, ledger)
        # 单一事件类型：只在 details/severity 上区分"已声明却分叉"（critical，需迁移）
        # 与"未声明"（warn，建议声明），避免为同一语义新增两个看板映射。
        if mismatches and declared:
            self.db.log_risk_event(
                event_type="account_domain_mismatch", severity="critical",
                details=(f"身份域={ledger_account_ref(generation)} 账本域={ledger}；"
                         "K03 幂等键分区与 K01 身份不一致，需显式迁移账户引用（U02/U13）"),
                action_taken="record only; no risk profile change")
        elif not declared:
            self.db.log_risk_event(
                event_type="account_domain_mismatch", severity="warn",
                details=(f"account_ref={ledger} 未显式声明（依赖密钥摘要或 "
                         "``<env>:local`` 回退），轮换/多账户下 K03 分区可能切裂；"
                         "建议声明 MTA_ACCOUNT_REF 以与身份域统一（须先完成迁移）"),
                action_taken="advisory")
        return domain

    def _purge_old_logs(self, keep_days: int = 7) -> None:''')

# S4. 每轮分类（只分类、不退出）。
patch("""        startup_safety = self._verify_startup_safety()
""", """        startup_safety = self._verify_startup_safety()
        # U02：重启/接手只**分类**存量仓位，绝不批量退出。旧仓（无策略归属）
        # 明确标记为需人工显式迁移，有归属但无预期记录的只重建预期。
        position_management = self._plan_position_restart()
""")

# S5. 结果只读键。
patch("""            "startup_safety": startup_safety,
""", """            "startup_safety": startup_safety,
            "position_management": position_management,
            "account_domain": self.account_domain.as_dict(),
""")

# S6. 人工 emergency：走同一判定入口 + 登记硬退出流水（不覆盖预期持仓）。
patch("""        receipts = []
        if new_state == "emergency":
            prices = {}
""", """        receipts = []
        if new_state == "emergency":
            # U02：全平只允许来自"人工 emergency"或"已确证的保护失败"。人工路径
            # 的语义不变（始终允许），但必须经过同一个判定入口，从而与"健康故障
            # 不得触发全平"（R11）使用同一条真值路径，不存在绕过口。
            decision = self.strategy_position_manager.should_flat_close(
                operator_emergency=True)
            prices = {}
""")

patch("""            max_rounds = 5
            for _ in range(max_rounds):
                receipts = self.broker.close_all(prices, self.db)
                state = self.broker.state if isinstance(self.broker.state, dict) else {}
                if not [p for p in (state.get("positions") or []) if abs(float(p.get("qty") or 0)) > 0]:
                    break
            self.db.log_risk_event(event_type="manual_emergency_close", severity="critical",
""", """            if not decision.allowed:  # pragma: no cover - 人工路径恒定允许
                # 结构性不可达：``should_flat_close(operator_emergency=True)`` 恒
                # 返回 allowed=True（并有契约回归锁死）。保留 fail-closed 分支只为
                # 万一语义被改坏时**不静默平仓**，并显式回报失败而非假装已执行。
                return {"ok": False, "state": new_state, "receipts": [],
                        "error": "operator_emergency_flat_close_blocked",
                        "flat_close": decision.as_dict()}
            max_rounds = 5
            flat_receipts: list[dict[str, Any]] = []
            for _ in range(max_rounds):
                receipts = self.broker.close_all(prices, self.db)
                flat_receipts.extend(receipts)
                state = self.broker.state if isinstance(self.broker.state, dict) else {}
                if not [p for p in (state.get("positions") or []) if abs(float(p.get("qty") or 0)) > 0]:
                    break
            # 人工急平同时登记硬退出流水：预期持仓**不被覆盖**（U02 命名空间分离），
            # 事后复盘能区分"保护触发"与"人工放弃"。登记全部轮次的平仓单，而不是
            # 只有最后一轮（否则残余仓位的平仓事实会丢）。
            for receipt in flat_receipts:
                lifecycle_key = ":".join((
                    str(self.account_domain.environment or "").lower(),
                    str(self.account_domain.account_ref or ""),
                    str(receipt.get("symbol") or "").upper(), "BOTH"))
                self.strategy_position_manager.record_hard_exit(
                    exit_id=str(receipt.get("order_id") or ""),
                    lifecycle_id=lifecycle_key,
                    symbol=str(receipt.get("symbol") or ""),
                    position_side="BOTH", trigger="OPERATOR_EMERGENCY",
                    evidence_id=f"emergency:{self.runtime_instance_id}",
                    requested_qty=receipt.get("qty") or 0,
                    now_ms=int(time.time() * 1000))
            self.db.log_risk_event(event_type="manual_emergency_close", severity="critical",
""")

print("\nSERVICE OK")
sys.exit(0)
