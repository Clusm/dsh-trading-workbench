# U08 初审报告（角色 B：执行绑定 / 跨任务一致性）

- 任务：U08 策略漏斗与管理责任展示（C 线 CD）
- 审查对象：`task/U08` @ `c147895`，base `2ed2e5d`
- 审查者：B / 独立会话 / r1
- 审查工作区（独立 detached）：`reviews/U08/B/r1`（已回读确认 @ `c147895`）
- 解释器：`<venv>/python.exe`，`PYTHONUTF8=1`；无 pytest（全部走模块自带 `main()`）
- 纪律：只读；探针只落 `_scratch_B/`；所有 stdout 重定向到 `_scratch_B/out/*.txt`；未联网、未读 `.env`/`data/`、未启停服务

> 本报告所有结论均由**独立探针**得出，未采用编排者预先核验的作者自测数字。
> 复现命令一律为 `PYTHONUTF8=1 <venv>/python.exe _scratch_B/pXX_*.py`。

---

## 0. 结论

**CHANGES_REQUESTED**（P0 4 条 / P1 3 条 / P2 4 条 / P3 6 条）

一句话：**模块本身（`src/core/strategy_funnel.py`）的纯函数质量不错，验收做得也扎实，但它交付的是一个"未接线的孤岛"，这个孤岛已经打断了统一离线回归入口，而且"故障会不会被读成没有信号"这件事上确实有洞。**

四个 P0：

1. **P0-1 共享适配 5 条补丁全部缺失** —— `docs/dispatch/patches/U08/` 不存在（本工作区与 `mat-wt/I` 均无）；`src/` 内**零处** import `core.strategy_funnel`。任务卡的 4 个集成目标（`scanner.py` / `signal_ticket.py` / `strategy_trader.py` / `dashboard/app.py`）**一个都没接**。作者自测里"120/0 / 43/0 / 补丁两配置 5/5"的成分**在本候选上无法复现**（测试自己打印"当前态：dashboard 漏斗展示 未应用"，冒烟打印 `not_applied`）。
2. **P0-2 候选打断统一离线回归入口**：`scripts/smoke_u08_strategy_funnel.py` 随本候选新增，却**未登记**进 `scripts/run_offline_regression.py` 的 `SCRIPTS`，也不在 `EXCLUDED_SMOKE_SCRIPTS`，`_validate_smoke_manifest()` 直接 `RuntimeError`。回归**一条都跑不起来**（本树 `len(SCRIPTS)==81`，与"基线 82 全绿"不相容）。
3. **P0-3 跨进程并发消费同一 ticket，4 个进程全部拿到 `CONSUMED`**（确定性复现，见 §3）。
4. **P0-4 真正的故障会被读成"没有信号"**（本任务必达①，详见 §2.0–§2.3）：`no_edge` / `no_candidates` 两条通路都成立，且 `round_explanation()` 会同时产出"顶层 blocked、内嵌 no_edge"的自相矛盾工件；反向也有一处常态性误标（真的 no_edge 被判 `blocked`）。

必达② 基本成立（另有 P1/P2 附加条件）；必达③ 成立（`position_class` 与 U02 **口径一致但非同源**，见 P2-1）；必达④ 成立且 `validate_detail_table()` 非空转；**必达⑤ 成立**（预留池不被研究触碰，我独立复算三条断言通过，仅 docstring 措辞有误，见 P2-4）。

问题索引：P0-1…P0-4（§1、§2、§3）；P1-1 损坏账本可被洗白、P1-2 崩溃窗口无门禁、P1-3 绕过漏斗的早退分支；P2-1 `position_class` 非同源、P2-2 `meets_management_demand()` 恒真、P2-3 总预算 0/负静默、P2-4 不变量措辞；P3-1 `REASON_ALL` 恒真断言、P3-2 读侧篡改打到展示层、P3-3 `reserve_ratio_effective` 文档有实现无、P3-4 终态带细分码被静默算拒绝、P3-5 细分码↔闸门无一致性约束、P3-6 `position_class` 未按类聚合。

---

## 1. P0

### P0-1 共享适配补丁全部缺失；模块在生产路径上不可达

**证据**

- `ls docs/dispatch/patches/` → `U01 U02 U05 U06a U13 U14 U15`；**无 `U08`**。
- `ls "C:/Users/Tao/Desktop/币安/mat-wt/I/docs/dispatch/patches/"` → 同上，**I 侧也没有 U08 补丁**。
- `git ls-tree -r --name-only c147895 | grep patches/U08` → 空；`7aa0ad3` / `240b286` / `integration/unattended-v3` 亦为空。
- `git diff --name-status 2ed2e5d..c147895` → **只有 4 个新增文件**：
  `docs/策略扫描原因码.md`、`scripts/smoke_u08_strategy_funnel.py`、`src/core/strategy_funnel.py`、`tests/integration/test_strategy_funnel.py`。
- `grep -rn "strategy_funnel" --include=*.py src/` → **只命中 `src/core/strategy_funnel.py` 自身**；`src/` 内**零消费者**。
- 作者交付记录 JSON（`7aa0ad3`）自称 `"patches": "docs/dispatch/patches/U08/c147895/（01-05）；两 git 配置各 5/5 OK"` —— 该路径**不存在**，该自述无从复现。

