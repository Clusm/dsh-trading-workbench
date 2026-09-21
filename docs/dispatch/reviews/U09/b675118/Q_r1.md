# Q · U09 独立验收报告 (R1)

- 验收工作树: `mat-wt/reviews/U09/R1/q`，detached HEAD @ `b675118`
- 作者代码 head: `7cb6a71`（`b675118` = `7cb6a71` + 补丁目录改名 + README，已复核 R100）
- base / pristine: `448a4e9`
- 解释器: `multi-agent-trader/.venv/Scripts/python.exe`（无 pytest，全部脚本直跑）
- 我的树（全部自建，`git archive | tar -x` 后**先 git init**再跑）:
  - `_scratch_Q/trees/pristine` = `448a4e9`（600 文件，`git ls-files`=600 == `git ls-tree`=600 ✓）
  - `_scratch_Q/trees/stateA` = pristine + 仅 `tests/acceptance/test_release_evidence_v2.py`
  - `_scratch_Q/trees/stateB` = `b675118` 独占产物，无共享补丁（609 文件）
  - `_scratch_Q/trees/stateC` = stateB + 4 个共享补丁（默认配置全部 `Applied cleanly`）

状态：**完成**。结论 **PASS**（无阻塞项）。

---

## 0. 三态复现（验收套件 + 冒烟）

命令：`cd <tree> && PYTHONUTF8=1 $PY <script>`，输出见 `_scratch_Q/r{A,B,C}_*.txt`。

| 态 | 验收套件 | exit | 冒烟 | exit | 作者自报 | 一致? |
|---|---|---|---|---|---|---|
| **A** | **26 通过 / 0 失败** | 0 | n/a | — | 26/0 | ✓ |
| **B** | **65 通过 / 0 失败** | 0 | **9 通过 / 0 失败** (`PATCHED=True, SHARED=False`) | 0 | 65/0, 9/0 | ✓ |
| **C** | **78 通过 / 0 失败** | 0 | **60 通过 / 0 失败** (`PATCHED=True, SHARED=True`) | 0 | 78/0, 60/0 | ✓ |

A 态证明"未修时确实坏"：`rA_acceptance.txt` 里 26 条 PASS 全为基线缺陷断言
（S08 300、NO_EDGE 计入、窗口自动截断、无修订/冲突披露、无 event_recovery）。

---

## 1【最高优先】S08 泄漏真修 —— 独立夹具（不使用作者脚本）

夹具：`_scratch_Q/probe_s08.py`（自建 `AuditDatabase` → 直插 `shadow_decisions` →
调 `build_release_readiness_report`）。200 条 A(`A_current_baseline`, bullish,
机会键 `A-opp-i`) + 50 B + 50 C（机会键互不相同）。

| 场景 | pristine `448a4e9` | patched `stateC` | 期望 | 结论 |
|---|---|---|---|---|
| A only | 200 | 200 | 200 | ✓ |
| **A+B+C** | **300** | **200** | 200 | ✓ **真修** |
| 删掉 B/C 后 | — | **200**（分母不变） | 200 | ✓ 可撤销/可重算 |
| 480 条 no_edge/neutral/unknown | 440 | **200** | 200 | ✓（`no_edge_declared=240`, `non_directional=120` 被排除） |
| 旧别名/空/未知/大小写变形 150 条 | 350 | **200**（`research_only_source:150`） | 200 | ✓ fail-closed |

- 差异来源：patched 报 `shadow_rows=300` 但 `counted_sources={A:200}`，
  `excluded_by_reason={research_only_source:100}`。
- **不是第二份名单**：`grep "A_current_baseline\|B_contract_market\|C_contract_plus_web3"`
  在 `src/core/release_evidence.py` **零命中**；来源判定只走
  `_candidate_exclusion` → `release_gate_role(source) != GATE_ROLE_EXECUTION_DECISION`
  （`release_evidence.py:554`），常量 import 自 `intel.shadow_status`（`:39-43`）。
- **反向验证（同一判定源，不是巧合）**：monkeypatch `release_gate_role` 恒返回
  `GATE_ROLE_EXECUTION_DECISION` → 同一 A+B+C 夹具分母 **200 → 300**，
  `filter_sources` 变为 `[A,B,C]`，`research_only_sources=[]`。证明分母由
  `shadow_status` 口径驱动。证据：`_scratch_Q/10_s08.txt`。

## 2 门禁未被降低

