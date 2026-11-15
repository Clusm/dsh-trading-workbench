# 03 · 币安 Demo 执行与硬风控设计（v1）

- 文档编号：03
- 日期：2026-11-09（Asia/Shanghai）
- 状态：设计稿（待评审）
- 上游输入：`trading-workbench/docs/00-需求基线.md`（唯一需求来源）
- 回答基线 §6 问题：**Q2（Demo 执行复用 vs 新写）**、**Q3（账户与账本）**、**Q6（风控硬规则）**、**Q10（失败路径）**
- 交付性质：**只做设计**。本文档编撰期间未发起任何网络请求、未下单、未读取或写入任何密钥；所有对 `multi-agent-trader` 的引用均为只读代码审阅。
- 下游消费者：t1 架构（服务边界/端口）、t2 AI 链（执行侧契约/仓位摘要）、t4 行情（新鲜度标记）、t5 UI（状态与原因码）、t6 评审。

---

## 0. 结论摘要（先看这一页）

| 议题 | 结论 | 依据章节 |
|---|---|---|
| **Q2 复用 vs 新写** | **新写轻量 Demo 适配器**（`demo_broker` + `demo_outbox` + `risk_rules` + `ledger`），**不整包复用** `BinanceFuturesBroker`（3456 行/190KB）与 `AuditDatabase`（3139 行/30+ 表）。**按"语义移植清单"继承 7 条不变量**，另从 `risk_guard.py` 同构移植日亏/回撤 latch（~80 行）。 | §2 |
| **Q3 账户** | 首选**独立 Demo 账户**（新 key）。若只能复用现有 demo 账户，则强制"接管前置条件"（空仓/无挂单/无 Algo 单/无脏账本），否则**拒绝启动**。 | §3、§4 |
| **Q3 账本** | **新库新曲线**：`data/workbench-v1.sqlite`；**会话权益 = 交易所权益 − baseline**（demo 自带资金不计入收益）；旧 demo 历史**不迁移、不读取、不混算**。 | §4 |
| **Q6 硬风控** | 26 条确定性规则（R00–R25），**两闸门**（计划闸 P1 + 提交闸 P2）+ 巡检闸 P3；AI 风控结论**只能收紧不能放宽**；全部阈值集中在 `config/risk.v1.yaml`，可直接落代码。 | §7 |
| **Q10 失败路径** | 20 条失败矩阵（F01–F20）+ 启动对账 12 步 + 重启恢复 + 人工收口清单；核心原则：**结果不确定时只查询不重发**（UNKNOWN 语义），**保护失败即平仓**（绝不裸仓）。 | §5、§6、§9 |
| **无 live 路径** | 三重强制：配置 schema 只接受 `demo`、代码内**不存在** `fapi.binance.com` 常量、每次请求前比对 host 白名单。附静态+运行时验证方法。 | §3.4、§11.3 |
| **明确不做** | 双账本（strategy/ai）、策略票据、Hedge 双边持仓、限价入场（v1.1）、收益流水精确对账、WebSocket 私有流、Windows 服务化。 | §1.2 |

---

## 1. 范围、假设与边界

### 1.1 本文档负责什么

1. **Demo 执行层**：账户/密钥/环境隔离、订单生命周期、`clientOrderId` 幂等、对账、仓位与权益跟踪。
2. **硬风控层**：单笔/组合/账户级规则、阈值表、执行位置、否决与降仓算法、kill switch。
3. **执行服务 API 与错误码**：插件 Host 调用的确定性接口。
4. **异常与恢复**：失败矩阵、重启恢复、人工干预边界。

### 1.2 本文档不负责（明确不做）

| 不做项 | 原因 | 归属 |
|---|---|---|
| 策略/通道/漏斗/多账本 | 基线 §3 已移除策略维度 | — |
| 6 个 agent 的 prompt / JSON schema / 推理档 | 属 AI 链 | t2 |
| 行情采集、指标、证据快照结构 | 属行情管线 | t4 |
| UI 组件与视觉 | 属 UI | t5 |
| 插件 Host/Client 注册与进程管理 | 属架构 | t1 |
| 限价入场单（GTC + 超时撤销） | 10 分钟决策周期下挂单引入额外状态机与"成交后无保护"窗口；v1 只用 MARKET 入场 | v1.1 再评估 |
| WebSocket 用户数据流（`ORDER_TRADE_UPDATE`） | 30s 对账已满足 Demo 精度；WS 带来 listenKey/断线补偿复杂度 | v2 |
| 手续费/资金费精确对账（原 `income_reconciliation.py` 64KB） | v1 只做订单级 round-trip PnL | v2 |

### 1.3 关键假设（若 t1 结论不同，仅影响 §8 的调用形态）

- A1：AI 编排在 DSH 插件 Host 侧；**行情/撮合/账本/风控在本地 Python 服务**（基线 §6 Q1 的推荐方向）。本文档把执行与风控设计成**一个可独立运行的 Python 服务**，API 是它的唯一入口。
- A2：本机单用户，服务只监听 `127.0.0.1`，用一次性 token 保护写接口（§3.3）。
- A3：币安 Demo 账户可用，且账户级持仓模式为 **one-way**（若为 Hedge，见 §3.2 与 §5.6 的 `positionSide` 分支）。
- A4：Demo 的 REST 契约与原项目实测一致（保护单必须走 `/fapi/v1/algoOrder`，见 §2.4 坑位 D1）；**M1 第一步用只读探针复核**（§11.3 T01–T03）。

---

## 2. Q2 · 现状评估：复用还是新写

> 只读审阅对象：`multi-agent-trader/src/exchange/binance_futures.py`、`src/agents/execution.py`、`src/exchange/order_outbox.py`、`src/core/risk_guard.py`、`config/settings.yaml`，以及被它们直接依赖的 `src/audit/database.py`、`src/exchange/protection_commands.py`、`src/exchange/trade_reconciliation.py`、`src/core/risk*.yaml`。

### 2.1 盘点

| 文件 | 规模 | 真实职责 | 与 v1 需求的重合度 |
|---|---|---|---|
| `src/exchange/binance_futures.py` | 3456 行 / 190KB | 签名 REST、时钟校准、`exchangeInfo` 过滤器、普通单/Algo 保护单提交、Outbox 接线、来源台账（strategy/ai）、保护单对账（`_resync_protection_impl` ~210 行）、平仓/减仓、退出预留、`close_all`、生命周期归因、`userTrades` 补记、收入对账入口 | 中（核心动作约 25% 行数） |
| `src/agents/execution.py`（`PaperBroker`） | 741 行 | 双虚拟账本 60/40、限价单队列、纸面成交、`_record_outcome` 归因 | 低（v1 无纸面模式） |
| `src/exchange/order_outbox.py` | 559 行 | `prepare→POST→UNKNOWN→按 clientOrderId 查询`、三分量数量守恒、成交修订、人工收口 | **高（语义必须继承）** |
| `src/core/risk_guard.py` | 204 行 | 日亏/会话回撤 latch、`anchor_id` 分段、`recovery_required` fail-closed、原子写 | **高（几乎可原样移植）** |
| `src/agents/risk.py` | 312 行 | 确定性单笔规则（杠杆/名义/保证金/RR/SL-TP 方向/降仓） | 中（规则清单可借鉴，实现需重写） |
| `config/settings.yaml` | 290 行 | 执行模式/凭证 env 映射、capital、channels、market_universe 等 | 低（只取执行与风险参数） |
| 依赖：`src/audit/database.py` | 3139 行 / 30+ 表 | 全量审计库 + 迁移 + release evidence + 收入/成交/生命周期表 | 低（v1 需 ~10 表） |
| 依赖：`src/exchange/protection_commands.py` | 67KB | 保护命令种类、三分量解析、撤旧挂新计划器（`plan_protection_refresh` / `execute_protection_plan`） | 低（v1 保护只需 2 张单 + 覆盖校验） |
| 依赖：`src/exchange/trade_reconciliation.py` | 29KB | 成交→仓位生命周期归因、净盈亏 | 低（v1 用订单级 PnL） |

### 2.2 为什么不能整包复用

1. **它自带 live 路径**。`src/exchange/factory.py:12-16` 的 `BASE_URLS` 含 `https://fapi.binance.com`，`VALID_MODES` 含 `live`，`capital.py` 有 `_initialize_live_anchor()` 实盘锚点分支。基线 §5.2 要求"不存在 live 下单路径"——把这段代码搬进来，等于把 live 能力也搬进来，之后要靠"约定"而不是"代码不存在"来保证安全。**（hard 冲突）**
2. **依赖面过大**。`BinanceFuturesBroker.__init__` 直接构造 `OrderOutbox`、`TradeReconciliationBook`、`ProtectionOrderMirror`，并调用 `_load_exchange_info()`；`execute()` 里还有 `decision_pipeline` 的授权校验分支（`binance_futures.py:1148-1156`）。要跑起来必须同时提供 `AuditDatabase`（30+ 表、release evidence、migrations）与 pydantic 决策模型，启动面 ≈ 600KB 代码 + 290 行配置。
3. **它的复杂度来自"实盘接管"**，不是来自"Demo 交易"：Hedge/One-way 兼容（`assess_takeover_compatibility`）、来源台账、退出预留与 TTL 回收（900s）、成交生命周期归因、`userTrades` 7 天窗口补记、收入幂等。v1 只有 1 个账户、1 个 agent 通道、同时最多 1 个仓位（与 AI 链产品约束一致，见 §15），这些机制 90% 用不上，却全部要在评审与测试里承担。
4. **升级耦合**。原项目仍在演进（`docs/dispatch/*` 有大量在途补丁）。插件与它同仓耦合会形成"互相拖拽"：一次原项目重构就可能破坏工作台启动。
5. **它当前的部分行为对 v1 是错的**：`PaperBroker` 假设双账本；`risk.py` 依赖 `ActionSpace`/`channels`/`AgentRecorder`；`max_position_notional: 100000`、`strong_max_leverage: 50`（`config/risk_learning.yaml:6,16`）对 Demo 5000U 账户毫无约束意义。

### 2.3 结论表：复用 / 移植 / 新写

| 能力 | 原实现 | 结论 | 新实现体量 | 理由与做法 |
|---|---|---|---|---|
| 签名请求 + 时钟校准 + `recvWindow` | `_signed`/`_request`/`_calibrate_time`（`binance_futures.py:299-433`） | **移植（重写）** | ~180 行 | 逻辑简单但坑多：仅 GET 重试、`-1021/-1022` 重校准、异常时原子换连接池、`code="200"` 字符串比较。逐条照抄语义，不复用类 |
| 合约过滤器与精度 | `_format_qty/_format_price/_min_qty/_tick_size`（`:534-601`） | **移植** | ~70 行 | `stepSize` 向下截断、`tickSize` 按方向取整（§5.2 步 4）、`MARKET_LOT_SIZE.maxQty` 分片 |
| `clientOrderId` 幂等 + Outbox | `order_outbox.py`（559 行） | **新写（简化）** | ~220 行 | 保留：POST 前落库、重复 ID 拒绝、UNKNOWN 只查询不重发、指数退避、人工收口只允许无成交终态。去掉：三分量守恒、保护命令种类、私有流接线、`environment` 多租户 |
| 保护单（SL/TP） | `_place_algo_protection` + `_resync_protection_impl` + `protection_commands.py` | **新写（简化）** | ~200 行 | 保留：Algo 端点、`workingType=MARK_PRICE`、`priceProtect=true`、`closePosition=true`、**先挂新后撤旧**、失败立即平仓。去掉：保护计划器/mirror/孤儿单批量清理（改为单 symbol 覆盖校验） |
| 账户/持仓读取与缓存 | `account/positions/state`（`:937-1141`） | **移植思路** | ~120 行 | 2s TTL 缓存、账户+持仓并行拉取、失败保留最近有效值并标记 stale（**绝不当成 0**） |
| 来源台账 `futures_source_ledger.json` | `_load_ledger` 等 | **丢弃** | 0 | 单通道无归因需求；归因用 `orders.cycle_id` 即可 |
| 日亏/回撤闸门 | `risk_guard.py`（204 行，零依赖） | **同构移植** | ~80 行 | UTC 日切、`anchor_id` 分段、latch 后不自动解除、状态文件损坏 → `recovery_required` fail-closed、原子写 |
| 单笔风控规则 | `agents/risk.py` | **重写** | ~250 行 | 规则清单（杠杆/名义/保证金/RR/方向/降仓）保留；去掉 ActionSpace/通道/Recorder 耦合；新增数据新鲜度、异常行情、爆仓距离 |
| 审计库 | `audit/database.py` | **新写精简 schema** | ~10 表 | 见 §10；不迁移旧库、不引入 release evidence |
| `PaperBroker` | `agents/execution.py` | **丢弃（保留为测试替身参考）** | 0 | v1 不做纸面模式；离线单测可用 `FakeBroker` 实现同一接口 |
| 收入/成交/生命周期对账 | `income_reconciliation.py`、`trade_reconciliation.py` | **丢弃** | 0 | v1 用 `userTrades` 的 `realizedPnl/commission` 算订单级 PnL，缺失标 `cost_status=INCOMPLETE` |
| WebSocket 私有流、Windows 服务、实例锁 | `user_stream.py`、`windows_service.py`、`instance_lock.py` | **丢弃** | 0 | 30s REST 对账 + 单进程文件锁足够 |

