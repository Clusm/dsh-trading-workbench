# U08 r3 独立复验报告（Q 角色 · 独立会话）

- 复验对象：`task/U08` @ `cc9287a`（父 `2c90642`）；上一轮 B 对 `c147895` 判 CHANGES_REQUESTED（P0×4）
- 工作区（独立 detached，已确认 @ `cc9287a`）：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U08\R3\r1`
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- 纪律：只读；所有产物落 `_scratch_Q/`；命令输出重定向到 `_scratch_Q/out/*.txt`；未联网、未读 `.env`/`data/`、未启停服务、未写入任何其它 worktree（`git status` 除 `_scratch_Q/` 外为空；`mat-wt/I` 状态在全部探针前后逐字节未变）
- 沙箱：`_scratch_Q/tree_pristine`（= `mat-wt/I` @ `b97ae0d` 的 `git archive`）、`tree_patched`（pristine + 4 个 U08 独占文件 + 5 补丁）、`tree_nopatch`（仅独占文件）

---

## 0. 结论

# CHANGES_REQUESTED（窄口径）

**上一轮 4 个 P0 全部为真修复，且都经我独立复现**（接线 7/7、跨进程 20/20、故障 vs no_edge 两侧 73/73、harness 入口 83 脚本 rc=0）。
**但 r2 自身引入了 2 个未登记的 P2（其中 1 个是"崩溃"类），另有 1 个 P1 交付卫生问题**，按本任务标尺（"残留项须为 P2 及以下**且已如实登记**"）不能给 PASS-with-notes。

必须处理（全部很便宜）：

| # | 级别 | 一句话 | 位置 |
|---|---|---|---|
| **N3** | **P2（未登记·崩溃）** | `run_strategy_trader` 末尾的 `ledger.save()` **不吞** `LedgerLockTimeout`，锁被占时异常**穿出**整个执行器 —— 而 `consume()`/`release()` 都吞了。后果：**已经消费落盘的票据被丢掉、本轮已构造的 intents 全部作废**、graph.py 只得到一句泛泛的"策略交易AI异常" | `src/agents/strategy_trader.py:322`；对照 `src/core/strategy_funnel.py:1333`（`save`）vs `:1416`/`:1500`（`consume`/`release` 都吞） |
| **N1** | **P2（未登记·误标）** | 卡册 JSON 损坏（首轮）**标成 `STRATEGY_LAB_DRIFT`**，不是 `CARDS_INVALID`：`_load_strategy_lab()` 自己先 `load_cards()` 抛错，`_load_cards_or_fault()` 排在它后面，**`CARDS_INVALID` 在主路径上不可达**。fail 方向仍正确（blocked，**没有**读成 no_edge），但"卡册损坏"这个事实被标成了另一种故障 | `src/signal_engine/scanner.py:346`→`:249`；`_load_cards_or_fault` 在 `:258` |
| **N2** | P2（半登记） | `graph.py` **既不传 `funnel=` 也不传 `ledger=`**（U12 边界，README 有笼统声明），所以生产上：执行侧逐票原因不可见、`TicketLedger` 的跨进程"只消费一次"**根本没有生效**。README §五 的表格却把 `TicketLedger.consume/...` 一行记 "**可达**" 而未加"依赖调用方注入"的限定 | `src/core/graph.py:320` / `:344`；README §五 表第 3 行 |
| **N4** | P3（未登记） | `funnel=None` 且 `ledger=` 有值 时，建意图失败的 `release()` 拿到 `round_id=""` → 返回 `False` → **该 ticket 被永久占死**（一次异常 = 该信号再也不能开仓） | `src/agents/strategy_trader.py` 的 `except` 分支 |
| **N5** | P3 | "本轮一张票都没有" → `verdict=no_candidates` 但 `primary_reason=NO_EDGE`（`NO_TICKETS_AVAILABLE` 登记为 `edge` 性质）。verdict 可区分、`round_faults` 里有事实，仅主因看起来像"没信号" | `src/core/strategy_funnel.py` `classify_no_open` 第 3 步 |
| **D1** | **P1（交付卫生）** | 两包并存且旧包**没有 superseded 标记**、作者报告把包写成 `76381b7/`（实际目录名 `ec8fd1c/`）。**实测 `c147895/05` 在今天的集成树上 4 种 git 配置全部 apply 失败** → 应用错包 = 回归入口直接 `RuntimeError` | `docs/dispatch/patches/U08/{c147895,ec8fd1c}/` |

**没有发现**：故障被读成 `no_edge`（两侧都成立）、跨进程重复消费、未登记的既有脚本新增失败、未登记的关键列写入。

---

## A. 判定："该用哪个补丁包" → **用 `ec8fd1c/`；`c147895/` 应标 superseded**

### A.1 事实（`git rev-parse` 实测）

| 项 | `c147895/` | `ec8fd1c/` |
|---|---|---|
| 加入该目录的提交 | `2c90642`（"preserve uncommitted patch package (recovery)"） | `76381b7`（"deliver r2 shared-adaptation patch package"） |
| README 自称基线 | `2ed2e5d` | `e50f022` |
| `05` 目标 blob（`run_offline_regression.py`） | `1e1ad0b…` | `ed0fe17…` |

5 个共享目标文件里 **4 个在 `HEAD`/`e50f022`/`2ed2e5d` 三处同 blob**；**唯一分歧是 `run_offline_regression.py`**：

```
HEAD(cc9287a) == 2ed2e5d = 1e1ad0b…        （无 U14 登记行）
e50f022       == mat-wt/I @ b97ae0d = ed0fe17…（含 U14 登记行）
```

`2ed2e5d → e50f022` 对该文件的差异恰好是 U14 新增：
```diff
+    # U14（E 运维）：外部告警送达/心跳交易证据/主机失联检测离线冒烟。
+    "scripts/smoke_u14_alert_delivery.py",
```

**关键：集成树 `mat-wt/I` 现在在 `b97ae0d`，其 5 个目标 blob 与 `e50f022` 逐文件相同。** → 基线匹配的是 `ec8fd1c`。

### A.2 实测结论（`out/q05_apply.txt`）

| 配置（仓库根 = `mat-wt/I`） | `ec8fd1c` | `c147895` |
|---|---|---|
| 仓库默认（`core.autocrlf=false`） | **5/5 OK** | 4 OK，**`05` rc=1** |
| `-c core.autocrlf=false -c core.eol=lf` | **5/5 OK** | 4 OK，**`05` rc=1** |
| `-c core.autocrlf=true` | **5/5 OK** | 4 OK，**`05` rc=1** |
| `-c core.autocrlf=true --ignore-whitespace` | **5/5 OK** | 同左 |
| 真应用（沙箱树，`git init` 后 `git apply`） | **5/5 APPLIED** | `05` **APPLY_FAILED** |

`c147895/05` 的失败信息：
```
error: while searching for:
    "scripts/smoke_u02_strategy_position_manager.py",
    # U06a（D 影子研究）：影子最小隔离与旧口径披露。全程离线。
    "scripts/smoke_u06a_shadow_isolation.py",
)
EXCLUDED_SMOKE_SCRIPTS = {
error: patch failed: scripts/run_offline_regression.py:102
```
（因为它期望 `smoke_u06a` 后面**紧跟** `)`，而现状是 U14 那一行。）

### A.3 目录命名约定的判定

作者的目录名 `ec8fd1c` **不是**创建该目录的提交（创建者是 `76381b7`）。`ec8fd1c` 是 U08 分支上"只含独占文件"的那个提交 —— 也就是**被投递内容的源提交**。所以本仓库的约定是 **"目录名 = 被投递的源提交 hash"**，"用创建目录的提交给目录命名"这条**不成立**；作者报告里写 `76381b7/` 是把两个口径混了。
→ **要求**：把 `docs/dispatch/patches/U08/c147895/` **删除或加 `SUPERSEDED.md`**（写明"基线 `2ed2e5d` 已过期，`05` 在 `e50f022`/`b97ae0d` 上 apply 失败，请用 `ec8fd1c/`"），并在报告里引用 `ec8fd1c/`。两包并存且无标记 = **会被应用错包**。

---

## B. 逐项复验（对应任务 1–8）

### B1（任务 1）接线是否真的接上 —— **是，真接上了**

**grep（我自己的沙箱，pristine = `git archive b97ae0d`）**

```
pristine : grep -rn strategy_funnel src/  -> 0 命中
patched  : grep -rn strategy_funnel src/  -> 7 命中
  src/signal_engine/scanner.py:24
  src/core/signal_ticket.py:9
  src/agents/strategy_trader.py:22
  src/dashboard/app.py:23, 1089, 1138, 1139
```
与作者自述 **7 处、文件与行号全同**。

**路由（自建 TestClient，`out/q01.txt` §A）**
```
GET /funnel      -> 200（页面含"停在哪个闸门"）
GET /api/funnel  -> 200（JSON 含 scanner_funnel + positions）
GET /channels    -> 200
无状态文件时 available=False / verdict=unavailable（不伪造成功）
```

**真实调用路径（不看测试，跑生产函数；`out/q01.txt` §B–§G）**

| 生产者 | 我做的真实路径实验 | 实测 |
|---|---|---|
| `last_scan.funnel` | 真跑 `SignalScanner.scan()`（无卡册） | 状态文件 `last_scan.funnel` 有内容 ✅ |
| `no_open_verdict` / `no_open_reason` | 同上 | `blocked` / `READINESS_BLOCKED` ✅ |
| 路由端到端 | 扫描后再 `GET /api/funnel` | `available=True, verdict=blocked`；`/funnel` 页出现 `CARDS_UNAVAILABLE` ✅ |
| 票据源故障 | `db.fetch_open_signal_tickets` 抛错 | `TICKET_SOURCE_UNAVAILABLE` → `blocked/DATA_UNAVAILABLE` ✅ |
| strategy lab 故障 | `strategy_runtime.REGISTRY=None` | 抛 `RuntimeError`（整轮中断语义不变）**且**状态文件写 `no_open_verdict=blocked`/`status=error` ✅ |
| `ticket_skip_detail()` | 真跑执行器 + 倒挂入场区 | 漏斗记 `ENTRY_ZONE_INVERTED` ✅ |
| 账本消费 | 真跑执行器两轮 | 第 1 轮 `intents=1`+`consumed=True`；第 2 轮 `0` 笔 + `handled/TICKET_ALREADY_CONSUMED` ✅ |

→ **不是"只在测试里被调用"**。

两态自测（我自己的沙箱，非作者对照）：集成测试 **未打补丁 127/0、已打补丁 177/0**；冒烟 **51/0 / 68/0** —— 与作者自述数字一致。

### B2（任务 2）跨进程消费 —— **真修了**（`out/q02.txt`，20/20 PASS）

自建 `multiprocessing(spawn)` + `Barrier` 探针（未使用作者的）：

| 用例 | 结果 |
|---|---|
| 4 子进程 barrier 同票（先 load 再 barrier，复刻 B-r1 竞态窗口） | `CONSUMED ×1 / ALREADY ×3`，0 异常 ✅ |
| 8 进程 × 5 轮 barrier | 每轮 `(1, 7, 0)` —— 5 轮全对 ✅ |
| 4 进程各消费**不同**票 | 4/4 `CONSUMED`，无串号无饿死 ✅ |
| **绕过①**：子进程抢锁前**删掉锁文件** | 仍 `CONSUMED ×1` ✅（锁是新建 sidecar） |
| **绕过②**：**残留**锁文件（模拟崩溃遗留、无进程持锁） | 仍 `CONSUMED ×1` ✅ |
| **绕过③**：另一进程持锁 3s，本进程 `lock_timeout_s=0.5` | `LEDGER_BUSY`（fail-closed，**不消费**）；持锁者释放后该票**仍未**被消费 ✅ |
| **绕过④**：同进程 16 线程 | `CONSUMED=1 / ALREADY=15` ✅ |
| **绕过⑤**：同进程**两个** `TicketLedger` 对象（RLock 不共享，只能靠文件锁） | `a=CONSUMED / b=ALREADY_CONSUMED`（陈旧内存态被"重读"纠正）✅ |
| 崩溃窗口：消费后**不调用** `save()`，父进程直接读盘 | 盘上 `consumed=True` —— `consume()` 返回前已落盘 ✅ |
| 损坏账本 + 并发写 | `consume→LEDGER_CORRUPT`；`register`/`save` 抛 `LedgerCorrupt`；损坏文件未被覆盖 ✅ |

### B3（任务 3）"故障读成 no_edge"两侧 —— **两侧都成立**（`out/q03.txt`，73/73 PASS）

完全独立构造（未引用作者用例）：**14 类候选级故障 + 11 类轮级故障 + 4 类真 no_edge + 6 类边界**。

- 故障侧：`blocked`（卡未指标化因登记为 `scope` 而为 `no_candidates/VALIDATION_REJECTED` —— 与作者自述一致，**不是** `no_edge`），全部 25 类**无一产出 `no_edge`**。
- 真 no_edge 侧：`NO_SIGNAL_ON_CLOSED_BAR` / `PRICE_OUTSIDE_ENTRY_ZONE` / `BAR_ALREADY_SEEN` / `SIGNAL_ALREADY_PERSISTED` → **全部仍为 `no_edge/NO_EDGE`**。
- 混合（真 no_edge + 5 张范围外卡 + 非 USDT）→ **仍 `no_edge`**（反向误标已修，对应任务 4）。
- `round_explanation` 顶层 vs 内嵌 `no_open`：4 组外部故障（`data_unavailable` / `source_errors` / 证据门禁 / 模型异常）+ 真 no_edge **逐字一致**，**不存在"顶层 blocked、内嵌 no_edge"**。
- 纯范围界定码 → `no_candidates/READINESS_BLOCKED`（**不是** `no_edge`）。
- 闸门×细分码约束：挂错闸门 / 轮级码挂候选闸门 / 终态带细分码 / 拒绝型闸门不带细分码 → 全部 `ValueError`（P3-5/P3-4 已修）。
- 读侧被篡改 → `verdict=unavailable`、**不抛异常**、不采信手写的 `no_edge`（P3-2 已修）。
- 注册表自检负例（删性质 / 映射到非九码）→ 抛错（非空转）。
- 预算为 0 → `MODEL_DEADLINE` + `RESEARCH_BUDGET_ZERO` 可见，轮次 `blocked`（P2-3 已修）。

### B4（任务 4）反向误标 —— 已验证，见 B3（`SYMBOL_NOT_IN_SCOPE` 只作辅因）✅

### B5（任务 5）P1/P3 抽验（`out/q04.txt`，40/40 PASS）

| 项 | 实测 |
|---|---|
| P1-1 损坏账本 | `register`/`save` 抛 `LedgerCorrupt`，损坏文件**未被洗白**；`reset_corrupt` 显式留痕 ✅ |
| P1-2 `release` 门禁 | 同轮次才允许；跨轮/无 `round_id` 拒绝；留痕 `release_count`/`release_round_id`/`released_at_ms`；`force=True` 才可人工撤销且同样留痕；损坏态 `False` ✅ |
| `orphan_consumed` | 无判定函数→返回全部已消费票；给判定→只返回"消费了但无订单"；判定抛错时**朝"不重发"方向保守（排除）** ✅ |
| 执行器建意图失败 | 同轮 `release`（票不永久占死），漏斗记 `STRATEGY_TRADER_EXCEPTION`，异常不再穿出；释放后可再次开仓 ✅ |
| 账本损坏时执行器 | 不下单，`TICKET_LEDGER_UNAVAILABLE` → `blocked` ✅ |
| `position_class` **同源** | `fm.PositionClass is spm.PositionClass` → **True**（同一对象）；赋值右侧引用 `PositionClass.ATTRIBUTED.value`；6 个用例语义全对 ✅ |
| `manager` vs `position_class` 正交 | `source=ai` 无归属仓：`manager=ai` + `position_class=LEGACY_UNATTRIBUTED` ✅ |
| `attribution_summary` | `by_position_class` + `legacy_pending` 都在（P3-6 已做）✅ |
| `meets_management_demand()` | **真判定**：over-claim → `shortfall>0` → `False`（不再恒真）✅ |
| 预留池不变量 | 7 组极端参数（total 0/-5/1/3/10 × ratio 0/0.5/0.9/1.0/2.0）全部 `management_available >= management_reserve` ✅ |
| P3-3 | `reserve_ratio_effective` 在 `as_dict()` 里（文档与实现一致）✅ |
| P2-4 措辞 | docstring 已明确否定"不下降"的说法 ✅ |

### B6（任务 6）补丁 + 全量 delta

**补丁**：见 §A.2（`ec8fd1c` 在 4 种 git 配置下 5/5，且真应用成功；`c147895/05` 全配置失败）。

**全量 delta（强制）** —— `out/q06_delta_console.txt`、`out/delta_result.json`、`out/delta_{base,head}.log`

方法：`git archive b97ae0d` → `tree_pristine`；同一 harness 清单（**两树 `SCRIPTS` 并集** = 83）、**同一 env**（复制 harness `main()` 的 env 构造：`EXECUTION_MODE=paper`、清 `MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`、剔除 `*BINANCE*/SENSENOVA*` 含 KEY/SECRET/TOKEN 的变量）、**逐条非 fail-fast**。

```
base_scripts=82  head_scripts=83  manifest_n=83
added(head-base)=['scripts/smoke_u08_strategy_funnel.py']
removed=[] 
absent_in_base=['scripts/smoke_u08_strategy_funnel.py']   absent_in_head=[]
base_fail_n = 0   []
head_fail_n = 0   []
NEW_FAIL = []     FIXED = []     BOTH_FAIL = []
harness 入口: base rc=0 (OFFLINE REGRESSION OK scripts=82)
              head rc=0 (OFFLINE REGRESSION OK scripts=83)
