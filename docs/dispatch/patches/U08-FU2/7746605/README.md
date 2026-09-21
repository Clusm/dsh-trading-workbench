# U08-FU2 · 审计库 `db.*` 异常不再整轮打断执行器（前后对照 / 方向论证）

- 作者工作区：`mat-wt/U08F2`，分支 `task/U08-FU2`，base = `b0c61ba`（干净）
- **代码 head（本包生成基准）：`7746605`**（本 `docs/` 包落盘后 head 再前进一步）
- 修复的一条：台账 `U08-R5-FU2`（P2）——`run_strategy_trader` 对**审计库 `db.*`** 的
  调用**没有护栏**，`sqlite3.OperationalError`（如 `database is locked`）会**穿出整个
  执行器**，本轮**已构造的 intents 全部作废、decision 不返回**。
- 与已修的 **N6 同形**（N6 只覆盖 `ledger.*`；`db.*` 这一路当时不在声明范围内）。
- 全程离线：临时目录造合成 SQLite / 内存桩，未读 `.env`/`data/`/密钥，未联网、
  未下单撤单，未动风险档/白名单/live 开关；产物只落本工作树。

## 0. 一句话结论

四处 `db.*` 调用全部加护栏，**方向与该文件既有先例（N6）在"异常不穿出"上一致、
在"失败语义"上刻意相反**：

| 调用 | 位置 | 失败语义 | 细分码 |
|---|---|---|---|
| `signal_ticket_has_order`（**读**，跨进程权威） | 去重闸门 | **fail-closed：状态不可验证 -> 不交易**（该票跳过 + 披露） | `AUDIT_UNAVAILABLE` |
| `log_risk_event` / `update_signal_ticket`（**纯审计写**） | 三处跳过分支 | **局部化：记 note + 本轮继续**（该开的仓照开） | note（带操作名 + 异常名） |

正常路径（审计库可用）行为**逐字不变**：该跳过的跳过、该入场的入场。

---

## 1. 交付物

| 类别 | 路径 | 行数 | 归属 |
|---|---|---|---|
| 修复（注册表） | `src/core/strategy_funnel.py` | +7/−0 | U08 独占 |
| 修复（执行器） | `src/agents/strategy_trader.py` | +57/−12 | U08 独占 |
| 文档（细分码登记） | `docs/策略扫描原因码.md` | +1/−0 | 本包内 |
| 验收（两态可跑） | `scripts/smoke_u08_strategy_funnel.py` 第 [10] 节 | +151/−0 | 本包内 |
| 修复 diff（复核用） | `01_*.diff` / `02_*.diff` / `03_*.diff` | 51 / 120 / 169 行 | 本目录 |
| 证据 | `evidence/` | — | 本目录 |

> 三个 diff 与分支 `task/U08-FU2` 的 `b0c61ba..7746605` 逐字节一致，供 I 用
> `git apply` 或直接合分支两条路径复核；三者文件不相交，应用顺序无关。
>
> `evidence/fu2_probe.py.txt` 是前后对照探针（`.py.txt` 后缀，避免把可运行 `.py`
> 归档进被 lint 的树）。
>
> **未**改动 `src/core/signal_ticket.py`（`ticket_skip_detail` 的真值表不动；
> `AUDIT_UNAVAILABLE` 与"形状/时效"无关，属执行侧细分码，故登记在
> `core/strategy_funnel.py`）。

## 2. 缺陷与前后对照（实测，不是推理）

探针 = `evidence/fu2_probe.py.txt`（不改被测逻辑，只在沙箱里对 `db` 打桩）。

**修前**（`evidence/prefix_probe.txt`）：