**改造量估算**：新写 ≈ 1,300–1,600 行 Python（含测试替身约 +600 行），移植语义 ≈ 250 行；**丢弃 ≈ 600KB**。相比"整包复用"，v1 启动面从"30+ 表 + 290 行配置 + 6 个强耦合模块"降到"10 表 + ~90 行配置 + 3 个模块"。

### 2.4 新写必须继承的 7 条不变量（评审时的硬检查项）

| # | 不变量 | 出处 | 违反后果 |
|---|---|---|---|
| I1 | **POST 前必须落库**（`SUBMITTING`），落库失败禁止发送 | `order_outbox.py:118-175` | 网络抖动 → 重复下单 |
| I2 | **结果不确定 = UNKNOWN，只查询不重发** | `order_outbox.py:437-503`、`:201-212` | 一次超时可能变两笔仓位 |
| I3 | **开仓必须带初始 SL/TP，挂保护失败立即市价平仓** | `binance_futures.py:1569-1590`、`execution.py:412-415`、`settings.yaml:163` | 裸仓 |
| I4 | **成交与持仓以交易所为准**，本地账本只是缓存/归因 | `binance_futures.py:2661-2808` | UI 与真实仓位分叉 |
| I5 | **交易所查询失败 ≠ 没有保护单**（`None` 与 `[]` 必须区分） | `binance_futures.py:1641-1658` | 误判无保护 → 重复挂单/双套 SL |
| I6 | **先挂新后撤旧**，任一 PLACE 失败则停止，不撤旧保护 | `binance_futures.py:1875-1885` | 撤旧成功但挂新失败 → 裸仓 |
| I7 | **权益不可用 → 禁止开新仓**，且不写 baseline | `risk_guard.py:113-126`、`capital.py:166-171` | 错误基线固化，会话权益永久偏移 |

### 2.5 Demo 环境坑位清单（实现 checklist，M1 逐条验证）

| ID | 坑位 | 处理 | 证据 |
|---|---|---|---|
| D1 | Demo 上 `STOP_MARKET`/`TAKE_PROFIT_MARKET` **不能**走 `/fapi/v1/order`（`-4120`），必须 `POST /fapi/v1/algoOrder`（`algoType=CONDITIONAL`） | 保护单只走 Algo 通道；普通单只走 `/fapi/v1/order` | `binance_futures.py:1600-1610`（原项目实测注释） |
| D2 | Algo 接口成功返回的 `code` 是**字符串** `"200"`，与 int 比较恒不等 → 成功被当失败 | 统一 `int(code) != 200` | `binance_futures.py:415-419`；`docs/archive/风险事件排查与改善计划_2026-09-03.md:100` |
| D3 | 本机时钟漂移 >5s 会撞 `-1021`/`-1022` | 启动 `/fapi/v1/time` 校准 + 每次签名加偏移；GET 失败重校准后重试一次 | `binance_futures.py:289-293,425-432` |
| D4 | `reduceOnly` 在 Hedge Mode 被禁止 | 提交前读 `positionSide/dual`；Hedge 用 `positionSide`，One-way 用 `reduceOnly` | `binance_futures.py:1029-1040` |
| D5 | 数量舍大导致 `-2022`（reduceOnly 数量 > 仓位） | 数量**向下**截断到 `stepSize` | `binance_futures.py:544` |
| D6 | 一次 MARKET 有 `MARKET_LOT_SIZE.maxQty` 上限 | 超出则分片提交（每片独立 `clientOrderId`） | `binance_futures.py:1419-1428,1992-2001` |
| D7 | `userTrades` 单窗口跨度上限 7 天（`-4165`） | 对账窗口切片（v1 只用 ±1 天，冲突概率极低） | `binance_futures.py:2859-2861` |
| D8 | 撤单 `-2011 Unknown order` 属正常（已触发/已撤） | 视为 CANCELED 收口，不计错误 | `binance_futures.py:1677-1688` |
| D9 | Demo 独立的 IP 权重池，与原项目公共行情不共享 | 自建本地预算（§6.2），读 `X-MBX-USED-WEIGHT-1M` 自适应 | `binance_futures.py:319-355`（只对 `fapi.binance.com` 生效，反面证据） |
| D10 | 保护单字段名在历史版本出现过 `triggerprice` 大小写差异 | M1 探针验证后用固定常量；请求体大小写**写死在常量表**，不做兼容猜测 | `docs/CHANGES_2026-09-01.md:13` vs `binance_futures.py:1613-1623` |

---

## 3. Q3（上）· 账户、密钥与环境隔离

### 3.1 账户选择

| 方案 | 优点 | 缺点/风险 | 结论 |
|---|---|---|---|
| A. 复用现有 demo 账户（原项目那套 key） | 零准备成本 | 旧仓位/旧挂单/旧杠杆设置会污染接管；旧数据库中的历史会诱导"混算"；原项目一旦启动会双写同一账户 | ❌ 不推荐 |
| B. **新建独立 Demo 账户（新 key）** | 干净起点；`account_ref` 独立；与原项目零共享 | 需在币安 Demo 站点手工建号（一次性 5 分钟） | ✅ **推荐** |
| C. 复用账户但加"接管前置条件" | 折中 | 需要人工清场且每次重启都要复核 | ⚠️ 备选（用户明确无法新建时） |

**推荐 B**。若用户选 C，启动对账（§6.1）必须满足全部前置条件，否则 **拒绝启动**（不是"警告后继续"）：

- 账户内**无持仓**（`positionRisk` 全为 0）；
- 账户内**无普通挂单**（`openOrders` 为空）；
- 账户内**无 Algo 保护单**（`openAlgoOrders` 为空）；
- 本地库为空或 `account_ref` 与当前 key 一致；
- 以上任一条件不满足 → `E_TAKEOVER_BLOCKED`，UI 显示"存在未纳管仓位/挂单，需人工清场"。

### 3.2 账户配置基线（一次性设置，代码不代改）

| 项 | 取值 | 说明 |
|---|---|---|
| 持仓模式 | **One-way**（推荐） | Hedge 会让 `reduceOnly` 失效、平仓语义变复杂；若账户已锁 Hedge，代码走 `positionSide` 分支（§5.6） |
| 保证金模式 | **ISOLATED**（逐仓） | 每次开仓 `POST /fapi/v1/marginType`，`-4046 No need to change` 视为成功 |
| 杠杆 | 按单设置，上限受 `risk.per_trade.max_leverage` | 设置失败 → 拒绝开仓（不猜） |
| 多资产模式 | 关闭 | 多资产保证金会让权益口径不可解释 |
| IP 白名单 | 可选 | Demo 支持；本机动态 IP 时不启用 |

### 3.3 密钥与鉴权

| 项 | 设计 |
|---|---|
| 存放 | `trading-workbench/.env.local`（`.gitignore` 必须包含 `.env.local`），或用户级凭据管理器；**只放在服务进程**，不进插件配置、不进前端 bundle、不进数据库 |
| 变量名 | `BINANCE_DEMO_API_KEY` / `BINANCE_DEMO_API_SECRET`（沿用原项目命名，便于用户理解；**代码里不存在任何 `*_LIVE_*` 变量名**） |
| 读取时机 | 服务启动一次读入内存；不做热更新 |
| 日志 | 全部日志过 `redact()`：key 只留 `sha256[:8]` 指纹；`signature` 永不打印；异常消息里剥离 `X-MBX-APIKEY` 头 |
| 传输 | 只在本机 `127.0.0.1` 进程间；插件 Host → 服务用一次性 bearer token（`WORKBENCH_API_TOKEN`，启动时写入用户态文件 `0600`） |
| 展示 | `/health` 只返回 `key_source: "env"` 与 `key_fingerprint: "ab12cd34"` |

### 3.4 Demo-only guard（三层，任一层失败即拒绝）

| 层 | 位置 | 规则 | 失败行为 |
|---|---|---|---|
| L1 配置 | 配置加载（pydantic `Literal["demo"]`） | `environment` 字段只能是 `"demo"`；`base_url` 必须逐字等于白名单常量 `https://demo-fapi.binance.com`；出现 `live`/`testnet` 键值 → schema 校验失败 | 进程**起不来**，退出码非 0，日志 `E_DEMO_GUARD_CONFIG` |
| L2 构造 | `DemoBroker.__init__` | 断言 `urlparse(base_url).hostname == "demo-fapi.binance.com"` 且 scheme 为 `https`；端口为空或 443 | 抛 `DemoGuardViolation`，不创建 HTTP 客户端 |
| L3 请求 | `_request()` 唯一出口 | 每次请求前断言 host ∈ `ALLOWED_HOSTS = {"demo-fapi.binance.com"}`；重定向 `follow_redirects=False` | 抛 `DemoGuardViolation`，**不发包**，记 critical |

### 3.5 "无 live 路径"的证明方式（可写进评审结论）

**静态**（CI 可执行）：

```powershell
# 1) 全仓不得出现 live 域名常量
#    注意：demo-fapi.binance.com 恰好包含子串 "fapi.binance.com"，
#    因此必须用"排除 demo 前缀"的写法，不能直接 grep 裸域名。
grep -rn "fapi\.binance\.com" trading-workbench/ | grep -v "demo-fapi\.binance\.com"   # 期望 0 命中
# 2) 不得出现 live 凭据变量名
grep -rniE "BINANCE_LIVE|EXECUTION_MODE=live|environment.*[\"']live[\"']" trading-workbench/   # 期望 0 命中
# 3) 唯一 host 白名单
grep -rn "ALLOWED_HOSTS" trading-workbench/        # 期望唯一常量定义 + 引用
```

**运行时**（单测）：

1. 构造 `DemoBroker(base_url="https://fapi.binance.com")` → 必须抛 `DemoGuardViolation`，且 `httpx` mock 的 `request` 调用次数为 0。
2. 把配置里 `environment` 改为 `live` → 服务启动失败（不进入 ready）。
3. 单测全局 patch `DemoBroker._request` 记录 host，断言测试期间 host 集合 ⊆ 白名单。

> 与基线 §5.2 的对应：任何非 `demo-fapi.binance.com` 的 base URL 都必须**拒绝启动/拒绝下单**；配置中不得出现 live 密钥路径。本文档把"拒绝"从"运行时判断"提升为"代码里不存在该常量"。

---

## 4. Q3（下）· 账本与权益口径

### 4.1 双口径权益（必须同时存在，且 UI 标注清楚）

| 口径 | 公式 | 用途 | 数据来源 |
|---|---|---|---|
| `equity_exchange` | `totalMarginBalance`（= 钱包余额 + 未实现盈亏） | 账户真实状态；风控分母的**最后手段** | `GET /fapi/v2/account` |
| `equity_session` | `equity_exchange − baseline` | **UI 主曲线**、日亏/回撤/单笔风险的分母 | 计算值 |
| `wallet_balance` | `totalWalletBalance` | "已实现收益" = `wallet_balance − 锚点钱包余额` | `GET /fapi/v2/account` |
| `upnl` | `totalUnrealizedProfit` | 展示；**不参与**日亏判定（日亏按 `equity_session` 计，见 §7.3 R18 / §7.6） | `GET /fapi/v2/account` |

**为什么需要 baseline**：Demo 账户自带虚拟资金（原项目账户约 5000U）。若不减 baseline，"充值/初始余额"会被当成交易收益，`session_equity` 永久偏移。原项目正是踩过这个坑：`capital.py:1-16` 记录了一起 `baseline=0` 被跨模式继承、导致会话权益等于交易所全部余额的事故。

### 4.2 锚点（baseline）规则

```text
锚点创建（首次启动 or 用户手动重置）：
  baseline = equity_exchange(锚点时刻) − initial_equity(配置, 默认 5000)
  若 baseline < 0 → baseline = 0（Demo 余额低于起点时按 0 处理，并记 warn）
  记录：anchor_id = "demo:<anchored_at ISO>"，mode="demo"，initial_equity，account_ref

硬约束：
  1. baseline 只在 mode=="demo" 且 account_ref 未变时可复用；
  2. equity 不可用时**不写库**（内存临时值），恢复后重算（原项目教训，capital.py:166-171）；
  3. 状态文件损坏 → 不自动修复，进入 close_only + 要求人工 `POST /api/capital/reanchor`
     （带 `confirm: "REANCHOR"` 与输入的两处权益数字做二次校验）。
```

### 4.3 旧 demo 历史隔离（对应基线 §2#17）

| 隔离手段 | 具体做法 |
|---|---|
| 数据库 | 新文件 `data/workbench-v1.sqlite`；**不 attach、不 import** `multi-agent-trader/data/agent_trader.sqlite` |
| 账户 | 独立 Demo 账户（§3.1 方案 B），从根上不共享持仓/挂单历史 |
| 账本 | 旧库仅作"只读参考"：如需对比，用 `sqlite3 file:...?mode=ro` 手工查询，**不进运行时** |
| 曲线 | UI 主曲线只读 `equity_snapshots`（新库）；空态显示"新账本尚无数据"，**绝不 fallback 到旧曲线** |
| 命名 | 所有表名不带 v0 前缀；`meta` 表记录 `schema_version=1, ledger_id=<uuid>, created_at`，UI 展示 `ledger_id` 便于人工确认"我看的是新账本" |

