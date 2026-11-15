# trading-workbench · 项目结构与临时项清理报告

- 日期：2026-11-13 11:15 CST（Asia/Shanghai）
- 范围：`trading-workbench/` 的代码结构梳理、临时构建/截图/缓存归档
- 约束：不触发交易、不改控制态、不重启服务、不改 `service/` 业务逻辑、不做大型重构
- 归档清单与逐项恢复：`trading-workbench/archive/2026-11-13/MANIFEST.md`

---

## 1. 代码图谱实际调用记录

| 项 | 值 |
|---|---|
| 插件 | **`dsh-plugin-codegraph` 0.6.5**（工具名 `codegraph`；DSH desktop profile 已启用） |
| 工程路径 | `C:\Users\Tao\Desktop\币安\trading-workbench` |
| 清理前索引 | 104 files / 3129 symbols / 11359 relationships |
| 清理后索引 | **107 files / 3147 symbols / 11371 relationships** |
| 语言 | python **69**、javascript **38** |
| 索引时间 | 2026-11-13T03:14:51Z（UTC） |
| 覆盖边界 | 只覆盖代码（`.js/.mjs/.py`）；**不覆盖** Markdown/HTML/PNG/日志/JSON 数据/归档 tar |
| 解析缺口 | 4960 个调用点未解析：动态模块加载、字符串路由、成员调用、跨语言 HTTP；不能仅凭“无引用”删除运行代码 |

实际调用过的操作：`status`、`files`、`context`、`search`、`node`、`callers`、`trace`、`codegraph_index`。主要结论均用 grep/测试/`package.json` 复核，不把图谱当唯一删除依据。

---

## 2. 项目结构（按入口与依赖）

```
trading-workbench/
├─ plugin/                          DSH 插件（Host + Client 同包）
│  ├─ index.js                     Host 入口：apply()
│  │   ├─ createIntegration()      → host/integration.js
│  │   ├─ integration.routes()     → /trading-workbench/api/*
│  │   ├─ createAutoScheduler()    → host/scheduler.js
│  │   └─ ctx.slots.insert(...)    → client.js（动态加载）
│  ├─ client.js                    Client 入口：window.__ModuleLoader__.load → apply → WorkbenchPanel
│  ├─ host/
│  │   ├─ integration.js           WorkbenchHostIntegration：state/routes/run/命令/审计
│  │   ├─ service-client.js        WorkbenchServiceClient：Host→Service HTTP 客户端
│  │   ├─ scheduler.js             自动轮次调度（本批不改）
│  │   ├─ review.js                AI 日报生成/缓存
│  │   ├─ round-view.js            RoundView DTO 投影
│  │   ├─ llm-config.js            逐角色模型/强度配置
│  │   └─ ai/                      chain / prompts / schemas / llm / scorecard / evidence ...
│  ├─ test/                        node:test：ai-chain / host / scheduler / audit / host-state
│  └─ client/                      checks / render-smoke / harness / screens
├─ service/                        Python FastAPI 服务（本批不改）
│  ├─ run_service.py               main() → create_app
│  ├─ workbench_service/app.py      create_app + routers + middleware
│  ├─ api/                         market / account / risk / execution / cycles / ai_audit / health
│  ├─ execution/                   broker / client / service / outbox / protection / credentials / filters
│  ├─ risk/                        models / rules / service / state / approval / config
│  ├─ market/                      service / transport / klines / evidence / quality / universe
│  ├─ ledger/ cycles/ db.py guard.py security.py
│  └─ tests*                       单元/安全/执行/周期测试
├─ scripts/                        analyze-max-tokens.py（工具，保留）
├─ docs/                           product-review / product-review-v2 / redesign（保留）
├─ data/                           运行数据、token 文件等（保留）
└─ archive/2026-11-13/             本次清理归档（可恢复）
```

### 2.1 关键依赖/调用关系

- `plugin/index.js:apply` → `createIntegration` / `createAutoScheduler` / `integration.routes()`；图谱可见。
- `WorkbenchHostIntegration` → `WorkbenchServiceClient` → `service` HTTP（跨语言，图谱不可见）。
- `plugin/client.js:apply` → 注册 slot；`WorkbenchPanel` 由动态 React 挂载（图谱只看到定义，**看不到 apply→WorkbenchPanel 的边**）。
- Client → Host 使用字符串常量：`/trading-workbench/api/state`、`/health`、`/market/prices`、`/risk/envelope`、`/positions/close`、`/protection/repair`、`/reconcile`、`/reviews/daily` 等；由 Host 路由与 render 测试核实。
- `service/run_service.py:main` → `create_app`；`create_app` 汇聚 API/执行/风控/行情路由；测试覆盖。
- `trace create_app → WorkbenchServiceClient` 无路径是**预期**：两者是进程间 HTTP 关系，不是同一语言调用图。

