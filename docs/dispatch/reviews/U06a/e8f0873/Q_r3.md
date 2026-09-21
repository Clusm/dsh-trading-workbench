# Q 独立复验报告 · U06a 修复轮 r2

- 复验角色：**Q**（独立 QA，独立会话，与作者不同会话）
- 复验对象：`e8f0873`（分支 `task/U06a`；修复提交 `a01d088`，父 `ebbe1e9`）
- 工作区（detached worktree `r126`）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U06a\R3\r1`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`，无 pytest）
- 上一轮（必读）：`docs/dispatch/reviews/U06a/ebbe1e9/C_r1.md`
- 补丁目录：`docs/dispatch/patches/U06a/a01d088/`（3 条；`cd9c2a4/` 已 superseded，本轮未使用）
- **结论：PASS-with-notes**

> 说明：所有探针均为我自写、自跑，**未采信作者对照**。原始 stdout 全部落在本工作区 `_scratch_Q/`。
> 报告内不写任何会被密钥扫描器命中的字面量。

---

## 0. 沙箱与方法（可复现）

全部树由 `git archive` 落在**我自己的工作区** `_scratch_Q/` 内；每次都设
`GIT_CEILING_DIRECTORIES=<沙箱根>`，**避免上一轮 C 记录过的"子目录 apply 静默 no-op 假绿"**。

| 树 | 内容 | 用途 |
|---|---|---|
| `sandbox/r1` | `59660b2` + 分支独占文件（`ebbe1e9` 版 `shadow_status.py`）+ **cd9c2a4** 3 补丁 | **修复前对照**（应复现 P2-1/P2-2） |
| `sandbox/r2` | `59660b2` + 分支独占文件（`e8f0873` 版）+ **a01d088** 3 补丁 | 修复后被测树 |
| `delta/ipristine` | `a2f09e8`（= 当前集成 HEAD）+ 分支独占文件 + **清单登记行** | 全量 delta 基线 |
| `delta/ipatched` | 同上 + a01d088 3 补丁 | 全量 delta 被测 |
| `applytest/b59660b2`、`applytest/bint` | `59660b2` / `a2f09e8` 原样 | `--check` × 2 前像 × 2 配置 |
| `gatepatched` | `ipatched` 的副本 + `git init` + 全量 add/commit | 跑需要 `git ls-files` 的门禁 |
| `conflict` | `ipatched` 但 smoke 脚本回退为前像 | 证伪/证实 smoke 补丁必要性 |

**补丁落盘已独立证真**（不是只看 `--check`）：a01d088 应用到 `bint` 后的 3 个文件
与我 `sandbox/r2` 的对应文件 **md5 逐一相同**（`service.py` = `cb1c0399…`）。
`src/intel/service.py` 三态字节数：base 31194 / r1 42405 / r2 44351 → 三态互异。

---

## 1. 必验① P2-1「陈旧当新值」—— **已闭合**（前后对照 + 5 类绕过）

探针：`_scratch_Q/probe_metrics_Q.py`（自建，13 条判定）。
原始输出：`metrics_r1.txt`、`metrics_r2.txt`。

### 1.1 前后对照（同一探针、同一序列）

| 阶段 | r1（修复前） | r2（修复后） |
|---|---|---|
| A1 首次成功 | `REFRESHED / age=0.0` | `REFRESHED / age=0.0` |
| **B1 聚合抛错后** | **`REFRESHED / age=0.0 / cached=False`** ← 陈旧当新值 | **`FAILED / age=1.2 / stale=True / last_error=RuntimeError`** |
| B2 失败后走"未到期" | `CACHED / age=0.0` | `CACHED / age=1.2`（继续增长） |
| B3 连续第二次失败 | `REFRESHED / age=0.0` | `FAILED / age=2.301` |
| B5 缓存/`computed_at` | 不动（真相可恢复） | 不动（`computed_at_equals_first=True`） |
| C1 恢复成功 | `REFRESHED / age=0.0`（但**缓存没换新值**判定为 False 侧） | `REFRESHED / age=0.0 / 缓存换成 NEW_VALUE / stale 清零 / last_error=None` |
| D 失败→成功→失败 | 全部 `age=0.0` | `FAILED(age=1.101) → REFRESHED(age=0.0) → FAILED(age=1.101)` |