### 4.4 权益快照与曲线口径

- **写库频率**：30s（快循环 L2）。字段见 §10 `equity_snapshots`。
- **采样策略**：30s 行保留 14 天；之后按天聚合 `min/max/first/last` 到 `equity_daily`（可选，M2）。
- **UI 展示**：1m 桶取 `last`；曲线用 `equity_session`；同时展示 `equity_exchange` 作为副轴/悬浮详情。
- **去重**：同一 `bucket_ts` 30s 只写一行（`INSERT OR REPLACE` on `(ledger_id, ts_bucket)`），避免重启补齐时重复点。
- **平仓后立即补一拍**（不等下一个 30s），保证成交后曲线不跳空。

### 4.5 账本对齐规则（冲突仲裁）

| 冲突 | 仲裁 |
|---|---|
| 本地 `positions` 与 `positionRisk` 不一致 | **交易所为准**；本地整行覆盖，写 `position_events(kind=EXTERNAL_SYNC)` |
| 交易所有仓位、本地无记录 | **收养**（adopt）：标 `origin=EXTERNAL`，缺失 SL/TP 时按 `mark ±2%/+4%` 兜底挂保护 + critical 事件（原项目同策略 `binance_futures.py:282-283,1820-1837`） |
| 本地有仓位、交易所没有 | 视为已在系统外平仓：标记 `CLOSED_EXTERNAL`，从 `userTrades` 尝试补记 PnL（查不到就留空，**不估算**） |
| 交易所有多套 SL/TP | 只保留"最新一对"并取消其余；取消失败只告警（`I6`：绝不先撤后挂） |
| 挂单/保护单查询失败（`None`） | 当轮**跳过**该 symbol 的保护对账（`I5`），不重挂、不平仓；超过 120s 仍失败 → emergency |

---

## 5. Q10（主干）· 订单生命周期

### 5.1 状态机

命令分两类：**STANDARD**（`/fapi/v1/order`）与 **PROTECTION**（`/fapi/v1/algoOrder`）。两者共用同一状态词表与同一 Outbox。

| 状态 | 含义 | 允许的下一状态 | 是否终态 |
|---|---|---|---|
| `SUBMITTING` | 已落 Outbox，POST 在途 | `ACKNOWLEDGED`, `NEW`, `UNKNOWN`, `REJECTED` | 否 |
| `UNKNOWN` | POST 结果不确定 | `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `REJECTED` | 否 |
| `ACKNOWLEDGED` | 交易所已受理但未给出明确状态 | `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED`, `REJECTED` | 否 |
| `NEW` | 挂单中（普通单） / 等待触发（保护单） | `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED` | 否 |
| `PARTIALLY_FILLED` | 部分成交 | `PARTIALLY_FILLED`, `FILLED`, `CANCELED`, `EXPIRED` | 否 |
| `FILLED` | 全部成交 | — | 是 |
| `CANCELED` | 已撤 | — | 是 |
| `EXPIRED` | 过期（含 `EXPIRED_IN_MATCH` 归一） | — | 是 |
| `REJECTED` | 交易所明确拒绝 | — | 是 |

**转移规则（不变式）**：

1. **单调性**：`SUBMITTING(0) < UNKNOWN(5) < ACKNOWLEDGED(10) < NEW(20) < PARTIALLY_FILLED(30) < 终态(40)`；不允许回退（沿用 `order_outbox.py:31-36` 的 rank 语义）。
2. **终态不可变**：终态之后到达的迟到成交只修订单量字段，**不改状态**（原项目 `_component_status` 的"终态保留原状态"，`order_outbox.py:45-81`）。
3. **数量守恒**：`accepted_qty = cumulative_filled + working + terminal_unfilled`，任何时刻成立；违反 → 拒绝写入 + critical（不猜测修复）。
4. **状态只由两处写**：`POST` 响应（同步）与 `GET` 查询结果（异步对账）。**不引入 WS**，所以不存在第三来源。

### 5.2 开仓时序（市价 + 初始 SL/TP）

> 前置：P1 计划闸已 `approved`（§7），`cycle_id` 已分配。

| # | 步骤 | 动作 | 失败分支 |
|---|---|---|---|
| 1 | 复检 | P2 提交闸：读实时 `positionRisk/account`，**重跑全部适用规则**（实时事实会变的部分：R02/R03/R05–R07/R14/R15/R17/R22/R23） | 拒绝 → `E_RISK_REJECT`（带 rule_id），**不发单** |
| 2 | 模式/杠杆 | `POST /fapi/v1/marginType`（`-4046` 视为 OK）→ `POST /fapi/v1/leverage` | 杠杆设置失败 → 拒绝开仓（`E_LEVERAGE_SET`）；不猜交易所上限 |
| 3 | 数量计算 | `qty = risk_amount / abs(entry − SL)` → 按 §7.4 降仓 → `floor_to_step(stepSize)` | `qty == 0` 或 `< minQty` 或 `notional < minNotional` → `E_RISK_BELOW_MIN` |
| 4 | 价格取整 | SL/TP 按 tick 取整，**方向安全**：LONG 的 SL 向下、TP 向上；SHORT 的 SL 向上、TP 向下 | 取整后越界（SL ≥ entry 或 TP ≤ entry）→ 拒绝 |
| 5 | 落 Outbox | `outbox.prepare(cid, ENTRY, ...)`，`status=SUBMITTING` | 写库失败 → **禁止发单**（`I1`），`E_LEDGER_WRITE` |
| 6 | 发单 | `POST /fapi/v1/order`（MARKET，`newClientOrderId=cid`） | 明确拒绝（4xx/`Binance -xxxx`）→ outbox `REJECTED`；超时/网络异常 → outbox `UNKNOWN` → 走 §9 F04 |
| 7 | 等成交 | 轮询 `GET /fapi/v1/order?origClientOrderId=` 最多 8 次 × 600ms（约 5s） | 未终态 → `fill_unconfirmed=true`，继续（后续对账收口） |
| 8 | 读真实仓位 | `GET /fapi/v2/positionRisk`（force），取 `abs(positionAmt)` | 仓位为 0（完全未成交）→ 取消保护计划，收口订单，`E_ZERO_FILL`（warn，不算故障） |
| 9 | 挂 SL | `POST /fapi/v1/algoOrder`：`STOP_MARKET` + `closePosition=true` + `workingType=MARK_PRICE` + `priceProtect=true` + `clientAlgoId=sl_cid` | 失败 → 重试 1 次；仍失败 → **步 11 紧急平仓** |
| 10 | 挂 TP | 同上，`TAKE_PROFIT_MARKET` | 失败 → 重试 1 次；仍失败 → **步 11 紧急平仓**（`require_initial_sl_tp=true`，不允许"只挂 SL"降级） |
| 11 | 紧急平仓（兜底） | `reduceOnly` MARKET 平掉步 8 的真实仓位 → 撤残留保护 → 复核 flat | 平仓也失败 → `control=emergency` + `E_PROTECTION_FAILED` + 需人工（§9 F06） |
| 12 | 校验覆盖 | `GET /fapi/v1/openAlgoOrders` → 该 symbol 恰好 1 张 STOP + 1 张 TP | 缺失/重复 → 走 §5.6 修复流程；仍失败 → 步 11 |
| 13 | 记账 | `orders` 落库、`fills` 落库、`positions` 覆盖为真实仓位、`position_events(OPEN)`、权益快照补一拍 | 记账失败：不回滚交易所动作，写 `risk_events(severity=critical)` 并对账修复 |

> **关键顺序理由**：先成交再挂保护，是因为保护单数量必须基于**真实持仓量**（部分成交时请求量 ≠ 成交量）。代价是存在 ~1–3s 的"裸仓窗口"；用 `closePosition=true` + 步 8 的强制取真值把窗口风险降到最小。若 P2 复检时已有同向仓位则直接拒绝（R02），不会叠加。

### 5.3 平仓 / 减仓

| # | 步骤 | 说明 |
|---|---|---|
| 1 | 读真实仓位 | `force=True`；无仓位 → `E_NO_POSITION`（幂等成功，不报错） |
| 2 | 计算平仓量 | `min(请求量, 真实量)`；请求量 ≤0 → 使用全量（`closePosition` 语义） |
| 3 | 落 Outbox | `purpose=EXIT`，`reduce_only=true` |
| 4 | 发单 | MARKET + `reduceOnly=true`（One-way）/ `positionSide`（Hedge）；按 `MARKET_LOT_SIZE` 分片 |
| 5 | 等成交 | 同上；未确认 → `fill_unconfirmed` |
| 6 | 复核仓位 | `force=True`；仍有余量 → **最多再发 1 次**剩余量（避免对已平仓位重复减仓） |
| 7 | 清理保护 | 仓位归零 → 取消该 symbol 全部 Algo 单（失败只告警，下一轮重试） |
| 8 | 记账 | 订单级 PnL = `Σ userTrades.realizedPnl`；`fee = Σ commission`；缺失 → `cost_status=INCOMPLETE`，PnL 留空**不估算** |

### 5.4 部分成交

| 场景 | 处理 |
|---|---|
| MARKET 部分成交（流动性不足） | 以 `positionRisk.positionAmt` 为保护基数；`orders.qty_filled < qty_requested` 记录；保护仍用 `closePosition=true`（自动覆盖实际量，不随减仓漂移） |
| MARKET 完全未成交 | 保护计划取消；订单收口；`risk_events(warn, E_ZERO_FILL)`；不计入当日交易次数 |
| 保护单触发时部分成交 | `closePosition=true` 由交易所保证"平掉触发时刻该仓位全部数量"；触发后仍有余量（极端行情） → §9 F12 |
| 平仓部分成交 | 重试剩余量 1 次；仍未平完 → 保留仓位 + 下一轮继续，`fill_unconfirmed=true`，**不新开仓**（R17） |

### 5.5 撤单 / 改单

v1 **不提供价格 amend**（Demo Algo 接口无 amend；普通单 amend 会引入新状态）。所有"改"都是 **cancel + replace**，且遵守 `I6`（先挂新后撤旧）。

| 场景 | 触发 | 动作 | 失败处理 |
|---|---|---|---|
| 保护单重建（移动止损/改 TP） | 操盘手 `adjust`，或 30s 对账发现保护价与期望不符 | 挂新 SL/TP → 校验 → 撤旧 | 挂新失败：**停止**，保留旧保护，`E_PROTECTION_FAILED`；撤旧失败：只告警（旧保护仍在，不构成裸仓） |
| 保护单孤儿清理 | 仓位已归零但仍有 Algo 单 | 二次确认仓位为 0 → 撤单 | `-2011` 视为已撤；其它失败下轮重试 |
| 撤销未成交普通单 | 仅用于"接管遗留单"或"人工撤销"（v1 不挂限价入场单） | `DELETE /fapi/v1/order?origClientOrderId=` | `-2011` 视为已撤；其它失败保留并重试 |
| 全局撤单（emergency） | kill switch | 撤该账户全部普通单 + Algo 单 → 平全部仓位 | 见 §9 F13 |

> 撤单前置校验：只允许撤销 `status ∈ {NEW, PARTIALLY_FILLED, UNKNOWN}` 的订单；`UNKNOWN` 撤单前先查询确认存在（否则 `-2011`）。

### 5.6 保护单失败与兜底（重点章节）

**判定"保护失败"** 只有两种情形（其余都算"未知"，不得当作失败）：

| 情形 | 判据 | 动作 |
|---|---|---|
| 确证失败 | 挂单返回明确 `Binance -xxxx` 拒绝，且重试 1 次仍失败 | 紧急平仓（§5.2 步 11） |
| 确证缺失 | `openAlgoOrders` 查询**成功**，但该 symbol 的 STOP 或 TP 数量 ≠ 1 | 修复：先挂缺失的一侧 → 再校验 → 仍缺 → 紧急平仓 |
| 观测失败（不是失败） | `openAlgoOrders` 抛异常/超时（返回 `None`） | **不平仓**、不重挂；标记 `protection_status=UNKNOWN`；120s 未恢复 → emergency（原项目同策略：`binance_futures.py:1780-1794`） |

**兜底保护（收养仓位）**：`origin=EXTERNAL` 或本地无 SL/TP 记录但交易所有仓位 → 按 `mark×(1∓2%)` 挂 SL、`mark×(1±4%)` 挂 TP（比例沿用原项目 `_FALLBACK_SL_PCT/_FALLBACK_TP_PCT`），同时 critical 事件 + UI 红标"兜底保护"。**不允许裸仓存活超过 1 个快循环（10s）。**

**Hedge 模式分支**（若账户锁定为 Hedge）：保护单必须带 `positionSide=LONG|SHORT` + `closePosition=true`，且**不能**同时发 `reduceOnly`（`D4`）；平仓单同理。此时 `R02` 升级为"同 symbol 同方向最多 1 仓，且禁止反手"。

### 5.7 `clientOrderId` 规则与幂等矩阵

**格式**（Binance 限制：≤36 字符，字符集 `[\.A-Z\:/a-z0-9_-]`）：

```text
普通单：wb-{cycle8}-{sym8}-{act2}-{n}
  cycle8 = cycle 序号（UTC 分钟级 8 位，如 1109T0140）
  sym8   = symbol 去掉 USDT 后截断 8 位（BTC、1000PEPE…）
  act2   = O(open) / C(close) / R(reduce)
  n      = 本轮内该 symbol 动作序号（0 起）
  例：wb-1109T0140-BTC-O-0  （20 字符）

