<!-- 归档: I 于 2026-09-20 从 F 子代理会话 ses_f40a4b1e1ffevvkMGfimxKGoLK 原文归档 -->
<!-- 性质: W0 准备证据（指纹差异清单），非候选审查；同模型偏差限制见文末 -->
<!-- 基线: planning_commit 5295632c748882574a8691d3e7b4b9df2ad7bf37 -->
# W0 指纹差异清单（F 角色交付物 · 只读分析）

基线：`mat-wt/reviews/U00/F/w0r1`（已 checkout planning_commit，含规划文档与源码）。
范围：为 U15/K01 运行身份契约准备差异清单；实盘关闭、风险参数不变；本报告不写盘。

---

## 1. 身份输入清单

| # | K01 要求（规格 §4 K01，`docs/无人值守交付与多Agent执行规格_2026-09-19.md:68-72`） | 当前实际行为（file:line） | 差异/缺口 | 风险 |
|---|---|---|---|---|
| 1 | `git_commit` 属于 RuntimeIdentity；不可变代码发布目录 | `src/core/release_manifest.py:78-87` `git rev-parse HEAD`（cwd=project_root）→ manifest 字段 `:177`；运行状态展示 `src/core/service.py:664` | ① 无 `.git` 时静默返回 `"unknown"`（`release_manifest.py:84-87`），无告警；② 不记录工作区是否 dirty，`content_hash` 取文件系统实际 src（`release_manifest.py:63`），与 HEAD 可能不一致而无标记；③ **`git_commit` 不在 release_id 种子**（`release_manifest.py:168-172`），HEAD 变化但文件内容相同 → release_id 不变；④ 全仓无任何“预期 commit/身份不符则拒绝”的启动门禁（grep `git_commit|rev-parse` 仅 5 处，均在本文件与展示层） | 中（可审计性）；对 F13“身份不符不发单”为**高** |
| 2 | `dependency_hash` 必须覆盖**实际 requirements 锁文件**；依赖环境与锁比对结果另存 | `release_manifest.py:22-29` `DEPENDENCY_FILES` = requirements.txt / requirements-dev.txt / pyproject.toml / uv.lock / poetry.lock / Pipfile.lock；实际存在的是**未锁定的范围约束** `requirements.txt:1-12` + 仅 ruff 配置的 `pyproject.toml` → `dependency_hash` 对范围文件计算（`:155-156`） | ① **`requirements-lock.txt` 未纳入**，而 README.md:26、`docs/代码维护规范.md:100`、`scripts/verify_dependency_lock.py:82` 都把它当作部署锁（总方案 `docs/模拟盘复审与上线优化总方案_2026-09-19.md:310` 已登记同一问题）；② `verify_dependency_lock.py:108-110` 的比对结果只打印，**不落库、不写 manifest**，“另存”未实现 | 高 |
| 3 | `effective_config_hash`：只取有效非敏感值；覆盖 profile 覆盖值；禁止对原始 `.env` 整文件哈希 | 逐文件 sha256 `config_hashes`（`release_manifest.py:127,149-153`，含 settings/decision_policy/agents/risk_learning/risk_profile.lock/windows_service.json）；profile 合并只在内存（`src/core/config_loader.py:22-33`）；模型/供应商/执行模式等**环境覆盖不参与任何哈希**（`src/llm/factory.py:29-37,49-52,60-64`；`src/exchange/factory.py:39-40`） | ① 无 `effective_config_hash` 字段；② `config_hashes` 无任何消费方（grep 仅定义处）；③ 无法回答“本次运行实际用了哪些有效配置/环境覆盖”；④ live 的 channels 覆盖（`settings.yaml:5-9`，0.0/1.0）与 demo 原始文件哈希相同，仅靠 `environment` 字符串区分。正面项：确实未读 `.env` 内容（`release_manifest.py:146`；全仓无 `.env` 哈希） | 中-高 |
| 4 | `strategy_hash` 必须覆盖**实际策略卡** | `strategy_config_hash` = decision_policy.yaml + risk_learning.yaml（`release_manifest.py:157-163`）；策略卡经 `knowledge_dir` 读取（`src/signal_engine/scanner.py:62-63,186-214`、`src/agents/strategy_library.py:15-16`），`knowledge_dir = project_root/knowledge`（`config_loader.py:95-97`） | ① 改 `knowledge/strategy_cards.json`（单行 JSON；顶层 `version/note/cards`，9 张卡，含 `exits.sl_pct/tp_pct/time_stop_hours`、`signal_fn`、`decision_policy`）**不改变 manifest 任何字段与 release_id**；② 发布包也不含 knowledge/（`scripts/create_release_package.py:22-40,65-93`） | 高（策略行为身份） |
| 5 | `model_route_hash` 覆盖模型名/推理档/备用路由及 profile 覆盖值 | `model_config_hash` = agents.yaml + settings.yaml **原始文件**（`release_manifest.py:160-164`）；实际路由在运行时决定：agents.yaml `model` > `SENSENOVA_MODEL` > `llm.default_model`（`factory.py:49-52`）；`MTA_AI_DECISION_PROVIDER=workbuddy` 仅换 ai_trader（`factory.py:29-37`）；`MTA_AI_DECISION_FALLBACK_PROVIDER=opencode` 才建备用（`factory.py:60-64`），最终模型串 `"deepseek-v4-flash\|fallback:union-alpha"`（`factory.py:134`）只进内存/`llm_calls` 审计（`src/agents/orchestrator.py:777-785`），**不进 manifest**；WorkBuddy CLI/node 路径来自环境（`src/llm/workbuddy_client.py:29-42`）且版本不记录 | ① 无 `model_route_hash` 字段；② 环境覆盖可实际换模型而 release_id 不变；③ 缺 key 静默 Mock（见下文 Q3） | 高 |
| 6 | `runtime_instance_id` 属于 RuntimeIdentity | `service.py:118` `uuid.uuid4().hex`，每进程新生成；注入 broker（`:121`）、readiness（`:694`）、签名授权上下文（`src/core/decision_pipeline.py:44-45`；`src/core/entry_readiness.py:167-171`）、观测 `session_id`（`:422`）、执行审计（`src/agents/execution.py:257`、`src/exchange/binance_futures.py:1062`） | ① **不持久化、不进 manifest**（`ReleaseManifest` 字段无此项，`release_manifest.py:117-134`）；② 实例锁只有 pid/thread/started_at（`src/core/instance_lock.py:63-67`），无运行代/epoch 与锁绑定（规格 §10.3 `:255` 要求锁包含进程创建时间/运行代）；③ 重启后无法关联同一“运行代” | 中 |
| 7 | `environment` / `account_ref` | `service.py:122-136`：environment=broker.environment（`exchange/factory.py:40` `mode = profile or EXECUTION_MODE or demo` 推导）；account_ref=`trade_reconciliation.account_ref`=sha256(api_key)[:16] 前缀环境（`src/exchange/trade_reconciliation.py:66-69`），无 key 时 `f"{env}:local"`（`service.py:127-128`）；manifest 侧二次脱敏（`release_manifest.py:106-114`） | ① **account_ref 不进 release_id 种子**（`release_manifest.py:168-171`）→ 同代码/配置、不同账户可得到同一 release_id；② profile=None 时 `EXECUTION_MODE=live` 可让 live 账户写入公共 `data/` 根（`config_loader.py:41-43`），无 environment 与状态根一致性校验；③ `MTA_RELEASE_ID` 环境变量可整体覆盖 release_id 且与哈希无绑定（`service.py:135`） | 高（账户混用/身份伪造） |

