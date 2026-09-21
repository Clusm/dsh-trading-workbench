# U08 修复轮 r3 独立复验报告（Q 角色 · r4 · 独立会话）

- 复验对象：`task/U08` @ `fbef4e0`（父链 `08bb2f6 ← e1b5070 ← 0c131f2 ← 692bf81 ← cc9287a`），
  交付物 = 补丁包 `docs/dispatch/patches/U08/0c131f2/`（5 diff + README，基线 `32ecf635`）。
- 我的工作区（独立 detached，已 `rev-parse HEAD` 确认 = `fbef4e0f8de194f1b624649a03ebc99064bd466c`）：
  `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U08\R4\r1`
- 上一轮报告（已读）：`mat-wt/I/docs/dispatch/reviews/U08/cc9287a/Q_r3.md`（CHANGES_REQUESTED，剩 N3/N1/N2 三条未登记 P2）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`（`PYTHONUTF8=1`），无 pytest
- **集成 HEAD 实测**：`509246f3350078dcdf4f86a0683a1c9a56716ee7`（我进场时 `5fe88e8`，我复验期间 I 又前移了一格；两处都验过，5 个目标 blob 逐文件相同）。
  `5fe88e8`/`509246f` 相对补丁基线 `32ecf635` **只多了 `docs/` 提交**。
- 纪律：只读；未提交/推送/打标签/切分支；未读 `.env`/`data/`；未联网；未启停服务；
  **我的 `git status` 除 `_scratch_Q/` 外为空**（`mat-wt/I` 只被 `apply --check` 只读探测过，其 `git status` 亦为空）。

---

## 0. 结论

# CHANGES_REQUESTED（单点阻塞）

任务 1–8 **全部逐项通过**，上一轮 4 个 P0 的真修复本轮**再验仍成立**，r2 自带的 N1/N2/N3/N4/N5 **确实处置**：

| 任务 | 判定 |
|---|---|
| 1 N3 崩溃穿出 | **声明范围内真闭合**（LockTimeout/Corrupt 均不穿出、orphan 可审计）；**但括号外的异常仍穿出** → 见 N6 |
| 2 N1 卡册损坏误标 | **真修**（`CARDS_INVALID` 主路径可达；真漂移仍记 `STRATEGY_LAB_DRIFT`）；有意行为变化**判为安全** |
| 3 N2 选 (b) | **如实**（`graph.py` grep 0；r3 表已改"生产不可达"） |
| 4 N4/N5 | 抽验通过（N4 release 真生效、票据不占死；N5 登记文字准确）；**但 README 把 round_id 字面写成 `exec:<iso>`，实际是 `exec-<iso>`**（见 N7） |
| 5 旧包标注 | **完整**（`c147895/` + `ec8fd1c/` 都已标 SUPERSEDED） |
| 6 补丁 + 全量 delta | 补丁对**当前集成 HEAD 5/5 可应用**；delta `base=83/head=84/NEW_FAIL=[]/非零 rc=[]` |
| 7 门禁 | `QUALITY CHECK OK files=290` / `SECRET SCAN OK files=525` |
| 8 回归抽样 ≥5 | 全通过（接线 7、真实路径、8×5 跨进程、故障 vs no_edge 31 码、管理预算、position_class 同源） |

**唯一阻塞项 N6**（P2 · 未登记 · 异常穿出执行器）：
`run_strategy_trader` 收尾 `ledger.save()` 只吞 `(LedgerLockTimeout, LedgerCorrupt)`；
`consume()`/`release()` 同位置也只吞 `LedgerLockTimeout`。于是 **`OSError`（磁盘满/EROFS）、
`PermissionError`（Windows `os.replace` 目标被占用）等任何其它异常仍会穿出整个执行器**，
后果与已修的 N3 完全同形：**已经消费落盘的票据被丢掉、本轮已构造的 intents 全部作废、
调用方只得到一句泛泛的"策略交易AI异常"**。作者在 README 里把 N3 的修复范围**如实限定**为
"`LedgerLockTimeout`（以及 `LedgerCorrupt`）"，所以这不是"假声明"；但**"N3 已闭合"这个总口径
覆盖不到它**，且该残留**在交付物里一字未登记**。按本任务标尺（"任何未登记的 P2 …
**异常穿出执行器** ⇒ CHANGES_REQUESTED"）判阻塞。

修法极便宜：三处 `except Exception`（`strategy_trader.py` 收尾 save；`consume()`/`release()`
取锁段），note 文本已自带 `type(exc).__name__`，**不丢任何诊断信息**。

---

## 1.（任务 1）N3 是否真闭合 —— 声明范围内**真闭合**；括号外**未闭合**

探针：`_scratch_Q/probes/q1_n3.py`（自建，未引用作者对照）+ `q1b_consume_escape.py`
前后对照：`out/q1_n3_BEFORE.txt`（`tree_r2` = 集成树 + U08 独占产物 + `ec8fd1c` 01–04 = **cc9287a 交付态**）
　　　　　`out/q1_n3_AFTER.txt`（`tree_head` = 集成树 + U08 独占产物 + `0c131f2` 5 补丁）

### 1.1 前后对照（我自己的构造，确定性）

| 场景 | **BEFORE（cc9287a 交付态）** | **AFTER（0c131f2）** |
|---|---|---|
| A 基线（save 正常） | `open / intents=1` | `open / intents=1` |
| B1 收尾 `save()` 抛 `LedgerLockTimeout` | **ESCAPED**，decision 未返回，intents 全废 | **NO_ESCAPE** `open/intents=1`；`round_faults=['LEDGER_SAVE_DEFERRED']` |
| B2 收尾 `save()` 抛 `LedgerCorrupt` | **ESCAPED** | **NO_ESCAPE**，同上 ✅ |
| B3 收尾 `save()` 抛 **`OSError`（磁盘满）** | ESCAPED | **ESCAPED** ❌ ← N6 |
| B4 收尾 `save()` 抛 **`PermissionError`** | ESCAPED | **ESCAPED** ❌ ← N6 |
| B5 任意 `RuntimeError` | ESCAPED | ESCAPED（对照，非声明范围） |
| C 连续两轮都失败 | 两轮都 ESCAPED | 两轮 `open/intents=1` + `LEDGER_SAVE_DEFERRED`，无穿出 ✅ |
| D 失败/成功交替（4 轮） | 失败轮 ESCAPED | 失败轮 no_escape+fault、成功轮 no_escape ✅ |
| E 真实外部进程持锁（`lock_timeout_s=0.3`） | **ESCAPED**（decision 未返回） | **NO_ESCAPE**；`decision=no_trade intents=0`；`TICKET_LEDGER_UNAVAILABLE` + `LEDGER_SAVE_DEFERRED`；verdict=blocked ✅ |
| F 孤儿可审计 | ESCAPED | NO_ESCAPE；**新账本读盘 `orphan_consumed=['t1']`、`is_consumed('t1')=True`** ✅ |

E 场景 AFTER 的原始输出（与作者自述逐字一致）：
```
NO_ESCAPE  decision=no_trade intents=0
  reasoning=t1 票据账本不可用（LEDGER_BUSY），跳过 [READINESS_BLOCKED/TICKET_LEDGER_UNAVAILABLE];
            账本收尾落盘未完成（LedgerLockTimeout），已消费事实留待下次写入 [LEDGER_SAVE_DEFERRED]
  funnel round_faults=['LEDGER_SAVE_DEFERRED']
  funnel detail_counts={'TICKET_LEDGER_UNAVAILABLE': 1, 'LEDGER_SAVE_DEFERRED': 1}
