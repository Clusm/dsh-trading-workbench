# U02 共享适配补丁包 r3（作者 B / 交 I 应用）

**本包取代 `docs/dispatch/patches/U02/2fe7b2a/`（r2 包）。** 目录名 = 交付时作者
分支 HEAD；补丁在 r3 修复轮生成。基线 = 生成时的集成 HEAD **`I@b4eeda9`**
（`git -C <I> rev-parse HEAD`）。6 个目标文件的 blob 在 I 的
`40ff52f` / `59660b2` / `b4eeda9` 三次 HEAD 上**逐字相同**，本包对当前 HEAD 有效。

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_...trade_reconciliation...` | `src/exchange/trade_reconciliation.py` | U15 跨任务阻塞项：账本 `account_ref` 消费 U15 派生值 + 来源标记 |
| `02_...binance_futures...` | `src/exchange/binance_futures.py` | 账户引用单一派生点；退出经管理预留（幂等/只减/泄漏回收）+ 巡检失败告警 |
| `03_core_service_u02_wiring.diff` | `src/core/service.py` | 账户域绑定与自检；管理器注入；重启只分类；emergency 同判定入口 |
| `04_...decision_pipeline...` | `src/core/decision_pipeline.py` | 管理优先队列 + 非 OPEN 意图授权引用（`enforce=True` fail-closed） |
| `05_...execution...` | `src/agents/execution.py` | 纸面退出同序 + 管理预留幂等守卫（回退键区分轮次；**同轮抑制见 §r3-3**） |
| `06_...dashboard_app...` | `src/dashboard/app.py` | 新增事件类型的中文映射（伴随修改） |

**补丁行尾是"字节精确"的**：`binance_futures.py`/`execution.py`/`app.py` 在集成
树里是 **mixed EOL**（既有行 CRLF、历史补丁新增行 LF），因此上下文行按目标**逐字节**
生成（`git -c core.autocrlf=false diff`，见 `evidence/emit_patches.py`）。

> **⑥ 应用配置（必须照做）**：请用 `git -c core.autocrlf=false -c core.eol=lf apply -p1`
> 应用。三条**纯 LF** 目标文件（`trade_reconciliation.py` / `service.py` /
> `decision_pipeline.py`）在默认 `core.autocrlf=true` 下会被 `git apply` **整文件转成
> CRLF**（`git add` 的归一化会把它折回 LF，所以只产生列分辨率/噪音，不是数据损坏）。
> 应用后请 `git diff --stat` 自检：应只出现真实改动行数，若某文件显示"整文件重写"，
> 说明用了错误配置。两种配置下 6/6 `apply --check` 都 OK（§验证）。

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

## r3 修复（对应 Q_r2 的 3 条未登记 P2 + 2 条 SHOULD FIX）

### r3-1（P2，NEW-1）已 CONSUMED 的键被账本淘汰后可被 TTL 回收并重发 —— **已真修**

账本按 `MAX_TERMINAL_RESERVATIONS=2000` 淘汰**终结**预留（内存有界）。在此之前，
"是否已成交"只看**当前记录**：CONSUMED 记录一旦被淘汰，管理器的
`_reclaim_if_stale_locked` 就落进 "预留已不在账本里" 分支，在 TTL 到期后把该
`exit_key` 回收并**重新占量提交**——同一笔退出被再次减仓，与模块 docstring
"已 `CONSUMED` 的键永不回收" 直接冲突。

**修法**：账本新增只增不淘汰的**成交事实**集合 `_consumed_ids`（带
`is_consumed()` 查询），`consume()` 在写状态**之前**先落事实；`reserve()` /
`release()` 一律按事实拒绝；管理器 `_reclaim_if_stale_locked` 与
`request_exit` 都回到事实上判重复。只增集合的代价已登记（§已知限制）。

前后对照（`evidence/probe_r3_consumed_reuse.py`，真实 2000 条上限、不 monkeypatch）：
```
BEFORE（4856aec 独占文件 + I@b4eeda9）
  A_before=CONSUMED release_accepted=True after=RELEASED re_reserve_accepted=True
  FAIL A_release_is_idempotent_on_consumed
  B_limit=2000 consumed_record_pruned=True exit_key_count=2000
  B_after_prune_same_key={'action': 'SUBMIT_REDUCE', 'duplicate': False, 'qty': 1.0, 'reasons': []}
  FAIL B_consumed_not_reclaimed_when_record_pruned
  B2_after_prune_same_key={'action': 'SUBMIT_REDUCE', 'duplicate': False, 'qty': 1.0,
                           'reasons': ['STALE_EXIT_RESERVATION_RECLAIMED']}     # 正是巡检 else 分支
  FAIL B2_consumed_not_reclaimed_via_ttl_sweep
  FAIL C_consumed_not_resubmitted_when_key_evicted
  R3_CONSUMED_PROBE FAIL=5
