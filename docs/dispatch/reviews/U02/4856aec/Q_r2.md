# Q 独立复验 · U02 修复轮 r2

- 复验对象：`task/U02` @ **`4856aec`**（base `45ebdff`；上一轮审 `5b71db4`）
- 我的工作区（独立 detached）：`mat-wt/reviews/U02/R2/r1`（HEAD=`4856aec`，`git status` clean）
- 上一轮报告：`mat-wt/I/docs/dispatch/reviews/U02/5b71db4/A_r1.md`
- 补丁目录（交付物，在 B 工作区）：`mat-wt/B/docs/dispatch/patches/U02/2fe7b2a/`（6 条）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（3.13.12，`PYTHONUTF8=1`）；**无 pytest**
- 沙箱（全部在 `_scratch_Q/`）：
  - `pristine_int` = `git archive I@59660b2`（我在会话开始时读到的 I HEAD）+ U02 独占文件（4856aec）
    + README 的 `run_offline_regression.py` 登记行（**登记行不在补丁里**，见 P2-10）
  - `sb_int` = `pristine_int` + r2 补丁 01..06（`-c core.autocrlf=false -c core.eol=lf`）
  - `sb_int2` = 同上但用默认 git 配置
  - `sb_old_full` = `pristine_int` + **r1 独占文件（`5b71db4`）** + r1 补丁 01/02/04/05/06（03 对当前 I HEAD 打不上，与无 P0 无关）
  - `sb_old`/`sb_old2` = `pristine_int` + r1 补丁（保留 r2 独占文件的混合态，仅作旁证）
- 探针：`probes/probe_q*.py`（全部自写）；原始输出：`out_*.txt`

> 说明：`git apply` 的 `--check` 结果见 §6.1。`sb_old_full` 是**忠实的前后对照**（r1 的独占文件 + r1 的共享补丁 vs r2 的独占文件 + r2 的共享补丁）。

---

## 1. P0（预留泄漏 → 域永久不可退出）：**真闭合**（前后对照 FAIL=8 → FAIL=0）

探针 `probes/probe_q0_p0.py`（自写，只桩网络/账本副作用，跑真实 `_close_or_reduce` /
`_reserve_exit` / `_settle_exit` / `_release_exit` / `StrategyPositionManager.request_exit`；
不联网、不下单撤单）。原始输出 `out_q0_sb_old_full.txt` / `out_q0_sb_int.txt`。

| 用例（反例设计） | r1（`sb_old_full`） | r2（`sb_int`） |
|---|---|---|
| A 占量后 `_submit_standard_order` 抛确定性拒绝 | 预留 `ACTIVE remaining=1.0`，`available=0` | `RELEASED`，`available=1.0`，审计事件 `futures_exit_reservation_released` |
| B 换新 key 重试退出 | `NOOP` `{EXIT_EXCEEDS_POSITION,NO_POSITION_TO_EXIT}` | `SUBMIT_REDUCE qty=1.0` |
| C 同稳定 key（guardian `mta-g-<sha24>`）重试 | `duplicate_skipped / DUPLICATE_EXIT_IGNORED` | `close`（真提交，submitted=2） |
| D 第 1 轮拒绝、之后交易所恢复（同一进程多轮） | `duplicate_skipped×3`，submits=1 | 第 2 轮 `close`，submits=2 |
| E 异常在**占量前**（仓位查询失败） | 不产生任何预留（两侧一致） | 同左 |
| F 异常在**成交后、结算前**（`_resync_protection` 抛错，仓位已归零） | 预留 `ACTIVE` 泄漏（**即使成交已确认也泄漏**） | `RELEASED` |
| G 仓位不变时连续 3 轮退出 | `NOOP,NOOP,NOOP`（域被占死） | `SUBMIT_REDUCE,NOOP,NOOP` |
| H 泄漏后仓位缩小（外部平仓）再退出 | `NOOP`（reconcile 溢出把漏的预留转 RELEASED，但仓已为 0） | `NOOP` |

```
# out_q0_sb_old_full.txt（r1）
INFO A_ledger={'available': '0', 'reserved': '1.0', 'position': '1.0', 'res': [('mta-g-aaaaaaaa','ACTIVE','1.0','0')]}
FAIL B_new_key_can_exit_after_exception {'action': 'NOOP', 'reasons': ['EXIT_EXCEEDS_POSITION','NO_POSITION_TO_EXIT']}
INFO F_raised=RuntimeError submits=1 ledger={'available': '0', ... 'ACTIVE' ...}
Q_P0_PROBE FAIL=8   # A_release_audit_event_logged / A_reservation_not_leaked / A_available_restored /
                    # B_new_key_can_exit / C_stable_key_resubmits / D_exit_channel_recovers /
                    # F_post_fill_error_releases / G_domain_not_permanently_blocked
# out_q0_sb_int.txt（r2）
INFO A_ledger={'available': '1.0', 'reserved': '0', ... 'RELEASED' ... 'reason_code': 'EXIT_PATH_ERROR'}
INFO A_audit_events=['futures_exit_reservation_released']
Q_P0_PROBE FAIL=0
```

**绕过尝试（逐阶段）**：占量前（E）、占量后提交前（F=收口段）、提交中（A/C/D）、
收口后（F）——r2 全部释放；占量前失败本来就不产生预留（两侧一致）。
**边界**：`_close_or_reduce` 在 `close_qty<=0` 时的提前 `_release_exit` 仍在 `try` 之外，
但它前面没有任何可抛出点（`pos`/`current` 已取过），不构成新泄漏面。

