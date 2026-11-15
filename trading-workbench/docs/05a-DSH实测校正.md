# 05a · DSH 实测校正与跨文档提示

- 日期：2026-11-09（Asia/Shanghai）
- 性质：**Captain 实测补充**，不是 t5 的替代稿；t5 的 IA/视图/状态设计仍然有效
- 实测方式：`cordis_inspect_query`
  - Client `Slots.listSubTree`（无 `input`）成功，返回当前 live Slot 拓扑与注册选项
  - Client `Theme.listTokens` 成功，返回当前主题 token 全表
  - Client `Slots.listSubTree` 带 `input.root` 仍被工具桥以 `"input" must be an object` 拒绝；因此单个 slot 的精确查询改用无参全树结果定位
- 目的：关闭 t5 §18 的 U1，并修正 t5 §3.2 / §11.1 中与 live 契约不一致的注册形态与 token 名；供 t6 评审、t7 整合直接引用
- **v2 校正（2026-11-09）**：初版仅依据 `Slots.listSubTree` 的 slot 专属 schema，误判 `locale`/`inject` 不可用、`panelInfo` 不可用。复核本机 `~/.dsh/profiles/desktop/node_modules/dshmarket/client/client.js` 的真实注册代码后修正；**以 v2 为准**。slot 专属 schema 与通用注册选项是两层，检查器只列前者。

---

## 1. 已核实的 Slot 契约

### 1.1 `main`（根级 keyed）

live 拓扑：

```json
{
  "type": "slot",
  "name": "main",
  "kind": "keyed",
  "scope": "root",
  "purpose": "Central panel selected by sidebar entry id.",
  "registration": [{ "name": "key", "type": "string", "required": true }],
  "keyDomain": "open: any string the owner dispatches (no compile-time key set), already taken: conversation"
}
```

结论：

- **slot 专属**注册选项只有 `key: string`；`label`/`order` 不适用于 keyed `main`。
- 但 `locale`、`inject` 属于**通用注册选项**（不在 slot 专属 schema 里，检查器不会列出）；本机 `dshmarket` 真实运行代码确认二者可用：`register({ name:'main', key:'dsh-market', locale:NS, inject:()=>({t}) }, Component)`。
- `key = 'trading-workbench'` 可用（当前已占用只有 `conversation`）。
- 注册形态应为：

```js
const NS = 'tradingWorkbench'
ctx.slots.register({
  name: 'main', key: 'trading-workbench', locale: NS,
  inject: () => ({ t }),
}, WorkbenchPanel)
```

- t5 §3.2 的 `locale: 'tradingWorkbench'` **可以保留**；`inject` 可选。v1 若组件内直接用 `ctx.locale.bind`，也可不传 `inject`。

### 1.2 `sidebar.panellist`（根级 list）

live 拓扑：

```json
{
  "type": "slot",
  "name": "sidebar.panellist",
  "kind": "list",
  "scope": "root",
  "purpose": "Global panel icons.",
  "registration": [
    { "name": "id", "type": "string", "required": true },
    { "name": "order", "type": "number", "required": false },
    { "name": "label", "type": "string | (() => string)", "required": false }
  ]
}
```

结论（关闭 t5 U1）：

- `label` 是**可见字符串或返回字符串的函数**，不是 locale key、不是本地化对象。
- `locale` 是通用注册选项（命名空间），**有效**；`dshmarket` 真实代码用法为 `label: () => t('nav'), locale: NS`。
- 正确写法：

```js
const NS = 'tradingWorkbench'
const t = ctx.locale.bind(NS)
ctx.slots.register({
  name: 'sidebar.panellist', id: 'trading-workbench', order: 40,
  label: () => t('panel.title'), locale: NS,
}, PanelIcon)
```

- `id` 必须与 `main` 的 `key` 一致（`trading-workbench`）。

### 1.3 `sidebar.toggle.badge`（single，不是 list）

live 拓扑：

```json
{
  "type": "slot",
  "name": "sidebar.toggle.badge",
  "kind": "single",
  "scope": "root",
  "purpose": "Non-interactive notification inside the collapsed sidebar expand button.",
  "replaceRisk": "shadows-shipped-ui",
  "children": []
}
```

结论：