```
A. 去重读取（:184 db.signal_ticket_has_order）抛 OperationalError
   escaped  = sqlite3.OperationalError: database is locked
   decision = <未返回>（整轮作废）        intents = <丢失>
   funnel   = {}                          <- 连"为什么没开"都没留下
A2. 只有 t-bad 的读取不可用
   escaped  = sqlite3.OperationalError     decision = <未返回>   intents = <丢失>
B. 纯审计写入（log_risk_event/update_signal_ticket）抛 OperationalError
   escaped  = sqlite3.OperationalError     decision = <未返回>   intents = <丢失>
   funnel   = {'SYMBOL_ALREADY_HELD': 1}   <- 跳过已记，但其前一张票**已构造的 intent 随栈丢失**
```

**修后**（`evidence/postfix_probe.txt`）：

```
A.  escaped = None   decision = no_trade   intents = 0
    reasoning = t-a 审计库不可用（OperationalError: database is locked），
                状态不可验证，跳过 [READINESS_BLOCKED/AUDIT_UNAVAILABLE]
    ledger.is_consumed('t-a') = False      <- 跳过不烧票，下一轮仍可评估
A2. escaped = None   decision = open       intents = 1   <- 坏票不牵连同轮其余票
B.  escaped = None   decision = open       intents = 1   <- 已构造的 intent 不丢
    reasoning = ...; 审计写入失败（log_risk_event(strategy_conflict_skip,t-hold)，
                OperationalError: database is locked），本轮继续
C.  escaped = builtins.KeyboardInterrupt   <- 只吞 Exception，Ctrl-C 照常穿出
```

三种"事实"由此**不再互相冒充**：

- `TICKET_ALREADY_CONSUMED` = **已经**下过单（跨进程权威说是）；
- `AUDIT_UNAVAILABLE` = **无法断定**下没下过（权威读不出来）——本轮不交易；
- 正常入场 = 权威说没有 + 账本说没有。

## 3. 方向论证：为什么"读不出就跳过"而不是"读不出就当没下过单"

`:184` 那个判断的语义是"**这张票已经下过单吗**"，且它是**跨进程的唯一权威**
（账本只补"消费了但订单还没落库"的窗口）。两类错误代价**完全不对称**：

| 选择 | 修法 | 失效后果 | 可恢复性 |
|---|---|---|---|
| **fail-open**（读不出 -> 当"没下过"） | `except: pass` / `continue` 到下单 | **重复下单**：同一张票在审计库抖动窗口内被再开一次，双倍仓位、双倍风险，且与"同 symbol 互斥"竞争 | **不可逆**——订单已落到交易所 |
| **fail-closed**（读不出 -> 跳过） | 本票跳过 + 披露 `AUDIT_UNAVAILABLE` | **少开一笔**，本票**未被账本消费、未被标记** | **可逆**——审计库恢复后下一轮照常评估（票据仍 `open`） |

无人值守下的关键差别：重复下单是**静默的钱**（一次抖动 = 一个不受控的额外仓位，
且它绕过了"这一票只开一次"的全部护栏），而少开一笔是**可观测的延迟**（漏斗上
一条 `AUDIT_UNAVAILABLE`、note 里带底层异常名，运维一眼可查）。因此"状态不可验证 ->
不交易"是唯一正确方向；**做反了比不修更糟**（不修至少还会整轮中断、当场暴露）。

同一逻辑也解释了为什么**不**复用 `TICKET_ALREADY_CONSUMED` 来"顺手跳过"：那会把
"读不出来"谎报成"已经下过单"，运维据此排查会得出完全错误的结论（去查订单而不去查
审计库可用性），且 `verdict` 会落到 `handled`（"已在处理"）而不是 `blocked`
（"能力受损"）。新码的性质登记为 `fault`，一轮里所有票都读不出来时 `verdict=blocked`。

**纯审计写入**方向相反（局部化 + 继续），因为那三处 `log_risk_event` /
`update_signal_ticket` **不参与交易决策**：它们只记录"我为什么跳过"。让它们的中断
去作废本轮**已经决策好**的 intents，是把"日志坏掉"升级成"不入场"——与 N6 的
`ledger.save()` 收尾落盘完全同构。但**不静默**：note 里带操作名与底层异常名。

