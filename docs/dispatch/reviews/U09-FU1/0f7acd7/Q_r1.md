# Q_U09-FU1_r1 — U09-FU1 独立验收（Q 角色，对抗式）

- 工作区: `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U09-FU1\R1\q`（detached HEAD @ `0f7acd7`）
- 代码 head `b03bee4`，base `4de8666`；分支仅改 3 个文件（见 §7）
- 只验证、只写报告、**未改任何产品代码**。
- 探针全部自建（`_scratch_Q/probe_q.py`、`probe2_q.py`、`enum_q.py`、`diff_q.py`、`zero_vs_unknown.py`、`driver_q.py`），**未复用**作者 `fu1_probe.py.txt`。
- 树: `_scratch_Q/pristine`（`git archive 4de8666`+`git init`，真旧码）、`_scratch_Q/patched`（`git archive b03bee4`+`git init`，仅分支头）、`_scratch_Q/combined`（= b03bee4 **+ patch02**，即作者回归证据所依据的状态）。

## 1. 结论

**PASS-with-notes**

两条 fail-open 均被独立复现（修前确实错），修复后确实翻转（修后确实对）；合格集合在**规范枚举值域内单调收紧**（0 个"旧不合格→新合格"，唯 §1 Note 3 的空白边角为不可达例外）；未知/合法零严格可分；测试改动为跟随新口径、未删/未放宽既有断言；门禁数字与 `gates` 键集合一字未动；补丁矩阵 6/6 干净。

**Notes（非阻塞，但交付方/合入方须处理）**

1. **`scripts/smoke_release_acceptance.py` 的 patch02 必须与分支同时合入**。否则分支头单独跑全量离线回归会在 `scripts/smoke_release_acceptance.py` 处 **rc=1 中断**（实测：patched 头跑 `run_offline_regression.py` 在第 42 个脚本报 `CalledProcessError ... exit status 1`，失败断言正是 `完整闭合生命周期达到门槛`）。作者已声明 patch02 只出补丁、由 I 合入 —— 属**已声明的依赖**，但若漏合即回归红。
2. `smoke_release_acceptance` 夹具改动确系"救活一条即将变红的既有断言"（§6），但救活方式是**把夹具数据补成它自称的状态**，既不改产品代码也不改断言，判**非放宽**。
3. **`.strip()` 带来的理论边角放宽（不可达）**：新码 `_status_of` 用 `.strip().upper()`，旧码仅 `.upper()`。故枚举值带前后空白（如 `" COMPLETE "` / `" ATTRIBUTED "`）时，**旧不合格 → 新合格**（实测 3 例，§3）。经查 **全仓无任何写方产出带空白的枚举值**（所有写方用规范字面量或 `trade_reconciliation` 计算出的 `COMPLETE/INCOMPLETE/UNKNOWN`），且被接受的是"带空白的肯定值"（不会让 INCOMPLETE 混入），故**判非阻塞**；但它严格违反了作者"没有任何旧不合格样本变合格"的绝对表述，如实记此偏差。

## 2. 第 1 条：6 场景前后对照（自建探针 `probe_q.py`）

命令：`python _scratch_Q/probe_q.py <tree>`（两态各一次；输出 `probe_pristine.txt` / `probe_patched.txt`，exit=0）。

