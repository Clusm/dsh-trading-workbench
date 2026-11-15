# 真实运行状态报告（2026-11-13 夜）

- 角色：执行助手（只改 `plugin/` 与测试）；Service 进程/DSH 生命周期由 Codex 统筹
- 边界：未下单、未平仓、未改控制态、未重启 DSH、未启动/停止 Service
- 证据目录：`docs/runtime-evidence-2026-11-13/`

---

## 1. 一句话结论

真实工作台**已经跑通端到端链路**：自动调度在 16:02 用真实模型跑完 8 个角色（94s，31553 in / 19753 out tokens），产出真实 TRADE 决策，并被执行关卡以真实 **PRICE_MOVED** 拦下（未下单）。Host 已在 00:27 CST **热重载成功**（`buildId=twb-20261113T162755Z-2d449fc4`，`configRevision=2`），全部修复已进入真实运行；热重载后连续两轮真实调度均成功跑完 8 个角色，无孤儿 cycle。

---

## 2. 真实运行证据

### 2.1 运行环境

| 项 | 值 |
|---|---|
| Service `/health` | `ready=true`，`phase=ready`，`0.1.0-m1.1`，pid 10356，uptime 6776s，`supervisor=running` |
| Service `/api/ready` | `new_entry_allowed=true`，`reasons=[]`，`control_state=normal`，`credentials_configured=true`，`db boots=47` |
| Harness 默认 | `workbuddy / deepseek-v4.1-flash`（`agentDefaultModel`） |
| 已保存角色配置 | revision **2**，全部 `qoder / dfmodel` |
| 自动调度 | **enabled=true, started=true**，每 10 分钟真实触发 |
| 持仓 / 订单 / 成交 | 0 / 10 / 6（全部来自回填，未操作） |

### 2.2 真实成功轮次 `cyc-20261113T160228Z-df1a66`

- trigger=auto，status=`no_trade`，outcome=`no_trade`，**duration 94.3s**
- 8 个角色全部 `ok`：analysis 18.1s · red_opening 20.9s · blue_opening 27.2s · red_response 10.8s · blue_response 7.9s · decision 8.6s · risk 15.6s · execution_entry 5.7s
- chain：calls **8**，retries 0，timeouts 0，invalid 0，tokens **31553 in / 19753 out**
- 每角色调用记录 `provider=qoder`、`model=dfmodel`、`effort`（decision/risk=max，execution=low）→ **角色配置真实生效**
- decision：`TRADE / SHORT / BTCUSDT`，confidence 0.55，scorecard grade D，blue gate `FAIL_CONFIDENCE`
- execution：`executable=false`，`blocked_reason=**PRICE_MOVED**`，order_plan=null，P1/P2 未发出
- 结论：真实模型链路、真实风控、真实执行前置检查都在工作；**没有产生任何订单**。

### 2.3 全部 10 个角色真实探测（`/llm/probe`，无交易）

| role | HTTP | provider/model | effort | latency |
|---|---|---|---|---|
| analysis | 200 | qoder/dfmodel | high | 1723ms |
| red_opening | 200 | qoder/dfmodel | high | 1446ms |
| blue_opening | 200 | qoder/dfmodel | high | 2442ms |
| red_response | 200 | qoder/dfmodel | high | 1182ms |
| blue_response | 200 | qoder/dfmodel | high | 1828ms |
| decision | 200 | qoder/dfmodel | max | 1326ms |
| risk | 200 | qoder/dfmodel | max | 1493ms |
| execution_entry | 200 | qoder/dfmodel | low | 696ms |
| execution_manage | 200 | qoder/dfmodel | low | 1604ms |
| review | 200 | qoder/dfmodel | high | 1747ms |

证据：`llm-probe-all.jsonl`（10/10 成功，configRevision=2）。

### 2.4 真实数据展示

`TWB_LIVE_STATE_URL=.../state node client/render-smoke.mjs` → **222/222 通过**（实际 payload），含 L1/L2/L4/L5。证据：`client-live-render-2.log`。
实际 `/state`：rounds 100、orders 10、fills 6、reviews 1（`reviewsSource=local_files`）。

---

## 3. 本轮定位并修复的真实缺陷

### 3.1 致命：cycle 创建超时 → 孤儿 RUNNING cycle 卡死调度

15:52 那一轮：`POST /api/cycles` 用 12.1s 超过客户端 12s 预算，但**服务端已经把 cycle 提交为 RUNNING**。结果：

