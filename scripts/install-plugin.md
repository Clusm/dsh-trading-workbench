# M1.0 交易工作台插件：安装与验证

本目录是 `@local/dsh-trading-workbench` 的 workspace bundle，安装目标为：
`C:\Users\Tao\Desktop\币安\trading-workbench\plugin`

## 已经执行的安装

```text
plugin_manager(action="install_bundle",
  target="C:\\Users\\Tao\\Desktop\\币安\\trading-workbench\\plugin")
→ application: applied
→ bundle: @local/dsh-trading-workbench
→ version: 0.1.0
→ warnings: []
```

安装后 profile 中已建立 junction：
`C:\Users\Tao\.dsh\profiles\desktop\node_modules\@local\dsh-trading-workbench`
→ `C:\Users\Tao\Desktop\币安\trading-workbench\plugin`

`plugin_manager(action="list_bundles")` 已确认：
`@local/dsh-trading-workbench` 0.1.0，`enabled: true`，`installed: true`，
row `trading-workbench`（entry `include:trading-workbench`），source 为上述 link。

## 验证命令

```powershell
# 1) JavaScript 语法
node --check trading-workbench\plugin\index.js
node --check trading-workbench\plugin\client.js

# 2) Host 健康（无需 token，仅 loopback）
curl.exe -sS http://127.0.0.1:59201/trading-workbench/api/health

# 3) 无 Session 的 Harness LLM 探针（POST-only）
curl.exe -sS --max-time 40 -X POST http://127.0.0.1:59201/trading-workbench/api/probe/llm

# 4) GET 必须 405，且不得触发模型调用
curl.exe -i http://127.0.0.1:59201/trading-workbench/api/probe/llm
```

本次实测（2026-11-09 08:22 CST，remove/install 重载后）：

```json
{
  "ok": true,
  "llm": {
    "routeSelected": true,
    "provider": "opencode-go",
    "model": "deepseek-v4.1-flash",
    "lastProbe": { "status": "success", "code": null, "at": "2026-11-09T00:22:19.155Z", "latencyMs": 1830 }
  },
  "finish": { "kind": "stop", "failure": null },
  "usage": { "inputTokens": 37, "outputTokens": 14, "totalTokens": 51 },
  "text": "OK",
  "latencyMs": 1830
}
```

GET 实测：`HTTP/1.1 405 Method Not Allowed`，`allow: POST`，body
`{"ok":false,"error":{"code":"METHOD_NOT_ALLOWED","message":"Allowed: POST"}}`。
GET 后再次读取 health，`lastProbe.status` 仍为 `never`，证明 GET 未触发模型调用。
带 `Origin: https://evil.example` + `Sec-Fetch-Site: cross-site` 的 POST 实测 403。

**重启要求**：Host 侧不需要重启 Harness；`remove_bundle` + `install_bundle` 均返回
`application: applied`。浏览器端 `client.js` 更新后需刷新页面或等待 HMR；本次未取得
主面板 DOM，故不宣称主面板已目检（侧栏入口已由用户目检）。

## 探针语义与限制

- **POST-only**：GET/HEAD 返回 405 `Allow: POST`，不会进入 `ctx.llm.stream`。
- 同源校验：浏览器 POST 若带 `Origin`，必须等于 `webServer.getOrigin()`；`Sec-Fetch-Site` 若不是
  `same-origin`/`none` 则 403。CLI/curl 不带这些头，仍允许（loopback-only）。
- 探针只走 `ctx.llm.stream`，使用 request-only 用户消息
  `{ role: 'user', content: [{ type: 'text', text: 'Reply with exactly the word OK.' }] }`。
- **没有创建/读取/修改 Session**，没有 `createUserMessage`，没有 `tools`，没有外部 HTTP/API key。
- 当前默认 provider `opencode-go` 的网关要求 `x-opencode-session` 路由头；适配器从
  `GenerateOptions.sessionId` 派生该头。探针因此传入一次性的
  `twb-probe-<uuid>` 作为**不透明路由 id**（不是 Session 对象，也不写入任何 Session）。
  若换到不需要该头的 provider，可省略此字段。
- 若 Harness 未配置默认模型，探针返回 `NO_ADAPTER`；若凭据缺失，返回
  `MISSING_CREDENTIAL`；两类错误都会在面板中显示为可读文本 + 原始 JSON。
- 探针超时 25s；输出上限 64 tokens，只用于可用性验证，不代表正式 agent 调用预算。

## health.llm 语义

`GET /trading-workbench/api/health` 的 `llm` 不是“可用”布尔值：

```json
{
  "llm": {
    "routeSelected": true,
    "provider": "opencode-go",
    "model": "deepseek-v4.1-flash",
    "source": "agentDefaultModel",
    "lastProbe": {
      "status": "never | success | error",
      "code": null,
      "at": "2026-11-09T00:00:00.000Z",
      "latencyMs": null
    }
  }
}
```

- `routeSelected` 只表示 Harness 已选择默认 provider/model，**不代表凭据、adapter、网络或调用成功**。
- `lastProbe` 是最近一次 POST 探针的内存快照：`never`（插件加载后未跑）、`success`、`error` + `code`。
  它不持久化，Host/插件重启后回到 `never`。
- 面板显示“路由”和“最近探针”两张卡，避免把“选好路由”误读为“模型可用”。

## Host `inject`（F02）

`index.js` 的 Host inject 为：

```js
export const inject = ['webServer', 'llm', 'agentDefaultModel']
```