**影响**：必达①的"17 道闸门是否穷尽实际拒绝路径"在本候选上**无法对着真实拒绝路径验证**——没有任何代码往漏斗里写。必达③的 `GET /funnel`、§6 的状态文件 `last_scan.funnel`、§7 的 `signal_ticket.ticket_skip_detail()` **都是文档描述，不是本候选的实现**。

**附加**：`docs/策略扫描原因码.md` §6/§7 与 `_route_level_check` 描述的能力属**未实现能力**（角色卡禁止把未实现写成已实现）。`src/core/signal_ticket.py` 中**没有** `ticket_skip_detail()`（全文 33 行，只有 `ticket_order_id` / `valid_signal_ticket`），所以"扫描侧与执行侧共用同一份真值表"（避免"扫描说有效、执行说过期"）**当前不成立**：`valid_signal_ticket()` 只返回一个 `bool`，无法区分 U08 登记的 4 个 ticket 细分码（`TICKET_SHAPE_INVALID` / `TICKET_EXPIRED` / `TICKET_EXPIRING_BEFORE_USE` / `ENTRY_ZONE_INVERTED`）——这 4 个码**没有任何生产者**。

**要求**：补交 5 条补丁（或明确改由 I 在集成时实现并更新交付记录），删掉/降级文档与测试中的未实现描述，并把 `smoke_u08` 登记进回归清单。

---

### P0-2 候选打断 `run_offline_regression.py`（回归入口 fail-closed 但方向错了）

**证据**（`_scratch_B/out/p00.txt`）

```
MANIFEST_RESULT=FAIL
EXC: RuntimeError 未登记冒烟脚本: scripts/smoke_u08_strategy_funnel.py
SCRIPTS_LEN= 81
```

`scripts/run_offline_regression.py:112` 的 `_validate_smoke_manifest()` 要求 `scripts/smoke_*.py` ⊆ `SCRIPTS` ∪ `EXCLUDED_SMOKE_SCRIPTS`；本候选新增了 `smoke_u08_strategy_funnel.py` 却没有做任何登记。`main()` 第一行就调它，所以 `main()` 在跑任何脚本前即抛出。

**影响**：编排者给出的"基线 scripts=82 全绿"与本候选**不相容**（本树 `len(SCRIPTS)==81`）。任何"全量 delta 全绿"的结论都不可能来自本候选。这一条属于 B 角色"执行绑定"范围：**新增的验收脚本必须纳入统一入口，否则等于没有回归保护**。

---

### P0-3 跨进程并发消费同一 ticket → 4 个进程全部拿到 `CONSUMED`

**证据**（`_scratch_B/p06b.py` / `out/p06b.txt`）——确定性构造竞态窗口（子进程先 `TicketLedger(path)` 完成 load，等所有进程 ready 后再 `consume()` + `save()`）：

```
确定性竞态结果 = ['CONSUMED', 'CONSUMED', 'CONSUMED|SAVE_ERR:PermissionError', 'CONSUMED']
CONSUMED 计数 = 4
最终 is_consumed = True 记录数 = 1
```

根因：`TicketLedger.consume()`（`src/core/strategy_funnel.py:797`）**只读内存态**，既不重读文件、也不加文件锁；`self._lock` 只覆盖进程内线程。`save()`（`:746`）的 `os.replace` 只保证"写原子"，**不构成跨进程互斥**。模块 docstring `:695` 的"进程安全：写用 tmp + os.replace 原子替换"把"原子写"讲成了"进程安全"，属过度声明。

附带缺陷：`save()` 的临时文件名是**确定性**的 `path.with_suffix(suffix + ".tmp")`，并发写直接 `PermissionError`（实测命中）——即"原子落盘"在并发下先用异常把调用方打掉。

**缓解**：U13 单实例锁在生产上限制了多写者（角色卡 E"单账户执行者"）。但在"崩溃后人工重启与旧进程重叠""双实例误启"下，重复消费是**真金白银**级别的风险。且 `consume` 的幂等是必达②的原文要求（"跨轮/重启不重复消费"），必须显式给出跨进程保证或明确降级说明。

**要求**：改为"consume 前重读文件 + 独占锁（复用 U05 `LeaseRegistry`/`instance_lock` 一类的既有机制）"，或把跨进程唯一性交给审计库的权威约束并在模块里写明；临时文件名加 pid/随机后缀。

---

## 2. 必达①（原因可区分）—— 硬结论（P0-4）

### 2.0 **故障是否会被读成 `no_edge`：会，三条独立通路**

| # | 通路 | 实测 | 应当 |
|---|---|---|---|
| a | `CARD_NOT_INDICATORIZED` → `NO_EDGE` | 3 张卡全未指标化 → `verdict=no_edge, primary=NO_EDGE` | 卡片配置缺陷应 `VALIDATION_REJECTED`/`blocked`（至少不能是 no_edge） |
| b | `NO_TICKETS_AVAILABLE` → `NO_EDGE` | 票据读取失败记此码 → `verdict=no_edge` | 读取失败应由生产者给 `TICKET_LEDGER_UNAVAILABLE`/`DATA_UNAVAILABLE`；`NO_TICKETS_AVAILABLE` 是"轮级空集"，与本轮"票据源读不到"是两件事，现在同为 `NO_EDGE` |
| c | `round_explanation()` 内嵌 `no_open` 与顶层结论**自相矛盾** | 顶层 `primary_reason=DATA_UNAVAILABLE`，内嵌 `no_open.verdict="no_edge"` | 同一份工件给出两个互相打脸的结论 |