**边界**：只吞 `Exception`。`KeyboardInterrupt` / `SystemExit`（`BaseException`）
继续穿出（U14 规矩，探针 C 与冒烟第 [10] 节第 4 条都实测）。

## 4. 两态验收（`scripts/smoke_u08_strategy_funnel.py` 第 [10] 节）

新增 `test_audit_unavailability_contained()`（+151 行，10 条断言）。选它而不是
`tests/acceptance/test_execution_faults.py` 的原因：本缺陷的整体在**策略执行器 +
漏斗**，该冒烟脚本已是 N3/N4/N6 三条同族护栏的归档地，且自带"双态可运行"能力探测。

| 状态 | 结果 | 证据 |
|---|---|---|
| **修后**（`task/U08-FU2`） | **PASS 101 / FAIL 0** | `evidence/postfix_smoke.txt` |
| **修前**（同脚本，仅 `src/`+`docs/` 回退到 `b0c61ba`） | **PASS 94 / FAIL 7**，退出码 1 | `evidence/prefix_failing_smoke.txt` |

修前那 7 条失败**不是崩溃**，而是干净地记录了"异常穿出"：`('OperationalError', None)`
（decision 未返回）。两态都跑通、都由同一个 `check()` 判定，不存在"修前跑不起来"。

修前失败的 7 条：读取穿出 / 未披露 `AUDIT_UNAVAILABLE` / 三种事实混淆 /
坏票牵连整轮 / 写入穿出 / 写入未披露 / 注册表未登记。
（`KeyboardInterrupt 仍穿出` 与 `跳过不烧票`、`写入失败不改跳过语义` 三条是**不变量**，
两态都过，符合预期。）

## 5. 反向证据：正常路径行为逐字不变

| 套件 | 修前 | 修后 | 证据 |
|---|---|---|---|
| `scripts/smoke_u08_strategy_funnel.py` | PASS 91 / FAIL 0（旧脚本口径） | **PASS 101 / FAIL 0**（+10 = 新增断言） | `postfix_smoke.txt` / `prefix_failing_smoke.txt` |
| `tests/integration/test_strategy_funnel.py` | PASS 208 / FAIL 0 | **PASS 208 / FAIL 0** | `postfix_integration.txt` |
| `KeyboardInterrupt` 仍穿出 | 是 | **是** | 探针 C + 冒烟第 [10] 节 |

`AUDIT_UNAVAILABLE` 只在"审计库真的抛异常"时出现——正常路径一次都不会走到，
因此 91 -> 101 的增量**全部**来自新增断言，既有结论一条未变。

## 6. 全量离线回归：pristine vs patched 失败集合增量

沙箱 = `git archive b0c61ba | tar -x` 后**立刻 `git init`**（独立 repo，非仓库子目录）。
`scripts/run_offline_regression.py`，**94** 个脚本。

| 集合 | 结果 |
|---|---|
| pristine 全量 | `OFFLINE REGRESSION OK scripts=94`（退出码 0） |
| patched 全量（`git apply` 本包三个 diff） | `OFFLINE REGRESSION OK scripts=94`（退出码 0） |
| **NEW_FAIL**（pristine 过 -> patched 挂） | **∅（空集）** |
| FIXED / CHANGED | ∅ / ∅ |

逐脚本 PASS/FAIL 摘要对照见 `evidence/offline_regression_delta.txt`（94 个脚本一一对齐，
唯一数字变化是 `smoke_u08_strategy_funnel.py` 的 `91 -> 101`，同为 FAIL 0）。

**没有硬编码迁移版本集合**：本包的断言全部是相对性质（"是否穿出"/"计数为 1"/"账本是否
仍可消费"），不引用任何 schema 版本上限。

### 质量门禁与契约冒烟（patched 沙箱）

