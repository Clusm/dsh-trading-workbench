# U09-FU1 · 发布门禁两条 fail-open 缺陷修复（前后对照 / 测试改动自证）

- 作者工作区：`mat-wt/U09F1`，分支 `task/U09-FU1`，base = 集成 HEAD `4de8666`（干净）
- **代码 head（本包生成基准）：`b03bee4`**（本 `docs/` 包落盘后 head 再前进一步）
- 修复的两条（台账 `dispatch.known_issues`，均由复验者 A 独立发现、判非阻塞但**方向都 fail-open**）：
  1. `U09-OUTCOME-VS-LIFECYCLE-COST` —— 门禁读 outcome 成本快照而非 lifecycle 权威态
  2. `U09-NET-UNKNOWN-PADDING` —— 净收益未知的生命周期仍被计入 30，且零门槛
- 全程离线：只在系统临时目录造合成 SQLite；未读 `.env`/`data/`/密钥，未联网、未下单、
  未发真实告警，未动风险档/白名单/live 开关；产物只落本工作树。

## 0. 一句话结论

两条缺陷都按 **fail-closed** 修：合格集合**只减不增**（旧口径下 eligible 的样本，
在新口径下要么仍 eligible、要么被排除，**没有任何一个旧口径下不 eligible 的样本
变成 eligible**）。发布门禁的三个数字 **7 天 / 200 方向候选 / 30 完整闭合 一个未动**。

---

## 1. 交付物

| 类别 | 路径 | 归属 |
|---|---|---|
| 修复 | `src/core/release_evidence.py`（+114/−19 行） | U09 独占（已提交 `task/U09-FU1`） |
| 测试（两态可跑） | `tests/acceptance/test_release_evidence_v2.py`（+135/−2） | 同上 |
| 测试（冒烟） | `scripts/smoke_u09_release_evidence.py`（+123/−2） | 同上 |
| **共享适配补丁** | `02_smoke_release_acceptance_fixture.diff`（+5/−1，目标树 `4de8666`） | **只交 diff，由 I 合入** |
| 修复 diff（复核用） | `01_u09fu1_release_evidence.diff` | 本目录 |
| 证据 | `evidence/`（前后探针输出、修前失败套件输出、两态输出、回归摘要、check_quality） | 本目录 |

> `01_*.diff` 与分支 `task/U09-FU1` 的 `4de8666..b03bee4` 逐字节一致，供 I 用
> `git apply` 或直接合分支两条路径复核。`02_*.diff` **不在分支里**（见 §7）。
>
> `evidence/fu1_probe.py.txt` 是前后对照探针（`.py.txt` 后缀，避免把可运行 `.py`
> 归档进被 lint 的树）。复现时另存为 `_fu1_probe.py` 放仓库根（`.gitignore` 的
> `/_*.py` 已覆盖，`check_quality` 的 `untracked=0` 可验证）。

### 补丁应用顺序与 3 种 git 配置实测

```
git apply 01_u09fu1_release_evidence.diff      # 与分支内容一致；若直接合分支可跳过
git apply 02_smoke_release_acceptance_fixture.diff
```

沙箱 = `git archive 4de8666 | tar -x` 后**立刻 `git init`**（独立 repo，非仓库子目录）。

| git 配置 | 01 | 02 |
|---|---|---|
| 默认（仓库自带 `core.autocrlf=false`） | `Checking patch` ×3，**0 Skipped / 0 error** | `Checking patch` ×1，**0 Skipped / 0 error** |
| `-c core.autocrlf=false -c core.eol=lf` | 同上 | 同上 |
| `-c core.autocrlf=true` | 同上 | 同上 |

---

## 2. 缺陷 1 · `U09-OUTCOME-VS-LIFECYCLE-COST`（修前 PASS / 修后 FAIL）

### 权威性依据（复验者已核实，本次原样采纳）

| 写方 | 动作 | 证据 |
|---|---|---|
| `src/exchange/trade_reconciliation.py:625` | **唯一**成本重算写方，`UPDATE position_lifecycles SET ... cost_status=?` | 全仓唯一 |
| `src/exchange/binance_futures.py:2652/:3120` | 拿到开仓手续费后**只把 lifecycle 降级**为 `INCOMPLETE`，不碰 outcome | 两处 |
| `src/audit/database.py:2082+` `log_trade_outcome` | `INSERT OR IGNORE`，同键复写**被静默丢弃** | 行数 1→1 |

