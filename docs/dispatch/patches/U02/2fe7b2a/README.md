# U02 共享适配补丁包 r2（作者 B / 交 I 应用）

**本包取代 `docs/dispatch/patches/U02/8c66f65/`（r1 包）。** 目录名 = 交付时作者分支 HEAD（`2fe7b2a`）；补丁在 `045a9a7` 生成，之后的提交只改独占文件与文档，补丁内容未变。 基线 = 生成时的集成
HEAD **`I@40ff52f`**（`git -C <I> rev-parse HEAD` 当时取值；6 个目标文件的 blob
在 I 从 `1207cd3` 到 `40ff52f` 的历次 HEAD 变动中**逐字未变**，本包对当前 HEAD
有效）。作者分支 `task/U02`：HEAD 见交付报告，补丁生成后未再改动共享文件。

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_...trade_reconciliation...` | `src/exchange/trade_reconciliation.py` | U15 跨任务阻塞项：账本 `account_ref` 消费 U15 派生值 + 来源标记 |
| `02_...binance_futures...` | `src/exchange/binance_futures.py` | 账户引用单一派生点；退出经管理预留（幂等/只减/泄漏回收） |
| `03_core_service_u02_wiring.diff` | `src/core/service.py` | 账户域绑定与自检；管理器注入；重启只分类；emergency 同判定入口 |
| `04_...decision_pipeline...` | `src/core/decision_pipeline.py` | 管理优先队列 + 非 OPEN 意图授权引用（`enforce=True` fail-closed） |
| `05_...execution...` | `src/agents/execution.py` | 纸面退出同序 + 管理预留幂等守卫（回退键区分轮次） |
| `06_...dashboard_app...` | `src/dashboard/app.py` | 新增事件类型的中文映射（伴随修改） |

**补丁行尾是"字节精确"的**：`binance_futures.py`/`execution.py`/`app.py` 在集成
树里是 **mixed EOL**（既有行 CRLF、历史补丁新增行 LF），因此上下文行按目标**逐字节**
生成（`git -c core.autocrlf=false diff`，见 `evidence/emit_patches.py`）。
U05 的教训：依赖 `core.autocrlf=true` 的补丁在 `autocrlf=false` 的集成工作区会整体打不上。

## 批次约束（约束 1 已在 r2 按事实改写）

1. **01 与 02 必须同批应用**——但 r1 理由写反了，事实是：**只上 01 就已经切换账本分区**
   （`account_reference` 自己读 `MTA_ACCOUNT_REF`，实测 `book.account_ref` 由
   `demo:fd8e…` 变为 `demo:acct-live`）。只上 01 的风险不是"无效"，而是**静默改数据
   分区且不留任何可见性**：没有 02 的单一派生点、没有 `account_ref_source` 标记、
   没有 03 的启动自检。只上 **02** 会 `ImportError: cannot import name
   'account_ref_source'`（直接崩）。因此 01+02 仍是原子单位，理由是"改分区必须同时
   具备可见性与自检"。
2. **03/06 与 01+02 同批**：03 比对"身份域 vs 账本域"（只上 03 会让未声明环境变成
   常态告警），06 是新增事件类型在 `smoke_ui` 下不红的必要条件。
3. **04/05 可独立应用**（默认 `enforce=False`/`getattr` 探测，缺少 U02 接入点时保持
   既有行为）；单独应用不构成"U02 已集成"。
4. `01..06` 顺序 `git apply` 成功；**两种 git 配置下 `apply --check` 全部 OK**
   （见验证节）。`03` 只对集成态 `service.py` 有效（作者分支未含 U13 的 service 改动）。

## K03 幂等键与既有数据：**可执行迁移方案**（重写）

- **K03 幂等键** = `(environment, account_ref, income_type, tran_id)`，分区维度里
  含 `account_ref`；`execution_fills`/`position_lifecycles`/`release_observations`/
  `trade_reconciliation_jobs` 的唯一键同样含它。
- **未声明账户**：取值仍是 `sha256(api_key)[:16]`，既有行完全不受影响（与 base 逐字
  一致，回归证据见下）。
- **一旦声明 `MTA_ACCOUNT_REF`**：同一账户的历史行会落在两个域，**同一 `trade_id`
  在两域各一行，唯一索引拦不住**——"声明后旧数据不可幂等"是**已被真实 schema 复现
  的结论**（A_r1 §3.2）。因此正确顺序是 **先迁移、后声明**。
- **r1 的迁移 SQL 不可执行**（A_r1 §3.3/§3.4 实测）：逐表 `UPDATE` 会在
  `execution_fills`/`position_lifecycles` 上撞 UNIQUE 约束、非原子、不可重入；漏了
  `trade_reconciliation_jobs`（`binance_futures.py:2926` 的"同开仓已收口"护栏按
  `account_ref` 过滤，漏迁会**重复生成 lifecycle**）；`lifecycle_fill_allocations`
  **没有 `account_ref` 列**（靠 `lifecycle_id` 关联），对它 `UPDATE` 是无效指令。
- **可执行方案（归属 U13；本包不含脚本，也不改表结构）**：

  **A. 表与唯一键清单（以真实 schema 为准）**
  | 表 | 迁移列 | 冲突键 | 处理 |
  |---|---|---|---|
  | `execution_fills` | `account_ref` | `(environment, account_ref, trade_id)` | 查重→**语义比对**→合并/删除重复行 |
  | `position_lifecycles` | `account_ref` | `(environment, account_ref, symbol, position_side)` | 两域各有 ACTIVE 即**冲突**，人工裁决，不得自动合并 |
  | `trade_reconciliation_jobs` | `account_ref`（NOT NULL） | 无唯一键但**必须迁**（护栏按它过滤） | 直接迁 |
  | `income_events` / `income_coverage` / `income_conflicts` | `account_ref` **+ `account_ref_norm`** | K03 幂等键（短横线=归一化后） | 两列同批更新，否则唯一索引仍视作两个域 |
  | `release_observations` | `account_ref` | `(release_id, environment, account_ref, observed_minute)` | 冲突时保留新域样本、删除旧域重复样本（`INSERT OR IGNORE` 会静默丢弃，见下） |
  | `lifecycle_fill_allocations` | **无该列** | 经 `lifecycle_id` | **不迁**（迁完 lifecycle 即随之归属） |
  | `data/release_manifest.json`（磁盘工件） | `account_ref` | — | 历史清单会与新域分叉（K01 身份对照会看到不一致），迁移需一并说明 |

  **B. 执行顺序（先迁移后声明）**：① 停服务并确认 `MTA_ACCOUNT_REF` **未设置**；
  ② `scripts/create_recovery_bundle.py` 备份；③ 在**单个事务**内执行迁移脚本
  （`BEGIN IMMEDIATE … COMMIT`），脚本必须：先按新键查重 → 冲突行**显式裁决**
  （可自动合并的语义等价行：保留新域行、删旧域行；不可裁决的冲突：**中止并打印冲突
  清单**）→ 逐表 `UPDATE … SET account_ref=new, account_ref_norm=normalize(new)
  WHERE environment=? AND account_ref=old`（写成可重入语句，重复执行是 no-op）；
  ④ 迁移后校验：行数/关键列校验和 + `SELECT DISTINCT account_ref` 只应剩新值 +
  `assert_account_domains_consistent` 通过；⑤ 再启动服务并**首次**设置
  `MTA_ACCOUNT_REF`。
  **C. 幂等/可重入**：所有语句以 `WHERE account_ref=old` 为条件且只做"移到新域"，
  重复执行不产生新行；事务中止即整体回滚，库里不会出现半迁移状态。
  **D. 回滚**：迁移在单事务内，失败自动回滚；成功后唯一回滚途径是恢复 ② 的
  recovery bundle（不要在迁移后"反向改回"，那会再次撞唯一键）。
- **【阻断级登记】在可执行迁移脚本落地并验证前，不得在生产启用 `MTA_ACCOUNT_REF`。**
  本包只提供"取值来源切换 + 可见性"，不提供迁移；`MTA_ACCOUNT_REF` 一旦被
  `.env`/部署脚本设置，01 会在**无人决策**的时刻切换数据分区。
- **附**：`release_observations` 的幂等键含 `account_ref`，同一 `release_id` 批次内
  跨域声明会让已观察过的分钟**写不进去（`INSERT OR IGNORE` 静默丢弃、无告警）**，
  发布门禁的"观察新鲜度"证据出现空洞（A_r1 §3.5）。切换声明时**显式开新
  `release_id`**，或把 `account_ref` 从该唯一键移除（需 schema 变更，属 U13）。

**Schema/迁移版本**：本包**不新增迁移、不改表结构、不推进 `schema_migrations`**；
6 个补丁内无任何 `CREATE/ALTER/_apply_migration` 字样。

## r2 修复（对应 A_r1）

1. **P0 退出预留泄漏 → 该标的永久不可退出**（`binance_futures.py`）：
   `_close_or_reduce` 的整个占量后主体进入 `try/finally`，异常路径 `finally` 释放
   预留并记 `futures_exit_reservation_released`；正常路径按 `close_unconfirmed`
   结算/释放（`settled` 标志，只能二选一）。**另一半**在管理器：`release_exit`
   成功后撤销该 `exit_key`（"释放 = 这次退出没有发生"→ 同键必须能重发），并新增
   `reclaim_stale_exits`/`in_flight_ttl_ms`：同键预留仍是 `ACTIVE` 且超过 TTL 未结算
   时按泄漏回收（`STALE_EXIT_RESERVATION_RECLAIMED`，有审计）；`_reserve_exit` 每轮
   先巡检一次，回收时记 `futures_stale_exit_reservation_reclaimed`。**已 `CONSUMED`
   （成交已确认）的键永不回收**，幂等语义不变。
2. **P1-2** `_open` 里引用未定义 `reservation_key` 的行已删除（qty≈0 恢复优雅跳过）。
3. **P1-3** 纸面回退退出键改为 `paper-exit:<symbol>:<side>:<本轮令牌>:<指纹>`：同轮
   同指纹仍共享一个键（保持抑制），**跨轮次各自独立**（原来恒稳键会让同名标的的
   第二次退出永久被吞）。
4. **P2-6** `filter_unbound_management(enforce=True)` 增加 fail-closed 前置：
   `authorization_ref_supported()` 为假（当前 `TradeIntent` 没有
   `management_authorization_id`，pydantic extra=ignore 会静默丢弃）时抛
   `ManagementEnforcementError`，不再"把全部退出静默摘除"。**该门仍无生产调用方**，
   接入属后续任务；默认 `enforce=False` 行为不变。
5. **P2-7** `plan_restart` 每轮都跑：已显式迁移的生命周期改记为 `ALREADY_MIGRATED`，
   `migrate_legacy` 的结果不再被下一轮撤销；旧仓告警只在**新增**旧仓时记一条
   （不再是每轮一条）。遗留（登记）：`set_expectation` 仍**无生产调用方**，
   `_expectations` 在生产上恒空 → `ALIGN_EXPECTATION` 恒报、`expected_qty_at_trigger`
   恒 0；`record_hard_exit` 的调用方仍只有人工 emergency（SL/TP 触发不进流水）；
   `migrate_legacy` 无运营入口。这三项需要生命周期/决策归属生产方（归属后续任务）。
6. **P2-8** 护栏不再传字面空元组：`plan_restart` 内部与 service 调用点都传**真实**
   产出的 actions；`RestartPlan.exit_actions` 改为按动作名实筛、`exit_action_count`
   为 `len(...)`（仍应为 0，但不再是恒真式）。
7. **P3-3** `migrate_legacy` 迁移未知 `lifecycle_id` 时留可见原因码
   `LEGACY_MIGRATION_UNKNOWN_LIFECYCLE`（不再静默写 `qty=0`）。
8. **幂等/审计记忆有界**（A_r1 提到 `_exit_keys` 只增不减）：`_exit_keys` 超过
   `MAX_EXIT_KEYS=2000` 时只淘汰**已终结**键（ACTIVE 永不淘汰），`_released_exits`
   为 200 条环形缓冲；账本内终结预留上限 2000（`MAX_TERMINAL_RESERVATIONS`）。

## 如实登记（未修，附位置与理由）

- **P1-4** 未采纳"仅显式传参才生效"的退路：`MTA_ACCOUNT_REF` 是 U15 登记的声明通道，
  改语义会让已确认的"声明=账本分区"契约失效并需重跑全部证据；改为**如实登记风险**
  （见上"阻断级登记"）。`account_ref_source()=='declared'` 的阻断级启动检查需要
  "迁移是否已完成"的外部事实，本任务的独占路径拿不到，登记给 U13。
- **P2-9**：U02 的全部状态（`_expectations`/`_hard_exits`/`_ledgers`/`_exit_keys`）
  都是**进程内易失**，跨重启的重复退出识别与在途预留**不成立**（重启后新管理器
  重新开始）。R09 只在单进程内成立。
- **P2-10**：本包仍**不含** `scripts/run_offline_regression.py` 的登记行（该文件属
  U00 且被禁止改动），I 必须一并落，否则 `_validate_smoke_manifest()` 直接抛错。
- **P2-11（新）**：`release_exit` 撤销键后，`FILL_UNCONFIRMED` 的下一轮会**重发**同一
  稳定 cid。有效性依据：交易所对仍存活的同一 `newClientOrderId` 会拒绝重复单
  （`_submit_standard_order` 会走确定性拒绝路径），且 `reduce_only=True` 不会反向
  开仓、提交量被可减量夹逼。**代价**：极端情况下可能对同一笔退出多发一次减仓单
  （不会反向、不会超仓）。取"可退出"优先于"绝不重发"，否则就是 P0。
- **P3-1/3-2/3-4/3-5/3-6/3-7/3-8/3-9**：未改（与环境前缀口径、域键外观、`record_hard_exit`
  无去重、`FlatCloseDecision` 三个 False 是默认值、U15 绑定可选、`release_observations`
  静默丢弃、纸面/实盘键形状差异、`reconcile_position` 部分裁剪切算入 `consumed_qty`
  等）。其中 3-7 已在迁移节写成运营动作，其余为一致性/外观项，不阻塞。

## SCRIPTS 登记行（I 应用到 `scripts/run_offline_regression.py`）

加在 `"scripts/smoke_u04_execution_faults.py",` 之后：

```python
    # U02（B 持仓与执行）：策略仓管理/有界退出授权/并发退出预留。全程离线。
    "scripts/smoke_u02_strategy_position_manager.py",
