# 归档索引（U10b）

本目录存放**已退出日常阅读路径、但必须保留原样**的历史脚本与文档。归档的含义是
"换了个位置 + 在这里登记"，**不是删除、不是改写**：被归档文件的正文一个字都没动
（唯一的例外是 `scripts/` 下的脚本，见"脚本归档"一节的深度修正说明）。

判断当前系统行为时不要读本目录，请从 [文档索引](../文档索引.md) 的"当前权威文档"出发。

## 归档规则（什么进得来）

一条记录被归档，必须**同时**满足下面全部条件，缺一不可：

1. **不在任何运行/质量入口里**：不出现在 `scripts/run_offline_regression.py` 的
   `SCRIPTS`/`EXCLUDED_SMOKE_SCRIPTS`、不被 `.github/workflows/ci.yml` 调用、
   不被 `src/`/`tests/`/其它脚本以路径或 import 引用（逐文件核实，不靠印象）。
2. **没有现役文档把它当作操作入口**：README、[运行与故障恢复手册](../运行与故障恢复手册_2026-09-06.md)、
   [代码维护规范](../代码维护规范.md)、[文档索引](../文档索引.md) 里的"脚本入口索引"
   不把它列为待执行命令。
3. **移动后不会断链**：Markdown 相对链接必须仍然可达 —— 发布包自检
   （`scripts/create_release_package.py` 的 `doc_links_resolve`）会对包内每篇 `.md`
   的每个相对链接做存在性校验，断一条就构建失败。因此**只归档出链为 0、且只被
   [文档索引](../文档索引.md) 引用的文档**；任何被别人相对链接的文件都留在原地。
4. **能力不消失**：归档只是换路径。若某文件有人还在用，它要么留在原地，要么在
   本页登记新路径（本页就是"哪个文件去哪了"的唯一出处）。

**不进归档的**：任何 `scripts/smoke_*.py`。`run_offline_regression.py` 的
`_validate_smoke_manifest()` 只 glob `scripts/` **顶层**的 `smoke_*.py`，把冒烟脚本
移进子目录等于让它**静默退出离线回归**——那是削减回归覆盖，不是整理。所以本任务
一个冒烟脚本都没动。

## 文档归档（docs/ → docs/archive/）

| 原路径 | 现路径 | 为什么归档 | 过期点 / 被谁取代 |
|---|---|---|---|
| `docs/上线前核查报告_2026-09-02.md` | [同名](上线前核查报告_2026-09-02.md) | 时点核查报告，核查范围早于 U00–U15 全部改造 | 结论与验收项以 [当前状态与剩余工作矩阵](../当前状态与剩余工作矩阵_2026-09-16.md) 为准 |
| `docs/风险事件排查与改善计划_2026-09-03.md` | [同名](风险事件排查与改善计划_2026-09-03.md) | 时点事故处置计划 | 事件与处置状态均已变化；现行口径见[运行与故障恢复手册](../运行与故障恢复手册_2026-09-06.md) |
| `docs/实盘上线评估与整改计划_2026-09-14.md` | [同名](实盘上线评估与整改计划_2026-09-14.md) | 实盘启用前的评估稿，实盘至今仍未启用 | 整改项归属以任务清单与 [当前状态与剩余工作矩阵](../当前状态与剩余工作矩阵_2026-09-16.md) 为准 |
| `docs/实盘接管阻断诊断_2026-09-16.md` | [同名](实盘接管阻断诊断_2026-09-16.md) | 针对当时提交的诊断结论 | 诊断对象为当时代码；结论不构成当前能力声明 |
| `docs/实盘账户并行接入评估与上线整改清单_2026-09-15.md` | [同名](实盘账户并行接入评估与上线整改清单_2026-09-15.md) | **本页是它被点名移交 U10b 的原因**：正文 §3.1 内嵌"源码需要 16"的 schema 版本断言，早已过期 | 现仓库最新迁移版本是 **17**（`src/audit/database.py` 的 `_apply_migration(17, "income_reconciliation", …)`，U01 引入）。以 `current_schema_version()` 的实际返回值为唯一口径，任何写死的版本数字都不要采信 |
| `docs/架空机制排查_2026-09-02.md` | [同名](架空机制排查_2026-09-02.md) | 排查对象为当时代码的"写了但没接线"机制 | 结论已被后续 U 系列改造覆盖 |
| `docs/UI重构方案_整合版.md` | [同名](UI重构方案_整合版.md) | **设计稿**：UI 目标形态，不是当前界面契约 | 以 `src/dashboard/` 实际模板与路由为准 |
| `docs/双进程双账本改造设计_2026-09-15.md` | [同名](双进程双账本改造设计_2026-09-15.md) | **设计稿**：双进程/双账本方案意图 | 实际路径与差异以[运行与故障恢复手册](../运行与故障恢复手册_2026-09-06.md) 第 9 章为准（该章已按实际代码校正） |

被归档的文档**一律保持原样**：事故与决策证据不因"计划已完成"改写或删除。若其中某条
结论今天仍然有效，先迁入现行文档并附代码指认，再引用。

