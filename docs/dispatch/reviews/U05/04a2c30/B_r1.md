# B-U05 初审报告（角色 B 持仓与执行 / 第 1 轮）

- 审查对象：`task/U05` head `04a2c30`（base `0e522b1` = 含 U00/U01/U03/U15 的集成 HEAD）
- 审查者：B（执行绑定 / 风险自述），独立会话，工作区 `reviews/U05/B/r1`（detached，只读）
- 解释器：`multi-agent-trader/.venv/Scripts/python.exe`（3.13.12，`PYTHONUTF8=1`），无 pytest
- 结论：**CHANGES_REQUESTED**
- 说明：本轮**未**复核作者自测计数之外的一切「作者自称」项，全部重新独立跑过；作者报告的自测计数（95/54/48/45）与「delta=∅」我独立复现并确认，但**不作为通过依据**。

原始 stdout 一律落盘于 `_scratch_B/`（`out_*.txt`、`delta_logs/`、`probe_*.py`、`out_probe_*.txt`、`result_*.json`），下文所有结论均可按文件名回溯。

---

## 0. 结论摘要

| 项 | 结果 |
|---|---|
| 四套测试 | 95/0、54/0、48/0、45/0 —— **独立复现一致**（`out_test_*.txt`） |
| 「双态可运行」 | **成立**：四套测试在**未打补丁**树上同样 95/54/48/45 全绿（`out_pristine_*.txt`） |
| 15 补丁 `git apply --check` | **15/15 OK**（独立沙箱，`_scratch_B/sb/patched`） |
| 15 补丁顺序应用 | **15/15 全部成功** |
| 统一清单全量回归 delta | **新增失败 = ∅**（pristine 与 patched 失败集合均为 `{smoke_windows_service_plan.py}` = KI02） |
| `registered_false_positives` 机制本身 | **安全**（精确三元组 + 双向 fail-closed） |
| patch 13 另外引入的 `.py` 裸标识符放宽 | **削弱了密钥检测**（P1，见 B1-5） |
| market_query 撤回约束 | **真实存在**（比作者陈述更强，见 §3 与 B2-1） |
| P0 / P1 / P2 / P3 | **0 / 5 / 9 / 5** |

---

## 1. 全量回归 delta（强制项，独立复现）

方法：`git archive HEAD` 出两棵新鲜树 `_scratch_B/sb/{pristine,patched}`，各自 `git init`；patched 顺序应用 15 个补丁；
用**同一份 harness 清单**（patched 树的 77 条，pristine 自带 73 条为其真子集，已验证）、**同一 env**（复制
`run_offline_regression.py::main` 的规则：`EXECUTION_MODE=paper`、剔除 `MTA_PROFILE`、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、剔除 `*BINANCE*/*SENSENOVA*` 的 KEY/SECRET/TOKEN），**逐条非 fail-fast** 跑（`_scratch_B/run_delta.py`）。

```
统一清单条数 = 77
pristine 自带清单条数 = 73 ；pristine 清单是统一清单的子集 ? True

pristine 失败集合: ['scripts/smoke_windows_service_plan.py']
patched  失败集合: ['scripts/smoke_windows_service_plan.py']
新增失败 (patched - pristine): []
修好     (pristine - patched): []

4 个 U05 冒烟在 patched 树的退出码: 全部 0
```

- **delta = ∅，独立确认成立**（`out_delta_unified.txt`、`result_unified.json`、`delta_logs/`）。
- 唯一失败 `smoke_windows_service_plan.py` = **KI02 既有环境项**，非本任务引入。证据：在**真实工作区 r1** 直接跑同样
  报 `ValueError: python_executable 不存在: ...\.venv\Scripts\python.exe`（`out_ki02_r1.txt`），pristine 与 patched
  两棵树亦同 —— 树根下没有 `.venv`，与本批 15 个补丁无关。
- 注意：harness 的 `main()` 是 **fail-fast**（`check=True`）且开头就 `_validate_smoke_manifest()`；我没有用
  `main()`，而是按 `SCRIPTS` 逐条跑，因此绕开了「一旦 manifest 报错就整轮中止」的问题（这一点在 §3 反而被我用来验证批次约束）。

---

## 2. 审查重点逐条的独立结论

### 2.1 R22 共享绝对 deadline —— **基本成立，有两处真实缺口**

| 反例 | 结果 | 证据 |
|---|---|---|
| 中途改写 span | 一致性谓词仍能发现（`DEADLINE_MISMATCH`），但 `SpanHandle` 是**可变 dataclass，写入无任何守卫**；改 `started_ms`/`ended_ms` 可任意伪造耗时且不触发任何违规 | `out_probe_A.txt` A5 |
| 嵌套 span | 正常；`slice()` 只收紧不延长（`min(parent.deadline, now+seconds)`） | A8 |
| 并发下共享对象被改写 | `DeadlineBudget` 是 frozen，改写抛 `FrozenInstanceError`；4 线程 × 50 span 无异常 | A8、A9 |
| `shared()` 同一对象 / 3 span 反推同一绝对期限 | **成立**：`shared() is b` = True，`deadline` 与 `now` 无关；3 span 绝对期限 spread=0，`deadline_violations`=() | A1、A2 |
| 偷用 `now+timeout`（第二个期限） | **被发现**：`('DEADLINE_MISMATCH:1000ms',)` | A3 |
| 越界 → OVERRUN | **被发现**：`('DEADLINE_OVERRUN:model_decision',)` | A4 |
| deadline 恰等于现在 / 已过期 / 负剩余 | `remaining==0` 时 `expired=True`，`check()` 与 `attempt_timeout()` 都抛 `DeadlineExceeded`；负剩余使 `span_violations` 报 `NEGATIVE_DEADLINE_REMAINING` | A6 |
| **`attempt_timeout` 本身越界** | **反例成立**：余 0.100s、`attempts_left=4` → 返回 **0.250s**（`MIN_ATTEMPT_SECONDS` 地板），即单次尝试被授予**超出本轮剩余**的超时 | A7 |