**r1 十处 `age_seconds` 全为 `0.0`**（`grep '"age_seconds"' metrics_r1.txt`），r2 年龄按
"距上次**成功**"单调增长并在真成功时复位。作者的"m3 年龄由 1.101 回到 2.202"式
**交叉污染确已修好**（r2 序列 0.0 → 1.2 → 2.301 → 0.0 → 1.101 → 0.0 → 1.101）。

### 1.2 绕过尝试（自建反例）

| 绕过 | 结果 |
|---|---|
| 失败后立刻走"未到期"分支（年龄会不会被打回 0） | 否，`age=1.2` 且继续增长 |
| 连续多次失败 | 每次仍 `FAILED`，年龄累加（2.301） |
| 失败与成功交替 | 每次成功换新值并复位年龄，每次失败如实标 `FAILED` |
| `computed_at` 未更新但 `state` 被别的路径改写 | **无第二条写路径**：全仓仅 `service.py:412` 一处写 `shadow_metrics.state`，且只来自 `_refresh_metrics_summary` 返回值 |
| **失败后再成功，缓存是否真换成新值** | 是（注入哨兵 `NEW_VALUE_7f3a` 后 `_metrics_cache.evaluations` 等于哨兵） |
| 页面是否明确告知"这是旧值" | 是，见 §1.3 |

判定：r2 上 **13/13 PASS**；r1 上 5/13 PASS（8 条 FAIL 全为 P2-1 的旧症状）。

### 1.3 页面是否让读者以为是新值（渲染侧独立验证）

`probe_p38_Q.py` 用真实模板 + 合成 `FAILED(age=12.5, last_error=RuntimeError)` 快照，
可见区（去掉全部 `<details>` 折叠块）实测渲染：

```
汇总指标缓存 · FAILED
最近一次聚合失败，当前显示的是 12.5s 前的旧值（RuntimeError），不是本轮新值；下一轮会重试。
缺失样本计数状态 CACHED。
```

→ **页面明确说"旧值/不是本轮新值"**；`CACHED` 态渲染为"未到期，显示 Ns 前成功计算的缓存值"，
`UNAVAILABLE` 态渲染为"尚未成功计算过，不得用 0 冒充「无数据」"。**无误导。**

### 1.4 残留观察（新发现，P3）

失败后若**下一轮未到期**（`allow_metrics=False`），`state` 变 `CACHED`、`stale` **翻回 `False`**，
而 `last_error` 仍为 `RuntimeError`（`DIAG_notdue_after_failure_stale_vs_lasterror`
= `{state: CACHED, stale: false, last_error: RuntimeError, age: 1.2}`）。
**不构成 fail-open**：年龄继续增长且页面把年龄写出来，读者可自判陈旧；
`last_error` 也仍在 JSON 里。**如实登记为 P3**（语义略不一致，非误导）。

---

## 2. 必验② P2-2「披露未渲染」—— **已闭合**（可见区独立判定）

探针：`_scratch_Q/probe_render_Q.py`（真实 `decisions.html` + 真实侧车轮次快照 + 真实
`_decision_observability`；**自定可见区口径 = 去掉所有 `<details>…</details>`**，
比作者口径更严，避免折叠块造成假 HIT）。原始输出 `render_r1.txt` / `render_r2.txt`。

| 探针串 | r1 RAW | r1 **VISIBLE** | r2 RAW | r2 **VISIBLE** |
|---|---|---|---|---|
| `B/C 研究影子行` | False | False | True | **True** |
| `仍会计入` | False | False | True | **True** |
| `方向候选分母` | False | False | True | **True** |
| `尚未按 source 过滤` | False | False | True | False（在折叠块内） |
| `未按 source 过滤` | False | False | True | **True**（残留缺口卡片） |
| `U09` | True | True | True | **True** |

r2 可见区实测两处（原文摘）：

```
影子口径 · LEGACY_ACTIVE
  旧口径 v1（轮转刷新取价，仅研究参考）； 执行权重 0（配置声明不可抬升，已登记冲突）；
  v2 固定时点标签 DEFERRED； 研究样本可作执行候选 否；
  门槛口径由 src/core/release_evidence.py (U09 固定并版本化) 固定。
  注意：B/C 研究影子行**目前仍会计入**发布门禁的方向候选分母 —— 见下方「发布门槛口径」的残留缺口说明。

发布门槛口径 · 残留缺口（已登记）
  研究样本可作执行候选 否； 现行门禁按 signal 过滤、未按 source 过滤：B/C 研究影子行仍会计入「200 方向…
```

