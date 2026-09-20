# U05 共享适配补丁包（作者 C-U05 / 交 I 应用）

补丁基线：`cc38768`（= `task/U05` 作者分支，12 个共享文件在作者分支内**未被改动**，
本包的上下文即集成树里的现状）。**由 I 在集成时应用；单个补丁不得脱离本包其余部分单独合入**
（批次约束见 §二）。

## 一、补丁清单（15 个）

| 补丁 | 目标文件 | 类型 | 用途 |
|---|---|---|---|
| `01_llm_factory_route_expectation.diff` | `src/llm/factory.py` | U05 集成目标 | `route_expectation()` 显式路由期望；Mock 分支显式标注 `degraded_reason`/`route_role`（S02 静默 Mock）；failover 分支传 `declared_models`（S02 参数继承） |
| `02_llm_failover_declared_models_observer.diff` | `src/llm/failover_client.py` | U05 集成目标 | `declared_models`/`attempt_observer`/`_mta.deadline_monotonic`+`deadline_remaining_ms`；主/备尝试共享同一绝对期限（DL07、S02） |
| `03_llm_openai_client_route_declaration.diff` | `src/llm/openai_compatible_client.py` | 共享适配 | `route_declaration`（含脱敏 `provider_scope`）与 `attempt_observer`（每次 HTTP 尝试回调） |
| `04_core_agent_recorder_decision_trace.diff` | `src/core/agent_recorder.py` | U05 集成目标 | `begin_decision_trace()`/`record_decision_trace()`：四类计数分开 + 分阶段 p95/p99 + 逐轮结局落审计 |
| `05_core_graph_shared_deadline.diff` | `src/core/graph.py` | U05 集成目标 | 本轮共享绝对期限 `shared_budget`；`aux_deadline()` 改为其**子切片**（旧口径每次 `now+aux_timeout` 重新推导，可越过总预算）；新增 `model_deadline()` 与 `cycle_budget=` 注入点 |
| `06_core_intel_cache_bounded_refresh.diff` | `src/core/intel_cache.py` | 共享适配 | `refresh_bounded()`/`source_cache`：HTTP+摘要总预算、分源 TTL、给管理预留让路（S03）。**既有 `get_or_refresh` 语义不变** |
| `07_llm_request_coordinator_lease_recovery.diff` | `src/llm/request_coordinator.py` | 共享适配 | 可选 `lease_registry`：进临界区前回收死租约、登记/释放租约、超时消息回带 blocker（S04 崩溃回收） |
| `08_llm_workbuddy_route_declaration.diff` | `src/llm/workbuddy_client.py` | 共享适配 | `route_declaration`/`declared_fallbacks=()`/`attempts` 计数（判"返回了却未发出请求"） |
| `09_audit_database_decision_trace_summary.diff` | `src/audit/database.py` | U05 集成目标 | `log_decision_trace_summary()`：复用既有 `agent_runs` 表，**不新增表、不推进 schema 版本** |
| `11_data_macro_public_reason.diff` | `src/data/macro_public.py` | 共享适配 | `reason_from_errors()`：源级错误 → U05 原因码 |
| `12_data_onchain_public_reason.diff` | `src/data/onchain_public.py` | 共享适配 | 同上 |
| `13_secret_scanner_false_positive_ki01.diff` | `scripts/scan_tracked_secrets.py` | **KI01（归属 U05/S02）** | 修误报：`.py` 里裸标识符实参不再判为内联凭据；另 3 处误报**显式登记**（路径+行号+规则,含理由），并新增 `STALE_ALLOWANCE` 检查（登记失效即失败） |
| `14_secret_scanner_tests.diff` | `scripts/test_secret_scanner.py` | 13 的伴随 | 裸标识符不误报、YAML 裸标量仍拦截、登记精确且无过期 |
| `15_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **I 独占，作者不直接改** | 登记 4 个 `scripts/smoke_u05_*.py`（否则 `_validate_smoke_manifest()` 抛错） |
| `16_smoke_llm_failover_mta_keys.diff` | `scripts/smoke_llm_failover.py` | **02 的伴随修改**（必读 §二.3） | 02 给 `_mta` 追加 2 个期限字段后，既有整字典相等断言 `result["_mta"] == {...}` 必失败；改为"关键事实 + 新增字段存在" |

> **`10_data_market_query_reason_from_quality.diff` 已撤回**：见 §二.4。

## 二、批次约束（必须遵守）

1. **13 必须与 14 同批**（14 只测 13 引入的行为）。
2. **新增 4 个冒烟脚本必须与 15 同批**：脚本一落库，`_validate_smoke_manifest()` 会以
   `未登记冒烟脚本: …` 直接 exit≠0。作者 worktree 内这 4 个脚本已存在，因此
   **15 未应用前，作者 worktree 的统一入口必然红**（实测：`未登记冒烟脚本` → 退出码非 0）。
3. **02 必须与 16 同批**。实测（独立沙箱，基线树 + 15 个补丁）：只应用 02 时
   `scripts/smoke_llm_failover.py` 以 `AssertionError` 失败
   （`result["_mta"] == {"provider": "fallback", "fallback_used": True}` 被新增的
   `deadline_monotonic`/`deadline_remaining_ms` 打断）。16 把该断言改为
   "provider/fallback_used 关键事实 + 两个新字段存在"。
   **若 16 被拒，02 也必须一起推迟**（02 的期限可观测性不是硬需求，硬需求是"主备共享同一绝对
   期限"，该行为由 02 的 `_call(deadline=)` 透传承担——推迟 02 会同时失去 S02 的参数继承声明）。
4. **`10`（`src/data/market_query.py` 的 `reason_from_quality`）已撤回，不交付**。原因（实测，非推测）：
   U03 的 `tests/integration/test_market_evidence_quality.py` 用 `resolve_patch_states()` **读取现网
   `src/data/market_query.py` 并正/反向应用一份内嵌 unified diff** 来重建"补丁前/补丁后"两态；
   只要该文件被第三方（U05）改动，反向应用即 `ValueError: patch context mismatch at line 100`
   （实测：`smoke_u03_market_evidence_quality.py` 退出码 1）。作者不修改 U03 独占测试，
   故撤回本补丁。**替代路径（不阻断）**：数据侧原因码已作为 U05 独占模块的公开 API
   `core.decision_trace.reason_from_source_errors(errors, labels=..., degraded=...)` 提供，
   由调用侧（U08 漏斗 / orchestrator）直接消费，无需改 `market_query.py`。
5. 01–09、11–16 之间无互相依赖；**逐个** `git apply --check -p1` 全通过，且在
   「基线树 + 全量 15 个补丁」沙箱里顺序应用全部成功（见 §四）。
6. **无迁移 / 无 schema 推进**：09 复用既有 `agent_runs` 表；15 个补丁文件对
   `schema_migrations` / `current_schema_version` / `range(1, N)` 关键字**零命中**
   （`grep -l … docs/dispatch/patches/U05/*/*.diff` 为空），因此**不需要**任何伴随断言补丁。
   既有断言（`smoke_db_migrations`/`smoke_account_mirror`/… 的 `list(range(1, 18))`、
   `smoke_fault_matrix` 的 `current_schema_version() == 18`、`smoke_release_acceptance` 的
   fixture `schema_version: 17`）在 pristine 与打补丁树上**都通过**（见 §四 delta）。

## 三、回退

`git checkout -- <文件>` 即可回退。01–09/11–16 全部是"新增函数 / 新增可选参数 / 新增属性"，
不改既有函数既有分支语义；唯一行为变化是 05 把 `aux_deadline()` 从"每次重新推导"收紧为
"截断在本轮共享期限之内"（方向是收紧，不会放行更多真实调用）。

## 四、验证（本批实跑）

```
git apply --check -p1 <01..09,11..16>            # 15/15 OK（基线树独立 git 沙箱）
沙箱顺序应用 01+02+...+16                          # 15/15 应用成功，ruff src/ 只余 2 处既有 F401
                                                    # （src/core/runtime_identity.py，HEAD 既存，非本批）
python scripts/scan_tracked_secrets.py             # SECRET SCAN OK files=432 registered_false_positives=3
python tests/integration/test_decision_trace.py    # PASS 95 / FAIL 0
python tests/integration/test_model_route_safety.py# PASS 54 / FAIL 0
python tests/integration/test_background_refresh_budget.py      # PASS 48 / FAIL 0
python tests/integration/test_cross_process_llm_admission.py    # PASS 45 / FAIL 0
# 全量离线回归（逐条非 fail-fast；失败集合见报告）
pristine (task_base_commit) : scripts=73 failed=1  {smoke_windows_service_plan.py = KI02}
基线树 + 15 补丁             : scripts=77 failed=1  {smoke_windows_service_plan.py = KI02}
新增失败 = ∅（差集为空）
```