### 1.1 TTL 回收 / 崩溃重启 / CONSUMED 反向风险 / 并发（`probes/probe_q0_ttl.py`）

```
PASS 1_ttl_minus_1_not_reclaimed NOOP                     # 差 1ms 不回收
PASS 1_at_ttl_exactly_reclaimed_and_resubmitted           # 恰好 == TTL 即回收+重发（>= 语义）
PASS 1_clock_rollback_does_not_reclaim NOOP               # 时钟回拨 fail-closed
PASS 1_now_zero_no_reclaim / 1_zero_ttl_no_reclaim        # now=0 / ttl<=0 -> 严格幂等
PASS 2_sweep_reclaims_only_stale ('k-old',)               # 只回收过期的，fresh 保留
PASS 3_consumed_never_reclaimed_with_intact_ledger NOOP   # 账本条目在位时 CONSUMED 不回收
PASS 6_in_process_state_lost_on_restart_no_cross_process_leak
PASS 7_sweep_reclaims_old_active_and_resubmits + 7_reclaim_audit_event_logged
      events=['futures_stale_exit_reservation_reclaimed']
PASS 8_concurrency_invariants_hold (8 线程 × 400 次 reclaim/reserve/confirm/release 混沌, errors=0)
PASS 8_no_over_reservation
FAIL 4_consumed_key_stays_duplicate_even_when_ledger_record_pruned
FAIL 5_release_is_idempotent_on_consumed_reservation
```

- **进程崩溃后是否真回收**：**该场景不存在**。U02 的账本/退出键全部是**进程内易失**状态
  （`TradingService.__init__` 重建管理器；全仓无任何把预留/退出键落库的代码——
  `grep` 未发现 INSERT 预留状态的语句）。所以"占量后崩溃"重启后**没有**要吃 TTL 的残留；
  TTL 回收只对**进程内**丢失的预留有意义（而 r2 的 `finally` 已使这条路径不再是主要防线）。
  实测（用例 6）：重启后同 key 直接 `SUBMIT_REDUCE`。→ **TTL 是纵深防御，不是崩溃恢复机制**。
- **新发现 F1（P2，未登记）**：`4_...` 用例证明 **"CONSUMED 键永不回收" 并不成立**。
  当同一 lifecycle 的账本条目数超过 `MAX_TERMINAL_RESERVATIONS=2000` 被淘汰掉该 CONSUMED
  记录后，`_reclaim_if_stale_locked` 走 "预留已不在账本里" 分支（`:708-712`），只在
  `now-registered >= TTL` 时就把键回收 → 同 key **重新占量并重发**：
  `INFO 4_after_prune_same_key={'action': 'SUBMIT_REDUCE', 'duplicate': False, 'reasons': ['STALE_EXIT_RESERVATION_RECLAIMED']}`。
  与 README r2-1「已 `CONSUMED`（成交已确认）的键永不回收」和 `strategy_position_manager` 模块
  docstring（`:24-25`）**直接冲突**。可达前提：**单个 lifecycle 域累计 >2000 条终结预留**
  （即同一 env/account/symbol/side 上出现 >2000 个不同 exit key；实盘无 cid 的退出用
  `uuid4()[:16]`，键基数无界，长期运行可达）。后果有界：只会多一次 `reduce_only` 减仓提交，
  且被 `min(请求量, available)` 夹逼（不会反向开仓、不会超仓）；下一轮 `request_exit`
  以交易所权威仓量校正后即自愈。
- **新发现 F2（P2，未登记，当前无生产调用方）**：`ManagementReservationLedger.release()` 对
  **CONSUMED** 条目**不是幂等**的：`INFO 5_before=CONSUMED release_accepted=True after=RELEASED
  re_reserve_accepted=True` —— 与它的 docstring「对已终结预留幂等」不符，且释放后同 id 可再次
  `reserve`（等于"撤销已确认的成交"）。`StrategyPositionManager.release_exit()` 保留 `_exit_ledgers`
  映射，所以调用它同样能解锁一个 CONSUMED 键。当前共享补丁里没有对已确认键调用 release 的路径
  （`_close_or_reduce` 用 `settled` 互斥），属**潜在**缺陷；但它使"CONSUMED 永不可重开"缺少一层
  硬保护，属 F1 的同源问题。

---

## 2. P1 两项

### 2.1 P1-2 `_open` 未定义 `reservation_key`：**已真修**（静态 + 动态）

`probes/probe_q1_nameerr.py`（自写：`symtable` 静态扫 + 桩掉前置调用后动态跑 `_open(qty=0)`）

```
# sb_old_full
INFO static_unbound_in_open=[('_open', 'reservation_key')]     FAIL S_open_has_no_unbound_global
INFO dynamic_open_qty0 result='unset' error="NameError: name 'reservation_key' is not defined"
# sb_int
INFO static_unbound_in_open=[]                                  PASS S_open_has_no_unbound_global
INFO dynamic_open_qty0 result=None error=''                     PASS D_open_qty0_graceful_skip
```
（r1 的该行在 `sb_old_full/src/exchange/binance_futures.py:1430`；r2 的 `_open` 体
`1342..2003` 内已无 `_release_exit`/`reservation_key`。）**可达性仍未量化**（qty≈0 的 open
意图能否到 `_open`，取决于上游过滤）——与作者登记一致。

### 2.2 P1-3 纸面回退退出键：**"跨轮不再被永久吞" 修好了，但 "同轮抑制" 反而坏了**

`probes/probe_q2_paperkey.py`（自写，直接驱动 `_exit_idempotency_guard`，r1/r2 兼容）