```
F 场景：invariant `save()` 失败**不**影响 `consume()` 已落盘的事实 —— 盘上 JSON 里
`"consumed": true`，**新进程**新建账本即可查得。**孤儿确实在可审计路径上** ✅

### 1.2 绕过矩阵

| 绕过 | 结果 |
|---|---|
| 不只 LockTimeout，还试 `LedgerCorrupt` | **已闭合** ✅ |
| `save()` 抛 `OSError`/`PermissionError` | **穿出** ❌（N6） |
| 抛任意 `RuntimeError` | 穿出（对照） |
| 连续两轮都失败 | 不穿出；两轮各自返回 intents + 各记一次 `LEDGER_SAVE_DEFERRED` ✅ |
| 失败与成功交替 | 不穿出；账本最终一致（4 张票全部 consumed，release_count 无漂移） ✅ |
| 孤儿落盘可审计 | ✅（见 F） |
| **我只让 `save()` 抛**（隔离变量）与**真实外部持锁**（端到端） | 两种都试过，结论一致 ✅ |
| **`consume()` 抛非 LockTimeout 异常** | **穿出** ❌（N6；`claim = ledger.consume(...)` 处无 try） |
| **`release()` 抛非 LockTimeout 异常** | **穿出** ❌（N6） |

`out/q1b_consume_escape.py` 原始输出：
```
consume->OSError: ESCAPED OSError: [Errno 28] No space left
consume->PermissionError: ESCAPED PermissionError: [Errno 13] denied
consume->LedgerCorrupt: ESCAPED LedgerCorrupt: x
release->OSError: ESCAPED OSError: [Errno 28] No space left
```

### 1.3 登记核验

`out/gate_quality_patched.txt` 之外的源码核验：`LEDGER_SAVE_DEFERRED` 已完整登记 ——
`strategy_funnel.py:239`（常量）、`:295` → `REASON_READINESS_BLOCKED`、`:373` → `NATURE_FAULT`、
`:436` → 空 stages（= 轮级，`ROUND_ONLY_DETAILS`）。我独立确认它 `in ROUND_ONLY_DETAILS`
且 nature=FAULT ✅，且**故障只作辅因**（同轮有 intents 时 verdict 仍 `opened`，见 B1 输出）。

**判定：N3 在声明的两类异常上真闭合、孤儿可审计；但"任何异常都不得穿出执行器"这一条不成立。**

---

## 2.（任务 2）N1 是否真修 + 有意行为变化是否安全

探针：`_scratch_Q/probes/q2_n1.py` → `out/q2_n1_BEFORE.txt` / `out/q2_n1_AFTER.txt`

| 用例 | BEFORE（cc9287a） | AFTER（0c131f2） |
|---|---|---|
| C1 **主路径**（不预置 `_registry`）+ 卡册 JSON 损坏 | **抛 `JSONDecodeError`**；`round_faults=['STRATEGY_LAB_DRIFT']`（= N1 的 bug，**逐字复现**） | **不抛**；`round_faults=['CARDS_INVALID','NO_TICKETS_AVAILABLE']`、`verdict=blocked`、`primary_reason=VALIDATION_REJECTED` ✅ |
| C2 预置 `_registry` + 卡册损坏 | `['CARDS_INVALID','NO_TICKETS_AVAILABLE']` | 同 ✅ |
| C3 **真漂移**（合法卡册 + 未注册 `signal_fn`） | `RuntimeError` + `['STRATEGY_LAB_DRIFT']` | `RuntimeError` + `['STRATEGY_LAB_DRIFT']` ✅ **未被 `CARDS_INVALID` 顶掉** |
| C4 注册表缺失 | `['STRATEGY_LAB_UNAVAILABLE']` | 同 ✅ |
| C5 卡册文件不存在 | `['CARDS_UNAVAILABLE','NO_TICKETS_AVAILABLE']` | 同 ✅ |
| C6 空卡册（合法 JSON） | `['NO_TICKETS_AVAILABLE']` → `no_candidates/NO_EDGE` | 同（= N5 已登记留白） |

**N1 真修**：两类故障**可区分**，`CARDS_INVALID` 在**主路径**可达，且**没有把两类混在一起**
（C3 真漂移仍是 `STRATEGY_LAB_DRIFT`，`CARDS_INVALID` 计数为 0）。

### 2.1 有意行为变化（"卡册损坏不再整轮中断"）的安全性判定 —— **安全**

四条独立理由：

1. **不违背必达①**：损坏轮 `verdict=blocked`（`round_faults=[CARDS_INVALID]` 是 `NATURE_FAULT`），
   **没有被读成 `no_edge`**。C1 的 `state.last_scan.no_open_verdict='blocked'` 是我的原始输出。
2. **不会"以为有信号"**：空卡册 ⇒ `cards=[]` ⇒ 扫描循环零候选，**不可能由卡册产生新信号**。
3. **唯一被放行的票据是审计库里的历史 open ticket**（C7：返回 `['hist-1']`），
   而**这与既有"卡册文件不存在"路径（C8）逐字同形**（BEFORE 的 C8 就已经这么干）。
   即：**本轮没有引入新的放行面**，只是把损坏从"整轮中断"对齐到既有的"缺失"语义。
4. 历史票据进执行器后**仍要**过 `ticket_skip_detail()` + 价格/入场区/账本消费/持仓冲突全套闸门，
   不存在"绕过卡册校验开仓"。

**观察（不阻塞）**：C7 的 `state.last_scan.status` 仍是 `'ok'`（`funnel` 里故障在，但 legacy `status`
字段没反映），且这一现象在 C8（缺卡册，**既有行为**）里完全相同 —— 属既有、成体系的一致行为，
非 r3 引入；读侧权威是 `no_open_verdict`/`round_faults`。

---

## 3.（任务 3）N2 选 (b) 是否如实 —— **如实**

- `grep -n "funnel\|ledger\|TicketLedger\|ScanFunnel" src/core/graph.py` → **0 命中**（补丁树与 pristine 树都是 0）。
  两个调用点实名核验：`graph.py` 的 `run_signal_scan(...)` **无 `funnel=`**；`run_strategy_trader(...)` **无 `funnel=`/`ledger=`** ✅
- r3 README §五 表格已改：`TicketLedger.consume/recover/release/orphan_consumed` 一行 →
  **"生产不可达（待 U12）… 不写"可达""** ✅；并明写"上一版把 ledger 记成'可达'是过乐观的，本版已改正"。
- **全仓搜一遍**（`grep -rn 可达 --include=*.md docs/ | grep -i ledger|漏斗|funnel`）：
  残留的过乐观表述**只存在于两个已标 SUPERSEDED 的归档包**里
  （`ec8fd1c/README.md:104,106`、`c147895/README.md:107`），且各自顶部都有"请勿使用本包"横幅。
  **有效包 `0c131f2/` 内无过乐观表述** ✅（归档保留原文属正常留档，不计为问题）

---

## 4.（任务 4）N4 / N5 抽验

`out/q3_n4n5.txt`（`_scratch_Q/probes/q3_n4n5.py`）

**N4（`funnel=None` + `ledger=`，建意图失败）** —— 真修：
```
decision=no_trade intents=0
reasoning=t1 建意图失败（ValueError），跳过 [VALIDATION_REJECTED/STRATEGY_TRADER_EXCEPTION]
账本记录：consumed=False release_count=1 release_round_id='exec-2026-09-21T03:31:29.649607+00:00'
票据是否仍被占死（is_consumed）=False
重读盘：is_consumed=False release_count=1        ← 撤销也落了盘
注入漏斗时 round_id='round-xyz'（用漏斗轮次）   ← 正确优先级
```
票据**不再被永久占死**，且 `release_count=1` / `release_round_id` / `consumed_round_id=''` 留痕正确 ✅

**N5** —— 登记文字准确：
```
本轮一张票都没有：verdict=no_candidates primary_reason=NO_EDGE
round_faults=['NO_TICKETS_AVAILABLE'] reason_codes=['NO_EDGE']
```
与已登记的 P3 留白逐字一致；`docs/策略扫描原因码.md:134` 明确写
`NO_TICKETS_AVAILABLE | NO_EDGE | edge | 轮级`，`:183-186` 教读者"不要只看 `primary_reason`"，
改用 `verdict` + `round_faults[].detail` 区分 ✅ **N5 登记属实**。

**新发现 N7（文档口径小错，P3）**：README §六 N4 行把未注入漏斗时的 round_id 写成
**`exec:<iso>`（冒号）**，实测代码产出 **`exec-<iso>`（连字符）**：
```
未注入漏斗时 round_id 字面 = 'exec-2026-09-21T03:31:29.674459+00:00'
startswith('exec-')=True   startswith('exec:')=False
```
行为无影响（`release()` 只要求"非空 + 与 `consumed_round_id` 相等"，且 `consumed_round_id` 同源），
但字面与文档不符，建议顺手改正。

---

## 5.（任务 5）旧补丁包标注 —— **完整；口径写清了**

| 资产 | `c147895/`（r1） | `ec8fd1c/`（r2） | `0c131f2/`（r3，现行） |
|---|---|---|---|
| README 顶部 SUPERSEDED 横幅 | ✅ `> # SUPERSEDED —— 请勿使用本包` | ✅ `> # SUPERSEDED —— 请勿使用本包（2026-09-21 r3 起）` | 不适用 |
| 独立 `SUPERSEDED.md` | ✅ 有 | ✗ **无**（仅 README 横幅） | 不适用 |