- `DEFAULT_THRESHOLDS`（`release_evidence.py:105-110`）：`min_directional_candidates=200`、
  `min_eligible_closed_trades=30`、`min_observation_days=7.0`、
  `min_observation_coverage=0.999`、`max_observation_gap_seconds=120.0` —— **五项全部不变**。
- `gates` 键集合 patched `1253-1305` vs pristine `448-489`：**逐字相同**、顺序相同
  （schema_version / directional_candidates / eligible_closed_trades / observation_coverage /
  observation_days / network_rate_limit / critical_open_incidents / unknown_orders /
  submitting_orders / entry_readiness）。证据 `_scratch_Q/11_gates_src.txt`。
- `release_readiness = "GO" if release_passed else "NO_GO"`，`release_passed = all(gates.values())`
  （`1307`,`1446`）—— 派生方式未变（仍只由既有 gates 汇总）。
- 未见新增整体 GO 字段（K06）；`k06` 块仅 `schema_version/groups/blocking_items/report_hash`。
（源码级确认。）

**运行时确认**（`probe_adv.py`，同一夹具跑 pristine 与 stateC）：
两边 `gates` 运行时键集合**逐字相同**（10 键同序）；`top_level_has_GO_field=[]`（无第二 GO 字段）；
`k06` 键只有 `{schema_version,groups,blocking_items,report_hash,note}`。证据 `_scratch_Q/19_adv.txt`、`19b_adv_pristine.txt`。

> **P3 观察（非阻塞）**：`build_release_readiness_report` 暴露 `thresholds=` 入参，传
> `{"min_directional_candidates":1,...}` 时 `required` 确实随之降到 1（实测）。但
> `DEFAULT_THRESHOLDS` 常量冻结、无配置/环境变量路径读它；全仓唯一传该参数的调用点是
> `scripts/smoke_release_acceptance.py:137`（把门槛**调高**到 201 做反例）。
> 即"**悄悄**放宽"不成立（必须显式在调用点写死），可接受。

---

## 4 窗口首尾 / 未声明 / 畸形（独立探针 `probe_win.py` + `probe_full.py`）

| 场景 | 结果 | 判定 |
|---|---|---|
| 声明窗口 31 min，实际 26（head 3, tail 2 缺） | `expected=31 observed=26 head_missing=3 tail_missing=2 coverage=0.83871 FAIL`；`observation_days=0.020833`（= **声明窗口** 30 min，非 observed span 25 min） | ✓ 首尾缺失计入分母，未自动截断 |
| 未声明窗口（有行） | 窗口 `state=FAIL codes=[WINDOW_MISSING]`；`directional_candidates`/`eligible_closed_trades` gate = **NO_DATA, actual=None**；`window_applied=false`；`release_evidence.directional_candidates=null`；coverage gate `NO_DATA+WINDOW_MISSING` | ✓ 不给出未加窗口的分母 |
| 畸形窗口 10 种（`not-a-date`/`inf`/`nan`/`1e400`(数值+字符串)/`2**64`/`2**64+1`/`99999999999999999999`/`True`/反向窗口） | 全部 `FAIL + WINDOW_INVALID`，**无一抛异常**（反向窗口 walk 到完整报告 `NO_CRASH=true`，coverage=FAIL） | ✓ |
| 反向窗口（完整报告） | window FAIL+WINDOW_INVALID；dc_gate FAIL；closed_gate FAIL；coverage FAIL | ✓ 不崩溃 |

## 5 lifecycle 去重 / 迟到修订 / 冲突 / 成本不完整（`probe_full.py`）

| 场景 | 结果 | 判定 |
|---|---|---|
| 30 lifecycle + 1 条迟到修订（同 lc，新 outcome_id，recorded_at 晚） | `eligible=30`（**不是 31**）；`revised_lifecycles=[lc-000]`；该 lc 净收益 `0.5`（fee 0.1→0.4 重算）；总额 23.7 | ✓ 不重复计数、按最新重算 |
| 同一 `recorded_at` 不同内容 | `conflicted=[lc-005] reason=REVISION_CONFLICT:same_recorded_at=...`；`eligible=29`（冲突**不进 30**）；gate FAIL | ✓ |
| `cost_status=UNKNOWN` 的 lifecycle | 不进 eligible（29）；总额只含合格行 | ✓ 不当 0 计 |
| 合格 lifecycle 但缺成本项（`funding_cashflow=NULL`） | `eligible=30` 仍内，但 `net_realized_unknown_lifecycles=1`、该行 `cost_recomputed=false`、总额不含它 | ✓ 未知仍未知，不谎报净 0 |