| 检查 | 结果 | 证据 |
|---|---|---|
| `scripts/check_quality.py` | `QUALITY CHECK OK files=303 untracked=0`（**未新增 exclude**；本包未触碰 `check_quality.py`） | `evidence/check_quality_patched_sandbox.txt` |
| `scripts/smoke_contracts_v3.py` | `PASS 173 / FAIL 0；golden cases=160` | `evidence/smoke_contracts_v3_patched_sandbox.txt` |

## 7. 补丁 3 种 git 配置实测

```
git apply 01_core_strategy_funnel_audit_code.diff
git apply 02_agents_strategy_trader_audit_guard.diff
git apply 03_smoke_u08_fu2_acceptance.diff
```

在两处 pristine 树各测一遍（`git apply --check --verbose`）：

| git 配置 | 忠实 checkout（`git worktree b0c61ba`） | 沙箱（`git archive` + `git init`） |
|---|---|---|
| 默认 | `Checking patch` ×4，**0 Skipped / 0 error** | 同上 |
| `-c core.autocrlf=false -c core.eol=lf` | 同上 | 同上 |
| `-c core.autocrlf=true` | 同上 | 同上 |

证据：`evidence/applycheck_*.txt`（`*_archive_sandbox.txt` 为沙箱那组）。

> 踩坑记录：第一次在沙箱里多做了 `git add -A && git commit`，此时该 repo 的 index 被
> 按全局 `core.autocrlf=true` 归一化，`-c core.autocrlf=false -c core.eol=lf` 会因
> "patch 期望 LF 工作树、磁盘却是 CRLF" 而失败——那是**沙箱自造的环境**，不是补丁缺陷。
> 按规格"解包后立刻 `git init`"（不 commit）重测，三配置全绿；仓库自身 checkout 出来的
> 忠实树也全绿。两种都能复现，故两组证据都留档。

## 8. 未尽事项（如实登记）

1. **跨进程"重复下单"窗口只收紧、未归零**：本修把"审计库读不出"变成不交易，但
   `db.*` 与 `ledger.*` **仍是两个独立权威**（审计库 = 已落库订单，账本 = 已消费）。
   极端时序（审计库读成功但随后写订单前进程被 kill）仍要靠在 `orphan_consumed()`
   上人工补单；这是 N4/N6 已登记的口径，本包不改。
2. **`AUDIT_UNAVAILABLE` 只是逐票跳过，不是轮级熔断**：审计库整体不可用时，本轮会
   逐票打出 N 条 `AUDIT_UNAVAILABLE`（`verdict=blocked`），但**不会**提前终止本轮
   去省 N 次失败查询。若将来观察到"审计库长时间不可用 + 大量票据"造成的退避压力，
   可考虑在轮级加一次短路——本轮**不**做（会改变正常路径的调用次数语义）。
3. **纯审计写入失败仍只是 note，不是结构化细分码**：`log_risk_event` /
   `update_signal_ticket` 失败**不入漏斗**（不产生 `FunnelItem`），只在
   `decision.reasoning` 的 note 里留痕（与 N6 的 `release()` 失败note 同构）。
   理由是它们**不改变**本轮结论（仍 `open`），把它登记为轮级故障会让 `verdict`
   在"仓位照常开"的一轮里变成 `blocked`，反而失去区分力。若运维需要看板侧的结构化
   计数，需另立轮级码——留后续。
4. **本次未顺手修**（按会话纪律刻意不碰）：`U08-R5-FU1` 的 `release()` 撤销覆盖、
   `U13FU3-INJECTION-TRUST`、`U09-K06-*`。
5. `:231` `_not_expired` 分支（过期 -> 写审计）在当前 `ticket_skip_detail` 真值表下
   **不可达**（同样的过期判定在循环顶部已 `continue`）。本次按"不得放宽既有语义"
   只加护栏、**未删**该分支，也未把它标为死代码——留给后续清理任务判定。
