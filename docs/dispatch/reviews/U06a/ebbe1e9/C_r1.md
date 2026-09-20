# C 初审报告 · U06a（影子最小隔离与口径修正）

- 审查者：C 角色（数据来源 / 统计诚实），独立会话，非作者会话
- 审查对象：`task/U06a` head = `ebbe1e9`（base = `59660b2` 集成 HEAD）
- 工作区（detached）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U06a\C\r1`
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（`PYTHONUTF8=1`，无 pytest）
- 结论：**有条件 GO**（2×P1 + 4×P2 + 4×P3；无 P0）

## 0. 沙箱与方法

三棵树，全部由 `git archive` 落在**我自己**工作区的 `_scratch_C/sandbox/`：

| 树 | 内容 | 用途 |
|---|---|---|
| `pristine` | `ebbe1e9` 原样 + 清单登记行 | delta 基线 |
| `patched` | 同上 + 3 个补丁 | delta 被测树 / 全部功能探针 |
| `svc_only` | 同上，只打 `src_intel_service` + `decisions.html`（**不打** smoke 补丁） | 验证 `smoke_intel_sidecar` 冲突真实性 |

> **方法学发现（对后续审查者重要）**：`git apply` 在本机默认会向外层仓库
> （`reviews/U06a/C/r1` 是 `r123` worktree）解析路径；从沙箱子目录运行时
> **静默 no-op**（返回码 0、`--check` 也报 OK，但文件不变）。必须
> `GIT_CEILING_DIRECTORIES=<worktree 根>` 才能对沙箱树真正落盘。
> 我第一轮"delta ∅ 全绿"是**假绿**，已作废重跑（§7）。

- 补丁前像 = 集成 HEAD `59660b2`；逐条 `--check` 三点全 OK（`core.autocrlf=false -c core.eol=lf`）。
- 打完补丁后 `patched/src/intel/service.py` 与作者 WIP `f0c0e48:src/intel/service.py`
  **逐字节相同**（41628 字节，md5 `2cffcf9b2f45420219ec01a5b6264cb6`）→ 补丁忠实还原意图。
- 补丁正文行尾：用**字节级**检查，3 个 `.diff` 的 `CR=0`、`CRLF=0`（纯 LF）→ README §2 属实。
  （注：本机 bash 的 `grep -c $'\r'` 会给出 20/58/371 的**假阳性**，不要用它。）
- 分支相对 base 的改动 = **7 个新增文件，0 个修改**（`git diff --name-status`）：3 个独占/补丁文档
  + `docs/dispatch/patches/U06a/cd9c2a4/*`。共享文件 `src/intel/service.py`、
  `decisions.html`、`src/core/service.py`、`smoke_intel_sidecar.py`、`run_offline_regression.py`
  的 diff 均为 **0 行** → 作者"共享文件已还原"属实。
- 全部命令输出重定向到 `_scratch_C/*.txt`；探针源码在 `_scratch_C/`。

---

## 1. 必达① 旧口径明确展示且执行权重为零

### 1.1 展示（独立渲染，不采信作者的 TestClient 结论）
自写 `probe_render.py`：直接用 `jinja2` 渲染**真实的** `decisions.html`，上下文 = 真实侧车轮次快照
（离线桩）+ 真实 `_decision_observability`。配置故意写 `execution_weight=0.9, shadow_only=False`。

```
RENDER 影子口径=True   LEGACY_ACTIVE=True   配置声明不可抬升=True   DEFERRED=True
       A/B/C 研究隔离=True   C=unconfigured=True   旧口径已知缺陷=True
       可人工评审=False     研究样本可作执行候选 否=True   release_evidence=True
       weight_rendered=['0','0']（两处执行权重均为 0）
```
→ 作者声称命中的字符串**全部独立复现**；`可人工评审` 已消失。

### 1.2 权重能否被抬到非零（穷举尝试，全部失败）
| 尝试 | 结果 |
|---|---|
| config `execution_weight=0.9 / -1.0 / True / "0.9"`、`shadow_only=False` | `effective` 恒 `0.0`；`conflict=True`，`forced_fields` 逐条登记 |
| 环境变量 `MTA_INTEL_EXECUTION_WEIGHT=0.9`、`EXECUTION_WEIGHT=0.9` | 无效果（全仓无读 env 的权重路径） |
| 运行时改实例属性 `svc.execution_weight = 0.9` | `snapshot()["execution_weight"]` 仍 `0.0`（`_empty_snapshot` 读模块常量） |
| 改源码常量 `shadow_status.EXECUTION_WEIGHT = 0.9` | snapshot **变 0.9** ← 唯一抬升路径，需改源码 |
| 消费者扫描 | `execution_weight` 仅"存库 + 看板显示"；**无任何执行路径读取**（`src/dashboard/app.py:477,484` 硬编码 0.0） |

→ **配置面/环境变量均无法抬升**，满足必达①。第 4 行是 P3（信息性）：patched 树比
pristine 树**更**依赖该模块常量（pristine 是字面量 `0.0`），若有人改常量则两态都会变，同属"改源码"威胁模型。

### 1.3 旧口径是否会被"C 已配置"挤掉
`E.caliber_state='LEGACY_ACTIVE'`、`E.limitations_count=6`，在 `C_status='configured'` 时
**依然全量展示** → 旧口径不会因配置齐全而隐藏。✔

### 1.4 "已知缺陷 6 条"是否覆盖了它自己的真实缺陷
逐条核对，**6 条全部属实**且能对应到代码（取价时点=轮转刷新当前标记价；缺价即消失；
未按版本/环境隔离；费用/滑点/资金费为固定研究假设 —— 对应
`database.py:1332-1342` 的 `fee_cost/slip_cost/funding_cost = rate*100*(horizon/480)`；
同轮重叠样本非独立；A/B/C 非随机实验）。

**但漏了一条最要紧的自有缺陷**：`LEGACY_LIMITATIONS` 里**没有**"B/C 研究影子行目前仍会计入
200 方向候选分母"。该 caveat 只写在 `release_gate.note`（**JSON 字段，模板未渲染**），
而页面显式打出"研究样本可作执行候选 **否**"，读者极易读成"不进发布门槛"。→ **P2**（§5 有硬证据）。

---

## 2. 必达② C 未配置 → 零无效绩效样本（独立复现，非手工插行）

作者的集成测试 `test_unknown_rows_really_become_counted_sample_rows` 是**手工插入**一行
`signal='unknown'` 来模拟旧行为。我在**真实旧代码路径**上重放（pristine 树 + 离线桩网络 +
C 未配置），得到：

```
PRISTINE  A.by_source = [(B_contract_market,['bullish']), (C_contract_plus_web3,['unknown'])]
          A.C_rows = 1   A.C_unknown_rows = 1
          回填 created_at 后 evaluate_shadow_decisions (grace=400) → A.outcomes_inserted_now = 8
          A.evaluations = [(B,1,1)×4, (C_contract_plus_web3, 1, 0)×4]
PATCHED   A.by_source = [(B_contract_market,['bullish'])]   A.C_rows = 0
          A.evaluations = [(B,1,1)×4]   A.skipped_emissions = 1   A.C_status='unconfigured'
```
→ **旧实现确实为 `unknown` 生成被计数的绩效行**（`samples=1, directional_samples=0`，×4 horizon），
新闸门把 C 组行数压到 0 并如实登记 `skipped_emissions`。**"修复前确实脏、修复后不脏" 已独立证实。**

### 2.1 绕过尝试（全部未突破）
| 边界 | 结果 |
|---|---|
| C 半配置：只有 `watched_wallets` / 只有 `token_contracts` | `unconfigured`，`emits=False`，真实轮次 `C_rows=0` |
| C "半配置"= 空字符串钱包 + 空 dict | `unconfigured`，不产样本 |
| C 配置后又清空（`[]` / `{}`） | `unconfigured`，不产样本 |
| C 配置齐全但本轮无钱包方向事件 | 真实轮次 `C_rows=0`（运行期闸门也生效） |
| `enabled=False` | `disabled`，A/B 也不写 |
| 只读凭据缺失（`credential_available=False`） | `credentials_missing`，不产样本；不影响 A/B |
| 非方向（unknown/neutral/空/None）与无效价（0/负/None/非数值/""/容器） | 全部拒写（10 例） |
| 未知来源 `D_unknown_cohort` | fail-closed 拒写 |
| 数据陈旧/为空 | C 未配置时 `missing_sample_accounting` 返回 `decided=0 state=OK`；**total=0 时 `coverage=None`，不用 0 冒充覆盖率** ✔ |
| 并发刷新竞态 | `cohort_plan`/`weight_guard` 在 `__init__` 由**冻结的** `self.config` 计算；`should_emit_sample` 每次从同一 config 重算同一结论 → 无 TOCTOU。`maybe_refresh` 在长 I/O 期间**不持** `self._lock` → 不会因侧车采集把管理线程挡在服务锁上 |

---

## 3. 必达③ 侧车隔离

### 3.1 AST 自解析（不采信作者结论）
```
shadow_status.py imports = ['__future__', 'dataclasses', 'typing']      禁止命中 = []
intel/service.py  imports = [... httpx, os, threading, time ...]        （既有数据抓取，非 LLM）
```
`shadow_status.py` 无网络/LLM/DB/线程/子进程导入，也无 `open/eval/exec/compile` 调用。✔

### 3.2 线程独立性
`src/core/service.py:1291-1294`：
`threading.Thread(target=self._intel_loop, name="readonly-intel-sidecar", daemon=True)`。
同文件其余线程：`release-evidence`(→`_evidence_loop`)、`review-worker`(→`_review_worker_loop`)。
目标函数集合互不重叠，侧车线程名唯一。✔

### 3.3 额外 LLM / 争用
- `_intel_loop` 只调 `maybe_refresh()`；`src/intel/` 整包无 LLM/Broker 导入；补丁对
  `src/intel/service.py` 只新增 `from intel import shadow_status` 一个 import。**无额外 LLM 调用。** ✔
- 争用：`maybe_refresh` 在 `_refresh()` 全程**不持** `self._lock`（仅中段短持锁更新
  `_last_attempt`/`_snapshot`）；共享的是 `db.lock`（短事务）。新缓存把最重的
  `intel_observability()`（`shadow_outcomes JOIN shadow_decisions` 全表 GROUP BY，持 `db.lock`）
  从**每轮**降到**≥`metrics_refresh_seconds`(默认 900s) 一次** → 争用**下降**。
  披露字段 `network_budget: shared_public_market_limiter` 如实承认共享限流预算，未假装"零争用"。✔

### 3.4 P2 —— 汇总指标缓存"把陈旧值当新值"
`probe_metrics.py`（真实 `ReadOnlyIntelService`）：
```
m1(首次)      = state=REFRESHED, age=0.0, computed_at=T1
让 intel_observability() 抛错：
m2(失败后)    = state=REFRESHED, cached=False, age=0.0, computed_at=T1(旧), errors=['metrics:RuntimeError']
m3(未到期)    = state=CACHED,    age=0.0（实际距最后一次**成功**计算已 >1s）
缓存仍是旧值，computed_at 未变 → 真相可恢复，但 state/cached/age 三者都在误导。
```
成因：`_refresh_metrics_summary` 在 `try` **之前**就执行 `self._metrics_attempt_at = time.monotonic()`；
计算失败时缓存不回滚、attempt 时间也不回滚。`_metrics_age_seconds` 的 docstring 自述是
"距上次**尝试**"，但对外字段名是 `age_seconds`。

附带两点：
- `shadow_metrics.state` / `age_seconds` **完全没有渲染**到 `/decisions`（模板只用了
  `wi.shadow_metrics.evaluations`）→ 页面可能长期显示 `CACHED` 或 `UNAVAILABLE` 的数字而**无任何陈旧提示**。
- 首次刷新若超预算：`_metrics_cache` 为 `None` → `shadow_evaluation={}` → 模板第 139 行
  `(wi.shadow_evaluation or {}).events_24h or 0` 把"未计算"渲染成 **"24h 事件 0"**，
  正是 `missing_sample_accounting` 明确拒绝的"用 0 冒充无缺失"反模式。作者在 README §5 披露了此点，
  但 UI 与 `LEGACY_LIMITATIONS` 都没有 → P3（一致性问题）。

---

## 4. 必达④ v2 标签必须 DEFERRED

- 单一定义：`shadow_status.py:36 V2_LABELS_STATE = "DEFERRED"`，三处消费一致：
  `sample_marker`(L332)、`legacy_metric_view`(L493)、`build_disclosure`(L536)；
  实测 `legacy_metric_view.v2 / marker.v2 / disclosure.v2` **三处均为 `DEFERRED`**。
- 模板两处回落字面量 `'DEFERRED'`（披露缺失时也不伪造"已支持"）。
- 全仓 grep `v2`：其余命中均为无关项（策略 `signals_v2`、币安 `/fapi/v2/*`、etherscan v2）。
  **无任何"v2 已支持"的暗示。** ✔

---

## 5. 必达⑤ —— 影子样本是否真的进不了发布门槛（硬结论）

### 硬结论：**进得去。B/C 研究影子行确实会计入 200 方向候选分母。**

`probe_gate.py` 用**真实** `build_release_readiness_report` + `smoke_release_acceptance` 的
同型 Fixture（`src/core/service.py:186` 真实调用 `db.set_release_context(...)` 复刻 release_id/environment 语境）：

```
baseline_A_only = directional_candidates=200, eligible_closed=30, observation_days=7.0, GO
with_B_C_shadow = directional_candidates=300, eligible_closed=30, observation_days=7.0, GO
                  shadow_source_rows={A:200, B:50, C:50}
Δ directional_candidates = +100      ← 影子样本进来了
Δ eligible_closed_lifecycles = 0     ← 30 闭合不受影响
Δ observation_days           = 0.0   ← 7 天观察不受影响
```
- 构造方式刻意复刻真实侧车写法：B/C 行**不带** `opportunity_id`，机会键退化为
  `symbol|signal|created_at[:16]`（分钟粒度），与 A 行不重合。这正是
  `release_evidence.py:188-194 _opportunity_key` 的回退分支。
- 成因（读码 + 实测一致）：`release_evidence.py:338-346` 的 candidate 查询只按
  `release_id / environment / signal IN ('bullish','bearish')` 过滤，**没有按 `source` 过滤**。
- 生产可达性已核实：`src/core/service.py:186` 对共享 `AuditDatabase` 调 `set_release_context`，
  而 `log_shadow_decision`（`database.py:1274-1275`）默认继承 `db.release_id / db.environment`
  → 侧车写出的行与门禁查询**同 release_id / environment**，能被计入。
- 放大效应：侧车每 `refresh_seconds`（≥60s，默认 300s）一轮、每标的各写一行，分钟粒度机会键
  **持续产生新键** → 7 天窗口下可持续推高分母。

### 但作者没有说谎
- `release_gate.note`（`shadow_status.py:551-556`）逐字写明："现有实现按 signal 去重后计数，
  **尚未按 source 过滤**；U06a 只提供 release_gate_role/research_sources 事实，供 U09 施加来源过滤，
  **不建立第二套 GO 判断器**"。README §4 同样如实。
- `gate_constants` 实测 == `release_evidence.DEFAULT_THRESHOLDS`（200 / 30），未出现第二个 GO 字段。
- 全仓无第二个 GO 判断器（唯一的 `release_readiness` / `release_gates_passed` 计算在
  `release_evidence.py`；grep 其余 `src/**.py` 零命中）→ "未建第二套 GO" 属实。
- **精确化（P3）**：`gate_constants` 在 `shadow_status.py:549-550` 是**硬编码字面量**，
  `shadow_status.py` **不 import** `release_evidence`；所谓"直接读…比对"是**测试**
  `test_gate_caliber_agrees_with_release_evidence` 在做漂移检测。若 U09 调阈值：
  测试会红（好），但**运行时披露会先说出过期的数字**（这是第二份常量副本）。
- `source_counts`（报告里的 `shadow_source_rows`）作为**诊断值**暴露，U09 可据此加 source 过滤。
- U06a **未引入回归**：补丁只**减少**行数（C unknown 行不再写），B 行行为与旧代码一致。

### 定级
**P1 —— 必达⑤ 未在代码层达成，仅"如实披露 + 指派 U09"。**
若按逐字必达判定则为**未达成**；考虑到 (a) `src/core/release_evidence.py` 既不在 U06a 独占路径
也不在集成目标，(b) 任务卡本身写明"200 门槛的业务口径由 U09 固定并版本化"，
(c) 披露准确无夸大 —— 我判定为**范围外依赖且已披露**，**不判为作者过失**，
但必须作为 U09 的**硬前置**登记，且建议把 caveat 渲染到页面（见 §1.4 的 P2）。

---

## 6. 补丁逐条审读

### 6.1 `src_intel_service.py.diff` —— 通过，附 1×P2 + 2×P3
- 与作者 WIP 逐字节一致；闸门 `should_emit_sample` 三条件（已配置 + 方向明确 + 参考价有效）齐备。
- `_record_fused_shadows` 未配置直接 `return len(rows)`（O(1)，不遍历）——语义正确。
- **P2**：`_refresh_metrics_summary` 的 state/age 语义（§3.4）。
- **P3**：`_record_fused_shadows` 返回的 `skipped` 把"未配置"与"无方向/无价"混在一个计数里，
  披露口径无法区分"配置缺"与"数据缺"（`isolation_facts.skipped_emissions` 是单值）。
- **P3**：`self._sample_loss` 只在 `allow_metrics=True` 分支刷新 → 未到期时覆盖率是陈旧值，
  且披露里没有对应 `state` 标注（同一"陈旧当新值"家族）。

### 6.2 `src_dashboard_templates_decisions.html.diff` —— 通过，附 1×P2
- 纯展示层；`wi.shadow_metrics.evaluations` 缺失时回落 `wi.shadow_evaluation.evaluations`；
  `sdw.effective|default(0, true)` 保证缺失不伪造权重；"可人工评审"徽章改 `muted`。
- **P2**：`release_gate.note`（含"尚未按 source 过滤"这句关键 caveat）**未渲染**；
  页面只显示"研究样本可作执行候选 否"，可能被误读为"不进发布门槛"。

### 6.3 `scripts_smoke_intel_sidecar.py.diff` —— **正当**，冲突已独立证伪/证实
- **冲突真实存在**：在 `svc_only`（适配已打、smoke 未打）实测
  `AssertionError: B/C影子组同时记录`，exit 1（原始 stdout 在 `_scratch_C/conflict_svc_only.txt`）。
- **未削弱原断言**：`sources == {B,C}` → `sources == {B}` 仍是**精确集合相等**（能抓住 A 行泄漏）；
  新增 `COUNT(C)==0` 是**更强**的精确计数断言。被删掉的只是"C 必须写 unknown 行"这一条
  **与必达②直接冲突**的旧口径断言。17 条其余断言逐字未改，实测 19 通过 / 0 失败。
- **偏差声明齐备**（README §1 表格 + ⚠️ 段），且**未把该文件提交进分支**（走补丁）。
- 保留意见：该文件不在任务卡三个集成目标内 → 程序性 P2（与 §7 的 P1 同源）。

### 6.4 缺一个补丁：`scripts/run_offline_regression.py` —— **P1**
见 §7。

---

## 7. 全量回归 delta（基于"补丁实际应用后的树"）

**方法与基线**：三棵树都加了同一行清单登记
`"scripts/smoke_u06a_shadow_isolation.py",`（否则两侧都在 `_validate_smoke_manifest` 直接 raise，
无法比较脚本集合）→ 与作者"沙箱里临时登记一次"的做法一致，harness 清单与 env 完全相同。
用自写 `probe_delta.py` **逐条非 fail-fast**（`run_offline_regression.py` 本身是 `check=True` 的
fail-fast，跑不出失败集合）跑完 80 个脚本，用 `^\s*FAIL\s+` 抽失败名 + 记录退出码。

```
pristine（不打补丁）: TOTAL=80  BAD=0
patched （打完 3 补丁）: TOTAL=80  BAD=0   → 失败集合差集 = ∅，两侧均全绿
```
日志：`_scratch_C/log_pristine.txt`、`log_patched.txt`、`out_pristine.json`、`out_patched.json`。
**无新增失败。** 作者"两侧 OFFLINE REGRESSION OK、差集 ∅"的说法在本树集上成立。

补充独立实测（与作者自述一致）：
- `tests/integration/test_shadow_status.py`：pristine **200/0**，patched **239/0**（exit 0）。
- 作者新增的 `scripts/smoke_u06a_shadow_isolation.py` 在两态都 exit 0（运行时探测共享适配是否应用）。

### P1 —— 分支 head 自身让主门禁变红
在**未做任何改动**的 `ebbe1e9` 树上直接跑主门禁：
```
RuntimeError: 未登记冒烟脚本: scripts/smoke_u06a_shadow_isolation.py
```
（原始输出 `_scratch_C/manifest_base` 复现，exit 1。）
即：交付分支**自身**使 `scripts/run_offline_regression.py` 立即失败。作者在
patch README 与脚本 docstring 三处声明"该登记行必须由 I 加入 / 必须同批原子落地"，
**但没有为 `scripts/run_offline_regression.py` 建补丁**——按同一逻辑它也是共享文件，
而作者刚刚为另一个"不在集成目标内"的共享文件（`smoke_intel_sidecar.py`）建了补丁，**标准不一致**。
建议二选一：(a) 补第 4 个补丁把登记行一起交给 I；(b) 把新冒烟脚本放到 `tests/`（避开 `smoke_*.py` 清单扫描）。

---

## 8. 问题清单

| # | 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|---|
| 1 | **P1** | `ebbe1e9`（分支自身）/ `scripts/run_offline_regression.py:101` | 新增冒烟脚本未登记 → 主门禁在分支上立即 raise；且未像其它共享文件那样走补丁 | `_scratch_C/manifest_base`：`RuntimeError: 未登记冒烟脚本` |
| 2 | **P1** | `src/core/release_evidence.py:338-346`（U09 领地） | 必达⑤未代码化：B/C 研究影子行进 200 方向候选分母（200→300） | `probe_gate_patched.txt`：`Δ=+100`；`src/core/service.py:186` 证实 release_id 可达 |
| 3 | P2 | `src/intel/service.py`（补丁态 `_refresh_metrics_summary`） | 聚合失败后 `state=REFRESHED`/`cached=False`/`age≈0`，实际仍是旧缓存 → 陈旧当新值 | `probe_metrics.txt` |
| 4 | P2 | `src/dashboard/templates/decisions.html:150-168`（补丁态） | `release_gate.note`（"尚未按 source 过滤"）未渲染；页面只显示"研究样本可作执行候选 否"，易被误读 | `probe_render_patched.txt` + 模板 diff |
| 5 | P2 | `docs/dispatch/patches/U06a/cd9c2a4/scripts_smoke_intel_sidecar.py.diff` | 修改了不在任务卡集成目标内的共享门禁文件（偏差已声明，正当但需 I 确认） | `conflict_svc_only.txt` 证明确有必要 |
| 6 | P2 | `src/intel/shadow_status.py:95-104` | `LEGACY_LIMITATIONS`（页面可见）**遗漏**"B/C 现行仍计入 200 候选"这条最要紧缺陷 | §1.4 / §5 |
| 7 | P3 | `src/intel/service.py:117`（补丁态） | `_empty_snapshot` 动态读模块常量 → 改源码常量即可把展示权重抬到 0.9（配置/环境变量不行） | `probe_core_patched.txt`：`B.after_module_const_snapshot = 0.9` |
| 8 | P3 | `decisions.html:139` | 首次刷新超预算时 `events_24h` 由 `{} or 0` 渲染成 **0**，把"未计算"显示成"无事件"（与 `missing_sample_accounting` 的反模式自相矛盾） | 模板 `or 0`；README §5 自述 |
| 9 | P3 | `decisions.html`（补丁态） | `shadow_metrics.state` / `age_seconds` 完全未渲染 → CACHED/UNAVAILABLE 无陈旧提示 | grep 模板无 `age_seconds` |
| 10 | P3 | `src/intel/service.py` `_sample_loss`（补丁态） | 覆盖率只在 `allow_metrics` 分支刷新，未到期时是陈旧值且无 state 标注 | 代码路径 |
| 11 | P3 | `src/intel/shadow_status.py:549-550` | `gate_constants` 是**硬编码字面量**（非"直接读" `release_evidence`）；U09 改阈值时运行时披露会先说错数字 | `grep release_evidence src/intel/shadow_status.py` 无 import |

**无 P0。**

---

## 9. 未覆盖项 / 无法离线验证

1. **真机 7×24 下 B/C 行对 200 的累计放大倍数**：我只能证明机制可达 + 构造样本 `+100`；
   真实放大需生产数据。
2. **真实多线程并发注入**未做（只做代码级锁分析）。`_cursor` 在并发 `force=True` 刷新下无锁自增
   是**既有**问题，本补丁未引入也未修复。
3. `credential_available` 在真实侧车里的取值由 `wallet_tracker.status != "unavailable"` 推导，
   我未在真实网络故障下穷举该字符串的所有取值。
4. U06b/U07 后置要求（固定时点采样、版本隔离、不回溯未来信息、重叠样本不冒充独立）**不在本次范围**，
   我只验证了"重叠样本不等于独立交易"这一条被写进披露。
5. `smoke_intel_sidecar` 补丁在**默认 git 配置**下的 `--check` 我用
   `core.autocrlf=false -c core.eol=lf` 复现成功；未额外在 CRLF 工作树上做逐字节等价比对
   （作者称已做，我未复验 CRLF 侧）。
6. 作者自述 `known_limits` 5 条中，"每轮刷新按 symbol 各写一行 B 样本（既有行为，U06b/U07 处理）"
   —— 我确认该行为真实存在（`smoke_u06a` 断言 `repeats == 2`），但**它同时是 P2-4/P1-2 的放大源**，
   建议 U09 与 U06b 一并考虑。

## 10. 结论

**有条件 GO。** 必达①③④ 独立复现通过；必达② 用**真实旧代码路径**证伪了作者的担忧、
证实了新闸门有效（修复前后对比可复现）；全量 delta 无新增失败。
两个 P1 都是"**范围外依赖 + 已披露**"（U09 门禁 source 过滤；I 的清单登记行），
不构成对作者诚实性的质疑；但必须显式登记为 U09/I 的前置项。
4 个 P2 中，建议**合入前必修**：P2-3（缓存陈旧值语义）与 P2-4/P2-6（把 caveat 渲染到页面并补进缺陷清单）。
