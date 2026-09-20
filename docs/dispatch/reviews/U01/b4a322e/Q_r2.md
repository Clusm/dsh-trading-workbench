# U01 r2 独立复验报告（Q 角色 / 独立会话）

- 复验对象 head: `b4a322e`（分支 `task/U01`），上一交付版 `c290c50`（B 判 CHANGES_REQUESTED），WIP `fb1378c`
- 工作区: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U01\R2\r1`（独立 detached，全程只读 tracked）
- 解释器: `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`，无 pytest）
- 探针目录: `_scratch_Q/`（原始 stdout 全部重定向保留）；沙箱（**未 tracked**）: `_scratch_Q/sb_*`、`_scratch_Q/combo_*`、`_scratch_Q/pristine`
- 不采信作者前后对照：三段对照、P1 逐条、补丁矩阵均由本会话自建探针复现
- 工作区只读性：`git status --short` 全程仅 `?? _scratch_Q/`

## 0. 结论

**CHANGES_REQUESTED**

理由：r2 的功能性修复**质量很高**——P0-B「重放不抹账」三段链条我独立复现**全部成立**，
B_r1 的 2 个 P0（除 I 的登记项外）与 9 处 P1 基本全部闭合（详见 §1），`[9]` 不再 SKIP 且走真实
`AuditDatabase`，补丁 01 的 `content_hash` 确与 K03 同一真值源。**但**补丁 01 引入迁移版本 17，
使统一离线回归清单中的 **9 个既有共享冒烟新增失败**（`scripts/smoke_*.py` 硬断言
`versions == list(range(1,17))` 等），即 **I 一应用补丁 01，发布门禁即变红**。作者 README 明写
「对既有表零影响」且**未登记**这一必需的同批伴随修改 —— 属**未登记的集成阻断级（P0）缺陷**，
按标尺不可判 PASS-with-notes。

## 1. 逐项复验

### [1] P0-B 真实存储幂等链三段对照 —— CLOSED（独立复现成立）

方法：自建探针 `_scratch_Q/probe_replay.py`（与作者 fixture 无关的自造数据：W1 四条
FUNDING_FEE `-0.37 / +0.11 / -2.03 / -0.51`，Σ = **-2.80**；`page_limit=2` 两页）。
三个隔离沙箱（本工作区的 untracked 副本 + 独立 `git init`）：

| 段 | 沙箱 | 源 | 补丁 | 原始输出 |
|---|---|---|---|---|
| A | `sb_c290_old` | `c290c50` 的 `income_reconciliation.py` | 旧 01 | `p22_c290_old.txt` |
| B | `sb_head_old` | HEAD | 旧 01 | `p21_head_old.txt` |
| C | `sb_head_new` | HEAD | 新 01 | `p20_head_new.txt` |

**A（`p22_c290_old.txt`）—— 复现崩溃**
```
[2] REPLAY CRASH: ValidationError: 1 validation error for IncomeEvent
  Input should be a valid dictionary or instance of IncomeEvent [type=model_type,
  input_value='0e9010b19eeb08d65010f6a7...7fe2c2e9f857b64d213e473', input_type=str]