证据：`_scratch_B/out/p01.txt`

```
[A] 空漏斗              : verdict=no_candidates primary=NO_EDGE candidates=0
[B] 卡全部未指标化       : verdict=no_edge       primary=NO_EDGE candidates=3
[C] NO_TICKETS_AVAILABLE: verdict=no_edge
[D] data_unavailable=..  : top-level primary=DATA_UNAVAILABLE
                          embedded no_open.verdict=no_edge   >>> INCONSISTENT=True
[E] source_errors=[..]   : primary=DATA_UNAVAILABLE / no_open.verdict=no_edge  >>> True
[G] evidence gate ok=F   : primary=RESEARCH_INCOMPLETE / no_open.verdict=no_edge >>> True
[F] model_error=Timeout  : primary=MODEL_DEADLINE / no_open.verdict=no_candidates
```

**根因（D/E/G）**：`round_explanation()`（`:1182`）把漏斗自身原因、源级错误、预算原因、U05 `classify_cycle` 合成顶层 `DecisionReason`，但内嵌的 `no_open` 来自 `funnel.no_open_explanation()`（`:1242`）——**只看 `funnel.reason_counts`**，完全看不见 `source_errors` / `evidence_report` / `model_error` / `plan`。于是"故障只在漏斗之外发生"时，顶层说 `blocked`、内嵌说 `no_edge`。

**根因（F）**：`no_open_explanation()` 的 `verdict`（`:477`）先看 `reason.codes ∩ blocked_codes`，再看 `total_candidates == 0`；当一轮里**根本没有候选被记录**（例如 `load_cards()` 读不到卡 / `_load_strategy_lab()` 抛错，都在第一道闸门之前），`total_candidates==0` → `no_candidates`。**"扫描根本没跑起来"与"扫描跑了但没有候选"被压成同一个 verdict，且 `primary_reason` 落成 `NO_EDGE`。** 漏斗 API 里**没有**任何"轮级故障"的 stage/detail（`record()` 的 `stage` 必须是 17 个候选阶段之一，`note()` 只是自由文本）——这正是"17 道闸门"不能穷尽实际拒绝路径的证据。

**次生问题（反向误标，P1）**：一个**真的 no_edge** 会被读成 `blocked`。实测（`_scratch_B/out/p07.txt`）：

```
纯 no_edge                                  : verdict=no_edge
no_edge + 另一 symbol 不在白名单（正常范围界定）: verdict=blocked primary=READINESS_BLOCKED
no_edge + 非 USDT 标的                       : verdict=blocked primary=READINESS_BLOCKED
```

`SYMBOL_NOT_IN_SCOPE` / `SYMBOL_NOT_USDT` → `READINESS_BLOCKED`，而 `READINESS_BLOCKED ∈ blocked_codes`。扫描器**每轮都会为范围外的卡/标的记录条目**（非通用模式下按卡走 `symbols`，白名单外的会被跳过），所以真实轮次里"有一张范围外的卡"几乎是常态 → 整轮 verdict 常态性变成 `blocked`。这样 `verdict` 就失去了区分力（永远 blocked），反而回到"只报一个 status"的老问题。

### 2.1 缺失/未知细分码确实 fail-closed ✔ （P0-4 的**例外**，无问题）

`_scratch_B/out/p03.txt`：

- `detail_reason("NOT_REGISTERED")` → `UnknownDetailCode`
- `ScanFunnel.record(拒绝型 stage 不带 detail)` → `ValueError`
- 拒绝型闸门不带细分码在 `FunnelItem.__post_init__`（`:327`）处兜住；`record(通过型 stage)` 才允许空 detail。

P3-4 一点边角：`FunnelItem(stage=INTENT_READY, detail=NO_SIGNAL_ON_CLOSED_BAR)` 不报错，而是 `passed=False`、`reason=NO_EDGE` —— 终态带细分码会被静默算成"拒绝"（不计入 `opened`）。低危，但"终态不得带细分码"没有被断言。

### 2.2 未登记码能否被塞进注册表？`validate_detail_table()` 非空转 ✔（P3-1 见下）

`_scratch_B/out/p03.txt`：

- 往 `DETAIL_REASON` 塞 `"BOGUS" -> "NOT_A_REASON"` → `UnknownDetailCode`
- 删掉 `MODEL_RATE_LIMITED` 的全部细分码 → `UnknownDetailCode`

所以"九码全部有细分码可达"这条**是真的有约束力**，不是空转。但测试里那条
`REASON_ALL == frozenset(trace.REASON_CODES)`（`tests/integration/test_strategy_funnel.py:155`）是**构造即恒真**
（`strategy_funnel.py:251` 就是 `frozenset(REASON_CODES)`），它不构成独立核验；真正
的约束力在 `validate_detail_table()`。二者不要混为一谈。

### 2.3 **P1-3 "17 道闸门"穷尽了实际拒绝路径吗？没有——存在绕过漏斗的早退分支**

我按扫描侧 / 执行侧的真实代码逐条比对了拒绝路径与 13 个拒绝型闸门：

