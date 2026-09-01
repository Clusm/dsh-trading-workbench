# multi-agent-trader

个人实战学习项目：多 Agent 加密货币交易实验平台。

当前阶段：phase 4（OpenCode Zen LLM + Tool Call按需查询任意symbol + 200U会话资金 + Binance Demo Trading + 交易软目标）。

## 运行

```bash
# 阶段1：接入真实免费数据源，跑一轮完整决策
.venv/Scripts/python.exe scripts/run_phase1.py

# 持续运行，每5分钟一轮
.venv/Scripts/python.exe scripts/run_phase1.py --loop

# 生成每日复盘
.venv/Scripts/python.exe scripts/run_review.py

# 启动本地 Web 控制台（默认后台每5分钟跑一轮）
.venv/Scripts/python.exe scripts/run_web.py
# 浏览器打开 http://127.0.0.1:8000
```

执行环境：
- `EXECUTION_MODE=demo`：Binance Demo Trading，主网真实行情 + 虚拟资金，推荐。key 在 https://demo.binance.com 的 API Management 创建，填入 `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`。
- `EXECUTION_MODE=testnet`：传统测试网，模拟行情。
- `EXECUTION_MODE=paper`：本地纸面成交，不需要币安 key。
- 缺少对应 key 时自动回退 paper。
- 会话资金固定 200U；会话权益亏到 ≤1U 时自动全平并从 200U 重启（见 `config/settings.yaml` 的 `capital`）。
- 行情扫描：Top 40 USDT永续全部提供 1m×600（10小时）、15m、1h K线及ATR/量能/资金费率/OI指标。
- 交易目标（软目标，非强制）：每小时短线 5 笔、每日小时级 3 笔；是否交易、品种、方向均由主 Agent 自主决定。
- 主 Agent 支持 Tool Call：可主动查询任意 USDT 永续的 1m/5m/15m/1h/4h/1d K线、ATR、量能、资金费率、OI，也可查询 Top 活跃合约。

## 双交易通道（当前架构）

- `signal_scanner`：确定性扫描 strategy_lab final 策略卡，生成 SignalTicket。
- `strategy_trader`：策略交易 AI，执行 ticket，订单来源 `strategy`。
- `ai_trader`：AI 自决策通道，独立判断，订单来源 `ai`，默认逐仓。
- 虚拟账本 60/40 每日 UTC 重算；AI 账户当日 -30% 熔断；两个通道分别计算保证金上限。
- 所有新开仓强制同时设置初始止损和止盈。
- 设计文档：`docs/dual_agent_design.md`

LLM 走 OpenCode Zen：主 Agent=deepseek-v4-pro（reasoning_effort=max），其余=deepseek-v4-flash。没有 key 时自动回退 Mock LLM。跑通后会：

- 在 `data/agent_trader.sqlite` 写入 Agent 运行、LLM 调用、决策与风控记录；
- 在 `data/artifacts/` 保存每个 Agent 的结构化输出；
- 在 `data/logs/YYYY-MM-DD/` 写 JSONL 原始日志。

## 目录

- `src/core`：配置、LangGraph 图、Agent 运行记录
- `src/llm`：DeepSeek 客户端与 Mock 客户端
- `src/agents`：各 Agent 节点与数据模型
- `src/dashboard`：本地 FastAPI 控制台
- `src/audit`：SQLite 审计库与 JSONL 日志
- `knowledge`：策略报告与结构化策略卡片（strategy_cards.json）
- `config`：全局配置、Agent 配置、风险档位