保护单：wbp-{sha1(order_cid + ":" + sl|tp)[:16]}
  例：wbp-3f9c1a77b2e04d18
```

**幂等矩阵**：

| 重复来源 | 幂等机制 | 结果 |
|---|---|---|
| 同一轮重复执行（Host 重试） | `cid` 由 `cycle_id + symbol + action` 确定性派生 → `orders.client_order_id` UNIQUE → 第二次插入失败 | `duplicate_skipped`，返回首次结果 |
| 同轮重复开仓意图（AI 输出重复） | `outbox` 业务键唯一索引：`(env, business_key) WHERE purpose='ENTRY' AND status IN ('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED')` | `E_DUPLICATE_ORDER`（409） |
| 定时器双触发 | `cycle_runs.slot_id` UNIQUE（10 分钟对齐的 slot） | 第二次返回 `E_CYCLE_DUPLICATE_SLOT`，不执行 |
| POST 超时后重试 | outbox `UNKNOWN` + `origClientOrderId` 查询；**服务端也**以 `newClientOrderId` 唯一拒绝 | 最多一笔真实订单 |
| 保护单重复挂 | `clientAlgoId` 确定性派生 + 挂前查询覆盖 | 不会出现双套 SL |

---

## 6. 对账与状态跟踪

### 6.1 启动对账（12 步，全部通过才 `ready`）

| # | 步骤 | 失败行为 |
|---|---|---|
| 1 | 加载配置，L1 demo guard | 退出（非 0） |
| 2 | 读 key，构造 broker，L2 guard | 退出 |
| 3 | `GET /fapi/v1/time` 校准（失败只 warn，偏移=0） | 继续 |
| 4 | `GET /fapi/v1/exchangeInfo` 过滤器缓存（symbol/stepSize/tickSize/minQty/minNotional/maxQty） | 重试 3 次 → 失败则 `ready=false`（`SYMBOL_FILTERS_UNAVAILABLE`） |
| 5 | `GET /fapi/v1/positionSide/dual` 判定持仓模式 | 失败/unproven → `ready=false`（`POSITION_MODE_UNPROVEN`） |
| 6 | `GET /fapi/v2/account` + `/positionRisk` + `/openOrders` + `/openAlgoOrders` | 任一失败 → `ready=false`（`EXCHANGE_UNREACHABLE`），进入只读重试 |
| 7 | Outbox：`SUBMITTING → UNKNOWN`（进程重启语义），再 `reconcile_unknown()` 一轮 | 有未决 → `ready=false`（`ORDER_OUTBOX_UNKNOWN`），但服务可用（只允许平仓） |
| 8 | 收养：交易所有仓位但本地无记录 → 建记录 + 兜底保护 | 保护失败 → `control=emergency` |
| 9 | 孤儿：本地有仓位但交易所无 → 收口 + 尝试从 `userTrades` 补 PnL | 补记失败只 warn |
| 10 | baseline：无锚点则建锚点（§4.2） | 权益不可用 → 不建锚点，`ready=false`（`EQUITY_UNAVAILABLE`） |
| 11 | 风控状态：读 `risk_state`；损坏 → `recovery_required` | `ready=false`（`RISK_STATE_CORRUPT`），只允许平仓 |
| 12 | 控制态：读 `control_state`；损坏 → `close_only` | `ready=false`（`CONTROL_NOT_NORMAL`） |

> 原则：**"不知道"一律不等于"安全"**。任何一步"无法证明"都进 `ready=false` 并给出稳定原因码（§8.3），UI 直接展示。

### 6.2 周期任务与刷新率（含权重预算）

| 循环 | 周期 | 内容 | 请求数与权重（估算） | 失败行为 |
|---|---|---|---|---|
| L0 行情 | 1s | 标记价/最优买卖（行情服务负责，不在本文档） | — | 行情侧标记 stale |
| **L1 执行快照** | **10s** | `account` + `positionRisk` + `openOrders` + `openAlgoOrders`（并行） | ~4 请求 / ~12 weight | 保留最近有效值 + `stale=true`；连续 6 次失败（60s）→ `ready=false` |
| **L2 权益/仓位落库** | **30s** | 复用 L1 缓存写 `equity_snapshots` / 覆盖 `positions` / 写变化的 `position_events` | 0 额外请求 | 写库失败 → critical；下一拍补 |
| L3 保护核对 | 30s | 复用 L1 的 `openAlgoOrders` 做覆盖校验（§5.6） | 0 额外 | 见 §5.6 |
| L4 未决订单 | 30s | `reconcile_unknown` 最多 3 笔/轮，指数退避 30s→60s→…→1800s | ≤3 请求 | 单笔失败只影响自身 |
| L5 决策周期 | 600s（+手动） | AI 链 → P1 → 下单 | — | 见 §9 |
| L6 日切 | UTC 00:05 | 重置 `day_start_equity`、生成复盘、清 `consecutive_losses` | 0 | 失败下一轮重试 |

**本地权重预算**：Demo 上限按 2400 weight/min 估计，本地软预算 **600/min（25%）**，超预算时 L1 降频到 20s、L3 合并到 L1。响应头 `X-MBX-USED-WEIGHT-1M` 写入 `risk_events` 便于事后核算（原项目对主网做过同样的 25% 预留，`settings.yaml:203-212`）。

### 6.3 仓位跟踪（30s）

`positions` 表**每个 `(ledger_id, symbol, position_side)` 只保留一行最新快照**；历史变化写 `position_events`（仅在字段变化时写，避免每 30s 一条噪声）。

| 字段 | 来源 | 用途 |
|---|---|---|
| `qty` / `entry_price` / `mark_price` / `upnl` | `positionRisk` | UI 仓位卡、风险计算 |
| `leverage` / `margin` / `margin_mode` | `positionRisk`（`isolatedMargin` 优先） | 保证金占用（R07） |
| `liq_price` | `positionRisk.liquidationPrice` | 爆仓距离（R11） |
| `sl_price` / `tp_price` | `openAlgoOrders`（`triggerPrice`） | UI 保护展示、覆盖校验 |
| `protection_status` | 计算：`OK / MISSING / PARTIAL / DUPLICATE / UNKNOWN` | ready 原因、UI 红标 |
| `origin` | `OPENED_BY_WORKBENCH / EXTERNAL` | 收养逻辑、复盘归因 |
| `opened_at` / `updated_at` | 本地 | 持仓时长、事件排序 |

**给操盘手 agent 的摘要**（每轮注入，≤2KB，t2 可调整字段但需保持这些语义）：

```json
{
  "equity_session": 4987.2, "equity_exchange": 5000.0, "available": 4210.5,
  "margin_used": 789.5, "margin_usage_ratio": 0.158, "upnl": -12.8,
  "daily_pnl": -12.8, "daily_loss_ratio": 0.0026, "drawdown_ratio": 0.0031,
  "open_positions": 1, "max_open_positions": 1,
  "positions": [{
    "symbol": "BTCUSDT", "side": "LONG", "qty": "0.012", "entry": "100250.5",
    "mark": "100180.0", "upnl": "-0.85", "leverage": 8, "margin": "150.4",
    "sl": "99000.0", "tp": "102800.0", "protection": "OK", "origin": "OPENED_BY_WORKBENCH",
    "holding_minutes": 42
  }],
  "control_state": "normal", "new_entry_allowed": true, "blocking_reasons": []
}
```

> 数量/金额一律**十进制字符串**（避免 float 尾差写库，原项目 U04/DC04 的教训：`str(0.1+0.2)` 写进"精确"列）。

### 6.4 与交易所不一致的处置优先级

1. **仓位**：交易所 > 本地（覆盖）。
2. **订单**：以 `GET /fapi/v1/order` 与 `openOrders` 为准；本地 `NEW` 但交易所无 → 查 `userTrades`；仍无 → 标 `EXPIRED`（不标 FILLED）。
3. **保护单**：以 `openAlgoOrders` 为准；查询失败不推断（`I5`）。
4. **权益**：以 `account.totalMarginBalance` 为准；不可用时**冻结**开仓，不用旧值冒充。
5. **人工收口**：`UNKNOWN` 只能收口为 `CANCELED|EXPIRED|REJECTED`（无成交类），**禁止人工标 FILLED**（沿用 `order_outbox.py:505-510`）；必须记录 `operator + evidence`。

---

## 7. Q6 · 硬风控

### 7.1 执行位置：两闸门 + 一巡检

```text
AI 决策链（Host）                         执行服务（Python, 确定性）
─────────────────                        ──────────────────────────────
分析 → 红/蓝 → 决策 → 风控agent(AI建议)
                      │
                      ▼  OrderPlan(JSON)
                 ┌──────────────────┐
                 │ P1 计划闸        │  risk_rules.evaluate_plan()
                 │ 依赖：本轮证据   │  → approve / downsize / reject(rule_id)
                 │       权益快照    │
                 └────────┬─────────┘
                          ▼  ApprovedPlan
                 ┌──────────────────┐
                 │ P2 提交闸        │  risk_rules.evaluate_submit()  ← 读交易所实时真值
                 │ 同规则 + 实时仓位 │  → 再拒一次（防 P1 与 P2 之间状态漂移）
                 └────────┬─────────┘
                          ▼
                    demo_broker.submit()
                          ▲
                 ┌────────┴─────────┐
                 │ P3 巡检闸 10s/30s │  evaluate_account(): R18/R19/R23/R24/R25
                 │ 触发 latch / kill │  → control_state 迁移
                 └──────────────────┘
```

**单一规则源**：`risk_rules.py` 一个模块、一组纯函数、一份配置。P1/P2/P3 只决定"取哪些上下文"，不复制阈值。**AI 风控结论只能收紧**：最终限额 = `min(AI 建议限额, 硬规则限额)`；AI 说"approve"不产生任何放行（R00）。

### 7.2 参数配置（`config/risk.v1.yaml`，可直接落代码）

```yaml
risk:
  version: 1

  session:
    initial_equity: 5000.0          # USDT，锚点基准（demo 账户自带资金）
    anchor_mode: exchange_snapshot  # exchange_snapshot | fixed

  per_trade:
    max_risk_pct: 0.010             # 单笔风险 = 1.0% 会话权益
    strong_max_risk_pct: 0.020      # 强信心档 2.0%
    strong_min_confidence: 0.65
    strong_min_rr: 2.0
    max_leverage: 10                # 常规档
    strong_max_leverage: 15         # 强信心档
    max_notional_multiple: 2.0      # 单笔名义 ≤ 2× 会话权益
    min_risk_reward: 1.2            # 实际 RR 下限（含强信心档）
    sl_min_distance_pct: 0.002      # 止损距离下限 0.2%
    sl_max_distance_pct: 0.050      # 止损距离上限 5.0%
    min_liquidation_distance_multiple: 2.0   # 爆仓距离 ≥ 2×SL 距离

  portfolio:
    max_open_positions: 1           # v1 与 AI 链产品约束对齐（§15）；只约束新开仓
    max_symbol_positions: 1         # 单标的最多 1 仓（且禁止反手）
    max_margin_usage: 0.60          # 总保证金 ≤ 60% 会话权益

  loss:
    daily_loss_stop: 0.030          # 日亏 3% → latch + close_only
    max_drawdown_from_peak: 0.080   # 会话回撤 8% → latch + emergency
    consecutive_losses: 2           # 连亏 2 笔
    consecutive_loss_cooldown_hours: 4

  protection:
    require_initial_sl_tp: true     # 强制 SL+TP 同时存在
    allow_sl_only: false            # 不允许降级
    fallback_sl_pct: 0.02           # 收养仓位兜底
    fallback_tp_pct: 0.04
    place_retry: 1
    verify_timeout_ms: 3000
    missing_escalate_seconds: 120   # 保护缺失持续 → emergency

  data_freshness:
    entry_snapshot_max_age_ms: 15000     # 下单用行情快照 ≤15s
    decision_evidence_max_age_ms: 400000 # 决策证据 ≤400s（M1.4c：链预算 340s 必须装得下；2026-11-10）
    equity_block_new_ms: 60000           # 权益 stale >60s → 禁开新仓
    equity_emergency_ms: 120000          # >120s → emergency

  anomaly:
    mark_move_60s_pct: 0.030        # 60s 内标记价变动 ≥3% → 禁开新仓
    mark_index_deviation_pct: 0.010 # 标记价与指数价偏离 ≥1%
    max_spread_bps: 50              # 买卖价差 ≥50bp

  orders:
    min_notional_usdt: 20.0         # 与交易所 minNotional 取 max
    max_orders_per_cycle: 2
    max_trades_per_day: 12
    max_trades_per_symbol_per_day: 3
    unknown_block_new_order: true   # 有 UNKNOWN 就禁新开
    unknown_resolve_ttl_ms: 120000

  control:
    auto_emergency_margin_ratio: 0.80   # 维持保证金/权益 ≥80%
    auto_close_only_consecutive_errors: 3

  exposure_guard:
    enabled: false                  # v1 关闭（见 §7.3 注）；单仓下无实际影响，v1.1 放开多仓再评估
    max_correlated_positions: 1     # 同向高相关（BTC/ETH 等）最多 N 仓