AFTER（r3）
  A_before=CONSUMED release_accepted=False after=CONSUMED re_reserve_accepted=False
  B_after_prune_same_key={'action': 'NOOP', 'duplicate': True, 'qty': 0, 'reasons': ['DUPLICATE_EXIT_IGNORED']}
  B2_after_prune_same_key={'action': 'NOOP', 'duplicate': True, 'qty': 0, 'reasons': ['DUPLICATE_EXIT_IGNORED']}
  C_after_key_eviction={'action': 'NOOP', 'duplicate': True, 'accepted': False, 'qty': 0}
  D_released=True retry={'action': 'SUBMIT_REDUCE', 'qty': 1.0}   # P0-1 未被修回去
  R3_CONSUMED_PROBE FAIL=0
```

### r3-2（P2，NEW-2）`release()` 对 CONSUMED 不幂等 —— **已真修**

`release()` 对 CONSUMED 条目此前会改成 RELEASED（且随后可再次 `reserve`），等于
"撤销一笔已确认的成交"并让同键重新可重发。现在 `release()` 对**任何**
终态（RELEASED/CONSUMED）或已淘汰但留下成交事实的 ID 都返回
`REASON_RESERVATION_ALREADY_CLOSED` 且**不改变状态**；`StrategyPositionManager.
release_exit()` 随之返回 `False` 并**保留**该键的幂等记忆。
对照输出见上（`A_release_is_idempotent_on_consumed`，BEFORE FAIL → AFTER PASS）。

### r3-3（P2，NEW-3）纸面"同轮抑制"声明与事实相反 —— **已让行为与声明一致**

r2 的 `release_exit` 会撤销 `exit_key`，而 `_exit_idempotency_guard` 是"接受后立即
release"，于是**同轮**第二次同名同指纹退出会再次提交（r2 README 写"保持抑制"，
与事实相反）。纸面成交是同步完成的，改用 `confirm_exit` 结算该预留：同轮内同键仍
判 `duplicate_skipped`，跨轮次用的是带轮次令牌的新键、照常执行（A_r1 P1-3 不回归）。

前后对照（`evidence/probe_r3_paper_round.py`，只驱动 `PaperBroker._exit_idempotency_guard`）：
```
BEFORE（r2 包）
  1_same_round first=EXECUTED second=EXECUTED              FAIL
  1b_after_fill first=EXECUTED second=EXECUTED             FAIL
  5_same_round_same_partial=EXECUTED,EXECUTED              FAIL
  2/3/4/6 全 PASS（跨轮、5 轮、部分平+剩余、跨标的）
  R3_PAPER_ROUND_PROBE FAIL=3
AFTER（r3）
  1_same_round first=EXECUTED second=duplicate_skipped     PASS
  1b_after_fill first=EXECUTED second=duplicate_skipped    PASS
  5_same_round_same_partial=EXECUTED,duplicate_skipped     PASS
  2/3/4/6 仍全 PASS（跨轮 / 5 轮全部 EXECUTED）
  R3_PAPER_ROUND_PROBE FAIL=0