### 重点回答

**Q1：当前有没有任何地方计算/校验这些哈希？**
- 计算：有。启动时 `build_release_manifest`（`service.py:129-136`），并写 `data/release_manifest.json`（`:148-149`，写失败只记 critical 事件，不阻断启动 `:150-156`）；离线脚本 `scripts/verify_profile_lock.py:30,53-55,82`、`verify_dependency_lock.py:82,108-110`（仅 CI/人工，`scripts/check_quality.py:63-64` 调用，不在服务启动路径）。
- 校验/拒绝：**没有**。全仓不存在“预期哈希比对→不符拒发单”的逻辑。`src/core/release_evidence.py:305-317` 只在出报告时校验 manifest 与请求 environment 一致，并按 `release_id+environment+account_ref` 过滤观测（`:328-391`）——是报告期一致性检查，不是启动门禁。`config_hashes/strategy_config_hash/model_config_hash` 无消费方（grep 仅 `release_manifest.py` 与 smoke fixture `scripts/smoke_release_acceptance.py:110-114` 的占位字符串）。
- 结论：F13“指纹必变，身份不符不发单”目前只做到“部分输入会变”，**“拒”未实现**，且变的不完整（锁文件、策略卡、环境覆盖、account_ref 均不变）。

**Q2：配置优先级实际是什么（agents.yaml vs 环境变量）？**
- 模型名：`agents.yaml` 的 `model` 字段 > `SENSENOVA_MODEL` 环境变量 > `settings.llm.default_model`（`factory.py:49-52`）；环境变量名本身由 `settings.llm.model_env` 指定（`settings.yaml:68`）。
- key / base_url：只从环境读（`factory.py:47-48,56-59`），settings 只提供变量名与默认 URL。
- 最高优先级开关：`MTA_AI_DECISION_PROVIDER=workbuddy` 对 ai_trader 直接短路返回，绕过 agents.yaml 与共享协调器（`factory.py:29-37`）；`MTA_AI_DECISION_FALLBACK_PROVIDER=opencode` 决定是否构建 Failover（`:60-75`）。
- 执行模式：`--profile`/`MTA_PROFILE` > `EXECUTION_MODE` > `settings.execution.default_mode`（`exchange/factory.py:40`）；profile 非空时 `EXECUTION_MODE` 被忽略。
- 配置陷阱：`settings.yaml:71` 的 `llm.mock: false` **没有任何代码读取**（grep `mock` 无 config_loader/factory 命中），不能当作 Mock 开关；Mock 仅由“缺 key / 模型为 none”决定（`factory.py:53-54`）。

