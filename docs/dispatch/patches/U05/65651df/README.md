# U05 共享适配补丁包（作者 C-U05 / 交 I 应用）— r3（B-r1/r2 修复轮）

本目录**取代** `docs/dispatch/patches/U05/8a8e31e/`（r2 包，已从工作树删除；git 历史可回溯）。

- **补丁基线 = 集成 HEAD `78526d7`**（已含 U01/U03/U04/U13/U15；`OFFLINE REGRESSION OK scripts=75`）。
  15 个 diff 的上下文与 blob 哈希都取自该提交，**不是** `0e522b1`/`8a8e31e`。
- 目录名 = **引入本包的 U05 分支提交 `65651df`**（r3 内容提交 = r3 完成时的 `task/U05` head）。
  U05 分支侧**不携带任何共享文件**（见下），故没有"U05 侧的树"可作补丁基线；基线一律以集成 HEAD 为准。
- 本包上下文 = `task/U05` head（r3 修复提交）。该提交只改作者**独占**路径
  （`src/core/decision_trace.py`、`tests/integration/test_{decision_trace,model_route_safety,
  background_refresh_budget,cross_process_llm_admission}.py`、`scripts/smoke_u05_*.py`），
  15 个共享文件逐字节未被触碰。
- **由 I 在集成时应用**；单个补丁不得脱离本包其余部分单独合入（批次约束见 §二）。

## 〇、r3 相对 r2 的差异（只有 13/14/15 三条内容变化 + 全包重出为"行尾安全"）

| 补丁 | r3 变化 |
|---|---|
| `13_secret_scanner_false_positive_ki01.diff` | **P1-5 残留闭合**：放宽条件由"形状像标识符"收紧为"**该名字在本模块 AST 符号表里确实被绑定过**"；顺带修 N5（右括号紧贴形态）。详见 §三 |
| `14_secret_scanner_tests.diff` | 13 的伴随（必须同批）：断言随新口径改写，PASS 26 → **PASS 36**；PASS 计数改为**运行时统计**（不再写死） |
| `15_run_offline_regression_scripts.diff` | 上下文更新到集成 HEAD（U04/U13 已先登记两条冒烟），U05 四条插在 U01 之后 |
| `01`–`09`、`11`、`12`、`16` | **代码内容与 r2 完全一致**，仅按集成 HEAD 重出（上下文/`index` 行） |

**全包重出的原因是 B-r1 N1（补丁可应用性）**：见 §四。

## 一、补丁清单（15 个）

