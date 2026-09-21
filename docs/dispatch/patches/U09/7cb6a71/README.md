# U09 · 发布证据与事件恢复门禁 —— 交付与补丁包

- 作者分支：`task/U09`，工作树 `mat-wt/W3`，base = 集成 HEAD `448a4e9`
- **代码 head（本包生成基准与最终交付 head）：`7cb6a71`**
  （本 `docs/` 包落盘后 head 再前进一步；4 个共享补丁的内容与 `7cb6a71` 工作树逐字节一致，已复核 `SAME`）
- 共享适配补丁的**目标树**：`448a4e9`（= 本工作树 base = 集成 HEAD），直接基于它出 diff

## 1. 交付物

| 类别 | 路径 | 归属 |
|---|---|---|
| 扩展的证据计算器 | `src/core/release_evidence.py`（+936/−… 行，538 → 1483 行） | U09 独占（已在 `task/U09`） |
| 验收套件（两态可跑） | `tests/acceptance/test_release_evidence_v2.py` | U09 独占（已在 `task/U09`） |
| 离线冒烟（登记进统一回归） | `scripts/smoke_u09_release_evidence.py` | U09 独占（已在 `task/U09`） |
| 共享适配补丁 | 本目录 `01..04*.diff` | **只交 diff，由 I 合入** |
| 证据脚本 | `evidence/apply_database_patch.py`、`evidence/probe_cli_report.py` | 本目录 |

### 补丁清单与建议应用顺序

| # | 目标共享文件 | 变更 | 规模 |
|---|---|---|---|
| 01 | `src/audit/database.py` | U09 接线标记 + 影子来源**存储别名归一**（`A_strategy`→`A_current_baseline` 等），并在 `log_shadow_decision` 写入时归一 | +28 −1 |
| 02 | `src/core/health_watch.py` | U09 接线标记 + 两个**只读探针**：`release_observation_window()`（窗口事实/session/config_hash/首尾断档/外部发布混样）与 `alert_receipt_truth()`（只认 `delivered_at_ms`+`device_receipt` 的送达真相；ACK≠送达） | +229 |
| 03 | `scripts/report_release_readiness.py` | `--report-at`/`--declaration`/`--strict-evidence`；K06 五组与阻断项的人读摘要写 stderr | +50 −3 |
| 04 | `scripts/run_offline_regression.py` | 把 `scripts/smoke_u09_release_evidence.py` 登记进 `SCRIPTS`（**必须与冒烟脚本同批**，否则 `_validate_smoke_manifest()` 直接 RuntimeError） | +3 |

01/02 相互独立；03 依赖 01+02 已应用（否则 CLI 的 `--report-at` 仍可用，探针块降级为
`NO_DATA`）；04 只依赖冒烟脚本文件存在。建议顺序 `04 → 01 → 02 → 03`（或任意顺序）。

### 应用方式与 `git apply --check` 结果（3 种 git 配置）

工作树字节 = 目标 blob 字节（**该文件是混合行尾**：`src/audit/database.py` 722/2800 行
CRLF，其余 LF；`--ignore-cr-at-eol` 在集成机 git 上不存在，故不使用）。

实测矩阵（3 种**工作树形态** × 3 种**git 配置** × 有无 `--ignore-whitespace`，每格 4 个补丁）：

| 工作树形态 | 默认（仓库自带 `core.autocrlf=false`） | `-c core.autocrlf=false -c core.eol=lf` | `-c core.autocrlf=true` |
|---|---|---|---|
| **W1 取自 blob**（`git archive \| tar -x`；`git worktree add` + `core.autocrlf=false`） | **4/4**（0 `Skipped`） | **4/4** | **4/4** |
| **W2 本机 system `core.autocrlf=true` 的 `git clone`**（`health_watch`/`report`/`run_offline_regression` 变 CRLF；`database.py` 因**混合行尾被 git 判为不可安全转换**而保持与 blob 同字节） | **4/4** | 1/4（仅 02/03/04 失败）→ 加 `--ignore-whitespace` 后 **4/4** | **4/4** |
| **W3 全 LF 检出**（`core.autocrlf=false,core.eol=lf`） | **4/4** | **4/4** | **4/4** |

结论：**3 种 git 配置下 4/4 干净应用**（`Checking patch`、0 `Skipped`）在 W1（集成工作区
现状：仓库自带 `core.autocrlf=false`、工作树取自 blob）与 W3 上全部成立，在 W2 上也成立
（除"配置声明不转换/用 LF，而工作树实际是 CRLF"这一自相矛盾组合）。