```

### 7.3 规则表（26 条，可直接实现）

> 记法：`equity = equity_session`；`used_margin = Σ position.margin`；`P1/P2/P3` 为执行点（§7.1）；"动作"列 `REJ`=拒绝、`DWN`=降仓、`ADJ`=确定性修正（取整/规范化，不算违规）、`SKIP`=幂等跳过、`LATCH`=置位并持久化、`KILL`=转 emergency。

| ID | 名称 | 判据（公式） | 动作 | 时点 | 事件类型 / 错误码 |
|---|---|---|---|---|---|
| **R00** | AI 只能收紧 | `final_limit = min(ai_limit, hard_limit)`；AI `approve` 不改变任何硬规则结果 | 恒成立 | P1 | `risk_ai_tightened` |
| **R01** | 品种白名单 | `symbol ∈ universe` 且 `exchangeInfo.status == TRADING` 且 `symbol.endswith("USDT")` | REJ | P1,P2 | `E_RISK_SYMBOL` |
| **R02** | 单标的单方向 | 该 symbol 无现存仓位；已有同向仓 → REJ（v1 不加仓）；已有反向仓 → REJ（不反手） | REJ | P1,P2 | `E_RISK_SYMBOL_POSITION` |
| **R03** | 最大并发持仓 | `open_positions + 1 ≤ max_open_positions(1)`（v1 等价于"有空仓才可开新仓"；只约束新开仓，不约束已有/外部仓位的保护与平仓） | REJ | P1,P2 | `E_RISK_MAX_POSITIONS` |
| **R04** | 杠杆上限 | `1 ≤ leverage ≤ min(cap(10/15), exchange_max_leverage[symbol])`；超限 **拒绝**（不静默降杠杆） | REJ | P1,P2 | `E_RISK_LEVERAGE` |
| **R05** | 单笔风险 | `qty × abs(entry − SL) ≤ equity × risk_pct(1%/2%)` | DWN | P1,P2 | `E_RISK_PER_TRADE`(downsize) |
| **R06** | 单笔名义 | `qty × entry ≤ equity × 2.0` | DWN | P1,P2 | `E_RISK_NOTIONAL` |
| **R07** | 保证金占用 | `used_margin + qty×entry/leverage ≤ equity × 0.60` | DWN→REJ | P1,P2 | `E_RISK_MARGIN` |
| **R08** | SL/TP 必填+方向 | `SL>0 且 TP>0`；LONG: `SL<entry<TP`；SHORT: `TP<entry<SL` | REJ | P1,P2 | `E_RISK_SLTP` |
| **R09** | SL 距离 | `0.002 ≤ abs(entry−SL)/entry ≤ 0.05` | REJ | P1,P2 | `E_RISK_SL_DISTANCE` |
| **R10** | 盈亏比 | `abs(TP−entry) / abs(entry−SL) ≥ 1.2`；强信心档另需 ≥ 2.0 | REJ | P1,P2 | `E_RISK_RR` |
| **R11** | 爆仓距离 | `(1/leverage − mmr) ≥ 2 × abs(entry−SL)/entry`，`mmr` 取交易所 `maintMarginPercent/100`（缺失按 1% 保守） | REJ | P1,P2 | `E_RISK_LIQ_DISTANCE` |
| **R12** | 最小名义/数量 | `notional ≥ max(20, exchange.minNotional)` 且 `qty ≥ max(minQty, stepSize)` | REJ | P1,P2 | `E_RISK_BELOW_MIN` |
| **R13** | 精度对齐 | `qty=floor(qty/step)·step`；价格按方向取整（§5.2 步 4） | ADJ | P1,P2 | `order_qty_snapped` |
| **R14** | 数据新鲜度 | 下单快照 age ≤ 15s；决策证据 age ≤ 400s；行情/证据任一缺失 → 禁开 | REJ | P1,P2 | `E_MARKET_STALE` |
| **R15** | 异常行情 | `abs(mark_t − mark_{t−60s})/mark ≥ 3%` 或 `abs(mark−index)/index ≥ 1%` 或 `spread ≥ 50bp` | REJ | P1,P2 | `E_MARKET_ANOMALY` |
| **R16** | 重复单 | 同 `clientOrderId` 已存在 / 同 `business_key` 有在途单 | SKIP | P1,P2 | `E_DUPLICATE_ORDER`(409) |
| **R17** | 未决订单 | `unknown_orders > 0` 或 `submitting_orders > 0` → 禁新开（平仓不受限） | REJ | P1,P2,P3 | `E_OUTBOX_PENDING` |
| **R18** | 日亏熔断 | `(day_start_equity − equity)/day_start_equity ≥ 3%` | LATCH → close_only | P3 | `risk_daily_loss_latched` |
| **R19** | 会话回撤 | `(peak_equity − equity)/peak_equity ≥ 8%` | LATCH → **emergency** | P3 | `risk_drawdown_latched` |
| **R20** | 连亏冷却 | 连续 2 笔净亏（`userTrades.realizedPnl` 汇总）→ 4h 禁新开 | LATCH(临时) | P3 | `risk_loss_streak_cooldown` |
| **R21** | 交易频率 | 当日开仓次数 ≤ 12；单 symbol ≤ 3 | REJ | P1 | `E_RISK_FREQUENCY` |
| **R22** | 控制态 | `control_state == normal` | REJ | P1,P2 | `E_CONTROL_LOCKED` |
| **R23** | 保护覆盖 | 所有持仓 `protection_status == OK`；`MISSING/PARTIAL/DUPLICATE` → 禁新开 | REJ | P1,P2,P3 | `E_PROTECTION_MISSING` |
| **R24** | 保证金率 | `totalMaintMargin / equity_exchange ≥ 80%` | **KILL** | P3 | `risk_margin_ratio_critical` |
| **R25** | 权益可用 | `equity` 新鲜（age ≤ 60s）且 `> 0` | REJ | P1,P2,P3 | `E_EQUITY_UNAVAILABLE` |

**组合相关性（可选，`exposure_guard`）**：同向且属于同一相关簇（BTC/ETH/SOL 视为一簇）的仓位 > `max_correlated_positions` → REJ。**v1 默认关闭**（`enabled: false`）——在"同时最多 1 仓"的前提下该规则无实际约束，保留字段只为 v1.1 放开多仓时免改 schema。

### 7.4 否决与降仓算法（P1/P2 共用）

```python
def evaluate(plan: OrderPlan, ctx: RiskContext) -> RiskVerdict:
    # 0) 结构性 / 状态类（只拒不放）
    for check in (r01_symbol, r02_position, r03_slots, r04_leverage,
                  r08_sltp, r09_sl_distance, r10_rr,
                  r14_freshness, r15_anomaly, r16_duplicate,
                  r17_outbox, r21_frequency, r22_control, r23_protection, r25_equity):
        if (reason := check(plan, ctx)):
            return RiskVerdict.reject(reason)          # 带 rule_id + 明细

    entry = ctx.mark if plan.entry_type == "MARKET" else plan.limit_price
    risk_per_unit = abs(entry - plan.sl)
    equity = ctx.equity_session
    risk_pct = (ctx.strong_limit if is_strong(plan, ctx) else cfg.per_trade.max_risk_pct)

    qty = Decimal(plan.qty)
    requested = qty
    reasons, downsized = [], False

    # 1) 单笔风险
    cap = equity * risk_pct / risk_per_unit
    if qty > cap:
        qty, downsized = cap, True
        reasons.append(("R05", f"risk {requested*risk_per_unit:.2f} -> {qty*risk_per_unit:.2f}"))

    # 2) 名义上限
    cap = equity * cfg.per_trade.max_notional_multiple / entry
    if qty > cap: qty, downsized = cap, True; reasons.append(("R06", ...))

    # 3) 保证金占用
    cap = (equity * cfg.portfolio.max_margin_usage - ctx.used_margin) * plan.leverage / entry
    if qty > cap: qty, downsized = cap, True; reasons.append(("R07", ...))

    # 4) 爆仓距离（与 qty 无关，但用调整后杠杆再确认）
    if ctx.liq_distance_ratio(plan.leverage) < 2 * risk_per_unit / entry:
        return RiskVerdict.reject(RuleViolation("R11", ...))

    # 5) 精度 + 最小名义
    qty = floor_to_step(qty, ctx.step_size)
    if qty <= 0 or qty * entry < max(Decimal(cfg.orders.min_notional_usdt), ctx.min_notional):
        return RiskVerdict.reject(RuleViolation("R12", "降仓后低于最小名义"))

    return RiskVerdict(verdict="reduced" if downsized else "approved",
                       approved_qty=qty, leverage=plan.leverage,
                       violations=reasons)