| 补丁 | 目标文件 | r3 状态 | 用途 |
|---|---|---|---|
| `01_llm_factory_route_expectation.diff` | `src/llm/factory.py` | 内容同 r2 | `route_expectation()` + `mock_explicitly_allowed()`：`mock_allowed` 只来自显式进程开关 `MTA_ALLOW_MOCK_LLM`（缺省 False），不再由"取不到 key"反推（P1-3）；Mock 分支标 `degraded_reason`/`route_role` |
| `02_llm_failover_declared_models_observer.diff` | `src/llm/failover_client.py` | 内容同 r2 | `declared_models`/`attempt_observer`/`_mta.deadline_*`；主备共享同一绝对期限 |
| `03_llm_openai_client_route_declaration.diff` | `src/llm/openai_compatible_client.py` | 内容同 r2 | `route_declaration` + `attempt_observer` |
| `04_core_agent_recorder_decision_trace.diff` | `src/core/agent_recorder.py` | 内容同 r2 | 落 `latency_ms=trace.elapsed_ms()`（真实耗时）；工件与 `agent_runs` 两条写入路径都受保护（P2-5） |
| `05_core_graph_shared_deadline.diff` | `src/core/graph.py` | 内容同 r2 | 本轮共享绝对期限；`aux_deadline()` 收紧为子切片 |
| `06_core_intel_cache_bounded_refresh.diff` | `src/core/intel_cache.py` | 内容同 r2 | `refresh_bounded()`/`source_cache` |
| `07_llm_request_coordinator_lease_recovery.diff` | `src/llm/request_coordinator.py` | 内容同 r2 | 可选 `lease_registry` 崩溃回收 |
| `08_llm_workbuddy_route_declaration.diff` | `src/llm/workbuddy_client.py` | 内容同 r2 | `route_declaration`/`declared_fallbacks`/`attempts` |
| `09_audit_database_decision_trace_summary.diff` | `src/audit/database.py` | 内容同 r2 | 列语义修正（P1-1）：`latency_ms` 只写真实耗时或 `NULL`；`failed_rounds` 随 `output_summary` 落库；docstring 不再承诺"工件进独立列" |
| `11_data_macro_public_reason.diff` | `src/data/macro_public.py` | 内容同 r2 | `reason_from_errors(errors, *, degraded=False)`：删 `len(errors) < 8` 魔法常量（P2-6） |
| `12_data_onchain_public_reason.diff` | `src/data/onchain_public.py` | 内容同 r2 | 同 11（删 `len(errors) < 6`） |
| `13_secret_scanner_false_positive_ki01.diff` | `scripts/scan_tracked_secrets.py` | **已改（r3）** | P1-5 收紧到"须真被绑定"；N5 修复；登记表内容指纹双锚（P2-9）；放宽留痕 |
| `14_secret_scanner_tests.diff` | `scripts/test_secret_scanner.py` | **已改（r3）** | 13 的伴随（必须同批） |
| `15_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **已改（r3）** | 登记 4 个 `scripts/smoke_u05_*.py`（必须与 4 个冒烟同批） |
| `16_smoke_llm_failover_mta_keys.diff` | `scripts/smoke_llm_failover.py` | 内容同 r2 | 02 的伴随（必须同批） |

> `10_data_market_query_reason_from_quality.diff` 已撤回、不交付 —— 见 §二.4（**显式偏差**）。

## 二、批次约束（必须遵守）

1. **13 必须与 14 同批**；**02 必须与 16 同批**；**4 个 `smoke_u05_*.py` 必须与 15 同批**
   （脚本一落库，`_validate_smoke_manifest()` 即以"未登记冒烟脚本" exit≠0）。
   4 个冒烟脚本是 U05 **独占**文件（在 `task/U05` 分支，不在本包内），集成时需一并取用。
2. **`04` 依赖 `09`（r1 的"01–09 之间无互相依赖"陈述有误，已更正）**：`record_decision_trace`
   调用 `self.db.log_decision_trace_summary`，只应用 04 时抛
   `AttributeError: 'AuditDatabase' object has no attribute 'log_decision_trace_summary'`。
   **04 与 09 必须同批**。其余 01–09/11–16 之间无互相依赖。
3. **无迁移 / 无 schema 推进**：09 复用既有 `agent_runs` 表；15 个 diff 对 `schema_migrations` /
   `current_schema_version` / `range(1, N)` 零命中，因此不需要任何伴随断言补丁。
4. **`10`（`src/data/market_query.py` 的 `reason_from_quality`）已撤回 —— 显式偏差。**
   约束真实且比 r1 陈述更强：U03 独占测试用 `resolve_patch_states()` 读现网 `market_query.py`
   正/反向应用内嵌 diff 重建两态，改动即 `ValueError: patch context mismatch at line 100`；
   纯 EOF 追加虽能过反向应用，却会让集成树的身份断言 `apply(前像, 补丁) == 现网` 失败。
   **未落地的具体影响**：`src/data/market_query.py` 侧适配缺失，11/12 给
   `macro_public`/`onchain_public` 加的 `reason_from_errors` 与
   `core.decision_trace.reason_from_source_errors` 在生产 `src/` **零调用方**；
   U08 接漏斗时需自行把 market 源错误串传给 `reason_from_source_errors`（替代路径已作为公开 API 存在）。

## 三、r3 修复对照（原始输出见交付报告）

### 3.1 P1-5 残留闭合（**代码位置的裸明文凭据不再漏报**）

r2 的放宽条件是"**形状**像标识符/属性链"——这不足以判定"变量引用"：`api_key = <40 位字母数字>`
是完全合法的 Python（运行期才 `NameError`），于是"把 `.env` 内容粘进 `.py`"这类
**代码位置**的裸明文凭据被静默放行（B-r1/r2 P1-5，且 r2 源码注释给出的理由不成立）。

r3 的口径：两条**同时**成立才放宽——
1. 该名字在本模块的 **AST 符号表里确实被绑定过**
   （赋值目标 / 增强赋值 / 海象 / `for`·`with ... as` 目标 / 函数参数 / `def`·`class` 名 /
   `import` 绑定名 / `except ... as` / `global`·`nonlocal`）；
2. 该出现位置在**真正的代码里**（`tokenize` 判定的注释/字符串区间之外）。

前后对照（同一探针，三份实现直载；原始输出 `_scratch_author_r4/raw_out_r3/probe_p15_contrast.txt`）：

| 用例 | pristine | r2 | **r3** |
|---|---|---|---|
| `.env` 单行粘进 `.py`（赋值形态） | 报 | **不报** | **报** ✅ |
| `.env` 多行（key + secret）粘进 `.py` | 报 2 条 | **不报** | **报 2 条** ✅ |
| 未加引号 64 位十六进制 key（赋值） | 报 | **不报** | **报** ✅ |
| `.py` 顶层 YAML 行 | 报 | **不报** | **报** ✅ |
| 模块层裸键 JSON 块 | 报 | **不报** | **报** ✅ |
| 对照：加引号的同一值 | 报 | 报 | 报（不回退） |
| 对照：注释里的同一值 | 报 | 报 | 报（不回退） |
| 对照：真变量引用（名字在别处被绑定） | 报(误报) | 不报 | **不报** ✅ |
| 对照：N5 右括号紧贴的实参（名字被绑定） | 报(误报) | **报(误报)** | **不报** ✅ |
| 对照：属性链（根名被绑定） | 报(误报) | 不报 | 不报 ✅ |
| 对照：语法错误的 `.py`（fail-closed） | 报 | 报 | 报（不回退） |

**N5（右括号紧贴）已修**：裸 token 先剥掉尾部分隔符（`)]},;:`）再取根名，
`build(<kw>=<name>)` 与 `build(<kw>=<name>, ...)` 现在同样被识别为变量引用。

**残留（见 §五.3）**：若明文凭据与**同一文件里某个已绑定名字**逐字同形，仍会被放行。

### 3.2 P1-1 / P1-3 / P1-2 / P1-4

与 r2 相同，未回退（r2 已由 Q 独立判 CLOSED）：`failed_rounds` 不占耗时列（P1-1）、
门禁以 `decision_gate()`/`report.ok` 为准（P1-2）、`mock_allowed` 只来自显式开关（P1-3）、
span 违规不击穿保护/对账（P1-4）。

## 四、`git apply` 的行尾前提（B-r1 N1 —— **必读**）

仓库 blob 是**混合行尾**（同一文件里 CRLF 与 LF 混排，如 `src/llm/factory.py` = 34 CRLF / 101 LF，
`src/data/macro_public.py` = 纯 CRLF），而 r2 的补丁是**纯 LF**，因此 r2 的 "15/15 OK" 依赖一个
**未声明**的前提 `core.autocrlf=true`。r3 的补丁**保留目标 blob 的行尾**（上下文行该 CR 就带 CR），
并按下表实测（原始输出 `_scratch_author_r4/raw_out_r3/apply_matrix.txt`）：

| 场景 | 工作树行尾 | `git apply --check` |
|---|---|---|
| 1 `-c core.autocrlf=false -c core.eol=lf` | 原样（混合） | **15/15 OK** |
| 2 **集成工作区：仓库自带 `core.autocrlf=false`，plain 无 override** | 原样（混合） | **15/15 OK** |
| 3 默认（本机 system gitconfig `core.autocrlf=true`，plain） | 原样（混合） | **15/15 OK** |
| 4 `-c core.autocrlf=true` | 原样（混合） | **15/15 OK** |
| 5 默认 / `-c core.autocrlf=true` | 全 CRLF（autocrlf=true 检出后） | 10/15；加 `--ignore-whitespace` 后 **15/15 OK** |
| 6 `-c core.autocrlf=false` | 全 CRLF | 2/15 |

**结论（写入本 README 的强制前提）**：
- ✅ **`core.autocrlf=false` 下 plain `git apply` 15/15 通过** —— 集成工作区
  （`multi-agent-trader/.git/config` 与 `mat-wt/I` 的 `core.autocrlf` 均为 `false`）正是该取值，
  **plain apply、无需任何额外参数或 `--ignore-whitespace`**。
- 工作树若是**原样混合行尾**（`git archive|tar -x`、或 `autocrlf=false` 的正常检出），
  场景 1–4 **全部 15/15**，与仓库 `core.autocrlf` 取值无关。
- 只有当工作树被**整体 CRLF 化**（`autocrlf=true` 检出）时才需追加 `--ignore-whitespace`
  （场景 5，15/15）；`autocrlf=false` + 全 CRLF 工作树（场景 6）是自相矛盾的组合，**不支持**。
- 根本原因是仓库既有混合行尾（**非 U05 引入**）；整仓行尾归一属另一任务。

## 五、未接线 / 已知限制 / 已知残余（如实登记）

### 5.1 生产未接线（同 r2）

U05 的运行时 API 在生产 `src/` **零调用方**（只在 U05 独占测试与冒烟包装里出现）：
`run_protection_first`、`classify_cycle`、`classify_evidence_report`、`reason_from_source_errors`、
`background_refresh`、`ModelRouteGuard`、`LeaseRegistry`、`deadline_violations`、
`begin_decision_trace`/`record_decision_trace`、`graph.model_deadline()`、11/12 的 `reason_from_errors`。
即：计数分离 / p95 / 原因码 / 保护优先目前**在真实跑批里一个字节都不落**。
**应归属**：漏斗与决策主流程接线属 **U08**；轨迹落审计与共享期限注入属 **U08/U12** 的 service 装配。
**P2-7（数据侧原因码链路未接线）**同此结论。

### 5.2 需 I 在集成台账里单列的偏差

`10`（`market_query.py`）撤回 = 任务卡"新增共享适配"清单里该项**未交付**，替代路径
`core.decision_trace.reason_from_source_errors` 已作为公开 API 存在（§二.4 已写明影响）。

### 5.3 P1-5 的**保留残余**（刻意保留的误报抑制代价）

**场景**：明文凭据的形状与**同一文件内某个已绑定名字**逐字相同（例如把 `.env` 里的
`api_<下划线>key=<某个已绑定变量名>` 粘进 `.py`）。**可达性与影响**：此时右值与真变量引用在
**纯文本/词法层面不可区分**，脚本若不装载该模块语义就无法判别；这类文件仍会被拦下的途径是
`_REGISTERED_FALSE_POSITIVES` 的显式登记与人工复核。**为何不进一步收紧**：再收紧会把 KI01 的
真实误报（`factory.py` 把裸标识符当关键字实参）重新引入——KI01 是仓库当前的实际形态。
**留痕**：每次放行都计入 `skipped_bare_identifiers`（CD 树上 r3 = 10）并可审计。

### 5.4 B-r1 遗留四项：**登记为"不修"**（复验者 Q-r2 §8/N3 指出 r2 缺登记文字，r3 逐条补上）

| # | 位置（集成 HEAD + 本包 15 补丁后实测） | 问题 | 影响 | 为何不修 |
|---|---|---|---|---|
| **P2-3** | `src/core/decision_trace.py:961`（`blocked: bool = False`），唯一构造处 `:1044` `blocked=False` | `blocked` 恒为 `False`，**无置真路径**，字段无语义 | 调用方若以 `CycleRunResult.blocked` 作为"是否被阻止"的判据，会恒读到"未被阻止"。**当前生产 `src/` 无任何调用方**（未接线，见 5.1），故暂无运行时后果 | 置真需要"谁有权阻止保护/对账"的**策略决定**，属 U08 决策主流程的接线内容；本轮若自行定义判定条件会与 U08 的漏斗语义冲突。**登记为 U08 待接线项** |
| **P3-3** | `src/llm/failover_client.py:58` 回调签名 `(provider, purpose, deadline)` vs `src/llm/openai_compatible_client.py:79` `(attempt, purpose, deadline, status)` | 两个客户端 `attempt_observer` **回调元数不一致**（3 参 / 4 参） | 生产者必须按 client 类型写两套回调；跨 client 复用同一 observer 会拿到错位参数 | **`attempt_observer` 在 `src/` 里只被自身调用**（无外部注入方），改签名没有收益、却会与 U08/U12 的装配约定抢先定型。登记为**待 U08/U12 统一装配时收敛** |
| **P3-4** | `src/core/decision_trace.py:902`（`SpanHandle`） | `SpanHandle` 是**可变** dataclass，`started_ms`/`ended_ms`/`deadline_remaining_ms` 可被任意改写，无守卫；耗时分布理论上可被伪造 | 只有在**同进程内**拿到该句柄的代码主动改字段时才会失真；当前 `SpanHandle` 不跨进程、不落地、无第三方持有 | 改成冻结对象会破坏 `finish()` 的既有写入路径（P1-4 修复依赖该可变性），而防伪造需要"只有 trace 本体可改"的封装，属**接口重构**而非本轮缺陷修复。一致性目前靠事后谓词校验（`span_errors`），**该谓词同样只在库层**（见 5.1） |
| **P3-5** | `src/llm/request_coordinator.py:198-199` | `admit()` 未给 `deadline` 时仍推导 `started + default_queue_deadline_seconds`，**产生第二个期限** | 与"共享绝对期限"（R22）并存时，队列等待可能用一个**新推的**期限而非本轮绝对期限，等待上限与调用方认知不一致 | 收紧为"无 `deadline` 即 fail-fast/拒绝"会**破坏既有调用方**（`admit()` 的 `deadline` 是可选参数，生产默认不传）。R22 的正确落点是**调用方把绝对期限传进来**，属 U08/U12 装配；本轮只登记。**现状**：先到期的那个生效（`remaining <= 0` 即 `LLMAdmissionTimeout`），不会无限等待 |

> 说明：上述四条在 r2 时**只存在于评审报告**，未进入交付 README / RESUME；r3 已逐条登记于此。
> RESUME 按协作约定**只由 I 更新**，作者不代写。

### 5.5 集成树的密钥门禁现状（r3 新增登记，**非 U05 引入**）

在**集成树**（`78526d7`）上跑补丁后的 `scripts/scan_tracked_secrets.py`，失败集合为
**18 条**，且**与打补丁前的 r2 口径逐条相同**（新增 = ∅，修复 = ∅）：

```
docs/dispatch/reviews/U05/04a2c30/B_r1.md:217-222      rule=secret-assignment   (6)
docs/dispatch/reviews/U05/2049c1c/Q_r2.md:60,61,62,66,67,128,187,363          (8)
docs/无人值守升级任务清单_2026-09-19.json:1283,1334    rule=secret-assignment   (2)
docs/无人值守升级任务清单_2026-09-19.json:3209         rule=provider-token, secret-assignment (2)
```

来源是**评审报告与被扫描的任务清单 JSON 本身**（它们逐字引用了被测字符串形状），
属评审/派发流程产物，**与 U05 共享适配无关**；U05 补丁既不新增也不消除它们。
在**不含这些评审文档的树**（`task/U05` 分支树）上，补丁后门禁为：

```
SECRET SCAN OK files=397 registered_false_positives=3 skipped_bare_identifiers=10
              unparsable_python_files=0            (exit 0)