外加 evals 表下方的常显脚注："…以上为旧口径研究参考…；**但这些研究行目前仍会计入发布门禁的
方向候选分母（残留缺口，权威属 U09）**…"。→ **共 3 处可见，满足"两处"要求。**

### 2.1 反向检查（不许删掉准确措辞、不许过度承诺）

- **准确性**：`release_gate.note` 逐字未改（仍含"尚未按 source 过滤"），
  `residual_gap.summary` 把同一事实**提到可见区**（"未按 source 过滤"）。✔
- **门禁事实**：我直接读 `src/core/release_evidence.py` 的真实候选查询——
  `SELECT decision_id,created_at,symbol,signal,feature_json,source FROM shadow_decisions
   WHERE release_id=? AND environment=? AND signal IN ('bullish','bearish') AND {candidate_time}`
  **只按 signal 过滤，无 source 谓词**（`source` 仅出现在 SELECT 列与诊断 `source_counts`）。
  披露与代码一致。✔
- **过度承诺扫描**：`不进入发布门槛 / 不会计入 / 已按 source 过滤 / 门槛已干净 / 不计入门禁 /
  不影响发布门槛 / 已过滤` —— r2 在 RAW 与 VISIBLE **全部 False**。✔
- **未裁剪既有信息**：原始 JSON 折叠块仍在（8 个 `<details>` 块，含 `wi|tojson`）。✔

### 2.2 口径注记（不判缺陷）

作者的 `test_dashboard_shows_legacy_caliber` 把"可见区"定义为 `html.split(原始JSON marker)[0]`，
该切片**仍包含**前面若干默认收起的 `<details>`，因此其断言
"可见区渲染出 release_gate.note 全文"是靠**折叠块**通过的（我的严格口径下 note 全文
`VISIBLE=False`）。**这不构成假 HIT**（切片确实排除了 tojson 块），但"可见"一词比字面宽松。
真正兜住该 caveat 的是 `residual_gap.summary` + 「注意」行，二者都在**严格可见区**。

---

## 3. 必验③ 逐条：P2-6 / P3-8 / P3-9 / P3-10 / P3-11

探针：`probe_p38_Q.py`、`probe_p310_Q.py`、`probe_drift_Q.py`、`probe_render_Q.py`。

| 条目 | 判定 | 独立证据 |
|---|---|---|
| **P2-6** `LEGACY_LIMITATIONS` 补条 | **闭合** | r1=6 条 → r2=**7 条**；新增第 7 条含"B/C 研究影子行在现行发布门禁里仍会被计入「200 方向候选」分母…src/core/release_evidence.py 的候选查询只按 signal 过滤、尚未按 source 过滤"。渲染为"旧口径已知缺陷（7 条）"。 |
| **P3-8** 未计算不再显示 0 | **闭合（可达用例）** | 合成快照 4 例：`shadow_evaluation={}` → `24h 事件 —（未计算）`；键缺失 → 同；`events_24h=0`（真实 0）→ `24h 事件 0`（正确）。r1 四例**全部渲染 0**。 |
| **P3-9** 渲染 state/age | **闭合** | 可见区出现 `汇总指标缓存 · FAILED` 与 `12.5s`、`旧值`；`CACHED/REFRESHED/UNAVAILABLE` 三态各有专属文案。 |
| **P3-10** `sample_loss_state` | **闭合（附 P3 注记）** | 状态机 `REFRESHED / CACHED / UNAVAILABLE` 实测：首轮 REFRESHED；底层查询抛错时 `_sample_loss` 逐源标 `state=UNAVAILABLE, reason_unavailable=RuntimeError`（**不用 0 冒充**）；未到期且已有值 → CACHED；从未算过 → UNAVAILABLE。r1 无 `sample_loss_state` 键（KeyError），确认为新增。 |
| **P3-11** `gate_constants_source` + 漂移测试 | **闭合，且漂移测试非空转** | 披露新增 `gate_constants_source`（"字面量副本（shadow_status 不 import core.release_evidence）…漂移由 …test_gate_caliber_agrees_with_release_evidence 检测，不是运行时读取"）。 |