**`src/signal_engine/scanner.py`（扫描侧，13 个 `continue` 分支基本都能对上闸门）**
`indicatorized=False`(L305)→`CARD_INDICATORIZED`；`signal_fn not in _registry`(L308)→`SIGNAL_FN_REGISTERED`；`not endswith("USDT")`(L324)、`symbol not in allowed_symbols`(L326)→`SYMBOL_IN_SCOPE`；`tf not in TF_MINUTES`(L334)→`TIMEFRAME_SUPPORTED`；`deferred`(L349)/`transport_outage`(L356)/`fetch_klines` 异常(L346)→`MARKET_DATA_READY`；`len(df)<210`(L365)→`KLINE_ROWS_INSUFFICIENT`；`out is None or len(out)!=len(df)`(L370)、`not last.signal`(L373)、`not _card_allows`(L376)、`f.iloc[-1].isna()`(L380)、`dedup_key in seen`(L385)、`ticket is None or expires<=now`(L394)、`existing_signal is not None`(L428)、`except Exception`(L440) → 都能对上一个码。**但对不上的恰恰是"更早"的那几处：**

1. **`load_cards()`（`scanner.py:62`）**：`strategy_cards.json` **不存在 → 返回 `[]`**；**JSON 损坏 → 直接抛 `json.JSONDecodeError`**。两者都发生在第一道闸门之前——前者让 `for card in cards` 一次都不进，**漏斗里一条记录都没有** → `verdict=no_candidates`、`primary_reason=NO_EDGE`（见 §2.0 [A]）。"策略卡文件读不到"与"本轮没有候选"被压成同一个结论，且**没有任何 stage/detail 能表达它**（`record()` 的 `stage` 必须是 17 个候选阶段之一，`note()` 是自由文本不进任何计数）。
2. **`_load_strategy_lab()`（`scanner.py:204`）**：`strategy_runtime.REGISTRY` 缺失 / `build_features` 缺失 / 卡与注册表漂移 → **抛 `RuntimeError`**（整轮中断）。同样无漏斗表达。
3. **`_load_state()`（`scanner.py:223`）**：状态文件损坏 → `except: pass` → 返回空 `seen` → **去重记忆清零**，同一根 bar 会被重新发信号。这不是漏斗的问题，但直接冲击必达②"跨轮不重复"（漏斗侧的 `TicketLedger` 只管"消费"，不管"发射"），应当登记为已知窗口。

**`src/agents/strategy_trader.py`（执行侧，存在两处"绕过漏斗"的早退）**

1. **进循环之前的一行静默过滤**（`strategy_trader.py:113-114`）：
   ```python
   tickets = [ticket for ticket in tickets if valid_signal_ticket(ticket, now)
              and (db is None or not db.signal_ticket_has_order(str(ticket["ticket_id"])))]
   ```
   不合法票据（对应 `TICKET_SHAPE_INVALID`/`TICKET_EXPIRED`/`ENTRY_ZONE_INVERTED`）与被 DB 判定"已下过单"的票据（对应 `TICKET_ALREADY_CONSUMED`）**在这一行被丢掉，永远进不了下面逐张记录的循环**。后果：一轮"所有票都已被消费过"的 `no_trade`，漏斗看到的是"没有票"（`NO_TICKETS_AVAILABLE` → **`NO_EDGE`**），而真相是 `HOLD_EXISTING`。**这又是一条"故障/重复消费被读成没有信号"的通路**，而且它就在生产代码里。
2. **`min_notional<=0 or max_notional<=0` 提前 return**（`strategy_trader.py:95-100`）：整轮不评估任何票，直接 `ChannelDecision(no_trade, "…只扫描不下单")`。U08 有 `CHANNEL_BUDGET_ZERO` 码，但**这个 return 发生在循环外**，没有任何地方记录它 → 状态文件里只会看到"没有票"。`CHANNEL_BUDGET_ZERO` 在 `strategy_funnel.py` **既无调用方也无文档化的落点阶段**。
3. `build_intent_from_ticket()` 没有 try/except（`strategy_trader.py:182`）：`STRATEGY_TRADER_EXCEPTION` 这个码当前**没有生产者**，异常会直接穿出 `run_strategy_trader`。

**结论**：`17 闸门`只覆盖"进入循环之后的逐候选拒绝"，**不覆盖**轮级前置失败（读卡失败 / 注册表漂移 / 通道预算为 0）与**进循环前的批量静默过滤**。必达①要求的"任意一轮 `no_trade` 都能回答为什么"在这些路径上**不成立**。请补一个轮级 stage（或明确让扫描器/执行器在这几处也 `record(origin=round)`），否则"没有信号"仍是唯一的兜底说法。

### 2.4 P3-5 细分码 ↔ 闸门 的一致性未被约束

模块里**没有** `DETAIL -> STAGE` 的表，也没有"每个拒绝型闸门至少有一个可达细分码"的自检。文档 §3 的 37 个码里，至少 `KLINE_QUALITY_REJECTED`、`EVIDENCE_*`、`TICKET_*` 在本候选上**没有任何生产者**（P0-1）。"17 闸门 × 37 码全映射"只是两张表各自自洽，不是"实际拒绝路径被穷尽"。

---

## 3. 必达②（跨轮/重启不重复消费）