```
即 `existing.values()` 的 key→hash 文本被当事件本体。与作者陈述一致。

**B（`p21_head_old.txt`）—— 复现「重放抹账」**
```
[1] first  dup=0 q=0 ins=4 events=4 sum=-2.80 cov=COMPLETE
[1] hash compare tran=Q_T1 stored=f71c19cb0bbd433a k03=67fa6f9a0a4c034f same=False
[2] replay dup=0 q=4 ins=0 events=0 sum=0 cov=COMPLETE
[2] VS first: events 4 -> 0; sum -2.80 -> 0; WIPED=True
```
四个事件全被判「同键不同内容」隔离 ⇒ 冲突键整体退出事件集 ⇒ 事件集 0、Σ=0，金额真值 -2.80 被抹掉。
与作者陈述一致（作者值 -1.80 系其 fixture 不同）。

**C（`p20_head_new.txt`）—— 复现「重放不抹账」：独立成立**
```
[1] first  dup=0 q=0 ins=4 events=4 sum=-2.80 cov=COMPLETE
[1] hash compare tran=Q_T1 stored=67fa6f9a0a4c034f k03=67fa6f9a0a4c034f same=True
[1] hash compare tran=Q_T2 stored=4d16508859a1fbe5 k03=4d16508859a1fbe5 same=True
[1] hash compare tran=Q_T3 stored=eeae4697cf48f992 k03=eeae4697cf48f992 same=True
[1] hash compare tran=Q_T4 stored=de6bf14a8ee7ce90 k03=de6bf14a8ee7ce90 same=True
[2] load_existing n=4
[2] replay dup=4 q=0 ins=0 events=4 sum=-2.80 cov=COMPLETE
[2] VS first: events 4 -> 4; sum -2.80 -> -2.80; WIPED=False
[3] db rows after replay=4 db_sum=-2.80
```
**P0-B 闭合**：同批键在重放时判 `duplicates`（非 quarantine），事件集与金额真值逐分不变，
库内行数不减。**「重放不抹账」独立复现成立（不需要采信作者证据）。**

### [2] `[9]` 是否真实执行 —— CLOSED

- `_scratch_Q/r01_test_income.txt`：`grep -c SKIP` = **0**；`exit=0`、`PASS=139 FAIL=0`。
- `[9]` 段真实输出（节选）：
  ```
  [9] 真实存储层（AuditDatabase）幂等入库、覆盖落库、重放不抹账
    证据来源：补丁沙箱 git apply（已应用）
    PASS  迁移版本 >= 17（收入表已建）
    PASS  真实库首次写入 4 行 / 重复写入新增 0 行 / 已入库键可回读 4 条
    PASS  重放不崩溃且全部判为 duplicates
    PASS  重放不抹账：窗口事件集仍为 4 且与库内一致
    PASS  重放金额真值不变（Σ == 首轮 Σ == -1.80）
    PASS  补丁 content_hash 与 K03 单一真值源一致（否则重放会被误判 ID 冲突）
    PASS  真实库上同键不同内容判为隔离 / 冲突行落库可审计 / 冲突行未污染事件表
  ```
- **真实 `AuditDatabase`（非内存替身）**：`_patched_database_dir()` 把 `src` 复制到
  `tempfile.mkdtemp()` 并 `git apply` 补丁 01，`AuditDatabase(tmp/"store.sqlite")` 是真实 SQLite 文件。
  我另测：**在真正的非 repo 临时目录里 `git apply -p1` 确实生效**（`pristine/src` 副本
  `database.py` 2230 → **2523** 行，`_scratch_Q/p70_badpatch.txt` 流程同）。
- **未打补丁时该用例的行为不被掩盖（关键反面验证）**：把补丁文件替换为垃圾内容后重跑，
  ```
  FAIL  真实存储层可用（补丁可应用或已应用）: 补丁 01 无法应用到 src 副本 … No valid patches in input
  U01 income reconciliation: PASS=125 FAIL=1   exit=1   SKIP 计数=0
  ```
  ⇒ 补丁不可应用时是 **FAIL（响亮失败）**，不是 SKIP。P2-8「SKIP 掩盖」已闭合。
- 遗留（P3，非缺陷）：模式标签 `f"补丁沙箱 git apply（{'已应用'}）"` 把「已应用」**硬编码**，
  唯一守卫是 `returncode != 0`。标签本身不构成证据（真正证据是 `迁移版本 >= 17` 通过 + 真实落库成功）。

### [3] 补丁 01 的 `content_hash` 与 K03 同一真值源 —— CLOSED

- 代码：新补丁 `_income_content_hash` 直接 `from core.contracts import income_content_hash` 并调用；
  旧补丁是本地 `canonical_hash({...})` 自造指纹（见 `_scratch_Q/old_patch01.diff` `:60-76`）。
- **独立比对**（非作者证据）：探针把库内每行 `content_hash` 与 K03
  `income_content_hash(_event_from_storage_row(行))` 逐行比 → 新补丁 **4/4 `same=True`**；
  旧补丁 **4/4 `same=False`**（`p20` vs `p21`/`p22`）。**同一真值源成立。**
- **同键不同内容 → 隔离而非静默覆盖**：`p20 [4]` → `dup=3 q=1 ins=0`；
  `_scratch_Q/p42_amount.txt`：冲突后库内 `[('K1','-1'), ('K2','-2')]`（**旧值 -1.00 未被覆盖**），
  冲突另落 `income_conflicts` 可查。隔离语义成立。
- 残留（P3，已在代码注释中作为策略声明，但未进 README 登记表）：冲突键整体退出**窗口聚合**，
  `p42`：首轮 events=2 Σ=-3 → 冲突轮 events=1 Σ=-2（`reasons=('INCOME_ID_CONFLICT',)`），
  即聚合层少算该键金额；库层不丢。带真实 attribution 时 `window_cost_finalization` 会补
  `INCOME_ID_CONFLICT` + INCOMPLETE；但 **`attribution=None` 时提前 return，该 reason 被掩盖**
  （只剩 `NO_ATTRIBUTION_DECISIONS`），状态仍 INCOMPLETE，故仅属 reason 粒度问题。

### [4] 补丁可应用性矩阵 —— CLOSED

方法：**每个组合一份 pristine 副本 + 独立 `git init`**（关键：先前在 repo 子目录里
`git apply --check` 会静默 `Skipped patch` 并返回 exit 0，属**假阳性**，本报告已改用该方法论）。
`_scratch_Q/p30_patch_matrix.txt` + 行数核验：

| 组合 | 结果 | 行数证据（db / futures / trade / policy） |
|---|---|---|
| 01 | clean apply | 2523 / 3073 / 583 / 187 |
| 02 | clean apply | 2230 / 3100 / 583 / 187 |
| 03 | clean apply | 2230 / 3073 / 611 / 187 |
| 04 | clean apply | 2230 / 3073 / 583 / **188** |
| 01+02+03+04 | clean apply | 2523 / 3100 / 611 / 188 |
| 01+02 / 01+02+04 / 02+04 | clean apply | 变更仅落在被指定文件 |

**02↔04 同批约束属实**（`_scratch_Q/p31_policy.txt`）：
```
02-only      -> AssertionError: 未登记 Binance 端点: [('GET', '/fapi/v1/income')]  exit=1
02+04 / 04 / 01+02+03+04 -> PASS request policy coverage: endpoints=35            exit=0
pristine     -> PASS request policy coverage: endpoints=34                        exit=0
```

### [5] P1×8 逐条判定

| 项 | 判定 | 我的反例与原始输出 |
|---|---|---|
| P1-1 跨 profile 覆盖 | **CLOSED** | `p40 [1a]` book(live).persist(sync(demo)) → `ProfileMismatchError: PROFILE_MISMATCH: sync=('demo','q_acct') book=('live','q_acct')`；`[1b]` coverage 行与事件行同域（均 `demo`/`q_acct`）；`[1c]` book 传 `'DEMO'/'q_acct'` 归一后同域放行；`[1d]` 事件域≠sync 域 → ProfileMismatchError |
| P1-2 按窗口读覆盖 / 缺失不当零 | **CLOSED** | `p40 [2b]` 无窗口参数 → 返回最新一条（docstring 已明确要求传窗口）；`[2c]` `coverage_for_window(W1)/(W2)` 各自命中；`[2d]` 不存在的窗口 → `None`；`[2g]` 1 COMPLETE + 9 GAP → `complete=False incomplete=9`；`[2h]` 零证据 → `complete=False missing=(全区间,) NO_COVERAGE`。**「缺失当零」被杜绝** |
| P1-3 分页零丢失 | **CLOSED** | `p40 [3a]` 页序回退 → `CROSS_PAGE_NOT_MONOTONIC` + `COVERAGE_GAP`；`[3b]` 同页两次读取不一致 → `PAGE_WINDOW_UNSTABLE` + GAP（`overlap_recheck` 已被真正消费）；`[3c]` 复读一致 → COMPLETE 无误报；`[3g]` 重复页 → `PAGINATION_STALLED`；`[3h]` 跨页重复 → events 键唯一 |
| P1-4 迟到修订 | **CLOSED（契约侧出口已登记给 I）** | `p41 [4a]` 首次 `first=True new_outcome=False`；`[4b]` 同内容重放 `first=False` + `('LATE_REVISION_ALREADY_APPLIED',)`；**`[4c]` 同键改额 → `LateIncomeContentChanged: LATE_REVISION_CONTENT_CHANGED: tran_id=R1 previous=bc00f1be6d8f incoming=445362746890`**（不再被吞）。死常量 `REASON_LATE_REVISION_DUPLICATE` 已启用（`duplicate_reason_codes`）。K03 侧「升格为新修订」按 README 登记给 I |
| P1-5 重复计费守卫 | **CLOSED（残留 P3）** | `p40 [5]`：`commission` / `commission_income_usdt` / `income_fees` / `realized_pnl_usdt` / `my_realized` → **全部 DoubleCountError**；合法键 `net_fees_from_fills_usdt` → ALLOWED；`reconcile_session 直接调用守卫: True`（非自愿调用）。主结论（income 手续费不重复计入）仍成立：`unexplained=+0.70 balanced=False` |
| P1-6 交叉核对 | **CLOSED** | `[6a]` 收入侧只有 REALIZED_PNL → `status=NOT_COMPARABLE`（**不再误报 MISMATCH**）；`[6b]` 不一致 → MISMATCH 且 `balanced=False`（**不再自相矛盾**）；`[6c]` realized 分歧 → `FILL_INCOME_REALIZED_PNL_MISMATCH` + `balanced=False`（**REALIZED_PNL 现在真核对**）；`p41 [6d2]` 一致场景 `status=MATCH balanced=True unexplained=0.00`，`[6d3]` 不一致 `status=MISMATCH balanced=False` |
| P1-7 隔离可审计 | **CLOSED** | `p41 [7a]` `persist.conflicts=1`；`[7b]` `income_conflicts` 行含 `existing/incoming_content_hash`、`amount`、`observed_at_ms`；`[7e]` **关闭并重开 DB 后仍可查**（跨进程可审计） |
| P1-8 income_type 治理 | **CLOSED** | `p40 [8a]` `assert_known_income_type`：`'FUNDINGFEE'`/`'未知类型'`/`''` → InvalidIncomeRow；`[8b]` `('DEMO','funding_fee','a')` 与 `('demo','FUNDING_FEE','A')` → 库内**1 行**（`*_norm` 列 + 唯一约束与 K03 `strip+casefold` 等价），覆盖分裂消除；`[8c]` 异类型/未知类型 → `UNKNOWN_INCOME_TYPE` + `INCOME_TYPE_MISMATCH`；`[8d]` 窗口声明 `FUNDINGFEE` → InvalidIncomeRow |

附带回归（B_r1 的 P2 清单）：
- **P2-1 CLOSED**：`p40 [3i]` 越窗行（含 `time==end`）→ `events=0` 且 `ROWS_OUTSIDE_WINDOW` 显式回带。
- **P2-2 CLOSED**：`[3h]` 跨页重复 → `by_key` 去重，`events=3 sum=-3.00`（不再双计）。
- **P2-3 CLOSED**：`p40 [P2-3]` 极小金额 5 腿 → `allocate_funding_or_incomplete` 降级 `complete=False reasons=('ALLOCATION_QUANTUM_CONFLICT',)`，不再整链崩溃。
- **P2-4 CLOSED**：`IncomeStore` Protocol 与 docstring 已改为事实描述（事件先、覆盖后、各自提交），不再声称「同事务」。
- **P2-5 CLOSED**：`[P2-5]` KZ 无汇率 → `unconverted=1` 显式回带、`unknown_usdt=None` 语义分离。
- **P2-6 部分闭合**：`[P2-6]` 未知分量仍写 0，但 `unknown_components=('realized_trade_pnl_usdt',)` 显式列出且 `balanced=False` → 关键副作用已消除。
- **P2-7 CLOSED**：`p41 [P2-7b]` 非空窗口缺归因 → `INCOMPLETE` + `NO_ATTRIBUTION_DECISIONS`（不再误导为证书零费用）；`[P2-7d]` 来源不可用 → `cost=UNKNOWN`（未知≠零）。
- **P2-8 CLOSED**：见 §[2]。

### [6] 新回归：翻页推进改「线上原始页」+ 守卫标记集合化 —— 无回归

- 反面验证（真正 TERMINATED 仍可识别）：`p40 [3e]` 末页短页 → `cov=COMPLETE pages=2 watermark==end:True`；
  `[3f]` 满页且末行达窗口末 → `plan_next_page` TERMINATED → `COMPLETE pages=2`。
- 原问题（整页被滤掉不再误判 TERMINATED）：`[3d]` 首页整页为未知类型 → `pages=2`，**继续翻到第 2 页**
  取到 `Y1`，`cov=COMPLETE reasons=('UNKNOWN_INCOME_TYPE',)`。修复方向正确。
- 无新误报/漏报：`[3c]` 正常复读一致 → COMPLETE 且不含 `PAGE_WINDOW_UNSTABLE`；
  `[3g]` 真停滞仍判 `PAGINATION_STALLED`。改动只把判定基准从「过滤后事件」改为「线上原始页」，
  方向上更严格（`[3i]` 越窗行同时触发 `ROWS_OUTSIDE_WINDOW` + `PAGE_NOT_SORTED`），**未见放松**。
- 守卫标记集合化：旧实现 `kind.lower() in additive` 只拦字面 `commission`/`realized_pnl`
  （`_scratch_Q/c290c50_income_reconciliation.py:745-762`）；新实现按 marker 子串
  （`commission`/`income_fee`/`realized`）反向判定 → **判定强度严格增强**，且未误伤
  `net_fees_from_fills_usdt`。**未发现强度下降或被绕过的新通道**。

## 2. 新发现

### P0-NEW（未登记，集成阻断）补丁 01 的迁移版本 17 使统一离线回归新增 9 处失败

- 事实：`src/audit/database.py` 基线最高迁移版本为 **16**；补丁 01 `_apply_migration(17, "income_reconciliation", …)`。
  仓库内 **9 个共享冒烟**硬编码了「版本集合恰为 1..16」或等价假设：
  - `== list(range(1, 17))`：`scripts/smoke_db_migrations.py:37,78`、`smoke_account_mirror.py:103`、
    `smoke_exchange_event_inbox.py:61`、`smoke_exchange_safety_monitor.py:95`、
    `smoke_order_mirror.py:188`、`smoke_order_outbox.py:212`、`smoke_protection_mirror.py:119`（**7 个**）
  - `scripts/smoke_fault_matrix.py:565-573` 手工 `INSERT INTO schema_migrations(version,…) VALUES(17,…)`
    → 与已应用的 17 冲突 → `sqlite3.IntegrityError: UNIQUE constraint failed: schema_migrations.version`（**1 个**）
  - `scripts/smoke_release_acceptance.py:112` 的 manifest `schema_version: 16` 与 DB 17 不符 →
    `src/core/release_evidence.py:448-451` 的 `schema_version` gate FAIL → 「严格发布报告可GO」失败（**1 个**）
- 全部 9 个都在 `scripts/run_offline_regression.py` 的 `SCRIPTS` 清单内
  （`check_execution_boundaries.py`…`smoke_contracts_v3.py`，共 69 项）。
- **复现**（`_scratch_Q/p60/p62_smoke_delta.txt`，全清单 69 项 × pristine / 已应用 01+02+03+04 各跑一遍）：
  ```
  pristine FAILURES: scripts/smoke_windows_service_plan.py（=1，环境性、与补丁无关）
  已应用后 FAILURES: 上项 + 下列 9 项（pristine 全为 0）
    NEW scripts/smoke_account_mirror.py
    NEW scripts/smoke_db_migrations.py
    NEW scripts/smoke_exchange_event_inbox.py
    NEW scripts/smoke_exchange_safety_monitor.py
    NEW scripts/smoke_fault_matrix.py
    NEW scripts/smoke_order_mirror.py
    NEW scripts/smoke_order_outbox.py
    NEW scripts/smoke_protection_mirror.py
    NEW scripts/smoke_release_acceptance.py
  ```
  逐条样本（`p50_migration_regression.txt`）：
  ```
  smoke_db_migrations          exit=1  AssertionError: 记录迁移版本1到16
  smoke_order_outbox           exit=1  AssertionError: 数据库迁移版本1到16完整
  smoke_fault_matrix           exit=1  sqlite3.IntegrityError: UNIQUE constraint failed: schema_migrations.version
  smoke_release_acceptance     exit=1  AssertionError: 严格发布报告可GO
  ```
- 统一入口实证（`_scratch_Q/p51_offline_patched.txt`）：在已应用 01+02+03+04 的沙箱里，把两个
  `smoke_u01_*` 临时登记后运行 `scripts/run_offline_regression.py` →
  `CalledProcessError: … 'scripts/smoke_order_outbox.py' returned non-zero exit status 1`，**exit=1**。
- **归因收敛（只与补丁 01 相关，与 02/03/04 无关）**：`combo_01`（只应用 01）复现同 9 项失败；
  而 `combo_02`/`combo_03`/`combo_04` 单独应用时 `smoke_db_migrations` 与 `smoke_order_outbox` 均 exit=0。
- 归因与处置：`scripts/` 共享冒烟/`run_offline_regression.py` **不在 U01 独占路径**
  （任务卡 §U01 独占路径不含 `scripts/`，`run_offline_regression.py` 属 U00），故作者**不应自行改**；
  但必须在 README「未修项登记 / 批次约束」中**登记为必需的同批伴随修改**（建议：8 处版本断言改为
  `>= 16` 或 `range(1, 18)`；`smoke_fault_matrix` 的手工插入改用 18；`smoke_release_acceptance`
  的 fixture manifest 改 17），否则 I 一旦应用 01 即让发布门禁变红。
- 加重情节：作者 README 明写「01 的迁移版本 17 为纯新增表…**对既有表零影响**」，其「验证」清单
  只跑了 test/smoke_u01_*/smoke_contracts_v3，**完全未跑统一离线回归清单中的这 9 个**，
  故该结论在事实层面不成立。

### P3-NEW 其他（不影响结论，建议登记）

| # | 位置 | 说明 |
|---|---|---|
| P3-N1 | `src/exchange/income_reconciliation.py:assert_no_double_count` | 守卫按「存在的收入事件类型」取 marker，**`income_events` 为空时对任何分量键都放行**（`p40 [5]`：`key='commission_income_usdt' events=0 -> ALLOWED`）。旧实现同样依赖该前提，**非本批回归**，但 B_r1 建议的「白名单式反向判定」只完成了一半 |
| P3-N2 | `src/audit/database.py:_income_row` | `str(row.get("amount") or "")`：`Decimal("0")`/`Decimal("0.00")` 为 falsy → 转成 `""` → `IncomeEvent` 校验抛 `ValidationError`（`p41 [N1]`）。仅当调用方绕过 `persist` 直接传 `Decimal` 才可达；**fail-loud**，但库层不该有这条崩溃路径。`symbol` 同法强制转换 |
| P3-N3 | `income_reconciliation.py` `window_cost_finalization` | `sync.events and attribution is None` 分支**提前 return**，未并入 `REASON_ID_CONFLICT`；带真实 attribution 时才补（`p41 [7d]` 只得 `NO_ATTRIBUTION_DECISIONS`）。状态仍 INCOMPLETE，仅排障粒度 |
| P3-N4 | 测试 `_patched_database_dir` 模式串 | `f"补丁沙箱 git apply（{'已应用'}）"` 硬编码，唯一守卫是 returncode≠0（实测非 repo 目录下 `git apply` 确实生效，故当前无假绿） |

## 3. 命令清单（全部输出重定向留档）

```bash
# 环境
PY="C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe"   # PYTHONUTF8=1

