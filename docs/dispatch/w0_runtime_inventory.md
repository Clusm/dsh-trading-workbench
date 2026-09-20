# W0 运行隔离只读盘点（U00 证据）

日期：2026-09-20（北京时间约 23:05）。执行：I。性质：只读盘点，未读取凭据内容、未停止任何进程、未改动运行目录。

## 1. 进程

| PID | 父 PID | 启动时间 | 命令行 |
|---|---|---|---|
| 12984 | 16840 | 2026-09-16 09:31:03 | `...\multi-agent-trader\.venv\Scripts\python.exe scripts\run_web.py --profile demo --host 127.0.0.1 --port 8000` |
| 25928 | 12984 | 2026-09-16 09:31:03 | 同上（子进程） |

活动证据（盘点时点）：

- `data/agent_trader.sqlite-wal` 修改于 23:03:37；
- `data/public_rate_limit_state.json` 修改于 23:03:42；
- `data/signal_scanner_state.json`、`market_universe_state.json`、`channel_state.json`、`futures_source_ledger.json` 修改于 23:01–23:03。

结论：demo 实例正在运行快循环，项目根是运行目录。

## 2. 代码根与状态根

- 代码根：`C:\Users\Tao\Desktop\币安\multi-agent-trader`，`main` @ `05d28e0`；工作区脏文件仅为 9-19/9-20 规划文档（未跟踪/1 个已修改）。
- 状态根：同目录 `data/`：`agent_trader.sqlite`（约 122 MB）+ WAL；`trading_service.lock`（2026-09-16 09:31:06，对应运行实例）；账户/控制/资金/通道状态；`logs/`、`backups/`、`recovery_bundles/`、`live/`、`shared/`、`reviews/`。
- 凭据：`data/` 与 `.env` 内容未读取。

## 3. 托管与启动路径

- Windows 服务：无与本项目匹配的服务（`Get-Service` 过滤 trader/binance/agent 无命中）。
- 计划任务：无匹配任务。
- 实际启动方式：分离进程方式运行（2026-09-16 09:31 启动），与本项目“SCM 延期、暂用分离启动”的现状一致。

## 4. 隔离结论（v1.1 手册 §2 规则）

1. 项目根为运行目录：禁止在项目根 checkout、合并、改配置、执行开发代码。
2. 规划基线提交在 `mat-wt/I`（`integration/unattended-v3`）完成；planning_commit=`5295632c748882574a8691d3e7b4b9df2ad7bf37`；运行 checkout 未动。
3. 全部开发/契约/审查在隔离 worktree 进行；子代理禁止读真实 `data/`、`.env`，禁止启动真实服务、下单、撤单、发真实告警。
4. 本次盘点不替代 D01（宿主托管决策）与 U12 OPS 验收；不构成“托管已达标”证据。

## 5. 后续事项（不阻断 W0）

- E/U13 需要基于本盘点输出 RuntimePaths 全消费者清单与兼容迁移；本次仅记录现状。
- 运行实例保持不动，直到用户另行指示部署/停止；W0–W3 开发不需要触碰它。