- 它是 **single** slot，没有 slot 专属注册选项；t5 §3.2 里的 `id` 参数**无效**（single 槽不由 id 选择）。
- `locale`/`inject` 作为通用选项仍可传：`ctx.slots.register({ name: 'sidebar.toggle.badge', locale: NS }, StatusDot)`；single slot 只能有一个占用者，需评估与宿主现有内容的冲突（`replaceRisk` 为 `shadows-shipped-ui`）。
- v1 建议**不使用**这个 slot，状态点放工作台顶栏，避免与宿主抢占；t5 已把它标为“可选降级”，可以保持不用。

### 1.4 `ctx.layout` 服务方法

live Client Service 契约只列出：

```text
selectPanel(panelId: MainPanelId | null): void
beginNavigation(): AbortSignal
toggleSidebar(): void
openRightbar(track: boolean, fullscreen: boolean): void
closeRightbar(): void
```

结论：

- `selectPanel('trading-workbench')`、`toggleSidebar()` 可用。
- `panelInfo` 不在顶层 `ctx.layout` 的 Service 方法表里，但本机 `dshmarket` 真实代码通过 `ctx.inject(['layout'], ({ layout }) => layout.panelInfo.getSnapshot().activePanelId)` 使用它，说明它是 **scoped layout 服务上的可观察状态**；t5 的 `ctx.layout.panelInfo` / `usePanelInfo()` 中，`panelInfo` 可用（需 `ctx.inject(['layout'], ...)`），`usePanelInfo()` 仍未证实，实现前复核。
- `beginNavigation()` 返回 AbortSignal，可用于取消面板切换时的在途请求；t5 未提，可作为实现优化。

### 1.5 Client 模块注入

模板（`templates/decoration/client.js`）显示 Client 半的模块对象应声明：

```js
return {
  inject: ['slots'],
  apply(ctx) { ... },
}
```

结论：

- t5 §3.1 的 `dsh.client.inject` 是**包激活顺序**，不能替代 Client 模块的 `inject: ['slots']`；实现时两者都要有。
- 若组件内使用 locale/theme 服务，再把对应服务加进模块 `inject`（如 `['slots','locale']`），不要 require Harness Client 包。

---

## 2. 已核实的 Theme token 全表（14 个）

`Theme.listTokens` 当前返回：

| token | 用途 |
|---|---|
| `--dsw-alias-bg-base` | 应用基础背景 |
| `--dsw-alias-bg-layer-1` | 主抬升面 |
| `--dsw-alias-bg-layer-2` | 次级嵌套面 |
| `--dsw-alias-bg-overlay` | 浮层/弹层背景 |
| `--dsw-alias-border-l1` | 主细边框 |
| `--dsw-alias-border-l2` | 次强边框 |
| `--dsw-alias-brand-primary` | 品牌主色 |
| `--dsw-alias-label-primary` | 主文字 |
| `--dsw-alias-label-secondary` | 次文字 |
| `--dsw-alias-state-error-primary` | 错误态 |
| `--dsw-alias-state-idle-primary` | 非活动/中性态 |
| `--dsw-alias-state-success-primary` | 成功态 |
| `--dsw-alias-state-warn-primary` | 警告态 |
| `--dsw-specific-sidebar-fill` | 宿主侧栏底色（工作台面板一般不用） |

### 2.1 对 t5 §11.1 的校正

t5 §11.1 表里大量 token 名不在 live 全表中，按以下替换：

| t5 原 token | 实测结论 | 建议替换 |
|---|---|---|
| `--dsw-alias-bg-l1` / `--dsw-alias-bg-l2` | 不存在（拼写缺 `layer-`） | `--dsw-alias-bg-layer-1` / `--dsw-alias-bg-layer-2` |
| `--dsw-elevation-panel` | 不存在 | 用 `--dsw-alias-bg-overlay` + token 派生阴影 |
| `--dsw-alias-label-primary-dimmed` | 不存在 | `--dsw-alias-label-secondary` |
| `--dsw-alias-label-tertiary` | 不存在 | `--dsw-alias-label-secondary` + opacity（如 0.72） |
| `--dsw-alias-border-l3` | 不存在 | `--dsw-alias-border-l2` |
| `--dsh-hairline-width` | 不存在 | `1px solid var(--dsw-alias-border-l1)`；不要伪造 0.5px |
| `--dsw-alias-state-business-primary` | 不存在 | `--dsw-alias-brand-primary` |
| `--dsw-alias-state-warning-primary` | 实际是 `warn` | `--dsw-alias-state-warn-primary` |
| `--dsw-alias-state-done-primary` | 不存在 | `--dsw-alias-brand-primary` 或 `--dsw-alias-state-success-primary` |
| `--dsw-focus-ring-color` / `--dsw-focus-ring-width` | 不存在 | `outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 2px` |
| `--dsw-specific-menu` / `--dsw-alias-menu-icon` / `--dsw-menu-backdrop-filter` | 不存在 | `--dsw-alias-bg-overlay` + `--dsw-alias-border-l1` |
| `--dsw-alias-toast-bg` / `--dsw-alias-toast-label` | 不存在 | `--dsw-alias-bg-overlay` + `--dsw-alias-label-primary` |
| `--dsw-alias-scrollbar-*` / `--dsh-scrollbar-*` | 不存在 | 浏览器默认；或 `scrollbar-color: var(--dsw-alias-border-l2) transparent` |
| `--dsw-font-family` / `--ds-font-family-code` / `--dsh-content-font-size` / `--dsh-code-font-size` | 不存在 | 字体 `inherit` 继承宿主；数字用系统等宽栈 `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`（字体族不是颜色，不违反 token 规则） |