| 场景（每场景 30 条基线，只改一件事） | 修前(pristine) | 修后(patched) | 作者声称 | 判定 |
|---|---|---|---|---|
| 两表都 COMPLETE | eligible=30, gate **PASS** | eligible=30, gate **PASS** | PASS 30→PASS 30 | 一致 |
| **l.INCOMPLETE / o.COMPLETE** | eligible=30, gate **PASS** ← 错 | eligible=**0**, gate **FAIL** (`COST_AUTHORITY_INCOMPLETE`×30) | PASS 30→FAIL 0 | 一致，**真翻转** |
| l.COMPLETE / o.INCOMPLETE | eligible=0, gate **FAIL** | eligible=0, gate **FAIL** (`COST_SNAPSHOT_INCOMPLETE`×30) | FAIL 0→FAIL 0 | 一致，未反向放宽 |
| 30 条净收益全未知 | eligible=30, gate **PASS** ← 错 | eligible=**0**, gate **FAIL** (`NET_REALIZED_UNKNOWN`×30) | PASS 30→FAIL 0 | 一致，**真翻转** |
| 29 真 + 1 未知 | eligible=30, gate **PASS** ← 错 | eligible=**29**, gate **FAIL** (`NET_REALIZED_UNKNOWN`×1) | PASS 30→FAIL 29 | 一致，**真翻转** |
| 合法零收益 net==0.0 | eligible=30, gate **PASS** | eligible=30, gate **PASS** | PASS 30→PASS 30 | 一致 |

补充：修后 `l.INCOMPLETE/o.COMPLETE` 场景 `cost_status_snapshot_mismatches`=30、`snapshot_more_optimistic`=true；`l.COMPLETE/o.INCOMPLETE` 场景 mismatch=30（snapshot_more_optimistic=false）。修前无该字段。

**结论：修前确错、修后确对，两条 fail-open 均被推翻并修复。**

**权威方向独立核实（不是照抄作者声明）**：我读了生产写方——
- `src/exchange/trade_reconciliation.py:608-625`：`cost_complete` 要求 `entry_fee/exit_fee/funding` 均非空，然后 `UPDATE position_lifecycles SET ... cost_status=?`；**是唯一把 lifecycle.cost_status 写成 COMPLETE 的地方**（全仓 `UPDATE position_lifecycles` 仅此一处 + `database.py` 的通用 update）。
- `src/exchange/binance_futures.py:2652-2658` 与 `:3118-3124`：拿到开仓手续费后调 `update_position_lifecycle(..., cost_status="INCOMPLETE")`，**只降级 lifecycle、不碰 outcome**。
- `log_trade_outcome`（`database.py:~2082`）用 `INSERT OR IGNORE`，且其 `cost_status` 取自 `lifecycle_result.cost_status`（对同一笔的历史快照）。
⇒ `position_lifecycles` 是可变权威态、`trade_outcomes` 是首写即冻结快照；且**生产上确实会出现 `l.INCOMPLETE / o.COMPLETE`**（降级路径），旧口径在此 fail-open。修复方向正确。

## 3. 第 2 条：合格集合差（单调收紧）

枚举器 `enum_q.py` 在两棵树上分别求 eligible，`diff_q.py` 做集合差（用 **pristine 真旧码** `_eligible_lifecycle` vs **patched 真新码** `lifecycle_authority_reason`+net 门槛）。

- 主网格 {COMPLETE, INCOMPLETE, UNKNOWN, 缺失} × {同} × {None, 0.0, 1.5} = **48 样本**：
  - **旧不合格但新合格（放宽）= 0** ✅（硬要求满足）
  - 旧合格但新不合格（收紧）= **10**：1 条净收益未知（`('COMPLETE','COMPLETE','None')`，原因=NET_REALIZED_UNKNOWN）+ 9 条权威态成本非 COMPLETE（`COST_AUTHORITY_INCOMPLETE`）。
  - 校验：patched 内嵌 `_old_manual` 与 pristine 真码不一致数 = **0**（旧集合取样忠实）。
- 额外网格 {ATTRIBUTED, UNKNOWN, 缺失}² × {CLOSED, OPEN}² = **36 样本**：
  - 放宽 = **0**；收紧 = **5**（`ATTRIBUTION_INCOMPLETE` / `CLOSED_EVIDENCE_INCOMPLETE`）。