| 用例 | r1（`sb_old_full`） | r2（`sb_int`） |
|---|---|---|
| 同轮 同名同量同动作 第二次 | `duplicate_skipped` ✓ | **`EXECUTED`** ✗（键 `paper-exit:BTCUSDT:LONG:r1:na:close:1`） |
| 跨轮 第二次退出 | `duplicate_skipped` ✗（A_r1 的 P1-3） | `EXECUTED` ✓ |
| 连续 5 轮 | `[EXECUTED, dup×4]` ✗ | `[EXECUTED×5]` ✓ |
| 同轮 部分平 0.3 → 0.7 | `[EXECUTED, dup]` ✗ | `[EXECUTED, EXECUTED]` ✓ |
| 同轮 重复同一笔 0.3 | `dup` ✓ | `EXECUTED` ✗ |
| 跨标的 + 跨轮 | 第 2 轮被吞 ✗ | 全部独立 ✓ |
| 显式 cid（观察） | 第 2 次 `duplicate_skipped` | 第 2 次 `EXECUTED`（**但** `execute()` 先用 `orders` 表
按 `client_order_id` 查重 → 生产路径仍被拦） |

**结论（两侧都坏过，方向相反）**：
- r1：同轮抑制有效、跨轮永久吞 → A_r1 P1-3 成立，r2 已修。
- r2：跨轮已修，但 **README r2-3 声称的"同轮同指纹仍共享一个键（保持抑制）"与事实不符**：
  键确实同指纹共享，可 `_exit_idempotency_guard` 接受后立刻 `manager.release_exit()`，
  而 r2 的 `release_exit` 会**撤销该键**（这正是 P0 另一半的修法）→ 同轮第二次同名请求
  查不到重复 → 再次 `SUBMIT_REDUCE` → 纸面退出被**执行两次**。
- **影响有界（我实测了闭环）**：纸面 `_close_or_reduce` 每次重新 `_find_position`
  且 `close_qty = min(intent.qty, pos["qty"])`，第一次平掉后仓位已减/移除 → 第二次是
  `noop`/减剩余：**不会反向开仓、不会超量平仓**。所以这是"幂等声明失真 + 纸面证据可读性"
  问题，不是交易安全漏洞。但它恰好落在 A_r1 指出的痛点上（纸面路径的幂等证据不可信）。
- 建议：把"本轮令牌"维度的抑制做实（例如 guard 在 release 后把键留在"本轮的已执行集合"里，
  或让纸面路径用 `confirm_exit` + 下一轮 `reconcile` 而不是立即 release），或**改写
  README/注释为事实**："同轮抑制依赖 `orders` 表（显式 cid）与仓量夹逼，管理预留不再提供同轮抑制"。

---

## 3. 第 8 项（账本 `account_ref` 迁移方案）：**硬结论 = 可执行**（附 1 处引证错误）

探针 `probes/probe_q3_migrate.py`（真实 `AuditDatabase` schema + 临时 DB 在 `_scratch_Q/tmp/`，
不读 `.env`/`data/`/密钥）。原始输出 `out_q3.txt`（`Q_MIGRATE_PROBE FAIL=0`）。

### 3.1 清单完整性（逐表实测，不是照抄）
```
1_tables_with_account_ref=['execution_fills','income_conflicts','income_coverage','income_events',
                           'position_lifecycles','release_observations','trade_reconciliation_jobs']
1_tables_with_account_ref_norm=['income_conflicts','income_coverage','income_events']
1_total_tables=47
PASS 1_readme_table_list_complete {'missing_from_readme': [], 'not_a_table': [], 'norm_tables_missing': []}
PASS 1_lifecycle_fill_allocations_has_no_account_ref False
```
→ 全库 47 张表里带 `account_ref` 的**恰好是这 7 张**，README 清单**无遗漏、无多余**；
`account_ref_norm` 只在 3 张 income 表；`lifecycle_fill_allocations` **确实无该列**（列出它是无效指令这一点 r2 已改正为"不迁"）。**清单完整。**

### 3.2 冲突键与真实唯一索引逐字一致
```
execution_fills      : idx_execution_fills_trade  = [environment, account_ref, trade_id]        ✓
position_lifecycles  : idx_active_position_lifecycle = [environment, account_ref, symbol, position_side] ✓
release_observations : [release_id, environment, account_ref, observed_minute]                  ✓
income_events        : UNIQUE(environment_norm, account_ref_norm, income_type_norm, tran_id_norm)
income_coverage      : UNIQUE(environment_norm, account_ref_norm, income_type_norm, start_ms, end_ms)
income_conflicts     : UNIQUE(environment_norm, account_ref_norm, income_type_norm, tran_id_norm, incoming_content_hash)
trade_reconciliation_jobs : idx_active_trade_reconcile_job=[lifecycle_id](partial), PK=[job_id]  # 无 account_ref 唯一键
```
→ README 的"冲突键"栏与实际索引**逐字一致**。
**`BEGIN IMMEDIATE` 是否足够 / `account_ref_norm` 是否与其他唯一键一致**：
- income 三表的唯一键**只建在 `*_norm` 列上**（不是 `account_ref`），所以"两列同批更新"是**必需**的：
  只改 `account_ref` 而不改 `account_ref_norm` → 唯一索引仍按归一化列去重 → 旧域与新域**仍被视作同一分区**，
  迁移对幂等键**完全没有效果**。README 把这一点写成"否则唯一索引仍视作两个域"——方向写反了（应是"仍视作**同一**域"），
  但结论（必须同批更新）正确。属**表述瑕疵**，不改变可执行性。