补充规则：

- 抽屉阴影不要用字面黑色；用 `color-mix(in srgb, var(--dsw-alias-label-primary) 12%, transparent)` 之类由 token 派生。
- 涨跌色没有专门 token：用 `success` / `error` 语义色 + 符号 + 箭头三重编码；图表序列属于 artwork，可用自带调色板。
- `--dsw-specific-sidebar-fill` 是宿主侧栏专用，工作台主面板不要拿它当卡片底色。

---

## 3. 给 t6 的跨文档提示（待评审确认，不是结论）

以下为 captain 在拼接 t3/t5 时观察到的潜在不一致，t6 应逐条核对并给出处置：

| # | 位置 | 观察 | 建议 |
|---|---|---|---|
| C1 | t5 §3.4 / t3 §8.1 | t5 写 Host 代理“14 个只读路由 + SSE”，t3 定义 16 个端点且含 write / Bearer token；t5 自身又说 UI 会下发 run/cancel/Demo 命令 | 明确拆成“只读数据路由 + 命令路由”，全部经 Host；token 只留在 Host，不进 Client |
| C2 | t5 §15.1 / t3 §12 | 风控枚举：t5 用 `downsize/reject`，t3 用 `reduced/rejected/approve` | 统一一套枚举（建议以 t3 为准，UI 做展示映射） |
| C3 | t5 §15.1 / t3 §7 | 新鲜度示例 `maxAgeMs: 900` 与 t3 R14 的 15s/120s 不一致 | UI 示例值改为引用 t3 阈值，或注明“示例值” |
| C4 | t5 §15 / t3 §12 | 字段命名风格：UI camelCase（`approvedQty`）vs 服务 snake_case（`approved_qty`） | 明确 Host 做一层 DTO 映射，禁止两边混用 |
| C5 | t5 §6.1 / t3 §5.1 | `RoundView.nodes[].status` 与订单状态枚举是两套状态 | 明确“决策节点状态”与“订单状态”分离，UI 不混用 |
| C6 | t5 §18 U3/U4 / t3 复盘 | 复盘生成时点、手动平仓归因仍未定 | 归入最终“未决问题”，由用户拍板 |
| C7 | t3 §13 Q1/Q2/Q3/Q6/Q8 | 独立 Demo 账户、初始权益锚点（5000 vs 500）、One-way/Hedge、阈值组、端口/token 未定 | t6 汇总为一份“需用户拍板清单”，t7 在最终稿单列 |
| C8 | t3 §13 Q2 | 基线默认曾提 500 U，t3 建议 5000 U | 不要自行定；最终稿必须让用户选 |

---

## 4. 结论

- t5 的 IA、5 视图、决策链、状态矩阵、SSE/轮询策略**继续有效**。
- 必须修正：`sidebar.toggle.badge` 的 `id` 用法（single 槽无 id）、§11.1 的 token 表；`main`/`sidebar.panellist` 的 `locale` 选项**可以保留**（但要配 `label: () => t(...)`），`panelInfo` 可通过 scoped layout 服务使用。
- U1 **关闭**：`label` 是字符串/函数，不是 locale key；`locale` 是命名空间，不是 label 的替代品。
- 其余 token ⚠ 项按 §2.1 替换后，不再需要“实现前复核 token 名”；实现时只需在真实页面做一次视觉目检。

---

## 5. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-11-09 | 初版：基于 live `Slots.listSubTree` / `Theme.listTokens` 实测，关闭 U1，修正 t5 token 表与 slot 注册，列出 8 条跨文档提示 |