```

**顺序理由**：先拒"结构性错误"（方向/白名单/状态），再降"数量类"（风险/名义/保证金）。结构性错误降仓无意义；数量类错误降仓可挽救。降仓后**必须**重算最小名义，否则会出现"名义 21U → 降到 19U → 交易所拒单"的循环。

### 7.5 控制态与 kill switch

| 状态 | 允许开新仓 | 允许平仓/减仓 | 允许改保护 | 典型触发 |
|---|---|---|---|---|
| `normal` | ✅ | ✅ | ✅ | — |
| `pause` | ❌ | ✅ | ✅ | 手动（如临时不想跑） |
| `close_only` | ❌ | ✅ | ✅ | R18 日亏、R17 未决、保护缺失 <120s、权益 stale |
| `emergency` | ❌ | ✅（且**立即全平**） | ❌ | R19 回撤、R24 保证金率、保护缺失 >120s、连续 3 次提交错误、手动 |

**自动迁移表**：

| 触发 | 目标态 | 动作 | 恢复方式 |
|---|---|---|---|
| R18 日亏 latch | `close_only` | 撤未成交开仓单；不平仓 | 次日 UTC 日切自动解除 + 手动确认 |
| R19 回撤 latch | `emergency` | 全平 + 撤全部挂单/保护单 | **仅手动**：`POST /api/control` + 重新锚定 |
| 保护缺失 >120s（查询成功前提下） | `emergency` | 全平 + 撤单 | 手动 |
| UNKNOWN 未决 >120s | `close_only` | 禁止开新仓 | 人工收口后自动 |
| 权益 stale >120s | `emergency` | 全平（权益未知时平仓仍可执行） | 权益恢复 + 手动 |
| 连续 3 次提交失败（明确拒绝） | `close_only` | 记录 | 手动 |
| 手动 `POST /api/control` | 任一 | 见下 | 手动 |

**手动控制接口**：`POST /api/control`，body `{"state": "close_only|normal|emergency", "confirm": "<state 大写>", "reason": "..."}`。→ `normal` 必须附带 `reason` 且**不能**解除 R19 latch（latch 只能靠 re-anchor 解除）。

**Kill switch 的确定性要求**：状态**落库**（`control_state` 表单行 + `updated_at` + `reason`）；文件/库损坏 → 读 `close_only`（fail-closed）；状态变更必须写 `risk_events`；服务重启后**沿用**库里的状态（不会自动回 `normal`）。

### 7.6 latch 与恢复

| 项 | 设计 |
|---|---|
| 存储 | `risk_state` 单行：`anchor_id, date, day_start_equity, peak_equity, daily_loss_latched, daily_loss_latched_at, drawdown_latched, drawdown_latched_at, consecutive_losses, cooldown_until, updated_at` |
| 日切 | UTC 00:00 重置 `day_start_equity = equity`、清 `daily_loss_latched`；**不清** `drawdown_latched`（回撤锚点只在 re-anchor 时重置） |
| 峰值 | `peak_equity = max(peak_equity, equity)`，每个 L2 周期更新 |
| 持久化 | 原子写（tmp + `os.replace`），全程持锁（沿用 `risk_guard.py:63-67`） |
| 损坏 | `recovery_required=true` → `ready=false`，只允许平仓；恢复必须 `POST /api/capital/reanchor` 并携带"人工核验过的权益 + 明确锚点"（沿用 `risk_guard.py:179-204`） |
| 连亏计数 | 平仓完成时按 `net_pnl < 0` 累加；`> 0` 清零；冷却期内仍允许平仓 |

---

## 8. 服务 API 与错误码

### 8.1 端点（本地 `127.0.0.1:<port>`，写接口需 `Authorization: Bearer <token>`）

| 方法 | 路径 | 用途 | 幂等 | 备注 |
|---|---|---|---|---|
| GET | `/health` | 存活 + 版本 + demo guard + key 指纹 | ✅ | 不查询交易所（防雪崩） |
| GET | `/api/ready` | 开仓就绪（`ready` + `reasons[]`） | ✅ | 与执行层**同一判定器**，UI 不自己发明闸门 |
| GET | `/api/status` | 账户/权益/仓位/保护/控制态/未决单汇总 | ✅ | UI 10s 轮询主入口 |
| GET | `/api/equity-curve?range=1d&bucket=1m` | 权益曲线 | ✅ | 读 `equity_snapshots` |
| GET | `/api/orders?status=&limit=50` | 订单列表 | ✅ | |
| GET | `/api/fills?limit=100` | 成交列表 | ✅ | |
| GET | `/api/positions` | 仓位快照（含保护状态） | ✅ | |
| POST | `/api/risk/evaluate` | **P1 计划闸**：入参 `OrderPlan`，出参 `RiskVerdict` | ✅（纯函数，无副作用） | 不入库时可加 `?dry_run=1` |
| POST | `/api/cycle/run` | 手动触发一轮决策 | ⚠️ | 60s 内重复 → 409 |
| POST | `/api/execution/order` | **P2 提交闸 + 下单**：入参 `ApprovedPlan` | 按 `clientOrderId` | 只接受已 P1 approved 的 plan（带签名/令牌） |
| POST | `/api/execution/close` | 平仓/减仓 `{symbol, qty?}` 或 `{symbol, all:true}` | 按 `clientOrderId` | 风控只允许 `close` 分支 |
| POST | `/api/protection/repair` | 手动触发保护核对与修复 | ✅ | 修复失败返回 `E_PROTECTION_FAILED` |
| POST | `/api/recovery/reconcile` | 手动全量对账（启动对账 12 步） | ✅ | 重启/异常后使用 |
| POST | `/api/control` | 控制态切换 | ⚠️ | 需 `confirm` 字段 |
| POST | `/api/capital/reanchor` | 人工重新锚定 baseline | ⚠️ | 需 `confirm:"REANCHOR"` + 两处权益 |
| POST | `/api/orders/{cid}/resolve` | 人工收口 UNKNOWN | ⚠️ | 只允许 `CANCELED/EXPIRED/REJECTED` + `operator` + `evidence` |

**约定**：所有金额/数量用**十进制字符串**；时间为 ISO-8601 UTC（`2026-11-09T01:40:00.000Z`）；所有响应带 `trace_id`。

### 8.2 关键响应示例

**`GET /api/ready`**（200 就绪 / 503 不就绪）：

```json
{
  "ready": false,
  "reasons": ["ORDER_OUTBOX_UNKNOWN", "PROTECTION_MISSING:BTCUSDT"],
  "environment": "demo",
  "base_url": "https://demo-fapi.binance.com",
  "control_state": "close_only",
  "broker_ok": true,
  "equity": {"session": "4987.20", "exchange": "5000.00", "stale_ms": 12000},
  "risk": {"daily_loss_ratio": "0.0026", "drawdown_ratio": "0.0031",
            "daily_latched": false, "drawdown_latched": false},
  "outbox": {"unknown": 1, "submitting": 0, "oldest_unknown_age_s": 47},
  "protection": {"positions": 2, "ok": 1, "missing": ["BTCUSDT"]},
  "server_time": "2026-11-09T01:40:12.345Z",
  "trace_id": "01J..."
}
```

**`POST /api/risk/evaluate`**：

```json
// 请求
{
  "cycle_id": "1109T0140",
  "plan": {
    "symbol": "BTCUSDT", "side": "LONG", "action": "open",
    "entry_type": "MARKET", "leverage": 8,
    "qty": "0.015", "sl": "99000.0", "tp": "102800.0",
    "confidence": 0.72, "reasoning_ref": "decision:1109T0140"
  },
  "context": { "mark": "100200.0", "snapshot_age_ms": 3200,
               "evidence_age_ms": 45000, "equity_session": "4987.20" }
}
// 200
{
  "verdict": "reduced",
  "approved_qty": "0.0124",
  "leverage": 8,
  "risk": {"amount": "49.87", "pct": "0.0100", "rr": 2.65},
  "violations": [{"rule": "R05", "action": "downsize",
                  "detail": "qty 0.015 -> 0.0124 (risk 74.8 -> 49.87)"}],
  "blocking": []
}
// 422
{
  "verdict": "rejected",
  "approved_qty": "0",
  "violations": [{"rule": "R10", "action": "reject",
                  "detail": "RR 0.9 < min 1.2"}],
  "error": {"code": "E_RISK_RR", "message": "盈亏比不足", "retryable": false}
}
```

**`POST /api/execution/order`**（成功 201 / 幂等 200 / 失败 4xx）：

```json
{
  "client_order_id": "wb-1109T0140-BTC-O-0",
  "status": "FILLED",
  "exchange_order_id": "8321001234",
  "qty_requested": "0.0124", "qty_filled": "0.0124", "avg_price": "100198.40",
  "protection": {"sl": {"client_algo_id": "wbp-...", "status": "NEW"},
                  "tp": {"client_algo_id": "wbp-...", "status": "NEW"}},
  "idempotent_replay": false
}
```

### 8.3 `ready` 原因码（UI 文案映射表）

| 原因码 | 含义 | 建议 UI |
|---|---|---|
| `DEMO_GUARD_FAILED` | base_url 非 demo | 红色阻断"环境非法" |
| `SERVICE_STARTING` | 启动对账未完成 | 灰色"启动中" |
| `EXCHANGE_UNREACHABLE` | Demo REST 不可达 | 红色 + 最后一次成功时间 |
| `SYMBOL_FILTERS_UNAVAILABLE` | exchangeInfo 拉取失败 | 红色 |
| `POSITION_MODE_UNPROVEN` | 无法证明持仓模式 | 红色 |
| `EQUITY_UNAVAILABLE` | 权益不可用 | 红色 + 最近有效值 |
| `RISK_STATE_CORRUPT` | 风控状态损坏 | 红色"需人工重新锚定" |
| `CONTROL_NOT_NORMAL` | 控制态非 normal | 黄/红按状态 |
| `ORDER_OUTBOX_UNKNOWN` | 有结果未知订单 | 黄"待核收口" |
| `ORDER_OUTBOX_SUBMITTING` | 有在途提交 | 灰"提交中" |
| `PROTECTION_MISSING:<SYM>` | 保护缺失 | 红"保护缺失" |
| `MARKET_STALE` | 行情过期 | 黄 |
| `MARKET_ANOMALY` | 异常行情 | 黄 |
| `DAILY_LOSS_LATCHED` | 日亏熔断 | 黄"今日停止开仓" |
| `DRAWDOWN_LATCHED` | 回撤熔断 | 红"需人工重置" |

### 8.4 错误码表

| code | HTTP | retryable | 含义 | 处置 |
|---|---|---|---|---|
| `E_DEMO_GUARD_CONFIG` | — | ❌ | 配置非 demo | 进程退出 |
| `E_DEMO_GUARD_RUNTIME` | 500 | ❌ | 请求 host 非白名单 | 阻断 + critical |
| `E_VALIDATION` | 400 | ❌ | 请求体 schema 不合法 | 修正请求 |
| `E_NOT_READY` | 503 | ⚠️ | 前置条件不满足（带 reasons） | 看 `/api/ready` |
| `E_CONTROL_LOCKED` | 423 | ❌ | 控制态禁止该操作 | 切控制态 |
| `E_CYCLE_IN_PROGRESS` | 409 | ⚠️ 稍后 | 已有轮次在执行 | 等待 |
| `E_CYCLE_DUPLICATE_SLOT` | 409 | ❌ | 同一 10min slot 已执行 | 忽略 |
| `E_RISK_<RULE>` | 422 | ❌ | 风控拒绝（R01…R25） | 调策略/改计划 |
| `E_RISK_BELOW_MIN` | 422 | ❌ | 降仓后低于最小名义 | 放弃该笔 |
| `E_DUPLICATE_ORDER` | 409 | ❌ | 重复单（幂等） | 返回首次结果 |
| `E_OUTBOX_PENDING` | 409 | ⚠️ | 有未决订单 | 先收口 |
| `E_EXCHANGE_UNAVAILABLE` | 502 | ✅ 退避 | Demo 不可达 | 自动重试（只 GET） |
| `E_RATE_LIMITED` | 429 | ✅ 按 `Retry-After` | 418/429 | 冷却，禁开新仓 |
| `E_EXCHANGE_REJECT` | 422 | ❌ | 交易所明确拒绝（带 `binance_code`） | 按码处置（§9） |
| `E_FILL_UNCONFIRMED` | 202 | ✅ | 已提交但未确认成交 | 对账收口 |
| `E_PROTECTION_FAILED` | 500 | ⚠️ | 保护单挂失败（已尝试平仓） | 人工核验 flat |
| `E_PROTECTION_MISSING` | 409 | ⚠️ | 保护覆盖不足 | 修复后重试 |
| `E_POSITION_MISMATCH` | 409 | ⚠️ | 本地与交易所不一致 | 全量对账 |
| `E_NO_POSITION` | 200/409 | ❌ | 无仓位可平（幂等成功） | 忽略 |
| `E_EQUITY_UNAVAILABLE` | 503 | ⚠️ | 权益不可用 | 禁开新仓 |
| `E_TAKEOVER_BLOCKED` | 409 | ❌ | 接管前置条件不满足 | 人工清场 |
| `E_LEDGER_WRITE` | 500 | ⚠️ | 落库失败（已阻止发单） | 修复 DB |
| `E_INTERNAL` | 500 | ⚠️ | 未分类异常 | 看日志 `trace_id` |

> 交易所 `binance_code` 映射（已知/来源标注）：

| binance code | 语义 | 处理 | 来源 |
|---|---|---|---|
| `-4120` | 该类型单必须走 Algo 接口 | 切 Algo 通道 | 原项目实测（`binance_futures.py:1603`） |
| `-2011` | Unknown order | 视为已撤/已触发 | 原项目实测（`:1680`） |
| `-2013` | Order does not exist | 查询跳过，下轮重试 | 原项目实测（`:1269`） |
| `-1021` | 时间戳超窗 | 重校准后**仅 GET** 重试一次 | 原项目实测（`:426-430`） |
| `-1022` | 签名无效（多为时间戳） | 同上 | 原项目实测（`:426-430`） |
| `-2022` | ReduceOnly 被拒（数量 > 仓位） | 重读仓位后重算数量（只许减） | 原项目实测（`:544`） |
| `-4165` | 时间窗口超限 | 切窗口重取 | 原项目实测（`:2860`） |
| `-2019` | 保证金不足 | 拒绝，不重试 | 官方通用 |
| `-4164` | 名义低于下限 | 拒绝（R12 兜底） | 官方通用 |
| `-1111` | 精度超限 | 重算一次 stepSize | 官方通用 |
| 其它 4xx | 参数/权限类 | 拒绝 + 记 `binance_code`，**不重试** | — |
| 5xx / 超时 | 网络不确定 | outbox `UNKNOWN` → 查询 | — |

---

## 9. Q10 · 异常与恢复（失败矩阵）

> 通用原则：**写操作绝不盲目重试**；读操作可退避重试；任何"不确定"都落到 UNKNOWN 语义并**阻断开新仓**，但**不阻断平仓**（平仓是风险收敛方向）。

### 9.1 失败矩阵 F01–F20

| ID | 场景 | 检测 | 立即动作 | 退避/重试 | 用户可见 | 恢复条件 |
|---|---|---|---|---|---|---|
| **F01** | Demo REST 不可达（DNS/连接/超时） | `httpx.TransportError` | L1 保留最近有效值 + `stale`；`ready=false` | GET 退避 1/2/4/8s，上限 30s；**POST 不重试** | 顶栏"Demo 连接中断"，显示最后成功时间 | 连续 2 次成功 |
| **F02** | `418`/`429` 限流 | HTTP 状态 + `Retry-After` | 进入冷却；禁开新仓；`close_only` 除非已 emergency | 冷却 ≥ `Retry-After`（418 默认 480s） | 横幅"限流冷却中 08:00" | 冷却结束 + 一次成功请求 |
| **F03** | 时钟漂移 `-1021/-1022` | 错误码 | 重校准 `/fapi/v1/time` | **仅 GET** 重试一次；POST 转查询 | 无感（日志 warn） | 下一次成功 |
| **F04** | 下单响应丢失/超时 | POST 抛异常 | outbox→`UNKNOWN`；按 `origClientOrderId` 查询 | 2/4/8/16/32/60s（单次等待上限 60s，总窗口约 2 分钟） | 订单显示"确认中" | 查到终态；超 120s → 禁开新仓 + 人工 |
| **F05** | 开仓部分成交 | `qty_filled < qty_requested` | 以真实仓位挂保护 | — | 订单显示 filled/requested | 保护覆盖 OK |
| **F06** | 保护单挂失败（确证） | Algo 返回拒绝 | 重试 1 次 → **市价平仓** → 复核 flat → 撤残留 | 平仓失败再重试 1 次 | 红色"保护失败已平仓" | flat 且无残留保护单 |
| **F07** | 保护单被外部撤销/触发后残留 | 30s 对账发现数量 ≠ 1 | 先挂缺失 → 校验 → 撤多余 | 3 个周期内修复 | 仓位卡红标 | `protection_status=OK` |
| **F08** | 周期重复触发 | `cycle_runs.slot_id` 冲突 或 进程锁占用 | 跳过本轮 | — | 无（日志） | 下一 slot |
| **F09** | 平仓响应丢失 | POST 异常 | outbox→`UNKNOWN` → 按 cid 查询 | 同 F04 | "平仓确认中" | 查到 FILLED 或仓位为 0 |
| **F10** | 权益接口不可用 | `account` 失败 | 不写 baseline；禁开新仓 | GET 退避 | 权益区显示"延迟" | 权益恢复 |
| **F11** | 本地/交易所仓位不一致 | L1 比对 | 交易所覆盖本地；收养外部仓 + 兜底保护 | — | "检测到外部仓位" | 记录一致 |
| **F12** | 保护触发后仍有持仓（极端行情） | 对账发现仓位但无保护 | 市价平仓（`reduceOnly`） | 1 次 | 红色 | flat |
| **F13** | emergency 全平失败 | 平仓订单 REJECTED/异常 | 重试 1 次 → 逐 symbol 单独平 → 仍失败则只告警 | 每 10s 重试，上限 5 次 | 红色"需人工介入" | flat |
| **F14** | 服务重启 | 进程启动 | `SUBMITTING→UNKNOWN`；跑启动对账 12 步 | — | "启动对账中" | 12 步全过 |
| **F15** | 保证金不足 `-2019` | 交易所拒绝 | 拒绝该单，不重试 | — | 风控提示 | 仓位/权益变化 |
| **F16** | 精度/最小名义被拒 | `-1111/-4164` | 重算一次 stepSize/minNotional → 仍失败则拒 | 1 次 | 订单拒绝原因 | — |
| **F17** | 行情过期/空窗 | `snapshot_age > 15s` | 禁开新仓（平仓不受限） | 行情侧重试 | "行情延迟" | 快照恢复 |
| **F18** | 决策 JSON 非法 | schema 校验失败（Host 侧） | 本轮 `no_trade` | 重试 1 次（同证据） | 决策链显示"解析失败" | 下一轮 |
| **F19** | DB 写失败（outbox/orders） | 异常 | **禁止发单**（`E_LEDGER_WRITE`） | 3 次后 `close_only` | 红色 | DB 恢复 + 对账 |
| **F20** | 重复/并发请求（双 Host 实例） | 进程锁（`flock`/命名互斥） | 第二个实例**拒绝启动** | — | "已有实例在运行" | 关闭旧实例 |

### 9.2 重启恢复顺序（严格串行）

```text
1. 取进程锁（F20）
2. 加载配置 → L1/L2 demo guard
3. 打开 DB → 校验 schema_version / ledger_id
4. control_state 读取（损坏→close_only）
5. risk_state 读取（损坏→recovery_required）
6. outbox: SUBMITTING → UNKNOWN
7. exchangeInfo / positionSide / account / positions / openOrders / openAlgoOrders
8. reconcile_unknown（≤3 笔）
9. 收养外部仓位 + 兜底保护
10. 收口"本地有、交易所无"的仓位
11. baseline 校验（不可用则不建）
12. ready = 全部通过
```

**任一步失败 → 不进入 `normal`**：允许 `close_only`（可平仓/修复），UI 直接展示原因码。

### 9.3 人工操作清单（UI 只暴露这几项）

| 操作 | 入口 | 前置 | 后果 |
|---|---|---|---|
| 暂停跑轮 | `/api/control` pause | — | 不自动跑决策 |
| 全平 | `/api/execution/close {all:true}` | 二次确认 | 平全部仓位 + 撤保护 |
| 紧急停止 | `/api/control` emergency | 二次确认 | 全平 + 撤单 + 禁开 |
| 恢复运行 | `/api/control` normal | `reason` 必填；R19 未解除时无效 | 恢复自动跑轮 |
| 重新对账 | `/api/recovery/reconcile` | — | 跑 12 步 |
| 修复保护 | `/api/protection/repair` | — | 补齐 SL/TP |
| UNKNOWN 收口 | `/api/orders/{cid}/resolve` | `operator` + `evidence` | 只允许无成交终态 |
| 重新锚定 | `/api/capital/reanchor` | 人工核验权益 | 重置 baseline + 清 drawdown latch |

---

## 10. 数据模型（精简 schema，10 表）

```sql
-- 元信息：确认"这是新账本"
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);  -- schema_version, ledger_id, created_at

