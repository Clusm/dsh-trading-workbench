# U05 共享适配补丁包（作者 C-U05 / 交 I 应用）— r2（B-r1 修复轮）

本目录**取代** `docs/dispatch/patches/U05/cc38768/`（r1 包，已从工作树删除；git 历史可回溯）。

- 补丁基线：集成树 `0e522b1`（含 U00/U01/U03/U15）。本包上下文 = `task/U05` head `8a8e31e`，
  该提交只改作者**独占**路径（`src/core/decision_trace.py`、`tests/integration/test_{decision_trace,model_route_safety}.py`），
  15 个共享文件逐字节未被触碰。
- 与 `8a8e31e` 之间只增删 `docs/dispatch/patches/U05/` 下的文档时，**共享文件内容不变**，
  两个提交上 `git apply --check` 结论一致。
- **由 I 在集成时应用；单个补丁不得脱离本包其余部分单独合入**（批次约束见 §二）。

## 一、补丁清单（15 个）

| 补丁 | 目标文件 | r2 状态 | 用途 |
|---|---|---|---|
| `01_llm_factory_route_expectation.diff` | `src/llm/factory.py` | **已改** | `route_expectation()` + `mock_explicitly_allowed()`：`mock_allowed` 只来自**显式**进程开关 `MTA_ALLOW_MOCK_LLM`（缺省 False），**不再**由"取不到 key"反推（P1-3）；Mock 分支标 `degraded_reason`/`route_role` |
| `02_llm_failover_declared_models_observer.diff` | `src/llm/failover_client.py` | 未改 | `declared_models`/`attempt_observer`/`_mta.deadline_*`；主备共享同一绝对期限 |
| `03_llm_openai_client_route_declaration.diff` | `src/llm/openai_compatible_client.py` | 未改 | `route_declaration` + `attempt_observer` |
| `04_core_agent_recorder_decision_trace.diff` | `src/core/agent_recorder.py` | **已改** | 落 `latency_ms=trace.elapsed_ms()`（真实耗时）；**工件与 `agent_runs` 两条写入路径都受保护**（P2-5） |
| `05_core_graph_shared_deadline.diff` | `src/core/graph.py` | 未改 | 本轮共享绝对期限；`aux_deadline()` 收紧为子切片 |
| `06_core_intel_cache_bounded_refresh.diff` | `src/core/intel_cache.py` | 未改 | `refresh_bounded()`/`source_cache` |
| `07_llm_request_coordinator_lease_recovery.diff` | `src/llm/request_coordinator.py` | 未改 | 可选 `lease_registry` 崩溃回收 |
| `08_llm_workbuddy_route_declaration.diff` | `src/llm/workbuddy_client.py` | 未改 | `route_declaration`/`declared_fallbacks`/`attempts` |
| `09_audit_database_decision_trace_summary.diff` | `src/audit/database.py` | **已改** | **列语义修正（P1-1）**：`latency_ms` 只写真实耗时或 `NULL`；`failed_rounds` 改随 `output_summary` 落库，不再占用共享耗时列；docstring 不再承诺"工件进独立列" |
| `11_data_macro_public_reason.diff` | `src/data/macro_public.py` | **已改** | `reason_from_errors(errors, *, degraded=False)`：`degraded` 由调用方显式声明，删除 `len(errors) < 8` 魔法常量（P2-6） |
| `12_data_onchain_public_reason.diff` | `src/data/onchain_public.py` | **已改** | 同 11（删 `len(errors) < 6`） |
| `13_secret_scanner_false_positive_ki01.diff` | `scripts/scan_tracked_secrets.py` | **已改** | **P1-5 收窄**：用 `tokenize` 区分"代码位置"与"注释/字符串文本"，放宽只作用于前者；登记表加**内容指纹**双锚（P2-9）；放宽**留痕** |
| `14_secret_scanner_tests.diff` | `scripts/test_secret_scanner.py` | **已改** | 13 的伴随（**必须同批**）：注释/docstring/内嵌 YAML 的明文**必须报出**、真变量引用不误报、留痕计数、指纹登记 |
| `15_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | 未改 | 登记 4 个 `scripts/smoke_u05_*.py`（**必须与 4 个冒烟同批**） |
| `16_smoke_llm_failover_mta_keys.diff` | `scripts/smoke_llm_failover.py` | 未改 | 02 的伴随（**必须同批**） |

> `10_data_market_query_reason_from_quality.diff` 已撤回、不交付 —— 见 §二.4（**显式偏差**）。

## 二、批次约束（必须遵守）

1. **13 必须与 14 同批**；**02 必须与 16 同批**；**4 个 `smoke_u05_*.py` 必须与 15 同批**
   （脚本一落库，`_validate_smoke_manifest()` 即以"未登记冒烟脚本" exit≠0）。
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

## 三、r2 修复对照（原始输出见交付报告）

- **P1-5（`.py` 明文盲区）**：`# api_key = abcdefghijklmnop1234`、三引号 docstring、
  `.py` 内嵌 YAML 三种探针：修复前 `[]`（静默漏报、无留痕）→ 修复后
  `['1:secret-assignment']` / `['2:secret-assignment']`；
  同时 `api_key=fallback_key,` 与 `api_key=cfg.fallback_key,` 两态**均不报**（KI01 保持关闭）。
  无法词法分析的 `.py` 整体放弃放宽（fail-closed）并计入 `unparsable_python_files`。