- **"只有 `0c131f2/` 有效"在交付物里说清楚了**：r3 README 顶部三条（第 11–13 行）
  "本包取代 `../ec8fd1c/`（r2），`../ec8fd1c/` 取代 `../c147895/`（r1）… **三包并存时请只用本包**"，
  且两个旧包各自顶部反向指回 `../0c131f2/`。**双向可追溯，不会应用错包** ✅
- r3 README 的 D1 行自述"`c147895/` 标注 SUPERSEDED（README 顶部 + `SUPERSEDED.md`）；`ec8fd1c/` 顶部标注 SUPERSEDED"
  —— **与实际资产逐条相符**（只说 c147895 有 `SUPERSEDED.md`，未虚报 ec8fd1c 有）✅
- **命名口径写清**：r3 README 第 8–10 行明写"本目录名 `0c131f2` = **本轮 U08 独占产物的源提交**，**不是**创建本目录的提交；
  目录命名一律跟**被投递内容的源提交**（B-r3 D1 澄清）" ✅

**观察（不阻塞）**：`ec8fd1c/` 没有独立 `SUPERSEDED.md`（只有 README 横幅）。横幅本身已无歧义
（"请勿使用本包 / 用本包 = 丢掉上述三个修复 / 请用 `../0c131f2/`"），故不判问题。