> 唯一失败的组合是"工作树被检出为 CRLF，却用 `core.autocrlf=false -c core.eol=lf`
> 声明按 LF 处理"；此组合下**任何**补丁都无法匹配（U05 `65651df` 登记过同一现象）。
> 保险起见，集成时统一用：
> `git apply --ignore-whitespace -p1 <diff>` —— 在上述 9 个格子里均实测通过。

复现命令（独立 repo 沙箱，非仓库子目录）：

```bash
git archive 448a4e9 | tar -x -C <sandbox> && cd <sandbox> && git init -q . && git add -A && git commit -qm base
for f in 01..04.diff; do git apply --check --verbose "$f"; done   # 3 种配置各跑一遍
```

## 2. 【最高优先】S08-SHADOW-GATE-LEAK 修复前后对照

**缺陷**：`release_evidence.py` 的候选查询取了 `source` 列但**没按它过滤**，分母是
"全部 source 去重后的机会键"；B/C（`research_only`）研究影子因此冒充执行候选。

**修法**：分母只计入 `intel.shadow_status.release_gate_role(source) ==
GATE_ROLE_EXECUTION_DECISION`（即 `SOURCE_A = "A_current_baseline"`）的行，
`release_evidence.py` **不持有**第二份 source 名单（`grep GATE_ROLE_EXECUTION_DECISION`
在本文件只出现于"向 U06a 取口径"的调用点）。

| 场景（同一夹具：200 条 A 明确方向样本 + 50 条 B + 50 条 C，机会键互不相同） | 未修 | 修后 |
|---|---|---|
| `directional_candidates`（A only） | 200 | 200 |
| `directional_candidates`（A+B+C） | **300** | **200** |
| 差额 Δ | **+100** | **0** |
| `shadow_rows`（诊断，含全部来源） | 300 | 300 |
| `shadow_source_rows`（诊断） | A=200,B=50,C=50 | A=200,B=50,C=50（**只披露不计分**） |

- 可撤销、可重算：把 B/C 行删掉再算，分母不变（同为 200）；反之加入 100 条 B/C，分母
  也不变（Δ=0）。证据：`scripts/smoke_u09_release_evidence.py` 的
  `补丁态：口径可撤销、可重算（去掉 B/C 前后分母同为 200）`（PASS）。
- 使用的常量/函数（全部来自 `src/intel/shadow_status.py`，U06a 唯一权威口径）：
  `GATE_ROLE_EXECUTION_DECISION`、`release_gate_role()`、`COHORTS`、`SOURCE_A`、
  `DIRECTIONAL_SIGNALS`、`is_directional()`、`research_sources()`。
- 写入侧（补丁 01）：历史别名 `A_strategy` → `A_current_baseline` 在
  `log_shadow_decision` 落库时归一，别名目标由验收套件断言**逐字等于**
  `shadow_status.SOURCE_*`（改名即红）；未被归一的未知来源一律按
  `research_only` 处理（fail-closed），不会顶替分母、也不会把真实执行候选漏出。

## 3. 必达要点逐条证据

命令前缀（下文省略）：`PY=.../venv/Scripts/python.exe`，`FULL=<patched sandbox tree>`，
`AC=<$PY tests/acceptance/test_release_evidence_v2.py>`，
`SM=<$PY scripts/smoke_u09_release_evidence.py>`。

