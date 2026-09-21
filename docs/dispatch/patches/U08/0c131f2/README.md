# U08 共享适配补丁包（r3 · 作者 C-U08 / 交 I 应用）

- **补丁基线 = 集成 HEAD `32ecf635`（`mat-wt/I`）** <sub>（r3 交付时点的集成 HEAD；
  由 `695023cc` 前进而来 —— 中间落地的 U10a 只改了本包的 `05` 目标文件
  `scripts/run_offline_regression.py`，其余 4 个目标 blob 未变，故 01–04 逐字节不变）</sub>。
  5 个 diff 的上下文与 `index` blob 哈希都取自该提交的 **pristine 共享文件**
  （`git hash-object` 与 `git rev-parse 32ecf635:<path>` 逐文件 SAME，见 §四）。
- **本目录名 `0c131f2` = 本轮（r3）U08 独占产物的源提交**，即 task/U08 上
  "只触碰独占文件"的那个 tip 提交。**它不是"创建本目录的提交"** —— 目录命名
  一律跟"被投递内容的源提交"，不要跟"创建目录的提交"（B-r3 D1 澄清）。
- **取代关系**：本包取代 `../ec8fd1c/`（r2），`../ec8fd1c/` 取代 `../c147895/`
  （r1）。旧两包已在各自 README 顶部标注 **SUPERSEDED**，`c147895/` 另有
  `SUPERSEDED.md`。**三包并存时请只用本包。**
- **批次约束**：本包**必须与 U08 独占产物同批合入**，且 `05` 必须与
  `scripts/smoke_u08_strategy_funnel.py` 同批（见 §二）。
- 说明：本包由 **I 在集成时应用**；`task/U08` 分支**不携带任何共享文件**。
- **基线不变性**：`695023cc` / `32ecf635` 与 r2 基线 `e50f022` 在**前 4 个**共享
  目标文件上 blob 逐文件相同；只有 `scripts/run_offline_regression.py` 变过
  （`e50f022`/`b97ae0d` = `ed0fe17` → U14 登记行 → `695023cc` = `ed0fe17` →
  U10a 后 `32ecf635` = `2113d65`）。因此 01–04 的上下文与 r2 一致，
  **本包相对 r2 的变更只在补丁内容**（r3 新增 N1/N3/N4 修复）与 `05` 的上下文。

## 一、补丁清单（5 个）

| 补丁 | 目标文件 | 用途 |
|---|---|---|
| `01_signal_engine_scanner_funnel.diff` | `src/signal_engine/scanner.py` | 逐 `(卡, symbol, timeframe)` 记录**停在哪道闸门、为什么**；**轮级故障**（卡册缺失/损坏、运行时不可用/漂移、票据源读取失败、本轮无票据）经 `record_round_fault()` 落漏斗；`last_scan` 新增 `funnel` / `no_open_reason` / `no_open_verdict`；`SignalScanner.__init__` 与 `run_signal_scan` 新增可选 `funnel=`；**r3 新增**：先读卡册再校验运行时（N1） |
| `02_core_signal_ticket_skip_detail.diff` | `src/core/signal_ticket.py` | `ticket_skip_detail()` 产出细分码；`valid_signal_ticket()` 成为它的薄包装（**同一份真值表**）；抽出 `VALID_TIMEFRAMES` |
| `03_agents_strategy_trader_reasons.diff` | `src/agents/strategy_trader.py` | 逐票跳过原因落漏斗 + note 文本附 `[九码/细分码]`；新增可选 `funnel=` / `ledger=`；**删除进循环前的静默批量过滤**；预算为 0 的早退改记轮级故障 `CHANNEL_BUDGET_ZERO`；`ledger` 在下单前消费、建意图失败按同轮 `release()`；**r3 新增**：收尾 `save()` 的 `LedgerLockTimeout` 局部化（N3）、未注入漏斗时同轮 `release()` 仍有效（N4） |
| `04_dashboard_app_funnel_page.diff` | `src/dashboard/app.py` | `GET /funnel`（自包含页面）、`GET /api/funnel`（JSON）、`/channels` 追加 `funnel`/`attribution` 上下文；`_funnel_report` 用**读侧重重建 + 对账**（未登记码/摘要与计数不一致 → `available=False`、`verdict=unavailable`，不 500） |
| `05_run_offline_regression_scripts.diff` | `scripts/run_offline_regression.py` | **I 独占文件**：登记 `scripts/smoke_u08_strategy_funnel.py` |