**成立的**（`_scratch_B/out/p02.txt`）：

- 同轮重复消费 → `ALREADY_CONSUMED` ✔
- `save()` 后重建对象 → `ALREADY_CONSUMED` ✔
- `consumed` 是独立布尔：即使 `clock=lambda: 0` 也不会退化成"可再消费" ✔
- 线程并发（16 线程）→ `CONSUMED=1 / ALREADY=15` ✔（`RLock` 下 check+set 原子）
- 截断/半写账本 → `corrupt_reason` 非空、`consume()` → `LEDGER_CORRUPT` ✔（fail-closed）

**不成立的**：

### P0-3 跨进程并发（详见 §1 P0-3：4 进程全部 `CONSUMED`）

### P1-1 损坏账本可被 `register()+save()` **洗白**，旧消费事实静默丢失 → 同一 ticket 可再次消费

`_scratch_B/out/p02.txt`：

```
tA consumed: True
损坏后 corrupt= True   consume(tA)= LEDGER_CORRUPT     <- 这一层是对的
register+save 洗白后：tA 是否仍为已消费 = False
>>> 洗白后 tA 可再次被消费 = True
```

`register()`（`:773`）**完全不看 `self._corrupt`**，`save()`（`:746`）也不看，于是"损坏 → 重新登记本轮新票 → 落盘"会把损坏文件覆盖成一份**只有新票**的合法账本，之前所有的消费事实一次性丢失。模块 docstring `:691` 明写"文件损坏时一律 `LEDGER_CORRUPT` 并**拒绝**新消费，而不是把损坏读成空账本（后者会让同一 ticket 被消费第二次）"——**这条承诺在写路径上被自己破坏**。而扫描器每轮都会 `register()` 新票，是最容易踩到的调用方。

**要求**：`register()` / `save()` 在 `_corrupt` 非空时同样 raise（或要求显式 `reset_corrupt()`），并把 `reset_corrupt()` 的结果落审计。

### P1-2 "崩溃在标记消费与订单落库之间"只有一个**无门禁**的手工 `release()`，模块内无法自证"只补不重发"

`_scratch_B/out/p02.txt`：

```
消费 tD -> CONSUMED ; 崩溃后 recover() 带回: []      <- 已消费的不会回到 recover
release() 跨进程可用? True                            <- 但 release 没有任何前置校验
```

- `release()`（`:835`）只做一件事：把 `consumed` 翻回 `False`（连 `consumed_at_ms`/`consumed_round_id` 一起清零，**没有审计留痕**）。docstring `:838` 说"只允许撤销当次消费"，但实现里**没有任何**"当次/跨进程/是否已成交"的门禁——任何拿到 ledger 对象的调用方都能撤销一次已经落库成功的消费，进而二次下单。
- 模块**没有**"consumed 但无对应订单"的查询 API（`recover()` 明确排除 consumed；`as_dict()` 只有计数）。所以作者"审计库是已落库的权威、只补该窗口"的说法，**在本模块内不可验证**，必须依赖（尚不存在的）审计库侧查询。这一条要么补一个 `orphan_consumed(ticket_has_order)` 之类的显式接口，要么把"权威在审计库"写进 U12 的验收项。

### P3-2 读侧篡改是 fail-closed，但会以异常形式打到展示层

`_scratch_B/out/p08.txt`：被篡改的状态文件（`reason_codes` 里塞入未登记码）→ `funnel_from_report()` 能构造成功，但 `no_open_explanation()` **抛 `UnknownReasonCode`**。fail-closed 方向正确；但 `no_open_explanation()` 是展示路径，调用方（看板）必须捕获，否则一个坏状态文件会把页面打成 500。文档 §6 承诺"旧状态文件被显式标成没有可用漏斗数据"——**没有 funnel 键**这条成立（`available=False` 一类的形态由补丁侧提供），但"损坏/被篡改的 funnel 键"这条没有对应处理。P3，请在看板接线时一并兜住。

---

## 4. 必达③（每仓可定位）

**成立的**：

- `attribute_position()` 与 U02 `StrategyPositionManager.classify_position()` 在 6 个用例上**逐例一致**（有归属 / ticket 归属 / ai / guardian / 无来源 / 空 strategy_id）——`_scratch_B/out/p03.txt`：
  `ATTRIBUTED/ATTRIBUTED`、`LEGACY_UNATTRIBUTED/LEGACY_UNATTRIBUTED` × 5，`same=True`。
- `rule_version` 对**内容变更如实变化**（`rv1:ef2322e15392` → `rv1:602902ec4702`），对**改名/删卡**返回 `rv1:unknown@<全局>` 且 `rule_known=False`，`attribution_summary()["rule_unknown"]` 里能看见。没有"沿用旧版本"的问题 ✔

### P2-1 `position_class` 与 U02 **不是同源**，只是字符串字面量巧合一致

`strategy_funnel.py:936-937` 自建 `POSITION_CLASS_ATTRIBUTED = "ATTRIBUTED"` / `POSITION_CLASS_LEGACY = "LEGACY_UNATTRIBUTED"`；`inspect.getsource()` 确认模块**没有** import `strategy_position_manager`（`_scratch_B/out/p03.txt`：`是否 import U02 PositionClass: False`，我 grep 到的 `PositionClass` 只是 docstring 文本）。docstring `:934` 写的"与 U02 `PositionClass` 同口径（**引用同一个语义，不另立**）"与实际不符。U02 一旦改枚举值/改名（U02 已有 3 个候选 commit，枚举属易变项），U08 会**静默漂移**，而这正是"跨任务一致性"要防的。
**要求**：`from core.strategy_position_manager import PositionClass` 并直接产出枚举值（或加一条断言把两个定义绑在一起）。

