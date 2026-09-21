# U08 共享适配补丁包（r2 · 作者 C-U08 / 交 I 应用）

- **补丁基线 = 集成 HEAD `e50f022`（`mat-wt/I`）**，即本轮 U08 独占产物的父链。
  5 个 diff 的上下文与 `index` blob 哈希都取自该提交的 **pristine 共享文件**
  （`git hash-object` 与 `git rev-parse e50f022:<path>` 逐文件相同，见 §四）。
- 本目录名 `ec8fd1c` 是 U08 分支上一个**只含独占文件**的提交（补丁不触碰独占文件，
  因此基线仍是 `e50f022` 的共享 blob）。
- **批次约束**：本包**必须与 U08 独占产物同批合入**，且 `05` 必须与
  `scripts/smoke_u08_strategy_funnel.py` 同批（见 §二）。
- 说明：本包由 **I 在集成时应用**；`task/U08` 分支**不携带任何共享文件**。

## 一、补丁清单（5 个）

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_signal_engine_scanner_funnel.diff` | `src/signal_engine/scanner.py` | 逐 `(卡, symbol, timeframe)` 记录**停在哪道闸门、为什么**；**轮级故障**（卡册缺失/损坏、运行时不可用/漂移、票据源读取失败、本轮无票据）经 `record_round_fault()` 落漏斗；`last_scan` 新增 `funnel` / `no_open_reason` / `no_open_verdict`；`SignalScanner.__init__` 与 `run_signal_scan` 新增可选 `funnel=` |
| `02_core_signal_ticket_skip_detail.diff` | `src/core/signal_ticket.py` | `ticket_skip_detail()` 产出细分码；`valid_signal_ticket()` 成为它的薄包装（**同一份真值表**）；抽出 `VALID_TIMEFRAMES` |
| `03_agents_strategy_trader_reasons.diff` | `src/agents/strategy_trader.py` | 逐票跳过原因落漏斗 + note 文本附 `[九码/细分码]`；新增可选 `funnel=` / `ledger=`；**删除进循环前的静默批量过滤**（改为逐票 record）；预算为 0 的循环外早退改为轮级故障 `CHANNEL_BUDGET_ZERO`；`ledger` 在下单前消费（跨轮/重启/跨进程只消费一次），建意图失败按同轮 `release()` |
| `04_dashboard_app_funnel_page.diff` | `src/dashboard/app.py` | `GET /funnel`（自包含页面）、`GET /api/funnel`（JSON）、`/channels` 追加 `funnel`/`attribution` 上下文；`_funnel_report` 用**读侧重重建 + 对账**（未登记码/摘要与计数不一致 → `available=False`、`verdict=unavailable`，不 500）；`_position_attribution` 含 `by_position_class`/`legacy_pending` |
| `05_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **I 独占文件**：登记 `scripts/smoke_u08_strategy_funnel.py` |

## 二、批次约束（必须遵守）

1. **先合入 U08 独占产物，再应用 `01`–`04`**：四者都会 `import core.strategy_funnel`
   （`strategy_funnel` 又 `import core.strategy_position_manager`）。只应用补丁而不合入
   `src/core/strategy_funnel.py` ⇒ `ModuleNotFoundError`。
2. **`05` 必须与 `scripts/smoke_u08_strategy_funnel.py` 同批**（I 的原子登记项）：
   脚本落库而 `05` 未应用，`_validate_smoke_manifest()` 立即以
   `RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py` 失败。
   实测（本 worktree `task/U08`，共享文件未打补丁）：
   ```
   RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py
   ```
   应插入的登记行（**必须与冒烟脚本同批**）：
   ```python
       "scripts/smoke_u06a_shadow_isolation.py",
       # U08 策略漏斗（必须与 U08 独占冒烟脚本同批落地）
       "scripts/smoke_u08_strategy_funnel.py",
   ```