```
与作者自述 `manifest_base=82 head_head=83 / NEW_FAIL=[]` **完全一致**。

`FAIL` 字样核查：base 日志 56 处 / head 57 处，**全部出现在 `rc=0` 的脚本里**（33 / 34 个脚本各带负向对照），其中 `smoke_u02_strategy_position_manager` 有 10 条负向 `FAIL` 行且 `rc=0`（与任务提示一致，**未误报**）。head 多出的 1 个含 FAIL 的脚本就是 `smoke_u08`（0 条 FAIL 行，只是它的 PASS 名里没有）。→ **无新增失败。**

**批次约束实测**：`tree_nopatch`（只有独占文件、没打 05）跑 harness 入口 → `RuntimeError: 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py`（rc=1）。→ README §二.2 的"05 必须与独占冒烟脚本同批"是**真约束**（P0-2 的成因）。

### B7（任务 7）门禁 —— `out/quality_patched.txt`、`out/secrets_patched.txt`

```
scripts/check_quality.py      -> QUALITY CHECK OK files=289   (rc=0)
scripts/scan_tracked_secrets.py -> SECRET SCAN OK files=519 registered_false_positives=2
                                   skipped_bare_identifiers=10 unparsable_python_files=0 (rc=0)
```
作者称 `files=289` ✅（我的 `untracked=289` 与作者不同，因为我的沙箱不是 git 仓库；`files=289` 一致）。

### B8（任务 8）两条诚实声明 —— 都成立

**① `classify_cycle` / `reason_from_source_errors` / `round_explanation` 生产可达性仍为 0**（`out/q07.txt` §1）

| 符号 | 全仓调用点（排除定义/`__all__`） |
|---|---|
| `classify_cycle` | 仅 `src/core/strategy_funnel.py:1963`（在 `round_explanation` 内） |
| `round_explanation` | **0 处** |
| `source_error_reason` | 仅 `strategy_funnel.py:1953` |
| `reason_from_source_errors` | `strategy_funnel.py:1918`、`data/macro_public.py:29`、`data/onchain_public.py:23` —— 而 `reason_from_errors`（macro/onchain）**调用方 0**，故可达性传递为 0 |
| `classify_evidence_report` | 仅 `decision_trace.py:428`（`classify_cycle` 内） |

README §五 已按"可达/不可达"重列，`classify_cycle` 一行明确写"**生产可达性仍为 0**"，**不再记"已接"** ✅

**② `_load_state()` 损坏静默清去重记忆，已登记**（`out/q07.txt` §3）
- 正常状态文件 → `seen`/`cursor` 正确读回；
- 损坏 → **静默**返回 `{"seen": {}, "scan_cursor": 0}`，无任何告警；
- README §五 第 3 条已明写"直接冲击必达②'跨轮不重复发射'，漏斗侧账本只管消费" ✅

补充核实（我做的）：**文档 ↔ 注册表无漂移** —— 我自己解析 `docs/策略扫描原因码.md` §3 的 markdown 表得到 44 行"细分码→九码→性质"，与 `DETAIL_REASON`/`DETAIL_NATURE` **逐条一致**（无单边项、无不一致项）；17 个闸门全部登记。7 个硬编码 `range(1,N)` 脚本两态**逐字节相同**；5 个 diff 对 `schema_migrations`/`current_schema_version`/`range(1,N)` **零命中**。

---

## C. 新发现（详细）

### N1（P2·未登记）卡册损坏被标成 `STRATEGY_LAB_DRIFT`，`CARDS_INVALID` 主路径不可达
`out/q01.txt` §C / §C2：
```
首轮（_registry 未加载）卡册 JSON 损坏：
  抛出 JSONDecodeError（整轮中断）
  round_faults = ('STRATEGY_LAB_DRIFT',)   verdict=blocked/VALIDATION_REJECTED   ← 期望 CARDS_INVALID