**Q3：缺 key 时 factory 返回什么、会不会在 Demo/live 冒充真实模型成功？**
- 返回 `MockLLMClient()` 与字符串 `"mock"`（`factory.py:53-54`）；辅助 agent 在 service 层被显式跳过（`service.py:221-224`），但 **ai_trader 主链路照常使用**（`:219` → `:881-882` → `graph.py:393-413`）。
- 行为：Mock `chat` 返回 `content` 固定 JSON（`src/llm/mock_client.py:12-19`，无 tool_calls）→ orchestrator 走“裸 JSON”分支（`orchestrator.py:1052-1094`）解析为合法 `no_trade`，`last_error=None`、`decision_was_downgraded=False`，甚至会 `resolve_risk_incidents_by_type("decision_downgraded")`（`:1132-1133`）。
- 结论：**不会伪造开仓成交成功**；但会“静默成功降级”——AI 通道每轮正常结束为 no_trade，`entry_readiness` 事实集不含模型身份（`entry_readiness.py:85-135`；`service.py:685-719`），策略通道仍可独立开仓，运行状态也不展示模型（`service.py:640-666` 只列 7 个 release_manifest 字段）。审计 `llm_calls/agent_runs` 里可见 `model="mock"`（`orchestrator.py:777-785,1123-1131`），但不是门禁。DC14/F13 要求的“Demo/live 主模型无效时阻断新仓”**未实现**。

---

## 2. RuntimePaths 相关缺口