---

## 6.（任务 6）补丁应用 + 全量 delta

### 6.1 补丁能否应用到**当前集成 HEAD** —— **能，不是集成阻塞项**

仓库根 = `C:/Users/Tao/Desktop/币安/mat-wt/I`，**HEAD 实测 = `509246f`**（我进场时 `5fe88e8`）。

**基线漂移核查（硬前提）**：5 个目标文件的 blob 在 `32ecf635` / `5fe88e8` / `509246f` 三处**逐文件相同**：
```
3cee84747c029f3460380680a92398e739e12479  src/signal_engine/scanner.py
956c2e9a82be0d6f10418b035077fe4b83a8aa61  src/core/signal_ticket.py
96a035fd28a3e2debc9fdf1db92995384266be5e  src/agents/strategy_trader.py
1f77be25861f2bc97b9596686ecb8e9d9ae33957  src/dashboard/app.py
2113d652c871830b5e0ae0246a0ddff865504cde  scripts/run_offline_regression.py
```
（这也解释了"为什么前移不影响"：`32ecf635 → 509246f` 的 5 个提交**只改 `docs/`**。）

**`git apply --check -v`（在真实集成树 `I` 上跑，3 种配置 × 5 补丁 = 15/15）** —— `out/phase1_applycheck_v.txt`：
```
cfg=''                                         5/5 rc=0  "Checking patch <path>..."
-c core.autocrlf=false -c core.eol=lf          5/5 rc=0
-c core.autocrlf=true                          5/5 rc=0
```
> 注：我刻意用了 `-v`。**不带 `-v` 时 `rc=0` 可能是 "Skipped patch"（空炮）**，
> 我最初在工作区内非 git 子目录直接 `git apply` 就踩到过这个坑（`-v` 显示 `Skipped patch 'src/...'`）。
> 本报告的 15/15 都在 `-v` 下确认**有 "Checking patch"，无 Skipped**。

