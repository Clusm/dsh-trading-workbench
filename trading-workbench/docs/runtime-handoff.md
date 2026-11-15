# 工作台运行时交接（Host/Client 实际接入）

- 更新：2026-11-14 00:40 CST（Host 已热重载，见 docs/runtime-status-2026-11-13.md）
- 角色：本会话只改 `plugin/` 与测试；**Service 启动/停止、DSH 进程由 Codex 统筹**
- 真实操作边界：未下单、未平仓、未改真实控制态、未运行交易轮次、未重启 DSH

---

## 1. 已核对的实际加载路径

| 项 | 实际值 |
|---|---|
| DSH profile | `C:\Users\Tao\.dsh\profiles\desktop` |
| profile 依赖 | `"@local/dsh-trading-workbench": "link:C:/Users/Tao/Desktop/币安/trading-workbench/plugin"` |
| 加载插件条目 | `include:trading-workbench`，`enabled=true`，`fiberPhase=active` |
| 源码入口 | `C:\Users\Tao\Desktop\币安\trading-workbench\plugin\index.js` |
| Host API | `http://127.0.0.1:61371/trading-workbench/api/*`（只读已实测） |
| 实际加载 build | **`twb-20261113T162755Z-2d449fc4`**（2026-11-14 00:27 热重载后），`configRevision=2` |
| 当前源码 build | `twb-20261113T162755Z-2d449fc4`（热重载时生成；源码再改会前进） |
| 结论 | **已完成热重载**：新 build 生效，probe/lease/review 回退/cycle 恢复均在真实运行 |

**路径一直正确**：profile 是 link 到源码目录。此前进程加载旧模块实例，已通过 `plugin_manager set_plugin include:trading-workbench` disable→enable 完成热重载（未重启 DSH/Service）。

---

## 2. 实际只读观察（14:01 CST）

已保存到 `docs/runtime-evidence-2026-11-13/`：

- `host-health.json`：`/health` 返回旧 build、`llm=opencode-go/deepseek-v4.1-flash`、`lastProbe.status=never`。
- `host-state.json`：`service.state=online`，`service.ready=true`，`ledger_id=6512827bcdfe4250a1bb5710217e044d`，`control_state=normal`，`new_entry_allowed=true`。
- 实际数据：rounds **100**，positions **0**，orders **10**，fills **6**；account/equity/performance 字段存在。
- `llm_config`：revision **1**，preset balanced，default/roleRoutes 均为 **qoder / dfmodel**；analysis high、decision max、risk max、execution low、review high。
- 模型目录 `/llm/catalog`：available，7 组（deepseek-official、deepseek-account、opencode-go、qoder、qoder-global、opencode-zen-free、workbuddy），包含 qoder `dfmodel`。
- 实际数据中的订单/成交来自 `external-*` 与 `wb-*`，说明 A03 回填已进入运行账本；**未对这些订单做任何操作**。
- 当前加载 Host 没有新 `/llm/probe`：实测 POST 返回 **HTTP 405**；源码已有，热重载后才能用。
- 当前加载 build 仍为 `twb-20261112T000426Z`；源码 build 为 `twb-20261113T142758Z`。

### 实际无交易模型调用

已对 **daily review** 调用真实 Host 路由（只生成复盘，不产生交易）：

```http
POST /trading-workbench/api/reviews/daily
Origin: http://127.0.0.1:61371
Content-Type: application/json

{"date":"2026-11-13"}
```

- HTTP **200**，耗时 **15.2s**
- 返回 `model=dfmodel`、`requestId=rev-b986d9e3`
- 生成文件：`data/reviews/2026-11-13.md`（1724 chars payload / 3254 bytes file）
- 结论：Harness 内部 LLM 可用；**review 角色**按配置实际使用 `qoder/dfmodel`，effort 由 reviewRoute 解析（配置 review=high）。
- 这**只验证 review 角色**，没有验证 analysis/red/blue/decision 的角色路由；后者的安全无交易验证需要新增 `/llm/probe` 并热重载。

---

## 3. 实际数据展示验证（源码 Client + 实际 Host state）

已用现有 live 模式对**实际运行的 Host `/state`** 渲染源码 Client：

```bash
TWB_LIVE_STATE_URL=http://127.0.0.1:61371/trading-workbench/api/state \
  node trading-workbench/plugin/client/render-smoke.mjs
```

