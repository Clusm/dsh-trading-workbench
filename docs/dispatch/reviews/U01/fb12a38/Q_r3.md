# U01 r3 独立复验报告（Q 角色 / 独立会话）

- 复验对象 head: `fb12a38`（分支 `task/U01`），链 `b4a322e` → `4127914` → `fb12a38`
- 上一轮: `b4a322e` 判 **CHANGES_REQUESTED**，唯一阻断项 = 补丁 01 迁移 17 破坏 9 个既有共享冒烟，
  且 README 谎称「对既有表零影响」（见 `docs/dispatch/reviews/U01/b4a322e/Q_r2.md`）
- 工作区: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U01\R3\r1`
  （`git worktree add --detach fb12a38`，独立、detached；全程未改任何 tracked 文件）
- 解释器: `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`，无 pytest）
- 沙箱（untracked）: `_scratch_Q/sb/{pristine,p01,p05,p0105,p0102030405,sb_order,sb_n3_new,sb_n3_old,sb_n4}`
  —— 每个 = `git archive fb12a38` + **独立 `git init`**（关键：避免在 repo 子目录里 `git apply`
  静默 `Skipped patch` 产生假阳性；r2 已踩过该坑）
- 不采信作者对照：四态对照、全量 delta、P3 反例、回归抽样全部由本会话自建探针复现

## 0. 结论

**PASS-with-notes**

r2 唯一阻断项已被**实质闭合**：补丁 05 真实存在、逐文件语义正确；本会话独立沙箱实测四态
（`pristine` 9/9 exit 0、`仅 01` 9/9 exit 1、`01+05` 9/9 exit 0、`01..04+05` 9/9 exit 0）；
**全量 69 项清单失败集合差集（pristine vs 01..04+05）= ∅**（两侧都只失败 `smoke_windows_service_plan.py`，
即既有 KI02 环境缺陷）；README 已撤回「对既有表零影响」并如实登记 01↔05 同批约束 + 逐文件理由。
P3-N3/N4 的「已修」经独立反例实证；P3-N1/N2 的「登记不修」理由经基线代码核对**成立**。
残留项均为 **P2 及以下且已在 README 登记**（新发现 2 条 P3 见 §2，均不影响门禁）。

## 1. 逐项复验

### [1] 补丁 05 真实存在且逐文件与声称一致 —— CLOSED（独立核对）

`docs/dispatch/patches/U01/author-2c73c9e/05_migration17_assertions.diff`，168 行 / 9 文件。
`awk` 逐文件统计（`_scratch_Q` 未落表，命令见 §3）：

| 文件 | +行 | -行 | hunk | 改动内容（独立核对） |
|---|---|---|---|---|
| `smoke_db_migrations.py` | 2 | 2 | 2 | `:37` `range(1,17)`→`range(1,18)`；`:78` 同 |
| `smoke_account_mirror.py` | 1 | 1 | 1 | `:103` 同 |
| `smoke_exchange_event_inbox.py` | 1 | 1 | 1 | `:61` 同 |
| `smoke_exchange_safety_monitor.py` | 1 | 1 | 1 | `:95` 同 |
| `smoke_order_mirror.py` | 1 | 1 | 1 | `:188` 同 |
| `smoke_order_outbox.py` | 1 | 1 | 1 | `:212` 同 |
| `smoke_protection_mirror.py` | 1 | 1 | 1 | `:119` 同 |
| `smoke_fault_matrix.py` | 14 | 14 | 3 | 见下 |
| `smoke_release_acceptance.py` | 1 | 1 | 1 | `:112` fixture manifest `schema_version` 16→17 |

**与声称一致性逐项核对结论：一致，无漏改 / 无多改 / 无语义改错。**

1. **「7 个脚本各 1 处 + `smoke_db_migrations` 2 处 = 8 处断言」** —— 成立。
   独立验证：全仓 `grep -rn "range(1, 1[0-9])"` 在 `scripts/` 下**恰好**命中这 8 处、分属 7 文件；
   `tests/`、`src/` 下**零**命中（`src` 只有 `SELECT MAX(version)` 形式）。
2. **「`smoke_fault_matrix` 手工 `VALUES(17)`→`18` 及配套断言/注释」** —— 成立，但计数口径需说明：
   该文件 3 个 hunk 共 14 条 ±行 = **9 个语义改动点**
   （注释 `:556`、注释 `:568`、`VALUES(17)`→`(18)`、`current_schema_version() == 18`、
   `恢复包含数据库且记录schema=18`、`ok_verify["database_schema"] == 18`、注释 `:594-595` 两行、
   `manifest["files"]["database"]["schema_version"] = 17`）
   **+ 5 行 `manifest16` → `manifest_legacy` 纯改名**（同一逻辑块内 5 处引用，README 已如实说明）。
   任务书与 README 写的「10 处」与我数出的「9 个语义点」差 1，属计数口径差异（是否把 2 行注释
   并作 1 处），**非事实偏差**。改名是必需的：改动后该变量装的是 `schema_version=17`，沿用旧名会让名字说谎。
   ✅ 独立核对 `grep` 后确认原 `manifest16` 的 **全部 5 处引用都已改名**，无遗留 `NameError` 风险。
   ✅ 负向用例语义**未被削弱**：`_scratch_Q/sb/p0102030405/scripts/smoke_fault_matrix.py:601-606`
   仍是「manifest 改写为更低版本 → `verify_recovery_bundle` 必须抛 `ValueError` → `mismatch_rejected=True`
   → `check(...)`」，实跑 exit 0，即**负控仍然真的拒绝**。
3. **「`smoke_release_acceptance` fixture manifest `schema_version` 16→17」** —— 成立，且是必要的：
   `src/core/release_evidence.py:448-451` 的 gate 比较 `DB 版本 == manifest 版本`。
4. **是否有漏改（未触及但同样依赖版本 16 的既有脚本）** —— **无**。独立全仓扫描
   `range(1, 17)` / `schema_version` / `current_schema_version`（`scripts/`、`tests/`、`src/`）：
   - 唯一形似的 `scripts/smoke_recovery_bundle.py:54` `assert manifest[...]["schema_version"] == 15`
     是**自建 raw sqlite fixture**（`:29-33` 手工 `INSERT INTO schema_migrations VALUES(15,'test')`），
     不经 `AuditDatabase`、与迁移 17 无关 —— 全量对照中该脚本在两态均 exit 0，实测证实。
   - 无其它 `== 16` / `>= 16` 形式的迁移版本断言。
5. **应用性与顺序**（独立沙箱，独立 `git init`）：`01→05`、`05→01` **两种顺序均 clean apply，无文本冲突**
   （`_scratch_Q/sb/sb_order`，05 先应用后 `range(1, 18)` 与 `_apply_migration(17` 同时存在）。
   与 README「两者无文本冲突」一致。

### [2] 四态对照 —— CLOSED（独立沙箱实测，与声称逐位吻合）

方法：`git archive fb12a38` + 独立 `git init`（**repo 根**，非子目录），
`git apply --check -v` 全部 `rc=0` 且**无 `Skipped`**，再顺序应用；用自建 harness
（复刻 `run_offline_regression.py` 的 env：`EXECUTION_MODE=paper`、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、剔除 `*BINANCE*KEY/SECRET/TOKEN*`、`cwd=项目根`）逐条跑并记录 exit code。

9 个脚本在四态下的结果（`_scratch_Q/r12_9_p01.txt`、`r13_9_p0105.txt`、`r14_9_p05.txt`）：

| 组合 | 结果 | 与声称 |
|---|---|---|
| `pristine`（不打补丁） | **9/9 exit 0** | ✅ 一致 |
| **仅 01** | **9/9 exit 1** | ✅ 一致 |
| `01+05` | **9/9 exit 0** | ✅ 一致 |
| `01+02+03+04+05` | **9/9 exit 0** | ✅ 一致 |

**仅 01 的失败原因已复现，且恰为声称的三类**（`_scratch_Q/r31_p01_reasons.txt`）：
```
smoke_db_migrations        exit=1  AssertionError: 记录迁移版本1到16
smoke_order_outbox         exit=1  AssertionError: 数据库迁移版本1到16完整
smoke_account_mirror       exit=1  AssertionError: 数据库迁移版本1到16完整
smoke_exchange_event_inbox exit=1  AssertionError: 数据库已记录迁移1到16
smoke_exchange_safety_mon. exit=1  AssertionError: 数据库迁移版本1到16完整
smoke_order_mirror         exit=1  AssertionError: 数据库迁移版本1到16完整
smoke_protection_mirror    exit=1  AssertionError: 数据库迁移版本1到16完整   ← 7× 版本断言
smoke_fault_matrix         exit=1  sqlite3.IntegrityError: UNIQUE constraint failed: schema_migrations.version
smoke_release_acceptance   exit=1  AssertionError: 严格发布报告可GO          ← schema gate
```
⇒ 与 r2 的 P0 归因**完全一致**（7× 断言 / 1× UNIQUE 崩溃 / 1× schema gate）。

### [3] 全量增量对比 —— CLOSED：**delta 为空，独立确认**

同一 harness 清单（从 `scripts/run_offline_regression.py` 的 `SCRIPTS` 元组机械解析，**69 项**）
× 同 env，在 `pristine` 与 `01+02+03+04+05` 两态各跑一遍（`_scratch_Q/r10_full_pristine.txt`、
`r11_full_p0102030405.txt`），逐条记录 exit code 后取差集（`_scratch_Q/r30_delta.txt`）：

```
A=_scratch_Q/full_pristine        n=69  FAIL=1
B=_scratch_Q/full_p0102030405     n=69  FAIL=1
A FAILURES: ['scripts/smoke_windows_service_plan.py']
B FAILURES: ['scripts/smoke_windows_service_plan.py']

NEW IN B (regressions): 0
GONE IN B: 0
DELTA EMPTY = True
```
**⇒ delta = ∅ 属实**，作者声称成立。两侧唯一的失败是**同一**既有环境缺陷
（KI02：`smoke_windows_service_plan.py` 期望项目根下有 `.venv/Scripts/python.exe`）
—— 我 diff 了两态该脚本的 traceback，除沙箱路径外**逐行相同**，与补丁无关。
**未发现任何「既有脚本新增失败」。**

### [4] P3 处置是否属实 —— CLOSED（逐条核验）

#### P3-N3（「已修」）—— 属实，且经**反面实证**
独立探针 `_scratch_Q/probe_n3.py`：先造 2 条已入账 FUNDING_FEE，再用
「1 条同键改额（→ quarantine）+ 1 条同键同内容（→ duplicate，仍留在 events）」的混合页重放，
**真正命中 `window_cost_finalization` 的提前 return 分支**（`sync.events` 非空 且 `attribution is None`）：

| 版本 | 混合轮 | `attribution=None` 的结果 |
|---|---|---|
| `fb12a38`（新） | `events=1 q=1 dup=1 ins=0 reasons=('INCOME_ID_CONFLICT',)` | `status=INCOMPLETE reasons=('NO_ATTRIBUTION_DECISIONS', 'INCOME_ID_CONFLICT')` → **`HAS_INCOME_ID_CONFLICT=True`** |
| `b4a322e`（旧） | 同上（行为相同） | `status=INCOMPLETE reasons=('NO_ATTRIBUTION_DECISIONS',)` → **`HAS_INCOME_ID_CONFLICT=False`** |

⇒ 修复**真实存在且是本批引入的行为变更**，新增回归断言（`test_income_reconciliation.py:415-423`，
「缺归因 + 有隔离 -> 两个 reason 同时回带」）对应真实反例，非空断言。
（附带观察：`attribution is None` 且 `sync.events` **为空**时走的是另一条分支 + `finalize_costs`，
此时也回带 `INCOME_ID_CONFLICT`；两条路径现在都覆盖。）

#### P3-N4（「已修」）—— 属实
- 代码：`_patched_database_dir` 返回的模式串改为 `f"补丁沙箱 git apply -p1（rc={applied.returncode}, {...}）"`；
  实跑输出证据为 `证据来源：补丁沙箱 git apply -p1（rc=0, 已应用）`（`_scratch_Q/r20_test_income.txt`）。
- 新增实证兜底：`_load_audit_database` 在导入后断言 `hasattr(module.AuditDatabase, "insert_income_events")`，
  缺失即 `raise RuntimeError`。
- **独立反面验证（不采信作者）**：在独立副本 `_scratch_Q/sb_n4` 里把补丁替换为垃圾内容后重跑
  （`_scratch_Q/r23_badpatch_n4.txt`）：
  ```
  FAIL  真实存储层可用（补丁可应用或已应用）: 补丁 01 无法应用到 src 副本 … No valid patches in input
  U01 income reconciliation: PASS=126 FAIL=1     exit=1     SKIP 计数=0
  ```
  ⇒ 补丁失效时仍然**响亮失败**，不是 SKIP、不是假绿。**前轮 P2-8 未被回退。**
- 微瑕（P3-N6，见 §2）：`else '未应用'` 分支实际不可达（非零 rc 已提前 raise），属冗余，非缺陷。

#### P3-N1（「登记不修：非本批回归」）—— 理由**成立**
独立核对基线代码 `git show c290c50:src/exchange/income_reconciliation.py`：
```
745: def assert_no_double_count(
...
757:     for event in income_events:          ← 作者引用的正是这一行
758:         kind = canonical_income_type(event.income_type)
759:         if kind in FILL_OWNED_TYPES and kind.lower() in additive:
```
`c290c50:757` **确实就是**那个以事件序列为外层循环的行；事件为空则判定体从不执行 →
基线同样对任意分量键放行。**「非本批回归」结论成立**，README 引用行号准确。
「不修」的理由（`_FILL_OWNED_KEY_MARKERS` 是「收入类型 → 键名 marker」映射，无事件则无法确定适用
哪些 marker；改为无条件拒绝是策略变更，会影响仓库外未知调用方）属**合理的范围控制**，且已如实登记。

#### P3-N2（「登记不修：仅存于已验收的补丁 01 内」）—— 理由**成立**
- 位置核对：`grep -n "_income_row" src/audit/database.py` → **HEAD 的 src 里零命中**；
  仅出现在补丁 01（3 处引用：定义 + `insert_income_events` + `insert_income_conflicts`）。
  ⇒ 「只存在于补丁 01 内、改它须重生成 01 并重跑全链」的事实前提**成立**。
- 危害等级核对（自建探针 `_scratch_Q/probe_n2.py`，`_scratch_Q/r28_n2.txt`）：
  ```
  [N2] str-0        -> inserted=1                     ← 正常路径（JSON 字符串）无碍
  [N2] Decimal-0    -> ValidationError: invalid decimal text: ''
  [N2] Decimal-0.00 -> ValidationError: invalid decimal text: ''
  [N2] int-0        -> ValidationError: invalid decimal text: ''
  [N2] str-ok       -> inserted=1
  ```
  ⇒ **fail-loud（结构化抛错），不是静默错账**；且只在调用方绕过 `persist` 直接把非字符串 falsy
  金额交给 `insert_income_events` 时可达。定级 P3 + 登记 + 给出后续小版本修法（`or ""` → `None` 判定）
  是**恰当**的。

### [5] 回归抽样（6 项，全部保持）

| # | 前轮 CLOSED 项 | 本轮独立复现 | 结论 |
|---|---|---|---|
| 1 | `[9]` 真跑（非 SKIP、走真实 `AuditDatabase`） | `_scratch_Q/r20_test_income.txt`：`SKIP` 计数 **0**、`PASS=140 FAIL=0`、`exit=0`；`[9]` 14 条断言全 PASS，含 `证据来源：补丁沙箱 git apply -p1（rc=0, 已应用）` | ✅ 未回退 |
| 2 | 补丁 01 的 `content_hash` 与 K03 同一真值源 | 自建探针自造 4 条流水（Σ=-2.80）落真实库后逐行比：`_scratch_Q/r21_probe_regress.txt` `[A] K03-SAME 4/4` | ✅ 未回退 |
| 3 | 「重放不抹账」 | 同探针 `[B]`：`replay ins=0 dup=4 q=0 events=4 sum=-2.80`；`db rows before=4 after=4 db_sum=-2.80`；`WIPED=False SUM_PRESERVED=True` | ✅ 未回退 |
| 4 | P2-8「补丁不可应用不得被 SKIP 掩盖」 | §[4]-N4 的垃圾补丁实验：`FAIL=1 exit=1 SKIP=0` | ✅ 未回退 |
| 5 | 作者自述 PASS 计数 | 独立复跑：`test_income_reconciliation` **PASS=140**、`test_review_statistics` **PASS=44**、`smoke_u01_income` **140**、`smoke_u01_review_statistics` **44**、`smoke_contracts_v3` **PASS 173 / FAIL 0** —— 五个数字与作者/编排者一致 | ✅ 一致 |
| 6 | 补丁矩阵（01..04 各自/组合 clean apply） | 五补丁 + `05→01` 逆序在独立 `git init` 沙箱 clean apply，无 `Skipped`（§[1].5） | ✅ 未回退 |

## 2. 新发现

### P3-N5（新，未登记，**不阻断**）生产运维文档仍把 `current_schema_version() == 16` 写死为验收断言

- `docs/双进程双账本改造设计_2026-09-15.md:222` `3. 断言 current_schema_version() == 16`
- `docs/双进程双账本改造设计_2026-09-15.md:224` `5. 断言 live 库 current_schema_version() == 16 且 …`
- `docs/实盘账户并行接入评估与上线整改清单_2026-09-15.md:202` / `:363` / `:472`（F-01 行）同上
- 事实：补丁 01 应用后 DB 迁移到 **17**，照这两份 runbook 断言 `== 16` 会**误判为失败**。
- 定级理由：**fail-closed**（多报一次不通过，不会放行错误状态），且这些是**带日期的历史设计/评估文档**，
  不是可执行门禁；不进 `run_offline_regression.py`，不影响任何 CI。故仅 P3。
- 建议：登记为「I 应用 01 时同步更新这两份 runbook 的断言为 `>= 16`」。
  （README 目前只登记了 `docs/contracts/README.md` §12，未覆盖这两份。）

### P3-N6（新，微瑕）`_patched_database_dir` 的 `else '未应用'` 分支不可达

`applied.returncode != 0` 时函数已在上方 `raise RuntimeError`，故
`f"...{'已应用' if applied.returncode == 0 else '未应用'}"` 的 else 分支是死代码。
**不影响正确性**（模式串现在由真实 rc 派生，且另有 `hasattr` 实证），仅提示可简化。

### 已确认的批次约束（非缺陷，需传导给 I）

- **`仅 05`（不打 01）会破坏 8/9 个脚本**（`_scratch_Q/r14_9_p05.txt`：`TOTAL=9 FAIL=8`，
  失败原因统一为断言 `…1到17…` 而 DB 仍在 16；只有 `smoke_fault_matrix` 因自建 DB 而通过）。
  README 已明确写出「05 的断言只有在 01 已应用时才成立」「不得只合 01 不合 05；若 05 被拒，01 也必须一起推迟」，
  **口径正确**，但 I 侧执行时须严格按 `先 01 后 05`（本次实测两种顺序文本上均可应用，仅断言成立性依赖顺序）。

## 3. 命令清单（原始输出全部重定向留档于 `_scratch_Q/`）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"   # PYTHONUTF8=1

# 0) 工作区（独立 detached）
git -C "…/mat-wt/I" worktree add --detach "…/reviews/U01/R3/r1" fb12a38

# 1) 沙箱：git archive + 独立 git init（避免 repo 子目录静默 Skipped）
for n in pristine p01 p05 p0105 p0102030405; do mkdir -p _scratch_Q/sb/$n;
  git -C . archive fb12a38 | tar -x -C _scratch_Q/sb/$n; done
# 每沙箱内: git init -q && git add -A && git commit -qm base
# 逐组合 git apply --check -v + git apply       -> _scratch_Q/s01_apply.txt
# 逆序验证 05→01                                 -> _scratch_Q/sb/sb_order

# 2) 四态 / 全量（自建 harness，复刻 run_offline_regression 的 env 与 cwd）
$PY _scratch_Q/harness.py _scratch_Q/sb/pristine      _scratch_Q/full_pristine      > _scratch_Q/r10_full_pristine.txt 2>&1
$PY _scratch_Q/harness.py _scratch_Q/sb/p0102030405   _scratch_Q/full_p0102030405   > _scratch_Q/r11_full_p0102030405.txt 2>&1
$PY _scratch_Q/harness.py _scratch_Q/sb/p01          _scratch_Q/full_p01  <9个>     > _scratch_Q/r12_9_p01.txt 2>&1
$PY _scratch_Q/harness.py _scratch_Q/sb/p0105        _scratch_Q/full_p0105 <9个>    > _scratch_Q/r13_9_p0105.txt 2>&1
$PY _scratch_Q/harness.py _scratch_Q/sb/p05          _scratch_Q/full_p05  <9个>     > _scratch_Q/r14_9_p05.txt 2>&1
$PY _scratch_Q/delta.py _scratch_Q/full_pristine _scratch_Q/full_p0102030405       > _scratch_Q/r30_delta.txt 2>&1

# 3) P3 反例 / 回归
$PY _scratch_Q/probe_regress.py                       > _scratch_Q/r21_probe_regress.txt 2>&1  # K03 真值源 + 重放不抹账
$PY _scratch_Q/probe_n3.py _scratch_Q/sb_n3_new/src HEAD-fb12a38 ;  # N3 新
$PY _scratch_Q/probe_n3.py _scratch_Q/sb_n3_old/src b4a322e-r2      > _scratch_Q/r22_n3.txt 2>&1
$PY _scratch_Q/probe_n2.py _scratch_Q/sb/p0102030405/src           > _scratch_Q/r28_n2.txt 2>&1
(cd _scratch_Q/sb_n4 && $PY tests/integration/test_income_reconciliation.py) > _scratch_Q/r23_badpatch_n4.txt 2>&1

# 4) 独立复跑作者自述计数（在工作区内，纯读）
$PY tests/integration/test_income_reconciliation.py   > _scratch_Q/r20_test_income.txt 2>&1   # PASS=140 SKIP=0
$PY tests/integration/test_review_statistics.py       > _scratch_Q/r24_test_review.txt 2>&1   # PASS=44
$PY scripts/smoke_u01_income.py                       > _scratch_Q/r25_smoke_u01_income.txt 2>&1
$PY scripts/smoke_u01_review_statistics.py            > _scratch_Q/r26_smoke_u01_review.txt 2>&1
$PY scripts/smoke_contracts_v3.py                     > _scratch_Q/r27_smoke_contracts_v3.txt 2>&1
```

## 4. 未覆盖项 / 无法离线验证

1. **真实 Binance 行为**：`GET /fapi/v1/income` 的真实字段形状、真实 `tranId` 改额、真实分页稳定性
   —— 未联网，本次仍未验证（承袭 r2 未覆盖项 1/2）。
2. **真实账户 / 密钥 / `.env` / `data/`**：按纪律未读取；多账户键唯一性、真实库迁移实测未验证。
3. **`docs/` 两份 runbook（P3-N5）的实际运维影响**：无法离线验证部署流程；仅做静态核对。
4. **`src/audit/migrations.py` 在「已升到 17 后回退代码」时的行为**：未验证（仅确认前向 17 可用）。
5. **作者 README 的 Ruff/格式主张**：未独立复跑（沙箱缺 `.git` 时 `scan_tracked_secrets.py` 会失败，
   与 r2 相同，**不可归因**）。
6. **I 侧原子登记项**（`scripts/run_offline_regression.py` 补登两个 `smoke_u01_*.py`）属 I 的职责，
   作者已如实登记为「登记给 I」；本报告未验证 I 是否已执行。

## 5. 放行条件

无 P0/P1/P2 阻断项。放行建议（全部为「登记/传导」，不要求作者再改代码）：

1. I 应用时**必须** `01` 与 `05` 同批、按 `先 01 后 05` 顺序（否则 8/9 个既有共享冒烟变红，已实测）；
   验收 = 已应用 `01..04+05` 的沙箱内 `run_offline_regression.py` 相对 pristine **无新增失败**（本次已证 delta=∅）。
2. I 的原子登记项：把两个 `scripts/smoke_u01_*.py` 补入 `SCRIPTS`，否则 `_validate_smoke_manifest()` 直接抛错。
3. 建议登记 **P3-N5**（两份 runbook 的 `== 16` 断言随 01 同批更新）与 **P3-N6**（死分支简化）。