3. **无迁移 / 无 schema 推进**：5 个 diff 对 `schema_migrations` /
   `current_schema_version` / `range(1, N)` 零命中。`signal_tickets` / `positions`
   表**未改**：漏斗所需字段全部读既有列（`strategy_id` / `ticket_id` / `source` /
   `status` / `expires_at` / `raw_json`）。
4. **`04` 不依赖模板改动**：`/funnel` 页在 `app.py` 内自包含渲染；`channels.html`
   对新增上下文键是"可选消费"。
5. **`01` 改变了 `_load_strategy_lab()` 失败时的行为顺序**：仍在同一处抛
   （整轮中断语义不变），但**先**记录轮级故障并落一次状态文件，再抛出。

## 三、`git apply` 验证矩阵（都在**仓库根**执行）

| 配置 | 结果 |
|---|---|
| `mat-wt/I` 仓库默认（`core.autocrlf=false`）plain | **5/5 OK** |
| `-c core.autocrlf=false -c core.eol=lf` | **5/5 OK** |
| `-c core.autocrlf=true` | **5/5 OK** |
| `-c core.autocrlf=true --ignore-whitespace` | **5/5 OK** |

命令（仓库根，逐补丁）：
```
git -C "C:/Users/Tao/Desktop/币安/mat-wt/I" apply --check <diff>
git -C "C:/Users/Tao/Desktop/币安/mat-wt/I" -c core.autocrlf=true apply --check <diff>
```
- 本包 diff **保留目标 blob 的逐行行尾**（仓库 blob 是混合行尾，如
  `src/dashboard/app.py` = 1437 CRLF + 859 LF）。生成 diff 时用"逻辑行最小映射"
  写回，只让新增/改动行采用邻近行尾；**行尾改写量 = 0 行**（`git diff --numstat`
  的插入/删除数即为真实改动量，无整文件 churn）。

## 四、交付物与验证（本轮实测）

- `git diff --numstat`（补丁体量）：scanner 198/10、signal_ticket 43/10、
  strategy_trader 149/14、dashboard 216/0、run_offline_regression 3/0。
- 共享文件 pristine 校验：`git hash-object` 与 `e50f022:<path>` 逐文件 SAME。
- 沙箱（= `e50f022` 树 + U08 独占产物 + 本包）实测：
  - `tests/integration/test_strategy_funnel.py` → exit 0（**两态**：未打补丁
    **PASS 127 / FAIL 0**；已打补丁 **PASS 177 / FAIL 0**）
  - `scripts/smoke_u08_strategy_funnel.py` → exit 0（两态：未打补丁 **51/0**；
    已打补丁 **68/0**）
  - `scripts/smoke_contracts_v3.py` → **PASS 173 / FAIL 0**
  - `scripts/check_quality.py` → **QUALITY CHECK OK files=289 untracked=3**
  - `scripts/run_offline_regression.py` → **OFFLINE REGRESSION OK scripts=83**
  - 全量 delta（pristine `e50f022` 树 vs 打过补丁的树，83 脚本逐条非 fail-fast）：
    `base_fail=0 / head_fail=0 / NEW_FAIL=[] / FIXED=[] / BOTH_FAIL=[]`；
    `ABSENT_IN_BASE=['scripts/smoke_u08_strategy_funnel.py']`（U08 新增脚本）。

## 五、已知边界

- `01` 的轮级故障依靠**注入漏斗**才会写进内存对象；未注入时（如 `graph.py` 的
  调用点）仍会落**状态文件**的 `last_scan.funnel`，但调用方拿不到同一个实例。
  `graph.py` 属 U12 接线范围，本包不改。
- `PROVIDER_RATE_LIMITED` 细分码仍**无生产者**（等待 U05 `classify_exception` 的
  调用点接入执行侧异常路径）。
- `_load_state()` 损坏时静默清零去重记忆（`except: pass`）是既有行为，本包未改；
  已作为已知窗口登记给 U12。