```

## 验证（原始输出可复现）

沙箱：`pristine_int` / `sb_int` = `git archive I@40ff52f` + U02 独占文件
（未打补丁 / 打 01..06）+ README 的登记行。证据与脚本在 `evidence/`。

```
git apply --check -p1                     # 6/6 OK，两种配置各跑一遍：
  -c core.autocrlf=false -c core.eol=lf   → 01..06 全 OK
  默认（本机 system autocrlf=true）        → 01..06 全 OK
逐条非 fail-fast 全量离线回归（76 条 = 75 + U02）：
  pristine_int : FAILED=0 []
  sb_int       : FAILED=0 []
  新增失败集合差集 = {}            # evidence/out_pristine_int.txt / out_sb_int.txt
测试与冒烟：
  tests/integration/test_strategy_position_manager.py  → PASS=144 FAIL=0（exit 0）
  scripts/smoke_u02_strategy_position_manager.py       → U02 SMOKE OK（exit 0）
     正向 144/0；N1 负向对照 142/2（必须失败，已失败）；N2 负向对照 138/6（已失败）
  scripts/smoke_contracts_v3.py                        → PASS 173 / FAIL 0
生效性探针（B 独占态与集成态各跑，均 FAIL=0）：
  _scratch_B/probe_u02_wiring.py / probe_u02_service.py / dl03_probe.py