### 3.1 漂移测试**非空转**（重点，独立证伪式验证）

`probe_drift_Q.py` 在运行 `test_gate_caliber_agrees_with_release_evidence` 前**篡改 U09 真正消费的常量**
（`core.release_evidence.DEFAULT_THRESHOLDS`，被 `release_evidence.py:318/455/461` 实际使用）：

```
未篡改               : PASS=11 FAIL=0
DEFAULT_THRESHOLDS.min_directional_candidates 200→250 : PASS=10 FAIL=1
   failures=['披露的 200 候选阈值与 release_evidence 一致']
DEFAULT_THRESHOLDS.min_eligible_closed_trades 30→45   : PASS=10 FAIL=1
   failures=['披露的 30 闭合阈值与 release_evidence 一致']
只改披露副本（模拟只改一处）                            : PASS=10 FAIL=1
恢复                                                   : PASS=11 FAIL=0
DRIFTQ NONVACUOUS = True
```

→ **阈值一变必红、副本单边漂移也必红、恢复即绿**。空转变为 False。✔
（`check()` 累计 FAIL；测试文件 `main()` 有 FAIL 即退出码 1，故统一回归能拦住。）

### 3.2 新增残留（P3，cosmetic）

1. **字面 `**` 泄漏进 HTML**：模板可见区出现 `B/C 研究影子行**目前仍会计入**发布门禁的方向候选分母`
   （Markdown 粗体写在 HTML 里，浏览器会原样显示 `**`）。渲染计数 `literal_double_star_visible=2`
   （r1=0）。同源问题也在折叠的缺陷清单第 7 条里（`**B/C 研究影子行在现行发布门禁里仍会被计入**`）。
2. **P3-8 中间态未覆盖**：`shadow_evaluation={"evaluations":[…]}` 但**无** `events_24h` 键时，
   Jinja `Undefined is not none` 为真 → 渲染成空串（既非 0 也非"—（未计算）"）。
   **今日不可达**：`AuditDatabase.intel_observability()` 的返回固定带 `events_24h`
   （`database.py:1429`），故仅登记。
3. `sample_loss_state` 顶层状态在"底层查询抛错"时仍报 `REFRESHED`（逐源已 UNAVAILABLE），
   页面只渲染顶层值。语义上是"本轮刷新过"，非"数据可用"；**JSON 内可还原真相**，不构成误导。
4. `residual_gap.impact` 用词"**观测到的** Δ=+100"——该数字来自上一轮 C 在**合成夹具**上的测量，
   非生产观测。措辞略强，建议改"夹具实测"。**不影响判定**。
5. 分支同时保留了 `cd9c2a4/` 与 `a01d088/` 两个补丁目录，且 `a01d088/README.md` **未写明**
   supersede 关系（我 grep `cd9c2a4|supersed|取代|上一版` 零命中）。后续集成者可能误用旧目录。

---

## 4. 必验④ 边界（11 新增 / 0 修改）—— **成立**

```
git diff --name-status 59660b2 HEAD  → 11 行，全部 "A"，0 行 "M"
git diff --shortstat  59660b2 HEAD  → 11 files changed, 3095 insertions(+)
git diff --name-only --diff-filter=M 59660b2 HEAD → （空）
```
逐文件 `git diff --quiet 59660b2 HEAD -- <f>` 全部 **SAME**：

```
SAME  src/intel/service.py        SAME  src/dashboard/templates/decisions.html
SAME  src/core/service.py         SAME  scripts/smoke_intel_sidecar.py
SAME  scripts/run_offline_regression.py
SAME  src/core/release_evidence.py
SAME  src/core/contracts          SAME  docs/contracts
```

11 个新增文件 = 2 个补丁 README + 6 个 `.diff` + `scripts/smoke_u06a_shadow_isolation.py`
+ `src/intel/shadow_status.py` + `tests/integration/test_shadow_status.py`。
→ 作者"11 新增、0 修改、5 个共享文件 diff 0 行、`src/core/contracts/`+`docs/contracts/`+
`run_offline_regression.py` 零改动"**全部属实**。

---

## 5. 必验⑤ P1-2 是否"只登记不越权"—— **是**