`RuntimePaths` 类目前**不存在**（grep `RuntimePaths|state_root|code_root|shared_egress_root` 零命中；规格 §11.1 `:259-265` 要求 U13 补上）。现状按三类归口：

### A. 代码根（随不可变代码目录迁移属预期）
- 模板目录 `src/dashboard/app.py:44`；各脚本自身 `ROOT/PROJECT_ROOT = Path(__file__).resolve().parents[1]`（如 `scripts/run_web.py:23`、`create_release_package.py:21` 等）；模块内默认配置路径 `src/core/decision_policy.py:15`、`src/data/market_query.py:51`、`src/core/net_env.py:65`（`.env`）。

### B. 状态根（必须稳定，不随代码目录变）
经 `Settings.runtime_path` 的（profile 感知，但 base 是代码根）：
- `config_loader.py:35-54,70-92` → data_dir/db_file/artifacts/logs/reviews；service 消费者：锁 `service.py:85`、kline 缓存 `:98`、备份 `:106`、manifest `:148`、capital `:166`、risk `:184`、universe `:191`、channels `:207`、control `:225`、signal 状态 `:340`、graph cache `:895`。

**绕过 Settings、硬编码代码根派生**（换目录后漂移，U13 迁移清单输入）：
| 路径 | 位置 | 漂移后果 |
|---|---|---|
| `knowledge/review_lessons.json` | `src/agents/lessons.py:12`（运行时写入，`.gitignore:27` 承认） | 换代码目录后读到空经验库，且跨 profile 共享写（报告 §7.3 已登记） |
| `data/futures_source_ledger.json`（兜底） | `src/exchange/binance_futures.py:201-202` | 生产经 `exchange/factory.py:65` 传入 profile 路径；**直接构造 Broker 的脚本走代码根兜底** |
| `data/signal_scanner_state.json`（看板读） | `src/dashboard/app.py:1020` 用 `settings.project_root`，而写入用 `runtime_path`（`service.py:340`） | live profile 下写 `data/live/…`、看板读 `data/…`，**读错账本**（显示层不一致） |
| `project_root/data`（health_watch 自实现 profile 分支） | `src/core/health_watch.py:82-95,97-134`；`scripts/health_watch.py:23,42-46` | 不经过 RuntimePaths，第二套解析逻辑 |
| 旧入口状态文件 | `scripts/run_phase1.py:41,55,69,99,170,181` | 直接用 `settings.project_root`，无 profile 语义 |
| knowledge_dir 混放 | `config_loader.py:95-97`（不在 data 内、不受 `runtime_path` 校验） | 只读策略卡与运行时输出同目录，迁移无法只搬其一 |

### C. 共享出口根（必须唯一、跨 profile 共享）
- 限流冷却：`config_loader.py:61-63` `project_root/data/public_rate_limit_state.json`（有意跨 profile，但随代码根漂移 → **换目录后 418/429 冷却被绕过**）。
- LLM 跨进程准入锁：`src/llm/request_coordinator.py:30-36` `parents[2]/data/shared/llm_coordination`（可被 `MTA_LLM_COORDINATION_DIR` 覆盖）。
- `MTA_EGRESS_ID` 仅作为字符串进 manifest（`service.py:134`），与上述路径无绑定。

**换不可变代码目录后必漂移清单（U13 直接输入）**：① 全部 `runtime_path` 状态（含实例锁 `service.py:85`——换目录即换锁，**两个 worktree 可同时管理同一账户**，违反 K01 `:72`）；② 共享限流状态与 LLM 协调锁；③ `knowledge/` 读取与 `review_lessons.json` 写入；④ 公共行情快照缓存 `src/data/binance_public.py:20,529`；⑤ `.env/.env.local` 读取（`config_loader.py:117-118`）及看板写 `.env`（`dashboard/app.py:410-411,2144`）——控制面写入代码目录，代码目录不可变化后该入口失效；⑥ 离线工具链（`create_recovery_bundle.py`、`report_release_readiness.py`、`run_offline_regression.py` 等 ROOT=parents[1]）。