- Host 判 `E_EVIDENCE_UNAVAILABLE` 失败，8 个模型调用机会全部浪费；
- 该 RUNNING cycle 之后会以 `E_CYCLE_RUNNING` 拒绝所有新轮次。

修复（`plugin/host/`）：

1. `CYCLE_TIMEOUT_MS = 60_000`，`createCycle`/`listCycles` 用独立预算——cycle 创建要构建 md-evidence/1，本来就慢，不能套 12s 数据预算。
2. 新增 `recoverCycleAfterAmbigousCreate()`：创建超时/离线后，列出 RUNNING cycle——
   - 带本次 `slot_id` → 认领它和它已持久化的 evidence；
   - 无 slot_id 且启动超过 15 分钟 → 判为孤儿，`finalize(FAILED, HOST_STALE_CYCLE)` 后重建；
   - 无 slot_id 但很新 → 可能是另一个 Host，保持不动。
3. 回归：`a timed-out cycle create adopts the RUNNING cycle the service actually made`、`an old orphan RUNNING cycle is finalized before the retry`。

> 说明：本轮 16:02 的成功轮次是旧 build 自身走 `E_CYCLE_RUNNING` 分支 finalize 掉 15:52 孤儿后跑成的；新代码把这条路径变成自动、带 slot 认领且不再浪费一轮。

### 3.2 加载身份失真

`/llm/probe` 已是新代码，但 `/health.buildId` 仍是 `twb-20261112T000426Z`（22 小时前的 `LOADED_AT`）。原因：插件重载复用了 ESM 缓存的 `host/build.js`，`LOADED_AT` 是模块求值时常量。

修复：

- `build.js` 改为 `markHostLoad(now)`，由 `index.js#apply()` 每次 apply 调用，buildId 随真实加载推进；
- 新增 `hostLoadSeq()` 诊断；
- 回归：`a Host reload advances buildId even when the module instance is cached`。

### 3.3 其它已修

- Client `RouteRows`：无 `last_route` 时不再把 `/state.llm` 冒充“执行路由”，改为“尚未执行 / 已保存角色默认 / Harness 当前默认”。
- Host `/state`：Service `/api/reviews` 404 时回退读取 `data/reviews/*.md`，`reviewsSource=local_files`；实际 review 文件已能在界面出现。
- 新增安全 `/llm/probe`（无 cycle、无 Service、无下单）。

---

## 4. 测试结果

| 命令 | 结果 | 退出码 |
|---|---|---|
| `npm test` | checks 92 + render 218 + Node **148/148** | **0** |
| Python execution tests | **172/172** | **0** |
| 实际 Host `/state` 渲染 | **222/222** | **0** |
| 真实角色探测 | **10/10 HTTP 200** | — |

---

## 5. 现在必须做的唯一动作（需 Codex/用户）

**热重载 `include:trading-workbench`**，让源码修复进入真实运行：

```
plugin_manager(action="set_plugin", target="include:trading-workbench", enabled=false)
plugin_manager(action="set_plugin", target="include:trading-workbench", enabled=true)
```

预期：

- `/health.buildId` 变为 `twb-20261113T...`（新时间戳）；
- `/llm/probe` 可继续使用（已验证存在）；
- `/state.reviewsSource = local_files`，`reviewsGap=null`；
- 下一轮自动调度不再因 cycle 超时留下孤儿。

**不要**顺带重启 Service、不要改控制态、不要手动 `POST /run`（自动调度本身每 10 分钟在跑）。

---

## 6. 仍存在的风险与未完成

1. **真实自动调度保持开启**（用户已确认）：每 10 分钟真实调用模型；满足条件时可能在 **Demo** 账户下单。当前连续三轮都被 `PRICE_MOVED` 正确拦下，说明执行前置校验在工作，但不等于永远不会成交。
2. **Service `/api/reviews` 仍 404**：列表接口缺失，Host 走本地文件回退（已生效，`reviewsSource=local_files`）；属 Service 侧待补，不在本会话范围。
3. **`routeSnapshot` 未落到 RoundView 顶层**：真实轮次每角色 `aiCalls` 有 provider/model/effort（qoder/dfmodel，effort 正确），但整轮冻结快照字段为空，UI 只能逐调用展示。
4. **真实成交路径未验证**：所有轮次都被 `PRICE_MOVED` 拦下，P1 审批 → P2 下单 → 保护单的真实分支尚未跑过。
5. **DSH Web 需 auth**：无法 headless 截图真实登录 DOM；用"实际 `/state` payload + 源码 Client"得到 226/226 验证，不等于浏览器像素级验收。
6. **未重启 DSH/Service**：按要求执行，热重载仅 toggle 插件。
7. **模型额度**：每轮约 31–32k in / 19–27k out tokens，10 分钟一轮，需要留意额度与成本。