P0 前后对照（evidence/probe_p0_reservation_leak.py）：
  修复前 FAIL=4：reservation_leaked_still_ACTIVE(status=ACTIVE,remaining=1.0)、
    available=0、stable_key_can_resubmit_after_release=duplicate_skipped、
    symbol_can_still_exit_after_submit_recovers（5 轮全被吞，submitted 不增长）
  修复后 FAIL=0：exception_path_releases_reservation=[]、
    released_reservation_restores_available_qty=1.0、
    stable_key_can_resubmit_after_release=SUBMIT_REDUCE、
    symbol_can_still_exit_after_submit_recovers=submitted、consumed_key_stays_duplicate_skipped
NameError 前后对照（probe_p1_nameerror_open.py）：
  修复前：NameError: name 'reservation_key' is not defined（result='unset'）
  修复后：_open qty=0 -> result=None error=''（优雅跳过）
纸面退出键对照（probe_p1_paper_exit_key.py）：修复前 FAIL=3（第二轮/部分平仓/同轮均
  duplicate_skipped）→ 修复后 FAIL=0
enforce 门对照（probe_p2_enforce_gate.py）：修复前 enforce=True → kept_intents=0 /
  decision=no_trade（静默摘除）→ 修复后 ManagementEnforcementError，探针 FAIL=0