- 单事务 `BEGIN IMMEDIATE` 对本方案足够：所有迁移语句都是同库同表 UPDATE/DELETE，SQLite 单写者模型下
  `IMMEDIATE` 会立即取写锁，避免"读到旧域后又有人写新域"的窗口；实测可重入（见 3.4）。

### 3.3 字面 UPDATE 会撞唯一索引（r1 方案不可执行，已被证实且 r2 已改）
```
4_naive_update[execution_fills]=IntegrityError: UNIQUE constraint failed: execution_fills.environment, account_ref, trade_id
6b_naive_update_position_lifecycles=IntegrityError: UNIQUE constraint failed:
    position_lifecycles.environment, account_ref, symbol, position_side
```
r2 的改法（先查重→显式裁决→再 UPDATE，全部在单事务内）**实测可执行且可重入**：
```
5_dedupe_then_update_execution_fills=ok deleted_dupes=2
5_rerun_idempotent=ok deleted_dupes=0          # 重复执行是 no-op
5_distinct_domains_after=['demo:acct-declared']
6c_adjudicated=ok adjudicated_dropped=1 keep=demo:acct-declared
6c_adjudication_reentrant=ok adjudicated_dropped=0
6c_domains_after=['demo:acct-declared']
```

### 3.4 "不可裁决即中止"可操作性
- 可裁决类（`execution_fills`：同 `trade_id` 两域各一行，内容可语义比对）→ 自动去重可做（上面已实现）。
- 不可裁决类（`position_lifecycles`：两域各有同标的同侧的 OPEN 生命周期）→ 实测**可确定性检出**：
  `SELECT environment,symbol,position_side,COUNT(*) ... HAVING COUNT(*)>1` 对 (env, symbol, side) 分组即可列出冲突清单；
  `idx_active_position_lifecycle` 是按 `account_ref` 分区的**偏索引**，所以冲突不会被数据库拦住、必须由脚本裁决 —— 这正是 README 说的"人工裁决，不得自动合并"，
  且脚本能**在动手前**打印冲突清单并中止。**该判定可操作。**
- 唯一需要注意的运营前提：`position_lifecycles` 的冲突裁决需要**场外事实**（哪个生命周期代表当前真实持仓），README 已写成"人工裁决"，未假装可自动化 —— 口径诚实。

### 3.5 回滚(bundle) 可用性
```
PASS 7_recovery_bundle_scripts_exist   scripts/create_recovery_bundle.py + scripts/restore_recovery_bundle.py 都在
7_bundle_copies_db=True includes_state_json=True has_checksum=True
```
- `create_recovery_bundle.py` 复制 DB + 4 个必需 state JSON + 校验和，且要求停写（`InstanceLock`）→ 与 README §B①"停服务"一致；`restore_recovery_bundle.py` 默认只预览。**回滚途径真实存在且可用。**
- README §D 的"成功后不要在迁移后反向改回（会再撞唯一键）"与 §3.3 的实测一致 → **正确且必要**。

### 3.6 引证瑕疵（P3，需要改文字）
README 把 `trade_reconciliation_jobs` 的迁移理由写成"`binance_futures.py:2926` 的同开仓已收口护栏按 `account_ref` 过滤"。实测：
- 未打补丁树 `binance_futures.py:2926` 的那条护栏查的是 **`position_lifecycles`**（`... AND status='CLOSED' ORDER BY closed_at_ms`），
  不是 `trade_reconciliation_jobs`；`position_lifecycles` 本来就在清单里。**理由引错了表。**
- 全仓对 `trade_reconciliation_jobs` 的读取只有三处：`status IN (...)`/`next_retry_at`（不按账户）、按 `job_id`、
  以及 `trade_reconciliation_status` 按 `environment` 汇总 —— **没有任何按 `account_ref` 过滤的读取**
  （`PASS 2_no_jobs_query_filters_by_account_ref`）。
→ 结论：迁 `trade_reconciliation_jobs` 属**一致性/整洁**（避免同一账户数据分处两域）而**不是**"不做就会重复生成 lifecycle"的功能必需。
  清单保留它无害，但理由必须改成事实，否则 U13 会按错误的风险模型排优先级。

### 3.7 阻断级登记：**已写入交付物**
`README.md:79`：`【阻断级登记】在可执行迁移脚本落地并验证前，不得在生产启用 MTA_ACCOUNT_REF。`
并附"`MTA_ACCOUNT_REF` 一旦被 `.env`/部署脚本设置，01 会在**无人决策**的时刻切换数据分区"。**确认存在且措辞是阻断级。**

### 3.8 硬结论
**可执行。** 表/唯一键清单完整且逐字核对无误；"先迁移后声明"的顺序、单事务 + 去重 + 显式裁决 + 可重入
四个要素都在真实 schema 上跑通了；回滚途径（recovery bundle）真实可用；"不可裁决即中止"可确定性检出。
唯一必须改的是 §3.6 的**引证**与 §3.2 的 income 表"方向写反"一句（都是文档表述，不影响可执行性）。
唯一**未**验证的是"这套方案被写成脚本后在**生产库规模**上的耗时/锁时长"（我没有生产库，也不允许读 `data/`）——属 U13 落地时验证。

---

## 4. 约束 1 的事实改写：**准确**