## 脚本归档（scripts/ → scripts/diagnostics|maintenance/）

移入子目录的脚本**文件名一字不改**。这不是洁癖：`scripts/create_release_package.py`
的 `SOURCE_ONLY_SCRIPT_PREFIXES` 按**文件名前缀**归类（`diag_`/`probe_`/`cleanup_`…），
改名就会变成"未归类文件"而让发布包**构建硬失败**。

### scripts/diagnostics/（只读诊断与显式在线探针）

| 原路径 | 现路径 | 说明 |
|---|---|---|
| `scripts/_js_check.py` | `scripts/diagnostics/_js_check.py` | 本机临时脚本（`_` 前缀约定即"不入包"），硬编码本机 Node 路径 |
| `scripts/diag_dump_run.py` | `scripts/diagnostics/diag_dump_run.py` | 单次运行落库明细转储 |
| `scripts/diag_failures.py` | `scripts/diagnostics/diag_failures.py` | 失败原因汇总 |
| `scripts/diag_latest_run.py` | `scripts/diagnostics/diag_latest_run.py` | 最近一次运行摘要 |
| `scripts/diag_positions_ledger.py` | `scripts/diagnostics/diag_positions_ledger.py` | 持仓账本核对 |
| `scripts/diag_tool_errors.py` | `scripts/diagnostics/diag_tool_errors.py` | 工具调用错误汇总 |
| `scripts/live_check_query_detail.py` | `scripts/diagnostics/live_check_query_detail.py` | 公共行情查询明细在线核对 |
| `scripts/probe_exchange_positions.py` | `scripts/diagnostics/probe_exchange_positions.py` | 交易所持仓在线探针 |
| `scripts/probe_futures_ws_routes.py` | `scripts/diagnostics/probe_futures_ws_routes.py` | 合约 WS 路由在线探针 |
| `scripts/probe_intel_with_proxy.py` | `scripts/diagnostics/probe_intel_with_proxy.py` | 指定出口代理的情报采集耗时对比（依赖 `cwd` 为仓库根） |
| `scripts/probe_symbol_trades.py` | `scripts/diagnostics/probe_symbol_trades.py` | 逐笔成交在线探针 |
| `scripts/probe_user_stream_live.py` | `scripts/diagnostics/probe_user_stream_live.py` | 用户数据流在线探针 |
| `scripts/verify_symbol_fix.py` | `scripts/diagnostics/verify_symbol_fix.py` | 一次性修复验收脚本 |

### scripts/maintenance/（有副作用的一次性维护与历史脚手架）

| 原路径 | 现路径 | 说明 |
|---|---|---|
| `scripts/cleanup_ghost_orders.py` | `scripts/maintenance/cleanup_ghost_orders.py` | **会改状态**：幽灵订单清理 |
| `scripts/cleanup_orphan_algo_orders.py` | `scripts/maintenance/cleanup_orphan_algo_orders.py` | **会改状态**：孤儿 Algo 保护单清理 |
| `scripts/cleanup_positions_ledger.py` | `scripts/maintenance/cleanup_positions_ledger.py` | **会改状态**：持仓账本清理 |
| `scripts/repair_bad_reconcile_orders.py` | `scripts/maintenance/repair_bad_reconcile_orders.py` | **会改状态**：错误对账订单修复 |
| `scripts/run_phase0.py` | `scripts/maintenance/run_phase0.py` | 早期阶段脚手架（打包器把它归为"`run_phase`：阶段脚手架，历史入口"） |

**唯一的正文改动（必须知道）**：`scripts/x.py` 移到 `scripts/<子目录>/x.py` 后，
`Path(__file__).resolve().parents[1]` 算出来的不再是仓库根，而是 `scripts/<子目录>/`。
因此上表中 **16 个**脚本把项目根深度由 `parents[1]` 改为 `parents[2]`
（`cleanup_ghost_orders.py` 有两处、其余各一处），并在该行上方加了一行注释指回本页；
`_js_check.py` 与 `probe_intel_with_proxy.py` 不依赖 `__file__`，未改。
该深度修正是**运行时验证过的**（用 AST 取出模块级里引用 `__file__` 的表达式，以新路径
求值，断言结果等于仓库根或其直接子目录），不是目测。

被归档脚本的**能力全部保留**：它们仍在 Git 索引里、仍被
`scripts/check_quality.py` 的 `ruff` + `py_compile` 覆盖，只是不再堆在 `scripts/` 顶层
与启动入口抢注意力。

## 已核实但**不**归档（留在原地的理由）