⇒ **`position_lifecycles` 是可变的当前权威态；`trade_outcomes` 是首写即冻结的快照。**

### 修法（单调收紧，不是"换个表读"）

1. 查询新增 `l.cost_status AS lifecycle_cost_status` /
   `l.attribution_status AS lifecycle_attribution_status`（`release_evidence.py`
   的 lifecycle 查询原本**根本没 SELECT lifecycle 的 cost_status**）。
2. 新增纯函数 `lifecycle_authority_reason(row) -> str | None`：返回**不计入 30 的
   原因码**，合格返回 `None`。判据顺序：CLOSED/证据齐 → 归因 → **权威态成本** →
   **快照成本** → 价格数量。
3. `_eligible_lifecycle(row)` 变成 `lifecycle_authority_reason(row) is None` 的薄包装。

**为什么要求"两表都 COMPLETE"**：只认 lifecycle 会让"lifecycle 说 COMPLETE、
快照说 INCOMPLETE"这一路从 FAIL 变成 PASS（**放宽**）。两表取与 ⇒ 合格集合是旧
合格集合的**子集**（旧口径只认快照；新口径 = 旧口径 ∧ 权威态），是严格单调控紧。
leader review 要求的"lifecycle 说未完成就不得计入 30"被无条件满足。
**outcome 快照没有被删除**：它继续出现在 `eligible_closed_rows`，并新增
`cost_status_snapshot_mismatches`（含 `snapshot_more_optimistic` 标记）作交叉校验。

### 前后对照（同一探针 `_fu1_probe.py`，命令 + exit）

```bash
PY=<venv>/Scripts/python.exe; export PYTHONUTF8=1
$PY _fu1_probe.py                      # 修前：./evidence/prefix_probe.txt   exit=0
                                       # 修后：./evidence/postfix_probe.txt  exit=0
```

| 场景（30 条基线样本，只改一件事） | 修前 | 修后 | 判定 |
|---|---|---|---|
| 两表都 `COMPLETE`（正常） | `PASS actual=30` | `PASS actual=30` | 无副作用 |
| **`lifecycle=INCOMPLETE` / `outcome=COMPLETE`** | **`PASS actual=30`** ← 错 | **`FAIL actual=0`**，`incomplete_closed=30` | **修复** |
| `lifecycle=COMPLETE` / `outcome=INCOMPLETE` | `FAIL actual=0` | `FAIL actual=0` | 未放宽 |

**"修前失败 / 修后通过"的套件级对照**（把**新**测试文件扣在**旧** `release_evidence.py`
上跑，`evidence/prefix_failing_*.txt`）：

| 命令 | 修前（旧代码 + 新测试） | 修后（新代码 + 新测试） |
|---|---|---|
| `$PY scripts/smoke_u09_release_evidence.py` | **exit=1**，`[4b]` 里 4 条 FAIL（`(30, {})`：权威态被无视） | exit=0，`67 通过 / 0 失败` |
| `$PY tests/acceptance/test_release_evidence_v2.py` | **exit=1**，`§4b` 里 4 条 FAIL | exit=0，`86 通过 / 0 失败` |

---

## 3. 缺陷 2 · `U09-NET-UNKNOWN-PADDING`（修前 PASS / 修后 FAIL）

### 我选的口径：**未知净收益不计入 eligible**（不采用"设上限"方案）

- **判据**：`record["net_realized"] is None` ⇒ 不计入 30，原因码
  `NET_REALIZED_UNKNOWN`，计入 `eligible_closed_exclusion_reasons`。
- **为什么不用"计入但设 `net_unknown` 上限并作为独立阻断项"**：
  上限需要一个**凭空发明的数字**（1？3？5%？），而这个数字本身就是新的门禁参数，
  容易被后续调参。**排除是"零参数"的**：不需要给"未知"定阈值，直接不认它当凭证，
  正是复验者要求的「未知不得充当完整闭合凭证」。门禁的 `required=30` 一个字未动。