探针 `probes/probe_q4_constraint1.py`（自建 `only01` / `only02` 沙箱）。原始输出见下（`Q_C1_PROBE`）。
```
pristine_int + MTA_ACCOUNT_REF=acct-live : account_ref='demo:5e318f8cf9cbe249'  (env 被忽略，未变)
only01       + MTA_ACCOUNT_REF=acct-live : account_ref='demo:acct-live'         account_ref_source='declared'
sb_int(01..06)+ MTA_ACCOUNT_REF=acct-live : account_ref='demo:acct-live'         broker 侧 account_ref 属性也在
only02       : ImportError: cannot import name 'account_ref_source' from 'exchange.trade_reconciliation'
未声明时 only01 == pristine 逐字一致（'demo:5e318f8cf9cbe249'）
```
→ README r2 的改写"**只上 01 就已经切换账本分区**（且没有 02 的派生点 / `account_ref_source` / 03 的启动自检 → 静默改分区）"**与实测一致**；
"只上 02 直接 `ImportError`"也**与实测一致**。A_r1 的 P1-4 已按事实修正，**改写准确**。

---

## 5. P2-6：`enforce=True` 现在 fail-closed：**不再"一开就全停"**

探针 `probes/probe_q5_enforce.py`（自写；真实 `TradeIntent`/`TradeDecision`/`RiskVerdict`）。
```
premise : TradeIntent.model_fields 无 management_* / model_config={} → 传 management_authorization_id 被静默丢弃
r2  : enforce=True → ManagementEnforcementError: MANAGEMENT_ENFORCEMENT_UNSUPPORTED，且 db 零事件（没有摘除副作用）
      enforce=False（默认）→ 意图集合不变，只记 management_intent_unbound
      带 management_authorization_id 字段的意图：绑定=保留；未绑定=摘除且**不降级为 open**（adjusted 也为空）
      门仍无生产调用方（grep 只命中 decision_pipeline.py 自身）
r1  : enforce=True → close 意图被静默摘掉，决策落成 'open'（只剩开仓意图）→ 正是"一开就全停"
```
→ **确认不再"一开就全停"**：现在是**抛错 + 不产生任何摘除副作用**；默认值安全（`enforce=False` 行为不变）。
README r2-4 同时如实登记了"该门仍无生产调用方"。**判定：闭合（P2 降级为已登记残留项）。**

---

## 6. 补丁与 delta

### 6.1 两种 git 配置下的逐条 `apply --check`（自跑，见 `out_setup.txt`）
```
r2 包（2fe7b2a）cfg=-c core.autocrlf=false -c core.eol=lf : 01 OK 02 OK 03 OK 04 OK 05 OK 06 OK   (6/6)
r2 包 cfg=默认(本机 system autocrlf=true)                    : 01..06 全 OK                        (6/6)
顺序应用：两种配置都 6/6 应用成功
r1 包（8c66f65，仅作对照）: 03 在当前 I HEAD 上 CHECK_FAIL（service.py 已被 U13/U05 改过，README 已声明"03 只对集成态有效"）
```
另核对（`blob` 逐字）：6 个目标文件在 `40ff52f` / `59660b2` / 当前 `25f5097` **三次 HEAD 上 blob 完全相同** → 补丁对**当前** I HEAD 仍有效。
I 工作区**磁盘上**的 EOL 与 `git archive` 逐字节一致（`binance_futures.py` 1488 CRLF / 3268 LF、`execution.py` 625/663、`app.py` 1430/2289；另 3 个目标文件纯 LF），无 `.gitattributes`，`git status` clean。

**EOL 字节精确性（自测，附表）**：三条 mixed-EOL 文件（`binance_futures.py`/`execution.py`/`app.py`）应用后**仍是 mixed**
（CRLF 1488→1716、625→691、1430→1436），两种配置下 `md5` **完全相同**；但三条**纯 LF** 文件
（`trade_reconciliation.py`/`service.py`/`decision_pipeline.py`）在**默认配置**（本机 `core.autocrlf=true`）下会被
**整文件转成 CRLF**：

```
                        pristine_int          sb_int(autocrlf=false)   sb_int2(默认 autocrlf=true)
trade_reconciliation.py crlf=0    lf=611      crlf=0    lf=657        crlf=657  lf=657   bytes 29429 -> 30086
service.py              crlf=0    lf=1560     crlf=0    lf=1722       crlf=1722 lf=1722  bytes 95299 -> 97021
decision_pipeline.py    crlf=0    lf=188      crlf=0    lf=323        crlf=323  lf=323   bytes 16273 -> 16596
binance_futures.py / execution.py / app.py：两种配置下 md5 相同（mixed 保持）
```
→ README 的"字节精确"对**补丁行**成立，但**不是"两种配置产出相同文件"**：默认配置下 3 个纯 LF 文件整文件 EOL 翻转。
（`git add` 的 autocrlf 归一化会把它折回 LF，所以**列分辨率/噪音**而非数据损坏；I 应统一按 README 的方式
`-c core.autocrlf=false -c core.eol=lf` 应用，或应用后确认 `git diff --stat` 只有真实改动行。）属 **P3 操作提示**。

