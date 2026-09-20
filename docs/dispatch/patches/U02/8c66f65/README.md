# U02 共享适配补丁包（作者 B / 交 I 应用）

补丁基线：`8c66f65`（= 分支 `task/U02` 的 patch 生成时 HEAD；共享文件内容与
`task_base_commit 45ebdff` 逐字相同，作者未在本分支提交任何共享文件）。
本包由 I 在集成时应用；**不适用的补丁不得单独合入**。

| 补丁 | 目标文件 | 独占/共享 | 用途 |
|---|---|---|---|
| `01_exchange_trade_reconciliation_account_ref_u15.diff` | `src/exchange/trade_reconciliation.py` | U02 集成目标（I 应用） | **U15 跨任务阻塞项**：账本侧 `account_ref` 改为消费 U15 派生值（声明时），并暴露来源标记 |
| `02_exchange_binance_futures_account_ref_and_exit_reservation.diff` | `src/exchange/binance_futures.py` | U02 集成目标（I 应用） | 账户引用单一派生点；退出经 U02 管理预留（重复退出幂等、在途不重复占量） |
| `03_core_service_u02_wiring.diff` | `src/core/service.py` | U02 集成目标（I 应用） | 账户域绑定与自检；策略仓管理器注入；重启只分类不退出；emergency 走同一判定入口 |
| `04_core_decision_pipeline_management_auth.diff` | `src/core/decision_pipeline.py` | U02 集成目标（I 应用） | 管理优先队列（DL03）与非 OPEN 意图的授权引用（`NEEDS_AUTH`） |
| `05_agents_execution_paper_exit_idempotency.diff` | `src/agents/execution.py` | U02 集成目标（I 应用） | 纸面退出同序（管理优先）+ 管理预留幂等守卫 |
| `06_dashboard_app_event_labels.diff` | `src/dashboard/app.py` | **伴随修改**（见约束 2） | 4 个新事件类型的中文映射，否则 `scripts/smoke_ui.py` 的"全部事件类型都有中文映射"失败 |

## 批次约束

1. **01 与 02 必须同批应用**。只上 01 会让 `trade_reconciliation.account_reference`
   在声明账户时返回 `<env>:<declared>`，而 02 之前 `binance_futures` 的调用点仍
   未把 `MTA_ACCOUNT_REF`/`declared_account_ref` 传进去 —— 两者不会崩，但账本域
   仍停在密钥摘要形态，等于补丁无效。只上 02 也有同样问题（02 依赖 01 新增的
   `account_ref_source`，会直接 `ImportError`）。**01+02 是一个原子单位。**
2. **03/06 必须与 01+02 同批**：03 会在启动时比对"身份域 vs 账本域"，只上 03
   会让 `account_domain_mismatch` 在未声明账户的环境里变成常态告警（噪声），
   且 06 是 03/04/02 新增事件类型在 `smoke_ui.py` 下不红的必要条件。
3. **04/05 可独立于 01/02/03 应用**（它们只新增函数与调用点，默认
   `enforce=False`/`getattr` 探测，缺少 U02 接入点时保持既有行为）。但单独应用
   04/05 不构成"U02 已集成"——管理授权拒绝路径没有 03 注入的授权来源仍不可用。
4. 六个补丁各自 `git apply --check -p1` 通过；`01..06` 顺序应用后全量离线回归
   与 pristine 的失败集合**差集为空**（见下节）。

## U15 跨任务阻塞项的处置（本包 01+02）

U15 `B_r1` P1-4 指出：账本侧 `src/exchange/trade_reconciliation.py:66-69` 的
`account_ref = f"{env}:{sha256(api_key)[:16]}"` 随密钥轮换漂移，且与 U15
`derive_account_ref` 的 `<env>:<declared>` 不是同一值，因此 U02 的
`ManagementAuthorization.account_ref` 两条路都不通。

**处置**：01 让 `account_reference(api_key, environment, *, declared_account_ref=None)`
**优先消费 U15 的派生值**（显式入参 > `MTA_ACCOUNT_REF` > 旧密钥摘要回退），并新增
`account_ref_source()` 返回 `declared` / `legacy-key-digest`。02 在
`BinanceFuturesBroker.__init__` 里把派生点收敛为一处（`self.account_ref`），
`TradeReconciliationBook` 与新增的 `self.account_ref_source` 共用它。03 在
service 启动时以 `assert_account_domains_consistent` 比对身份域与账本域，不一致
时记 `account_domain_mismatch`（已声明却分叉 = critical；未声明 = warn）。

## K03 幂等键与既有数据的影响 / 迁移建议

- **K03 幂等键** = `(environment, account_ref, income_type, tran_id)`
  （`src/core/contracts/k03_income.py`）。01 只改变 `account_ref` 的**取值来源**，
  不改变键形态（仍是 `<env>:<token>`），因此：
  - **未声明账户**（现状生产默认）：取值不变（仍是 `sha256(api_key)[:16]`），
    **K03 分区、`execution_fills`/`position_lifecycles`/`income_events`/
    `income_coverage`/`income_conflicts`/`release_observations` 的既有行完全不受
    影响**，行为与打补丁前逐字相同（回归证据见下节）。
  - **一旦声明 `MTA_ACCOUNT_REF`**：`account_ref` 从 `<env>:<sha16>` 变为
    `<env>:<declared>`，**同一账户的历史行会被分到两个不同的域** —— 这是真实的
    数据域迁移，不是"零影响"。