registry 已加载后再损坏：
  round_faults = ('CARDS_INVALID', 'NO_TICKETS_AVAILABLE')   ← 只有这条路径可达
```
根因：`scan()` 先 `self._load_strategy_lab()`（`scanner.py:346`），而它内部自己 `load_cards()`（`:249`）；损坏 JSON 在此抛出，外层 `except` 按"REGISTRY 是 dict 且 `build_features` 可调用"推断 → `STRATEGY_LAB_DRIFT`。`_load_cards_or_fault()`（`:258`）排在它之后。
**fail 方向正确**（仍是 fault → `blocked`），故不是 P0/P1；但作者 README §四 的"[C2] 五类卡册故障 → blocked+故障码"在 `CARDS_INVALID` 这一项上**主路径不成立**。
建议：把 `_load_cards_or_fault()` 提到 `_load_strategy_lab()` 之前，或让 `_load_strategy_lab` 的异常分类先看"是不是卡册解析错误"。

### N2（P2·半登记）生产上 `funnel=`/`ledger=` 都没注入，README 的"可达"标注过乐观
`out/q07.txt` §2：`graph.py:320` 的 `run_signal_scan(...)` 无 `funnel=`；`:344` 的 `run_strategy_trader(...)` 无 `funnel=`/`ledger=`。
- 后果 1：执行侧逐票跳过原因（B-r1 P1-3 的第 1 条"进循环前的静默批量过滤"）在**生产上仍然不可见**（模块修好了，调用点没接）。
- 后果 2：`TicketLedger` 的跨进程"只消费一次"**在生产上未生效**，去重仍只靠审计库 `signal_ticket_has_order`。**必达②在生产上仍是"库层保证"**。
README §五 bullet 1 只有一句"轮级故障依靠注入漏斗才会写进内存对象……`graph.py` 属 U12 接线范围"，**没有覆盖 `ledger`**，且表格第 3 行把 `TicketLedger.consume/...` 记为"可达"。
建议：把该行改成"**模块内可达；生产调用点（`graph.py`）未注入 `ledger=`，故跨进程去重在生产上未生效，待 U12**"。这是"未如实登记"的一种，和 B-r1 §8 批评的形态同源。

### N3（P2·未登记·崩溃）`ledger.save()` 让 `LedgerLockTimeout` 穿出整个执行器
`out/q08.txt` §4（确定性复现：外部进程持锁 8s，`lock_timeout_s=0.3`）：
```
holder -> READY
**异常穿出** LedgerLockTimeout: 取不到跨进程锁（0.3s 超时）: ...\lk.json.lock
漏斗 detail={'TICKET_LEDGER_UNAVAILABLE': 1}
```
- 同一 `TicketLedger` 的 `consume()`（`strategy_funnel.py:1416` 捕获 → `LEDGER_BUSY`）与 `release()`（`:1500` 捕获 → 内存撤销）**都吞掉了锁超时**，只有 `save()`（`:1333`）不吞，而 `run_strategy_trader` 末尾无条件 `ledger.save()`（`strategy_trader.py:322`）。
- 后果链：`consume()` 已经把"已消费"落盘 → intent 已构造 → 末尾 `save()` 抛 → **`intents` 全部作废**、graph.py 只得到泛泛的"策略交易AI异常" → 那些票据成为 **consumed 但无订单的孤儿**（要靠 `orphan_consumed` + 审计库补）。
- 可达性：默认 `lock_timeout_s=10.0`，需要 10s 级持锁；且今天 `graph.py` 不传 `ledger=`，故**当前生产不可达** —— 但这是本包交付给 U12 的代码路径，且与本模块自己声明的 "取不到锁返回 `LEDGER_BUSY`（fail-closed）" 设计不一致。
**要求**：末尾落盘按 `release()` 的写法吞掉 `LedgerLockTimeout`（或至少 `except LedgerLockTimeout: pass` + `note`），保证"单票 fail-closed"不升级为"整轮中断 + 丢 intents"。

### N4（P3·未登记）`funnel=None` + `ledger=` 时 release 无效 → 票据被永久占死
`out/q08.txt` §5：建意图失败后 `is_consumed('e1') is True`。因为 `release(round_id="")` 在无 `round_id` 时直接 `False`。
（`funnel` 与 `ledger` 同时注入是设计上的正常用法，`funnel=None` 属降级用法，故 P3。）

### N5（P3）`NO_TICKETS_AVAILABLE` 的 `primary_reason` 是 `NO_EDGE`
`out/q03.txt` §3：`verdict=no_candidates`，`primary_reason=NO_EDGE`。verdict 已可区分、`round_faults` 有事实，仅主因字面像"没信号"。P3，可只在文档里说明。

---

## D. 命令清单（可直接重跑）

```bash
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
cd "C:/Users/Tao/Desktop/币安/mat-wt/reviews/U08/R3/r1/_scratch_Q"