| # | 要点 | 命令 | 输出摘要 | exit |
|---|---|---|---|---|
| 1 | 来源口径（S08） | `cd $FULL && $SM` | `directional_candidates=200 shadow_rows=300`；`补丁态：分母只计 A 组（200），B/C 不再抬升（300 → 200）` PASS | 0 |
| 1' | 口径来自 U06a | `$AC` | `补丁态：来源口径来自 intel.shadow_status（不是第二份 source 名单）` PASS | 0 |
| 1'' | 别名归一（DB 补丁） | `$AC` | `数据库补丁：A_strategy 被归一到 U06a 规范来源名` / `别名目标逐字等于 shadow_status 常量` PASS | 0 |
| 2 | 纯 NO_EDGE 不计分母 | `$SM`/`$AC` | A=200 起，加入 120 条 `{"no_edge": true}` + 120 条 `{"reason_code":"NO_EDGE"}` + 240 条 neutral/unknown → 分母**仍 200**；`no_edge_declared=240, non_directional_signal=120` | 0 |
| 3 | 窗口首尾缺失计入分母 | `$AC` §3 | `expected=31 observed=26 head_missing=3 tail_missing=2`，`observation_coverage=FAIL`；`observation_days` 按声明窗口 0.0208 天（不是 observed span） | 0 |
| 3' | 禁止自动截断 | `$AC` §3 | 未声明窗口 → `NO_DATA + WINDOW_MISSING`，且方向候选/闭合交易 `state=NO_DATA, actual=None`（不为 0 冒充） | 0 |
| 4 | lifecycle 去重 + 迟到成本重算 | `$AC` §4 | 30 笔仍 30（不是 31）；`lc-0` 净收益 0.85 → 0.55（`exit_fee 0.1→0.4`），`revised_lifecycles=[lc-0]`，`evidence_revision_hash` 变化 | 0 |
| 4' | 冲突隔离 / 未知仍未知 | `$AC` §4 | 同一 `recorded_at` 内容冲突 → `REVISION_CONFLICT`，冲突生命周期**不计入 30**（29 笔 + 门禁 FAIL）；`cost_status!=COMPLETE` 不计入且净收益不谎报 0 | 0 |
| 5 | ACK ≠ 恢复 | `$AC` §5 | `delivered_confirmed=1`，`ack_without_delivery=[ack-only-1]`，`provider_accepted_only=1`，状态 `FAIL`，原因码 `{MANUAL_ACK_ONLY, NO_CONFIRMED_DELIVERY}`（**直接复用 U14 常量**）；incident `acked_not_recovered_count=1` + `ACK_WITHOUT_RECOVERY`；补上真实回执后才变成 2/0 | 0 |
| 6 | 重启 ≠ 换版 | `$AC` §6 | 2 session + 单一 `config_hash` → `RESTART_SAME_ARTIFACT`、`restart_count=1`、`restart_is_version_change=False`、`state=PASS`；出现第二个 `config_hash` → `CONFIG_CHANGED_WITHIN_WINDOW` + `MIXED_CONFIG_SAMPLES` + `FAIL`；窗口内其它 `release_id` 的观察 → `MIXED_RELEASE_SAMPLES` | 0 |
| 6' | 首尾断档（观察者未写入） | `$AC`/`$SM` | `head_gap_minutes=3, tail_gap_minutes=2`（与"有行但不健康"分开命名，未知则 `null`） | 0 |
| 7 | 不建第二套 GO 判断器 | `$AC` §7 | `gates` 键集合与升级前**完全一致**；`release_readiness == all(gates)`；`k06` 固定键集合且组状态只取 `PASS/FAIL/NO_DATA`，无整体 GO 字段 | 0 |
| 8 | 缺证据/声明 PASS/畸形窗口 | `$AC` §3b §7 | 畸形窗口 7 个取值（`"not-a-date"`/`inf`/`nan`/`"1e400"`/`2**64`/`2**64+1`/`"99999999999999999999"`/`True`）与反向窗口 → `FAIL + WINDOW_INVALID` 且**不崩溃**；声明 PASS 无窗口 → `FAIL + PASS_WITHOUT_EVIDENCE + WINDOW_MISSING`；缺工件身份 → `IDENTITY_REF_MISSING`；无证据组 → `NO_DATA` + 阻断项（`NO_DATA != PASS`） | 0 |
| — | CLI 端到端 | `$FULL && $PY docs/.../probe_cli_report.py` | 窗口声明：`rc=0`，`directional_candidates=200 shadow_rows=250`，`blocking=3`；`--strict`/`--strict-evidence` → `rc=1`；未声明窗口 → `blocking=6 window=['WINDOW_MISSING']`；畸形窗口 `rc=0 window=['WINDOW_INVALID']`（不崩溃） | 0/1 |
| — | 只读性 | `$SM` §8 / `$AC` §1 | 报告前后 SQLite 文件字节一致 | 0 |

> 注：CLI 探针的 `release_readiness=NO_GO` 是**预期**的——合成夹具只有 30 分钟观察窗口，
> 远达不到既有 `min_observation_days=7` 门槛；此处要验的是"窗口/来源/阻断项"通道，不是
> 让合成夹具 GO。

## 4. 故障用例（F10/F12/F16/F23/F24）