### P3-6 `position_class` 未参与任何告警/统计口径的连接

`manager_of()` 的 `manager` 与 `position_class` 是**正交**的（AI 通道接管的无归属仓位既是 `manager=ai` 又是 `LEGACY_UNATTRIBUTED`）——这部分设计是对的、也写清楚了。但 `attribution_summary()` 只按 `manager` 聚合，没有按 `position_class` 聚合，也没有"存在 LEGACY 仓位时"的显式提示位（U02 侧有 `pending_legacy`）。可定位性够用，但"需要迁移的旧仓"在看板上不可一眼看出。P3。

---

## 5. 必达④（九码一致）

**成立且非空转**（`_scratch_B/out/p03.txt`）：

- `from core.decision_trace import REASON_CODES, DecisionReason, choose_primary, ...` —— **直接 import**，不是复制字面量；`REASON_ALL = frozenset(REASON_CODES)`（`:251`）。
- `validate_detail_table()` 能抓住"映射到非九码"和"某个九码无细分码可达"两种情况（实测抛错）。
- 漏斗侧无自造码：`FunnelItem` 的 `reason` 走 `normalise_reason_codes()`（`UnknownReasonCode`）。

**须澄清**（P3-1）：测试里 `REASON_ALL == frozenset(trace.REASON_CODES)` 是恒真断言（构造即同一对象），不能作为"独立核验"。真正有效的是 `validate_detail_table()`。建议测试直接断言 `validate_detail_table()` 的行为（含负例），删掉/降级那条恒真断言。

---

## 6. 必达⑤（管理优先预算）

**成立的**（`_scratch_B/out/p02.txt`）：预留池**永远**不被研究触碰。我在 7 组极端参数（ratio 0/0.5/0.9/1.0/2.0、total 0/-5/1/3/10）与对抗交错下验证 `management_available >= management_reserve` 恒成立（除管理侧自己主动取走预留——那是"管理优先"的定义，不是违规）。研究抢光后 `research_reason()` 给出 `MODEL_DEADLINE` + `MANAGEMENT_PRIORITY_BUDGET` ✔。

**不成立的 / 须修**：

### P2-4 作者自述"三条不变量"的第 2 条措辞与实现不符（实现与测试本身是对的）

模块 docstring `:1063` 与 `docs/策略扫描原因码.md` §5 写"研究侧的取用**永远**不减少管理侧仍可取的量，即 `claim_research` 后 `management_available()` 不下降"；冒烟 `smoke_u08_strategy_funnel.py:384` 的用例名也叫"研究取用不减少管理可取用的量"。字面上**不成立**：实测 `total=10, reserve=5`，`claim_research(100)` 拿到 5 之后 `management_available` 由 10 降到 5。

需要说清的是：**真正的不变量是"预留池不被研究触碰"（`management_available >= management_reserve`），这一点实现是对的、冒烟 L397-414 的三条断言也是对的那三条**（`research_used <= total - reserve`、守恒、`management_used + management_available >= reserve`），我独立复算通过。所以**必达⑤成立**，问题只在措辞：把"不下降"改成"不低于预留量"，否则容易被后来者当成一条可依赖的 API 契约。

### P2-2 `meets_management_demand()` 是**恒真空转**

```python
def meets_management_demand(self) -> bool:
    return self.management_demands == 0 or self.research_available >= 0
```

`research_available`（`:1111`）是 `max(0, ...)`，**恒 >= 0**，所以该函数恒 `True`。它被 docstring 说成 DC10 的检查（"不允许研究把管理挤下去"），实际不构成任何检查。实测（`total=0` 且管理要过钱）也返回 `True`。**要求**：改成有意义的谓词（例如"管理侧每次 `claim_management` 都拿到请求量"的记录），否则删除，避免用空转断言冒充门禁。

### P2-3 总预算为 0/负时是**静默**的

`research_starved`（`:1118`）要求 `total_budget > 0`，所以 `total_budget ∈ {0, -7}` 时 `research_reason()` 返回 `None`，研究侧一分钱也拿不到却**没有任何原因码**（实测）。任务卡原话是"研究池被吃光时给 `MODEL_DEADLINE` + `MANAGEMENT_PRIORITY_BUDGET`，而不是静默地'这轮没信号'"。总预算为 0 正是"研究拿不到钱"的极端形态，当前落回 `None` → 上层只能读到 `NO_EDGE`。请区分"没有预算可谈"与"预算被管理占用"，或至少在 `as_dict()` 之外给一个显式原因。

### P3-3 `from_total` 的非法比例退化是显式但**不可见**

docstring 说"退化时 `management_reserve=0` 是**显式可见**的（`as_dict` 里有 `reserve_ratio_effective`）"，但 `as_dict()`（`:1154`）**没有** `reserve_ratio_effective` 这个键。文档与实现不符（P3）。

---