```
**登记（口径变化，非缺陷）**：显式 `client_order_id`（如 guardian 的
`mta-g-<sha24>`）在**纸面**路径上现在**跨轮**也判重复（r2 会再次 EXECUTED）。
这与生产一致——`execute()` 先用 `orders` 表按 `client_order_id` 查重，同一 cid
本就不会二次提交；且纸面成交是同步全量成交，仓位已平，再发也是 noop。

### r3-4（SHOULD FIX 4）迁移 README 两处引证/方向错误 —— **已核实并改正**

* `trade_reconciliation_jobs` **引错了表**：r2 写"`binance_futures.py:2926` 的
  '同开仓已收口'护栏按 `account_ref` 过滤，漏迁会重复生成 lifecycle"。实测
  `:2926` 那条护栏查的是 **`position_lifecycles`**（`WHERE environment=? AND
  account_ref=? AND symbol=? AND position_side=? AND status='CLOSED'`），且该表
  本来就在清单里。**自行核实**：全仓对 `trade_reconciliation_jobs` 的读取只有三类
  ——`SELECT DISTINCT symbol ... WHERE status IN ('PENDING','RETRY','RUNNING')
  AND next_retry_at<=?`（不按账户）、`WHERE job_id=?`（`attempt_count`，两处）、
  `GROUP BY status`（只按 `environment`）——**没有任何按 `account_ref` 过滤的读取**。
  因此迁它是**一致性/整洁**（同一账户的记录不跨两个域），**不是**功能必需；
  清单保留它无害，但理由已按事实改写（见下"迁移方案"表）。
* `income_*` 的**方向写反**：r2 写"只改 `account_ref` 而不改 `account_ref_norm`
  会让唯一索引仍视作**两个**域"。事实相反——唯一键**只建在 `*_norm` 列上**
  （`UNIQUE(environment_norm, account_ref_norm, income_type_norm, tran_id_norm)`
  等），只改 `account_ref` 不改 `account_ref_norm` 的话，旧域与新域仍被归一化列
  视作**同一**分区，迁移对幂等键**完全没有效果**。已改正。

### r3-5（SHOULD FIX 5）巡检失败无告警 —— **已加告警**

`_sweep_stale_exit_reservations` 此前把巡检异常整体吞掉（`return ()`），
"巡检长期失败 → 该标的不可退出"没有运维可见性。现在异常分支会记一条
`futures_exit_reservation_sweep_failed`（warn，含异常类型），**且告警自身的 db
失败也不会抛回退出路径**；06 补丁同步加了中文映射（`smoke_ui` 的映射完整性检查
要求）。前后对照（`evidence/probe_r3_sweep_alert.py`）：
```
BEFORE（r2） events=[]                                                     FAIL 5_sweep_failure_is_alerted
AFTER （r3） events=['futures_exit_reservation_sweep_failed']              PASS
             db 坏掉时 res_skip=False（退出不受影响）                       PASS