-- 订单（唯一真相 = 交易所；本表是镜像 + 归因）
CREATE TABLE orders (
  client_order_id   TEXT PRIMARY KEY,
  exchange_order_id TEXT,
  kind              TEXT NOT NULL,       -- STANDARD | PROTECTION
  purpose           TEXT NOT NULL,       -- ENTRY | EXIT | SL | TP
  symbol            TEXT NOT NULL,
  side              TEXT NOT NULL,       -- BUY | SELL
  position_side     TEXT,                -- LONG | SHORT | NULL(one-way)
  order_type        TEXT NOT NULL,       -- MARKET | STOP_MARKET | TAKE_PROFIT_MARKET
  qty_requested     TEXT NOT NULL,       -- 十进制文本
  qty_filled        TEXT NOT NULL DEFAULT '0',
  avg_price         TEXT,
  trigger_price     TEXT,
  reduce_only       INTEGER NOT NULL DEFAULT 0,
  close_position    INTEGER NOT NULL DEFAULT 0,
  status            TEXT NOT NULL,
  cycle_id          TEXT,
  binance_code      INTEGER,
  error_msg         TEXT,
  raw_json          TEXT,
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  resolved_at       TEXT
);
CREATE INDEX idx_orders_symbol_time ON orders(symbol, created_at DESC);
CREATE INDEX idx_orders_status      ON orders(status);

-- 提交前意图（幂等 + UNKNOWN 恢复）
CREATE TABLE order_outbox (
  client_order_id  TEXT PRIMARY KEY,
  environment      TEXT NOT NULL DEFAULT 'demo',
  business_key     TEXT NOT NULL,        -- {purpose}:{symbol}:{position_side}
  endpoint         TEXT NOT NULL,
  request_json     TEXT NOT NULL,
  status           TEXT NOT NULL,        -- SUBMITTING/UNKNOWN/...（与 orders.status 同词表）
  attempts         INTEGER NOT NULL DEFAULT 0,
  unknown_since    TEXT,
  next_retry_at    TEXT,
  last_error       TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  resolved_at      TEXT
);
CREATE UNIQUE INDEX uq_outbox_active_entry
  ON order_outbox(environment, business_key)
  WHERE business_key LIKE 'ENTRY:%' AND status IN
        ('SUBMITTING','UNKNOWN','ACKNOWLEDGED','NEW','PARTIALLY_FILLED');

-- 成交（幂等键 = exchange tradeId）
CREATE TABLE fills (
  fill_id        TEXT PRIMARY KEY,       -- 交易所 tradeId
  client_order_id TEXT NOT NULL,
  symbol         TEXT NOT NULL,
  side           TEXT NOT NULL,
  price          TEXT NOT NULL,
  qty            TEXT NOT NULL,
  realized_pnl   TEXT,
  commission     TEXT,
  trade_time_ms  INTEGER NOT NULL,
  source         TEXT NOT NULL           -- userTrades | ws(later)
);

-- 仓位（快照，一行一仓）
CREATE TABLE positions (
  ledger_id       TEXT NOT NULL,
  symbol          TEXT NOT NULL,
  position_side   TEXT NOT NULL,          -- LONG | SHORT
  qty             TEXT NOT NULL,
  entry_price     TEXT NOT NULL,
  mark_price      TEXT,
  upnl            TEXT,
  leverage        INTEGER,
  margin          TEXT,
  margin_mode     TEXT,
  liq_price       TEXT,
  sl_price        TEXT,
  tp_price        TEXT,
  protection_status TEXT NOT NULL,        -- OK|MISSING|PARTIAL|DUPLICATE|UNKNOWN
  origin          TEXT NOT NULL,          -- OPENED_BY_WORKBENCH|EXTERNAL
  opened_at       TEXT,
  updated_at      TEXT NOT NULL,
  PRIMARY KEY (ledger_id, symbol, position_side)
);

-- 仓位变化事件（只在变化时写）
CREATE TABLE position_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  at            TEXT NOT NULL,
  symbol        TEXT NOT NULL,
  position_side TEXT NOT NULL,
  kind          TEXT NOT NULL,           -- OPEN|CHANGE|CLOSE|ADOPT|CLOSE_EXTERNAL|PROTECTION_CHANGE
  before_json   TEXT,
  after_json    TEXT,
  note          TEXT
);

-- 权益快照（30s，bucket 去重）
CREATE TABLE equity_snapshots (
  ledger_id        TEXT NOT NULL,
  ts_bucket        TEXT NOT NULL,        -- ISO 秒，30s 对齐
  equity_exchange  TEXT NOT NULL,
  equity_session   TEXT NOT NULL,
  wallet_balance   TEXT,
  available        TEXT,
  upnl             TEXT,
  margin_used      TEXT,
  day_start_equity TEXT,
  peak_equity      TEXT,
  daily_loss_ratio TEXT,
  drawdown_ratio   TEXT,
  source           TEXT NOT NULL,        -- loop|fill|manual
  PRIMARY KEY (ledger_id, ts_bucket)
);

-- 风控状态（单行）
CREATE TABLE risk_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  anchor_id TEXT NOT NULL,
  date TEXT NOT NULL,
  day_start_equity TEXT,
  peak_equity TEXT,
  daily_loss_latched INTEGER NOT NULL DEFAULT 0,
  daily_loss_latched_at TEXT,
  drawdown_latched INTEGER NOT NULL DEFAULT 0,
  drawdown_latched_at TEXT,
  consecutive_losses INTEGER NOT NULL DEFAULT 0,
  cooldown_until TEXT,
  recovery_required INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- 风控事件（可审计，UI 时间线）
CREATE TABLE risk_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  rule_id TEXT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL,               -- info|warn|critical
  action TEXT,
  details_json TEXT,
  trace_id TEXT
);

-- 轮次
CREATE TABLE cycle_runs (
  cycle_id TEXT PRIMARY KEY,
  slot_id TEXT,                          -- 10min 对齐；手动轮次为 NULL
  state TEXT NOT NULL,                   -- RUNNING|DONE|FAILED|SKIPPED
  started_at TEXT NOT NULL,
  finished_at TEXT,
  outcome TEXT,                          -- trade|no_trade|error
  error_code TEXT,
  decision_json TEXT                     -- 引用 decisions 表/证据文件
);
CREATE UNIQUE INDEX uq_cycle_slot ON cycle_runs(slot_id) WHERE slot_id IS NOT NULL;

-- 控制态（单行）
CREATE TABLE control_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL,                   -- normal|pause|close_only|emergency
  reason TEXT,
  updated_at TEXT NOT NULL,
  updated_by TEXT
);
```

> 与基线 §5.7（可审计）对齐：输入证据快照写 `data/evidence/{cycle_id}.json` + `sha256`（由 t2 定义结构）；本文档只负责 `execution_events / orders / fills / equity_snapshots / risk_events` 的落库与引用。

---

## 11. 实现清单与验收

### 11.1 模块清单（M1 范围）

| 模块 | 职责 | 预估行数 |
|---|---|---|
| `wb/demo_guard.py` | L1/L2/L3 三层 host 校验 + `ALLOWED_HOSTS` | 60 |
| `wb/demo_broker.py` | 签名请求、过滤器、普通单、Algo 保护单、账户/持仓读取、`close_all` | 480 |
| `wb/demo_outbox.py` | prepare/ack/reject/mark_unknown/reconcile/manual_resolve | 220 |
| `wb/risk_rules.py` | R00–R25 纯函数 + 降仓算法 + latch 判定 | 300 |
| `wb/risk_state.py` | risk_state/control_state 持久化（同构移植 `risk_guard.py`） | 120 |
| `wb/ledger.py` | 10 表读写、原子事务、权益快照、仓位覆盖 | 260 |
| `wb/reconcile.py` | 启动 12 步 + 周期对账 + 收养/孤儿 | 240 |
| `wb/api.py` | §8 端点、错误码映射、token 鉴权 | 220 |
| `wb/loops.py` | L1/L2/L3/L4 调度、锁、日切 | 160 |
| 合计 | | **≈ 2,060** |

> 与 §2.3 的"1,300–1,600 行"差异来自把 API/循环单独拆出；实际可合并。

### 11.2 配置骨架（可直接落文件）

```yaml
server:
  host: 127.0.0.1
  port: 8791
  token_env: WORKBENCH_API_TOKEN

exchange:
  environment: demo                      # 唯一合法值（Literal）
  base_url: https://demo-fapi.binance.com
  api_key_env: BINANCE_DEMO_API_KEY
  api_secret_env: BINANCE_DEMO_API_SECRET
  recv_window_ms: 5000
  request_timeout_s: 12
  connect_timeout_s: 5
  user_agent: dsh-trading-workbench/0.1

ledger:
  db_path: data/workbench-v1.sqlite
  evidence_dir: data/evidence
  equity_snapshot_seconds: 30
  position_snapshot_seconds: 30

loops:
  state_refresh_seconds: 10
  protection_check_seconds: 30
  unknown_reconcile_seconds: 30
  decision_interval_minutes: 10
  daily_rollover_utc: "00:05"

execution:
  entry_order_type: MARKET               # v1 仅 MARKET
  margin_mode: ISOLATED
  fill_poll_attempts: 8
  fill_poll_interval_ms: 600
  market_max_qty_fallback: 5000
  protection:
    sl_type: STOP_MARKET
    tp_type: TAKE_PROFIT_MARKET
    working_type: MARK_PRICE
    price_protect: true
    close_position: true