| ID | 反例 | 本任务的验证 | 结果 |
|---|---|---|---|
| **F16** | B 影子样本冒充 AI 候选 | A-only vs A+B+C 两次快照对比；删除 B/C 后分母不变 | 分母**不膨胀**（300 → 200）；B/C 只在诊断字段；判据可撤销、可重算 |
| **F24** | 窗口首尾断档、不同 RC/配置混样、无确认事件被排除、纯 NO_EDGE 凑 200 | §3/§6/§2b | 首尾断档计入分母（expected=31）；`MIXED_CONFIG_SAMPLES`/`MIXED_RELEASE_SAMPLES` 阻断；无来源/无方向/NO_EDGE 样本不计分；**拒绝统计伪通过**（冲突样本 29 笔 → 门禁 FAIL） |
| **F23** | 满页中插入迟到记录、重复 ID 不同内容、已 COMPLETE 后费用修订 | §4 | 覆盖不虚报（`current_closed` 如实、eligible 去重）；冲突隔离（`REVISION_CONFLICT`，不进 30）；净收益与发布证据**可修订**（最新修订重算 + 修订 hash 变化 + 被取代 outcome 留痕） |
| **F10** | 收入重复/同毫秒满页/迟到/双向仓/零资金费/非 USDT 费用 | §4（发布证据侧） | 同毫秒 30 笔互不吞并；重复 `outcome_id` 幂等（不双计）；双向仓 LONG/SHORT/BOTH 各计一次；`funding=0` 当作 0（不是缺证据）；缺成本项 → 净收益 `None` 且 `cost_status!=COMPLETE` 不进 30（**未知仍未知**） |
| **F12** | 告警"供应商接受但不送达" | §5 | 按**真实回执**展示：`delivered_at_ms + device_receipt` 才算送达；`provider_receipt`/`ACCEPTED` 只算"上游接受"；人工 ACK 单独列 `ack_without_delivery`，**不当作送达/恢复**；补上回执后才转为送达 |

## 5. 两态结果（不假定补丁是否已应用）

| 态 | 树 | 验收套件 | 冒烟 |
|---|---|---|---|
| A | `448a4e9` 纯基线 + 仅验收套件（无任何 U09 产物） | **26 通过 / 0 失败**（逐条断言基线缺陷确实存在：S08 300、NO_EDGE 计入、窗口自动截断、无修订/冲突披露、无 event_recovery） | 不适用（冒烟需独占产物） |
| B | `28ad90f`（独占产物）+ **无共享补丁** | **65 通过 / 0 失败**（v2 口径生效；依赖共享补丁的证据块如实 `NO_DATA + U09_SHARED_PATCH_MISSING`） | **9 通过 / 0 失败**（`PATCHED=True, SHARED=False`） |
| C | `28ad90f` + 01..04 全部应用 | **78 通过 / 0 失败** | **60 通过 / 0 失败**（`PATCHED=True, SHARED=True`） |

态 A/B/C 的期望**都是真的**，没有 SKIP 掩盖；不硬编码迁移版本集合（断言改为"版本集合
是 1..max 的连续区间且 max == `current_schema_version()`"这类相对性质），并含
`ruff check`（见 §7）。

## 6. 沙箱 delta 表（全量离线回归）

沙箱：`git archive <commit> | tar -x -C <自己的临时目录>`（独立 repo，`git init` 后
`test_secret_scanner.py` 才能跑通），不进入任何他人工作区。

| 运行 | 树 | 登记脚本数 | 结果 | `OFFLINE REGRESSION` rc | 内部 `FAIL` 行 |
|---|---|---|---|---|---|
| **pristine** | `448a4e9` | 84 | `OFFLINE REGRESSION OK scripts=84` | 0 | 10（全部来自 `smoke_u02_strategy_position_manager.py`，见下） |
| patched@29d7d79 | `29d7d79` + 01..04 | 85 | `OFFLINE REGRESSION OK scripts=85` | 0 | 10（同集合） |
| patched@28ad90f | `28ad90f` + 01..04 | 85 | `OFFLINE REGRESSION OK scripts=85` | 0 | 10（同集合） |
| **patched@7cb6a71（最终冻结复跑）** | `7cb6a71` + 01..04 | 85（+`smoke_u09_release_evidence.py`） | `OFFLINE REGRESSION OK scripts=85`，含 `U09 冒烟: 60 通过 / 0 失败` | **0** | 10（同集合） |
| **NEW_FAIL（我引入的新失败）** | — | — | — | — | **∅** |

- 断言方式：把两次运行的 `  FAIL  <名称>` 行**逐行排序对比**，
  `diff pristine.fails final2.fails` 为空 → 新失败集合 = ∅。
- 那 10 行 `FAIL` 是 `scripts/smoke_u02_strategy_position_manager.py` **自己打印的内部断言**
  （涉及 U15 的 `account_ref`/幂等键期望），该脚本仍以 rc=0 结束，且**在 pristine 与 patched
  两次运行里逐字相同**——属既有状态，不是我引入的失败；按"如实登记"原则原样保留在此。