## 二、批次约束（必须遵守）

1. **先合入 U08 独占产物，再应用 `01`–`04`**：四者都会 `import core.strategy_funnel`
   （`strategy_funnel` 又 `import core.strategy_position_manager`）。只应用补丁而不
   合入 `src/core/strategy_funnel.py` ⇒ `ModuleNotFoundError`。
2. **`05` 必须与 `scripts/smoke_u08_strategy_funnel.py` 同批**：
   脚本落库而 `05` 未应用，`_validate_smoke_manifest()` 立即以
   `RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py` 失败。
   `05` 实际插入的位置（紧接 U06a 那一行，**后面还有 U14/U10a 的登记行**）：
   ```python
       "scripts/smoke_u06a_shadow_isolation.py",
       # U08 策略漏斗（必须与 U08 独占冒烟脚本同批落地）
       "scripts/smoke_u08_strategy_funnel.py",
       # U14（E 运维）：...
       "scripts/smoke_u14_alert_delivery.py",
   ```
   （`git apply` 自己按上下文定位；**不要**手工找"紧跟 `)`"的位置 ——
   r1 包 `c147895/05` 正是因为把上下文写成"紧跟 `)`"而在 U14 落地后 4 配置全失败。）
3. **无迁移 / 无 schema 推进**：5 个 diff 对 `schema_migrations` /
   `current_schema_version` / `range(1, N)` 零命中。`signal_tickets` / `positions`
   表**未改**：漏斗所需字段全部读既有列（`strategy_id` / `ticket_id` / `source` /
   `status` / `expires_at` / `raw_json`）。
4. **`04` 不依赖模板改动**：`/funnel` 页在 `app.py` 内自包含渲染；`channels.html`
   对新增上下文键是"可选消费"。
5. **`01` 里 `_load_strategy_lab()` 的失败语义不变**：注册表缺失/漂移**仍抛
   `RuntimeError`（整轮中断）**，只是改为**先**记录轮级故障并落一次状态文件再抛；
   新增的可选参数 `cards=None` 让 `scan()` 复用已读过的卡册（既有调用方
   `scanner._load_strategy_lab()` 不传参数，行为不变）。
6. **`01` 有一处**有意**的行为变化（B-r3 N1）**：卡册 JSON **损坏**时，
   `scan()` **不再**抛 `JSONDecodeError`（旧行为 = 整轮中断 + 被标成运行时漂移），
   而是记 `CARDS_INVALID` 后**以空卡册继续本轮**（与"卡册文件不存在"的既有行为
   一致：仍会处理审计库里的历史票据），verdict = `blocked`/`VALIDATION_REJECTED`。
   **注册表缺失/漂移仍然整轮中断**（见上条），两者可区分。
   —— 若集成方希望"卡册损坏也整轮中断"，请在 U12 的整轮聚合器里按
   `round_faults` 决定，而不是在扫描器里重新抛（那会重新丢掉 `CARDS_INVALID`）。

## 三、`git apply` 验证矩阵（r3 实测，都在**仓库根** `mat-wt/I` 执行）

| 配置 | 结果 |
|---|---|
| 仓库默认（`core.autocrlf=false`）plain | **5/5 OK** |
| `-c core.autocrlf=false -c core.eol=lf` | **5/5 OK** |
| `-c core.autocrlf=true` | **5/5 OK** |
| 真应用（独立沙箱树 = `32ecf635` + `git init` 后 `git apply`） | **5/5 APPLIED**，且重放结果与已验证树**逐字节相同** |

- 命令（仓库根 = `C:/Users/Tao/Desktop/币安/mat-wt/I`，逐补丁）：
  ```
  git -C "<I>" apply --check <diff>
  git -C "<I>" -c core.autocrlf=false -c core.eol=lf apply --check <diff>
  git -C "<I>" -c core.autocrlf=true apply --check <diff>
  ```