- **"未知" vs "合法零"如何区分**：只看 `is None`，**不看真值**。
  `float(...) is None` ⇒ 未知；`0.0` ⇒ 合法零收益，照常计入。
  证据：`[D2-30条合法零收益(net=0.0)]` 修前修后都是 `PASS actual=30`，
  `net_realized_unknown_lifecycles=0`，且 `eligible_closed_rows` 里
  `net_realized == 0.0`（不是 `None`）。
- **未知也可能来自 `funding=NULL`**（与 `funding=0.0` 区分），沿用 `_net_realized`
  既有的"任一成本项缺失即 `None`，不用 0 冒充"语义，未改公式。
- **顺带满足 U01 不变量**：`cost_status=COMPLETE ⟹ 净收益可算`。两表都 COMPLETE
  却算不出净收益 = **数据自相矛盾**，本修复把它从"静默计入"变成"排除 + 披露原因"。

### 前后对照（探针）

| 场景 | 修前 | 修后 |
|---|---|---|
| 30 条 `COMPLETE` + `funding=NULL`（净收益可算样本 0） | **`PASS actual=30`**，`net_computable_rows=0` ← 错 | **`FAIL actual=0`**，`net_realized_unknown_lifecycles=30` |
| **29 条真凭证 + 1 条未知** | **`PASS actual=30`** ← 错（1 个未知凑成 30） | **`FAIL actual=29`**，`NET_REALIZED_UNKNOWN=1` |
| 30 条合法零收益 `net_realized == 0.0` | `PASS actual=30` | `PASS actual=30`（**未误伤**） |

---

## 4. 反向证据（未误伤合法场景）

| 合法场景 | 构造 | 断言 | 结果 |
|---|---|---|---|
| 两表都 `COMPLETE` 的正常样本 | 30 条标准夹具 | 仍计入 30 且 `eligible_closed_trades=PASS` | ✅ 修前修后一致 |
| **合法零收益** `net_realized == 0.0` | `gross=0, entry_fee=0, exit_fee=0, funding=0.0` | 仍计入 30、`net_unknown=0`、每行 `net_realized == 0.0` | ✅ 修前修后一致 |
| 迟到修订重算（F23） | 既有夹具（`lc-0` 修订 `exit_fee 0.1→0.4`） | 仍 30、`net_realized=0.55`、hash 变化 | ✅ 既有断言全绿 |
| 冲突隔离（F23） | 同 `recorded_at` 内容冲突 | 仍 29 + 门禁 FAIL | ✅ 既有断言全绿 |
| 窗口/来源/ACK/重启（F16/F24/F12） | 既有场景 | 逐条不变 | ✅ 60→67 全绿（新增 7 条只增不减） |

**"谁能声明 PASS"未放宽**：本次改动只碰 `eligible_closed_trades` 的**分母**，
未触碰 `declaration=` / `K06` / `_group_evidence` / `PASS_WITHOUT_EVIDENCE` /
`IDENTITY_REF_MISSING` 任何一行；`smoke` 的
`声明 PASS 而窗口缺失 → FAIL + PASS_WITHOUT_EVIDENCE/WINDOW_MISSING` 与
`缺运行工件身份 → IDENTITY_REF_MISSING 阻断` 两条断言两态照旧通过。

**`directional_candidates` 的 200 口径一个字没动**：S08 来源过滤
（`GATE_ROLE_EXECUTION_DECISION`）、NO_EDGE 排除、机会键去重全部未触碰；
`A only=200 / A+B+C=200` 的断言仍通过。

---

## 5. 改动的既有行为（需要知道的两处语义变化）

| 字段 | 旧语义 | 新语义 | 为什么不是"放宽" |
|---|---|---|---|
| `release_evidence.net_realized_unknown_lifecycles` | 计入 30 的样本里净收益不可算的条数（旧口径下实测可为 30 却仍 PASS） | **因净收益不可算而未计入 30** 的生命周期条数 | 同一批数据下该字段值不变（30 / 1），但**它现在是一个排除原因，不再是"披露而已"**；断言 `== 0` 在两态仍成立 |
| `eligible_closed_rows[*].cost_recomputed` | `False` 表示"计入了但没算出" | 恒为 `True` | 不可算的行已不进 `eligible_closed_rows`；这是不变量的显式化，旧断言 `cost_recomputed is True` 仍成立 |