- 受影响的既有共享冒烟（在 patched 沙箱逐个跑，rc 全 0）：
  `smoke_contracts_v3`、`smoke_release_acceptance`、`smoke_db_migrations`、
  `smoke_health_watch`、`smoke_release_observation_freshness`、`smoke_u14_alert_delivery`、
  `smoke_u06a_shadow_isolation`、`smoke_u10a_release_package`。

## 7. 复现命令

```bash
PY=<venv>/Scripts/python.exe; export PYTHONUTF8=1
cd <worktree|sandbox>
$PY tests/acceptance/test_release_evidence_v2.py     # 78/0（补丁态）
$PY scripts/smoke_u09_release_evidence.py            # 60/0（补丁态）
$PY docs/dispatch/patches/U09/evidence/probe_cli_report.py
$PY -m ruff check src/core/release_evidence.py scripts/smoke_u09_release_evidence.py \
    tests/acceptance/test_release_evidence_v2.py     # All checks passed!
$PY scripts/run_offline_regression.py                # OFFLINE REGRESSION OK scripts=85
```

## 8. 未尽事项 / 无法离线验证的部分（如实登记，未隐去）

1. **`--ignore-whitespace` 补救**：若集成时工作树被整体重写成单一 CRLF/LF 行尾，
   补丁 01 需要 `--ignore-whitespace` 才能应用（见 §1 场景 4/5）。集成工作区现状
   （`core.autocrlf=false` + 取自 blob 的混合行尾）不需要该补救。
2. **窗口声明尚无生产写入方**：网窗口只能来自"调用方 `since/until`"或"清单声明"
   （`observation_window_start/release_started_at` 等键）。`src/core/service.py`
   （生产写入路径）不在 U09 集成目标内，因此**跨发布窗口的自动声明未落地**：真实
   报告若两者都不给，只会得到 `NO_DATA + WINDOW_MISSING`（fail-closed，不猜）。
   建议 U12/U15 在发布清单里写入该键，或另开任务在 service 侧持久化发布起始时刻。
3. **未做真实故障注入**：全部为离线合成夹具；未在运行中的 demo 服务上注入
   窗口断档/迟到修订/告警不送达（运行目录只读、禁真告警）。U12 的部署验收需要
   在真实宿主上复核"投递独立 + 未确认不假送达"。
4. **`event_recovery` 的告警来源是 `alert_deliveries` 审计镜像**（U14 表），不是投递
   队列本身（队列独立于交易库）。因此"镜像没写"与"投递没问题"仍然无法离线区分：
   镜像为空时本任务一律 `NO_DATA`（不冒充通过，也不冒充失败）。
5. **`risk_incidents` 的 ACK/恢复口径**依赖表内 `acknowledged_at`/`resolved_at`；
   若历史库缺列，探针按 `NULL` 处理（等于"没有 ACK 证据"），不会猜。
6. **K06 组的"声明状态"由调用方传入**（`declaration=`）：本任务只保证"声明 PASS 必须
   自证"的判据走冻结契约；**谁有权声明 PASS** 属 U12 的发布审查流程，未在本任务定义。
7. **`runtime_identity.py:2283` 登记的 fu-3**（`release_observations` 的 `release_id`
   绑定是否足以在身份不符时拒绝开仓）本任务只覆盖"报告期一致性 + 身份不完整即阻断"，
   **启动期门禁**仍归 U12/U13。
8. **窗口不成立时不给出"未加窗口的分母"**：`resolve_release_window` 失败时，
   `release_evidence.directional_candidates` / `eligible_closed_lifecycles` 等**分母字段**
   一律 `None`/`[]`（并给 `window_applied=false` + `window_reason_codes`），只保留
   `shadow_rows` 这类原始行数诊断——避免把"没有窗口的全库行数"读成本发布的候选。
9. **NO_EDGE 判定只认显式标记**（`no_edge`/`rejected` 为真、原因码 `NO_EDGE`、
   `action` 取 skip/hold/none/reject）。`feature_json` 里没有这些标记、却语义上"无边际"
   的行不会被排除；相反，`new_entry_allowed=false` 属"看见了但未开仓"，**不**计入排除
   （另以 `blocked_entry_rows` 诊断披露）。这条边界是有意选择，若 U12/Q 认为应把
   "被门禁拦下的候选"也排除出分母，需要一次口径显式变更（并同步更新 200 分母定义）。
