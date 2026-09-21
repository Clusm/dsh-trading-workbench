> # SUPERSEDED —— 请勿使用本包（2026-09-21 r3 明确标注）
>
> **r4 更新（2026-09-21）**：最新有效包是 `../9195782/`（r4 修 N6）。取代链
> `../9195782/` → `../0c131f2/` → `../ec8fd1c/` → `./`。
>
> **本包已作废，不要再应用它。**
> - 复验报告 `docs/dispatch/reviews/U08/cc9287a/Q_r3.md` §A.2 实测：本包的
>   `05_run_offline_regression_scripts.diff` 在集成树 `b97ae0d`（以及当前
>   `695023cc`）上 **4 种 git 配置全部 `git apply` 失败** —— 它期望
>   `"scripts/smoke_u06a_shadow_isolation.py",` 后面**紧跟** `)`，而现状是 U14
>   新增的 `"scripts/smoke_u14_alert_delivery.py",` 那一行。应用错包 =
>   离线回归入口直接 `RuntimeError`。
> - 本包基线 `2ed2e5d` 已过期（不含 U14 登记行）。
> - **请改用 `../0c131f2/`**（r3 包，基线 = 集成 HEAD `695023cc`）。
>   `../ec8fd1c/`（r2 包）也已同时标注 superseded。
>
> 以下内容仅作历史留档，**不代表当前可用状态**。

# U08 共享适配补丁包（作者 C-U08 / 交 I 应用）

- **补丁基线 = 本目录名所指的 `task/U08` head `c147895`**，其父链等于集成 HEAD `2ed2e5d`
  （已含 U00/U01/U02/U03/U04/U05/U06a/U13/U15；`OFFLINE REGRESSION OK scripts=81`）。
  5 个 diff 的上下文与 `index` blob 哈希都取自该提交在本 worktree 中的 pristine 共享文件
  （`task/U08` 分支只新增独占文件；5 个共享目标文件在分支侧逐字节未变）。
- `task/U08` 分支侧**不携带任何共享文件**（见下），因此没有"U08 侧的树"可作补丁基线；
  基线一律以集成 HEAD（+U08 独占产物）为准。
- 本包**由 I 在集成时应用**；`05` 与 U08 独占冒烟脚本必须同批（见 §二）。

## 一、补丁清单（5 个）

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_signal_engine_scanner_funnel.diff` | `src/signal_engine/scanner.py` | 扫描漏斗：逐 `(卡, symbol, timeframe)` 记录**停在哪道闸门、为什么**；`last_scan` 新增 `funnel` / `no_open_reason` / `no_open_verdict`；`SignalScanner.__init__` 与 `run_signal_scan` 新增可选 `funnel=` |
| `02_core_signal_ticket_skip_detail.diff` | `src/core/signal_ticket.py` | `ticket_skip_detail()` 产出细分码；`valid_signal_ticket()` 成为它的薄包装（**同一份真值表**，扫描侧/执行侧不再可能两套口径）；抽出 `VALID_TIMEFRAMES` |
| `03_agents_strategy_trader_reasons.diff` | `src/agents/strategy_trader.py` | 逐票跳过原因落漏斗 + note 文本附 `[九码/细分码]`；新增可选 `funnel=` / `ledger=`；`ledger` 在下单前消费（跨轮/重启只消费一次） |
| `04_dashboard_app_funnel_page.diff` | `src/dashboard/app.py` | `GET /funnel`（自包含页面）、`GET /api/funnel`（JSON）、`/channels` 追加 `funnel`/`attribution` 上下文；`_funnel_report` / `_position_attribution` / `_funnel_page_html` |
| `05_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **I 独占文件**：登记 `scripts/smoke_u08_strategy_funnel.py` |

## 二、批次约束（必须遵守）

1. **本包依赖 U08 独占提交已合入**：`01`–`04` 都会 `import core.strategy_funnel`。
   只应用补丁而不合入 `src/core/strategy_funnel.py` ⇒ `ModuleNotFoundError`。
2. **`05` 必须与 U08 独占冒烟脚本 `scripts/smoke_u08_strategy_funnel.py` 同批**：
   脚本一落库而 `05` 未应用，`run_offline_regression.py` 的 `_validate_smoke_manifest()`
   立即以 `RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py` exit≠0
   （本 worktree 的实测输出见交付报告）。
3. **无迁移 / 无 schema 推进**：5 个 diff 对 `schema_migrations` /
   `current_schema_version` / `range(1, N)` 零命中，因此不需要任何伴随断言补丁。
   `signal_tickets` / `positions` 表**未改**：漏斗所需的字段全部读既有列
   （`strategy_id` / `ticket_id` / `source` / `status` / `expires_at` / `raw_json`）。
   全仓搜索结论：不存在断言旧迁移版本集合的脚本或文档需要随本包修改。
