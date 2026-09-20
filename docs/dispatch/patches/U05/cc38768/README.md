# U05 共享适配补丁包（作者 C-U05 / 交 I 应用）

补丁基线：`cc38768`（= `task/U05` 作者分支 head；12 个共享文件在作者分支内**未被改动**，
因此本包与基线之间的上下文就是集成树里的现状）。分支 `task/U05`。
**本包由 I 在集成时应用；任何单个补丁都不得脱离本包其余部分单独合入**（批次约束见下）。

## 一、补丁清单

| 补丁 | 目标文件 | 类型 | 用途 |
|---|---|---|---|
| `01_llm_factory_route_expectation.diff` | `src/llm/factory.py` | U05 集成目标 | `route_expectation()` 显式路由期望（模型/档位/温度/max_tokens/声明备用）；Mock 分支**显式标注** `degraded_reason`/`route_role`（S02 静默 Mock）；failover 分支传 `declared_models`（S02 参数继承） |
| `02_llm_failover_declared_models_observer.diff` | `src/llm/failover_client.py` | U05 集成目标 | `declared_models` / `attempt_observer` / `_mta.deadline_monotonic`+`deadline_remaining_ms`；主/备尝试都带同一个绝对期限（DL07、S02） |
| `03_llm_openai_client_route_declaration.diff` | `src/llm/openai_compatible_client.py` | 共享适配 | `route_declaration`（含脱敏 `provider_scope`）与 `attempt_observer`（每次 HTTP 尝试回调，供判早返回与跨进程额度） |
| `04_core_agent_recorder_decision_trace.diff` | `src/core/agent_recorder.py` | U05 集成目标 | `begin_decision_trace()` / `record_decision_trace()`：四类计数分开 + 分阶段 p95/p99 + 逐轮结局落审计 |
| `05_core_graph_shared_deadline.diff` | `src/core/graph.py` | U05 集成目标 | 本轮**共享绝对期限** `shared_budget`；`aux_deadline()` 改为共享期限的**子切片**（旧口径每次 `now+aux_timeout` 重新推导，可越过总预算）；新增 `model_deadline()` 与 `cycle_budget=` 注入点 |
| `06_core_intel_cache_bounded_refresh.diff` | `src/core/intel_cache.py` | 共享适配 | `refresh_bounded()` / `source_cache`：HTTP+摘要总预算、分源 TTL、给管理预留让路（S03）。**既有 `get_or_refresh` 语义不变** |
| `07_llm_request_coordinator_lease_recovery.diff` | `src/llm/request_coordinator.py` | 共享适配 | 可选 `lease_registry`：进入跨进程临界区前回收死租约、登记/释放租约、超时消息回带 blocker（S04 崩溃回收） |
| `08_llm_workbuddy_route_declaration.diff` | `src/llm/workbuddy_client.py` | 共享适配 | `route_declaration` / `declared_fallbacks=()` / `attempts` 计数（判"返回了却未发出请求"） |
| `09_audit_database_decision_trace_summary.diff` | `src/audit/database.py` | U05 集成目标 | `log_decision_trace_summary()`：复用既有 `agent_runs` 表，**不新增表、不推进 schema 版本** |
| `10_data_market_query_reason_from_quality.diff` | `src/data/market_query.py` | 共享适配 | `reason_from_quality()`：由 `data_quality` 结构化缺口派生 `DATA_UNAVAILABLE`（部分源可用时另附 `RESEARCH_INCOMPLETE`） |
| `11_data_macro_public_reason.diff` | `src/data/macro_public.py` | 共享适配 | `reason_from_errors()`：源级错误 → U05 原因码 |
| `12_data_onchain_public_reason.diff` | `src/data/onchain_public.py` | 共享适配 | 同上 |
| `13_secret_scanner_false_positive_ki01.diff` | `scripts/scan_tracked_secrets.py` | **KI01 归属 U05** | 修误报：`.py` 里裸标识符实参（`fallback_key` 这类变量引用）不再判为内联凭据；另 3 处误报**显式登记**（含行号+规则），并新增 `STALE_ALLOWANCE` 检查（登记失效即失败） |
| `14_secret_scanner_tests.diff` | `scripts/test_secret_scanner.py` | 伴随修改 | 13 的回归：裸标识符不误报、YAML 裸标量仍拦截、登记精确且无过期 |
| `15_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **I 独占，作者不直接改** | 登记 4 个 `scripts/smoke_u05_*.py`（否则 `_validate_smoke_manifest()` 直接抛错） |

## 二、批次约束

1. **13 + 14 必须同批**（14 只测 13 引入的行为）。
2. **新增 4 个冒烟脚本必须与 15 同批**：`scripts/smoke_u05_*.py` 一旦落库，`run_offline_regression.py`
   的 `_validate_smoke_manifest()` 会以 `未登记冒烟脚本: …` 直接 exit≠0。
   作者分支内这 4 个脚本已存在，因此**作者 worktree 的统一入口在 15 未应用前必然红**（见报告 delta 说明）。
3. 01–12 之间无互依赖，可任意子集应用；`git apply --check -p1` 对 15 个补丁**逐个**通过，
   且在「基线树 + 15 个全量应用」的独立沙箱里顺序应用全部成功（见报告）。
4. **无迁移 / 无 schema 推进**：09 复用既有 `agent_runs` 表，未新增表、未新增迁移号。
   全仓已复核"把迁移集合写死"的既有断言（`versions == list(range(1, 17))`、
   `current_schema_version() == 17`）——本包**不触碰**这些断言，见报告"迁移/schema 影响"节。

## 三、回退

`git checkout -- <文件>` 撤补丁即可回退；01–12 都是"新增函数/新增可选参数/新增属性"，
没有改变既有函数的既有分支语义（唯一的行为变化是 05 把 `aux_deadline()` 从"每次重新推导"
改为"截断在本轮共享期限之内"，方向是收紧、不会放行更多真实调用）。