新增字段（全部为**新增**，不动既有键）：`eligible_closed_exclusion_reasons`、
`cost_authority`、`cost_status_snapshot_mismatches`，以及
`gates.eligible_closed_trades` 的 `cost_authority` / `excluded_by_reason` /
`net_unknown_excluded` / `cost_snapshot_mismatches`。

---

## 6. 测试改动的逐条自证（**为什么是"跟随新口径"而不是"为变绿放宽"**）

先给结论：**没有任何一条既有断言被删除或放宽**——两个套件在**只换实现、不换测试**
的情况下（`evidence/postfix_*` 与 `prefix_*` 的差异只有新增场景）就已经全绿。
真正的改动是**新增覆盖**，外加两处 `+1` 行的夹具修正。

| # | 文件 | 改动 | 类型 | 自证 |
|---|---|---|---|---|
| 1 | `smoke_u09_release_evidence.py` | `add_lifecycle()` 新增**可选**入参 `lifecycle_cost_status`（默认 `None` ⇒ 与 `cost_status` 同值） | 夹具扩展 | 默认路径与旧行为**逐字相同**；既有 60 条断言不改一字仍全绿。不新增该参数就**无法**构造"两表分歧"这一被修复的对象 |
| 2 | 同上 | 新增 `[4b]` 场景 `scenario_cost_authority_and_net_unknown()`，7 条断言 | **新增** | 6 类构造 × 前后对照见 §2/§3；其中 3 条是**反向证据**（正常样本、快照悲观、合法零收益），即在防止过度收紧 |
| 3 | `tests/acceptance/test_release_evidence_v2.py` | `add_closed_lifecycle()` 同上加 `lifecycle_cost_status` | 夹具扩展 | 同上 |
| 4 | 同上 | 新增 `§4b` 函数 `test_lifecycle_cost_authority_and_net_unknown()`，8 条断言 | **新增** | 5 条是**两态**断言（`APPLIED` 分支各写一半，两态都为真，无 SKIP 掩盖）；基线分支**断言缺陷存在**（`eligible_closed_lifecycles == 30`），不是"基线态跳过" |
| 5 | 同上 | `main()` 注册新函数 | 接线 | 7 → 8 个测试函数，无删除 |
| 6 | 同上 | 一处 FAIL 详情由 `block` 改为 `'net=None 的行数=N'` | 输出可读性 | **不涉及断言真假**；旧写法在失败时打印 ~28KB 的整块转储（见 `evidence/prefix_failing_acceptance.txt` 的历史形态） |

**两态可跑实测**（`evidence/baseline_two_state_acceptance.txt`）：
把**新**测试文件扣在 **U09 之前的** `release_evidence.py`（`git show 448a4e9:...`）上：

```
$PY tests/acceptance/test_release_evidence_v2.py     # exit=0
U09 验收: 31 通过 / 0 失败        （基线 26 条 + 本次新增的 5 条两态断言）
  PASS  基线态：lifecycle 的 INCOMPLETE 被无视，快照 COMPLETE 即放行（缺陷证据）
  PASS  基线态：净收益未知的样本仍被计入 30（缺陷证据）
```

即：**不硬编码"补丁已应用"**，也不硬编码迁移版本集合。冒烟脚本在**基线态本来就跑不起来**
（`build_release_readiness_report()` 不接受 `declaration=`），这是**既有**状态，与本修复无关：
未改动的 `4de8666` 冒烟扣到 `448a4e9` 上**同样**抛
`TypeError: ... unexpected keyword argument 'declaration'`
（`evidence/baseline_smoke_pre_existing_typeerror.txt`）。U09 README §5 已登记"冒烟不适用于基线态"。**未修**（见 §9）。

---

## 7. 共享文件：只出补丁，不提交进我的分支

### `02_smoke_release_acceptance_fixture.diff`（`scripts/smoke_release_acceptance.py`，+5/−1）