### 6.2 全量回归 delta（强制项，自写 runner：`run_delta_Q.py`）
方法：`ast` 解析**该树自己的** `scripts/run_offline_regression.py` 的 `SCRIPTS`，用与 `main()` **同一套 env**
（`EXECUTION_MODE=paper`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、剔除 `MTA_PROFILE` 与 `*BINANCE/SENSENOVA*KEY/SECRET/TOKEN*`），
逐条**非 fail-fast** 执行并记录退出码。原始输出：`out_delta_pristine.txt` / `out_delta_sb.txt`。
```
pristine_int : manifest_entries=80  FAILED=0 []   elapsed≈193s
sb_int(01..06): manifest_entries=80  FAILED=0 []   elapsed≈193s
两组逐条状态行 diff → IDENTICAL_STATUS_LINES（差集 = {}）
```
- **口径声明**：`pristine_int` = `I@59660b2` + U02 独占文件（**未打补丁**）+ README 的登记行，所以本 delta 隔离的是
  **01..06 六个共享补丁**，**不覆盖** U02 独占模块自身（同 A_r1 口径）。作者称 76 条；我这里是 **80 条**，
  原因是基线不同：**I 树在会话期间前移**（我读到过 `59660b2 → 73225b92 → 25f5097`），U05 又并入 4 个 smoke。
  我**固定 `IBASE=59660b2`** 保证两棵树可比。**差集 `{}`、两组 `FAILED=0` 与作者声称一致**（KI02 已修，基线全绿）。
- 我另在 `sb_int` 单跑了 U02 的 `tests/integration/test_strategy_position_manager.py`：**PASS=144 FAIL=0**（与作者自述一致）。

### 6.3 06 的必要性（README 表格里的"伴随修改"是否成立）
`sb_no06`（01..05，无 06）单跑 `smoke_ui.py`：
```
FAIL 全部事件类型都有中文映射 缺映射：['account_domain_mismatch','futures_duplicate_exit_skipped',
     'futures_exit_reservation_released','futures_stale_exit_reservation_reclaimed',
     'legacy_position_requires_migration','management_intent_unbound']
```
→ 缺映射集合**正好包含 r2 新增的两个事件**（`futures_exit_reservation_released`/`fetures_stale_...`），
  06 是新增事件的必要伴随。README 的表格描述**成立**。

---

## 7. 回归抽样（≥5，独立断言，`probes/probe_q7_regression.py`，`Q_REGRESSION_PROBE FAIL=0`）

| # | 不变量 | 我的独立结论 |
|---|---|---|
| 1 | 三分量守恒 / 预留守恒 | 守恒式在 reserve/consume/reconcile 边界始终成立；超量 reserve 被拒且**不改状态**；重复 consume 不二次扣减；仓位缩小触发溢出释放且 `available>=0`。
| 2 | 重复退出不反向开仓 | 同 key 二次 = `NOOP/submit_qty=0/opens_position=False`；LONG→SELL、SHORT→BUY 且 `reduce_only=True`；空仓退出 = `NOOP`（`EXIT_EXCEEDS_POSITION,NO_POSITION_TO_EXIT`）。
| 3 | 旧仓显式迁移 | 混合新旧仓 → 旧仓 `MIGRATE_EXPLICIT`、有归属无预期 → `ALIGN_EXPECTATION`、`exit_action_count=0`；`migrate_legacy` 缺 任一字段抛 `ValueError`；迁移后**下一轮不再被撤销**（`ALREADY_MIGRATED`，r1 是重新登记，见 `probe_q7b`：r1 `plan2=['MIGRATE_EXPLICIT']` vs r2 `['ALREADY_MIGRATED']`）；护栏吃真实动作（r1 `plan_restart_passes_real_actions=False` → r2 `True`）。
| 4 | 过期授权不阻断 UNKNOWN 查单 | `submit_allowed=False(AUTHORIZATION_EXPIRED)` 而 `reconcile_allowed=True`；`plan_unknown_reconcile→QUERY_DUE`；`execute_unknown_reconcile` → `queried=1 resolved=1 submit_calls=0`，outbox 确认 1 次。
| 5 | 健康故障不触发全平 | 28 个名称（含 `STOP_LOSS_FAILED/NAKED_POSITION/MARGIN_CALL/HTTP_5XX/TIMEOUT/UNKNOWN_FAULT/垃圾名/None/异常对象`）**只有** `PROTECTION_FAILED`/`PLACE_FAILED`（含大小写/空白变体）放行；`operator_emergency=True` 恒允许；调用前后 `as_dict()` 逐字不变（纯函数）。
| 6 | R09 并发不重复占量 | 8 线程抢 1.0（各 0.25）→ **恰 4 个 accepted**，`reserved=1.0 available=0`，全部 `reduce_only` 且不反向。

---

## 8. 新发现（r2 引入 / 遗留，按严重度）

- **NEW-1（P2，未登记）**：**"已 CONSUMED 的键永不回收"不成立**（账本淘汰分支）。见 §1.1 F1。后果有界（多一次 reduce_only 提交、被可用量夹逼、下轮对账自愈），但与 README r2-1 与模块 docstring 的**绝对声明冲突**，必须改写声明或在 `_reclaim_if_stale_locked` 里显式区分"被淘汰的 CONSUMED"。
- **NEW-2（P2，未登记，当前无调用方）**：`ManagementReservationLedger.release()` 对 **CONSUMED** 条目**不幂等**（会改成 RELEASED 并可再次 `reserve`），与它自己的 docstring 冲突；`StrategyPositionManager.release_exit()` 会传递这一行为。见 §1.1 F2。
- **NEW-3（P2，声明失真）**：README r2-3 称纸面回退键"同轮同指纹仍共享一个键（**保持抑制**）"——**事实相反**：因 `_exit_idempotency_guard` 接受后立即 `release_exit`（而 r2 的 release 会撤销键），同轮第二次同名同指纹退出**会再次执行**（r1 反而是抑制的）。实测影响有界（`min(intent.qty, pos.qty)` 夹逼 + 仓量已减 → 第二次为 noop/减剩余，不反向、不超量），但声明必须改成事实。**这一条与被修的 P1-3 是同一处代码的两面**，属"修了 A 坏了 B 且未登记"。
- **NEW-4（P3，文档）**：迁移清单里 `trade_reconciliation_jobs` 的理由引错了表/护栏（§3.6）；income 节的"否则唯一索引仍视作两个域"方向写反（§3.2 应为"仍视作同一域"）。
- **NEW-5（P3，观察）**：`_sweep_stale_exit_reservations` 把巡检异常整体吞掉（`return ()`）。此时若预留又未过期，该标的退化为 `duplicate_skipped`（fail-closed，不放行）——见 `probe_q0c` 用例 D；代码注释已说明"兜底回收在下一轮 sweep"，属**已登记语义**，但"巡检长期失败 → 该标的不可退出"这条链没有运维可见性（无告警事件），建议加一条巡检失败的 warn 事件。
- **NEW-6（P3，操作）**：默认 `core.autocrlf=true` 下应用补丁会把 3 个纯 LF 目标文件**整文件转成 CRLF**（§6.1 表），I 应统一用 `-c core.autocrlf=false -c core.eol=lf` 应用；应用后建议 `git diff --stat` 自检（应只出现真实改动行数）。
- 说明：r1 的 P2-7/P2-8/P3-3 的修复我都独立复现了（`probe_q7b`/`probe_q7`），P2-9（进程内易失）/P2-10（登记行不在补丁里）作者已如实登记，我复核一致。