## 6 ACK ≠ 恢复（`probe_full.py`，走共享补丁 `health_watch.alert_receipt_truth`）

| 场景 | 结果 |
|---|---|
| 仅 ACK(+供应商接受) 无送达 | `state=FAIL`，`reason_codes=[MANUAL_ACK_ONLY, NO_CONFIRMED_DELIVERY]`，`delivered_confirmed=0`，`ack_without_delivery=[inc-ack-only]`，`provider_accepted_only=1` |
| 渲染事件恢复 | `ACK_WITHOUT_RECOVERY`+`ACK_WITHOUT_DELIVERY`+`MANUAL_ACK_ONLY`+`NO_CONFIRMED_DELIVERY`；`ack_is_not_recovery=true` |
| 补真实回执（`delivered_at_ms`+`device_receipt`）后 | `delivered_confirmed=1`，`ack_without_delivery=[]`；仍因另一 incident 只有供应商接受而保留 `NO_CONFIRMED_DELIVERY`（不误判） |

原因码确为 U14 冻结常量（`core.ops.alert_delivery`），未另造话术。

## 7 重启 vs 换版（`probe_full.py`，走共享补丁 `health_watch.release_observation_window`）

| 场景 | verdict | state | 原因码 |
|---|---|---|---|
| 2 session + 单一 config_hash | `RESTART_SAME_ARTIFACT` | PASS | —；`restart_count=1`，`restart_is_version_change=false` |
| 第二个 config_hash | `CONFIG_CHANGED_WITHIN_WINDOW` | FAIL | `MIXED_CONFIG_SAMPLES` |
| 窗口内其它 release_id 行 | `MIXED_RELEASE` | FAIL | `MIXED_RELEASE_SAMPLES` |
| 1 session | `SINGLE_SESSION` | PASS | — |

health_watch 探针另有 `head_gap_minutes=0 / tail_gap_minutes=27`（观察者未写入断档，与 coverage 的"有行但不健康"分开命名）。

## 8 两套件三态
见 §0（A 26/0、B 65/0+9/0、C 78/0+60/0），全部复现。

## 10 补丁应用矩阵（`_scratch_Q/21_matrix.txt`，实跑）

工作树形态：W1=取自 blob；W2=把 `health_watch/report/run_offline_regression` 转 CRLF；
W3=全 LF。git 配置 3 种。每格 4 补丁。

| 形态 | 默认 | `autocrlf=false -c core.eol=lf` | `autocrlf=true` |
|---|---|---|---|
| W1_blob | **4/4** | **4/4** | **4/4** |
| W2_crlf | **4/4** | **1/4**（02/03/04 失败）→ `--ignore-whitespace` 后 4/4 | **4/4** |
| W3_all_lf | **4/4** | **4/4** | **4/4** |

**结论：作者自述成立** —— 唯一失败组合是"工作树 CRLF 却声明不下转换/用 LF"，
该格 02/03/04 各以 `error: while searching for:` 失败；其余 17 格全 4/4 且 0 `Skipped`。
`--ignore-whitespace` 在上述所有格子均通过。

---

## 对抗性验证（伪造 / 绕过 / 越权）

| # | 攻击 | 结果 | 判定 |
|---|---|---|---|
| A1 | 声明 PASS 但**无窗口** | 5 组全 `FAIL` + `PASS_WITHOUT_EVIDENCE` + `WINDOW_MISSING`；`release_readiness=NO_GO` | ✓ 按规格 |
| A2 | **身份引用缺失**（`artifact_hash`/`runtime_instance_id` 均无） | `identity state=NO_DATA codes=[ARTIFACT_HASH_MISSING, IDENTITY_REF_MISSING]`；`_group_evidence` 不传 `identity_ref`（`release_evidence.py:1490`） | ✓ |
| A3 | 绕过来源过滤：旧别名 `A_strategy`×30、空串×30、未知值×30、小写/大写变形各 30 | 全部 `research_only_source` 排除，分母 **200**（pristine 350）；大小写变形也**不被误计**（fail-closed） | ✓ |
| A4 | 纯 NO_EDGE / neutral / unknown 480 条 | 分母 **200** 不变；`no_edge_declared=240`、`non_directional_signal=120` | ✓ |
| A5 | 越权放宽 200/30/7：传 `thresholds={min_directional_candidates:1,...}` | `required` 随之变 1（**显式调用点入参**）；`DEFAULT_THRESHOLDS` 不变；无配置/环境变量路径；全仓唯一调用点 `smoke_release_acceptance.py:137` 是把门槛**调高**到 201 | ⚠ P3 观察，非阻塞 |
| A6 | 伪造完整身份（假 64 位 hash + 假 instance）+ 合法 8 天窗口 + **空证据** + 声明 PASS | K06 五组全 `PASS`、`blocking_item_count=0`；但 **`release_readiness=NO_GO`**、`release_gates_passed=false`（dc/closed gate actual=0 FAIL）。同场景**不声明**时 4 组 `NO_DATA`、blocking=4 | ⚠ P2 观察 |