---

## 3. 声明与实现不一致清单

| # | 声明 | 实现 | 影响 | 级别 |
|---|---|---|---|---|
| 1 | `docs/WorkBuddy_DeepSeek_V4.1_实盘决策适配说明_2026-09-16.md:36-40`：“仅当**实盘进程**显式携带 MTA_AI_DECISION_PROVIDER 才使用 WorkBuddy” | `factory.py:29-30` 只判断 agent 名与环境变量，**无 profile 判断**；`scripts/verify_llm_provider.py:24` 也不读 profile | Demo 进程同样可切 WorkBuddy；“实盘专属试用”约束未落地 | 中 |
| 2 | `settings.yaml:91-93` 注释：WorkBuddy“仅能经 MTA_AI_DECISION_PROVIDER=workbuddy 显式启用，且只作用于 ai_trader” | 与实现一致（`factory.py:30`） | —（一致项，供 U00 契约确认） | 低 |
| 3 | `settings.yaml:71` `mock: false`（暗示可配置关闭 Mock） | 无代码读取；Mock 仅由缺 key/模型 none 决定（`factory.py:53-54`） | 配置无法阻止/触发 Mock；易误读为已禁用 | 中 |
| 4 | `config/agents.yaml:4-9` strategy_trader 声明 `model: deepseek-v4-flash` | 主图实际 `model=None` 确定性执行（`src/core/graph.py:351`；决策链路报告 §3.1 `:98` 已指出） | 维护者误判策略通道走模型 | 低 |
| 5 | `release_manifest.py:22-29` DEPENDENCY_FILES 代表“依赖指纹” | 缺 `requirements-lock.txt`；README.md:26、代码维护规范.md:100、`verify_dependency_lock.py:82` 以 lock 为准 | 依赖指纹与部署锁脱节（F13/DC14 负例目前会漏检） | 高 |
| 6 | 规格 §11.1 与部署手册隐含“双 profile 托管” | `WindowsServiceConfig`（`windows_service.py:65-85`）无 profile 字段，`run_arguments()`（`:92-99`）不传 `--profile`；`config/windows_service.json:9` port=8000，而 live 控制台固定 8001（`config_loader.py:57-58`） | SCM 托管无法表达 live；总方案 `:310` 已登记 | 中 |
| 7 | `settings.yaml:113-117` 注释把 `EXECUTION_MODE` 当作模式来源 | `exchange/factory.py:40` `mode = profile or EXECUTION_MODE or default`；profile=None + `EXECUTION_MODE=live` 时 live 账户写入公共 data 根（`config_loader.py:41-43`） | environment/账户与状态根可错配且无校验 | 高 |
| 8 | `release_manifest.py:146` “不读 .env 内容” | 一致（无 `.env` 哈希逻辑） | —（一致项） | 低 |
| 9 | WorkBuddy 文档 `:72` “不使用 SenseNova 共享请求协调器” | 一致：`factory.py:30-37` 在 coordinator 创建前返回 | —（一致项） | 低 |
| 10 | WorkBuddy 文档 `:71` “子进程遵守主决策绝对 deadline” | 一致：`workbuddy_client.py:199-203` 取 min(deadline) | —（一致项） | 低 |
| 11 | 规格 §9 `:219` “最终交付包含可复核运行包” | `create_release_package.py:34-40` SAFE_DOCS 白名单不含 2026-09-19 v3 规格/决策链路报告等；tracked 的 `scripts/*.py` 全量入包（`:69-73`），仅无 git 兜底分支才排除 diag_/probe_/verify_（`:81`） | 发布包含诊断脚本、缺最新规划文档 | 低 |
| 12 | 规格 §4 K01 `:70` 字段名 `artifact_hash / effective_config_hash / model_route_hash / runtime_instance_id` | `ReleaseManifest`（`release_manifest.py:117-134`）实际为 `content_hash / strategy_config_hash / model_config_hash`，且无 artifact/effective/model_route/runtime_instance 字段 | U00 冻结 K01 契约时需做显式映射或改名，否则 golden fixture 与实现漂移 | 中 |
| 13 | 观测表字段名 `config_hash` | `service.py:425` 实际写入 `content_hash`（代码哈希），非配置哈希 | 报告/门禁若按字段名理解会误读证据 | 低 |
| 14 | 决策链路报告 §3.3 `:118-122` “静默 Mock、配置优先级”为**待修复设计** | 实现仍为原状（`factory.py:53-54,49-52`） | 报告是设计声明，非已实施记录；避免误读为已修复 | 中（提示） |
| 15 | `agents.yaml:14` 注释 “SenseNova V4 Flash 不接受 max；xhigh 是网关实际支持的最高档” | 离线无法核实供应商现行枚举 | UNKNOWN（见 §4） | UNKNOWN |