```

### r3-6（SHOULD FIX 6）默认 `autocrlf=true` 的 EOL 问题 —— **已在本文档顶部写明**

见顶部"⑥ 应用配置"。补丁对两种配置都能 `apply --check`，但**默认配置会把三条纯 LF
文件整文件转 CRLF**，因此要求统一下用 `-c core.autocrlf=false -c core.eol=lf`，
并附 `git diff --stat` 自检。

## K03 幂等键与既有数据：**可执行迁移方案**（r3 修正引证）

- **K03 幂等键** = `(environment, account_ref, income_type, tran_id)`，分区维度里
  含 `account_ref`；`execution_fills`/`position_lifecycles`/`release_observations`
  的唯一键同样含它。
- **未声明账户**：取值仍是 `sha256(api_key)[:16]`，既有行完全不受影响（与 base 逐字
  一致，回归证据见下）。
- **一旦声明 `MTA_ACCOUNT_REF`**：同一账户的历史行会落在两个域，**同一 `trade_id`
  在两域各一行，唯一索引拦不住**——"声明后旧数据不可幂等"是**已被真实 schema 复现
  的结论**（A_r1 §3.2）。因此正确顺序是 **先迁移、后声明**。
- **r1 的迁移 SQL 不可执行**（A_r1 §3.3/§3.4 实测）：逐表 `UPDATE` 会在
  `execution_fills`/`position_lifecycles` 上撞 UNIQUE 约束、非原子、不可重入；
  `lifecycle_fill_allocations` **没有 `account_ref` 列**（靠 `lifecycle_id` 关联），
  对它 `UPDATE` 是无效指令。
- **可执行方案（归属 U13；本包不含脚本，也不改表结构）**：

  **A. 表与唯一键清单（以真实 schema 为准）**
  | 表 | 迁移列 | 冲突键 | 处理 |
  |---|---|---|---|
  | `execution_fills` | `account_ref` | `(environment, account_ref, trade_id)` | 查重→**语义比对**→合并/删除重复行 |
  | `position_lifecycles` | `account_ref` | `(environment, account_ref, symbol, position_side)` | 两域各有 ACTIVE 即**冲突**，人工裁决，不得自动合并 |
  | `trade_reconciliation_jobs` | `account_ref`（NOT NULL） | 无账户相关唯一键（部分唯一索引 `idx_active_trade_reconcile_job(lifecycle_id) WHERE status IN (...)` 与账户无关） | 直接迁。**理由（r3 核实改正）**：全仓没有按 `account_ref` 过滤的读取（只有"按 `status`/`next_retry_at` 取到期 symbol""按 `job_id` 取 `attempt_count`""按 `environment` 汇总状态"三类），漏迁**不会**重复生成 lifecycle；迁它是**一致性/整洁**，不是功能必需 |
  | `income_events` / `income_coverage` / `income_conflicts` | `account_ref` **+ `account_ref_norm`** | K03 幂等键（`*_norm` 列） | **两列必须同批更新**：唯一键只建在 `*_norm` 列上，只改 `account_ref` 不改 `account_ref_norm` → 旧域与新域仍被视作**同一**分区，迁移对幂等键无效（r3 改正方向） |
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

## r2 修复（对应 A_r1，保持有效）

1. **P0 退出预留泄漏 → 该标的永久不可退出**（`binance_futures.py`）：
   `_close_or_reduce` 的整个占量后主体进入 `try/finally`，异常路径 `finally` 释放
   预留并记 `futures_exit_reservation_released`；正常路径按 `close_unconfirmed`
   结算/释放（`settled` 标志，只能二选一）。**另一半**在管理器：`release_exit`
   成功后撤销该 `exit_key`（"释放 = 这次退出没有发生"→ 同键必须能重发），并新增
   `reclaim_stale_exits`/`in_flight_ttl_ms`：同键预留仍是 `ACTIVE` 且超过 TTL 未结算
   时按泄漏回收（`STALE_EXIT_RESERVATION_RECLAIMED`，有审计）；`_reserve_exit` 每轮
   先巡检一次，回收时记 `futures_stale_exit_reservation_reclaimed`。**已 `CONSUMED`
   （成交已确认）的键永不回收**，幂等语义不变（r3 把这条声明补成"记录被淘汰后仍
   成立"，见 r3-1）。
2. **P1-2** `_open` 里引用未定义 `reservation_key` 的行已删除（qty≈0 恢复优雅跳过）。
3. **P1-3** 纸面回退退出键改为 `paper-exit:<symbol>:<side>:<本轮令牌>:<指纹>`：跨轮次
   各自独立（原来恒稳键会让同名标的的第二次退出永久被吞）。**同轮抑制**由守卫
   `confirm_exit` 结算提供（r2 曾写"同指纹共享一个键保持抑制"，与事实相反，r3 已修，
   见 r3-3）。
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
8. **幂等/审计记忆有界**：`_exit_keys` 超过 `MAX_EXIT_KEYS=2000` 时只淘汰**已终结**
   键（ACTIVE 永不淘汰），`_released_exits` 为 200 条环形缓冲；账本内终结预留上限
   2000（`MAX_TERMINAL_RESERVATIONS`）。

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
- **P2-11**：`release_exit` 撤销键后，`FILL_UNCONFIRMED` 的下一轮会**重发**同一
  稳定 cid。有效性依据：交易所对仍存活的同一 `newClientOrderId` 会拒绝重复单
  （`_submit_standard_order` 会走确定性拒绝路径），且 `reduce_only=True` 不会反向
  开仓、提交量被可减量夹逼。**代价**：极端情况下可能对同一笔退出多发一次减仓单
  （不会反向、不会超仓）。取"可退出"优先于"绝不重发"，否则就是 P0。
- **P3-1/3-2/3-4/3-5/3-6/3-7/3-8/3-9**：未改（与环境前缀口径、域键外观、
  `record_hard_exit` 无去重、`FlatCloseDecision` 三个 False 是默认值、U15 绑定可选、
  `release_observations` 静默丢弃、纸面/实盘键形状差异、`reconcile_position` 部分
  裁剪切算入 `consumed_qty` 等）。其中 3-7 已在迁移节写成运营动作，其余为一致性/
  外观项，不阻塞。
- **P3-NEW（观察，未修）**：`_sweep_stale_exit_reservations` 的兜底回收发生在
  `_reserve_exit` 里，即"只有再收到该标的的退出请求时才会巡检"。若某标的长期没有
  新退出请求，其泄漏预留不会被任何东西回收——这不是"持仓不可退出"（没有新请求
  就无需退出），且已由 r3-5 的告警给出可见性；真正的周期巡检应由上层调度接入。

## SCRIPTS 登记行（I 应用到 `scripts/run_offline_regression.py`）

加在 `"scripts/smoke_u04_execution_faults.py",` 之后：

```python
    # U02（B 持仓与执行）：策略仓管理/有界退出授权/并发退出预留。全程离线。
    "scripts/smoke_u02_strategy_position_manager.py",