A6 说明（**非阻塞，已被作者披露**）：K06 组的 `status = declared.get(group, computed)`
（`release_evidence.py:1483`）——**调用方声明优先**。只要 `identity_ref` 结构性存在 +
窗口有效，声明 PASS 就会让五组变绿，即使底层零证据。但**权威 GO 字段
`release_readiness` 只看 `gates`**，此路无法把它变 GO。作者 README §8.6 已如实登记
"谁有权声明 PASS 属 U12 发布审查流程，未在本任务定义"。建议 U12 复核时把
"声明 PASS 必须同时有非空底层证据"提升为 K06 硬约束（若尚未在冻结契约里）。

## 其它核对（既有注入 vs 新引入）

- **补丁 01 别名归一**（`src/audit/database.py`）：`canonical_shadow_source` 独立实测
  `A_strategy→SOURCE_A`、`binance_top_trader_proxy→SOURCE_B` 逐字等于 `shadow_status` 常量；
  未知名/空串**原样透传**（不误改）；`release_gate_role("A_strategy")` 单独为
  `RESEARCH_ONLY` → 证明别名表**不是**第二份门禁清单，只是存储归一（fail-closed）。
  证据 `_scratch_Q/25_alias.txt`。
- **补丁 02 只读性**：新增探针 diff 中**零** `INSERT/UPDATE/DELETE/DROP/ALTER`（只有
  `facts.update({...})` 字典更新）。证据 `_scratch_Q/26_ro.txt`。
- **B 态诚实降级**：`SHARED=False` 时冒烟明示"共享补丁未应用 → 依赖块标
  `NO_DATA + U09_SHARED_PATCH_MISSING`（不猜不伪造）"，9/0。
- **A 态非空洞**：26 条 PASS 逐条打印基线缺陷**实数**（S08 分母 300；NO_EDGE 200→440；
  首尾断档被静默吸收；未声明窗口时自动截到首末观察记录、覆盖率虚报 100%；无修订/冲突披露；
  无 event_recovery 块）。证据 `_scratch_Q/rA_acceptance.txt`。

---

## 9 全量 delta（各 13 分钟，独立沙箱，`git init` 前置）

| 运行 | 树 | 登记脚本数 | 结果 | rc | 内部 `FAIL` 行 |
|---|---|---|---|---|---|
| pristine | `448a4e9` | 84 | `OFFLINE REGRESSION OK scripts=84` | 0 | 10 |
| patched | `b675118`+01..04 | 85 | `OFFLINE REGRESSION OK scripts=85` | 0 | 10 |

- **NEW_FAIL = ∅**：`FAIL` 行集合双向 diff 均为空（`_scratch_Q/28_delta.txt`，`delta.py`）。
- 该 10 行**全部**来自 `scripts/smoke_u02_strategy_position_manager.py`（逐行分组确认），
  两态逐字相同；u02 自身三段汇总（158/0、156/2、150/8）在两态也逐字相同 →
  **既有状态，非本次引入**。
- patched 回归内含 `=== OFFLINE scripts/smoke_u09_release_evidence.py ===` 且
  `U09 冒烟: 60 通过 / 0 失败 (PATCHED=True, SHARED=True)`。
- 复现：`git -C <q> archive <commit> | tar -x -C <tmp> && cd <tmp> && git init -q . &&
  git add -A -f && git commit -qm base && PY scripts/run_offline_regression.py`。

> 注：u02 自己打印 10 行 `FAIL` 但汇总写 `FAIL=8`（另两段 0/2）——是 u02 **既有**的自计数
> 小瑕疵，两态一致，与 U09 无关，仅记录。

---

## 新发现清单