**为什么非改不可**：该夹具造 30 条 `lifecycle` + `outcome`，两表都写
`cost_status="COMPLETE"`，却**不给 `funding_cashflow`**（留 `NULL`）。这正是复验者
A 的最小构造 V1 的同型数据 —— `cost_status=COMPLETE` 却成本项缺失，**违反 U01 不变量**
（`trade_reconciliation.py:610` 的 `cost_complete` 要求 `entry_fee/exit_fee/funding`
三者非 `None` 才给 `COMPLETE`）。它同时断言
`eligible_closed_lifecycles == 30` 与 `release_readiness == "GO"`。

于是：**任何** fail-closed 的修法（排除制 **或** 阻断项制）都会让它变红——
因为它自称"完整闭合的成本证据"，而它的数据里根本没有资金费证据。
我不是在选择性调参，而是在**修正一个与自身断言矛盾的夹具**。

改动本身：加 `funding_cashflow=0.0`。

| 维度 | 说明 |
|---|---|
| 门槛 | 未动（本文件里没有任何门槛数字） |
| 语义 | `0.0` = **零资金费**（≠ 缺证据），与 U09 README §4 F10「`funding=0` 当作 0（不是缺证据）」一致 |
| 断言 | 一条都未改；两条断言（30 条 + GO）从"靠静默忽略缺失成本项成立"变成"靠真实齐备的成本成立" |
| 谁受影响 | 仅 `run_offline_regression.py` 里登记的 `scripts/smoke_release_acceptance.py` |
| 证据 | 修前 `AssertionError: 完整闭合生命周期达到门槛`（exit=1）→ 加 `0.0` 后 `17 通过 / 0 失败`（exit=0） |

> 说明：**没有**"为了让它变绿而降低口径"。若把该夹具的 `cost_status` 改成
> `UNKNOWN` 来规避，那才是放宽；改成"真的把成本三项给齐"是把夹具拉回它自己声称的状态。

---

## 8. 沙箱全量回归 delta（NEW_FAIL 必须为 ∅）

沙箱：`git archive 4de8666 | tar -x -C <临时目录>`，**解包后立刻 `git init`**（独立 repo）。
`patched` = 同一 base + `01`（= 我分支内容）+ `02`。

| 运行 | 树 | 登记脚本数 | 结果 | rc | 内部 `FAIL` 行 |
|---|---|---|---|---|---|
| **pristine** | `4de8666` | 94 | `OFFLINE REGRESSION OK scripts=94` | 0 | 10 |
| **patched** | `4de8666` + 01 + 02 | 94 | `OFFLINE REGRESSION OK scripts=94` | 0 | 10 |
| **NEW_FAIL（我引入的新失败）** | — | — | — | — | **∅** |
| **FIXED（我修掉的失败）** | — | — | — | — | **∅** |

断言方式：`grep -E "^  FAIL" | sort` 两次逐行对比（`comm -13` / `comm -23`），两侧均为 ∅。
那 10 行是 `scripts/smoke_u02_strategy_position_manager.py` **自己打印的内部断言**
（U15 的 `account_ref`/幂等键期望，属既有状态，两态逐字相同；脚本本身 rc=0）。

受影响/邻近套件（**patched 沙箱内逐个跑，rc 全 0**）：

| 套件 | pristine | patched |
|---|---|---|
| `scripts/smoke_u09_release_evidence.py` | 60 / 0 | **67 / 0** |
| `tests/acceptance/test_release_evidence_v2.py` | 78 / 0 | **86 / 0** |
| `scripts/smoke_contracts_v3.py` | PASS 173 / FAIL 0 | PASS 173 / FAIL 0 |
| `scripts/smoke_u06a_shadow_isolation.py` | rc=0 | rc=0 |
| `tests/integration/test_shadow_labels.py` | `U06B SHADOW LABELS OK` | 同 |
| `scripts/smoke_db_migrations.py` | 13 / 0 | 13 / 0 |
| `scripts/smoke_release_acceptance.py` | 17 / 0 | 17 / 0（需 `02`） |
| `docs/dispatch/patches/U09/evidence/probe_cli_report.py` | 脚本 rc=0；分场景 rc=0/1（预期） | 同（`directional_candidates=200 shadow_rows=250` 未变） |

