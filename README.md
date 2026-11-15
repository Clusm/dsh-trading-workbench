# dsh-trading-workbench

把 **AI 决策链 + Demo 交易执行 + 风控** 搬进 [DeepSeek Harness](https://github.com/deepseek-ai) 的 Web UI：一个能看、能查、能手动跑一轮的交易工作台插件。

![操作台：账户与风险、持仓与保护、AI 摘要、今日流水](docs/screenshots/workbench-cockpit.png)

![AI 决策：本轮结论、确定性评分卡、红蓝对比](docs/screenshots/workbench-ai-decisions.png)

## 它解决什么问题

LLM 参与交易时，最贵的不是"下单"，而是**为什么这么决定**和**当时到底发生了什么**。这个插件把每一轮决策做成可复核的事实：

- 决策链（分析 → 红队/蓝队 → 决策 → 风控 → 操盘手）逐段留痕，失败也留痕；
- 每轮带**原因码**与**归因**（类别 / 阶段 / 角色 / 一句话 / 下一步）；
- 逐调用审计：角色、尝试、延迟、token、结束方式、校验错误；
- 审计归档**按日分文件、全量保留**，默认只看今天，其他日期按日期查询；
- 执行前有确定性闸门（入场区间、报价年龄、止损止盈合法性、轮内规则冻结），风控拒绝也写进轮次。

## 功能

| 区域 | 内容 |
| --- | --- |
| 操作台 | 账户与风险带、持仓与保护状态、AI 摘要、今日流水、待处理清单 |
| AI 决策 → 本轮 | 结论与原因码、确定性评分卡（S/A/B/C + 门禁）、红蓝对比、辩论双侧、决策依据、证据抽屉 |
| AI 决策 → 历史 | 按归档日期查询（今天默认）、筛选失败/不交易/成交、耗时分布、单轮详情 |
| AI 决策 → 分析 | 轮次分布、原因码构成、权益曲线、资金口径（服务端 /pnl）、AI 日报 |
| 设置 | 连接与账户、Demo/实盘凭据接入、自动调度、只读限额、运行身份诊断 |
| 顶栏 | 预检、运行一轮、启动/停止自动交易、服务一键启动、环境初始化向导 |

## 架构

两半，进程隔离：

```
DSH Web UI ── HTTP(loopback) ── Host 插件 (Node ESM, 本仓库 plugin/)
                                   │ 路由 /trading-workbench/api/*
                                   │ AI 决策链（ctx.llm.stream）
                                   │ 按日归档 data/rounds/YYYY-MM-DD.jsonl
                                   │
                                   └── HTTP(loopback, Bearer) ── 交易服务 (Python, service/)
                                                                   行情 / 账户 / 持仓 / 订单 / 闸门
                                                                   Demo 守卫 + 风控 + 账本
```

- **浏览器不直连交易所、不持 token**：所有数据经 Host 路由，写命令要求 loopback + same-origin。
- **服务是事实源**：行情、账户、持仓、订单只来自服务；取不到就显示不可用，不编数。
- **没有凭据 → 只读**：Demo key/secret 只写本机 0600 文件，不经过浏览器存储、不进归档、不落日志。

## 安装

```sh
# npm（发布后）
dsh plugin --profile <profile> add dsh-trading-workbench

# 或在 DSH 里：设置 → 插件 → 搜索 dsh-trading-workbench → 安装
```

本地/离线安装（仓库根就是可分发的 bundle）：

```sh
npm pack                                  # -> dsh-trading-workbench-0.1.0.tgz
dsh plugin --profile <profile> add ./dsh-trading-workbench-0.1.0.tgz
```

或者直接从 Git 安装：

```sh
dsh plugin --profile <profile> add github:Clusm/dsh-trading-workbench
```

安装后重载插件（或重启 DSH）：侧边栏出现「交易工作台」，顶栏多出「AI 决策」。

## 首次运行

1. 打开工作台。没有交易服务时，顶部横幅显示 **服务不可达**：
   - 已装过服务 → 按钮是 **启动服务**；
   - 新机器 → 按钮是 **初始化服务**。
2. **初始化服务** 先如实检测（Python / 虚拟环境 / 依赖 / 服务），再**一键初始化**：
   创建 `<dataDir>/service-venv` → `pip install -r service/requirements.txt` → 启动服务 → 用服务自己的 `/health` 校验。
   每步有状态与最近输出，失败带日志尾部、可重试；没有 Python 会直接告诉你去装，而不是给个无效按钮。
3. 服务就绪后只读视图可用；要真正下单还需要 Demo 凭据（设置 → 连接与账户）。

## 配置

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `dataDir` | `$DSH_HOME/data/trading-workbench`（无 `DSH_HOME` 时 `~/.dsh/data/trading-workbench`）；包内 `data/` 已有内容则沿用 | 归档、配置、日志、token、凭据 |
| `serviceBaseUrl` | `http://127.0.0.1:8791` | 交易服务地址 |
| `tokenFile` | `dataDir/.api-token` | 服务 token |
| `autoSchedule.enabled` | `true` | 本机每 10 分钟自动跑一轮 |

环境变量：`WORKBENCH_SERVICE_PYTHON`、`WORKBENCH_SERVICE_ENTRY`、`WORKBENCH_ALLOW_SERVICE_START=off`、
`WORKBENCH_ROUNDS_RETENTION_DAYS`（默认全量保留）、`WORKBENCH_DATA_DIR`。

## 安全边界

- **Demo-only**：服务启动时要求 demo 行情基址；没有凭据时全链路只读。
- **两把开关**：本机调度器（自动跑）与服务端控制态（允许开新仓）互相独立；手动「运行一轮」只受预检与服务可用性约束。
- **不代管资金与凭据**：插件不读浏览器存储里的密钥；实盘接入需要显式环境与确认流程。
- 卸载插件**不平仓、不撤保护单**。

## 仓库结构

仓库根就是可分发的 DSH bundle（`package.json` 里有 `dsh.bundle` + `dsh.client`）：

```
index.js / client.js      # Host 入口 + 客户端 UI（无构建步骤，直接由 DSH 加载）
host/                     # Host 半：integration / ai 决策链 / round-archive / service-setup / scheduler
locale/  icon.svg         # 中英文案与图标
cordis.patch.yml          # 挂载到 DSH 组合的 bundle patch
service/                  # Python 交易服务（FastAPI，Demo 守卫、闸门、账本）
test/                     # node --test 套件
client/*.mjs              # 源码约束检查 + 渲染冒烟
scripts/                  # 启动/验证/发布辅助脚本
docs/                     # 设计文档与截图
```

## 开发

```sh
npm test                 # checks + render + node --test
node client/checks.mjs   # 源码约束（文案键、路由、安全边界、分发前提）
node client/render-smoke.mjs
node --test test/*.test.mjs
```

## 许可

未定（发布前填写）。本仓库内容仅供研究与 Demo 使用，不构成投资建议。
