# DSH Trading Workbench（`dsh-trading-workbench`）

DeepSeek Harness 的交易工作台插件：AI 决策链、确定性策略管理（支持「规则直通 0 延迟」与「AI 辅助过滤」双执行引擎、闭合 K 线与 EMA/RSI 指标原语、单策略自动熔断与绩效归因）、按日归档的轮次历史、2 秒轻量持仓标记价及 Demo 执行与风控视图。
它由两半组成：

| 半 | 语言 | 作用 |
| --- | --- | --- |
| Host 插件（本包） | Node ESM | 注册 `/trading-workbench/api/*`、跑 AI 决策链与双执行引擎策略调度、把轮次写成本地按日归档、调用本机交易服务 |
| 交易服务（`service/`） | Python | 行情/账户/持仓/订单事实源、策略版本与熔断存储、确定性规则求值、Demo 下单与闸门 |

## AI 交易与策略执行的范围

- **范围**：在固定的 20 币白名单内，每 10 分钟一轮，支持「原生 AI」或「已授权策略（规则直通 / AI 辅助过滤）」，决定做不做 / 做多还是做空 / 入场区间 / 止损止盈 / 仓位；有持仓时只做减风险的管理动作。
- **不选品**：白名单运行时不轮换；`universe.market_scan` 会展示白名单外的全市场异动，但标记 `in_whitelist=false`，只作知情、不作候选。
- **不联网、不调用工具**：模型只读一份确定性生成、带 hash、可复现的证据快照（`md-evidence/1`），所以"为什么这么决定"可以复核。
- **7 个角色**：`analysis` → `red_team`/`blue_team`（固定多空、对称两轮）→ `decision`（三选一）→ `risk`（只能收紧）→ `execution`（转订单计划，不直接下单）→ `review`（每日归因，不阻塞交易）。
- **只能更严**：无论是 AI 决策还是规则直通，最终都必须通过 P1/P2 硬风控（R00–R25）与执行前闸门；AI 或策略建议均不能放宽硬上限。

## 安装

在 DSH 里（推荐，无需命令行）：**设置 → 插件 → 搜索 `dsh-trading-workbench` → 安装**。

命令行等价：

```sh
dsh plugin --profile <profile> add dsh-trading-workbench
```

或从本地目录 / tgz 安装：

```sh
dsh plugin --profile <profile> add ./dsh-trading-workbench-0.2.0.tgz
```

安装后**重载插件**（或重启 DSH）：侧边栏出现「交易工作台」，顶栏提供「操作台 / AI 决策 / 策略管理」三个一级工作区。

## 首次运行

1. 打开「交易工作台」。若本机还没有交易服务，顶部横幅会显示 **服务不可达**：
   - 已经装过服务 → 横幅按钮是 **启动服务**；
   - 还没装过（新机器）→ 按钮是 **初始化服务**，点开是一张环境清单。
2. **初始化服务** 会先如实检测：Python 有没有、虚拟环境在不在、依赖装没装、服务通不通；
   然后 **一键初始化**：创建 `<dataDir>/service-venv` → 安装 `service/requirements.txt` → 启动服务 → 用服务自己的 `/health` 校验。
   每一步都有状态和最近输出，失败会带日志尾部，可重试。
   - 需要本机有 Python 3.11+（建议 3.13）；没有时向导会直接告诉你先装 Python，不会给一个无效按钮。
3. 服务就绪后：
   - 只读视图（行情、账户、持仓、代价、复盘）直接可用；
   - **下单仍然受 Demo 守卫与风控闸门约束**，没有配置凭据时服务只读。

## 配置（`cordis.patch.yml` 的 `config:`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `dataDir` | `$DSH_HOME/data/trading-workbench`（无 `DSH_HOME` 时 `~/.dsh/data/trading-workbench`）；包内 `data/` 已有内容时沿用 | 轮次归档、`llm-config.json`、日志、API token、凭据文件所在目录 |
| `serviceBaseUrl` | `http://127.0.0.1:8791` | 本机交易服务地址；也可指向别人已经跑着的服务 |
| `tokenFile` | `dataDir/.api-token` | 服务 token（首次由服务生成） |
| `autoSchedule.enabled` | `true` | 是否允许本机每 10 分钟自动跑一轮（`WORKBENCH_AUTO_SCHEDULE=off` 可整体关闭） |
| `chain` | `{}` | AI 链参数（超时、上限等） |

环境变量：`WORKBENCH_SERVICE_PYTHON`（指定解释器）、`WORKBENCH_SERVICE_ENTRY`（指定服务入口）、
`WORKBENCH_ALLOW_SERVICE_START=off`（禁用一键启动）、`WORKBENCH_ROUNDS_RETENTION_DAYS`（归档保留天数，默认全部保留）。

## 平台

- Windows 与 Linux/macOS 同一套 `service/run_service.py`；虚拟环境路径按平台取 `Scripts/python.exe` 或 `bin/python`。
- 已在 **dsh 0.2.2-rc.1** 上验证；包声明 `engines.dsh = ">=0.2.2-rc.1 <0.3.0-0"`、`node >= 22.19`。宿主不满足时安装会被拦下。

## 安全边界

- 所有 HTTP 路由 **只接受 loopback**；写命令额外要求 same-origin 与 JSON 正文。
- **Demo-only**：Demo 守卫要求 demo 行情基址；没有凭据时服务是只读的。
- 凭据只从本机文件/环境读取，**不进入插件包、不进入归档、不出现在 UI**。
- 轮次归档只保存决策与执行事实（原因码、归因、调用审计、证据哈希），不保存 Prompt 正文。

## 卸载

在 DSH 的插件页卸载，或 `dsh plugin --profile <profile> remove dsh-trading-workbench`。
卸载**不会**关闭交易所仓位，也不会撤销保护单；数据目录保留，删除前请自行确认。