→ 缺口 1：`DeadlineBudget.attempt_timeout()`（`src/core/decision_trace.py:442-454`）的返回值可 > `remaining_seconds()`，
与「所有尝试共享同一绝对期限」的自述存在张力。实际约束靠调用方把 `deadline` 透传给 `_call(deadline=)`（02 补丁做到了），
所以**行为上**仍受 `deadline` 保护，但 `attempt_timeout` 的语义应当写明「这是建议值，可能大于剩余」或直接 clamp。
→ 缺口 2：一致性判据是**事后**谓词，`SpanHandle` 全程可变（见 2.4 的 P3）。

### 2.2 LLM 失败不阻塞保护与对账 —— **正常路径成立；期限耗尽路径被击穿（P1）**

- 正常：`order = ['protection','reconciliation','model']`；模型失败时保护/对账仍 `ok=True`，`blocked=False`
  （`out_probe_B.txt` B1/B2）。顺序恒定为 protection→reconciliation→model，与自述一致。
- 模型**卡住**（sleep 3s 不抛错）：保护仍先行（`order.index('protection') < order.index('model')`），
  但 `run_protection_first` **没有任何 deadline/超时参数**（签名仅 7 个参数，无 deadline），整轮返回被模型拖住 3.0s
  —— 保护本身不被延迟，但**这一轮不会结束、下一轮不会开始**（B3）。
- **P1 反例（新发现，测试未覆盖）**：budget 已耗尽时，span 的 `deadline_remaining_ms<0` 被判违规，
  异常从 `with trace.span(...)` 的 `finally` 抛出并**击穿整个 `run_protection_first`**：
  ```
  B5: !! 抛出 SpanValidationError: invalid span: NEGATIVE_DEADLINE_REMAINING
      已执行的步骤: ['protection']      # 保护步已产生副作用
  ```
  即：保护步**已经执行**（有副作用）却拿不到结果；对账与模型**根本没跑**；调用方只收到异常。
  这与交付要点 3「LLM 失败不阻塞保护和对账」的精神直接冲突，且是「静默半执行」形态。
  相关代码：`DecisionTrace.span` 的 `finally` → `_append` → `span_violations`（`src/core/decision_trace.py:698-724`、
  `src/core/contracts/k05_trace.py:112-118`）。
- `blocked` 字段：`run_protection_first` **恒返回 `blocked=False`**（`src/core/decision_trace.py:921`），
  dataclass 默认也是 `False`，实现里**没有任何置真路径**；测试还显式断言 `result.blocked is False`。
  因此该字段是常量，不携带信息（P2，见 §4）。反例「blocked 置真而保护未跑」**不可能发生**。

### 2.3 原因码 —— 九码一致、主/辅因正确；**与 U03 的衔接有实质缺口（P1）**

- 九码与任务卡**完全一致**（`任务卡九码 == REASON_CODES ? True`，无缺失无多余）；`REASON_ORDER` 长度 9（C1）。
- 未知码 `UnknownReasonCode`；主因唯一（结构上只有一个槽位，「两个主因」不可表示）；辅因去重且不与主因重复；
  主因选取**与入参顺序无关**（按 `REASON_ORDER` 优先级）：`["HOLD_EXISTING","NO_EDGE"]` 与反序都得 `NO_EDGE`（C3/C4）。
- 空原因：`DecisionReason(primary="")` → `UnknownReasonCode(('',))`；`classify_cycle()` 无入参 → `NO_EDGE`（C2/C5）。
  但 `normalise_reason_codes([""])`/`(["   "])` **静默丢弃、不抛错**，与其 docstring「空码与未登记码都拒绝」不符（P3）。
- **P1（U03 衔接）**：`classify_evidence_report` 的 docstring 声称消费 `decision_gate()`、`rejected_intent_ids`，
  但实现**只遍历 `intents`**，源码里既没有 `.ok` 也没有 `decision_gate`（D4 实测 `源码含 '.ok': False`）。
  反例：
  ```
  D1: report.ok = False, accepted_intent_ids=('i1',), decision_gate()['ok']=False
      classify_evidence_report -> {'primary': 'NO_EDGE', ...,
                                   'details': ['evidence_gate_ok']}
  D5: identity_pinned=False, ok=False  ->  同上（NO_EDGE + evidence_gate_ok）
  D2: ok=False 且 intents=() 而 rejected_intent_ids=('ghost-1',)  ->  NO_EDGE
  D3: 只有非 OPEN 意图被拒（report.ok=False） -> NO_EDGE + details='evidence_gate_ok'
  ```
  即「门禁整体失败」被读成 `NO_EDGE`（"有证据但没有可下单的边际"）+ details 声称 `evidence_gate_ok`，
  与 `report.ok=False` **直接矛盾**。这是 U03↔U05 接缝上最重的问题：原因码是 U05 的核心交付物，此处会产出**错误的审计结论**。