# 三段对照（自建探针，在隔离副本上应用补丁，未改工作区）
python _scratch_Q/probe_replay.py _scratch_Q/sb_head_new/src  "HEAD+NEW"  new > _scratch_Q/p20_head_new.txt 2>&1
python _scratch_Q/probe_replay.py _scratch_Q/sb_head_old/src  "HEAD+OLD"  old > _scratch_Q/p21_head_old.txt 2>&1
python _scratch_Q/probe_replay.py _scratch_Q/sb_c290_old/src  "c290+OLD"  old > _scratch_Q/p22_c290_old.txt 2>&1

# 补丁矩阵（每组合 pristine 副本 + git init，避免 repo 子目录静默 Skipped patch）
bash _scratch_Q/probe_patches.sh                                   > _scratch_Q/p30_patch_matrix.txt 2>&1
python scripts/check_binance_request_policy.py   # 在 combo_02 / combo_0204 / pristine 内 -> _scratch_Q/p31_policy.txt

# P1 逐条
python _scratch_Q/probe_p1.py  _scratch_Q/sb_head_new/src > _scratch_Q/p40_p1.txt 2>&1
python _scratch_Q/probe_p1b.py _scratch_Q/sb_head_new/src > _scratch_Q/p41_p1b.txt 2>&1
python _scratch_Q/probe_amount.py _scratch_Q/sb_head_new/src > _scratch_Q/p42_amount.txt 2>&1