---

## 3. 清理结果（先归档，不删除）

归档目录：`archive/2026-11-13/`；共 **61 个归档内容 + 1 个 MANIFEST = 62 个文件**。

| # | 项 | 原位置 | 归档后 | 依据 | 恢复 |
|---|---|---|---|---|---|
| 1 | 一次性 CSS 修补脚本 3 个 | 根目录 `tmp-*.mjs` | `archive/2026-11-13/tmp-scripts/` | 不在工程内、无 import/测试引用；手工临时脚本 | 移回根目录 |
| 2 | 设计中间构建 9 个 | `docs/redesign/final/_build/` | `design-debug/final/_build/` | HTML/日志中间物；最终 `index.html` 保留 | 移回 `final/` |
| 3 | 中间截图 1 张 | `final/_preview-zh.png` | `design-debug/final/` | 过程图；最终截图/资料保留 | 移回 |
| 4 | 迭代截图 47 张 | `client/screens/b*、c1-c3*、c4-wait` | `screens-iterations/` | 迭代过程图；最终 `c4-*`、P0/P1/P2/t14/audit/completion 证据保留 | 移回 `client/screens/` |
| 5 | `__pycache__` 12 目录 / 133 `.pyc` | `service/**/__pycache__` | `pycache.tar.gz`（954 KB） | 解释器可再生成；非源码/非运行数据 | `tar -xzf ... -C .`（仓库根） |

清理后：
- `client/screens/`：**47 个文件**（保留最终 c4、p0/p1/p2/t14/audit/completion 证据）。
- `__pycache__`：**0**；运行时会按需重建。
- 根目录 `tmp-*.mjs`：**0**。
- 归档：`archive/2026-11-13/` 62 个文件（61 个归档内容 + MANIFEST，含 pycache tar）。

---

## 4. 明确保留（未清理）

| 项 | 保留理由 |
|---|---|
| `plugin/client/screens/make-p0-shots.mjs` | 图谱无入边、无 npm script 引用，但它是 P0 截图再生成工具；保留可复现证据 |
| `plugin/test/render-smoke.mjs` | 独立脱敏回归测试，不是未用中间项 |
| `docs/redesign/backup-current/client-p1.js` | 用户要求保留 Clean Console 备份；与当前 client 同名符号是预期 |
| `plugin/morning-check.log` | 日志证据 |
| `data/`、`.api-token`、`llm-config.json` | 运行数据/配置/密钥 |
| `plugin/test/**`、`service/**/tests*` | 测试 |
| `docs/product-review*`、`docs/redesign/{a,b,c,final,backup-current}` | 设计与评审资料 |
| `client/screens/{p0,p1,p2,t14,audit,completion,c4}-*.png` | 验收/产品证据 |

---

## 5. 清理后检查/测试

| 命令 | 结果 | 退出码 |
|---|---|---:|
| `node --check client.js` | 通过 | **0** |
| `node client/checks.mjs` | All 92 checks passed. | **0** |
| `node client/render-smoke.mjs` | All 217 render checks passed. | **0** |
| `npm test` | tests 140 / pass 140 / fail 0 / 72.8s | **0** |

- 未改 `service/` 源码；未启动/重启服务；未触发交易；未改控制态。
- Python `__pycache__` 清理不影响源码与测试；Node 测试不依赖它。
- 代码图谱已刷新；archive 内 3 个 `.mjs` 也被索引（107 files），属于归档后的预期，不是运行入口。

---

## 6. 尚不能确认 / 后续

1. `make-p0-shots.mjs` 后续是否仍人工使用；保留，避免“无引用即删”误伤。
2. `test/render-smoke.mjs` 未显式进 `npm test`；属于测试工具，建议后续纳入 CI。
3. 归档 `.mjs` 仍被 codegraph 索引；若要图谱完全排除 archive，需要 ignore 配置或把归档移出工程，本批未做。
4. `docs/redesign/final/spec.md` 不存在（只有 index.html）；不补写、不冒充完整设计文档。
5. 代码图谱 4960 个未解析调用点，尤其动态模块与跨语言 HTTP；本次所有清理项都经过 grep/测试/打包配置复核，未仅凭图谱删除任何运行代码。