## 7. 必达⑥/⑦ 补丁与 delta、跨任务一致性

### 7.1 补丁 `git apply --check`

**无法执行：补丁不存在**（P0-1）。`docs/dispatch/patches/U08/` 在本工作区与 `mat-wt/I` 均不存在，所以编排者"两配置各 5/5 OK"的结论**在本候选上不可复现**。我不为不存在的对象出具核对结论。

### 7.2 跨任务一致性（"未写既有列"）—— 成立，但是**平凡成立**

- `git diff --name-only 2ed2e5d..c147895 -- src/signal_engine/scanner.py src/core/signal_ticket.py src/agents/strategy_trader.py src/dashboard/app.py src/audit/migrations.py src/agents/models.py src/audit/database.py scripts/run_offline_regression.py` → **空**。
- 全量 `git diff --name-status` → 只有 4 个**新增**文件。

所以"`positions.*` / `signal_tickets.status` / `orders.ticket_id` / `last_scan.*` 全部只读""未新增列/枚举/迁移"**为真，但因为是"零共享改动"**——这也正是 P0-1 的另一面：没有改动 = 没有接线。U05 栽过的"偷偷写既有列"这个坑这次确实没踩（`grep` 在 `strategy_funnel.py` 里没有任何 SQL/`log_*`/`INSERT`）。

### 7.3 7 个硬编码 `range(1, 18)` 脚本两态逐字相同 —— 成立

`git hash-object` 逐文件比对 `2ed2e5d` vs `c147895`：`smoke_account_mirror.py`、`smoke_db_migrations.py`、`smoke_exchange_event_inbox.py`、`smoke_exchange_safety_monitor.py`、`smoke_order_mirror.py`、`smoke_order_outbox.py`、`smoke_protection_mirror.py` **全部 SAME**。鉴于第 7.2 条（零共享改动），这是必然结果。

### 7.4 全量 delta（强制）

方法：`git archive 2ed2e5d → _scratch_B/tree_base`、`git archive c147895 → _scratch_B/tree_head`；**同一 harness 清单**（base `SCRIPTS` 81 条 ∪ head 新增 `smoke_u08_strategy_funnel.py`，共 82 条）；**同一 env**（`EXECUTION_MODE=paper`、清 `MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`、`MTA_NO_PROXY=1`、`PYTHONUTF8=1`、剔除 `*BINANCE*/SENSENOVA*` 含 KEY/SECRET/TOKEN 的变量）；**逐条非 fail-fast**，记录每条的退出码与尾部输出。

脚本：`_scratch_B/p04_delta.py`；原始日志 `_scratch_B/out/delta_full.log`；结构化结果 `_scratch_B/out/delta_result.json`。

**结果**（`_scratch_B/out/p04_console.txt`、`delta_result.json`、`manifest.json`）

```
base_scripts= 81  head_scripts= 81  added= []  removed= []  manifest_n= 81
base_fail_n= 0
head_fail_n= 0
NEW_FAIL(regression)= []
FIXED= []
BOTH_FAIL= []
```

- **逐条非 fail-fast 跑满 162 次（81 × 2），两态退出码全 0；新增失败 ∅ / 修复 ∅。** 与作者自述"新增 ∅/修复 ∅"一致（作者的另一半"pristine scripts=81"也对上了——见下）。
- **但这份"全绿"不能按原样采信为"回归通过"**：这就是 P0-2 —— **harness 自己跑不起来**。`run_offline_regression.py` 在 `main()` 的第一行 `_validate_smoke_manifest()` 就抛 `RuntimeError`，所以**官方入口在本候选上退出非 0**，我上面这份全绿是我**绕过 harness、按它自己的 `SCRIPTS` 清单逐条重放**得到的。
- 另外：`base_scripts == head_scripts == 81`，说明 **`scripts=82` 这个基线数字不是本树的事实**（本树是 81；`smoke_u08` 未登记），请 I 核对口基线口径。
- `smoke_u08_strategy_funnel.py` **不在任何一版的 `SCRIPTS` 里**，因此不在上面的 162 次内；我单独跑了两态：head 树 `PASS 26 / FAIL 0`（`out/smoke_u08_head.txt`），base 树该文件不存在。
- 作者集成测试在 head 树：`PASS 82 / FAIL 0`（`out/test_u08_head.txt`）——**这是"未打补丁"分支**，文件自己打印"当前态：dashboard 漏斗展示 未应用"。
- 说明：delta 日志里出现的 `FAIL` 字样（78 处）**全部**来自 `rc=0` 的脚本（`smoke_u02_strategy_position_manager` 7×2、`smoke_contracts_v3` 3×2、`smoke_u06a_shadow_isolation` 2×2 等），是各脚本自带的**负向对照**，两态等价出现，**不是回归失败**。与任务提示一致，未误报。

**结论**：delta 命令层面零新增失败；但 harness 入口 fail-closed（P0-2），加上 P0-1 导致 `smoke_u08` 实际未进入统一回归，**"回归通过"这个结论在当前候选上不成立**。

<!-- (占位已替换) -->

---

## 8. 必达⑧ U05 接线声明的独立复核