- 真应用后的 `git diff --stat`（沙箱，核对无整文件 churn）：
  ```
   scripts/run_offline_regression.py |   3 +
   src/agents/strategy_trader.py     | 187 ++++++++++++++++++++++++++++---
   src/core/signal_ticket.py         |  53 +++++++--
   src/dashboard/app.py              | 216 +++++++++++++++++++++++++++++++++++
   src/signal_engine/scanner.py      | 230 ++++++++++++++++++++++++++++++++++++--
   5 files changed, 653 insertions(+), 36 deletions(-)
  ```
- 本包 diff **保留目标 blob 的逐行行尾**（仓库 blob 是混合行尾，如
  `src/dashboard/app.py` = 1437 CRLF + 859 LF）。只让新增/改动行采用邻近行尾，
  行尾改写量 = 0 行。

## 四、交付物与验证（r3 实测）

- `git diff --numstat`（补丁体量，r3）：scanner **218/12**、signal_ticket 43/10、
  strategy_trader **173/14**、dashboard 216/0、run_offline_regression 3/0。
  （r2 为 scanner 198/10、strategy_trader 149/14；增量即 N1/N3/N4。）
- 共享文件 pristine 校验：`git hash-object` 与 `32ecf635:<path>` 逐文件 SAME。
- 沙箱（= 集成 HEAD `32ecf635` 树 + U08 独占产物 + 本包）实测：
  - `tests/integration/test_strategy_funnel.py` → exit 0（**两态**：未打补丁
    **PASS 127 / FAIL 0**；已打补丁 **PASS 187 / FAIL 0**）
  - `scripts/smoke_u08_strategy_funnel.py` → exit 0（两态：未打补丁 **53/0**；
    已打补丁 **76/0**）
  - `scripts/smoke_contracts_v3.py` → **PASS 173 / FAIL 0**
  - `scripts/check_quality.py` → **QUALITY CHECK OK files=290 untracked=3**
  - `scripts/scan_tracked_secrets.py` → **SECRET SCAN OK files=525**
  - `SMOKE MANIFEST OK included=80 excluded=1`（`SCRIPTS` 总数 = **84**）
  - 全量 delta（pristine `32ecf635` 树 vs 打过补丁的树，逐条非 fail-fast，
    同一 SCRIPTS 清单并集、同一 env）：
    ```
    base_scripts=83  head_scripts=84  union=84
    added(head-base)=['scripts/smoke_u08_strategy_funnel.py']   removed=[]
    base_fail_n=0 []   head_fail_n=0 []   NEW_FAIL=[]  FIXED=[]  BOTH_FAIL=[]
    非零 rc：base=[] head=[]
    harness 入口：base rc=0 (OFFLINE REGRESSION OK scripts=83)
                  head rc=0 (OFFLINE REGRESSION OK scripts=84)
    ```

## 五、已知边界（**生产可达性如实重列**，含 B-r3 N2 修正）

**先给结论**：本包把模块修好、把**扫描侧**接上；**执行侧的可达性依赖调用方注入
`funnel=`/`ledger=`，而生产调用点 `src/core/graph.py` 两者都没传**（`grep funnel`/
`grep ledger` 在 `graph.py` 里 **0 命中**）。因此下表区分"模块内可达"与
"生产可达"。**上一版把 ledger 记成"可达"是过乐观的，本版已改正。**