- **P1-1（跨任务数据污染）**：`failed_rounds=3` + `latency_ms=1234` 落库 →
  修复前 `agent_runs.latency_ms=3`（`review.py:231-237` 的 `AVG(latency_ms)` 会读成 3ms）
  → 修复后 `latency_ms=1234`，`failed_rounds=3` 落在 `output_summary.failed_rounds`。
- **P1-3（自证清白）**：缺 key → 修复前 `mock_allowed=True` 且守卫 `clean=True`
  → 修复后 `mock_allowed=False`、守卫 `clean=False violations=['SILENT_MOCK']`；
  显式开关打开时 `degraded_reason=None` 且守卫 `clean=True`。

## 四、验证（本批实跑，原始输出见交付报告）

```
git apply --check -p1 <01..09,11..16>   # 独立 git repo 根（git archive head + git init）：15/15 OK
全量离线回归 delta（逐条非 fail-fast）
  pristine (8a8e31e)     : scripts=77 failed=1 {smoke_windows_service_plan.py = KI02}
  + 15 补丁              : scripts=77 failed=1 {同上}
  新增失败 = ∅ ；修复 = ∅
scripts/smoke_contracts_v3.py           # PASS 173 / FAIL 0（pristine 与 patched 树均 173/0）
scripts/scan_tracked_secrets.py         # SECRET SCAN OK files=397 registered_false_positives=3
                                        #   skipped_bare_identifiers=20 unparsable_python_files=0
scripts/test_secret_scanner.py          # PASS 26 / FAIL 0
tests/integration/test_decision_trace.py             # 115 / 0
tests/integration/test_model_route_safety.py         # 作者树 56/0（打补丁专用断言跳过）；集成树 63/0
tests/integration/test_background_refresh_budget.py  # 48 / 0
tests/integration/test_cross_process_llm_admission.py # 45 / 0
```

## 五、未接线（如实登记，非本次修复范围）

U05 的运行时 API 在生产 `src/` **零调用方**（只在 U05 独占测试与冒烟包装里出现）：
`run_protection_first`、`classify_cycle`、`classify_evidence_report`、`reason_from_source_errors`、
`background_refresh`、`ModelRouteGuard`、`LeaseRegistry`、`deadline_violations`、
`begin_decision_trace`/`record_decision_trace`、`graph.model_deadline()`、11/12 的 `reason_from_errors`。
即：计数分离 / p95 / 原因码 / 保护优先目前**在真实跑批里一个字节都不落**。

**应归属**：漏斗与决策主流程的接线属 **U08**（研究漏斗 → `reason_from_source_errors`、
`classify_evidence_report`、`run_protection_first` 进 `orchestrator` 决策轮）；
轨迹落审计与共享期限注入属 **U08/U12** 的 service 装配（`agent_recorder.begin_decision_trace`
+ `graph.model_deadline()`）。R22 的"共享绝对期限"在库层成立，生产侧目前由既有的
`orchestrator.py` 单点推导期限以另一套方式满足（数值同为 240s，暂看不出差别）。

## 六、回退

`git checkout -- <文件>` 即可回退。01–09/11–16 全部是"新增函数 / 新增可选参数 / 新增属性"，
不改既有函数既有分支语义；r2 的行为变化只有三处，方向都是**收紧/纠正**：
05 把 `aux_deadline()` 截断在本轮共享期限内；09 不再把 `failed_rounds` 写进 `latency_ms`
（改为 `NULL` 或真实耗时）；13 的放宽从"整行"收窄到"代码位置"。**回退 13 会让 KI01 误报复现**
（`src/llm/factory.py` 的裸标识符实参），故 13/14 不建议单独回退。