```

（r2 为 `skipped=20`；r3 收紧后 20 → 10，**且失败集合不变**——被取消放行的 10 条
全部落在既有 `_is_placeholder` 抑制面上。）
**建议 I**：要么在集成台账把这几处评审文档登记为已知误报，要么让门禁对
`docs/dispatch/reviews/` 采用独立口径（属另一任务，非 U05 可改）。

### 5.6 补丁包位置（B-r1 N4）

包在 **`mat-wt/CD/docs/dispatch/patches/U05/<本目录>/`**（`mat-wt/I/docs/dispatch/patches/`
下没有 U05 目录）；I 集成时需从 CD 侧取用。

## 六、验证（r3 本批实跑，原始输出见交付报告与 `_scratch_author_r4/raw_out_r3/`）

```
git apply --check（逐条，15 条；矩阵见 §四）
  -c core.autocrlf=false -c core.eol=lf                   15/15 OK
  集成工作区（仓库自带 autocrlf=false，plain）              15/15 OK
  默认(plain, system autocrlf=true) + 原样工作树             15/15 OK
  -c core.autocrlf=true + 原样工作树                        15/15 OK
  autocrlf=true + 全 CRLF 工作树（需 --ignore-whitespace）   10/15 → 15/15
  autocrlf=false + 全 CRLF 工作树（自相矛盾）                2/15（不支持）