作者声明："**接了**：`classify_cycle` 0→1、`reason_from_source_errors` 0→1、`classify_evidence_report` 经 `round_explanation(evidence_report=)` 可达、`DecisionReason`/`choose_primary`/九码常量。**仍留 U12**：`run_protection_first`、`graph.model_deadline()`（唯一命中 0 调用方）、`AgentRecorder` 轨迹落审计、`ModelRouteGuard`/`LeaseRegistry`/`background_refresh`、`macro/onchain.reason_from_errors`。"

**复核结论：计数属实，但"可达性"不属实——这是典型的"把不可达说成已接线"。**

`grep` 全仓（`src/ scripts/ tests/`）结果：

| 符号 | 真实 call site | 结论 |
|---|---|---|
| `classify_cycle(` | `strategy_funnel.py:1224`（**函数内**）、`test_decision_trace.py` ×3 | 生产 0；`strategy_funnel` 那 1 处位于 `round_explanation()` 内 |
| `round_explanation(` | `strategy_funnel.py` 定义、`test_strategy_funnel.py` ×6 | **全仓唯一调用者是作者自己的测试**；`src/` 内 0 |
| `source_error_reason(` | `strategy_funnel.py:1214`（`round_explanation` 内）、测试 2 处 | 生产 0（只能经 `round_explanation` 到达） |
| `classify_evidence_report(` | `decision_trace.py:428`（`classify_cycle` 内）、`test_decision_trace.py` | U08 侧 0 直接调用，只能经 `classify_cycle`←`round_explanation` |
| `graph.model_deadline()` | `src/core/graph.py:151` 定义，**调用方 0** | 声明属实 ✔ |
| `macro_public.reason_from_errors` / `onchain_public.reason_from_errors` | 定义于 U05 补丁，调用方 0 | 声明属实 ✔（U08 的 `source_error_reason` 也没有调用它们） |

即：`classify_cycle` 的"0→1"是**在一个没有任何调用方的函数里**从 0 变成 1。链条 `round_explanation → classify_cycle → classify_evidence_report` 的**可达性 = 0**：`round_explanation` 只被测试调用。所以本候选**没有**把 U05 的原因分类接进任何决策/展示路径。作者把"仍留 U12"的边界划在 `run_protection_first` 等处是合理的，但**把 `classify_cycle`/`reason_from_source_errors` 记成"已接"是过度声明**——它们与"仍留 U12"的那些项在可达性上没有区别（都是 0 生产调用方）。

**要求**：把 U05 接线声明改成"已在模块内提供入口，**生产可达性为 0**，待 U08 共享适配补丁落地后由 scanner/trader 调用"，或直接把该表按"可达/不可达"重列。

---

## 9. 未覆盖项 / 无法离线验证

- **生产首次跑批观测量**（真实一轮里 17 道闸门各自的命中分布、`stopped_at` 是否真的覆盖实际拒绝路径）——归 U12；本候选零接线，无从观测。
- **`GET /funnel` / `GET /api/funnel` 的真实路由**：补丁缺失，无法验证；作者测试里的 `_route_level_check` 只在其"已打补丁"分支运行，我**不采信**该分支的自述结果（我实测本候选走的是"未应用"分支并打印"接线确实缺席"）。
- **账本与审计库的勾稽权威性**（`signal_ticket_has_order` 能否兜住"已消费未落库"窗口）——需要 DB 侧代码，本任务只读范围外，归 U12。
- **真实多实例/宿主侧并发**：我用 4 个子进程确定性复现了竞态（P0-3）；真实宿主上是否会被 U13 单实例锁完全挡住，取决于部署形态，未验证。
- **`check_quality.py` / 密钥扫描**：编排者给出的 files=286 / 494 我未独立复跑（本候选只新增 4 个文件、且探针只在 `_scratch_B/`，与全仓扫描目的重叠度低）；新增文件为纯 Python/文档，未见密钥形态字面量。

## 10. 附：本报告用到的探针与原始输出

> `_scratch_B/tree_base`、`_scratch_B/tree_head` 是我用 `git archive` 物化的两棵树（base/head），供 delta 逐条重放；**只读快照，不是工作区**，可由 `p04_delta.py` 随时重建。工作区 tracked 文件零改动（`git status` 除 `_scratch_B/` 外为空）。

| 探针 | 主题 | 原始输出 |
|---|---|---|
| `p00_manifest.py` | 回归清单登记 | `out/p00.txt` |
| `p01_fault_noedge.py` | 故障→no_edge / verdict 一致性 | `out/p01.txt` |
| `p02_budget_ledger.py` | 预算不变量、账本损坏/并发/窗口 | `out/p02.txt` |
| `p03_attrib.py` | 归属、position_class 同源、注册表非空转 | `out/p03.txt` |
| `p04_delta.py` | 双树全量 delta | `out/delta_full.log`、`out/delta_result.json`、`out/manifest.json` |
| `p05_xproc.py` / `p06b.py` | 跨进程消费竞态 | `out/p05.txt`、`out/p06b.txt` |
| `p07_reverse.py` | 真 no_edge 被读成 blocked | `out/p07.txt` |
| `p08_readside.py` | 状态文件读侧篡改 | `out/p08.txt` |
| — | 作者集成测试（未打补丁态） | `out/test_u08_head.txt`（PASS 82 / FAIL 0） |
| — | 作者冒烟（未打补丁态） | `out/smoke_u08_head.txt`（PASS 26 / FAIL 0） |