**真实应用（沙箱 `tree_head` = 集成树 + U08 独占产物 + 5 补丁）** —— `out/phase1_build.txt`：
```
rc=0 × 5 全部 APPLIED
 scripts/run_offline_regression.py |   3 +
 src/agents/strategy_trader.py     | 187 +++++++++++---
 src/core/signal_ticket.py         |  53 ++++--
 src/dashboard/app.py              | 216 +++++++++++++++++++
 src/signal_engine/scanner.py      | 230 ++++++++++++++++++++--
 5 files changed, 653 insertions(+), 36 deletions(-)
```
与 README §三 的 `git diff --stat` **逐位相同**（653/36）✅
对照：`ec8fd1c/05` 在当前基线上**确实失败**（`error: patch failed: scripts/run_offline_regression.py:102`，
因为 U14 在 `smoke_u06a` 后插了登记行）—— 印证"旧包不能用、`0c131f2/05` 的上下文写法是对的"。

→ **`0c131f2/` 可干净应用到当前集成 HEAD；不存在因 HEAD 前移而冲突的集成阻塞项。**

### 6.2 全量回归 delta（强制）—— `out/q6_delta_console.txt` / `out/delta_result.json` / `out/delta_{base,head}.log` / `out/delta_harness_{base,head}.txt`