- **迁移建议（归属 U13，与备份/恢复同批）**：不要在生产库上直接声明
  `MTA_ACCOUNT_REF`。按下列顺序显式迁移：
  1. 备份（`scripts/create_recovery_bundle.py`）；
  2. 计算新旧值：`old = f"{env}:{sha256(api_key)[:16]}"`（用**迁移前的**密钥），
     `new = derive_account_ref(env, declared_account_ref=...).value`；
  3. 对 `execution_fills`、`position_lifecycles`、`lifecycle_fill_allocations`
     （经 lifecycle 关联）、`income_events`、`income_coverage`、
     `income_conflicts`、`release_observations` 逐表 `UPDATE ... SET account_ref=new
     WHERE environment=env AND account_ref=old`；注意 `income_*` 表还有
     `account_ref_norm` 归一化列必须同批更新（否则唯一索引仍视作两个域）；
  4. 迁移后用 `account_ref_source()` 与 `assert_account_domains_consistent()`
     验证两端一致，再启动服务。
  - **本补丁不包含该 UPDATE**：U02 的独占路径不含 `scripts/`（`run_offline_regression.py`
    属 U00；迁移脚本属 U13 的备份/恢复批次），且生产库不在本任务的允许改动范围内。
  - 若 I 认为该迁移应更早落地，可把 01 的"声明优先"改为**仅当显式传参**时生效
    （不读 `MTA_ACCOUNT_REF`），从而把生产影响面留到 U13 显式开口。
- **Schema/迁移版本**：本包**不新增迁移、不改任何表结构、不推进
  `schema_migrations` 版本号**（`smoke_db_migrations.py` 的既有相对断言不触发，
  全量回归无新增失败）。因此无需伴随迁移版本断言补丁。

## SCRIPTS 登记行（I 应用到 `scripts/run_offline_regression.py`）

加在 `"scripts/smoke_u04_execution_faults.py",` 之后：

```python
    # U02（B 持仓与执行）：策略仓管理/有界退出授权/并发退出预留。全程离线。
    "scripts/smoke_u02_strategy_position_manager.py",
```

不加会让 `_validate_smoke_manifest()` 以 `未登记冒烟脚本: …` 直接抛错。

## 验证与 delta（沙箱 = `git archive 8c66f65` + 独立 `git init`）

```
git apply --check -p1 <01|02|03|04|05|06>          # 六个补丁各自 OK（独立 repo 根）
顺序应用 01..06                                     # 全部成功，无冲突
非 fail-fast 逐条跑 SCRIPTS（75 条，同一清单/同一环境清理规则）：
  pristine      : FAILED=1  ['scripts/smoke_windows_service_plan.py']   # 既有 KI02
  01..06 已应用 : FAILED=1  ['scripts/smoke_windows_service_plan.py']
  新增失败集合差集 : {}   （无 U02 引入的新失败）
受影响既有冒烟（补丁后逐个复跑，全部 PASS）：
  smoke_trade_reconciliation / smoke_decision_pipeline / smoke_service_lifecycle /
  smoke_close_reconcile / smoke_fault_matrix / smoke_contracts_v3 /
  smoke_entry_readiness / smoke_ui
生效性探针（证明不是"文件改了行为没变"）：tests/probe_u02_wiring.py、tests/probe_u02_service.py
```

## 写入的共享列/字段：既有语义与消费者（不改语义，只改来源或新增）

| 字段 | 既有语义 | 既有消费者 | 本补丁的改动 |
|---|---|---|---|
| `account_ref` | 账户分区标识 `<env>:<token>` | `execution_fills`/`position_lifecycles`/`income_events`/`income_coverage`/`income_conflicts`/`release_observations` 的主键或唯一键分量；K03 幂等键；`release_manifest.account_ref`→K01 身份；`AuditDatabase.set_release_context` | **仅取值来源**：声明时用 U15 派生值（与轮换解耦）。形态与语义不变 |
| `BinanceFuturesBroker.account_ref` | 新增 | 本补丁的 `_exit_domain_key` | 新增属性；未接入时无消费者 |
| `BinanceFuturesBroker.strategy_position_manager` | 新增（`getattr` 探测） | `_reserve_exit`/`_settle_exit`/`_release_exit` | 新增；缺省即旧行为 |
| `TradingService.strategy_position_manager` / `.account_domain` / `.hard_exits` | 新增 | 本补丁自身；看板可按需消费 | 新增 |
| `run_once` 结果键 `position_management` / `account_domain` | 新增 | 只读展示 | 新增，未改动既有键 |
| `EVENT_TYPE` 新值（4 个） | 新增 | `dashboard.app._event_type_cn` | 新增映射（补丁 06） |