### 2.4 计数分离 —— `failed_rounds` 与请求计数确实正交；有三处「名不副实」

- 反例「5 请求 / 0 失败轮次」与「1 失败轮次 / 3 请求」**未被合并**（§任务书要求验证的两类）：
  ```
  cycles_total  A=1  B=1      api_attempts_total A=5 B=3
  requests_failed A=0 B=1     failed_rounds    A=0 B=1
  ```
  A 读作「1 轮、5 次请求、0 失败轮次」，B 读作「1 轮、3 次请求、1 失败轮次」——**语义独立成立**（B7）。
- 但：`requests_total` **恒等于** `api_attempts_total`（`summary()` 里 `requests_total=counters.api_attempts_total`，
  `src/core/decision_trace.py:765`），两个名字一个值 → 「四类分开」有一类是装饰性的（P2）。
- `rounds_total = len(self._outcomes) or counters.cycles_total`（`:769`）：**两个来源混合**。反例：
  登记了 4 个 cycle span 但一次 `finish_cycle` 都没调用 → `rounds_total=4, failed_rounds=0`
  （读作「4 轮 0 失败」，实际没有任何轮次结局被登记）。模块在 `percentile_ms` 上刻意用 `None` 避免「空即 0」，
  这里却出现了「空即 0」的不一致（P2，B8）。
- 并发下 `requests_failed` 用 `+=` 非原子（`:713-717`）：4 线程 × 300 次失败 span 实测 1200/1200 **未丢**
  （GIL 下未复现），但这是**未复现 ≠ 无风险**；`_spans`/`_outcomes` 同样无锁（B6）。如实标注为「未复现」。

### 2.5 p95/p99 —— **与手算一致**

nearest-rank `ceil(q*n)-1` 无插值，6 组手算全部 match（n=1、n=2（取低中位）、n=3、n=10、n=20、q=1.0）；
空样本返回 `None`（不是 0）；`q=0` 抛 `ValueError`；`stage_latency_stats([])` 返回 `{}`；未结束 span 不参与分位（B9）。
`0.95*20` 在 IEEE754 下恰为 `19.0`，无浮点越界。**小样本（n<100）正确**。

### 2.6 S02–S04 四类路由风险 —— 逐条有显式判定与反例；**但静默 Mock 的判定被自身抵销（P1）**

| 风险 | 显式判定 | 反例证据 |
|---|---|---|
| SILENT_MOCK（客户端） | ✅ | `observe_attempt(client_name="mock")` → `['SILENT_MOCK']`（D7） |
| SILENT_MOCK（响应体） | ✅ | `provider="mock"` / `model="mock"` / `raw["mock"]` / `_mta["mock"]` 四种形态（D7） |
| EARLY_RETURN | ✅ | `expect_attempt` 后 `finish_call(returned=True)` 且无尝试 → `['EARLY_RETURN']`（D7） |
| PARAMETER_INHERITANCE | ✅ | 响应模型 ≠ 期望且未声明 → 违规；`declared_fallbacks` 命中则不报（D7） |
| CROSS_PROCESS_QUOTA | ⚠️ 部分 | 只有显式传**空** `provider_scope` 才会触发；`observe_attempt(span=...)` 路径把 `provider_scope` 直接设为 `request_id`（`:1015-1016`），**恒非空 → 该路径永不触发**（D6，`clean=True`） |

- **P1（S02 核心反例）**：缺 key 时
  ```
  F1: build_llm_for_agent -> MockLLMClient, model='mock', degraded_reason='missing_api_key'
      （**没有报错、没有阻断**，只是多了可观测属性；src/llm/factory.py:97-101）
  F2: route_expectation -> model='m1' mock_allowed=True require_cross_process=True
      （src/llm/factory.py:56：mock_allowed=(not api_key or not model or model in ("","none"))）
      随后收到 mock 响应 -> violations = []   ← 守卫报「干净」
  ```
  `mock_allowed` 与「降级条件」**同源同条件**，因此守卫在结构上**不可能**发现它所针对的那次静默降级。
  这正是任务书 S02 要防的形态。作者 README 称这是「显式声明」，但「由同一个条件自动推出」不是显式声明。
  修复方向：`mock_allowed` 必须来自与本轮「是否允许 Mock」无关的、调用方/上级 profile 的显式开关
  （U15 的 Demo/live 门禁），而不是从「取不到 key」反推；或让守卫在 `degraded_reason` 存在时**无条件**上报。
- 早返回/参数继承的适配器侧声明齐备：03 的 `route_declaration`（含脱敏 `provider_scope`）、
  08 的 `declared_fallbacks=()` + `attempts` 计数、02 的 `declared_models`。有一处接口不一致见 §4 P3。