| 文件 | 留在原地的理由 |
|---|---|
| `scripts/diag_algo_orphans.py` | **lint 契约**：`pyproject.toml` 的 `per-file-ignores` 只对 `scripts/*.py` 豁免 `E402`，而该脚本必须在 `apply_proxy_env()` **之后**才能导入 SDK 客户端，E402 是结构性的。移进子目录会让它立刻违反 ruff，除非新增一条 ruff exclude —— 本任务明令禁止新增 exclude，故留在顶层 |
| `scripts/run_phase1.py` | `src/core/runtime_paths.py` 以**字面路径**登记了它（`ConsumerEntry("legacy_phase1_entry", "scripts/run_phase1.py:41", …)`）。移动它必须同批改 `src/`，超出本任务独占范围 |
| `scripts/export_usdm_incidents.py` | [运行与故障恢复手册](../运行与故障恢复手册_2026-09-06.md) 把它登记为事故导出入口 |
| `scripts/benchmark_demo_endpoints.py` / `scripts/benchmark_runtime_baseline.py` | README、[运行与故障恢复手册](../运行与故障恢复手册_2026-09-06.md)、[代码维护规范](../代码维护规范.md) 登记为性能基线测量入口 |
| `scripts/verify_llm_provider.py` | README、[文档索引](../文档索引.md)、[项目说明书](../项目说明书.md) 登记为在线协议检查入口 |
| `scripts/verify_close_reconcile_live.py` | 在线验证工具，与离线冒烟 `scripts/smoke_close_reconcile.py` 成对使用 |
| `scripts/start_web_detached.py` / `scripts/run_web_service.py` | README 与运行手册的过渡启动方式；且 `scripts/smoke_detached_profiles.py`（在离线回归清单里）引用了它们 |
| `scripts/test_*.py` | `test_` 前缀本就是 source-only 归类；其中两个在离线回归清单里 |
| `scripts/probe_*` 之外的 `scripts/smoke_*.py` | 全部在离线回归清单里；移动即削减覆盖（见上文规则） |
| `docs/策略扫描原因码.md` | `tests/integration/test_strategy_funnel.py` **断言该路径存在**，移动会让测试失败 |
| `docs/项目问题盘点与优化计划_2026-09-07.md`、`docs/USD-M合约系统稳定性与AI情报升级开发计划_2026-09-05.md`、`docs/WorkBuddy_DeepSeek_V4.1_实盘决策适配说明_2026-09-16.md`、`docs/决策链路专项评估与优化报告_2026-09-19.md`、`docs/模拟盘复审与上线优化总方案_2026-09-19.md`、`docs/无人值守交付与多Agent执行规格_2026-09-19.md`、`docs/无人值守方案细审与修订记录_2026-09-19.md` | 被 README / 项目说明书 / 技术报告 / `docs/contracts/README.md`（**冻结目录，不得改**）/ 分派手册以**相对链接**引用。归档它们必须同批改至少一篇不在本任务独占路径内的文档；本任务只做**不动别人的**那一档，故不归档 |
| `docs/CHANGES_2026-09-01.md`、`docs/dual_agent_design.md` | 出链为 0，但被[技术报告](../技术报告.md)与[项目说明书](../项目说明书.md)以相对链接引用（两篇均为现行文档） |
| `docs/上线前最后一轮优化工作计划_2026-09-15.md` | 有 16 条出链指向 `src/`（归档后这些相对链接会全部断掉），另有入链；改造成本高于收益，留在原地 |
| `docs/测试与验收矩阵_2026-09-16.md`、`docs/当前状态与剩余工作矩阵_2026-09-16.md`、`docs/上线前人工检查单_2026-09-16.md` | [文档索引](../文档索引.md) 把这三篇列为**现行**（质量门禁/当前状态/人工确认），不属于历史材料 |
| `docs/dispatch/**` | 编排流程记录，是否随包只由 `scripts/create_release_package.py` 的 `EXCLUDED_PATH_PREFIXES` 决定，不归本目录管 |

## 删除清单（本任务不删除任何文件）

| 项 | 数量 | 核验方式 |
|---|---|---|
| 删除的文件 | **0** | `git diff --name-status --no-renames 0706db1 <U10b HEAD>`：`D(消失)=26`、`A(新增)=30`，且**每个消失路径都有同名文件在新路径存在**（26/26 配对，孤儿删除 = 0）。另 4 个新增是 `docs/archive/README.md` 与本任务补丁目录的 3 个文本，无对应删除 |
| 删除的能力 / 入口 | **0** | 归档的 18 个脚本仍在 Git 索引内，仍被 `scripts/check_quality.py` 的 `ruff` + `py_compile` 覆盖；8 篇文档正文逐字节未改（`git diff -M` 全部为 `R100`） |
| 复制的 `.py.txt` 之类降级件 | 0 | 本任务没有把可运行脚本降级成文本存档；归档件保持可运行（并已修正项目根深度） |

被引用的策略注册表与来源**全部保留未动**：`knowledge/strategy_cards.json`、
`knowledge/backup_strategy_cards.json`、`knowledge/策略报告_整理版.md`、
`config/*`、`src/strategy_runtime/PROVENANCE.md` 在 `0706db1 → U10b HEAD` 的 diff 里
零命中。

## 复验入口

- 离线回归（含 `SMOKE MANIFEST OK` 与 `OFFLINE REGRESSION OK scripts=N`）：
  `scripts/run_offline_regression.py`
- 发布包不漏包/不断链/不含凭据：`scripts/smoke_u10a_release_package.py`
- 质量门禁：`scripts/check_quality.py`

本页本身的相对链接同样受发布包 `doc_links_resolve` 校验，断链会让构建失败。