```

## 验证（原始输出可复现）

沙箱：`pristine_int` / `sb_int` = `git archive I@b4eeda9` + U02 独占文件
（未打补丁 / 打 01..06）+ README 的登记行；`sb_r2` = 同样基线 + r2 包的 6 补丁 +
`4856aec` 的独占文件（③/⑤ 的前后对照用）。证据与脚本在 `evidence/`。

```
git apply --check -p1                     # 6/6 OK，两种配置各跑一遍：
  -c core.autocrlf=false -c core.eol=lf   → 01..06 全 OK
  默认（本机 system autocrlf=true）        → 01..06 全 OK
补丁内容核对：应用后 6 个目标文件与生成树**逐字相同**（LF 归一化比较）
逐条非 fail-fast 全量离线回归（80 条 = 79 + U02）：
  pristine_int : FAILED=0 []
  sb_int       : FAILED=0 []
  两组逐条状态行 `diff` = 空（新增失败集合差集 = {}）
                    # evidence/out_delta_pristine.txt / out_delta_sb.txt
测试与冒烟（均在 sb_int）：
  tests/integration/test_strategy_position_manager.py  → PASS=158 FAIL=0（exit 0）
  scripts/smoke_u02_strategy_position_manager.py       → U02 SMOKE OK（exit 0）
     正向 158/0；N1 负向对照 156/2（account_ref 漂移，必须失败）；N2 负向对照
     150/8（重复退出反向开仓，必须失败）
  scripts/smoke_contracts_v3.py                        → PASS 173 / FAIL 0
  scripts/smoke_ui.py                                  → 通过 52 / 失败 0（含新增事件映射）
                    # evidence/out_verify_sb_int.txt / out_verify_smoke_ui.txt /
                    #          out_apply_check.txt
r3 前后对照探针（evidence/，全部 FAIL→0）：
  probe_r3_consumed_reuse.py  → BEFORE FAIL=5 → AFTER FAIL=0（r3-1/r3-2）
  probe_r3_paper_round.py     → BEFORE FAIL=3 → AFTER FAIL=0（r3-3）
  probe_r3_sweep_alert.py     → BEFORE FAIL=1 → AFTER FAIL=0（r3-5）