### 2.7 原因码 `MODEL_DEADLINE` 同码等缺口 —— 见 §5 严重度判定

---

## 3. 补丁审读：15 条逐条意见

> 基线：`git archive HEAD`（= 04a2c30）→ `_scratch_B/sb/patched`，15/15 `--check` OK、15/15 应用成功。

| # | 文件 | 意见 | 级别 |
|---|---|---|---|
| 01 | `src/llm/factory.py` | 新增 `route_expectation`；Mock 分支加 `degraded_reason`/`route_role`；failover 传 `declared_models`。**问题**：`mock_allowed`（`:56`）与 Mock 降级条件完全同源 → 守卫被抵销（见 2.6 P1）。`build_llm_for_agent` 仍**返回 Mock 而不报错**（`:97-101`），S02「缺 key 是否静默降级」的答案是「仍然静默，只是可观测」。 | **P1** |
| 02 | `src/llm/failover_client.py` | `declared_models` / `attempt_observer` / `_mta.deadline_monotonic`+`deadline_remaining_ms`；主备共用一个 `deadline` 参数透传（`_call(deadline=)`），**R22 在本层成立**。`_annotate` 只增本系统命名空间字段，不碰供应商协议。 | OK |
| 03 | `src/llm/openai_compatible_client.py` | `route_declaration`（含脱敏 `provider_scope`）与 `attempt_observer`；每次 HTTP 尝试回调，观测器异常被吞（旁路不阻塞主流程）—— 正确。`_notify_attempt` 在 `remaining<=0` 时以 `status="deadline"` 上报后再抛，语义清晰。**问题**：回调元数 `(attempt, purpose, deadline, status)` 与 02 的 `(provider, purpose, deadline)` 不一致（P3）。 | OK（1 处 P3） |
| 04 | `src/core/agent_recorder.py` | `begin_decision_trace`/`record_decision_trace`：四类计数分开、分阶段 p95/p99、逐轮结局。**问题 1**：`record_decision_trace` 硬依赖 `db.log_decision_trace_summary`（只由 **09** 提供）→ **04 依赖 09**，README §二.5「01–09 之间无互相依赖」不成立（实测：只应用 04 时 `AttributeError: 'AuditDatabase' object has no attribute 'log_decision_trace_summary'`，`out_n09b.txt`）。**问题 2**：docstring 称「落库失败只标 `audit_error`，不影响决策主流程」，但 `try/except` 只包住 `save_artifact`，**db 写入未被保护**，异常直接抛给调用方（P2）。 | **P2** |
| 05 | `src/core/graph.py` | `shared_budget` + `model_deadline()` + `aux_deadline()` 截断。方向是**收紧**无误：`slice()` 只在父更紧时收紧。**已核实调用点语义正确**：`build_graph` 在 `service.py:880`（`_run_once_inner` 内）**每个决策轮重建一次**，所以 `shared_budget` 随轮新鲜，不会出现「一轮建图、期限在后续轮全部过期」的退化。**问题**：`model_deadline()`（`:143` 附近）**零调用方**（见 §5 缺口⑦）。 | OK（1 处 P1 见⑦） |
| 06 | `src/core/intel_cache.py` | 纯新增 `refresh_bounded`/`source_cache`；`get_or_refresh`/`clear` 既有语义不变（`clear` 顺带重置分源缓存，合理）。不改既有分支。 | OK |
| 07 | `src/llm/request_coordinator.py` | `lease_registry` 可选；进临界区前 `reap`、登记/释放租约、超时消息回带 blocker。**动态验证**：死 pid 租约立即回收（`reaped=1`，`read=None`），活 pid 不回收；`coordinator.lease_registry` 正确接入（`out_probe_F.txt` F6）。**注意**：租约 TTL 180s 短于可能的长等待，但真正的互斥仍是 OS 文件锁，租约只作可观测/回收判据，故不构成正确性问题；`admit()` 在未给 `deadline` 时仍以 `now + default_queue_deadline_seconds` **推导第二个期限**（`:189-190`，队列等待用，非模型调用），与「不再重新推导期限」的自述口径需分开陈述（P3）。 | OK（1 处 P3） |
| 08 | `src/llm/workbuddy_client.py` | `route_declaration`（无凭据，作用域为 `workbuddy:<model>#effort:<effort>`）、`declared_fallbacks=()`、`attempts` 计数（可判「返回了却未发出请求」）。动态验证 `route_declaration` 输出正确（F5）。 | OK |
| 09 | `src/audit/database.py` | 复用 `agent_runs`，不新增表、不推进 schema（已复核：15 个 diff 对 `schema_migrations`/`current_schema_version`/`range(1, N)` 零命中，harness 里 `smoke_db_migrations`/`smoke_fault_matrix` 等确实双态都过）。**但列映射错误**：`VALUES` 第 12 个值 `latency_ms` 填的是 `int(failed_rounds)`，第 13 个 `error` 恒 `None`（patched `src/audit/database.py:1731-1734`）。实测：`failed_rounds=3` 的行 `latency_ms=3`、`error=None`。**影响**：`src/agents/review.py:231-237` 用 `AVG(latency_ms)` 按 `agent_name,status` 聚合 `agent_runs`，每日复盘报告会出现 `decision_trace / degraded / n=1 / ms=3.0` —— 把「失败轮次个数」当成毫秒混进共享耗时列（P1）。另：docstring 称「工件路径进 error 之外的独立列」，实际 `artifact_path` 只在 `output_summary` JSON 里，`error` 是 `None`，陈述与实现不符（P3）。 | **P1** |
| 11 | `src/data/macro_public.py` | `reason_from_errors` → `degraded=len(errors) < 8`。**魔法常量**：用「错误条数 < 源总数」近似「部分可用」，源集合增删即静默失准（实测 1 个错 → `DATA_UNAVAILABLE`+`RESEARCH_INCOMPLETE`，8 个错 → 仅 `DATA_UNAVAILABLE`，F3）。且**无任何生产调用方**。 | **P2** |
| 12 | `src/data/onchain_public.py` | 同 11，`degraded=len(errors) < 6`。同一问题。 | **P2** |
| 13 | `scripts/scan_tracked_secrets.py` | 修 `factory.py:111` 裸标识符误报 + 3 处显式登记 + `STALE_ALLOWANCE`。**登记机制安全**（见 §3.1）；**但 `_is_python_code_reference` 放宽削弱检测**（见 §3.1，P1）。 | **P1**（放宽）/ OK（机制） |
| 14 | `scripts/test_secret_scanner.py` | 13 的伴随；断言裸变量/属性链不误报、同一行在非 `.py` 视图仍拦、`.py` 里 `sk-live-...` 裸明文仍拦、登记非空且精确、`stale_allowances() == ()`。实测 `PASS 13 / FAIL 0`；**仅测 13 引入的行为 → 13/14 必须同批成立**：只应用 14 会 `ImportError`（`_REGISTERED_FALSE_POSITIVES`/`stale_allowances` 不存在）。 | OK（批次属实） |
| 15 | `scripts/run_offline_regression.py` | 登记 4 个 `smoke_u05_*.py`。**登记行与实际文件逐一核对一致**（4 个文件、4 行、名字逐一相同）。**批次约束实测成立**：4 个冒烟脚本随 `cc38768` 已入库，未应用 15 时统一入口必然红 —— 我用 pristine 树直接跑 `scripts/run_offline_regression.py`，得到 `RuntimeError: 未登记冒烟脚本: scripts/smoke_u05_{background_refresh_budget,cross_process_admission,decision_trace,model_route_safety}.py`（`out_pristine_harness.txt`）。另：`ci.yml:30` 就是 `python scripts/run_offline_regression.py`，**无需**改 CI。 | OK（批次属实） |
| 16 | `scripts/smoke_llm_failover.py` | 02 的伴随。**批次约束实测成立**：只应用 02 时 `smoke_llm_failover.py` 以 `AssertionError: result["_mta"] == {"provider": "fallback", "fallback_used": True}` 失败（`out_b02.txt`），16 把整字典相等改为「关键事实 + 两个新字段存在」，改后 `LLM_FAILOVER_OK`。 | OK（批次属实） |
| — | `10_data_market_query_reason_from_quality.diff` | **已撤回，未交付**。撤回约束**真实**（见 §3.2），且比作者陈述更强。 | 见 §3.2 |