4. **`04` 不依赖模板改动**：`/funnel` 页在 `app.py` 内自包含渲染；
   `channels.html` 对新增上下文键是"可选消费"（Jinja 未使用键不报错）。

## 三、`git apply` 的行尾前提（实测矩阵）

仓库 blob 是**混合行尾**（如 `src/dashboard/app.py` = 1437 CRLF / 2296 LF）。
本包 diff **保留目标 blob 的逐行行尾**（上下文行该 CR 就带 CR）。

| 场景 | 工作树 | 结果 |
|---|---|---|
| `-c core.autocrlf=false -c core.eol=lf` | 原样（混合） | **5/5 OK** |
| 默认（本机 system `core.autocrlf=true`）plain | 原样（混合） | **5/5 OK** |
| `-c core.autocrlf=true` / `input` / `false` plain | 原样（混合） | 各 **5/5 OK** |
| `-c core.autocrlf=true` + `--ignore-whitespace` | 全 CRLF（autocrlf=true 检出） | 5/5 OK |
| `-c core.autocrlf=false` | 全 CRLF | 0/5（自相矛盾组合，不支持） |

集成工作区（`multi-agent-trader/.git` 与 `mat-wt/I` 的 `core.autocrlf` 均为 `false`，
工作树为正常混合行尾检出）**plain `git apply` 5/5 通过，无需任何额外参数**。
所有验证都在**仓库根**执行（子目录会静默 no-op 造成假绿）。

## 四、共享列 / 字段的既有语义与消费者（写入前已核）

| 字段 | 既有语义（消费者） | 本包怎么用 |
|---|---|---|
| `positions.strategy_id` / `ticket_id` | U02 `strategy_position_manager` 判 `ATTRIBUTED`/`LEGACY_UNATTRIBUTED` 的唯一依据 | **只读**，用于 `manager`/`position_class`/`rule_version`；不写 |
| `positions.source` | `strategy_trader` 用 `source=="strategy"` 过滤策略仓；默认 `'ai'` | **只读**，用于"谁在管"（与归属类别是两个正交事实，同时输出） |
| `signal_tickets.status` | `expire_open_signal_tickets` 写 `expired`；`execution.py`/`binance_futures.py` 写 `filled`；`fetch_open_signal_tickets` 只认 `open` | 本包**不新增状态值**，执行器仍写 `expired`，不改写既有语义 |
| `orders.ticket_id` / `client_order_id` | `signal_ticket_has_order()` 的跨进程权威去重 | **只读**（经该函数），账本只补"消费了但订单未落库"的窗口 |
| `last_scan.*`（状态文件） | `dashboard.app._signal_scanner_status` 读 `status/symbols/tickets/market_pairs/error_count`；`channels.html` 渲染其中若干键 | **新增键**，既有键一个不改语义；`channels.html` 未使用的键不影响渲染 |
| `agent_runs.*` | U05 已修：`latency_ms` 只写真实耗时或 NULL | 本包**完全不写**该表 |

**未新增任何数据库列、未新增任何状态枚举值。**

## 五、验证（本批实跑）

```
pristine（2ed2e5d，81 脚本）            : OFFLINE REGRESSION OK scripts=81，exit 0
+ 5 补丁 + U08 独占冒烟（82 脚本）       : OFFLINE REGRESSION OK scripts=82，exit 0
失败集合差集：新增 ∅ ；修复 ∅（10 条 FAIL 行为既有其他任务脚本的自报，逐字相同）
tests/integration/test_strategy_funnel.py  两态通过：未打补丁 82/0；已打补丁 120/0
scripts/smoke_u08_strategy_funnel.py       两态通过：未打补丁 26/0；已打补丁 43/0
scripts/smoke_signal_scanner_integration.py 通过（既有行为不变）
路由级：TestClient GET /funnel 200 且含闸门+仓位两块；GET /api/funnel JSON 含两键；/channels 仍 200
scripts/smoke_ui.py 52/0；scripts/smoke_dashboard_health.py OK
scripts/check_quality.py（干净树）      : QUALITY CHECK OK files=286 untracked=0
scripts/scan_tracked_secrets.py（干净树）: SECRET SCAN OK files=494
```

## 六、U05 接线缺口：本包接了什么 / 留了什么（作者口径）

U05 补丁包 §5.1 把"运行时 API 零调用方"按归属分给 U08 与 U08/U12。本节逐条交代。

**已接（本包 + U08 独占模块，`src/` 内调用点从 0 变为 N）**：