方法：`git archive 509246f` → `tree_base`；`tree_base + U08 独占产物 + 5 补丁` → `tree_head`。
**同一 SCRIPTS 清单并集**、**同一 env 逐行复刻 harness `main()`**
（`EXECUTION_MODE=paper`；清 `MTA_PROFILE`/`MTA_STATE_ROOT`/`MTA_DATA_ROOT`；`MTA_NO_PROXY=1`；
`PYTHONUTF8=1`；`PYTHONIOENCODING=utf-8`；剔除名含 `BINANCE|SENSENOVA` × `KEY|SECRET|TOKEN` 的变量）；
**逐条非 fail-fast**。

```
base_scripts=83  head_scripts=84  union=84
added(head-base)=['scripts/smoke_u08_strategy_funnel.py']   removed=[]
absent_in_base=['scripts/smoke_u08_strategy_funnel.py']     absent_in_head=[]
base_fail_n=0 []          head_fail_n=0 []
NEW_FAIL=[]               FIXED=[]        BOTH_FAIL=[]
非零 rc：base=[]          head=[]
harness 入口：base rc=0 (OFFLINE REGRESSION OK scripts=83)
              head rc=0 (OFFLINE REGRESSION OK scripts=84)
```
**逐条 rc 直方图：167 个脚本执行（base 83 + head 84）全部 `rc=0`，无一个非零。**
与作者自述 `base=83 / head=84 / NEW_FAIL=[] / 非零 rc=[]` **完全一致** ✅

`FAIL` 字样核查（**未误报**）：两树各只有 **1 个脚本**含 `FAIL` 行 ——
`scripts/smoke_u02_strategy_position_manager.py`（各 10 行），其 `rc=0`（自带负向对照）；
**没有任何 `rc!=0` 的脚本含 `FAIL` 行** ✅ 与任务提示一致。

对照性核验：`tree_head` 里 `scripts/smoke_u08_strategy_funnel.py`、`smoke_u10a_release_package.py`（72s）、
`smoke_u14_alert_delivery.py`、契约/门禁等在 head 全绿；`smoke_signal_scanner_integration.py`
（唯一以无参形式调用 `_load_strategy_lab()` 的既有调用方）亦 `rc=0` → README §二.5"行为不变"成立 ✅

---

## 7.（任务 7）门禁 —— 全绿

沙箱 = `tree_head`（打过 5 补丁）：
```
scripts/check_quality.py       -> QUALITY CHECK OK files=290 untracked=0            (rc=0)
scripts/scan_tracked_secrets.py-> SECRET SCAN OK files=525 registered_false_positives=2
                                  skipped_bare_identifiers=10 unparsable_python_files=0 (rc=0)
```
作者称 `files=290` / secret `525` → **两个数都一致** ✅
（`untracked` 我这里是 0 而作者是 3：仅因我的沙箱把独占产物 `git add` 进了初始提交，计数口径差异，非问题。）

---

## 8.（任务 8）回归抽样 ≥5 —— 全通过

| 抽样项 | 结果 | 原始输出 |
|---|---|---|
| ① 接线 grep | base `src/` 命中 **0**；head **7** 处（`strategy_trader.py:22`、`signal_ticket.py:9`、`app.py:23/1089/1138/1139`、`scanner.py:24`）——与作者自述逐行相同 | `§` 本报告上文 |
| ② 真实路径 `last_scan.funnel` / `no_open_verdict` | 真跑 `scan()` 后状态文件里 `funnel`（含 `round_faults`/`detail_counts`）与 `no_open_verdict='blocked'` 均在（C1/C5/C7 多例） | `out/q2_n1_AFTER.txt` |
| ③ `ticket_skip_detail()` 真实路径 | 真跑执行器：倒挂→`ENTRY_ZONE_INVERTED`、非法周期→`TICKET_SHAPE_INVALID`、过期→`TICKET_EXPIRED`，均落 `detail_counts` | `out/q4_sampling.txt` §2 |
| ④ 跨进程消费 | **8 进程 × 5 轮，每轮恰好 1 `CONSUMED` / 7 `ALREADY_CONSUMED`**；盘上 5 张票全 consumed | `out/q4b_xproc.txt` |
| ⑤ 故障 vs no_edge | 自枚举 **31 个 `NATURE_FAULT` 码，无一产出 `no_edge`**；5 个真 `NO_EDGE` 码（除轮级 `NO_TICKETS_AVAILABLE`）仍 `no_edge`；混合仍 `no_edge`；纯范围码→`no_candidates/READINESS_BLOCKED` | `out/q4_sampling.txt` §1 |
| ⑥ 管理优先预算 | 极端参数 5×5 组下 `management_used + management_available >= management_reserve` 恒成立；预算 0→`RESEARCH_BUDGET_ZERO` 可见；研究池被吃光→`MANAGEMENT_PRIORITY_BUDGET`；`meets_management_demand()` 是**真判定**（demand 11 > 全池 10 → `shortfall=1` → `False`） | `out/q4_sampling.txt` §4 |
| ⑦ `position_class` 同源 | `sf.PositionClass is spm.PositionClass` → **True**（同一对象）；`POSITION_CLASS_*` 取自枚举 `.value` | `out/q4_sampling.txt` §4 |