- `src/core/release_evidence.py`：`git diff --quiet 59660b2 HEAD` → **SAME（零改动）**。✔
- **未建第二套 GO**：全仓 `grep -rn "release_readiness|release_gates_passed|ReleaseReadiness|def .*go_decision" src/ --include=*.py`
  排除 `release_evidence.py` 后 **零命中**。✔
- 事实进**披露**：`release_gate.{role_by_source, research_sources, research_eligible=False,
  executed_trade_counted=False, observation_days_counted=False, gate_constants,
  gate_constants_source, residual_gap, note}` —— 全部是**描述性**字段，无判定逻辑。
- **机器可读 `residual_gap`**：`{id: release_gate_counts_research_rows, summary: "…未按 source 过滤…",
  impact: …, authority: "U09（src/core/release_evidence.py）", fixed_here: false, disclosure_only: true}`。✔
- 补丁对 `src/intel/service.py` 只新增**一个** import（`from intel import shadow_status`），
  无网络/LLM 新依赖。✔
- `note` 保留"不建立第二套 GO 判断器"。✔

**判定：P1-2 属 U09 硬前置（范围外依赖），本批只登记不越权。** 不计为本任务缺陷。

---

## 6. 必验⑥ 补丁与全量 delta

### 6.1 `git apply --check`：2 前像 × 2 配置 × 3 补丁 = **12/12 exit 0**

```
base=59660b2  DEFAULT ×3 exit=0   CFG_A(-c core.autocrlf=false -c core.eol=lf) ×3 exit=0
base=a2f09e8  DEFAULT ×3 exit=0   CFG_A ×3 exit=0
```
且两前像的 3 个目标文件 **blob 完全相同**（`59660b2:<f>` == `a2f09e8:<f>`）。
补丁正文为**纯 LF**（字节级：CR=0；`smoke`20/`html`93/`service`407 LF）。
**实应用**（非仅 check）：在 `a2f09e8` 树上 3 条全部落盘，产物与 `59660b2` 树产物 **md5 逐一同**.

### 6.2 全量回归 delta（强制，非 fail-fast）

`probe_delta_Q.py` 读**同一份** harness `SCRIPTS` 清单（81 条），逐条 subprocess，
同 env（`PYTHONUTF8=1`，pop `MTA_STATE_ROOT/MTA_DATA_ROOT/MTA_PROFILE/PYTHONPATH`），记录退出码 + `^\s*FAIL\s+` 失败名。

```
DELTAQ ipristine: total=81 bad_exit=0 scripts_with_FAIL=1
DELTAQ ipatched : total=81 bad_exit=0 scripts_with_FAIL=1   ← 同一支：smoke_u02
badA-badB = []      badB-badA = []
FAIL-set diff A-B = {}      FAIL-set diff B-A = {}
```
官方 harness 两树均：
```
OFFLINE REGRESSION OK scripts=81   (exit 0, 两侧相同)
```
**失败集合差集 = ∅，无任何新增失败。** 关于 `smoke_u02_strategy_position_manager.py` 的 10 行
`FAIL ...`：其 **exit=0**，是脚本**自带的负向对照**（未终止运行），两态完全一致，**不误报**。

### 6.3 smoke 补丁必要性（独立证伪）

`conflict` 树 = ipatched 但 `scripts/smoke_intel_sidecar.py` 回退为前像：
```
AssertionError: B/C影子组同时记录   (exit=1, _scratch_Q/conflict_svc_only.txt)
```
→ 该补丁**确有必要**（旧断言与必达②直接冲突）。补丁只把 `sources == {B,C}` 改为
`sources == {B}`（仍是**精确集合相等**）并把"C 必须写 2 行 unknown"改为"C 一行都不写"，
其余 17 条断言逐字未改。

- `tests/integration/test_shadow_status.py`：ipristine **205/0**，ipatched **264/0**（均 exit 0）。

---

## 7. 必验⑦ 门禁—— **全绿**

在 `git init` 后的 patched 沙箱（523 tracked）上：

```
scripts/check_quality.py        exit=0
  All checks passed!
  PROFILE LOCK OK / DEPENDENCY LOCK OK
  SECRET SCAN OK files=482 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
  QUALITY CHECK OK files=283 untracked=0
scripts/scan_tracked_secrets.py exit=0
  SECRET SCAN OK files=482 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
```
**无新增抑制**：`_REGISTERED_FALSE_POSITIVES` 定义在 `scripts/scan_tracked_secrets.py`
（本任务零改动，与 `59660b2` 逐字相同），补丁只触及 3 个非扫描器文件。