`scripts/check_quality.py`：**exit=0**，`QUALITY CHECK OK files=303 untracked=0`
（未新增任何 ruff exclude；`_fu1_probe.py` 被 `.gitignore` 的 `/_*.py` 覆盖，未进检查面）。

---

## 9. 未尽事项（如实登记）

1. **`smoke_u09_release_evidence.py` 在 U09 之前的基线上跑不起来**（
   `build_release_readiness_report()` 不接受 `declaration=`）。这是**既有**状态
   （未改动的原版在 `448a4e9` 上同样抛 `TypeError`，见
   `evidence/baseline_smoke_pre_existing_typeerror.txt`），U09 README §5 已登记
   "冒烟不适用于基线态"。本次**未顺手修**（避免夹带无关改动）；"两态可跑"由
   验收套件承担并有实测。
2. **真写路径端到端仍未能离线复现**"lifecycle 与 outcome 分歧"的产生过程
   （需要联网/下单跑 `trade_reconciliation` + 迟到 EXIT 成交）。本次只做**静态构造**
   + 纯函数/DB 行级实测，与复验者 A 的"待真机复核"结论一致。
3. **`net_realized` 的数值仍取自 outcome 最新修订行**（F23 的迟到修订语义），
   **不**改从 lifecycle 的数值列重算。理由：lifecycle 的 `exit_fee`/`gross_realized`
   只在 `trade_reconciliation:625` 被更新、`entry_fee`/`funding_cashflow` 由别的路径
   写；若改从 lifecycle 取数，会破坏 F23 的"修订后重算"语义（`exit_fee 0.1→0.4` 的
   修订只写在 outcome 侧）。本次只把 **lifecycle 作为成本完整性的否决位**，
   net 的**取数口**保持不动 —— 这是一处**有意保留的边界**。
4. **`funding=NULL` 与 `funding=0.0` 的区分**依赖写入方显式给 0；
   历史库里若确有大量 `funding=NULL` 但 `cost_status=COMPLETE` 的旧行，
   本修复会把它们从 30 里排除（fail-closed）。这属预期行为，但**真实发布前应
   用一次离线统计确认影响面**（本次未读真实 `data/`，无法给出数字）。
5. **`_eligible_lifecycle` 的 `.upper()` 宽容比较**（P3 · N2）保留未动：
   与 U01 大写枚举的严格性差异仍在，本次不扩围。
6. 其余已登记项（`U13FU3-INJECTION-TRUST`、`U08-R5-*`、`U09-K06-*`、
   `REVISION_CONFLICT:outcome_id=` 死分支 P3、`smoke_u02` 自计数瑕疵）**本次一概未动**。

## 10. 复现命令

```bash
PY=<venv>/Scripts/python.exe; export PYTHONUTF8=1
cd <worktree|sandbox>
$PY _fu1_probe.py                                    # 前后对照探针（另存自 evidence/*.py.txt）
$PY tests/acceptance/test_release_evidence_v2.py     # 86/0（修后）
$PY scripts/smoke_u09_release_evidence.py            # 67/0（修后）
$PY -m ruff check src/core/release_evidence.py scripts/smoke_u09_release_evidence.py \
    tests/acceptance/test_release_evidence_v2.py     # All checks passed!
$PY scripts/check_quality.py                         # QUALITY CHECK OK
$PY scripts/run_offline_regression.py                # OFFLINE REGRESSION OK scripts=94
```

沙箱（3 种 git 配置 `--check`）：

```bash
git archive 4de8666 | tar -x -C <sandbox> && cd <sandbox> && git init -q . && \
  git add -A && git commit -qm base
for f in 01_u09fu1_release_evidence.diff 02_smoke_release_acceptance_fixture.diff; do
  git apply --check --verbose "$f"
  git -c core.autocrlf=false -c core.eol=lf apply --check --verbose "$f"
  git -c core.autocrlf=true apply --check --verbose "$f"
done    # 6/6：0 Skipped / 0 error
```