| 入口 | 模块内 | **生产可达性（r3 事实）** |
|---|---|---|
| `ScanFunnel.record` / `record_round_fault` | 可达 | **扫描侧可达**：`scanner.scan()` **自建**漏斗（`self.funnel or ScanFunnel(...)`）并写入状态文件 `last_scan.funnel`，所以生产上**能读**；但调用方拿不到**同一个实例**。执行侧要注入 `funnel=` 才可达 —— `graph.py:344` **未注入**，故**执行侧逐票跳过原因在生产上不可见**（U12） |
| `ticket_skip_detail()` | 可达 | **可达**：`run_strategy_trader` 在逐票循环里**无条件**调用（与 `funnel=` 是否注入无关）；扫描侧另有 `TICKET_EXPIRING_BEFORE_USE` |
| `TicketLedger.consume/recover/release/orphan_consumed` | 可达 | **生产不可达（待 U12）**：唯一接线点是 `strategy_trader.run_strategy_trader(ledger=...)`，而 `graph.py:344` **未传 `ledger=`** ⇒ 跨进程"只消费一次"**在生产上未生效**，去重仍只靠审计库 `signal_ticket_has_order`。**不写"可达"。** |
| `classify_cycle` / `reason_from_source_errors` / `round_explanation` | 模块内有入口 | **生产可达性仍为 0**：只被本任务测试调用；真正的调用点是整轮聚合器（`core/graph.py` 的 cycle），**归 U12** |
| `run_protection_first` / `graph.model_deadline()` / `AgentRecorder` / `ModelRouteGuard` / `LeaseRegistry` / `background_refresh` / `macro·onchain.reason_from_errors` | — | 仍留 U12（不变） |

**为什么不在本包顺手接线 `graph.py`**：`graph.py` 不在 U08 的独占路径内，且
"一轮里账本实例的生命周期/路径/预算"是整轮聚合器的设计决定（§五已把 cycle 的
其余接线一并归 U12）。按 B-r3 N2 给的二选一，本轮选 **(b) 如实修正可达性表**，
不用一个跨界补丁把 U12 的边界搅浑。**执行侧逐票原因与跨进程去重的"生产生效"
是 U12 的验收项**，此项**不算本包已完成**。

- `01` 的轮级故障依靠**注入漏斗**才会写进内存对象；未注入时（如 `graph.py` 的
  调用点）仍会落**状态文件**的 `last_scan.funnel`，但调用方拿不到同一个实例。
- `PROVIDER_RATE_LIMITED` 细分码仍**无生产者**（等待 U05 `classify_exception` 的
  调用点接入执行侧异常路径）。
- `_load_state()` 损坏时静默清零去重记忆（`except: pass`）是既有行为，本包未改；
  已作为已知窗口登记（**直接冲击必达②"跨轮不重复发射"**，漏斗侧账本只管"消费"）。
- `record_round_fault(CHANNEL_BUDGET_ZERO)` 的性质是 `scope`：通道被配置成只扫描
  不下单时 verdict 是 `no_candidates` + 主因 `READINESS_BLOCKED`（**不是**
  `blocked`），这是"配置态"而非"故障"的有意选择。
- **`LEDGER_SAVE_DEFERRED`（r3 新增）是轮级故障**：执行器收尾 `ledger.save()` 取不到
  跨进程锁（或盘上账本损坏）时登记，verdict 变 `blocked/READINESS_BLOCKED`。它**不
  表示某张票没被消费** —— 每张票的消费事实在 `consume()` 返回前已单独落盘；它表示
  本轮"消费/释放"的**合并写被推迟**。若同一轮另外产出了 intents，`classify_no_open`
  第 1 步先看 `opened`，verdict 仍是 `opened`（故障只作辅因）。
- **N5 已知留白（P3）**：本轮"一张票都没有"时 `verdict=no_candidates` 但
  `primary_reason` 字面是 `NO_EDGE`（九码集合冻结在 U05，"候选集为空"没有专属九码）。
  区分靠 `verdict` + `round_faults[].detail == "NO_TICKETS_AVAILABLE"`，**不要只看
  `primary_reason`**。详见 `docs/策略扫描原因码.md` §4。

## 六、r3 变更与自证

| 项 | 内容 |
|---|---|
| **N1** | 卡册损坏与运行时漂移**可区分**，且 `CARDS_INVALID` 在**主路径**（注册表尚未加载）上可达。改法：`scan()` **先** `_load_cards_or_fault()` 再 `_load_strategy_lab(cards)`（后者不再自己 `load_cards()`）。 |
| **N3** | 执行器收尾 `ledger.save()` 的 `LedgerLockTimeout`（以及 `LedgerCorrupt`）**局部化**：本轮照常返回已构造的 intents，落盘推迟登记为轮级故障 `LEDGER_SAVE_DEFERRED`，票据仍在 `orphan_consumed` 的可审计路径上。 |
| **N4** | 未注入漏斗（`funnel=None`）时也给一个非空 `round_id`（`exec:<iso>`），使建意图失败的**同轮 `release()` 真的生效**，票据不被永久占死。 |
| **N5** | 如实登记（P3）：见 §五末条 + `docs/策略扫描原因码.md` §4。 |
| **D1** | `c147895/` 标注 SUPERSEDED（README 顶部 + `SUPERSEDED.md`）；`ec8fd1c/` 顶部标注 SUPERSEDED；目录命名口径在本 README 顶部写明。 |