### 3.1 `registered_false_positives` 专项结论（安全控制）

**机制本身：安全。** 三条登记为 `(仓库相对路径, 行号, 规则名) -> 理由` 的精确三元组，且两个方向都 fail-closed：

- 正向：`scan_tracked_files()` 只剔除**精确命中同一行同一规则**的项，不含糊放行整类规则；
- 反向：`stale_allowances()` 基于**未过滤**的 `raw_hits()`，登记项若不再命中 → `main()` 打印 `STALE_ALLOWANCE` 并 `return 1`（扫描失败），所以「文件一改、登记即失效」。
- 独立复核：pristine 扫描器的 4 处命中与 3 条登记**逐一吻合**（`docs/dispatch/reviews/U00/0b85bef/C_w0r1_r2.md:31`、`tests/acceptance/test_runtime_identity.py:524`、`:708`，均 `secret-assignment`/`provider-token`），第 4 处 `src/llm/factory.py:111 rule=secret-assignment` 正是被 13 的代码放宽修掉的那个（`out_secret_pristine.txt`）。打补丁后 `SECRET SCAN OK files=398 registered_false_positives=3`，`test_secret_scanner.py` 13/13。
（`files=398` 是 `git archive HEAD` 沙箱计数；作者报告 432 应为作者 worktree 含未跟踪文件所致，非差异性问题。）

**但是：patch 13 同时引入的 `.py` 裸标识符放宽**确实**削弱了密钥检测（P1）**。

`find_secret_hits(text, suffix=".py")` 对**裸右值**（未加引号）新增：
```
_PY_CODE_REFERENCE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
python_source = str(suffix).lower() == ".py"      # :102
if python_source and _is_python_code_reference(value): continue   # :114
```
放宽的**理由**是「未加引号的右值在 Python 里只能是表达式，内联明文凭据不加引号过不了语法解析」——对**代码行**成立，
但扫描器是**逐行文本**扫描，不做 AST。因此该放宽同样作用于 `.py` 里的**注释、docstring/三引号串、内嵌的非 Python 片段**，
而那里「未加引号」完全合法。实测（`out_probe_H.txt`）：