| ID | 级别 | 现象 | 是作者新引入? | 证据 |
|---|---|---|---|---|
| Q-1 | **P3** | `build_release_readiness_report` 暴露 `thresholds=` 入参，显式传入可把 `required` 从 200/30/7 调低（实测 required→1）。无配置/环境变量路径，默认常量冻结，全仓唯一调用点是把门槛**调高**到 201 的反例。 | 否（既有函数签名本就带 `thresholds`；pristine 同有） | `_scratch_Q/19_adv.txt` |
| Q-2 | **P2** | 伪造"结构性完整"的身份（假 hash+假 instance）+ 合法窗口 + 空证据 + 声明 PASS → K06 五组全 PASS、blocking=0；但权威 `release_readiness` 仍 `NO_GO`（gates actual=0 FAIL）。不声明时 4 组 NO_DATA、blocking=4。 | 否（新代码，但口径已由作者 README §8.6 披露"声明状态由调用方传入"） | `_scratch_Q/23_forge.txt` |
| Q-3 | **P3** | `smoke_u02_strategy_position_manager.py` 自打印 10 行 `FAIL` 但汇总结论写 `FAIL=8`；两态逐字一致。 | 否（既有） | `_scratch_Q/28_delta.txt` |
| Q-4 | **P3** | 大小写/空白变形来源（`a_current_baseline`/`A_CURRENT_BASELINE`）**不被**计入分母（fail-closed）。安全方向，但若历史库真有大小写脏数据，会被当研究影子排除。 | 否（口径来自 U06a `COHORTS` 精确匹配） | `_scratch_Q/10_s08.txt` |

未发现 P0/P1 阻塞项。

---

## 结论

**PASS**（无阻塞项；2 条 P2/P3 观察供 U12 复核，均不改变本任务硬主张）。

10 项核心主张全部**独立复现**、数字与作者一致：

1. ✓ S08 真修（自建夹具 A+B+C 300→200；删 B/C 分母不变；无第二份名单；反向 monkeypatch 证明同源）
2. ✓ 门禁未降（200/30/7.0/0.999/120 不变；gates 键集合源码级+运行时逐字相同；无整体 GO 字段）
3. ✓ 纯 NO_EDGE/无方向不计入
4. ✓ 窗口首尾计入分母、不自动截断；未声明 → NO_DATA+WINDOW_MISSING 且分母 `None`；畸形 → FAIL+WINDOW_INVALID 不崩溃
5. ✓ lifecycle 去重、迟到修订重算、冲突隔离、成本不完整不谎报
6. ✓ ACK≠恢复（复用 U14 原因码；补真实回执后转正）
7. ✓ 重启 vs 换版区分（RESTART_SAME_ARTIFACT / CONFIG_CHANGED_WITHIN_WINDOW / MIXED_RELEASE_SAMPLES）
8. ✓ 两套件三态 A 26/0、B 65/0+9/0、C 78/0+60/0
9. ✓ 全量 delta scripts 84→85，NEW_FAIL=∅（10 行 FAIL 全属既有 u02，两态逐字相同）
10. ✓ 补丁 3 配置 × 3 形态矩阵与作者自述一致（唯一失败组合=CRLF 工作树却声明 LF 不转换）

## 我未能验证的部分

- **补丁 01/02/03 的运行时效果只在我自建的合成 DB 上验证**，未在真实 demo 服务/真实
  `data/` 上验证（运行目录只读约束）；未做真实故障注入（窗口断档/告警不送达）。
- **B 态全量回归未跑**（无 patch 04 时 `_validate_smoke_manifest()` 必抛
  `RuntimeError`）；此点仅**静态**确认（`discovered - listed - excluded` 非空），未实跑。
- **补丁 03 CLI（`report_release_readiness.py --report-at/--declaration/--strict-evidence`）
  未端到端运行**：其证据脚本 `docs/dispatch/patches/U09/evidence/probe_cli_report.py`
  属作者产物，未采信。
- **`alert_deliveries`/`risk_incidents` 的真实数据形态**未验证（合成行）。
- `ruff` **已复跑**（venv 有 ruff 0.16.6）：三文件 `All checks passed!` rc=0。~~未复跑~~
- K06 冻结契约内部（`build_release_report`）未逐行审计，只测其对外行为。

## 报告文件

`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U09\R1\q\_scratch_Q\Q_U09_r1.md`
（原始输出同目录：`10_s08.txt` `12_win.txt` `17_full.txt` `19_adv.txt` `21_matrix.txt`
`23_forge.txt` `25_alias.txt` `28_delta.txt` `rA/rB/rC_*.txt` `20_reg_*.txt`）