结果：**All 222 render checks passed**，包括：

- `L1 live state responds ok`
- `L2 live service connected in S4`
- `L4 live positions empty state`（实际 positions=0）
- `L5 live has no stale "not exposed" copy`

证据：`docs/runtime-evidence-2026-11-13/client-live-state-render.log`（222/222，exit 0）。
注意：这验证的是**源码 Client + 实际 Host payload**；不是已登录 DSH DOM 截图。当前页面仍加载旧 build，热重载后才显示新 Client。

## 3. 实际无交易模型调用结果

- daily review 已实际调用：HTTP 200，15.2s，`model=dfmodel`，`requestId=rev-b986d9e3`。
- 生成 `data/reviews/2026-11-13.md`；这是 review 角色，非 analysis/red/blue/decision。
- 说明 Harness 内部 LLM 可用，review 角色按 `qoder/dfmodel` 配置实际执行。
- 实际状态：`last_route=null`、`/state.llm=opencode-go/deepseek-v4.1-flash`、`llm_config.default/roleRoutes=qoder/dfmodel`；这是旧 UI 误显示执行路由的根因，已在源码修复。

## 4. 本轮源码已补、待热重载生效

本轮已修改 `plugin/`：

- `plugin/host/integration.js`：新增 `LLM_PROBE_PATH = /trading-workbench/api/llm/probe`，`probeRole(role)` 使用 `llmConfig.roleRoutes[role]` + 显式 effort，调用 `streamPrompt` 的极小 prompt；不创建 cycle、不调 Service、不下单。
- `plugin/host/integration.js`：Host 执行 lease acquire/P1-P2 携带/释放/revoke_run。
- `plugin/host/service-client.js`：lease endpoints。
- 测试：`role probe uses the configured role route...`、`state falls back to local daily-review files...` 已通过；Host lease 测试通过。
- `client.js`：RouteRows 在无 `last_route` 时不再把 `/state.llm` 当前默认冒充“执行路由”，改为“尚未执行 + 已保存角色默认 + Harness 当前默认”。
- 实际 `/state` 的 `last_route=null`、`/state.llm=opencode-go`、`llm_config.roleRoutes=qoder/dfmodel`，旧 UI 会把 live 默认显示成执行路由；源码已修正，待热重载。
- Host `state()`：Service `/api/reviews` 404 时回退读取 `data/reviews/*.md`，`reviewsSource=local_files`，并保留 gap 说明；真实 review 文件因此可在真实 UI 显示。

### 热重载方法（供 Codex 执行，本会话不执行）

**首选（不重启 DSH）**：在 DSH 插件管理里对当前 profile 的 `include:trading-workbench` 做一次 disable → enable，使 Host/Client 重新 import 源码。

可用工具/动作：

```text
plugin_manager(action="set_plugin", target="include:trading-workbench", enabled=false)
plugin_manager(action="set_plugin", target="include:trading-workbench", enabled=true)
```

执行前确认：
- 不重启 DSH；当前对话页可能短暂失去工作台 Host 路由/面板，恢复后继续。
- Service 不在本操作范围内，不要顺带启动/停止 Service。
- 保留 `data/`、`.api-token`、`data/reviews/2026-11-13.md`。

**验证加载成功**：

```http
GET http://127.0.0.1:61371/trading-workbench/api/health
```

必须看到：

- `build.buildId` = 当前源码 build（热重载后重新计算，应为 `twb-20261113T...-2d449fc4` 或更新）
- `build.loaded_at` 为本次重载时间
- `version=0.1.0`

如果仍显示 `twb-20261112T000426Z-2d449fc4`，说明插件管理 toggle 没有重新 import Host；需要 DSH 插件热重载专用机制或由 Codex 决定是否重启 DSH（**不是本会话权限**）。

> HMR 仅覆盖 Client 的可能性：`dsh-hmr` 已启用，但 Host 路由不会因 Client HMR 更新；必须看 `/health.build` 判断 Host 是否重载。

---

## 5. 热重载后的真实角色探测（无交易）

先确认 `GET /llm/catalog` 可用，再逐个跑（每次一个 role，30s 超时）：