| 探针（同一行内容） | 打补丁后 `.py` 视图 | 旧口径 | 判定 |
|---|---|---|---|
| `api_key=fallback_key,`（目标误报） | 不报 | 报 | 修复目标达成 ✅ |
| `# api_key = abcdefghijklmnop1234`（注释里的明文） | **不报** | 报 | **真实盲区** ❌ |
| `NOTE = """api_key = zzQ8wLp4Rt7YbN1w"""` | 报 | 报 | 未被绕过（因尾部引号破坏了 fullmatch） |
| `api_key = zx8k2mQ9vLp4Rt7YbN1w`（字母开头裸 token） | **不报** | 报 | **真实盲区** ❌ |
| `api_key = 9f8e7d6c5b4a3210`（数字开头） | 报 | 报 | 侥幸拦住（正则要求首字符非数字） |
| `api_key: abcdefghijklmnop1234`（YAML 形态出现在 `.py` 内） | **不报** | 报 | **真实盲区** ❌ |

即：**「YAML/JSON 里的裸标量必须继续拦」只在文件后缀不是 `.py` 时成立**；`.py` 里内嵌的 YAML/JSON 模板、配置示例、
以及注释/文档串中的明文凭据，`secret-assignment` 规则不再报，且**没有任何「已跳过 N 条候选」的留痕**（静默）。
`_PROVIDER_TOKEN_RE`（`sk-...`）与私钥/Bearer 规则不受影响，所以 `sk-` 形状仍拦；缺口集中在**通用赋值形态**的凭据。

建议（任一即可，按成本排序）：
1. 仅在**不是注释/字符串上下文**时启用放宽：行首去空白后以 `#` 开头 → 不走放宽；或只对「出现在 `(`/`,` 之后的关键字实参位置」放开放宽；
2. 或者保留放宽但**必须留痕**：把被跳过的候选计数并打印（如 `skipped_bare_identifiers=N`），使放宽可审计；
3. 或把判定改为「该标识符在同一文件内被定义/赋值过」才跳过（真正区分「变量引用」与「粘进来的字面量」）。

另有两处次要问题（P3）：
- 登记项锚在**行号**上，而其中 2 条位于 **U15 的** `tests/acceptance/test_runtime_identity.py`（:524/:708）。
  U15 一旦增删行 → `STALE_ALLOWANCE` → **全仓密钥门禁失败**，这是跨任务的隐式耦合（应改为「内容指纹 + 行号」双锚）。
- 登记只匹配 `(path,line,rule)`，若该行内容日后被换成**真实**凭据（同规则），会被静默抑制。

### 3.2 market_query 撤回的约束 —— 独立确认

**约束真实存在，且比作者陈述更强。** 我在 `_scratch_B/probe_E_market_query.py` 里直接复用 U03 独占测试的严格
unified diff 应用器（不依赖 git 基线），对「补丁后像 + U05 改动」做**反向应用**（集成树里反向还原前像的那条路径），
并同时模拟集成树的**身份断言** `apply(frozen_baseline, patch) == 现网`（`out_probe_E.txt`）：

| 场景 | 反向应用 | 身份断言 |
|---|---|---|
| 不改动（基线） | OK（还原后状态 = `unapplied`） | True |
| line 8 插入 `reason_from_quality`（hunk1 之前） | **`ValueError: patch context mismatch at line 100`** | False |
| line 160 插入（hunk 之间） | **`ValueError: patch context mismatch at line 221`** | False |
| 文件末尾追加 | OK | **False** |
| 文件开头加一行注释 | **`ValueError: patch context mismatch at line 100`** | False |
| 在 hunk 覆盖区域内改内容 | **`ValueError: patch context mismatch at line 299`** | False |

- 作者引用的报错文本 **`patch context mismatch at line 100` 精确复现**（line 8 插入、line 1 注释两个场景）。
- 作者陈述「改动即 ValueError」在**一处偏强/偏弱**：纯 EOF 追加能让**反向应用**通过，但它会让**集成树里的身份断言
  `apply(前像,补丁)==现网` 失败**（因为 `resolve_patch_states` 在 `applied` 态下把 `patched := 现网`）。
  两条路径都通向失败，**结论不变：U05 无法在不弄红 U03 独占测试的前提下改 `src/data/market_query.py`。**
  真实工作区 r1 里 `market_query.py` 已是后像（含 `_source_quality`），provenance 为「反向应用/已应用」。