# 建沙箱（pristine = 集成树 b97ae0d；未写入任何其它 worktree）
mkdir -p tree_pristine tree_patched tree_nopatch
git -C "C:/Users/Tao/Desktop/币安/mat-wt/I" archive b97ae0d | tar -x -C tree_pristine
cp -r tree_pristine/. tree_patched/   # 再 cd tree_patched && git init -q . && git apply -p1 <5 补丁>
# 4 个 U08 独占文件从本工作区 cp 进 tree_patched / tree_nopatch

PYTHONUTF8=1 "$PY" q01_routes_realpath.py   # 接线/路由/真实路径   -> out/q01.txt
PYTHONUTF8=1 "$PY" q02_xproc.py             # 跨进程 + 5 种绕过   -> out/q02.txt
PYTHONUTF8=1 "$PY" q03_fault_noedge.py      # 故障 vs no_edge 两侧 -> out/q03.txt
PYTHONUTF8=1 "$PY" q04_p1p3.py              # P1/P3 抽验          -> out/q04.txt
bash q05_apply_matrix.sh                    # 两包 × 4 配置 apply -> out/q05_apply.txt
PYTHONUTF8=1 "$PY" q06_delta.py             # 全量 delta          -> out/q06_delta_console.txt
PYTHONUTF8=1 "$PY" q07_honesty.py           # 诚实声明 + 文档漂移 -> out/q07.txt
PYTHONUTF8=1 "$PY" q08_edges.py             # 新引入早退/异常边界 -> out/q08.txt
```

| 探针 | 主题 | 原始输出 |
|---|---|---|
| `q01_routes_realpath.py` | 路由 + 真实调用路径 | `out/q01.txt` |
| `q02_xproc.py`（+ `q02_xproc` 的 worker） | 跨进程消费与绕过 | `out/q02.txt` |
| `q03_fault_noedge.py` | 故障 vs 真 no_edge | `out/q03.txt` |
| `q04_p1p3.py` | P1/P3 抽验、预算、归属 | `out/q04.txt` |
| `q05_apply_matrix.sh` | 两包 apply 矩阵 | `out/q05_apply.txt` |
| `q06_delta.py` | 全量 delta | `out/q06_delta_console.txt`、`out/delta_result.json`、`out/delta_{base,head}.log`、`out/delta_harness_{base,head}.txt` |
| `q07_honesty.py` | 诚实声明 + 文档漂移 | `out/q07.txt` |
| `q08_edges.py` + `q08_lock_holder.py` | 新引入的早退/异常路径 | `out/q08.txt` |
| — | 两态自测（我独立跑的） | `out/test_patched.txt`、`out/test_nopatch.txt`、`out/smoke_patched.txt`、`out/smoke_nopatch.txt` |
| — | 门禁 | `out/quality_patched.txt`、`out/secrets_patched.txt` |

---

## E. 未覆盖 / 无法离线验证

- **真实生产首次跑批**：`graph.py` 不注入 `funnel`/`ledger`（U12），因此"真实一轮里 17 道闸门的命中分布"仍无从观测。
- **补丁在"U08 独占产物 + 5 补丁"整批合入 `mat-wt/I` 后**的完整回归：我是在自己的沙箱里做的（`tree_patched`），没有、也不允许改动 `mat-wt/I`；沙箱与集成树在该 5 个文件上的 blob 已逐文件核对相同，但**集成树仍可能在合入时叠加其它任务的共享改动**，故真实集成仍需 I 自己跑一次 harness。
- **真实宿主多实例并发**：我用 4/8 子进程 + 持锁进程确定性复现；U13 单实例锁在真实部署下能否挡住，取决于部署形态，未验证。
- **账本与审计库的勾稽权威性**（`signal_ticket_has_order` 是否真能兜住"已消费未落库"窗口）：需要 DB 侧数据，超出只读范围，归 U12。
- **`/funnel` 在真实浏览器里的渲染**：我只用 TestClient 断言 200 与关键字，未做视觉/交互核对。