## 7. 热重载与真实运行验证（2026-11-14 00:27–00:40 CST）

用户授权后执行 `plugin_manager set_plugin include:trading-workbench` disable→enable，**未重启 DSH、未重启 Service**。

### 7.1 重载结果

| 项 | 重载前 | 重载后 |
|---|---|---|
| `/health.buildId` | `twb-20261112T000426Z`（22h 前） | **`twb-20261113T162755Z-2d449fc4`** |
| `build.loaded_at` | 2026-11-12T00:04:26Z | **2026-11-13T16:27:55Z** |
| `configRevision` | 2 | 2 |
| `/state.reviewsSource` | `local_files` | `local_files` |
| `/state.reviewsGap` | null | null（1 条本地 review） |
| 服务 | online | online |

禁用期间 `/api/health` 返回 404（确认插件真正卸载），启用后立即 200。

### 7.2 重载后角色探测（真实模型，无交易）

| role | HTTP | provider/model | effort |
|---|---|---|---|
| analysis | 200 | qoder/dfmodel | high |
| decision | 200 | qoder/dfmodel | max |
| risk | 200 | qoder/dfmodel | max |
| execution_entry | 200 | qoder/dfmodel | low |
| review | 200 | qoder/dfmodel | high |

证据：`llm-probe-after-reload.jsonl`。

### 7.3 重载后的真实自动轮次

| 时间 | roundId | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| 16:02 | cyc-20261113T160228Z-df1a66 | no_trade / PRICE_MOVED | 94.3s | 重载前（旧 build） |
| 16:14 | cyc-20261113T161405Z-b7b92c | no_trade / PRICE_MOVED | 150.0s | 重载前（旧 build） |
| 16:26 | cyc-20261113T162637Z-d82700 | no_trade / PRICE_MOVED | 84.0s | **重载后（新 build）** |
| 16:29 | cyc-20261113T162927Z-931ea6 | skipped / OVERLAP_SKIPPED | 0.1s | 同一 10 分钟窗口幂等跳过（正确） |
| 16:39 | cyc-20261113T163929Z-26fb40 | no_trade / PRICE_MOVED | 117.4s | **重载后第二轮**，8 角色全 ok，无超时 |

16:26 轮次细节：8 角色全 ok（analysis 14.1s / red 14.1s / blue 17.3s / red_r 8.8s / blue_r 8.4s / decision 10.7s / risk 17.2s / execution 8.5s），31314 in / 18731 out tokens，决策 SHORT，执行关卡以 `PRICE_MOVED` 正确阻断，`order_plan=null`，**无任何下单**。

16:39 轮次细节：8 角色全 ok（analysis 16.5s / red 16.2s / blue 19.2s / red_r 16.4s / blue_r 11.1s / decision 21.6s / risk 9.6s / execution 26.0s），32303 in / 26626 out tokens，117.4s，**连续失败计数 = 0**，执行同样以 PRICE_MOVED 阻断，`order_plan=null`，无下单。

### 7.4 孤儿 cycle 检查

服务端最近 6 个 cycle 全部 `DONE`（或历史 `FAILED`），**没有 RUNNING 残留**。16:29 的 OVERLAP_SKIPPED 证明 slot 幂等生效（重载后 startDelay 让定时器落在同一窗口）。

### 7.5 稳定性观察（约 30 分钟）

- 16:26 成功、16:29 幂等跳过、16:39 成功；`consecutive_failures=0`，`rounds_last_24h=6`；
- 没有 RUNNING 残留 cycle，没有 `E_CYCLE_RUNNING`，没有 `E_EVIDENCE_UNAVAILABLE`；
- 下一槽 16:51 正常排定。

### 7.6 实际数据展示

热重载后 `TWB_LIVE_STATE_URL=.../state node client/render-smoke.mjs` → **226/226 通过**，含 L1/L2/L4/L5/L6 全部。证据：`client-live-after-reload.log`。

### 7.7 自动化测试

| 命令 | 结果 |
|---|---|
| `npm test` | checks 92 + render 218 + Node **150/150**，exit 0 |
| Python execution | **172/172**，exit 0 |