- **S03 影响判定：可接受（P2），但必须登记为显式偏差。** 理由：
  1. 被撤回的 `reason_from_quality` 的替代路径（`core.decision_trace.reason_from_source_errors`）已作为公开 API 存在；
  2. **但** 11/12 给 `macro_public`/`onchain_public` 加的 `reason_from_errors` 与 `reason_from_source_errors` 本身
     **在 `src/` 里零调用方**（我逐一 grep 过：`reason_from_source_errors`/`classify_cycle`/`background_refresh`/
     `ModelRouteGuard`/`run_protection_first`/`LeaseRegistry`/`begin_decision_trace`/`record_decision_trace` 全部只在测试与冒烟包装里出现）。
     也就是说，即便保留 patch 10，**今天也不会有任何运行时差别**。
  3. 因此撤回的实际代价是：任务卡「新增共享适配」清单里的 `src/data/market_query.py` 一项**未交付**，且 11/12 造成
     「两个数据模块有 helper、market_query 没有」的不对称；U08 接漏斗时需自行把 market 源错误串传给 `reason_from_source_errors`。
  → 建议：I 在集成记录里把该偏差写成正式 `deviation`（含原因与替代路径），而不是仅在补丁包 README 里说明。

---

## 4. 其它问题清单（P2 / P3）

**P2**

| # | 位置 | 问题 | 证据 |
|---|---|---|---|
| P2-1 | `src/core/decision_trace.py:765` | `requests_total` 恒等于 `api_attempts_total`，两名字一值 | B7 `A.requests_total == A.api_attempts_total ? True` |
| P2-2 | `src/core/decision_trace.py:769` | `rounds_total` 用 `or` 混合两个来源；无结局时读作「N 轮 0 失败」（与 `percentile_ms` 的 `None` 口径自相矛盾） | B8 |
| P2-3 | `src/core/decision_trace.py:921` | `blocked` 恒为 `False`，无置真路径 → 字段无语义 | B4 |
| P2-4 | 补丁 04/09 批注 | 04 硬依赖 09，README §二.5「无互相依赖」不成立 | `out_n09b.txt` |
| P2-5 | `src/core/agent_recorder.py`（补丁 04） | 只保护 `save_artifact`，db 写入异常直接外抛，与 docstring「落库失败不影响主流程」不符 | `out_n09b.txt`（AttributeError 直穿） |
| P2-6 | 补丁 11/12 | `degraded=len(errors) < 8` / `< 6` 魔法常量，与源集合耦合 | F3 |
| P2-7 | S03 数据侧原因码 | 11/12 新增的 `macro_public.reason_from_errors` / `onchain_public.reason_from_errors` 在 `src/` 里**零调用方**（它们虽调用 `reason_from_source_errors`，但没有任何地方调用它们）→ 数据侧原因码整条链未接线 | §2.6 grep |
| P2-8 | `src/core/decision_trace.py:442-454` | `attempt_timeout` 可返回 > 剩余预算（0.25s 地板） | A7 |
| P2-9 | 补丁 13 登记表 | 登记锚在别人（U15）文件的行号上，行移位即全仓门禁红；行内容被替换为真凭据会被静默抑制 | §3.1 |

**P3**

| # | 位置 | 问题 |
|---|---|---|
| P3-1 | `src/core/decision_trace.py:1015-1016` | `observe_attempt(span=...)` 以 `request_id` 充当 `provider_scope`（自证清白）→ `CROSS_PROCESS_QUOTA` 在该路径永不触发（D6） |
| P3-2 | `normalise_reason_codes`（`:115-130`） | docstring 称空码拒绝，实现静默丢弃（C2） |
| P3-3 | 补丁 02 vs 03 | `attempt_observer` 回调元数不一致（`(provider,purpose,deadline)` vs `(attempt,purpose,deadline,status)`） |
| P3-4 | `SpanHandle`（`:795-823`） | 可变且无守卫，`started_ms`/`ended_ms`/`deadline_remaining_ms` 可随意改写，耗时分布可被伪造；一致性只靠事后谓词（且谓词无调用方） |
| P3-5 | `src/llm/request_coordinator.py:189-190` | `admit()` 未给 `deadline` 时仍 `now + default_queue_deadline_seconds` 推导第二个期限 |
| P3-6 | 补丁 09 docstring（`out_artifact`） | 称「工件路径进独立列」，实际只进 `output_summary` JSON，`error` 恒 `None` |

---

## 5. 作者自述缺口的严重度判定（任务书第 7 项）