全量离线回归 delta（统一清单 = patched 树 79 条，逐条非 fail-fast）
  pristine (78526d7 + U05 独占文件) : 失败 ∅
  + 15 补丁                        : 失败 ∅
  新增失败 = ∅ ；修复 = ∅
scripts/smoke_contracts_v3.py            # PASS 173 / FAIL 0
scripts/test_secret_scanner.py           # PASS 36 / FAIL 0（r2 为 26）
scripts/scan_tracked_secrets.py          # 见 §5.5（CD 树 OK；集成树 18 条既有）
tests/integration/test_decision_trace.py              # 115 / 0
tests/integration/test_model_route_safety.py          # 63 / 0（集成树）
tests/integration/test_background_refresh_budget.py   # 48 / 0
tests/integration/test_cross_process_llm_admission.py # 45 / 0
```

## 七、回退

`git checkout -- <文件>` 即可回退。01–09/11–16 全部是"新增函数 / 新增可选参数 / 新增属性"，
不改既有函数既有分支语义；r2 的行为变化只有三处（05 截断 `aux_deadline()`、09 不再把
`failed_rounds` 写进 `latency_ms`、13 的放宽收窄到"代码位置"）；**r3 只动 13 的判定口径**：
放宽集合进一步缩小（形状 → 须真被绑定）。**回退 13 会让 KI01 误报复现**
（`src/llm/factory.py` 的裸标识符实参），故 13/14 不建议单独回退。