- **边角（不在规定网格内，但对抗性必报）**：枚举值带前后空白时出现"旧不合格→新合格"（`ws_test.py`）：
  - `o.cost_status=" COMPLETE "`：old=False → new=True
  - `o.attribution_status=" ATTRIBUTED "`：old=False → new=True
  - `l.cost_status=" COMPLETE "`：old=True → new=True（无变化）
  根因：新 `_status_of` 用 `.strip().upper()`，旧 `_eligible_lifecycle` 仅 `.upper()`。⇒ 严格意义上"放宽样本数"在**空白边角**为 3（非 0）。可达性=**不可达**（全仓无写方产出带空白枚举值，§1 Note 3），且只接受"带空白的肯定值"，不会让 INCOMPLETE 混入。**判非阻塞**，但如实纠正"0 放宽"的绝对表述（在**规范枚举值域内**放宽=0）。

**披露字段不参与放行**（`probe2_q.py` 场景 A）：构造两组都非 COMPLETE 的 30 条——
- A1 `l=UNKNOWN,o=UNKNOWN`：mismatch_n=**0**，eligible=0，gate FAIL，原因 `COST_AUTHORITY_INCOMPLETE`×30；
- A2 `l=UNKNOWN,o=INCOMPLETE`：mismatch_n=**30**，eligible=0，gate FAIL，原因**同为** `COST_AUTHORITY_INCOMPLETE`×30。
→ `cost_status_snapshot_mismatches` / `snapshot_more_optimistic` 只披露，mismatch 有无**不改变** GO/NO_GO。`NET_REALIZED_UNKNOWN` 是排除机制本身（非纯披露），其参与由第 3 条单独验证。

## 4. 第 3 条：零 vs 未知

函数级（`zero_vs_unknown.py`，exit=0）+ 端到端（`probe2_q.py` 场景 B，落库后再出报告）：

| 输入（funding/ 净收益维度） | `_net_realized` | 计入 30？ | 排除码 |
|---|---|---|---|
| `0.0` | `0.0` | **计入**（e2e eligible=30, gate PASS） | — |
| `0`（int） | `0.0` | **计入** | — |
| `Decimal("0")` | `0.0` | **计入** | — |
| `-0.0` | `0.0` | **计入** | — |
| `None` | `None` | 不计入（e2e eligible=0, gate FAIL） | `NET_REALIZED_UNKNOWN` |
| `""`（空串） | `None` | 不计入（e2e eligible=0） | `NET_REALIZED_UNKNOWN` |
| 缺失键 | `None` | 不计入 | `NET_REALIZED_UNKNOWN` |
| `float("nan")` | `None` | 不计入（e2e eligible=0） | `NET_REALIZED_UNKNOWN` |
| `float("inf")`（附加） | `None` | 不计入 | — |

**四种"零的写法"全部照常计入；四种"未知"全部不计入且原因码为 `NET_REALIZED_UNKNOWN`；`nan` 未被当合法值放行。** 根因在 `_as_float` 用 `math.isfinite` 把 nan/inf 归为 None，且 `_net_realized` 任一成本项缺失即返回 None。**未发现阻塞项。**

## 5. 第 4 条：测试改动裁决 + 反向测试

### 5.1 逐 hunk：有无删/放宽既有断言？

`git diff 4de8666..b03bee4 -- tests/acceptance/test_release_evidence_v2.py scripts/smoke_u09_release_evidence.py`：

- 被删除的行**共 4 行**，全部是把夹具里的 `cost_status=cost_status` 换成 `cost_status=authority_cost`（`create_position_lifecycle`/`update_position_lifecycle` 各 2 处）；**没有任何 `check(` / `assert` 行被删**（程序化核验：删除行中匹配 `check(`/`assert ` 的数量 = 0）。
- 新增：夹具**可选**参数 `lifecycle_cost_status: str | None = None`，实现为 `authority_cost = cost_status if lifecycle_cost_status is None else lifecycle_cost_status` ⇒ **默认路径与旧行为逐字相同**（默认时 authority_cost 恒等于 cost_status）。
- 新增断言：smoke `[4b]` 7 条 / acceptance `§4b` 8 条，含三处**反向证据**（两表都 COMPLETE、快照悲观、合法零收益）。另有一处 FAIL 详情由"逐条打印"改为"计数打印"（`b03bee4`），断言语义未变（条件仍为 `all(net_realized is not None)`）。