| 缺口 | 我的判定 | 理由 |
|---|---|---|
| ④ 供应商「不可达」与「超时」同码 `MODEL_DEADLINE` | **P3（可接受）** | 实测 `TimeoutError`/`TransportError`/HTTP 503 三类都落 `MODEL_DEADLINE`，但 `details` 保留异常类型名与 `HTTP 503`（C6），审计侧可经 `details` 区分；拆码需改冻结契约（K05 小版本），本层不越权是对的。建议在 §12 把这条写成「已知限制」索引项，而不是只写在模块 docstring。 |
| ⑤ 跨 span 期限谓词落在执行侧（K05 无 `cycle_deadline_ms`） | **P2** | 谓词本身设计正确（`started_ms + deadline_remaining_ms` 是合法反推，A2/A3/A4 都按预期工作），但 `deadline_violations()`/`span_absolute_deadline_ms()` **零生产调用方**，所以「同 deadline 有机器可检查证据」目前只在测试里成立。不阻断，但应记为「未接线」。 |
| ③ `background_refresh` 约束「是否发起」而非「中断在途」 | **P3（可接受）** | 模块 docstring 已**显式声明**该口径边界（"预算约束的是**是否发起**下一次调用，不是**中断已发起的调用**"，`src/core/decision_trace.py:1489-1494`）。越界量有界（最多一次调用耗时），且分源 TTL/让路逻辑（先让路 → 缓存 → 逐源 → 摘要）顺序正确。属如实声明，非隐瞒。 |
| ⑦ `record_decision_trace`/`graph.model_deadline` 无生产调用方 | **P1（加重）** | 我把它**扩大**了：不只这两个符号 —— `run_protection_first`、`classify_cycle`、`classify_evidence_report`、`reason_from_source_errors`、`background_refresh`、`ModelRouteGuard`、`LeaseRegistry`、`EvidenceLink`、`deadline_violations`、`begin_decision_trace` 在 `src/` 里**全部零调用方**（仅测试/冒烟包装引用文件名）。**结论：U05 的运行时行为在生产路径上完全未接线**，R22 的「共享绝对期限」在库层成立（2.1）、生产侧则由**既有的** `orchestrator.py:802`（`deadline = time.monotonic() + total_timeout_seconds`，只在 802 行推导一次并透传给每一轮 `call_chat(deadline=deadline)`，`orchestrator.py:860-863`）以**另一套**期限满足 —— 数值上都是 240s 所以现在看不出差别，但 `graph.model_deadline()` 是**悬空胶水**（定义了不用），计数分离/p95/原因码在真实跑批里**一个字节都不落**。这不是「文档未更新」，是交付物与集成目标之间的实质落差。 |

---

## 6. 未覆盖 / 无法离线验证的项

1. **真实供应商与网络**：未联网，`_mta.deadline_*` 在真实 Failover 往返中的取值、`Retry-After` 冷却与备用额度的真实交互未验。
2. **真实宿主 / Windows 服务 / 跨进程真并发**：S04 的租约与回收只在**单机进程 + 假 pid** 下验证（死 pid 用 999999 模拟）；
   `owner_alive()` 的 Windows 分支（`OpenProcess`+`WaitForSingleObject`）只在 `os.name=="nt"` 下跑到，
   我没有构造「真进程被 kill -9」的跨进程场景。
3. **`smoke_windows_service_plan.py`（KI02）**：因缺 `.venv` 无法判定其真实失效原因是否只与环境有关，只证明「与 U05 无关」。
4. **并发下计数丢更新**：4 线程 × 300 次未复现丢失（B6），未做更高压力/`sys.setswitchinterval` 扰动，**不构成「无风险」结论**。
5. **UI / 看板 / 发布门禁**：`agent_runs.latency_ms` 被污染对**看板**的实际展示影响未核（我只核了 `review.py` 的 SQL 聚合路径；`dashboard/app.py:563` 读的是 `llm_calls` 表，不受影响）。
6. **U08/U09 消费侧**：原因码/漏斗的最终消费形态未验（U08 尚未实现）。
7. **补丁 13 对真实历史泄密的回溯**：只验证了「当前仓库 4 处命中 → 3 处登记 + 1 处放宽」，未做历史提交扫描。

---

## 7. 结论与放行条件

**CHANGES_REQUESTED。**

必须处理（P1）：
1. **补丁 09 列映射**：`latency_ms` 必须改为真实耗时或 `NULL`，`failed_rounds` 不得占用耗时列；`artifact_path` 按 docstring 承诺落独立列或改 docstring。（`src/audit/database.py:1731-1734` patched）
2. **`classify_evidence_report` 读 `report.ok`/`decision_gate()`**：门禁整体失败不得报 `NO_EDGE`，`details` 不得在 `report.ok=False` 时报 `evidence_gate_ok`。（`src/core/decision_trace.py:248-308`）
3. **S02 静默 Mock**：`route_expectation.mock_allowed` 不得从「缺 key」反推；或守卫在 `degraded_reason` 存在时无条件上报。（`src/llm/factory.py:56`）
4. **`run_protection_first` 在期限耗尽时被 span 违规击穿**：span 登记失败必须降级为「记录失败」而不是把异常抛出 `run_protection_first` 之外；补一条覆盖「expired budget + 保护优先」的测试。（`src/core/decision_trace.py:698-724`）
5. **补丁 13 的 `.py` 裸标识符放宽**：按 §3.1 的方案收窄（至少排除注释行）并留痕；否则请把该放宽从本批拆出单独评审。

应当处理（P2/P3）：见 §4，其中 **P2-4（04→09 依赖，README 批次表需修正）**、**P2-5**、**P2-6**、**P2-9** 建议在合并前一次改掉。

**已独立确认、可作为放行依据的部分**：
- 15 补丁 `--check` 15/15、顺序应用 15/15；
- 统一清单全量回归 **delta = ∅**（唯一失败 = KI02 既有环境项）；
- 作者自测计数 95/54/48/45 与「双态可运行」均属实；
- **market_query 撤回的约束真实**（作者引用的 `patch context mismatch at line 100` 精确复现），
  S03 影响可接受但需登记为显式偏差；
- `registered_false_positives` **机制本身安全**（精确三元组 + 双向 fail-closed），
  但 patch 13 附带的代码放宽**确实削弱了密钥检测**（注释/文档串/内嵌非 Python 片段中的明文凭据静默漏报）。