---

## 9. 命令清单（可复跑）

```bash
# 0) 沙箱（全部落在 _scratch_Q/）
bash setup.sh                                   # int_base/I@59660b2 + U02 独占文件 + 登记行；build pristine/sb_int/sb_int2/sb_old*/chk；apply --check 矩阵
# 前对照树（r1 独占文件 + r1 补丁）
git -C <I> archive 5b71db4 -- src/core/strategy_position_manager.py src/core/management_authorization.py \
   scripts/smoke_u02_strategy_position_manager.py tests/integration/test_strategy_position_manager.py | tar -x -C sb_old_full
for p in 01 02 04 05 06; do (cd sb_old_full && git apply -p1 <B>/docs/dispatch/patches/U02/8c66f65/${p}_*.diff); done
# 1) P0 前后对照 / 绕过
python probes/probe_q0_p0.py sb_old_full   # FAIL=8（泄漏、永久占死、稳定键被吞、成交后异常也泄漏）
python probes/probe_q0_p0.py sb_int        # FAIL=0
python probes/probe_q0_ttl.py sb_int       # TTL 边界/时钟回拨/停机重启/CONSUMED/并发
python probes/probe_q0c_releasefail.py sb_int   # finally 释放自身失败 -> TTL 巡检兜住
# 2) P1
python probes/probe_q1_nameerr.py sb_old_full && python probes/probe_q1_nameerr.py sb_int
python probes/probe_q2_paperkey.py sb_old_full && python probes/probe_q2_paperkey.py sb_int
# 3) 迁移方案
python probes/probe_q3_migrate.py sb_int
# 4) 约束 1 / 5) enforce
python probes/probe_q4_constraint1.py only01 --declared acct-live
python probes/probe_q5_enforce.py sb_int
# 6) delta（单树一条，非 fail-fast）
python run_delta_Q.py pristine_int out_delta_pristine.txt
python run_delta_Q.py sb_int       out_delta_sb.txt
# 7) 回归抽样 / 迁移未撤销 / 06 必要性
python probes/probe_q7_regression.py sb_int
python probes/probe_q7b_legacy.py sb_old_full && python probes/probe_q7b_legacy.py sb_int
(cd sb_no06 && python scripts/smoke_ui.py)      # 缺映射 6 项 -> 06 必要
(cd sb_int && python tests/integration/test_strategy_position_manager.py)   # PASS=144 FAIL=0
```

## 10. 未覆盖 / 无法离线验证（明说）

1. **未接真实交易所**：所有 Broker 路径用桩（`__new__` + 方法替换），P0/P1 是代码路径级证明；未下单、未撤单、未联网。
2. **P0 触发频率未量化**；`_open` 的 qty≈0 分支可达性仍取决于上游过滤（未穷举），与作者登记一致。
3. **R09 只单进程**：多进程/多实例共享账本不成立（P2-9 已登记）；**跨进程崩溃不存在可回收的残留**（预留不落库，已实测）。
4. **未读 `.env`/`data/`/密钥/账户标识**；迁移方案没有在生产库规模上跑过（锁时长/耗时未验证）。
5. **I 树是移动靶**：会话期间 I HEAD 前移两次；我用固定 `IBASE=59660b2` 做 delta，并另核对 6 个目标文件 blob 在 `40ff52f`/`59660b2`/`25f5097` 三次 HEAD 完全相同。
6. `TradeIntent.management_authorization_id` 的端到端接线仍未验（字段不存在、门无调用方）——作者已登记为后续任务。
7. 未审 U15/K01/K04 本体；只审 U02 使用到的接口与行为。

## 11. 结论：**CHANGES_REQUESTED**