### 5.2 反向测试（新测试 × 旧实现）

- **T1 = 新测试文件扣在 `4de8666`（U09 base，无 FU1）代码上**（`_scratch_Q/t1_base`）：
  - smoke：**exit=1，63 通过 / 4 失败**（4 条失败全部是 [4b] 的 U09-FU1 新断言）。
  - acceptance：**exit=1，82 通过 / 4 失败**（同样 4 条 §4b 断言）。
  - → 与作者声称的"smoke exit=1/4 FAIL、acceptance exit=1/4 FAIL"**完全一致**。新断言确实在检验新口径（旧实现上必红）⇒ **不存在"新测试在旧实现上仍绿"的阻塞项**。
  - 判定依据：`APPLIED = bool(getattr(evidence,"U09_EVIDENCE_VERSION",""))` 是 **U09 标记**（base 已置位），故 T1 走 `if APPLIED:` 分支断言修后口径 → 在旧实现上红。

### 5.3 新测试 × U09 之前实现（`448a4e9`）

- **T2 = 新测试扣在 `448a4e9` 代码上**（`_scratch_Q/t2_preu09`）：acceptance **exit=0，31 通过 / 0 失败**。
- [4b] 段落**主动断言缺陷存在**（非 SKIP）：
  - `PASS 基线态：lifecycle 的 INCOMPLETE 被无视，快照 COMPLETE 即放行（缺陷证据）`
  - `PASS 基线态：净收益未知的样本仍被计入 30（缺陷证据）`
- → 与作者声称"31/0 exit=0 且主动断言缺陷存在（eligible==30）非 SKIP"**一致**。

### 5.4 只换实现不换测试（作者 60/0、78/0 声称）

- **T5 = 旧测试文件扣在 `b03bee4` 新实现上**（`_scratch_Q/t5_oldtests_newcode`）：smoke **60/0 exit=0**、acceptance **78/0 exit=0**。
- 计数闭环：pristine（旧测试×旧实现）= smoke 60/0、acc 78/0；combined（新测试×新实现）= smoke **67/0**、acc **86/0**；新测试×旧实现 = 67 中 4 红、86 中 4 红。
- → **实现改动本身未使任何既有断言变红**，故不存在"为变绿而删/放宽既有断言"的必要。

### 5.5 其它观察（非阻塞）

- `_eligible_lifecycle` 在 patched 树里**已成为死代码**（仅定义、无调用点；主流程直接调 `lifecycle_authority_reason`）。保留它不影响正确性，但读者可能误以为它仍是判据入口。
- 修复 diff 的 `row["cost_recomputed"] = True` 是**在净收益未知样本已被 `continue` 排除之后**执行，故恒为真、语义正确（旧码此处会写 False 表示"计入了但没算"，新语义下该行根本不计入）。

## 6. 第 5 条：`smoke_release_acceptance` 夹具改动裁决

### 6.1 原夹具是否真自相矛盾？—— 是

- `src/exchange/trade_reconciliation.py`（唯一成本重算写方）第 608–611 行：
  `cost_complete = (entry_fee is not None and exit_fee_value is not None and funding is not None and fee_assets_ok)`；
  `cost_status = "COMPLETE" if cost_complete else ...`
  ⇒ **`cost_status=COMPLETE` 的必要条件之一就是 `funding != None`**（U01 不变量）。该文件另有 `UPDATE position_lifecycles SET ... cost_status=?` 落库。
- 原夹具（`scripts/smoke_release_acceptance.py` L62–81）以 `cost_status="COMPLETE"` 建/更 lifecycle 与 outcome，**却把 `funding_cashflow` 留空**（`log_trade_outcome` 未传 funding）。⇒ 声称与数据自相矛盾。
- 该矛盾**不是 DB 约束**（schema 对 `cost_status` 无 CHECK/trigger，`funding_cashflow REAL` 可空），仅由 `trade_reconciliation` 在**重算路径**上语义保证；夹具绕过重算直接写列，故能"写进"矛盾态。