`agentDefaultModel` 是**硬依赖**：`resolveRoute()` 必须读取当前默认 provider/model 才能发探针；
不作为可选服务静默降级。缺该服务的 profile 会让插件保持 inactive，而不是让探针返回假成功。

## dsh.client.inject 说明（F04）

本插件保留：

```json
["@deepseek-ai/dsh-client-ui-layout", "@deepseek-ai/dsh-client-ui-sidebar", "@deepseek-ai/dsh-client-locale"]
```

**不添加 `@deepseek-ai/dsh-client-ui-renderer`**，理由：

- `main` / `sidebar.panellist` 两个槽由 `ui-layout` / `ui-sidebar` 声明；插件只向已声明槽注册，不实现 renderer。
- 本机 `dshmarket`（同样向宿主 UI 注册面板）的 `dsh.client.inject` 未列 renderer，仅列 locale/settings/theme，说明 renderer 不是槽注册的前置。
- 当前安装 `application=applied`、`warnings=[]`；用户已目检侧栏入口可见，说明激活链可用。
- 如果未来需要自建渲染根/替换 application root，再单独评估；M1.0 不做。

## 面板注册

Client 半在 `client.js` 中注册：

- root keyed `main`：`{ name: 'main', key: 'trading-workbench', locale: 'tradingWorkbench', inject: () => ({ t }) }`
- root list `sidebar.panellist`：
  `{ name: 'sidebar.panellist', id: 'trading-workbench', order: 40, label: () => t('panel.title'), locale: 'tradingWorkbench' }`
- `label` 是函数，返回本地化后的可见字符串；`id` 与 `main.key` 相同。
- 面板为 M1.0 骨架：DEMO 硬标签、服务状态、Host 健康、Harness 路由、最近探针、LLM 探针按钮与结果。
  不接行情/执行服务、不下单、不读密钥、不做 5 视图。

## 页面可见性验证

- **已目检**：用户刷新 Harness 页面后确认左侧侧栏出现「交易工作台」入口（order 40）。
- **未目检**：本会话 `cordis_inspect_query(client, Slots)` 连续 10s 超时，无法取得主面板 DOM/live slot 截图；
  因此本文件不宣称“主面板内容已目检”，只确认侧栏入口可见。
- 已确认的安装/注册证据：
  1. `install_bundle` 返回 `application: applied` 且 `warnings: []`；
  2. profile 中 bundle junction 存在，`list_bundles` 显示 installed/enabled 与 row `trading-workbench`；
  3. `client.js` 通过 `node --check`，并包含上述 `main` / `sidebar.panellist` 注册；
  4. Host 路由 `/health` 与 `/probe/llm` 实际可访问，POST 探针返回 `finish=stop` + usage。

## 手动安装（如果自动安装失败或需要重装）

`plugin_manager` 的安装动作需要 Full access/批准。若当前会话没有该权限：

1. 在 Harness 中打开 Settings → Plugins（或使用有 Full access 的会话）；
2. 选择 “Install bundle / 从本地目录安装”；
3. 目标填：
   `C:\Users\Tao\Desktop\币安\trading-workbench\plugin`
4. 确认安装；预期返回 `application: applied`、`warnings: []`；
5. 刷新 Web 页面；
6. 用上面的 curl 命令验证 Host 路由。

不要手工修改 profile 的 `package.json`/`cordis.patch.yml`，也不要手工在 profile 目录执行 pnpm；
`install_bundle` 会完成 link 与 bundle 选择。

### 重新安装（代码更新后）

```text
plugin_manager(action="remove_bundle", target="@local/dsh-trading-workbench")
plugin_manager(action="install_bundle", target="C:\\Users\\Tao\\Desktop\\币安\\trading-workbench\\plugin")
```

移除会短暂删除侧栏入口/主面板；重装后刷新页面。只改 `client.js` 的代码可能被页面缓存，
以移除/重装或页面刷新为准。

## 故障排查

| 现象 | 处理 |
|---|---|
| 侧栏没有“交易工作台” | 确认 bundle `application=applied`；刷新页面；检查 Settings → Plugins 是否 enabled |
| 主面板空白/控制台 slot crash | 检查 `client.js` 是否被浏览器加载；`node --check`；确认没有 import Harness Client 包 |
| `/health` 404 | 确认插件 row 的 `fiberPhase=active`；重装；检查 Host 日志 `[twb]` |
| `/probe/llm` GET 返回 405 | 这是预期；探针是 POST-only，GET 不触发模型调用 |
| `/probe/llm` 返回 `CROSS_ORIGIN_BLOCKED` | 用同源页面或 curl/loopback；不要从其它 Origin 调 POST |
| `/probe/llm` 返回 `NO_ADAPTER` | 在 Harness 选择默认模型后重试 |
| `/probe/llm` 返回 `MISSING_CREDENTIAL/AUTH` | 在 Harness 完成 provider 登录/凭据配置 |
| `/probe/llm` 返回 `INVALID_REQUEST` + `MissingSessionID` | 当前网关要求 `x-opencode-session`；本插件已传一次性不透明 id；若仍出现，检查 adapter 版本 |
| 页面 401 | 浏览器访问根路径需要 DSH Web 认证；插件路由 `/trading-workbench/api/*` 仅 loopback，不经过根认证 |

## 不包含

- 不接 `workbench-service`、不读币安 key、不下 Demo 单；
- 不调用外部 LLM API、不创建 Session、不注册 Tool、不改 system prompt；
- 不实现完整 5 视图/决策链/订单/权益。