```

### 11.3 验收用例（M1 gate）

**契约探针（只读/最小额，人工执行一次，记录到 `docs/evidence/demo-probe-YYYYMMDD.md`）**

| ID | 用例 | 期望 |
|---|---|---|
| T01 | `GET /fapi/v1/time` + `GET /fapi/v1/exchangeInfo` | 200；记录 `stepSize/tickSize/minNotional` |
| T02 | `GET /fapi/v1/positionSide/dual` | 返回 `dualSidePosition`（证明模式） |
| T03 | 最小额 MARKET 开后立即 `POST /fapi/v1/algoOrder`（STOP_MARKET, `closePosition=true`）→ `GET /fapi/v1/openAlgoOrders` → `DELETE` → 平仓 | 验证 D1/D2/D10；记录字段名与返回 `code` 类型 |

**离线单测（CI 可跑，全 mock，无网络）**

| ID | 用例 | 断言 |
|---|---|---|
| T10 | demo guard：`base_url=https://fapi.binance.com` | 抛 `DemoGuardViolation`；`httpx.request` 调用 0 次 |
| T11 | 配置 `environment=live` | 校验失败，进程不 ready |
| T12 | 静态扫描 | 排除 `demo-fapi` 前缀后，`fapi.binance.com` 命中 0；`BINANCE_LIVE` 命中 0 |
| T13 | Outbox：prepare 失败 | 不发 POST |
| T14 | POST 超时 → 查询命中 | 只 1 次 POST，状态收敛 NEW/FILLED |
| T15 | POST 超时 → 查询未命中 | 状态 UNKNOWN，`ready=false`，**不发第二次 POST** |
| T16 | 重复 `clientOrderId` | 第二次返回 `duplicate_skipped`，POST 1 次 |
| T17 | 保护单挂失败 | 触发市价平仓；平仓失败 → emergency |
| T18 | `openAlgoOrders` 抛异常 | 不重挂、不平仓，`protection=UNKNOWN` |
| T19 | `openAlgoOrders` 返回缺 TP | 先挂 TP；仍失败 → 平仓 |
| T20 | 部分成交 | 保护基数 = 真实仓位；`qty_filled` 正确 |
| T21 | R05 降仓 | `qty` 被压到 `equity×1%/risk_per_unit`；再算 R12 |
| T22 | R10 RR=0.9 | REJ，`E_RISK_RR` |
| T23 | R09 SL 距离 0.1% / 6% | REJ |
| T24 | R11 爆仓距离不足 | REJ |
| T25 | R18 日亏 3% | latch + `close_only`；重启后仍 latch |
| T26 | R19 回撤 8% | `emergency` + 全平调用 1 次 |
| T27 | risk_state 文件损坏 | `recovery_required`，`ready=false`，仅允许平仓 |
| T28 | 定时器双触发 | 第二次 `E_CYCLE_DUPLICATE_SLOT` |
| T29 | 重启：outbox 有 SUBMITTING | 变 UNKNOWN，且**不重发** |
| T30 | 收养外部仓位 | 兜底 SL/TP 被挂（2%/4%），事件 critical |
| T31 | `-2011` 撤单 | 视为 CANCELED，不报错 |
| T32 | `-2022` 平仓 | 重读仓位、重算数量，仍只减不增 |

**人工验收（Demo 环境，小额）**

| ID | 场景 | 期望 |
|---|---|---|
| T40 | 完整一轮：开仓 + SL/TP | 交易所可见 1 仓 + 2 保护单；UI 一致 |
| T41 | 手动触发保护触发（把 SL 调到接近市价） | 仓位平掉；本地收口；曲线更新 |
| T42 | 断网 60s | `ready=false`，恢复后自动回 `normal` |
| T43 | 强制 kill（`control=emergency`） | 全平 + 撤单；`ready=false`；重启后仍 emergency |
| T44 | 手动 reanchor | `session_equity == initial_equity`；drawdown latch 清除 |

### 11.4 里程碑（本文档范围）

| 里程碑 | 内容 | 出口条件 |
|---|---|---|
| **M1-a 契约** | T01–T03 探针 + 坑位清单定稿 | 记录归档，D1/D2/D10 确认 |
| **M1-b 执行骨架** | guard + broker + outbox + 单笔开/平 + 保护 | T10–T20 通过；Demo 小额 T40 |
| **M1-c 风控** | risk_rules + 两闸门 + latch + kill switch | T21–T28 通过 |
| **M1-d 对账/恢复** | 启动 12 步 + 周期对账 + 人工收口 | T29–T32、T42–T44 通过 |
| **M2** | 复盘、日切、曲线聚合、UI 深度整合 | 另行定义 |

---

## 12. 与其它文档的契约（供 t1/t2/t4/t5 直接引用）

| 契约 | 内容 | 消费者 |
|---|---|---|
| `OrderPlan` / `ApprovedPlan` | §8.2 的 `plan` 对象字段：`symbol / side / action=open / entry_type / leverage / qty / sl / tp / confidence / reasoning_ref`；`cycle_id` 属**请求信封**（不属于 plan）。**plan 不携带 `clientOrderId`**——由执行器按 `cycle_id+symbol+action` 派生（§5.7），若模型仍输出则忽略并记 warn。`ApprovedPlan = plan + approved_qty + verdict_ref` | t2（操盘手输出必须能映射到此） |
| `RiskVerdict` | `{verdict: approved/reduced/rejected, approved_qty, leverage, violations[{rule,action,detail}], error{code}}` | t2（风控 agent 展示）、t5（UI 角标） |
| `ready.reasons[]` 原因码 | §8.3 全表 | t5（文案映射）、t1（健康检查） |
| 控制态枚举 | `normal / pause / close_only / emergency` | t1、t5 |
| 订单状态枚举 | §5.1 全表 + `UNKNOWN` 语义 | t5（订单列表）、t2（复盘归因） |
| `position_summary` | §6.3 的 JSON | t2（操盘手/风控 agent 输入） |
| 行情新鲜度字段 | `snapshot_age_ms` / `evidence_age_ms` / `stale` 标记 | t4 提供，本文档消费（R14/R15） |
| 证据快照引用 | `cycle_id → evidence_path + sha256` | t2 产出，本文档只存引用 |
| 服务端口/token | `127.0.0.1:8791` + `WORKBENCH_API_TOKEN` | t1 统一（若 t1 定别的端口，仅改配置） |

**冲突预防约定**：
- 本文档**不**定义 agent prompt / 决策 JSON schema（属 t2），只定义"执行侧接受什么"。
- 本文档**不**定义 UI 组件（属 t5），只定义状态与原因码。
- 本文档**不**定义服务进程如何被拉起（属 t1），只定义"服务必须自证 ready"。

---

## 13. 未决问题（需要用户/t1 拍板）

| # | 问题 | 影响 | 建议 |
|---|---|---|---|
| Q1 | 是否能新建**独立 Demo 账户**（方案 B） | §3.1 走 A/B/C 哪条 | 建议 B；若否，走 C + 接管前置 |
| Q2 | 初始权益锚点取多少（5000？实际余额？） | 单笔风险/日亏阈值绝对值 | 建议 `anchor_mode=exchange_snapshot`：首次启动时 `baseline = 当前权益 − initial_equity`，会话权益从 `initial_equity`（建议 5000）起算 |
| Q3 | 账户持仓模式是否可改为 One-way | §5.6 分支、`reduceOnly` 语义 | 建议 One-way；锁定 Hedge 则启用 `positionSide` 分支 |
| Q4 | v1 是否允许人工在币安网站直接操作（手动平仓/改杠杆） | 收养逻辑、对账告警频率 | 建议允许但**必须在 UI 标注 EXTERNAL** |
| Q5 | UI 主曲线用 `equity_session` 还是 `equity_exchange` | §4.1 | 建议 session 为主、exchange 为副 |
| Q6 | 单笔风险 1%、日亏 3%、回撤 8%、杠杆 10x 是否接受 | §7.2 全部阈值 | 这组数字是"Demo 验证决策质量"的保守起点，可整体调档 |
| Q7 | 是否接受"不提供限价入场（v1.1）" | §1.2 | 建议接受（少一个状态机） |
| Q8 | 服务端口/鉴权口径 | §8.1 | 待 t1 统一 |
| Q9 | 是否需要 30s 以外的更高频仓位刷新 | §6.2 | 建议 10s（L1），30s 落库 |

---

## 14. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-09 | 初版：回答基线 §6 Q2/Q3/Q6/Q10；给出复用/新写结论、Demo 订单生命周期、20 条失败矩阵、26 条风控规则与可直接落地的配置骨架 |
| 2026-11-09 | 与 02（AI 链）跨文档对齐：① `RiskVerdict.verdict` 统一为 `approved/reduced/rejected`；② 明确 `OrderPlan` 字段集与"`clientOrderId` 由执行器派生、AI 不生成"；③ 硬上限 `max_open_positions` 由 3 调为 **1**，与 AI 链"同时最多 1 仓"产品约束合一（见 §15） |
| 2026-11-09 | U-06 时间预算同步：`decision_evidence_max_age_ms=180000`、R14 决策证据 age ≤180s；保护/UNKNOWN/权益 stale 等无关阈值不变（见 §15） |

---

## 15. 跨文档对齐记录（与 02 / AI 链，2026-11-09）

t2（ai-chain）提出的三处差异 + U-06 时间预算同步，处理如下（02 侧已按 03 更新，本表为 03 侧确认）：

| # | 差异 | 03 的处理 | 结论 |
|---|---|---|---|
| 1 | `RiskVerdict.verdict` 取值：03 §12 曾写 `approve`，而 §7.4/§8.2 用 `approved` | 以 §7.4/§8.2 的实现口径为准，§12 改为 **`approved`** | ✅ 已对齐，统一 `approved/reduced/rejected` |
| 2 | 操盘手输出映射到 `OrderPlan`：`symbol/side/action=open/entry_type/leverage/qty/sl/tp/confidence/reasoning_ref`；**AI 不生成 `clientOrderId`** | P1/P2 接受上述字段（§8.2 `plan` 对象）；`cycle_id` 由请求信封提供；`clientOrderId` 由执行器按 `cycle_id+symbol+action` 派生（§5.7），plan 携带则忽略并 warn | ✅ 接受，无需 t2 改 02 |
| 3 | 02 产品约束 AI 链同时最多 1 仓（03 原硬上限 3）；持仓 >1 时 02 `NO_TRADE(POSITION_EXISTS)` 不接管 | **03 硬上限改为 1**（R03 / `portfolio.max_open_positions`），与产品约束合一，消除"两个最大持仓数"的矛盾。R03 只约束**新开仓**：接管/外部仓位仍可能 >1，此时 02 不接管、03 保护与平仓巡检照常（§4.5/§5.6） | ✅ 已对齐，无需 t2 改 `MANAGE actions[]`；v1.1 若要放开多仓，需同时改 R03 与 02 的 MANAGE 能力 |
| 4 | U-06 两轮后 AI 链硬截止 140s，03 原 R14 决策证据上限过短、会踩线 | **2026-11-09 已同步 180s**：`decision_evidence_max_age_ms=180000`、R14 age ≤180s；其它保护/UNKNOWN/权益 stale 阈值不变 | ✅ 已对齐，02/v1 无残留标记 |

> 2026-11-09 后续方案 A：AI 链硬截止由 140s 调整为 **180s**（R1/R2 32s、decision 30s、Host analysis override 45s；chain 常量 analysis 28s）。Host 角色硬限小计 45+32+32+30+14+20=**173s**（≤180s，留 7s）；含数据/P1/P2 的端到端硬上界 190s，由 180s chain deadline 截断。上表第 4 行保留当时 U-06 的 140s 历史记录。R14 决策证据门禁仍为 **180s**。

**连带影响**：§2.2 规模论证、§6.3 `position_summary`、§7.2 YAML、§7.3 R03、`exposure_guard` 注释已同步为 1 仓；其余条款（保护、对账、失败矩阵）按 N≥1 设计，不受影响。

---

## M1.4 变更（2026-11-10）

- **调度**：补上需求基线 §12 的「每 10 分钟自动跑」，Host 侧新增 `plugin/host/scheduler.js`（默认开启，`WORKBENCH_AUTO_SCHEDULE=off` 关闭）。
- **时间预算**：AI 链硬截止 **180s → 250s**，R1/R2 单次超时 **32s → 40s**，输出上限 4800/5850 → **4000/4600**（130 tok/s 下 4800@32s 数学上写不完）。
- **R14 决策证据门禁**：**180s → 300s → 400s**（M1.4c 再放宽：链预算提到 340s）。理由：证据门禁必须严格大于链预算，否则跑满预算的轮次会在 P1 被判 `E_MARKET_STALE`（实测已有 196.7s 的轮次）。下单行情快照仍为 15s，由 P1/P2 用服务端实时行情把关。
- **R11 维持保证金率**：改为按**目标杠杆档位**取 `maintMarginRatio`，不再取全档最大值。原实现取 `max(ratios)`（1x 档常见 50%），会让 `1/lev − mmr` 对任何 lev≥2 恒为负 → **所有计划被 R11 拒绝**。缺失杠杆档时仍 fail-closed（`E_RISK_LIQ_DISTANCE`）。