**N3 前后对照（构造 `LedgerLockTimeout`，原始输出）**

场景①真实外部进程持锁（`lock_timeout_s=0.3`）/ 场景②只让收尾 `save()` 抛超时：
```
### 修复前（ec8fd1c 包 = cc9287a 交付态）
场景① RESULT：**异常穿出整个执行器** LedgerLockTimeout: 取不到跨进程锁（0.3s 超时）
        decision 没有被返回 => 本轮 intents 全部作废
场景② RESULT：**异常穿出整个执行器** LedgerLockTimeout: 构造：收尾落盘取不到跨进程锁
        decision 没有被返回 => 已构造的 intents 全部被丢弃

### 修复后（本包）
场景① RESULT：**没有异常穿出**；decision=no_trade intents=0
        reasoning=t-real 票据账本不可用（LEDGER_BUSY），跳过 [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE]; 账本收尾落盘未完成（LedgerLockTimeout），已消费事实留待下次写入 [LEDGER_SAVE_DEFERRED]
        漏斗 details={"TICKET_LEDGER_UNAVAILABLE": 1, "LEDGER_SAVE_DEFERRED": 1}
        round_faults=["LEDGER_SAVE_DEFERRED"]
场景② RESULT：**没有异常穿出**；decision=open intents=1
        reasoning=t-inj 进入待执行: BTCUSDT LONG @ 100.0 名义=20.00U 杠杆=1x; 账本收尾落盘未完成（LedgerLockTimeout），已消费事实留待下次写入 [LEDGER_SAVE_DEFERRED]
        round_faults=["LEDGER_SAVE_DEFERRED"]
        盘上 orphan_consumed=["t-inj"]
```

**N1 `CARDS_INVALID` 可达性证据（主路径）**

`tests/integration/test_strategy_funnel.py` / `scripts/smoke_u08_strategy_funnel.py`
新增用例：**不预置** `scanner._registry`（即走 `_load_strategy_lab()` 主路径）+
卡册 JSON 损坏 ⇒
```
PASS  N1：首轮（注册表未加载）卡册损坏 → CARDS_INVALID（主路径可达）
PASS  N1：'卡册损坏'与'运行时漂移'确实可区分（漂移码没被误冒出来）
PASS  N1：真的漂移仍记 STRATEGY_LAB_DRIFT（没有被卡册故障顶掉）
```
修复前 / 修复后同一构造（**不预置** `_registry` + 卡册 JSON 损坏）的原始输出：
```
### 修复前（ec8fd1c 包 = cc9287a 交付态）
{"scan_exception": "JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 3 (char 2)",
 "round_faults": ["STRATEGY_LAB_DRIFT"], "verdict": "blocked",
 "primary_reason": "VALIDATION_REJECTED", "detail_counts": {"STRATEGY_LAB_DRIFT": 1}}
  ⇒ CARDS_INVALID 计数为 **0**：主路径上不可达，"卡册损坏"被标成了"运行时漂移"。

### 修复后（本包）
{"scan_exception": "", "round_faults": ["CARDS_INVALID", "NO_TICKETS_AVAILABLE"],
 "verdict": "blocked", "primary_reason": "VALIDATION_REJECTED",
 "detail_counts": {"CARDS_INVALID": 1, "NO_TICKETS_AVAILABLE": 1}}
  ⇒ 主路径可达；失败方向仍是 blocked（**没有**被读成 no_edge）。
```

**全量 delta（r3）**：`base=83 / head=84 / NEW_FAIL=[] / 非零 rc=[]`（见 §四）。
基线由 `695023cc` 前进到 `32ecf635`（U10a 落地）后已重跑，结论不变。