（说明：⑥ 我第一次构造错了断言——`demand=5 ≤ 全池 10` 本就不该有 shortfall；改用 `demand > 全池` 后
`meets_management_demand()` 正确返回 `False`。失败的**不是**产品，是我的探针，已修正。）

---

## 9. 新发现汇总

| # | 级别 | 一句话 | 位置 | 阻塞? |
|---|---|---|---|---|
| **N6** | **P2（未登记·异常穿出执行器）** | 收尾 `save()` 只吞 `(LedgerLockTimeout, LedgerCorrupt)`；`consume()`/`release()` 只吞 `LedgerLockTimeout` ⇒ **`OSError`/`PermissionError` 等仍穿出整个执行器**，本轮 intents 全部作废、票据成孤儿。与已修 N3 同形、同后果，交付物**一字未登记** | `src/agents/strategy_trader.py:338-346`（save）、`:274`（consume 无 try）、`:312-314`（release 无 try）；对照 `strategy_funnel.py:1440/1524`（只吞 LockTimeout） | **是** |
| N7 | P3（文档口径） | README §六 N4 行写 round_id = `exec:<iso>`，**实际是 `exec-<iso>`** | `0c131f2/README.md` §六（N4 行） | 否 |
| N8 | P3（观察·既有） | 卡册损坏轮 `state.last_scan.status='ok'`（legacy 字段），故障只在 `funnel`/`no_open_verdict` 里；与既有"卡册缺失"路径同形，非 r3 引入 | `scanner.py` 状态文件写入处 | 否 |
| N9 | 观察 | `ec8fd1c/` 无独立 `SUPERSEDED.md`（仅 README 顶部横幅）；横幅已无歧义 | `patches/U08/ec8fd1c/` | 否 |

**没有发现**：故障被读成 `no_edge`（31 码 + 混合例，两侧都成立）、跨进程重复消费、
未登记的既有脚本新增失败（`NEW_FAIL=[]`）、N1 两类故障互相污染、补丁应用失败。
上一轮的 N1/N2/N3/N4/N5 **全部确认已处置**（N3 见 §1 的范围限定）。

---

## 10. 命令清单（可重跑）