### r2 确实办到的事（我独立复现，非采信作者）
| 项 | 判定 | 我的证据 |
|---|---|---|
| P0 预留泄漏 → 域永久不可退出 | **真闭合** | `out_q0_sb_old_full.txt` FAIL=8 → `out_q0_sb_int.txt` FAIL=0；含占量前/后/提交中/收口后四阶段、异常释放 + 审计事件、稳定 key 可重发、换新 key 可退出、多轮恢复 |
| P0 的 TTL/巡检/边界/崩溃 | 真闭合 | `out_q0_ttl_int.txt`：TTL 恰好回收、差 1ms 不回收、时钟回拨 fail-closed、now=0/ttl=0 严格幂等、只回收过期键、审计事件、8×400 并发混沌 0 错误；**崩溃重启场景不存在**（预留不落库，重启后同键直接可退 — 实测） |
| P0 的兜底（释放自身失败） | 真闭合 | `out_q0c.txt`：finally 里 release 抛错 → 预留留 ACTIVE 但**无假审计**；超过 TTL 后巡检回收并恢复可退；巡检也坏时 `request_exit` 的重复分支仍回收（`C`）；未过期则退化为 duplicate（fail-closed 不放行） |
| P1-2 `_open` NameError | **真修** | 静态 `static_unbound_in_open=[]` + 动态 `_open(qty=0) → None`（r1 是 NameError） |
| P1-3 纸面键（主要缺陷） | **真修** | 跨轮/多轮/部分平/跨标的全部 `EXECUTED`（r1 全被吞） |
| P2-6 `enforce=True` | **真闭合** | r1 静默摘除退出（决策落成 `open`）→ r2 抛 `ManagementEnforcementError` 且零摘除副作用 |
| P2-7 / P2-8 | **真修** | r1 下一轮把已迁移生命周期重新登记（`plan2=['MIGRATE_EXPLICIT']`）→ r2 `['ALREADY_MIGRATED']`；护栏 r1 `passes_real_actions=False` → r2 `True` |
| 约束 1 改写 | **准确** | only01 + `MTA_ACCOUNT_REF` → `demo:acct-live`（分区已切）；only02 → `ImportError`；未声明时逐字一致 |
| 第 8 项迁移方案 | **可执行**（硬结论见 §3.8） | 清单 7/7 完整、冲突键逐字一致、单事务去重裁决可重入、bundle 回滚可用 |
| 补丁 & delta | **通过** | 两种 git 配置 6/6；全量 80 条两树 `FAILED=0`、差集 `{}`；EOL 保持 mixed；06 必要性成立 |
| 回归抽样 ≥5 | **通过** | `probe_q7` 6/6（守恒、不反向开仓、显式迁移、过期授权不阻断收口、健康故障不全平、R09 并发）+ 官方测试 144/0 |

### 为什么仍判 CHANGES_REQUESTED
标尺是"残留项须为 **P2 及以下且已如实登记**；任何**未登记**的 P2 fail-open ⇒ CHANGES_REQUESTED"。r2 有**三条未登记**的 P2，其中一条是幂等守卫的 fail-open、一条把 README 的安全声明写成了事实的反面（与上一轮 P1-4 同类）：

1. **NEW-1（P2，未登记；幂等守卫 fail-open）**：账本 `MAX_TERMINAL_RESERVATIONS` 淘汰掉 CONSUMED 条目后，
   `_reclaim_if_stale_locked` 的 else 分支（`:708-712`）会在 TTL 后回收该键并**重新占量提交**（实测 `action=SUBMIT_REDUCE, reasons=[STALE_EXIT_RESERVATION_RECLAIMED]`），
   与 README r2-1 及模块 docstring（`:24-25`）的**绝对声明"已 CONSUMED 的键永不回收"冲突**。
   可达前提：单个 lifecycle 域累计 ≥2001 条终结预留（≈同域 ≥2001 个不同 exit key；无 cid 的实盘退出用 `uuid4()[:16]`，键基数无界）。
   **修法（二选一，都很小）**：① 改声明 —— 把"永不回收"改成"账本条目在位时永不回收；条目被淘汰后按 TTL 兜底回收"并登记后果；
   ② 改代码 —— 在管理器里维护独立的 `_consumed_exits` 集合并跳过回收。
2. **NEW-2（P2，未登记，当前无调用方）**：`ManagementReservationLedger.release()` 对 **CONSUMED** 条目**不幂等**
   （`CONSUMED → RELEASED` 且随后 `reserve` 可再次接受，实测），与它自己的 docstring"对已终结预留幂等"冲突；
   `StrategyPositionManager.release_exit()` 会把这一行为透出。**修法**：`release()` 对非 RELEASED 的终结状态返回 `ALREADY_CLOSED`（不改状态）。
3. **NEW-3（P2，未登记；README 声明与事实相反）**：README r2-3 写"同轮同指纹仍共享一个键（**保持抑制**）"，
   实测同轮第二次同名同指纹退出**会再次执行**（`1_same_round first=EXECUTED second=EXECUTED`；r1 反而是 `duplicate_skipped`）。
   根因是 r2 的正确修法（`release_exit` 撤销键）与"接受后立即 release"的纸面路径叠加，使 `_exit_keys` 的同轮抑制失效；
   实际影响有界（`min(intent.qty, pos.qty)` 夹逼 + 仓位已减 → 第二次为 noop/减剩余，不反向、不超量，且显式 cid 仍由 `orders` 表拦住）。
   **修法**：把 README/注释改成事实（"同轮抑制由 `orders` 表与仓量夹逼提供"），或让纸面路径在"本轮已执行集合"里保留键。

（NEW-4 的两处引证/方向错误见 §3.2/§3.6，P3。）

三条的**后果都被 `reduce_only` + 可用量夹逼 + 逐轮权威对账限界**，不构成反向开仓/超仓/预留永久泄漏/崩溃，
所以不是"必须改代码"，而是"**必须把声明改成事实**"——这正是上一轮把 P1-4 判为必修的同一标准。
另附建议（非阻塞）：给巡检失败加一条 warn 事件（NEW-5），否则"巡检长期失败 → 该标的不可退出"没有运维可见性。