```

## 复现步骤（沙箱全部落在作者工作区内，不写仓库文件）

```bash
B="C:/Users/Tao/Desktop/币安/mat-wt/B"; I="C:/Users/Tao/Desktop/币安/mat-wt/I"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
mkdir -p "$B/_scratch_B/r3/pristine_int"
git -C "$I" archive b4eeda9 | tar -x -C "$B/_scratch_B/r3/pristine_int"
for f in src/core/strategy_position_manager.py src/core/management_authorization.py \
         tests/integration/test_strategy_position_manager.py \
         scripts/smoke_u02_strategy_position_manager.py; do cp "$B/$f" "$B/_scratch_B/r3/pristine_int/$f"; done
# 再把 README 的 SCRIPTS 登记行加进 pristine_int/scripts/run_offline_regression.py
# （沙箱必须是独立 git 仓，否则子目录内的 git apply 会静默不动作）
(cd "$B/_scratch_B/r3/pristine_int" && git init -q . && git add -A && git commit -qm base)
cp -r "$B/_scratch_B/r3/pristine_int" "$B/_scratch_B/r3/sb_int"
for p in 01 02 03 04 05 06; do (cd "$B/_scratch_B/r3/sb_int" && \
  git -c core.autocrlf=false -c core.eol=lf apply -p1 \
  "$B/docs/dispatch/patches/U02/<本包目录>/${p}_"*.diff); done
cd "$B/_scratch_B/r3/sb_int" && "$PY" tests/integration/test_strategy_position_manager.py
"$PY" scripts/smoke_contracts_v3.py && "$PY" scripts/smoke_ui.py
"$PY" "$B/docs/dispatch/patches/U02/<本包目录>/evidence/probe_r3_consumed_reuse.py" .
"$PY" "$B/docs/dispatch/patches/U02/<本包目录>/evidence/probe_r3_paper_round.py" .
"$PY" "$B/docs/dispatch/patches/U02/<本包目录>/evidence/probe_r3_sweep_alert.py" .
"$PY" "$B/docs/dispatch/patches/U02/<本包目录>/evidence/run_delta_r3.py" "$B/_scratch_B/r3/pristine_int"
"$PY" "$B/docs/dispatch/patches/U02/<本包目录>/evidence/run_delta_r3.py" "$B/_scratch_B/r3/sb_int"
# ③/⑤ 的前后对照树 sb_r2 = 同基线 + r2 包的 6 补丁 + 4856aec 的独占文件（同法构建）
```

## 回滚

- 补丁层：`git apply -R`（逆序 06→01）即可完全回退，无 schema/迁移副作用。
- U02 独占模块与补丁无耦合：未应用补丁时 `getattr` 探测使既有行为不变；已应用后
  回退补丁，账本 `account_ref` 立即回到密钥摘要形态（**已迁移的历史行不受影响**，
  因为迁移不在本包内）。
- 生产已声明 `MTA_ACCOUNT_REF` 且已迁移：回滚补丁**不等于**回滚数据，需恢复
  recovery bundle。

## 已知限制

- 未接真实交易所：全部证据是代码路径级（桩掉网络/账本副作用），未下单、未撤单、
  未联网、未读 `.env`/`data/`/密钥。
- P0 触发频率未量化；`_open` 的 qty≈0 分支可达性依赖上游过滤（未穷举）。
- R09/预留回收只有单进程证明；多进程/多实例共享同一账本不成立（P2-9）。
- **r3 新增**：`ManagementReservationLedger._consumed_ids`（成交事实）**只增不淘汰**
  ——这是"已成交的退出永不可重开"这条硬不变量的代价。单个 id 只是一个短字符串
  （远轻于整条 `ManagementReservation` 记录），但长进程/纸面模式下会随不同
  `exit_key` 数量线性增长；账本新增 `consumed_id_count` 供运维观测。若将来要回收
  它，必须换成"持久化的已消费记录"（例如落库/落盘）后再回收，否则 r3-1 会复发。
- `in_flight_ttl_ms` 默认 900s：比任何合理"提交+等成交"窗口长得多，但仍是一个
  经验值；若某次退出合法地超过该时长，会被回收并允许重发（见 P2-11 的代价说明）。
- `04`/`05` 的 TTL/令牌机制只在集成态生效（作者分支无共享文件）。
