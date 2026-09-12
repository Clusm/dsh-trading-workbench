# multi-agent-trader

本地运行的多 Agent 数字货币合约实验系统。系统仅处理币安 USD-M、USDT 本位永续合约
白名单，默认连接 Binance Demo Trading；AI 生成候选决策，确定性代码负责风控、幂等
下单、保护单、账户对账和完整审计。

当前资金核算以 500 U 为会话起点。Web3 与市场微观结构情报处于只读影子阶段，执行权重
固定为 0，不会改变现有风险档或交易决策。

LLM 默认通过 SenseNova OpenAI 兼容网关调用 `deepseek-v4-flash`。本机凭据建议放在
`.env.local`；该文件后加载并覆盖 `.env`，两者均不会进入 Git。

## 快速开始

环境要求：Windows、PowerShell、Python 3.11+。

```powershell
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements-lock.txt
Copy-Item .env.example .env
.venv\Scripts\python.exe scripts\verify_profile_lock.py
.venv\Scripts\python.exe scripts\run_web.py
```

维护重构前后可用 `scripts/benchmark_runtime_baseline.py` 做同机离线性能对照；结果不代表
交易所网络性能，也不会改变风险或执行阈值。

控制台地址：`http://127.0.0.1:8000`。

执行环境由 `.env` 中的 `EXECUTION_MODE` 决定：

- `demo`：币安模拟盘，主网行情与虚拟资金，当前默认模式；
- `testnet`：传统测试网；
- `paper`：本地纸面成交；
- `live`：真实账户，必须显式配置对应密钥并完成上线核验。

非 paper 环境缺少密钥时，Broker 工厂会拒绝启动，绝不静默切换资金口径。不要把
`.env`、API 密钥、签名请求或 `data/` 运行状态提交到 Git。

## 运行模型

- 慢循环每 10 分钟运行：行情与策略扫描、双通道决策、确定性风控和执行。
- 快循环每 30 秒运行：账户/订单/保护单对账与存量仓位安全检查。
- `strategy` 通道消费内置策略运行时生成的确定性策略票据；研究仓库不参与生产导入。
- `ai` 通道使用 LLM 和白名单行情工具形成候选决策。
- 当前所有实际 LLM 节点统一使用 `deepseek-v4-flash`；Risk/Execution 是确定性代码。
- AI 决策节点使用 SenseNova 支持的最高 `xhigh` 推理档；其他节点按职责使用 low/high。
- 所有新开仓必须携带初始止损和止盈，并依次经过证据契约、影子决策质量、
  组合状态、ActionSpace、RiskGuard、运行时签名和 Outbox。Broker 会拒绝未签名开仓。
- Position Guardian 独立复核已有 AI 仓位的结构无效点，当前保持 `shadow`；
  数据不可验证时显示 `REVIEW_DUE`，不会猜测退出。
- 市场情报覆盖 20 个明确配置的 USD-M 合约；AI 单轮行情查询预算为 4 次。

## 核心安全边界

- 风险参数由 `config/risk_learning.yaml` 与 `config/risk_profile.lock.json` 锁定。
- 交易范围由 `config/settings.yaml` 的 `market_universe.core_symbols` 限定。
- 账户流、账户镜像和订单镜像目前主要用于观察、持久化与对账。
- Outbox 对不确定提交进行隔离，避免网络超时后重复下单。
- 保护单镜像检查缺失、孤儿、方向和数量不一致。
- SQLite 保存决策、意图、风控、订单、快照、事件、incident 和影子评估结果。
- Binance 已确认的普通平仓、风险退出及交易所侧 SL/TP/人工平仓会以稳定成交键写入
  `trade_outcomes`，并连回原始开仓论点；未确认成交不会用估算值污染策略评价。

## 常用检查

```powershell
# 核心配置未漂移
.venv\Scripts\python.exe scripts\verify_profile_lock.py

# 当前环境与精确依赖锁一致
.venv\Scripts\python.exe scripts\verify_dependency_lock.py

# 基础回归
.venv\Scripts\python.exe scripts\check_quality.py
.venv\Scripts\python.exe scripts\run_offline_regression.py
.venv\Scripts\python.exe scripts\check_execution_boundaries.py
.venv\Scripts\python.exe scripts\smoke_service_lifecycle.py
.venv\Scripts\python.exe scripts\smoke_ui.py

# 只读查看 Demo 候选、闭合归因交易和观察天数门槛
.venv\Scripts\python.exe scripts\report_demo_acceptance.py

# 服务运行时只读采样本地 API；可显式追加 --include-binance-public 测公共网络
.venv\Scripts\python.exe scripts\benchmark_demo_endpoints.py --samples 20

# 显式联网验证 LLM 文本与 Function Calling（会产生一次小额 API 调用）
.venv\Scripts\python.exe scripts\verify_llm_provider.py

# 安装精确锁定的运行依赖和开发工具
.venv\Scripts\pip.exe install -r requirements-dev-lock.txt
```

`requirements.txt`/`requirements-dev.txt` 表示允许升级范围；日常安装和 CI 使用对应的
`*-lock.txt` 精确版本。依赖升级必须在隔离环境重新解析锁文件，并通过全量离线回归，
禁止只改某个传递依赖后直接部署。

## 目录

- `src/core`：服务生命周期、图编排、资金、通道、控制和风险守卫；
- `src/agents`：Agent 节点、模型、策略与执行逻辑；
- `src/exchange`：Binance Broker、Outbox、私有流、镜像和安全监控；
- `src/intel`：只读 USD-M/Web3 情报与 A/B/C 影子评估；
- `src/audit`：SQLite 迁移、审计查询和 JSONL 日志；
- `src/dashboard`：FastAPI 控制台；
- `scripts`：启动、离线回归、恢复、诊断与显式在线核验脚本；其中 `probe_*`、
  `diag_*`、`verify_*_live` 不进入默认 CI，`cleanup_*`/`repair_*` 必须先预览并做后置核验；
- `docs`：架构、运行恢复、维护规范和历史设计记录。

USD-M 回放位于 `src/core/usdm_replay.py` 和 `src/core/strategy_evaluation.py`，支持重复
开仓加权、部分平仓余量、费用/资金费/滑点及信号到成交延迟归因。事故夹具必须去敏、
带内容哈希并显式声明数据覆盖。仓库同时保留合成边界样本与 9 月 7 日真实审计成交导出的
归一化夹具；后者不包含订单 ID、审计原文、提示词或账户字段。真实夹具仍不能代替
200 候选、30 笔 Demo 闭合交易和 7 天观察门槛。

进一步阅读：

- [项目说明书](docs/项目说明书.md)
- [运行与故障恢复手册](docs/运行与故障恢复手册_2026-09-06.md)
- [问题盘点与整改状态](docs/项目问题盘点与优化计划_2026-09-07.md)
- [代码维护规范](docs/代码维护规范.md)
- [稳定性与 AI 情报升级计划](docs/USD-M合约系统稳定性与AI情报升级开发计划_2026-09-05.md)

本项目不保证盈利。Demo 测试通过不代表实盘安全，也不构成投资建议。
