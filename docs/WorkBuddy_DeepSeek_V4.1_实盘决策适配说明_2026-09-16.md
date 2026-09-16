# WorkBuddy DeepSeek‑V4.1‑Flash 实盘决策适配说明

最后更新：2026-09-16。适用提交：`03b9dcf`、`f23dc9a`。

## 1. 目标与边界

WorkBuddy Code CLI 是编码 Agent，不是 Binance 客户端，也不是原生 OpenAI Function
Calling 服务。项目仅把其内置 `deepseek-v4.1-flash` 作为 `ai_trader` 的临时决策模型；
它不能直接读取项目、访问交易所或执行订单。

启用后链路为：

```text
市场/持仓摘要 → WorkBuddy StructuredOutput → 本地工具选择
             → 本地行情工具 → submit_decision
             → 证据校验 → ActionSpace → Risk → 签名 → Outbox → Binance
```

模型输出只是候选决策。任何一步异常、超时、越权函数、参数错误或证据不足都会安全降级为
`no_trade`，不会绕过既有退出和保护路径。

## 2. 权限模型

适配器只向 WorkBuddy CLI 开放内部 `StructuredOutput`：

- 不开放 Read、Write、Edit、Bash、浏览器或 MCP；
- 不传递 `.env`、API Key、Secret、签名、账户编号或 Binance 客户端；
- WorkBuddy 不执行行情查询；它只能选择本系统注册的工具，由 orchestrator 在本地执行；
- 返回函数名必须在本轮动态白名单中，参数必须是 JSON object；
- `submit_decision` 仍经过查询来源、数据时间、仓位、方向、止盈止损和组合规则校验。

实现位于 `src/llm/workbuddy_client.py`，构造入口位于 `src/llm/factory.py`。

## 3. 启用、验证与回退

仅当实盘进程显式携带以下变量时，`ai_trader` 才使用 WorkBuddy：

```powershell
$env:MTA_AI_DECISION_PROVIDER="workbuddy"
```

默认不设置该变量，系统沿用 SenseNova `deepseek-v4-flash`。辅助市场、宏观、链上和复盘
Agent 也继续使用原提供方；这次试用不会把编码 Agent 扩展到全链路。

启动前可执行最小协议验收：

```powershell
$env:MTA_AI_DECISION_PROVIDER="workbuddy"
$env:MTA_PROFILE="live"
.venv\Scripts\python.exe scripts\verify_llm_provider.py
Remove-Item Env:MTA_AI_DECISION_PROVIDER
Remove-Item Env:MTA_PROFILE
```

该检查只要求模型选择一个虚拟工具，不读取交易账户、不连接 Binance、不创建订单。离线协议
回归使用：

```powershell
.venv\Scripts\python.exe scripts\smoke_workbuddy_adapter.py
```

回退时停止对应 live 实例，移除 `MTA_AI_DECISION_PROVIDER` 后按原方式启动。不得在同一个
运行进程中热切模型，也不得通过修改风险档来补偿模型差异。

## 4. 稳定性处理

- 每次决策使用无持久会话的 CLI 子进程，避免跨轮记忆污染；
- `--setting-sources user` 不加载项目级 Agent 配置；
- 上下文按角色设置上限，并在 24,000 字符前 fail-closed，避免 Windows 命令行截断；
- 禁用 `--system-prompt-file`：该组合曾使 CLI 退化为交互会话，修复见 `f23dc9a`；
- 子进程遵守主决策绝对 deadline；超时由 orchestrator 记录并降级 `no_trade`；
- WorkBuddy 不使用 SenseNova 的共享请求协调器，因此健康页中的 provider count 不能单独
  证明它是否运行；应以 `ai_trader` 审计记录、模型协议验收和无残留 CLI 会话共同判断。

## 5. 已知限制

- WorkBuddy CLI 自带固定系统开销，单轮成本和延迟可能高于直接 API；
- 为满足 Windows 命令行限制，模型看到的是压缩上下文；原始证据仍由本地 Validator 掌握；
- 辅助情报 Agent 的 429、网络超时仍可能延长整个慢循环，即使 WorkBuddy 本身正常；
- 当前切换是进程级试用，不是永久配置。关机或按默认启动命令重启后不会自动启用；
- 模型可用不等于发布门禁通过，也不代表一定产生交易。

## 6. 观察指标

试用期间至少检查：

- `/api/health` 与 `/api/ready`；
- `runtime.last_slow_cycle_error`、慢循环耗时和 `llm_time_budget_exhausted` incident；
- `order_outbox.unknown_orders/submitting_orders`；
- 保护单 `missing_protection/unsafe_orders`；
- `ai_trader.jsonl` 是否出现合法 `submit_decision`，以及非法决策是否降级；
- WorkBuddy CLI 是否存在慢循环结束后仍残留的交互会话。

任何残留会话、连续超时或错误率显著升高都应停止试用并回退原提供方；不得通过放宽风险、
提高杠杆、扩大白名单或取消证据门槛解决模型链路问题。