---

## 8. 必验⑧ 回归抽样（5 项，独立复现）

探针 `probe_misc_Q.py`（原始输出 `misc_r2.txt`）：

| # | 项 | 结果 |
|---|---|---|
| 1 | 旧口径权重恒 0（配置写 0.9 不生效） | `snapshot.execution_weight = 0.0`；`disclosure.execution_weight.effective = 0.0`；`conflict=True`，`forced_fields=['execution_weight:non_zero','shadow_only:false']` → **不可抬升** |
| 2 | C 未配置零样本 | 真实轮次 `rows_by_source` 中 `C` 计数 = **0**；`cohorts.C.status=unconfigured, emits_samples=False`；`skipped_emissions=1`。C 配齐（`watched_wallets`+`token_contracts`）后 `should_emit_sample(unknown)=False`、`(bullish)=True`（只挡无效行，不挡合法研究行） |
| 3 | 侧车隔离（AST） | `shadow_status.py` imports = `['__future__','dataclasses','typing']`；禁命中（httpx/requests/openai/anthropic/sqlite3/threading/subprocess/socket/asyncio/concurrent/multiprocessing）= **[]**；危险调用（open/eval/exec/compile/__import__）= **[]** |
| 4 | v2 `DEFERRED` 三处一致 | `disclosure.v2_labels.state` = `legacy_metric_view(...)[0].v2_labels` = `sample_marker(B).v2_labels` = **`DEFERRED`** |
| 5 | 影子不计入门槛的**披露准确性** | `research_eligible=False / executed_trade_counted=False / observation_days_counted=False`；`research_sources=['B','C']`；`role_by_source` 中 B/C=`research_only`、A=`execution_decision_copy`；`note` 含"尚未按 source 过滤"与"不建立第二套 GO"；与真实候选查询（无 source 谓词）**一致** |

---

## 9. 问题清单（本轮）

| # | 级别 | 位置 | 问题 | 证据 |
|---|---|---|---|---|
| N1 | P3 | `decisions.html`（a01d088 补丁）/ `shadow_status.py:104` | Markdown `**` 泄漏进 HTML，可见区显示字面星号（2 处可见 + 1 处折叠） | `literal_double_star_visible=2` |
| N2 | P3 | `decisions.html:139` 补丁态 | `shadow_evaluation` 为真值 dict 但**无** `events_24h` 键 → 渲染空串（今日不可达，`intel_observability` 固定返回该键） | `probe_p38_Q.py` CASE b |
| N3 | P3 | `service.py:_refresh_metrics_summary` | 失败后下一轮"未到期"时 `stale` 翻回 `False` 而 `last_error` 仍在（年龄继续增长，页面写出年龄，不误导） | `DIAG_notdue_after_failure_stale_vs_lasterror` |
| N4 | P3 | 同上 | 底层样本计数查询抛错时顶层 `sample_loss_state` 仍为 `REFRESHED`（逐源已 UNAVAILABLE） | `p310_r2.txt` S2 |
| N5 | P3 | `shadow_status.py` `residual_gap.impact` | "**观测到的** Δ=+100" 实为合成夹具测量，措辞略强 | 上一轮 C §5 |
| N6 | P3 | `docs/dispatch/patches/U06a/` | `cd9c2a4/` 与 `a01d088/` 并存，README 未声明 supersede | grep 零命中 |

**跨任务遗留（已在 r2 README §7 如实登记，非本批可改）：**

| # | 级别 | 项 | 状态 |
|---|---|---|---|
| P1-1 | P1 | `scripts/run_offline_regression.py` 未登记 `smoke_u06a_shadow_isolation.py` → **分支树自身让主门禁 raise** | 已复现：`RuntimeError: 未登记冒烟脚本: scripts/smoke_u06a_shadow_isolation.py`（exit 1）。派发要求**不改**该文件，登记为 I 的原子登记项 `W1-MANIFEST-TRANSIENT` |
| P1-2 | P1 | `src/core/release_evidence.py` 候选查询未按 source 过滤 | 已登记为 **U09 硬前置**（见 §5），本批只披露不修 |