```bash
curl -sS --max-time 40 \
  -H 'Origin: http://127.0.0.1:61371' \
  -H 'Content-Type: application/json' \
  -X POST http://127.0.0.1:61371/trading-workbench/api/llm/probe \
  -d '{"role":"analysis"}'
```

重复 `analysis`、`red_opening`、`blue_opening`、`decision`、`risk`、`execution_entry`、`review`。

每个响应应包含：

- `role`
- `provider` / `model`（当前配置应为 `qoder` / `dfmodel`）
- `effort`（analysis=high、decision=max、risk=max、execution_entry=low、review=high）
- `latencyMs`、`usage`

这不会创建 cycle、不调 `/api/risk/evaluate` 或 `/execution/order`。若某个角色失败，记录错误码；不要用全局默认静默替代。

---

## 6. 实际 UI 数据验证

当前 DSH Web 页面要求 auth（`curl /` 返回 `dsh web authentication required`），本会话无法直接截取已登录 DOM；**不把“无法 headless 截图”当作数据验证失败**。

可安全验证的路径：

1. 热重载后打开实际 DSH 工作台面板。
2. 核对 `GET /state` 与面板一致：
   - `service.state=online`（不是离线横幅）
   - `orders` 10 条、`fills` 6 条（订单/成交 tab）
   - `positions` 0 条，空态不伪造仓位
   - `reviews`：当前 Service `/api/reviews` 仍 404，Host state 会显示 reviews gap；daily review 文件已生成，属于 Host 缓存
   - `llm_config.revision=1` 与系统抽屉 route facts 一致
3. 打开系统抽屉，核对资金/风险明细、操作历史真实 requestId。
4. 不要点“运行一轮”、平仓、补挂、控制态按钮。

如果面板仍显示旧布局/旧 build，先按 §3 重载；不要用 fixture 截图冒充实际 UI。

---

## 7. 最终只读快照（22:27 CST）

- `host-health-final.json`：version 0.1.0，buildId **twb-20261112T000426Z-2d449fc4**（旧），`llm=opencode-go/deepseek-v4.1-flash`，`lastProbe=never`。
- `host-state-final.json`：service **online**，rounds 100，orders 10，fills 6，reviews 0，reviewsGap `E_REVIEWS_ENDPOINT_MISSING`；build 仍是旧 build。
- 实际 daily review 文件已生成，但旧 Host 的 reviews 列表仍为空；源码 local-review fallback 尚未加载。
- 源码 build：`twb-20261113T142758Z-2d449fc4`。

本轮源码测试：

| 命令 | 结果 |
|---|---|
| `npm test` | checks 92 + render 218 + Node 147/147，exit 0 |
| Python execution tests | 172/172，exit 0 |
| `TWB_LIVE_STATE_URL=.../state node client/render-smoke.mjs` | **222/222**，exit 0（实际 payload） |

## 7. 当前阻塞/需 Codex 决定

1. **Host 未热重载**：实际 build 旧，源码新；这是实际界面接入的首要阻塞。
2. **Service reviews 端点 404**：Host `/state.gaps` 报 `reviews unavailable (E_NOT_FOUND HTTP 404)`；daily review 文件存在但列表 API 不可用。Service 由 Codex 负责，本会话不改 Service。
3. **DSH Web auth**：本会话无法用 headless 浏览器打开已登录页面；只能做 Host API + 源码测试，实际面板截图由 Codex/用户侧完成。
4. **角色探测**：`/llm/probe` 已写源码，但未热重载，当前 404；热重载后按 §4 执行。真实 review 调用已验证 `qoder/dfmodel`。
5. **模型默认不一致**：`/health.llm` 显示 Harness 全局默认 `opencode-go/deepseek-v4.1-flash`，而冻结配置/roleRoutes 是 `qoder/dfmodel`；这不是故障，但 UI 必须区分“当前 Harness 默认”和“已保存角色配置”。热重载后以 `/state.llm_config` 与 round route snapshot 为准。

---

## 8. 不得执行

- 不启动/停止/重启 workbench-service（Codex 负责）。
- 不重启 DSH。
- 不调用 `POST /run`、`POST /execution/order`、`POST /execution/close`、`POST /protection/repair`、`POST /control`。
- 不修改真实账户、订单、持仓、控制态。
- 不为通过验证伪造 `/state` 或把 fixture 截图当作 live。