```

生成器（可复现）：`evidence/gen_patch_tree.py`（02/04/05/06）+
`evidence/gen_patch_service.py`（03 的集成态重移植）+ `evidence/emit_patches.py`
（字节精确 EOL 映射）；delta：`evidence/run_delta.py <tree>`。`_scratch_B/` 是作者
本地证据目录（**非交付物**，可整体删除）。

## 回滚

- 补丁层：`git apply -R`（逆序 06→01）即可完全回退，无 schema/迁移副作用。
- U02 独占模块与补丁无耦合：未应用补丁时 `getattr` 探测使既有行为不变；已应用后
  回退补丁，账本 `account_ref` 立即回到密钥摘要形态（**已迁移的历史行不受影响**，
  因为迁移不在本包内）。
- 生产已声明 `MTA_ACCOUNT_REF` 且已迁移：回滚补丁**不等于**回滚数据，需恢复
  recovery bundle。

## 已知限制

- 未接真实交易所：全部证据是代码路径级（桩掉网络/账本副作用），未下单、未撤网、
  未联网、未读 `.env`/`data/`/密钥。
- P0 触发频率未量化；`_open` 的 qty≈0 分支可达性依赖上游过滤（未穷举）。
- R09/预留回收只有单进程证明；多进程/多实例共享同一账本不成立（P2-9）。
- `in_flight_ttl_ms` 默认 900s：比任何合理"提交+等成交"窗口长得多，但仍是一个
  经验值；若某次退出合法地超过该时长，会被回收并允许重发（见 P2-11 的代价说明）。
- `04`/`05` 的 TTL/令牌机制只在集成态生效（作者分支无共享文件）。