### 6.2 是否等于"悄悄救活一条即将变红的既有断言"？—— 是救活，但**判非放宽**

实测（新门禁 + 两种夹具）：
- **t3 = 新门禁 + 原夹具（funding NULL）**：`scripts/smoke_release_acceptance.py` **exit=1**，`AssertionError: 完整闭合生命周期达到门槛`（L127）。即原夹具下 `eligible_closed_lifecycles==0`（应为 30）→ 会连带 `严格发布报告可GO`(L131) 等。
- **t4 = 新门禁 + patch02 夹具（`funding_cashflow=0.0`）**：**exit=0，17 通过 / 0 失败**（与作者"17/0"一致）。

裁决：
1. patch02 **不改任何产品代码**（只改 smoke 夹具），故不存在"把门禁放松"的风险；
2. 救活方式是**把夹具补成它自称的状态**（"零资金费" 0.0，与 F10"funding=0 当作 0"一致），使该样本成为**货真价实的完整闭合**，L127 断言的意图（完整闭合达门槛）与数据终于自洽；
3. 该救活**未降低覆盖**：真正的"成本不完整"反例已由新增的 [4b]/§4b（`COST_AUTHORITY_INCOMPLETE`/`COST_SNAPSHOT_INCOMPLETE`/`NET_REALIZED_UNKNOWN`）显式覆盖。
⇒ **非放宽，属夹具数据修正**；但如实标注：它确实阻止了该 smoke 在合入 FU1 后变红（与 §1 Note 1 同因）。

## 7. 第 6/7/8 条

### 7.1 第 6 条：门禁数字未动

- `DEFAULT_THRESHOLDS` 两棵树**逐字相同**：`min_directional_candidates=200`、`min_eligible_closed_trades=30`、`min_observation_days=7.0`、`min_observation_coverage=0.999`、`max_observation_gap_seconds=120.0`。
- `gates` 顶层键集合两棵树**完全相同**：`schema_version, directional_candidates, eligible_closed_trades, observation_coverage, observation_days, critical_open_incidents, unknown_orders, submitting_orders`（8 个，无增无减）。
- `release_gate_role` / `SOURCE_A` 在 diff 中出现次数 = **0**（未改一字）；`directional_candidates` 门禁块 diff = **0**（未改）。
- `_gate` 新增的 kwargs 走 `**extra`（仅进 detail），不影响 `state`/`passed`。

### 7.2 第 7 条：回归

**全量 `scripts/run_offline_regression.py`（94 脚本）两态逐脚本 rc**（`driver_q.py`，不因首个失败中止）：

| 树 | rc≠0 集合 |
|---|---|
| pristine（4de8666） | `{scripts/smoke_u10a_release_package.py}` |
| combined（b03bee4+patch02） | `{scripts/smoke_u10a_release_package.py}` |

- **NEW_FAIL = ∅**；**FIXED = ∅**（两态失败集合相同）。
- 唯一失败脚本 `smoke_u10a_release_package.py` 与 FU1 无关（FU1 只碰 3 个文件）：判定为**沙箱环境差异**——它在**未 commit 的 `git init` 树**里直接崩溃（内层 `build_release_manifest` 需 git HEAD）；在**已 commit 的沙箱**里则报 2 条"发布包文件完备性"断言失败，两态**断言相同**（仅"应有 N 个"因 `__pycache__` 噪声 286 vs 284 不同）。作者证据中的 `OFFLINE REGRESSION OK scripts=94` 来自其真实工作区，**我的沙箱无法复现该脚本通过**（见 §8）。
- "内部 FAIL 行"共 **10** 条，全部来自 `scripts/smoke_u02_strategy_position_manager.py`（该脚本 rc=0，内部 harness 打印 FAIL）；两态均为 10 条 → 预存在、非 FU1。与作者 delta（10 vs 10）一致。
- 附注：branch 头（`patched`，**不含 patch02**）单跑全量回归会在第 42 个脚本 `smoke_release_acceptance.py` 处 rc=1 中断（§1 Note 1）。