# [9] 真实执行 / 无 SKIP / 补丁不可应用时响亮失败
python tests/integration/test_income_reconciliation.py            > _scratch_Q/r01_test_income.txt 2>&1   # exit 0, SKIP=0
python tests/integration/test_income_reconciliation.py            > _scratch_Q/p70_badpatch.txt 2>&1     # 垃圾补丁 -> FAIL=1 exit=1

# 统一离线回归清单 69 项 × pristine / 已应用 01+02+03+04
bash _scratch_Q/probe_smokes.sh                                   > _scratch_Q/p62_smoke_delta.txt 2>&1
python scripts/run_offline_regression.py                          > _scratch_Q/p51_offline_patched.txt 2>&1
```

## 4. 未覆盖项 / 无法离线验证

1. **真实 Binance `page`/`limit` 翻页稳定性**：离线只能用替身 fetch。P1-3 的跨页防线是机制层证明
   （页序回退 + 重叠复读 + 原始页推进）；真实并发写入下的丢行发生率需联网观测，**本次未验证**。
2. 补丁 01..04 应用后的**真实交易/网络路径**：未联网、不启服务；未验证真实响应的字段形状
   （`incomeType`/`tranId` 大小写）与真实同 `tranId` 改额行为。
3. **真实账户/密钥/`.env`/`data/`**：按纪律未读取；`account_ref` 派生与多账户键唯一性未验证。
4. `IncomeReconciliationBook` 与 §10.1 实际调用方的接线（谁提供 `existing`、谁聚合多窗口覆盖）
   **未验证**；P1-2 的实际触发面取决于接线方式。
5. **补全统一离线回归的完整绿**：`smoke_windows_service_plan.py` 在本环境（pristine）即失败，
   属环境性预存问题；我据此只报告「补丁 01 使 9 项**新增**失败」的**增量**，未声称门禁全绿基线。
6. `src/audit/migrations.py` 版本状态机在**已升级到 17 后又回退代码**时的行为未验证（仅确认前向 17 可用）。
7. 作者 README 的「Ruff/格式」主张未独立复跑（`scripts/check_quality.py` 在 pristine 与已应用两态
   同样失败于 `scan_tracked_secrets.py`，属沙箱缺 `.git` 的环境性问题，**不可归因**）。

## 5. 放行条件

必须（P0）：
1. **把「补丁 01 迁移 17 的 9 处伴随修改」登记给 I 并绑定 01 同批应用**
   （8 处版本断言 + `smoke_fault_matrix` 手工插入 + `smoke_release_acceptance` fixture manifest），
   并在 README 中撤回/修正「对既有表零影响」的表述；验收 = 已应用 01..04 的沙箱内
   `scripts/run_offline_regression.py` 相对 pristine **无新增失败**。
2.（I 的原子登记项，非作者缺陷）登记两个 `scripts/smoke_u01_*.py` 使统一入口 exit 0。

建议：P3-N1..N4 登记；§[3] 的「冲突键退出窗口聚合」写进 README 登记表（当前仅在代码注释里）。