**无 P0；无未登记的 P2 fail-open / 崩溃 / 陈旧当新值 / 误导性披露；无未登记的既有脚本新增失败。**

---

## 10. 命令清单（摘要）

```bash
# 沙箱
git archive 59660b2 | tar -x -C _scratch_Q/sandbox/base
git show e8f0873:src/intel/shadow_status.py > _scratch_Q/sandbox/base/src/intel/shadow_status.py
GIT_CEILING_DIRECTORIES=<sandbox> git apply <patch>            # 逐条 r1/r2
# 边界
git diff --name-status 59660b2 HEAD ; git diff --shortstat 59660b2 HEAD
git diff --quiet 59660b2 HEAD -- <shared_file>                 # ×8 全部 SAME
git diff --name-only --diff-filter=M 59660b2 HEAD              # 空
# 补丁
... git apply --check {DEFAULT|-c core.autocrlf=false -c core.eol=lf} <p.diff>   # 12/12 exit0
# 回归
python _scratch_Q/probe_delta_Q.py _scratch_Q/delta/ipristine ipristine
python _scratch_Q/probe_delta_Q.py _scratch_Q/delta/ipatched  ipatched
(cd _scratch_Q/delta/<t> && python scripts/run_offline_regression.py)             # 两树 exit0
# 门禁
(cd _scratch_Q/gatepatched && python scripts/check_quality.py)        # exit0
(cd _scratch_Q/gatepatched && python scripts/scan_tracked_secrets.py) # exit0
# 探针
python _scratch_Q/probe_metrics_Q.py <tree> ; probe_render_Q.py ; probe_p38_Q.py
python _scratch_Q/probe_p310_Q.py <tree>    ; probe_drift_Q.py ; probe_misc_Q.py
```

## 11. 未覆盖 / 无法离线验证

1. **U09 的 source 过滤**与本任务范围无关，未验证（我只验证"披露与现行代码一致"）。
2. **v2 固定时点标签**属 U06b，未验证。
3. 真机 7×24 下 B/C 行对 200 候选的**累计放大倍数**未测（只验证机制与"披露准确"）。
4. 真实多线程并发注入未做（`maybe_refresh` 不在 I/O 期持 `self._lock` 为上一轮结论，本轮未复跑）。
5. `_render_snapshot` 的 `metrics` 走的是**合成** `FAILED` 态；真实生产进入 FAILED 的概率未评估。
6. 报告中的 `**` 泄漏仅做静态渲染确认，未在真实浏览器截图核对（无头浏览器未启用，避免联网/启服务）。

---

## 12. 结论

**PASS-with-notes。**

- **P2-1 陈旧当新值：闭合。** 修复前（r1 树）独立复现"聚合失败仍报 `REFRESHED/age=0.0/cached=False`"，
  修复后 `FAILED/stale=True/年龄锚在上次成功/last_error`，且**失败后再成功会换成真正的新值并复位年龄**；
  年龄交叉污染已消除；页面明确写"这是旧值，不是本轮新值"。13/13 判定通过，5 类绕过全部未突破。
- **P2-2 披露未渲染：闭合。** 严格可见区（剔除全部折叠块）确认 caveat 出现 3 处；
  原 note 措辞未被删改，"未按 source 过滤"与代码事实一致；过度承诺串全为 False。
- **漂移测试非空转。** 篡改 `DEFAULT_THRESHOLDS` 或单边改副本 → 必红；恢复 → 绿。
- **边界成立。** 11 新增 / 0 修改；共享文件、契约、`release_evidence.py`、`run_offline_regression.py` 全部零改动。
- **P1-2 只登记不越权。** `release_evidence.py` 零 diff，全仓无第二套 GO，事实进披露 + 机器可读 `residual_gap`。
- **全量 delta ∅。** 81 脚本两态退出码与失败集合完全一致；官方 harness 两侧 `OFFLINE REGRESSION OK scripts=81`。
- **门禁全绿。** `check_quality` / `scan_tracked_secrets` exit 0，且未新增任何误报抑制。
- **新发现均为 P3**（N1–N6），无 P2 及以上新增缺陷。
- 两条 P1（`W1-MANIFEST-TRANSIENT` 登记行、U09 source 过滤）**保持登记**，属 I/U09 的硬前置，非本任务缺陷。