| U05 API | 接线点 | `src/` 内调用点 |
|---|---|---|
| `classify_cycle` | `core/strategy_funnel.round_explanation()` | 0 → 1（`strategy_funnel.py:1224`） |
| `reason_from_source_errors` | `core/strategy_funnel.source_error_reason()` | 0 → 1（漏斗侧）+ 既有 2 处数据包装 |
| `classify_evidence_report` | 经 `round_explanation(evidence_report=...)` → `classify_cycle` | 直接 0；**经漏斗传递后可达** |
| `DecisionReason` / `choose_primary` / `normalise_reason_codes` / 九码常量 | 漏斗的原因码归集与细分码注册表 | 0 → 多处 |

**未接（如实登记，属 U12 的 service / pipeline 装配，超出 U08 集成目标）**：

| U05 API | 为什么不在本包 |
|---|---|
| `run_protection_first` | 保护→对账→模型的**步骤排序**在 `core/graph.py` / `core/decision_pipeline.py`；两者都不是 U08 的集成目标（U08 只负责"管理优先**预算**"这半边，已由 `ManagementPriorityPlan` + 原因码交付） |
| `graph.model_deadline()` | 需要改 `core/graph.py` 的 service 装配（U05 README 明写归 U08/U12） |
| `AgentRecorder.begin_decision_trace` / `record_decision_trace` | 需要改 `core/graph.py` / `core/agent_recorder.py` 的调用点（轨迹落审计） |
| `ModelRouteGuard` / `LeaseRegistry` / `deadline_violations` / `background_refresh` | 模型路由与跨进程准入的装配在 `llm/*` 与 `llm/request_coordinator.py`，不在 U08 目标 |
| `macro_public.reason_from_errors` / `onchain_public.reason_from_errors` | 数据侧包装仍无生产调用方；漏斗已提供入口（`source_error_reason`），串联它们需要改 `src/data/*`（非 U08 目标） |

## 七、其他已知限制（如实登记）

1. **`ScanFunnel` 实例是"一轮"的**：`run_signal_scan` 每次调用新建扫描器 ⇒ 默认
   每轮新漏斗；调用方若刻意复用同一实例（把扫描与执行合并成一轮），计数会跨轮累加，
   此时应看 `as_dict()["rounds"]`（保留最近 8 个 `round_id`）与逐轮 `no_open` 快照。
   U08 未提供 `new_round()` 重置 API——没有生产消费者，不预建。
2. **`candidates` 的口径**：进入漏斗的扫描侧条目数（含在 scope/signal_fn 等**前三道
   闸门**就被拒的卡），不等于"实际评估过的标的数"。看板按"停在哪道闸门"分开展示。
3. **轮级条目不计入候选**：`NO_TICKETS_AVAILABLE` 以 `origin="round"` 登记，只进
   `throughput`/原因计数（测试逐条断言）。
4. **`TicketLedger` 与审计库的分工**：审计库 `signal_ticket_has_order()` 是
   "已落库订单"的跨进程权威；账本只补**"消费了但订单还没落库就被重启"**的窗口。
   因此账本不是替代品，**两者都要**（缺省 `ledger=None` 时既有行为不变）。
5. **账本损坏时 fail-closed**：`LEDGER_CORRUPT` ⇒ 拒绝新消费（宁可不追单也不重复消费），
   需人工确认"审计库能兜住去重"后显式 `reset_corrupt()`（会记 `reset_at_ms` 留痕）。
6. **`manager` 与 `position_class` 是两个正交事实**（都是既有字段的读出，不是新列）：
   `source="ai"` 且无策略归属的仓位，`manager="ai"`（AI 通道在管），
   同时 `position_class="LEGACY_UNATTRIBUTED"`（U02 口径）。不用一个字段冒充另一个。
7. **`rule_known=False` 必须可见**：策略卡被改/被删后，仓位规则版本显示
   `rv1:unknown[@<全局哈希>]`，看板单列该清单，**不冒充已知版本**。
8. **未做真实跑批验证**：本包只验证到"离线真实路径（含真实扫描轮次 + 真实判定函数）"，
   没有、也不允许启动真实服务或下单；线上首次跑批的观测量（`/funnel`、
   `last_scan.funnel`）由 U12 观察交付验证。
9. **既有噪声（非本包引入）**：`scripts/smoke_u02_strategy_position_manager.py` 等
   脚本自身会打印 10 条 `FAIL` 行但 exit 0；这 10 条在 pristine 与 patched 树上
   **逐字相同**（失败集合差集 = ∅）。另有 7 个脚本硬编码迁移版本断言
   `list(range(1, 18))`：本包零迁移命中，这 7 个脚本两态逐字相同且全绿，
   故**不需要伴随补丁**（证据见交付报告）。