```bash
W="C:/Users/Tao/Desktop/币安/mat-wt/reviews/U08/R4/r1"; S="$W/_scratch_Q"
PY="C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8 MTA_NO_PROXY=1

# 沙箱（全部落在我的工作区内；未写入 I/CD/任何其它 worktree）
git -C "$W" archive 509246f | tar -x -C "$S/tree_base"          # 集成 HEAD 树
cp -r "$S/tree_base/." "$S/tree_head/"; cp -r "$S/tree_base/." "$S/tree_r2/"
for f in src/core/strategy_funnel.py scripts/smoke_u08_strategy_funnel.py \
         tests/integration/test_strategy_funnel.py "docs/策略扫描原因码.md"; do
  cp "$W/$f" "$S/tree_head/$f"; cp "$W/$f" "$S/tree_r2/$f"; done
for T in tree_base tree_head tree_r2; do (cd "$S/$T" && git init -q . && git add -A -f \
  && git -c user.email=q@q -c user.name=q commit -qm base); done
(cd "$S/tree_head" && for d in "$W"/docs/dispatch/patches/U08/0c131f2/0*.diff; do git apply "$d"; done)
(cd "$S/tree_r2"   && for d in "$W"/docs/dispatch/patches/U08/ec8fd1c/0*.diff;  do git apply "$d"; done)  # 05 会失败=预期

# 探针（原始输出在 $S/out/）
"$PY" "$S/probes/q1_n3.py" "$S/tree_r2"   BEFORE > "$S/out/q1_n3_BEFORE.txt" 2>&1
"$PY" "$S/probes/q1_n3.py" "$S/tree_head" AFTER  > "$S/out/q1_n3_AFTER.txt"  2>&1
"$PY" "$S/probes/q1b_consume_escape.py" "$S/tree_head"      > "$S/out/q1b.txt" 2>&1
"$PY" "$S/probes/q2_n1.py" "$S/tree_r2"   BEFORE > "$S/out/q2_n1_BEFORE.txt" 2>&1
"$PY" "$S/probes/q2_n1.py" "$S/tree_head" AFTER  > "$S/out/q2_n1_AFTER.txt"  2>&1
"$PY" "$S/probes/q3_n4n5.py" "$S/tree_head"                 > "$S/out/q3_n4n5.txt" 2>&1
"$PY" "$S/probes/q4_sampling.py" "$S/tree_head"             > "$S/out/q4_sampling.txt" 2>&1
(cd "$S/probes" && "$PY" q4b_xproc.py "$S/tree_head")       > "$S/out/q4b_xproc.txt" 2>&1
"$PY" "$S/probes/q6_delta.py"                               > "$S/out/q6_delta_console.txt" 2>&1  # 约 13 分钟

# 补丁矩阵（仓库根，只读）
git -C "C:/Users/Tao/Desktop/币安/mat-wt/I" apply --check -v "$W"/docs/dispatch/patches/U08/0c131f2/0*.diff
# + 加 -c core.autocrlf=false -c core.eol=lf / -c core.autocrlf=true 各跑一遍

# 门禁
(cd "$S/tree_head" && "$PY" scripts/check_quality.py && "$PY" scripts/scan_tracked_secrets.py)
```

| 探针 | 主题 | 原始输出 |
|---|---|---|
| `q1_n3.py` + `q1b_consume_escape.py` | N3 前后对照 + 绕过矩阵 | `out/q1_n3_{BEFORE,AFTER}.txt`、`out/q1b.txt` |
| `q2_n1.py` | N1 主路径/漂移/空卡册安全性 | `out/q2_n1_{BEFORE,AFTER}.txt` |
| `q3_n4n5.py` | N4/N5 | `out/q3_n4n5.txt` |
| `q4_sampling.py` + `q4_xproc_worker.py` | 故障 vs no_edge、真实路径、预算、归属 | `out/q4_sampling.txt` |
| `q4b_xproc.py` | 跨进程 8×5 | `out/q4b_xproc.txt` |
| `q6_delta.py` | 全量 delta | `out/q6_delta_console.txt`、`out/delta_result.json`、`out/delta_{base,head}.log`、`out/delta_harness_{base,head}.txt` |
| — | 补丁矩阵 / 门禁 / blob 漂移 | `out/phase1_applycheck*.txt`、`out/phase1_build*.txt`、`out/phase0_blobdrift.txt`、`out/gate_*.txt` |

---

## 11. 未覆盖 / 无法离线验证

- **`OSError`/`PermissionError` 的真实触发概率**：我用 monkeypatch 确定性构造（也用了真实外部持锁进程）；
  真实磁盘满/文件被占用导致的频率**未测**，只能说明"路径存在、后果同 N3"。
- **生产端到端一轮**：`graph.py` 不注入 `funnel`/`ledger`（U12 边界），"真实一轮 17 道闸门的命中分布"
  与"跨进程去重在生产生效"仍无从观测 —— 这一条 r3 已如实归 U12，我确认其**仍未生效**。
- **`/funnel` 真实浏览器渲染**：未做视觉/交互核对（TestClient 级）。
- **账本与审计库勾稽的权威性**（`signal_ticket_has_order` 能否兜住"已消费未落库"窗口）：需 DB 数据，超只读范围。
- **真实集成合入**：我在自己的沙箱做（`tree_head`），未也不允许改动 `mat-wt/I`；
  沙箱与集成 HEAD 在该 5 文件上 blob 已逐文件核对相同，但真实合入仍应由 I 自己跑一次 harness。
- 全程**未联网**；`get_live_prices` 路径在探针里一律以显式 `prices=` 短路。