**其余指定检查（两态各跑）**：

| 检查 | pristine | combined/patched |
|---|---|---|
| `scripts/check_quality.py` | exit=0, `QUALITY CHECK OK files=303` | exit=0, `QUALITY CHECK OK files=303` |
| `scripts/smoke_contracts_v3.py` | exit=0, PASS 173 / FAIL 0 | exit=0, PASS 173 / FAIL 0 |
| `scripts/smoke_db_migrations.py` | exit=0, 13 通过 / 0 失败 | exit=0, 13 通过 / 0 失败 |
| `tests/integration/test_shadow_labels.py` | exit=0, PASS=100 FAIL=0 | exit=0, PASS=100 FAIL=0 |

- check_quality **未新增 exclude**：`scripts/check_quality.py` 在 `4de8666..b03bee4` 内**零改动**（diff 为空），两态输出逐字一致。

### 7.3 第 8 条：补丁矩阵

`_scratch_Q/patch_matrix.txt`（沙箱 = `git archive 4de8666`+`git init`，`git apply --check -v`）：

| git 配置 | patch01 (3 文件) | patch02 (1 文件) |
|---|---|---|
| 默认 | rc=0, `Checking patch`=3, **Skipped=0**, error=0 | rc=0, `Checking`=1, **Skipped=0**, error=0 |
| `-c core.autocrlf=false -c core.eol=lf` | rc=0, Checking=3, **Skipped=0**, error=0 | rc=0, Checking=1, **Skipped=0**, error=0 |
| `-c core.autocrlf=true` | rc=0, Checking=3, **Skipped=0**, error=0 | rc=0, Checking=1, **Skipped=0**, error=0 |

- 另核：`01_u09fu1_release_evidence.diff` 与 `git diff 4de8666..b03bee4`（全量 3 文件）**逐字节相同**；`git apply` 后 `release_evidence.py` 与 `b03bee4` blob **内容一致**（忽略 Windows CRLF 差异）。
- 附注：`git apply` 写入时因本机 git 默认（autocrlf 相关）产生 CRLF，仅为写入端行尾差异，`--check` 三配置均干净、内容一致。

## 8. 未能验证的部分

1. **`scripts/smoke_u10a_release_package.py` 在"真实作者工作区"下是否通过**：我的两棵树都是 `git archive` + `git init` 沙箱，该脚本依赖 git HEAD 与仓库完整跟踪文件集，在我的沙箱内必崩/必报 2 条完备性断言。故我只能确认它**在两态下同样失败**（NEW_FAIL=∅），**无法独立复现作者"scripts=94 全 OK"中的这一条**。
2. **真实生产数据上 `position_lifecycles.cost_status` 的分布**：我只做了代码路径推演（`create_position_lifecycle` 默认 `UNKNOWN`；仅 `trade_reconciliation` 与 `binance_futures` 的降级路径写该列）。无法评估"新口径在真实库上会不会把 eligible 从 30+ 打到 <30"这一**运营影响**（属策略面，非门禁正确性）。
3. **`docs/dispatch/patches/.../evidence/fu1_probe.py.txt` 与作者其它 evidence 的逐字复算**：按任务要求，未复用作者探针；作者 evidence 内容仅用于对照声称值，未逐字校验其输出文件。
4. **`.strip()` 边角的真实可达性**：我判定"不可达"基于全仓写方静态检索；未做运行期污点追踪（成本高、收益低）。
5. **未运行真实 demo 服务 / 未联网 / 未触碰真实库**（硬约束）；因此所有结论限于离线合成夹具。

## 9. 报告路径

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U09-FU1\R1\q\_scratch_Q\Q_U09-FU1_r1.md`