---

## 4. 未知与限制

**未能核实（UNKNOWN）**
- 供应商当前实际接受的模型名/effort 枚举（无法联网；`agents.yaml:14` 注释与 WorkBuddy 文档 `:8` 仅为历史记录）。
- 本机实际生效的 `MTA_PROFILE` / `EXECUTION_MODE` / `.env.local` 取值、WorkBuddy CLI 安装版本与路径（受禁令约束，未读取 .env/.env.local、未启动进程、未执行外部命令；`workbuddy_client.py:29-42` 支持任意覆盖，故当前运行路由无法在本报告断言）。
- 当前 git HEAD 值（只读文件系统，未运行 git 命令）。
- `knowledge/strategy_cards.json` 仅按结构核对（单行 JSON，9 张卡）；未做逐卡语义/回测核验。

**需 U15/E 后续验证**
- 换 worktree/换代码目录后实例锁与账户作用域是否互斥（K01 `:72`、F18）。
- `MTA_RELEASE_ID` 覆盖 release_id 后的报告链是否可被伪装（`service.py:135`）。
- `release_observations` 的 release_id 绑定是否足以在“身份不符”时拒绝开仓（当前仅报告期过滤，`release_evidence.py:328-391`）。
- Windows SCM 双 profile 托管（port/profile 参数化）。
- Demo/live 缺 key 负例（DC14/F13）与“指纹必变”负例（F13）的可执行验收脚本尚不存在（`scripts/smoke_release_acceptance.py` 只覆盖证据/门禁计数）。

**偏差与声明**
- 同模型偏差：本报告作者与编排者使用同一底层模型（deepseek-v4.1-flash），本报告**不构成独立交叉审计**；按规格 §2 `:46`，审查记录须注明该同模型限制。
- 明确声明：未读取任何凭据（未打开 `.env`、`.env.local` 或 `multi-agent-trader/data/`）；未触碰运行目录；未启动服务/进程；未联网；未写任何文件；未执行 git 提交/推送/打标签。
- 本报告所有断言均给出 file:line 证据；无法定位的项标 UNKNOWN，未做推测。

---

### 基线正面项（供 U15 复用时保留，不计为缺口）
- 启动即生成脱敏 manifest 并写库/写盘，失败留 critical 事件（`service.py:129-156`）。
- account_ref 为 API key 不可逆短哈希（`trade_reconciliation.py:66-69`）。
- 开仓授权绑定 `environment/scope_hash/runtime_instance_id` 且有 HMAC 签名与进程内密钥（`decision_pipeline.py:30-61`；`entry_readiness.py:154-177`）。
- 实例锁在 Windows 用只读句柄判活，避免 PID 复用/误杀（`instance_lock.py:24-51`）。
- profile 路径逃逸防护（`config_loader.py:44-53`）。
- 离线 profile 锁与依赖锁校验脚本存在（`verify_profile_lock.py`、`verify_dependency_lock.py`），可作为 U15 验收入口，但需补“结果另存/启动门禁”与 lock 文件覆盖。
